"""Offline viewer: ``data/viewer-data.js`` (see ``bundle``) plus the static ``index.html``."""

from importlib import resources
from pathlib import Path

from exit_archive.archive.writer import resolve_inside
from exit_archive.viewer.bundle import build_viewer_data

INDEX_NAME = "index.html"

__all__ = ["INDEX_NAME", "build_viewer", "load_template"]


def load_template() -> bytes:
    """The packaged ``index.html`` (static; company name and dates come from the data file)."""
    return resources.files(__name__).joinpath(INDEX_NAME).read_bytes()


def build_viewer(work_dir: Path, company_name: str | None = None) -> Path:
    """Write ``data/viewer-data.js`` and ``index.html`` into ``work_dir``; return the index path."""
    build_viewer_data(work_dir, company_name)
    dest = resolve_inside(work_dir, INDEX_NAME)
    tmp = dest.with_name(dest.name + ".part")
    try:
        tmp.write_bytes(load_template())
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest
