"""End-to-end exporter tests on fixtures. respx mocks only; the token is a fake value."""

import copy
import json
import logging
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import exit_archive.exporter as exporter_module
from exit_archive.errors import AuthError, ExitArchiveError, ExportError, NotFoundError
from exit_archive.exporter import ExportResult, export_account
from exit_archive.harvest.http import HarvestClient
from exit_archive.harvest.resources import Company, iter_all, iter_invoice_payments
from exit_archive.secrets import HarvestToken

SECRET = "fake-token-9f8e7d6c5b4a-SECRET"  # noqa: S105 (fake test value)
API = "https://api.harvestapp.com"
ID = "https://id.getharvest.com"
BASE_URI = "https://fake-co.harvestapp.com"
FIXTURES = Path(__file__).parent / "fixtures" / "harvest"
RESOURCES = [
    "clients", "contacts", "projects", "tasks", "users",
    "user_assignments", "task_assignments", "time_entries",
    "expense_categories", "expenses", "invoice_item_categories", "invoices",
    "estimate_item_categories", "estimates",
]  # fmt: skip
PDF = b"%PDF-1.4 fake"
RECEIPT_URL = "https://fake-co.harvestapp.com/expenses/1/receipt"


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def mock_harvest(*, accounts: int = 1) -> dict[str, respx.Route]:
    """Mock every endpoint the exporter touches; returns the routes by name."""
    routes: dict[str, respx.Route] = {}
    listing = [
        {"id": 42 + i, "name": f"Fake Co {i}", "product": "harvest"} for i in range(accounts)
    ]
    routes["accounts"] = respx.get(f"{ID}/api/v2/accounts").respond(
        200, json={"user": {"id": 1}, "accounts": listing}
    )
    routes["company"] = respx.get(f"{API}/v2/company").respond(200, json=fixture("company"))
    for name in RESOURCES:
        data = fixture(name)
        if name == "expenses":
            data = copy.deepcopy(data)
            data["expenses"][0]["receipt"] = {"url": RECEIPT_URL, "file_name": "lunch.png"}
        if name == "estimates":
            data = copy.deepcopy(data)
            for i, est in enumerate(data["estimates"]):
                est["number"] = f"E{i}"
                est["client_key"] = f"estkey{i}"
        routes[name] = respx.get(f"{API}/v2/{name}").respond(200, json=data)
    for iid in (101, 102):
        routes[f"pay{iid}"] = respx.get(f"{API}/v2/invoices/{iid}/payments").respond(
            200, json=fixture(f"invoice_payments_{iid}")
        )
    routes["receipt"] = respx.get(RECEIPT_URL).respond(200, content=b"IMGBYTES")
    for key in ("fakekey101", "fakekey102"):
        respx.get(f"{BASE_URI}/client/invoices/{key}.pdf").respond(200, content=PDF)
    for key in ("estkey0", "estkey1"):
        respx.get(f"{BASE_URI}/client/estimates/{key}.pdf").respond(200, content=PDF)
    return routes


def record_count(name: str) -> int:
    return len(fixture(name)[name])


def read_zip(path: Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    with zipfile.ZipFile(path) as zf:
        members = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}
    manifest_name = next(n for n in members if n.endswith("/MANIFEST.json"))
    return members, json.loads(members[manifest_name])


def leftovers(out: Path) -> list[str]:
    return sorted(p.name for p in out.iterdir())


@respx.mock
async def test_export_account_end_to_end(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_harvest()
    caplog.set_level(logging.DEBUG)
    result = await export_account(HarvestToken(SECRET), tmp_path)

    assert isinstance(result, ExportResult)
    assert result.zip_path.parent == tmp_path
    assert leftovers(tmp_path) == [result.zip_path.name]
    members, manifest = read_zip(result.zip_path)
    for name in RESOURCES:
        assert manifest["counts"][name] == record_count(name)
    assert manifest["counts"]["invoice_payments"] == sum(
        len(fixture(f"invoice_payments_{i}")["invoice_payments"]) for i in (101, 102)
    )
    assert result.counts == manifest["counts"]
    assert manifest["account_id"] == 42
    assert manifest["skipped"] == [] and result.skipped == []
    assert result.missing == manifest["missing"] == []
    names = list(members)
    assert any(n.endswith("/README.txt") for n in names)
    assert any(n.endswith("/receipts/1_lunch.png") for n in names)
    assert sum("/invoices/" in n for n in names) == 2
    assert sum("/estimates/" in n for n in names) == 2
    assert all(len(m["sha256"]) == 64 for m in manifest["files"])
    paths = {m["path"] for m in manifest["files"]}
    assert "data/json/clients.json" in paths and "receipts/1_lunch.png" in paths

    raw = result.zip_path.read_bytes()
    assert SECRET.encode() not in raw
    assert all(SECRET.encode() not in blob for blob in members.values())
    out, err = capsys.readouterr()
    assert SECRET not in out + err + caplog.text


@respx.mock
async def test_progress_callback_reports_stages(tmp_path: Path) -> None:
    mock_harvest()
    events: list[tuple[str, int, int | None]] = []
    await export_account(HarvestToken(SECRET), tmp_path, progress=lambda *a: events.append(a))
    stages = [e[0] for e in events]
    assert stages[0] == "accounts" and stages[-1] == "archive"
    assert {"accounts", "company", "records", "files", "archive"} <= set(stages)
    records = [e for e in events if e[0] == "records"]
    assert (
        records[-1][1] == sum(record_count(n) for n in RESOURCES) + 2
    )  # two fixture payments in total
    files = [e for e in events if e[0] == "files"]
    assert files[-1][1] == files[-1][2]


@respx.mock
async def test_disabled_features_are_skipped_and_recorded(tmp_path: Path) -> None:
    routes = mock_harvest()
    company = fixture("company") | {"estimate_feature": False, "expense_feature": False}
    routes["company"].respond(200, json=company)
    result = await export_account(HarvestToken(SECRET), tmp_path)
    assert "estimates" in result.skipped and "expenses" in result.skipped
    assert "estimates" not in result.counts
    assert routes["estimates"].call_count == 0


@respx.mock
async def test_account_id_selects_among_several(tmp_path: Path) -> None:
    routes = mock_harvest(accounts=2)
    with pytest.raises(ExportError, match="Several Harvest accounts"):
        await export_account(HarvestToken(SECRET), tmp_path)
    assert routes["company"].call_count == 0
    result = await export_account(HarvestToken(SECRET), tmp_path, account_id=43)
    _, manifest = read_zip(result.zip_path)
    assert manifest["account_id"] == 43
    assert routes["company"].calls[0].request.headers["Harvest-Account-Id"] == "43"


@respx.mock
async def test_401_on_discovery_aborts_without_token_in_message(tmp_path: Path) -> None:
    mock_harvest()
    respx.get(f"{ID}/api/v2/accounts").respond(401)
    with pytest.raises(AuthError) as exc:
        await export_account(HarvestToken(SECRET), tmp_path)
    assert SECRET not in str(exc.value)
    assert leftovers(tmp_path) == []


@respx.mock
async def test_payments_404_for_one_invoice_is_recorded_and_export_continues(
    tmp_path: Path,
) -> None:
    routes = mock_harvest()
    routes["pay101"].respond(404)
    result = await export_account(HarvestToken(SECRET), tmp_path)
    expected = {"type": "invoice_payments", "id": 101, "reason": "not_found"}
    assert result.missing == [expected]
    _, manifest = read_zip(result.zip_path)
    assert manifest["missing"] == [expected]
    assert manifest["counts"]["invoice_payments"] == 1  # invoice 102's payment
    assert routes["pay102"].call_count == 1
    assert routes["estimates"].call_count == 1  # later resources still fetched
    assert manifest["counts"]["estimates"] == record_count("estimates")


@respx.mock
async def test_payments_403_is_recorded_as_forbidden(tmp_path: Path) -> None:
    routes = mock_harvest()
    routes["pay102"].respond(403)
    result = await export_account(HarvestToken(SECRET), tmp_path)
    assert result.missing == [{"type": "invoice_payments", "id": 102, "reason": "forbidden"}]


@respx.mock
async def test_file_failures_land_in_missing(tmp_path: Path) -> None:
    routes = mock_harvest()
    routes["receipt"].respond(404)
    result = await export_account(HarvestToken(SECRET), tmp_path)
    assert {"type": "receipt", "id": 1, "reason": "not_found"} in result.missing


@respx.mock
async def test_401_mid_export_aborts_and_cleans_up(tmp_path: Path) -> None:
    routes = mock_harvest()
    routes["pay102"].respond(401)
    with pytest.raises(AuthError):
        await export_account(HarvestToken(SECRET), tmp_path)
    assert leftovers(tmp_path) == []


@respx.mock
@pytest.mark.parametrize("failure", ["auth", "boom"])
async def test_mid_stream_failure_leaves_nothing(tmp_path: Path, failure: str) -> None:
    routes = mock_harvest()
    if failure == "auth":
        routes["time_entries"].respond(401)
    else:
        routes["time_entries"].mock(side_effect=RuntimeError("boom"))
    with pytest.raises((AuthError, RuntimeError)):
        await export_account(HarvestToken(SECRET), tmp_path)
    assert leftovers(tmp_path) == []


@respx.mock
async def test_download_stage_auth_failure_leaves_nothing(tmp_path: Path) -> None:
    routes = mock_harvest()
    routes["receipt"].respond(401)
    with pytest.raises(AuthError):
        await export_account(HarvestToken(SECRET), tmp_path)
    assert leftovers(tmp_path) == []


@respx.mock
async def test_zip_oserror_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_harvest()

    def broken(self: zipfile.ZipFile, *args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(zipfile.ZipFile, "write", broken)
    with pytest.raises(OSError, match="disk full"):
        await export_account(HarvestToken(SECRET), tmp_path)
    assert leftovers(tmp_path) == []


@respx.mock
async def test_out_dir_is_created_and_client_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_harvest()
    closed: list[bool] = []
    original = HarvestClient.__aexit__

    async def spy(self: HarvestClient, *args: Any) -> None:
        closed.append(True)
        await original(self, *args)

    monkeypatch.setattr(HarvestClient, "__aexit__", spy)
    out = tmp_path / "a" / "b"
    result = await export_account(HarvestToken(SECRET), out)
    assert result.zip_path.parent == out
    assert len(closed) == 2  # discovery client + export client
    routes = mock_harvest()
    routes["company"].respond(400)
    with pytest.raises(ExitArchiveError):
        await export_account(HarvestToken(SECRET), out)
    assert len(closed) == 4


@respx.mock
async def test_slim_projections_do_not_keep_full_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_harvest()
    seen: dict[str, Any] = {}
    real = exporter_module.download_files

    async def spy(client: Any, work_dir: Path, **kwargs: Any) -> Any:
        for key in ("expenses", "invoices", "estimates"):
            seen[key] = list(kwargs[key])
        return await real(client, work_dir, **kwargs)

    monkeypatch.setattr(exporter_module, "download_files", spy)
    await export_account(HarvestToken(SECRET), tmp_path)
    assert seen["expenses"] == [
        {"id": 1, "receipt": {"url": RECEIPT_URL, "file_name": "lunch.png"}}
    ]
    assert seen["invoices"] == [
        {"id": 101, "number": "1001", "client_key": "fakekey101"},
        {"id": 102, "number": "1002", "client_key": "fakekey102"},
    ]
    assert set(seen["estimates"][0]) == {"id", "number", "client_key"}


# ---- T1.6.1a: invoice payments missing[] and id validation -------------------------------


def make_client() -> HarvestClient:
    async def no_sleep(_: float) -> None:
        return None

    return HarvestClient(HarvestToken(SECRET), 42, sleep=no_sleep)


def company() -> Company:
    return Company(base_uri=BASE_URI, name="Fake Co", invoice_feature=True,
                   expense_feature=True, estimate_feature=True)  # fmt: skip


@respx.mock
@pytest.mark.parametrize("bad", [0, -5])
async def test_invoice_id_below_one_raises_before_any_request(bad: int) -> None:
    route = respx.route(url__regex=r".*/payments").respond(200, json={})
    async with make_client() as client:
        with pytest.raises(ExportError):
            async for _ in iter_invoice_payments(client, [bad]):
                pass
        with pytest.raises(ExportError):
            async for _ in iter_invoice_payments(client, [101, bad]):
                pass
    assert route.call_count == 0


@respx.mock
async def test_iter_all_rejects_invoice_with_bad_id_without_payment_request() -> None:
    for name in RESOURCES:
        data = fixture(name)
        if name == "invoices":
            data = {**data, "invoices": [{"id": 0, "number": "x", "client_key": "k"}]}
        respx.get(f"{API}/v2/{name}").respond(200, json=data)
    pay = respx.route(url__regex=r".*/payments").respond(200, json={})
    async with make_client() as client:
        with pytest.raises(ExportError):
            async for _ in iter_all(client, company()):
                pass
    assert pay.call_count == 0


@respx.mock
@pytest.mark.parametrize(("status", "reason"), [(404, "not_found"), (403, "forbidden")])
async def test_iter_invoice_payments_reports_missing_via_callback(status: int, reason: str) -> None:
    respx.get(f"{API}/v2/invoices/1/payments").respond(status)
    respx.get(f"{API}/v2/invoices/2/payments").respond(200, json=fixture("invoice_payments_101"))
    missing: list[dict[str, Any]] = []
    async with make_client() as client:
        got = [p async for p in iter_invoice_payments(client, [1, 2], on_missing=missing.append)]
    assert missing == [{"type": "invoice_payments", "id": 1, "reason": reason}]
    assert [p["invoice_id"] for p in got] == [2]


@respx.mock
@pytest.mark.parametrize("status", [401, 429])
async def test_fatal_payment_errors_still_abort(status: int) -> None:
    respx.get(f"{API}/v2/invoices/1/payments").respond(status, headers={"Retry-After": "0"})
    async with make_client() as client:
        with pytest.raises(Exception) as exc:  # noqa: PT011
            async for _ in iter_invoice_payments(client, [1], on_missing=lambda m: None):
                pass
    assert type(exc.value).__name__ in {"AuthError", "RateLimitedError"}


@respx.mock
async def test_without_callback_not_found_still_raises() -> None:
    respx.get(f"{API}/v2/invoices/1/payments").respond(404)
    async with make_client() as client:
        with pytest.raises(NotFoundError):
            async for _ in iter_invoice_payments(client, [1]):
                pass


@respx.mock
async def test_503_payments_abort_with_callback() -> None:
    from exit_archive.errors import HarvestUnavailableError

    respx.get(f"{API}/v2/invoices/1/payments").mock(return_value=httpx.Response(503))
    async with make_client() as client:
        with pytest.raises(HarvestUnavailableError):
            async for _ in iter_invoice_payments(client, [1], on_missing=lambda m: None):
                pass
