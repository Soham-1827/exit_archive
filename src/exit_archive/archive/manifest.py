"""MANIFEST.json: counts, per-file sha256 and size, missing and skipped items."""

import hashlib
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from exit_archive import __version__
from exit_archive.errors import ExportError

MANIFEST_NAME = "MANIFEST.json"
_MISSING_KEYS = ("type", "id", "reason")
_CHUNK = 1024 * 1024


def iter_files(root: Path) -> Iterator[Path]:
    """Yield regular files under ``root`` sorted by path; symlinks are refused."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                raise ExportError("Archive work directory contains a non-regular file")
            yield path
        for d in dirnames:
            if (Path(dirpath) / d).is_symlink():
                raise ExportError("Archive work directory contains a symlink")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    work_dir: Path,
    *,
    counts: Mapping[str, int],
    missing: Sequence[Mapping[str, Any]],
    skipped: Sequence[str],
    account_id: int | str,
    company_name: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Hash every file in ``work_dir`` (except MANIFEST.json) and assemble the manifest."""
    files = [
        {
            "path": path.relative_to(work_dir).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in iter_files(work_dir)
        if path.relative_to(work_dir).as_posix() != MANIFEST_NAME
    ]
    when = generated_at or datetime.now(UTC)
    return {
        "generated_at": when.isoformat(),
        "tool_version": __version__,
        "account_id": account_id,
        "company_name": company_name,
        "counts": dict(counts),
        "files": files,
        "missing": [{k: m.get(k) for k in _MISSING_KEYS} for m in missing],
        "skipped": list(skipped),
    }


def write_manifest(work_dir: Path, **kwargs: Any) -> Path:
    """Build the manifest and write it as ``MANIFEST.json`` (last, so it never lists itself)."""
    manifest = build_manifest(work_dir, **kwargs)
    path = work_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
