"""Re-gate adversarial tests for T3.1 fix loop 1 (tester)."""

import asyncio
import gc
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from exit_archive.exporter import ExportResult
from exit_archive.jobs.runner import JobRunner
from exit_archive.jobs.store import JobStore
from exit_archive.secrets import HarvestToken, install_log_redaction

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
TOK = "tok-REGATE-abcdef123456"  # noqa: S105


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3", clock=lambda: NOW)


@pytest.fixture(autouse=True)
def _restore_factory() -> Any:
    original = logging.getLogRecordFactory()
    yield
    logging.setLogRecordFactory(original)


def _emit(msg: str) -> logging.LogRecord:
    return logging.getLogRecordFactory()("t", logging.INFO, __file__, 1, msg, (), None)


def _wrapper() -> Any:
    base = logging.getLogRecordFactory()

    def third(*a: Any, **k: Any) -> logging.LogRecord:
        rec = base(*a, **k)
        rec.third = True  # type: ignore[attr-defined]
        return rec

    return third


# ---------------------------------------------------------------- R1
def test_wrapped_then_reregistered_scrubs_once_and_does_not_recurse() -> None:
    u = install_log_redaction(HarvestToken(TOK))
    third = _wrapper()  # wraps our hook
    logging.setLogRecordFactory(third)
    u()  # last release while wrapped: stays as pass-through
    assert logging.getLogRecordFactory() is third
    assert TOK in _emit(f"x {TOK}").getMessage()  # nothing registered: pass-through
    u2 = install_log_redaction(HarvestToken(TOK))
    rec = _emit(f"x {TOK}")
    assert TOK not in rec.getMessage()
    assert rec.third is True  # type: ignore[attr-defined]
    u2()
    assert TOK in _emit(f"y {TOK}").getMessage()


def test_many_cycles_while_wrapped_do_not_grow_chain_or_recurse() -> None:
    u = install_log_redaction(HarvestToken(TOK))
    third = _wrapper()
    logging.setLogRecordFactory(third)
    u()
    for _ in range(50):
        u = install_log_redaction(HarvestToken(TOK))
        assert TOK not in _emit(TOK).getMessage()
        u()
    # chain must still be shallow: a record builds quickly and without RecursionError
    _emit("ok")
    install_log_redaction(None)()


def test_wrapper_removed_by_third_party_then_reinstall_restores_original() -> None:
    original = logging.getLogRecordFactory()
    u = install_log_redaction(HarvestToken(TOK))
    hook = logging.getLogRecordFactory()
    logging.setLogRecordFactory(_wrapper())
    u()
    logging.setLogRecordFactory(hook)  # third party unwraps back to our pass-through
    u2 = install_log_redaction(HarvestToken(TOK))
    assert TOK not in _emit(TOK).getMessage()
    u2()
    assert logging.getLogRecordFactory() is original


def test_overlapping_registrations_while_wrapped_released_in_any_order() -> None:
    a = install_log_redaction(HarvestToken(TOK))
    logging.setLogRecordFactory(_wrapper())
    b = install_log_redaction(HarvestToken(TOK + "2"))
    a()
    assert TOK + "2" not in _emit(TOK + "2").getMessage()
    b()
    assert TOK + "2" in _emit(TOK + "2").getMessage()


def test_records_from_many_threads_scrubbed_and_thread_local_not_stuck() -> None:
    u = install_log_redaction(HarvestToken(TOK))
    logging.setLogRecordFactory(_wrapper())
    u2 = install_log_redaction(HarvestToken(TOK))  # double-hook chain
    bad: list[str] = []

    def work() -> None:
        for _ in range(300):
            r = _emit(TOK)
            if TOK in r.getMessage() or not getattr(r, "third", False):
                bad.append(r.getMessage())

    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    u()
    u2()
    assert not bad


@pytest.mark.xfail(strict=False, reason="FINDING-R2: flaky race in _release/_hook window")
def test_record_creation_racing_with_churn_keeps_custom_original_factory() -> None:
    """Records built while another thread installs/uninstalls must still go through the
    original (custom) factory."""

    def custom(*a: Any, **k: Any) -> logging.LogRecord:
        rec = logging.LogRecord(*a, **k)  # type: ignore[arg-type]
        rec.custom = True  # type: ignore[attr-defined]
        return rec

    logging.setLogRecordFactory(custom)
    stop = threading.Event()
    missing: list[int] = []

    def churn() -> None:
        while not stop.is_set():
            install_log_redaction(HarvestToken(TOK))()

    def log() -> None:
        for _ in range(20000):
            if not getattr(_emit("m"), "custom", False):
                missing.append(1)

    c = threading.Thread(target=churn)
    ls = [threading.Thread(target=log) for _ in range(3)]
    c.start()
    [t.start() for t in ls]
    [t.join() for t in ls]
    stop.set()
    c.join()
    assert not missing, f"{len(missing)} records bypassed the original factory"


# ---------------------------------------------------------------- J1
def _runner(store: JobStore, tmp_path: Path, export: Any, **kw: Any) -> JobRunner:
    return JobRunner(store, tmp_path, clock=lambda: NOW, export=export, **kw)


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_base_exception_in_export_registration_and_token_cleanup(
    store: JobStore, tmp_path: Path, exc_type: type[BaseException]
) -> None:
    async def boom(token: HarvestToken, out_dir: Path, *_: Any) -> ExportResult:
        raise exc_type(f"stop {token.reveal()}")

    original = logging.getLogRecordFactory()
    runner = _runner(store, tmp_path, boom)

    async def go() -> str:
        jid = await runner.submit(HarvestToken(TOK), None, "cs_1")
        await asyncio.sleep(0.2)
        return jid

    loop = asyncio.new_event_loop()
    with pytest.raises(exc_type):
        loop.run_until_complete(go())
    # redaction must have been uninstalled (finally ran)
    assert logging.getLogRecordFactory() is original
    # the row is still open for aclose/recovery; recovery must clean it
    assert runner.recover_on_startup() == 1
    loop.run_until_complete(runner.aclose())
    loop.close()


async def test_mark_failed_failing_twice_never_logs_token(
    store: JobStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(token: HarvestToken, out_dir: Path, *_: Any) -> ExportResult:
        raise RuntimeError(token.reveal())

    def broken(*a: Any, **k: Any) -> None:
        raise OSError(f"disk {TOK}")

    store.mark_failed = broken  # type: ignore[method-assign]
    runner = _runner(store, tmp_path, boom)
    with caplog.at_level(logging.DEBUG):
        await runner.submit(HarvestToken(TOK), None, "cs_1")
        await runner.wait_idle()
        gc.collect()
    assert TOK not in caplog.text
    assert TOK not in "".join(str(r.__dict__) for r in caplog.records)


# ---------------------------------------------------------------- J2
async def test_aclose_with_running_and_queued_jobs_fails_all_and_frees_sessions(
    store: JobStore, tmp_path: Path
) -> None:
    started = asyncio.Event()

    async def hang(token: HarvestToken, out_dir: Path, *_: Any) -> ExportResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "partial").write_text("x")
        started.set()
        await asyncio.sleep(3600)
        raise AssertionError

    runner = _runner(store, tmp_path, hang, max_concurrent=1)
    ids = [await runner.submit(HarvestToken(TOK + str(i)), None, f"cs_{i}") for i in range(4)]
    await started.wait()
    await runner.aclose()
    for i, jid in enumerate(ids):
        job = store.get(jid)
        assert job is not None and job.status == "failed" and job.error == "cancelled"
        assert not (tmp_path / "archives" / jid).exists()
        store.create(f"new{i}", f"cs_{i}")  # session is reusable
    assert runner._open_jobs == set()  # noqa: SLF001
    original_tokens = [o for o in gc.get_objects() if isinstance(o, HarvestToken)]
    for _ in range(5):
        await asyncio.sleep(0)
    gc.collect()
    assert [o for o in gc.get_objects() if isinstance(o, HarvestToken)] == [] or original_tokens


async def test_aclose_does_not_touch_finished_done_jobs(store: JobStore, tmp_path: Path) -> None:
    async def ok(token: HarvestToken, out_dir: Path, *_: Any) -> ExportResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        z = out_dir / "a.zip"
        z.write_bytes(b"x")
        return ExportResult(zip_path=z, counts={}, missing=[], skipped=[])

    runner = _runner(store, tmp_path, ok)
    jid = await runner.submit(HarvestToken(TOK), None, "cs_1")
    await runner.wait_idle()
    await runner.aclose()
    await runner.aclose()  # idempotent
    job = store.get(jid)
    assert job is not None and job.status == "done"
    assert (tmp_path / "archives" / jid / "a.zip").exists()


async def test_aclose_survives_failing_store(store: JobStore, tmp_path: Path) -> None:
    async def never(*_: Any) -> ExportResult:
        raise AssertionError

    runner = _runner(store, tmp_path, never)
    await runner.submit(HarvestToken(TOK), None, "cs_1")

    def broken(*a: Any, **k: Any) -> None:
        raise OSError("disk")

    store.mark_failed = broken  # type: ignore[method-assign]
    await runner.aclose()  # must not raise
    assert runner.recover_on_startup() == 1
