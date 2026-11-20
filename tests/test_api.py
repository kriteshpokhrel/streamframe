"""Run with python -m unittest discover -s tests -p test_api.py."""

import asyncio
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from streamframe.jobs import (
    Cancelled, Conflict, DownloadProxy, JobStore, analyze_media, hls_segments, padded_face_crop,
    public_target, resolve_video, source_range, validate_remote_source,
    validate_candidate,
)

try:
    import httpx
    from streamframe.server import create_app
except ImportError:
    create_app = None


def candidate():
    return dict(id="clip1", start=1.0, end=3.0, score=0.8, evidence=["speech"],
                words=[dict(start=1.0, end=1.5, text="Hello")],
                face_crop=None, content_crop=None)


def analyze(path, progress, cancelled):
    progress(0.5, "Analyzing")
    return [candidate()]


def render(path, item, output, progress, cancelled):
    Path(output).write_bytes(b"rendered")
    progress(1, "Rendered")


class Workspace(unittest.TestCase):
    def setUp(self):
        # Deliberately keep every fixture inside the repository, not a temp dir.
        self.workspace = Path.cwd() / (".api-test-" + uuid.uuid4().hex)
        self.workspace.mkdir()
        self.source = self.workspace / "input.mp4"
        self.source.write_bytes(b"preview-media")

    def tearDown(self):
        shutil.rmtree(self.workspace)


class StoreTests(Workspace):
    def wait(self, store, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with store.lock:
                if job_id not in store.active:
                    return store.get(job_id)
            time.sleep(0.01)
        self.fail("Worker did not finish")

    def test_persistence_preview_edit_and_export(self):
        root = self.workspace / "data"
        store = JobStore(root, [self.workspace], analyze, render)
        try:
            job = store.create({"type": "file", "path": str(self.source)})
            job = self.wait(store, job["id"])
            self.assertEqual(job["status"], "succeeded")
            self.assertEqual(store.media_path(job["id"]).read_bytes(), b"preview-media")
            store.edit(job["id"], "clip1", {"end": 4})
            store.export(job["id"], "clip1")
            job = self.wait(store, job["id"])
            self.assertEqual(job["status"], "succeeded")
            self.assertEqual(store.media_path(job["id"], job["exports"][0]["filename"]).read_bytes(), b"rendered")
        finally:
            store.close()
        reopened = JobStore(root, [self.workspace], analyze, render)
        try:
            self.assertEqual(reopened.get(job["id"])["candidates"][0]["end"], 4)
        finally:
            reopened.close()

    def test_cancel_resume_and_failure(self):
        entered = threading.Event()

        def blocking(path, progress, cancelled):
            entered.set()
            cancelled.wait(3)
            progress(0.5, "stop")

        store = JobStore(self.workspace / "data", [self.workspace], blocking, render)
        try:
            job = store.create({"type": "file", "path": str(self.source)})
            self.assertTrue(entered.wait(2))
            store.cancel(job["id"])
            self.assertEqual(self.wait(store, job["id"])["status"], "cancelled")
            store.analyzer = analyze
            store.resume(job["id"])
            self.assertEqual(self.wait(store, job["id"])["status"], "succeeded")
            store.analyzer = lambda *args: (_ for _ in ()).throw(RuntimeError("analysis unavailable"))
            bad = store.create({"type": "file", "path": str(self.source)})
            failed = self.wait(store, bad["id"])
            self.assertEqual(failed["status"], "failed")
            self.assertIn("analysis unavailable", failed["error"])
        finally:
            store.close()

    def test_restart_marks_running_jobs_interrupted(self):
        root = self.workspace / "data"
        store = JobStore(root, [self.workspace], analyze, render)
        job = store.create({"type": "file", "path": str(self.source)})
        self.wait(store, job["id"])
        store.update(job["id"], status="running")
        store.close()
        reopened = JobStore(root, [self.workspace], analyze, render)
        try:
            self.assertEqual(reopened.get(job["id"])["status"], "interrupted")
        finally:
            reopened.close()

    def test_validation(self):
        store = JobStore(self.workspace / "data", [self.workspace / "allowed"], analyze, render)
        try:
            with self.assertRaises(ValueError):
                store.create({"type": "file", "path": str(self.source)})
            with self.assertRaises(KeyError):
                store.media_path("../escape", "../../input.mp4")
            for patch_value in (
                {"end": float("nan")}, {"start": -1}, {"face_crop": {"x": 2}},
                {"words": [{"start": 2, "end": 3, "text": "later"},
                           {"start": 1, "end": 2, "text": "earlier"}]},
            ):
                with self.assertRaises(ValueError):
                    validate_candidate({**candidate(), **patch_value})
        finally:
            store.close()

    def test_source_range_validation(self):
        self.assertEqual(source_range({}), {})
        self.assertEqual(source_range({"end": 42}), {"start": 0, "end": 42})
        self.assertEqual(source_range({"start": 42}), {"start": 42, "end": 1842})
        for bounds in ({"start": True}, {"start": -1}, {"start": 10**400},
                       {"end": float("inf")}, {"end": None}, {"end": 1801},
                       {"start": 3, "end": 3}, {"start": 3, "end": 2}):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                source_range(bounds)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
    def test_real_range_import_seeks_large_source_and_resumes_from_trim(self):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        supports = subprocess.run([ffmpeg, "-hide_banner", "-protocols"],
                                  check=True, capture_output=True, text=True, timeout=10)
        if "fd" not in supports.stdout.split():
            self.skipTest("FFmpeg seekable fd input unavailable")
        subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=s=320x240:r=20", "-t", "8", "-c:v", "libx264",
             "-preset", "ultrafast", str(self.source)],
            check=True, capture_output=True, timeout=20,
        )

        def fail(*args):
            raise RuntimeError("Stop after importing preview")

        root = self.workspace / "data"
        store = JobStore(root, [self.workspace], fail, render)
        try:
            maximum = self.source.stat().st_size - 1
            with patch("streamframe.jobs.MAX_MEDIA_BYTES", maximum), patch.dict(
                os.environ, {"STREAMFRAME_FFMPEG_PATH": ffmpeg}
            ):
                with self.assertRaises(ValueError):
                    store.create({"type": "file", "path": str(self.source)})
                job = store.create({"type": "file", "path": str(self.source),
                                    "start": 4.25, "end": 5.75})
                job = self.wait(store, job["id"])
            self.assertIn("Stop after importing preview", job["error"])
            self.assertEqual((job["source"]["start"], job["source"]["end"]), (4.25, 5.75))
            preview = store.media_path(job["id"])
            self.assertEqual(preview.name, "source.mp4")
            self.assertLess(preview.stat().st_size, maximum)
            timing = subprocess.check_output(
                [ffprobe, "-v", "error", "-show_entries", "format=start_time,duration",
                 "-of", "csv=p=0", str(preview)], text=True, timeout=10,
            )
            start, duration = map(float, timing.strip().split(","))
            self.assertAlmostEqual(start, 0)
            self.assertAlmostEqual(duration, 1.5, delta=0.05)
            self.source.unlink()
            store.close()
            store = JobStore(root, [self.workspace], analyze, render)
            store.resume(job["id"])
            resumed = self.wait(store, job["id"])
            self.assertEqual(resumed["status"], "succeeded", resumed)
            self.assertEqual(store.media_path(job["id"]), preview)
        finally:
            store.close()

    def test_cancelled_trim_cleans_partial_file_and_resumes_same_bounds(self):
        entered = threading.Event()
        calls = []

        class Process:
            returncode = None

            def __init__(self, command, **kwargs):
                calls.append((command, kwargs))
                Path(command[-1]).write_bytes(b"partial")
                if len(calls) == 1:
                    entered.set()
                else:
                    self.returncode = 0

            def poll(self):
                return self.returncode

        store = JobStore(self.workspace / "data", [self.workspace], analyze, render)
        try:
            with patch("streamframe.jobs.subprocess.Popen", Process), patch("streamframe.jobs.stop_process") as stop:
                job = store.create({"type": "file", "path": str(self.source), "end": 10})
                self.assertTrue(entered.wait(2))
                self.assertEqual(store.cancel(job["id"])["stage"], "trim")
                self.assertEqual(self.wait(store, job["id"])["status"], "cancelled")
                stop.assert_called_once()
                self.assertEqual(list((store.root / job["id"]).iterdir()), [])
                store.resume(job["id"])
                self.assertEqual(self.wait(store, job["id"])["status"], "succeeded")
            for command, options in calls:
                self.assertEqual(command[command.index("-ss") + 1], "0.000000")
                self.assertEqual(command[command.index("-t") + 1], "10.000000")
                self.assertEqual(command[command.index("-protocol_whitelist") + 1], "fd")
                self.assertNotIn(str(self.source), command)
                self.assertTrue(options["start_new_session"])
                self.assertEqual(len(options["pass_fds"]), 1)
        finally:
            store.close()

    def test_private_urls_and_rebinding_blocked(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1", "http://[::1]",
                    "http://[64:ff9b::a00:1]",
                    "http://169.254.169.254", "http://user:pass@example.com", "http://example.com:22"):
            with self.assertRaises(ValueError, msg=url):
                public_target(url)
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=answers):
            with self.assertRaises(ValueError):
                public_target("https://example.com")

    def test_analysis_adapter_uses_documented_public_schema(self):
        payload = {
            "status": "ok", "media": {"width": 1000, "height": 500},
            "transcript": {"words": [
                {"start": 1, "end": 2, "text": "Hi", "probability": 0.9},
                {"start": 5, "end": 6, "text": "Outside", "probability": 0.9},
            ]},
            "candidates": [{"start": 1, "end": 3, "duration": 2, "score": 0.8,
                            "text": "Hi", "evidence": {"speech_coverage": 0.9}}],
            "facecam": {"box": {"x": 100, "y": 50, "width": 200, "height": 100}},
        }
        event = threading.Event()
        options = {}

        def fake_analyze(path, **kwargs):
            options.update(kwargs)
            return payload

        module = SimpleNamespace(analyze_media=fake_analyze)
        with patch("streamframe.jobs.importlib.import_module", return_value=module), patch.dict(
            os.environ, {"STREAMFRAME_YUNET_MODEL_PATH": "/custom/model.onnx",
                         "STREAMFRAME_FFMPEG_PATH": "/custom/ffmpeg"}
        ):
            result = analyze_media(self.source, lambda *args: None, event)
            output = result["candidates"]
        self.assertEqual(options["yunet_model_path"], "/custom/model.onnx")
        self.assertEqual(options["ffmpeg_path"], "/custom/ffmpeg")
        self.assertFalse(options["cancellation_callback"]())
        self.assertEqual(len(output), 1)
        crop = validate_candidate(output[0])["face_crop"]
        self.assertAlmostEqual(crop["width"] * 1000 / (crop["height"] * 500), 9 / 8)
        self.assertLessEqual(crop["x"], 0.1)
        self.assertGreaterEqual(crop["x"] + crop["width"], 0.3)
        self.assertEqual(output[0]["words"], [{"start": 1, "end": 2, "text": "Hi"}])
        self.assertEqual(result["source_words"], [
            {"start": 1, "end": 2, "text": "Hi"},
            {"start": 5, "end": 6, "text": "Outside"},
        ])
        self.assertIn("verify the face crop", output[0]["evidence"][-1])
        payload["facecam"] = {"box": None}
        with patch("streamframe.jobs.importlib.import_module", return_value=module):
            output = analyze_media(self.source, lambda *args: None, event)["candidates"]
        self.assertIn("set the face crop manually", output[0]["evidence"][-1])
        payload["facecam"] = {
            "box": {"x": 20, "y": 30, "width": 600, "height": 200},
            "framing": "webcam_boundary",
        }
        with patch("streamframe.jobs.importlib.import_module", return_value=module):
            output = analyze_media(self.source, lambda *args: None, event)["candidates"]
        self.assertEqual(output[0]["face_crop"],
                         {"x": 0.02, "y": 0.06, "width": 0.6, "height": 0.4})
        self.assertIn("Webcam boundary is estimated", output[0]["evidence"][-1])
        payload["facecam"]["framing"] = "face_fallback"
        with patch("streamframe.jobs.importlib.import_module", return_value=module):
            output = analyze_media(self.source, lambda *args: None, event)["candidates"]
        self.assertIn("Webcam boundary uncertain", output[0]["evidence"][-1])

    def test_expanding_candidate_preserves_transcript_and_corrections(self):
        payload = {
            "media": {}, "facecam": {},
            "transcript": {"words": [
                {"start": 0, "end": 0.5, "text": "Before", "probability": 0.9},
                {"start": 1, "end": 2, "text": "Inside", "probability": 0.9},
                {"start": 5, "end": 6, "text": "After", "probability": 0.9},
            ]},
            "candidates": [
                {"start": 1, "end": 3, "score": 0.8, "evidence": {}},
                {"start": 2, "end": 4, "score": 0.7, "evidence": {}},
            ],
        }
        rendered = {}

        def capture_render(path, item, output, progress, cancelled):
            rendered.update(item)
            render(path, item, output, progress, cancelled)

        module = SimpleNamespace(analyze_media=lambda path, **kwargs: payload)
        store = JobStore(self.workspace / "data", [self.workspace], analyze_media, capture_render)
        try:
            with patch("streamframe.jobs.importlib.import_module", return_value=module):
                job = store.create({"type": "file", "path": str(self.source)})
                job = self.wait(store, job["id"])
            self.assertEqual(job["status"], "succeeded", job)
            self.assertEqual([len(item["words"]) for item in job["candidates"]], [1, 0])
            self.assertNotIn("source_words", job)
            item = job["candidates"][0]
            item["words"][0]["correction"] = "Corrected"
            store.edit(job["id"], item["id"], {
                "words": item["words"], "face_crop": {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
            })
            store.close()
            store = JobStore(self.workspace / "data", [self.workspace], analyze_media, capture_render)
            edited = store.edit(job["id"], item["id"], {"start": 0, "end": 7})
            self.assertEqual(edited["candidates"][0]["words"][1]["correction"], "Corrected")
            self.assertEqual(edited["candidates"][1]["words"], [])
            store.edit(job["id"], item["id"], {"start": 1, "end": 3})
            edited = store.edit(job["id"], item["id"], {"start": 0, "end": 7})
            self.assertEqual(len(edited["candidates"][0]["words"]), 3)
            store.export(job["id"], item["id"])
            self.assertEqual(self.wait(store, job["id"])["status"], "succeeded")
            self.assertEqual([word["text"] for word in rendered["words"]],
                             ["Before", "Inside", "After"])
            self.assertEqual(rendered["words"][1]["correction"], "Corrected")
            self.assertEqual(rendered["face_crop"], {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2})
            self.assertEqual((rendered["start"], rendered["end"]), (0, 7))
            remaining = [word for word in rendered["words"] if word["text"] != "Inside"]
            store.edit(job["id"], item["id"], {"words": remaining})
            edited = store.edit(job["id"], item["id"], {"end": 8})
            self.assertEqual([word["text"] for word in edited["candidates"][0]["words"]],
                             ["Before", "After"])
        finally:
            store.close()

    def test_export_failure_resume_preserves_edits(self):
        def broken(*args):
            raise RuntimeError("Renderer unavailable")

        store = JobStore(self.workspace / "data", [self.workspace], analyze, broken)
        try:
            job = store.create({"type": "file", "path": str(self.source)})
            self.wait(store, job["id"])
            store.edit(job["id"], "clip1", {
                "end": 5, "words": [{"start": 1, "end": 2, "text": "Hi", "correction": "Hello"}],
            })
            store.export(job["id"], "clip1")
            self.assertEqual(self.wait(store, job["id"])["status"], "failed")
            store.renderer = render
            store.analyzer = lambda *args: self.fail("Export resume must not rerun analysis")
            store.resume(job["id"])
            result = self.wait(store, job["id"])
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["candidates"][0]["end"], 5)
            self.assertEqual(result["candidates"][0]["words"][0]["correction"], "Hello")
            self.assertEqual(len(result["exports"]), 1)
        finally:
            store.close()

    def test_bounded_workers_and_exclusive_data_directory(self):
        entered = threading.Event()

        def block(path, progress, cancelled):
            entered.set()
            cancelled.wait(3)
            raise Cancelled()

        store = JobStore(self.workspace / "data", [self.workspace], block, render, workers=1)
        try:
            job = store.create({"type": "file", "path": str(self.source)})
            self.assertTrue(entered.wait(2))
            with self.assertRaises(Conflict):
                store.create({"type": "file", "path": str(self.source)})
            with self.assertRaises(Conflict):
                JobStore(self.workspace / "data")
            store.cancel(job["id"])
            self.wait(store, job["id"])
        finally:
            store.close()

    def test_downloader_proxy_blocks_redirect_targets(self):
        event = threading.Event()
        with DownloadProxy(event) as url:
            from urllib.parse import urlsplit
            proxy = urlsplit(url)
            for method, target in (("GET", "http://127.0.0.1/private"),
                                   ("CONNECT", "169.254.169.254:443")):
                conn = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=2)
                try:
                    conn.request(method, target)
                    self.assertEqual(conn.getresponse().status, 403)
                finally:
                    conn.close()

    def test_metadata_resolver_uses_proxy_without_downloading_video(self):
        captured = {}

        class Process:
            returncode = 0

            def __init__(self, command, **kwargs):
                captured.update(command=command, **kwargs)
                kwargs["stdout"].write(json.dumps({
                    "duration": 100, "live_status": "was_live", "formats": [],
                }).encode())

            def poll(self):
                return 0

        with patch("streamframe.jobs.subprocess.Popen", Process):
            self.assertEqual(resolve_video("twitch", "https://www.twitch.tv/videos/12",
                                           self.workspace, threading.Event(), time.monotonic() + 5)["duration"], 100)
        command = captured["command"]
        self.assertIn("--ignore-config", command)
        self.assertIn("--no-config-locations", command)
        self.assertIn("--no-plugin-dirs", command)
        self.assertIn("--no-playlist", command)
        self.assertIn("--skip-download", command)
        self.assertTrue(command[command.index("--proxy") + 1].startswith("http://127.0.0.1:"))
        self.assertEqual(captured["env"]["NO_PROXY"], "")
        self.assertEqual(captured["env"]["HOME"], str(self.workspace))
        self.assertEqual(captured["env"]["XDG_CONFIG_HOME"], str(self.workspace))
        self.assertTrue(captured["start_new_session"])
        import yt_dlp

        parsed = yt_dlp.parse_options(command[4:])
        self.assertFalse(parsed.options.usenetrc)
        self.assertIsNone(parsed.options.cookiefile)
        self.assertIsNone(parsed.options.cookiesfrombrowser)
        self.assertEqual(parsed.urls, ["https://www.twitch.tv/videos/12"])
        # The installed CLI parses the production flags, then exits without networking.
        smoke = subprocess.run([*command[:-2], "--version", *command[-2:]], env=captured["env"],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        self.assertEqual(smoke.stdout.strip(), yt_dlp.version.__version__)

    def test_remote_input_and_segment_limits(self):
        youtube = {"type": "url", "url": "https://youtu.be/abcd1234XYZ"}
        with self.assertRaisesRegex(ValueError, "explicit"):
            validate_remote_source(youtube)
        self.assertEqual(validate_remote_source({**youtube, "start": 10, "end": 70})["url"],
                         "https://www.youtube.com/watch?v=abcd1234XYZ")
        for invalid in ({"type": "streamer", "identifier": "name", "duration": 301},
                        {"type": "streamer", "identifier": "name", "mode": "live", "start": 0, "end": 2},
                        {"type": "url", "url": "https://127.0.0.1/videos/1", "start": 0, "end": 2}):
            with self.assertRaises(ValueError):
                validate_remote_source(invalid)
        manifest = "#EXTM3U\n#EXTINF:2,\na.ts\n#EXTINF:2,\nb.ts\n#EXTINF:2,\nc.ts\n#EXT-X-ENDLIST"
        base = "https://cdn.example/vod/index.m3u8"
        self.assertEqual(hls_segments(manifest, base, 2, 4), [(2, "https://cdn.example/vod/b.ts")])
        for altered in (manifest.replace("#EXT-X-ENDLIST", ""),
                        manifest.replace("b.ts", "#EXT-X-KEY:METHOD=AES-128\nb.ts"),
                        manifest.replace("b.ts", "#EXT-X-BYTERANGE:10@0\nb.ts")):
            with self.assertRaises(ValueError):
                hls_segments(altered, base, 2, 4)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
    def test_twitch_latest_downloads_only_overlapping_hls_segments(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        protocols = subprocess.check_output([ffmpeg, "-hide_banner", "-protocols"], text=True, timeout=10)
        if "fd" not in protocols.split():
            self.skipTest("FFmpeg seekable fd input unavailable")
        manifest_path = self.workspace / "index.m3u8"
        subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=20",
             "-t", "8", "-c:v", "libx264", "-g", "40", "-sc_threshold", "0", "-f", "hls",
             "-hls_time", "2", "-hls_playlist_type", "vod", str(manifest_path)],
            check=True, capture_output=True, timeout=20,
        )
        requested = []
        archive = SimpleNamespace(url="https://www.twitch.tv/videos/12", platform="twitch",
                                  title="Controlled VOD", is_live=False, selection="latest")

        class FixtureFetch:
            def __init__(self, event, deadline):
                pass

            @contextmanager
            def open(self, url):
                name = url.rsplit("/", 1)[-1]
                requested.append(name)
                yield url, (self_path / name).read_bytes()

            def chunks(self, response):
                yield response

        self_path = self.workspace
        store = JobStore(self.workspace / "data", [self.workspace], analyze, render)
        try:
            with patch("streamframe.sources.resolve_source", return_value=archive) as resolve, patch(
                "streamframe.jobs.resolve_video", return_value={
                    "duration": 8, "formats": [{"url": "https://cdn.example/index.m3u8", "height": 720,
                                                "protocol": "m3u8_native"}],
                },
            ), patch("streamframe.jobs.RemoteFetch", FixtureFetch):
                job = store.create({"type": "streamer", "identifier": "streamer", "mode": "latest",
                                    "start": 2.5, "end": 5.5})
                job = self.wait(store, job["id"])
            self.assertEqual(job["status"], "succeeded", job)
            self.assertEqual(resolve.call_args.args, ("streamer",))
            self.assertEqual(resolve.call_args.kwargs["mode"], "latest")
            self.assertEqual(job["resolved_source"]["title"], "Controlled VOD")
            self.assertEqual(job["imported_range"], {"start": 2.5, "end": 5.5})
            self.assertEqual(requested, ["index.m3u8", "index1.ts", "index2.ts"])
            timing = subprocess.check_output(
                [ffprobe, "-v", "error", "-show_entries", "format=start_time,duration",
                 "-of", "csv=p=0", str(store.media_path(job["id"]))], text=True, timeout=10,
            )
            start, duration = map(float, timing.strip().split(","))
            self.assertAlmostEqual(start, 0)
            self.assertAlmostEqual(duration, 3.0, delta=0.05)
            self.assertFalse((store.root / job["id"] / ".remote-segments.ts").exists())
        finally:
            store.close()

    def test_youtube_range_fails_without_fallback(self):
        from streamframe.youtube_vod import YouTubeVodUnavailable
        store = JobStore(self.workspace / "data", [self.workspace], analyze, render)
        try:
            with patch("streamframe.jobs.resolve_video") as resolver, patch(
                "streamframe.youtube_vod.import_youtube_vod",
                side_effect=YouTubeVodUnavailable("No supported bounded archive"),
            ) as youtube:
                job = store.create({"type": "url", "url": "https://youtu.be/abcd1234XYZ",
                                    "start": 0, "end": 10})
                job = self.wait(store, job["id"])
            self.assertEqual(job["status"], "failed")
            self.assertIn("No supported bounded archive", job["error"])
            self.assertIsNone(job["media_url"])
            resolver.assert_not_called()
            youtube.assert_called_once()
        finally:
            store.close()

    def test_auto_live_routes_to_capture_before_analysis(self):
        resolved = SimpleNamespace(url="https://www.twitch.tv/streamer", platform="twitch",
                                   title="On Air", is_live=True, selection="live")
        timeline = "2026-03-01T10:00:01+00:00"
        pipeline = []

        def capture(source, directory, *, duration, cancelled, progress, on_alignment):
            pipeline.append(("capture", source.title, duration))
            self.assertFalse(cancelled.is_set())
            progress(0.5, "Waiting for live segment")
            on_alignment(timeline)
            path = directory / "source.mp4"
            path.write_bytes(b"capture")
            return path

        def analyzer(path, progress, cancelled):
            pipeline.append(("analysis", path.read_bytes()))
            return analyze(path, progress, cancelled)

        store = JobStore(self.workspace / "data", [self.workspace], analyzer, render)
        try:
            with patch("streamframe.sources.resolve_source", return_value=resolved), patch(
                "streamframe.live.capture_live", side_effect=capture
            ), patch("streamframe.jobs.import_remote") as vod:
                job = store.create({"type": "streamer", "identifier": "streamer", "duration": 12})
                job = self.wait(store, job["id"])
            vod.assert_not_called()
            self.assertEqual(job["status"], "succeeded", job)
            self.assertEqual(job["capture_started_at"], timeline)
            self.assertEqual(job["resolved_source"]["selection"], "live")
            self.assertEqual(pipeline, [("capture", "On Air", 12), ("analysis", b"capture")])
        finally:
            store.close()

    def test_late_cancel_does_not_overwrite_terminal_status(self):
        store = JobStore(self.workspace / "data", [self.workspace], analyze, render)
        try:
            job = store.create({"type": "file", "path": str(self.source)})
            self.wait(store, job["id"])
            event = threading.Event()
            with store.lock:
                # This is the exact published-result/worker-teardown intermediate state.
                store.active[job["id"]] = (event, None)
                try:
                    self.assertEqual(store.cancel(job["id"])["status"], "succeeded")
                    self.assertFalse(event.is_set())
                finally:
                    store.active.pop(job["id"])
        finally:
            store.close()

    def test_face_crop_contains_portrait_box_near_edge(self):
        crop = padded_face_crop({"x": 0, "y": 350, "width": 80, "height": 140}, 1000, 500)
        self.assertEqual(crop["x"], 0)
        self.assertAlmostEqual(crop["width"] * 1000 / (crop["height"] * 500), 9 / 8)
        self.assertLessEqual(crop["y"] * 500, 350)
        self.assertGreaterEqual((crop["y"] + crop["height"]) * 500, 490)
        self.assertIsNone(padded_face_crop({"x": 0, "y": 0, "width": 65, "height": 150}, 100, 200))


@unittest.skipIf(create_app is None, "FastAPI/httpx dependencies unavailable")
class APITests(Workspace):
    def test_candidate_media_route_serves_full_window_and_ranges(self):
        async def run():
            app = create_app(self.workspace / "data", [self.workspace], analyze, render)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
                async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                    url = "/api/jobs/rolling/candidates/window-1/media"
                    with patch.object(app.state.jobs, "candidate_media_path", return_value=self.source,
                                      create=True) as lookup:
                        full = await client.get(url)
                        self.assertEqual(full.status_code, 200, full.text)
                        self.assertEqual(full.headers["Content-Type"], "video/mp4")
                        self.assertEqual(full.content, b"preview-media")
                        lookup.assert_called_once_with("rolling", "window-1")
                        partial = await client.get(url, headers={"Range": "bytes=0-6"})
                        self.assertEqual(partial.status_code, 206)
                        self.assertEqual(partial.content, b"preview")
                        forbidden = await client.get(url, headers={"Origin": "https://evil.example"})
                        self.assertEqual(forbidden.status_code, 403)
                        self.assertEqual(lookup.call_count, 2)
                        lookup.side_effect = KeyError("window-1")
                        self.assertEqual((await client.get(url)).status_code, 404)

        asyncio.run(run())

    def test_live_crop_route_accepts_rectangle_and_reset(self):
        async def run():
            app = create_app(self.workspace / "data", [self.workspace], analyze, render)
            crop = {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}
            async with app.router.lifespan_context(app):
                store = app.state.jobs
                with store.lock, store.connect() as db:
                    store._save(db, {"id": "rolling", "status": "running",
                                     "live_session": {"face_crop": None, "windows": []}})
                transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
                async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                    url = "/api/jobs/rolling/live-crop"
                    response = await client.patch(url, json={"face_crop": crop})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["live_session"]["face_crop"], crop)
                    self.assertEqual(store.get("rolling")["live_session"]["face_crop"], crop)
                    self.assertEqual((await client.patch(url, json={})).status_code, 422)
                    self.assertEqual((await client.patch(url, json={"face_crop": {"x": 1}})).status_code, 422)
                    self.assertEqual((await client.patch(url, json={"face_crop": None},
                                                        headers={"Origin": "https://evil.example"})).status_code, 403)
                    response = await client.patch(url, json={"face_crop": None})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIsNone(response.json()["live_session"]["face_crop"])
                    store.update("rolling", status="succeeded")
                    self.assertEqual((await client.patch(url, json={"face_crop": crop})).status_code, 409)

        asyncio.run(run())

    def test_api_contract(self):
        async def run():
            app = create_app(self.workspace / "data", [self.workspace], analyze, render)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
                async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                    self.assertEqual((await client.get("/api/health")).json(), {"status": "ok"})
                    response = await client.post("/api/jobs", json={"source": {"type": "file", "path": str(self.source)}})
                    self.assertEqual(response.status_code, 202, response.text)
                    job = response.json()
                    for _ in range(100):
                        job = (await client.get("/api/jobs/" + job["id"])).json()
                        if job["status"] in {"succeeded", "failed"}:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(job["status"], "succeeded", job)
                    self.assertEqual((await client.get(job["media_url"])).content, b"preview-media")
                    partial = await client.get(job["media_url"], headers={"Range": "bytes=0-6"})
                    self.assertEqual(partial.status_code, 206)
                    self.assertEqual(partial.content, b"preview")
                    self.assertEqual(len((await client.get("/api/jobs")).json()["jobs"]), 1)
                    response = await client.patch(f"/api/jobs/{job['id']}/candidates/clip1", json={"end": 5})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["candidates"][0]["end"], 5)
                    self.assertEqual((await client.post(f"/api/jobs/{job['id']}/candidates/clip1/export")).status_code, 202)
                    for _ in range(100):
                        job = (await client.get("/api/jobs/" + job["id"])).json()
                        if job["exports"]:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual((await client.get(job["exports"][0]["url"])).content, b"rendered")
                    self.assertEqual((await client.get("/api/jobs/missing")).status_code, 404)
                    self.assertEqual((await client.post("/api/jobs", json={"source": {"type": "url", "url": "http://127.0.0.1"}})).status_code, 422)
                    self.assertEqual((await client.post("/api/jobs", content="{}")).status_code, 415)
                    self.assertEqual((await client.post("/api/jobs", json={"source": {}, "other": 1})).status_code, 422)
                    self.assertEqual((await client.post("/api/jobs", content=b"{" + b" " * (1024 * 1024),
                                                       headers={"Content-Type": "application/json"})).status_code, 413)
                    self.assertEqual((await client.patch(f"/api/jobs/{job['id']}/candidates/clip1",
                                                        json={"id": "../escape"})).status_code, 422)
                    with patch.object(app.state.jobs, "set_live_crop", return_value={"id": job["id"]}) as set_crop:
                        crop = {"face_crop": {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.3}}
                        response = await client.patch(f"/api/jobs/{job['id']}/live-crop", json=crop)
                        self.assertEqual(response.status_code, 200)
                        set_crop.assert_called_once_with(job["id"], crop)
                        self.assertEqual((await client.patch(f"/api/jobs/{job['id']}/live-crop", json=crop,
                                                             headers={"Origin": "https://evil.example"})).status_code, 403)
                    self.assertEqual((await client.get(f"/api/jobs/{job['id']}/exports/missing.mp4")).status_code, 404)
                    self.assertEqual((await client.get("/api/health", headers={"Origin": "https://evil.example"})).status_code, 403)
                    self.assertEqual((await client.get("/api/health", headers={"Host": "evil.example"})).status_code, 403)
                    self.assertEqual((await client.get("/api/health", headers={"X-Forwarded-For": "127.0.0.1"})).status_code, 403)
                remote = httpx.ASGITransport(app=app, client=("192.0.2.1", 1234))
                async with httpx.AsyncClient(transport=remote, base_url="http://127.0.0.1") as client:
                    self.assertEqual((await client.get("/api/health")).status_code, 403)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
