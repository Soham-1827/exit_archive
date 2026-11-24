import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exit_archive.archive.manifest import write_manifest
from exit_archive.archive.writer import ArchiveWriter
from exit_archive.archive.zipper import company_slug, zip_archive
from exit_archive.errors import ExportError

DATE = datetime(2026, 11, 24, tzinfo=UTC)


def make_work(base: Path) -> Path:
    work = base / "work"
    with ArchiveWriter(work) as w:
        counts = w.add_all([("clients", {"id": 1})])
        w.write_readme()
    write_manifest(work, counts=counts, missing=[], skipped=[], account_id=1, company_name="Acme")
    return work


def test_zip_layout_and_work_dir_removed(tmp_path: Path) -> None:
    work = make_work(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    zpath = zip_archive(work, out, "Acme Inc.", now=DATE)
    assert zpath.name == "exit-archive-acme-inc-20261124.zip"
    assert not work.exists()
    top = "exit-archive-acme-inc-20261124/"
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
        assert zf.testzip() is None
    for expected in ("MANIFEST.json", "README.txt", "data/json/", "data/csv/"):
        assert top + expected in names
    assert top + "data/json/clients.json" in names
    for n in names:
        assert ".." not in n.split("/")
        assert not n.startswith("/")
        assert n.startswith(top)


@pytest.mark.parametrize(
    ("company", "slug"),
    [
        ("Acme Inc.", "acme-inc"),
        ("../../etc/passwd", "etc-passwd"),
        ("日本語", "company"),
        ("", "company"),
        ("A" * 200, "a" * 40),
        ("a/b\\c\x00d", "a-b-c-d"),
    ],
)
def test_company_slug(company: str, slug: str) -> None:
    assert company_slug(company) == slug


def test_zip_rejects_symlink(tmp_path: Path) -> None:
    work = make_work(tmp_path)
    target = tmp_path / "secret.txt"
    target.write_text("s")
    (work / "l.txt").symlink_to(target)
    with pytest.raises(ExportError):
        zip_archive(work, tmp_path, "Acme", now=DATE)


def test_zip_refuses_output_inside_work_dir(tmp_path: Path) -> None:
    work = make_work(tmp_path)
    with pytest.raises(ExportError):
        zip_archive(work, work / "sub", "Acme", now=DATE)
    assert work.exists()


def test_zip_missing_work_dir(tmp_path: Path) -> None:
    with pytest.raises(ExportError):
        zip_archive(tmp_path / "nope", tmp_path, "Acme", now=DATE)
