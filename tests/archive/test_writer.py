import csv
import json
from pathlib import Path

import pytest

from exit_archive.archive.writer import ArchiveWriter, flatten, resolve_inside, safe_name
from exit_archive.errors import ExportError

NASTY = [
    "../../etc/passwd",
    "a/b/c",
    "a\\b\\c",
    "nul\x00byte",
    "x" * 300,
    "héllo wörld ✓ 日本語",
    "..",
    ".",
    "",
    "/abs/path",
    "C:\\Windows\\system32",
    "....//....//x",
]


@pytest.mark.parametrize("name", NASTY)
def test_safe_name_stays_inside_work_dir(tmp_path: Path, name: str) -> None:
    safe = safe_name(name, 42)
    assert safe
    assert len(safe) <= 100
    assert all(c.isalnum() and c.isascii() or c in "._-" for c in safe)
    assert not safe.startswith(".")
    resolved = resolve_inside(tmp_path, "receipts", safe)
    assert resolved.is_relative_to(tmp_path.resolve())


def test_safe_name_id_prefix_and_keeps_extension() -> None:
    assert safe_name("receipt.pdf", 7) == "7_receipt.pdf"
    long = safe_name("y" * 300 + ".pdf", 123456)
    assert len(long) <= 100
    assert long.startswith("123456_")
    assert long.endswith(".pdf")


def test_safe_name_without_id() -> None:
    assert safe_name("clients") == "clients"
    assert ".." not in safe_name("../../etc/passwd")
    assert "/" not in safe_name("../../etc/passwd")


def test_resolve_inside_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(ExportError):
        resolve_inside(tmp_path, "..", "evil")
    with pytest.raises(ExportError):
        resolve_inside(tmp_path, "/etc/passwd")


def test_resolve_inside_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / "link").symlink_to(outside)
    with pytest.raises(ExportError):
        resolve_inside(work, "link", "f.txt")


def test_flatten_nested_and_lists() -> None:
    flat = flatten({"id": 1, "client": {"id": 2, "name": "A"}, "items": [{"a": 1}], "n": None})
    assert flat["client.id"] == 2
    assert flat["client.name"] == "A"
    assert flat["items"] == '[{"a": 1}]'
    assert flat["n"] is None
    assert "client" not in flat


def read_json(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


def test_csv_round_trip_nested_and_tricky_values(tmp_path: Path) -> None:
    tricky = 'a, "quoted"\nsecond line'
    with ArchiveWriter(tmp_path) as w:
        w.add("time_entries", {"id": 1, "notes": tricky, "client": {"id": 9, "name": "Acme"}})
    header, rows = read_csv(tmp_path / "data/csv/time_entries.csv")
    assert "client.name" in header
    row = dict(zip(header, rows[0], strict=True))
    assert row["notes"] == tricky
    assert row["client.name"] == "Acme"
    assert row["client.id"] == "9"
    assert read_json(tmp_path / "data/json/time_entries.json")[0]["notes"] == tricky


def test_interleaved_stream(tmp_path: Path) -> None:
    stream = [
        ("invoices", {"id": 1, "client": {"name": "A"}}),
        ("invoice_payments", {"id": 10, "invoice_id": 1, "amount": 5}),
        ("invoices", {"id": 2, "client": {"name": "B"}, "extra": {"k": "v"}}),
        ("invoice_payments", {"id": 11, "invoice_id": 2, "amount": 6, "note": "x"}),
    ]
    with ArchiveWriter(tmp_path) as w:
        counts = w.add_all(stream)
    assert counts == {"invoices": 2, "invoice_payments": 2}
    inv = read_json(tmp_path / "data/json/invoices.json")
    assert [r["id"] for r in inv] == [1, 2]
    pay = read_json(tmp_path / "data/json/invoice_payments.json")
    assert [r["id"] for r in pay] == [10, 11]
    header, rows = read_csv(tmp_path / "data/csv/invoices.csv")
    assert header == ["id", "client.name", "extra.k"]
    assert rows == [["1", "A", ""], ["2", "B", "v"]]
    header, rows = read_csv(tmp_path / "data/csv/invoice_payments.csv")
    assert header == ["id", "invoice_id", "amount", "note"]
    assert rows == [["10", "1", "5", ""], ["11", "2", "6", "x"]]


def test_empty_resource_and_no_jsonl_left(tmp_path: Path) -> None:
    with ArchiveWriter(tmp_path) as w:
        w.open_resource("clients")
        w.add("projects", {"id": 1})
    assert read_json(tmp_path / "data/json/clients.json") == []
    header, rows = read_csv(tmp_path / "data/csv/clients.csv")
    assert rows == []
    assert not list(tmp_path.rglob("*.jsonl"))
    assert not (tmp_path / ".tmp").exists()


def test_streams_closed_and_cleaned_on_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), ArchiveWriter(tmp_path) as w:
        w.add("clients", {"id": 1})
        raise RuntimeError("boom")
    assert not list(tmp_path.rglob("*.jsonl"))


def test_resource_name_is_sanitised(tmp_path: Path) -> None:
    with ArchiveWriter(tmp_path) as w:
        w.add("../../evil", {"id": 1})
    for p in tmp_path.rglob("*"):
        assert p.resolve().is_relative_to(tmp_path.resolve())
    assert not (tmp_path.parent / "evil.json").exists()


def test_unicode_and_formula_cells(tmp_path: Path) -> None:
    with ArchiveWriter(tmp_path) as w:
        w.add("clients", {"id": 1, "name": "日本語 ✓", "note": '=HYPERLINK("x")', "amt": -5})
    header, rows = read_csv(tmp_path / "data/csv/clients.csv")
    row = dict(zip(header, rows[0], strict=True))
    assert row["name"] == "日本語 ✓"
    assert row["note"].startswith("'=")
    assert row["amt"] == "-5"
    assert read_json(tmp_path / "data/json/clients.json")[0]["note"] == '=HYPERLINK("x")'


def test_bool_and_none_cells(tmp_path: Path) -> None:
    with ArchiveWriter(tmp_path) as w:
        w.add("clients", {"id": 1, "active": True, "x": None})
    header, rows = read_csv(tmp_path / "data/csv/clients.csv")
    row = dict(zip(header, rows[0], strict=True))
    assert row["active"] == "true"
    assert row["x"] == ""


def test_readme_written(tmp_path: Path) -> None:
    with ArchiveWriter(tmp_path) as w:
        w.write_readme()
    text = (tmp_path / "README.txt").read_text(encoding="utf-8")
    assert "MANIFEST.json" in text
    assert "sha256" in text.lower()


def test_add_after_close_rejected(tmp_path: Path) -> None:
    w = ArchiveWriter(tmp_path)
    w.close()
    with pytest.raises(ExportError):
        w.add("clients", {"id": 1})
