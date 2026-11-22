"""Idempotent orchestration across the three platform adapters.

The ledger is claimed before any network call, so a retried cron run, a
double-clicked command, or two processes racing publish a clip exactly once.

Nothing here renders, edits, or deletes a clip. Publishing is a separate step,
as the repository's capture boundary requires.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .ledger import CLAIMED, DONE, IN_FLIGHT, RETRY_EXHAUSTED, Ledger
from .media import build_host
from .model import NEEDS_MANUAL, PUBLISHED, Post, PublishError
from .settings import PLATFORMS, ConfigError, Settings

PLANNED = "planned"
SKIPPED = "skipped"
FAILED = "failed"

_SECRET = re.compile(r"(?i)(access_token|refresh_token|client_secret|code)=[^&\s]+")


@dataclass(frozen=True)
class Outcome:
    platform: str
    action: str
    status: str | None = None
    platform_id: str | None = None
    url: str | None = None
    detail: str | None = None


class Publisher:
    def __init__(self, settings: Settings, *, ledger=None, clients=None, media_host=None):
        self.settings = settings
        self.ledger = ledger or Ledger(settings.state_dir / "publications.sqlite3")
        self._clients = dict(clients or {})
        self._media_host = media_host

    # -- construction ---------------------------------------------------

    @property
    def media_host(self):
        if self._media_host is None:
            self._media_host = build_host(self.settings.media, timeout=self.settings.http_timeout)
        return self._media_host

    def client(self, platform):
        if platform in self._clients:
            return self._clients[platform]
        if platform == "youtube":
            from .youtube import YouTubeClient
            client = YouTubeClient(self.settings)
        elif platform == "instagram":
            from .instagram import InstagramClient
            client = InstagramClient(self.settings, self.media_host)
        elif platform == "tiktok":
            from .tiktok import TikTokClient
            client = TikTokClient(self.settings)
        else:
            raise ConfigError(f"Unknown platform {platform}")
        self._clients[platform] = client
        return client

    # -- publishing -----------------------------------------------------

    def publish(self, post: Post, *, platforms=None, dry_run=False):
        selected = self._select(platforms)
        if not dry_run:
            self.ledger.recover_stale()
        outcomes = []
        for platform in selected:
            outcomes.append(self._one(post, platform, dry_run=dry_run))
        return outcomes

    def _one(self, post, platform, *, dry_run):
        if dry_run:
            existing = self.ledger.status_for(post.clip_key).get(platform)
            if existing is None or existing.status == "failed":
                return Outcome(platform, PLANNED, detail="would publish")
            return Outcome(platform, SKIPPED, status=existing.status,
                           detail=f"already {existing.status}")

        decision = self.ledger.begin(post.clip_key, platform)
        if not decision.should_publish:
            reasons = {
                DONE: decision.record.status if decision.record else "done",
                IN_FLIGHT: "another run is publishing this clip",
                RETRY_EXHAUSTED: "retry limit reached; use publish retry",
            }
            return Outcome(platform, SKIPPED, status=decision.record.status
                           if decision.record else None,
                           detail=reasons[decision.action])

        try:
            result = self._dispatch(platform, post)
        except PublishError as exc:
            self.ledger.fail(post.clip_key, platform, _sanitize(str(exc)))
            return Outcome(platform, FAILED, detail=_sanitize(str(exc)))
        except Exception as exc:  # adapters must never take the run down
            message = f"{type(exc).__name__}"
            self.ledger.fail(post.clip_key, platform, message)
            return Outcome(platform, FAILED, detail=message)

        status = "published" if result.status == PUBLISHED else "needs_manual"
        self.ledger.finish(post.clip_key, platform, status,
                           platform_id=result.platform_id, url=result.url,
                           detail=result.detail)
        return Outcome(platform, status, status=status, platform_id=result.platform_id,
                       url=result.url, detail=result.detail)

    def _dispatch(self, platform, post):
        client = self.client(platform)
        if platform == "youtube":
            return client.upload(post)
        if platform == "tiktok":
            return client.upload_draft(post)
        return client.publish(post)

    def _select(self, platforms):
        selected = tuple(platforms) if platforms else self.settings.platforms
        unknown = sorted(set(selected) - set(PLATFORMS))
        if unknown:
            raise ConfigError(f"Unknown platforms: {', '.join(unknown)}")
        return selected

    # -- reporting ------------------------------------------------------

    def status(self, clip_key=None):
        return self.ledger.records(clip_key)


def _sanitize(message):
    """Keep failures useful without ever echoing a credential."""
    text = _SECRET.sub(r"\1=[redacted]", str(message))
    return text[:300]


__all__ = ["Outcome", "Publisher", "CLAIMED", "NEEDS_MANUAL", "PLANNED", "SKIPPED", "FAILED"]
