"""In-process asyncio job runner. The customer token lives only inside one running job.

The token is handed to the task, never stored on the runner, and the task drops it (and its
per-job log redaction) in ``finally``. Only a safe error text is persisted: a typed
``ExitArchiveError`` message passed through ``redact()``, or the bare exception type name.
"""

import asyncio
import contextlib
import logging
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from exit_archive.errors import ExitArchiveError
from exit_archive.exporter import ExportResult, Progress, export_account
from exit_archive.jobs.store import Clock, JobStore, to_iso, utcnow
from exit_archive.secrets import HarvestToken, install_log_redaction, redact

ARCHIVE_TTL = timedelta(days=7)
ARCHIVES_DIR = "archives"
_log = logging.getLogger(__name__)
PROGRESS_MIN_INTERVAL = 0.5
MAX_ERROR_LEN = 500

ExportFn = Callable[[HarvestToken, Path, int | None, Progress | None], Awaitable[ExportResult]]


class _ProgressWriter:
    """Persist export progress, throttled; a failing write never fails the export."""

    def __init__(self, store: JobStore, job_id: str) -> None:
        self._store = store
        self._job_id = job_id
        self._last_stage: str | None = None
        self._last_time = 0.0

    def __call__(self, stage: str, done: int, total: int | None) -> None:
        now = time.monotonic()
        finished = total is not None and done >= total
        recent = now - self._last_time < PROGRESS_MIN_INTERVAL
        if stage == self._last_stage and not finished and recent:
            return
        self._last_stage, self._last_time = stage, now
        with contextlib.suppress(Exception):  # progress is best effort
            self._store.update_progress(self._job_id, stage, done, total)


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        data_dir: Path,
        max_concurrent: int = 2,
        clock: Clock = utcnow,
        export: ExportFn = export_account,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._store = store
        self._data_dir = Path(data_dir)
        self._clock = clock
        self._export = export
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: set[asyncio.Task[None]] = set()
        self._open_jobs: set[str] = set()

    def recover_on_startup(self) -> int:
        """Fail jobs left queued or running by a previous process; returns how many."""
        return self._store.fail_unfinished("server_restarted")

    async def submit(
        self, token: HarvestToken, account_id: int | None, session_id: str | None
    ) -> str:
        """Create the job row and start its task; raises ``SessionInUseError`` on reuse."""
        job_id = secrets.token_urlsafe(32)
        await asyncio.to_thread(self._store.create, job_id, session_id)
        self._open_jobs.add(job_id)
        task = asyncio.create_task(self._run(job_id, token, account_id))
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return job_id

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Retrieved so asyncio never logs it; only the type is ours to log.
            _log.warning("job task ended with %s", type(task.exception()).__name__)

    async def wait_idle(self) -> None:
        """Wait until every submitted job has finished."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel unfinished jobs and wait; any of this runner's jobs still open are failed."""
        for task in list(self._tasks):
            task.cancel()
        await self.wait_idle()
        for job_id in list(self._open_jobs):  # e.g. cancelled before the task ever ran
            self._fail(job_id, self._data_dir / ARCHIVES_DIR / job_id, "cancelled")

    async def _run(self, job_id: str, token: HarvestToken, account_id: int | None) -> None:
        uninstall = install_log_redaction(token)
        job_dir = self._data_dir / ARCHIVES_DIR / job_id
        error: str | None = None
        try:
            async with self._semaphore:
                self._store.mark_running(job_id)
                result = await self._export(
                    token, job_dir, account_id, _ProgressWriter(self._store, job_id)
                )
                self._finish(job_id, result)
        except asyncio.CancelledError:
            self._fail(job_id, job_dir, "cancelled")
            raise
        except ExitArchiveError as exc:
            error = redact(str(exc), token)[:MAX_ERROR_LEN]
        except Exception as exc:  # noqa: BLE001 - store only the type name, never repr/traceback
            error = type(exc).__name__
        finally:
            del token  # drop this job's last reference to the secret
            uninstall()
        if error is not None:  # outside the except block: nothing here chains the export error
            self._fail(job_id, job_dir, error)

    def _finish(self, job_id: str, result: ExportResult) -> None:
        relative = result.zip_path.resolve().relative_to(self._data_dir.resolve())
        finished = self._clock()
        self._store.mark_done(
            job_id, relative.as_posix(), to_iso(finished), to_iso(finished + ARCHIVE_TTL)
        )
        self._open_jobs.discard(job_id)

    def _fail(self, job_id: str, job_dir: Path, error: str) -> None:
        """Record a failure; a store or OS error here is logged by type and never raised."""
        shutil.rmtree(job_dir, ignore_errors=True)
        for _ in range(2):
            try:
                self._store.mark_failed(job_id, error)
            except Exception as exc:  # noqa: BLE001 - row stays for recover_on_startup()
                _log.warning("could not record job failure (%s)", type(exc).__name__)
                continue
            self._open_jobs.discard(job_id)
            return
