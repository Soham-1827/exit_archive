"""sweep() and run_cleanup_loop (T3.1.4)."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from exit_archive.jobs.cleanup import run_cleanup_loop, sweep
from exit_archive.jobs.store import JobStore

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
HOUR = 3600


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3", clock=lambda: NOW)


@pytest.fixture
def archives(tmp_path: Path) -> Path:
    root = tmp_path / "archives"
    root.mkdir()
    return root


def _age(path: Path, seconds: float) -> None:
    t = NOW.timestamp() - seconds
    os.utime(path, (t, t), follow_symlinks=False)


def _done_job(store: JobStore, archives: Path, job_id: str, expires: datetime) -> Path:
    store.create(job_id, None)
    job_dir = archives / job_id
    job_dir.mkdir()
    zip_path = job_dir / "exit-archive-x.zip"
    zip_path.write_bytes(b"zip")
    store.mark_done(
        job_id,
        f"archives/{job_id}/{zip_path.name}",
        _iso(NOW - timedelta(days=8)),
        _iso(expires),
    )
    return zip_path


def test_expired_archive_deleted_and_row_marked_expired(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    zip_path = _done_job(store, archives, "old", NOW - timedelta(seconds=1))
    sweep(store, tmp_path, NOW)
    assert not zip_path.exists()
    assert not zip_path.parent.exists()
    job = store.get("old")
    assert job is not None and job.status == "expired"


def test_future_archive_is_kept(store: JobStore, archives: Path, tmp_path: Path) -> None:
    zip_path = _done_job(store, archives, "fresh", NOW + timedelta(days=1))
    sweep(store, tmp_path, NOW)
    assert zip_path.exists()
    job = store.get("fresh")
    assert job is not None and job.status == "done"


def test_sweep_returns_counts(store: JobStore, archives: Path, tmp_path: Path) -> None:
    _done_job(store, archives, "old", NOW - timedelta(days=1))
    (archives / "ghost").mkdir()
    result = sweep(store, tmp_path, NOW)
    assert result.expired == 1
    assert result.orphans_removed == 1


def test_sweep_without_archives_dir_is_a_noop(store: JobStore, tmp_path: Path) -> None:
    result = sweep(store, tmp_path, NOW)
    assert (result.expired, result.orphans_removed) == (0, 0)


def test_old_orphan_work_dir_removed_young_kept(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    _done_job(store, archives, "j", NOW + timedelta(days=1))
    old = archives / "j" / "tmpold123"
    young = archives / "j" / "tmpyoung45"
    for d in (old, young):
        (d / "data").mkdir(parents=True)
        (d / "data" / "f.json").write_text("{}")
    _age(old, 2 * HOUR)
    _age(young, 10 * 60)
    sweep(store, tmp_path, NOW)
    assert not old.exists()
    assert young.exists()
    assert (archives / "j" / "exit-archive-x.zip").exists()


def test_old_zip_part_removed_young_kept(store: JobStore, archives: Path, tmp_path: Path) -> None:
    _done_job(store, archives, "j", NOW + timedelta(days=1))
    old = archives / "j" / "a.zip.part"
    young = archives / "j" / "b.zip.part"
    old.write_bytes(b"x")
    young.write_bytes(b"x")
    _age(old, 2 * HOUR)
    _age(young, 60)
    sweep(store, tmp_path, NOW)
    assert not old.exists() and young.exists()


def test_running_and_queued_jobs_work_dirs_are_kept_even_when_old(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    for name, run in (("run", True), ("que", False)):
        store.create(name, None)
        if run:
            store.mark_running(name)
        (archives / name).mkdir()
        work = archives / name / "tmpwork"
        work.mkdir()
        part = archives / name / "x.zip.part"
        part.write_bytes(b"1")
        _age(work, 5 * HOUR)
        _age(part, 5 * HOUR)
    sweep(store, tmp_path, NOW)
    for name in ("run", "que"):
        assert (archives / name / "tmpwork").exists()
        assert (archives / name / "x.zip.part").exists()


def test_failed_job_orphans_removed_and_empty_dir_dropped(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    store.create("bad", None)
    store.mark_failed("bad", "boom")
    work = archives / "bad" / "tmpwork"
    work.mkdir(parents=True)
    _age(work, 3 * HOUR)
    sweep(store, tmp_path, NOW)
    assert not work.exists()
    assert not (archives / "bad").exists()


def test_dir_without_row_is_removed(store: JobStore, archives: Path, tmp_path: Path) -> None:
    ghost = archives / "ghost"
    (ghost / "tmpx").mkdir(parents=True)
    (ghost / "a.zip").write_bytes(b"z")
    sweep(store, tmp_path, NOW)
    assert not ghost.exists()


def test_old_zip_part_directly_under_archives_removed(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    part = archives / "stray.zip.part"
    part.write_bytes(b"x")
    keep = archives / "stray2.zip.part"
    keep.write_bytes(b"x")
    _age(part, 2 * HOUR)
    _age(keep, 30)
    sweep(store, tmp_path, NOW)
    assert not part.exists() and keep.exists()


def test_symlink_under_archives_is_unlinked_not_followed(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.txt"
    precious.write_text("keep me")
    (archives / "evil").symlink_to(outside, target_is_directory=True)
    sweep(store, tmp_path, NOW)
    assert not (archives / "evil").is_symlink()
    assert precious.read_text() == "keep me"


def test_symlink_inside_job_dir_is_not_followed(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    _done_job(store, archives, "j", NOW + timedelta(days=1))
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.txt"
    precious.write_text("keep me")
    link = archives / "j" / "tmplink"
    link.symlink_to(outside, target_is_directory=True)
    _age(link, 3 * HOUR)
    sweep(store, tmp_path, NOW)
    assert precious.read_text() == "keep me"


def test_file_symlink_to_outside_is_not_followed(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("s")
    link = archives / "x.zip.part"
    link.symlink_to(target)
    _age(link, 3 * HOUR)
    sweep(store, tmp_path, NOW)
    assert target.read_text() == "s"


def test_symlinked_archives_root_is_left_alone(store: JobStore, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    (outside / "ghost").mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "archives").symlink_to(outside, target_is_directory=True)
    store2 = JobStore(data / "jobs.sqlite3", clock=lambda: NOW)
    sweep(store2, data, NOW)
    assert (outside / "ghost").exists()


def test_expired_row_with_hostile_id_never_deletes_outside(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "f").write_text("x")
    store.create("../victim", None)
    store.mark_done("../victim", "archives/../victim/f", _iso(NOW), _iso(NOW - timedelta(days=1)))
    sweep(store, tmp_path, NOW)
    assert (victim / "f").exists()


def test_loose_file_under_archives_is_left_alone(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    note = archives / "notes.txt"
    note.write_text("n")
    _age(note, 10 * HOUR)
    sweep(store, tmp_path, NOW)
    assert note.exists()


async def test_cleanup_loop_sweeps_repeatedly_and_survives_errors(
    store: JobStore, archives: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    done = asyncio.Event()
    real = sweep

    def flaky(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("disk on fire")
        if calls >= 3:
            done.set()
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("exit_archive.jobs.cleanup.sweep", flaky)
    task = asyncio.create_task(run_cleanup_loop(store, tmp_path, interval=0, clock=lambda: NOW))
    await asyncio.wait_for(done.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 3


def test_expiry_unlinks_symlinked_job_dir_without_touching_sibling(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    victim_zip = _done_job(store, archives, "victim", NOW + timedelta(days=1))
    (archives / "evil").symlink_to(archives / "victim", target_is_directory=True)
    store.create("evil", None)
    store.mark_done("evil", "archives/evil/x.zip", _iso(NOW), _iso(NOW - timedelta(days=1)))
    result = sweep(store, tmp_path, NOW)
    assert result.expired == 1
    assert not (archives / "evil").is_symlink()
    assert victim_zip.exists()
    evil = store.get("evil")
    assert evil is not None and evil.status == "expired"


def test_status_is_reread_per_entry_not_from_a_snapshot(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    store.create("j1", None)
    store.mark_failed("j1", "x")
    (archives / "j1").mkdir()
    real_get = store.get

    def get_and_start(job_id: str) -> Any:
        job = real_get(job_id)
        if job_id == "j1":  # the job is resubmitted right after we looked at it
            store.mark_running("j1")
        return job

    store.get = get_and_start  # type: ignore[method-assign]
    sweep(store, tmp_path, NOW)
    assert (archives / "j1").exists()


def test_empty_dir_of_queued_job_is_kept_but_failed_is_dropped(
    store: JobStore, archives: Path, tmp_path: Path
) -> None:
    store.create("q", None)
    store.create("f", None)
    store.mark_failed("f", "x")
    (archives / "q").mkdir()
    (archives / "f").mkdir()
    sweep(store, tmp_path, NOW)
    assert (archives / "q").exists() and not (archives / "f").exists()
