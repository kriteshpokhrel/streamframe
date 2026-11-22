from dataclasses import replace
import json
from pathlib import Path

import pytest

from streamframe.publish.instagram import InstagramClient
from streamframe.publish.media import MediaError
from streamframe.publish.model import Post, PublishError
from streamframe.publish.settings import (
    InstagramSettings,
    MediaSettings,
    Settings,
    TikTokSettings,
    YouTubeSettings,
)
from streamframe.publish.tiktok import TikTokClient, chunk_plan
from streamframe.publish.tokens import CredentialStore
from streamframe.publish.youtube import YouTubeClient

FUTURE = 4_000_000_000


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class Routed:
    """Deterministic HTTP double: first matching (method, url fragment) wins."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for expected_method, fragment, response in self.routes:
            if expected_method == method and fragment in url:
                return response
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._call("PUT", url, **kwargs)


def make_settings(tmp_path, **overrides):
    settings = Settings(
        data_dir=tmp_path,
        state_dir=tmp_path / "publish",
        env_file=None,
        platforms=("youtube", "instagram", "tiktok"),
        youtube=YouTubeSettings(client_id="yt-id", client_secret="yt-secret"),
        instagram=InstagramSettings(app_id="ig-app", app_secret="ig-secret"),
        tiktok=TikTokSettings(client_key="tt-key", client_secret="tt-secret"),
        media=MediaSettings(host="local", public_base_url="https://clips.test",
                            local_dir=tmp_path / "public"),
        poll_timeout=5,
    )
    return replace(settings, **overrides) if overrides else settings


def store_credentials(settings, **records):
    store = CredentialStore(settings.state_dir / "credentials.json")
    store.save(records)
    return store


def clip(tmp_path, size=1024):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * size)
    return Post(path=path, clip_key="clip1", title="A good hook",
                caption="caption", description="desc", tags=("one", "two"))


# -- YouTube ------------------------------------------------------------

def test_youtube_upload_sends_metadata_then_bytes(tmp_path):
    settings = make_settings(tmp_path)
    store_credentials(settings, youtube={"refresh_token": "rt", "access_token": "at",
                                         "expires_at": FUTURE})
    session = Routed([
        ("POST", "upload/youtube/v3/videos", Response(200, headers={"Location": "https://up.test/session"})),
        ("PUT", "https://up.test/session", Response(200, {"id": "vid123"})),
    ])

    result = YouTubeClient(settings, session=session).upload(clip(tmp_path))

    assert result.platform_id == "vid123"
    assert result.url == "https://www.youtube.com/shorts/vid123"
    create = session.calls[0][2]
    metadata = json.loads(create["data"])
    assert metadata["status"]["privacyStatus"] == "public"
    assert metadata["snippet"]["categoryId"] == "24"
    assert "#Shorts" in metadata["snippet"]["description"]
    assert session.calls[0][2]["headers"]["X-Upload-Content-Length"] == "1024"


def test_youtube_rejects_a_failed_session(tmp_path):
    settings = make_settings(tmp_path)
    store_credentials(settings, youtube={"access_token": "at", "expires_at": FUTURE})
    session = Routed([("POST", "upload/youtube/v3/videos", Response(403))])
    with pytest.raises(PublishError, match="HTTP 403"):
        YouTubeClient(settings, session=session).upload(clip(tmp_path))


# -- Instagram ----------------------------------------------------------

class FakeHost:
    def __init__(self):
        self.puts, self.removals = [], []

    def put(self, path, key, *, content_type="video/mp4"):
        self.puts.append((key, content_type))

        class Hosted:
            pass

        hosted = Hosted()
        hosted.key = key + ".mp4"
        hosted.url = f"https://clips.test/{key}.mp4"
        return hosted

    def remove(self, key):
        self.removals.append(key)


def test_instagram_creates_a_reel_container_then_publishes(tmp_path):
    settings = make_settings(tmp_path)
    store_credentials(settings, instagram={"access_token": "ig-token", "user_id": "42",
                                           "expires_at": FUTURE})
    session = Routed([
        ("POST", "/42/media_publish", Response(200, {"id": "media1"})),
        ("POST", "/42/media", Response(200, {"id": "container1"})),
        ("GET", "/container1", Response(200, {"status_code": "FINISHED"})),
        ("GET", "/media1", Response(200, {"permalink": "https://instagram.com/reel/media1/"})),
    ])
    host = FakeHost()

    result = InstagramClient(settings, host, session=session,
                             sleep=lambda _: None).publish(clip(tmp_path))

    assert result.platform == "instagram"
    assert result.platform_id == "media1"
    assert result.url == "https://instagram.com/reel/media1/"
    create = session.calls[0][2]["data"]
    assert create["media_type"] == "REELS"
    assert create["video_url"] == "https://clips.test/clip1.mp4"
    assert create["caption"] == "caption"
    assert host.removals == ["clip1.mp4"]


def test_instagram_stops_when_the_container_errors(tmp_path):
    settings = make_settings(tmp_path)
    store_credentials(settings, instagram={"access_token": "t", "user_id": "42",
                                           "expires_at": FUTURE})
    session = Routed([
        ("POST", "/42/media", Response(200, {"id": "c"})),
        ("GET", "/c", Response(200, {"status_code": "ERROR"})),
    ])
    with pytest.raises(PublishError, match="ended ERROR"):
        InstagramClient(settings, FakeHost(), session=session,
                        sleep=lambda _: None).publish(clip(tmp_path))


def test_instagram_uses_the_facebook_host_for_page_linked_accounts(tmp_path):
    settings = make_settings(tmp_path)
    page_linked = replace(settings, instagram=replace(settings.instagram, login="facebook"))
    assert InstagramClient(page_linked, FakeHost()).base == "https://graph.facebook.com/v23.0"
    assert InstagramClient(settings, FakeHost()).base == "https://graph.instagram.com/v23.0"


def test_instagram_requires_a_public_host(tmp_path):
    settings = make_settings(tmp_path)
    store_credentials(settings, instagram={"access_token": "t", "user_id": "42",
                                           "expires_at": FUTURE})

    class NoHost:
        def put(self, path, key, *, content_type="video/mp4"):
            raise MediaError("nope")

        def remove(self, key):
            pass

    with pytest.raises(PublishError, match="nope"):
        InstagramClient(settings, NoHost(), session=Routed([])).publish(clip(tmp_path))


# -- TikTok -------------------------------------------------------------

@pytest.mark.parametrize("size,chunks", [
    (1, 1),
    (4 * 1024 * 1024, 1),
    (10 * 1024 * 1024, 1),
    (11 * 1024 * 1024, 1),
    (21 * 1024 * 1024, 2),
    (65 * 1024 * 1024, 6),
])
def test_chunk_plan_respects_tiktok_media_rules(size, chunks):
    chunk_size, total = chunk_plan(size)
    assert total == chunks
    assert 1 <= total <= 1000
    if total > 1:
        assert 5 * 1024 * 1024 <= chunk_size <= 64 * 1024 * 1024


def test_chunk_plan_covers_every_byte(tmp_path):
    size = 21 * 1024 * 1024
    chunk_size, total = chunk_plan(size)
    if total == 1:
        assert chunk_size == size
        return
    remainder = size - chunk_size * (total - 1)
    assert remainder > 0
    # TikTok lets the final chunk absorb the remainder, up to 128 MB.
    assert remainder <= 128 * 1024 * 1024


def test_tiktok_delivers_a_draft_and_reports_it_needs_a_human(tmp_path):
    settings = make_settings(tmp_path)
    store_credentials(settings, tiktok={"access_token": "tt-token", "refresh_token": "r",
                                        "expires_at": FUTURE})
    session = Routed([
        ("POST", "inbox/video/init", Response(200, {
            "data": {"publish_id": "p1", "upload_url": "https://upload.test/video?t=1"},
            "error": {"code": "ok"},
        })),
        ("PUT", "upload.test", Response(200)),
    ])

    result = TikTokClient(settings, session=session).upload_draft(clip(tmp_path))

    assert result.status == "needs_manual"
    assert result.platform_id == "p1"
    init = session.calls[0][2]["json"]["source_info"]
    assert init["source"] == "FILE_UPLOAD"
    assert init["video_size"] == 1024
    assert session.calls[1][2]["headers"]["Content-Range"] == "bytes 0-1023/1024"


def test_tiktok_surfaces_api_errors(tmp_path):
    settings = make_settings(tmp_path)
    store_credentials(settings, tiktok={"access_token": "t", "expires_at": FUTURE})
    session = Routed([("POST", "inbox/video/init", Response(403, {
        "error": {"code": "spam_risk_too_many_pending_share", "message": "too many drafts"},
    }))])
    with pytest.raises(PublishError, match="spam_risk_too_many_pending_share"):
        TikTokClient(settings, session=session).upload_draft(clip(tmp_path))
