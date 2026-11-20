"""Persistent local jobs and the deliberately small pipeline integration boundary.

Analysis adapter converts analysis.analyze_media's transcript, candidates, and
pixel-space facecam data to API candidates. Injected analyzers instead accept
(path, progress_callback, cancellation_event) and return API candidates or
{"candidates": [...], "source_words": [...]}. The complete source transcript is
stored once outside public job JSON; boundary expansion merges newly included
words without replacing caption edits or restoring words deleted in old bounds.
Legacy jobs and list-only analyzers have no source transcript to merge.
Injected renderers accept (path, candidate, output_path, progress, cancelled).

Rolling windows retain editable source alongside validated exports, under one
durable session media budget. Window times are session-relative; candidate times
remain relative to the retained encoded window.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import http.server
import importlib
import importlib.metadata
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import select
import shutil
import signal
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import uuid
from datetime import datetime, timezone


TERMINAL = {"succeeded", "failed", "cancelled"}
MAX_MEDIA_BYTES = 2 * 1024**3
MAX_DOWNLOAD_SECONDS = 600
MAX_RANGE_SECONDS = 1800
MAX_TRIM_SECONDS = 900
MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mpeg", ".mpg"}
TRIM_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpeg,mpegts"
MAX_REMOTE_METADATA = 16 * 1024**2
MAX_REMOTE_SEGMENTS = 1200
MAX_REMOTE_BYTES = 512 * 1024**2
MAX_MANIFEST_BYTES = 4 * 1024**2
DNS_SLOTS = threading.BoundedSemaphore(8)
ROLLING_DISK_BYTES = 512 * 1024**2
ROLLING_QUEUE_SIZE = 2
ROLLING_ENCODE_BYTES = 128 * 1024**2
ROLLING_EXPORT_BYTES = 128 * 1024**2
ROLLING_MAX_WINDOWS = 900
ROLLING_CANDIDATE_BYTES = 256 * 1024
ROLLING_WORK = ".rolling-work"
ROLLING_EXPORTS = "rolling-exports"
ROLLING_SOURCES = "rolling-sources"
ROLLING_EDITS = ".rolling-edits"
CANDIDATE_FIELDS = {"id", "start", "end", "score", "evidence", "words", "face_crop", "content_crop"}
DIAGNOSTIC_HISTORY_LIMIT = 24
DIAGNOSTIC_STAGES = {"queued", "import", "trim", "resolve", "download", "capture", "rolling",
                     "analysis", "export", "completed", "failed", "cancelled", "interrupted"}
DIAGNOSTIC_STATUSES = {"queued", "running", "exporting", "cancelling", "succeeded", "failed",
                       "cancelled", "interrupted"}
DIAGNOSTIC_ERRORS = {
    "YouTubeVodUnavailable": ("This YouTube archive does not offer a verified, supported "
                              "bounded HLS range or is still processing."),
    "SourceResolutionError": "The public source could not be resolved.",
    "LiveCaptureError": "The live source could not be captured.",
    "LiveBackpressureError": "Live capture could not keep up with processing.",
    "ValueError": "The source or processing result could not be used.",
    "OSError": "A file or network operation failed.",
    "UnexpectedError": "An unexpected processing error occurred.",
}
YOUTUBE_VOD_ERROR = "Bounded YouTube VOD import is unavailable: no verified time-to-byte index"


def _diagnostic_error(exc):
    """Keep only recognized exception categories, never exception text."""
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        name = type(current).__name__
        code = name if name in DIAGNOSTIC_ERRORS else "UnexpectedError"
        if isinstance(current, ValueError) and str(current) == YOUTUBE_VOD_ERROR:
            code = "YouTubeVodUnavailable"
        chain.append(code)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return chain


def _diagnostic_url(value):
    """Only show known public page paths; provider/CDN paths may themselves be secrets."""
    if not isinstance(value, str) or len(value) > 4096:
        return "Unavailable"
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        path = parsed.path
        if parsed.scheme not in {"http", "https"} or not host:
            return "Unavailable"
        allowed = (
            host in {"youtube.com", "www.youtube.com", "m.youtube.com"}
            and (path == "/watch" or re.fullmatch(r"/(?:@[A-Za-z0-9_.-]{1,100}|channel/[A-Za-z0-9_-]{1,100})/?", path))
        ) or (host == "youtu.be" and re.fullmatch(r"/[A-Za-z0-9_-]{11}", path)) or (
            host in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
            and re.fullmatch(r"/(?:videos/[0-9]+|[A-Za-z0-9_]{1,25})/?", path)
        )
        return f"{parsed.scheme}://{host}{path}" if allowed else "[URL redacted]"
    except ValueError:
        return "Unavailable"


def _diagnostic_time(value):
    if not finite_number(value):
        return "time unavailable"
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return "time unavailable"


def _diagnostic_version(package):
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"
    return version if re.fullmatch(r"[A-Za-z0-9.+_-]{1,64}", version) else "unavailable"


def _ffmpeg_version():
    try:
        executable = os.environ.get("STREAMFRAME_FFMPEG_PATH", os.environ.get("STREAMFRAME_FFMPEG", "ffmpeg"))
        result = subprocess.run([executable, "-version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=2, check=False)
        first = result.stdout.splitlines()[0][:256].decode("ascii", errors="replace")
        match = re.match(r"ffmpeg version ([A-Za-z0-9.+_-]{1,64})(?:\s|$)", first)
        return match.group(1) if match else "unavailable"
    except (OSError, subprocess.SubprocessError, IndexError):
        return "unavailable"


def video_page(url):
    """Canonical single-video URLs keep generic and playlist extractors out."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in {
        "www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be",
        "www.twitch.tv", "twitch.tv", "m.twitch.tv",
    } or parsed.fragment or len(url) > 4096 or any(ord(c) <= 32 for c in url):
        raise ValueError("Use a public HTTPS YouTube watch or Twitch VOD URL")
    if parsed.netloc.endswith("twitch.tv") and re.fullmatch(r"/videos/[0-9]+/?", parsed.path):
        return "twitch", "https://www.twitch.tv" + parsed.path.rstrip("/")
    if parsed.netloc == "youtu.be":
        ids = [parsed.path.removeprefix("/")]
    elif parsed.netloc.endswith("youtube.com") and parsed.path == "/watch":
        ids = parse_qs(parsed.query).get("v", [])
    else:
        ids = []
    if len(ids) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{11}", ids[0]):
        return "youtube", "https://www.youtube.com/watch?" + urlencode({"v": ids[0]})
    raise ValueError("Use a single YouTube watch or Twitch /videos/ URL, not a live channel or playlist")


class Cancelled(Exception):
    pass


class Conflict(ValueError):
    pass


class DeletionFailed(RuntimeError):
    pass


class TransientRemoteError(OSError):
    pass


def bounded_dns(host, port, cancelled, deadline):
    done = threading.Event()
    answer = []
    if not DNS_SLOTS.acquire(blocking=False):
        raise ValueError("DNS resolver is busy")

    def resolve():
        try:
            answer.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            answer.append(exc)
        finally:
            DNS_SLOTS.release()
            done.set()

    threading.Thread(target=resolve, daemon=True).start()
    while not done.wait(0.05):
        if cancelled.is_set():
            raise Cancelled()
        if time.monotonic() >= deadline:
            raise ValueError("DNS resolution timed out")
    if isinstance(answer[0], OSError):
        raise answer[0]
    return answer[0]


def public_target(url, *, cancelled=None, deadline=None):
    """Validate every DNS result; return a pinned public socket address."""
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) <= 32 for c in url):
        raise ValueError("Invalid URL")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.port not in {None, 80, 443}
                or "\\" in url):
            raise ValueError("Only public HTTP(S) URLs without credentials are supported")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = (bounded_dns(parsed.hostname, port, cancelled, deadline)
                     if cancelled is not None and deadline is not None
                     else socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM))
    except (OSError, ValueError) as exc:
        raise ValueError("URL host is invalid or cannot be resolved") from exc
    if not addresses:
        raise ValueError("URL host cannot be resolved")
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        transition = ip.version == 6 and any(
            ip in ipaddress.ip_network(network) for network in
            ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
        )
        if not ip.is_global or ip.is_multicast or transition or (ip.version == 6 and ip.ipv4_mapped is not None):
            raise ValueError("Private, reserved, and loopback URL hosts are forbidden")
    return addresses[0]


class DownloadProxy:
    """DNS-pinning proxy for metadata-only yt-dlp requests and redirected targets."""

    def __init__(self, cancelled, limit=MAX_MEDIA_BYTES):
        self.cancelled = cancelled
        self.closed = threading.Event()
        self.remaining = limit
        self.lock = threading.Lock()
        self.deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_CONNECT(self):
                self.forward(True)

            def do_GET(self):
                self.forward(False)

            def do_HEAD(self):
                self.forward(False)

            def do_POST(self):
                # Platform metadata APIs can POST through HTTPS tunnels only.
                self.send_error(403)

            def forward(self, tunnel):
                upstream = None
                try:
                    target = "https://" + self.path if tunnel else self.path
                    if tunnel and not re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\]):443", self.path):
                        raise ValueError("Invalid tunnel authority")
                    family, kind, proto, _, address = public_target(
                        target, cancelled=owner.cancelled, deadline=min(owner.deadline, time.monotonic() + 8)
                    )
                    upstream = socket.socket(family, kind, proto)
                    upstream.settimeout(10)
                    upstream.connect(address)
                    if tunnel:
                        self.send_response(200, "Connection established")
                        self.end_headers()
                    else:
                        parsed = urlsplit(target)
                        path = parsed.path or "/"
                        if parsed.query:
                            path += "?" + parsed.query
                        request = f"{self.command} {path} HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
                        for key, value in self.headers.items():
                            if key.lower() not in {
                                "host", "connection", "proxy-connection",
                                "proxy-authorization", "authorization", "cookie",
                            }:
                                request += f"{key}: {value}\r\n"
                        upstream.sendall((request + "Connection: close\r\n\r\n").encode("latin-1"))
                    self.connection.settimeout(2)
                    upstream.settimeout(2)
                    peers = [self.connection, upstream]
                    while not owner.closed.is_set() and not owner.cancelled.is_set() and time.monotonic() < owner.deadline:
                        ready, _, _ = select.select(peers, [], [], 0.25)
                        for peer in ready:
                            data = peer.recv(65536)
                            if not data:
                                return
                            with owner.lock:
                                owner.remaining -= len(data)
                                if owner.remaining < 0:
                                    return
                            (upstream if peer is self.connection else self.connection).sendall(data)
                except (OSError, ValueError, Cancelled):
                    if upstream is None:
                        self.send_error(403, "Destination denied")
                finally:
                    if upstream is not None:
                        upstream.close()
                    self.close_connection = True

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *_):
        self.closed.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def analyze_media(path, progress, cancelled, *, cache_transcription=True):
    module = importlib.import_module("streamframe.analysis")
    progress(0, "Analyzing media")
    yunet = os.environ.get("STREAMFRAME_YUNET_MODEL_PATH", os.environ.get("STREAMFRAME_YUNET_MODEL"))
    if yunet is None:
        bundled_model = Path(__file__).resolve().parents[2] / "models" / "face_detection_yunet_2023mar.onnx"
        yunet = str(bundled_model) if bundled_model.is_file() else None
    result = module.analyze_media(
        str(path),
        ffmpeg_path=os.environ.get("STREAMFRAME_FFMPEG_PATH",
                                   os.environ.get("STREAMFRAME_FFMPEG", "ffmpeg")),
        whisper_model=os.environ.get("STREAMFRAME_WHISPER_MODEL", "small"),
        device=os.environ.get("STREAMFRAME_DEVICE", "cpu"),
        compute_type=os.environ.get("STREAMFRAME_COMPUTE_TYPE", "int8"),
        yunet_model_path=yunet or None,
        cache_transcription=cache_transcription,
        cancellation_callback=cancelled.is_set,
    )
    if cancelled.is_set():
        raise Cancelled()
    words = result.get("transcript", {}).get("words", [])
    media = result.get("media", {})
    facecam = result.get("facecam", {})
    box = facecam.get("box")
    crop = None
    width, height = media.get("width"), media.get("height")
    if box and width and height:
        if facecam.get("framing") in {"webcam_boundary", "face_fallback"}:
            x, y, w, h = (box[key] for key in ("x", "y", "width", "height"))
            if min(x, y) >= 0 and min(w, h) > 0 and x + w <= width and y + h <= height:
                crop = {"x": x / width, "y": y / height,
                        "width": w / width, "height": h / height}
        else:
            crop = padded_face_crop(box, width, height)
    candidates = []
    for item in result["candidates"]:
        evidence = item.get("evidence", {})
        if isinstance(evidence, dict):
            evidence = [f"{key}: {value:.3f}" for key, value in evidence.items()
                        if finite_number(value)]
        if crop is None and box:
            evidence.append("Detected face cannot fit the output panel; set the face crop manually.")
        elif crop is None:
            evidence.append("Facecam not confidently detected; set the face crop manually.")
        elif facecam.get("framing") == "face_fallback":
            evidence.append("Webcam boundary uncertain; a roomy face-based crop is suggested. Verify or set the face crop manually.")
        elif facecam.get("framing") == "webcam_boundary":
            evidence.append("Webcam boundary is estimated; verify the face crop and streamer identity.")
        else:
            evidence.append("Detected face may not be the streamer; verify the face crop.")
        candidates.append(dict(
            id=uuid.uuid4().hex, start=item["start"], end=item["end"],
            score=item.get("score", 0), evidence=evidence,
            words=[{key: word[key] for key in ("start", "end", "text")}
                   for word in words if word["start"] < item["end"] and word["end"] > item["start"]],
            face_crop=crop, content_crop=None,
        ))
    progress(1, "Analysis complete")
    return {
        "candidates": candidates,
        "source_words": [{key: word[key] for key in ("start", "end", "text")} for word in words],
    }


def render_candidate(path, candidate, output_path, progress, cancelled, *, max_output_bytes=None):
    module = importlib.import_module("streamframe.render")
    return module.render_candidate(
        str(path), candidate, str(output_path), progress=progress, cancelled=cancelled,
        max_output_bytes=max_output_bytes,
    )


def finite_number(value):
    if type(value) is int:
        return abs(value).bit_length() <= 1023
    return type(value) is float and math.isfinite(value)


def padded_face_crop(box, width, height):
    x, y, w, h = (box[key] for key in ("x", "y", "width", "height"))
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height:
        return None
    ratio = 9 / 8
    needed = max(h, w / ratio)
    available = min(height, width / ratio)
    if needed > available:
        return None
    crop_h = min(available, needed * 1.30)
    crop_w = crop_h * ratio
    crop_x = max(0, min(width - crop_w, x + (w - crop_w) / 2))
    crop_y = max(0, min(height - crop_h, y + (h - crop_h) / 2))
    return {"x": crop_x / width, "y": crop_y / height,
            "width": crop_w / width, "height": crop_h / height}


def source_range(source):
    if "start" not in source and "end" not in source:
        return {}
    start = source.get("start", 0)
    if not finite_number(start) or not 0 <= start <= 2**31 - 1:
        raise ValueError("Source start must be a nonnegative finite timestamp")
    end = source.get("end", start + MAX_RANGE_SECONDS)
    if not finite_number(end) or not start < end <= start + MAX_RANGE_SECONDS:
        raise ValueError(f"Source end must exceed start by at most {MAX_RANGE_SECONDS} seconds")
    # FFmpeg's time options have microsecond resolution.
    start, end = round(start, 6), round(end, 6)
    if end <= start:
        raise ValueError("Source range is shorter than one microsecond")
    return {"start": start, "end": end}


def validate_remote_source(source):
    allowed = {"type", "mode", "duration", "start", "end", "rolling",
               "session_duration", "window_duration"}
    remote_url = source["type"] == "url"
    allowed |= {"url"} if remote_url else {"identifier", "platform"}
    if set(source) - allowed:
        raise ValueError("Invalid remote source fields")
    mode = source.get("mode", "auto")
    if mode not in ("auto", "live", "latest"):
        raise ValueError("Source mode must be auto, live, or latest")
    rolling = source.get("rolling", False)
    if type(rolling) is not bool:
        raise ValueError("rolling must be a boolean")
    if rolling:
        from .live import MAX_SESSION_SECONDS

        if mode == "latest" or {"duration", "start", "end"} & source.keys():
            raise ValueError("Rolling capture is live-only and uses session_duration/window_duration")
        session = source.get("session_duration", 600)
        if not finite_number(session) or not 1 <= session <= MAX_SESSION_SECONDS:
            raise ValueError(f"session_duration must be between 1 and {MAX_SESSION_SECONDS} seconds")
        window = source.get("window_duration", min(60, session))
        if not finite_number(window) or not 1 <= window <= min(60, session):
            raise ValueError("window_duration must be between 1 and 60 seconds and fit within the session")
        # Even Auto must never resolve to or download a VOD in rolling mode.
        mode = "live"
        source = {**source, "mode": mode, "session_duration": session, "window_duration": window}
    elif {"session_duration", "window_duration"} & source.keys():
        raise ValueError("session_duration and window_duration require rolling=true")
    explicit_range = "start" in source or "end" in source
    if explicit_range and (not {"start", "end"} <= source.keys() or mode == "live"):
        raise ValueError("VOD ranges require both start and end; live uses duration")
    duration = source.get("duration", 60)
    if not finite_number(duration) or not 1 <= duration <= 300:
        raise ValueError("Capture duration must be between 1 and 300 seconds")
    if remote_url:
        url = source.get("url")
        if not isinstance(url, str):
            raise ValueError("A public Twitch or YouTube HTTPS URL is required")
        try:
            _, canonical = video_page(url)
        except ValueError:
            parsed = urlsplit(url)
            twitch = parsed.netloc in {"www.twitch.tv", "twitch.tv", "m.twitch.tv"}
            youtube = parsed.netloc in {"www.youtube.com", "youtube.com", "m.youtube.com"}
            channel = ((twitch and re.fullmatch(r"/[A-Za-z0-9_]{1,25}/?", parsed.path))
                       or (youtube and re.fullmatch(r"/(?:@[A-Za-z0-9_.-]+|channel/[A-Za-z0-9_-]+)/?", parsed.path)))
            if (parsed.scheme != "https" or not channel or parsed.query or parsed.fragment
                    or len(url) > 4096 or any(ord(c) <= 32 for c in url)):
                raise ValueError("Use a public HTTPS Twitch or YouTube video or channel URL")
        else:
            if mode != "live" and not explicit_range and "mode" not in source:
                raise ValueError("Finished video URLs require explicit start and end")
            return {**source, "url": canonical, **source_range(source)}
    else:
        platform, identifier = source.get("platform", "twitch"), source.get("identifier")
        if platform not in ("twitch", "youtube") or not isinstance(identifier, str):
            raise ValueError("A Twitch username or explicit YouTube handle/channel is required")
        pattern = (r"[A-Za-z0-9_]{1,25}" if platform == "twitch"
                   else r"@[A-Za-z0-9_.-]{1,100}")
        if not re.fullmatch(pattern, identifier):
            raise ValueError("Invalid streamer identifier; use type=url for channel links")
        source = {**source, "platform": platform}
    return {**source, "mode": mode, **source_range(source)}


def open_regular_file(path):
    """Open an already resolved absolute path without following raced symlinks."""
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            next_dir = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=directory)
            os.close(directory)
            directory = next_dir
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Source is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def wait_process(command, event, deadline, *, output=None, size_limit=None, **options):
    if event.is_set():
        raise Cancelled()
    process = subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, **options)
    try:
        while True:
            if event.is_set():
                raise Cancelled()
            if time.monotonic() >= deadline:
                raise ValueError("Media import timed out")
            if output is not None and output.exists() and output.stat().st_size >= size_limit:
                raise ValueError("Media import exceeds its size limit")
            if process.poll() is not None:
                return process.returncode
            event.wait(0.1)
    finally:
        stop_process(process)


def trim_media(fd, directory, start, end, event, *, deadline=None, staged=False):
    partial = directory / ".trim.mp4"
    target = directory / "source.mp4"
    if event.is_set():
        raise Cancelled()
    ffmpeg = os.environ.get("STREAMFRAME_FFMPEG_PATH", os.environ.get("STREAMFRAME_FFMPEG", "ffmpeg"))
    command = [
        ffmpeg, "-nostdin", "-v", "error", "-n",
        "-protocol_whitelist", "fd", "-format_whitelist", "mpegts" if staged else TRIM_FORMATS,
        *(["-f", "mpegts"] if staged else ["-enable_drefs", "0", "-use_absolute_path", "0"]),
        *([] if staged else ["-ss", f"{start:.6f}"]),
        "-fd", str(fd), "-i", "fd:",
        *(["-ss", f"{start:.6f}"] if staged else []),
        "-t", f"{end - start:.6f}", "-map", "0:V:0", "-map", "0:a:0?",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:a", "aac", "-movflags", "+faststart",
        "-abort_on", "empty_output_stream", "-fs", str(MAX_MEDIA_BYTES),
        str(partial),
    ]
    partial.unlink(missing_ok=True)
    try:
        env = {key: value for key, value in os.environ.items() if not key.lower().endswith("_proxy")}
        code = wait_process(
            command, event, deadline or time.monotonic() + MAX_TRIM_SECONDS,
            output=partial, size_limit=MAX_MEDIA_BYTES, pass_fds=(fd,),
            stdout=subprocess.DEVNULL, env=env,
        )
        if code:
            raise ValueError("FFmpeg could not trim this media range (seekable input and libx264 required)")
        if not partial.is_file() or not 0 < partial.stat().st_size < MAX_MEDIA_BYTES:
            raise ValueError("Trimmed media is empty or exceeds the size limit")
        partial.replace(target)
        return target
    finally:
        partial.unlink(missing_ok=True)


class RemoteFetch:
    """Fetch media over TLS with DNS pinning on every hop and a shared byte budget."""

    def __init__(self, event, deadline, *, validate_url=None):
        self.event = event
        self.deadline = deadline
        self.validate_url = validate_url
        self.remaining = MAX_REMOTE_BYTES
        self.lock = threading.Lock()
        self.exhausted = False

    def check(self):
        if self.event.is_set():
            raise Cancelled()
        if time.monotonic() >= self.deadline:
            raise ValueError("Remote range import timed out")
        if self.exhausted:
            raise ValueError("Remote media exceeds the transfer limit")

    @contextmanager
    def open(self, url):
        for _ in range(6):
            self.check()
            if self.validate_url is not None:
                self.validate_url(url)
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.port not in {None, 443}:
                raise ValueError("Remote media and redirects must use HTTPS")
            timeout = min(8, max(0.1, self.deadline - time.monotonic()))
            family, kind, proto, _, address = public_target(
                url, cancelled=self.event, deadline=min(self.deadline, time.monotonic() + timeout)
            )
            connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout)
            active = []
            finished = threading.Event()

            def interrupt():
                while not finished.wait(0.05):
                    if self.event.is_set() or time.monotonic() >= self.deadline:
                        for sock in tuple(active):
                            try:
                                sock.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                        break

            watcher = threading.Thread(target=interrupt, daemon=True)
            watcher.start()
            try:
                with socket.socket(family, kind, proto) as raw:
                    active.append(raw)
                    raw.settimeout(timeout)
                    raw.connect(address)
                    context = ssl.create_default_context()
                    with context.wrap_socket(raw, server_hostname=parsed.hostname,
                                             do_handshake_on_connect=False) as tls:
                        active.append(tls)
                        self.check()
                        tls.do_handshake()
                        connection.sock = tls
                        path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
                        headers = {"Accept-Encoding": "identity", "Connection": "close"}
                        connection.request("GET", path, headers=headers)
                        response = connection.getresponse()
                        try:
                            if response.status in {301, 302, 303, 307, 308}:
                                location = response.getheader("Location")
                                if not location:
                                    raise ValueError("Remote redirect is missing a destination")
                                url = urljoin(url, location)
                                continue
                            if response.status in {408, 429} or 500 <= response.status <= 599:
                                raise TransientRemoteError(f"Remote server returned HTTP {response.status}")
                            if response.status != 200:
                                raise ValueError("Remote media is unavailable")
                            if response.getheader("Content-Encoding", "identity") != "identity":
                                raise ValueError("Compressed remote HTTP responses are unsupported")
                            yield url, response
                            self.check()
                            return
                        finally:
                            response.close()
            except (OSError, http.client.HTTPException):
                self.check()
                raise
            finally:
                finished.set()
                watcher.join()
                connection.close()
        raise ValueError("Too many remote redirects")

    def chunks(self, response):
        while True:
            self.check()
            data = response.read1(65536)
            if not data:
                return
            with self.lock:
                self.remaining -= len(data)
                if self.remaining < 0:
                    self.exhausted = True
                    raise ValueError("Remote media exceeds the transfer limit")
            yield data


def hls_segments(manifest, base_url, start, end):
    """Select overlapping segments from an ended, unencrypted TS media playlist."""
    lines = [line.strip() for line in manifest.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U" or lines[-1] != "#EXT-X-ENDLIST":
        raise ValueError("Twitch requires a finished VOD with an ended media playlist")
    position = 0.0
    duration = None
    selected = []
    for line in lines[1:]:
        if line.startswith("#EXTINF:"):
            if duration is not None:
                raise ValueError("Malformed segment manifest")
            duration = float(line.removeprefix("#EXTINF:").split(",", 1)[0])
            if not 0 < duration <= 60:
                raise ValueError("Unsupported segment duration")
        elif line in {"#EXT-X-KEY:METHOD=NONE", "#EXT-X-DISCONTINUITY-SEQUENCE:0"}:
            continue
        elif line.startswith(("#EXT-X-KEY", "#EXT-X-MAP", "#EXT-X-BYTERANGE", "#EXT-X-DISCONTINUITY",
                              "#EXT-X-STREAM-INF", "#EXT-X-GAP", "#EXT-X-PART", "#EXT-X-SKIP")):
            raise ValueError("Unsupported Twitch playlist; only plain MPEG-TS segments are allowed")
        elif not line.startswith("#"):
            if duration is None:
                raise ValueError("Malformed segment manifest")
            next_position = position + duration
            if position < end and next_position > start:
                selected.append((position, urljoin(base_url, line)))
                if len(selected) > MAX_REMOTE_SEGMENTS:
                    raise ValueError("Source range exceeds the segment limit")
            position, duration = next_position, None
    if not selected or end > position + 0.25 or duration is not None:
        raise ValueError("Source range exceeds the completed Twitch playlist")
    return selected


def resolve_video(platform, url, directory, event, deadline):
    metadata = directory / ".remote-metadata.json"
    try:
        with DownloadProxy(event, limit=MAX_REMOTE_METADATA) as proxy, metadata.open("wb") as output:
            command = [
                sys.executable, "-I", "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs",
                "--no-config-locations", "--no-cookies", "--no-cookies-from-browser",
                "--no-cache-dir", "--no-playlist", "--no-warnings", "--skip-download",
                "--dump-single-json", "--socket-timeout", "8", "--retries", "0",
                "--extractor-retries", "0", "--proxy", proxy,
                "--use-extractors", "youtube" if platform == "youtube" else "twitch:vod", "--", url,
            ]
            env = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
            env.update(HOME=str(directory), XDG_CONFIG_HOME=str(directory),
                       HTTP_PROXY=proxy, HTTPS_PROXY=proxy, ALL_PROXY=proxy, NO_PROXY="")
            code = wait_process(
                command, event, deadline, stdout=output, env=env,
                output=metadata, size_limit=MAX_REMOTE_METADATA,
            )
        if code:
            raise ValueError("yt-dlp could not resolve this public VOD without authentication")
        result = json.loads(metadata.read_bytes())
    finally:
        metadata.unlink(missing_ok=True)
    if (result.get("_type") not in {None, "video"} or result.get("is_live")
            or result.get("live_status") not in {None, "not_live", "was_live"}
            or result.get("availability") not in {None, "public", "unlisted"}
            or not finite_number(result.get("duration")) or result["duration"] <= 0):
        raise ValueError("URL must be a public, ended VOD with a known duration")
    return result


def import_remote(source, directory, event, *, allow_shorter=False, on_resolved=None):
    platform, url = video_page(source["url"])
    if platform == "youtube":
        from .youtube_vod import import_youtube_vod
        return import_youtube_vod(url, directory, event, source["start"], source["end"],
                                  allow_shorter=allow_shorter, on_resolved=on_resolved)
    deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
    info = resolve_video(platform, url, directory, event, deadline)
    start, end = source["start"], source["end"]
    if allow_shorter:
        end = min(end, info["duration"])
    if end <= start:
        raise ValueError("Requested range starts past the public video's duration")
    if end > info["duration"] + 0.25:
        raise ValueError("Requested range exceeds the public video's duration")
    if on_resolved:
        on_resolved(info.get("title"), end)
    fetch = RemoteFetch(event, deadline)
    formats = [item for item in info.get("formats", []) if isinstance(item, dict) and isinstance(item.get("url"), str)
               and item["url"].startswith("https://") and not item.get("has_drm")]
    if platform == "twitch":
        variants = [item for item in formats if item.get("protocol") in {"m3u8_native", "m3u8"}
                    and item.get("vcodec") != "none" and not item.get("is_live")]
        if not variants:
            raise ValueError("No supported Twitch HLS format is available")
        # Prefer mid-resolution renditions for practical transfer and encode costs.
        variant = min(variants, key=lambda item: abs((item.get("height") or 720) - 720))
        with fetch.open(variant["url"]) as (base_url, response):
            manifest = bytearray()
            for data in fetch.chunks(response):
                manifest.extend(data)
                if len(manifest) > MAX_MANIFEST_BYTES:
                    raise ValueError("Twitch manifest exceeds its size limit")
        selected = hls_segments(manifest.decode("utf-8-sig"), base_url, start, end)
        staged = directory / ".remote-segments.ts"
        staged.unlink(missing_ok=True)
        try:
            with staged.open("xb") as output:
                for _, segment_url in selected:
                    with fetch.open(segment_url) as (_, response):
                        length = 0
                        for data in fetch.chunks(response):
                            if any(value != 0x47 for value in data[(-length % 188)::188]):
                                raise ValueError("Unsupported Twitch segment; expected MPEG-TS")
                            length += len(data)
                            output.write(data)
                        if not length or length % 188:
                            raise ValueError("Twitch segment is empty or truncated")
            offset = selected[0][0]
            with staged.open("rb") as media:
                return trim_media(media.fileno(), directory, start - offset, end - offset,
                                  event, deadline=deadline, staged=True)
        finally:
            staged.unlink(missing_ok=True)


def validate_candidate(candidate):
    """Canonical JSON candidate schema shared by adapters and PATCH."""
    if not isinstance(candidate, dict):
        raise ValueError("Candidate must be an object")
    allowed = CANDIDATE_FIELDS
    if set(candidate) - allowed:
        raise ValueError("Unknown candidate fields")
    result = copy.deepcopy(candidate)
    result.setdefault("id", uuid.uuid4().hex)
    if not isinstance(result["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", result["id"]):
        raise ValueError("Invalid candidate id")
    start, end = result.get("start"), result.get("end")
    if not finite_number(start) or not finite_number(end) or not 0 <= start < end:
        raise ValueError("Candidate requires finite times with 0 <= start < end")
    result.setdefault("score", 0)
    if not finite_number(result["score"]):
        raise ValueError("Invalid score")
    result.setdefault("evidence", [])
    if not isinstance(result["evidence"], list) or not all(isinstance(x, str) for x in result["evidence"]):
        raise ValueError("Evidence must be a list of strings")
    result.setdefault("words", [])
    if not isinstance(result["words"], list) or len(result["words"]) > 100000:
        raise ValueError("Invalid words")
    previous_start = -1.0
    for word in result["words"]:
        if (not isinstance(word, dict) or set(word) - {"start", "end", "text", "correction"}
                or not {"start", "end", "text"}.issubset(word)
                or not finite_number(word["start"]) or not finite_number(word["end"])
                or not 0 <= word["start"] < word["end"]
                or not isinstance(word["text"], str) or len(word["text"]) > 10000
                or ("correction" in word and
                    (not isinstance(word["correction"], str) or len(word["correction"]) > 10000))):
            raise ValueError("Words require start, end, text and optional string correction")
        if word["start"] < previous_start:
            raise ValueError("Words must be ordered by start time")
        previous_start = word["start"]
    for field in ("face_crop", "content_crop"):
        result.setdefault(field, None)
        crop = result[field]
        if crop is not None:
            if (not isinstance(crop, dict) or set(crop) != {"x", "y", "width", "height"}
                    or not all(finite_number(x) for x in crop.values())
                    or crop["x"] < 0 or crop["y"] < 0 or crop["width"] <= 0 or crop["height"] <= 0
                    or crop["x"] + crop["width"] > 1 or crop["y"] + crop["height"] > 1):
                raise ValueError("Crops must be normalized x, y, width, height rectangles")
    json.dumps(result, allow_nan=False)
    return result


class _RollingDisk:
    """Reserve media bytes under the same lock as the producer's check-and-append."""

    def __init__(self, raw_directory, retained=0, persist=None, limit=None):
        self.raw_directory = raw_directory
        self.lock = threading.Lock()
        self.working, self.retained = 0, retained
        self.persist = persist or (lambda retained, working: None)
        self.limit = ROLLING_DISK_BYTES if limit is None else limit

    def disk_remaining(self):
        # Called with the lock held until the raw append completes.
        raw = 0
        for path in self.raw_directory.glob("*"):
            try:
                # The producer's only disk writes are the unbuffered TS segment appends.
                raw += path.stat().st_size
            except FileNotFoundError:
                pass  # Consumer or producer just removed this file.
        return self.limit - raw - self.working - self.retained

    def reserve(self, limit, overhead=0, headroom=0):
        with self.lock:
            allowance = min(limit, self.disk_remaining() - overhead - headroom)
            if allowance <= 0:
                raise ValueError("Live session disk budget exhausted")
            self.working += allowance + overhead
            self.persist(self.retained, self.working)
            return allowance

    def reduce(self, amount):
        with self.lock:
            self.working -= amount
            self.persist(self.retained, self.working)

    def finish_window(self, published_bytes):
        with self.lock:
            self.retained += published_bytes
            self.working = 0
            self.persist(self.retained, self.working)

    def allocation(self):
        return _RollingAllocation(self)


class _RollingAllocation:
    """Release only this operation's reservation, never a concurrent export's."""

    def __init__(self, disk):
        self.disk, self.working = disk, 0

    def reserve(self, limit, overhead=0, headroom=0):
        maximum = self.disk.reserve(limit, overhead, headroom)
        self.working += maximum + overhead
        return maximum

    def reduce(self, amount):
        self.disk.reduce(amount)
        self.working -= amount

    def finish_window(self, published_bytes):
        with self.disk.lock:
            self.disk.retained += published_bytes
            self.disk.working -= self.working
            self.working = 0
            self.disk.persist(self.disk.retained, self.disk.working)


def _rolling_metadata(metadata, previous, session, window):
    if not isinstance(metadata, dict):
        raise ValueError("Missing live window metadata")
    first, last = metadata.get("first_sequence"), metadata.get("last_sequence")
    start, end = metadata.get("start"), metadata.get("end")
    alignment = metadata.get("started_at")
    if (type(first) is not int or type(last) is not int or not 0 <= first <= last < 2**63
            or not finite_number(start) or not finite_number(end)
            or not 0 <= start < end <= session + 1e-6 or end - start > window + 1e-6
            or (alignment is not None and
                (not isinstance(alignment, str) or not re.fullmatch(
                    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:\+00:00|Z)", alignment)))):
        raise ValueError("Invalid live window metadata")
    if ((previous is not None and first != previous["last_sequence"] + 1)
            or abs(start - (previous["end"] if previous else 0)) > 1e-6):
        raise ValueError("Live window sequence gap, overlap, or timestamp reset")
    return dict(first_sequence=first, last_sequence=last, start=start, end=end, started_at=alignment,
                sequence_key=f"{first}:{last}")


def _raw_fingerprint(raw, event):
    digest = hashlib.sha256()
    with raw.open("rb") as media:
        while block := media.read(1024 * 1024):
            if event.is_set():
                raise Cancelled()
            digest.update(block)
    return digest.hexdigest()


def _speech_fingerprint(candidate):
    text = " ".join(w["text"] for w in candidate["words"]
                    if w["start"] < candidate["end"] and w["end"] > candidate["start"])
    tokens = re.findall(r"[^\W_]+", text.casefold())
    if len(tokens) < 16 or len(set(tokens)) < 8:
        return None, set()
    grams = set(zip(tokens, tokens[1:], tokens[2:], tokens[3:], tokens[4:]))
    return hashlib.sha256(" ".join(tokens).encode()).hexdigest(), grams


def _sync_path(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rolling_encode(raw, media, duration, event, maximum):
    from . import live

    deadline = time.monotonic() + live.ENCODE_SECONDS
    live._encode(raw, media, duration, event, deadline, maximum)
    probe = os.environ.get("STREAMFRAME_FFPROBE_PATH", os.environ.get("STREAMFRAME_FFPROBE", "ffprobe"))
    data, write = live._memory_writer(4096, "Unexpected live window duration probe output")
    if live._run([probe, "-v", "error", "-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0",
                  "-use_absolute_path", "0", "-show_entries", "format=duration",
                  "-of", "json", str(media)], event, deadline, write):
        raise ValueError("Could not verify encoded live window duration")
    return float(json.loads(data)["format"]["duration"])


def _rolling_process(args, event, stage, *, data=None, env=None, committed=None):
    if event.is_set():
        raise Cancelled()
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", *args],
        stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE if committed is None else subprocess.DEVNULL,
        stderr=subprocess.PIPE, start_new_session=True, env=env,
    )
    try:
        deadline = time.monotonic() + 180
        while True:
            try:
                stdout, diagnostic = process.communicate(input=data, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                data = None
                if event.is_set() and (committed is None or not committed.is_file()):
                    stop_process(process)
                    # The atomic rename may have won the race with termination.
                    if committed is not None and committed.is_file():
                        return None
                    raise Cancelled()
                if time.monotonic() >= deadline:
                    if committed is not None and committed.is_file():
                        return None
                    raise ValueError(f"Live window {stage} timed out")
        if process.returncode and (committed is None or not committed.is_file()):
            raise ValueError(f"Live window {stage} failed: {diagnostic[-1500:].decode(errors='replace')}")
        return stdout
    finally:
        stop_process(process)


def _rolling_analyze(media, event):
    script = (
        "import json,sys,threading; from streamframe.jobs import analyze_media; "
        "result=analyze_media(sys.argv[1],lambda *_:None,threading.Event(),cache_transcription=False); "
        "json.dump(result,sys.stdout,allow_nan=False)"
    )
    data = _rolling_process([script, str(media)], event, "analysis",
                            env={**os.environ, "CUDA_CACHE_DISABLE": "1"})
    return json.loads(data)


ROLLING_FONTS = b"""<?xml version="1.0"?>
<fontconfig>
 <dir>/System/Library/Fonts</dir><dir>/Library/Fonts</dir>
 <dir>/usr/share/fonts</dir><dir>/usr/local/share/fonts</dir>
 <cachedir>/dev/null</cachedir>
</fontconfig>
"""


def _rolling_render_overhead(candidate):
    from . import render

    subtitles = render._words(candidate["words"], candidate["start"], candidate["end"])
    return len(ROLLING_FONTS) + (len(render._ass(subtitles).encode("utf-8")) if subtitles else 0)


def _rolling_render(media, candidate, output, event, quota, maximum=None):
    # libass gets no writable cache; faststart rewrites the same bounded MP4.
    if maximum is None:
        maximum = quota.reserve(ROLLING_EXPORT_BYTES, _rolling_render_overhead(candidate))
    config = output.parent / "fonts.conf"
    config.write_bytes(ROLLING_FONTS)
    # Scope the font configuration to this render, not concurrent jobs.
    script = (
        "import json,sys,threading; from streamframe.jobs import render_candidate; "
        "render_candidate(sys.argv[1],json.load(sys.stdin),sys.argv[2],None,"
        "threading.Event(),max_output_bytes=int(sys.argv[3]))"
    )
    _rolling_process([script, str(media), str(output), str(maximum)], event, "render",
                     data=json.dumps(candidate, allow_nan=False).encode("utf-8"),
                     env={**os.environ, "FONTCONFIG_FILE": str(config)}, committed=output)
    return maximum


def _remove_owned_directory(parent_fd, name, entry, device):
    # Use descriptor-relative operations on Python 3.10 as well. Symlinks
    # inside the tree are unlinked, never traversed.
    if not stat.S_ISDIR(entry.st_mode) or entry.st_dev != device:
        raise OSError("Unsafe job artifact directory")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        if not os.path.samestat(entry, os.fstat(fd)):
            raise OSError("Job artifact directory changed during deletion")
        for child in os.listdir(fd):
            info = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _remove_owned_directory(fd, child, info, device)
            else:
                os.unlink(child, dir_fd=fd)
        if not os.path.samestat(entry, os.stat(name, dir_fd=parent_fd, follow_symlinks=False)):
            raise OSError("Job artifact directory changed during deletion")
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(fd)


class JobStore:
    def __init__(self, root, import_roots=None, analyzer=None, renderer=None, workers=2):
        if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 16:
            raise ValueError("workers must be between 1 and 16")
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root_identity = self.root.lstat()
        self.owner_lock = (self.root / ".runner.lock").open("a+b")
        try:
            fcntl.flock(self.owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner_lock.close()
            raise Conflict("Another job runner already owns this data directory")
        self.import_roots = [Path(p).expanduser().resolve()
                             for p in ([Path.home()] if import_roots is None else import_roots)]
        self.analyzer = analyzer or analyze_media
        self.renderer = renderer or render_candidate
        self.workers = workers
        self.lock = threading.RLock()
        self.active = {}
        self.corrections = {}
        self.rolling_disks = {}
        self.closed = False
        self.db = self.root / "jobs.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS transcripts (job_id TEXT PRIMARY KEY, words TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS rolling_budgets
                          (job_id TEXT PRIMARY KEY, retained INTEGER NOT NULL,
                           working INTEGER NOT NULL, maximum INTEGER NOT NULL)""")
            os.chmod(self.db, 0o600)
            for row in db.execute("SELECT id, data FROM jobs").fetchall():
                job = json.loads(row["data"])
                if job.get("deletion_pending"):
                    continue
                if "live_session" in job:
                    # Reservations surviving a crash remain charged. Deletion or
                    # restart must never manufacture fresh session allowance.
                    retained = sum(p.stat().st_size for name in (ROLLING_SOURCES, ROLLING_EXPORTS)
                                   for p in (self.root / job["id"] / name).glob("*")
                                   if p.is_file() and not p.is_symlink())
                    db.execute("INSERT OR IGNORE INTO rolling_budgets VALUES (?, ?, 0, ?)",
                               (job["id"], retained, ROLLING_DISK_BYTES))
                    db.execute("""UPDATE rolling_budgets SET retained=max(retained+working, ?),
                                  working=0 WHERE job_id=?""", (retained, job["id"]))
                    self._recover_rolling(job)
                    for candidate in job["candidates"]:
                        if candidate.get("export_status") in {"queued", "running"}:
                            candidate.update(export_status="cancelled", export_error="Worker interrupted")
                    self._save(db, job)
                if job["status"] in {"queued", "running", "cancelling", "exporting"}:
                    if "live_session" in job:
                        session = job["live_session"]
                        session.update(phase="interrupted", acquiring=False)
                        session["warnings"].append("Worker interrupted; unrecorded windows may have been discarded.")
                        for window in session["windows"]:
                            if window["status"] == "processing":
                                window.update(status="discarded", warning="Interrupted before durable completion")
                        error = "Worker stopped; rolling sessions cannot be resumed"
                    else:
                        error = "Worker stopped; resume explicitly"
                    job.update(status="interrupted", stage="interrupted", error=error)
                    self._save(db, job)

    def _recover_rolling(self, job):
        directory = self.root / job["id"]
        exports = directory / ROLLING_EXPORTS
        for name in (ROLLING_WORK, ROLLING_EDITS):
            work = directory / name
            if work.is_dir() and not work.is_symlink():
                shutil.rmtree(work)
        if exports.is_dir() and not exports.is_symlink():
            committed = {record["filename"] for record in job["exports"]}
            for path in exports.iterdir():
                if re.fullmatch(r"[a-f0-9]{32}\.mp4", path.name) and path.name not in committed:
                    path.unlink()
        sources = directory / ROLLING_SOURCES
        if sources.is_dir() and not sources.is_symlink():
            committed = {f"{candidate['id']}.mp4" for candidate in job["candidates"]
                         if candidate.get("editable") is True}
            for path in sources.iterdir():
                if re.fullmatch(r"window-[1-9][0-9]*\.mp4", path.name) and path.name not in committed:
                    path.unlink()

    def _rolling_disk(self, job_id):
        with self.lock:
            if job_id not in self.rolling_disks:
                with self.connect() as db:
                    db.execute("INSERT OR IGNORE INTO rolling_budgets VALUES (?, 0, 0, ?)",
                               (job_id, ROLLING_DISK_BYTES))
                    row = db.execute("SELECT * FROM rolling_budgets WHERE job_id=?", (job_id,)).fetchone()

                def persist(retained, working):
                    with self.connect() as db:
                        db.execute("UPDATE rolling_budgets SET retained=?, working=? WHERE job_id=?",
                                   (retained, working, job_id))

                self.rolling_disks[job_id] = _RollingDisk(
                    self.root / job_id / ROLLING_WORK / "raw", row["retained"], persist, row["maximum"])
            return self.rolling_disks[job_id]

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _save(self, db, job):
        now = time.time()
        old_row = db.execute("SELECT data FROM jobs WHERE id=?", (job["id"],)).fetchone()
        old = json.loads(old_row["data"]) if old_row else {}
        if (old.get("stage"), old.get("status")) != (job.get("stage"), job.get("status")):
            history = job.get("diagnostic_history", [])
            if not isinstance(history, list):
                history = []
            history.append({"at": now, "stage": job.get("stage"), "status": job.get("status"),
                            "previous_stage": old.get("stage")})
            job["diagnostic_history"] = history[-DIAGNOSTIC_HISTORY_LIMIT:]
        job["updated_at"] = now
        db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)", (job["id"], json.dumps(job, allow_nan=False)))

    def _get(self, db, job_id, *, allow_pending=False):
        row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        job = json.loads(row["data"])
        if job.get("deletion_pending") and not allow_pending:
            raise Conflict("Job deletion is pending; retry deletion")
        return job

    def get(self, job_id):
        with self.lock, self.connect() as db:
            return self._get(db, job_id)

    def diagnostics(self, job_id):
        job = self.get(job_id)
        source = job.get("source") if isinstance(job.get("source"), dict) else {}
        resolved = job.get("resolved_source") if isinstance(job.get("resolved_source"), dict) else {}
        source_type = source.get("type") if source.get("type") in {"file", "url", "streamer"} else "unknown"
        operation = job.get("operation") if job.get("operation") in {"analyze", "rolling", "export"} else "unknown"
        status = job.get("status") if job.get("status") in DIAGNOSTIC_STATUSES else "unknown"
        stage = job.get("stage") if job.get("stage") in DIAGNOSTIC_STAGES else "unknown"
        platform = resolved.get("platform") or source.get("platform")
        if not platform and source_type == "url":
            try:
                host = (urlsplit(source.get("url", "")).hostname or "").lower()
                if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
                    platform = "youtube"
                elif host in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}:
                    platform = "twitch"
            except (TypeError, ValueError):
                pass
        platform = platform if platform in {"youtube", "twitch"} else "unknown"
        mode = source.get("mode", "auto") if source_type != "file" else "local"
        mode = mode if mode in {"auto", "live", "latest", "local"} else "unknown"
        if source_type == "url" and "start" in source and "mode" not in source:
            mode = "bounded VOD range"
        selection = resolved.get("selection")
        selection = selection if selection in {"live", "latest", "video"} else "unknown"
        raw_chain = job.get("diagnostic_error_chain")
        chain = [code for code in raw_chain[:5] if isinstance(code, str) and code in DIAGNOSTIC_ERRORS] \
            if isinstance(raw_chain, list) else []
        if not chain and job.get("error"):
            # Old jobs only retained a rendered exception. Only this exact, known
            # message is safe to classify; arbitrary stored text is never shown.
            chain = (["YouTubeVodUnavailable"] if job["error"] == f"ValueError: {YOUTUBE_VOD_ERROR}"
                     else ["UnexpectedError"])
        history = job.get("diagnostic_history")
        history = history[-DIAGNOSTIC_HISTORY_LIMIT:] if isinstance(history, list) else []
        failure_stage = None
        for item in reversed(history):
            if isinstance(item, dict) and item.get("status") == "failed":
                previous = item.get("previous_stage")
                if previous in DIAGNOSTIC_STAGES and previous not in {"failed", "cancelled", "interrupted"}:
                    failure_stage = previous
                break
        if status == "failed" and not failure_stage and chain and chain[0] == "YouTubeVodUnavailable":
            failure_stage = "download"
        label = f"{stage} (failure during {failure_stage})" if status == "failed" and failure_stage else stage
        if status == "failed" and not failure_stage:
            label += " (failure stage unavailable)"
        lines = [
            "StreamFrame diagnostics",
            f"Job ID: {job_id if re.fullmatch(r'[a-f0-9]{32}', job_id) else '[redacted]'}",
            f"Job type: {operation}; source type: {source_type}",
            f"Status: {status}; stage: {label}",
            f"Source platform: {platform}; requested mode: {mode}; resolved selection: {selection}",
        ]
        if source_type == "file":
            lines.append("Source: local file (path omitted)")
        elif source_type == "streamer":
            identifier = source.get("identifier")
            pattern = r"[A-Za-z0-9_]{1,25}" if platform == "twitch" else r"@[A-Za-z0-9_.-]{1,100}"
            lines.append("Source identifier: " + (identifier if isinstance(identifier, str) and
                                                   re.fullmatch(pattern, identifier) else "[redacted]"))
        else:
            lines.append("Source URL: " + _diagnostic_url(source.get("url")))
        if resolved:
            lines.append("Resolved URL: " + _diagnostic_url(resolved.get("url")))
            if isinstance(resolved.get("is_live"), bool):
                lines.append("Resolved live: " + ("yes" if resolved["is_live"] else "no"))
        lines.append("Error: " + (f"{chain[0]} — {DIAGNOSTIC_ERRORS[chain[0]]}" if chain else "none recorded"))
        if chain:
            lines.append("Error chain (categories only): " + " -> ".join(chain))
        lines.append(f"Versions: StreamFrame {_diagnostic_version('streamframe')}; "
                     f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}; "
                     f"yt-dlp {_diagnostic_version('yt-dlp')}; FFmpeg {_ffmpeg_version()}")
        lines.append("History (UTC; most recent transitions):")
        for item in history:
            if not isinstance(item, dict):
                continue
            event_stage = item.get("stage") if item.get("stage") in DIAGNOSTIC_STAGES else "unknown"
            event_status = item.get("status") if item.get("status") in DIAGNOSTIC_STATUSES else "unknown"
            previous = item.get("previous_stage")
            failure = (f" (during {previous})" if event_status == "failed"
                       and previous in DIAGNOSTIC_STAGES and previous != "failed" else "")
            lines.append(f"  {_diagnostic_time(item.get('at'))} — {event_stage} / {event_status}{failure}")
        if not history:
            lines.append("  No persisted transition history (older job).")
        lines.append("Format limits: bounded VOD import requires public, unencrypted, muxed MPEG-TS HLS. "
                     "YouTube additionally requires a finished archive with a complete verified timeline; "
                     "most YouTube VOD formats are unsupported.")
        if chain and chain[0] == "YouTubeVodUnavailable":
            lines.append("Next action: if the archive is still processing, wait for it to finish; "
                         "the finished format may still be unsupported. Otherwise use a supported public "
                         "Twitch VOD range or an allowed local video file.")
        elif status == "failed":
            lines.append("Next action: check the source and supported format, then create a new job or retry if appropriate.")
        elif status == "interrupted":
            lines.append("Next action: resume if available; interrupted rolling sessions require a new job.")
        else:
            lines.append("Next action: no failure action is required.")
        return {"text": "\n".join(lines)}

    def list(self):
        with self.lock, self.connect() as db:
            return sorted((json.loads(row[0]) for row in db.execute("SELECT data FROM jobs")),
                          key=lambda job: job["created_at"], reverse=True)

    def update(self, job_id, **values):
        with self.lock, self.connect() as db:
            job = self._get(db, job_id)
            if values.get("error", ...) is None:
                job.pop("diagnostic_error_chain", None)
            job.update(values)
            self._save(db, job)
            return job

    def create(self, source):
        if not isinstance(source, dict) or source.get("type") not in ("file", "url", "streamer"):
            raise ValueError("source requires type=file, url, or streamer")
        if source["type"] == "file":
            if set(source) - {"type", "path", "start", "end"} or not isinstance(source.get("path"), str):
                raise ValueError("Invalid file source fields")
            bounds = source_range(source)
            path = Path(source["path"]).expanduser().resolve(strict=True)
            if (not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES
                    or not any(path.is_relative_to(root) for root in self.import_roots)
                    or path.is_relative_to(self.root)
                    or (not bounds and path.stat().st_size > MAX_MEDIA_BYTES)):
                raise ValueError("File must be an allowed media file within an import root")
            source = {"type": "file", "path": str(path), **bounds}
        else:
            source = validate_remote_source(source)
        with self.lock:
            if self.closed:
                raise Conflict("Job runner is stopped")
            if len(self.active) + len(self.corrections) >= self.workers:
                raise Conflict("Worker capacity reached; retry when a job finishes")
            job_id = uuid.uuid4().hex
            directory = self.root / job_id
            directory.mkdir(mode=0o700)
            job = dict(id=job_id, source=source, status="queued", stage="queued", progress=0,
                       candidates=[], exports=[], media_url=None, error=None,
                       operation="analyze", export_candidate_id=None,
                       created_at=time.time(), updated_at=time.time())
            if source.get("rolling"):
                job["operation"] = "rolling"
                job["live_session"] = dict(phase="queued", acquiring=False, windows_seen=0,
                                           windows_processed=0, global_time=0, warnings=[], windows=[],
                                           face_crop=None)
            with self.connect() as db:
                self._save(db, job)
            self._start(job_id)
            return self.get(job_id)

    def _start(self, job_id, candidate=None):
        event = threading.Event()
        thread = threading.Thread(target=self._run, args=(job_id, event, candidate), daemon=True)
        self.active[job_id] = (event, thread)
        thread.start()

    def _progress(self, job_id, event, value, message=""):
        if event.is_set():
            raise Cancelled()
        if not finite_number(value) or not 0 <= value <= 1:
            raise ValueError("Pipeline emitted invalid progress")
        self.update(job_id, progress=value, message=str(message)[:1000])

    def _media(self, job_id):
        directory = self.root / job_id
        paths = [p for p in directory.glob("source.*") if p.suffix.lower() in MEDIA_SUFFIXES]
        return paths[0] if len(paths) == 1 else None

    def _import(self, job, event):
        directory = self.root / job["id"]
        if job["source"]["type"] == "file":
            original = Path(job["source"]["path"])
            # Recheck at use-time, including symlinks, and never read special files.
            resolved = original.resolve(strict=True)
            if (resolved != original or resolved.is_relative_to(self.root)
                    or not any(resolved.is_relative_to(root) for root in self.import_roots)):
                raise ValueError("Import path escaped import roots")
            fd = open_regular_file(resolved)
            try:
                if "start" in job["source"]:
                    self.update(job["id"], stage="trim", message="Trimming source range")
                    return trim_media(fd, directory, job["source"]["start"], job["source"]["end"], event)
                if os.fstat(fd).st_size > MAX_MEDIA_BYTES:
                    raise ValueError("Media exceeds size limit")
                target = directory / ("source" + original.suffix.lower())
                src = os.fdopen(fd, "rb")
                fd = None
                with src, target.open("wb") as dst:
                    total = 0
                    while True:
                        if event.is_set():
                            raise Cancelled()
                        data = src.read(1024 * 1024)
                        if not data:
                            break
                        total += len(data)
                        if total > MAX_MEDIA_BYTES:
                            raise ValueError("Media exceeds size limit")
                        dst.write(data)
            finally:
                if fd is not None:
                    os.close(fd)
            return target
        source = job["source"]
        if source["type"] == "url" and "start" in source and "mode" not in source:
            self.update(job["id"], stage="download", message="Fetching the selected video range")
            platform, _ = video_page(source["url"])
            return import_remote(source, directory, event, on_resolved=lambda title, end: self.update(
                job["id"], resolved_source={"url": source["url"], "platform": platform, "title": title,
                                            "is_live": False, "selection": "video"},
                imported_range={"start": source["start"], "end": end},
            ))
        previous = job.get("resolved_source")
        if previous and not previous["is_live"] and "imported_range" in job:
            self.update(job["id"], stage="download", message="Retrying the selected VOD range")
            return import_remote({"url": previous["url"], **job["imported_range"]}, directory, event)
        from .sources import resolve_source

        self.update(job["id"], stage="resolve", message="Resolving public stream or latest VOD")
        resolved = resolve_source(source.get("identifier") or source["url"], mode=source.get("mode", "auto"),
                                  platform=source.get("platform"), cancel_event=event)
        self.update(job["id"], capture_started_at=None, resolved_source={
            "url": resolved.url, "platform": resolved.platform, "title": resolved.title,
            "is_live": resolved.is_live, "selection": resolved.selection,
            "source_id": getattr(resolved, "source_id", None),
        })
        if resolved.is_live:
            from .live import capture_live

            if "start" in source:
                raise ValueError("Live capture uses duration, not original video start/end")
            if source.get("mode") == "latest":
                raise ValueError("Latest mode requires a finished VOD")
            self.update(job["id"], stage="capture", message="Capturing bounded live media")
            result = capture_live(
                resolved, directory, duration=source.get("duration", 60), cancelled=event,
                progress=lambda value, message="": self._progress(job["id"], event, value, message),
                on_alignment=lambda timestamp: self.update(job["id"], capture_started_at=timestamp),
            )
            target = directory / "source.mp4"
            if (Path(result) != target or target.is_symlink() or not target.is_file()
                    or not 0 < target.stat().st_size <= MAX_MEDIA_BYTES):
                raise ValueError("Live capture did not produce a usable preview MP4")
            return target
        if source.get("mode") == "live":
            raise ValueError("No accessible live stream was found")
        if resolved.selection == "video" and "start" not in source:
            raise ValueError("Finished video URLs require explicit start and end")
        self.update(job["id"], stage="download", message="Fetching a bounded finished VOD range")
        def persist_range(_title, effective_end):
            self.update(job["id"], imported_range={"start": source.get("start", 0), "end": effective_end})

        if "start" in source:
            return import_remote({"url": resolved.url, "start": source["start"], "end": source["end"]},
                                 directory, event, on_resolved=persist_range)
        return import_remote({"url": resolved.url, "start": 0, "end": source.get("duration", 60)},
                             directory, event, allow_shorter=True, on_resolved=persist_range)

    def _live_update(self, job_id, *, session=None, window=None, export=None, candidate=None,
                     source_words=None, **values):
        with self.lock, self.connect() as db:
            db.execute("PRAGMA synchronous=FULL")
            job = self._get(db, job_id)
            live = job["live_session"]
            live.update(session or {})
            if window is not None:
                index = window["index"] - 1
                if index == len(live["windows"]):
                    live["windows"].append(window)
                else:
                    live["windows"][index] = window
                live["windows_seen"] = len(live["windows"])
                live["global_time"] = max(live["global_time"], window["end"])
                live["windows_processed"] = sum(w["status"] in {"exported", "no_face", "no_candidate",
                                                                "duplicate"}
                                                for w in live["windows"])
                if window.get("warning") and window["warning"] not in live["warnings"]:
                    live["warnings"].append(window["warning"])
            if candidate is not None:
                job["candidates"].append(candidate)
                if source_words is not None:
                    db.execute("INSERT OR REPLACE INTO transcripts VALUES (?, ?)",
                               (f"{job_id}:{candidate['id']}", json.dumps(source_words, allow_nan=False)))
            if export is not None:
                job["exports"].append(export)
            job.update(values)
            self._save(db, job)
            return live

    def set_live_crop(self, job_id, patch):
        """Set a normalized face crop (or None for auto) for windows not yet processing."""
        if not isinstance(patch, dict) or set(patch) != {"face_crop"}:
            raise ValueError("Expected exactly one face_crop field")
        crop = validate_candidate({"start": 0, "end": 1, **patch})["face_crop"]
        with self.lock:
            job = self.get(job_id)
            if "live_session" not in job or job["status"] not in {"queued", "running"}:
                raise Conflict("Manual live crop requires an active rolling session")
            self._live_update(job_id, session={"face_crop": crop})
            return self.get(job_id)

    def _consume_window(self, job_id, event, raw, window, work, exports, quota, live_state, recent_speech):
        index = window["index"]
        workspace = work / f"window-{index}"
        workspace.mkdir(mode=0o700)
        media = workspace / "encoded.mp4"
        published_bytes = 0
        warning = None

        def retain_source(candidate):
            nonlocal published_bytes
            sources = self.root / job_id / ROLLING_SOURCES
            sources.mkdir(mode=0o700, exist_ok=True)
            _sync_path(sources.parent)
            _sync_path(media)
            os.replace(media, sources / f"{candidate['id']}.mp4")
            published_bytes += size
            _sync_path(sources)
            candidate.update(editable=True, window_index=index, window_duration=actual, revision=0,
                             export_status="succeeded" if window["status"] == "exported" else "idle",
                             export_error=None,
                             source_media_url=f"/api/jobs/{job_id}/candidates/{candidate['id']}/media",
                             media_url=f"/api/jobs/{job_id}/candidates/{candidate['id']}/media")

        try:
            fingerprint = window["raw_sha256"] = _raw_fingerprint(raw, event)
            replay = next((w["index"] for w in live_state["windows"][:-1]
                           if w.get("raw_sha256") == fingerprint), None)
            if replay is not None:
                window.update(status="duplicate", duplicate_of=replay,
                              warning=f"Window {index}: exact MPEG-TS replay of window {replay}.")
                self._live_update(job_id, window=window)
                return
            duration = window["end"] - window["start"]
            maximum = quota.reserve(ROLLING_ENCODE_BYTES)
            actual = _rolling_encode(raw, media, duration, event, maximum)
            size = media.stat().st_size
            if (not finite_number(actual) or not 0 < actual <= duration + 1e-6
                    or media.is_symlink() or not media.is_file() or not 0 < size <= maximum):
                raise ValueError("Invalid encoded live window")
            raw.unlink()
            quota.reduce(maximum - size)
            window["media_end"] = window["start"] + actual
            if duration - actual > 1e-6:
                warning = (f"Window {index}: encoder validated only {actual:.3f}s of {duration:.3f}s; "
                           "the tail is not included in analysis or export.")
                window["warning"] = warning
            if event.is_set():
                raise Cancelled()
            progress = lambda value, message="": None  # Worker-level progress is the live timeline.
            if self.analyzer is analyze_media:
                result = _rolling_analyze(media, event)
            else:
                result = self.analyzer(media, progress, event)
            if event.is_set():
                raise Cancelled()
            source_words = None
            if isinstance(result, dict):
                source_words = result.get("source_words")
                result = result["candidates"]
                if source_words is not None:
                    source_words = validate_candidate(
                        {"start": 0, "end": 1, "words": source_words})["words"]
            if not isinstance(result, list) or len(result) > 1000:
                raise ValueError("Analysis must return at most 1000 candidates")
            candidates = [validate_candidate(c) for c in result]
            for candidate in candidates:
                # Ranking rounds timestamps to milliseconds; never render past the probe.
                if candidate["start"] >= actual or candidate["end"] > actual + 0.001:
                    raise ValueError("Live candidate extends past validated window media")
                candidate["end"] = min(candidate["end"], actual)
            if not candidates:
                window.update(status="no_candidate", warning=f"{warning + ' ' if warning else ''}"
                              f"Window {index}: analysis found no usable candidate.")
                self._live_update(job_id, window=window)
                return
            candidate = max(candidates, key=lambda c: c["score"])
            candidate["id"] = f"window-{index}"
            if len(json.dumps(candidate, allow_nan=False).encode("utf-8")) > ROLLING_CANDIDATE_BYTES:
                raise ValueError("Live candidate metadata exceeds its size limit")
            window["candidate_id"] = candidate["id"]
            if live_state["face_crop"] is not None:
                candidate["face_crop"] = live_state["face_crop"]
                candidate["evidence"].append("Manual live-session face crop used.")
            if candidate["face_crop"] is None:
                window.update(status="no_face", warning=f"{warning + ' ' if warning else ''}"
                              f"Window {index}: no face crop; edit this clip or set a crop for future windows.")
                retain_source(candidate)
                self._live_update(job_id, window=window, candidate=candidate, source_words=source_words)
                return
            speech, grams = _speech_fingerprint(candidate)
            if speech is not None:
                window["transcript_sha256"] = speech
                # Only compare nontrivial speech in nearby exported windows.
                # Silent/short speech clips get no fuzzy match.
                for previous, other in recent_speech:
                    if index - previous <= 2 and 20 * len(grams & other) >= 9 * (len(grams) + len(other)):
                        window.update(status="duplicate", duplicate_of=previous,
                                      warning=f"{warning + ' ' if warning else ''}Window {index}: "
                                      f"near-duplicate transcript of exported window {previous}.")
                        self._live_update(job_id, window=window, candidate=candidate)
                        return
            if event.is_set():
                raise Cancelled()
            filename = uuid.uuid4().hex + ".mp4"
            staged = workspace / filename
            if self.renderer is render_candidate:
                maximum = _rolling_render(media, candidate, staged, event, quota)
            else:
                # Injected adapters are trusted test hooks, not production quota enforcement.
                maximum = quota.reserve(ROLLING_EXPORT_BYTES)
                self.renderer(media, candidate, staged, progress, event)
            if staged.is_symlink() or not staged.is_file() or not 0 < staged.stat().st_size <= maximum:
                raise ValueError("Renderer did not produce a bounded live export")
            export_bytes = staged.stat().st_size
            record = dict(candidate_id=candidate["id"], filename=filename,
                          url=f"/api/jobs/{job_id}/exports/{filename}", window_index=index,
                          window={k: window[k] for k in ("first_sequence", "last_sequence", "start",
                                                         "end", "started_at", "media_end", "sequence_key",
                                                         "raw_sha256")},
                          global_start=window["start"] + candidate["start"],
                          global_end=window["start"] + candidate["end"], revision=0)
            with self.lock:
                # Once validated, stop cannot revoke this export. The file is synced
                # first; startup deletes any orphan if the process dies before commit.
                target = exports / filename
                _sync_path(staged)
                os.replace(staged, target)
                published_bytes += export_bytes
                _sync_path(exports)
                window.update(status="exported", filename=filename)
                retain_source(candidate)
                self._live_update(job_id, window=window, export=record, candidate=candidate,
                                  source_words=source_words)
            if grams:
                recent_speech[:] = (recent_speech + [(index, grams)])[-2:]
        finally:
            raw.unlink(missing_ok=True)
            shutil.rmtree(workspace)
            quota.finish_window(published_bytes)

    def _run_rolling(self, job, event):
        from . import live
        from .sources import resolve_source

        job_id, source = job["id"], job["source"]
        directory = self.root / job_id
        work, exports = directory / ROLLING_WORK, directory / ROLLING_EXPORTS
        self._live_update(job_id, session={"phase": "resolving"}, stage="resolve")
        try:
            resolved = resolve_source(source.get("identifier") or source["url"], mode="live",
                                      platform=source.get("platform"), cancel_event=event)
            if not resolved.is_live:
                raise ValueError("No accessible live stream was found; rolling capture cannot use a VOD")
            if event.is_set():
                raise Cancelled()
            work.mkdir(mode=0o700)
            exports.mkdir(mode=0o700)
            _sync_path(directory)
            raw_dir = work / "raw"
            raw_dir.mkdir(mode=0o700)
            quota = self._rolling_disk(job_id)
            pending = queue.Queue(maxsize=ROLLING_QUEUE_SIZE)
            producer_done = threading.Event()
            session_seconds, window_seconds = source["session_duration"], source["window_duration"]
            state = {"last": None, "index": 0, "progress": (0, "Waiting for live windows")}

            def progress(value, message=""):
                if not finite_number(value) or not 0 <= value <= 1:
                    raise ValueError("Live producer emitted invalid progress")
                state["progress"] = (value, str(message)[:1000])

            def on_window(path, metadata):
                path = Path(path)
                if event.is_set():
                    raise Cancelled()
                if (path.parent != raw_dir or path.suffix != ".ts" or path.is_symlink()
                        or not path.is_file() or path.stat().st_size <= 0):
                    raise ValueError("Producer returned an invalid live window file")
                window = _rolling_metadata(metadata, state["last"], session_seconds, window_seconds)
                index = state["index"] + 1
                if index > ROLLING_MAX_WINDOWS:
                    raise ValueError(f"Live session exceeds the {ROLLING_MAX_WINDOWS}-window limit")
                window.update(index=index, status="processing")
                try:
                    pending.put_nowait((path, window))
                except queue.Full:
                    state["rejected"] = {**window, "status": "discarded",
                                         "warning": f"Window {index}: discarded because the live queue is full."}
                    raise live.LiveBackpressureError("Live backpressure: window queue is full") from None
                state.update(last={"last_sequence": window["last_sequence"], "end": window["end"]},
                             index=index)

            def produce():
                try:
                    live.capture_windows(resolved, raw_dir, session_seconds=session_seconds,
                                         window_seconds=window_seconds, cancelled=event,
                                         progress=progress, on_window=on_window,
                                         disk_remaining=quota.disk_remaining, disk_lock=quota.lock)
                except Exception as exc:
                    if not event.is_set() or not isinstance(exc, Cancelled):
                        state["error"] = exc
                finally:
                    producer_done.set()
                    try:
                        fraction, message = state["progress"]
                        self._live_update(job_id, session={"phase": "draining", "acquiring": False},
                                          progress=fraction, message=message)
                    except Exception as exc:
                        state.setdefault("error", exc)

            self._live_update(job_id, session={"phase": "capturing", "acquiring": True}, stage="capture",
                              resolved_source={"url": resolved.url, "platform": resolved.platform,
                                               "title": resolved.title, "is_live": True,
                                               "selection": resolved.selection,
                                               "source_id": getattr(resolved, "source_id", None)})
            producer = threading.Thread(target=produce, name=f"live-{job_id}", daemon=True)
            producer.start()
            try:
                previous_status = None
                recent_speech = []
                while not event.is_set():
                    with self.lock:
                        capturing = not producer_done.is_set()
                        fraction, message = state["progress"]
                        status = (capturing, fraction, message)
                        if status != previous_status:
                            self._live_update(job_id, session={"phase": "capturing" if capturing else "draining",
                                                               "acquiring": capturing},
                                              progress=fraction, message=message)
                            previous_status = status
                    try:
                        raw, window = pending.get(timeout=0.25 if capturing else 0)
                    except queue.Empty:
                        if not capturing:
                            break
                        continue
                    live_state = self._live_update(
                        job_id, window=window, stage="rolling",
                        **({"capture_started_at": window["started_at"]} if window["index"] == 1 else {}),
                    )
                    try:
                        if event.is_set():
                            raise Cancelled()
                        self._consume_window(job_id, event, raw, window, work, exports, quota.allocation(),
                                             live_state, recent_speech)
                    except Exception as exc:
                        stopped = event.is_set()
                        event.set()
                        window.update(status="discarded" if stopped else "failed",
                                      warning=f"Window {window['index']}: " +
                                      ("stopped before completion." if stopped else f"{exc}"[:1000]))
                        window.pop("filename", None)
                        self._live_update(job_id, window=window)
                        if stopped:
                            raise Cancelled() from exc
                        raise
            finally:
                if not producer_done.is_set():
                    event.set()
                producer.join()
                while not pending.empty():
                    _, window = pending.get_nowait()
                    window.update(status="discarded",
                                  warning=f"Window {window['index']}: not processed before the session stopped.")
                    self._live_update(job_id, window=window)
                if "rejected" in state:
                    self._live_update(job_id, window=state["rejected"])
                # Correction exports have separate workspaces and may still run.
                shutil.rmtree(work)
            if "error" in state:
                raise state["error"]
            if event.is_set():
                raise Cancelled()
            if not state["index"]:
                raise ValueError("Rolling capture ended without a complete window")
            with self.lock:
                if event.is_set():
                    raise Cancelled()
                self._live_update(job_id, session={"phase": "completed", "acquiring": False},
                                  status="succeeded", stage="completed", progress=1)
        except Cancelled:
            with self.lock:
                warnings = self.get(job_id)["live_session"]["warnings"]
                warnings.append("Stopped; any incomplete acquisition was discarded.")
                self._live_update(job_id, session={"phase": "cancelled", "acquiring": False,
                                                   "warnings": warnings},
                                  status="cancelled", stage="cancelled")
        except Exception as exc:
            self._live_update(job_id, session={"phase": "failed", "acquiring": False},
                              status="failed", stage="failed", error=f"{type(exc).__name__}: {exc}"[:2000],
                              diagnostic_error_chain=_diagnostic_error(exc))

    def _run(self, job_id, event, candidate):
        progress = lambda value, message="": self._progress(job_id, event, value, message)
        try:
            self.update(job_id, status="exporting" if candidate else "running",
                        stage="export" if candidate else "import", error=None, progress=0)
            job = self.get(job_id)
            if job["source"].get("rolling"):
                self._run_rolling(job, event)
                return
            media = self._media(job_id) if job.get("media_url") else None
            if candidate is not None:
                if media is None:
                    raise ValueError("Preview media is unavailable")
                filename = uuid.uuid4().hex + ".mp4"
                output = self.root / job_id / filename
                self.renderer(media, candidate, output, progress, event)
                if event.is_set():
                    raise Cancelled()
                if output.is_symlink() or not output.is_file() or output.stat().st_size == 0:
                    raise ValueError("Renderer did not produce an export")
                exports = job["exports"] + [dict(candidate_id=candidate["id"], filename=filename,
                                                url=f"/api/jobs/{job_id}/exports/{filename}")]
                self.update(job_id, exports=exports)
            else:
                if media is None:
                    for path in (self.root / job_id).glob("source.*"):
                        path.unlink()
                    media = self._import(job, event)
                if event.is_set():
                    raise Cancelled()
                self.update(job_id, media_url=f"/api/jobs/{job_id}/media", stage="analysis", progress=0)
                result = self.analyzer(media, progress, event)
                source_words = None
                if isinstance(result, dict):
                    source_words = result.get("source_words")
                    result = result["candidates"]
                if not isinstance(result, list) or len(result) > 1000:
                    raise ValueError("Analysis must return at most 1000 candidates")
                candidates = [validate_candidate(c) for c in result]
                if len({c["id"] for c in candidates}) != len(candidates):
                    raise ValueError("Analysis emitted duplicate candidate ids")
                if event.is_set():
                    raise Cancelled()
                with self.lock, self.connect() as db:
                    if source_words is not None:
                        source_words = validate_candidate(
                            {"start": 0, "end": 1, "words": source_words}
                        )["words"]
                        db.execute("INSERT OR REPLACE INTO transcripts VALUES (?, ?)",
                                   (job_id, json.dumps(source_words, allow_nan=False)))
                    else:
                        db.execute("DELETE FROM transcripts WHERE job_id=?", (job_id,))
                    current = self._get(db, job_id)
                    current["candidates"] = candidates
                    self._save(db, current)
            with self.lock:
                if event.is_set():
                    raise Cancelled()
                self.update(job_id, status="succeeded", stage="completed", progress=1)
        except Cancelled:
            self.update(job_id, status="cancelled", stage="cancelled")
        except Exception as exc:
            self.update(job_id, status="cancelled" if event.is_set() else "failed",
                        stage="cancelled" if event.is_set() else "failed",
                        error=f"{type(exc).__name__}: {exc}"[:2000],
                        diagnostic_error_chain=_diagnostic_error(exc))
        finally:
            with self.lock:
                self.active.pop(job_id, None)
                if self.closed and not self.active and not self.corrections:
                    self._release_owner()

    def cancel(self, job_id):
        with self.lock:
            job = self.get(job_id)
            for (owner, _), (event, _) in self.corrections.items():
                if owner == job_id:
                    event.set()
            if job_id in self.active and job["status"] not in TERMINAL:
                self.active[job_id][0].set()
                return self.update(job_id, status="cancelling")
            return job

    def delete(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise KeyError(job_id)
        # Serialize with worker registration, retirement, and owner release.
        with self.lock:
            if self.closed:
                raise Conflict("Job runner is stopped")
            try:
                with self.connect() as db:
                    db.execute("PRAGMA synchronous=FULL")
                    job = self._get(db, job_id, allow_pending=True)
                    if (job_id in self.active or any(owner == job_id for owner, _ in self.corrections)
                            or job["status"] in {"queued", "running", "cancelling", "exporting"}):
                        raise Conflict("Stop the job and wait for any active exports to finish before deleting")
                    if not job.get("deletion_pending"):
                        # Commit intent before touching disk. If cleanup or the final
                        # commit fails, resume/export stay blocked and DELETE can retry.
                        job["deletion_pending"] = True
                        self._save(db, job)
                self._remove_job_artifacts(job_id)
                with self.connect() as db:
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("DELETE FROM transcripts WHERE job_id=? OR job_id GLOB ?",
                               (job_id, f"{job_id}:*"))
                    db.execute("DELETE FROM rolling_budgets WHERE job_id=?", (job_id,))
                    db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                self.rolling_disks.pop(job_id, None)
            except (OSError, sqlite3.Error) as exc:
                raise DeletionFailed("Could not complete job deletion; retry DELETE") from exc

    def _remove_job_artifacts(self, job_id):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if not os.path.samestat(self.root_identity, os.fstat(fd)):
                raise OSError("Job storage directory changed")
            try:
                entry = os.stat(job_id, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass  # The job can have no artifacts, or a previous attempt removed them.
            else:
                _remove_owned_directory(fd, job_id, entry, self.root_identity.st_dev)
            os.fsync(fd)
        finally:
            os.close(fd)

    def resume(self, job_id):
        with self.lock:
            job = self.get(job_id)
            if "live_session" in job:
                raise Conflict("Rolling live sessions cannot be resumed or restart their budget")
            if (self.closed or job_id in self.active
                    or len(self.active) + len(self.corrections) >= self.workers
                    or job["status"] not in {"cancelled", "failed", "interrupted"}):
                raise Conflict("Job cannot be resumed now")
            candidate = None
            if job.get("operation") == "export":
                candidate = next((c for c in job["candidates"]
                                  if c["id"] == job.get("export_candidate_id")), None)
                if candidate is None:
                    raise Conflict("Export candidate no longer exists")
            self.update(job_id, status="queued", stage="queued", error=None, progress=0)
            self._start(job_id, candidate)
            return self.get(job_id)

    def edit(self, job_id, candidate_id, patch):
        with self.lock:
            job = self.get(job_id)
            if "live_session" in job:
                if (job_id, candidate_id) in self.corrections:
                    raise Conflict("Wait for this candidate's active export before editing")
                if not isinstance(patch, dict) or not patch or set(patch) - (CANDIDATE_FIELDS - {"id"}):
                    raise ValueError("PATCH requires editable candidate fields other than id")
                for candidate in job["candidates"]:
                    if candidate["id"] == candidate_id:
                        self.candidate_media_path(job_id, candidate_id)
                        edited = validate_candidate({
                            **{k: v for k, v in candidate.items() if k in CANDIDATE_FIELDS}, **patch})
                        if edited["end"] > candidate["window_duration"]:
                            raise ValueError("Candidate extends past retained window media")
                        if edited["start"] < candidate["start"] or edited["end"] > candidate["end"]:
                            with self.connect() as db:
                                row = db.execute("SELECT words FROM transcripts WHERE job_id=?",
                                                 (f"{job_id}:{candidate_id}",)).fetchone()
                            if row is not None:
                                seen = {(word["start"], word["end"]) for word in edited["words"]}
                                for word in json.loads(row["words"]):
                                    overlaps_new = word["start"] < edited["end"] and word["end"] > edited["start"]
                                    overlaps_old = word["start"] < candidate["end"] and word["end"] > candidate["start"]
                                    identity = (word["start"], word["end"])
                                    if overlaps_new and not overlaps_old and identity not in seen:
                                        edited["words"].append(word)
                                        seen.add(identity)
                                edited["words"].sort(key=lambda word: (word["start"], word["end"]))
                        if len(json.dumps(edited).encode()) > ROLLING_CANDIDATE_BYTES:
                            raise ValueError("Live candidate metadata exceeds its size limit")
                        candidate.update(edited, revision=candidate["revision"] + 1,
                                         export_status="idle", export_error=None)
                        return self.update(job_id, candidates=job["candidates"])
                raise KeyError(candidate_id)
            if job_id in self.active:
                raise Conflict("Wait for the active operation before editing")
            if not isinstance(patch, dict) or not patch or "id" in patch:
                raise ValueError("PATCH requires candidate fields other than id")
            for index, candidate in enumerate(job["candidates"]):
                if candidate["id"] == candidate_id:
                    edited = validate_candidate({**candidate, **patch})
                    if edited["start"] < candidate["start"] or edited["end"] > candidate["end"]:
                        with self.connect() as db:
                            row = db.execute("SELECT words FROM transcripts WHERE job_id=?",
                                             (job_id,)).fetchone()
                        if row is not None:
                            # Timestamp identity preserves corrected text while avoiding duplicates.
                            seen = {(word["start"], word["end"]) for word in edited["words"]}
                            for word in json.loads(row["words"]):
                                overlaps_new = word["start"] < edited["end"] and word["end"] > edited["start"]
                                overlaps_old = word["start"] < candidate["end"] and word["end"] > candidate["start"]
                                identity = (word["start"], word["end"])
                                if overlaps_new and not overlaps_old and identity not in seen:
                                    edited["words"].append(word)
                                    seen.add(identity)
                            edited["words"].sort(key=lambda word: (word["start"], word["end"]))
                            edited = validate_candidate(edited)
                    job["candidates"][index] = edited
                    return self.update(job_id, candidates=job["candidates"])
            raise KeyError(candidate_id)

    def export(self, job_id, candidate_id):
        with self.lock:
            job = self.get(job_id)
            if "live_session" in job:
                return self._export_rolling(job, candidate_id)
            if (self.closed or job_id in self.active
                    or len(self.active) + len(self.corrections) >= self.workers):
                raise Conflict("Worker capacity reached or job is busy")
            candidate = next((c for c in job["candidates"] if c["id"] == candidate_id), None)
            if candidate is None:
                raise KeyError(candidate_id)
            self.update(job_id, status="queued", stage="export", error=None, progress=0,
                        operation="export", export_candidate_id=candidate_id)
            self._start(job_id, candidate)
            return self.get(job_id)

    def candidate_media_path(self, job_id, candidate_id):
        """Return only the retained encoded window belonging to this candidate."""
        job = self.get(job_id)
        candidate = next((c for c in job["candidates"] if c["id"] == candidate_id), None)
        if (candidate is None or candidate.get("editable") is not True
                or not re.fullmatch(r"window-[1-9][0-9]*", candidate_id)):
            raise KeyError(candidate_id)
        path = self.root / job_id / ROLLING_SOURCES / f"{candidate_id}.mp4"
        if (path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(self.root / job_id)):
            raise KeyError(candidate_id)
        return path

    def _export_rolling(self, job, candidate_id):
        job_id = job["id"]
        key = (job_id, candidate_id)
        if (self.closed or key in self.corrections or job["status"] == "cancelling"
                or len(self.active) + len(self.corrections) >= self.workers):
            raise Conflict("Worker capacity reached or candidate export is busy")
        media = self.candidate_media_path(job_id, candidate_id)
        candidate = next(c for c in job["candidates"] if c["id"] == candidate_id)
        if candidate["face_crop"] is None:
            raise ValueError("A face crop is required before exporting")
        quota = self._rolling_disk(job_id).allocation()
        # Reserve synchronously so a full session cannot enqueue unbounded retries.
        overhead = _rolling_render_overhead(candidate) if self.renderer is render_candidate else 0
        headroom = 0
        if job_id in self.active and job["status"] not in TERMINAL:
            from . import live

            # Leave one maximum segment and a complete encode/export step free.
            # This allowance belongs to capture, not to the correction's lease.
            headroom = live.MAX_SEGMENT_BYTES + ROLLING_ENCODE_BYTES + ROLLING_EXPORT_BYTES
        try:
            maximum = quota.reserve(ROLLING_EXPORT_BYTES, overhead, headroom)
        except ValueError as exc:
            if headroom:
                raise Conflict("Insufficient capture headroom in the session disk budget; "
                               "wait until capture stops before re-exporting") from exc
            raise
        candidate.update(export_status="queued", export_error=None)
        try:
            self.update(job_id, candidates=job["candidates"])
            event = threading.Event()
            thread = threading.Thread(target=self._run_rolling_export,
                                      args=(job_id, copy.deepcopy(candidate), media, quota, maximum, event),
                                      daemon=True)
            self.corrections[key] = (event, thread)
            thread.start()
        except Exception:
            self.corrections.pop(key, None)
            quota.finish_window(0)
            raise
        return self.get(job_id)

    def _run_rolling_export(self, job_id, candidate, media, quota, maximum, event):
        key = (job_id, candidate["id"])
        workspace = self.root / job_id / ROLLING_EDITS / uuid.uuid4().hex
        published_bytes = 0

        def state(status, error=None, record=None):
            with self.lock, self.connect() as db:
                current = self._get(db, job_id)
                target = next(c for c in current["candidates"] if c["id"] == candidate["id"])
                target.update(export_status=status, export_error=error)
                if record is not None:
                    current["exports"].append(record)
                self._save(db, current)

        try:
            state("running")
            workspace.mkdir(mode=0o700, parents=True)
            filename = uuid.uuid4().hex + ".mp4"
            staged = workspace / filename
            payload = {k: v for k, v in candidate.items() if k in CANDIDATE_FIELDS}
            if event.is_set():
                raise Cancelled()
            if self.renderer is render_candidate:
                _rolling_render(media, payload, staged, event, quota, maximum=maximum)
            else:
                self.renderer(media, payload, staged, lambda *_: None, event)
            if (staged.is_symlink() or not staged.is_file()
                    or not 0 < staged.stat().st_size <= maximum):
                raise ValueError("Renderer did not produce a bounded live export")
            with self.lock:
                if event.is_set():
                    raise Cancelled()
                job = self.get(job_id)
                window = next(w for w in job["live_session"]["windows"]
                              if w["index"] == candidate["window_index"])
                record = dict(candidate_id=candidate["id"], filename=filename,
                              url=f"/api/jobs/{job_id}/exports/{filename}",
                              window_index=candidate["window_index"], revision=candidate["revision"],
                              global_start=window["start"] + candidate["start"],
                              global_end=window["start"] + candidate["end"],
                              window={k: v for k, v in window.items()
                                      if k in {"first_sequence", "last_sequence", "start", "end",
                                               "started_at", "media_end", "sequence_key", "raw_sha256"}})
                _sync_path(staged)
                size = staged.stat().st_size
                exports = self.root / job_id / ROLLING_EXPORTS
                os.replace(staged, exports / filename)
                published_bytes = size
                _sync_path(exports)
                state("succeeded", record=record)
        except Cancelled:
            state("cancelled", "Export cancelled; previous exports are unchanged")
        except Exception as exc:
            state("failed", f"{type(exc).__name__}: {exc}"[:2000])
        finally:
            if workspace.is_dir():
                shutil.rmtree(workspace)
            quota.finish_window(published_bytes)
            with self.lock:
                self.corrections.pop(key, None)
                if self.closed and not self.active and not self.corrections:
                    self._release_owner()

    def media_path(self, job_id, filename=None):
        job = self.get(job_id)
        if filename is None:
            path = self._media(job_id) if job.get("media_url") else None
        else:
            if not re.fullmatch(r"[a-f0-9]{32}\.mp4", filename):
                raise KeyError(filename)
            directory = self.root / job_id
            if "live_session" in job:
                directory /= ROLLING_EXPORTS
            path = directory / filename if any(e["filename"] == filename for e in job["exports"]) else None
        if (path is None or not path.is_file() or path.is_symlink()
                or not path.resolve().is_relative_to(self.root / job_id)):
            raise KeyError(filename or "media")
        return path

    def close(self):
        with self.lock:
            self.closed = True
            active = list(self.active.values()) + list(self.corrections.values())
            for event, _ in active:
                event.set()
        for _, thread in active:
            thread.join(timeout=3)
        with self.lock:
            if not self.active and not self.corrections:
                self._release_owner()

    def _release_owner(self):
        if not self.owner_lock.closed:
            fcntl.flock(self.owner_lock.fileno(), fcntl.LOCK_UN)
            self.owner_lock.close()
