from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from streamframe import jobs, live, render, sources


def playlist(sequence, *items, ended=False):
    return (
        f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:{sequence}\n"
        + "".join(items) + ("#EXT-X-ENDLIST\n" if ended else "")
    ).encode()


def segment(name, tags=""):
    return f"{tags}#EXTINF:1.0,\n{name}\n"


class FixtureTransport:
    """Localhost-only transport for synthetic, isolated tests, never production."""

    def __init__(self, base, remaining=10 * 1024**2):
        self.base = base
        self.remaining = remaining

        class FixtureRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, response, code, message, headers, new_url):
                if not new_url.startswith(base + "/"):
                    raise urllib.error.URLError("fixture redirects must stay on the test server")
                return super().redirect_request(request, response, code, message, headers, new_url)

        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), FixtureRedirect())

    @contextmanager
    def open(self, url):
        assert url.startswith(self.base + "/")
        with self.opener.open(url, timeout=1) as response:
            assert response.url.startswith(self.base + "/")
            yield response.url, response

    def chunks(self, response):
        while chunk := response.read(65536):
            self.remaining -= len(chunk)
            if self.remaining < 0:
                raise ValueError("fixture transfer limit")
            yield chunk


@contextmanager
def serve(routes):
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            seen.append(self.path)
            status, value = routes.get(self.path, (404, b""))
            if callable(value):
                status, value = value(seen.count(self.path))
            self.send_response(status)
            if isinstance(value, str):
                self.send_header("Location", value)
                value = b""
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            self.wfile.write(value)

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            yield base, seen
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def media_segments(tmp_path, request):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required for live media verification")
    size, rate = getattr(request, "param", ("128x72", 30))
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000", "-t", "4", "-c:v", "libx264",
        "-g", str(rate), "-sc_threshold", "0", "-bf", "0", "-c:a", "aac",
        "-f", "hls", "-hls_time", "1", "-hls_segment_filename", str(tmp_path / "fixture-%d.ts"),
        str(tmp_path / "fixture.m3u8"),
    ], check=True, timeout=15)
    return [(tmp_path / f"fixture-{i}.ts").read_bytes() for i in range(4)]


def capture(base, directory, *, duration=1, event=None, deadline=4, remaining=10 * 1024**2, **kwargs):
    alignment, progress = [], []
    target = live._capture(
        base + "/live", directory, duration, event or threading.Event(),
        lambda value, message: progress.append((value, message)), alignment.append,
        FixtureTransport(base, remaining), time.monotonic() + deadline, **kwargs,
    )
    return target, alignment, progress


def test_live_master_redirect_edge_alignment_and_reconnect(tmp_path, media_segments):
    now = "#EXT-X-PROGRAM-DATE-TIME:2026-03-01T10:00:00Z\n"
    master = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="external",URI="../must-not-fetch-audio"
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,AUDIO="external"
../must-not-fetch-variant
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360
../variant
"""
    snapshots = [
        playlist(17, segment("previous.ts", now)),
        playlist(17, segment("previous.ts", now), segment("one.ts")),
        playlist(18, segment("one.ts"), segment("two.ts")),
    ]
    routes = {
        "/live": (302, "./master/index.m3u8"), "/master/index.m3u8": (200, master),
        "/variant": (307, "./content/playlist/live.m3u8"),
        "/content/playlist/live.m3u8": (200, lambda n: (200, snapshots[min(n - 1, 2)])),
        "/content/playlist/one.ts": (302, "../one.ts"),
        "/content/one.ts": (200, lambda n: (503, b"") if n == 1 else (200, media_segments[1])),
        "/content/playlist/two.ts": (200, media_segments[2]),
    }
    with serve(routes) as (base, seen):
        output, alignment, progress = capture(base, tmp_path, duration=1.5)
    assert output.name == "source.mp4"
    assert alignment == ["2026-03-01T10:00:01+00:00"]
    assert any("Reconnecting" in message for _, message in progress)
    assert seen.count("/content/one.ts") == 2
    assert not any("previous.ts" in path or "must-not-fetch" in path for path in seen)
    probe = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=start_time,duration",
        "-of", "json", str(output),
    ], timeout=5)
    timeline = json.loads(probe)["format"]
    assert float(timeline["start_time"]) == pytest.approx(0, abs=0.05)
    assert 1.15 <= float(timeline["duration"]) <= 1.5
    assert not list(tmp_path.glob(".live-*"))


@pytest.mark.parametrize("scenario", [
    "complete", "backpressure", "cancel", "transfer", "disk", "clock_reset",
    "sequence_reset", "discontinuity", "deadline", "duration_drift",
])
def test_rolling_capture_writes_owned_contiguous_windows(tmp_path, monkeypatch, media_segments, scenario):
    initial = playlist(40, segment("previous.ts", "#EXT-X-PROGRAM-DATE-TIME:2026-03-01T10:00:00Z\n"))
    changed = initial + b"".join(segment(f"{i}.ts").encode() for i in range(1, 4))
    if scenario == "sequence_reset":
        changed = playlist(39, *(segment(f"{i}.ts") for i in range(1, 4)))
    elif scenario == "discontinuity":
        changed = initial + segment("1.ts").encode() + b"#EXT-X-DISCONTINUITY\n" + segment("2.ts").encode()
    elif scenario == "deadline":
        changed = initial + segment("1.ts").encode()
    elif scenario == "duration_drift":
        changed = initial + b"#EXTINF:0.4,\n1.ts\n" + segment("2.ts").encode()
    routes = {
        "/live": (200, lambda n: (200, initial if n == 1 else changed)),
        **{f"/{i}.ts": (200, media_segments[i]) for i in range(1, 4)},
    }
    if scenario == "clock_reset":
        routes["/2.ts"] = routes["/1.ts"]
    directory = tmp_path / "windows"
    directory.mkdir()
    event = threading.Event()
    disk_lock = threading.Lock()
    delivered, fetches, resolutions, messages, disk_checks = [], [], [], [], []

    def consume(path, metadata):
        assert path.suffix == ".ts" and not disk_lock.locked()
        assert path.read_bytes() == media_segments[len(delivered) + 1]
        if scenario == "backpressure" and delivered:
            raise live.LiveBackpressureError("queue full")
        delivered.append((path, metadata))

    def progress(_, message):
        messages.append(message)
        if scenario == "cancel" and message == "Captured 3s of live media":
            event.set()

    def disk_remaining():
        assert disk_lock.locked()
        existing = sum(path.stat().st_size for path in directory.iterdir())
        disk_checks.append(existing)
        return disk_cap - existing

    disk_cap = len(media_segments[1]) + 1 if scenario == "disk" else live.MAX_DISK_BYTES
    with serve(routes) as (base, seen):
        def metadata(*args):
            resolutions.append(args)
            return base + "/live"

        def transport(*args):
            fetches.append(args)
            return FixtureTransport(base)

        monkeypatch.setattr(live, "_resolve_hls", metadata)
        monkeypatch.setattr(jobs, "RemoteFetch", transport)
        if scenario == "transfer":
            # The second segment would fit if the first segment and both manifests
            # had been charged to different budgets.
            monkeypatch.setattr(live, "MAX_TRANSFER_BYTES",
                                sources.MAX_METADATA_BYTES + live.MAX_METADATA_BYTES
                                + len(initial) + len(changed) + len(media_segments[1]) + len(media_segments[2]) - 1)

        def run():
            resolved = SimpleNamespace(platform="twitch", is_live=True, url="https://www.twitch.tv/fixture")
            live.capture_windows(
                resolved, directory,
                session_seconds=1.2 if scenario == "deadline" else 3.2,
                window_seconds=1.5 if scenario in {"cancel", "duration_drift"} else 1,
                cancelled=event, progress=progress, on_window=consume, disk_remaining=disk_remaining,
                disk_lock=disk_lock,
            )

        errors = {
            "backpressure": (live.LiveBackpressureError, "queue full"),
            "cancel": (jobs.Cancelled, ""),
            "transfer": (live.LiveCaptureError, "transfer limit"),
            "disk": (live.LiveCaptureError, "disk byte limit"),
            "clock_reset": (live.LiveCaptureError, "clock gap or reset"),
            "sequence_reset": (live.LiveCaptureError, "sequence reset"),
            "discontinuity": (live.LiveCaptureError, "gap or discontinuity"),
            "duration_drift": (live.LiveCaptureError, "drifts from the MPEG-TS clock"),
        }
        if scenario in errors:
            exception, message = errors[scenario]
            with pytest.raises(exception, match=message):
                run()
        else:
            run()
    assert len(fetches) == len(resolutions) == 1
    assert "/previous.ts" not in seen
    expected_count = {"complete": 3, "cancel": 2, "sequence_reset": 0, "duration_drift": 0}.get(scenario, 1)
    assert len(delivered) == expected_count
    assert set(directory.iterdir()) == {path for path, _ in delivered}
    for i, (_, metadata) in enumerate(delivered):
        assert metadata == {
            "first_sequence": 41 + i, "last_sequence": 41 + i,
            "start": float(i), "end": float(i + 1), "started_at": f"2026-03-01T10:00:0{i + 1}+00:00",
        }
    if scenario in {"complete", "cancel"}:
        assert disk_checks == [0, len(media_segments[1]), len(media_segments[1]) + len(media_segments[2])]
    if scenario == "deadline":
        assert "deadline reached; acquired 1/1.2s" in messages[-1]


@pytest.mark.parametrize("scenario", ["complete", "deadline", "cancel", "transfer", "disk", "segments"])
def test_long_rolling_session_keeps_one_cursor_and_resource_budget(tmp_path, monkeypatch, scenario):
    class VirtualEvent:
        now = 0.0
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, seconds):
            self.now += seconds * (0.75 if scenario == "complete" else 1)
            return self.stopped

    event = VirtualEvent()
    monkeypatch.setattr(live.time, "monotonic", lambda: event.now)
    if scenario == "transfer":
        monkeypatch.setattr(live, "MAX_TRANSFER_BYTES",
                            sources.MAX_METADATA_BYTES + live.MAX_METADATA_BYTES + 145_000)
    if scenario == "segments":
        monkeypatch.setattr(live, "MAX_SEGMENTS", 165)
    directory = tmp_path / "windows"
    directory.mkdir()
    messages, delivered, transports, requests = [], [], [], []
    resolutions = []

    def ts_segment(index):
        packets = []
        for offset in range(4):
            pcr = (index * 180_000 + offset * 45_000) << 7
            packet = bytearray(188)
            packet[:6] = b"\x47\x00\x01\x20\x07\x10"
            packet[6:11] = pcr.to_bytes(5, "big")
            packets.append(packet)
        return b"".join(packets)

    class Response:
        def __init__(self, body, retry):
            self.body, self.retry = body, retry

        def getheader(self, name):
            return str(len(self.body)) if name == "Content-Length" else None

    class Transport:
        def __init__(self, cancelled, deadline):
            self.remaining = 0
            self.spent = 0
            self.manifests = 0
            self.retried = False
            self.deadline = deadline
            transports.append(self)

        @contextmanager
        def open(self, url):
            requests.append(url)
            if url.endswith("/live"):
                self.manifests += 1
                index = self.manifests - 2
                body = playlist(99, segment("old.ts")) if index < 0 else playlist(
                    100 + index, f"#EXTINF:2,\n{index}.ts\n"
                )
                body = body.replace(b"TARGETDURATION:1", b"TARGETDURATION:4")
                retry = False
            else:
                index = int(url.rsplit("/", 1)[1].removesuffix(".ts"))
                body = ts_segment(index)
                retry = index == 160 and not self.retried
                self.retried |= retry
            yield url, Response(body, retry)

        def chunks(self, response):
            body = response.body[:32] if response.retry else response.body
            self.remaining -= len(body)
            self.spent += len(body)
            if self.remaining < 0:
                raise ValueError("transfer limit")
            yield body
            if response.retry:
                raise OSError("transient interruption")

    def resolve(*args):
        resolutions.append(args)
        return "https://example.com/live"

    def progress(_, message):
        messages.append(message)
        if scenario == "cancel" and message == "Captured 610s of live media":
            event.set()

    disk_cap = len(ts_segment(0)) * 165 if scenario == "disk" else live.MAX_DISK_BYTES
    monkeypatch.setattr(live, "_resolve_hls", resolve)
    monkeypatch.setattr(jobs, "RemoteFetch", Transport)
    resolved = SimpleNamespace(platform="twitch", is_live=True, url="https://www.twitch.tv/fixture")

    def run():
        live.capture_windows(
            resolved, directory, session_seconds=1800 if scenario == "complete" else 720,
            window_seconds=10, cancelled=event,
            progress=progress, on_window=lambda path, metadata: delivered.append((path, metadata)),
            disk_remaining=lambda: disk_cap - sum(path.stat().st_size for path in directory.iterdir()),
        )

    errors = {
        "cancel": (jobs.Cancelled, ""),
        "transfer": (live.LiveCaptureError, "transfer limit"),
        "disk": (live.LiveCaptureError, "disk byte limit"),
        "segments": (live.LiveCaptureError, "cumulative segment limit"),
    }
    if scenario in errors:
        exception, message = errors[scenario]
        with pytest.raises(exception, match=message):
            run()
    else:
        run()
    assert len(resolutions) == len(transports) == 1
    assert transports[0].remaining == (live.MAX_TRANSFER_BYTES - sources.MAX_METADATA_BYTES
                                       - live.MAX_METADATA_BYTES - transports[0].spent)
    assert event.now > 300
    assert any("Reconnecting to live segment" in message for message in messages)
    assert requests.count("https://example.com/160.ts") == 2
    assert all(item["first_sequence"] == 100 + index * 5 and
               item["start"] == index * 10 for index, (_, item) in enumerate(delivered))
    assert all(item["last_sequence"] == 104 + index * 5 for index, (_, item) in enumerate(delivered)
               if item["end"] - item["start"] == 10)
    assert set(directory.iterdir()) == {path for path, _ in delivered}
    if scenario == "complete":
        assert delivered[-1][1]["end"] == 1800
        assert "media cap reached at 1800s" in messages[-1]
    if scenario == "deadline":
        assert event.now >= 720
        assert "wall-clock deadline reached" in messages[-1]


@pytest.mark.parametrize("duration", [1801, float("inf"), float("nan"), True])
def test_rolling_rejects_duration_over_cap_before_resolution(tmp_path, duration):
    with pytest.raises(live.LiveCaptureError, match="between 1 and 1800"):
        live.capture_windows(
            None, tmp_path, session_seconds=duration, window_seconds=1,
            cancelled=threading.Event(), progress=lambda *_: None,
            on_window=lambda *_: None, disk_remaining=lambda: live.MAX_DISK_BYTES,
        )


def test_rolling_publish_cannot_rename_during_disk_scan(tmp_path, monkeypatch, media_segments):
    directory = tmp_path / "windows"
    directory.mkdir()
    initial = playlist(40, segment("previous.ts"))
    routes = {
        "/live": (200, lambda n: (200, initial if n == 1 else initial + segment("next.ts").encode())),
        "/next.ts": (200, media_segments[1]),
    }
    quota_lock = threading.Lock()
    ready_to_scan = threading.Event()
    scan_listed = threading.Event()
    publish_attempt = threading.Event()
    delivered = []

    class ProducerLock:
        def acquire(self, *, timeout):
            if scan_listed.is_set():
                publish_attempt.set()
            return quota_lock.acquire(timeout=timeout)

        def release(self):
            quota_lock.release()
            if not ready_to_scan.is_set():
                ready_to_scan.set()
                assert scan_listed.wait(5)

    original_rename = Path.rename

    def observe_rename(path, target):
        result = original_rename(path, target)
        if path.parent == directory and path.suffix == ".part":
            publish_attempt.set()
        return result

    monkeypatch.setattr(Path, "rename", observe_rename)
    with serve(routes) as (base, _), ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(live, "_resolve_hls", lambda *_: base + "/live")
        monkeypatch.setattr(jobs, "RemoteFetch", lambda *_: FixtureTransport(base))
        resolved = SimpleNamespace(platform="twitch", is_live=True, url="https://www.twitch.tv/fixture")
        producer = pool.submit(
            live.capture_windows, resolved, directory, session_seconds=1.5, window_seconds=1,
            cancelled=threading.Event(), progress=lambda *_: None,
            on_window=lambda path, _: delivered.append(path),
            disk_remaining=lambda: live.MAX_DISK_BYTES, disk_lock=ProducerLock(),
        )
        try:
            assert ready_to_scan.wait(5)
            with quota_lock:
                [listed] = directory.iterdir()
                assert listed.suffix == ".part"
                scan_listed.set()
                assert publish_attempt.wait(5)
                assert listed.stat().st_size == len(media_segments[1])
        finally:
            scan_listed.set()
            producer.result(timeout=5)
    [published] = delivered
    assert published == listed.with_suffix(".ts")
    assert not listed.exists()


def test_jobstore_live_hls_analyze_and_export(tmp_path, monkeypatch, media_segments):
    configured_model = os.environ.get("STREAMFRAME_WHISPER_MODEL", "small")
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    snapshots = cache / f"models--Systran--faster-whisper-{configured_model}" / "snapshots"
    model = next((path.resolve() for path in [Path(configured_model).expanduser(), *sorted(snapshots.glob("*"))]
                  if all((path / name).is_file() for name in ("config.json", "model.bin", "tokenizer.json"))), None)
    if model is None:
        pytest.skip("Full live job test requires a locally cached faster-whisper model")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("STREAMFRAME_WHISPER_MODEL", str(model))
    monkeypatch.setenv("STREAMFRAME_DEVICE", "cpu")
    monkeypatch.setenv("STREAMFRAME_COMPUTE_TYPE", "int8")

    url = "https://www.twitch.tv/fixture_channel"
    initial = playlist(40, segment("previous.ts", "#EXT-X-PROGRAM-DATE-TIME:2026-03-01T10:00:00Z\n"))
    extended = initial + segment("one.ts").encode() + segment("two.ts").encode() + segment("three.ts").encode()
    waiting = threading.Event()
    stalled = threading.Event()

    def manifest(count):
        if stalled.is_set():
            waiting.set()
            return 200, initial
        return 200, initial if count == 1 else extended

    routes = {
        "/live": (200, manifest),
        "/one.ts": (200, media_segments[1]),
        "/two.ts": (200, media_segments[2]),
        "/three.ts": (200, media_segments[3]),
    }

    def resolve(identifier, *, mode, platform, cancel_event):
        assert identifier == url and mode == "live"
        assert not cancel_event.is_set()
        return sources.ResolvedSource(url, "twitch", "Synthetic live", True, "fixture_channel", mode, "live")

    def wait_terminal(store, job_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with store.lock:
                job = store.get(job_id)
                if job["status"] in jobs.TERMINAL and job_id not in store.active:
                    return job
            time.sleep(0.05)
        pytest.fail(f"Job did not finish: {store.get(job_id)}")

    def probe(path):
        return json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,width,height:format=start_time,duration",
            "-of", "json", str(path),
        ], timeout=5))

    with serve(routes) as (base, seen):
        def metadata(extractor, source_url, directory, event, deadline):
            assert (extractor, source_url) == ("twitch:stream", url)
            return base + "/live"

        monkeypatch.setattr(sources, "resolve_source", resolve)
        monkeypatch.setattr(live, "_resolve_hls", metadata)
        monkeypatch.setattr(jobs, "RemoteFetch", lambda event, deadline: FixtureTransport(base))

        root = tmp_path / "jobs"
        store = jobs.JobStore(root)
        try:
            source = {"type": "url", "url": url, "mode": "live", "duration": 2.8}
            job_id = store.create(source)["id"]
            analyzed = wait_terminal(store, job_id)
            assert analyzed["status"] == "succeeded", analyzed["error"]
            assert analyzed["capture_started_at"] == "2026-03-01T10:00:01+00:00"
            assert "/previous.ts" not in seen
            assert [path for path in seen if path.endswith(".ts")] == ["/one.ts", "/two.ts", "/three.ts"]
            preview = probe(store.media_path(job_id))
            duration = float(preview["format"]["duration"])
            assert 2.45 <= duration <= 2.8
            assert float(preview["format"]["start_time"]) == pytest.approx(0, abs=0.05)
            assert {stream["codec_name"] for stream in preview["streams"]} == {"h264", "aac"}
            assert analyzed["candidates"], "real analysis should produce a visual candidate"
            candidate = analyzed["candidates"][0]
            assert 0 <= candidate["start"] < candidate["end"] <= duration + 0.001

            # Render a real caption in a nonzero source-time window.
            store.edit(job_id, candidate["id"], {
                "start": 0.4, "end": 1.8,
                "face_crop": {"x": 0.0, "y": 0.0, "width": 0.4, "height": 0.4},
                "words": [{"start": 0.6, "end": 1.5, "text": "Local timebase"}],
            })
            store.export(job_id, candidate["id"])
            exported = wait_terminal(store, job_id)
            assert exported["status"] == "succeeded", exported["error"]
            [export] = exported["exports"]
            assert export["candidate_id"] == candidate["id"]
            output = probe(store.media_path(job_id, export["filename"]))
            assert float(output["format"]["start_time"]) == pytest.approx(0, abs=0.05)
            # Editor exports carry the bundled Lobby Crew preset, so the branded
            # outro extends the clip past its 1.4-second trim.
            assert float(output["format"]["duration"]) == pytest.approx(
                1.4 + render.OUTRO_SECONDS, abs=0.08)
            assert {stream["codec_name"] for stream in output["streams"]} == {"h264", "aac"}
            video = next(stream for stream in output["streams"] if stream["codec_type"] == "video")
            assert (video["width"], video["height"]) == (1080, 1920)

            stalled.set()
            cancelled_id = store.create(source)["id"]
            assert waiting.wait(5), "worker never reached the live fixture"
            store.cancel(cancelled_id)
            assert wait_terminal(store, cancelled_id, timeout=5)["status"] == "cancelled"
            assert not list((root / cancelled_id).glob("*.mp4"))
            assert not list(root.glob("*/.live-*"))
        finally:
            store.close()


@pytest.mark.parametrize(("updated", "message"), [
    (playlist(15, segment("never.ts")), "sequence gap"),
    (playlist(14, segment("never.ts", "#EXT-X-DISCONTINUITY\n")), "gap or discontinuity"),
    (playlist(14, segment("never.ts", "#EXT-X-GAP\n")), "gap or discontinuity"),
    (playlist(14, segment("never.ts", '#EXT-X-KEY:METHOD=AES-128,URI="/key"\n')), "Encrypted"),
    (playlist(14, segment("never.ts", '#EXT-X-MAP:URI="/init.mp4"\n')), "Unsupported HLS"),
    (playlist(14, ended=True), "ended before the requested"),
])
def test_live_fails_closed_before_requesting_unsafe_or_missing_segments(tmp_path, updated, message):
    routes = {"/live": (200, lambda n: (200, playlist(13, segment("old.ts")) if n == 1 else updated))}
    with serve(routes) as (base, seen):
        with pytest.raises(live.LiveCaptureError, match=message):
            capture(base, tmp_path)
    assert seen == ["/live", "/live"]
    assert not list(tmp_path.iterdir())


def test_ended_and_separate_audio_are_not_captured(tmp_path):
    ended = playlist(1, segment("old.ts"), ended=True)
    separate = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200,AUDIO="a"\nvideo.m3u8\n'
    for data, error in [(ended, "ended before capture began"), (separate, "Separate HLS audio")]:
        with serve({"/live": (200, data)}) as (base, seen):
            with pytest.raises(live.LiveCaptureError, match=error):
                capture(base, tmp_path)
        assert seen == ["/live"]


def test_live_disk_and_transfer_bounds(tmp_path, media_segments):
    routes = {
        "/live": (200, lambda n: (200, playlist(1, segment("old")) if n == 1 else playlist(2, segment("new")))),
        "/new": (200, media_segments[1]),
    }
    for options, message in [
        ({"disk_limit": len(media_segments[1]) - 1}, "segment exceeds"),
        ({"disk_limit": len(media_segments[1]) + 128}, "disk byte limit"),
        ({"remaining": 500}, "transfer limit"),
    ]:
        with serve(routes) as (base, _):
            with pytest.raises(live.LiveCaptureError, match=message):
                capture(base, tmp_path, **options)
        assert not list(tmp_path.glob(".live-*"))
        assert not (tmp_path / "source.mp4").exists()


def test_unmarked_mpegts_clock_reset_is_not_hidden_by_encoding(tmp_path, media_segments):
    routes = {
        "/live": (200, lambda n: (
            200, playlist(1, segment("previous")) if n == 1
            else playlist(2, segment("part-a"), segment("part-b"))
        )),
        "/part-a": (200, media_segments[1]),
        "/part-b": (200, media_segments[1]),
    }
    with serve(routes) as (base, _):
        with pytest.raises(live.LiveCaptureError, match="packet timestamp gap or reset"):
            capture(base, tmp_path, duration=1.5)
    assert not list(tmp_path.glob(".live-*"))
    assert not (tmp_path / "source.mp4").exists()


def test_program_date_time_jump_is_reported_before_downloading_next_segment(tmp_path, media_segments):
    routes = {
        "/live": (200, lambda n: (
            200, playlist(1, segment("previous")) if n == 1
            else playlist(2, segment("part-a", "#EXT-X-PROGRAM-DATE-TIME:2026-03-01T10:00:00Z\n"),
                          segment("part-b", "#EXT-X-PROGRAM-DATE-TIME:2026-03-01T10:00:30Z\n"))
        )),
        "/part-a": (200, media_segments[1]),
    }
    with serve(routes) as (base, seen):
        with pytest.raises(live.LiveCaptureError, match="timestamp gap"):
            capture(base, tmp_path, duration=1.5)
    assert "/part-b" not in seen


@pytest.mark.parametrize("duration", [0, 0.5, 301, float("inf"), float("nan"), True])
def test_live_rejects_out_of_range_duration_before_resolution(tmp_path, duration):
    with pytest.raises(live.LiveCaptureError, match="between 1 and 300"):
        live.capture_live(None, tmp_path, duration=duration, cancelled=threading.Event(), progress=lambda *_: None)


def test_live_stalled_playlist_times_out_or_cancels(tmp_path):
    routes = {"/live": (200, playlist(1, segment("old")))}
    with serve(routes) as (base, _):
        with pytest.raises(live.LiveCaptureError, match="timed out"):
            capture(base, tmp_path, deadline=0.1)
        cancelled = threading.Event()
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        try:
            with pytest.raises(jobs.Cancelled):
                capture(base, tmp_path, event=cancelled)
        finally:
            timer.cancel()
            timer.join()
    assert not list(tmp_path.iterdir())


def test_cancellation_kills_subprocess_group(tmp_path):
    marker = tmp_path / "descendant-survived"
    grandchild = (
        "import pathlib,sys,time; time.sleep(0.8); pathlib.Path(sys.argv[1]).touch()"
    )
    script = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);"
        " print('started',flush=True); time.sleep(30)"
    )
    event = threading.Event()
    with pytest.raises(jobs.Cancelled):
        live._run([sys.executable, "-c", script, grandchild, str(marker)],
                  event, time.monotonic() + 2, lambda _: event.set())
    time.sleep(0.9)
    assert not marker.exists()


def test_production_reader_rejects_non_https_and_local_hosts_without_connecting():
    event = threading.Event()
    deadline = time.monotonic() + 2
    fetch = jobs.RemoteFetch(event, deadline)
    for url in ["file:///etc/hosts", "http://127.0.0.1/index.m3u8", "https://127.0.0.1/stream.ts"]:
        with pytest.raises(live.LiveCaptureError, match="rejected by the download guard"):
            live._read(fetch, url, 4096, "segment", event, deadline, lambda *_: None, 0)


@pytest.mark.parametrize(("platform", "url", "extractor"), [
    ("twitch", "https://www.twitch.tv/public_channel", "twitch:stream"),
    ("youtube", "https://www.youtube.com/watch?v=abcdefghijk", "youtube"),
])
def test_canonical_public_live_page(platform, url, extractor):
    assert live._page(SimpleNamespace(platform=platform, url=url, is_live=True)) == (extractor, url)
