"""HarvestClient tests. respx mocks only; no network, no real sleeping."""

import logging

import httpx
import pytest
import respx

from exit_archive import __version__
from exit_archive.errors import (
    AuthError,
    ExportError,
    ForbiddenError,
    HarvestUnavailableError,
    NotFoundError,
    RateLimitedError,
)
from exit_archive.harvest.http import HarvestClient, RateLimiter
from exit_archive.secrets import HarvestToken

SECRET = "fake-token-1234567890-SECRET"  # noqa: S105 (fake test value)
BASE = "https://api.harvestapp.com"


class FakeTime:
    """Fake clock whose sleep advances time and records the requested delays."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def ft() -> FakeTime:
    return FakeTime()


def make_client(ft: FakeTime, account_id: int | None = 42) -> HarvestClient:
    return HarvestClient(HarvestToken(SECRET), account_id, clock=ft.clock, sleep=ft.sleep)


def assert_no_secret(exc: BaseException, caplog: pytest.LogCaptureFixture) -> None:
    assert SECRET not in str(exc)
    assert SECRET not in repr(exc)
    assert SECRET not in caplog.text
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


@respx.mock
async def test_headers_on_every_request(ft: FakeTime) -> None:
    route = respx.get(f"{BASE}/v2/clients").respond(200, json={"ok": 1})
    async with make_client(ft) as client:
        await client.get("/v2/clients")
        await client.get("/v2/clients")
    assert route.call_count == 2
    for call in route.calls:
        h = call.request.headers
        assert h["Authorization"] == f"Bearer {SECRET}"
        assert h["Harvest-Account-Id"] == "42"
        assert h["User-Agent"] == f"ExitArchive/{__version__}"
        assert h["User-Agent"].startswith("ExitArchive/")


@respx.mock
async def test_no_account_header_when_account_id_none(ft: FakeTime) -> None:
    route = respx.get(f"{BASE}/v2/x").respond(200, json={})
    async with make_client(ft, account_id=None) as client:
        await client.get("/v2/x")
    assert "Harvest-Account-Id" not in route.calls[0].request.headers


async def test_client_requires_context_manager(ft: FakeTime) -> None:
    with pytest.raises(RuntimeError):
        await make_client(ft).get("/v2/x")


async def test_explicit_timeouts(ft: FakeTime) -> None:
    async with make_client(ft) as client:
        t = client.timeout
        assert t.connect is not None
        assert t.read is not None
        assert t.write is not None
        assert t.pool is not None


@respx.mock
async def test_paginate_three_pages(ft: FakeTime) -> None:
    p2 = f"{BASE}/v2/clients?page=2&per_page=100"
    p3 = f"{BASE}/v2/clients?page=3&per_page=100"
    route = respx.get(f"{BASE}/v2/clients").mock(
        side_effect=[
            httpx.Response(200, json={"clients": [{"id": 1}, {"id": 2}], "links": {"next": p2}}),
            httpx.Response(200, json={"clients": [{"id": 3}], "links": {"next": p3}}),
            httpx.Response(200, json={"clients": [{"id": 4}], "links": {"next": None}}),
        ]
    )
    async with make_client(ft) as client:
        ids = [r["id"] async for r in client.paginate("/v2/clients", "clients", {"per_page": 100})]
    assert ids == [1, 2, 3, 4]
    urls = [str(c.request.url) for c in route.calls]
    assert urls == [f"{BASE}/v2/clients?per_page=100", p2, p3]


@respx.mock
async def test_paginate_without_links_stops(ft: FakeTime) -> None:
    respx.get(f"{BASE}/v2/clients").respond(200, json={"clients": [{"id": 1}]})
    async with make_client(ft) as client:
        got = [r async for r in client.paginate("/v2/clients", "clients")]
    assert got == [{"id": 1}]


@respx.mock
async def test_paginate_missing_key_raises(ft: FakeTime) -> None:
    respx.get(f"{BASE}/v2/clients").respond(200, json={"other": []})
    async with make_client(ft) as client:
        with pytest.raises(ExportError):
            [r async for r in client.paginate("/v2/clients", "clients")]


@respx.mock
async def test_paginate_refuses_next_link_on_other_host(
    ft: FakeTime, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(f"{BASE}/v2/clients").respond(
        200, json={"clients": [], "links": {"next": "https://evil.example.com/v2/clients?page=2"}}
    )
    evil = respx.get("https://evil.example.com/v2/clients").respond(200, json={"clients": []})
    async with make_client(ft) as client:
        with pytest.raises(ExportError) as info:
            [r async for r in client.paginate("/v2/clients", "clients")]
    assert not evil.called
    assert_no_secret(info.value, caplog)


@respx.mock
async def test_429_waits_retry_after_then_succeeds(ft: FakeTime) -> None:
    route = respx.get(f"{BASE}/v2/clients").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    async with make_client(ft) as client:
        assert await client.get("/v2/clients") == {"ok": True}
    assert route.call_count == 2
    assert ft.sleeps == [2]


@respx.mock
async def test_429_retry_after_capped_at_60(ft: FakeTime) -> None:
    respx.get(f"{BASE}/v2/clients").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "900"}),
            httpx.Response(200, json={}),
        ]
    )
    async with make_client(ft) as client:
        await client.get("/v2/clients")
    assert ft.sleeps == [60]


@pytest.mark.parametrize("value", ["soon", "nan", "-3", ""])
@respx.mock
async def test_429_bad_retry_after_uses_default(ft: FakeTime, value: str) -> None:
    respx.get(f"{BASE}/v2/clients").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": value}),
            httpx.Response(200, json={}),
        ]
    )
    async with make_client(ft) as client:
        await client.get("/v2/clients")
    assert len(ft.sleeps) == 1
    assert 0 < ft.sleeps[0] <= 60


@respx.mock
async def test_429_forever_raises_rate_limited_after_5_tries(
    ft: FakeTime, caplog: pytest.LogCaptureFixture
) -> None:
    route = respx.get(f"{BASE}/v2/clients").respond(429, headers={"Retry-After": "1"})
    async with make_client(ft) as client:
        with pytest.raises(RateLimitedError) as info:
            await client.get("/v2/clients")
    assert route.call_count == 5
    assert_no_secret(info.value, caplog)


@respx.mock
async def test_503_twice_then_ok_uses_exponential_backoff(ft: FakeTime) -> None:
    route = respx.get(f"{BASE}/v2/clients").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(502),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    async with make_client(ft) as client:
        assert await client.get("/v2/clients") == {"ok": True}
    assert route.call_count == 3
    assert ft.sleeps == [1, 2]


@respx.mock
async def test_503_forever_raises_unavailable_after_4_tries(
    ft: FakeTime, caplog: pytest.LogCaptureFixture
) -> None:
    route = respx.get(f"{BASE}/v2/clients").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(504),
        ]
    )
    async with make_client(ft) as client:
        with pytest.raises(HarvestUnavailableError) as info:
            await client.get("/v2/clients")
    assert route.call_count == 4
    assert info.value.status_code == 504
    assert_no_secret(info.value, caplog)


@respx.mock
async def test_transport_error_retried_then_unavailable(
    ft: FakeTime, caplog: pytest.LogCaptureFixture
) -> None:
    route = respx.get(f"{BASE}/v2/clients").mock(side_effect=httpx.ConnectError("boom"))
    async with make_client(ft) as client:
        with pytest.raises(HarvestUnavailableError) as info:
            await client.get("/v2/clients")
    assert route.call_count == 4
    assert_no_secret(info.value, caplog)


@respx.mock
async def test_transport_error_then_ok(ft: FakeTime) -> None:
    respx.get(f"{BASE}/v2/clients").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={"ok": 1})]
    )
    async with make_client(ft) as client:
        assert await client.get("/v2/clients") == {"ok": 1}


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthError), (403, ForbiddenError), (404, NotFoundError)],
)
@respx.mock
async def test_status_maps_to_error(
    ft: FakeTime, caplog: pytest.LogCaptureFixture, status: int, error: type[Exception]
) -> None:
    route = respx.get(f"{BASE}/v2/invoices").respond(status, json={"message": SECRET})
    async with make_client(ft) as client:
        with pytest.raises(error) as info:
            await client.get("/v2/invoices")
    assert route.call_count == 1
    assert "/v2/invoices" in str(info.value)
    assert_no_secret(info.value, caplog)


@respx.mock
async def test_resource_name_override(ft: FakeTime) -> None:
    respx.get(f"{BASE}/v2/invoices/9").respond(404)
    async with make_client(ft) as client:
        with pytest.raises(NotFoundError, match="invoice 9"):
            await client.get("/v2/invoices/9", resource="invoice 9")


@respx.mock
async def test_unexpected_status_is_export_error(
    ft: FakeTime, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(f"{BASE}/v2/x").respond(418)
    async with make_client(ft) as client:
        with pytest.raises(ExportError) as info:
            await client.get("/v2/x")
    assert_no_secret(info.value, caplog)


@respx.mock
async def test_non_json_or_non_object_body_is_export_error(ft: FakeTime) -> None:
    respx.get(f"{BASE}/v2/a").respond(200, text="<html>login</html>")
    respx.get(f"{BASE}/v2/b").respond(200, json=[1, 2])
    async with make_client(ft) as client:
        with pytest.raises(ExportError):
            await client.get("/v2/a")
        with pytest.raises(ExportError):
            await client.get("/v2/b")


@respx.mock
async def test_logging_stays_clean(ft: FakeTime, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    respx.get(f"{BASE}/v2/clients").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={})]
    )
    async with make_client(ft) as client:
        await client.get("/v2/clients")
    assert SECRET not in caplog.text


async def test_rate_limiter_delays_101st_call(ft: FakeTime) -> None:
    limiter = RateLimiter(max_calls=100, period=15.0, clock=ft.clock, sleep=ft.sleep)
    for _ in range(100):
        await limiter.acquire()
    assert ft.sleeps == []
    await limiter.acquire()
    assert ft.sleeps == [15.0]


async def test_rate_limiter_rolling_window(ft: FakeTime) -> None:
    limiter = RateLimiter(max_calls=2, period=15.0, clock=ft.clock, sleep=ft.sleep)
    await limiter.acquire()
    ft.now += 10
    await limiter.acquire()
    await limiter.acquire()  # must wait for the first call to leave the window
    assert ft.sleeps == [5.0]


async def test_rate_limiter_no_delay_after_window_passes(ft: FakeTime) -> None:
    limiter = RateLimiter(max_calls=1, period=15.0, clock=ft.clock, sleep=ft.sleep)
    await limiter.acquire()
    ft.now += 16
    await limiter.acquire()
    assert ft.sleeps == []


def test_rate_limiter_rejects_bad_config(ft: FakeTime) -> None:
    with pytest.raises(ValueError):
        RateLimiter(max_calls=0, period=15.0)


@respx.mock
async def test_client_applies_rate_limit_to_101st_request(ft: FakeTime) -> None:
    respx.get(f"{BASE}/v2/x").respond(200, json={})
    async with make_client(ft) as client:
        for _ in range(101):
            await client.get("/v2/x")
    assert ft.sleeps == [15.0]


@respx.mock
async def test_500_retried_then_unavailable_with_status_500(ft: FakeTime) -> None:
    route = respx.get(f"{BASE}/v2/x").respond(500)
    async with make_client(ft) as client:
        with pytest.raises(HarvestUnavailableError) as info:
            await client.get("/v2/x")
    assert route.call_count == 4
    assert info.value.status_code == 500
    assert ft.sleeps == [1, 2, 4]


@respx.mock
async def test_500_then_ok(ft: FakeTime) -> None:
    respx.get(f"{BASE}/v2/x").mock(side_effect=[httpx.Response(500), httpx.Response(200, json={})])
    async with make_client(ft) as client:
        assert await client.get("/v2/x") == {}


@pytest.mark.parametrize("status", [301, 302, 307, 399])
@respx.mock
async def test_any_3xx_is_clear_export_error(ft: FakeTime, status: int) -> None:
    route = respx.get(f"{BASE}/v2/x").respond(status, headers={"Location": f"{BASE}/v2/y"})
    async with make_client(ft) as client:
        with pytest.raises(ExportError, match=f"unexpected redirect \\({status}\\)"):
            await client.get("/v2/x")
    assert route.call_count == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://api.harvestapp.com/v2/x",
        "https://API.harvestapp.com:8443/v2/x",
        "https://user:pw@api.harvestapp.com/v2/x",
        "https://api.harvestapp.com@evil.example.com/v2/x",
        "//evil.example.com/v2/x",
        "https://evil.example.com\\@api.harvestapp.com/v2/x",
        "https://api.harvestapp.com:notaport/v2/x",
        "ftp://api.harvestapp.com/v2/x",
        "v2/x",
    ],
)
@respx.mock
async def test_origin_guard_blocks_before_any_request(ft: FakeTime, url: str) -> None:
    route = respx.route().respond(200, json={})
    async with make_client(ft) as client:
        with pytest.raises(ExportError, match="different origin"):
            await client.get(url)
    assert route.call_count == 0


@pytest.mark.parametrize(
    "url",
    ["https://api.harvestapp.com/v2/x", "https://API.HarvestApp.com:443/v2/x", "/v2/x"],
)
@respx.mock
async def test_origin_guard_allows_same_origin(ft: FakeTime, url: str) -> None:
    respx.get(f"{BASE}/v2/x").respond(200, json={"ok": 1})
    async with make_client(ft) as client:
        assert await client.get(url) == {"ok": 1}


@respx.mock
async def test_pagination_loop_detected(ft: FakeTime) -> None:
    respx.get(f"{BASE}/v2/c").mock(
        side_effect=[
            httpx.Response(200, json={"c": [], "links": {"next": f"{BASE}/v2/c?page=2"}}),
            httpx.Response(200, json={"c": [], "links": {"next": f"{BASE}/v2/c"}}),
        ]
    )
    async with make_client(ft) as client:
        with pytest.raises(ExportError, match="pagination loop"):
            [r async for r in client.paginate("/v2/c", "c")]


@respx.mock
async def test_pagination_page_cap(ft: FakeTime, monkeypatch: pytest.MonkeyPatch) -> None:
    from exit_archive.harvest import http

    monkeypatch.setattr(http, "MAX_PAGES", 3)

    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={"c": [], "links": {"next": f"{BASE}/v2/c?page={n + 1}"}})

    respx.get(f"{BASE}/v2/c").mock(side_effect=handler)
    async with make_client(ft) as client:
        with pytest.raises(ExportError, match="too many pages"):
            [r async for r in client.paginate("/v2/c", "c")]
