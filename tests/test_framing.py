import cv2
import numpy as np

from streamframe.framing import frame_rectangles, stable_rectangle


def _sample(index, left, *, border=True):
    rng = np.random.default_rng(index)
    frame = rng.integers(30, 55, (180, 240, 3), dtype=np.uint8)
    top, right, bottom = 52, left + 92, 130
    color = (220, 220, 220)
    if border:
        cv2.rectangle(frame, (left, top), (right - 1, bottom - 1), color, 3)
        frame[top + 3:bottom - 3, left + 3:right - 3] = (120, 110, 125)
    # A moving face seed inside the overlay; a stronger face could be elsewhere.
    x, y = left + 27 + index * 6, 73 + index
    cv2.ellipse(frame, (x + 8, y + 10), (8, 10), 0, 0, 360, (180, 200, 220), -1)
    return frame, [(185, 20, 20, 25), (x, y, 16, 20)]


def _observations(layouts):
    return [
        (score, rect, index, face)
        for index, (left, border) in enumerate(layouts)
        for score, rect, face in frame_rectangles(*_sample(index, left, border=border))
    ]


def test_stable_region_survives_face_motion_and_one_missing_border():
    match = stable_rectangle(_observations([(40, True)] * 4 + [(40, False)]), 5)
    assert match is not None
    bounds, faces = match
    assert faces == [1] * 4
    assert all(abs(actual - expected) <= 5 for actual, expected in zip(bounds, (40, 52, 132, 130)))


def test_moving_overlay_and_single_sample_do_not_confirm_a_boundary():
    assert stable_rectangle(_observations([(15, True), (35, True), (55, True), (75, True)]), 4) is None
    assert stable_rectangle(_observations([(40, True)]), 1) is None


def test_equally_supported_different_rectangles_are_ambiguous():
    observations = [
        (0.9, rect, index, None)
        for index in range(4)
        for rect in ((5, 15, 75, 65), (80, 105, 165, 162))
    ]
    assert stable_rectangle(observations, 4) is None
