"""Relative-path rules for downloaded files, shared by the downloader and the viewer bundle.

Both ``harvest.files`` (which writes the files) and ``viewer.bundle`` (which links to them) must
agree on where a receipt or PDF lives, so the rules exist once, here. Paths are ``(folder, name)``
pairs relative to the archive root; names pass through ``safe_name``.
"""

from typing import Any

from exit_archive.archive.writer import safe_name

MAX_NUMBER_LEN = 60
RECEIPT_DIR = "receipts"


def receipt_file_name(expense: dict[str, Any], receipt: dict[str, Any]) -> str:
    """File name for an expense receipt (id-prefixed so names never collide)."""
    return safe_name(str(receipt.get("file_name") or ""), expense.get("id"))


def pdf_file_name(record: dict[str, Any]) -> str:
    """File name for an invoice or estimate PDF: ``<number>_<id>.pdf``."""
    number = safe_name(str(record.get("number") or ""))[:MAX_NUMBER_LEN]
    return safe_name(f"{number}_{record.get('id')}.pdf")
