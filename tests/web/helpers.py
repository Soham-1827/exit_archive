"""Shared helpers for the web tests (not a tester-owned file)."""

import secrets
import zipfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from exit_archive.exporter import ExportResult, Progress
from exit_archive.secrets import HarvestToken
from exit_archive.web.app import create_app
from exit_archive.web.settings import Settings

TOKEN = "tok-SECRET-9f8e7d6c5b4a"  # noqa: S105
ZIP_NAME = "exit-archive-acme-20261125.zip"


def make_job_id() -> str:
    return secrets.token_urlsafe(32)


class RecordingExport:
    """Stub exporter: writes a tiny zip like the real one and records its call arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[HarvestToken, int | None]] = []

    async def __call__(
        self,
        token: HarvestToken,
        out_dir: Path,
        account_id: int | None = None,
        progress: Progress | None = None,
    ) -> ExportResult:
        self.calls.append((token, account_id))
        out_dir.mkdir(parents=True, exist_ok=True)
        zip_path = out_dir / ZIP_NAME
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("MANIFEST.json", "{}")
        if progress:
            progress("files", 3, 3)
        return ExportResult(zip_path=zip_path, counts={}, missing=[], skipped=[])


async def raising_export(
    token: HarvestToken,
    out_dir: Path,
    account_id: int | None = None,
    progress: Progress | None = None,
) -> ExportResult:
    raise RuntimeError(f"boom with {token.reveal()}")


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"data_dir": tmp_path, "payment_required": False}
    values.update(overrides)
    return Settings(**values)


def build(settings: Settings, export: Any = None, payments: Any = None) -> TestClient:
    app = create_app(settings, export=export or RecordingExport(), payments=payments)
    return TestClient(app, raise_server_exceptions=False)


def wait_idle(client: TestClient) -> None:
    client.portal.call(client.app.state.runner.wait_idle)  # type: ignore[union-attr,attr-defined]


def post_job(client: TestClient, **fields: str) -> Any:
    data = {"token": TOKEN, **fields}
    return client.post("/jobs", data=data, follow_redirects=False)
