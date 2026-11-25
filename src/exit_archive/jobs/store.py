"""SQLite job metadata (stdlib ``sqlite3``). There is no token and no account id column.

Every method opens its own short-lived connection, so the store is safe to call from the event
loop or from worker threads (``asyncio.to_thread``). ``archive_path`` is relative to DATA_DIR.
Timestamps are ISO-8601 UTC strings of one fixed format, so string order equals time order.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from exit_archive.errors import SessionInUseError

__all__ = ["Clock", "Job", "JobStore", "SessionInUseError", "Status", "to_iso", "utcnow"]

Status = Literal["queued", "running", "done", "failed", "expired"]
Clock = Callable[[], datetime]
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SESSION_INDEX = "jobs_checkout_session_live"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    checkout_session_id TEXT NULL,
    status TEXT NOT NULL,
    stage TEXT,
    done INTEGER,
    total INTEGER,
    error TEXT,
    created_at TEXT,
    finished_at TEXT,
    expires_at TEXT,
    archive_path TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS {_SESSION_INDEX} ON jobs(checkout_session_id)
    WHERE status IN ('queued','running','done','expired');
"""
_COLUMNS = (
    "id, checkout_session_id, status, stage, done, total, error, "
    "created_at, finished_at, expires_at, archive_path"
)

_GET_SQL = f"SELECT {_COLUMNS} FROM jobs WHERE id = ?"  # noqa: S608 - constant columns only
_EXPIRED_SQL = _GET_SQL.replace("id = ?", "status = 'done' AND expires_at < ?")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    """Format an aware datetime as UTC ISO-8601 (``2026-11-25T12:00:00Z``)."""
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return moment.astimezone(UTC).strftime(_TIME_FORMAT)


@dataclass(frozen=True)
class Job:
    id: str
    checkout_session_id: str | None
    status: Status
    stage: str | None
    done: int | None
    total: int | None
    error: str | None
    created_at: str | None
    finished_at: str | None
    expires_at: str | None
    archive_path: str | None


def _job(row: sqlite3.Row | tuple[object, ...]) -> Job:
    return Job(*row)


class JobStore:
    def __init__(self, db_path: Path, clock: Clock = utcnow) -> None:
        self.db_path = Path(db_path)
        self._clock = clock
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def _execute(self, sql: str, params: tuple[object, ...] = ()) -> int:
        with closing(self._connect()) as conn, conn:
            return conn.execute(sql, params).rowcount

    def _query(self, sql: str, params: tuple[object, ...] = ()) -> list[Job]:
        with closing(self._connect()) as conn:
            return [_job(r) for r in conn.execute(sql, params).fetchall()]

    def create(self, job_id: str, checkout_session_id: str | None) -> Job:
        """Insert a queued job; raise ``SessionInUseError`` if the session is already used."""
        created_at = to_iso(self._clock())
        try:
            self._execute(
                "INSERT INTO jobs (id, checkout_session_id, status, created_at) "
                "VALUES (?, ?, 'queued', ?)",
                (job_id, checkout_session_id, created_at),
            )
        except sqlite3.IntegrityError as exc:
            if "checkout_session_id" in str(exc):
                raise SessionInUseError from None
            raise
        return Job(
            job_id,
            checkout_session_id,
            "queued",
            None,
            None,
            None,
            None,
            created_at,
            None,
            None,
            None,
        )

    def get(self, job_id: str) -> Job | None:
        rows = self._query(_GET_SQL, (job_id,))
        return rows[0] if rows else None

    def mark_running(self, job_id: str) -> None:
        self._execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))

    def update_progress(self, job_id: str, stage: str, done: int, total: int | None) -> None:
        self._execute(
            "UPDATE jobs SET stage = ?, done = ?, total = ? WHERE id = ?",
            (stage, done, total, job_id),
        )

    def mark_done(self, job_id: str, archive_path: str, finished_at: str, expires_at: str) -> None:
        self._execute(
            "UPDATE jobs SET status = 'done', archive_path = ?, finished_at = ?, "
            "expires_at = ? WHERE id = ?",
            (archive_path, finished_at, expires_at, job_id),
        )

    def mark_failed(self, job_id: str, error: str) -> None:
        self._execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
            (error, to_iso(self._clock()), job_id),
        )

    def mark_expired(self, job_id: str) -> None:
        self._execute("UPDATE jobs SET status = 'expired' WHERE id = ?", (job_id,))

    def list_expired(self, now: datetime) -> list[Job]:
        """Finished jobs whose archive is past ``expires_at``."""
        return self._query(_EXPIRED_SQL, (to_iso(now),))

    def fail_unfinished(self, reason: str) -> int:
        """Fail every queued or running job (used at startup); returns how many."""
        return self._execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? "
            "WHERE status IN ('queued', 'running')",
            (reason, to_iso(self._clock())),
        )

    def active_job_ids(self) -> set[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
        return {r[0] for r in rows}
