"""YouTube Shorts upload through the Data API.

One resumable ``videos.insert`` call. Shorts are not a separate API surface:
the same request publishes a Short when the file is vertical and at most three
minutes. ``#Shorts`` is added to the description because discovery still reads
it, not because the upload needs it.

The default project quota is 100 uploads per day, billed to its own bucket, so
ten clips a day needs no extension.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

from .model import Post, PublishResult, PublishError, PUBLISHED
from .settings import Settings
from .tokens import access_token

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 5000


class YouTubeClient:
    def __init__(self, settings: Settings, *, session=None):
        self.settings = settings
        self.session = session or requests
        self.timeout = settings.http_timeout

    def upload(self, post: Post) -> PublishResult:
        token = access_token(self.settings, "youtube", session=self.session)
        path = Path(post.path)
        privacy = post.privacy or self.settings.youtube.privacy
        if privacy not in {"public", "unlisted", "private"}:
            raise PublishError(f"Unknown privacy {privacy!r}", retryable=False)
        metadata = {
            "snippet": {
                "title": _shorten(post.title_for("youtube"), TITLE_LIMIT),
                "description": _description(post),
                "tags": list(post.tags)[:15],
                "categoryId": str(self.settings.youtube.category_id),
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": self.settings.youtube.made_for_kids,
            },
        }
        response = self.session.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(path.stat().st_size),
                "X-Upload-Content-Type": "video/mp4",
            },
            data=json.dumps(metadata).encode("utf-8"),
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise PublishError(f"YouTube rejected the upload session (HTTP {response.status_code})",
                               status=response.status_code)
        location = response.headers.get("Location")
        if not location:
            raise PublishError("YouTube did not return an upload session")

        with path.open("rb") as body:
            uploaded = self.session.put(
                location,
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "video/mp4",
                    "Content-Length": str(path.stat().st_size),
                },
                timeout=self.timeout,
            )
        if uploaded.status_code not in {200, 201}:
            raise PublishError(f"YouTube rejected the video (HTTP {uploaded.status_code})",
                               status=uploaded.status_code)
        try:
            video_id = uploaded.json()["id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PublishError("YouTube returned no video id") from exc
        return PublishResult(
            platform="youtube",
            status=PUBLISHED,
            platform_id=video_id,
            url=f"https://www.youtube.com/shorts/{video_id}",
            detail=f"privacy={privacy}",
        )


def _shorten(text, limit):
    text = (text or "Untitled clip").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _description(post):
    parts = [post.description or post.caption or ""]
    text = "\n\n".join(part for part in parts if part).strip()
    if "#shorts" not in text.lower():
        text = f"{text}\n\n#Shorts".strip()
    return _shorten(text, DESCRIPTION_LIMIT)
