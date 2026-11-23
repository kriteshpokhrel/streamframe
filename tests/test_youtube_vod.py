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
        "live_status": "was_live", "duration": 20, "language": "en-US",
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
    fetch = FixtureFetch(event, time.monotonic() + 120)

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
        assert cancel is event and options == {"deadline": fetch.deadline, "staged": True}
        return "output.mp4"

    with patch.object(jobs, "trim_media", side_effect=trim):
        assert vod.import_youtube_vod(PAGE, ".", event, 6, 12,
                                      on_resolved=lambda *args: resolved.append(args)) == "output.mp4"
    assert fetch.opened == [MANIFEST, CDN + "b.ts", CDN + "c.ts"]
    assert resolved == [("Synthetic archive", 12)]


def test_separate_video_and_audio_hls_supports_a_range(extraction, monkeypatch, tmp_path):
    event, fetch = extraction
    video_manifest, audio_manifest = CDN + "video.m3u8", CDN + "audio.m3u8"

    def playlist(names):
        return ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n"
                "#EXT-X-TARGETDURATION:5\n"
                + "".join(f"#EXTINF:5,\n{CDN}{name}.ts\n" for name in names)
                + "#EXT-X-ENDLIST\n").encode()

    fetch.bodies[video_manifest] = playlist([f"v{i}" for i in range(4)])
    fetch.bodies[audio_manifest] = playlist([f"a{i}" for i in range(4)])
    for manifest in (video_manifest, audio_manifest):
        fetch.headers[(manifest, "Content-Type")] = "application/x-mpegurl"
    for index in range(4):
        fetch.bodies[CDN + f"v{index}.ts"] = TS
        fetch.bodies[CDN + f"a{index}.ts"] = b"ID3" + b"\x00" * 40
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(formats=[
        dict(url=video_manifest, protocol="m3u8_native", vcodec="avc1", acodec="none",
             height=720),
        dict(url=audio_manifest, protocol="m3u8_native", vcodec="none", acodec="mp4a.40.2",
             language="en-US", language_preference=10),
    ]))

    def trim(fd, directory, start, end, cancel, **options):
        assert (start, end) == (1, 7)
        assert options["audio_staged"] is False
        assert options["audio_adts"] is True
        return "separate.mp4"

    with patch.object(jobs, "trim_media", side_effect=trim):
        assert vod.import_youtube_vod(PAGE, tmp_path, event, 6, 12) == "separate.mp4"
    assert fetch.opened == [video_manifest, audio_manifest,
                            CDN + "v1.ts", CDN + "v2.ts", CDN + "a1.ts", CDN + "a2.ts"]


@pytest.mark.parametrize("start,end,full,success", [
    (None, None, True, True), (0, 20, False, True), (1, 15, False, False),
])
def test_short_muxed_mp4_requires_whole_video(extraction, monkeypatch, tmp_path, start, end, full, success):
    event, fetch = extraction
    url = CDN + "video.mp4"
    body = b"\x00\x00\x00\x18ftypisom"
    fetch.bodies[url] = body
    fetch.headers[(url, "Content-Type")] = "video/mp4"
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(formats=[{
        "url": url, "protocol": "https", "vcodec": "avc1", "acodec": "mp4a.40.2",
    }]))
    with patch.object(jobs, "trim_media", side_effect=lambda fd, *_args, **_kw: os.read(fd, 100)) as trim:
        if success:
            assert vod.import_youtube_vod(PAGE, tmp_path, event, start, end, full_video=full) == body
            assert trim.call_args.args[2:4] == (0, 20)
            assert fetch.opened == [url]
        else:
            with pytest.raises(vod.YouTubeVodUnavailable):
                vod.import_youtube_vod(PAGE, tmp_path, event, start, end, full_video=full)
            assert not fetch.opened
            trim.assert_not_called()


def test_full_video_rejects_long_source_without_fetching_media(extraction, monkeypatch, tmp_path):
    event, fetch = extraction
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(duration=301))
    with pytest.raises(ValueError, match="limited to 5 minutes"):
        vod.import_youtube_vod(PAGE, tmp_path, event, None, None, full_video=True)
    assert not fetch.opened


def test_full_video_prefers_hd_picture_with_separate_audio(extraction, monkeypatch, tmp_path):
    event, fetch = extraction
    picture, sound, low = (CDN + name for name in ("hd", "sound", "low"))
    body = b"\x00\x00\x00\x18ftypisom"
    fetch.bodies.update({picture: body + b"video", sound: body + b"audio", low: body})
    fetch.headers[(picture, "Content-Type")] = "video/mp4"
    fetch.headers[(sound, "Content-Type")] = "audio/mp4"
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(formats=[
        dict(url=low, protocol="https", height=360, vcodec="avc1", acodec="mp4a.40.2"),
        dict(url=picture, protocol="https", height=1080, vcodec="avc1", acodec="none"),
        dict(url=sound, protocol="https", vcodec="none", acodec="mp4a.40.2"),
    ]))

    def trim(fd, _directory, start, end, _event, *, audio_fd, deadline):
        assert os.read(fd, 100) == body + b"video"
        assert os.read(audio_fd, 100) == body + b"audio"
        assert (start, end) == (0, 20)
        return "output.mp4"

    with patch.object(jobs, "trim_media", side_effect=trim):
        assert vod.import_youtube_vod(PAGE, tmp_path, event, None, None, full_video=True) == "output.mp4"
    assert fetch.opened == [picture, sound]


def test_full_video_requires_a_youtube_video_link_and_no_range():
    source = {"type": "url", "url": PAGE, "full_video": True}
    assert jobs.validate_remote_source(source) == source
    with pytest.raises(ValueError, match="cannot be combined"):
        jobs.validate_remote_source({**source, "start": 0, "end": 20})
    with pytest.raises(ValueError, match="YouTube"):
        jobs.validate_remote_source({**source, "url": "https://www.twitch.tv/videos/1234"})


@pytest.mark.parametrize("required", [False, True])
def test_full_video_tries_other_hd_before_sd(extraction, monkeypatch, tmp_path, required):
    event, fetch = extraction
    hd, medium, sound, low = (CDN + name for name in ("1080", "720", "audio", "360"))
    body = b"\x00\x00\x00\x18ftypisom"
    formats = []
    for url, height, acodec in ((hd, 1080, "none"), (medium, 720, "none"),
                                (low, 360, "mp4a.40.2")):
        fetch.bodies[url] = body
        fetch.headers[(url, "Content-Type")] = "video/mp4"
        formats.append(dict(url=url, height=height, protocol="https", vcodec="avc1", acodec=acodec))
    fetch.headers[(hd, "Content-Type")] = "text/html"
    fetch.bodies[sound] = body
    fetch.headers[(sound, "Content-Type")] = "audio/mp4"
    formats.append(dict(url=sound, protocol="https", vcodec="none", acodec="mp4a.40.2"))
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(formats=formats))
    before = fetch.remaining
    with patch.object(jobs, "trim_media", return_value="hd.mp4") as trim:
        assert vod.import_youtube_vod(PAGE, tmp_path, event, None, None,
                                      full_video=True, require_hd=required) == "hd.mp4"
        assert "audio_fd" in trim.call_args.kwargs
        assert trim.call_args.kwargs["deadline"] == fetch.deadline
    assert fetch.opened == [hd, medium, sound]
    assert fetch.remaining < before


def test_full_video_tries_alternative_audio_without_redownloading_picture(extraction, monkeypatch, tmp_path):
    event, fetch = extraction
    picture, bad, good = (CDN + name for name in ("picture", "a-audio", "b-audio"))
    body = b"\x00\x00\x00\x18ftypisom"
    for url, mime in ((picture, "video/mp4"), (bad, "text/html"), (good, "audio/mp4")):
        fetch.bodies[url] = body
        fetch.headers[(url, "Content-Type")] = mime
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(formats=[
        dict(url=picture, height=1080, protocol="https", vcodec="avc1", acodec="none"),
        *(dict(url=url, protocol="https", vcodec="none", acodec="mp4a.40.2") for url in (bad, good)),
    ]))
    with patch.object(jobs, "trim_media", return_value="hd.mp4"):
        assert vod.import_youtube_vod(PAGE, tmp_path, event, None, None,
                                      full_video=True, require_hd=True) == "hd.mp4"
    assert fetch.opened == [picture, bad, good]


def test_original_english_beats_aac_dub_and_never_falls_back_to_it(extraction, monkeypatch, tmp_path):
    event, fetch = extraction
    picture, dub, original = [CDN + name for name in ("picture", "dub", "original")]
    fetch.bodies.update({picture: b"\x00\x00\x00\x20ftyp" + b"x" * 24,
                         dub: b"\x00\x00\x00\x20ftyp" + b"x" * 24,
                         original: b"\x1a\x45\xdf\xa3" + b"x" * 24})
    for url, mime in [(picture, "video/mp4"), (dub, "audio/mp4"), (original, "audio/webm")]:
        fetch.headers[url, "Content-Type"] = mime
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(formats=[
        dict(url=picture, height=1080, protocol="https", vcodec="avc1", acodec="none"),
        dict(url=dub, protocol="https", vcodec="none", acodec="mp4a.40.2",
             language="hi", language_preference=-1),
        dict(url=original, protocol="https", vcodec="none", acodec="opus",
             language="en-US", language_preference=10),
    ]))
    with patch.object(jobs, "trim_media", return_value="english.mp4") as trim:
        assert vod.import_youtube_vod(PAGE, tmp_path, event, None, None,
                                      full_video=True, require_hd=True) == "english.mp4"
        assert trim.call_args.kwargs["audio_webm"]
    assert fetch.opened == [picture, original]
    fetch.opened.clear()
    fetch.bodies[original] = b"invalid"
    with patch.object(jobs, "trim_media") as trim, pytest.raises(vod.YouTubeVodUnavailable):
        vod.import_youtube_vod(PAGE, tmp_path, event, None, None,
                               full_video=True, require_hd=True)
    trim.assert_not_called()
    assert fetch.opened == [picture, original]


def test_english_dub_does_not_replace_a_non_english_original(extraction, monkeypatch, tmp_path):
    event, fetch = extraction
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(formats=[
        dict(url=CDN + "english", height=720, protocol="https", vcodec="avc1",
             acodec="mp4a", language="en-US", language_preference=5),
        dict(url=CDN + "original", protocol="https", vcodec="none",
             acodec="opus", language="hi", language_preference=10),
    ]))
    with pytest.raises(vod.YouTubeVodUnavailable, match="original English audio"):
        vod.import_youtube_vod(PAGE, tmp_path, event, None, None, full_video=True, require_hd=True)
    assert not fetch.opened


def test_english_audio_marked_with_a_negative_preference_is_accepted(extraction, monkeypatch, tmp_path):
    event, fetch = extraction
    picture, sound = CDN + "picture", CDN + "sound"
    fetch.bodies.update({picture: b"\x00\x00\x00\x20ftyp" + b"x" * 24,
                         sound: b"\x00\x00\x00\x20ftyp" + b"x" * 24})
    for url, mime in [(picture, "video/mp4"), (sound, "audio/mp4")]:
        fetch.headers[url, "Content-Type"] = mime
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(formats=[
        dict(url=picture, height=1080, protocol="https", vcodec="avc1", acodec="none"),
        # What YouTube returns today: one English track, preference -1.
        dict(url=sound, protocol="https", vcodec="none", acodec="mp4a.40.2",
             language="en", language_preference=-1),
    ]))
    with patch.object(jobs, "trim_media", return_value="ok.mp4"):
        assert vod.import_youtube_vod(PAGE, tmp_path, event, None, None,
                                      full_video=True, require_hd=True) == "ok.mp4"
    assert fetch.opened == [picture, sound]


@pytest.mark.parametrize("failure", [None, "incorrect-offset", "inconsistent-total", "truncated", "request-cap"])
def test_full_track_is_reassembled_when_whole_get_is_denied(extraction, monkeypatch, tmp_path, failure):
    event, fetch = extraction
    track = CDN + "range-only"
    payload = b"\x00\x00\x00\x20ftyp" + b"x" * 22
    requested = []
    initial_budget = fetch.remaining
    monkeypatch.setattr(vod, "TRACK_RANGE_BYTES", 12)
    if failure == "request-cap":
        monkeypatch.setattr(vod, "MAX_TRACK_RANGE_REQUESTS", 2)

    @contextmanager
    def open_track(url, *, byte_range=None):
        assert url == track
        fetch.check()
        if byte_range is None:
            raise jobs.RemoteForbidden("whole GET unavailable")
        requested.append(byte_range)
        first, stop = byte_range
        last = min(stop, len(payload) - 1)
        total = len(payload)
        body = payload[first:last + 1]
        if first and failure == "incorrect-offset":
            first -= 1
        if first and failure == "inconsistent-total":
            total += 1
        length = len(body)
        if first and failure == "truncated":
            body = body[:-1]
        yield url, ({"Content-Type": "video/mp4", "Content-Length": str(length),
                     "Content-Range": f"bytes {first}-{last}/{total}"}, body)

    @contextmanager
    def open_response(*args, **kwargs):
        with open_track(*args, **kwargs) as (url, (headers, body)):
            yield url, type("Response", (), {"getheader": lambda _, key, default=None: headers.get(key, default),
                                            "body": body})()

    def chunks(response):
        fetch.remaining -= len(response.body)
        yield response.body

    monkeypatch.setattr(fetch, "open", open_response)
    monkeypatch.setattr(fetch, "chunks", chunks)
    metadata = info(formats=[dict(url=track, height=720, protocol="https", vcodec="avc1", acodec="mp4a")])

    def verify(fd, *_args, **kwargs):
        assert kwargs["require_hd"]
        assert os.read(fd, len(payload) + 1) == payload
        return "verified.mp4"

    with patch.object(jobs, "trim_media", side_effect=verify) as trim:
        result = vod._whole_mp4(metadata, 20, tmp_path, fetch, None, require_hd=True)
    if failure:
        assert result is None
        trim.assert_not_called()
        assert requested == [(0, 11), (12, 23)]
    else:
        assert result == "verified.mp4"
        assert requested == [(0, 11), (12, 23), (24, 29)]
        assert fetch.remaining == initial_budget - len(payload)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="FFmpeg tools unavailable")
def test_full_video_accepts_hd_vp9_with_opus_when_avc_is_unavailable(extraction, monkeypatch, tmp_path):
    event, fetch = extraction
    avc, picture, sound = (CDN + name for name in ("avc", "vp9", "opus"))
    picture_path, sound_path = tmp_path / "picture.webm", tmp_path / "sound.webm"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=1280x720:r=5:d=1",
        "-c:v", "libvpx-vp9", str(picture_path),
        "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=1",
        "-map", "1:a", "-c:a", "libopus", str(sound_path),
    ], check=True, capture_output=True)
    fetch.bodies.update({avc: b"unavailable!", picture: picture_path.read_bytes(),
                         sound: sound_path.read_bytes()})
    fetch.headers[(picture, "Content-Type")] = "video/webm"
    fetch.headers[(sound, "Content-Type")] = "audio/webm"
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(duration=1, formats=[
        dict(url=avc, height=720, protocol="https", vcodec="avc1", acodec="none"),
        dict(url=picture, height=720, protocol="https", vcodec="vp9", acodec="none"),
        dict(url=sound, protocol="https", vcodec="none", acodec="opus"),
    ]))
    result = vod.import_youtube_vod(PAGE, tmp_path, event, None, None, full_video=True, require_hd=True)
    streams = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "json", str(result),
    ]))["streams"]
    assert streams == [{"codec_name": "h264", "width": 1280, "height": 720}, {"codec_name": "aac"}]
    assert fetch.opened == [avc, picture, sound]


@pytest.mark.parametrize("required", [False, True])
def test_sd_fallback_is_available_only_when_hd_not_required(extraction, monkeypatch, tmp_path, required):
    event, fetch = extraction
    url = CDN + "sd"
    body = b"\x00\x00\x00\x18ftypisom"
    fetch.bodies[url] = body
    fetch.headers[(url, "Content-Type")] = "video/mp4"
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(formats=[
        dict(url=url, height=360, protocol="https", vcodec="avc1", acodec="mp4a.40.2"),
        *info()["formats"],
    ]))
    with patch.object(jobs, "trim_media", return_value="sd.mp4") as trim:
        if required:
            with pytest.raises(vod.YouTubeVodUnavailable, match="no SD fallback"):
                vod.import_youtube_vod(PAGE, tmp_path, event, None, None,
                                       full_video=True, require_hd=True)
            assert not fetch.opened
            trim.assert_not_called()
        else:
            assert vod.import_youtube_vod(PAGE, tmp_path, event, None, None,
                                          full_video=True) == "sd.mp4"
            assert fetch.opened == [url]


def test_required_hd_can_use_verified_hd_hls(extraction, monkeypatch, tmp_path):
    event, fetch = extraction
    metadata = info()
    metadata["formats"][0]["height"] = 720
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: metadata)
    with patch.object(jobs, "trim_media", return_value="hd.mp4"):
        assert vod.import_youtube_vod(PAGE, tmp_path, event, None, None,
                                      full_video=True, require_hd=True) == "hd.mp4"
    assert fetch.opened == [MANIFEST, *(CDN + c + ".ts" for c in "abcd")]


@pytest.mark.parametrize("require_hd", [True, False, "true", 1, None])
def test_require_hd_is_explicit_full_video_only(require_hd):
    source = dict(type="url", url=PAGE, require_hd=require_hd)
    with pytest.raises(ValueError, match="require_hd"):
        jobs.validate_remote_source(source)
    source["full_video"] = True
    if type(require_hd) is bool:
        assert jobs.validate_remote_source(source) == source
    else:
        with pytest.raises(ValueError, match="require_hd"):
            jobs.validate_remote_source(source)


@pytest.mark.parametrize("options", [{"full_video": True}, {"full_video": True, "require_hd": False},
                                     {"start": 2, "end": 5}])
def test_import_remote_enforces_hd_contract(tmp_path, options):
    source = dict(type="url", url=PAGE, **options)
    with patch.object(vod, "import_youtube_vod", return_value="hd.mp4") as importer:
        assert jobs.import_remote(source, tmp_path, threading.Event()) == "hd.mp4"
    assert importer.call_args.kwargs["require_hd"] is True
    assert importer.call_args.kwargs["full_video"] is options.get("full_video", False)


def test_hd_alternative_requests_are_capped(extraction, monkeypatch, tmp_path):
    event, fetch = extraction
    formats = []
    for index in range(vod.MAX_MP4_TRACK_REQUESTS + 2):
        url = CDN + f"bad-{index:02}"
        fetch.bodies[url] = b"not accessible"
        formats.append(dict(url=url, height=1080, protocol="https", vcodec="avc1", acodec="mp4a.40.2"))
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(formats=formats))
    with pytest.raises(vod.YouTubeVodUnavailable, match="no SD fallback"):
        vod.import_youtube_vod(PAGE, tmp_path, event, None, None, full_video=True, require_hd=True)
    assert len(fetch.opened) == vod.MAX_MP4_TRACK_REQUESTS


def test_hd_alternatives_do_not_retry_after_cancellation(extraction, monkeypatch, tmp_path):
    event, fetch = extraction
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: info(formats=[
        dict(url=CDN + name, height=height, protocol="https", vcodec="avc1", acodec="mp4a.40.2")
        for name, height in (("1080", 1080), ("720", 720), ("360", 360))
    ]))
    with patch.object(vod, "_read_mp4", side_effect=jobs.Cancelled()) as read:
        with pytest.raises(jobs.Cancelled):
            vod.import_youtube_vod(PAGE, tmp_path, event, None, None, full_video=True)
    assert read.call_count == 1


@pytest.mark.parametrize("height", [720, 718])
@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="FFmpeg tools unavailable")
def test_job_verifies_youtube_hls_resolution_with_real_media(tmp_path, monkeypatch, height):
    manifest = tmp_path / "index.m3u8"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=s=1280x{height}:r=10:d=8",
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
    metadata = info(duration=8)
    metadata["formats"][0]["height"] = 720
    monkeypatch.setattr(sources, "youtube_vod_metadata", lambda *_: metadata)
    store = jobs.JobStore(tmp_path / "data", [tmp_path], lambda *_: {"candidates": []}, lambda *_: None)
    try:
        job = store.create({"type": "url", "url": PAGE, "start": 2.5, "end": 5.5})
        limit = time.monotonic() + 30
        while time.monotonic() < limit:
            job = store.get(job["id"])
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert fetches[0].opened == [MANIFEST, CDN + "index1.ts", CDN + "index2.ts"]
        if height < 720:
            assert job["status"] == "failed"
            assert "did not verify at 720p" in job["error"]
            assert not (store.root / job["id"] / "source.mp4").exists()
            return
        assert job["status"] == "succeeded", job
        assert job["resolved_source"]["platform"] == "youtube"
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
    (info(formats=[{"protocol": "http_dash_segments", "url": CDN + "archive.mpd"}]), "no supported muxed or separate"),
    (info(formats=[{"protocol": "https", "url": CDN + "direct.mp4"}]), "no supported muxed or separate"),
    (info(formats=[{**info()["formats"][0], "acodec": "none"}]), "no supported muxed or separate"),
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
