"""Idempotency ledger for published clips.

One row per (clip, platform). A clip is identified by the SHA-256 of its bytes,
so re-rendering the same file is the same clip and a different render is a new
one. The row is claimed with ``BEGIN IMMEDIATE`` before any network call, which
makes two concurrent runs — or a retried cron job — publish exactly once.

States:

``in_flight``    claimed by a running process; a crash leaves it here
``published``    live on the platform; never attempted again
``needs_manual`` delivered but waiting for a human (TikTok inbox drafts)
``failed``       retryable, up to ``max_attempts``

``recover_stale`` returns crashed ``in_flight`` rows to ``failed`` so a killed
process does not block a clip forever.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import time
from pathlib import Path

CLAIMED = "claimed"
DONE = "done"
IN_FLIGHT = "in_flight"
RETRY_EXHAUSTED = "retry_exhausted"

TERMINAL_STATES = {"published", "needs_manual"}
MAX_ATTEMPTS = 3
STALE_SECONDS = 900

_SCHEMA = """
CREATE TABLE IF NOT EXISTS publications (
    clip_key    TEXT NOT NULL,
    platform    TEXT NOT NULL,
    status      TEXT NOT NULL,
    platform_id TEXT,
    url         TEXT,
    detail      TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (clip_key, platform)
);
CREATE INDEX IF NOT EXISTS publications_status ON publications (status, updated_at);
"""


@dataclass(frozen=True)
class Decision:
    action: str
    record: dict | None = None

    @property
    def should_publish(self):
        return self.action == CLAIMED


@dataclass(frozen=True)
class Record:
    clip_key: str
    platform: str
    status: str
    platform_id: str | None
    url: str | None
    detail: str | None
    error: str | None
    attempts: int
    created_at: float
    updated_at: float


class Ledger:
    """Local SQLite ledger; safe across threads and processes."""

    def __init__(self, path, *, max_attempts=MAX_ATTEMPTS, stale_seconds=STALE_SECONDS):
        self.path = Path(path)
        self.max_attempts = max_attempts
        self.stale_seconds = stale_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        if not self._initialized:
            db.executescript(_SCHEMA)
            self._initialized = True
        return db

    # -- claims ---------------------------------------------------------

    def begin(self, clip_key, platform, *, now=None):
        """Atomically claim (clip, platform), or report why it is skipped."""
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM publications WHERE clip_key=? AND platform=?",
                (clip_key, platform),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO publications "
                    "(clip_key, platform, status, attempts, created_at, updated_at) "
                    "VALUES (?, ?, 'in_flight', 1, ?, ?)",
                    (clip_key, platform, now, now),
                )
                db.execute("COMMIT")
                return Decision(CLAIMED)
            record = _record(row)
            if record.status in TERMINAL_STATES:
                db.execute("COMMIT")
                return Decision(DONE, record)
            if (record.status == "in_flight"
                    and now - record.updated_at < self.stale_seconds):
                db.execute("COMMIT")
                return Decision(IN_FLIGHT, record)
            if record.attempts >= self.max_attempts:
                db.execute("COMMIT")
                return Decision(RETRY_EXHAUSTED, record)
            db.execute(
                "UPDATE publications SET status='in_flight', attempts=attempts+1, "
                "error=NULL, updated_at=? WHERE clip_key=? AND platform=?",
                (now, clip_key, platform),
            )
            db.execute("COMMIT")
            return Decision(CLAIMED, record)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    # -- outcomes -------------------------------------------------------

    def finish(self, clip_key, platform, status, *, platform_id=None, url=None,
               detail=None, error=None, now=None):
        if status not in TERMINAL_STATES | {"failed"}:
            raise ValueError("Unknown ledger status")
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute(
                "UPDATE publications SET status=?, platform_id=?, url=?, detail=?, "
                "error=?, updated_at=? WHERE clip_key=? AND platform=?",
                (status, platform_id, url, detail, error, now, clip_key, platform),
            )
        finally:
            db.close()

    def fail(self, clip_key, platform, message, *, now=None):
        self.finish(clip_key, platform, "failed", error=message, now=now)

    def reset(self, clip_key, platform, *, now=None):
        """Clear the attempt count so an operator can retry after fixing a cause."""
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute(
                "UPDATE publications SET status='failed', attempts=0, error=NULL, "
                "updated_at=? WHERE clip_key=? AND platform=?",
                (now, clip_key, platform),
            )
        finally:
            db.close()

    def recover_stale(self, *, now=None):
        """Return crashed claims to ``failed``; returns how many were recovered."""
        now = time.time() if now is None else now
        cutoff = now - self.stale_seconds
        db = self._connect()
        try:
            cursor = db.execute(
                "UPDATE publications SET status='failed', updated_at=?, "
                "error=COALESCE(error, 'interrupted') "
                "WHERE status='in_flight' AND updated_at < ?",
                (now, cutoff),
            )
            return cursor.rowcount
        finally:
            db.close()

    # -- reads ----------------------------------------------------------

    def records(self, clip_key=None):
        if not self.path.exists():
            return []
        db = self._connect()
        try:
            if clip_key:
                rows = db.execute(
                    "SELECT * FROM publications WHERE clip_key=? ORDER BY platform",
                    (clip_key,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM publications ORDER BY created_at DESC, platform"
                ).fetchall()
            return [_record(row) for row in rows]
        finally:
            db.close()

    def status_for(self, clip_key):
        return {record.platform: record for record in self.records(clip_key)}


def _record(row):
    return Record(
        clip_key=row["clip_key"],
        platform=row["platform"],
        status=row["status"],
        platform_id=row["platform_id"],
        url=row["url"],
        detail=row["detail"],
        error=row["error"],
        attempts=row["attempts"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
