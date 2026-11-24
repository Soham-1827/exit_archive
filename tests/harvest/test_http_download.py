"""HarvestClient.download tests. respx mocks only; no network, no real sleeping."""

import logging
from pathlib import Path

import httpx
import pytest
import respx

from exit_archive.errors import (
    AuthError,
    ExportError,
    ForbiddenError,
    HarvestUnavailableError,
    NotFoundError,
)
from exit_archive.harvest.http import (
    DownloadError,
    HarvestClient,
    is_harvest_host_url,
)
from exit_archive.secrets import HarvestToken

SECRET = "fake-token-1234567890-SECRET"  # noqa: S105 (fake test value)
URL = "https://x.harvestapp.com/receipt/1.png"


class FakeTime:
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


def make_client(ft: FakeTime) -> HarvestClient:
    return HarvestClient(HarvestToken(SECRET), 42, clock=ft.clock, sleep=ft.sleep)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://x.harvestapp.com/a", True),
        ("https://harvestapp.com/a", True),
        ("https://A.B.HarvestApp.com:443/a", True),
        ("http://x.harvestapp.com/a", False),
        ("https://x.harvestapp.com:8443/a", False),
        ("https://harvestapp.com.evil.com/a", False),
        ("https://evilharvestapp.com/a", False),
        ("https://x@x.harvestapp.com/a", False),
        ("https://evil.example.com/a", False),
        ("javascript:alert(1)", False),
        ("", False),
    ],
)
def test_is_harvest_host_url(url: str, ok: bool) -> None:
    assert is_harvest_host_url(url) is ok


@respx.mock
async def test_allowed_host_gets_authorization(ft: FakeTime, tmp_path: Path) -> None:
    route = respx.get(URL).respond(200, content=b"PNGDATA")
    dest = tmp_path / "r.png"
    async with make_client(ft) as client:
        n = await client.download(URL, dest, send_token=True, max_bytes=100)
    assert n == 7
    assert dest.read_bytes() == b"PNGDATA"
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {SECRET}"


@respx.mock
async def test_send_token_false_never_sends_authorization(ft: FakeTime, tmp_path: Path) -> None:
    route = respx.get(URL).respond(200, content=b"%PDF-1")
    async with make_client(ft) as client:
        await client.download(URL, tmp_path / "a.pdf", send_token=False, max_bytes=100)
    assert "Authorization" not in route.calls[0].request.headers
    assert "Harvest-Account-Id" not in route.calls[0].request.headers


@respx.mock
async def test_disallowed_host_gets_no_authorization(ft: FakeTime, tmp_path: Path) -> None:
    route = respx.get("https://evil.example.com/a").respond(200, content=b"x")
    async with make_client(ft) as client:
        await client.download(
            "https://evil.example.com/a", tmp_path / "a", send_token=True, max_bytes=10
        )
    assert "Authorization" not in route.calls[0].request.headers


@respx.mock
async def test_redirect_to_s3_drops_authorization(ft: FakeTime, tmp_path: Path) -> None:
    first = respx.get(URL).respond(302, headers={"Location": "https://bucket.s3.amazonaws.com/k"})
    second = respx.get("https://bucket.s3.amazonaws.com/k").respond(200, content=b"IMG")
    dest = tmp_path / "r.png"
    async with make_client(ft) as client:
        await client.download(URL, dest, send_token=True, max_bytes=100)
    assert first.calls[0].request.headers["Authorization"] == f"Bearer {SECRET}"
    assert "Authorization" not in second.calls[0].request.headers
    assert dest.read_bytes() == b"IMG"


@respx.mock
async def test_redirect_within_harvest_keeps_authorization_and_relative_location(
    ft: FakeTime, tmp_path: Path
) -> None:
    respx.get(URL).respond(301, headers={"Location": "/other.png"})
    second = respx.get("https://x.harvestapp.com/other.png").respond(200, content=b"A")
    async with make_client(ft) as client:
        await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)
    assert second.calls[0].request.headers["Authorization"] == f"Bearer {SECRET}"


@respx.mock
async def test_redirect_back_to_harvest_after_other_host_gets_token_only_if_allowed(
    ft: FakeTime, tmp_path: Path
) -> None:
    respx.get(URL).respond(302, headers={"Location": "https://evil.example.com/a"})
    respx.get("https://evil.example.com/a").respond(
        302, headers={"Location": "https://y.harvestapp.com/b"}
    )
    last = respx.get("https://y.harvestapp.com/b").respond(200, content=b"A")
    async with make_client(ft) as client:
        await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)
    # the host is allowed again, so the token is attached again (per-hop rule)
    assert "Authorization" in last.calls[0].request.headers


@respx.mock
@pytest.mark.parametrize(
    "loc", ["http://x.harvestapp.com/a", "javascript:alert(1)", "https://u@h.com/a"]
)
async def test_redirect_to_http_or_garbage_is_refused(
    ft: FakeTime, tmp_path: Path, loc: str
) -> None:
    respx.get(URL).respond(302, headers={"Location": loc})
    async with make_client(ft) as client:
        with pytest.raises(DownloadError) as ei:
            await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)
    assert ei.value.reason == "bad_redirect"
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_redirect_without_location_is_refused(ft: FakeTime, tmp_path: Path) -> None:
    respx.get(URL).respond(302)
    async with make_client(ft) as client:
        with pytest.raises(DownloadError) as ei:
            await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)
    assert ei.value.reason == "bad_redirect"


@respx.mock
async def test_at_most_five_redirects(ft: FakeTime, tmp_path: Path) -> None:
    for i in range(7):
        respx.get(f"https://x.harvestapp.com/h{i}").respond(
            302, headers={"Location": f"https://x.harvestapp.com/h{i + 1}"}
        )
    async with make_client(ft) as client:
        with pytest.raises(DownloadError) as ei:
            await client.download(
                "https://x.harvestapp.com/h0", tmp_path / "a", send_token=True, max_bytes=10
            )
    assert ei.value.reason == "too_many_redirects"
    assert len(respx.calls) == 6  # original + 5 followed hops


@respx.mock
async def test_five_redirects_then_success_is_ok(ft: FakeTime, tmp_path: Path) -> None:
    for i in range(5):
        respx.get(f"https://x.harvestapp.com/h{i}").respond(
            302, headers={"Location": f"https://x.harvestapp.com/h{i + 1}"}
        )
    respx.get("https://x.harvestapp.com/h5").respond(200, content=b"ok")
    async with make_client(ft) as client:
        await client.download(
            "https://x.harvestapp.com/h0", tmp_path / "a", send_token=True, max_bytes=10
        )
    assert (tmp_path / "a").read_bytes() == b"ok"


@respx.mock
async def test_too_large_body_deletes_partial(ft: FakeTime, tmp_path: Path) -> None:
    respx.get(URL).respond(200, content=b"x" * 50)
    dest = tmp_path / "big"
    async with make_client(ft) as client:
        with pytest.raises(DownloadError) as ei:
            await client.download(URL, dest, send_token=True, max_bytes=10)
    assert ei.value.reason == "too_large"
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_too_large_content_length_rejected_early(ft: FakeTime, tmp_path: Path) -> None:
    respx.get(URL).respond(200, content=b"x" * 5, headers={"Content-Length": "999"})
    async with make_client(ft) as client:
        with pytest.raises(DownloadError) as ei:
            await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)
    assert ei.value.reason == "too_large"
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_exactly_max_bytes_is_allowed(ft: FakeTime, tmp_path: Path) -> None:
    respx.get(URL).respond(200, content=b"x" * 10)
    async with make_client(ft) as client:
        assert await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10) == 10


@respx.mock
async def test_require_prefix_mismatch_leaves_no_file(ft: FakeTime, tmp_path: Path) -> None:
    respx.get(URL).respond(200, content=b"<html>login</html>")
    async with make_client(ft) as client:
        with pytest.raises(DownloadError) as ei:
            await client.download(
                URL, tmp_path / "a.pdf", send_token=False, max_bytes=100, require_prefix=b"%PDF"
            )
    assert ei.value.reason == "not_a_pdf"
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_empty_body_with_require_prefix_fails(ft: FakeTime, tmp_path: Path) -> None:
    respx.get(URL).respond(200, content=b"")
    async with make_client(ft) as client:
        with pytest.raises(DownloadError):
            await client.download(
                URL, tmp_path / "a", send_token=False, max_bytes=100, require_prefix=b"%PDF"
            )
    assert list(tmp_path.iterdir()) == []


@respx.mock
@pytest.mark.parametrize(
    ("status", "exc"),
    [(401, AuthError), (403, ForbiddenError), (404, NotFoundError), (418, ExportError)],
)
async def test_status_mapping(ft: FakeTime, tmp_path: Path, status: int, exc: type) -> None:
    respx.get(URL).respond(status)
    async with make_client(ft) as client:
        with pytest.raises(exc):
            await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_retries_5xx_then_succeeds(ft: FakeTime, tmp_path: Path) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, content=b"ok")]
    )
    async with make_client(ft) as client:
        await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)
    assert route.call_count == 3
    assert ft.sleeps == [1.0, 2.0]


@respx.mock
async def test_5xx_exhausted_raises_unavailable(ft: FakeTime, tmp_path: Path) -> None:
    respx.get(URL).respond(500)
    async with make_client(ft) as client:
        with pytest.raises(HarvestUnavailableError):
            await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)


@respx.mock
async def test_429_honours_retry_after(ft: FakeTime, tmp_path: Path) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, content=b"ok"),
        ]
    )
    async with make_client(ft) as client:
        await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)
    assert ft.sleeps == [7.0]


@respx.mock
async def test_transport_error_is_retried_then_unavailable(ft: FakeTime, tmp_path: Path) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    async with make_client(ft) as client:
        with pytest.raises(HarvestUnavailableError):
            await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)


@respx.mock
async def test_mid_stream_failure_deletes_partial(ft: FakeTime, tmp_path: Path) -> None:
    class Boom(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield b"abc"
            raise httpx.ReadError("cut")

    respx.get(URL).mock(return_value=httpx.Response(200, stream=Boom()))
    async with make_client(ft) as client:
        with pytest.raises(HarvestUnavailableError):
            await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_shares_rate_limiter(ft: FakeTime, tmp_path: Path) -> None:
    respx.get(URL).respond(200, content=b"x")
    async with make_client(ft) as client:
        for i in range(101):
            await client.download(URL, tmp_path / f"f{i}", send_token=True, max_bytes=10)
    assert sum(ft.sleeps) > 0  # the 101st call waited for the window


async def test_download_requires_context_manager(ft: FakeTime, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        await make_client(ft).download(URL, tmp_path / "a", send_token=True, max_bytes=1)


@respx.mock
async def test_token_never_leaks(
    ft: FakeTime, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    respx.get(URL).respond(404)
    async with make_client(ft) as client:
        with pytest.raises(NotFoundError) as ei:
            await client.download(URL, tmp_path / "a", send_token=True, max_bytes=10)
    assert SECRET not in str(ei.value) + repr(ei.value) + caplog.text
    assert ei.value.__cause__ is None


@respx.mock
async def test_dest_parent_is_created(ft: FakeTime, tmp_path: Path) -> None:
    respx.get(URL).respond(200, content=b"x")
    dest = tmp_path / "sub" / "dir" / "a"
    async with make_client(ft) as client:
        await client.download(URL, dest, send_token=True, max_bytes=10)
    assert dest.read_bytes() == b"x"
