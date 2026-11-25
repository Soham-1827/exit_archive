"""JobStore: schema, status transitions, session uniqueness (T3.1.2)."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exit_archive.errors import ExitArchiveError
from exit_archive.jobs.store import JobStore, SessionInUseError

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3", clock=lambda: NOW)


def _columns(db: Path) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [row[1] for row in conn.execute("PRAGMA table_info(jobs)")]


def test_schema_has_expected_columns_and_no_token_or_account(store: JobStore) -> None:
    cols = _columns(store.db_path)
    assert cols == [
        "id",
        "checkout_session_id",
        "status",
        "stage",
        "done",
        "total",
        "error",
        "created_at",
        "finished_at",
        "expires_at",
        "archive_path",
    ]
    assert not any("token" in c.lower() or "account" in c.lower() for c in cols)


def test_creates_parent_directory(tmp_path: Path) -> None:
    JobStore(tmp_path / "nested" / "deeper" / "jobs.sqlite3")
    assert (tmp_path / "nested" / "deeper" / "jobs.sqlite3").exists()


def test_create_returns_queued_job_with_utc_iso_timestamp(store: JobStore) -> None:
    job = store.create("job1", "cs_1")
    assert job.status == "queued"
    assert job.checkout_session_id == "cs_1"
    assert job.created_at == "2026-11-25T12:00:00Z"
    assert job.finished_at is None and job.expires_at is None and job.archive_path is None
    assert store.get("job1") == job


def test_get_unknown_returns_none(store: JobStore) -> None:
    assert store.get("nope") is None


def test_full_success_lifecycle(store: JobStore) -> None:
    store.create("j", None)
    store.mark_running("j")
    store.update_progress("j", "records", 500, None)
    job = store.get("j")
    assert job is not None
    assert (job.status, job.stage, job.done, job.total) == ("running", "records", 500, None)
    store.mark_done("j", "archives/j/x.zip", "2026-11-25T12:30:00Z", "2026-12-02T12:30:00Z")
    job = store.get("j")
    assert job is not None
    assert job.status == "done"
    assert job.archive_path == "archives/j/x.zip"
    assert job.finished_at == "2026-11-25T12:30:00Z"
    assert job.expires_at == "2026-12-02T12:30:00Z"


def test_mark_failed_stores_error_and_finished_at(store: JobStore) -> None:
    store.create("j", None)
    store.mark_failed("j", "boom")
    job = store.get("j")
    assert job is not None
    assert (job.status, job.error, job.finished_at) == ("failed", "boom", "2026-11-25T12:00:00Z")


def test_mark_expired(store: JobStore) -> None:
    store.create("j", None)
    store.mark_done("j", "a/b.zip", "2026-11-25T12:30:00Z", "2026-12-02T12:30:00Z")
    store.mark_expired("j")
    job = store.get("j")
    assert job is not None and job.status == "expired"


def test_list_expired_only_done_rows_past_expiry(store: JobStore) -> None:
    for name, exp in [("old", "2026-11-20T00:00:00Z"), ("new", "2026-12-20T00:00:00Z")]:
        store.create(name, None)
        store.mark_done(name, f"archives/{name}/x.zip", "2026-11-19T00:00:00Z", exp)
    store.create("running", None)
    store.mark_running("running")
    assert [j.id for j in store.list_expired(NOW)] == ["old"]
    assert store.list_expired(NOW - timedelta(days=30)) == []


def test_list_expired_accepts_non_utc_aware_time(store: JobStore) -> None:
    store.create("old", None)
    store.mark_done("old", "a/x.zip", "2026-11-19T00:00:00Z", "2026-11-25T12:30:00Z")
    from datetime import timezone

    plus5 = timezone(timedelta(hours=5))
    # 2026-11-25 18:00+05:00 is 13:00Z, after the 12:30Z expiry
    assert len(store.list_expired(datetime(2026, 11, 25, 18, 0, tzinfo=plus5))) == 1
    assert store.list_expired(datetime(2026, 11, 25, 17, 0, tzinfo=plus5)) == []


def test_fail_unfinished_only_touches_queued_and_running(store: JobStore) -> None:
    store.create("q", None)
    store.create("r", None)
    store.mark_running("r")
    store.create("d", None)
    store.mark_done("d", "a/x.zip", "2026-11-25T12:30:00Z", "2026-12-02T12:30:00Z")
    assert store.fail_unfinished("server_restarted") == 2
    for job_id in ("q", "r"):
        job = store.get(job_id)
        assert job is not None
        assert (job.status, job.error) == ("failed", "server_restarted")
    done = store.get("d")
    assert done is not None and done.status == "done"


def test_active_job_ids(store: JobStore) -> None:
    store.create("q", None)
    store.create("r", None)
    store.mark_running("r")
    store.create("f", None)
    store.mark_failed("f", "x")
    assert store.active_job_ids() == {"q", "r"}


@pytest.mark.parametrize("blocking", ["queued", "running", "done", "expired"])
def test_second_job_on_same_session_conflicts(store: JobStore, blocking: str) -> None:
    store.create("first", "cs_dup")
    if blocking == "running":
        store.mark_running("first")
    elif blocking == "done":
        store.mark_done("first", "a/x.zip", "2026-11-25T12:30:00Z", "2026-12-02T12:30:00Z")
    elif blocking == "expired":
        store.mark_done("first", "a/x.zip", "2026-11-25T12:30:00Z", "2026-12-02T12:30:00Z")
        store.mark_expired("first")
    with pytest.raises(SessionInUseError):
        store.create("second", "cs_dup")
    assert store.get("second") is None


def test_session_in_use_error_is_typed_and_hides_session_id(store: JobStore) -> None:
    store.create("a", "cs_secret_session")
    with pytest.raises(SessionInUseError) as info:
        store.create("b", "cs_secret_session")
    assert isinstance(info.value, ExitArchiveError)
    assert "cs_secret_session" not in str(info.value)


def test_session_accepted_again_after_failed(store: JobStore) -> None:
    store.create("a", "cs_retry")
    store.mark_failed("a", "boom")
    store.create("b", "cs_retry")
    assert store.get("b") is not None
    with pytest.raises(SessionInUseError):
        store.create("c", "cs_retry")


def test_null_session_ids_never_conflict(store: JobStore) -> None:
    store.create("a", None)
    store.create("b", None)
    assert store.get("a") and store.get("b")


def test_duplicate_job_id_is_not_reported_as_session_conflict(store: JobStore) -> None:
    store.create("same", "cs_1")
    with pytest.raises(sqlite3.IntegrityError):
        store.create("same", "cs_2")


def test_partial_unique_index_exists(store: JobStore) -> None:
    with sqlite3.connect(store.db_path) as conn:
        sql = [r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE type='index'")]
    assert any("UNIQUE" in (s or "").upper() and "checkout_session_id" in (s or "") for s in sql)


def test_reopening_store_keeps_rows(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    JobStore(db).create("j", "cs_x")
    assert JobStore(db).get("j") is not None
