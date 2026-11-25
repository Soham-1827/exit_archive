"""iter_json_records reads back exactly what ArchiveWriter wrote, one record at a time."""

import json
from pathlib import Path
from typing import Any

import pytest

from exit_archive.archive.writer import ArchiveWriter, iter_json_records


def _write(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    with ArchiveWriter(tmp_path) as writer:
        writer.open_resource("things")
        for record in records:
            writer.add("things", record)
    return tmp_path / "data" / "json" / "things.json"


@pytest.mark.parametrize("count", [0, 1, 2, 250])
def test_reader_round_trips_writer_output(tmp_path: Path, count: int) -> None:
    records = [{"id": i, "name": f"n{i}", "nested": {"a": [1, 2]}} for i in range(count)]
    path = _write(tmp_path, records)
    assert list(iter_json_records(path)) == records


def test_reader_handles_newlines_unicode_and_brackets(tmp_path: Path) -> None:
    records = [
        {"id": 1, "notes": "line one\nline two\r\nthree\ttab"},
        {"id": 2, "notes": "café 日本語 \U0001f600    "},
        {"id": 3, "notes": "[\n]\n[],"},
        {"id": 4, "notes": "ends with comma,"},
    ]
    path = _write(tmp_path, records)
    assert list(iter_json_records(path)) == records


def test_reader_is_lazy_and_never_parses_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [{"id": i} for i in range(50)]
    path = _write(tmp_path, records)
    whole = path.read_text(encoding="utf-8")
    real_loads = json.loads

    def guarded(text: str, *a: Any, **k: Any) -> Any:
        assert len(text) < len(whole) / 5, "parsed a large chunk"
        return real_loads(text, *a, **k)

    def forbidden(*a: Any, **k: Any) -> Any:
        raise AssertionError("json.load of a whole file")

    monkeypatch.setattr(json, "load", forbidden)
    monkeypatch.setattr(json, "loads", guarded)
    it = iter_json_records(path)
    assert next(it) == {"id": 0}
    assert list(it) == records[1:]
