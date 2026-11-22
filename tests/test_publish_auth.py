"""Instagram authorization exchanges, without any network."""

import time
from dataclasses import replace

from streamframe.publish import auth
from streamframe.publish.settings import InstagramSettings, Settings


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class Session:
    def __init__(self, *, post_response=None, get_responses=()):
        self.post_response = post_response
        self.get_responses = list(get_responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.post_response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.get_responses.pop(0)


def settings_for(tmp_path, login="instagram"):
    return Settings(data_dir=tmp_path, state_dir=tmp_path / "publish", env_file=None,
                    platforms=("instagram",),
                    instagram=InstagramSettings(app_id="app", app_secret="secret",
                                                login=login))


def test_a_short_lived_token_is_exchanged_for_a_sixty_day_token(tmp_path):
    session = Session(get_responses=[
        Response(payload={"access_token": "long", "expires_in": 5184000}),
    ], post_response=Response(payload={"access_token": "short", "user_id": "42"}))

    record = auth._exchange(settings_for(tmp_path), "instagram", "code",
                            "https://example.test/cb", None, session)

    assert session.calls[0][:2] == ("POST", auth.INSTAGRAM_TOKEN)
    # Exchanging uses /access_token, not /refresh_access_token.
    assert session.calls[1][:2] == ("GET", auth.INSTAGRAM_EXCHANGE)
    assert session.calls[1][2]["params"]["grant_type"] == "ig_exchange_token"
    assert record["access_token"] == "long"
    assert record["user_id"] == "42"
    assert record["expires_at"] > time.time() + 50 * 86400


def test_facebook_login_finds_the_page_and_its_instagram_account(tmp_path):
    session = Session(get_responses=[
        Response(payload={"access_token": "short"}),
        Response(payload={"access_token": "long", "expires_in": 5184000}),
        Response(payload={"data": [
            {"id": "page0", "name": "No Instagram here"},
            {"id": "page1", "name": "Clips", "access_token": "pagetoken",
             "instagram_business_account": {"id": "ig42"}},
        ]}),
    ])

    record = auth._exchange(settings_for(tmp_path, login="facebook"), "instagram", "code",
                            "https://example.test/cb", None, session)

    graph = "https://graph.facebook.com/v23.0"
    assert session.calls[0][:2] == ("GET", f"{graph}/oauth/access_token")
    assert session.calls[1][2]["params"]["grant_type"] == "fb_exchange_token"
    assert session.calls[2][:2] == ("GET", f"{graph}/me/accounts")
    assert record["user_id"] == "ig42"
    assert record["access_token"] == "pagetoken"
    assert record["page_id"] == "page1"
    # Page tokens from a long-lived user token have no scheduled expiry.
    assert record["expires_at"] is None


def test_the_configured_redirect_uri_is_used_when_none_is_passed(tmp_path):
    settings = replace(settings_for(tmp_path, login="facebook"),
                       publish_redirect_uri="https://example.test/cb")

    assert auth.redirect_for(settings, "instagram") == "https://example.test/cb"
    assert auth.redirect_for(settings, "instagram", "https://other.test/x") == "https://other.test/x"
    assert auth.redirect_for(settings, "youtube").startswith("http://127.0.0.1:")
    assert auth.redirect_for(settings_for(tmp_path), "instagram") == ""


def test_a_configured_page_id_skips_discovery(tmp_path):
    session = Session(get_responses=[
        Response(payload={"access_token": "short"}),
        Response(payload={"access_token": "long"}),
        Response(payload={"id": "999", "name": "Lobby Crew", "access_token": "pagetoken",
                          "instagram_business_account": {"id": "ig42"}}),
    ])
    settings = settings_for(tmp_path, login="facebook")
    settings = replace(settings, instagram=replace(settings.instagram, page_id="999"))

    record = auth._exchange(settings, "instagram", "code",
                            "https://example.test/cb", None, session)

    api = settings.instagram.api_version
    assert session.calls[2][:2] == ("GET", f"https://graph.facebook.com/{api}/999")
    assert record["user_id"] == "ig42"
    assert record["access_token"] == "pagetoken"


def test_facebook_login_explains_a_page_without_instagram(tmp_path):
    session = Session(get_responses=[
        Response(payload={"access_token": "short"}),
        Response(payload={"access_token": "long"}),
        Response(payload={"data": [{"id": "page0", "name": "No Instagram here"}]}),
    ])

    with_error = None
    try:
        auth._exchange(settings_for(tmp_path, login="facebook"), "instagram", "code",
                       "https://example.test/cb", None, session)
    except auth.AuthError as exc:
        with_error = str(exc)
    assert with_error and "Link the Instagram account" in with_error
    assert "No Instagram here" in with_error


def test_facebook_login_explains_an_empty_page_list(tmp_path):
    session = Session(get_responses=[
        Response(payload={"access_token": "short"}),
        Response(payload={"access_token": "long"}),
        Response(payload={"data": []}),
        Response(payload={"id": "1", "name": "Wrong Profile"}),
    ])

    with_error = None
    try:
        auth._exchange(settings_for(tmp_path, login="facebook"), "instagram", "code",
                       "https://example.test/cb", None, session)
    except auth.AuthError as exc:
        with_error = str(exc)
    assert with_error and "grant access to your Page" in with_error
    assert "authorized as Wrong Profile" in with_error
