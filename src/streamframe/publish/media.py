"""Public media hosting for Instagram.

YouTube and TikTok accept the file bytes, so they need nothing here. Meta
fetches Reels from a URL, so a clip has to be reachable on the public internet
before it can be published.

Two free options are supported, both behind the same interface:

``local``  copy into a directory you already serve (Caddy, nginx, a tunnel)
``r2``     upload to a Cloudflare R2 or S3-compatible bucket with SigV4

``none`` disables Instagram rather than failing mid-publish. Nothing is ever
written outside the configured directory or bucket.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import Path
import shutil
from urllib.parse import parse_qsl, quote, urlsplit

import requests

from .settings import ConfigError, MediaSettings

CHUNK = 1024 * 1024
CATBOX_ENDPOINT = "https://litterbox.catbox.moe/resources/internals/api.php"


class MediaError(RuntimeError):
    """A clip could not be made publicly reachable."""


@dataclass(frozen=True)
class PublishedMedia:
    key: str
    url: str


def file_digest(path):
    """Streamed SHA-256; used for both clip identity and payload signing."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


class LocalDirHost:
    """Publish by copying into a directory the operator already serves."""

    def __init__(self, directory, base_url):
        self.directory = Path(directory)
        self.base_url = base_url.rstrip("/")

    def put(self, path, key, *, content_type="video/mp4"):
        self.directory.mkdir(parents=True, exist_ok=True)
        suffix = Path(path).suffix.lower() or ".mp4"
        name = f"{key}{suffix}"
        target = self.directory / name
        if target.resolve().parent != self.directory.resolve():
            raise MediaError("Media key escapes the configured directory")
        shutil.copyfile(path, target)
        return PublishedMedia(key=name, url=f"{self.base_url}/{quote(name)}")

    def remove(self, key):
        (self.directory / key).unlink(missing_ok=True)


class R2Host:
    """Upload to an S3-compatible bucket (Cloudflare R2 by default)."""

    region = "auto"

    def __init__(self, settings: MediaSettings, *, timeout=120):
        self.bucket = settings.r2_bucket
        self.endpoint = settings.r2_endpoint.rstrip("/")
        self.access_key = settings.r2_access_key_id
        self.secret_key = settings.r2_secret_access_key
        self.base_url = settings.public_base_url.rstrip("/")
        self.timeout = timeout

    def _object_url(self, key):
        return f"{self.endpoint}/{quote(self.bucket)}/{quote(key, safe='/')}"

    def put(self, path, key, *, content_type="video/mp4"):
        path = Path(path)
        payload_hash = file_digest(path)
        url = self._object_url(key)
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(path.stat().st_size),
        }
        signed = sign_headers(
            method="PUT",
            url=url,
            headers=headers,
            payload_hash=payload_hash,
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            service="s3",
        )
        try:
            with path.open("rb") as body:
                response = requests.put(url, data=body, headers=signed,
                                        timeout=self.timeout)
        except requests.RequestException as exc:
            raise MediaError(f"Media upload failed: {type(exc).__name__}") from exc
        if response.status_code not in {200, 201, 204}:
            raise MediaError(f"Media upload was rejected (HTTP {response.status_code})")
        return PublishedMedia(key=key, url=f"{self.base_url}/{quote(key)}")

    def remove(self, key):
        try:
            signed = sign_headers(
                method="DELETE",
                url=self._object_url(key),
                headers={},
                payload_hash=hashlib.sha256(b"").hexdigest(),
                access_key=self.access_key,
                secret_key=self.secret_key,
                region=self.region,
                service="s3",
            )
            requests.delete(self._object_url(key), headers=signed, timeout=self.timeout)
        except (requests.RequestException, MediaError):
            pass


class NoMediaHost:
    def put(self, path, key, *, content_type="video/mp4"):
        raise MediaError(
            "Instagram needs a public media URL: set STREAMFRAME_MEDIA_HOST "
            "(local or r2) and STREAMFRAME_MEDIA_PUBLIC_BASE_URL"
        )

    def remove(self, key):
        return None


class CatboxHost:
    """Transient public hosting with no account and no card.

    Litterbox keeps the file for a fixed window and then removes it, which is
    the lifetime Meta needs: it fetches the clip once, seconds after the media
    container is created. The upload is public at an unguessable URL while it
    lives.
    """

    def __init__(self, settings: MediaSettings, *, timeout=120):
        self.expiry = settings.catbox_expiry
        self.timeout = timeout

    def put(self, path, key, *, content_type="video/mp4"):
        path = Path(path)
        name = f"{key}{path.suffix.lower() or '.mp4'}"
        try:
            with path.open("rb") as body:
                response = requests.post(
                    CATBOX_ENDPOINT,
                    data={"reqtype": "fileupload", "time": self.expiry},
                    files={"fileToUpload": (name, body, content_type)},
                    timeout=self.timeout,
                )
        except requests.RequestException as exc:
            raise MediaError(f"Temporary upload failed: {type(exc).__name__}") from exc
        url = (response.text or "").strip()
        if response.status_code != 200 or not url.startswith("https://"):
            raise MediaError("Temporary upload was rejected")
        return PublishedMedia(key=name, url=url)

    def remove(self, key):
        # Litterbox expires the upload itself; there is no delete without an account.
        return None


def build_host(settings: MediaSettings, *, timeout=120):
    if settings.host == "catbox":
        return CatboxHost(settings, timeout=timeout)
    if not settings.configured:
        return NoMediaHost()
    if settings.host == "local":
        return LocalDirHost(settings.local_dir, settings.public_base_url)
    if settings.host == "r2":
        return R2Host(settings, timeout=timeout)
    return NoMediaHost()


# -- SigV4 --------------------------------------------------------------

def _hmac(key, message):
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret, datestamp, region, service):
    key = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def canonical_request(*, method, url, headers, payload_hash):
    """Build the AWS canonical request; exposed so tests can pin its shape."""
    parsed = urlsplit(url)
    uri = quote(parsed.path or "/", safe="/-_.~%")
    query = "&".join(
        f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}"
        for name, value in sorted(parse_qsl(parsed.query, keep_blank_values=True))
    )
    normalized = {name.lower().strip(): " ".join(str(value).split())
                  for name, value in headers.items()}
    normalized["host"] = parsed.netloc
    signed_names = sorted(normalized)
    canonical_headers = "".join(f"{name}:{normalized[name]}\n" for name in signed_names)
    signed_headers = ";".join(signed_names)
    request = "\n".join([method.upper(), uri, query, canonical_headers,
                         signed_headers, payload_hash])
    return request, signed_headers


def sign_headers(*, method, url, headers, payload_hash, access_key, secret_key,
                 region, service, now=None):
    """Return headers augmented with a SigV4 ``Authorization`` value."""
    now = now or datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    augmented = dict(headers)
    augmented["x-amz-date"] = amzdate
    if service == "s3":
        # Required by S3-compatible stores; other AWS services do not send it.
        augmented["x-amz-content-sha256"] = payload_hash
    request, signed_headers = canonical_request(
        method=method, url=url, headers=augmented, payload_hash=payload_hash
    )
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amzdate,
        scope,
        hashlib.sha256(request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(secret_key, datestamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    augmented["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return augmented


__all__ = [
    "CatboxHost", "LocalDirHost", "MediaError", "NoMediaHost", "PublishedMedia", "R2Host",
    "build_host", "canonical_request", "file_digest", "sign_headers",
    "ConfigError",
]
