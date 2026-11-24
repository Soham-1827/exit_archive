"""Adversarial probes for the archive writer, manifest and zipper (tester-added)."""

import csv
import hashlib
import json
import tracemalloc
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from exit_archive.archive.manifest import MANIFEST_NAME, write_manifest
from exit_archive.archive.writer import ArchiveWriter
from exit_archive.archive.zipper import zip_archive

NOW = datetime(2026, 11, 24, tzinfo=UTC)


def read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def build(work: Path, stream: list[tuple[str, dict[str, Any]]]) -> dict[str, int]:
    with ArchiveWriter(work) as w:
        counts = w.add_all(stream)
        w.write_readme()
    return counts


FORMULAS = [
    ("=1+1", "'=1+1"),
    ("+cmd|' /C calc'!A0", "'+cmd|' /C calc'!A0"),
    ("-2+3", "'-2+3"),
    ("@SUM(1)", "'@SUM(1)"),
    ("\t=1", "'\t=1"),
    (" =1", " =1"),  # leading space: not escaped (report only)
    ("-5", "-5"),
    ("+1 555 0100", "'+1 555 0100"),
    ("1e5", "1e5"),
]


@pytest.mark.parametrize(("original", "expected"), FORMULAS)
def test_formula_guard_csv_only(tmp_path: Path, original: str, expected: str) -> None:
    build(tmp_path / "w", [("notes", {"id": 1, "text": original})])
    assert json.loads((tmp_path / "w/data/json/notes.json").read_text())[0]["text"] == original
    rows = read_csv(tmp_path / "w/data/csv/notes.csv")
    assert rows[1][rows[0].index("text")] == expected


def test_unicode_survives_and_row_counts_match(tmp_path: Path) -> None:
    texts = ["\U0001f600 party", "שלום مرحبا", "漢字かな"]
    stream = [("clients", {"id": i, "name": t}) for i, t in enumerate(texts)]
    stream += [("projects", {"id": i}) for i in range(5)]
    counts = build(tmp_path / "w", stream)
    for name, n in counts.items():
        data = json.loads((tmp_path / f"w/data/json/{name}.json").read_text(encoding="utf-8"))
        rows = read_csv(tmp_path / f"w/data/csv/{name}.csv")
        assert len(data) == n == len(rows) - 1
    rows = read_csv(tmp_path / "w/data/csv/clients.csv")
    assert [r[1] for r in rows[1:]] == texts
    assert [
        r["name"] for r in json.loads((tmp_path / "w/data/json/clients.json").read_text())
    ] == texts


def test_dotted_key_collision_report(tmp_path: Path) -> None:
    rec = {"id": 1, "client.name": "literal", "client": {"name": "nested"}}
    build(tmp_path / "w", [("x", rec)])
    # JSON keeps both values.
    assert json.loads((tmp_path / "w/data/json/x.json").read_text())[0] == rec
    rows = read_csv(tmp_path / "w/data/csv/x.csv")
    values = rows[1]
    # Documented finding: CSV has a single "client.name" column; one value is silently lost.
    assert rows[0].count("client.name") == 1
    assert ("literal" in values) != ("nested" in values)


def test_empty_nested_dict_has_no_csv_column_report(tmp_path: Path) -> None:
    build(tmp_path / "w", [("x", {"id": 1, "meta": {}})])
    assert read_csv(tmp_path / "w/data/csv/x.csv")[0] == ["id"]  # finding: {} dropped from CSV


def test_abort_midstream_leaves_valid_json_and_no_tmp(tmp_path: Path) -> None:
    work = tmp_path / "w"
    with pytest.raises(RuntimeError), ArchiveWriter(work) as w:
        w.add("clients", {"id": 1})
        w.add("clients", {"id": 2})
        raise RuntimeError("boom")
    data = json.loads((work / "data/json/clients.json").read_text())
    assert data == [{"id": 1}, {"id": 2}]
    assert not (work / ".tmp").exists()
    assert list((work / "data/csv").iterdir()) == []  # no CSV after abort


def test_manifest_tamper_detected_and_no_pii_fields(tmp_path: Path) -> None:
    work = tmp_path / "w"
    rec = {"id": 1, "email": "jane@example.com", "tok": "Bearer abcdef0123456789abcdef0123456789"}
    build(work, [("users", rec)])
    write_manifest(
        work,
        counts={"users": 1},
        missing=[{"type": "receipt", "id": 3, "reason": "http_404", "email": "a@example.com"}],
        skipped=[],
        account_id=1,
        company_name="Acme",
        generated_at=NOW,
    )
    manifest_text = (work / MANIFEST_NAME).read_text()
    assert "jane@example.com" not in manifest_text and "Bearer" not in manifest_text
    assert "a@example.com" not in manifest_text
    manifest = json.loads(manifest_text)
    assert all(f["path"] != MANIFEST_NAME for f in manifest["files"])

    def bad(m: dict[str, Any]) -> list[str]:
        return [
            f["path"]
            for f in m["files"]
            if hashlib.sha256((work / f["path"]).read_bytes()).hexdigest() != f["sha256"]
        ]

    assert bad(manifest) == []
    (work / "data/json/users.json").write_text("[]")
    assert bad(manifest) == ["data/json/users.json"]


def test_zip_roundtrip_hashes_and_no_part_file(tmp_path: Path) -> None:
    work = tmp_path / "w"
    build(work, [("clients", {"id": i, "name": f"c{i}"}) for i in range(50)])
    write_manifest(
        work, counts={"clients": 50}, missing=[], skipped=[], account_id=1, company_name="A"
    )
    out = tmp_path / "out"
    z = zip_archive(work, out, "Acme & Sons!", now=NOW)
    assert not work.exists()
    assert [p.name for p in out.iterdir()] == [z.name]
    assert z.name == "exit-archive-acme-sons-20261124.zip"
    names = zipfile.ZipFile(z).namelist()
    assert all(".." not in n.split("/") and not n.startswith("/") for n in names)
    extract = tmp_path / "x"
    zipfile.ZipFile(z).extractall(extract)
    top = extract / z.stem
    manifest = json.loads((top / MANIFEST_NAME).read_text())
    assert manifest["files"]
    for f in manifest["files"]:
        assert hashlib.sha256((top / f["path"]).read_bytes()).hexdigest() == f["sha256"]
        assert (top / f["path"]).stat().st_size == f["bytes"]


def test_zip_exception_leaves_no_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "w"
    build(work, [("clients", {"id": 1})])

    def boom(self: zipfile.ZipFile, *a: Any, **k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(zipfile.ZipFile, "write", boom)
    out = tmp_path / "out"
    with pytest.raises(OSError, match="disk full"):
        zip_archive(work, out, "A", now=NOW)
    assert list(out.iterdir()) == []
    assert work.exists()


def test_20000_records_stream_with_bounded_memory(tmp_path: Path) -> None:
    def gen() -> Any:
        for i in range(20_000):
            yield "time_entries", {"id": i, "notes": "x" * 200, "project": {"id": i % 7}}

    tracemalloc.start()
    with ArchiveWriter(tmp_path / "w") as w:
        counts = w.add_all(gen())
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert counts == {"time_entries": 20_000}
    assert len(read_csv(tmp_path / "w/data/csv/time_entries.csv")) == 20_001
    assert len(json.loads((tmp_path / "w/data/json/time_entries.json").read_text())) == 20_000
    # Whole data set is ~5 MB of text; a materialised list would exceed this bound.
    assert peak < 3_000_000, peak
