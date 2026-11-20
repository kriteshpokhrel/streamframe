"""Conservative, face-seeded rectangle inference in sampled video frames."""

from __future__ import annotations

import math
from statistics import median

import cv2
import numpy as np


_EDGE_THRESHOLD = 18


def _edge_maps(frame):
    # Compare short strips straddling a boundary in color, not just luma.
    # Index i denotes the boundary between pixels i-1 and i.
    def across_x(image):
        soft = cv2.blur(image.astype(np.float32), (1, 5))
        delta = (soft[:, 2:-1] + soft[:, 3:]) - (soft[:, :-3] + soft[:, 1:-2])
        strength = np.abs(delta).max(axis=2) / 2
        return np.pad(strength, ((0, 0), (2, 1)))

    return across_x(frame), across_x(frame.transpose(1, 0, 2))


def frame_rectangles(frame, faces):
    """Return plausible (score, left/top/right/bottom, face index) candidates.

    The frame must already be unpadded. All four edges need visible support;
    neither the canvas boundary nor FFmpeg's letterbox counts as evidence.
    """
    height, width = frame.shape[:2]
    if min(height, width) < 24 or not faces:
        return []
    strengths = _edge_maps(frame)

    def positions(axis, start, end, lower, upper):
        edges = strengths[axis]
        coords = np.arange(max(2, lower), min(edges.shape[1] - 1, upper))
        if not len(coords):
            return []
        strip = edges[start:end, coords]
        support = (strip >= _EDGE_THRESHOLD).mean(axis=0)
        strength = np.minimum(strip, 120).mean(axis=0)
        picked = []
        for index in np.argsort(-strength, kind="stable"):
            coord = int(coords[index])
            if support[index] >= 0.65 and all(abs(coord - previous) > 4 for previous in picked):
                picked.append(coord)
            if len(picked) == 5:
                break
        return picked

    def edge_score(edges, start, end, line):
        # Allow a one-pixel wobble from resampling/compression. Trim corners,
        # but require support in both halves rather than one long fragment.
        trim = max(1, (end - start) // 12)
        contrast = edges[start + trim:end - trim, line - 1:line + 2].max(axis=1)
        along = contrast >= _EDGE_THRESHOLD
        middle = len(along) // 2
        coverage = min(float(along[:middle].mean()), float(along[middle:].mean()))
        strength = float(np.minimum(contrast, 160).mean()) / 160
        return coverage, 0.6 * coverage + 0.4 * strength

    candidates = []
    for index, (x, y, w, h) in enumerate(faces):
        x1, y1 = max(0, math.floor(x)), max(0, math.floor(y))
        x2, y2 = min(width, math.ceil(x + w)), min(height, math.ceil(y + h))
        if min(x2 - x1, y2 - y1) < 5:
            continue
        margin_x, margin_y = max(2, round(w * 0.12)), max(2, round(h * 0.12))
        lefts = positions(0, y1, y2, x1 - math.ceil(5 * w), x1 - margin_x + 1)
        rights = positions(0, y1, y2, x2 + margin_x, x2 + math.ceil(5 * w) + 1)
        tops = positions(1, x1, x2, y1 - math.ceil(5 * h), y1 - margin_y + 1)
        bottoms = positions(1, x1, x2, y2 + margin_y, y2 + math.ceil(5 * h) + 1)
        side_scores = {}

        def side(axis, start, end, line):
            key = (axis, start, end, line)
            if key not in side_scores:
                side_scores[key] = edge_score(strengths[axis], start, end, line)
            return side_scores[key]

        for left in lefts:
            for right in rights:
                rw = right - left
                if rw < max(14, w * 1.65):
                    continue
                for top in tops:
                    for bottom in bottoms:
                        rh = bottom - top
                        if (rh < max(14, h * 1.55) or not 0.45 <= rw / rh <= 2.8
                                or rw * rh > width * height * 0.65):
                            continue
                        sides = (side(0, top, bottom, left), side(0, top, bottom, right),
                                 side(1, left, right, top), side(1, left, right, bottom))
                        if all(coverage >= 0.72 for coverage, _ in sides):
                            score = min(value for _, value in sides)
                            candidates.append((score, (left, top, right, bottom), index))
    return candidates


def stable_rectangle(observations, decoded_samples):
    """Find a rectangle repeated on distinct frames, or abstain on a close rival."""
    groups = []

    def close(a, b):
        tolerance = max(3, min(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1]) * 0.06)
        return all(abs(x - y) <= tolerance for x, y in zip(a, b))

    for score, rect, sample, face in sorted(observations, key=lambda item: -item[0]):
        group = next((g for g in groups if close(g[0], rect)), None)
        if group is None:
            group = (rect, {})
            groups.append(group)
        group[1].setdefault(sample, (score, rect, face))

    required = max(2, math.ceil(decoded_samples * 0.6))
    supported = [list(matches.values()) for _, matches in groups if len(matches) >= required]
    if not supported:
        return None

    def mean_score(matches):
        return sum(match[0] for match in matches) / len(matches)

    supported.sort(key=lambda matches: (len(matches), mean_score(matches)), reverse=True)
    best = supported[0]
    if any(len(rival) >= 0.8 * len(best) and mean_score(rival) >= mean_score(best) - 0.06
           for rival in supported[1:]):
        return None
    bounds = tuple(median(match[1][edge] for match in best) for edge in range(4))
    return bounds, [match[2] for match in best]


def roomy_face_box(face, width, height):
    """Estimate head-and-shoulders framing, not a webcam boundary."""
    x, y, w, h = (face[key] for key in ("x", "y", "width", "height"))
    # Keep an estimate exportable in the existing 9:8 face panel when possible.
    # A verified webcam rectangle, unlike an estimate, must not be shrunk to fit.
    panel_aspect = 9 / 8
    crop_w = min(2.3 * w, width, height * panel_aspect)
    crop_h = min(2.5 * h, height, width / panel_aspect)
    if crop_w < w or crop_h < h:
        return face
    left = min(max(0, x + (w - crop_w) / 2), width - crop_w)
    top = min(max(0, y + h * 0.6 - crop_h / 2), height - crop_h)
    return {"x": left, "y": top, "width": crop_w, "height": crop_h}
