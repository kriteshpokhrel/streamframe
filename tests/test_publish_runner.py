from dataclasses import replace

import pytest

from streamframe.publish.ledger import Ledger
from streamframe.publish.model import NEEDS_MANUAL, PUBLISHED, Post, PublishError, PublishResult
from streamframe.publish.runner import Publisher
from streamframe.publish.settings import Settings


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result or PublishResult(platform="x", status=PUBLISHED, platform_id="id1")
        self.error = error
        self.calls = 0

    def _invoke(self, post):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    upload = _invoke
    publish = _invoke
    upload_draft = _invoke


def make_settings(tmp_path, platforms=("youtube", "instagram", "tiktok")):
    return Settings(data_dir=tmp_path, state_dir=tmp_path / "publish", env_file=None,
                    platforms=platforms)


def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x")
    return Post(path=path, clip_key="clip1", title="t", caption="c")


def test_a_clip_publishes_exactly_once(tmp_path):
    client = FakeClient()
    publisher = Publisher(make_settings(tmp_path), clients={"youtube": client})
    post = clip(tmp_path)

    first = publisher.publish(post, platforms=["youtube"])
    second = publisher.publish(post, platforms=["youtube"])

    assert first[0].action == "published"
    assert second[0].action == "skipped"
    assert client.calls == 1


def test_dry_run_never_claims_or_publishes(tmp_path):
    client = FakeClient()
    publisher = Publisher(make_settings(tmp_path), clients={"youtube": client})

    outcomes = publisher.publish(clip(tmp_path), platforms=["youtube"], dry_run=True)

    assert outcomes[0].action == "planned"
    assert client.calls == 0
    assert publisher.ledger.records() == []


def test_failures_are_recorded_and_can_be_retried(tmp_path):
    failing = FakeClient(error=PublishError("HTTP 500"))
    publisher = Publisher(make_settings(tmp_path), clients={"youtube": failing})
    post = clip(tmp_path)

    assert publisher.publish(post, platforms=["youtube"])[0].action == "failed"
    record = publisher.ledger.status_for("clip1")["youtube"]
    assert record.status == "failed"
    assert "HTTP 500" in record.error

    working = FakeClient()
    publisher._clients["youtube"] = working
    assert publisher.publish(post, platforms=["youtube"])[0].action == "published"
    assert working.calls == 1


def test_a_tiktok_draft_is_needs_manual_not_published(tmp_path):
    client = FakeClient(PublishResult(platform="tiktok", status=NEEDS_MANUAL,
                                      platform_id="p1", detail="finish in the app"))
    publisher = Publisher(make_settings(tmp_path), clients={"tiktok": client})

    outcome = publisher.publish(clip(tmp_path), platforms=["tiktok"])[0]

    assert outcome.action == "needs_manual"
    assert publisher.ledger.status_for("clip1")["tiktok"].status == "needs_manual"
    # A second run must not burn another draft.
    assert publisher.publish(clip(tmp_path), platforms=["tiktok"])[0].action == "skipped"
    assert client.calls == 1


def test_unexpected_adapter_errors_do_not_escape(tmp_path):
    client = FakeClient(error=RuntimeError("boom"))
    publisher = Publisher(make_settings(tmp_path), clients={"youtube": client})

    assert publisher.publish(clip(tmp_path), platforms=["youtube"])[0].action == "failed"
    assert publisher.ledger.status_for("clip1")["youtube"].error == "RuntimeError"


def test_error_messages_never_echo_a_credential(tmp_path):
    client = FakeClient(error=PublishError("failed at access_token=SECRET123&x=1"))
    publisher = Publisher(make_settings(tmp_path), clients={"youtube": client})

    publisher.publish(clip(tmp_path), platforms=["youtube"])

    assert "SECRET123" not in publisher.ledger.status_for("clip1")["youtube"].error


def test_unknown_platform_is_rejected_before_any_claim(tmp_path):
    publisher = Publisher(make_settings(tmp_path), clients={})
    with pytest.raises(Exception):
        publisher.publish(clip(tmp_path), platforms=["myspace"])
    assert publisher.ledger.records() == []
