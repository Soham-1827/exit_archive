import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exit_archive import __version__
from exit_archive.archive.manifest import build_manifest, write_manifest
from exit_archive.archive.writer import ArchiveWriter
from exit_archive.errors import ExportError


def make_work(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    with ArchiveWriter(tmp_path) as w:
        counts = w.add_all([("clients", {"id": 1}), ("clients", {"id": 2}), ("tasks", {"id": 3})])
        w.write_readme()
    (tmp_path / "invoices").mkdir()
    (tmp_path / "invoices/1_a.pdf").write_bytes(b"%PDF-1.4 fake")
    return tmp_path, counts


def test_manifest_lists_every_file_with_sha256(tmp_path: Path) -> None:
    work, counts = make_work(tmp_path)
    path = write_manifest(
        work,
        counts=counts,
        missing=[{"type": "receipt", "id": 5, "reason": "not_found"}],
        skipped=["estimates"],
        account_id=123,
        company_name="Acme Inc",
        generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    assert path == work / "MANIFEST.json"
    m = json.loads(path.read_text(encoding="utf-8"))
    on_disk = {p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()} - {
        "MANIFEST.json"
    }
    assert {f["path"] for f in m["files"]} == on_disk
    for f in m["files"]:
        data = (work / f["path"]).read_bytes()
        assert f["sha256"] == hashlib.sha256(data).hexdigest()
        assert f["bytes"] == len(data)
    assert m["counts"] == {"clients": 2, "tasks": 1}
    assert m["missing"] == [{"type": "receipt", "id": 5, "reason": "not_found"}]
    assert m["skipped"] == ["estimates"]
    assert m["account_id"] == 123
    assert m["company_name"] == "Acme Inc"
    assert m["tool_version"] == __version__
    assert m["generated_at"] == "2026-01-02T03:04:05+00:00"
    assert "MANIFEST.json" not in {f["path"] for f in m["files"]}


def test_manifest_drops_unexpected_missing_fields(tmp_path: Path) -> None:
    work, counts = make_work(tmp_path)
    m = build_manifest(
        work,
        counts=counts,
        missing=[{"type": "receipt", "id": 5, "reason": "x", "email": "a@b.com", "token": "t"}],
        skipped=[],
        account_id="9",
        company_name="C",
    )
    assert m["missing"] == [{"type": "receipt", "id": 5, "reason": "x"}]
    assert "a@b.com" not in json.dumps(m)


def test_manifest_defaults_generated_at_is_utc_iso(tmp_path: Path) -> None:
    work, counts = make_work(tmp_path)
    m = build_manifest(work, counts=counts, missing=[], skipped=[], account_id=1, company_name="C")
    assert datetime.fromisoformat(m["generated_at"]).tzinfo is not None


def test_manifest_refuses_symlink(tmp_path: Path) -> None:
    work, counts = make_work(tmp_path)
    secret = tmp_path.parent / "outside.txt"
    secret.write_text("x")
    (work / "link.txt").symlink_to(secret)
    with pytest.raises(ExportError):
        build_manifest(work, counts=counts, missing=[], skipped=[], account_id=1, company_name="C")
