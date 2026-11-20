"""Offline end-to-end coverage using an attributed, CC BY real-face fixture."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
import uuid
from unittest.mock import patch

from streamframe import analysis, jobs, render


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
PORTRAIT = FIXTURES / "official_portrait_barack_obama.jpg"
MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
FULL = Path("/opt/homebrew/opt/ffmpeg-full/bin")


def binary(name):
    directory = os.environ.get("STREAMFRAME_TEST_FFMPEG_DIR")
    if directory:
        return str(Path(directory) / name)
    if (FULL / name).is_file():
        return str(FULL / name)
    return shutil.which(name)


FFMPEG, FFPROBE = binary("ffmpeg"), binary("ffprobe")


@unittest.skipUnless(importlib.util.find_spec("cv2"), "OpenCV is not installed")
@unittest.skipUnless(FFMPEG and FFPROBE and MODEL.is_file(), "FFmpeg or YuNet model unavailable")
class RealFaceIntegrationTests(unittest.TestCase):
    def test_face_detection_render_and_caption_timing(self):
        import cv2
        import numpy as np

        work = FIXTURES / (".real-face-test-" + uuid.uuid4().hex)
        work.mkdir()
        try:
            source, output = work / "source.mp4", work / "output.mp4"
            subprocess.run([
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-loop", "1", "-framerate", "30", "-i", str(PORTRAIT),
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-filter_complex",
                "[0:v]scale=248:338[face];"
                "color=c=blue:s=640x480:r=30[bg];"
                "[bg][face]overlay=42:62:shortest=1[v]",
                "-map", "[v]", "-map", "1:a", "-t", "2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "pc",
                "-c:a", "aac", str(source),
            ], check=True, capture_output=True)
            source_metadata = json.loads(subprocess.check_output([
                FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(source)
            ]))
            source_video = next(s for s in source_metadata["streams"] if s["codec_type"] == "video")
            self.assertEqual(source_video["color_range"], "pc")

            detection = analysis.discover_facecam(
                source, ffmpeg_path=FFMPEG, duration=2, source_width=640,
                source_height=480, yunet_model_path=MODEL,
                sample_count=4,
            )
            self.assertTrue(detection["detected"], detection)
            self.assertGreaterEqual(detection["samples"], 2)
            self.assertEqual(detection["framing"], "webcam_boundary", detection)
            region = detection["box"]
            for key, expected in zip(("x", "y", "width", "height"), (42, 62, 248, 338)):
                self.assertAlmostEqual(region[key], expected, delta=8, msg=detection)
            box = detection["face_box"]
            self.assertGreater(region["width"], box["width"] * 2)
            self.assertGreater(region["height"], box["height"] * 2)

            filters = subprocess.run([FFMPEG, "-hide_banner", "-filters"],
                                     capture_output=True, text=True, check=True).stdout
            if not any(line.split()[1:2] == ["ass"] for line in filters.splitlines()):
                self.skipTest("FFmpeg lacks the libass filter")

            content_rect = {"x": 352 / 640, "y": 0, "width": 288 / 640, "height": 1}
            words = [
                {"start": 0.6, "end": 1.0, "text": "wrong", "correction": "hello"},
                {"start": 1.0, "end": 1.4, "text": "world"},
            ]
            media = analysis.probe_media(source, ffmpeg_path=FFMPEG)
            mocked_analysis = {
                "media": media, "facecam": detection,
                "transcript": {"words": [{k: w[k] for k in ("start", "end", "text")} for w in words]},
                "candidates": [{"start": 0.4, "end": 1.8, "score": 0.9}],
            }
            event = threading.Event()
            progress = []
            with patch.object(analysis, "analyze_media", return_value=mocked_analysis):
                adapted = jobs.analyze_media(source, lambda *args: progress.append(args), event)
            self.assertEqual(len(adapted["candidates"]), 1)
            candidate = adapted["candidates"][0]
            crop = candidate["face_crop"]
            self.assertIsNotNone(crop)
            x, y = crop["x"] * 640, crop["y"] * 480
            width, height = crop["width"] * 640, crop["height"] * 480
            for actual, expected in zip((x, y, width, height),
                                        (region["x"], region["y"], region["width"], region["height"])):
                self.assertAlmostEqual(actual, expected, delta=0.0001)
            self.assertIsNone(candidate["content_crop"])
            candidate["content_crop"] = content_rect
            candidate["words"][0]["correction"] = "hello"
            generated_ass = []
            original_ass = render._ass
            probed = []
            original_probe = render._probe

            def capture_probe(*args):
                result = original_probe(*args)
                probed.append(result)
                return result

            def capture_ass(captions):
                result = original_ass(captions)
                generated_ass.append(result)
                return result

            with patch.dict(os.environ, {"PATH": str(Path(FFMPEG).parent) + os.pathsep + os.environ.get("PATH", "")}), \
                    patch.object(render, "_ass", side_effect=capture_ass), \
                    patch.object(render, "_probe", side_effect=capture_probe):
                try:
                    jobs.render_candidate(source, candidate, output,
                                          lambda *args: progress.append(args), event)
                except render.RenderError as exc:
                    self.fail(f"{exc}; probed streams: {[p['streams'] for p in probed]}")
            self.assertIn("0:00:00.20,0:00:00.60", generated_ass[0])
            self.assertIn("0:00:00.60,0:00:01.00", generated_ass[0])
            self.assertIn("hello", generated_ass[0])
            self.assertNotIn("wrong", generated_ass[0])

            metadata = json.loads(subprocess.check_output([
                FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)
            ]))
            self.assertEqual({s["codec_name"] for s in metadata["streams"]}, {"h264", "aac"})
            video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
            self.assertEqual(video["pix_fmt"], "yuv420p")
            self.assertIn(video.get("color_range", "tv"), ("tv", "unknown"))
            self.assertAlmostEqual(float(metadata["format"]["duration"]), 1.4, delta=0.1)

            def frame_at(second):
                raw = subprocess.check_output([
                    FFMPEG, "-v", "error", "-ss", str(second), "-i", str(output),
                    "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"
                ])
                return np.frombuffer(raw, dtype=np.uint8).reshape((1920, 1080, 3))

            before, first, second, after = (frame_at(t) for t in (0.1, 0.4, 0.8, 1.2))
            detector = cv2.FaceDetectorYN.create(str(MODEL), "", (1080, 960), 0.6, 0.3, 5000)
            _, faces = detector.detect(cv2.cvtColor(first[:960], cv2.COLOR_RGB2BGR))
            self.assertIsNotNone(faces, "face is missing from rendered top panel")
            expected_x = (box["x"] - x) * 1080 / width
            expected_y = (box["y"] - y) * 960 / height
            expected_w = box["width"] * 1080 / width
            expected_h = box["height"] * 960 / height
            self.assertGreaterEqual(expected_x, 0)
            self.assertGreaterEqual(expected_y, 0)
            self.assertLessEqual(expected_x + expected_w, 1080)
            self.assertLessEqual(expected_y + expected_h, 960)

            def overlap(face):
                fx, fy, fw, fh = face[:4]
                ix = max(0, min(fx + fw, expected_x + expected_w) - max(fx, expected_x))
                iy = max(0, min(fy + fh, expected_y + expected_h) - max(fy, expected_y))
                return ix * iy / (fw * fh + expected_w * expected_h - ix * iy)

            self.assertGreater(max(overlap(face) for face in faces), 0.6)
            self.assertGreater(int(first[1200, 200, 2]), int(first[1200, 200, 0]) + 100)

            # ASS uses bright yellow for the active word. Inspect the caption
            # safe area: no highlight before/after, and moving yellow pixels
            # between the first and second word demonstrate timed switching.
            def yellow(frame):
                region = frame[1400:1780, 100:980].astype(np.int16)
                return (region[:, :, 0] > 170) & (region[:, :, 1] > 170) & (region[:, :, 2] < 100)

            self.assertLess(int(yellow(before).sum()), 20)
            self.assertGreater(int(yellow(first).sum()), 100)
            self.assertGreater(int(yellow(second).sum()), 100)
            self.assertLess(int(yellow(after).sum()), 20)
            self.assertGreater(float(np.where(yellow(second))[1].mean()),
                               float(np.where(yellow(first))[1].mean()) + 50)
        finally:
            shutil.rmtree(work)

    def test_full_frame_portrait_has_roomy_fallback_not_a_webcam_boundary(self):
        work = FIXTURES / (".real-face-test-" + uuid.uuid4().hex)
        work.mkdir()
        try:
            source = work / "borderless.mp4"
            subprocess.run([
                FFMPEG, "-v", "error", "-n", "-loop", "1", "-i", str(PORTRAIT),
                "-vf", "scale=294:400", "-t", "1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ], check=True, capture_output=True)
            result = analysis.discover_facecam(
                source, ffmpeg_path=FFMPEG, yunet_model_path=MODEL, sample_count=4,
            )
            self.assertEqual(result["framing"], "face_fallback", result)
            self.assertEqual(result["reason"], "webcam_boundary_uncertain")
            box, face = result["box"], result["face_box"]
            self.assertIsNotNone(jobs.padded_face_crop(box, 294, 400))
            for start, size, limit in (("x", "width", 294), ("y", "height", 400)):
                self.assertGreater(box[size], face[size] * 1.5)
                self.assertLessEqual(0, box[start])
                self.assertLessEqual(box[start], face[start])
                self.assertGreaterEqual(box[start] + box[size], face[start] + face[size])
                self.assertLessEqual(box[start] + box[size], limit)
        finally:
            shutil.rmtree(work)


if __name__ == "__main__":
    unittest.main()
