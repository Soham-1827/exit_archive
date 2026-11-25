"""Receipt and PDF download policy: which URLs, token or not, file names, missing[] entries.

The actual transfer (redirects, token scoping, size cap, retries) is ``HarvestClient.download``;
nothing here touches the token. Every per-file failure becomes a ``missing[]`` entry and the run
continues, except ``AuthError`` on a receipt (the token died), which aborts.
"""

import asyncio
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from exit_archive.archive.paths import (
    MAX_NUMBER_LEN,  # noqa: F401  (re-exported for callers of the old location)
    RECEIPT_DIR,
    pdf_file_name,
    receipt_file_name,
)
from exit_archive.archive.writer import resolve_inside
from exit_archive.errors import (
    AuthError,
    ExitArchiveError,
    ForbiddenError,
    HarvestUnavailableError,
    NotFoundError,
    RateLimitedError,
)
from exit_archive.harvest.http import DownloadError, HarvestClient, is_harvest_host_url

MAX_FILE_BYTES = 50 * 1024 * 1024
CONCURRENCY = 4
PDF_MAGIC = b"%PDF"

Missing = dict[str, Any]


@dataclass(frozen=True)
class FilesResult:
    """Relative paths written (posix style) and the ``missing[]`` entries."""

    files: list[str] = field(default_factory=list)
    missing: list[Missing] = field(default_factory=list)


@dataclass(frozen=True)
class _Job:
    kind: str
    record_id: Any
    rel_dir: str
    file_name: str
    url: str
    send_token: bool
    require_prefix: bytes | None = None


def _validated_base(base_uri: object) -> str | None:
    """Return ``https://host`` for an allowed company base_uri, else None."""
    if not isinstance(base_uri, str) or not is_harvest_host_url(base_uri):
        return None
    return f"https://{urlsplit(base_uri).hostname}"


def _receipt_jobs(expenses: Iterable[dict[str, Any]]) -> tuple[list[_Job], list[Missing]]:
    jobs: list[_Job] = []
    missing: list[Missing] = []
    for expense in expenses:
        receipt = expense.get("receipt")
        if not isinstance(receipt, dict):
            continue
        url = receipt.get("url")
        if not isinstance(url, str) or not url:
            continue
        eid = expense.get("id")
        if not is_harvest_host_url(url):
            missing.append({"type": "receipt", "id": eid, "reason": "host_not_allowed"})
            continue
        name = receipt_file_name(expense, receipt)
        jobs.append(_Job("receipt", eid, RECEIPT_DIR, name, url, send_token=True))
    return jobs, missing


def _pdf_jobs(
    kind: str, folder: str, docs: Iterable[dict[str, Any]], base: str | None
) -> tuple[list[_Job], list[Missing]]:
    jobs: list[_Job] = []
    missing: list[Missing] = []
    for record in docs:
        rid = record.get("id")
        key = record.get("client_key")
        if base is None:
            missing.append({"type": f"{kind}_pdf", "id": rid, "reason": "base_uri_not_allowed"})
        elif not isinstance(key, str) or not key:
            missing.append({"type": f"{kind}_pdf", "id": rid, "reason": "no_client_key"})
        else:
            name = pdf_file_name(record)
            url = f"{base}/client/{folder}/{quote(key, safe='')}.pdf"
            jobs.append(
                _Job(f"{kind}_pdf", rid, folder, name, url, False, require_prefix=PDF_MAGIC)
            )
    return jobs, missing


def _reason(exc: ExitArchiveError) -> str:
    if isinstance(exc, DownloadError):
        return exc.reason
    if isinstance(exc, NotFoundError):
        return "not_found"
    if isinstance(exc, ForbiddenError):
        return "forbidden"
    if isinstance(exc, HarvestUnavailableError | RateLimitedError):
        return "unavailable"
    return "download_failed"


async def _run_job(
    client: HarvestClient, work_dir: Path, job: _Job, sem: asyncio.Semaphore
) -> tuple[str | None, Missing | None]:
    async with sem:
        dest = resolve_inside(work_dir, job.rel_dir, job.file_name)
        try:
            await client.download(
                job.url,
                dest,
                send_token=job.send_token,
                max_bytes=MAX_FILE_BYTES,
                require_prefix=job.require_prefix,
            )
        except AuthError:
            if job.send_token:
                raise
            return None, {"type": job.kind, "id": job.record_id, "reason": "forbidden"}
        except ExitArchiveError as exc:
            return None, {"type": job.kind, "id": job.record_id, "reason": _reason(exc)}
    return f"{job.rel_dir}/{job.file_name}", None


async def _gather_cancelling(tasks: list[asyncio.Task[Any]]) -> list[Any]:
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def download_files(
    client: HarvestClient,
    work_dir: Path,
    *,
    expenses: Iterable[dict[str, Any]],
    invoices: Iterable[dict[str, Any]],
    estimates: Iterable[dict[str, Any]],
    base_uri: object,
) -> FilesResult:
    """Download receipts and invoice/estimate PDFs under ``work_dir``; never writes the manifest."""
    base = _validated_base(base_uri)
    jobs, missing = _receipt_jobs(expenses)
    for kind, folder, docs in (
        ("invoice", "invoices", invoices),
        ("estimate", "estimates", estimates),
    ):
        more_jobs, more_missing = _pdf_jobs(kind, folder, docs, base)
        jobs += more_jobs
        missing += more_missing
    sem = asyncio.Semaphore(CONCURRENCY)
    awaitables: list[Awaitable[tuple[str | None, Missing | None]]] = [
        _run_job(client, work_dir, job, sem) for job in jobs
    ]
    tasks = [asyncio.ensure_future(a) for a in awaitables]
    results = await _gather_cancelling(tasks)
    files = [path for path, _ in results if path is not None]
    missing += [m for _, m in results if m is not None]
    return FilesResult(files=files, missing=missing)
