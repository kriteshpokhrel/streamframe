"""One-time OAuth authorization for each platform.

Google accepts a ``http://127.0.0.1`` redirect, so YouTube uses a loopback
listener. Meta and TikTok require a registered HTTPS redirect, so those use a
paste-the-code flow against whatever callback the app already has. Both paths
write the same credential record, and nothing here publishes anything.

Redirect URIs default to the ones an app dashboard usually registers. Override
them with ``--redirect-uri`` when your app uses something else.
"""

from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from .settings import ConfigError, Settings
from .tokens import INSTAGRAM_REFRESH, TIKTOK_TOKEN_ENDPOINT, TOKEN_ENDPOINT, store_for

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
INSTAGRAM_AUTHORIZE = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_TOKEN = "https://api.instagram.com/oauth/access_token"
# Short-lived -> long-lived uses /access_token; refreshing afterwards uses
# /refresh_access_token. Using one for both breaks the 60-day token.
INSTAGRAM_EXCHANGE = "https://graph.instagram.com/access_token"
INSTAGRAM_SCOPES = "instagram_business_basic,instagram_business_content_publish"
# Page-linked (Facebook Login) variant. New Meta apps only offer this or Unified.
FACEBOOK_SCOPES = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement"
TIKTOK_AUTHORIZE = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_SCOPES = "video.upload,user.info.basic"
DEFAULT_PORT = 8766
TIMEOUT = 300

_PAGE = b"<html><body><h3>StreamFrame authorized this account.</h3>" \
        b"<p>You can close this tab.</p></body></html>"


class AuthError(RuntimeError):
    """Authorization could not be completed."""


def default_redirect(platform, port=DEFAULT_PORT):
    if platform == "youtube":
        return f"http://127.0.0.1:{port}/callback"
    return ""


def authorize(settings: Settings, platform, *, redirect_uri=None, port=DEFAULT_PORT,
              manual=None, session=None, input_fn=input, echo=print):
    session = session or requests
    redirect_uri = redirect_uri or default_redirect(platform, port)
    if not redirect_uri:
        raise AuthError(
            f"{platform} needs your app's registered HTTPS callback. Re-run with "
            f"--redirect-uri https://your-domain/callback"
        )
    state = secrets.token_urlsafe(24)
    loopback = redirect_uri.startswith(("http://127.0.0.1", "http://localhost"))
    if manual is None:
        manual = not loopback

    verifier = None
    if platform == "youtube":
        if not settings.youtube.configured:
            raise ConfigError("YouTube client credentials are not configured")
        verifier = _pkce_verifier()
        url = GOOGLE_AUTHORIZE + "?" + urlencode({
            "client_id": settings.youtube.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
        })
    elif platform == "instagram":
        if not settings.instagram.configured:
            raise ConfigError("Instagram app credentials are not configured")
        if settings.instagram.login == "facebook":
            url = (f"https://www.facebook.com/{settings.instagram.api_version}/dialog/oauth?"
                   + urlencode({
                       "client_id": settings.instagram.app_id,
                       "redirect_uri": redirect_uri,
                       "response_type": "code",
                       "scope": FACEBOOK_SCOPES,
                       "state": state,
                   }))
        else:
            url = INSTAGRAM_AUTHORIZE + "?" + urlencode({
                "client_id": settings.instagram.app_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": INSTAGRAM_SCOPES,
                "state": state,
            })
    elif platform == "tiktok":
        if not settings.tiktok.configured:
            raise ConfigError("TikTok client credentials are not configured")
        url = TIKTOK_AUTHORIZE + "?" + urlencode({
            "client_key": settings.tiktok.client_key,
            "scope": TIKTOK_SCOPES,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        })
    else:
        raise ConfigError(f"Unknown platform {platform}")

    echo(f"Open this URL and approve access:\n\n{url}\n")
    if not manual:
        _open_browser(url)
    code = _collect_code(url, redirect_uri, state, manual=manual, port=port,
                         input_fn=input_fn, echo=echo)
    record = _exchange(settings, platform, code, redirect_uri, verifier, session)
    store_for(settings).put(platform, record)
    echo(f"Saved {platform} credentials to {store_for(settings).path}")
    return record


def _open_browser(url):
    """Best effort: the printed URL is still the source of truth (SSH, no GUI)."""
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        return


def _collect_code(url, redirect_uri, state, *, manual, port, input_fn, echo):
    if manual:
        echo("After approving, your callback page will show a code (or a URL containing one).")
        pasted = input_fn("Paste the full callback URL or just the code: ").strip()
        if not pasted:
            raise AuthError("No authorization code was provided")
        if "code=" in pasted:
            parsed = urlsplit(pasted)
            params = parse_qs(parsed.query or parsed.fragment)
            code = (params.get("code") or [""])[0]
            returned_state = (params.get("state") or [""])[0]
        else:
            code, returned_state = pasted, state
        if not code:
            raise AuthError("That URL did not contain an authorization code")
        if returned_state and returned_state != state:
            raise AuthError("Authorization state did not match; start again")
        return code
    return _listen(port, redirect_uri, state)


def _listen(port, redirect_uri, state):
    result = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            if parsed.path != urlsplit(redirect_uri).path:
                self.send_error(404)
                return
            error = (params.get("error") or [None])[0]
            result["code"] = (params.get("code") or [None])[0]
            result["state"] = (params.get("state") or [None])[0]
            result["error"] = error
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_PAGE)))
            self.end_headers()
            self.wfile.write(_PAGE)
            done.set()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(TIMEOUT):
            raise AuthError("Timed out waiting for the browser callback")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    if result.get("error"):
        raise AuthError(f"Authorization was denied ({result['error']})")
    if not result.get("code"):
        raise AuthError("No authorization code was returned")
    if result.get("state") != state:
        raise AuthError("Authorization state did not match; start again")
    return result["code"]


def _exchange(settings, platform, code, redirect_uri, verifier, session):
    if platform == "youtube":
        response = session.post(TOKEN_ENDPOINT, timeout=30, data={
            "client_id": settings.youtube.client_id,
            "client_secret": settings.youtube.client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
        payload = _payload(response, "Google token exchange failed")
        if not payload.get("refresh_token"):
            raise AuthError("Google returned no refresh token; revoke access and retry")
        return {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "expires_at": time.time() + int(payload.get("expires_in", 3600)),
        }

    if platform == "instagram":
        if settings.instagram.login == "facebook":
            return _exchange_facebook(settings, code, redirect_uri, session)
        short = session.post(INSTAGRAM_TOKEN, timeout=30, data={
            "client_id": settings.instagram.app_id,
            "client_secret": settings.instagram.app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        })
        payload = _payload(short, "Instagram token exchange failed")
        token = payload.get("access_token")
        user_id = payload.get("user_id")
        if not token:
            raise AuthError("Instagram returned no access token")
        long_lived = session.get(INSTAGRAM_EXCHANGE, timeout=30, params={
            "grant_type": "ig_exchange_token",
            "client_secret": settings.instagram.app_secret,
            "access_token": token,
        })
        if long_lived.status_code == 200:
            payload = long_lived.json()
            token = payload.get("access_token", token)
            expires_in = int(payload.get("expires_in", 5184000))
        else:
            expires_in = 3600
        return {
            "access_token": token,
            "user_id": str(user_id) if user_id else None,
            "expires_at": time.time() + expires_in,
        }

    if platform == "tiktok":
        response = session.post(TIKTOK_TOKEN_ENDPOINT, timeout=30, data={
            "client_key": settings.tiktok.client_key,
            "client_secret": settings.tiktok.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
        payload = _payload(response, "TikTok token exchange failed")
        return {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token"),
            "open_id": payload.get("open_id"),
            "expires_at": time.time() + int(payload.get("expires_in", 86400)),
        }

    raise ConfigError(f"Unknown platform {platform}")


def _exchange_facebook(settings, code, redirect_uri, session):
    """User token -> long-lived user token -> the Page's Instagram account.

    The Page access token from a long-lived user token is what publishes Reels,
    so no refresh schedule is needed; if Meta invalidates it, re-run auth.
    """
    graph = f"https://graph.facebook.com/{settings.instagram.api_version}"
    short = session.get(f"{graph}/oauth/access_token", timeout=30, params={
        "client_id": settings.instagram.app_id,
        "client_secret": settings.instagram.app_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    })
    token = _body(short, "Facebook token exchange failed").get("access_token")
    if not token:
        raise AuthError("Facebook returned no access token")

    long_lived = session.get(f"{graph}/oauth/access_token", timeout=30, params={
        "grant_type": "fb_exchange_token",
        "client_id": settings.instagram.app_id,
        "client_secret": settings.instagram.app_secret,
        "fb_exchange_token": token,
    })
    if long_lived.status_code == 200:
        token = long_lived.json().get("access_token", token)

    if settings.instagram.page_id:
        page = _page_by_id(graph, settings.instagram.page_id, token, session)
        return {
            "access_token": page.get("access_token") or token,
            "user_id": page["instagram_business_account"]["id"],
            "page_id": page.get("id"),
            "page_name": page.get("name"),
            "expires_at": None,
        }

    accounts = session.get(f"{graph}/me/accounts", timeout=30, params={
        "fields": "id,name,access_token,instagram_business_account",
        "access_token": token,
    })
    data = _body(accounts, "Could not list Facebook Pages").get("data") or []
    pages = [page for page in data
             if isinstance(page, dict) and page.get("instagram_business_account")]
    if not pages:
        names = ", ".join(str(page.get("name", "unnamed")) for page in data
                          if isinstance(page, dict))
        if not data:
            who = ""
            try:
                me = session.get(f"{graph}/me", timeout=30,
                                 params={"fields": "id,name", "access_token": token})
                if me.status_code == 200:
                    identity = me.json()
                    label = identity.get("name") or identity.get("id")
                    if label:
                        who = f" (authorized as {label})"
            except Exception:
                who = ""
            raise AuthError(
                "Facebook returned no Pages for this token" + who + ". That profile has no "
                "role on the Page, or the Page was not granted in the dialog. Re-authorize "
                "as the Page's admin and grant access to your Page, then try again.")
        raise AuthError(
            f"No Instagram professional account is linked to: {names}. "
            "Link the Instagram account to that Page inside the Instagram app, "
            "then authorize again.")
    page = pages[0]
    return {
        "access_token": page.get("access_token") or token,
        "user_id": page["instagram_business_account"]["id"],
        "page_id": page.get("id"),
        "page_name": page.get("name"),
        "expires_at": None,
    }


def _page_by_id(graph, page_id, token, session):
    """Read one Page directly, bypassing me/accounts discovery."""
    response = session.get(f"{graph}/{page_id}", timeout=30, params={
        "fields": "id,name,access_token,instagram_business_account",
        "access_token": token,
    })
    page = _body(response, f"Could not read Page {page_id}")
    if not page.get("instagram_business_account"):
        raise AuthError(
            f"{page_id} is reachable but has no linked Instagram professional account. "
            "If this is a personal profile rather than a Page, use the Page's own ID "
            "(Business Settings → Accounts → Pages).")
    return page


def _body(response, message):
    if response.status_code != 200:
        raise AuthError(f"{message} (HTTP {response.status_code})")
    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthError(message) from exc
    if not isinstance(payload, dict):
        raise AuthError(message)
    return payload


def _payload(response, message):
    if response.status_code != 200:
        raise AuthError(f"{message} (HTTP {response.status_code})")
    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthError(message) from exc
    if not isinstance(payload, dict) or "access_token" not in payload:
        raise AuthError(message)
    return payload


def _pkce_verifier():
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")


def _pkce_challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


__all__ = ["AuthError", "authorize", "default_redirect"]
