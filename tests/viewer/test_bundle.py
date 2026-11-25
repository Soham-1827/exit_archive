"""build_viewer_data: slim, safe, incremental viewer payload."""

import json
from pathlib import Path
from typing import Any

import pytest

from exit_archive.archive.writer import ArchiveWriter
from exit_archive.viewer.bundle import build_viewer_data

PREFIX = "window.EXIT_ARCHIVE_DATA = "
KEYS = {"time_entries", "invoices", "estimates", "expenses", "clients", "projects"}
XSS = "</script><img src=x onerror=alert(1)>"


def _client(i: int = 1) -> dict[str, Any]:
    return {"id": i, "name": f"Client {i}"}


def _time_entry(i: int, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": i,
        "spent_date": "2024-01-02",
        "hours": 1.5,
        "notes": f"note {i}",
        "is_billed": True,
        "billable": True,
        "client": _client(),
        "project": {"id": 5, "name": "Proj", "code": "P"},
        "task": {"id": 6, "name": "Dev"},
        "user": {"id": 7, "name": "Ann", "email": "ann@example.com"},
        "invoice": {"id": 9, "number": "INV-1"},
        "external_reference": {"permalink": "https://x"},
    }
    base.update(over)
    return base


def _write(work: Path, **resources: list[dict[str, Any]]) -> None:
    with ArchiveWriter(work) as writer:
        for name, records in resources.items():
            writer.open_resource(name)
            for record in records:
                writer.add(name, record)


def _load(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith(PREFIX)
    assert text.endswith(";\n")
    return json.loads(text[len(PREFIX) : -2])  # type: ignore[no-any-return]


def _full(work: Path) -> None:
    (work / "receipts").mkdir(parents=True, exist_ok=True)
    (work / "invoices").mkdir(parents=True, exist_ok=True)
    (work / "estimates").mkdir(parents=True, exist_ok=True)
    (work / "receipts" / "11_r.png").write_bytes(b"x")
    (work / "invoices" / "INV-1_9.pdf").write_bytes(b"%PDF")
    (work / "estimates" / "E-1_3.pdf").write_bytes(b"%PDF")
    _write(
        work,
        time_entries=[_time_entry(1)],
        invoices=[
            {
                "id": 9,
                "number": "INV-1",
                "client": _client(),
                "issue_date": "2024-01-01",
                "due_date": "2024-02-01",
                "amount": 100.0,
                "state": "paid",
                "client_key": "SECRETKEY",
                "line_items": [{"x": 1}],
            }
        ],
        estimates=[
            {
                "id": 3,
                "number": "E-1",
                "client": _client(),
                "issue_date": "2024-01-01",
                "amount": 50.0,
                "state": "sent",
                "client_key": "K2",
            }
        ],
        expenses=[
            {
                "id": 11,
                "spent_date": "2024-01-03",
                "total_cost": 12.5,
                "notes": "lunch",
                "client": _client(),
                "project": {"id": 5, "name": "Proj"},
                "expense_category": {"id": 2, "name": "Meals"},
                "receipt": {"url": "https://x.harvestapp.com/r", "file_name": "r.png"},
                "user": {"id": 7, "name": "Ann"},
            }
        ],
        clients=[
            {"id": 1, "name": "Client 1", "is_active": True, "currency": "EUR", "address": "x"}
        ],
        projects=[
            {
                "id": 5,
                "name": "Proj",
                "code": "P",
                "is_active": False,
                "client": _client(),
                "budget": 1,
            }
        ],
    )


def test_file_shape_and_keys(tmp_path: Path) -> None:
    _full(tmp_path)
    out = build_viewer_data(tmp_path, "Acme")
    assert out == tmp_path / "data" / "viewer-data.js"
    data = _load(out)
    assert set(data) >= KEYS
    assert set(data) <= KEYS | {"company_name", "generated_at"}
    assert data["company_name"] == "Acme"


def test_company_name_optional(tmp_path: Path) -> None:
    _full(tmp_path)
    data = _load(build_viewer_data(tmp_path))
    assert data.get("company_name") is None


def test_exact_slim_key_sets_and_values(tmp_path: Path) -> None:
    _full(tmp_path)
    data = _load(build_viewer_data(tmp_path))
    te = data["time_entries"][0]
    assert te == {
        "id": 1,
        "date": "2024-01-02",
        "client_id": 1,
        "client_name": "Client 1",
        "project_id": 5,
        "project_name": "Proj",
        "task_name": "Dev",
        "user_name": "Ann",
        "hours": 1.5,
        "notes": "note 1",
        "billable": True,
        "invoice_id": 9,
    }
    assert set(data["invoices"][0]) == {
        "id", "number", "client_id", "client_name", "issue_date", "due_date",
        "amount", "state", "pdf",
    }  # fmt: skip
    assert set(data["estimates"][0]) == {
        "id", "number", "client_id", "client_name", "issue_date", "amount", "state", "pdf",
    }  # fmt: skip
    assert data["expenses"][0] == {
        "id": 11,
        "date": "2024-01-03",
        "client_id": 1,
        "client_name": "Client 1",
        "project_id": 5,
        "project_name": "Proj",
        "category_name": "Meals",
        "amount": 12.5,
        "notes": "lunch",
        "receipt": "receipts/11_r.png",
    }
    assert data["clients"][0] == {"id": 1, "name": "Client 1", "is_active": True, "currency": "EUR"}
    assert data["projects"][0] == {
        "id": 5,
        "name": "Proj",
        "code": "P",
        "client_id": 1,
        "client_name": "Client 1",
        "is_active": False,
    }
    raw = (tmp_path / "data" / "viewer-data.js").read_text(encoding="utf-8")
    for leaked in ("SECRETKEY", "ann@example.com", "harvestapp.com", "client_key"):
        assert leaked not in raw


def test_paths_exist_and_are_relative_posix(tmp_path: Path) -> None:
    _full(tmp_path)
    data = _load(build_viewer_data(tmp_path))
    assert data["invoices"][0]["pdf"] == "invoices/INV-1_9.pdf"
    assert data["estimates"][0]["pdf"] == "estimates/E-1_3.pdf"
    for key in ("invoices", "estimates", "expenses"):
        for rec in data[key]:
            path = rec.get("pdf", rec.get("receipt"))
            assert path is not None
            assert (tmp_path / path).is_file()
            assert not path.startswith("/") and ".." not in path and "\\" not in path


def test_missing_files_give_null(tmp_path: Path) -> None:
    _full(tmp_path)
    (tmp_path / "receipts" / "11_r.png").unlink()
    (tmp_path / "invoices" / "INV-1_9.pdf").unlink()
    (tmp_path / "estimates" / "E-1_3.pdf").unlink()
    data = _load(build_viewer_data(tmp_path))
    assert data["expenses"][0]["receipt"] is None
    assert data["invoices"][0]["pdf"] is None
    assert data["estimates"][0]["pdf"] is None


def test_hostile_names_never_escape(tmp_path: Path) -> None:
    (tmp_path / "invoices").mkdir()
    _write(
        tmp_path,
        invoices=[{"id": 1, "number": "../../etc/passwd", "client": None}],
        expenses=[{"id": 2, "receipt": {"file_name": "../../x\\y.png", "url": "u"}}],
    )
    data = _load(build_viewer_data(tmp_path))
    assert data["invoices"][0]["pdf"] is None
    assert data["expenses"][0]["receipt"] is None


def test_xss_note_round_trips_and_file_has_no_angle_brackets(tmp_path: Path) -> None:
    nasty = XSS + " &     café 日本"
    _write(tmp_path, time_entries=[_time_entry(1, notes=nasty)])
    out = build_viewer_data(tmp_path)
    raw = out.read_text(encoding="utf-8")
    assert "<" not in raw and ">" not in raw and "&" not in raw
    assert " " not in raw and " " not in raw
    assert "café 日本" in raw  # non-ASCII kept literally, UTF-8
    assert _load(out)["time_entries"][0]["notes"] == nasty


def test_features_off_gives_empty_lists(tmp_path: Path) -> None:
    _write(tmp_path, time_entries=[_time_entry(1)], clients=[], projects=[])
    data = _load(build_viewer_data(tmp_path))
    assert data["invoices"] == [] and data["estimates"] == [] and data["expenses"] == []
    assert len(data["time_entries"]) == 1


def test_no_data_dir_at_all(tmp_path: Path) -> None:
    data = _load(build_viewer_data(tmp_path))
    assert all(data[k] == [] for k in KEYS)


def test_null_and_missing_nested_objects(tmp_path: Path) -> None:
    _write(
        tmp_path,
        time_entries=[
            {"id": 1, "client": None, "project": None, "task": None, "user": None, "invoice": None},
            {"id": 2, "client": "weird", "task": 5},
            {"id": 3},
        ],
        invoices=[{"id": 4}],
        estimates=[{"id": 5, "client": None}],
        expenses=[{"id": 6, "receipt": None, "expense_category": None}],
        clients=[{"id": 7}],
        projects=[{"id": 8, "client": None}],
    )
    data = _load(build_viewer_data(tmp_path))
    for rec in data["time_entries"]:
        for key in (
            "client_id",
            "client_name",
            "project_id",
            "task_name",
            "user_name",
            "invoice_id",
        ):
            assert rec[key] is None
    assert data["invoices"][0]["client_name"] is None and data["invoices"][0]["pdf"] is None
    assert data["expenses"][0]["category_name"] is None and data["expenses"][0]["receipt"] is None
    assert data["clients"][0]["name"] is None
    assert data["projects"][0]["client_id"] is None


def test_nan_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "data" / "json").mkdir(parents=True)
    (tmp_path / "data" / "json" / "time_entries.json").write_text(
        '[\n{"id": 1, "hours": NaN}\n]\n', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        build_viewer_data(tmp_path)


def test_large_fixture_streams_without_whole_file_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = 5000
    _write(tmp_path, time_entries=[_time_entry(i) for i in range(n)])
    size = (tmp_path / "data" / "json" / "time_entries.json").stat().st_size
    real_loads = json.loads

    def forbidden(*a: Any, **k: Any) -> Any:
        raise AssertionError("whole-file json.load")

    def guarded(text: Any, *a: Any, **k: Any) -> Any:
        assert len(text) < size / 100, "parsed a large chunk"
        return real_loads(text, *a, **k)

    monkeypatch.setattr(json, "load", forbidden)
    monkeypatch.setattr(json, "loads", guarded)
    out = build_viewer_data(tmp_path)
    monkeypatch.undo()
    data = _load(out)
    assert len(data["time_entries"]) == n
    assert [r["id"] for r in data["time_entries"][:3]] == [0, 1, 2]


def test_symlink_escaping_work_dir_gives_null(tmp_path: Path) -> None:
    work = tmp_path / "work"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "INV-1_1.pdf").write_bytes(b"%PDF")
    work.mkdir()
    (work / "invoices").symlink_to(outside, target_is_directory=True)
    _write(work, invoices=[{"id": 1, "number": "INV-1"}])
    assert _load(build_viewer_data(work))["invoices"][0]["pdf"] is None
