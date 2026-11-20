import asyncio
import json
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import httpx

from streamframe.jobs import JobStore, validate_remote_source
from streamframe.live import MAX_SESSION_SECONDS
from streamframe.server import create_app


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path.cwd() / (".diagnostics-test-" + uuid.uuid4().hex)
        self.workspace.mkdir()

    def tearDown(self):
        shutil.rmtree(self.workspace)

    def wait(self, store, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with store.lock:
                if job_id not in store.active:
                    return store.get(job_id)
            time.sleep(0.01)
        self.fail("job did not finish")

    def test_rolling_session_validation_uses_live_limit(self):
        source = {"type": "streamer", "identifier": "streamer", "rolling": True}
        self.assertEqual(MAX_SESSION_SECONDS, 1800)
        self.assertEqual(validate_remote_source({**source, "session_duration": 1800})["session_duration"], 1800)
        with self.assertRaisesRegex(ValueError, "1800 seconds"):
            validate_remote_source({**source, "session_duration": 1801})
        self.assertEqual(validate_remote_source(source)["session_duration"], 600)

    def test_acquisition_failure_history_and_redaction_persist(self):
        store = JobStore(self.workspace / "data")
        resolved = SimpleNamespace(
            url="https://user:password@www.youtube.com/watch?v=private&token=secret",
            platform="youtube", title="token=secret", is_live=False, selection="latest", source_id="secret",
        )

        def fail(*args, **kwargs):
            try:
                raise OSError("Cookie: secret; /home/private/file")
            except OSError as cause:
                raise ValueError("Authorization: Bearer secret https://cdn.example/signed?token=secret") from cause

        try:
            with patch("streamframe.sources.resolve_source", return_value=resolved), \
                 patch("streamframe.jobs.import_remote", side_effect=fail):
                job = store.create({"type": "streamer", "platform": "youtube",
                                    "identifier": "@channel", "mode": "latest"})
                job = self.wait(store, job["id"])
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["resolved_source"]["platform"], "youtube")
            report = store.diagnostics(job["id"])["text"]
            self.assertIn("failure during download", report)
            self.assertIn("Resolved URL: https://www.youtube.com/watch", report)
            self.assertIn("ValueError -> OSError", report)
            self.assertIn("Source platform: youtube", report)
            for secret in ("secret", "password", "private", "Authorization", "Cookie", "token=", "/home/"):
                self.assertNotIn(secret, report)
            events = job["diagnostic_history"]
            self.assertTrue(any(event["stage"] == "download" for event in events))
            self.assertTrue(all(isinstance(event["at"], float) for event in events))
            self.assertIn("T", report.split("History (UTC; most recent transitions):")[1].splitlines()[1])
        finally:
            store.close()
        reopened = JobStore(self.workspace / "data")
        try:
            self.assertIn("failure during download", reopened.diagnostics(job["id"])["text"])
        finally:
            reopened.close()

    def test_old_youtube_failure_is_classified_without_guessing_other_errors(self):
        store = JobStore(self.workspace / "data")
        job_id = "3d0547011e6b4d8eab2e3a22e7cb7ced"
        job = {"id": job_id, "status": "failed", "stage": "failed", "operation": "analyze",
               "source": {"type": "streamer", "platform": "youtube", "identifier": "@user", "mode": "latest"},
               "resolved_source": {"platform": "youtube", "selection": "latest", "is_live": False,
                                   "url": "https://cdn.example/signed/path?token=secret"},
               "error": "ValueError: Bounded YouTube VOD import is unavailable: no verified time-to-byte index",
               "created_at": 1, "updated_at": 1}
        try:
            with store.connect() as db:
                db.execute("INSERT INTO jobs VALUES (?, ?)", (job_id, json.dumps(job)))
            report = store.diagnostics(job_id)["text"]
            self.assertIn("failure during download", report)
            self.assertIn("YouTubeVodUnavailable", report)
            self.assertIn("the finished format may still be unsupported", report)
            self.assertIn("[URL redacted]", report)
            self.assertNotIn("signed/path", report)
            job["error"] = "ValueError: secret /private/path"
            with store.connect() as db:
                db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job_id))
            report = store.diagnostics(job_id)["text"]
            self.assertIn("failure stage unavailable", report)
            self.assertNotIn("secret", report)
            self.assertNotIn("/private/path", report)
        finally:
            store.close()

    def test_history_is_bounded_and_url_source_is_sanitized(self):
        source = self.workspace / "input.mp4"
        source.write_bytes(b"input")
        store = JobStore(self.workspace / "data", [self.workspace], lambda *_: [])
        try:
            job = store.create({"type": "file", "path": str(source)})
            self.wait(store, job["id"])
            for index in range(40):
                store.update(job["id"], stage="analysis" if index % 2 else "import")
            with store.lock, store.connect() as db:
                job = store._get(db, job["id"])
                job["source"] = {"type": "url",
                                 "url": "https://name:password@www.youtube.com/watch?v=id&token=secret",
                                 "start": 0}
                store._save(db, job)
            job = store.get(job["id"])
            self.assertEqual(len(job["diagnostic_history"]), 24)
            report = store.diagnostics(job["id"])["text"]
            self.assertIn("Source URL: https://www.youtube.com/watch", report)
            self.assertIn("Source platform: youtube; requested mode: bounded VOD range", report)
            self.assertNotIn("password", report)
            self.assertNotIn("token", report)
            self.assertLess(len(report), 6000)
        finally:
            store.close()

    def test_endpoint_schema_missing_and_local_path(self):
        source = self.workspace / "private.mp4"
        source.write_bytes(b"input")

        async def run():
            app = create_app(self.workspace / "data", [self.workspace], lambda *_: [], lambda *_: None)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
                async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                    response = await client.post("/api/jobs", json={"source": {"type": "file", "path": str(source)}})
                    job_id = response.json()["id"]
                    response = await client.get(f"/api/jobs/{job_id}/diagnostics")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(set(response.json()), {"text"})
                    self.assertIn(job_id, response.json()["text"])
                    self.assertNotIn(str(source), response.json()["text"])
                    self.assertEqual((await client.get("/api/jobs/missing/diagnostics")).status_code, 404)
                    self.assertEqual((await client.get(f"/api/jobs/{job_id}/diagnostics",
                                                       headers={"Origin": "https://evil.example"})).status_code, 403)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
