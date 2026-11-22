"""Value types shared by the publishing adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PUBLISHED = "published"
NEEDS_MANUAL = "needs_manual"


class PublishError(RuntimeError):
    """A platform rejected the upload or could not be reached."""

    def __init__(self, message, *, retryable=True, status=None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class Post:
    """One clip and the metadata each platform wants.

    ``caption`` is the shared text. Per-platform overrides exist because the
    first line is what survives truncation, and it is rarely the same sentence
    on TikTok and on YouTube.
    """

    path: Path
    clip_key: str
    title: str = ""
    caption: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    captions: dict = field(default_factory=dict)
    titles: dict = field(default_factory=dict)
    privacy: str | None = None

    def caption_for(self, platform):
        return self.captions.get(platform) or self.caption or self.title

    def title_for(self, platform):
        return self.titles.get(platform) or self.title or self.caption


@dataclass(frozen=True)
class PublishResult:
    platform: str
    status: str
    platform_id: str | None = None
    url: str | None = None
    detail: str | None = None
