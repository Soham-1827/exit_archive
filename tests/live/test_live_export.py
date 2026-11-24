"""Live smoke test against the Harvest trial account. Skipped unless credentials exist.

Secrecy: nothing here prints, and assertion messages carry counts and file names only.
Comparisons that would make pytest introspection show the token, account id or record
values are reduced to booleans or name lists first.
"""

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from exit_archive.exporter import export_account
from exit_archive.secrets import HarvestToken
from tests.conftest import LiveCredentials

pytestmark = pytest.mark.live

EXPECTED_COUNTS = {
    "clients": 1,
    "projects": 1,
    "time_entries": 1,
    "expenses": 1,
    "invoices": 1,
}
KEEP_VAR = "EXIT_ARCHIVE_KEEP_LIVE_ZIP"
REPO_ROOT = Path(__file__).resolve().parents[2]
KEEP_DIR = REPO_ROOT / "tmp" / "live"


def _keep_copy(zip_path: Path) -> None:
    """Copy the zip into the gitignored ``tmp/live``; skip the copy if not ignored."""
    target = KEEP_DIR / zip_path.name
    check = subprocess.run(  # noqa: S603
        ["git", "check-ignore", "-q", str(target.relative_to(REPO_ROOT))],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    if check.returncode != 0:
        return
    KEEP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(zip_path, target)


async def test_live_export_matches_trial_account(
    harvest_live_credentials: LiveCredentials, tmp_path: Path
) -> None:
    creds = harvest_live_credentials
    out_dir = tmp_path / "out"
    result = await export_account(
        HarvestToken(creds.token), out_dir, account_id=int(creds.account_id)
    )
    zip_path = result.zip_path
    if os.environ.get(KEEP_VAR) == "1":
        _keep_copy(zip_path)

    wrong = {
        k: result.counts.get(k) for k, v in EXPECTED_COUNTS.items() if result.counts.get(k) != v
    }
    assert not wrong, f"unexpected counts for: {sorted(wrong)}"

    extract = tmp_path / "extract"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract)
    tops = [p for p in extract.iterdir() if p.is_dir()]
    assert len(tops) == 1, f"top-level folders: {len(tops)}"
    root = tops[0]
    files = sorted(p for p in extract.rglob("*") if p.is_file())

    # token absent from the zip bytes and from every extracted file
    needle = creds.token.encode()
    leaked = [p.name for p in [zip_path, *files] if needle in p.read_bytes()]
    assert not leaked, f"token found in: {leaked}"

    receipts = [p for p in files if p.relative_to(root).parts[0] == "receipts"]
    invoices = [p for p in files if p.relative_to(root).parts[0] == "invoices"]
    assert len(receipts) == 1, f"receipt files: {len(receipts)}"
    assert len(invoices) == 1, f"invoice files: {len(invoices)}"

    expenses = json.loads((root / "data/json/expenses.json").read_text(encoding="utf-8"))
    expected_size = expenses[0]["receipt"]["file_size"]
    size_ok = receipts[0].stat().st_size == expected_size
    assert size_ok, "receipt size differs from receipt.file_size"
    is_pdf = invoices[0].read_bytes().startswith(b"%PDF")
    assert is_pdf, "invoice file does not start with %PDF"

    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    entries = manifest["files"]
    assert entries, "manifest lists no files"
    bad = []
    for entry in entries:
        path = root / entry["path"]
        ok = (
            path.is_file()
            and path.stat().st_size == entry["bytes"]
            and hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        )
        if not ok:
            bad.append(entry["path"])
    assert not bad, f"manifest mismatch: {bad}"
