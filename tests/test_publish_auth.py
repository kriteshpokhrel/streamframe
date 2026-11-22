"""The short-lived to long-lived Instagram exchange, without any network."""

import time

from streamframe.publish import auth
from streamframe.publish.settings import InstagramSettings, Settings


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class Session:
    def __init__(self, *, post_response, get_response):
        self.post_response = post_response
        self.get_response = get_response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.post_response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.get_response


def settings_for(tmp_path):
    return Settings(data_dir=tmp_path, state_dir=tmp_path / "publish", env_file=None,
                    platforms=("instagram",),
                    instagram=InstagramSettings(app_id="app", app_secret="secret"))


def test_a_short_lived_token_is_exchanged_for_a_sixty_day_token(tmp_path):
    session = Session(
        post_response=Response(payload={"access_token": "short", "user_id": "42"}),
        get_response=Response(payload={"access_token": "long", "expires_in": 5184000}),
    )

    record = auth._exchange(settings_for(tmp_path), "instagram", "code",
                            "https://example.test/cb", None, session)

    assert session.calls[0][:2] == ("POST", auth.INSTAGRAM_TOKEN)
    # Exchanging uses /access_token, not /refresh_access_token.
    assert session.calls[1][:2] == ("GET", auth.INSTAGRAM_EXCHANGE)
    assert session.calls[1][2]["params"]["grant_type"] == "ig_exchange_token"
    assert record["access_token"] == "long"
    assert record["user_id"] == "42"
    assert record["expires_at"] > time.time() + 50 * 86400
