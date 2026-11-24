"""Adversarial probes for resources.py (tester-added). respx + fixtures only."""

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from exit_archive.errors import ExportError, NotFoundError
from exit_archive.harvest.http import HarvestClient
from exit_archive.harvest.resources import (
    REGISTRY,
    Company,
    fetch_company,
    iter_all,
    iter_invoice_payments,
)
from exit_archive.secrets import HarvestToken

BASE = "https://api.harvestapp.com"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "harvest"
SECRET = "fake-token-adversarial-9999"  # noqa: S105


def make_client() -> HarvestClient:
    async def no_sleep(_: float) -> None:
        return None

    return HarvestClient(HarvestToken(SECRET), 42, sleep=no_sleep)


def all_on() -> Company:
    return Company("https://x.harvestapp.com", "Acme", True, True, True)


@respx.mock
@pytest.mark.parametrize("bad", ["1", "1/../../company", 1.5, True, False, None, [1], {"a": 1}])
async def test_non_int_invoice_ids_rejected_before_any_request(bad: Any) -> None:
    route = respx.route().mock(return_value=httpx.Response(200, json={"invoice_payments": []}))
    async with make_client() as c:
        with pytest.raises(ExportError):
            async for _ in iter_invoice_payments(c, [bad]):
                pass
    assert route.call_count == 0


@respx.mock
@pytest.mark.parametrize("good", [0, 7, -5, 10**30])
async def test_int_ids_only_hit_invoice_payments_pattern(good: int) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"invoice_payments": [{"id": 1}], "links": {"next": None}})

    respx.route().mock(side_effect=handler)
    async with make_client() as c:
        out = [p async for p in iter_invoice_payments(c, [good])]
    assert out == [{"id": 1, "invoice_id": good}]
    assert all(re.fullmatch(r"/v2/invoices/-?\d+/payments", p) for p in seen)


@respx.mock
async def test_iter_all_invoice_with_bad_id_types_never_requests_odd_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v2/invoices":
            return httpx.Response(
                200, json={"invoices": [{"id": "9/../x"}], "links": {"next": None}}
            )
        return httpx.Response(200, json={"links": {"next": None}, **{r.key: [] for r in REGISTRY}})

    respx.route().mock(side_effect=handler)
    async with make_client() as c:
        with pytest.raises(ExportError):
            async for _ in iter_all(c, all_on()):
                pass
    assert not any("payments" in p for p in seen)


@respx.mock
@pytest.mark.parametrize("base_uri", ["http://evil.example.com", "javascript:alert(1)", ""])
async def test_base_uri_not_validated_by_fetch_company(base_uri: str) -> None:
    body = {
        "base_uri": base_uri,
        "name": "A",
        "invoice_feature": True,
        "expense_feature": True,
        "estimate_feature": True,
    }
    respx.get(f"{BASE}/v2/company").mock(return_value=httpx.Response(200, json=body))
    async with make_client() as c:
        company = await fetch_company(c)
    # Known gap: resources.py passes any string through; T1.5 must validate before use.
    assert company.base_uri == base_uri


@respx.mock
async def test_streaming_single_request_for_first_item() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        page = int(request.url.params.get("page", "1"))
        nxt = f"{BASE}/v2/clients?page={page + 1}" if page < 3 else None
        return httpx.Response(
            200, json={"clients": [{"id": page * 10 + i} for i in range(2)], "links": {"next": nxt}}
        )

    respx.route().mock(side_effect=handler)
    async with make_client() as c:
        it = iter_all(c, all_on())
        name, rec = await it.__anext__()
        assert (name, rec) == ("clients", {"id": 10})
        assert len(calls) == 1
        await it.aclose()


@respx.mock
async def test_payments_404_for_one_invoice_aborts_whole_export_without_leak(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/invoices":
            return httpx.Response(
                200, json={"invoices": [{"id": 1}, {"id": 2}], "links": {"next": None}}
            )
        if path == "/v2/invoices/1/payments":
            return httpx.Response(404, text=f"Bearer {SECRET}")
        return httpx.Response(200, json={r.key: [] for r in REGISTRY} | {"links": {"next": None}})

    respx.route().mock(side_effect=handler)
    names: list[str] = []
    async with make_client() as c:
        with pytest.raises(NotFoundError) as exc:
            async for name, _ in iter_all(c, all_on()):
                names.append(name)
    # Finding: one 404 aborts the entire export; invoice 2 is never fetched.
    assert SECRET not in str(exc.value) and SECRET not in caplog.text
    assert names.count("invoices") == 1


def test_fixtures_have_no_personal_data_or_token_like_strings() -> None:
    email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
    phone = re.compile(r"\+?\d[\d\s().-]{8,}\d")
    token_like = re.compile(r"[0-9a-fA-F]{32,}|\d+\.pt\.|Bearer\s+\S+")
    safe_domains = ("example.com", "example.org", "example.net", ".test", ".invalid")
    for f in FIXTURES.glob("*.json"):
        text = f.read_text()
        json.loads(text)
        assert not token_like.search(text), f.name
        for m in email.findall(text):
            assert m.endswith(safe_domains), f"{f.name}: {m}"
        for m in phone.findall(text):
            assert re.fullmatch(r"[\d.\-: T+Z]+", m) is not None and "-" in m, f"{f.name}: {m}"


@respx.mock
async def test_yielded_records_are_independent_of_fixture_and_later_yields() -> None:
    pages = json.loads((FIXTURES / "invoice_payments_101.json").read_text())
    pristine = json.loads(json.dumps(pages))
    respx.route().mock(return_value=httpx.Response(200, json=pages))
    inv_id = 101
    async with make_client() as c:
        first = [r async for r in iter_invoice_payments(c, [inv_id])]
        first.append({"x": 1})
        for rec in first:
            rec["mutated"] = True
    assert pages == pristine
