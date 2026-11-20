import json
import http.server
import shutil
import subprocess
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from streamframe import render


ROOT = Path(__file__).resolve().parent


class CaptionTests(unittest.TestCase):
    def test_ffprobe_uses_restricted_input_options(self):
        with patch.object(render, "_run", return_value='{"streams": []}') as run:
            render._probe(Path("input.mp4"), "ffprobe", None)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file,pipe")
        allowed = args[args.index("-format_whitelist") + 1].split(",")
        self.assertIn("mov", allowed)
        for unsafe in ("hls", "dash", "concat"):
            self.assertNotIn(unsafe, allowed)
        self.assertEqual(args[args.index("-enable_drefs") + 1], "0")
        self.assertEqual(args[args.index("-use_absolute_path") + 1], "0")

    def test_caption_corrections_clipping_and_ass_injection(self):
        words = [
            {"start": 0.8, "end": 1.2, "text": "wrong", "correction": "right"},
            {"start": 1.2, "end": 1.7, "text": r"{\p1}bad\Nline"},
            {"start": 2.0, "end": 2.2, "text": "outside"},
        ]
        clipped = render._words(words, 1, 2)
        self.assertEqual(clipped[0], (0, 0.19999999999999996, "right"))
        ass = render._ass(clipped)
        self.assertIn("0:00:00.00,0:00:00.20", ass)
        self.assertIn(r"{\c&H00FFFF&}right", ass)
        self.assertIn("｛＼p1｝bad＼Nline", ass)
        self.assertNotIn(r"{\p1}", ass)
        self.assertNotIn("wrong", ass)
        self.assertNotIn("outside", ass)

    def test_invalid_inputs(self):
        for rectangle in [(0, 0, 0, 1), (-0.1, 0, 1, 1), (0.5, 0, 0.6, 1), (0, 0, float("nan"), 1)]:
            with self.subTest(rectangle=rectangle), self.assertRaises(ValueError):
                render._crop(rectangle, "crop")
        for word in [
            {"start": 0, "end": 0, "text": "x"},
            {"start": 0, "end": 1, "text": "x", "correction": 123},
            {"start": float("inf"), "end": 2, "text": "x"},
        ]:
            with self.subTest(word=word), self.assertRaises(ValueError):
                render._words([word], 0, 1)

    def test_render_command_uses_safe_subtitle_basename_and_preserves_audio(self):
        output = ROOT / f"capture-{uuid.uuid4().hex}.mp4"
        seen = []
        source_info = {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                       "format": {"duration": "2", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}
        output_info = {"streams": [{"codec_type": "video", "codec_name": "h264",
                                    "width": 1080, "height": 1920, "pix_fmt": "yuv420p",
                                    "duration": "1"},
                                   {"codec_type": "audio", "codec_name": "aac"}],
                       "format": {"duration": "1"}}

        def fake_run(args, cancel, *, cwd=None):
            seen.append(args)
            if "-filter_complex" in args:
                graph = args[args.index("-filter_complex") + 1]
                ass_name = graph.split("ass=filename=")[1].split("[captioned]")[0].strip("'")
                self.assertEqual(Path(ass_name).name, ass_name)
                self.assertIn("corrected", (cwd / ass_name).read_text())
                Path(args[-1]).write_bytes(b"fake")
            return ""

        try:
            with patch.object(render, "_probe", side_effect=[source_info, output_info]), \
                    patch.object(render, "_run", side_effect=fake_run):
                render.render_clip("source.mp4", output, start=0, end=1,
                                   facecam_crop=(0, 0, 1, 1), content_crop=(0, 0, 1, 1),
                                   words=[{"start": 0, "end": 1, "text": "original",
                                           "correction": "corrected"}])
            command = seen[0]
            self.assertIn("0:a:0", command)
            self.assertNotIn("-af", command)
            self.assertIn("-xerror", seen[1])
            for args in seen:
                self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file,pipe")
                self.assertIn("-format_whitelist", args)
                self.assertEqual(args[args.index("-enable_drefs") + 1], "0")
                self.assertEqual(args[args.index("-use_absolute_path") + 1], "0")
                self.assertLess(args.index("-protocol_whitelist"), args.index("-i"))
        finally:
            output.unlink(missing_ok=True)

    def test_candidate_adapter_converts_crops_and_cancellation(self):
        candidate = {"start": 1, "end": 2, "words": [],
                     "face_crop": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}}
        progress = []
        from threading import Event
        cancelled = Event()
        with patch.object(render, "render_clip", return_value=Path("result")) as mocked:
            result = render.render_candidate("source", candidate, "output",
                                             lambda value, message: progress.append(value), cancelled)
        self.assertEqual(result, Path("result"))
        self.assertEqual(mocked.call_args.kwargs["facecam_crop"], (0.1, 0.2, 0.3, 0.4))
        self.assertEqual(mocked.call_args.kwargs["content_crop"], (0, 0, 1, 1))
        self.assertFalse(mocked.call_args.kwargs["cancel"]())
        self.assertEqual(progress, [0, 1])

    def test_invalid_export_is_not_published(self):
        output = ROOT / f"output-{uuid.uuid4().hex}.mp4"
        partials = []
        source_info = {"streams": [{"codec_type": "video"}], "format": {"duration": "2"}}
        invalid_output = {"streams": [{"codec_type": "video", "codec_name": "h264",
                                       "width": 10, "height": 10, "pix_fmt": "yuv420p"}]}

        def fake_run(args, cancel, *, cwd=None):
            partials.append(Path(args[-1]))
            partials[-1].write_bytes(b"partial")
            return ""

        try:
            output.write_bytes(b"existing")
            with patch.object(render, "_probe", side_effect=[source_info, invalid_output]), \
                    patch.object(render, "_run", side_effect=fake_run), \
                    self.assertRaises(render.RenderError):
                render.render_clip("source.mp4", output, start=0, end=1,
                                   facecam_crop=(0, 0, 1, 1), content_crop=(0, 0, 1, 1))
            self.assertEqual(output.read_bytes(), b"existing")
            self.assertTrue(partials)
            self.assertFalse(partials[0].exists())
        finally:
            output.unlink(missing_ok=True)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
class FFmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = ROOT / f"fixture-{uuid.uuid4().hex}.mp4"
        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=red:s=320x240:r=30:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
            "-vf", "drawbox=x=160:y=0:w=160:h=240:color=blue:t=fill,"
                   "drawbox=x=0:y=0:w=160:h=240:color=green:t=fill:enable='gte(t,0.5)'",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(cls.source),
        ], capture_output=True, text=True)
        if result.returncode:
            cls.source.unlink(missing_ok=True)
            raise unittest.SkipTest(f"FFmpeg fixture encoder unavailable: {result.stderr}")

    @classmethod
    def tearDownClass(cls):
        cls.source.unlink(missing_ok=True)

    def setUp(self):
        self.output = ROOT / f"output-{uuid.uuid4().hex}.mp4"

    def tearDown(self):
        self.output.unlink(missing_ok=True)

    def render(self, **kwargs):
        options = dict(start=0.2, end=0.6, facecam_crop=(0, 0, 0.5, 1),
                       content_crop=(0.5, 0, 0.5, 1))
        options.update(kwargs)
        return render.render_clip(self.source, self.output, **options)

    def test_actual_split_clip_with_audio_and_full_decode(self):
        self.assertEqual(self.render(), self.output)
        info = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(self.output)
        ]))
        self.assertAlmostEqual(float(info["format"]["duration"]), 0.4, delta=0.1)
        self.assertEqual({s["codec_name"] for s in info["streams"]}, {"h264", "aac"})
        pixels = subprocess.check_output([
            "ffmpeg", "-v", "error", "-i", str(self.output), "-frames:v", "1",
            "-vf", "scale=2:2", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"
        ])
        self.assertGreater(pixels[0], pixels[2] + 100)
        self.assertGreater(pixels[8], pixels[6] + 100)
        data = self.output.read_bytes()
        self.assertLess(data.index(b"moov"), data.index(b"mdat"))

    def test_bounded_output_is_validated_and_never_replaces_existing_on_limit(self):
        self.render(max_output_bytes=4 * 1024 * 1024)
        original = self.output.read_bytes()
        with self.assertRaises((render.RenderError, ValueError)):
            self.render(max_output_bytes=1000)
        self.assertEqual(self.output.read_bytes(), original)
        with self.assertRaises(ValueError):
            self.render(max_output_bytes=True)

    def test_wide_webcam_preserves_both_edges_with_padding(self):
        source = ROOT / f"webcam-edges-{uuid.uuid4().hex}.mp4"
        try:
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                "-i", "color=red:s=320x180:r=10:d=0.5",
                "-vf", "drawbox=x=0:y=0:w=24:h=180:color=green:t=fill,"
                       "drawbox=x=296:y=0:w=24:h=180:color=blue:t=fill",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ], check=True)
            render.render_clip(source, self.output, start=0, end=0.4,
                               facecam_crop=(0, 0, 1, 1), content_crop=(0, 0, 1, 1))
            def pixel(x, y):
                return subprocess.check_output([
                    "ffmpeg", "-v", "error", "-i", str(self.output), "-frames:v", "1",
                    "-vf", f"crop=10:10:{x}:{y},scale=1:1",
                    "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"
                ])
            left, right, padding = pixel(20, 480), pixel(1050, 480), pixel(500, 20)
            self.assertGreater(left[1], left[0] + 50)
            self.assertGreater(right[2], right[0] + 100)
            self.assertLess(max(padding), 20)
        finally:
            source.unlink(missing_ok=True)

    def test_seek_starts_at_requested_source_time(self):
        self.render(start=0.6, end=0.9, include_audio=False)
        pixels = subprocess.check_output([
            "ffmpeg", "-v", "error", "-i", str(self.output), "-frames:v", "1",
            "-vf", "scale=2:2", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"
        ])
        self.assertGreater(pixels[1], pixels[0] + 50)

    def test_actual_silent_output(self):
        self.render(include_audio=False)
        info = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(self.output)
        ]))
        self.assertEqual([s["codec_type"] for s in info["streams"]], ["video"])

    def test_source_without_audio(self):
        source = ROOT / f"silent-{uuid.uuid4().hex}.mp4"
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(self.source),
                            "-map", "0:v:0", "-c", "copy", str(source)], check=True)
            render.render_clip(source, self.output, start=0, end=0.3,
                               facecam_crop=(0, 0, 1, 1), content_crop=(0, 0, 1, 1))
            info = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(self.output)
            ]))
            self.assertEqual([s["codec_type"] for s in info["streams"]], ["video"])
        finally:
            source.unlink(missing_ok=True)

    def test_full_range_input_converts_to_limited_range(self):
        source = ROOT / f"full-range-{uuid.uuid4().hex}.mp4"
        try:
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-i", str(self.source),
                "-vf", "scale=in_range=tv:out_range=full", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-color_range", "pc", "-an", str(source),
            ], check=True)
            input_stream = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)
            ]))["streams"][0]
            self.assertEqual(input_stream["color_range"], "pc")
            render.render_clip(source, self.output, start=0, end=0.3,
                               facecam_crop=(0, 0, 1, 1), content_crop=(0, 0, 1, 1))
            output_stream = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(self.output)
            ]))["streams"][0]
            self.assertIn(output_stream.get("color_range", "tv"), ("tv", "unknown"))
            self.assertEqual(output_stream["pix_fmt"], "yuv420p")
            pixel = subprocess.check_output([
                "ffmpeg", "-v", "error", "-i", str(self.output), "-frames:v", "1",
                "-vf", "crop=100:100:100:200,scale=1:1",
                "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"
            ])
            self.assertGreater(pixel[0], 200)
            self.assertLess(pixel[1], 40)
            self.assertLess(pixel[2], 40)
        finally:
            source.unlink(missing_ok=True)

    def test_matroska_input_with_mov_reference_options(self):
        source = ROOT / f"source-{uuid.uuid4().hex}.mkv"
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(self.source),
                            "-map", "0:v:0", "-c", "copy", str(source)], check=True)
            render.render_clip(source, self.output, start=0, end=0.3,
                               facecam_crop=(0, 0, 1, 1), content_crop=(0, 0, 1, 1))
            self.assertTrue(self.output.is_file())
        finally:
            source.unlink(missing_ok=True)

    def test_actual_subtitles_when_libass_is_available(self):
        filters = subprocess.check_output(["ffmpeg", "-hide_banner", "-filters"], text=True)
        if not any(line.split()[1:2] == ["ass"] for line in filters.splitlines()):
            self.skipTest("FFmpeg was built without libass")
        self.render(words=[{"start": 0.1, "end": 0.4, "text": "hello", "correction": "fixed"},
                           {"start": 0.4, "end": 0.7, "text": "world"}])
        self.assertGreater(self.output.stat().st_size, 0)

    def test_cancellation_and_invalid_clip_preserve_existing_output(self):
        self.output.write_bytes(b"existing")
        with self.assertRaises(render.RenderCancelled):
            self.render(cancel=lambda: True)
        self.assertEqual(self.output.read_bytes(), b"existing")
        with self.assertRaises(ValueError):
            self.render(end=3)
        self.assertEqual(self.output.read_bytes(), b"existing")

    def test_missing_encoder_failure_preserves_existing_output(self):
        self.output.write_bytes(b"existing")
        with self.assertRaises(render.RenderError):
            self.render(ffmpeg="missing-streamframe-ffmpeg")
        self.assertEqual(self.output.read_bytes(), b"existing")

    def test_playlists_cannot_open_network_or_local_references(self):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(404)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        playlist = ROOT / f"playlist-{uuid.uuid4().hex}.mp4"
        try:
            url = f"http://127.0.0.1:{server.server_port}/private.ts"
            playlist.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{url}\n")
            with self.assertRaises(render.RenderError):
                render._probe(playlist, "ffprobe", None)
            with self.assertRaises(render.RenderError):
                render._run(["ffmpeg", "-v", "error", *render._INPUT_OPTIONS,
                             "-i", str(playlist), "-f", "null", "-"], None)
            self.assertEqual(requests, [])
            playlist.write_text(f"ffconcat version 1.0\nfile '{self.source}'\n")
            with self.assertRaises(render.RenderError):
                render._probe(playlist, "ffprobe", None)
        finally:
            playlist.unlink(missing_ok=True)
            server.shutdown()
            server.server_close()
            thread.join()

    def test_caption_filter_failure_preserves_existing_output(self):
        filters = subprocess.check_output(["ffmpeg", "-hide_banner", "-filters"], text=True)
        if any(line.split()[1:2] == ["ass"] for line in filters.splitlines()):
            self.skipTest("FFmpeg supports libass")
        self.output.write_bytes(b"existing")
        with self.assertRaises(render.RenderError):
            self.render(words=[{"start": 0.2, "end": 0.4, "text": "spoken"}])
        self.assertEqual(self.output.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
