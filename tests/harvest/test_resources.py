"""Registry and fetcher tests. respx mocks and hand-written fixtures only."""

import copy
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import respx

from exit_archive.errors import ExportError, NotFoundError
from exit_archive.harvest.http import HarvestClient
from exit_archive.harvest.resources import (
    INVOICE_PAYMENTS,
    REGISTRY,
    Company,
    Resource,
    fetch_company,
    iter_all,
    iter_invoice_payments,
    iter_resource,
    skipped_resources,
)
from exit_archive.secrets import HarvestToken

BASE = "https://api.harvestapp.com"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "harvest"
EXPECTED = [
    "clients", "contacts", "projects", "tasks", "users",
    "user_assignments", "task_assignments", "time_entries",
    "expense_categories", "expenses", "invoice_item_categories", "invoices",
    "estimate_item_categories", "estimates",
]  # fmt: skip


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def make_client() -> HarvestClient:
    async def no_sleep(_: float) -> None:
        return None

    return HarvestClient(HarvestToken("fake-token-0000"), 42, sleep=no_sleep)


def company(**flags: bool) -> Company:
    values = {"invoice_feature": True, "expense_feature": True, "estimate_feature": True}
    values.update(flags)
    return Company(base_uri="https://fake-co.harvestapp.com", name="Fake Co", **values)


def mock_all() -> None:
    for name in EXPECTED:
        respx.get(f"{BASE}/v2/{name}").respond(200, json=fixture(name))
    for iid in (101, 102):
        respx.get(f"{BASE}/v2/invoices/{iid}/payments").respond(
            200, json=fixture(f"invoice_payments_{iid}")
        )


async def collect(it: Any) -> list[Any]:
    return [x async for x in it]


def test_registry_has_exactly_the_14_resources() -> None:
    assert [r.name for r in REGISTRY] == EXPECTED
    assert "invoice_payments" not in {r.name for r in REGISTRY}
    assert INVOICE_PAYMENTS.key == "invoice_payments"


def test_registry_entries_are_frozen_with_paths_and_flags() -> None:
    flags = {r.name: r.feature for r in REGISTRY if r.feature}
    assert flags == {
        "invoices": "invoice_feature",
        "invoice_item_categories": "invoice_feature",
        "estimates": "estimate_feature",
        "estimate_item_categories": "estimate_feature",
        "expenses": "expense_feature",
        "expense_categories": "expense_feature",
    }
    assert all(r.path == f"/v2/{r.name}" and r.key == r.name for r in REGISTRY)
    with pytest.raises(AttributeError):
        REGISTRY[0].name = "x"  # type: ignore[misc]


@respx.mock
async def test_fetch_company() -> None:
    respx.get(f"{BASE}/v2/company").respond(200, json=fixture("company"))
    async with make_client() as client:
        result = await fetch_company(client)
    assert result == company()
    with pytest.raises(AttributeError):
        result.name = "x"  # type: ignore[misc]


@respx.mock
@pytest.mark.parametrize("bad", [{"name": "x"}, {"base_uri": 1, "name": "x"}])
async def test_fetch_company_rejects_bad_shape(bad: dict[str, Any]) -> None:
    respx.get(f"{BASE}/v2/company").respond(200, json=bad)
    async with make_client() as client:
        with pytest.raises(ExportError):
            await fetch_company(client)


@respx.mock
async def test_fetch_company_rejects_non_bool_flag() -> None:
    body = {**fixture("company"), "invoice_feature": "yes"}
    respx.get(f"{BASE}/v2/company").respond(200, json=body)
    async with make_client() as client:
        with pytest.raises(ExportError):
            await fetch_company(client)


@respx.mock
@pytest.mark.parametrize("resource", REGISTRY, ids=lambda r: r.name)
async def test_every_resource_is_fetched_from_fixture(resource: Resource) -> None:
    route = respx.get(f"{BASE}{resource.path}").respond(200, json=fixture(resource.name))
    async with make_client() as client:
        records = await collect(iter_resource(client, resource))
    assert route.call_count == 1
    assert [r["id"] for r in records] == [r["id"] for r in fixture(resource.name)[resource.key]]


@respx.mock
async def test_iter_resource_follows_pagination() -> None:
    page1 = {"clients": [{"id": 1}], "links": {"next": f"{BASE}/v2/clients?page=2"}}
    page2 = {"clients": [{"id": 2}], "links": {"next": None}}
    respx.get(f"{BASE}/v2/clients", params={"page": "2"}).respond(200, json=page2)
    respx.get(f"{BASE}/v2/clients").respond(200, json=page1)
    async with make_client() as client:
        got = await collect(iter_resource(client, REGISTRY[0]))
    assert [r["id"] for r in got] == [1, 2]


def test_skipped_resources_by_flag() -> None:
    assert skipped_resources(company()) == []
    assert skipped_resources(company(invoice_feature=False)) == [
        "invoice_item_categories", "invoices", "invoice_payments",
    ]  # fmt: skip
    assert set(skipped_resources(company(estimate_feature=False, expense_feature=False))) == {
        "estimate_item_categories", "estimates", "expense_categories", "expenses",
    }  # fmt: skip


@respx.mock
async def test_iter_all_yields_every_resource_and_payments() -> None:
    mock_all()
    async with make_client() as client:
        pairs = await collect(iter_all(client, company()))
    names = [n for n, _ in pairs]
    assert set(names) == set(EXPECTED) | {"invoice_payments"}
    assert names.count("invoice_payments") == 2


@respx.mock
async def test_invoice_feature_off_makes_no_invoice_requests() -> None:
    mock_all()
    async with make_client() as client:
        pairs = await collect(iter_all(client, company(invoice_feature=False)))
    paths = {c.request.url.path for c in respx.calls}
    assert "/v2/invoices" not in paths
    assert "/v2/invoice_item_categories" not in paths
    assert not any("/payments" in p for p in paths)
    assert {"invoices", "invoice_item_categories", "invoice_payments"}.isdisjoint(
        n for n, _ in pairs
    )
    assert "invoices" in skipped_resources(company(invoice_feature=False))
    assert "/v2/clients" in paths


@respx.mock
async def test_expense_and_estimate_flags_skip_requests() -> None:
    mock_all()
    async with make_client() as client:
        await collect(iter_all(client, company(expense_feature=False, estimate_feature=False)))
    paths = {c.request.url.path for c in respx.calls}
    for p in ("expenses", "expense_categories", "estimates", "estimate_item_categories"):
        assert f"/v2/{p}" not in paths
    assert "/v2/invoices" in paths


@respx.mock
async def test_payments_two_invoices_two_requests_with_invoice_id() -> None:
    mock_all()
    invoices = fixture("invoices")["invoices"]
    original = copy.deepcopy(invoices)
    async with make_client() as client:
        payments = await collect(iter_invoice_payments(client, [i["id"] for i in invoices]))
    pay_calls = [c for c in respx.calls if c.request.url.path.endswith("/payments")]
    assert len(pay_calls) == 2
    assert {(p["id"], p["invoice_id"]) for p in payments} == {(5001, 101), (5002, 102)}
    assert invoices == original


@respx.mock
async def test_payments_are_new_dicts_not_the_fetched_ones() -> None:
    fetched: list[dict[str, Any]] = []

    async def spy(client: HarvestClient, ids: Iterable[int]) -> None:
        async for p in iter_invoice_payments(client, ids):
            fetched.append(p)

    body = fixture("invoice_payments_101")
    respx.get(f"{BASE}/v2/invoices/101/payments").respond(200, json=body)
    async with make_client() as client:
        await spy(client, [101])
    assert fetched[0]["invoice_id"] == 101
    assert "invoice_id" not in body["invoice_payments"][0]


@respx.mock
async def test_invoice_payments_404_propagates() -> None:
    respx.get(f"{BASE}/v2/invoices/101/payments").respond(404, json={})
    async with make_client() as client:
        with pytest.raises(NotFoundError):
            await collect(iter_invoice_payments(client, [101]))


@respx.mock
async def test_non_dict_payment_record_is_an_error() -> None:
    respx.get(f"{BASE}/v2/invoices/101/payments").respond(
        200, json={"invoice_payments": [5], "links": {"next": None}}
    )
    async with make_client() as client:
        with pytest.raises(ExportError):
            await collect(iter_invoice_payments(client, [101]))


@respx.mock
async def test_iter_all_rejects_invoice_without_int_id() -> None:
    body = {"invoices": [{"id": "../x"}], "links": {"next": None}}
    respx.get(f"{BASE}/v2/invoices").respond(200, json=body)
    for r in REGISTRY:
        if r.name != "invoices":
            respx.get(f"{BASE}{r.path}").respond(200, json={r.key: [], "links": {"next": None}})
    async with make_client() as client:
        with pytest.raises(ExportError):
            await collect(iter_all(client, company()))
    assert not any("/payments" in c.request.url.path for c in respx.calls)


@respx.mock
async def test_iter_all_streams_lazily() -> None:
    mock_all()
    async with make_client() as client:
        it = iter_all(client, company())
        first = await anext(it)
        assert first[0] == "clients"
        assert len(respx.calls) == 1  # nothing beyond the first page fetched yet
        await it.aclose()
