"""Per-machine OAuth token store and refreshers.

Client IDs and secrets come from ``.env``. The tokens that grant posting
access do not: they are written to ``credentials.json`` with mode 0600 and
refreshed in place. That keeps a shared ``.env`` safe to hand to a teammate
while each machine authorizes its own accounts.

Refresh rules differ per platform and are encoded here rather than in the
clients, so a cron job can call ``refresh_all`` without publishing anything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

import requests

from .settings import ConfigError, Settings

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
INSTAGRAM_REFRESH = "https://graph.instagram.com/refresh_access_token"
TIKTOK_TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"

REFRESH_SKEW = 300


class TokenError(RuntimeError):
    """Authorization is missing, expired, or could not be refreshed."""


class CredentialStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise TokenError("Credential store is unreadable; run publish auth again") from exc

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def get(self, platform):
        record = self.load().get(platform)
        if not record:
            raise TokenError(f"No {platform} credentials; run: streamframe publish auth {platform}")
        return record

    def put(self, platform, record):
        data = self.load()
        data[platform] = record
        self.save(data)


def store_for(settings: Settings):
    return CredentialStore(settings.state_dir / "credentials.json")


def expired(record, *, now=None, skew=REFRESH_SKEW):
    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    return (time.time() if now is None else now) >= expires_at - skew


def access_token(settings: Settings, platform, *, session=None, now=None):
    """Return a usable access token, refreshing and persisting when needed."""
    session = session or requests
    store = store_for(settings)
    record = store.get(platform)
    if platform == "youtube":
        if record.get("access_token") and not expired(record, now=now):
            return record["access_token"]
        return _refresh_youtube(settings, store, record, session)
    if platform == "instagram":
        if not expired(record, now=now):
            return record["access_token"]
        return _refresh_instagram(settings, store, record, session)
    if platform == "tiktok":
        if not expired(record, now=now):
            return record["access_token"]
        return _refresh_tiktok(settings, store, record, session)
    raise TokenError(f"Unknown platform {platform}")


def _refresh_youtube(settings, store, record, session):
    if not settings.youtube.configured:
        raise ConfigError("YouTube client credentials are not configured")
    refresh_token = record.get("refresh_token")
    if not refresh_token:
        raise TokenError("YouTube refresh token is missing; run publish auth youtube")
    response = session.post(TOKEN_ENDPOINT, timeout=30, data={
        "client_id": settings.youtube.client_id,
        "client_secret": settings.youtube.client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    if response.status_code != 200:
        raise TokenError("YouTube token refresh failed; run publish auth youtube")
    payload = response.json()
    record = {**record,
              "access_token": payload["access_token"],
              "expires_at": time.time() + int(payload.get("expires_in", 3600))}
    store.put("youtube", record)
    return record["access_token"]


def _refresh_instagram(settings, store, record, session):
    token = record.get("access_token")
    if not token:
        raise TokenError("Instagram token is missing; run publish auth instagram")
    response = session.get(INSTAGRAM_REFRESH, timeout=30, params={
        "grant_type": "ig_refresh_token",
        "access_token": token,
    })
    if response.status_code != 200:
        raise TokenError("Instagram token refresh failed; run publish auth instagram")
    payload = response.json()
    record = {**record,
              "access_token": payload["access_token"],
              "expires_at": time.time() + int(payload.get("expires_in", 5184000))}
    store.put("instagram", record)
    return record["access_token"]


def _refresh_tiktok(settings, store, record, session):
    if not settings.tiktok.configured:
        raise ConfigError("TikTok client credentials are not configured")
    refresh_token = record.get("refresh_token")
    if not refresh_token:
        raise TokenError("TikTok refresh token is missing; run publish auth tiktok")
    response = session.post(TIKTOK_TOKEN_ENDPOINT, timeout=30, data={
        "client_key": settings.tiktok.client_key,
        "client_secret": settings.tiktok.client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    if response.status_code != 200:
        raise TokenError("TikTok token refresh failed; run publish auth tiktok")
    payload = response.json()
    record = {**record,
              "access_token": payload["access_token"],
              "refresh_token": payload.get("refresh_token", refresh_token),
              "expires_at": time.time() + int(payload.get("expires_in", 86400)),
              "open_id": payload.get("open_id", record.get("open_id"))}
    store.put("tiktok", record)
    return record["access_token"]


def refresh_all(settings: Settings, *, session=None):
    """Refresh every authorized platform; used by a monthly cron entry."""
    results = {}
    for platform in settings.platforms:
        try:
            access_token(settings, platform, session=session, now=time.time() + 10**9)
            results[platform] = "refreshed"
        except (TokenError, ConfigError, KeyError, ValueError) as exc:
            results[platform] = f"skipped: {type(exc).__name__}"
    return results
