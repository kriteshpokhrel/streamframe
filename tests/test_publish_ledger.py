from streamframe.publish.ledger import (
    CLAIMED,
    DONE,
    IN_FLIGHT,
    RETRY_EXHAUSTED,
    Ledger,
)


def test_a_clip_claims_once_then_reports_done(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    first = ledger.begin("clip", "youtube")
    assert first.action == CLAIMED

    ledger.finish("clip", "youtube", "published", platform_id="abc",
                  url="https://youtu.be/abc")

    second = ledger.begin("clip", "youtube")
    assert second.action == DONE
    assert second.record.platform_id == "abc"


def test_platforms_are_independent(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    assert ledger.begin("clip", "youtube").action == CLAIMED
    ledger.finish("clip", "youtube", "published")
    assert ledger.begin("clip", "instagram").action == CLAIMED


def test_in_flight_claims_block_a_second_process(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    assert ledger.begin("clip", "tiktok").action == CLAIMED
    again = ledger.begin("clip", "tiktok")
    assert again.action == IN_FLIGHT


def test_failures_retry_up_to_the_limit(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3", max_attempts=2)
    assert ledger.begin("clip", "youtube").action == CLAIMED
    ledger.fail("clip", "youtube", "boom")
    assert ledger.begin("clip", "youtube").action == CLAIMED
    ledger.fail("clip", "youtube", "boom")
    assert ledger.begin("clip", "youtube").action == RETRY_EXHAUSTED


def test_stale_claims_recover_after_a_crash(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3", stale_seconds=60)
    assert ledger.begin("clip", "youtube", now=1000).action == CLAIMED
    assert ledger.begin("clip", "youtube", now=1030).action == IN_FLIGHT

    assert ledger.recover_stale(now=1200) == 1
    assert ledger.begin("clip", "youtube", now=1201).action == CLAIMED


def test_reset_clears_the_attempt_budget(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3", max_attempts=1)
    ledger.begin("clip", "youtube")
    ledger.fail("clip", "youtube", "boom")
    assert ledger.begin("clip", "youtube").action == RETRY_EXHAUSTED
    ledger.reset("clip", "youtube")
    assert ledger.begin("clip", "youtube").action == CLAIMED


def test_records_are_ordered_and_filterable(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.begin("a", "youtube", now=1)
    ledger.begin("b", "tiktok", now=2)
    assert [row.clip_key for row in ledger.records()] == ["b", "a"]
    assert [row.platform for row in ledger.records("a")] == ["youtube"]
