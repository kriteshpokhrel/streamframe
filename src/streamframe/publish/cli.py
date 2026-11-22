"""``streamframe publish`` — a separate command from capture and rendering.

Exit codes: 0 success, 1 a publish failed, 2 the request was refused before
touching a platform (for example a real run without ``--yes``).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from .auth import AuthError, authorize
from .ledger import Ledger
from .media import file_digest
from .model import Post
from .runner import FAILED, Publisher
from .settings import ConfigError, PLATFORMS, Settings
from .tokens import TokenError, store_for


def add_parser(subparsers):
    publish = subparsers.add_parser(
        "publish",
        help="post rendered clips to YouTube, Instagram and TikTok",
        description="Publish an exported clip. YouTube and Instagram post "
                    "immediately; TikTok receives an inbox draft that a person "
                    "finishes in the app. Safe to re-run: each clip publishes once.",
        epilog="Secrets live in .env; OAuth tokens live in the state directory. "
               "Run `streamframe publish doctor` first.",
    )
    commands = publish.add_subparsers(dest="publish_command", required=True)

    doctor = commands.add_parser("doctor", help="check configuration and credentials")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")

    setup = commands.add_parser(
        "setup", help="create .env and show the next console step")
    setup.add_argument("--write-env", action="store_true",
                       help="create .env from .env.example if it is missing")

    auth = commands.add_parser("auth", help="authorize one platform on this machine")
    auth.add_argument("platform", choices=PLATFORMS)
    auth.add_argument("--redirect-uri", help="registered redirect URI")
    auth.add_argument("--port", type=int, default=8766, help="loopback port for YouTube")
    auth.add_argument("--manual", action="store_true",
                      help="paste the code instead of waiting on a loopback listener")

    run = commands.add_parser("run", help="publish one exported clip")
    run.add_argument("--file", required=True, help="exported MP4 path")
    run.add_argument("--platforms", help="comma-separated subset (default: configured)")
    run.add_argument("--title", default="", help="YouTube/TikTok title")
    run.add_argument("--caption", default="", help="shared caption")
    run.add_argument("--description", default="", help="YouTube description")
    run.add_argument("--tags", default="", help="comma-separated tags")
    run.add_argument("--clip-key", help="override the content hash used for idempotency")
    run.add_argument("--dry-run", action="store_true", help="report the plan, publish nothing")
    run.add_argument("--yes", action="store_true", help="confirm a real publish")
    run.add_argument("--json", action="store_true", help="machine-readable output")

    status = commands.add_parser("status", help="show publication history")
    status.add_argument("--clip-key", help="limit to one clip")
    status.add_argument("--json", action="store_true")

    retry = commands.add_parser("retry", help="allow failed clips to be attempted again")
    retry.add_argument("--clip-key", required=True)
    retry.add_argument("--platform", choices=PLATFORMS, action="append")

    commands.add_parser("refresh", help="refresh stored OAuth tokens")

    return publish


def run(args):
    try:
        settings = Settings.load()
        if args.publish_command == "setup":
            return _setup(settings, args)
        if args.publish_command == "doctor":
            return _doctor(settings, as_json=args.json)
        if args.publish_command == "auth":
            return _auth(settings, args)
        if args.publish_command == "run":
            return _run(settings, args)
        if args.publish_command == "status":
            return _status(settings, args)
        if args.publish_command == "retry":
            return _retry(settings, args)
        if args.publish_command == "refresh":
            return _refresh(settings)
    except (ConfigError, TokenError, AuthError) as exc:
        print(f"streamframe publish: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstreamframe publish: interrupted", file=sys.stderr)
        return 130
    return 2


# -- commands -----------------------------------------------------------

def _doctor(settings, *, as_json=False):
    ledger = Ledger(settings.state_dir / "publications.sqlite3")
    ledger.recover_stale()
    store = store_for(settings)
    credentials = store.load()
    report = {
        "env_file": str(settings.env_file) if settings.env_file else None,
        "state_dir": str(settings.state_dir),
        "platforms": list(settings.platforms),
        "media_host": settings.media.host,
        "media_ready": settings.media.configured,
        "problems": [{"platform": p, "message": m} for p, m in settings.problems()],
        "credentials": {
            platform: _credential_state(credentials.get(platform))
            for platform in settings.platforms
        },
        "records": len(ledger.records()),
    }
    if as_json:
        print(json.dumps(report, indent=2))
        return 0 if not report["problems"] else 1
    print(f"env file      {report['env_file'] or 'none found (.env at the repo root is expected)'}")
    print(f"state dir     {report['state_dir']}")
    print(f"media host    {settings.media.host}" + ("" if settings.media.configured
                                                else "  (Instagram will not publish)"))
    for platform in settings.platforms:
        state = report["credentials"][platform]
        print(f"{platform:<13} credentials: {state}")
    for problem in report["problems"]:
        print(f"  ! {problem['platform']}: {problem['message']}")
    print(f"records       {report['records']}")
    return 0 if not report["problems"] else 1


CONSOLE_LINKS = {
    "youtube": [
        ("Create or pick a Google Cloud project", "https://console.cloud.google.com/projectcreate"),
        ("Enable YouTube Data API v3", "https://console.cloud.google.com/apis/library/youtube.googleapis.com"),
        ("OAuth consent screen (External, add yourself as a test user)",
         "https://console.cloud.google.com/apis/credentials/consent"),
        ("Create an OAuth client, type Desktop app", "https://console.cloud.google.com/apis/credentials"),
    ],
    "instagram": [
        ("Create a Meta app", "https://developers.facebook.com/apps/"),
        ("Add product: Instagram, API setup with Instagram login",
         "https://developers.facebook.com/apps/"),
        ("Business login settings: add an HTTPS redirect URI",
         "https://developers.facebook.com/apps/"),
    ],
    "tiktok": [
        ("Create a TikTok app", "https://developers.tiktok.com/apps"),
        ("Add Login Kit and Content Posting API (Upload only)", "https://developers.tiktok.com/apps"),
    ],
}

MEDIA_LINKS = [
    ("Create an R2 bucket (free tier)", "https://dash.cloudflare.com/?to=/:account/r2"),
]


def _setup(settings, args):
    """Local setup only: write .env, then report the next console step.

    App registration and OAuth consent are web flows with no public API, so
    this command stops at the edge of what can be automated.
    """
    env_path = Path.cwd() / ".env"
    if args.write_env:
        if env_path.exists():
            print(f".env already exists at {env_path}; leaving it untouched.")
        else:
            template = _env_template()
            env_path.write_text(template, encoding="utf-8")
            os.chmod(env_path, 0o600)
            print(f"Created {env_path} from .env.example (mode 0600).")
            print("It is git-ignored. Fill in the values as you go.\n")

    store = store_for(settings)
    credentials = store.load()
    problems = {}
    for platform, message in settings.problems():
        problems.setdefault(platform, []).append(message)

    print("Publishing setup\n")
    env_shown = settings.env_file or (env_path if env_path.exists() else "missing")
    print(f"  .env        {env_shown}")
    print(f"  state dir   {settings.state_dir}\n")

    outstanding = 0
    for platform in settings.platforms:
        blocked = platform in problems
        authorized = platform in credentials
        if not blocked and authorized:
            print(f"{platform}: ready")
            continue
        outstanding += 1
        print(f"{platform}: needs work")
        if blocked:
            for message in problems[platform]:
                print(f"  - {message}")
            print("  Then:")
            for label, url in CONSOLE_LINKS.get(platform, []):
                print(f"    {label}\n      {url}")
        if not authorized:
            print("  After that, authorize this machine:")
            print(f"    ./streamframe publish auth {platform}")
            if platform != "youtube":
                print("    (add --redirect-uri https://your-site/cb if needed)")
        print()

    if "instagram" in settings.platforms and not settings.media.configured:
        print("Instagram also needs a public media URL:")
        for label, url in MEDIA_LINKS:
            print(f"    {label}\n      {url}")
        print("  Then set STREAMFRAME_MEDIA_HOST (local or r2), its public base URL,\n"
              "  and restart the server.\n")

    if outstanding:
        print(f"{outstanding} platform(s) still incomplete. Re-run this command after each step.")
        return 1
    print("All configured. Publish a clip with:")
    print("  ./streamframe publish run --file clip.mp4 --caption \"...\"")
    return 0


def _env_template():
    candidates = [
        Path.cwd() / ".env.example",
        Path(__file__).resolve().parents[3] / ".env.example",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise ConfigError("No .env.example found; create .env by hand from the docs")


def _credential_state(record):
    if not record:
        return "missing (run publish auth)"
    expires_at = record.get("expires_at")
    if not expires_at:
        return "stored"
    when = datetime.fromtimestamp(expires_at, timezone.utc).isoformat(timespec="seconds")
    return f"stored, expires {when}"


def _auth(settings, args):
    authorize(settings, args.platform, redirect_uri=args.redirect_uri, port=args.port,
              manual=True if args.manual else None)
    return 0


def _run(settings, args):
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise ConfigError(f"Clip not found: {path}")
    if path.suffix.lower() not in {".mp4", ".mov"}:
        raise ConfigError("Publish an exported .mp4 or .mov file")
    platforms = ([p.strip().lower() for p in args.platforms.split(",") if p.strip()]
                 if args.platforms else list(settings.platforms))
    dry_run = args.dry_run or not args.yes

    blocked = {}
    for platform, message in settings.problems():
        blocked.setdefault(platform, []).append(message)

    post = Post(
        path=path,
        clip_key=args.clip_key or file_digest(path),
        title=args.title,
        caption=args.caption,
        description=args.description,
        tags=tuple(t.strip() for t in args.tags.split(",") if t.strip()),
    )

    requested = list(platforms)
    if not dry_run:
        platforms = [platform for platform in platforms if platform not in blocked]
        if not platforms:
            for platform in requested:
                for message in blocked.get(platform, []):
                    print(f"  ! {platform}: {message}", file=sys.stderr)
            print("streamframe publish: nothing can be published yet; "
                  "run publish doctor", file=sys.stderr)
            return 2
    skipped_for_config = {p: blocked[p] for p in requested if p in blocked}

    publisher = Publisher(settings)
    outcomes = publisher.publish(post, platforms=platforms, dry_run=dry_run)

    if args.json:
        print(json.dumps([outcome.__dict__ for outcome in outcomes], indent=2))
    else:
        print(f"clip {post.clip_key[:16]}  {path.name}")
        for outcome in outcomes:
            line = f"  {outcome.platform:<10} {outcome.action:<14}"
            if outcome.detail:
                line += outcome.detail
            if outcome.url:
                line += f"  {outcome.url}"
            print(line)
            blocked_reason = blocked.get(outcome.platform)
            if blocked_reason:
                print(f"  {'':<10} {'blocked':<14}{blocked_reason[0]}")
        if not dry_run:
            for platform, reasons in skipped_for_config.items():
                print(f"  ! skipped {platform}: {reasons[0]}", file=sys.stderr)
    if dry_run:
        print("\nNothing was published. Re-run with --yes to publish for real.")
        return 0
    failed = any(o.action == FAILED for o in outcomes)
    return 1 if failed or skipped_for_config else 0


def _status(settings, args):
    ledger = Ledger(settings.state_dir / "publications.sqlite3")
    records = ledger.records(args.clip_key)
    if args.json:
        print(json.dumps([record.__dict__ for record in records], indent=2))
        return 0
    if not records:
        print("No publications yet.")
        return 0
    print(f"{'clip':<18}{'platform':<11}{'status':<14}{'id':<24}updated")
    for record in records:
        updated = datetime.fromtimestamp(record.updated_at, timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"{record.clip_key[:16]:<18}{record.platform:<11}{record.status:<14}"
              f"{(record.platform_id or '-'):<24}{updated}")
        if record.error:
            print(f"    error: {record.error}")
        if record.detail:
            print(f"    note:  {record.detail}")
    return 0


def _retry(settings, args):
    ledger = Ledger(settings.state_dir / "publications.sqlite3")
    targets = args.platform or list(settings.platforms)
    cleared = 0
    for platform in targets:
        record = ledger.status_for(args.clip_key).get(platform)
        if record and record.status == "failed":
            ledger.reset(args.clip_key, platform)
            cleared += 1
    print(f"Reset {cleared} failed record(s) for {args.clip_key[:16]}.")
    return 0 if cleared else 1


def _refresh(settings):
    from .tokens import refresh_all
    results = refresh_all(settings)
    for platform, state in results.items():
        print(f"  {platform:<10} {state}")
    return 0


__all__ = ["add_parser", "run"]
