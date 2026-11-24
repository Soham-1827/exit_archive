"""Receipt/PDF download policy tests. respx mocks only."""

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from exit_archive.errors import AuthError
from exit_archive.harvest.files import FilesResult, download_files
from exit_archive.harvest.http import HarvestClient
from exit_archive.secrets import HarvestToken

SECRET = "fake-token-1234567890-SECRET"  # noqa: S105 (fake test value)
BASE_URI = "https://acme.harvestapp.com"


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_client() -> HarvestClient:
    ft = FakeTime()
    return HarvestClient(HarvestToken(SECRET), 42, clock=ft.clock, sleep=ft.sleep)


def expense(eid: int, url: str | None, name: str = "r.png") -> dict[str, Any]:
    receipt = None if url is None else {"url": url, "file_name": name}
    return {"id": eid, "receipt": receipt}


def doc(did: int, number: str = "INV-1", key: str | None = "abc123") -> dict[str, Any]:
    return {"id": did, "number": number, "client_key": key}


async def run(
    work: Path,
    *,
    expenses: list[dict[str, Any]] | None = None,
    invoices: list[dict[str, Any]] | None = None,
    estimates: list[dict[str, Any]] | None = None,
    base_uri: str | None = BASE_URI,
) -> FilesResult:
    async with make_client() as client:
        return await download_files(
            client,
            work,
            expenses=expenses or [],
            invoices=invoices or [],
            estimates=estimates or [],
            base_uri=base_uri,
        )


@respx.mock
async def test_receipt_on_harvest_host_gets_token(tmp_path: Path) -> None:
    route = respx.get("https://x.harvestapp.com/r/1").respond(200, content=b"IMG")
    res = await run(tmp_path, expenses=[expense(7, "https://x.harvestapp.com/r/1", "pic.png")])
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {SECRET}"
    assert res.files == ["receipts/7_pic.png"]
    assert (tmp_path / "receipts" / "7_pic.png").read_bytes() == b"IMG"
    assert res.missing == []


@respx.mock
async def test_receipt_on_foreign_host_not_requested(tmp_path: Path) -> None:
    route = respx.get("https://evil.example.com/r").respond(200, content=b"IMG")
    res = await run(tmp_path, expenses=[expense(1, "https://evil.example.com/r")])
    assert route.call_count == 0
    assert res.files == []
    assert res.missing == [{"type": "receipt", "id": 1, "reason": "host_not_allowed"}]


@respx.mock
async def test_receipt_redirect_to_s3_has_no_authorization(tmp_path: Path) -> None:
    respx.get("https://x.harvestapp.com/r/1").respond(
        302, headers={"Location": "https://s3.amazonaws.com/b/k?sig=1"}
    )
    s3 = respx.get("https://s3.amazonaws.com/b/k").respond(200, content=b"IMG")
    res = await run(tmp_path, expenses=[expense(1, "https://x.harvestapp.com/r/1")])
    assert "Authorization" not in s3.calls[0].request.headers
    assert res.files == ["receipts/1_r.png"]


@respx.mock
async def test_expense_without_receipt_is_skipped(tmp_path: Path) -> None:
    res = await run(tmp_path, expenses=[expense(1, None), {"id": 2}, {"id": 3, "receipt": {}}])
    assert res == FilesResult(files=[], missing=[])


@respx.mock
async def test_404_on_one_of_three_receipts(tmp_path: Path) -> None:
    respx.get("https://x.harvestapp.com/r/1").respond(200, content=b"1")
    respx.get("https://x.harvestapp.com/r/2").respond(404)
    respx.get("https://x.harvestapp.com/r/3").respond(200, content=b"3")
    res = await run(
        tmp_path, expenses=[expense(i, f"https://x.harvestapp.com/r/{i}") for i in (1, 2, 3)]
    )
    assert res.files == ["receipts/1_r.png", "receipts/3_r.png"]
    assert res.missing == [{"type": "receipt", "id": 2, "reason": "not_found"}]
    assert sorted(p.name for p in (tmp_path / "receipts").iterdir()) == ["1_r.png", "3_r.png"]


@respx.mock
@pytest.mark.parametrize(
    ("status", "reason"), [(403, "forbidden"), (404, "not_found"), (500, "unavailable")]
)
async def test_receipt_failure_reasons(tmp_path: Path, status: int, reason: str) -> None:
    respx.get("https://x.harvestapp.com/r").respond(status)
    res = await run(tmp_path, expenses=[expense(1, "https://x.harvestapp.com/r")])
    assert res.missing == [{"type": "receipt", "id": 1, "reason": reason}]


@respx.mock
async def test_receipt_unexpected_status_and_429_exhausted(tmp_path: Path) -> None:
    respx.get("https://x.harvestapp.com/a").respond(418)
    respx.get("https://x.harvestapp.com/b").respond(429, headers={"Retry-After": "0"})
    res = await run(
        tmp_path,
        expenses=[
            expense(1, "https://x.harvestapp.com/a"),
            expense(2, "https://x.harvestapp.com/b"),
        ],
    )
    assert res.missing == [
        {"type": "receipt", "id": 1, "reason": "download_failed"},
        {"type": "receipt", "id": 2, "reason": "unavailable"},
    ]


@respx.mock
async def test_auth_error_on_receipt_aborts(tmp_path: Path) -> None:
    respx.get("https://x.harvestapp.com/r/1").respond(401)
    respx.get("https://x.harvestapp.com/r/2").respond(200, content=b"2")
    with pytest.raises(AuthError) as ei:
        await run(
            tmp_path, expenses=[expense(i, f"https://x.harvestapp.com/r/{i}") for i in (1, 2)]
        )
    assert SECRET not in str(ei.value)


@respx.mock
async def test_receipt_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("exit_archive.harvest.files.MAX_FILE_BYTES", 5)
    respx.get("https://x.harvestapp.com/r").respond(200, content=b"x" * 50)
    res = await run(tmp_path, expenses=[expense(1, "https://x.harvestapp.com/r")])
    assert res.missing == [{"type": "receipt", "id": 1, "reason": "too_large"}]
    assert list((tmp_path / "receipts").iterdir()) == []


@respx.mock
@pytest.mark.parametrize("name", ["../../x", "a/b\\c.png", "..", "", "/etc/passwd", "a\x00b.png"])
async def test_receipt_file_names_are_made_safe(tmp_path: Path, name: str) -> None:
    work = tmp_path / "work"
    work.mkdir()
    respx.get("https://x.harvestapp.com/r").respond(200, content=b"x")
    res = await run(work, expenses=[expense(5, "https://x.harvestapp.com/r", name)])
    assert len(res.files) == 1
    written = (work / res.files[0]).resolve()
    assert written.is_relative_to(work.resolve())
    assert written.parent == (work / "receipts").resolve()
    assert written.is_file()
    assert [p for p in tmp_path.iterdir()] == [work]


@respx.mock
async def test_pdfs_carry_no_authorization(tmp_path: Path) -> None:
    inv = respx.get(f"{BASE_URI}/client/invoices/abc123.pdf").respond(200, content=b"%PDF-1.4 x")
    est = respx.get(f"{BASE_URI}/client/estimates/k2.pdf").respond(200, content=b"%PDF-1.7 y")
    res = await run(tmp_path, invoices=[doc(9, "INV 1/2")], estimates=[doc(4, "EST-1", "k2")])
    for route in (inv, est):
        h = route.calls[0].request.headers
        assert "Authorization" not in h
        assert "Harvest-Account-Id" not in h
    assert res.files == ["invoices/INV_1_2_9.pdf", "estimates/EST-1_4.pdf"]
    assert (tmp_path / "invoices" / "INV_1_2_9.pdf").read_bytes().startswith(b"%PDF")
    assert res.missing == []


@respx.mock
async def test_html_instead_of_pdf_is_not_a_pdf(tmp_path: Path) -> None:
    respx.get(f"{BASE_URI}/client/invoices/abc123.pdf").respond(200, html="<html>login</html>")
    res = await run(tmp_path, invoices=[doc(1)])
    assert res.files == []
    assert res.missing == [{"type": "invoice_pdf", "id": 1, "reason": "not_a_pdf"}]
    assert list((tmp_path / "invoices").iterdir()) == []


@respx.mock
async def test_missing_client_key(tmp_path: Path) -> None:
    res = await run(
        tmp_path, invoices=[doc(1, key=None), doc(2, key="")], estimates=[doc(3, key=None)]
    )
    assert res.missing == [
        {"type": "invoice_pdf", "id": 1, "reason": "no_client_key"},
        {"type": "invoice_pdf", "id": 2, "reason": "no_client_key"},
        {"type": "estimate_pdf", "id": 3, "reason": "no_client_key"},
    ]
    assert len(respx.calls) == 0


@respx.mock
@pytest.mark.parametrize(
    "bad",
    [
        "http://acme.harvestapp.com",
        "https://evil.example.com",
        "https://harvestapp.com.evil.com",
        "https://x@acme.harvestapp.com",
        "javascript:alert(1)",
        "",
        None,
        "https://acme.harvestapp.com:8443",
        "https://acme.harvestapp.com\\@evil.com",
    ],
)
async def test_bad_base_uri_makes_no_requests(tmp_path: Path, bad: str | None) -> None:
    res = await run(tmp_path, invoices=[doc(1), doc(2)], estimates=[doc(3)], base_uri=bad)
    assert len(respx.calls) == 0
    assert res.files == []
    assert res.missing == [
        {"type": "invoice_pdf", "id": 1, "reason": "base_uri_not_allowed"},
        {"type": "invoice_pdf", "id": 2, "reason": "base_uri_not_allowed"},
        {"type": "estimate_pdf", "id": 3, "reason": "base_uri_not_allowed"},
    ]


@respx.mock
@pytest.mark.parametrize("good", ["https://acme.harvestapp.com", "https://acme.harvestapp.com/"])
async def test_good_base_uri_works(tmp_path: Path, good: str) -> None:
    respx.get(f"{BASE_URI}/client/invoices/abc123.pdf").respond(200, content=b"%PDF-")
    res = await run(tmp_path, invoices=[doc(1)], base_uri=good)
    assert res.files == ["invoices/INV-1_1.pdf"]


@respx.mock
async def test_client_key_is_url_quoted(tmp_path: Path) -> None:
    route = respx.get(url__regex=r"https://acme\.harvestapp\.com/client/invoices/.*").respond(
        200, content=b"%PDF-"
    )
    await run(tmp_path, invoices=[doc(1, key="a/../b?x=1")])
    assert route.calls[0].request.url.raw_path == b"/client/invoices/a%2F..%2Fb%3Fx%3D1.pdf"


@respx.mock
async def test_pdf_401_is_forbidden_not_abort(tmp_path: Path) -> None:
    respx.get(f"{BASE_URI}/client/invoices/abc123.pdf").respond(401)
    res = await run(tmp_path, invoices=[doc(1)])
    assert res.missing == [{"type": "invoice_pdf", "id": 1, "reason": "forbidden"}]


@respx.mock
async def test_concurrency_limited_to_four(tmp_path: Path) -> None:
    state = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return httpx.Response(200, content=b"x")

    respx.get(url__regex=r"https://x\.harvestapp\.com/r/\d+").mock(side_effect=handler)
    res = await run(
        tmp_path, expenses=[expense(i, f"https://x.harvestapp.com/r/{i}") for i in range(1, 13)]
    )
    assert len(res.files) == 12
    assert state["peak"] == 4


@respx.mock
async def test_token_not_in_missing_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    respx.get("https://x.harvestapp.com/r").respond(404)
    res = await run(tmp_path, expenses=[expense(1, "https://x.harvestapp.com/r")])
    assert SECRET not in repr(res) + caplog.text
