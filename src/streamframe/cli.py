"""Command-line access to StreamFrame without a browser.

Run ``python -m streamframe.cli --help`` for commands. By default commands
connect to 127.0.0.1:8765 and start a local background server if needed. The
server uses ``.streamframe`` in the current directory for its database and
outputs, unless ``STREAMFRAME_DATA_DIR`` is set. An already-running server
retains its own configuration. Set ``--api-url`` to use another server;
remote servers are never started locally. The backend defaults to allowing
local imports from the user's home directory; configure
``STREAMFRAME_IMPORT_ROOTS`` (separated by the OS path separator) for others.

The CLI uses the JSON HTTP API exposed by ``streamframe.jobs``. It requires the
backend's FastAPI/uvicorn dependencies, but neither a browser nor a server
started in advance. ``serve`` runs the same application in the foreground.
Local file sources accept optional ``start`` and ``end`` in source seconds.
Direct video links require both bounds; the bounded VOD backend currently
supports Twitch and some YouTube VODs in verified muxed MPEG-TS HLS, not
YouTube DASH or direct MP4.
Preview, candidate and caption timestamps are zero-based relative to the
selected range.
Use ``streamer`` for channel selectors and bounded live capture.
Edits patch a candidate's ``start``, ``end``, ``face_crop``, ``content_crop``,
and/or ``words`` fields; crops are normalized x/y/width/height rectangles and
words are a JSON array of objects with start, end, and text. Export starts a
background render; its resulting location appears in the job's ``exports``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .live import MAX_SESSION_SECONDS


DEFAULT_API_URL = "http://127.0.0.1:8765"
MAX_RANGE_SECONDS = 1800
MIN_CAPTURE_SECONDS = 1
MAX_CAPTURE_SECONDS = 300
FAILED_STATES = {"failed", "error", "cancelled", "canceled", "interrupted"}
TERMINAL_STATES = {"completed", "succeeded", "complete", "done"} | FAILED_STATES


class CLIError(Exception):
    """A user-facing command failure."""


def _request(base_url, method, path, payload=None, timeout=5):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
            if isinstance(detail, dict):
                detail = detail.get("detail", detail)
            body = detail if isinstance(detail, str) else json.dumps(detail)
        except ValueError:
            pass
        raise CLIError(f"HTTP {exc.code}: {body or exc.reason}") from exc
    except (URLError, OSError) as exc:
        raise CLIError(f"Cannot connect to {base_url}: {exc}") from exc
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CLIError("Server returned a non-JSON response") from exc


def _local_address(base_url):
    try:
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            return None
        return parsed.hostname, parsed.port or 80
    except ValueError:
        return None


def _ensure_server(base_url, auto_start=True):
    """Probe the API, starting a local server only when it is unreachable."""
    def check_health():
        if _request(base_url, "GET", "/api/health", timeout=1) != {"status": "ok"}:
            raise CLIError(f"{base_url} did not return the StreamFrame health response")

    try:
        check_health()
        return
    except CLIError as exc:
        if not str(exc).startswith("Cannot connect to "):
            raise
        initial_error = exc
    address = _local_address(base_url)
    if not auto_start or address is None:
        raise initial_error
    host, port = address
    try:
        env = os.environ.copy()
        env.setdefault("STREAMFRAME_DATA_DIR", str(Path.cwd() / ".streamframe"))
        process = subprocess.Popen(
            [sys.executable, "-m", "streamframe.cli", "serve", "--host", host, "--port", str(port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=env,
        )
    except OSError as exc:
        raise CLIError(f"Unable to start local server: {exc}") from exc
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            check_health()
            return
        except CLIError as exc:
            if not str(exc).startswith("Cannot connect to "):
                raise
        if process.poll() is not None:
            # A competing invocation may have won the bind; try it once more.
            try:
                check_health()
                return
            except CLIError:
                break
        time.sleep(0.1)
    raise CLIError(
        "Local server did not start. Check the backend dependencies and run "
        "`python -m streamframe.cli serve` in the project directory for details."
    )


def _id(value):
    return quote(str(value), safe="")


def _source(value, start=None, end=None):
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise CLIError(f"Invalid source: {exc}") from exc
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        if start is None or end is None:
            raise CLIError("Public Twitch/YouTube links require both --start and --end")
        source = {"type": "url", "url": value}
    else:
        if parsed.scheme:
            raise CLIError("Source must be a local file or a public Twitch/YouTube link")
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise CLIError(f"Source file does not exist: {path}")
        source = {"type": "file", "path": str(path)}
    source.update(_range_bounds(start, end))
    return source


def _range_bounds(start, end):
    bounds = {}
    for key, bound in (("start", start), ("end", end)):
        if bound is not None:
            if not math.isfinite(bound) or bound < 0:
                raise CLIError(f"--{key} must be finite and nonnegative")
            bounds[key] = bound
    if end is not None and not 0 < end - (start or 0) <= MAX_RANGE_SECONDS:
        raise CLIError(f"--end must be after the start and the range must be at most {MAX_RANGE_SECONDS} seconds")
    return bounds


def _streamer_source(args):
    identifier = args.identifier.strip()
    if not identifier:
        raise CLIError("Enter a Twitch username or channel URL, or a YouTube channel URL or @handle")
    try:
        parsed = urlsplit(identifier)
    except ValueError as exc:
        raise CLIError(f"Invalid channel: {exc}") from exc
    if parsed.scheme:
        if parsed.scheme != "https" or not parsed.hostname:
            raise CLIError("Expected a public HTTPS Twitch or YouTube channel URL")
        if args.platform is not None:
            raise CLIError("--platform applies only to usernames and handles, not URLs")
        source = {"type": "url", "url": identifier, "mode": args.mode}
    else:
        if identifier.startswith("@") and args.platform != "youtube":
            raise CLIError("YouTube @handles require --platform youtube")
        platform = args.platform or "twitch"
        source = {"type": "streamer", "platform": platform, "identifier": identifier, "mode": args.mode}
    if args.rolling:
        if args.mode != "live" or args.duration is not None or args.start is not None or args.end is not None:
            raise CLIError("--rolling requires --mode live and cannot use --duration or VOD ranges")
        if (not math.isfinite(args.session_duration) or not 1 <= args.session_duration <= MAX_SESSION_SECONDS
                or not math.isfinite(args.window_duration) or not 1 <= args.window_duration <= 60
                or args.window_duration > args.session_duration):
            raise CLIError(f"Rolling session must be 1–{MAX_SESSION_SECONDS} seconds with windows of 1–60 seconds "
                           "no longer than the session")
        source.update(rolling=True, session_duration=args.session_duration,
                      window_duration=args.window_duration)
    elif args.session_duration != 600 or args.window_duration != 60:
        raise CLIError("--session-duration and --window-duration require --rolling")
    if args.duration is not None:
        if not MIN_CAPTURE_SECONDS <= args.duration <= MAX_CAPTURE_SECONDS:
            raise CLIError(f"--duration must be between {MIN_CAPTURE_SECONDS} and {MAX_CAPTURE_SECONDS} seconds")
        source["duration"] = args.duration
    if args.start is not None or args.end is not None:
        if args.mode == "live":
            raise CLIError("Ranges apply only to finished streams; live capture uses --duration")
        if args.start is None or args.end is None:
            raise CLIError("A finished stream range requires both --start and --end")
    source.update(_range_bounds(args.start, args.end))
    return source


def _print_result(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _watch(base_url, job_id, interval):
    while True:
        result = _request(base_url, "GET", f"/api/jobs/{_id(job_id)}")
        _print_result(result)
        state = str(result.get("status", "")).lower() if isinstance(result, dict) else ""
        if state in TERMINAL_STATES:
            return 1 if state in FAILED_STATES else 0
        time.sleep(interval)


def _add_follow_options(command, help_text):
    command.add_argument("--follow", action="store_true", help=help_text)
    command.add_argument("--interval", type=float, default=2, help="poll interval in seconds")


def _add_publish_options(command):
    command.add_argument("--auto-publish", dest="auto_publish", action="store_true",
                         default=None,
                         help="publish finished exports automatically "
                              "(default: STREAMFRAME_PUBLISH_AUTO)")
    command.add_argument("--no-auto-publish", dest="auto_publish", action="store_false",
                         help="never publish this source's exports")
    command.add_argument("--publish-title", metavar="TEXT",
                         help="title for automatically published clips (required with --auto-publish)")
    command.add_argument("--publish-platforms", metavar="LIST",
                         help="comma-separated platforms for automatic upload")
    command.add_argument("--publish-privacy", choices=("public", "unlisted", "private"),
                         help="YouTube visibility for automatic upload")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="streamframe",
        description="Create, inspect and edit StreamFrame jobs without a browser.",
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL,
                        help=f"API endpoint (default: {DEFAULT_API_URL})")
    parser.add_argument("--no-start", action="store_true",
                        help="do not start a local server automatically")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the API server in the foreground")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    submit = commands.add_parser(
        "submit", help="submit a local file or public video range",
        description="Submit a local file or a public video range. URL sources require "
                    "both --start and --end; local files can omit either or both. "
                    "Finished VOD ranges require a supported public, clear, muxed MPEG-TS HLS archive.",
        epilog="Preview, candidate and caption timestamps are zero-based relative to the selected range.",
    )
    submit.add_argument("source", help="local file path or public Twitch VOD URL")
    submit.add_argument("--start", type=float,
                        help="range start in source seconds; local start defaults to 0 with --end")
    submit.add_argument("--end", type=float,
                        help="range end in source seconds; maximum span is 1800 seconds "
                             "(local --start alone takes up to that span)")
    _add_follow_options(submit, "watch the job until it finishes")
    _add_publish_options(submit)
    streamer = commands.add_parser(
        "streamer", help="capture a live channel or use its latest finished stream",
        description="Select a Twitch username or channel URL, or a YouTube channel URL or @handle.",
        epilog="Captured media, candidate and caption timestamps start at zero. Auto with an explicit "
               "range fails if the channel is live; use --duration for live capture. "
               "Finished YouTube streams are not currently supported by the backend.",
    )
    streamer.add_argument("identifier", help="e.g. twitchname, @youtubehandle, or a channel URL")
    streamer.add_argument("--platform", choices=("twitch", "youtube"),
                          help="override platform for names and handles (a bare name defaults to Twitch)")
    streamer.add_argument("--mode", choices=("auto", "live", "latest"), default="auto",
                          help="auto: live if available, otherwise latest finished; live: live only; "
                               "latest: latest finished only")
    streamer.add_argument("--duration", type=float, metavar="SECONDS",
                          help=f"capture duration in seconds ({MIN_CAPTURE_SECONDS}–{MAX_CAPTURE_SECONDS}; default: 60)")
    streamer.add_argument("--rolling", action="store_true", help="automatically export during a finite live session")
    streamer.add_argument("--session-duration", type=float, default=600, metavar="SECONDS",
                          help=f"rolling session limit in seconds (1–{MAX_SESSION_SECONDS}; default: 600)")
    streamer.add_argument("--window-duration", type=float, default=60, metavar="SECONDS",
                          help="rolling window maximum in seconds (1–60; default: 60)")
    streamer.add_argument("--start", type=float, help="finished stream range start; requires --end")
    streamer.add_argument("--end", type=float, help="finished stream range end; requires --start")
    _add_follow_options(streamer, "watch the job until it finishes")
    _add_publish_options(streamer)
    commands.add_parser("list", help="list jobs")
    status = commands.add_parser("status", help="show job progress")
    status.add_argument("job_id")
    status.add_argument("--watch", action="store_true", help="poll until the job finishes")
    status.add_argument("--interval", type=float, default=2, help="poll interval in seconds")
    for name in ("cancel", "resume"):
        command = commands.add_parser(name, help=f"{name} a job")
        command.add_argument("job_id")
    live_crop = commands.add_parser("live-crop", help="set the face crop for future rolling windows")
    live_crop.add_argument("job_id")
    crop_group = live_crop.add_mutually_exclusive_group(required=True)
    crop_group.add_argument("--face-crop", type=float, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    crop_group.add_argument("--auto", action="store_true", help="restore automatic face detection")
    edit = commands.add_parser("edit", help="edit a candidate's timing, crop or captions")
    edit.add_argument("job_id")
    edit.add_argument("candidate_id")
    edit.add_argument("--start", type=float, help="start time in seconds")
    edit.add_argument("--end", type=float, help="end time in seconds")
    for kind in ("face", "content"):
        group = edit.add_mutually_exclusive_group()
        group.add_argument(f"--{kind}-crop", type=float, nargs=4,
                           metavar=("X", "Y", "WIDTH", "HEIGHT"),
                           help="normalized crop rectangle (0 to 1)")
        group.add_argument(f"--clear-{kind}-crop", action="store_true",
                           help=f"remove the {kind} crop")
    edit.add_argument("--words-json", metavar="JSON",
                      help='replace captions with JSON array, e.g. \'[{"start":0,"end":1,"text":"Hi"}]\'')
    export = commands.add_parser("export", help="render a candidate")
    export.add_argument("job_id")
    export.add_argument("candidate_id")
    _add_follow_options(export, "watch the export until it finishes")
    from .publish.cli import add_parser as add_publish_parser

    add_publish_parser(commands)
    return parser


def _serve(host, port):
    try:
        import uvicorn
    except ImportError as exc:
        raise CLIError("Serving requires the backend dependencies, including uvicorn") from exc
    os.environ.setdefault("STREAMFRAME_DATA_DIR", str(Path.cwd() / ".streamframe"))
    uvicorn.run("streamframe.server:app", host=host, port=port)


def _edit_payload(args):
    payload = {}
    for key in ("start", "end"):
        value = getattr(args, key)
        if value is not None:
            if not math.isfinite(value):
                raise CLIError(f"{key} must be finite")
            payload[key] = value
    for kind in ("face", "content"):
        values = getattr(args, f"{kind}_crop")
        if values is not None:
            if not all(math.isfinite(value) for value in values):
                raise CLIError("Crop coordinates must be finite")
            payload[f"{kind}_crop"] = dict(zip(("x", "y", "width", "height"), values))
        elif getattr(args, f"clear_{kind}_crop"):
            payload[f"{kind}_crop"] = None
    if args.words_json is not None:
        try:
            words = json.loads(args.words_json)
        except ValueError as exc:
            raise CLIError(f"Invalid captions JSON: {exc}") from exc
        if not isinstance(words, list):
            raise CLIError("Captions must be a JSON array of word objects")
        payload["words"] = words
    if not payload:
        raise CLIError("Specify at least one edit")
    return payload


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "publish":
            # Publishing is local-only and must never start the editing server.
            from .publish.cli import run as run_publish
            return run_publish(args)
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise CLIError("Port must be between 1 and 65535")
            if args.host not in {"127.0.0.1", "localhost", "::1"}:
                raise CLIError("The server may only bind to a loopback host")
            _serve(args.host, args.port)
            return 0
        try:
            parsed = urlsplit(args.api_url)
            valid_url = (parsed.scheme in {"http", "https"} and parsed.hostname
                         and not parsed.username and not parsed.password
                         and not parsed.query and not parsed.fragment)
            if parsed.port == 0:
                valid_url = False
        except ValueError:
            valid_url = False
        if not valid_url:
            raise CLIError("API URL must be an http(s) URL without credentials, query or fragment")
        if hasattr(args, "interval") and (not math.isfinite(args.interval) or args.interval <= 0):
            raise CLIError("Poll interval must be greater than zero")
        source = None
        if args.command == "submit":
            source = _source(args.source, args.start, args.end)
        elif args.command == "streamer":
            source = _streamer_source(args)
        edit_payload = _edit_payload(args) if args.command == "edit" else None
        _ensure_server(args.api_url, not args.no_start)
        if args.command == "list":
            result = _request(args.api_url, "GET", "/api/jobs")
        elif args.command in {"submit", "streamer"}:
            payload = {"source": source}
            if getattr(args, "auto_publish", None) is not None:
                payload["auto_publish"] = args.auto_publish
            if getattr(args, "publish_title", None):
                payload["publish_title"] = args.publish_title
            if getattr(args, "publish_platforms", None):
                payload["publish_platforms"] = [p.strip() for p in args.publish_platforms.split(",")
                                                if p.strip()]
            if getattr(args, "publish_privacy", None):
                payload["publish_privacy"] = args.publish_privacy
            result = _request(args.api_url, "POST", "/api/jobs", payload)
            _print_result(result)
            if args.follow:
                if not isinstance(result, dict) or result.get("id") is None:
                    raise CLIError("Server did not return a job id")
                return _watch(args.api_url, result["id"], args.interval)
            return 0
        elif args.command == "status":
            if args.watch:
                return _watch(args.api_url, args.job_id, args.interval)
            result = _request(args.api_url, "GET", f"/api/jobs/{_id(args.job_id)}")
        elif args.command == "edit":
            path = f"/api/jobs/{_id(args.job_id)}/candidates/{_id(args.candidate_id)}"
            result = _request(args.api_url, "PATCH", path, edit_payload)
        elif args.command == "live-crop":
            crop = dict(zip(("x", "y", "width", "height"), args.face_crop)) if args.face_crop else None
            if crop is not None and not all(math.isfinite(value) for value in crop.values()):
                raise CLIError("Crop coordinates must be finite")
            result = _request(args.api_url, "PATCH", f"/api/jobs/{_id(args.job_id)}/live-crop",
                              {"face_crop": crop})
        elif args.command == "export":
            path = f"/api/jobs/{_id(args.job_id)}/candidates/{_id(args.candidate_id)}/export"
            result = _request(args.api_url, "POST", path)
            _print_result(result)
            if args.follow:
                return _watch(args.api_url, args.job_id, args.interval)
            return 0
        else:
            result = _request(args.api_url, "POST", f"/api/jobs/{_id(args.job_id)}/{args.command}")
        _print_result(result)
        return 0
    except CLIError as exc:
        print(f"streamframe: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstreamframe: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
