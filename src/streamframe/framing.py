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


def frame_rectangles(frame, faces, *, strengths=None):
    """Return plausible (score, left/top/right/bottom, face index) candidates.

    The frame must already be unpadded. A flush overlay may borrow up to two
    adjacent frame edges, but those edges do not count as visual evidence.
    """
    height, width = frame.shape[:2]
    if min(height, width) < 24 or not faces:
        return []
    if strengths is None:
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
            if support[index] >= 0.5 and all(abs(coord - previous) > 4 for previous in picked):
                picked.append(coord)
            if len(picked) == 7:
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
        inside = float(np.minimum(contrast, 160).mean())
        # A webcam seam ends at the corners. A long line continuing into
        # gameplay, or through the webcam background, is weaker evidence.
        extension = max(6, (end - start) // 4)
        outside = [
            float(np.minimum(edges[a:b, line - 1:line + 2].max(axis=1), 160).mean())
            for a, b in ((max(0, start - extension), max(0, start - 3)),
                         (min(edges.shape[0], end + 3), min(edges.shape[0], end + extension)))
            if b - a >= 4
        ]
        termination = max(0.0, inside - max(outside, default=inside))
        return coverage, (0.25 * coverage + 0.35 * inside / 160 + 0.4 * termination / 160)

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
        # A flush webcam can leave little or no space beyond the face.
        if x1 <= 5 * w:
            lefts.append(0)
        if width - x2 <= 5 * w:
            rights.append(width)
        if y1 <= 5 * h:
            tops.append(0)
        if height - y2 <= 5 * h:
            bottoms.append(height)
        side_scores = {}

        def side(axis, start, end, line):
            if line in (0, strengths[axis].shape[1]):
                return None
            key = (axis, start, end, line)
            if key not in side_scores:
                side_scores[key] = edge_score(strengths[axis], start, end, line)
            return side_scores[key]

        for left in lefts:
            for right in rights:
                rw = right - left
                if rw < max(14, w * 1.65) or rw == width:
                    continue
                for top in tops:
                    for bottom in bottoms:
                        rh = bottom - top
                        if (rh < max(14, h * 1.55) or rh == height or not 0.45 <= rw / rh <= 2.8
                                or rw * rh > width * height * 0.65):
                            continue
                        sides = (side(0, top, bottom, left), side(0, top, bottom, right),
                                 side(1, left, right, top), side(1, left, right, bottom))
                        visible = [edge for edge in sides if edge is not None]
                        flush = len(visible) < 4
                        if flush and rw * rh > width * height * 0.33:
                            continue
                        if all(coverage >= 0.62 for coverage, _ in visible):
                            score = sum(value for _, value in visible) / len(visible) - 0.025 * (4 - len(visible))
                            candidates.append((score, (left, top, right, bottom), index))
    return candidates


def _close_rectangles(a, b):
    tolerance = max(3, min(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1]) * 0.06)
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def stable_rectangle(observations, decoded_samples):
    """Find a rectangle repeated on distinct frames, or abstain on a close rival."""
    groups = []
    for score, rect, sample, face in sorted(observations, key=lambda item: -item[0]):
        group = next((g for g in groups if _close_rectangles(g[0], rect)), None)
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

    supported.sort(key=lambda matches: mean_score(matches) * (0.75 + 0.25 * len(matches) / decoded_samples),
                   reverse=True)
    best = supported[0]
    if any(len(rival) >= 0.8 * len(best) and mean_score(rival) >= mean_score(best) - 0.06
           for rival in supported[1:]):
        return None
    bounds = tuple(median(match[1][edge] for match in best) for edge in range(4))
    return bounds, [match[2] for match in best]


def temporal_rectangle(frames, seed, observations):
    """Recover a seam briefly hidden by similar colors outside the overlay."""
    if len(frames) < 4:
        return None
    strengths = tuple(np.median(np.stack(axis), axis=0) for axis in zip(*map(_edge_maps, frames)))
    admitted = []
    for score, rect, _ in frame_rectangles(frames[0], [seed], strengths=strengths):
        support = {sample: face for _, match, sample, face in observations if _close_rectangles(rect, match)}
        if score >= 0.45 and len(support) >= 2:
            admitted.append((score, rect, support))
    admitted.sort(key=lambda hit: hit[0], reverse=True)
    if not admitted:
        return None
    score, rect, support = admitted[0]
    if any(other_score >= score - 0.06 and not _close_rectangles(rect, other)
           for other_score, other, _ in admitted[1:]):
        return None
    return rect, list(support.values())
