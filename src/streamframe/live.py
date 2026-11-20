"""Finite public live capture from a single clear, multiplexed MPEG-TS HLS rendition."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import uuid4

from . import jobs


MAX_LIVE_SECONDS = 300
MAX_SESSION_SECONDS = 1800
MAX_WINDOW_SECONDS = 60
MAX_DISK_BYTES = 512 * 1024**2
MAX_TRANSFER_BYTES = 512 * 1024**2
MAX_SEGMENT_BYTES = 24 * 1024**2
MAX_MANIFEST_BYTES = 1024**2
MAX_METADATA_BYTES = 8 * 1024**2
MAX_SEGMENTS = 900
EXTRACT_SECONDS = 30
CAPTURE_GRACE_SECONDS = 30
ENCODE_SECONDS = 90
_CUT_MARGIN = 0.1  # Room for a video frame and audio encoder/muxer padding.
_TRANSIENT = (OSError, http.client.HTTPException)


class LiveCaptureError(ValueError):
    """A public live source could not be captured within the supported bounds."""


class LiveBackpressureError(LiveCaptureError):
    """A rolling window could not be handed to the consumer promptly."""


class _LiveDeadline(LiveCaptureError):
    pass


@dataclass(frozen=True)
class _Segment:
    sequence: int
    duration: float
    url: str
    timestamp: float | None
    gap: bool
    discontinuity_sequence: int


def _page(resolved):
    if not resolved.is_live:
        raise LiveCaptureError("Live capture requires an active public broadcast")
    parsed = urlsplit(resolved.url)
    if resolved.platform == "youtube":
        ids = parse_qs(parsed.query).get("v", [])
        if (parsed.scheme == "https" and parsed.netloc == "www.youtube.com" and parsed.path == "/watch"
                and len(ids) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{11}", ids[0])):
            return "youtube", f"https://www.youtube.com/watch?v={ids[0]}"
    if resolved.platform == "twitch":
        if (parsed.scheme == "https" and parsed.netloc == "www.twitch.tv"
                and re.fullmatch(r"/[a-zA-Z0-9_]{1,25}", parsed.path)):
            return "twitch:stream", f"https://www.twitch.tv{parsed.path}"
    raise LiveCaptureError("Live capture requires a canonical YouTube watch or Twitch channel URL")


def _check(event, deadline, message):
    if event.is_set():
        raise jobs.Cancelled()
    if time.monotonic() >= deadline:
        raise _LiveDeadline(message)


def _run(command, event, deadline, write, *, stdin=None, env=None):
    """Drain bounded output without blocking on the child or retaining stderr."""
    _check(event, deadline, "Live capture subprocess timed out")
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL if stdin is None else stdin,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise LiveCaptureError("Live capture requires yt-dlp, FFmpeg and ffprobe") from exc
    try:
        with process.stdout:
            while True:
                _check(event, deadline, "Live capture subprocess timed out")
                if select.select([process.stdout], [], [], 0.1)[0]:
                    block = os.read(process.stdout.fileno(), 65536)
                    if not block:
                        break
                    write(block)
        while process.poll() is None:
            _check(event, deadline, "Live capture subprocess timed out")
            event.wait(0.05)
        _check(event, deadline, "Live capture subprocess timed out")
        return process.returncode
    finally:
        # A wrapper may have exited while a descendant still holds its pipes.
        process.poll()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                process.kill()
        process.wait()


def _memory_writer(limit, message):
    buffer = bytearray()

    def write(block):
        if len(buffer) + len(block) > limit:
            raise LiveCaptureError(message)
        buffer.extend(block)

    return buffer, write


def _resolve_hls(extractor, url, directory, event, deadline):
    metadata, write = _memory_writer(MAX_METADATA_BYTES, "Live extraction metadata exceeds its byte limit")
    guard = jobs.DownloadProxy(event, limit=MAX_METADATA_BYTES)
    guard.deadline = deadline
    with guard as proxy:
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        env.update(HOME=str(directory), XDG_CONFIG_HOME=str(directory),
                   HTTP_PROXY=proxy, HTTPS_PROXY=proxy, ALL_PROXY=proxy, NO_PROXY="")
        command = [
            sys.executable, "-I", "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs",
            "--no-cache-dir", "--no-playlist", "--skip-download", "--dump-single-json",
            "--no-warnings", "--socket-timeout", "6", "--retries", "0", "--extractor-retries", "0",
            "--no-allow-dynamic-mpd", "--proxy", proxy, "--use-extractors", extractor, "--", url,
        ]
        if _run(command, event, deadline, write, env=env):
            raise LiveCaptureError("yt-dlp could not resolve the public broadcast without credentials")
    try:
        info = json.loads(metadata)
    except (ValueError, UnicodeError) as exc:
        raise LiveCaptureError("yt-dlp did not return valid live metadata") from exc
    if (not isinstance(info, dict) or info.get("_type") not in {None, "video"}
            or info.get("is_live") is not True
            or info.get("availability") not in {None, "public", "unlisted"}
            or info.get("has_drm")):
        raise LiveCaptureError("URL is not a public, active, unencrypted broadcast")
    variants = [
        item for item in info.get("formats") or []
        if isinstance(item, dict) and item.get("protocol") in {"m3u8", "m3u8_native"}
        and isinstance(item.get("url"), str) and item["url"].startswith("https://")
        and not item.get("has_drm") and item.get("vcodec") != "none" and item.get("acodec") != "none"
    ]
    if not variants:
        raise LiveCaptureError("No supported single-rendition public audio/video HLS format is available")
    def distance(item):
        height = item.get("height")
        return abs(height - 720) if isinstance(height, (int, float)) else 0

    return min(variants, key=distance)["url"]


def _segment_url(base_url, value):
    if any(ord(char) <= 32 for char in value) or "\\" in value:
        raise LiveCaptureError("Invalid live HLS URL")
    try:
        return urljoin(base_url, value)
    except ValueError as exc:
        raise LiveCaptureError("Invalid live HLS URL") from exc


def _master_variant(data, base_url):
    """Choose one rendition; never fetch external audio groups or encryption keys."""
    try:
        lines = [line.strip() for line in data.decode("utf-8-sig").splitlines() if line.strip()]
    except UnicodeError as exc:
        raise LiveCaptureError("Live HLS playlist is not valid UTF-8") from exc
    if not any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        return None
    if lines[0] != "#EXTM3U":
        raise LiveCaptureError("Expected an HLS master playlist")
    variants = []
    for index, line in enumerate(lines):
        tag, _, attributes = line.partition(":")
        if tag in {"#EXT-X-SESSION-KEY", "#EXT-X-KEY", "#EXT-X-DEFINE", "#EXTINF"}:
            raise LiveCaptureError("Unsupported or encrypted HLS master playlist")
        if tag != "#EXT-X-STREAM-INF":
            continue
        if index + 1 == len(lines) or lines[index + 1].startswith("#"):
            raise LiveCaptureError("Invalid HLS master rendition URL")
        if re.search(r'(?:^|,)AUDIO=', attributes):
            continue
        height = re.search(r"(?:^|,)RESOLUTION=[0-9]{1,6}x([0-9]{1,6})(?:,|$)", attributes)
        variants.append((abs(int(height[1]) - 720) if height else 0, lines[index + 1]))
    if not variants:
        raise LiveCaptureError("Separate HLS audio playlists are unsupported")
    return _segment_url(base_url, min(variants)[1])


def _manifest(data, base_url):
    try:
        lines = [line.strip() for line in data.decode("utf-8-sig").splitlines() if line.strip()]
    except UnicodeError as exc:
        raise LiveCaptureError("Live HLS playlist is not valid UTF-8") from exc
    if not lines or lines[0] != "#EXTM3U":
        raise LiveCaptureError("Expected a live HLS media playlist")
    segments = []
    sequence = duration = timestamp = target = None
    ended = gap = False
    discontinuity_sequence = 0
    discontinuity_base_seen = False
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name in {"#EXT-X-MAP", "#EXT-X-BYTERANGE", "#EXT-X-STREAM-INF", "#EXT-X-SKIP",
                    "#EXT-X-DEFINE", "#EXT-X-I-FRAMES-ONLY", "#EXT-X-PART", "#EXT-X-SESSION-KEY"}:
            raise LiveCaptureError("Unsupported HLS playlist; a full, clear MPEG-TS rendition is required")
        if name == "#EXT-X-KEY" and value != "METHOD=NONE":
            raise LiveCaptureError("Encrypted or credential-protected HLS is unsupported")
        if name == "#EXT-X-DISCONTINUITY-SEQUENCE":
            if discontinuity_base_seen or segments or gap or not re.fullmatch(r"[0-9]{1,19}", value):
                raise LiveCaptureError("Invalid live HLS discontinuity sequence")
            discontinuity_sequence = int(value)
            discontinuity_base_seen = True
        elif name in {"#EXT-X-DISCONTINUITY", "#EXT-X-GAP"}:
            gap = True
            timestamp = None
            if name == "#EXT-X-DISCONTINUITY":
                discontinuity_sequence += 1
        elif name == "#EXT-X-MEDIA-SEQUENCE":
            if sequence is not None or not re.fullmatch(r"[0-9]{1,19}", value):
                raise LiveCaptureError("Invalid live HLS media sequence")
            sequence = int(value)
        elif name == "#EXT-X-TARGETDURATION":
            if target is not None or not re.fullmatch(r"[0-9]{1,2}", value) or not 1 <= int(value) <= 20:
                raise LiveCaptureError("Unsupported live HLS target duration")
            target = int(value)
        elif name == "#EXTINF":
            try:
                if duration is not None:
                    raise ValueError()
                duration = float(value.split(",", 1)[0])
                if not 0 < duration <= 20:
                    raise ValueError()
            except ValueError as exc:
                raise LiveCaptureError("Invalid live HLS segment duration") from exc
        elif name == "#EXT-X-PROGRAM-DATE-TIME":
            try:
                date = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
                if date.tzinfo is None or not 1970 <= date.year <= 9998:
                    raise ValueError()
                timestamp = date.timestamp()
            except (ValueError, OverflowError) as exc:
                raise LiveCaptureError("Invalid live HLS program date/time") from exc
        elif name == "#EXT-X-ENDLIST":
            ended = True
        elif not line.startswith("#"):
            if duration is None or sequence is None or len(segments) >= MAX_SEGMENTS or ended:
                raise LiveCaptureError("Invalid or oversized live HLS media playlist")
            segments.append(_Segment(sequence + len(segments), duration, _segment_url(base_url, line),
                                     timestamp, gap, discontinuity_sequence))
            timestamp = timestamp + duration if timestamp is not None else None
            duration, gap = None, False
    if duration is not None or target is None or sequence is None:
        raise LiveCaptureError("Incomplete live HLS media playlist")
    return segments, sequence + len(segments), target, ended


def _read(fetch, url, limit, label, event, deadline, progress, fraction):
    for attempt in range(3):
        _check(event, deadline, f"Live capture timed out waiting for {label}")
        data, write = _memory_writer(limit, f"Live {label} exceeds its byte limit")
        try:
            with fetch.open(url) as (final_url, response):
                for block in fetch.chunks(response):
                    _check(event, deadline, f"Live capture timed out downloading {label}")
                    write(block)
                length = response.getheader("Content-Length")
                if length is not None and (not length.isdecimal() or len(length) > 20 or len(data) != int(length)):
                    raise http.client.IncompleteRead(data)
            return final_url, data
        except _TRANSIENT:
            _check(event, deadline, f"Live capture timed out downloading {label}")
            if attempt == 2:
                raise LiveCaptureError(f"Live capture could not reconnect to the {label}; capture incomplete") from None
            progress(fraction, f"Reconnecting to live {label} (attempt {attempt + 2}/3)")
            event.wait(min(0.25 * (attempt + 1), max(0, deadline - time.monotonic())))
        except LiveCaptureError:
            raise
        except ValueError:
            _check(event, deadline, f"Live capture timed out downloading {label}")
            raise LiveCaptureError(f"Live {label} was rejected by the download guard or exceeded its transfer limit") from None


def _collect(url, raw, fetch, duration, event, deadline, progress, disk_limit):
    next_sequence = None
    elapsed, count, input_size = 0.0, 0, 0
    origin = None
    while elapsed < duration:
        base, data = _read(fetch, url, MAX_MANIFEST_BYTES, "playlist", event, deadline, progress, elapsed / duration)
        if next_sequence is None:
            for hop in range(3):
                variant = _master_variant(data, base)
                if variant is None:
                    break
                if hop == 2:
                    raise LiveCaptureError("Too many nested HLS master playlists")
                url = variant
                base, data = _read(fetch, url, MAX_MANIFEST_BYTES, "playlist",
                                   event, deadline, progress, elapsed / duration)
        segments, edge, target, ended = _manifest(data, base)
        if next_sequence is None:
            if ended:
                raise LiveCaptureError("Broadcast ended before capture began")
            next_sequence = edge
        if segments and segments[0].sequence > next_sequence:
            raise LiveCaptureError(f"Live HLS sequence gap: expected {next_sequence}, found {segments[0].sequence}")
        if edge < next_sequence:
            raise LiveCaptureError("Live HLS sequence moved backwards; capture alignment was lost")
        for segment in segments:
            if segment.sequence < next_sequence:
                continue
            if segment.gap:
                raise LiveCaptureError(f"Live HLS gap or discontinuity at sequence {segment.sequence}")
            if segment.timestamp is not None:
                estimated = segment.timestamp - elapsed
                if origin is not None and abs(estimated - origin) > 0.5:
                    raise LiveCaptureError(f"Live HLS timestamp gap at sequence {segment.sequence}")
                origin = origin if origin is not None else estimated
            if count >= MAX_SEGMENTS:
                raise LiveCaptureError("Live capture exceeds its segment limit")
            if disk_limit <= input_size:
                raise LiveCaptureError("Live capture exceeds its disk byte limit")
            _, body = _read(fetch, segment.url, min(MAX_SEGMENT_BYTES, disk_limit - input_size),
                            "segment", event, deadline, progress, elapsed / duration)
            if len(body) < 188 or len(body) % 188 or body[::188] != b"\x47" * (len(body) // 188):
                raise LiveCaptureError("Live rendition contains unsupported or truncated MPEG-TS segments")
            raw.write(body)
            input_size += len(body)
            count += 1
            elapsed += segment.duration
            next_sequence += 1
            progress(min(1.0, elapsed / duration), f"Captured {min(elapsed, duration):.1f}/{duration:g}s")
            if elapsed >= duration:
                return input_size, origin
        if ended:
            raise LiveCaptureError("Broadcast ended before the requested live duration was captured")
        event.wait(min(max(0, deadline - time.monotonic()), target / 2))
    raise AssertionError("Live collector did not produce its result")


def _encode(raw, partial, duration, event, deadline, disk_left):
    ffmpeg = os.environ.get("STREAMFRAME_FFMPEG_PATH", os.environ.get("STREAMFRAME_FFMPEG", "ffmpeg"))
    ffprobe = os.environ.get("STREAMFRAME_FFPROBE_PATH", os.environ.get("STREAMFRAME_FFPROBE", "ffprobe"))
    env = {key: value for key, value in os.environ.items() if not key.lower().endswith("_proxy")}
    starts = [_validate_packets(ffprobe, raw, stream, event, deadline, env) for stream in ("v:0", "a:0")]
    if abs(starts[0] - starts[1]) > 0.5:
        raise LiveCaptureError("Captured live audio and video do not start on the same timeline")
    command = [
        ffmpeg, "-nostdin", "-v", "error", "-xerror", "-abort_on", "empty_output_stream",
        "-protocol_whitelist", "pipe", "-f", "mpegts", "-i", "pipe:0",
        "-map", "0:v:0", "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1",
        "-t", f"{duration - _CUT_MARGIN:.6f}", "-vf", "fps=30,pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-bf", "0",
        "-c:a", "aac", "-ar", "48000", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    with raw.open("rb") as source, partial.open("xb") as dest:
        def write(block):
            nonlocal disk_left
            disk_left -= len(block)
            if disk_left < 0:
                raise LiveCaptureError("Live capture exceeds its disk byte limit")
            dest.write(block)

        if _run(command, event, deadline, write, stdin=source, env=env):
            raise LiveCaptureError("FFmpeg could not transcode the captured MPEG-TS audio/video")
    metadata, write = _memory_writer(4096, "Unexpected live capture verification output")
    if _run([
        ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0",
        "-use_absolute_path", "0", "-show_entries", "format=start_time,duration:stream=codec_type",
        "-of", "json", str(partial.resolve()),
    ], event, deadline, write, env=env):
        raise LiveCaptureError("Could not verify the captured live media")
    try:
        info = json.loads(metadata)
        actual = float(info["format"]["duration"])
        start = float(info["format"]["start_time"])
        streams = {stream["codec_type"] for stream in info["streams"]}
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveCaptureError("Captured live media does not have a verifiable timeline") from exc
    if (not duration - 0.35 <= actual <= duration or not 0 <= start <= 0.05
            or streams != {"audio", "video"}):
        raise LiveCaptureError("Captured live media has a timestamp gap or does not fit the requested duration")


def _validate_packets(ffprobe, raw, stream, event, deadline, env):
    """Reject jumps in the actual TS clock before FFmpeg can fill or smooth them."""
    first = previous = None
    pending = b""

    def write(block):
        nonlocal pending, first, previous
        *lines, pending = (pending + block).split(b"\n")
        if len(pending) > 1024:
            raise LiveCaptureError("Unexpected live packet verification output")
        for line in lines:
            if not line:
                continue
            timestamp, _, flags = line.partition(b",")
            if not re.fullmatch(rb"-?[0-9]{1,14}\.[0-9]{1,12}", timestamp):
                raise LiveCaptureError("Captured live packet is missing a verifiable timestamp")
            current = float(timestamp)
            if first is None:
                if stream == "v:0" and not flags.startswith(b"K"):
                    raise LiveCaptureError("Captured live video does not start on a keyframe")
                first = current
            if previous is not None and not 0 < current - previous <= 0.5:
                raise LiveCaptureError("Captured live media has a packet timestamp gap or reset")
            previous = current

    with raw.open("rb") as source:
        status = _run([
            ffprobe, "-v", "error", "-protocol_whitelist", "pipe", "-f", "mpegts",
            "-select_streams", stream, "-show_entries", "packet=dts_time,flags",
            "-of", "csv=p=0", "pipe:0",
        ], event, deadline, write, stdin=source, env=env)
    if status or first is None or pending:
        raise LiveCaptureError("Could not verify the captured live MPEG-TS audio/video")
    return first


def _capture(url, directory, duration, event, progress, on_alignment, fetch,
             capture_deadline, *, disk_limit=MAX_DISK_BYTES):
    suffix = uuid4().hex
    raw = directory / f".live-{suffix}.ts"
    partial = directory / f".live-{suffix}.mp4"
    target = directory / "source.mp4"
    try:
        with raw.open("xb") as dest:
            size, origin = _collect(url, dest, fetch, duration, event, capture_deadline, progress, disk_limit)
        progress(1.0, "Verifying captured live media")
        encode_deadline = time.monotonic() + ENCODE_SECONDS
        _encode(raw, partial, duration, event, encode_deadline, disk_limit - size)
        _check(event, encode_deadline, "Live capture encoding timed out")
        if on_alignment is not None:
            timestamp = datetime.fromtimestamp(origin, timezone.utc).isoformat() if origin is not None else None
            on_alignment(timestamp)
        _check(event, encode_deadline, "Live capture encoding timed out")
        os.link(partial, target)  # Atomic no-clobber publication in the job directory.
        return target
    finally:
        raw.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)


def capture_live(resolved, directory, *, duration, cancelled, progress, on_alignment=None):
    """Capture a finite stream starting with the next completed HLS segment.

    The resulting MP4 starts at media time zero. If supplied, ``on_alignment``
    receives the corresponding UTC HLS wall-clock estimate (or None if absent).
    ``progress`` receives a 0..1 fraction and status, including reconnects.
    """
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not 1 <= duration <= MAX_LIVE_SECONDS):
        raise LiveCaptureError(f"Live capture duration must be between 1 and {MAX_LIVE_SECONDS} seconds")
    directory = Path(directory).resolve()
    extractor, url = _page(resolved)
    if (directory / "source.mp4").exists():
        raise LiveCaptureError("A capture already exists in this job directory")
    progress(0.0, "Resolving public live broadcast")
    url = _resolve_hls(extractor, url, directory, cancelled, time.monotonic() + EXTRACT_SECONDS)
    deadline = time.monotonic() + duration + CAPTURE_GRACE_SECONDS
    fetch = jobs.RemoteFetch(cancelled, deadline)
    fetch.remaining = MAX_TRANSFER_BYTES
    progress(0.0, "Waiting for the next complete live segment")
    return _capture(url, directory, duration, cancelled, progress, on_alignment, fetch, deadline)


def _ts_clock(body, previous, event, deadline):
    """Validate the mux clock without decoding or starting a subprocess."""
    if len(body) < 188 or len(body) % 188 or body[::188] != b"\x47" * (len(body) // 188):
        raise LiveCaptureError("Rolling rendition contains unsupported or truncated MPEG-TS segments")
    pid, last = previous if previous is not None else (None, None)
    first = None
    for offset in range(0, len(body), 188):
        if offset % (188 * 4096) == 0:
            _check(event, deadline, "Live session reached its wall-clock deadline while validating a segment")
        if not body[offset + 3] & 0x20 or body[offset + 4] < 7 or not body[offset + 5] & 0x10:
            continue
        packet_pid = ((body[offset + 1] & 0x1f) << 8) | body[offset + 2]
        if pid is None:
            pid = packet_pid
        if pid != packet_pid:
            continue
        value = int.from_bytes(body[offset + 6:offset + 11], "big") >> 7
        if last is not None and (value - last) % (1 << 33) > 45_000:
            raise LiveCaptureError("Live MPEG-TS clock gap or reset across segments")
        if body[offset + 5] & 0x80:
            raise LiveCaptureError("Live MPEG-TS clock discontinuity")
        if first is None:
            first = value
        last = value
    if first is None:
        raise LiveCaptureError("Rolling rendition is missing a continuous MPEG-TS program clock")
    return (pid, last), first


@contextmanager
def _disk_guard(lock, event, deadline):
    if lock is None:
        yield
        return
    if not lock.acquire(timeout=max(0, min(0.25, deadline - time.monotonic()))):
        _check(event, deadline, "Live session reached its wall-clock deadline")
        raise LiveBackpressureError("Live session disk reservation lock blocked the producer")
    try:
        yield
    finally:
        lock.release()


def capture_windows(resolved, directory, *, session_seconds, window_seconds, cancelled,
                    progress, on_window, disk_remaining, disk_lock=None):
    """Synchronously hand off closed TS windows from one finite public live session.

    ``on_window(path, metadata)`` must enqueue without blocking, returning normally
    to take ownership or raising ``LiveBackpressureError`` without taking it.
    Metadata has inclusive ``first_sequence``/``last_sequence``, session-relative
    HLS ``start``/``end`` in seconds, and UTC ISO-8601 ``started_at`` or None.
    ``disk_remaining()`` must subtract the current raw file and all artifacts
    and reservations. Concurrent writers must use the same ``disk_lock`` when
    reserving bytes, or pre-reserve producer headroom separately. A free-space
    snapshot alone is not safe. Quota critical sections must stay brief.
    A normal callback return transfers ownership, even if the callback was slow.
    The producer does not encode, remove handed-off files, or wait for consumers.
    """
    for label, value, limit in (("session", session_seconds, MAX_SESSION_SECONDS),
                                ("window", window_seconds, MAX_WINDOW_SECONDS)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= limit:
            raise LiveCaptureError(f"Live {label} duration must be between 1 and {limit} seconds")
    if window_seconds > session_seconds:
        raise LiveCaptureError("Live window duration cannot exceed session duration")
    directory = Path(directory).resolve()
    extractor, url = _page(resolved)
    from .sources import MAX_METADATA_BYTES as source_metadata_limit

    media_budget = MAX_TRANSFER_BYTES - source_metadata_limit - MAX_METADATA_BYTES
    if media_budget <= 0:
        raise LiveCaptureError("Live session transfer limit leaves no budget after metadata reservations")
    now = time.monotonic()
    deadline = now + session_seconds
    _check(cancelled, deadline, "Live session reached its wall-clock deadline")
    progress(0.0, "Resolving public live broadcast")
    url = _resolve_hls(extractor, url, directory, cancelled, min(deadline, now + EXTRACT_SECONDS))
    fetch = jobs.RemoteFetch(cancelled, deadline)
    # Reserve both guarded metadata phases even if the caller resolved offline.
    # RemoteFetch charges all consumed bodies (including retry attempts) and
    # does not consume redirect bodies; TLS and headers are not payload bytes.
    fetch.remaining = media_budget
    next_sequence = previous_base = discontinuity = origin = clock = clock_origin = None
    elapsed = window_start = 0.0
    count = 0
    first_sequence = last_sequence = path = raw = None

    def report(message):
        progress(min(1.0, elapsed / session_seconds), message)

    def publish():
        nonlocal path, raw, first_sequence, window_start
        if raw is None:
            return
        if cancelled.is_set():
            raise jobs.Cancelled()
        raw.close()
        raw = None
        closed_path = path.with_suffix(".ts")
        # A concurrent quota scan must see the file under one name throughout.
        with _disk_guard(disk_lock, cancelled, deadline):
            if cancelled.is_set():
                raise jobs.Cancelled()
            path.rename(closed_path)
            path = closed_path
        started = None if origin is None else datetime.fromtimestamp(origin + window_start, timezone.utc).isoformat()
        began = time.monotonic()
        on_window(path, {
            "first_sequence": first_sequence, "last_sequence": last_sequence,
            "start": window_start, "end": elapsed, "started_at": started,
        })
        path = first_sequence = None
        window_start = elapsed
        if time.monotonic() - began > 0.25:
            raise LiveBackpressureError("Live window consumer blocked the producer; capture stopped")

    try:
        try:
            while True:
                _check(cancelled, deadline, "Live session reached its wall-clock deadline")
                base, data = _read(fetch, url, MAX_MANIFEST_BYTES, "playlist", cancelled,
                                   deadline, progress, elapsed / session_seconds)
                if next_sequence is None:
                    for hop in range(3):
                        variant = _master_variant(data, base)
                        if variant is None:
                            break
                        if hop == 2:
                            raise LiveCaptureError("Too many nested HLS master playlists")
                        url = variant
                        base, data = _read(fetch, url, MAX_MANIFEST_BYTES, "playlist", cancelled,
                                           deadline, progress, elapsed / session_seconds)
                segments, edge, target, ended = _manifest(data, base)
                base_sequence = edge - len(segments)
                if next_sequence is None:
                    if ended:
                        raise LiveCaptureError("Broadcast ended before rolling capture began")
                    next_sequence = edge
                    if segments:
                        discontinuity = segments[-1].discontinuity_sequence
                if base_sequence > next_sequence:
                    raise LiveCaptureError(f"Live HLS sequence gap: expected {next_sequence}, found {base_sequence}")
                if edge < next_sequence or (previous_base is not None and base_sequence < previous_base):
                    raise LiveCaptureError("Live HLS media sequence reset; capture alignment was lost")
                previous_base = base_sequence
                for segment in segments:
                    if segment.sequence < next_sequence:
                        continue
                    _check(cancelled, deadline, "Live session reached its wall-clock deadline")
                    if segment.gap or (discontinuity is not None
                                       and segment.discontinuity_sequence != discontinuity):
                        raise LiveCaptureError(f"Live HLS gap or discontinuity at sequence {segment.sequence}")
                    if segment.duration > window_seconds:
                        raise LiveCaptureError("Live HLS segment exceeds the rolling window duration")
                    if elapsed + segment.duration > session_seconds + 1e-7:
                        publish()
                        report(f"Live session media cap reached at {elapsed:g}s; next segment would exceed it")
                        return
                    if elapsed - window_start + segment.duration > window_seconds + 1e-7:
                        publish()
                    if count >= MAX_SEGMENTS:
                        raise LiveCaptureError("Live session exceeds its cumulative segment limit")
                    if segment.timestamp is not None:
                        estimated = segment.timestamp - elapsed
                        if origin is not None and abs(estimated - origin) > 0.5:
                            raise LiveCaptureError(f"Live HLS timestamp gap at sequence {segment.sequence}")
                    _, body = _read(fetch, segment.url, MAX_SEGMENT_BYTES, "segment", cancelled,
                                    deadline, progress, elapsed / session_seconds)
                    clock, first_pcr = _ts_clock(body, clock, cancelled, deadline)
                    if clock_origin is None:
                        clock_origin = first_pcr
                    pcr_elapsed = ((first_pcr - clock_origin) % (1 << 33)) / 90_000
                    if abs(pcr_elapsed - elapsed) > 0.5:
                        raise LiveCaptureError(f"Live HLS timeline drifts from the MPEG-TS clock at sequence {segment.sequence}")
                    _check(cancelled, deadline, "Live session reached its wall-clock deadline")
                    with _disk_guard(disk_lock, cancelled, deadline):
                        _check(cancelled, deadline, "Live session reached its wall-clock deadline")
                        if len(body) > disk_remaining():
                            raise LiveCaptureError("Live session exceeds its cumulative disk byte limit")
                        if raw is None:
                            path = directory / f".live-window-{uuid4().hex}.part"
                            raw = path.open("xb", buffering=0)
                            first_sequence = segment.sequence
                        if raw.write(body) != len(body):
                            raise LiveCaptureError("Could not write a complete live segment")
                    if segment.timestamp is not None and origin is None:
                        origin = estimated
                    last_sequence = segment.sequence
                    discontinuity = segment.discontinuity_sequence
                    count += 1
                    elapsed += segment.duration
                    next_sequence += 1
                    if elapsed - window_start >= window_seconds - 1e-7:
                        publish()
                    if elapsed >= session_seconds - 1e-7:
                        publish()
                        report(f"Live session media cap reached at {elapsed:g}s")
                        return
                    report(f"Captured {elapsed:g}s of live media")
                if ended:
                    publish()
                    raise LiveCaptureError(f"Broadcast ended early after {elapsed:g}s of live media")
                cancelled.wait(min(target / 2, max(0, deadline - time.monotonic())))
        except _LiveDeadline:
            if cancelled.is_set():
                raise jobs.Cancelled()
            if not count:
                raise LiveCaptureError("Live session deadline expired before any complete segment was acquired") from None
            publish()
            report(f"Live session wall-clock deadline reached; acquired {elapsed:g}/{session_seconds:g}s of live media")
    finally:
        if raw is not None:
            raw.close()
        if path is not None:
            path.unlink(missing_ok=True)
