"""Lifespan: settings from env, recovery, log redaction, shutdown."""

import asyncio
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exit_archive.jobs.store import JobStore
from exit_archive.web.app import create_app
from exit_archive.web.settings import SettingsError
from tests.web.helpers import RecordingExport, make_job_id, post_job, settings_for


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DATA_DIR",
        "MAX_CONCURRENT_JOBS",
        "PAYMENT_REQUIRED",
        "STRIPE_PAYMENT_LINK_URL",
        "PUBLIC_BASE_URL",
        "STRIPE_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ({}, "DATA_DIR"),
        ({"MAX_CONCURRENT_JOBS": "0"}, "MAX_CONCURRENT_JOBS"),
        ({"MAX_CONCURRENT_JOBS": "abc"}, "MAX_CONCURRENT_JOBS"),
        ({"PAYMENT_REQUIRED": "yes"}, "PAYMENT_REQUIRED"),
        ({"STRIPE_PAYMENT_LINK_URL": "http://buy.example/x"}, "STRIPE_PAYMENT_LINK_URL"),
    ],
)
def test_startup_fails_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str], name: str
) -> None:
    if name != "DATA_DIR":
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SettingsError, match=name), TestClient(create_app()):
        pass


def test_settings_load_from_environment_in_lifespan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("PAYMENT_REQUIRED", "false")
    with TestClient(create_app(export=RecordingExport())) as client:
        assert client.get("/healthz").status_code == 200
        assert (tmp_path / "d" / "archives").is_dir()
        assert client.app.state.settings.payment_required is False  # type: ignore[attr-defined]


def test_running_row_becomes_failed_server_restarted(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    job_id = make_job_id()
    store.create(job_id, None)
    store.mark_running(job_id)
    with TestClient(create_app(settings_for(tmp_path), export=RecordingExport())):
        job = store.get(job_id)
        assert job is not None
        assert (job.status, job.error) == ("failed", "server_restarted")


def test_log_redaction_active_while_running_and_restored_after(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    original = logging.getLogRecordFactory()
    with TestClient(create_app(settings_for(tmp_path), export=RecordingExport())):
        assert logging.getLogRecordFactory() is not original
        with caplog.at_level(logging.INFO):
            logging.getLogger("some.lib").warning("Authorization: Bearer abc123SECRET")
        assert "abc123SECRET" not in caplog.text
        assert "***" in caplog.text
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogRecordFactory() is original


def test_shutdown_cancels_running_jobs(tmp_path: Path) -> None:

    async def slow(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(60)

    app = create_app(settings_for(tmp_path), export=slow)  # type: ignore[arg-type]
    with TestClient(app) as client:
        job_id = post_job(client).headers["location"].rsplit("/", 1)[1]
    job = JobStore(tmp_path / "jobs.sqlite3").get(job_id)
    assert job is not None
    assert (job.status, job.error) == ("failed", "cancelled")


def test_startup_sweeps_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exit_archive.jobs import cleanup
    from exit_archive.web import app as web_app

    calls: list[object] = []
    real = cleanup.sweep

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(web_app, "sweep", counting)
    monkeypatch.setattr(cleanup, "sweep", counting)
    with TestClient(create_app(settings_for(tmp_path), export=RecordingExport())) as client:
        assert client.get("/healthz").status_code == 200
    assert len(calls) == 1


async def test_cleanup_loop_initial_delay_defers_first_sweep(tmp_path: Path) -> None:
    import asyncio

    from exit_archive.jobs.cleanup import run_cleanup_loop

    store = JobStore(tmp_path / "jobs.sqlite3")
    task = asyncio.create_task(run_cleanup_loop(store, tmp_path, interval=3600, initial_delay=3600))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
