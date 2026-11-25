"""Adversarial checks for the viewer bundle and the shared path helpers."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from exit_archive.archive.paths import pdf_file_name, receipt_file_name
from exit_archive.archive.writer import ArchiveWriter, iter_json_records, safe_name
from exit_archive.viewer.bundle import build_viewer_data

PREFIX = "window.EXIT_ARCHIVE_DATA = "


def _write(work: Path, **resources: list[dict[str, Any]]) -> None:
    with ArchiveWriter(work) as writer:
        for name, records in resources.items():
            writer.open_resource(name)
            for record in records:
                writer.add(name, record)


def _load(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return json.loads(text[len(PREFIX) : -2])  # type: ignore[no-any-return]


NASTY = 'a b c&amp;<>\r\n\t\x00\x1f\u0085﻿]\n[,}{"\\ \U0001f600'


def test_nasty_text_round_trips_and_raw_file_is_safe(tmp_path: Path) -> None:
    _write(tmp_path, time_entries=[{"id": 1, "notes": NASTY}, {"id": 2, "notes": "]"}])
    raw = build_viewer_data(tmp_path, NASTY).read_text(encoding="utf-8")
    for bad in ("<", ">", "&", " ", " "):
        assert bad not in raw
    data = _load(tmp_path / "data" / "viewer-data.js")
    assert data["company_name"] == NASTY
    assert [e["notes"] for e in data["time_entries"]] == [NASTY, "]"]
    assert raw.count("\n") == 1  # one trailing newline: valid single-line JS statement


def test_hostile_numbers_and_ids_never_escape_or_leak(tmp_path: Path) -> None:
    (tmp_path / "secret.pdf").write_bytes(b"%PDF")
    (tmp_path / "invoices").mkdir()
    inv = [
        {"id": "../..", "number": "../../secret", "client_key": "K"},
        {"id": 3, "number": "/etc/passwd\\..\\x"},
        {"id": 4, "number": None},
    ]
    _write(tmp_path, invoices=inv)
    build_viewer_data(tmp_path)
    for r in _load(tmp_path / "data" / "viewer-data.js")["invoices"]:
        assert "client_key" not in r
        p = r["pdf"]
        assert p is None or not (p.startswith("/") or ".." in p.split("/") or "\\" in p)


def test_symlinked_pdf_dir_outside_gives_null(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    rec = {"id": 9, "number": "INV-1"}
    (outside / pdf_file_name(rec)).write_bytes(b"%PDF")
    os.symlink(outside, work / "invoices")
    _write(work, invoices=[rec])
    build_viewer_data(work)
    assert _load(work / "data" / "viewer-data.js")["invoices"][0]["pdf"] is None


def test_wrong_typed_fields_do_not_crash(tmp_path: Path) -> None:
    _write(
        tmp_path,
        expenses=[{"id": 1, "receipt": "x", "client": 5, "project": [], "expense_category": "c"}],
        time_entries=[{"id": 1, "client": "str", "task": 3, "user": [], "invoice": True}],
    )
    build_viewer_data(tmp_path)
    data = _load(tmp_path / "data" / "viewer-data.js")
    assert data["expenses"][0]["receipt"] is None
    assert data["time_entries"][0]["client_name"] is None


def test_reader_non_dict_lines_and_garbage(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    p.write_text('[\n1,\n"s",\n{"a":1}\n]\n', encoding="utf-8")
    assert list(iter_json_records(p))[-1] == {"a": 1}
    p.write_text("[\n{broken\n]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        list(iter_json_records(p))


@pytest.mark.parametrize(
    "fname", ["", None, "../../x.png", "a b/c.png", "é.png", "x" * 500 + ".png"]
)
@pytest.mark.parametrize("eid", [1, "7", None, "../1"])
def test_helpers_match_original_inline_logic(fname: Any, eid: Any) -> None:
    exp = {"id": eid, "number": fname}
    receipt = {"file_name": fname}
    assert receipt_file_name(exp, receipt) == safe_name(str(receipt.get("file_name") or ""), eid)
    number = safe_name(str(exp.get("number") or ""))[:60]
    assert pdf_file_name(exp) == safe_name(f"{number}_{eid}.pdf")
