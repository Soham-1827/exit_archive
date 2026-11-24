"""Adversarial probes for HarvestClient (tester-added). respx only; fake clock."""

import asyncio
import contextlib
import logging

import httpx
import pytest
import respx

from exit_archive.errors import ExportError, HarvestUnavailableError, RateLimitedError
from tests.harvest.test_http import BASE, SECRET, FakeTime, make_client


def _no_token_requests(route_calls) -> None:
    for call in route_calls:
        assert "Authorization" not in call.request.headers


@respx.mock
@pytest.mark.parametrize(
    "nxt",
    [
        "https://evil.example.com/v2/clients?page=2",
        "http://api.harvestapp.com/v2/clients?page=2",
        "https://api.harvestapp.com@evil.example.com/v2/clients",
        "https://api.harvestapp.com:8443/v2/clients?page=2",
        "//evil.example.com/v2/clients?page=2",
        "https://evil.example.com\\@api.harvestapp.com/v2/clients",
    ],
    ids=["other-host", "http-scheme", "userinfo", "port", "scheme-relative", "backslash"],
)
async def test_next_link_never_sends_token_off_origin(nxt: str) -> None:
    ft = FakeTime()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(200, json={"clients": [{"id": 1}], "links": {"next": nxt}})
        return httpx.Response(200, json={"clients": [], "links": {"next": None}})

    respx.route().mock(side_effect=handler)
    async with make_client(ft) as c:
        with contextlib.suppress(ExportError, httpx.HTTPError):
            _ = [r async for r in c.paginate("/v2/clients", "clients")]
    for request in seen[1:]:
        url = request.url
        assert (url.scheme, url.host, url.port) == ("https", "api.harvestapp.com", None), (
            f"token request sent to {url}"
        )


@respx.mock
async def test_redirect_not_followed_and_clear_error(caplog) -> None:
    ft = FakeTime()
    respx.get(f"{BASE}/v2/clients").mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example.com/x"})
    )
    evil = respx.route(host="evil.example.com").mock(return_value=httpx.Response(200, json={}))
    async with make_client(ft) as c:
        with pytest.raises(ExportError) as exc:
            await c.get("/v2/clients")
    assert evil.call_count == 0
    assert SECRET not in str(exc.value) and SECRET not in caplog.text


@respx.mock
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0.0),
        ("1.5", 1.5),
        ("999", 60.0),
        ("Wed, 21 Oct 2099 07:28:00 GMT", None),
    ],
)
async def test_retry_after_edges(value: str, expected: float | None) -> None:
    ft = FakeTime()
    respx.get(f"{BASE}/v2/x").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": value}),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    async with make_client(ft) as c:
        assert await c.get("/v2/x") == {"ok": 1}
    assert len(ft.sleeps) == 1
    if expected is None:
        assert 0 <= ft.sleeps[0] <= 60  # HTTP-date: default or parsed, never unbounded
    else:
        assert ft.sleeps[0] == expected


@respx.mock
@pytest.mark.parametrize("status", [429, 503])
async def test_error_body_echoing_token_does_not_leak(status: int, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    ft = FakeTime()
    respx.get(f"{BASE}/v2/x").mock(
        return_value=httpx.Response(status, text=f"bad Bearer {SECRET} {SECRET}")
    )
    async with make_client(ft) as c:
        with pytest.raises((RateLimitedError, HarvestUnavailableError)) as exc:
            await c.get("/v2/x")
    assert SECRET not in str(exc.value) and SECRET not in repr(exc.value)
    assert SECRET not in caplog.text


@respx.mock
@pytest.mark.parametrize("err", [httpx.ConnectError, httpx.ReadTimeout])
async def test_transport_errors_retried_then_unavailable(err, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    ft = FakeTime()
    route = respx.get(f"{BASE}/v2/x").mock(side_effect=err(f"boom {SECRET}"))
    async with make_client(ft) as c:
        with pytest.raises(HarvestUnavailableError) as exc:
            await c.get("/v2/x")
    assert route.call_count == 4
    assert SECRET not in str(exc.value) and SECRET not in caplog.text
    assert exc.value.__cause__ is None


@respx.mock
async def test_pagination_loop_terminates() -> None:
    ft = FakeTime()
    p1 = {"clients": [{"id": 1}], "links": {"next": f"{BASE}/v2/clients?page=2"}}
    p2 = {"clients": [{"id": 2}], "links": {"next": f"{BASE}/v2/clients?page=1"}}
    respx.get(f"{BASE}/v2/clients", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=p2)
    )
    respx.get(f"{BASE}/v2/clients").mock(return_value=httpx.Response(200, json=p1))
    seen = 0
    async with make_client(ft) as c:
        try:
            async for _ in c.paginate("/v2/clients", "clients"):
                seen += 1
                assert seen < 50, "links.next loop never terminates"
        except ExportError:
            pass


async def test_concurrent_calls_never_exceed_100_per_window() -> None:
    from exit_archive.harvest.http import RateLimiter

    ft = FakeTime()
    limiter = RateLimiter(clock=ft.clock, sleep=ft.sleep)
    stamps: list[float] = []

    async def one() -> None:
        await limiter.acquire()
        stamps.append(ft.now)

    await asyncio.gather(*(one() for _ in range(150)))
    for s in stamps:
        assert sum(1 for t in stamps if s <= t < s + 15.0) <= 100
    assert len(stamps) == 150


@respx.mock
@pytest.mark.parametrize(
    "url",
    [
        "https://API.harvestapp.com:443/v2/x",
        # leading-zero port is numerically 443: same endpoint, so allowed (decision recorded)
        "https://api.harvestapp.com:0443/v2/x",
        "https://api.harvestapp.com/v2/x",
        "/v2/x",
        "/..%2F..%2F",
    ],
)
async def test_origin_guard_allows_same_origin(url: str) -> None:
    ft = FakeTime()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": 1})

    respx.route().mock(side_effect=handler)
    async with make_client(ft) as c:
        assert await c.get(url) == {"ok": 1}
    assert [r.url.host for r in seen] == ["api.harvestapp.com"]
    assert seen[0].url.port is None
    assert seen[0].url.scheme == "https"


@respx.mock
@pytest.mark.parametrize(
    "url",
    [
        "https://api.harvestapp.com.evil.com/",
        "https://api.harvestapp.com%2F@evil.com/",
        "HTTPS://evil.com",
        " https://api.harvestapp.com/",
        "https://api.harvestapp.com:80/",
        "ftp://api.harvestapp.com/",
    ],
)
async def test_origin_guard_refuses_with_zero_requests(url: str) -> None:
    ft = FakeTime()
    route = respx.route().mock(return_value=httpx.Response(200, json={"ok": 1}))
    async with make_client(ft) as c:
        with pytest.raises(ExportError) as exc:
            await c.get(url)
    assert route.call_count == 0
    assert SECRET not in str(exc.value)


@respx.mock
@pytest.mark.parametrize("cycle", ["self", "abab"])
async def test_loop_guard_no_duplicates(cycle: str) -> None:
    ft = FakeTime()
    a = f"{BASE}/v2/clients"
    b = f"{BASE}/v2/clients?page=2"
    pages = {
        "self": {a: ([1], a)},
        "abab": {a: ([1], b), b: ([2], a)},
    }[cycle]

    def handler(request: httpx.Request) -> httpx.Response:
        recs, nxt = pages[str(request.url)]
        return httpx.Response(
            200, json={"clients": [{"id": i} for i in recs], "links": {"next": nxt}}
        )

    respx.route().mock(side_effect=handler)
    got: list[int] = []
    async with make_client(ft) as c:
        with pytest.raises(ExportError, match="loop"):
            async for rec in c.paginate("/v2/clients", "clients"):
                got.append(rec["id"])
    assert got == sorted(set(got))
    assert got == ([1] if cycle == "self" else [1, 2])


@respx.mock
async def test_500_then_500_then_200_sleeps_1_2() -> None:
    ft = FakeTime()
    respx.get(f"{BASE}/v2/x").mock(
        side_effect=[httpx.Response(500), httpx.Response(500), httpx.Response(200, json={"a": 1})]
    )
    async with make_client(ft) as c:
        assert await c.get("/v2/x") == {"a": 1}
    assert ft.sleeps == [1, 2]


@respx.mock
async def test_500_forever_unavailable_status_500(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    ft = FakeTime()
    respx.get(f"{BASE}/v2/x").mock(return_value=httpx.Response(500, text=f"Bearer {SECRET}"))
    async with make_client(ft) as c:
        with pytest.raises(HarvestUnavailableError) as exc:
            await c.get("/v2/x")
    assert exc.value.status_code == 500
    assert SECRET not in str(exc.value) and SECRET not in caplog.text


@respx.mock
@pytest.mark.parametrize("status", [301, 302, 307])
async def test_3xx_is_unexpected_redirect_and_not_followed(status: int) -> None:
    ft = FakeTime()
    respx.get(f"{BASE}/v2/x").mock(
        return_value=httpx.Response(status, headers={"Location": "https://evil.example.com/"})
    )
    evil = respx.route(host="evil.example.com").mock(return_value=httpx.Response(200))
    async with make_client(ft) as c:
        with pytest.raises(ExportError, match="unexpected redirect"):
            await c.get("/v2/x")
    assert evil.call_count == 0
