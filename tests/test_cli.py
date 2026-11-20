import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from streamframe import cli


class CLITests(unittest.TestCase):
    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    @patch("streamframe.cli._request")
    @patch("streamframe.cli._ensure_server")
    def test_submit_local_source_as_absolute_path(self, ensure, request):
        request.return_value = {"id": 42, "status": "queued"}
        source = str(Path(__file__).resolve())
        code, output, error = self.run_cli("submit", source)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["id"], 42)
        request.assert_called_once_with(cli.DEFAULT_API_URL, "POST", "/api/jobs",
                                        {"source": {"type": "file", "path": source}})
        ensure.assert_called_once_with(cli.DEFAULT_API_URL, True)

    @patch("streamframe.cli._request")
    @patch("streamframe.cli._ensure_server")
    def test_submit_url_and_follow(self, ensure, request):
        request.side_effect = [
            {"id": "job/1", "status": "queued"},
            {"id": "job/1", "status": "completed"},
        ]
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        code, output, error = self.run_cli("submit", url, "--start", "20.5", "--end", "30", "--follow")
        self.assertEqual((code, error), (0, ""))
        self.assertIn('"completed"', output)
        self.assertEqual(request.call_args_list[0].args[3],
                         {"source": {"type": "url", "url": url, "start": 20.5, "end": 30.0}})
        self.assertEqual(request.call_args_list[1].args[2], "/api/jobs/job%2F1")

    @patch("streamframe.cli._request")
    @patch("streamframe.cli._ensure_server")
    def test_submit_with_local_trim_bounds(self, ensure, request):
        request.return_value = {"id": 42, "status": "queued"}
        source = str(Path(__file__).resolve())
        for flags, bounds in [
            (("--start", "20.5", "--end", "30"), {"start": 20.5, "end": 30.0}),
            (("--start", "20.5"), {"start": 20.5}),
            (("--end", "30"), {"end": 30.0}),
        ]:
            with self.subTest(flags=flags):
                code, _, error = self.run_cli("submit", source, *flags)
                self.assertEqual((code, error), (0, ""))
                request.assert_called_with(cli.DEFAULT_API_URL, "POST", "/api/jobs",
                                           {"source": {"type": "file", "path": source, **bounds}})

    @patch("streamframe.cli._ensure_server")
    def test_invalid_trim_does_not_start_server(self, ensure):
        source = str(Path(__file__).resolve())
        url = "https://www.twitch.tv/videos/123456789"
        for args, message in [
            ((url,), "require both --start and --end"),
            ((url, "--start", "0"), "require both --start and --end"),
            ((url, "--end", "15"), "require both --start and --end"),
            ((url, "--start", "0", "--end", "inf"), "finite and nonnegative"),
            ((url, "--start", "15", "--end", "10"), "must be after"),
            ((url, "--start", "0", "--end", "1801"), "at most 1800 seconds"),
            ((source, "--start", "nan"), "finite and nonnegative"),
            ((source, "--end", "-1"), "finite and nonnegative"),
            ((source, "--start", "3", "--end", "3"), "must be after"),
            ((source, "--end", "1801"), "at most 1800 seconds"),
        ]:
            with self.subTest(args=args):
                code, _, error = self.run_cli("submit", *args)
                self.assertEqual(code, 1)
                self.assertIn(message, error)
        ensure.assert_not_called()

    @patch("streamframe.cli._ensure_server")
    def test_invalid_source_fails_before_starting_server(self, ensure):
        code, output, error = self.run_cli("submit", "not-a-real-video-file")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("does not exist", error)
        ensure.assert_not_called()

    @patch("streamframe.cli._request")
    @patch("streamframe.cli._ensure_server")
    def test_streamer_modes_and_platforms(self, ensure, request):
        request.return_value = {"id": "j", "status": "queued"}
        cases = [
            ("streamername", [], "auto", {"type": "streamer", "platform": "twitch", "identifier": "streamername"}),
            ("@youtubehandle", ["--platform", "youtube", "--mode", "live", "--duration", "45"], "live",
             {"type": "streamer", "platform": "youtube", "identifier": "@youtubehandle", "duration": 45.0}),
            ("https://www.youtube.com/@youtubehandle", ["--mode", "live", "--duration", "300"], "live",
             {"type": "url", "url": "https://www.youtube.com/@youtubehandle", "duration": 300.0}),
            ("https://www.twitch.tv/streamername", ["--mode", "latest", "--start", "5", "--end", "30"], "latest",
             {"type": "url", "url": "https://www.twitch.tv/streamername", "start": 5.0, "end": 30.0}),
            ("streamername", ["--mode", "live", "--rolling", "--session-duration", "120",
                              "--window-duration", "30"], "live",
             {"type": "streamer", "platform": "twitch", "identifier": "streamername",
              "rolling": True, "session_duration": 120.0, "window_duration": 30.0}),
            ("streamername", ["--mode", "live", "--rolling", "--session-duration", "1800"], "live",
             {"type": "streamer", "platform": "twitch", "identifier": "streamername",
              "rolling": True, "session_duration": 1800.0, "window_duration": 60.0}),
        ]
        for identifier, flags, mode, source in cases:
            with self.subTest(identifier=identifier, mode=mode):
                code, _, error = self.run_cli("streamer", identifier, *flags)
                self.assertEqual((code, error), (0, ""))
                request.assert_called_with(cli.DEFAULT_API_URL, "POST", "/api/jobs", {
                    "source": {**source, "mode": mode},
                })

    @patch("streamframe.cli._ensure_server")
    def test_invalid_streamer_options_do_not_start_server(self, ensure):
        for args, message in [
            (("",), "Enter a Twitch username"),
            (("name", "--duration", "0"), "duration"),
            (("name", "--duration", "inf"), "duration"),
            (("name", "--duration", "301"), "between 1 and 300 seconds"),
            (("name", "--mode", "live", "--start", "0"), "finished streams"),
            (("name", "--start", "0"), "requires both"),
            (("@channel",), "require --platform youtube"),
            (("name", "--rolling"), "requires --mode live"),
            (("name", "--mode", "live", "--rolling", "--duration", "10"), "cannot use --duration"),
            (("name", "--mode", "live", "--rolling", "--session-duration", "10"), "no longer than the session"),
            (("name", "--mode", "live", "--rolling", "--session-duration", "1801"), "1–1800 seconds"),
            (("name", "--window-duration", "20"), "require --rolling"),
        ]:
            with self.subTest(args=args):
                code, _, error = self.run_cli("streamer", *args)
                self.assertEqual(code, 1)
                self.assertIn(message, error)
        ensure.assert_not_called()

    @patch("streamframe.cli._request")
    @patch("streamframe.cli._ensure_server")
    def test_live_crop(self, ensure, request):
        request.return_value = {"status": "running"}
        code, _, error = self.run_cli("live-crop", "job/1", "--face-crop", "0.1", "0.2", "0.3", "0.4")
        self.assertEqual((code, error), (0, ""))
        request.assert_called_with(cli.DEFAULT_API_URL, "PATCH", "/api/jobs/job%2F1/live-crop",
                                   {"face_crop": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}})
        code, _, error = self.run_cli("live-crop", "job/1", "--auto")
        self.assertEqual((code, error), (0, ""))
        request.assert_called_with(cli.DEFAULT_API_URL, "PATCH", "/api/jobs/job%2F1/live-crop",
                                   {"face_crop": None})

    @patch("streamframe.cli._request")
    @patch("streamframe.cli._ensure_server")
    def test_job_commands(self, ensure, request):
        request.return_value = {"status": "queued"}
        for name, method, path in [
            ("list", "GET", "/api/jobs"),
            ("status", "GET", "/api/jobs/a%2Fb"),
            ("cancel", "POST", "/api/jobs/a%2Fb/cancel"),
            ("resume", "POST", "/api/jobs/a%2Fb/resume"),
        ]:
            with self.subTest(command=name):
                request.reset_mock()
                args = (name,) if name == "list" else (name, "a/b")
                code, _, error = self.run_cli("--no-start", *args)
                self.assertEqual((code, error), (0, ""))
                request.assert_called_once_with(cli.DEFAULT_API_URL, method, path)
                self.assertEqual(ensure.call_args.args, (cli.DEFAULT_API_URL, False))

    @patch("streamframe.cli.time.sleep")
    @patch("streamframe.cli._request")
    @patch("streamframe.cli._ensure_server")
    def test_watch_stops_on_terminal_state(self, ensure, request, sleep):
        request.side_effect = [{"status": "running"}, {"status": "failed"}]
        code, output, _ = self.run_cli("status", "1", "--watch", "--interval", "0.5")
        self.assertEqual(code, 1)
        self.assertIn('"failed"', output)
        sleep.assert_called_once_with(0.5)

    @patch("streamframe.cli._ensure_server")
    def test_invalid_interval(self, ensure):
        code, _, error = self.run_cli("status", "1", "--watch", "--interval", "0")
        self.assertEqual(code, 1)
        self.assertIn("greater than zero", error)
        ensure.assert_not_called()

    @patch("streamframe.cli._request")
    @patch("streamframe.cli._ensure_server")
    def test_edit_candidate(self, ensure, request):
        request.return_value = {"id": "j"}
        code, _, error = self.run_cli(
            "edit", "j", "clip/1", "--start", "1.5", "--end", "4",
            "--face-crop", "0", "0.1", "0.5", "0.7", "--clear-content-crop",
            "--words-json", '[{"start":2,"end":3,"text":"Hello"}]',
        )
        self.assertEqual((code, error), (0, ""))
        request.assert_called_once_with(cli.DEFAULT_API_URL, "PATCH", "/api/jobs/j/candidates/clip%2F1", {
            "start": 1.5, "end": 4.0,
            "face_crop": {"x": 0.0, "y": 0.1, "width": 0.5, "height": 0.7},
            "content_crop": None, "words": [{"start": 2, "end": 3, "text": "Hello"}],
        })

    @patch("streamframe.cli._ensure_server")
    def test_invalid_edits_fail_before_starting_server(self, ensure):
        for args in [(), ("--words-json", "{}"), ("--words-json", "["), ("--start", "nan")]:
            with self.subTest(args=args):
                code, _, error = self.run_cli("edit", "j", "c", *args)
                self.assertEqual(code, 1)
                self.assertTrue(error)
        ensure.assert_not_called()

    @patch("streamframe.cli._request")
    @patch("streamframe.cli._ensure_server")
    def test_export_and_follow(self, ensure, request):
        request.side_effect = [{"status": "running"}, {"status": "succeeded", "exports": []}]
        code, output, error = self.run_cli("export", "j", "c", "--follow")
        self.assertEqual((code, error), (0, ""))
        self.assertIn('"succeeded"', output)
        self.assertEqual(request.call_args_list[0].args,
                         (cli.DEFAULT_API_URL, "POST", "/api/jobs/j/candidates/c/export"))
        self.assertEqual(request.call_args_list[1].args, (cli.DEFAULT_API_URL, "GET", "/api/jobs/j"))

    @patch("streamframe.cli.urlopen")
    def test_http_errors_are_user_facing(self, urlopen):
        urlopen.side_effect = HTTPError("http://x", 422, "Unprocessable", {}, io.BytesIO(b'{"detail":"bad source"}'))
        with self.assertRaisesRegex(cli.CLIError, "HTTP 422: bad source"):
            cli._request("http://x", "POST", "/api/jobs", {})

    @patch("streamframe.cli.urlopen")
    def test_connection_errors_are_user_facing(self, urlopen):
        urlopen.side_effect = URLError("refused")
        with self.assertRaisesRegex(cli.CLIError, "Cannot connect"):
            cli._request("http://x", "GET", "/api/jobs")

    @patch("streamframe.cli.subprocess.Popen")
    @patch("streamframe.cli._request")
    def test_auto_starts_local_server(self, request, popen):
        request.side_effect = [cli.CLIError("Cannot connect to local"), {"status": "ok"}]
        cli._ensure_server("http://127.0.0.1:8765")
        request.assert_called_with("http://127.0.0.1:8765", "GET", "/api/health", timeout=1)
        command = popen.call_args.args[0]
        self.assertEqual(command[-5:], ["serve", "--host", "127.0.0.1", "--port", "8765"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertTrue(popen.call_args.kwargs["env"]["STREAMFRAME_DATA_DIR"])

    @patch("streamframe.cli.subprocess.Popen")
    @patch("streamframe.cli._request")
    def test_never_auto_starts_remote_server(self, request, popen):
        request.side_effect = cli.CLIError("Cannot connect to remote")
        with self.assertRaisesRegex(cli.CLIError, "Cannot connect"):
            cli._ensure_server("https://example.com")
        popen.assert_not_called()

    @patch("streamframe.cli.subprocess.Popen")
    @patch("streamframe.cli._request")
    def test_existing_http_error_does_not_start_server(self, request, popen):
        request.side_effect = cli.CLIError("HTTP 500: broken")
        with self.assertRaisesRegex(cli.CLIError, "HTTP 500"):
            cli._ensure_server(cli.DEFAULT_API_URL)
        popen.assert_not_called()

    @patch("streamframe.cli._serve")
    def test_serve(self, serve):
        code, _, error = self.run_cli("serve", "--port", "9999")
        self.assertEqual((code, error), (0, ""))
        serve.assert_called_once_with("127.0.0.1", 9999)

    @patch("streamframe.cli._serve")
    def test_serve_rejects_non_loopback_host(self, serve):
        code, _, error = self.run_cli("serve", "--host", "0.0.0.0")
        self.assertEqual(code, 1)
        self.assertIn("loopback", error)
        serve.assert_not_called()

    @patch("streamframe.cli._ensure_server")
    def test_invalid_api_url(self, ensure):
        for url in ("http://[", "ftp://example.com", "http://u:p@example.com"):
            with self.subTest(url=url):
                code, _, error = self.run_cli("--api-url", url, "list")
                self.assertEqual(code, 1)
                self.assertIn("API URL", error)
        ensure.assert_not_called()

    def test_help_requires_no_backend(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exc:
                cli.main(["--help"])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("submit", output.getvalue())

    def test_http_client_round_trip(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                calls.append((self.path, self.headers["Content-Type"], json.loads(body)))
                data = json.dumps({"id": "one"}).encode()
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            payload = {"source": {"type": "url", "url": "https://www.twitch.tv/videos/123456789",
                                  "start": 0, "end": 20}}
            result = cli._request(f"http://127.0.0.1:{server.server_port}", "POST", "/api/jobs", payload)
            self.assertEqual(result, {"id": "one"})
            self.assertEqual(calls, [("/api/jobs", "application/json", payload)])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_real_api_routes_with_stubbed_store(self):
        try:
            import uvicorn
            from streamframe.jobs import validate_remote_source
            from streamframe.server import create_app
        except ImportError:
            self.skipTest("Backend dependencies unavailable")
        store = unittest.mock.MagicMock()
        store.list.return_value = []
        store.create.return_value = {"id": "j", "status": "queued"}
        store.edit.return_value = {"id": "j", "status": "completed"}
        store.export.return_value = {"id": "j", "status": "running"}
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(create_app(), log_level="critical", lifespan="on"))
        with patch("streamframe.server.JobStore", return_value=store):
            thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and time.monotonic() < deadline and thread.is_alive():
                    time.sleep(0.01)
                self.assertTrue(server.started, "API server failed to start")
                url = "https://www.twitch.tv/videos/123456789"
                code, _, error = self.run_cli("--api-url", address, "--no-start", "submit", url,
                                              "--start", "0", "--end", "20")
                self.assertEqual((code, error), (0, ""))
                store.create.assert_called_once_with({"type": "url", "url": url, "start": 0.0, "end": 20.0})
                store.create.side_effect = lambda source: {"id": "j", "source": validate_remote_source(source)}
                code, _, error = self.run_cli("--api-url", address, "--no-start", "streamer", "@channel",
                                              "--platform", "youtube", "--mode", "live", "--duration", "45")
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(store.create.call_args.args, ({
                    "type": "streamer", "platform": "youtube", "identifier": "@channel",
                    "mode": "live", "duration": 45.0,
                },))
                channel_url = "https://www.twitch.tv/streamername"
                code, output, error = self.run_cli("--api-url", address, "--no-start", "streamer", channel_url,
                                                   "--mode", "latest")
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(json.loads(output)["source"], {"type": "url", "url": channel_url,
                                                               "mode": "latest"})
                code, _, error = self.run_cli("--api-url", address, "--no-start", "edit", "j", "c",
                                               "--start", "1", "--words-json", "[]")
                self.assertEqual((code, error), (0, ""))
                store.edit.assert_called_once_with("j", "c", {"start": 1.0, "words": []})
                code, _, error = self.run_cli("--api-url", address, "--no-start", "export", "j", "c")
                self.assertEqual((code, error), (0, ""))
                store.export.assert_called_once_with("j", "c")
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                listener.close()
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
