import json
import http.server
import shutil
import subprocess
from tempfile import TemporaryDirectory
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

    def test_opening_title_uses_clip_time_and_treats_text_as_literal(self):
        ass = render._ass([], r"{\p1}The setup\N", 1.65)
        self.assertIn("Dialogue: 1,0:00:00.00,0:00:01.65,Opening", ass)
        self.assertIn(rf"\pos(540,{render.WEBCAM_HEIGHT + 5})", ass)
        self.assertIn("｛＼p1｝The setup＼N", ass)
        self.assertNotIn(r"{\p1}", ass)
        self.assertIn("0:00:02.40,Opening", render._ass([], "A short hook", 30))

    def test_captions_keep_fixed_phrases_and_do_not_reveal_phrases_across_a_pause(self):
        ass = render._ass([(0, 0.3, "First"), (0.32, 0.7, "phrase"), (2.8, 3.2, "Later")])
        first, second, later = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        self.assertIn(r"{\c&H00FFFF&}First{\c&HFFFFFF&} phrase", first)
        self.assertIn(r"First {\c&H00FFFF&}phrase{\c&HFFFFFF&}", second)
        self.assertNotIn("Later", first)
        self.assertNotIn("Later", second)
        self.assertIn("0:00:00.32,0:00:00.70", second)
        self.assertIn("0:00:02.80,0:00:03.20", later)
        self.assertNotIn("phrase", later)

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
            "-vf", "crop=2:2:540:360", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"
        ])
        self.assertGreater(pixels[0], pixels[2] + 100)
        screen = subprocess.check_output([
            "ffmpeg", "-v", "error", "-i", str(self.output), "-frames:v", "1",
            "-vf", "crop=2:2:540:740", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"
        ])
        self.assertGreater(screen[2], screen[0] + 100)
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

    def test_branded_render_replays_blurred_footage_without_repeating_audio(self):
        with TemporaryDirectory() as work:
            brand = Path(work)
            for name, color in (("watermark", "white@0.8"), ("outro", "magenta")):
                subprocess.run([
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color={color}:s=80x80,format=rgba",
                    "-frames:v", "1", "-threads", "1", "-update", "1", str(brand / f"{name}.png"),
                ], check=True)
            self.render(brand_dir=brand)

        def pixels(time):
            return subprocess.check_output([
                "ffmpeg", "-v", "error", "-ss", str(time), "-i", str(self.output),
                "-frames:v", "1", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ])

        def pixel(frame, x, y):
            offset = (y * 1080 + x) * 3
            return frame[offset:offset + 3]

        duration = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
            "-of", "csv=p=0", str(self.output),
        ], text=True)
        self.assertAlmostEqual(float(duration), 1.9, places=5)

        main, outro, settled = pixels(0), pixels(0.5), pixels(1.7)
        red, _, blue = pixel(main, 540, 360)
        self.assertGreater(red, blue + 100)
        padding = pixel(main, 20, 200)
        self.assertTrue(all(abs(value - expected) < 6 for value, expected in zip(padding, (20, 28, 34))))
        red, green, blue = pixel(main, 100, 732)
        self.assertGreater(red, 180)
        self.assertGreater(blue, green + 35)
        marked = pixel(main, 460, 794)
        clean = pixel(main, 420, 794)
        self.assertGreater(marked[0], clean[0] + 60)
        self.assertLess(marked[0], clean[0] + 150)
        self.assertGreater(pixel(outro, 540, 960)[0], 200)
        early_background = pixel(pixels(0.43), 540, 360)
        late_background = pixel(settled, 540, 360)
        self.assertGreater(early_background[0], late_background[0] + 8)
        self.assertGreater(late_background[1], early_background[1] + 4)
        self.assertLess(max(pixel(outro, 100, 800)), 130)
        for y in (1310, 1390, 1470):
            self.assertTrue(any(min(pixel(settled, x, row)) > 200
                                for x in range(422, 760) for row in range(y - 20, y + 20)))
        self.assertLess(max(pixel(outro, 358, 1470)), 60)
        self.assertGreater(min(pixel(settled, 358, 1470)), 200)
        self.assertLess(max(pixel(settled, 371, 1470)), 60)
        self.assertGreater(min(pixel(settled, 352, 1310)), 200)
        self.assertGreater(min(pixel(settled, 380, 1298)), 180)
        self.assertLess(max(pixel(settled, 371, 1310)), 60)
        from array import array
        peaks = []
        for start in (0.15, 0.38, 0.46, 1.7):
            audio = array("h", subprocess.check_output([
                "ffmpeg", "-v", "error", "-ss", str(start), "-i", str(self.output),
                "-t", "0.015", "-map", "0:a", "-f", "s16le", "-",
            ]))
            self.assertTrue(audio)
            peaks.append(max(map(abs, audio)))
        main_peak, fading_peak, early_outro, late_outro = peaks
        self.assertGreater(main_peak, 100)
        self.assertLess(0, fading_peak)
        self.assertLess(fading_peak, main_peak * 0.4)
        self.assertLess(max(early_outro, late_outro), 10)

    def test_branded_title_shows_inset_badge_and_never_extends_into_outro(self):
        with TemporaryDirectory() as work:
            brand = Path(work)
            for name in ("watermark", "outro"):
                subprocess.run([
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=magenta:s=80x80",
                    "-frames:v", "1", "-threads", "1", "-update", "1", str(brand / f"{name}.png"),
                ], check=True)
            self.render(brand_dir=brand, opening_title="iShowSpeed meets a Hulk-like monster in GTA 6",
                        include_audio=False)

        def pixel_at(time, x, y):
            return subprocess.check_output([
                "ffmpeg", "-v", "error", "-ss", str(time), "-i", str(self.output),
                "-frames:v", "1", "-vf", f"crop=2:2:{x}:{y}", "-threads", "1",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ])[:3]

        red, green, blue = pixel_at(0.1, 150, 700)
        self.assertGreater(min(red, blue), green + 120)
        red, green, blue = pixel_at(0.1, 280, 700)
        self.assertGreater(blue, red + 40)
        red, green, blue = pixel_at(0.45, 150, 700)
        self.assertLess(max(red, green, blue), 130)
        title = subprocess.check_output([
            "ffmpeg", "-v", "error", "-ss", "0.1", "-i", str(self.output),
            "-frames:v", "1", "-vf", "crop=680:160:280:650",
            "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ])
        rows = [y for y in range(160) if sum(
            min(title[offset:offset + 3]) > 220 for offset in range(y * 2040, (y + 1) * 2040, 3)
        ) > 5]
        self.assertGreaterEqual(rows[-1] - rows[0], 76)

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
            self.assertLess(max(padding), 45)
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
