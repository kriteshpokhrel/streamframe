import time
from types import SimpleNamespace

import pytest

from streamframe import jobs
from streamframe.publish.model import PublishError
from streamframe.publish.runner import Publisher
from streamframe.publish.settings import Settings

CROP = {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.4}


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    pytest.fail("job or publish results did not arrive")


class FakeClient:
    def __init__(self, *, status="published", error=None, platform_id="id1"):
        self.status, self.error, self.platform_id = status, error, platform_id
        self.calls = 0
        self.posts = []

    def _run(self, post):
        self.calls += 1
        self.posts.append(post)
        if self.error:
            raise self.error
        return SimpleNamespace(platform="x", status=self.status,
                               platform_id=self.platform_id, url="https://example.test/x",
                               detail=None)

    upload = _run
    publish = _run
    upload_draft = _run


def make_settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        state_dir=tmp_path / "publish",
        env_file=None,
        platforms=("youtube", "instagram", "tiktok"),
    )


def make_store(tmp_path, publisher):
    def analyze(path, progress, event):
        return [{"id": "c1", "start": 0.0, "end": 1.0, "score": 1.0, "face_crop": CROP}]

    def render(path, candidate, output, progress, event):
        output.write_bytes(b"rendered")

    return jobs.JobStore(tmp_path / "data", import_roots=[tmp_path], analyzer=analyze,
                         renderer=render, publisher=publisher)


def finished_export(store, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"media")
    job = store.create({"type": "file", "path": str(clip)},
                       auto_export={"title": "A hook"}, auto_publish=False)
    job = wait_for(lambda: store.get(job["id"])["status"] in jobs.TERMINAL and store.get(job["id"]))
    return store.get(job["id"]), job["exports"][0]["filename"]


def test_manual_publish_records_one_result_per_platform(tmp_path):
    client = FakeClient()
    publisher = Publisher(make_settings(tmp_path),
                          clients={"youtube": client, "instagram": client, "tiktok": client})
    store = make_store(tmp_path, publisher)
    try:
        job, filename = finished_export(store, tmp_path)

        store.publish_export(job["id"], filename, ["youtube"], title="A hook")
        results = wait_for(lambda: store.get(job["id"]).get("publish_results"))

        assert len(results) == 1
        assert results[0]["platform"] == "youtube"
        assert results[0]["action"] == "published"
        assert results[0]["filename"] == filename
    finally:
        store.close()


def test_one_failing_platform_does_not_block_the_others(tmp_path):
    settings = make_settings(tmp_path)
    publisher = Publisher(settings, clients={
        "youtube": FakeClient(),
        "instagram": FakeClient(error=PublishError("Instagram rejected the upload")),
        "tiktok": FakeClient(status="needs_manual"),
    })
    store = make_store(tmp_path, publisher)
    try:
        job, filename = finished_export(store, tmp_path)

        store.publish_export(job["id"], filename, ["youtube", "instagram", "tiktok"],
                             title="A hook")
        results = wait_for(lambda: (store.get(job["id"]).get("publish_results")
                                    if len(store.get(job["id"]).get("publish_results", [])) >= 3
                                    else None))

        by_platform = {entry["platform"]: entry for entry in results}
        assert by_platform["youtube"]["action"] == "published"
        assert by_platform["instagram"]["action"] == "failed"
        assert by_platform["tiktok"]["action"] == "needs_manual"
    finally:
        store.close()


def test_publishing_twice_does_not_post_twice(tmp_path):
    client = FakeClient()
    publisher = Publisher(make_settings(tmp_path), clients={"youtube": client})
    store = make_store(tmp_path, publisher)
    try:
        job, filename = finished_export(store, tmp_path)

        store.publish_export(job["id"], filename, ["youtube"], title="A hook")
        wait_for(lambda: store.get(job["id"]).get("publish_results"))
        store.publish_export(job["id"], filename, ["youtube"], title="A hook")
        wait_for(lambda: len(store.get(job["id"]).get("publish_results", [])) >= 2)

        assert client.calls == 1
        actions = [entry["action"] for entry in store.get(job["id"])["publish_results"]]
        assert actions == ["published", "skipped"]
    finally:
        store.close()


def test_invalid_requests_are_rejected(tmp_path):
    store = make_store(tmp_path, Publisher(make_settings(tmp_path), clients={}))
    try:
        job, filename = finished_export(store, tmp_path)

        with pytest.raises(ValueError):
            store.publish_export(job["id"], filename, [])
        with pytest.raises(ValueError):
            store.publish_export(job["id"], filename, ["myspace"])
        with pytest.raises(ValueError):
            store.publish_export(job["id"], "../escape")
        with pytest.raises(KeyError):
            store.publish_export(job["id"], "does-not-exist.mp4", ["youtube"], title="A hook")
    finally:
        store.close()


def test_a_title_is_required_and_bounded(tmp_path):
    store = make_store(tmp_path, Publisher(make_settings(tmp_path), clients={}))
    try:
        job, filename = finished_export(store, tmp_path)

        with pytest.raises(ValueError, match="title"):
            store.publish_export(job["id"], filename, ["youtube"])
        with pytest.raises(ValueError, match="title"):
            store.publish_export(job["id"], filename, ["youtube"], title="   ")
        with pytest.raises(ValueError, match="title"):
            store.publish_export(job["id"], filename, ["youtube"], title="x" * 101)
        with pytest.raises(ValueError, match="privacy"):
            store.publish_export(job["id"], filename, ["youtube"], title="A hook",
                                 privacy="everyone")
    finally:
        store.close()


def test_title_and_privacy_reach_the_platform(tmp_path):
    client = FakeClient()
    publisher = Publisher(make_settings(tmp_path), clients={"youtube": client})
    store = make_store(tmp_path, publisher)
    try:
        job, filename = finished_export(store, tmp_path)

        store.publish_export(job["id"], filename, ["youtube"],
                             title="  A real hook  ", privacy="public")
        wait_for(lambda: store.get(job["id"]).get("publish_results"))

        assert client.posts[-1].title == "A real hook"
        assert client.posts[-1].privacy == "public"
        assert store.get(job["id"])["publish_results"][0]["action"] == "published"
    finally:
        store.close()


def test_publish_config_reports_enabled_and_configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STREAMFRAME_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STREAMFRAME_PUBLISH_PLATFORMS", "youtube")
    monkeypatch.setenv("STREAMFRAME_YOUTUBE_CLIENT_ID", "id")
    monkeypatch.setenv("STREAMFRAME_YOUTUBE_CLIENT_SECRET", "secret")

    store = make_store(tmp_path, None)
    try:
        config = {entry["name"]: entry for entry in store.publish_config()["platforms"]}
        assert config["youtube"]["enabled"] is True
        assert config["youtube"]["configured"] is True
        assert config["instagram"]["enabled"] is False
        assert "STREAMFRAME_PUBLISH_PLATFORMS" in config["instagram"]["detail"]
        assert store.publish_config()["default_privacy"] == "public"
    finally:
        store.close()


def test_publish_routes_are_wired(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from streamframe.server import create_app

    def analyze(path, progress, event):
        return []

    def render(*args, **kwargs):
        raise AssertionError("renderer should not run")

    app = create_app(tmp_path / "data", [tmp_path], analyze, render)
    with TestClient(app, base_url="http://127.0.0.1",
                    client=("127.0.0.1", 50000)) as client:
        config = client.get("/api/publish")
        assert config.status_code == 200
        assert {entry["name"] for entry in config.json()["platforms"]} == {
            "youtube", "instagram", "tiktok"}

        missing = client.post("/api/jobs/nope/exports/nope.mp4/publish",
                              json={"platforms": ["youtube"], "title": "Hook"})
        assert missing.status_code == 404

        no_title = client.post("/api/jobs/nope/exports/nope.mp4/publish",
                               json={"platforms": ["youtube"]})
        assert no_title.status_code == 422

        bad_platform = client.post("/api/jobs/nope/exports/nope.mp4/publish",
                                   json={"platforms": ["myspace"], "title": "Hook"})
        assert bad_platform.status_code == 422

        unknown_field = client.post("/api/jobs/nope/exports/nope.mp4/publish",
                                    json={"platforms": ["youtube"], "bogus": 1})
        assert unknown_field.status_code == 422
