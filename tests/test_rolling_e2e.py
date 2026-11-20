"""Controlled HLS acquisition through real rolling processing and FFmpeg exports."""

import json
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

from streamframe import jobs, live, sources
from test_live import FixtureTransport, media_segments, playlist, segment, serve


def test_rolling_hls_exports_before_stop_and_preserves_clips(tmp_path, monkeypatch, media_segments):
    channel = "https://www.twitch.tv/fixture_channel"
    available = [1]
    guard = threading.Lock()
    initial_manifest = threading.Event()

    def manifest(_count):
        with guard:
            count = available[0]
        initial_manifest.set()
        items = [segment("old.ts", "#EXT-X-PROGRAM-DATE-TIME:2026-03-01T10:00:00Z\n")]
        items.extend(segment(f"segment-{i}.ts") for i in range(1, count))
        return 200, playlist(40, *items)

    routes = {"/live": (200, manifest)}
    routes.update({f"/segment-{i}.ts": (200, media_segments[i]) for i in range(1, 4)})
    monkeypatch.setattr(sources, "resolve_source", lambda *_args, **_kwargs: sources.ResolvedSource(
        channel, "twitch", "Synthetic live", True, "fixture_channel", "live", "live"))

    def analyzer(_path, progress, event):
        assert not event.is_set()
        return [{"id": "candidate", "start": 0.1, "end": 0.7, "score": 1,
                 "words": [{"start": 0.2, "end": 0.6, "text": "Test caption"}],
                 "face_crop": {"x": 0, "y": 0, "width": 0.5, "height": 1},
                 "content_crop": {"x": 0.5, "y": 0, "width": 0.5, "height": 1}}]

    with serve(routes) as (base, seen):
        monkeypatch.setattr(live, "_resolve_hls", lambda *_args: base + "/live")
        monkeypatch.setattr(jobs, "RemoteFetch", lambda _event, _deadline: FixtureTransport(base))
        store = jobs.JobStore(tmp_path / "jobs", analyzer=analyzer)
        try:
            job_id = store.create({"type": "url", "url": channel, "mode": "live",
                                   "rolling": True, "session_duration": 30,
                                   "window_duration": 1})["id"]

            def wait(predicate, timeout=30, allow_cancelled=False):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    job = store.get(job_id)
                    if predicate(job):
                        return job
                    if job["status"] == "failed" or (job["status"] == "cancelled" and not allow_cancelled):
                        raise AssertionError(job)
                    time.sleep(0.05)
                raise AssertionError(store.get(job_id))

            assert initial_manifest.wait(10)
            for index in (1, 2):
                with guard:
                    available[0] = index + 1
                current = wait(lambda job: len(job["exports"]) >= index)
                assert current["status"] == "running"
                assert current["live_session"]["acquiring"]
                output = store.media_path(job_id, current["exports"][index - 1]["filename"])
                streams = json.loads(subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output),
                ], timeout=10))["streams"]
                assert {stream["codec_name"] for stream in streams} == {"h264", "aac"}
                video = next(stream for stream in streams if stream["codec_type"] == "video")
                assert (video["width"], video["height"]) == (1080, 1920)
                subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output),
                                "-f", "null", "-"], check=True, timeout=15)
            original = current["exports"][0]
            old_path = store.media_path(job_id, original["filename"])
            old_bytes = old_path.read_bytes()
            candidate_id = original["candidate_id"]
            retained = store.candidate_media_path(job_id, candidate_id)
            assert retained.is_file()
            store.edit(job_id, candidate_id, {
                "words": [{"start": 0.2, "end": 0.6, "text": "Test caption", "correction": "Edited"}],
                "face_crop": {"x": 0.1, "y": 0, "width": 0.4, "height": 1},
            })
            store.export(job_id, candidate_id)
            current = wait(lambda job: len(job["exports"]) == 3)
            assert current["live_session"]["acquiring"] and current["status"] == "running"
            assert old_path.read_bytes() == old_bytes
            revised = store.media_path(job_id, current["exports"][-1]["filename"])
            subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(revised),
                            "-f", "null", "-"], check=True, timeout=15)
            store.cancel(job_id)
            finished = wait(lambda job: job["status"] == "cancelled")
            assert len(finished["exports"]) == 3
            assert [record["window"]["first_sequence"] for record in finished["exports"][:2]] == [41, 42]
            assert finished["exports"][0]["global_end"] <= finished["exports"][1]["global_start"]
            assert all(store.media_path(job_id, item["filename"]).is_file() for item in finished["exports"])
            assert retained.is_file()
            store.edit(job_id, candidate_id, {"words": [
                {"start": 0.2, "end": 0.6, "text": "Test caption", "correction": "Final"},
            ]})
            store.export(job_id, candidate_id)
            after_stop = wait(lambda job: len(job["exports"]) == 4, allow_cancelled=True)
            assert after_stop["status"] == "cancelled"
            assert old_path.read_bytes() == old_bytes and retained.is_file()
        finally:
            store.close()


def test_rolling_real_speech_face_and_captions(tmp_path, monkeypatch):
    """Run actual analysis and rendering without accessing a broadcaster."""
    cache = Path.home() / ".cache/huggingface/hub/models--Systran--faster-whisper-small/snapshots"
    model = next((p.resolve() for p in sorted(cache.glob("*"))
                  if all((p / name).is_file() for name in ("config.json", "model.bin", "tokenizer.json"))), None)
    if model is None or not shutil.which("say") or not shutil.which("ffmpeg"):
        pytest.skip("Requires cached Whisper Small, macOS say, and FFmpeg")
    root = Path(__file__).resolve().parents[1]
    portrait = root / "tests/fixtures/official_portrait_barack_obama.jpg"
    speech = tmp_path / "speech.aiff"
    subprocess.run(["say", "-r", "150", "-o", str(speech),
                    "Hello everyone this is a live stream. Welcome to the game. "
                    "We are playing together and having a great time today."], check=True, timeout=15)
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-loop", "1", "-framerate", "30",
        "-i", str(portrait), "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=30",
        "-i", str(speech), "-filter_complex",
        "[0:v]scale=293:400[face];[1:v][face]overlay=30:80:shortest=1[v]",
        "-map", "[v]", "-map", "2:a", "-t", "8", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-g", "30", "-sc_threshold", "0", "-bf", "0",
        "-c:a", "aac", "-f", "hls", "-hls_time", "1",
        "-hls_segment_filename", str(tmp_path / "part-%d.ts"), str(tmp_path / "fixture.m3u8"),
    ], check=True, timeout=30)
    assert all((tmp_path / f"part-{i}.ts").is_file() for i in range(7))
    available = [1]
    lock, initial = threading.Lock(), threading.Event()

    def manifest(_count):
        with lock:
            count = available[0]
        initial.set()
        return 200, playlist(100, *(segment(f"part-{i}.ts") for i in range(count)))

    routes = {"/live": (200, manifest)}
    routes.update({f"/part-{i}.ts": (200, (tmp_path / f"part-{i}.ts").read_bytes()) for i in range(1, 7)})
    channel = "https://www.twitch.tv/fixture_channel"
    monkeypatch.setenv("STREAMFRAME_WHISPER_MODEL", str(model))
    monkeypatch.setenv("STREAMFRAME_DEVICE", "cpu")
    monkeypatch.setenv("STREAMFRAME_COMPUTE_TYPE", "int8")
    monkeypatch.setattr(sources, "resolve_source", lambda *_args, **_kwargs: sources.ResolvedSource(
        channel, "twitch", "Synthetic live", True, "fixture_channel", "live", "live"))

    with serve(routes) as (base, _seen):
        monkeypatch.setattr(live, "_resolve_hls", lambda *_args: base + "/live")
        monkeypatch.setattr(jobs, "RemoteFetch", lambda _event, _deadline: FixtureTransport(base))
        store = jobs.JobStore(tmp_path / "jobs")
        try:
            job_id = store.create({"type": "url", "url": channel, "mode": "live",
                                   "rolling": True, "session_duration": 120,
                                   "window_duration": 3})["id"]
            assert initial.wait(10)
            for index in (1, 2):
                with lock:
                    available[0] = 1 + index * 3
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    job = store.get(job_id)
                    if len(job["exports"]) >= index:
                        break
                    if job["status"] in jobs.TERMINAL:
                        raise AssertionError(job)
                    time.sleep(0.1)
                else:
                    raise AssertionError(store.get(job_id))
                candidate = next(c for c in job["candidates"] if c["id"] == job["exports"][-1]["candidate_id"])
                assert job["live_session"]["acquiring"]
                assert candidate["face_crop"] is not None
                assert candidate["words"], "real Whisper must return subtitle words"
                output = store.media_path(job_id, job["exports"][-1]["filename"])
                subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output),
                                "-f", "null", "-"], check=True, timeout=15)
            store.cancel(job_id)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and store.get(job_id)["status"] != "cancelled":
                time.sleep(0.05)
            assert len(store.get(job_id)["exports"]) == 2
        finally:
            store.close()
