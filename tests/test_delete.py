from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient
import pytest

from streamframe import jobs
from streamframe.server import create_app


LOCAL = "http://127.0.0.1"
CANDIDATE = {"id": "clip", "start": 0.1, "end": 0.8,
             "face_crop": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.4}}


def analyze(*_):
    return {"candidates": [CANDIDATE], "source_words": []}


def render(path, candidate, output, progress, cancelled):
    output.write_bytes(b"generated export")


@pytest.fixture
def library(tmp_path):
    original = tmp_path / "input.mp4"
    original.write_bytes(b"user original")
    root = tmp_path / "library"

    def app(analyzer=analyze, renderer=render):
        return create_app(root, [tmp_path], analyzer, renderer)

    return root, original, app


def launch(client, original, job_id=None):
    store = client.app.state.jobs
    with store.lock:
        # Call the same store operations as the HTTP routes while holding the
        # lock long enough to get the worker handle before it may retire.
        if job_id is None:
            job_id = store.create({"type": "file", "path": str(original)})["id"]
        else:
            store.export(job_id, CANDIDATE["id"])
        worker = store.active[job_id][1]
    return job_id, worker


def join(worker):
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_delete_removes_only_selected_job_and_generated_media(library):
    root, original, app = library
    with TestClient(app(), base_url=LOCAL, client=("127.0.0.1", 30000)) as client:
        job_id, worker = launch(client, original)
        join(worker)
        other_id, worker = launch(client, original)
        join(worker)
        _, worker = launch(client, original, job_id)
        join(worker)
        url = f"/api/jobs/{job_id}"
        exported = client.get(url).json()["exports"][0]["url"]
        assert client.get(url + "/media").content == b"user original"
        assert client.get(exported).content == b"generated export"
        assert client.delete(url, headers={"Origin": "https://not-local.example"}).status_code == 403
        assert (root / job_id / "source.mp4").exists()

        response = client.delete(url)
        assert (response.status_code, response.content) == (204, b"")
        for path in (url, url + "/media", exported):
            assert client.get(path).status_code == 404
        assert client.delete(url).status_code == 404
        assert [j["id"] for j in client.get("/api/jobs").json()["jobs"]] == [other_id]
        assert not (root / job_id).exists()
        assert client.get(f"/api/jobs/{other_id}/media").content == original.read_bytes() == b"user original"
        with client.app.state.jobs.connect() as db:
            assert [row["job_id"] for row in db.execute("SELECT job_id FROM transcripts")] == [other_id]


def test_delete_export_preserves_original_source_and_other_exports(library):
    root, original, app = library
    with TestClient(app(), base_url=LOCAL, client=("127.0.0.1", 30000)) as client:
        job_id, worker = launch(client, original)
        join(worker)
        for _ in range(2):
            _, worker = launch(client, original, job_id)
            join(worker)
        url = f"/api/jobs/{job_id}"
        first, second = client.get(url).json()["exports"]
        assert first["created_at"] and first["start"] == CANDIDATE["start"]
        assert client.delete(first["url"], headers={"Origin": "https://not-local.example"}).status_code == 403
        assert client.delete(first["url"]).status_code == 204
        assert client.delete(first["url"]).status_code == 404
        assert client.get(first["url"]).status_code == 404
        assert not (root / job_id / first["filename"]).exists()
        assert client.get(second["url"]).content == b"generated export"
        assert client.get(url + "/media").content == original.read_bytes() == b"user original"
        job = client.get(url).json()
        assert job["exports"] == [second]
        assert job["candidates"][0]["id"] == CANDIDATE["id"]
        assert client.delete(url + "/exports").status_code == 204
        assert client.get(second["url"]).status_code == 404
        assert client.get(url).json()["exports"] == []
        assert client.get(url).json()["candidates"] == job["candidates"]
        assert client.get(url + "/media").content == b"user original"


@pytest.mark.parametrize(("settings", "expected_title"), [
    ({}, "input"),
    ({"title": ""}, "input"),
    ({"title": "  Custom hook  "}, "Custom hook"),
    ({"title": None}, ""),
])
def test_auto_export_ranks_two_and_applies_opening_title(library, settings, expected_title):
    _, original, app = library
    rendered = []

    def analyzer(*_):
        return [{**CANDIDATE, "id": name, "score": score, "start": start, "end": start + 0.7}
                for name, score, start in (("low", .2, 9.0), ("best", .9, 0.1), ("next", .7, 5.0))]

    def renderer(path, candidate, output, progress, cancelled):
        rendered.append((candidate["id"], candidate["opening_title"]))
        render(path, candidate, output, progress, cancelled)

    with TestClient(app(analyzer, renderer), base_url=LOCAL, client=("127.0.0.1", 30000)) as client:
        store = client.app.state.jobs
        with store.lock:
            job_id = store.create({"type": "file", "path": str(original)}, settings)["id"]
            worker = store.active[job_id][1]
        join(worker)
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "succeeded", job["error"]
        assert rendered == [("best", expected_title), ("next", expected_title)]
        assert [entry["candidate_id"] for entry in job["exports"]] == ["best", "next"]
        assert job["auto_export_pending"] == []
        assert "auto_export_notice" not in job


def test_auto_export_exports_one_when_the_two_best_moments_overlap(library):
    _, original, app = library
    rendered = []

    def analyzer(*_):
        return [{**CANDIDATE, "id": name, "score": score, "start": start, "end": start + 30}
                for name, score, start in (("best", .9, 100.0), ("duplicate", .8, 104.0),
                                           ("distinct", .1, 400.0))]

    def renderer(path, candidate, output, progress, cancelled):
        rendered.append(candidate["id"])
        render(path, candidate, output, progress, cancelled)

    with TestClient(app(analyzer, renderer), base_url=LOCAL, client=("127.0.0.1", 30000)) as client:
        store = client.app.state.jobs
        with store.lock:
            job_id = store.create({"type": "file", "path": str(original)}, {"title": "Hook"})["id"]
            worker = store.active[job_id][1]
        join(worker)
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "succeeded", job["error"]
        assert rendered == ["best"]
        assert [entry["candidate_id"] for entry in job["exports"]] == ["best"]
        assert job["auto_export_pending"] == []
        assert "same footage" in job["auto_export_notice"]


def test_unsafe_paths_never_follow_symlinks(library):
    root, original, app = library
    with TestClient(app(), base_url=LOCAL, client=("127.0.0.1", 30000)) as client:
        job_id, worker = launch(client, original)
        join(worker)
        other_id, worker = launch(client, original)
        join(worker)
        store = client.app.state.jobs
        with pytest.raises(KeyError):
            store.delete("../input.mp4")
        assert client.delete("/api/jobs/%2e%2e%2finput.mp4").status_code in {404, 405}
        assert client.delete("/api/jobs/.runner.lock").status_code == 404

        directory = root / job_id
        displaced = root / "displaced"
        directory.rename(displaced)
        directory.symlink_to(root / other_id, target_is_directory=True)
        url = f"/api/jobs/{job_id}"
        assert client.delete(url).status_code == 500
        assert directory.is_symlink()
        assert (root / other_id / "source.mp4").read_bytes() == b"user original"

        directory.unlink()
        displaced.rename(directory)
        (directory / "nested").mkdir()
        (directory / "nested" / "link-to-other-job").symlink_to(root / other_id, target_is_directory=True)
        (directory / "link-to-original").symlink_to(original)
        assert client.delete(url).status_code == 204
        assert original.read_bytes() == (root / other_id / "source.mp4").read_bytes() == b"user original"


def test_active_job_conflicts_until_worker_retires(library):
    _, original, app = library
    entered, finish = threading.Event(), threading.Event()

    def blocked(path, progress, event):
        entered.set()
        assert finish.wait(5)
        return analyze()

    with TestClient(app(blocked), base_url=LOCAL, client=("127.0.0.1", 30000)) as client:
        job_id, worker = launch(client, original)
        url = f"/api/jobs/{job_id}"
        try:
            assert entered.wait(5)
            assert client.delete(url).status_code == 409
            assert client.get(url).json()["status"] == "running"
            assert client.post(url + "/cancel").json()["status"] == "cancelling"
            assert client.delete(url).status_code == 409
        finally:
            finish.set()
            join(worker)
        assert client.delete(url).status_code == 204


def seed_rolling(store):
    job_id = "1" * 32
    candidate = jobs.validate_candidate({
        **CANDIDATE, "id": "window-1",
        "face_crop": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.4},
    })
    candidate.update(editable=True, window_index=1, revision=0)
    with store.connect() as db:
        store._save(db, {
            "id": job_id, "status": "succeeded", "created_at": 1,
            "source": {"rolling": True}, "candidates": [candidate], "exports": [],
            "live_session": {"windows": [{"index": 1, "start": 0, "end": 1}]},
        })
        db.execute("INSERT INTO transcripts VALUES (?, '[]')", (f"{job_id}:window-1",))
        db.execute("INSERT INTO rolling_budgets VALUES (?, 100, 0, ?)",
                   (job_id, jobs.ROLLING_DISK_BYTES))
    for folder in (jobs.ROLLING_EXPORTS, jobs.ROLLING_SOURCES):
        (store.root / job_id / folder).mkdir(parents=True)
    (store.root / job_id / jobs.ROLLING_SOURCES / "window-1.mp4").write_bytes(b"retained source")
    return job_id


def test_rolling_delete_waits_for_manual_exports(library):
    root, _, app = library
    entered, finish = threading.Event(), threading.Event()

    def blocked(path, candidate, output, progress, event):
        entered.set()
        assert finish.wait(5)
        render(path, candidate, output, progress, event)

    with TestClient(app(renderer=blocked), base_url=LOCAL, client=("127.0.0.1", 30000)) as client:
        store = client.app.state.jobs
        job_id = seed_rolling(store)
        url = f"/api/jobs/{job_id}"
        export_url = url + "/candidates/window-1/export"
        assert client.post(export_url).status_code == 202
        worker = store.corrections[(job_id, "window-1")][1]
        try:
            assert entered.wait(5)
            assert client.delete(url).status_code == 409
            # Cancelling the manual export doesn't change the capture status.
            assert client.post(url + "/cancel").json()["status"] == "succeeded"
            assert client.delete(url).status_code == 409
        finally:
            finish.set()
            join(worker)
        assert client.get(url).json()["candidates"][0]["export_status"] == "cancelled"
        assert client.post(export_url).status_code == 202
        with store.lock:
            correction = store.corrections.get((job_id, "window-1"))
        if correction:
            join(correction[1])
        export = client.get(url).json()["exports"][0]["url"]
        assert client.get(export).content == b"generated export"
        assert client.delete(url).status_code == 204
        assert client.get(export).status_code == 404
        assert client.get(url + "/candidates/window-1/media").status_code == 404
        assert not (root / job_id).exists()
        assert job_id not in store.rolling_disks
        with store.connect() as db:
            assert db.execute("SELECT * FROM rolling_budgets").fetchall() == []
            assert db.execute("SELECT * FROM transcripts").fetchall() == []


@pytest.mark.parametrize("failure", ["disk", "sqlite"])
def test_failed_delete_is_retriable_across_restart(library, monkeypatch, failure):
    root, _, app = library
    with TestClient(app(), base_url=LOCAL, client=("127.0.0.1", 30000)) as client:
        store = client.app.state.jobs
        job_id = seed_rolling(store)
        url = f"/api/jobs/{job_id}"
        with store.connect() as db:
            db.execute("UPDATE rolling_budgets SET working=25 WHERE job_id=?", (job_id,))
        with monkeypatch.context() as patch:
            if failure == "disk":
                def fail_after_removing_source(_):
                    (root / job_id / jobs.ROLLING_SOURCES / "window-1.mp4").unlink()
                    raise OSError("injected partial disk failure")

                patch.setattr(store, "_remove_job_artifacts", fail_after_removing_source)
            else:
                with store.connect() as db:
                    db.execute("""CREATE TRIGGER block_delete BEFORE DELETE ON jobs
                                  BEGIN SELECT RAISE(ABORT, 'injected SQLite failure'); END""")
            assert client.delete(url).status_code == 500
        assert (root / job_id).exists() == (failure == "disk")
        assert client.get("/api/jobs").json()["jobs"][0]["deletion_pending"] is True
        assert client.post(url + "/candidates/window-1/export").status_code == 409
        with store.connect() as db:
            assert tuple(db.execute("SELECT retained, working FROM rolling_budgets").fetchone()) == (100, 25)
            assert db.execute("SELECT job_id FROM transcripts").fetchone()[0] == f"{job_id}:window-1"
            db.execute("DROP TRIGGER IF EXISTS block_delete")

    with TestClient(app(), base_url=LOCAL, client=("127.0.0.1", 30000)) as client:
        with client.app.state.jobs.connect() as db:
            assert tuple(db.execute("SELECT retained, working FROM rolling_budgets").fetchone()) == (100, 25)
        assert client.get(url).status_code == 409
        assert client.delete(url).status_code == 204
        assert client.get(url).status_code == 404


def test_intent_write_failure_leaves_job_and_disk_untouched(library):
    root, original, app = library
    with TestClient(app(), base_url=LOCAL, client=("127.0.0.1", 30000)) as client:
        job_id, worker = launch(client, original)
        join(worker)
        with client.app.state.jobs.connect() as db:
            db.execute("""CREATE TRIGGER block_intent BEFORE INSERT ON jobs
                          BEGIN SELECT RAISE(ABORT, 'injected SQLite intent failure'); END""")
        url = f"/api/jobs/{job_id}"
        assert client.delete(url).status_code == 500
        assert not client.get(url).json().get("deletion_pending")
        assert (root / job_id / "source.mp4").read_bytes() == original.read_bytes()


def test_delete_serializes_against_resume_and_another_delete(library, monkeypatch):
    root, original, app = library
    entered, finish = threading.Event(), threading.Event()
    with TestClient(app(), base_url=LOCAL, client=("127.0.0.1", 30000)) as client:
        job_id, worker = launch(client, original)
        join(worker)
        url = f"/api/jobs/{job_id}"
        store = client.app.state.jobs
        store.update(job_id, status="failed")
        remove = store._remove_job_artifacts

        def blocked(job_id):
            entered.set()
            assert finish.wait(5)
            remove(job_id)

        monkeypatch.setattr(store, "_remove_job_artifacts", blocked)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(client.delete, url)
            try:
                assert entered.wait(5)
                resume = pool.submit(client.post, url + "/resume")
                second = pool.submit(client.delete, url)
                with pytest.raises(jobs.Conflict, match="already owns"):
                    jobs.JobStore(root)
            finally:
                finish.set()
            assert first.result(timeout=5).status_code == 204
            assert resume.result(timeout=5).status_code == second.result(timeout=5).status_code == 404
