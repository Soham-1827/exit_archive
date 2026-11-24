"""Zip the work dir as exit-archive-<company-slug>-<YYYYMMDD>.zip, then remove it."""

import re
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from exit_archive.archive.manifest import iter_files
from exit_archive.errors import ExportError

_SLUG_MAX = 40
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def company_slug(company_name: str) -> str:
    """Lowercase ASCII slug of the company name, at most 40 chars; ``company`` if empty."""
    slug = _NON_SLUG.sub("-", company_name.lower()).strip("-")[:_SLUG_MAX].strip("-")
    return slug or "company"


def zip_archive(
    work_dir: Path, out_dir: Path, company_name: str, now: datetime | None = None
) -> Path:
    """Zip ``work_dir`` into ``out_dir`` under a top-level folder, then delete ``work_dir``."""
    if not work_dir.is_dir() or work_dir.is_symlink():
        raise ExportError("Archive work directory does not exist")
    root = work_dir.resolve()
    out = out_dir.resolve()
    if out == root or out.is_relative_to(root):
        raise ExportError("Zip output directory must be outside the work directory")
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d")
    top = f"exit-archive-{company_slug(company_name)}-{stamp}"
    files = list(iter_files(work_dir))
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"{top}.zip"
    partial = out / f"{top}.zip.part"
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            _add_dirs(zf, work_dir, top)
            for path in files:
                zf.write(path, f"{top}/{path.relative_to(work_dir).as_posix()}")
        partial.replace(zip_path)
    finally:
        partial.unlink(missing_ok=True)
    shutil.rmtree(work_dir)
    return zip_path


def _add_dirs(zf: zipfile.ZipFile, work_dir: Path, top: str) -> None:
    zf.writestr(f"{top}/", b"")
    for path in sorted(p for p in work_dir.rglob("*") if p.is_dir()):
        zf.writestr(f"{top}/{path.relative_to(work_dir).as_posix()}/", b"")
