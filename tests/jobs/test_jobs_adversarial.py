"""Adversarial tests for T3.1 (tester). Findings are marked xfail(strict=True) with an id."""

import asyncio
import gc
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from exit_archive.errors import ExitArchiveError, SessionInUseError
from exit_archive.exporter import ExportResult
from exit_archive.jobs import cleanup
from exit_archive.jobs.runner import JobRunner
from exit_archive.jobs.store import JobStore
from exit_archive.secrets import HarvestToken, install_log_redaction

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
TOK = "tok-ADV-1234567890abcdef"  # noqa: S105


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3", clock=lambda: NOW)


def _live_tokens() -> list[HarvestToken]:
    gc.collect()
    return [o for o in gc.get_objects() if isinstance(o, HarvestToken)]


# ---------------------------------------------------------------- session race
def test_concurrent_creates_same_session_exactly_one_wins(store: JobStore) -> None:
    results: list[str] = []
    barrier = threading.Barrier(16)

    def go(i: int) -> None:
        barrier.wait()
        try:
            store.create(f"id{i}", "cs_same")
            results.append("ok")
        except SessionInUseError as exc:
            assert "cs_same" not in str(exc)
            results.append("conflict")

    threads = [threading.Thread(target=go, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 1
    assert results.count("conflict") == 15


def test_duplicate_job_id_is_not_reported_as_session_conflict(store: JobStore) -> None:
    import sqlite3

    store.create("dup", None)
    with pytest.raises(sqlite3.IntegrityError):
        store.create("dup", "cs_x")


# ---------------------------------------------------------------- redaction registry
def test_registry_threads_install_uninstall_restores_factory() -> None:
    original = logging.getLogRecordFactory()
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            for _ in range(200):
                un = install_log_redaction(HarvestToken(f"tok-thread-{i}-xxxxxxxx"))
                un()
                un()  # double uninstall is a no-op
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert logging.getLogRecordFactory() is original


def test_substring_tokens_fully_masked_any_uninstall_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    short, long_ = HarvestToken("abcdef123"), HarvestToken("abcdef123456789")
    u1, u2 = install_log_redaction(short), install_log_redaction(long_)
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger("x").info("v=%s", "abcdef123456789")
        assert "456789" not in caplog.text
        u2()
        caplog.clear()
        with caplog.at_level(logging.INFO):
            logging.getLogger("x").info("v=%s", "abcdef123456789")
        assert "abcdef123" not in caplog.text  # short still active
    finally:
        u1()
        u2()


def test_third_party_factory_replaced_in_between_is_preserved() -> None:
    original = logging.getLogRecordFactory()
    un = install_log_redaction(HarvestToken(TOK))
    marker = logging.getLogRecordFactory()

    def third(*a: Any, **k: Any) -> logging.LogRecord:
        rec = marker(*a, **k)
        rec.thirdparty = True  # type: ignore[attr-defined]
        return rec

    logging.setLogRecordFactory(third)
    try:
        un()
        assert logging.getLogRecordFactory() is third  # FINDING-R1 if restored to original
    finally:
        logging.setLogRecordFactory(original)


# ---------------------------------------------------------------- runner
async def _raising_export(token: HarvestToken, out_dir: Path, *_: Any) -> ExportResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    raise RuntimeError(f"boom {token.reveal()}")


async def test_no_token_object_survives_after_failed_and_cancelled_jobs(
    store: JobStore, tmp_path: Path
) -> None:
    runner = JobRunner(store, tmp_path, clock=lambda: NOW, export=_raising_export)
    await runner.submit(HarvestToken(TOK), None, "cs_1")
    await runner.wait_idle()

    started = asyncio.Event()

    async def hang(token: HarvestToken, out_dir: Path, *_: Any) -> ExportResult:
        started.set()
        await asyncio.sleep(3600)
        raise AssertionError

    r2 = JobRunner(store, tmp_path, clock=lambda: NOW, export=hang)
    await r2.submit(HarvestToken(TOK + "2"), None, "cs_2")
    await started.wait()
    await r2.aclose()
    for _ in range(5):
        await asyncio.sleep(0)
    live = _live_tokens()
    if live:
        for t in live:
            for r in gc.get_referrers(t):
                print("REFERRER", type(r), str(r)[:200])
    assert live == []


async def test_mark_failed_disk_error_does_not_leak_token_via_asyncio_log(
    store: JobStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """FINDING-J1: if the store fails while recording the failure, the task dies with a chained
    exception that asyncio logs after redaction was uninstalled."""
    runner = JobRunner(store, tmp_path, clock=lambda: NOW, export=_raising_export)

    def broken(*a: Any, **k: Any) -> None:
        raise OSError("disk full")

    store.mark_failed = broken  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    seen: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _l, ctx: seen.append(ctx))
    await runner.submit(HarvestToken(TOK), None, "cs_1")
    await asyncio.sleep(0.2)
    gc.collect()
    await asyncio.sleep(0)
    gc.collect()
    # What asyncio would print for the default handler:
    import traceback

    texts = []
    for ctx in seen:
        exc = ctx.get("exception")
        if exc is not None:
            texts.append("".join(traceback.format_exception(exc)))
    assert not any(TOK in t for t in texts), "token appears in unretrieved task exception"


async def test_mark_done_disk_error_marks_failed_without_token(
    store: JobStore, tmp_path: Path
) -> None:
    async def ok(token: HarvestToken, out_dir: Path, *_: Any) -> ExportResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        z = out_dir / "a.zip"
        z.write_bytes(b"x")
        return ExportResult(zip_path=z, counts={}, missing=[], skipped=[])

    def broken(*a: Any, **k: Any) -> None:
        raise OSError(f"disk full {TOK}")

    store.mark_done = broken  # type: ignore[method-assign]
    runner = JobRunner(store, tmp_path, clock=lambda: NOW, export=ok)
    jid = await runner.submit(HarvestToken(TOK), None, "cs_1")
    await runner.wait_idle()
    job = store.get(jid)
    assert job is not None and job.status == "failed"
    assert job.error == "OSError"
    assert not (tmp_path / "archives" / jid).exists()


async def test_aclose_immediately_after_submit_does_not_leave_queued_row(
    store: JobStore, tmp_path: Path
) -> None:
    async def never(*_: Any) -> ExportResult:
        raise AssertionError

    runner = JobRunner(store, tmp_path, clock=lambda: NOW, export=never)
    jid = await runner.submit(HarvestToken(TOK), None, "cs_1")
    await runner.aclose()
    job = store.get(jid)
    assert job is not None and job.status == "failed"
    # and the session must be reusable
    store.create("again", "cs_1")


async def test_semaphore_honoured_when_running_jobs_are_cancelled(
    store: JobStore, tmp_path: Path
) -> None:
    running = 0
    peak = 0
    gates: list[asyncio.Event] = []

    async def exp(token: HarvestToken, out_dir: Path, *_: Any) -> ExportResult:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        ev = asyncio.Event()
        gates.append(ev)
        try:
            await ev.wait()
            out_dir.mkdir(parents=True, exist_ok=True)
            z = out_dir / "a.zip"
            z.write_bytes(b"x")
            return ExportResult(zip_path=z, counts={}, missing=[], skipped=[])
        finally:
            running -= 1

    runner = JobRunner(store, tmp_path, max_concurrent=2, clock=lambda: NOW, export=exp)
    ids = [await runner.submit(HarvestToken(TOK + str(i)), None, None) for i in range(5)]
    for _ in range(5):
        await asyncio.sleep(0)
    assert running == 2
    next(iter(runner._tasks)).cancel()  # noqa: SLF001
    for _ in range(5):
        await asyncio.sleep(0)
    assert peak <= 2
    while len(runner._tasks) > 0:  # noqa: SLF001
        for g in gates:
            g.set()
        await asyncio.sleep(0.01)
    assert peak <= 2
    assert {store.get(i).status for i in ids} <= {"done", "failed"}  # type: ignore[union-attr]


async def test_error_text_has_no_traceback_or_repr(store: JobStore, tmp_path: Path) -> None:
    class Weird(ExitArchiveError):
        pass

    async def exp(token: HarvestToken, out_dir: Path, *_: Any) -> ExportResult:
        raise Weird(f"x {token.reveal()} Bearer {token.reveal()} " + "y" * 600)

    runner = JobRunner(store, tmp_path, clock=lambda: NOW, export=exp)
    jid = await runner.submit(HarvestToken(TOK), None, None)
    await runner.wait_idle()
    err = store.get(jid).error  # type: ignore[union-attr]
    assert err is not None and TOK not in err and "Traceback" not in err and len(err) <= 500


async def test_token_straddling_truncation_boundary_is_masked(
    store: JobStore, tmp_path: Path
) -> None:
    async def exp(token: HarvestToken, out_dir: Path, *_: Any) -> ExportResult:
        raise ExitArchiveError("a" * 490 + token.reveal())

    runner = JobRunner(store, tmp_path, clock=lambda: NOW, export=exp)
    jid = await runner.submit(HarvestToken(TOK), None, None)
    await runner.wait_idle()
    err = store.get(jid).error  # type: ignore[union-attr]
    assert err is not None and TOK[:6] not in err


# ---------------------------------------------------------------- cleanup
def _done_row(
    store: JobStore, jid: str, expires: datetime, archive_path: str | None = None
) -> None:
    store.create(jid, None)
    store.mark_done(
        jid,
        archive_path or f"archives/{jid}/a.zip",
        "2026-01-01T00:00:00Z",
        expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def test_hostile_archive_path_never_deletes_outside(store: JobStore, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    (tmp_path / "archives").mkdir()
    _done_row(store, "j1", NOW - timedelta(days=1), "../../../victim")
    _done_row(store, "j2", NOW - timedelta(days=1), "/etc")
    (tmp_path / "archives" / "j1").mkdir()
    res = cleanup.sweep(store, tmp_path, NOW)
    assert res.expired == 2
    assert (victim / "keep").exists()


def test_hostile_job_id_in_db_never_escapes(store: JobStore, tmp_path: Path) -> None:
    import sqlite3

    victim = tmp_path / "victim"
    victim.mkdir()
    (tmp_path / "archives").mkdir()
    with sqlite3.connect(store.db_path) as c:
        c.execute(
            "INSERT INTO jobs(id,status,expires_at) "
            "VALUES('../victim','done','2000-01-01T00:00:00Z')"
        )
    cleanup.sweep(store, tmp_path, NOW)
    assert victim.exists()


def test_archives_root_symlink_untouched(store: JobStore, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "orph").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    os.symlink(outside, tmp_path / "data" / "archives")
    s = JobStore(tmp_path / "data" / "j.sqlite3", clock=lambda: NOW)
    cleanup.sweep(s, tmp_path / "data", NOW)
    assert (outside / "orph").exists()


def test_symlink_job_dir_to_outside_not_followed(store: JobStore, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("x")
    root = tmp_path / "archives"
    root.mkdir()
    os.symlink(outside, root / "j1")
    _done_row(store, "j1", NOW - timedelta(days=1))
    cleanup.sweep(store, tmp_path, NOW)
    cleanup.sweep(store, tmp_path, NOW)
    assert (outside / "f").exists()
    assert not (root / "j1").exists()
    assert store.get("j1").status == "expired"  # type: ignore[union-attr]


def test_nested_symlinks_inside_job_dir_not_followed(store: JobStore, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("x")
    root = tmp_path / "archives"
    (root / "j1").mkdir(parents=True)
    os.symlink(outside, root / "j1" / "link")
    _done_row(store, "j1", NOW - timedelta(days=1))
    cleanup.sweep(store, tmp_path, NOW)
    assert (outside / "f").exists()


def test_symlink_to_sibling_job_dir_does_not_delete_sibling(
    store: JobStore, tmp_path: Path
) -> None:
    root = tmp_path / "archives"
    (root / "victim").mkdir(parents=True)
    (root / "victim" / "a.zip").write_text("x")
    store.create("victim", None)
    store.mark_done(
        "victim", "archives/victim/a.zip", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"
    )
    os.symlink(root / "victim", root / "evil")
    _done_row(store, "evil", NOW - timedelta(days=1))
    cleanup.sweep(store, tmp_path, NOW)
    assert (root / "victim" / "a.zip").exists()


def test_age_boundary_exactly_one_hour_is_kept(store: JobStore, tmp_path: Path) -> None:
    root = tmp_path / "archives"
    root.mkdir()
    p = root / "x.zip.part"
    p.write_text("x")
    os.utime(p, (1_800_000_000, 1_800_000_000))
    now = datetime.fromtimestamp(1_800_000_000 + 3600, UTC)
    assert cleanup.sweep(store, tmp_path, now).orphans_removed == 0
    now2 = datetime.fromtimestamp(1_800_000_000 + 3601, UTC)
    assert cleanup.sweep(store, tmp_path, now2).orphans_removed == 1


def test_sweep_does_not_rmdir_dir_of_job_that_became_running_after_snapshot(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "archives"
    (root / "j1").mkdir(parents=True)
    store.create("j1", None)
    store.mark_running("j1")
    monkeypatch.setattr(store, "active_job_ids", lambda: set())  # snapshot taken just before
    cleanup.sweep(store, tmp_path, NOW)
    assert (root / "j1").exists()


def test_runner_never_has_dir_before_row(store: JobStore, tmp_path: Path) -> None:
    """The row is created (awaited) before the task, and so before the export makes the dir."""
    seen: list[bool] = []

    async def exp(token: HarvestToken, out_dir: Path, *_: Any) -> ExportResult:
        seen.append(store.get(out_dir.name) is not None)
        raise ExitArchiveError("stop")

    async def go() -> None:
        r = JobRunner(store, tmp_path, clock=lambda: NOW, export=exp)
        await r.submit(HarvestToken(TOK), None, None)
        await r.wait_idle()

    asyncio.run(go())
    assert seen == [True]


def test_sqlite_and_dirs_do_not_contain_token_after_failure(
    store: JobStore, tmp_path: Path
) -> None:
    async def go() -> None:
        r = JobRunner(store, tmp_path, clock=lambda: NOW, export=_raising_export)
        await r.submit(HarvestToken(TOK), None, "cs_1")
        await r.wait_idle()

    asyncio.run(go())
    time.sleep(0)
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert TOK.encode() not in p.read_bytes(), p
