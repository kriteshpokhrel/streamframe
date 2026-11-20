import os
import json
import shutil
import subprocess
from contextlib import contextmanager
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from streamframe import jobs, sources, youtube_vod as vod


PAGE = "https://www.youtube.com/watch?v=qSyo5d-dVzA"
CDN = "https://manifest.googlevideo.com/test/"
MANIFEST = CDN + "index.m3u8"
PLAYLIST = b"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-TARGETDURATION:5
#EXTINF:5,
a.ts
#EXTINF:5,
b.ts
#EXTINF:5,
c.ts
#EXTINF:5,
d.ts
#EXT-X-ENDLIST
"""
TS = b"\x47" + b"\x00" * 187


def info(**overrides):
    return {
        "id": "qSyo5d-dVzA", "title": "Synthetic archive", "availability": "public",
        "live_status": "was_live", "duration": 20,
        "formats": [{
            "url": MANIFEST, "protocol": "m3u8_native",
            "vcodec": "avc1", "acodec": "mp4a.40.2", "height": 360,
        }],
        **overrides,
    }


class FixtureFetch:
    def __init__(self, event, deadline):
        self.event, self.deadline = event, deadline
        self.remaining = jobs.MAX_REMOTE_BYTES
        self.opened = []
        self.read = []
        self.bodies = {MANIFEST: PLAYLIST, **{CDN + c + ".ts": TS for c in "abcd"}}
        self.headers = {}
        self.redirects = {}

    def check(self):
        if self.event.is_set():
            raise jobs.Cancelled()

    @contextmanager
    def open(self, url):
        self.check()
        self.validate_url(url)
        self.opened.append(url)
        if url in self.redirects:
            url = self.redirects[url]
            self.validate_url(url)
            self.opened.append(url)
        default = {
            "Content-Length": str(len(self.bodies[url])),
            "Content-Type": "application/x-mpegurl" if url == MANIFEST else "video/mp2t",
        }

        class Response:
            def getheader(_, name, fallback=None):
                return self.headers.get((url, name), default.get(name, fallback))

        yield url, Response()

    def chunks(self, _response):
        url = self.opened[-1]
        self.read.append(url)
        for offset in range(0, len(self.bodies[url]), 137):
            part = self.bodies[url][offset:offset + 137]
            self.remaining -= len(part)
            if self.remaining < 0:
                raise ValueError("Exceeded test transfer limit")
            yield part


@pytest.fixture
def extraction(monkeypatch):
    event = threading.Event()
    fetch = FixtureFetch(event, 12345)

    def remote_fetch(*_args, validate_url):
        fetch.validate_url = validate_url
        return fetch

    monkeypatch.setattr(jobs, "RemoteFetch", remote_fetch)
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info())
    return event, fetch


def test_selective_import_reads_only_overlapping_segments_and_trims_local_fd(extraction):
    event, fetch = extraction
    resolved = []

    def trim(fd, directory, start, end, cancel, **options):
        assert os.read(fd, 1000) == TS * 2
        assert (start, end) == (1, 7)
        assert cancel is event and options == {"deadline": 12345, "staged": True}
        return "output.mp4"

    with patch.object(jobs, "trim_media", side_effect=trim):
        assert vod.import_youtube_vod(PAGE, ".", event, 6, 12,
                                      on_resolved=lambda *args: resolved.append(args)) == "output.mp4"
    assert fetch.opened == [MANIFEST, CDN + "b.ts", CDN + "c.ts"]
    assert resolved == [("Synthetic archive", 12)]

@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="FFmpeg tools unavailable")
def test_job_imports_selected_youtube_hls_range_with_real_media(tmp_path, monkeypatch):
    manifest = tmp_path / "index.m3u8"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=8",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8",
        "-c:v", "libx264", "-g", "20", "-sc_threshold", "0", "-c:a", "aac",
        "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments", str(manifest),
    ], check=True, capture_output=True)
    fetches = []

    def remote_fetch(event, deadline, *, validate_url):
        fetch = FixtureFetch(event, deadline)
        fetch.validate_url = validate_url
        fetch.bodies = {MANIFEST: manifest.read_bytes()}
        for segment in tmp_path.glob("index*.ts"):
            fetch.bodies[CDN + segment.name] = segment.read_bytes()
        fetches.append(fetch)
        return fetch

    monkeypatch.setattr(jobs, "RemoteFetch", remote_fetch)
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(duration=8))
    store = jobs.JobStore(tmp_path / "data", [tmp_path], lambda *_: {"candidates": []}, lambda *_: None)
    try:
        job = store.create({"type": "url", "url": PAGE, "start": 2.5, "end": 5.5})
        limit = time.monotonic() + 30
        while time.monotonic() < limit:
            job = store.get(job["id"])
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert job["status"] == "succeeded", job
        assert job["resolved_source"]["platform"] == "youtube"
        assert fetches[0].opened == [MANIFEST, CDN + "index1.ts", CDN + "index2.ts"]
        details = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
            str(store.media_path(job["id"])),
        ]))
        assert {stream["codec_type"] for stream in details["streams"]} >= {"video", "audio"}
        assert abs(float(details["format"]["duration"]) - 3) < 0.1
    finally:
        store.close()


def test_reuses_index_and_will_not_retrieve_whole_archive_across_windows(extraction):
    event, fetch = extraction
    archive = vod.prepare_youtube_vod(PAGE, event)
    with patch.object(jobs, "trim_media", return_value="trim.mp4"):
        assert archive.acquire(".", 0, 9) == "trim.mp4"
        assert archive.acquire(".", 10, 14) == "trim.mp4"
        with pytest.raises(vod.YouTubeVodUnavailable, match="entire archive"):
            archive.acquire(".", 14, 17)
    assert fetch.opened == [MANIFEST, *(CDN + c + ".ts" for c in "abc")]


@pytest.mark.parametrize("line", [
    b"#EXT-X-MAP:URI=\"init.mp4\"", b"#EXT-X-BYTERANGE:188@0",
    b"#EXT-X-KEY:METHOD=AES-128,URI=\"secret\"", b"#EXT-X-SESSION-KEY:METHOD=AES-128",
    b"#EXT-X-DEFINE:NAME=\"a\",VALUE=\"b\"", b"#EXT-X-DISCONTINUITY",
    b"#EXT-X-GAP", b"#EXT-X-MEDIA-SEQUENCE:39099", b"#EXT-X-PLAYLIST-TYPE:EVENT",
])
def test_rejects_unsupported_media_addressing_without_fetching_any_segments(extraction, line):
    event, fetch = extraction
    fetch.bodies[MANIFEST] = PLAYLIST.replace(b"#EXT-X-VERSION:3", line)
    with pytest.raises(vod.YouTubeVodUnavailable):
        vod.prepare_youtube_vod(PAGE, event)
    assert fetch.opened == [MANIFEST]


@pytest.mark.parametrize("body", [
    PLAYLIST.replace(b"#EXT-X-ENDLIST", b""), PLAYLIST.replace(b"d.ts", b"b.ts"),
    PLAYLIST.replace(b"b.ts", b"https://127.0.0.1/foo"),
    PLAYLIST.replace(b"b.ts", b"https://manifest.googlevideo.com.evil.example/foo"),
    PLAYLIST.replace(b"b.ts", b"https://user@manifest.googlevideo.com/foo"),
    PLAYLIST.replace(b"#EXT-X-INDEPENDENT-SEGMENTS", b""),
    PLAYLIST.replace(b"a.ts", b"a.ts\n#EXTINF:5,\nx.ts"),
    b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=400000\nmedia.m3u8\n",
])
def test_rejects_unverified_index(extraction, body):
    event, fetch = extraction
    fetch.bodies[MANIFEST] = body
    with pytest.raises(vod.YouTubeVodUnavailable):
        vod.prepare_youtube_vod(PAGE, event)
    assert fetch.opened == [MANIFEST]


@pytest.mark.parametrize(("metadata", "message"), [
    (info(live_status="post_live", duration=85542), "still processing"),
    (info(formats=[{"protocol": "http_dash_segments", "url": CDN + "archive.mpd"}]), "no supported muxed HLS"),
    (info(formats=[{"protocol": "https", "url": CDN + "direct.mp4"}]), "no supported muxed HLS"),
    (info(formats=[{**info()["formats"][0], "acodec": "none"}]), "no supported muxed HLS"),
    (info(availability="subscriber_only"), "finished, public"),
    (info(has_drm=True), "unencrypted"),
])
def test_unavailable_never_uses_media_fallback(extraction, monkeypatch, metadata, message):
    event, fetch = extraction
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: metadata)
    with pytest.raises(vod.YouTubeVodUnavailable, match=message):
        vod.prepare_youtube_vod(PAGE, event)
    assert not fetch.opened


def test_byte_limits_and_false_manifest_type_close_before_read(extraction):
    event, fetch = extraction
    fetch.headers[(MANIFEST, "Content-Type")] = "video/mp4"
    with pytest.raises(vod.YouTubeVodUnavailable, match="did not return an HLS playlist"):
        vod.prepare_youtube_vod(PAGE, event)
    assert not fetch.read
    del fetch.headers[(MANIFEST, "Content-Type")]
    archive = vod.prepare_youtube_vod(PAGE, event)
    fetch.headers[(CDN + "b.ts", "Content-Type")] = "video/mp4"
    with pytest.raises(vod.YouTubeVodUnavailable, match="did not return an MPEG-TS"):
        archive.acquire(".", 6, 7)
    del fetch.headers[(CDN + "b.ts", "Content-Type")]
    fetch.headers[(CDN + "b.ts", "Content-Length")] = str(vod.MAX_SEGMENT_BYTES + 1)
    with pytest.raises(vod.YouTubeVodUnavailable, match="byte limit"):
        archive.acquire(".", 6, 7)
    assert fetch.read == [MANIFEST]


@pytest.mark.parametrize("redirect_from", [MANIFEST, CDN + "b.ts"])
def test_unrelated_public_redirect_is_rejected_before_connection(extraction, redirect_from):
    event, fetch = extraction
    fetch.redirects[redirect_from] = "https://example.com/redirected"
    if redirect_from == MANIFEST:
        with pytest.raises(vod.YouTubeVodUnavailable, match="invalid CDN URL"):
            vod.prepare_youtube_vod(PAGE, event)
        assert fetch.opened == [MANIFEST]
        assert fetch.read == []
    else:
        archive = vod.prepare_youtube_vod(PAGE, event)
        with pytest.raises(vod.YouTubeVodUnavailable, match="invalid CDN URL"):
            archive.acquire(".", 6, 7)
        assert fetch.opened == [MANIFEST, CDN + "b.ts"]
        assert fetch.read == [MANIFEST]


def test_cancellation_during_body_read_propagates_unchanged(extraction):
    event, fetch = extraction
    archive = vod.prepare_youtube_vod(PAGE, event)
    cancellation = jobs.Cancelled()
    with patch.object(fetch, "chunks", side_effect=cancellation), patch.object(jobs, "trim_media") as trim:
        with pytest.raises(jobs.Cancelled) as error:
            archive.acquire(".", 6, 7)
        assert error.value is cancellation
        trim.assert_not_called()


def test_real_transport_rejects_redirect_before_dns_or_second_connect(monkeypatch):
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info())
    with (
        patch.object(jobs, "public_target", return_value=(2, 1, 6, "", ("192.0.2.1", 443))) as dns,
        patch.object(jobs.socket, "socket", return_value=MagicMock()) as socket,
        patch.object(jobs.ssl, "create_default_context"),
        patch.object(jobs.http.client, "HTTPSConnection") as https,
    ):
        response = https.return_value.getresponse.return_value
        response.status = 302
        response.getheader.return_value = "https://example.com/redirected"
        with pytest.raises(vod.YouTubeVodUnavailable, match="invalid CDN URL"):
            vod.prepare_youtube_vod(PAGE, threading.Event(), deadline=time.monotonic() + 5)
        assert [call.args[0] for call in dns.call_args_list] == [MANIFEST]
        assert socket.call_count == https.call_count == 1
        socket.return_value.__enter__.return_value.connect.assert_called_once()
        response.read1.assert_not_called()
        response.close.assert_called_once()


def test_truncated_and_non_ts_bodies_never_reach_ffmpeg(extraction):
    event, fetch = extraction
    archive = vod.prepare_youtube_vod(PAGE, event)
    fetch.headers[(CDN + "b.ts", "Content-Length")] = str(len(TS) + 1)
    with patch.object(jobs, "trim_media") as trim:
        with pytest.raises(vod.YouTubeVodUnavailable, match="truncated"):
            archive.acquire(".", 6, 7)
        fetch.bodies[CDN + "c.ts"] = b"x" * len(TS)
        with pytest.raises(vod.YouTubeVodUnavailable, match="not a complete MPEG-TS"):
            archive.acquire(".", 11, 12)
        trim.assert_not_called()


def test_many_windows_share_one_segment_limit_and_cancellation(extraction, monkeypatch):
    event, fetch = extraction
    archive = vod.prepare_youtube_vod(PAGE, event)
    monkeypatch.setattr(jobs, "MAX_REMOTE_SEGMENTS", 1)
    with patch.object(jobs, "trim_media"):
        archive.acquire(".", 6, 7)
        with pytest.raises(vod.YouTubeVodUnavailable, match="cumulative"):
            archive.acquire(".", 11, 12)
        event.set()
        with pytest.raises(jobs.Cancelled):
            archive.acquire(".", 11, 12)
    assert fetch.opened == [MANIFEST, CDN + "b.ts"]


def test_window_uses_verified_duration_and_byte_budget(extraction):
    event, fetch = extraction
    archive = vod.prepare_youtube_vod(PAGE, event)
    assert archive.duration == 20
    with patch.object(jobs, "trim_media", return_value="trim.mp4") as trim:
        with pytest.raises(ValueError, match="exceeds the completed"):
            archive.acquire(".", 17, 25)
        assert archive.acquire(".", 17, 25, allow_shorter=True) == "trim.mp4"
        assert trim.call_args.args[2:4] == (2, 5)
        fetch.remaining = len(TS) - 1
        with pytest.raises(vod.YouTubeVodUnavailable, match="byte limit"):
            archive.acquire(".", 11, 12)
        assert trim.call_count == 1
    assert fetch.read == [MANIFEST, CDN + "d.ts"]
