"""Resolve public Twitch/YouTube sources without downloading media.

Bare identifiers are Twitch usernames. YouTube accepts channel URLs, or an
@handle with an explicit platform; display names are deliberately unsupported.
Channel ``latest`` means an archived broadcast (YouTube's Streams tab), not an
upcoming broadcast or an archive that is still growing. Resolution examines at
most 20 recent archives and fails closed when completion cannot be established.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

MAX_SECONDS = 30
MAX_METADATA_BYTES = 10 * 1024**2
MAX_CANDIDATES = 20
MAX_MANIFEST_BYTES = 1024**2


class SourceResolutionError(ValueError):
    """A public, playable source could not be established."""


class SourceResolutionCancelled(SourceResolutionError):
    """Resolution was cancelled."""


class _MetadataLimit(SourceResolutionError):
    pass


class _MetadataFailure(SourceResolutionError):
    pass


@dataclass(frozen=True)
class ResolvedSource:
    url: str
    platform: str
    title: str
    is_live: bool
    source_id: str
    requested_mode: str
    selection: str


@dataclass(frozen=True)
class _Target:
    url: str
    platform: str
    channel: bool


def _target(source, platform=None):
    if platform not in {None, "twitch", "youtube"}:
        raise SourceResolutionError("Platform must be twitch or youtube")
    if (not isinstance(source, str) or not source or len(source) > 4096
            or any(ord(char) <= 32 for char in source) or "\\" in source):
        raise SourceResolutionError("Enter a Twitch username or public stream/video URL")
    if "://" not in source:
        if platform == "youtube":
            if not re.fullmatch(r"@[\w.-]{3,30}", source, flags=re.ASCII):
                raise SourceResolutionError("YouTube requires an @handle or channel URL, not a display name")
            return _Target("https://www.youtube.com/" + source, "youtube", True)
        if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", source):
            raise SourceResolutionError("Enter a Twitch username; YouTube @handles require platform=youtube")
        return _Target("https://www.twitch.tv/" + source.lower(), "twitch", True)
    try:
        parsed = urlsplit(source)
        if (parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or parsed.port is not None or parsed.fragment):
            raise ValueError()
    except ValueError as exc:
        raise SourceResolutionError("Only public HTTPS Twitch and YouTube URLs are supported") from exc
    host = parsed.hostname
    if host in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}:
        actual = "twitch"
        match = re.fullmatch(r"/videos/([0-9]+)/?", parsed.path)
        if match:
            target = _Target("https://www.twitch.tv/videos/" + match[1], actual, False)
        else:
            match = re.fullmatch(r"/([A-Za-z0-9_]{1,25})(?:/videos)?/?", parsed.path)
            if not match or match[1].lower() in {"directory", "downloads", "settings", "videos", "search"}:
                raise SourceResolutionError("Use a Twitch channel or /videos/ URL")
            target = _Target("https://www.twitch.tv/" + match[1].lower(), actual, True)
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        actual = "youtube"
        path = parsed.path.rstrip("/")
        if host == "youtu.be":
            video_id = path.removeprefix("/")
        elif path == "/watch":
            ids = parse_qs(parsed.query).get("v", [])
            video_id = ids[0] if len(ids) == 1 else ""
        else:
            match = re.fullmatch(r"/(?:live|shorts)/([A-Za-z0-9_-]{11})", path)
            video_id = match[1] if match else ""
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            target = _Target("https://www.youtube.com/watch?" + urlencode({"v": video_id}), actual, False)
        elif host != "youtu.be" and re.fullmatch(
            r"/(?:@[\w.-]{3,30}|channel/UC[A-Za-z0-9_-]{22})(?:/(?:live|streams|videos|featured))?",
            path, flags=re.ASCII,
        ):
            base = re.sub(r"/(?:live|streams|videos|featured)$", "", path)
            target = _Target("https://www.youtube.com" + base, actual, True)
        else:
            raise SourceResolutionError("Use a YouTube video URL, channel URL, or explicit @handle")
    else:
        raise SourceResolutionError("Only public Twitch and YouTube URLs are supported")
    if platform is not None and actual != platform:
        raise SourceResolutionError("URL does not match the selected platform")
    return target


def _check(cancelled, deadline):
    if cancelled.is_set():
        raise SourceResolutionCancelled("Source resolution cancelled")
    if time.monotonic() >= deadline:
        raise SourceResolutionError("Source resolution exceeded the 30-second limit")


def _metadata(url, *, flat, proxy, cancelled, deadline, budget):
    _check(cancelled, deadline)
    if budget[0] <= 0:
        raise _MetadataLimit("Source metadata exceeded the 10 MB limit")
    command = [
        sys.executable, "-I", "-m", "yt_dlp", "--ignore-config", "--no-config-locations",
        "--no-plugin-dirs", "--no-cookies", "--no-cookies-from-browser",
        "--no-cache-dir", "--proxy", proxy,
        "--socket-timeout", "5", "--retries", "0", "--extractor-retries", "0",
        "--skip-download", "--simulate", "--no-warnings",
        "--playlist-end", str(MAX_CANDIDATES),
    ]
    if flat:
        command += ["--dump-single-json", "--flat-playlist"]
    else:
        # Full format metadata can include enormous fragment lists; only these
        # fields are needed for validation and Twitch VOD manifest checks.
        command += [
            "--no-playlist", "--print",
            '{"meta":%(.{id,title,availability,is_private,needs_auth,live_status,is_live,webpage_url,_type,duration,has_drm})j,'
            '"formats":%(formats.:.{url,vcodec,acodec,protocol,height,has_drm}|[])j}',
        ]
    command += ["--", url]
    # Do not inherit proxy, PYTHONPATH, credential, or yt-dlp environment settings.
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    if "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=environment, start_new_session=True,
        )
    except OSError as exc:
        raise SourceResolutionError("Cannot start yt-dlp metadata resolver") from exc
    output = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            for name in output:
                selector.register(getattr(process, name), selectors.EVENT_READ, name)
            while selector.get_map():
                _check(cancelled, deadline)
                for key, _ in selector.select(timeout=min(0.1, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    budget[0] -= len(chunk)
                    if budget[0] < 0:
                        raise _MetadataLimit("Source metadata exceeded the 10 MB limit")
                    output[key.data].extend(chunk)
            while process.poll() is None:
                _check(cancelled, deadline)
                cancelled.wait(0.02)
        if process.returncode:
            error = output["stderr"].decode("utf-8", errors="replace").lower()
            if any(text in error for text in ("is offline", "not currently live")):
                raise _Offline()
            # Map remote-controlled stderr to fixed messages; never expose it verbatim.
            if any(text in error for text in (
                "sign in to confirm", "confirm you're not a bot", "confirm you’re not a bot",
                "bot detection", "login required", "sign in to view",
            )):
                message = "The provider requires sign-in or bot verification; public unauthenticated access is unavailable"
            elif "http error 429" in error or "too many requests" in error:
                message = "The provider rate-limited the metadata request; try again later"
            elif "http error 403" in error:
                message = "The provider denied the metadata request (HTTP 403); try again later"
            elif "timed out" in error or "timeout" in error:
                message = "The metadata request timed out; check your connection and try again"
            elif "no such option" in error or "unrecognized arguments" in error:
                message = "The installed yt-dlp does not support a required resolver option; update or reinstall yt-dlp"
            else:
                message = "yt-dlp could not resolve a public source; check the URL and provider availability"
            raise _MetadataFailure(message)
        try:
            data = json.loads(output["stdout"])
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise SourceResolutionError("yt-dlp returned invalid source metadata") from exc
        if not isinstance(data, dict):
            raise SourceResolutionError("yt-dlp returned invalid source metadata")
        if not flat:
            if not isinstance(data.get("meta"), dict) or not isinstance(data.get("formats"), list):
                raise SourceResolutionError("yt-dlp returned invalid source metadata")
            data = {**data["meta"], "formats": data["formats"]}
        return data
    finally:
        # Also terminate descendants if the parent exited while a pipe stayed open.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()


class _Offline(SourceResolutionError):
    pass


def _public(info):
    return info.get("availability") in {None, "public", "unlisted"} and not (
        info.get("is_private") or info.get("needs_auth")
    )


def _playable(info):
    return info.get("_type") in {None, "video"} and any(
        isinstance(item, dict) and item.get("url") and item.get("vcodec") != "none"
        for item in info.get("formats", []) or []
    )


def _live(info):
    # Conflicting metadata is never enough to classify a broadcast as live.
    return info.get("live_status") == "is_live" and info.get("is_live") is not False


def _manifest_finished(info, proxy, cancelled, deadline, transfer=None):
    """Twitch VOD metadata often omits live_status, even during a live archive."""
    from .jobs import Cancelled, RemoteFetch

    formats = info.get("formats") or []
    urls = [
        item["url"] for item in formats if isinstance(item, dict)
        and item.get("protocol") in {"m3u8", "m3u8_native"}
        and item.get("vcodec") != "none" and isinstance(item.get("url"), str)
    ]
    if not urls:
        return False
    url = urls[0]
    fetch = RemoteFetch(cancelled, deadline)
    if transfer is not None:
        fetch.remaining = max(0, transfer.remaining)
    else:
        fetch.remaining = MAX_METADATA_BYTES
    for _ in range(2):
        _check(cancelled, deadline)
        try:
            with fetch.open(url) as (final_url, response):
                body = bytearray()
                for chunk in fetch.chunks(response):
                    _check(cancelled, deadline)
                    if transfer is not None:
                        with transfer.lock:
                            transfer.remaining -= len(chunk)
                            if transfer.remaining < 0:
                                raise _MetadataLimit("Source metadata exceeded the 10 MB limit")
                    body.extend(chunk)
                    if len(body) > MAX_MANIFEST_BYTES:
                        return False
        except Cancelled as exc:
            raise SourceResolutionCancelled("Source resolution cancelled") from exc
        except _MetadataLimit:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            _check(cancelled, deadline)
            if fetch.exhausted:
                raise _MetadataLimit("Source metadata exceeded the 10 MB limit")
            return False
        lines = [line.strip() for line in body.decode("utf-8", errors="replace").splitlines() if line.strip()]
        if not lines or lines[0] != "#EXTM3U":
            return False
        if lines[-1] == "#EXT-X-ENDLIST" and any(line.startswith("#EXTINF:") for line in lines):
            return True
        if not any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
            return False
        variants = [line for line in lines if not line.startswith("#")]
        if not variants:
            return False
        url = urljoin(final_url, variants[0])
    return False


def _finished(info, platform, proxy, cancelled, deadline, transfer=None):
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        return False
    if platform == "twitch":
        return _manifest_finished(info, proxy, cancelled, deadline, transfer)
    return info.get("live_status") in {"not_live", "was_live"}


def youtube_vod_metadata(source, event, deadline):
    """Fetch bounded metadata for one canonical YouTube video; never download media."""
    from .jobs import DownloadProxy

    target = _target(source, "youtube")
    if target.channel:
        raise SourceResolutionError("Resolve the channel to a single YouTube video first")
    end = min(deadline, time.monotonic() + MAX_SECONDS)
    _check(event, end)
    guard = DownloadProxy(event, limit=MAX_METADATA_BYTES)
    guard.deadline = end
    with guard as proxy:
        info = _metadata(target.url, flat=False, proxy=proxy, cancelled=event,
                         deadline=end, budget=[MAX_METADATA_BYTES])
    if info.get("id") != parse_qs(urlsplit(target.url).query)["v"][0]:
        raise SourceResolutionError("Metadata did not match the requested YouTube video")
    return info


def resolve_source(source, mode="auto", platform=None, cancel_event=None, *, cancelled=None, deadline=None):
    """Return a verified source, sharing a 30s/10MB budget across all probes.

    ``deadline`` is an optional monotonic deadline; it can shorten, never extend,
    the hard ceiling. ``cancelled`` aliases ``cancel_event`` for worker adapters.
    Errors fail closed; an unavailable live probe is not evidence of being offline.
    """
    if mode not in {"auto", "live", "latest"}:
        raise SourceResolutionError("Mode must be auto, live, or latest")
    target = _target(source, platform)
    event = cancel_event if cancel_event is not None else cancelled
    if event is None:
        event = threading.Event()
    end = min(time.monotonic() + MAX_SECONDS, deadline if deadline is not None else float("inf"))
    _check(event, end)
    # Imported lazily to keep the backend's resolver adapter free of import cycles.
    from .jobs import DownloadProxy

    guard = DownloadProxy(event, limit=MAX_METADATA_BYTES)
    guard.deadline = end
    budget = [MAX_METADATA_BYTES]
    with guard as proxy:
        def metadata(url, flat=False):
            return _metadata(url, flat=flat, proxy=proxy, cancelled=event, deadline=end, budget=budget)

        def result(info, selection, fallback):
            raw_url = info.get("webpage_url") or fallback
            resolved = _target(raw_url, target.platform)
            if selection != "live" and resolved.channel:
                raise SourceResolutionError("Metadata did not identify a single archived video")
            return ResolvedSource(
                resolved.url, target.platform, str(info.get("title") or ""),
                selection == "live", str(info.get("id") or ""), mode, selection,
            )

        if not target.channel:
            info = metadata(target.url)
            if not _public(info) or not _playable(info):
                raise SourceResolutionError("This video is not publicly accessible and playable")
            if _live(info):
                if mode == "latest":
                    raise SourceResolutionError("The supplied video is still live, not a finished VOD")
                return result(info, "live", target.url)
            if target.platform == "youtube" and info.get("live_status") == "post_live":
                raise SourceResolutionError(
                    "YouTube reports this broadcast is still processing its archive; "
                    "the full video timeline is not yet verified"
                )
            if mode == "live" or not _finished(info, target.platform, proxy, event, end, guard):
                raise SourceResolutionError("This video is not a verified finished VOD")
            return result(info, "video", target.url)

        if mode != "latest":
            try:
                info = metadata(target.url + ("/live" if target.platform == "youtube" else ""))
            except _Offline:
                info = {}
            if _public(info) and _playable(info) and _live(info):
                return result(info, "live", target.url)
            if info and info.get("live_status") not in {"not_live", "was_live", "is_upcoming", "post_live"}:
                raise SourceResolutionError("Could not establish whether the channel is live")
            if mode == "live":
                raise SourceResolutionError("This channel is not currently live")
        listing = metadata(
            target.url + ("/streams" if target.platform == "youtube" else "/videos?filter=archives&sort=time"),
            flat=True,
        )
        entries = listing.get("entries")
        if not isinstance(entries, list):
            raise SourceResolutionError("No publicly accessible finished VOD was found")
        metadata_failure = None
        for entry in entries[:MAX_CANDIDATES]:
            _check(event, end)
            if not isinstance(entry, dict) or not _public(entry):
                continue
            if entry.get("is_live") or entry.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
                continue
            raw_url = entry.get("webpage_url") or entry.get("url")
            if not isinstance(raw_url, str):
                continue
            try:
                candidate = _target(raw_url, target.platform)
            except SourceResolutionError:
                continue
            if candidate.channel:
                continue
            try:
                info = metadata(candidate.url)
            except (SourceResolutionCancelled, _MetadataLimit):
                raise
            except _MetadataFailure as exc:
                metadata_failure = metadata_failure or str(exc)
                _check(event, end)
                continue
            except SourceResolutionError:
                _check(event, end)
                continue
            if (_public(info) and _playable(info)
                    and _finished(info, target.platform, proxy, event, end, guard)):
                return result(info, "latest", candidate.url)
        if metadata_failure:
            raise SourceResolutionError(
                "No publicly accessible finished VOD was found; " + metadata_failure
            )
    raise SourceResolutionError("No publicly accessible finished VOD was found among the 20 most recent archives")
