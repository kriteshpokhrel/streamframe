"""Instagram Reels publishing.

Two calls: create a media container from a public URL, then publish it. Meta
fetches the file itself, so the clip must already be reachable — that is the
one place publishing needs hosting, and it is why ``media`` exists.

A container expires after 24 hours, so it is created immediately before
publishing rather than when the clip was scheduled.
"""

from __future__ import annotations

import time

import requests

from .media import MediaError
from .model import Post, PublishResult, PublishError, PUBLISHED
from .settings import Settings
from .tokens import access_token, store_for

CAPTION_LIMIT = 2200
FINAL_STATES = {"FINISHED", "PUBLISHED", "ERROR", "EXPIRED"}


class InstagramClient:
    def __init__(self, settings: Settings, media_host, *, session=None, sleep=time.sleep):
        self.settings = settings
        self.media_host = media_host
        self.session = session or requests
        self.sleep = sleep
        self.timeout = settings.http_timeout

    @property
    def base(self):
        version = self.settings.instagram.api_version
        host = ("graph.facebook.com" if self.settings.instagram.login == "facebook"
                else "graph.instagram.com")
        return f"https://{host}/{version}"

    def publish(self, post: Post) -> PublishResult:
        credentials = store_for(self.settings).get("instagram")
        user_id = credentials.get("user_id")
        if not user_id:
            raise PublishError("Instagram user id is missing; run publish auth instagram",
                               retryable=False)
        token = access_token(self.settings, "instagram", session=self.session)

        try:
            hosted = self.media_host.put(post.path, key=post.clip_key)
        except MediaError as exc:
            raise PublishError(str(exc), retryable=False) from exc
        try:
            container = self._create_container(token, user_id, hosted.url, post)
            self._wait_ready(token, container)
            media_id = self._publish_container(token, user_id, container)
        finally:
            self.media_host.remove(hosted.key)

        return PublishResult(
            platform="instagram",
            status=PUBLISHED,
            platform_id=media_id,
            url=self._permalink(token, media_id),
            detail="reel",
        )

    def _create_container(self, token, user_id, video_url, post):
        response = self.session.post(
            f"{self.base}/{user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": _caption(post),
                "share_to_feed": "true" if self.settings.instagram.share_to_feed else "false",
                "access_token": token,
            },
            timeout=self.timeout,
        )
        payload = _json(response, "Instagram rejected the upload")
        container = payload.get("id")
        if not container:
            raise PublishError("Instagram returned no container id",
                               retryable=False, status=response.status_code)
        return container

    def _wait_ready(self, token, container):
        deadline = time.monotonic() + self.settings.poll_timeout
        delay = 2.0
        while True:
            response = self.session.get(
                f"{self.base}/{container}",
                params={"fields": "status_code", "access_token": token},
                timeout=self.timeout,
            )
            payload = _json(response, "Instagram could not report container status")
            status = payload.get("status_code")
            if status in FINAL_STATES:
                if status in {"FINISHED", "PUBLISHED"}:
                    return
                raise PublishError(f"Instagram container ended {status}",
                                   retryable=status != "ERROR",
                                   status=response.status_code)
            if time.monotonic() >= deadline:
                raise PublishError("Instagram did not finish processing the reel in time",
                                   status=response.status_code)
            self.sleep(delay)
            delay = min(delay * 1.5, 10.0)

    def _publish_container(self, token, user_id, container):
        response = self.session.post(
            f"{self.base}/{user_id}/media_publish",
            data={"creation_id": container, "access_token": token},
            timeout=self.timeout,
        )
        payload = _json(response, "Instagram rejected the publish")
        media_id = payload.get("id")
        if not media_id:
            raise PublishError("Instagram returned no media id",
                               retryable=False, status=response.status_code)
        return media_id

    def _permalink(self, token, media_id):
        try:
            response = self.session.get(
                f"{self.base}/{media_id}",
                params={"fields": "permalink", "access_token": token},
                timeout=self.timeout,
            )
            if response.status_code == 200:
                return response.json().get("permalink")
        except (requests.RequestException, ValueError):
            pass
        return None


def _caption(post):
    text = (post.caption_for("instagram") or "").strip()
    return text if len(text) <= CAPTION_LIMIT else text[:CAPTION_LIMIT - 1].rstrip() + "…"


def _json(response, message):
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        error = payload.get("error") if isinstance(payload, dict) else None
        detail = error.get("message") if isinstance(error, dict) else None
        suffix = f": {detail}" if detail else ""
        raise PublishError(f"{message} (HTTP {response.status_code}){suffix}",
                           retryable=response.status_code in {429, 500, 502, 503, 504},
                           status=response.status_code)
    if not isinstance(payload, dict):
        raise PublishError(message, retryable=False, status=response.status_code)
    return payload
