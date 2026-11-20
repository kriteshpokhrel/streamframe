import cv2
import numpy as np

from streamframe.framing import frame_rectangles, stable_rectangle, temporal_rectangle


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


def test_webcam_flush_to_two_edges_requires_both_interior_seams():
    def observations(with_seams):
        for index in range(5):
            rng = np.random.default_rng(index)
            frame = rng.integers(30, 55, (180, 240, 3), dtype=np.uint8)
            if with_seams:
                frame[112:, :96] = (120, 110, 125)
            x = 26 + index * 6
            cv2.ellipse(frame, (x + 8, 142), (8, 10), 0, 0, 360, (180, 200, 220), -1)
            for score, rect, face in frame_rectangles(frame, [(x, 132, 16, 20)]):
                yield score, rect, index, face

    match = stable_rectangle(observations(True), 5)
    assert match is not None
    bounds, _ = match
    assert all(abs(actual - expected) <= 2 for actual, expected in zip(bounds, (0, 112, 96, 180)))
    assert stable_rectangle(observations(False), 5) is None


def test_temporal_seams_recover_a_flush_webcam_against_changing_gameplay():
    frames, observations = [], []
    seed = (34, 79, 16, 20)
    for index in range(8):
        frame = np.full((180, 240, 3), 30, dtype=np.uint8)
        frame[52:130, :96] = 110
        # A patch of matching gameplay obscures a different seam each time.
        if index < 6:
            axis = index % 3
            if axis == 0:
                frame[42:52, 16:60] = 110
            elif axis == 1:
                frame[72:108, 96:112] = 110
            else:
                frame[130:140, 16:60] = 110
        frames.append(frame)
        observations.extend((score, rect, index, 0)
                            for score, rect, _ in frame_rectangles(frame, [seed]))
    assert stable_rectangle(observations, len(frames)) is None
    bounds, _ = temporal_rectangle(frames, seed, observations)
    assert all(abs(actual - expected) <= 2 for actual, expected in zip(bounds, (0, 52, 96, 130)))
    assert temporal_rectangle(frames, seed, []) is None
