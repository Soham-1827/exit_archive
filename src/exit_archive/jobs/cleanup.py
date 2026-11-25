"""Expiry sweep for ``$DATA_DIR/archives/``.

Everything deleted lives under ``archives/``. Symlinks are never followed: a link found there
is unlinked itself (its target is untouched) and a symlinked ``archives`` root is skipped.
"""

import asyncio
import contextlib
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from exit_archive.archive.writer import resolve_inside
from exit_archive.errors import ExportError
from exit_archive.jobs.store import Clock, JobStore, utcnow

ARCHIVES_DIR = "archives"
ORPHAN_AGE_SECONDS = 3600
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepResult:
    expired: int
    orphans_removed: int


def _remove(path: Path) -> None:
    """Delete a link, file or directory tree without following links."""
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


def _is_old(path: Path, now: datetime) -> bool:
    try:
        return now.timestamp() - path.lstat().st_mtime > ORPHAN_AGE_SECONDS
    except OSError:
        return False


def _expire(store: JobStore, root: Path, now: datetime) -> int:
    count = 0
    for job in store.list_expired(now):
        if _JOB_ID.fullmatch(job.id):
            entry = root / job.id
            if entry.is_symlink():
                entry.unlink(missing_ok=True)  # never follow a link; its target is not ours
            else:
                try:
                    _remove(resolve_inside(root, job.id))
                except ExportError:
                    continue
        store.mark_expired(job.id)
        count += 1
    return count


def _clean_job_dir(store: JobStore, job_dir: Path, now: datetime) -> int:
    removed = 0
    for child in list(job_dir.iterdir()):
        is_dir = child.is_dir() and not child.is_symlink()
        orphan = child.is_symlink() or is_dir or child.name.endswith(".zip.part")
        if orphan and _is_old(child, now) and not _is_active(store, job_dir.name):
            _remove(child)
            removed += 1
    job = store.get(job_dir.name)  # re-read right before acting
    if job is not None and job.status in ("failed", "expired"):
        with contextlib.suppress(OSError):  # only succeeds when empty
            job_dir.rmdir()
    return removed


def _is_active(store: JobStore, job_id: str) -> bool:
    job = store.get(job_id)
    return job is not None and job.status in ("queued", "running")


def _clean_orphans(store: JobStore, root: Path, now: datetime) -> int:
    removed = 0
    for entry in list(root.iterdir()):
        if entry.is_symlink():
            _remove(entry)
            removed += 1
        elif entry.is_file():
            if entry.name.endswith(".zip.part") and _is_old(entry, now):
                _remove(entry)
                removed += 1
        elif entry.is_dir():
            job = store.get(entry.name)  # status read per entry, not from one snapshot
            if job is None:
                _remove(entry)
                removed += 1
            elif job.status not in ("queued", "running"):
                removed += _clean_job_dir(store, entry, now)
    return removed


def sweep(store: JobStore, data_dir: Path, now: datetime) -> SweepResult:
    """Expire old archives, then remove orphans under ``archives/``."""
    root = Path(data_dir) / ARCHIVES_DIR
    if root.is_symlink() or not root.is_dir():
        return SweepResult(0, 0)
    expired = _expire(store, root, now)
    return SweepResult(expired, _clean_orphans(store, root, now))


async def run_cleanup_loop(
    store: JobStore, data_dir: Path, interval: float = 3600, clock: Clock = utcnow
) -> None:
    """Sweep every ``interval`` seconds until cancelled; a failed sweep is logged and retried."""
    while True:
        try:
            await asyncio.to_thread(sweep, store, data_dir, clock())
        except Exception as exc:  # noqa: BLE001 - keep sweeping; log only the type
            _log.warning("cleanup sweep failed (%s)", type(exc).__name__)
        await asyncio.sleep(interval)


__all__ = ["SweepResult", "run_cleanup_loop", "sweep"]
