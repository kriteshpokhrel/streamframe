"""TikTok draft delivery through the inbox (``video.upload``) scope.

This is the free, audited-app-free route: the clip lands in the creator's
TikTok inbox and a human finishes it in the app. It is deliberately not
Direct Post — that scope needs an audit that excludes a utility uploading to
accounts you manage.

Consequences encoded here:

* the result is ``needs_manual``, not ``published``;
* no caption is sent, because TikTok collects it in the app;
* at most five drafts may be pending in 24 hours, so the ledger must not
  re-upload a clip that already succeeded.
"""

from __future__ import annotations

from pathlib import Path

import requests

from .model import Post, PublishResult, PublishError, NEEDS_MANUAL
from .settings import Settings
from .tokens import access_token

INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
DEFAULT_CHUNK = 10 * 1024 * 1024
MAX_CHUNKS = 1000


def chunk_plan(size):
    """Return (chunk_size, chunk_count) that satisfies TikTok's media rules.

    Every chunk except the last must be between 5 MB and 64 MB, and files under
    5 MB must be sent whole. The final chunk absorbs any remainder.
    """
    if size <= 0:
        raise PublishError("TikTok cannot upload an empty file", retryable=False)
    if size < MIN_CHUNK:
        return size, 1
    chunk_size = DEFAULT_CHUNK
    total = size // chunk_size
    if total < 1:
        return size, 1
    if total > MAX_CHUNKS:
        chunk_size = max(MIN_CHUNK, -(-size // MAX_CHUNKS))
        total = size // chunk_size
    return min(chunk_size, MAX_CHUNK), max(1, total)


class TikTokClient:
    def __init__(self, settings: Settings, *, session=None):
        self.settings = settings
        self.session = session or requests
        self.timeout = settings.http_timeout

    def upload_draft(self, post: Post) -> PublishResult:
        token = access_token(self.settings, "tiktok", session=self.session)
        path = Path(post.path)
        size = path.stat().st_size
        chunk_size, total = chunk_plan(size)
        response = self.session.post(
            INIT_URL,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=UTF-8"},
            json={"source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk_size,
                "total_chunk_count": total,
            }},
            timeout=self.timeout,
        )
        payload = _unwrap(response, "TikTok rejected the upload request")
        publish_id = payload.get("publish_id")
        upload_url = payload.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError("TikTok returned no upload session", retryable=False)

        self._put_chunks(_absolute(upload_url), path, size, chunk_size, total)
        return PublishResult(
            platform="tiktok",
            status=NEEDS_MANUAL,
            platform_id=publish_id,
            detail="Draft is in the TikTok inbox; finish it in the app within 24 hours",
        )

    def _put_chunks(self, upload_url, path, size, chunk_size, total):
        offset = 0
        with path.open("rb") as handle:
            for index in range(total):
                length = chunk_size if index < total - 1 else size - offset
                handle.seek(offset)
                blob = handle.read(length)
                if len(blob) != length:
                    raise PublishError("TikTok upload read less data than expected",
                                       retryable=False)
                last = offset + length - 1
                response = self.session.put(
                    upload_url,
                    data=blob,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(length),
                        "Content-Range": f"bytes {offset}-{last}/{size}",
                    },
                    timeout=self.timeout,
                )
                if response.status_code not in {200, 201, 206}:
                    raise PublishError(
                        f"TikTok rejected chunk {index + 1} of {total} "
                        f"(HTTP {response.status_code})",
                        status=response.status_code,
                    )
                offset += length


def _absolute(url):
    return url if url.startswith("https://") else f"https://{url.lstrip('/')}"


def _unwrap(response, message):
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if response.status_code >= 400 or (code and code != "ok"):
        reason = error.get("message") if isinstance(error, dict) else None
        raise PublishError(
            f"{message}: {code or 'HTTP ' + str(response.status_code)}"
            + (f" ({reason})" if reason else ""),
            retryable=response.status_code in {429, 500, 502, 503, 504},
            status=response.status_code,
        )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise PublishError(message, retryable=False, status=response.status_code)
    return data
