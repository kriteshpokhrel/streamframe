import shutil
import sqlite3
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from streamframe import jobs, live, sources


CROP = {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.4}
SOURCE = {"type": "streamer", "identifier": "streamer", "rolling": True,
          "session_duration": 10, "window_duration": 1}


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    pytest.fail("Rolling job did not reach expected state")


def terminal(store, job_id):
    def finished():
        job = store.get(job_id)
        return job if job["status"] in jobs.TERMINAL else None

    return wait_for(finished)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    def encode(raw, output, duration, event, maximum):
        output.write_bytes(raw.read_bytes())
        return duration - 0.1, 1280, 720

    def analyze(path, progress, event):
        return [
            {"id": "same", "start": 0.1, "end": 0.8, "score": 1},
            {"id": "same", "start": 0.2, "end": 0.8, "score": 2, "face_crop": CROP},
        ]

    def render(path, candidate, output, progress, event):
        output.write_bytes(path.read_bytes())

    monkeypatch.setattr(sources, "resolve_source", lambda _, **kw: SimpleNamespace(
        url="https://www.twitch.tv/streamer", platform="twitch", title="Live test",
        selection="live", source_id="streamer", is_live=True))
    monkeypatch.setattr(jobs, "_rolling_encode", encode)
    stores = []

    def make(producer, *, analyzer=analyze, renderer=render):
        monkeypatch.setattr(live, "capture_windows", producer)
        store = jobs.JobStore(tmp_path, analyzer=analyzer, renderer=renderer)
        stores.append(store)
        return store

    yield make
    for store in stores:
        store.close()


def handoff(directory, kwargs, index, *, sequence=None, payload=None):
    path = directory / f".test-window-{index}.ts"
    payload = f"window {index}".encode() if payload is None else payload
    with kwargs["disk_lock"]:
        assert kwargs["disk_remaining"]() >= len(payload)
        path.write_bytes(payload)
    sequence = 10 + index if sequence is None else sequence
    try:
        kwargs["on_window"](path, dict(first_sequence=sequence, last_sequence=sequence,
                                      start=index - 1, end=index,
                                      started_at=f"2026-01-01T00:00:0{index - 1}+00:00"))
    except Exception:
        path.unlink()
        raise


@pytest.mark.parametrize("width,height,published", [
    (1280, 720, True), (1920, 1080, True), (720, 1280, True),
    (854, 480, False), (1280, 719, False),
])
def test_automatic_publication_requires_verified_720p_source(runner, monkeypatch, width, height, published):
    def encode(raw, output, duration, event, maximum):
        output.write_bytes(raw.read_bytes())
        return duration - 0.1, width, height

    monkeypatch.setattr(jobs, "_rolling_encode", encode)
    store = runner(lambda resolved, directory, **kw: handoff(directory, kw, 1))
    job = terminal(store, store.create(SOURCE)["id"])
    assert job["status"] == "succeeded"
    window = job["live_session"]["windows"][0]
    assert (window["source_width"], window["source_height"]) == (width, height)
    assert len(job["exports"]) == int(published)
    assert job["live_session"]["windows_processed"] == 1
    if not published:
        assert window["status"] == "low_quality"
        assert "requires at least 720p" in window["warning"]
        candidate = job["candidates"][0]
        assert candidate["editable"]
        assert store.candidate_media_path(job["id"], candidate["id"]).is_file()
        store.export(job["id"], candidate["id"])
        job = candidate_finished(store, job["id"])
        assert len(job["exports"]) == 1


def test_exports_are_durable_while_capture_is_active_and_stop_preserves_them(runner):
    permits = [threading.Event() for _ in range(3)]
    joined = threading.Event()

    def producer(resolved, directory, **kw):
        try:
            assert kw["session_seconds"] == 10 and kw["window_seconds"] == 1
            for index in range(1, 4):
                handoff(directory, kw, index)
                assert permits[index - 1].wait(4)
            assert kw["cancelled"].wait(4)
        finally:
            joined.set()

    store = runner(producer)
    job_id = store.create(SOURCE)["id"]
    for index in range(1, 4):
        job = wait_for(lambda: (job if len((job := store.get(job_id))["exports"]) == index else None))
        assert job["status"] == "running" and job["live_session"]["acquiring"]
        record = job["exports"][-1]
        assert record["window_index"] == index
        assert record["global_start"] == pytest.approx(index - 0.8)
        assert record["global_end"] == pytest.approx(index - 0.2)
        assert record["window"]["media_end"] == pytest.approx(index - 0.1)
        assert record["window"]["first_sequence"] == 10 + index
        assert store.media_path(job_id, record["filename"]).read_bytes() == f"window {index}".encode()
        permits[index - 1].set()
    store.cancel(job_id)
    job = terminal(store, job_id)
    assert joined.is_set()
    assert job["status"] == "cancelled" and len(job["exports"]) == 3
    assert [candidate["id"] for candidate in job["candidates"]] == ["window-1", "window-2", "window-3"]
    assert job["live_session"]["windows_processed"] == 3
    assert job["live_session"]["global_time"] == 3
    assert any("tail is not included" in warning for warning in job["live_session"]["warnings"])
    with pytest.raises(jobs.Conflict, match="cannot be resumed"):
        store.resume(job_id)
    store.close()
    reopened = jobs.JobStore(store.root)
    try:
        assert reopened.get(job_id)["exports"] == job["exports"]
    finally:
        reopened.close()


def test_stop_after_renderer_produced_output_commits_export_and_marks_queued_window(runner):
    ready = threading.Event()
    release = threading.Event()
    queued = threading.Event()

    def producer(_, directory, **kw):
        handoff(directory, kw, 1)
        assert ready.wait(4)
        handoff(directory, kw, 2)
        queued.set()
        assert kw["cancelled"].wait(4)

    def render(path, candidate, output, progress, event):
        output.write_bytes(b"validated")
        ready.set()
        assert release.wait(4)
        progress(1, "done")

    store = runner(producer, renderer=render)
    job_id = store.create(SOURCE)["id"]
    try:
        assert queued.wait(4)
        store.cancel(job_id)
    finally:
        release.set()
    job = terminal(store, job_id)
    assert job["status"] == "cancelled"
    assert [w["status"] for w in job["live_session"]["windows"]] == ["exported", "discarded"]
    assert store.media_path(job_id, job["exports"][0]["filename"]).read_bytes() == b"validated"


@pytest.mark.parametrize("failure", ["queue", "gap", "repeat_sequence", "producer", "consumer"])
def test_failures_are_explicit_and_unprocessed_windows_are_counted(runner, failure):
    consuming = threading.Event()
    release = threading.Event()
    producer_joined = threading.Event()

    def analyze(path, progress, event):
        consuming.set()
        assert release.wait(4)
        if failure == "consumer":
            raise RuntimeError("analyzer broke")
        return []

    def producer(_, directory, **kw):
        try:
            handoff(directory, kw, 1)
            assert consuming.wait(4)
            if failure in {"gap", "repeat_sequence"}:
                handoff(directory, kw, 2, sequence=99 if failure == "gap" else 11)
            elif failure == "producer":
                raise RuntimeError("transport broke")
            elif failure == "consumer":
                handoff(directory, kw, 2)
                release.set()
                assert kw["cancelled"].wait(4)
            else:
                for index in range(2, 5):
                    handoff(directory, kw, index)
        finally:
            release.set()
            producer_joined.set()

    store = runner(producer, analyzer=analyze)
    job = terminal(store, store.create(SOURCE)["id"])
    assert producer_joined.is_set() and job["status"] == "failed"
    assert not job["live_session"]["acquiring"]
    assert not (store.root / job["id"] / jobs.ROLLING_WORK).exists()
    error, statuses = job["error"], [w["status"] for w in job["live_session"]["windows"]]
    if failure == "queue":
        assert "backpressure" in error and statuses == ["no_candidate"] * 3 + ["discarded"]
    elif failure in {"gap", "repeat_sequence"}:
        assert "sequence gap" in error and statuses == ["no_candidate"]
    elif failure == "producer":
        assert "transport broke" in error and statuses == ["no_candidate"]
    else:
        assert "analyzer broke" in error and statuses == ["failed", "discarded"]


def test_no_face_is_visible_and_manual_crop_applies_to_future_windows(runner):
    next_window = threading.Event()

    def producer(_, directory, **kw):
        handoff(directory, kw, 1)
        assert next_window.wait(4)
        handoff(directory, kw, 2)

    def analyze(path, progress, event):
        return [{"start": 0.1, "end": 0.7}]

    store = runner(producer, analyzer=analyze)
    job_id = store.create(SOURCE)["id"]
    try:
        assert wait_for(lambda: store.get(job_id)["live_session"]["windows_processed"] == 1)
        job = store.get(job_id)
        assert job["live_session"]["windows"][0]["status"] == "no_face"
        assert job["candidates"][0]["face_crop"] is None and not job["exports"]
        store.set_live_crop(job_id, {"face_crop": CROP})
        assert store.set_live_crop(job_id, {"face_crop": None})["live_session"]["face_crop"] is None
        store.set_live_crop(job_id, {"face_crop": CROP})
        with pytest.raises(ValueError, match="exactly one"):
            store.set_live_crop(job_id, CROP)
    finally:
        next_window.set()
    job = terminal(store, job_id)
    assert job["status"] == "succeeded"
    assert job["exports"][0]["window_index"] == 2
    assert job["candidates"][1]["face_crop"] == CROP


def test_suppresses_exact_ts_and_near_duplicate_speech_but_not_silent_media(runner):
    speech = ("apple boy child dog evening fruit goat horse ice joker kale leaf "
              "morning night open peach queen river snow train umbrella violin water xylophone yellow")
    permits = [threading.Event() for _ in range(6)]

    def producer(_, directory, **kw):
        for index in range(1, 7):
            handoff(directory, kw, index, payload=b"window 1" if index == 3 else None)
            assert permits[index - 1].wait(4)

    def analyze(path, progress, event):
        payload = path.read_bytes()
        text = (speech if payload == b"window 1" else
                speech.replace("yellow", "zebra") if payload == b"window 2" else "")
        return [{"start": 0.1, "end": 0.8, "face_crop": CROP,
                 "words": [{"start": 0.2, "end": 0.7, "text": text}] if text else []}]

    store = runner(producer, analyzer=analyze)
    job_id = store.create(SOURCE)["id"]
    try:
        for index, permit in enumerate(permits, 1):
            wait_for(lambda: store.get(job_id)["live_session"]["windows_processed"] == index)
            permit.set()
    finally:
        for permit in permits:
            permit.set()
    job = terminal(store, job_id)
    assert job["status"] == "succeeded"
    windows = job["live_session"]["windows"]
    assert [w["status"] for w in windows] == ["exported", "duplicate", "duplicate"] + ["exported"] * 3
    assert windows[1]["transcript_sha256"] != windows[0]["transcript_sha256"]
    assert windows[2]["raw_sha256"] == windows[0]["raw_sha256"]
    assert windows[1]["duplicate_of"] == windows[2]["duplicate_of"] == 1
    assert windows[0]["sequence_key"] != windows[2]["sequence_key"]


def test_rolling_disk_reserves_raw_and_retained_exports_under_one_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "ROLLING_DISK_BYTES", 100)
    disk = jobs._RollingDisk(tmp_path)
    raw = tmp_path / "raw.ts"
    with disk.lock:
        raw.write_bytes(b"x" * 34)
    assert disk.reserve(100, overhead=2) == 64
    with disk.lock:
        assert disk.disk_remaining() == 0
    disk.reduce(11)
    disk.finish_window(55)
    with disk.lock:
        assert disk.disk_remaining() == 11
        with raw.open("ab", buffering=0) as file:
            file.write(b"x" * 11)
    with pytest.raises(ValueError, match="disk budget"):
        disk.reserve(1)


@pytest.mark.parametrize(("start", "end", "accepted"), [
    (0.2, 0.901, True),
    (0.2, 0.901001, False),
    (0.9001, 0.9009, False),
])
def test_rolling_clamps_candidate_end_only_for_millisecond_rounding(runner, start, end, accepted):
    def producer(_, directory, **kw):
        handoff(directory, kw, 1)

    def analyze(path, progress, event):
        return [{"start": start, "end": end, "face_crop": CROP}]

    rendered = []

    def render(path, candidate, output, progress, event):
        rendered.append(candidate["end"])
        output.write_bytes(b"validated")

    store = runner(producer, analyzer=analyze, renderer=render)
    job = terminal(store, store.create(SOURCE)["id"])
    if accepted:
        assert job["status"] == "succeeded"
        assert rendered == [0.9]
        assert job["candidates"][0]["end"] == 0.9
        assert job["exports"][0]["global_end"] == 0.9
    else:
        assert job["status"] == "failed"
        assert "extends past validated window media" in job["error"]
        assert not rendered and not job["exports"]


def test_reopen_discards_owned_orphan_and_marks_interrupted_windows(runner):
    def producer(_, directory, **kw):
        handoff(directory, kw, 1)

    store = runner(producer)
    job = terminal(store, store.create(SOURCE)["id"])
    job_id = job["id"]
    store.close()
    root = store.root / job_id
    scratch = root / jobs.ROLLING_WORK
    scratch.mkdir()
    (scratch / "unfinished.ts").write_bytes(b"unfinished")
    orphan = root / jobs.ROLLING_EXPORTS / ("0" * 32 + ".mp4")
    orphan.write_bytes(b"uncommitted")
    non_owned = root / ("1" * 32 + ".mp4")
    non_owned.write_bytes(b"untouched")
    with sqlite3.connect(store.db) as db:
        db.execute("""UPDATE jobs SET data=json_set(data,'$.status','running',
                      '$.live_session.windows[1]',json('{"status":"processing"}')) WHERE id=?""", (job_id,))
    reopened = jobs.JobStore(store.root)
    try:
        interrupted = reopened.get(job_id)
        assert interrupted["status"] == "interrupted"
        assert interrupted["live_session"]["windows"][1]["status"] == "discarded"
        assert not scratch.exists() and not orphan.exists() and non_owned.is_file()
        assert reopened.media_path(job_id, job["exports"][0]["filename"]).is_file()
    finally:
        reopened.close()


def test_rolling_source_validates_bounds_and_never_resolves_vod(runner, monkeypatch):
    def producer(*args, **kw):
        pytest.fail("Rolling capture must never be attempted on VOD")

    def resolve(*args, **kw):
        assert kw["mode"] == "live"
        return SimpleNamespace(is_live=False)

    store = runner(producer)
    monkeypatch.setattr(sources, "resolve_source", resolve)
    job = terminal(store, store.create({"type": "url", "url": "https://twitch.tv/streamer",
                                        "rolling": True, "mode": "auto"})["id"])
    assert job["status"] == "failed" and "cannot use a VOD" in job["error"]
    assert job["source"]["session_duration"] == 600 and job["source"]["window_duration"] == 60
    for fields in ({"rolling": 1}, {"rolling": False}, {"mode": "latest"},
                   {"duration": 30}, {"session_duration": 1801}, {"window_duration": 61}):
        with pytest.raises(ValueError):
            store.create({**SOURCE, **fields})


def candidate_finished(store, job_id, status="succeeded", timeout=5):
    return wait_for(lambda: (job if (job := store.get(job_id))["candidates"][0].get(
        "export_status") == status and not store.corrections else None), timeout=timeout)


def test_edit_export_overlaps_capture_and_remains_editable_after_stop_and_restart(runner, monkeypatch):
    monkeypatch.setattr(jobs, "ROLLING_DISK_BYTES", 80)
    monkeypatch.setattr(jobs, "ROLLING_ENCODE_BYTES", 16)
    monkeypatch.setattr(jobs, "ROLLING_EXPORT_BYTES", 16)
    monkeypatch.setattr(live, "MAX_SEGMENT_BYTES", 8)
    next_window, rendering, finish = (threading.Event() for _ in range(3))

    def producer(_, directory, **kw):
        handoff(directory, kw, 1)
        assert next_window.wait(4)
        handoff(directory, kw, 2)
        assert kw["cancelled"].wait(4)

    def render(path, candidate, output, progress, event):
        if jobs.ROLLING_EDITS in output.parts:
            rendering.set()
            assert finish.wait(4)
        output.write_bytes(path.read_bytes() + str(candidate["start"]).encode())

    store = runner(producer, renderer=render)
    job_id = store.create(SOURCE)["id"]
    try:
        job = wait_for(lambda: (j if len((j := store.get(job_id))["exports"]) == 1 else None))
        candidate = job["candidates"][0]
        assert candidate["editable"] and candidate["window_duration"] == pytest.approx(0.9)
        assert candidate["source_media_url"] == f"/api/jobs/{job_id}/candidates/window-1/media"
        assert store.candidate_media_path(job_id, "window-1").read_bytes() == b"window 1"
        store.edit(job_id, "window-1", {"start": 0.3})
        store.export(job_id, "window-1")
        assert rendering.wait(4)
        with pytest.raises(jobs.Conflict, match="active export"):
            store.edit(job_id, "window-1", {"start": 0.4})
        with pytest.raises(jobs.Conflict):
            store.export(job_id, "window-1")
        next_window.set()
        # A second window is encoded, analyzed and published while the user's
        # correction renderer is blocked: neither capture nor consumer is held.
        job = wait_for(lambda: (j if (j := store.get(job_id))[
            "live_session"]["windows_processed"] == 2 else None))
        assert job["status"] == "running" and job["candidates"][0]["export_status"] == "running"
        disk = store._rolling_disk(job_id)
        wait_for(lambda: disk.retained == 38)
        assert disk.retained == 38 and disk.working == 16
        assert disk.disk_remaining() == 26
        assert store.candidate_media_path(job_id, "window-2").read_bytes() == b"window 2"
        finish.set()
        job = candidate_finished(store, job_id)
        assert len(job["exports"]) == 3 and job["exports"][-1]["revision"] == 1
        assert job["exports"][-1]["global_start"] == 0.3
        store.cancel(job_id)
        stopped = terminal(store, job_id)
        assert stopped["status"] == "cancelled"
        store.edit(job_id, "window-1", {"start": 0.4})
        store.export(job_id, "window-1")
        job = candidate_finished(store, job_id)
        assert job["status"] == "cancelled" and len(job["exports"]) == 4
        for record in job["exports"]:
            assert store.media_path(job_id, record["filename"]).is_file()
    finally:
        finish.set()
        next_window.set()
        store.close()
    reopened = jobs.JobStore(store.root)
    try:
        assert reopened.get(job_id)["exports"] == job["exports"]
        assert reopened.candidate_media_path(job_id, "window-1").read_bytes() == b"window 1"
        assert reopened.candidate_media_path(job_id, "window-2").read_bytes() == b"window 2"
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_correction_failure_or_cancel_keeps_previous_outputs_and_source(runner, failure):
    rendering = threading.Event()

    def producer(_, directory, **kw):
        handoff(directory, kw, 1)

    def render(path, candidate, output, progress, event):
        output.write_bytes(b"partial" if jobs.ROLLING_EDITS in output.parts else b"validated")
        if jobs.ROLLING_EDITS in output.parts:
            rendering.set()
            if failure == "cancel":
                assert event.wait(4)
            else:
                raise ValueError("renderer failed")

    store = runner(producer, renderer=render)
    job = terminal(store, store.create(SOURCE)["id"])
    job_id = job["id"]
    original = job["exports"][0]
    store.edit(job_id, "window-1", {"start": 0.3})
    store.export(job_id, "window-1")
    assert rendering.wait(4)
    if failure == "cancel":
        store.cancel(job_id)
    job = candidate_finished(store, job_id, "failed" if failure == "error" else "cancelled")
    assert job["status"] == "succeeded" and job["exports"] == [original]
    assert store.media_path(job_id, original["filename"]).read_bytes() == b"validated"
    assert store.candidate_media_path(job_id, "window-1").read_bytes() == b"window 1"
    assert not list((store.root / job_id / jobs.ROLLING_EDITS).glob("*"))
    assert store._rolling_disk(job_id).working == 0


def test_stopping_capture_does_not_remove_inflight_correction_workspace(runner):
    rendering, release = threading.Event(), threading.Event()
    outputs = []

    def producer(_, directory, **kw):
        handoff(directory, kw, 1)
        assert kw["cancelled"].wait(4)

    def render(path, candidate, output, progress, event):
        output.write_bytes(b"validated")
        if jobs.ROLLING_EDITS in output.parts:
            outputs.append(output)
            rendering.set()
            assert release.wait(4)

    store = runner(producer, renderer=render)
    job_id = store.create(SOURCE)["id"]
    try:
        original = wait_for(lambda: (j["exports"][0] if (j := store.get(job_id))["exports"] else None))
        store.export(job_id, "window-1")
        assert rendering.wait(4)
        store.cancel(job_id)
        assert terminal(store, job_id)["status"] == "cancelled"
        assert outputs[0].is_file(), "capture teardown must not delete an active correction's workspace"
        assert store.media_path(job_id, original["filename"]).read_bytes() == b"validated"
        assert store.candidate_media_path(job_id, "window-1").read_bytes() == b"window 1"
    finally:
        release.set()
    job = candidate_finished(store, job_id, "cancelled")
    assert job["exports"] == [original]


def test_correction_is_rejected_if_it_would_steal_capture_headroom(runner, monkeypatch):
    monkeypatch.setattr(jobs, "ROLLING_DISK_BYTES", 56)
    monkeypatch.setattr(jobs, "ROLLING_ENCODE_BYTES", 16)
    monkeypatch.setattr(jobs, "ROLLING_EXPORT_BYTES", 16)
    monkeypatch.setattr(live, "MAX_SEGMENT_BYTES", 8)
    next_window = threading.Event()

    def producer(_, directory, **kw):
        handoff(directory, kw, 1)
        assert next_window.wait(4)
        handoff(directory, kw, 2)
        assert kw["cancelled"].wait(4)

    store = runner(producer)
    job_id = store.create(SOURCE)["id"]
    try:
        wait_for(lambda: store._rolling_disk(job_id).retained == 16)
        with pytest.raises(jobs.Conflict, match="capture headroom"):
            store.export(job_id, "window-1")
        assert not store.corrections and store._rolling_disk(job_id).working == 0
        next_window.set()
        wait_for(lambda: len(store.get(job_id)["exports"]) == 2)
        assert store.get(job_id)["live_session"]["acquiring"]
        store.cancel(job_id)
        terminal(store, job_id)
        store.export(job_id, "window-1")
        job = candidate_finished(store, job_id)
        assert job["status"] == "cancelled" and len(job["exports"]) == 3
    finally:
        next_window.set()


def test_source_directory_parent_is_synced_before_candidate_publication(runner, monkeypatch):
    synced = []
    sync = jobs._sync_path

    def record(path):
        if (path / jobs.ROLLING_SOURCES).is_dir():
            synced.append("parent")
        elif path.name == jobs.ROLLING_SOURCES:
            synced.append("sources")
        sync(path)

    def producer(_, directory, **kw):
        handoff(directory, kw, 1)

    monkeypatch.setattr(jobs, "_sync_path", record)
    store = runner(producer)
    job = terminal(store, store.create(SOURCE)["id"])
    assert job["candidates"][0]["editable"]
    assert synced == ["parent", "sources"]


def test_recovery_reconciles_owned_orphan_sources_without_refunding_budget(runner, monkeypatch):
    monkeypatch.setattr(jobs, "ROLLING_DISK_BYTES", 32)

    def producer(_, directory, **kw):
        handoff(directory, kw, 1)

    store = runner(producer)
    job = terminal(store, store.create(SOURCE)["id"])
    job_id = job["id"]
    store.close()
    directory = store.root / job_id / jobs.ROLLING_SOURCES
    orphan = directory / "window-999.mp4"
    orphan.write_bytes(b"orphaned")
    non_owned = directory / "keep.txt"
    non_owned.write_bytes(b"keep")
    reopened = jobs.JobStore(store.root)
    try:
        assert not orphan.exists() and non_owned.read_bytes() == b"keep"
        assert reopened.candidate_media_path(job_id, "window-1").read_bytes() == b"window 1"
        assert reopened._rolling_disk(job_id).retained == 28
        assert reopened._rolling_disk(job_id).reserve(100) == 4
        assert reopened.media_path(job_id, job["exports"][0]["filename"]).is_file()
    finally:
        reopened.close()


def test_shared_budget_is_persistent_and_cannot_be_reset_by_deleting_exports(runner, monkeypatch):
    monkeypatch.setattr(jobs, "ROLLING_DISK_BYTES", 40)

    def producer(_, directory, **kw):
        handoff(directory, kw, 1)

    store = runner(producer)
    job = terminal(store, store.create(SOURCE)["id"])
    job_id = job["id"]
    assert store._rolling_disk(job_id).retained == 16  # original window plus export
    for expected in (24, 32, 40):
        store.export(job_id, "window-1")
        candidate_finished(store, job_id)
        assert store._rolling_disk(job_id).retained == expected
    for record in store.get(job_id)["exports"]:
        store.media_path(job_id, record["filename"]).unlink()
    with pytest.raises(ValueError, match="disk budget exhausted"):
        store.export(job_id, "window-1")
    store.close()
    monkeypatch.setattr(jobs, "ROLLING_DISK_BYTES", 1000)
    reopened = jobs.JobStore(store.root)
    try:
        assert reopened._rolling_disk(job_id).retained == 40
        assert reopened._rolling_disk(job_id).limit == 40
        with pytest.raises(ValueError, match="disk budget exhausted"):
            reopened.export(job_id, "window-1")
        with pytest.raises(jobs.Conflict, match="budget"):
            reopened.resume(job_id)
        assert reopened.candidate_media_path(job_id, "window-1").read_bytes() == b"window 1"
    finally:
        reopened.close()


def test_capture_stops_explicitly_at_shared_cap_without_evicting_completed_windows(runner, monkeypatch):
    monkeypatch.setattr(jobs, "ROLLING_DISK_BYTES", 32)
    permits = [threading.Event(), threading.Event()]
    exhausted = threading.Event()

    def producer(_, directory, **kw):
        for index in (1, 2):
            handoff(directory, kw, index)
            assert permits[index - 1].wait(4)
        with kw["disk_lock"]:
            assert kw["disk_remaining"]() == 0
        exhausted.set()
        raise live.LiveCaptureError("Live session exceeds its cumulative disk byte limit")

    store = runner(producer)
    job_id = store.create(SOURCE)["id"]
    try:
        for index, permit in enumerate(permits, 1):
            wait_for(lambda: len(store.get(job_id)["exports"]) == index)
            # Wait for the consumer's reservation-to-retained accounting handoff.
            wait_for(lambda: store._rolling_disk(job_id).retained == index * 16)
            permit.set()
        job = terminal(store, job_id)
        assert exhausted.is_set() and job["status"] == "failed"
        assert "cumulative disk byte limit" in job["error"]
        assert len(job["exports"]) == 2
        for candidate in job["candidates"]:
            assert store.candidate_media_path(job_id, candidate["id"]).is_file()
        for record in job["exports"]:
            assert store.media_path(job_id, record["filename"]).is_file()
        assert store._rolling_disk(job_id).retained == 32
    finally:
        for permit in permits:
            permit.set()


def test_inflight_reservations_survive_restart_and_source_deletion_cannot_free_budget(runner, monkeypatch):
    monkeypatch.setattr(jobs, "ROLLING_DISK_BYTES", 40)

    def producer(_, directory, **kw):
        handoff(directory, kw, 1)

    store = runner(producer)
    job = terminal(store, store.create(SOURCE)["id"])
    job_id = job["id"]
    assert store._rolling_disk(job_id).reserve(100) == 24
    store.candidate_media_path(job_id, "window-1").unlink()
    store.close()
    reopened = jobs.JobStore(store.root)
    try:
        disk = reopened._rolling_disk(job_id)
        assert disk.retained == 40 and disk.working == 0
        with pytest.raises(ValueError, match="disk budget"):
            disk.reserve(1)
        with pytest.raises(KeyError):
            reopened.candidate_media_path(job_id, "window-1")
    finally:
        reopened.close()


def test_no_face_window_can_be_corrected_and_boundary_and_preview_guards_hold(runner):
    def producer(_, directory, **kw):
        handoff(directory, kw, 1)

    store = runner(producer, analyzer=lambda *_: [{"start": 0.1, "end": 0.7}])
    job = terminal(store, store.create(SOURCE)["id"])
    job_id = job["id"]
    assert job["candidates"][0]["editable"] and not job["exports"]
    with pytest.raises(ValueError, match="face crop"):
        store.export(job_id, "window-1")
    for patch in ({"end": 0.901}, {"editable": True}, {"source_media_url": "/wrong"}):
        with pytest.raises(ValueError):
            store.edit(job_id, "window-1", patch)
    for identifier in ("../window-1", "window-2", "source", "window-0"):
        with pytest.raises(KeyError):
            store.candidate_media_path(job_id, identifier)
    store.edit(job_id, "window-1", {"face_crop": CROP, "end": 0.9})
    store.export(job_id, "window-1")
    job = candidate_finished(store, job_id)
    assert len(job["exports"]) == 1 and job["exports"][0]["global_end"] == 0.9


def test_concurrent_allocations_never_release_each_others_reserved_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "ROLLING_DISK_BYTES", 100)
    disk = jobs._RollingDisk(tmp_path)
    consumer, correction = disk.allocation(), disk.allocation()
    assert consumer.reserve(60) == 60
    assert correction.reserve(60) == 40
    consumer.reduce(30)
    consumer.finish_window(30)
    assert disk.retained == 30 and disk.working == 40
    assert disk.disk_remaining() == 30
    correction.finish_window(20)
    assert disk.retained == 50 and disk.working == 0


def test_window_transcript_expansion_preserves_edits_and_deleted_words(runner):
    words = [{"start": start, "end": start + 0.05, "text": text}
             for start, text in ((0.1, "before"), (0.3, "edited"), (0.4, "deleted"), (0.8, "after"))]

    def producer(_, directory, **kw):
        handoff(directory, kw, 1)

    def analyze(*_):
        return {"source_words": words,
                "candidates": [{"start": 0.2, "end": 0.7, "face_crop": CROP, "words": words[1:3]}]}

    store = runner(producer, analyzer=analyze)
    job = terminal(store, store.create(SOURCE)["id"])
    job_id = job["id"]
    store.edit(job_id, "window-1", {"words": [{**words[1], "correction": "corrected"}]})
    store.edit(job_id, "window-1", {"start": 0, "end": 0.9})
    candidate = store.get(job_id)["candidates"][0]
    assert [w["text"] for w in candidate["words"]] == ["before", "edited", "after"]
    assert candidate["words"][1]["correction"] == "corrected"


def test_real_ffmpeg_reexports_retained_window_during_capture_and_after_stop(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Requires FFmpeg and ffprobe")
    filters = subprocess.check_output(["ffmpeg", "-hide_banner", "-filters"], text=True)
    if not any(line.split()[1:2] == ["ass"] for line in filters.splitlines()):
        pytest.skip("Caption correction requires FFmpeg with libass")
    fixture = tmp_path / "fixture.ts"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", str(fixture),
    ], check=True, timeout=15)
    payload = fixture.read_bytes()

    def producer(_, directory, **kw):
        handoff(directory, kw, 1, payload=payload)
        assert kw["cancelled"].wait(30)

    def analyze(*_):
        return [{"start": 0.1, "end": 0.7, "face_crop": CROP,
                 "words": [{"start": 0.2, "end": 0.6, "text": "Original caption"}]}]

    render = jobs._rolling_render

    def checked_render(media, candidate, output, event, quota, maximum=None):
        if maximum is not None:
            assert quota.working == maximum + jobs._rolling_render_overhead(candidate)
        return render(media, candidate, output, event, quota, maximum=maximum)

    monkeypatch.setattr(jobs, "_rolling_render", checked_render)
    monkeypatch.setattr(live, "capture_windows", producer)
    monkeypatch.setattr(sources, "resolve_source", lambda *_args, **_kw: SimpleNamespace(
        url="https://www.twitch.tv/streamer", platform="twitch", title="Fixture",
        selection="live", source_id="streamer", is_live=True))
    store = jobs.JobStore(tmp_path / "jobs", analyzer=analyze)
    try:
        job_id = store.create(SOURCE)["id"]
        job = wait_for(lambda: (j if (j := store.get(job_id))["exports"] else None), timeout=30)
        original = store.media_path(job_id, job["exports"][0]["filename"]).read_bytes()
        for correction in ("During capture", "After stop"):
            store.edit(job_id, "window-1", {"words": [
                {"start": 0.2, "end": 0.6, "text": "Original caption", "correction": correction}]})
            store.export(job_id, "window-1")
            job = candidate_finished(store, job_id, timeout=30)
            output = store.media_path(job_id, job["exports"][-1]["filename"])
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(output),
                            "-f", "null", "-"], check=True, timeout=15)
            if correction == "During capture":
                assert job["status"] == "running" and job["live_session"]["acquiring"]
                store.cancel(job_id)
                assert terminal(store, job_id)["status"] == "cancelled"
        assert len(job["exports"]) == 3 and job["status"] == "cancelled"
        assert store.media_path(job_id, job["exports"][0]["filename"]).read_bytes() == original
        retained = store.candidate_media_path(job_id, "window-1")
        actual = sum(p.stat().st_size for name in (jobs.ROLLING_SOURCES, jobs.ROLLING_EXPORTS)
                     for p in (store.root / job_id / name).glob("*"))
        assert retained.is_file() and store._rolling_disk(job_id).retained == actual
        assert actual < jobs.ROLLING_DISK_BYTES
    finally:
        store.close()
