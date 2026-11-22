"""Publishing configuration, loaded from a git-ignored ``.env`` file.

Publishing is the only StreamFrame feature that talks to third parties with
credentials that grant posting rights, so configuration is kept deliberately
separate from the editing server:

* ``.env`` holds client IDs, client secrets and non-secret defaults. It is
  shared through a password manager, never through git.
* OAuth tokens (the part that actually posts) live in the state directory,
  written with mode 0600 by ``streamframe publish auth``. They are not
  environment variables because they rotate and must never be pasted around.

Real environment variables win over ``.env`` entries so a shell or CI job can
override a checked-out file without editing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

PLATFORMS = ("youtube", "instagram", "tiktok")
MEDIA_HOSTS = ("none", "local", "r2")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ConfigError(ValueError):
    """Configuration is missing or malformed for the requested platform."""


def parse_env(text):
    """Parse dotenv syntax without a third-party dependency.

    Supports comments, blank lines, an optional ``export`` prefix, single or
    double quotes, and the usual escapes inside double quotes. Anything else is
    left verbatim so values such as bucket names or URLs pass through untouched.
    """
    values = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not _ENV_NAME.match(name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote, body = value[0], value[1:-1]
            if quote == '"':
                body = (body.replace("\\n", "\n").replace("\\t", "\t")
                        .replace('\\"', '"').replace("\\\\", "\\"))
            else:
                body = body.replace("\\'", "'")
            value = body
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value
    return values


def env_file_candidates(data_dir=None):
    """Return the .env locations checked, in precedence order."""
    candidates = []
    explicit = os.environ.get("STREAMFRAME_PUBLISH_ENV")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.cwd() / ".env")
    if data_dir is not None:
        candidates.append(Path(data_dir) / ".env")
    return candidates


def load_env_file(path, environ=None):
    """Merge one .env file into ``environ`` without overwriting existing keys."""
    environ = os.environ if environ is None else environ
    text = Path(path).read_text(encoding="utf-8")
    for name, value in parse_env(text).items():
        environ.setdefault(name, value)
    return environ


def load_environment(data_dir=None, environ=None):
    """Load the first .env that exists and return (environ, path_or_None)."""
    environ = os.environ if environ is None else environ
    for candidate in env_file_candidates(data_dir):
        if candidate.is_file():
            load_env_file(candidate, environ)
            return environ, candidate
    return environ, None


def _flag(value, default=False):
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _text(value):
    value = (value or "").strip()
    return value or None


def _number(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class YouTubeSettings:
    client_id: str | None = None
    client_secret: str | None = None
    privacy: str = "public"
    category_id: int = 24
    made_for_kids: bool = False

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)


@dataclass(frozen=True)
class InstagramSettings:
    app_id: str | None = None
    app_secret: str | None = None
    api_version: str = "v23.0"
    share_to_feed: bool = True

    @property
    def configured(self):
        return bool(self.app_id and self.app_secret)


@dataclass(frozen=True)
class TikTokSettings:
    client_key: str | None = None
    client_secret: str | None = None

    @property
    def configured(self):
        return bool(self.client_key and self.client_secret)


@dataclass(frozen=True)
class MediaSettings:
    """Where Instagram can fetch a clip from. TikTok and YouTube upload bytes."""

    host: str = "none"
    public_base_url: str | None = None
    local_dir: Path | None = None
    r2_bucket: str | None = None
    r2_endpoint: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None

    @property
    def configured(self):
        if not self.public_base_url:
            return False
        if self.host == "local":
            return self.local_dir is not None
        if self.host == "r2":
            return all((self.r2_bucket, self.r2_endpoint,
                        self.r2_access_key_id, self.r2_secret_access_key))
        return False


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    state_dir: Path
    env_file: Path | None
    platforms: tuple[str, ...]
    youtube: YouTubeSettings = field(default_factory=YouTubeSettings)
    instagram: InstagramSettings = field(default_factory=InstagramSettings)
    tiktok: TikTokSettings = field(default_factory=TikTokSettings)
    media: MediaSettings = field(default_factory=MediaSettings)
    http_timeout: float = 120.0
    poll_timeout: float = 300.0

    @classmethod
    def load(cls, data_dir=None, environ=None):
        environ = os.environ if environ is None else environ
        data_dir = Path(data_dir or environ.get("STREAMFRAME_DATA_DIR")
                        or Path.home() / ".streamframe").expanduser()
        environ, env_file = load_environment(data_dir, environ)

        raw_platforms = _text(environ.get("STREAMFRAME_PUBLISH_PLATFORMS"))
        if raw_platforms:
            platforms = tuple(p.strip().lower() for p in raw_platforms.split(",") if p.strip())
            unknown = sorted(set(platforms) - set(PLATFORMS))
            if unknown:
                raise ConfigError(f"Unknown platforms in STREAMFRAME_PUBLISH_PLATFORMS: {', '.join(unknown)}")
        else:
            platforms = PLATFORMS

        state_dir = Path(environ.get("STREAMFRAME_PUBLISH_STATE_DIR")
                         or (data_dir / "publish")).expanduser()

        local_dir = _text(environ.get("STREAMFRAME_MEDIA_LOCAL_DIR"))
        media = MediaSettings(
            host=(_text(environ.get("STREAMFRAME_MEDIA_HOST")) or "none").lower(),
            public_base_url=_text(environ.get("STREAMFRAME_MEDIA_PUBLIC_BASE_URL")),
            local_dir=Path(local_dir).expanduser() if local_dir else None,
            r2_bucket=_text(environ.get("STREAMFRAME_R2_BUCKET")),
            r2_endpoint=_text(environ.get("STREAMFRAME_R2_ENDPOINT")),
            r2_access_key_id=_text(environ.get("STREAMFRAME_R2_ACCESS_KEY_ID")),
            r2_secret_access_key=_text(environ.get("STREAMFRAME_R2_SECRET_ACCESS_KEY")),
        )
        if media.host not in MEDIA_HOSTS:
            raise ConfigError(f"STREAMFRAME_MEDIA_HOST must be one of {', '.join(MEDIA_HOSTS)}")

        privacy = (_text(environ.get("STREAMFRAME_YOUTUBE_PRIVACY")) or "public").lower()
        if privacy not in {"public", "unlisted", "private"}:
            raise ConfigError("STREAMFRAME_YOUTUBE_PRIVACY must be public, unlisted or private")

        return cls(
            data_dir=data_dir,
            state_dir=state_dir,
            env_file=env_file,
            platforms=platforms,
            youtube=YouTubeSettings(
                client_id=_text(environ.get("STREAMFRAME_YOUTUBE_CLIENT_ID")),
                client_secret=_text(environ.get("STREAMFRAME_YOUTUBE_CLIENT_SECRET")),
                privacy=privacy,
                category_id=_number(environ.get("STREAMFRAME_YOUTUBE_CATEGORY_ID"), 24),
                made_for_kids=_flag(environ.get("STREAMFRAME_YOUTUBE_MADE_FOR_KIDS")),
            ),
            instagram=InstagramSettings(
                app_id=_text(environ.get("STREAMFRAME_INSTAGRAM_APP_ID")),
                app_secret=_text(environ.get("STREAMFRAME_INSTAGRAM_APP_SECRET")),
                api_version=_text(environ.get("STREAMFRAME_INSTAGRAM_API_VERSION")) or "v23.0",
                share_to_feed=_flag(environ.get("STREAMFRAME_INSTAGRAM_SHARE_TO_FEED"), True),
            ),
            tiktok=TikTokSettings(
                client_key=_text(environ.get("STREAMFRAME_TIKTOK_CLIENT_KEY")),
                client_secret=_text(environ.get("STREAMFRAME_TIKTOK_CLIENT_SECRET")),
            ),
            media=media,
            http_timeout=float(_number(environ.get("STREAMFRAME_PUBLISH_HTTP_TIMEOUT"), 120)),
            poll_timeout=float(_number(environ.get("STREAMFRAME_PUBLISH_POLL_TIMEOUT"), 300)),
        )

    def problems(self):
        """Return (platform, message) pairs for configuration that cannot post.

        ``doctor`` prints these instead of raising, so one missing platform does
        not hide the state of the other two.
        """
        found = []
        if "youtube" in self.platforms and not self.youtube.configured:
            found.append(("youtube", "Set STREAMFRAME_YOUTUBE_CLIENT_ID and "
                                     "STREAMFRAME_YOUTUBE_CLIENT_SECRET"))
        if "instagram" in self.platforms:
            if not self.instagram.configured:
                found.append(("instagram", "Set STREAMFRAME_INSTAGRAM_APP_ID and "
                                          "STREAMFRAME_INSTAGRAM_APP_SECRET"))
            if not self.media.configured:
                found.append(("instagram", "Instagram needs a public media URL: set "
                                           "STREAMFRAME_MEDIA_HOST plus its public base URL"))
        if "tiktok" in self.platforms and not self.tiktok.configured:
            found.append(("tiktok", "Set STREAMFRAME_TIKTOK_CLIENT_KEY and "
                                    "STREAMFRAME_TIKTOK_CLIENT_SECRET"))
        return found
