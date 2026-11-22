import time
from types import SimpleNamespace

import pytest

from streamframe import jobs
from streamframe.cli import build_parser

CROP = {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.4}


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    pytest.fail("Job or publisher did not reach the expected state")


class FakePublisher:
    def __init__(self, *, action="published", error=None):
        self.posts = []
        self.action = action
        self.error = error

    def publish(self, post, *, platforms=None):
        self.posts.append(post)
        if self.error is not None:
            raise self.error
        return [SimpleNamespace(platform="youtube", action=self.action,
                                url="https://youtu.be/x", detail=None)]


def make_store(tmp_path, publisher):
    def analyze(path, progress, event):
        return [{"id": "c1", "start": 0.0, "end": 1.0, "score": 1.0, "face_crop": CROP}]

    def render(path, candidate, output, progress, event):
        output.write_bytes(b"rendered")

    return jobs.JobStore(tmp_path / "data", import_roots=[tmp_path], analyzer=analyze,
                         renderer=render, publisher=publisher)


def submit(store, tmp_path, **kwargs):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"media")
    return store.create({"type": "file", "path": str(clip)},
                        auto_export={"title": "A real hook"}, **kwargs)


def terminal(store, job_id):
    return wait_for(lambda: (store.get(job_id)["status"] in jobs.TERMINAL
                             and store.get(job_id)) or None)


def test_auto_publish_posts_finished_exports(tmp_path):
    publisher = FakePublisher()
    store = make_store(tmp_path, publisher)
    try:
        job = submit(store, tmp_path, auto_publish=True)
        finished = terminal(store, job["id"])
        results = wait_for(lambda: finished and "publish_results" in store.get(job["id"])
                           and store.get(job["id"])["publish_results"])

        assert finished["status"] == "succeeded"
        assert len(publisher.posts) == 1
        assert publisher.posts[0].title == "A real hook"
        assert results[0]["action"] == "published"
        assert results[0]["filename"].endswith(".mp4")
    finally:
        store.close()


def test_auto_publish_off_publishes_nothing(tmp_path):
    publisher = FakePublisher()
    store = make_store(tmp_path, publisher)
    try:
        job = submit(store, tmp_path, auto_publish=False)
        terminal(store, job["id"])
        time.sleep(0.1)
        assert publisher.posts == []
        assert "publish_results" not in store.get(job["id"])
    finally:
        store.close()


def test_a_publishing_failure_never_fails_the_export(tmp_path):
    publisher = FakePublisher(error=RuntimeError("boom"))
    store = make_store(tmp_path, publisher)
    try:
        job = submit(store, tmp_path, auto_publish=True)
        finished = terminal(store, job["id"])
        results = wait_for(lambda: finished and store.get(job["id"]).get("publish_results"))

        assert finished["status"] == "succeeded"
        assert finished["exports"]
        assert results[0]["action"] == "failed"
        assert results[0]["detail"] == "RuntimeError"
    finally:
        store.close()


def test_the_app_setting_supplies_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMFRAME_PUBLISH_AUTO", "1")
    assert jobs._auto_publish_default() is True
    monkeypatch.setenv("STREAMFRAME_PUBLISH_AUTO", "off")
    assert jobs._auto_publish_default() is False

    monkeypatch.setenv("STREAMFRAME_PUBLISH_AUTO", "1")
    store = make_store(tmp_path, FakePublisher())
    try:
        job = submit(store, tmp_path)
        assert job["auto_publish"] is True
    finally:
        store.close()


def test_an_explicit_per_source_flag_overrides_the_app_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMFRAME_PUBLISH_AUTO", "1")
    store = make_store(tmp_path, FakePublisher())
    try:
        job = submit(store, tmp_path, auto_publish=False)
        assert job["auto_publish"] is False
    finally:
        store.close()


def test_auto_publish_must_be_boolean(tmp_path):
    store = make_store(tmp_path, FakePublisher())
    try:
        with pytest.raises(ValueError):
            submit(store, tmp_path, auto_publish="yes")
    finally:
        store.close()


def test_publish_flags_reach_the_job_payload():
    parser = build_parser()
    assert parser.parse_args(["submit", "clip.mp4"]).auto_publish is None
    assert parser.parse_args(["submit", "clip.mp4", "--auto-publish"]).auto_publish is True
    assert parser.parse_args(["submit", "clip.mp4", "--no-auto-publish"]).auto_publish is False
    assert parser.parse_args(["streamer", "name", "--auto-publish"]).auto_publish is True
