"""Bounded acquisition of clear YouTube videos.

Use import_youtube_vod for one range, or prepare_youtube_vod once and call
acquire sequentially in distinct window directories for a selective scan.
Selective reads use HLS only. A single whole short video may use a muxed MP4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import logging
import math
import re
import tempfile
import time
from urllib.parse import urljoin, urlsplit

from . import sources

logger = logging.getLogger(__name__)
MAX_SEGMENT_BYTES = 16 * 1024**2
MAX_INDEX_SEGMENTS = 50_000
MAX_FULL_VIDEO_SECONDS = 300
MAX_MP4_TRACK_REQUESTS = 12
TRACK_RANGE_BYTES = 4 * 1024**2
MAX_TRACK_RANGE_REQUESTS = 160
HLS_TYPES = {"application/vnd.apple.mpegurl", "application/x-mpegurl",
             "application/mpegurl", "audio/mpegurl", "audio/x-mpegurl"}
SEGMENT_TYPES = {"video/mp2t", "application/octet-stream"}


class YouTubeVodUnavailable(ValueError):
    """A complete, bounded YouTube acquisition could not be established."""


def _finished_duration(info):
    if info.get("live_status") == "post_live":
        raise YouTubeVodUnavailable(
            "YouTube reports this broadcast is still processing its archive; the full video timeline is not yet verified"
        )
    duration = info.get("duration")
    if (info.get("_type") not in {None, "video"} or info.get("is_live")
            or info.get("availability") not in {"public", "unlisted"}
            or info.get("is_private") or info.get("needs_auth") or info.get("has_drm")
            or info.get("live_status") not in {"was_live", "not_live"}
            or not _number(duration) or duration <= 0):
        raise YouTubeVodUnavailable("YouTube VOD requires a finished, public, unencrypted video")
    return duration


def _number(value):
    return type(value) in (float, int) and abs(value) <= 10**8 and math.isfinite(value)


def _cdn_url(url, base=None):
    try:
        if (not isinstance(url, str) or not url or len(url) > 4096
                or "\\" in url or any(not 33 <= ord(c) <= 126 for c in url)):
            raise ValueError()
        url = urljoin(base, url) if base is not None else url
        parsed = urlsplit(url)
        if (len(url) > 4096 or parsed.scheme != "https" or not parsed.hostname
                or not re.fullmatch(r"(?:[a-z0-9-]+\.)+googlevideo\.com", parsed.hostname)
                or parsed.port not in (None, 443) or parsed.fragment
                or parsed.username is not None or parsed.password is not None):
            raise ValueError()
    except ValueError:
        raise YouTubeVodUnavailable("YouTube media contains an invalid CDN URL") from None
    return url


@dataclass(frozen=True)
class _Segment:
    start: float
    end: float
    url: str = field(repr=False)


def _index(body, base, expected_duration):
    try:
        lines = body.decode("utf-8-sig").splitlines()
    except UnicodeError:
        raise YouTubeVodUnavailable("YouTube HLS playlist is not valid UTF-8") from None
    if not lines or lines[0] != "#EXTM3U" or lines[-1] != "#EXT-X-ENDLIST":
        raise YouTubeVodUnavailable("YouTube HLS does not expose an ended media playlist")
    segments, position, pending = [], 0.0, None
    headers = set()
    for line in lines[1:-1]:
        if not line:
            continue
        name, _, value = line.partition(":")
        if name == "#EXTINF":
            duration = value.partition(",")[0]
            if pending is not None or not re.fullmatch(r"\d{1,2}(?:\.\d{1,9})?", duration, re.ASCII):
                raise YouTubeVodUnavailable("YouTube HLS contains invalid segment timing")
            pending = float(duration)
            if not 0 < pending <= 60:
                raise YouTubeVodUnavailable("YouTube HLS contains an unsupported segment duration")
        elif line.startswith("#"):
            if not line.startswith("#EXT"):
                continue  # Non-tag HLS comments have no playback semantics.
            if line == "#EXT-X-KEY:METHOD=NONE":
                continue
            valid = (
                line in {"#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-MEDIA-SEQUENCE:0",
                         "#EXT-X-DISCONTINUITY-SEQUENCE:0", "#EXT-X-INDEPENDENT-SEGMENTS"}
                or name == "#EXT-X-VERSION" and bool(re.fullmatch(r"[1-9]", value))
                or name == "#EXT-X-TARGETDURATION" and bool(re.fullmatch(r"[1-9][0-9]?", value))
            )
            if not valid or name in headers or segments or pending is not None:
                raise YouTubeVodUnavailable(
                    "YouTube HLS is not a continuous, clear MPEG-TS VOD playlist"
                )
            headers.add(name)
        else:
            if pending is None or len(segments) >= MAX_INDEX_SEGMENTS:
                raise YouTubeVodUnavailable("YouTube HLS segment index is invalid or too large")
            url = _cdn_url(line, base)
            segments.append(_Segment(position, position + pending, url))
            position, pending = position + pending, None
    if (not {"#EXT-X-PLAYLIST-TYPE", "#EXT-X-INDEPENDENT-SEGMENTS"} <= headers
            or not segments or pending is not None
            or abs(position - expected_duration) > 1):
        raise YouTubeVodUnavailable("YouTube HLS does not verify the full archived timeline")
    # A playlist with reused URIs cannot independently address its declared windows.
    if len({s.url for s in segments}) != len(segments):
        raise YouTubeVodUnavailable("YouTube HLS reuses media URLs across segment times")
    return tuple(segments)


def _read(fetch, url, limit, *, playlist=False):
    from .jobs import Cancelled

    try:
        with fetch.open(url) as (final, response):
            _cdn_url(final)
            content_type = response.getheader("Content-Type", "").partition(";")[0].strip().lower()
            if content_type not in (HLS_TYPES if playlist else SEGMENT_TYPES):
                raise YouTubeVodUnavailable(
                    "YouTube did not return an HLS playlist" if playlist
                    else "YouTube HLS did not return an MPEG-TS segment"
                )
            declared = response.getheader("Content-Length")
            if declared is not None:
                if not re.fullmatch(r"[0-9]{1,10}", declared) or not 0 < int(declared) <= limit:
                    raise YouTubeVodUnavailable("YouTube HLS response exceeds its byte limit")
                declared = int(declared)
            data = bytearray()
            for block in fetch.chunks(response):
                if len(data) + len(block) > limit:
                    raise YouTubeVodUnavailable("YouTube HLS response exceeds its byte limit")
                data.extend(block)
            if not data or declared is not None and declared != len(data):
                raise YouTubeVodUnavailable("YouTube HLS response is empty or truncated")
            return final, data
    except (YouTubeVodUnavailable, Cancelled):
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise YouTubeVodUnavailable("YouTube HLS request failed or exceeded its transfer limits") from None


@dataclass
class YouTubeVod:
    title: str
    duration: float
    _segments: tuple[_Segment, ...] = field(repr=False)
    _fetch: object = field(repr=False)
    _attempts: int = field(default=0, init=False)
    _attempted_urls: set[str] = field(default_factory=set, init=False, repr=False)

    def acquire(self, directory, start, end, *, allow_shorter=False, on_resolved=None,
                whole_video=False, require_hd=False):
        """Return one trimmed local path; all windows share deadline, bytes and request cap."""
        from . import jobs

        self._fetch.check()
        if not (_number(start) and _number(end) and 0 <= start < end <= start + jobs.MAX_RANGE_SECONDS):
            raise ValueError("Invalid bounded YouTube VOD range")
        if allow_shorter:
            end = min(end, self.duration)
        if not start < end <= self.duration:
            raise ValueError("Requested range exceeds the completed YouTube VOD")
        selected = tuple(s for s in self._segments if s.start < end and s.end > start)
        if (not selected or selected[-1].end < end - 1e-6
                or (not whole_video
                    and len(self._attempted_urls.union(s.url for s in selected)) == len(self._segments))):
            raise YouTubeVodUnavailable(
                "YouTube HLS cannot retrieve this range without fetching the entire archive "
                "or leaving part of the range uncovered"
            )
        if self._attempts + len(selected) > jobs.MAX_REMOTE_SEGMENTS:
            raise YouTubeVodUnavailable("YouTube VOD exceeds the cumulative segment-request limit")
        if on_resolved:
            on_resolved(self.title, end)
        with tempfile.TemporaryFile(dir=directory) as output:
            for segment in selected:
                self._fetch.check()
                self._attempts += 1
                self._attempted_urls.add(segment.url)
                _, data = _read(self._fetch, segment.url,
                                min(MAX_SEGMENT_BYTES, self._fetch.remaining))
                if len(data) < 188 or len(data) % 188 or data[::188] != b"\x47" * (len(data) // 188):
                    raise YouTubeVodUnavailable("YouTube HLS segment is not a complete MPEG-TS response")
                output.write(data)
            output.flush()
            output.seek(0)
            origin = selected[0].start
            return jobs.trim_media(output.fileno(), directory, start - origin, end - origin,
                                   self._fetch.event, deadline=self._fetch.deadline, staged=True,
                                   **({"require_hd": True} if require_hd else {}))


def _metadata(url, event, deadline):
    from . import jobs

    try:
        return sources.youtube_vod_metadata(url, event, deadline)
    except sources.SourceResolutionCancelled:
        raise jobs.Cancelled() from None


def _fetch(event, deadline):
    from . import jobs

    fetch = jobs.RemoteFetch(event, deadline, validate_url=_cdn_url)
    fetch.remaining -= sources.MAX_METADATA_BYTES + 65536
    return fetch


def _muxed(item):
    return (isinstance(item, dict) and not item.get("has_drm")
            and isinstance(item.get("vcodec"), str) and item["vcodec"].startswith(("avc1", "h264"))
            and isinstance(item.get("acodec"), str) and item["acodec"].startswith(("mp4a", "aac")))


def _original_english(info):
    def english(language):
        return isinstance(language, str) and bool(re.fullmatch(r"en(?:-[a-zA-Z0-9]{2,8})*", language, re.I))

    formats = [f for f in info["formats"] if isinstance(f, dict) and not f.get("has_drm")]
    # yt-dlp's YouTube extractor uses preference 10 for the original audio.
    originals = [f.get("language") for f in formats if f.get("language_preference") == 10]
    languages = {f["language"] for f in formats if isinstance(f.get("language"), str)
                 and f.get("acodec") != "none"}
    if originals:
        return lambda f: english(f.get("language")) and f.get("language_preference") == 10
    if not english(info.get("language")) or any(not english(language) for language in languages):
        return lambda _: False
    # Every audio track here is English (or unlabelled), so a negative
    # preference is yt-dlp's default marker rather than a dubbed track.
    # YouTube now reports -1 for the only audio track on ordinary videos.
    return lambda f: english(f.get("language")) or f.get("language") is None


def _hls_archive(info, duration, fetch, *, require_hd=False):
    from . import jobs

    variants = []
    original_audio = _original_english(info)
    for item in info["formats"]:
        if (not _muxed(item) or not original_audio(item)
                or item.get("protocol") not in {"m3u8", "m3u8_native"}):
            continue
        try:
            candidate = _cdn_url(item.get("url"))
        except YouTubeVodUnavailable:
            continue
        height = item.get("height")
        if require_hd and not (_number(height) and height >= 720):
            continue
        variants.append((abs(height - (1080 if require_hd else 360))
                         if _number(height) and height > 0 else 10**8, candidate))
    if not variants:
        raise YouTubeVodUnavailable(
            "YouTube exposes no supported muxed HLS archive; no verified selective index for its other formats"
        )
    for _, url in sorted(set(variants))[:MAX_MP4_TRACK_REQUESTS if require_hd else 1]:
        fetch.check()
        try:
            base, body = _read(fetch, url, jobs.MAX_MANIFEST_BYTES, playlist=True)
            segments = _index(body, base, duration)
        except YouTubeVodUnavailable:
            if not require_hd:
                raise
            continue
        # Integer player duration and decimal EXTINF times may round differently at the tail.
        return YouTubeVod(str(info.get("title") or ""), min(duration, segments[-1].end), segments, fetch)
    raise YouTubeVodUnavailable("No supported YouTube HD HLS archive is available within the import limits")


def prepare_youtube_vod(url, event, *, deadline=None):
    """Verify one direct video and its manifest, without fetching media segments."""
    from . import jobs

    ceiling = time.monotonic() + jobs.MAX_DOWNLOAD_SECONDS
    deadline = min(deadline, ceiling) if deadline is not None else ceiling
    info = _metadata(url, event, deadline)
    return _hls_archive(info, _finished_duration(info), _fetch(event, deadline))


def _copy_track_part(fetch, response, media, length):
    expected_end = media.tell() + length
    for block in fetch.chunks(response):
        if media.tell() + len(block) > expected_end:
            raise YouTubeVodUnavailable("YouTube track exceeds its declared size")
        media.write(block)
    if media.tell() != expected_end:
        raise YouTubeVodUnavailable("YouTube track is truncated")


def _track_type(response):
    return response.getheader("Content-Type", "").partition(";")[0].strip().lower()


def _read_track_ranges(fetch, url, media, mime, open_range):
    total = None
    while total is None or media.tell() < total:
        start = media.tell()
        stop = start + TRACK_RANGE_BYTES - 1
        if total is not None:
            stop = min(stop, total - 1)
        with open_range(url, (start, stop)) as (final, response):
            _cdn_url(final)
            content_range = response.getheader("Content-Range", "")
            match = re.fullmatch(r"bytes ([0-9]{1,10})-([0-9]{1,10})/([0-9]{1,10})", content_range)
            if not match or _track_type(response) != mime:
                raise YouTubeVodUnavailable("YouTube returned an invalid track byte range")
            first, last, size = map(int, match.groups())
            if (first != start or last != min(stop, size - 1)
                    or not 12 <= size <= start + fetch.remaining
                    or (total is not None and size != total)
                    or response.getheader("Content-Length") != str(last - first + 1)):
                raise YouTubeVodUnavailable("YouTube track byte range is incomplete or inconsistent")
            total = size
            _copy_track_part(fetch, response, media, last - first + 1)


def _read_mp4(fetch, url, media, mime, open_range):
    from . import jobs

    try:
        try:
            with fetch.open(url) as (final, response):
                _cdn_url(final)
                length = response.getheader("Content-Length", "")
                if (_track_type(response) != mime or response.getheader("Content-Range")
                        or not re.fullmatch(r"[0-9]{1,10}", length)
                        or not 12 <= int(length) <= fetch.remaining):
                    raise YouTubeVodUnavailable("YouTube did not return a complete media track within the transfer limit")
                _copy_track_part(fetch, response, media, int(length))
        except jobs.RemoteForbidden:
            if media.tell():
                raise
            _read_track_ranges(fetch, url, media, mime, open_range)
        media.seek(0)
        header = media.read(12)
        if not (header.startswith(b"\x1a\x45\xdf\xa3") if mime.endswith("/webm") else header[4:8] == b"ftyp"):
            raise YouTubeVodUnavailable("YouTube response is not a standalone media track")
        media.seek(0)
    except (YouTubeVodUnavailable, jobs.Cancelled):
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise YouTubeVodUnavailable("YouTube media track could not be retrieved within the import limits") from None


def _whole_mp4(info, duration, directory, fetch, on_resolved, *, require_hd=False):
    from . import jobs

    muxed, video, audio = [], [], []
    original_audio = _original_english(info)
    for item in info["formats"]:
        if not isinstance(item, dict) or item.get("protocol") != "https" or item.get("has_drm"):
            continue
        try:
            url = _cdn_url(item.get("url"))
        except YouTubeVodUnavailable:
            continue
        height = item.get("height")
        rank = abs(height - 1080) if _number(height) else 10**8
        is_hd = _number(height) and height >= 720
        vcodec, acodec = item.get("vcodec"), item.get("acodec")
        if _muxed(item) and original_audio(item) and (is_hd or not require_hd):
            muxed.append((not is_hd, rank, 0, url, "video/mp4", False))
        elif acodec == "none" and is_hd and isinstance(vcodec, str):
            if vcodec.startswith(("avc1", "h264", "av01", "vp9", "vp09")):
                webm = vcodec.startswith(("vp9", "vp09")) or item.get("ext") == "webm"
                video.append((False, rank, 2 if webm else 1, url,
                              "video/webm" if webm else "video/mp4", True))
        elif vcodec == "none" and isinstance(acodec, str) and original_audio(item):
            if acodec.startswith(("mp4a", "aac", "opus")):
                pref = 0 if acodec == "mp4a.40.2" else 1 if acodec.startswith("opus") else 2
                audio.append((pref, url, "audio/webm" if pref == 1 else "audio/mp4"))
    candidates = set(muxed)
    if audio:
        candidates.update(video)
    preferred_audio = [sorted(set(track for track in audio if track[0] == pref)) for pref in range(3)]
    audio_tracks = [group[i] for i in range(3) for group in preferred_audio if i < len(group)]
    attempts = range_requests = 0

    def open_range(url, byte_range):
        nonlocal range_requests
        fetch.check()
        if range_requests >= MAX_TRACK_RANGE_REQUESTS:
            raise YouTubeVodUnavailable("YouTube track exceeded the cumulative byte-range request limit")
        range_requests += 1
        return fetch.open(url, byte_range=byte_range)

    def read_track(url, media, mime):
        nonlocal attempts
        fetch.check()
        if attempts >= MAX_MP4_TRACK_REQUESTS:
            raise YouTubeVodUnavailable("YouTube MP4 rendition attempts exceed the import limit")
        attempts += 1
        _read_mp4(fetch, url, media, mime, open_range)

    def finish(picture, mime, sound=None, audio_mime=None):
        options = {"require_hd": True} if require_hd else {}
        if mime == "video/webm":
            options["video_webm"] = True
        if sound is not None:
            options["audio_fd"] = sound.fileno()
            if audio_mime == "audio/webm":
                options["audio_webm"] = True
        return jobs.trim_media(picture.fileno(), directory, 0, duration, fetch.event,
                               deadline=fetch.deadline, **options)

    for _, _, _, url, mime, separate in sorted(candidates):
        if attempts >= MAX_MP4_TRACK_REQUESTS:
            break
        with tempfile.TemporaryFile(dir=directory, buffering=0) as picture:
            try:
                read_track(url, picture, mime)
            except YouTubeVodUnavailable:
                logger.info("YouTube MP4 rendition unavailable; trying another within the same budget")
                continue
            if not separate:
                if on_resolved:
                    on_resolved(str(info.get("title") or ""), duration)
                try:
                    return finish(picture, mime)
                except YouTubeVodUnavailable:
                    continue
            for _, audio_url, audio_mime in audio_tracks:
                if attempts >= MAX_MP4_TRACK_REQUESTS:
                    break
                with tempfile.TemporaryFile(dir=directory, buffering=0) as sound:
                    try:
                        read_track(audio_url, sound, audio_mime)
                    except YouTubeVodUnavailable:
                        continue
                    if on_resolved:
                        on_resolved(str(info.get("title") or ""), duration)
                    try:
                        return finish(picture, mime, sound, audio_mime)
                    except YouTubeVodUnavailable:
                        break
    return None


def import_youtube_vod(url, directory, event, start, end, *, allow_shorter=False,
                       full_video=False, require_hd=False, on_resolved=None):
    from . import jobs

    if type(require_hd) is not bool:
        raise ValueError("require_hd must be boolean")
    deadline = time.monotonic() + jobs.MAX_DOWNLOAD_SECONDS
    info = _metadata(url, event, deadline)
    duration = _finished_duration(info)
    original_audio = _original_english(info)
    if not any(isinstance(item, dict) and not item.get("has_drm")
               and isinstance(item.get("acodec"), str) and item["acodec"] != "none"
               and original_audio(item) for item in info["formats"]):
        raise YouTubeVodUnavailable("YouTube did not identify an original English audio track; no dubbed audio was imported")
    if full_video and duration > MAX_FULL_VIDEO_SECONDS:
        raise ValueError("Full video is limited to 5 minutes. Choose a custom range.")
    whole = full_video or (not allow_shorter and start == 0 and end == duration
                          and duration <= MAX_FULL_VIDEO_SECONDS)
    fetch = _fetch(event, deadline)
    if whole:
        mp4 = _whole_mp4(info, duration, directory, fetch, on_resolved, require_hd=require_hd)
        if mp4 is not None:
            return mp4
    if not whole and not allow_shorter and (_number(end) and end > duration):
        raise ValueError("Requested range exceeds the completed YouTube VOD")
    try:
        archive = _hls_archive(info, duration, fetch, require_hd=require_hd)
    except YouTubeVodUnavailable:
        if require_hd:
            raise YouTubeVodUnavailable(
                "YouTube HD unavailable: no compatible 720p or better rendition within the import limits; "
                "no SD fallback was used"
            ) from None
        raise
    return archive.acquire(
        directory, 0 if whole else start, archive.duration if whole else end,
        allow_shorter=allow_shorter, on_resolved=on_resolved, whole_video=whole, require_hd=require_hd,
    )
