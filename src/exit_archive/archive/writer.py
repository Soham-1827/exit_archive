"""Streaming JSON + CSV writer for the archive work dir.

Input is the interleaved ``(resource_name, record)`` stream from ``harvest.resources.iter_all``.
One stream per resource is opened lazily and kept open, so records of different resources may
arrive in any order. Pass 1 writes the JSON array and a temp JSONL while collecting the union
of flattened column names; pass 2 writes the CSV from the JSONL line by line. Memory stays
per-record. The temp JSONL area is deleted when the writer closes.
"""

import csv
import json
import re
import shutil
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from exit_archive.errors import ExportError

MAX_NAME_LEN = 100
_DISALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_DOT_RUNS = re.compile(r"\.{2,}")
_TMP_DIR = ".tmp"
_FORMULA_START = ("=", "@", "\t", "\r")
_NUMBER = re.compile(r"[+-]?\d+(\.\d+)?([eE][+-]?\d+)?")

README_TEXT = """Exit Archive
============

This folder is a complete export of your Harvest account.

  data/json/<resource>.json   full Harvest records, one JSON array per resource
  data/csv/<resource>.csv     the same records flattened for spreadsheets
                              (nested fields use dotted columns such as client.name;
                              lists are stored as JSON text; UTF-8 with BOM for Excel)
  MANIFEST.json               record counts, files with size and sha256, anything missing

To verify the archive, compute the sha256 of a file and compare it with its entry in
MANIFEST.json. MANIFEST.json does not list itself. Text cells in the CSV files that start
with =, @ or a sign are prefixed with an apostrophe so spreadsheets do not run them as
formulas; the JSON files hold the original values.
"""


def safe_name(name: str, record_id: int | str | None = None) -> str:
    """Return a file name of ``[A-Za-z0-9._-]`` only, at most 100 chars, optionally id-prefixed."""
    cleaned = _DOT_RUNS.sub(".", _DISALLOWED.sub("_", str(name))).lstrip(".").rstrip(".")
    prefix = ""
    if record_id is not None:
        prefix = _DISALLOWED.sub("_", str(record_id))[:20].lstrip(".") + "_"
    room = MAX_NAME_LEN - len(prefix)
    if len(cleaned) > room:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and stem and len(ext) <= 10:
            cleaned = stem[: room - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:room]
    return (prefix + (cleaned or "file"))[:MAX_NAME_LEN]


def resolve_inside(base: Path, *parts: str) -> Path:
    """Join ``parts`` onto ``base`` and raise if the resolved path leaves ``base``."""
    root = base.resolve()
    target = root.joinpath(*parts).resolve()
    if target != root and not target.is_relative_to(root):
        raise ExportError("Refusing to write outside the archive work directory")
    return target


def flatten(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into dotted keys; lists are kept as JSON text."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{full}."))
        elif isinstance(value, list):
            out[full] = json.dumps(value, ensure_ascii=False)
        else:
            out[full] = value
    return out


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if isinstance(value, str) and text:
        starts_formula = text.startswith(_FORMULA_START) or (
            text[0] in "+-" and not _NUMBER.fullmatch(text)
        )
        if starts_formula:
            return "'" + text
    return text


class _ResourceStream:
    """Open JSON array + temp JSONL for one resource."""

    def __init__(self, json_path: Path, jsonl_path: Path) -> None:
        self.count = 0
        self.columns: dict[str, None] = {}
        self._json: IO[str] = json_path.open("w", encoding="utf-8")
        self._jsonl: IO[str] = jsonl_path.open("w", encoding="utf-8")
        self._json.write("[")

    def add(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        self._json.write(("," if self.count else "") + "\n" + line)
        self._jsonl.write(line + "\n")
        self.columns.update(dict.fromkeys(flatten(record)))
        self.count += 1

    def close(self) -> None:
        self._json.write("\n]\n" if self.count else "]\n")
        self._json.close()
        self._jsonl.close()


class ArchiveWriter:
    """Write ``data/json`` and ``data/csv`` under ``work_dir``; use as a context manager."""

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self._streams: dict[str, _ResourceStream] = {}
        self._paths: dict[str, tuple[Path, Path, Path]] = {}
        self._closed = False
        for sub in ("data/json", "data/csv", _TMP_DIR):
            resolve_inside(work_dir, *sub.split("/")).mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "ArchiveWriter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()

    def open_resource(self, name: str) -> None:
        """Declare a resource so it is written even if it has no records."""
        self._stream(name)

    def _stream(self, name: str) -> _ResourceStream:
        if self._closed:
            raise ExportError("Archive writer is already closed")
        stream = self._streams.get(name)
        if stream is None:
            base = safe_name(name)
            paths = (
                resolve_inside(self.work_dir, "data", "json", f"{base}.json"),
                resolve_inside(self.work_dir, "data", "csv", f"{base}.csv"),
                resolve_inside(self.work_dir, _TMP_DIR, f"{base}.jsonl"),
            )
            if any(paths[0] == p[0] for p in self._paths.values()):
                raise ExportError("Two resources map to the same archive file name")
            self._paths[name] = paths
            stream = _ResourceStream(paths[0], paths[2])
            self._streams[name] = stream
        return stream

    def add(self, name: str, record: dict[str, Any]) -> None:
        self._stream(name).add(record)

    def add_all(self, stream: Iterable[tuple[str, dict[str, Any]]]) -> dict[str, int]:
        """Consume a ``(name, record)`` stream and return record counts per resource."""
        for name, record in stream:
            self.add(name, record)
        return self.counts()

    def counts(self) -> dict[str, int]:
        return {name: s.count for name, s in self._streams.items()}

    def write_readme(self) -> Path:
        path = resolve_inside(self.work_dir, "README.txt")
        path.write_text(README_TEXT, encoding="utf-8")
        return path

    def close(self) -> None:
        """Finish JSON arrays, build every CSV from its JSONL, delete the temp area."""
        if self._closed:
            return
        try:
            for stream in self._streams.values():
                stream.close()
            for name, stream in self._streams.items():
                self._write_csv(self._paths[name][1], self._paths[name][2], stream.columns)
        finally:
            self._closed = True
            shutil.rmtree(self.work_dir / _TMP_DIR, ignore_errors=True)

    def abort(self) -> None:
        """Close files and delete temp data without building CSVs (error path)."""
        if self._closed:
            return
        self._closed = True
        for stream in self._streams.values():
            stream.close()
        shutil.rmtree(self.work_dir / _TMP_DIR, ignore_errors=True)

    @staticmethod
    def _write_csv(csv_path: Path, jsonl_path: Path, columns: dict[str, None]) -> None:
        header = list(columns)
        with csv_path.open("w", encoding="utf-8-sig", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(header)
            for record in _read_jsonl(jsonl_path):
                flat = flatten(record)
                writer.writerow([_cell(flat.get(col)) for col in header])


def iter_json_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the records of a ``data/json/<resource>.json`` file one at a time.

    Relies on the layout ``_ResourceStream`` writes: ``[`` on the first line, one record per
    line (all but the last followed by a comma), ``]`` on the last. The file is never parsed
    as a whole, so memory stays per record.
    """
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if text in ("[", "]", "[]", ""):
                continue
            yield json.loads(text.removesuffix(","))


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            yield json.loads(line)
