"""Adversarial probes for HarvestClient.download and download_files (tester-added)."""

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from exit_archive.errors import AuthError, ExitArchiveError
from exit_archive.harvest import files as files_mod
from exit_archive.harvest.files import download_files
from exit_archive.harvest.http import DownloadError, HarvestClient
from exit_archive.secrets import HarvestToken

SECRET = "fake-token-dl-adversarial-777"  # noqa: S105
S3 = "https://bucket.s3.amazonaws.com/obj"


def make_client() -> HarvestClient:
    async def no_sleep(_: float) -> None:
        return None

    return HarvestClient(HarvestToken(SECRET), 42, sleep=no_sleep)


def route(seen: list[httpx.Request], mapping: dict[str, httpx.Response]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return mapping.get(str(request.url), httpx.Response(404))

    respx.route().mock(side_effect=handler)


def has_token(request: httpx.Request) -> bool:
    return "Authorization" in request.headers or "Harvest-Account-Id" in request.headers


@respx.mock
async def test_three_hop_chain_token_only_on_harvest_hops(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    route(
        seen,
        {
            "https://x.harvestapp.com/r": httpx.Response(302, headers={"Location": S3}),
            S3: httpx.Response(302, headers={"Location": "https://y.harvestapp.com/back"}),
            "https://y.harvestapp.com/back": httpx.Response(200, content=b"img"),
        },
    )
    async with make_client() as c:
        n = await c.download(
            "https://x.harvestapp.com/r", tmp_path / "f.bin", send_token=True, max_bytes=100
        )
    assert n == 3
    assert [has_token(r) for r in seen] == [True, False, True]  # re-attached on harvest hop
    assert all(r.url.host != "bucket.s3.amazonaws.com" or not has_token(r) for r in seen)


@respx.mock
async def test_http_first_url_never_carries_token(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    route(seen, {"http://x.harvestapp.com/r": httpx.Response(200, content=b"img")})
    async with make_client() as c:
        with contextlib.suppress(DownloadError):
            await c.download(
                "http://x.harvestapp.com/r", tmp_path / "f", send_token=True, max_bytes=100
            )
    assert not any(has_token(r) for r in seen)  # report: sent without token or rejected


@respx.mock
@pytest.mark.parametrize(
    "url",
    [
        "https://x.harvestapp.com:8443/r",
        "https://harvestapp.com.evil.com/r",
        "https://x.harvestapp.com@evil.com/r",
    ],
)
async def test_non_harvest_first_urls_get_no_token(tmp_path: Path, url: str) -> None:
    seen: list[httpx.Request] = []
    route(seen, {})
    async with make_client() as c:
        with contextlib.suppress(Exception):
            await c.download(url, tmp_path / "f", send_token=True, max_bytes=100)
    assert not any(has_token(r) for r in seen)


@respx.mock
@pytest.mark.parametrize(
    "location",
    ["http://evil.com/x", "javascript:alert(1)", "data:text/plain,hi", "//evil.com/x", "ftp://a/b"],
)
async def test_bad_redirect_targets(tmp_path: Path, location: str) -> None:
    seen: list[httpx.Request] = []
    route(seen, {"https://x.harvestapp.com/r": httpx.Response(302, headers={"Location": location})})
    async with make_client() as c:
        with pytest.raises(ExitArchiveError) as exc:
            await c.download(
                "https://x.harvestapp.com/r", tmp_path / "f", send_token=True, max_bytes=100
            )
    # "//evil.com/x" resolves to https://evil.com/x and is followed WITHOUT a token (finding);
    # every other target is rejected as bad_redirect before any request.
    if location.startswith("//"):
        assert [r.url.host for r in seen] == ["x.harvestapp.com", "evil.com"]
    else:
        assert isinstance(exc.value, DownloadError) and exc.value.reason == "bad_redirect"
        assert len(seen) == 1
    assert not any(r.url.host == "evil.com" and has_token(r) for r in seen)
    assert not list(tmp_path.glob("*"))


@respx.mock
async def test_api_default_authorization_never_on_download_requests(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    route(
        seen,
        {
            "https://x.harvestapp.com/r": httpx.Response(302, headers={"Location": S3}),
            S3: httpx.Response(200, content=b"%PDF-1"),
        },
    )
    async with make_client() as c:
        await c.download(
            S3.replace("bucket.s3.amazonaws.com", "x.harvestapp.com/r").replace("/obj", ""),
            tmp_path / "a",
            send_token=False,
            max_bytes=100,
        )
    assert seen and not any(has_token(r) for r in seen)
    assert SECRET not in "".join(str(r.headers) for r in seen)


@respx.mock
async def test_cookie_from_harvest_not_sent_to_other_host(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    route(
        seen,
        {
            "https://x.harvestapp.com/r": httpx.Response(
                302, headers={"Location": S3, "Set-Cookie": "sess=abc123; Path=/"}
            ),
            S3: httpx.Response(200, content=b"img"),
        },
    )
    async with make_client() as c:
        await c.download(
            "https://x.harvestapp.com/r", tmp_path / "f", send_token=True, max_bytes=100
        )
    s3_requests = [r for r in seen if r.url.host == "bucket.s3.amazonaws.com"]
    assert s3_requests and all("cookie" not in r.headers for r in s3_requests)


@respx.mock
async def test_content_length_lie_and_oversize_header(tmp_path: Path) -> None:
    big = b"A" * 5000
    respx.get("https://x.harvestapp.com/lie").mock(
        return_value=httpx.Response(200, content=big, headers={"Content-Length": "10"})
    )
    respx.get("https://x.harvestapp.com/huge").mock(
        return_value=httpx.Response(200, content=b"x", headers={"Content-Length": "999999"})
    )
    async with make_client() as c:
        for path in ("lie", "huge"):
            with pytest.raises((DownloadError, httpx.HTTPError)):
                await c.download(
                    f"https://x.harvestapp.com/{path}",
                    tmp_path / path,
                    send_token=True,
                    max_bytes=1000,
                )
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_transport_error_midstream_no_part_file(tmp_path: Path, caplog: Any) -> None:
    caplog.set_level(logging.DEBUG)

    class Boom(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            yield b"abc"
            raise httpx.ReadError(f"boom {SECRET}")

    respx.get("https://x.harvestapp.com/r").mock(return_value=httpx.Response(200, stream=Boom()))
    async with make_client() as c:
        with pytest.raises(Exception) as exc:  # noqa: PT011
            await c.download(
                "https://x.harvestapp.com/r", tmp_path / "f", send_token=True, max_bytes=100
            )
    assert SECRET not in str(exc.value) and SECRET not in caplog.text
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_semaphore_cap_and_auth_error_cancels_cleanly(tmp_path: Path) -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        if request.url.path.endswith("/bad"):
            return httpx.Response(401)
        return httpx.Response(200, content=b"img")

    respx.route().mock(side_effect=handler)
    expenses = [
        {"id": i, "receipt": {"url": f"https://x.harvestapp.com/r/{i}", "file_name": f"{i}.png"}}
        for i in range(12)
    ]
    expenses.insert(
        6, {"id": 99, "receipt": {"url": "https://x.harvestapp.com/r/bad", "file_name": "b.png"}}
    )
    async with make_client() as c:
        with pytest.raises(AuthError):
            await download_files(
                c,
                tmp_path,
                expenses=expenses,
                invoices=[],
                estimates=[],
                base_uri="https://a.harvestapp.com",
            )
    assert peak <= files_mod.CONCURRENCY
    assert list(tmp_path.rglob("*.part")) == []
    await asyncio.sleep(0.05)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert all(t.done() or "handler" not in repr(t) for t in pending)


@respx.mock
async def test_pdf_download_logging_baseline(tmp_path: Path, caplog: Any) -> None:
    caplog.set_level(logging.INFO)
    respx.get("https://a.harvestapp.com/client/invoices/k.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 x")
    )
    async with make_client() as c:
        await c.download(
            "https://a.harvestapp.com/client/invoices/k.pdf",
            tmp_path / "i.pdf",
            send_token=False,
            max_bytes=100,
            require_prefix=b"%PDF",
        )
    print("LOGBASELINE", [(r.name, r.levelname, r.getMessage()) for r in caplog.records])
    assert SECRET not in caplog.text
