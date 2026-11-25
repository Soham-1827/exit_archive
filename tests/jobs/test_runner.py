"""JobRunner: lifecycle, concurrency limit, token hygiene (T3.1.3)."""

import asyncio
import gc
import logging
import types
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from exit_archive.errors import AuthError, ExitArchiveError
from exit_archive.exporter import ExportResult, Progress
from exit_archive.jobs.runner import ARCHIVE_TTL, JobRunner
from exit_archive.jobs.store import JobStore, SessionInUseError
from exit_archive.secrets import HarvestToken

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
TOKEN_TEXT = "tok-SECRET-9f8e7d6c5b4a"  # noqa: S105


@pytest.fixture
def token() -> HarvestToken:
    return HarvestToken(TOKEN_TEXT)


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3", clock=lambda: NOW)


def _make_runner(store: JobStore, tmp_path: Path, export: Any, **kw: Any) -> JobRunner:
    return JobRunner(store, tmp_path, clock=lambda: NOW, export=export, **kw)


async def _ok_export(
    token: HarvestToken,
    out_dir: Path,
    account_id: int | None = None,
    progress: Progress | None = None,
) -> ExportResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "tmpwork"
    work.mkdir()
    zip_path = out_dir / "exit-archive-acme-20261125.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("MANIFEST.json", "{}")
    work.rmdir()
    if progress:
        progress("records", 500, None)
        progress("files", 3, 3)
    return ExportResult(zip_path=zip_path, counts={}, missing=[], skipped=[])


async def test_successful_job_records_done_with_relative_path_and_expiry(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    runner = _make_runner(store, tmp_path, _ok_export)
    job_id = await runner.submit(token, None, "cs_1")
    await runner.wait_idle()
    job = store.get(job_id)
    assert job is not None
    assert job.status == "done"
    assert job.archive_path == f"archives/{job_id}/exit-archive-acme-20261125.zip"
    assert (tmp_path / job.archive_path).is_file()
    assert job.finished_at == "2026-11-25T12:00:00Z"
    expected = (NOW + ARCHIVE_TTL).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert job.expires_at == expected
    assert timedelta(days=7) == ARCHIVE_TTL
    assert job.stage == "files" and job.done == 3 and job.total == 3


async def test_job_id_is_a_urlsafe_capability(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    runner = _make_runner(store, tmp_path, _ok_export)
    ids = {await runner.submit(token, None, None) for _ in range(3)}
    await runner.wait_idle()
    assert len(ids) == 3
    assert all(len(i) >= 43 for i in ids)


async def test_account_id_is_passed_to_export(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    seen: list[int | None] = []

    async def export(
        tok: HarvestToken, out_dir: Path, account_id: int | None = None, progress: Any = None
    ) -> ExportResult:
        seen.append(account_id)
        return await _ok_export(tok, out_dir, account_id, progress)

    runner = _make_runner(store, tmp_path, export)
    await runner.submit(token, 42, None)
    await runner.wait_idle()
    assert seen == [42]


async def test_concurrency_limit_of_two(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    running = 0
    peak = 0
    started: list[int] = []
    two_running = asyncio.Event()
    gate = asyncio.Event()

    async def export(
        tok: HarvestToken, out_dir: Path, account_id: int | None = None, progress: Any = None
    ) -> ExportResult:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        started.append(1)
        if running == 2:
            two_running.set()
        await gate.wait()
        running -= 1
        return await _ok_export(tok, out_dir, account_id, progress)

    runner = _make_runner(store, tmp_path, export, max_concurrent=2)
    ids = [await runner.submit(token, None, None) for _ in range(3)]
    await asyncio.wait_for(two_running.wait(), timeout=5)
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(started) == 2 and peak == 2
    third = store.get(ids[2])
    assert third is not None and third.status == "queued"
    gate.set()
    await runner.wait_idle()
    assert peak == 2 and len(started) == 3
    assert all((j := store.get(i)) is not None and j.status == "done" for i in ids)


@pytest.mark.parametrize("kind", ["archive", "plain"])
async def test_failure_error_never_contains_token(
    store: JobStore, tmp_path: Path, token: HarvestToken, kind: str
) -> None:
    async def export(
        tok: HarvestToken, out_dir: Path, account_id: int | None = None, progress: Any = None
    ) -> ExportResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "partial").write_text("x")
        if kind == "archive":
            raise ExitArchiveError(f"bad Bearer {TOKEN_TEXT} and {TOKEN_TEXT}")
        raise RuntimeError(f"boom {TOKEN_TEXT}")

    runner = _make_runner(store, tmp_path, export)
    job_id = await runner.submit(token, None, None)
    await runner.wait_idle()
    job = store.get(job_id)
    assert job is not None and job.status == "failed"
    assert job.error is not None
    assert TOKEN_TEXT not in job.error
    if kind == "plain":
        assert job.error == "RuntimeError"
    assert not (tmp_path / "archives" / job_id).exists()


async def test_typed_harvest_error_keeps_its_safe_message(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    async def export(*_a: Any, **_k: Any) -> ExportResult:
        raise AuthError()

    runner = _make_runner(store, tmp_path, export)
    job_id = await runner.submit(token, None, None)
    await runner.wait_idle()
    job = store.get(job_id)
    assert job is not None and job.error == "Harvest returned 401: the token was rejected"


async def test_no_token_bytes_anywhere_on_disk_after_job(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    async def export(
        tok: HarvestToken, out_dir: Path, account_id: int | None = None, progress: Any = None
    ) -> ExportResult:
        logging.getLogger("leaky").warning("token is %s", TOKEN_TEXT)
        return await _ok_export(tok, out_dir, account_id, progress)

    async def failing(*_a: Any, **_k: Any) -> ExportResult:
        raise RuntimeError(TOKEN_TEXT)

    runner = _make_runner(store, tmp_path, export)
    await runner.submit(token, None, "cs_ok")
    failing_runner = _make_runner(store, tmp_path, failing)
    await failing_runner.submit(token, None, "cs_bad")
    await runner.wait_idle()
    await failing_runner.wait_idle()
    needle = TOKEN_TEXT.encode()
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert any(p.name == "jobs.sqlite3" for p in files)
    for path in files:
        assert needle not in path.read_bytes(), path


async def test_token_reference_dropped_after_job(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    runner = _make_runner(store, tmp_path, _ok_export)
    await runner.submit(token, None, None)
    await runner.wait_idle()
    gc.collect()
    holders = [
        r
        for r in gc.get_referrers(token)
        if isinstance(r, asyncio.Task | types.CoroutineType | types.FrameType | JobRunner)
        or r is runner.__dict__
        or (isinstance(r, dict) and any(v is runner for v in r.values()))
    ]
    # frames of this test legitimately hold the fixture; anything else must not
    holders = [h for h in holders if not isinstance(h, types.FrameType)]
    assert holders == []
    for value in vars(runner).values():
        assert value is not token
        if isinstance(value, dict | set | list):
            assert token not in value


async def test_per_job_redaction_installed_and_removed(
    store: JobStore, tmp_path: Path, token: HarvestToken, caplog: pytest.LogCaptureFixture
) -> None:
    before = logging.getLogRecordFactory()
    inside: list[bool] = []

    async def export(
        tok: HarvestToken, out_dir: Path, account_id: int | None = None, progress: Any = None
    ) -> ExportResult:
        inside.append(logging.getLogRecordFactory() is not before)
        with caplog.at_level(logging.INFO):
            logging.getLogger("t").info("leak %s", TOKEN_TEXT)
        return await _ok_export(tok, out_dir, account_id, progress)

    runner = _make_runner(store, tmp_path, export)
    await runner.submit(token, None, None)
    await runner.wait_idle()
    assert inside == [True]
    assert TOKEN_TEXT not in "\n".join(r.getMessage() for r in caplog.records)
    assert logging.getLogRecordFactory() is before


async def test_overlapping_jobs_second_token_still_scrubbed_after_first_finishes(
    store: JobStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tok_a, tok_b = HarvestToken("tok-AAAA-aaaa-1"), HarvestToken("tok-BBBB-bbbb-2")
    a_done = asyncio.Event()
    b_may_log = asyncio.Event()

    async def export(
        tok: HarvestToken, out_dir: Path, account_id: int | None = None, progress: Any = None
    ) -> ExportResult:
        if tok == tok_b:
            await b_may_log.wait()
            with caplog.at_level(logging.INFO):
                logging.getLogger("t").info("b leaks tok-BBBB-bbbb-2")
        return await _ok_export(tok, out_dir, account_id, progress)

    runner = _make_runner(store, tmp_path, export)
    await runner.submit(tok_b, None, None)
    await runner.submit(tok_a, None, None)
    for _ in range(20):
        await asyncio.sleep(0)
    a_done.set()
    b_may_log.set()
    await runner.wait_idle()
    assert "tok-BBBB-bbbb-2" not in "\n".join(r.getMessage() for r in caplog.records)


async def test_session_conflict_raises_before_any_task_starts(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    calls = 0

    async def export(*a: Any, **k: Any) -> ExportResult:
        nonlocal calls
        calls += 1
        return await _ok_export(*a, **k)

    runner = _make_runner(store, tmp_path, export)
    await runner.submit(token, None, "cs_once")
    with pytest.raises(SessionInUseError):
        await runner.submit(token, None, "cs_once")
    await runner.wait_idle()
    assert calls == 1


async def test_retry_allowed_after_failure(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    attempts = 0

    async def export(*a: Any, **k: Any) -> ExportResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("x")
        return await _ok_export(*a, **k)

    runner = _make_runner(store, tmp_path, export)
    first = await runner.submit(token, None, "cs_r")
    await runner.wait_idle()
    second = await runner.submit(token, None, "cs_r")
    await runner.wait_idle()
    f, s = store.get(first), store.get(second)
    assert f is not None and f.status == "failed"
    assert s is not None and s.status == "done"


def test_recover_on_startup_fails_queued_and_running(store: JobStore, tmp_path: Path) -> None:
    store.create("q", None)
    store.create("r", None)
    store.mark_running("r")
    runner = _make_runner(store, tmp_path, _ok_export)
    assert runner.recover_on_startup() == 2
    for job_id in ("q", "r"):
        job = store.get(job_id)
        assert job is not None
        assert (job.status, job.error) == ("failed", "server_restarted")


async def test_aclose_cancels_running_jobs_and_marks_them_failed(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    entered = asyncio.Event()

    async def export(*_a: Any, **_k: Any) -> ExportResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    runner = _make_runner(store, tmp_path, export)
    job_id = await runner.submit(token, None, None)
    await asyncio.wait_for(entered.wait(), timeout=5)
    await runner.aclose()
    job = store.get(job_id)
    assert job is not None and job.status == "failed" and job.error == "cancelled"
    assert not (tmp_path / "archives" / job_id).exists()


async def test_progress_write_failure_does_not_fail_the_job(
    store: JobStore, tmp_path: Path, token: HarvestToken, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db locked")

    monkeypatch.setattr(store, "update_progress", boom)
    runner = _make_runner(store, tmp_path, _ok_export)
    job_id = await runner.submit(token, None, None)
    await runner.wait_idle()
    job = store.get(job_id)
    assert job is not None and job.status == "done"


async def test_failing_mark_failed_never_escapes_or_logs_token(
    store: JobStore,
    tmp_path: Path,
    token: HarvestToken,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def export(*_a: Any, **_k: Any) -> ExportResult:
        raise RuntimeError(f"boom {TOKEN_TEXT}")

    calls = 0

    def broken(*_a: Any, **_k: Any) -> None:
        nonlocal calls
        calls += 1
        raise OSError(f"disk full {TOKEN_TEXT}")

    store.mark_failed = broken  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    handled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _l, ctx: handled.append(ctx))
    runner = _make_runner(store, tmp_path, export)
    with caplog.at_level(logging.DEBUG):
        job_id = await runner.submit(token, None, "cs_f")
        await runner.wait_idle()
        gc.collect()
        await asyncio.sleep(0)
    assert handled == []  # nothing for asyncio's "never retrieved" handler
    assert calls == 2  # one retry, no loop
    out = capsys.readouterr()
    text = "\n".join(r.getMessage() for r in caplog.records) + out.out + out.err
    assert TOKEN_TEXT not in text and "never retrieved" not in text
    assert "OSError" in text
    job = store.get(job_id)
    assert job is not None and job.status == "running"  # left for recover_on_startup()


async def test_mark_failed_recovers_on_retry(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    async def export(*_a: Any, **_k: Any) -> ExportResult:
        raise RuntimeError("x")

    real = store.mark_failed
    attempts = 0

    def flaky(job_id: str, error: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("locked")
        real(job_id, error)

    store.mark_failed = flaky  # type: ignore[method-assign]
    runner = _make_runner(store, tmp_path, export)
    job_id = await runner.submit(token, None, None)
    await runner.wait_idle()
    job = store.get(job_id)
    assert job is not None and job.status == "failed" and job.error == "RuntimeError"


async def test_cancel_failure_path_error_still_reraises_cancelled_only(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    entered = asyncio.Event()

    async def export(*_a: Any, **_k: Any) -> ExportResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError

    def broken(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    store.mark_failed = broken  # type: ignore[method-assign]
    runner = _make_runner(store, tmp_path, export)
    await runner.submit(token, None, None)
    await asyncio.wait_for(entered.wait(), timeout=5)
    await runner.aclose()  # must not raise


async def test_aclose_before_task_starts_fails_row_frees_session_and_drops_token(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    async def never(*_a: Any, **_k: Any) -> ExportResult:
        raise AssertionError

    runner = _make_runner(store, tmp_path, never)
    job_id = await runner.submit(token, None, "cs_early")
    await runner.aclose()
    job = store.get(job_id)
    assert job is not None and (job.status, job.error) == ("failed", "cancelled")
    store.create("retry", "cs_early")  # session accepted again
    gc.collect()
    holders = [
        r
        for r in gc.get_referrers(token)
        if isinstance(r, asyncio.Task | types.CoroutineType | JobRunner)
    ]
    assert holders == []
    for value in vars(runner).values():
        if isinstance(value, dict | set | list):
            assert token not in value
    assert vars(runner)["_open_jobs"] == set()


async def test_aclose_leaves_finished_jobs_alone(
    store: JobStore, tmp_path: Path, token: HarvestToken
) -> None:
    runner = _make_runner(store, tmp_path, _ok_export)
    job_id = await runner.submit(token, None, None)
    await runner.wait_idle()
    await runner.aclose()
    job = store.get(job_id)
    assert job is not None and job.status == "done"
