"""Orchestrate one export: token in, zip out. Shared by the CLI and (later) the web app.

Records stream from Harvest straight into the archive writer; only slim projections needed to
download files (ids, receipt dicts, invoice/estimate numbers and client keys) are kept in memory.
On any failure the work dir is removed and no ``.zip`` or ``.zip.part`` is left in ``out_dir``.
"""

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exit_archive.archive.manifest import write_manifest
from exit_archive.archive.writer import ArchiveWriter
from exit_archive.archive.zipper import zip_archive
from exit_archive.harvest.accounts import choose_account, discover_accounts
from exit_archive.harvest.files import download_files
from exit_archive.harvest.http import HarvestClient
from exit_archive.harvest.resources import (
    INVOICE_PAYMENTS,
    REGISTRY,
    Company,
    fetch_company,
    iter_all,
    skipped_resources,
)
from exit_archive.secrets import HarvestToken

Progress = Callable[[str, int, int | None], None]
RECORD_PROGRESS_EVERY = 500


@dataclass(frozen=True)
class ExportResult:
    zip_path: Path
    counts: dict[str, int]
    missing: list[dict[str, Any]]
    skipped: list[str]


@dataclass
class _Slim:
    """Just enough of each record to download its files later."""

    expenses: list[dict[str, Any]]
    invoices: list[dict[str, Any]]
    estimates: list[dict[str, Any]]

    def remember(self, name: str, record: dict[str, Any]) -> None:
        if name == "expenses":
            receipt = record.get("receipt")
            if isinstance(receipt, dict):
                self.expenses.append({"id": record.get("id"), "receipt": receipt})
        elif name in ("invoices", "estimates"):
            target = self.invoices if name == "invoices" else self.estimates
            target.append(
                {k: record.get(k) for k in ("id", "number", "client_key")},
            )


def _resource_names(company: Company) -> list[str]:
    skipped = set(skipped_resources(company))
    names = [r.name for r in REGISTRY]
    names.insert(names.index("estimate_item_categories"), INVOICE_PAYMENTS.name)
    return [n for n in names if n not in skipped]


async def _stream_records(
    client: HarvestClient,
    company: Company,
    writer: ArchiveWriter,
    missing: list[dict[str, Any]],
    report: Progress,
) -> _Slim:
    slim = _Slim([], [], [])
    done = 0
    async for name, record in iter_all(client, company, on_missing=missing.append):
        writer.add(name, record)
        slim.remember(name, record)
        done += 1
        if done % RECORD_PROGRESS_EVERY == 0:
            report("records", done, None)
    report("records", done, None)
    return slim


async def export_account(
    token: HarvestToken,
    out_dir: Path,
    account_id: int | None = None,
    progress: Progress | None = None,
) -> ExportResult:
    """Export one Harvest account to ``out_dir`` as a zip and return what was written."""

    def report(stage: str, done: int, total: int | None) -> None:
        if progress is not None:
            progress(stage, done, total)

    report("accounts", 0, 1)
    account = choose_account(await discover_accounts(token), account_id)
    report("accounts", 1, 1)
    out_dir.mkdir(parents=True, exist_ok=True)
    async with HarvestClient(token, account.id) as client:
        report("company", 0, 1)
        company = await fetch_company(client)
        report("company", 1, 1)
        work_dir = Path(tempfile.mkdtemp(dir=out_dir))
        writer = ArchiveWriter(work_dir)
        try:
            return await _export_into(
                client, company, writer, work_dir, out_dir, account.id, report
            )
        except BaseException:
            writer.abort()
            raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


async def _export_into(
    client: HarvestClient,
    company: Company,
    writer: ArchiveWriter,
    work_dir: Path,
    out_dir: Path,
    account_id: int,
    report: Progress,
) -> ExportResult:
    for name in _resource_names(company):
        writer.open_resource(name)
    missing: list[dict[str, Any]] = []
    slim = await _stream_records(client, company, writer, missing, report)
    total_files = len(slim.expenses) + len(slim.invoices) + len(slim.estimates)
    report("files", 0, total_files)
    files = await download_files(
        client,
        work_dir,
        expenses=slim.expenses,
        invoices=slim.invoices,
        estimates=slim.estimates,
        base_uri=company.base_uri,
    )
    report("files", total_files, total_files)
    missing += files.missing
    writer.write_readme()
    writer.close()
    counts = writer.counts()
    skipped = skipped_resources(company)
    report("archive", 0, 1)
    write_manifest(
        work_dir,
        counts=counts,
        missing=missing,
        skipped=skipped,
        account_id=account_id,
        company_name=company.name,
    )
    zip_path = zip_archive(work_dir, out_dir, company.name)
    report("archive", 1, 1)
    return ExportResult(zip_path=zip_path, counts=counts, missing=missing, skipped=skipped)
