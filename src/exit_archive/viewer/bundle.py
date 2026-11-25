"""Build ``data/viewer-data.js``, the slim data file the offline viewer loads.

The file is ``window.EXIT_ARCHIVE_DATA = {...};`` followed by a newline. Keys are flat; a
missing or null nested object gives ``null`` fields. Schema (T2.2's JS reads exactly this):

* ``company_name``: str | null; ``generated_at``: UTC ISO-8601 str
* ``time_entries``: id, date, client_id, client_name, project_id, project_name, task_name,
  user_name, hours, notes, billable, invoice_id
* ``invoices``: id, number, client_id, client_name, issue_date, due_date, amount, state, pdf
* ``estimates``: id, number, client_id, client_name, issue_date, amount, state, pdf
* ``expenses``: id, date, client_id, client_name, project_id, project_name, category_name,
  amount, notes, receipt
* ``clients``: id, name, is_active, currency
* ``projects``: id, name, code, client_id, client_name, is_active

``pdf`` / ``receipt`` are posix paths relative to the archive root, or null when the file is
not in the work dir. Input is the already-written ``data/json/<resource>.json``, read one record
at a time; output is written incrementally. ``<``, ``>``, ``&``, U+2028 and U+2029 are escaped so
the payload can never close a script element or break a JS string.
"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from exit_archive.archive.paths import RECEIPT_DIR, pdf_file_name, receipt_file_name
from exit_archive.archive.writer import iter_json_records, resolve_inside
from exit_archive.errors import ExportError

OUTPUT_REL = ("data", "viewer-data.js")
_PREFIX = "window.EXIT_ARCHIVE_DATA = "
_ESCAPES = str.maketrans(
    {
        "<": "\\u003c",
        ">": "\\u003e",
        "&": "\\u0026",
        " ": "\\u2028",
        " ": "\\u2029",
    }
)

Slim = dict[str, Any]


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).translate(_ESCAPES)


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else None


def _file_if_exists(work_dir: Path, folder: str, name: str) -> str | None:
    try:
        target = resolve_inside(work_dir, folder, name)
    except ExportError:
        return None
    if not target.is_file():
        return None
    return f"{folder}/{name}"


def _pdf_path(work_dir: Path, folder: str, record: dict[str, Any]) -> str | None:
    return _file_if_exists(work_dir, folder, pdf_file_name(record))


def _receipt_path(work_dir: Path, expense: dict[str, Any]) -> str | None:
    receipt = expense.get("receipt")
    if not isinstance(receipt, dict):
        return None
    return _file_if_exists(work_dir, RECEIPT_DIR, receipt_file_name(expense, receipt))


def _ref(record: dict[str, Any], key: str, prefix: str) -> Slim:
    obj = record.get(key)
    return {f"{prefix}_id": _get(obj, "id"), f"{prefix}_name": _get(obj, "name")}


def _time_entry(_: Path, r: dict[str, Any]) -> Slim:
    return {
        "id": r.get("id"),
        "date": r.get("spent_date"),
        **_ref(r, "client", "client"),
        **_ref(r, "project", "project"),
        "task_name": _get(r.get("task"), "name"),
        "user_name": _get(r.get("user"), "name"),
        "hours": r.get("hours"),
        "notes": r.get("notes"),
        "billable": r.get("billable"),
        "invoice_id": _get(r.get("invoice"), "id"),
    }


def _invoice(work_dir: Path, r: dict[str, Any]) -> Slim:
    return {
        "id": r.get("id"),
        "number": r.get("number"),
        **_ref(r, "client", "client"),
        "issue_date": r.get("issue_date"),
        "due_date": r.get("due_date"),
        "amount": r.get("amount"),
        "state": r.get("state"),
        "pdf": _pdf_path(work_dir, "invoices", r),
    }


def _estimate(work_dir: Path, r: dict[str, Any]) -> Slim:
    return {
        "id": r.get("id"),
        "number": r.get("number"),
        **_ref(r, "client", "client"),
        "issue_date": r.get("issue_date"),
        "amount": r.get("amount"),
        "state": r.get("state"),
        "pdf": _pdf_path(work_dir, "estimates", r),
    }


def _expense(work_dir: Path, r: dict[str, Any]) -> Slim:
    return {
        "id": r.get("id"),
        "date": r.get("spent_date"),
        **_ref(r, "client", "client"),
        **_ref(r, "project", "project"),
        "category_name": _get(r.get("expense_category"), "name"),
        "amount": r.get("total_cost"),
        "notes": r.get("notes"),
        "receipt": _receipt_path(work_dir, r),
    }


def _client(_: Path, r: dict[str, Any]) -> Slim:
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "is_active": r.get("is_active"),
        "currency": r.get("currency"),
    }


def _project(_: Path, r: dict[str, Any]) -> Slim:
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "code": r.get("code"),
        **_ref(r, "client", "client"),
        "is_active": r.get("is_active"),
    }


_SECTIONS: tuple[tuple[str, str, Callable[[Path, dict[str, Any]], Slim]], ...] = (
    ("time_entries", "time_entries", _time_entry),
    ("invoices", "invoices", _invoice),
    ("estimates", "estimates", _estimate),
    ("expenses", "expenses", _expense),
    ("clients", "clients", _client),
    ("projects", "projects", _project),
)


def _records(work_dir: Path, resource: str) -> Iterator[dict[str, Any]]:
    path = resolve_inside(work_dir, "data", "json", f"{resource}.json")
    if not path.is_file():
        return
    for record in iter_json_records(path):
        if isinstance(record, dict):
            yield record


def _write_section(
    out: IO[str],
    work_dir: Path,
    resource: str,
    slim: Callable[[Path, dict[str, Any]], Slim],
) -> None:
    out.write("[")
    first = True
    for record in _records(work_dir, resource):
        out.write(("" if first else ",") + _encode(slim(work_dir, record)))
        first = False
    out.write("]")


def build_viewer_data(work_dir: Path, company_name: str | None = None) -> Path:
    """Write ``work_dir/data/viewer-data.js`` and return its path."""
    dest = resolve_inside(work_dir, *OUTPUT_REL)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as out:
            out.write(_PREFIX + "{")
            out.write(f'"company_name":{_encode(company_name)},')
            out.write(f'"generated_at":{_encode(datetime.now(UTC).isoformat())}')
            for key, resource, slim in _SECTIONS:
                out.write(f',"{key}":')
                _write_section(out, work_dir, resource, slim)
            out.write("};\n")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest
