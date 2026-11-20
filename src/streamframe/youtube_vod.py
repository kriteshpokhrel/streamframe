"""Bounded acquisition of complete, clear, muxed YouTube HLS archives only.

Use import_youtube_vod for one range, or prepare_youtube_vod once and call
acquire sequentially in distinct window directories for a selective scan.
Neither API falls back to direct files, DASH, or FFmpeg network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import math
import re
import tempfile
import time
from urllib.parse import urljoin, urlsplit

from . import sources

MAX_SEGMENT_BYTES = 16 * 1024**2
MAX_INDEX_SEGMENTS = 50_000
HLS_TYPES = {"application/vnd.apple.mpegurl", "application/x-mpegurl",
             "application/mpegurl", "audio/mpegurl", "audio/x-mpegurl"}
SEGMENT_TYPES = {"video/mp2t", "application/octet-stream"}


class YouTubeVodUnavailable(ValueError):
    """A complete, bounded YouTube acquisition could not be established."""


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
        raise YouTubeVodUnavailable("YouTube HLS contains an invalid CDN URL") from None
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

    def acquire(self, directory, start, end, *, allow_shorter=False, on_resolved=None):
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
                or len(self._attempted_urls.union(s.url for s in selected)) == len(self._segments)):
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
                                   self._fetch.event, deadline=self._fetch.deadline, staged=True)


def prepare_youtube_vod(url, event, *, deadline=None):
    """Verify one direct video and its manifest, without fetching media segments."""
    from . import jobs

    ceiling = time.monotonic() + jobs.MAX_DOWNLOAD_SECONDS
    deadline = min(deadline, ceiling) if deadline is not None else ceiling
    try:
        info = sources.youtube_vod_metadata(url, event, deadline)
    except sources.SourceResolutionCancelled:
        raise jobs.Cancelled() from None
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
    variants = []
    for item in info["formats"]:
        if (not isinstance(item, dict) or item.get("protocol") not in {"m3u8", "m3u8_native"}
                or item.get("has_drm") or not isinstance(item.get("vcodec"), str)
                or not item["vcodec"].startswith(("avc1", "h264"))
                or not isinstance(item.get("acodec"), str)
                or not item["acodec"].startswith(("mp4a", "aac"))):
            continue
        try:
            candidate = _cdn_url(item.get("url"))
        except YouTubeVodUnavailable:
            continue
        height = item.get("height")
        variants.append((abs(height - 360) if _number(height) and height > 0 else 10**8, candidate))
    if not variants:
        raise YouTubeVodUnavailable(
            "YouTube exposes no supported muxed HLS archive; no verified selective index for its other formats"
        )
    fetch = jobs.RemoteFetch(event, deadline, validate_url=_cdn_url)
    # Reserve metadata traffic and one possible final over-limit read by RemoteFetch.
    fetch.remaining -= sources.MAX_METADATA_BYTES + 65536
    base, body = _read(fetch, min(variants)[1], jobs.MAX_MANIFEST_BYTES, playlist=True)
    segments = _index(body, base, duration)
    # Integer player duration and decimal EXTINF times may round differently at the tail.
    return YouTubeVod(str(info.get("title") or ""), min(duration, segments[-1].end), segments, fetch)


def import_youtube_vod(url, directory, event, start, end, *, allow_shorter=False, on_resolved=None):
    """Worker adapter for a single explicit source range."""
    return prepare_youtube_vod(url, event).acquire(
        directory, start, end, allow_shorter=allow_shorter, on_resolved=on_resolved
    )
