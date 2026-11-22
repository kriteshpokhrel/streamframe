from datetime import datetime, timezone
import hashlib
from pathlib import Path

import pytest

from streamframe.publish.media import (
    LocalDirHost,
    MediaError,
    NoMediaHost,
    canonical_request,
    file_digest,
    sign_headers,
)

EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def test_file_digest_is_stable_and_content_addressed(tmp_path):
    first = tmp_path / "a.mp4"
    first.write_bytes(b"clip-bytes")
    same = tmp_path / "b.mp4"
    same.write_bytes(b"clip-bytes")
    different = tmp_path / "c.mp4"
    different.write_bytes(b"other")
    assert file_digest(first) == file_digest(same)
    assert file_digest(first) != file_digest(different)


def test_local_host_copies_and_serves_a_stable_url(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x")
    public = tmp_path / "public"
    host = LocalDirHost(public, "https://clips.example.com/")

    published = host.put(source, key="abc123")
    assert published.url == "https://clips.example.com/abc123.mp4"
    assert (public / "abc123.mp4").is_file()

    host.remove(published.key)
    assert not (public / "abc123.mp4").exists()


def test_local_host_refuses_to_escape_its_directory(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x")
    host = LocalDirHost(tmp_path / "public", "https://clips.example.com")
    with pytest.raises(MediaError):
        host.put(source, key="../../escape")


def test_no_host_explains_what_instagram_needs():
    with pytest.raises(MediaError, match="public media URL"):
        NoMediaHost().put("clip.mp4", key="k")


def test_sigv4_matches_the_aws_published_vector():
    """AWS4-HMAC-SHA256 example for IAM ListUsers, 2015-08-30."""
    url = "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08"
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}

    request, signed_headers = canonical_request(
        method="GET",
        url=url,
        headers={**headers, "x-amz-date": "20150830T123600Z"},
        payload_hash=EMPTY_HASH,
    )
    assert signed_headers == "content-type;host;x-amz-date"
    assert hashlib.sha256(request.encode()).hexdigest() == (
        "f536975d06c0309214f805bb90ccff089219ecd68b2577efef23edd43b7e1a59"
    )

    signed = sign_headers(
        method="GET",
        url=url,
        headers=headers,
        payload_hash=EMPTY_HASH,
        access_key="AKIDEXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        service="iam",
        now=datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc),
    )
    assert signed["Authorization"] == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )


def test_sigv4_covers_the_payload_hash_for_s3():
    signed = sign_headers(
        method="PUT",
        url="https://acct.r2.cloudflarestorage.com/bucket/clip.mp4",
        headers={"Content-Type": "video/mp4"},
        payload_hash=EMPTY_HASH,
        access_key="id",
        secret_key="secret",
        region="auto",
        service="s3",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert signed["x-amz-content-sha256"] == EMPTY_HASH
    assert "x-amz-content-sha256" in signed["Authorization"]
    # Signing is deterministic for a fixed clock and covers the payload hash.
    again = sign_headers(
        method="PUT",
        url="https://acct.r2.cloudflarestorage.com/bucket/clip.mp4",
        headers={"Content-Type": "video/mp4"},
        payload_hash=EMPTY_HASH,
        access_key="id",
        secret_key="secret",
        region="auto",
        service="s3",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert again["Authorization"] == signed["Authorization"]
    other = sign_headers(
        method="PUT",
        url="https://acct.r2.cloudflarestorage.com/bucket/clip.mp4",
        headers={"Content-Type": "video/mp4"},
        payload_hash="0" * 64,
        access_key="id",
        secret_key="secret",
        region="auto",
        service="s3",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert other["Authorization"] != signed["Authorization"]
