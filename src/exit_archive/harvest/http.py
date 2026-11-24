"""Async Harvest HTTP client: auth headers, rate limiting, retries, error mapping, pagination.

This module and ``secrets.py`` are the only places allowed to call ``HarvestToken.reveal()``.
Nothing here logs request headers, and mapped errors are raised ``from None`` so httpx
exceptions (which can carry request details) are never chained.
"""

import asyncio
import contextlib
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin, urlparse, urlsplit

import httpx

from exit_archive import __version__
from exit_archive.errors import (
    AuthError,
    ExportError,
    ForbiddenError,
    HarvestUnavailableError,
    NotFoundError,
    RateLimitedError,
)
from exit_archive.secrets import HarvestToken

API_BASE_URL = "https://api.harvestapp.com"
MAX_REQUESTS = 100
WINDOW_SECONDS = 15.0
MAX_RETRY_AFTER = 60.0
DEFAULT_RETRY_AFTER = 5.0
MAX_429_TRIES = 5
MAX_5XX_TRIES = 4
BACKOFF_BASE = 1.0
RETRYABLE_5XX = frozenset({500, 502, 503, 504})
MAX_PAGES = 10_000
DEFAULT_PORTS = {"https": 443, "http": 80}
ORIGIN_ERROR = "Refusing to follow a link to a different origin"

MAX_REDIRECTS = 5
HARVEST_DOMAIN = "harvestapp.com"
CHUNK_SIZE = 64 * 1024

Origin = tuple[str, str, int]


def _origin(url: str) -> Origin | None:
    """Return (scheme, lowercase host, effective port), or None if not a clean absolute URL."""
    if "\\" in url or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return scheme, parts.hostname.lower(), port or DEFAULT_PORTS[scheme]


def is_harvest_host_url(url: str) -> bool:
    """True for https, port 443, no userinfo, host ``harvestapp.com`` or ``*.harvestapp.com``."""
    origin = _origin(url)
    if origin is None:
        return False
    scheme, host, port = origin
    if scheme != "https" or port != 443:
        return False
    return host == HARVEST_DOMAIN or host.endswith("." + HARVEST_DOMAIN)


class DownloadError(ExportError):
    """A single file download failed for a fixed, token-free reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Download failed: {reason}")


Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class RateLimiter:
    """Allow at most ``max_calls`` acquisitions per rolling ``period`` seconds."""

    def __init__(
        self,
        max_calls: int = MAX_REQUESTS,
        period: float = WINDOW_SECONDS,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if max_calls < 1 or period <= 0:
            raise ValueError("max_calls must be >= 1 and period > 0")
        self._max = max_calls
        self._period = period
        self._clock = clock
        self._sleep = sleep
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= self._period:
                    self._stamps.popleft()
                if len(self._stamps) < self._max:
                    self._stamps.append(now)
                    return
                await self._sleep(self._stamps[0] + self._period - now)


def _retry_after(response: httpx.Response) -> float:
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return DEFAULT_RETRY_AFTER
    if seconds != seconds or seconds < 0:  # NaN or negative
        return DEFAULT_RETRY_AFTER
    return min(seconds, MAX_RETRY_AFTER)


class HarvestClient:
    """Rate-limited, retrying client for one Harvest host; use as ``async with``."""

    def __init__(
        self,
        token: HarvestToken,
        account_id: int | str | None,
        *,
        base_url: str = API_BASE_URL,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        headers = {
            "Authorization": f"Bearer {token.reveal()}",
            "User-Agent": f"ExitArchive/{__version__}",
            "Accept": "application/json",
        }
        if account_id is not None:
            headers["Harvest-Account-Id"] = str(account_id)
        self._headers = headers
        self._base_url = base_url
        self._origin = _origin(base_url)
        self._sleep = sleep
        self._limiter = RateLimiter(clock=clock, sleep=sleep)
        self._client: httpx.AsyncClient | None = None
        self._dl_client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=httpx.Timeout(30.0, connect=10.0, pool=10.0),
            follow_redirects=False,
        )
        # Header-less client for binary downloads: Authorization is added per hop, never by default.
        self._dl_client = httpx.AsyncClient(
            headers={"User-Agent": self._headers["User-Agent"]},
            timeout=httpx.Timeout(30.0, connect=10.0, pool=10.0),
            follow_redirects=False,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        client, self._client = self._client, None
        dl_client, self._dl_client = self._dl_client, None
        for c in (client, dl_client):
            if c is not None:
                await c.aclose()

    @property
    def timeout(self) -> httpx.Timeout:
        return self._require().timeout

    def _require(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HarvestClient must be used as an async context manager")
        return self._client

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        resource: str | None = None,
    ) -> dict[str, Any]:
        """GET ``path`` (or an absolute URL on the same host) and return the JSON object."""
        client = self._require()
        name = resource or urlparse(path).path or path
        self._check_origin(path)

        async def send() -> httpx.Response:
            return await client.get(path, params=params)

        response = await self._send_with_retries(send, name)
        return self._parse(response)

    def _check_origin(self, path: str) -> None:
        if path.startswith("/") and not path.startswith("//") and "\\" not in path:
            return
        if _origin(path) != self._origin or self._origin is None:
            raise ExportError(ORIGIN_ERROR) from None

    async def _send_with_retries(
        self,
        send: Callable[[], Awaitable[httpx.Response]],
        name: str,
        *,
        allow_redirect: bool = False,
    ) -> httpx.Response:
        """Run ``send`` under the shared limiter and 429/5xx rules; return a 2xx (or 3xx) reply.

        Replies that are not returned are closed, so streamed bodies never leak.
        """
        tries_429 = 0
        tries_5xx = 0
        last_5xx = 503
        while True:
            await self._limiter.acquire()
            try:
                response = await send()
            except httpx.TransportError:
                response = None
            status = response.status_code if response is not None else 503
            if response is not None and (status < 300 or (allow_redirect and status < 400)):
                return response
            if response is not None:
                await response.aclose()
            if response is not None and 300 <= status < 400:
                raise ExportError(
                    f"Harvest returned an unexpected redirect ({status}) for {name}"
                ) from None
            if status == 429 and response is not None:
                tries_429 += 1
                if tries_429 >= MAX_429_TRIES:
                    raise RateLimitedError(name) from None
                await self._sleep(_retry_after(response))
            elif response is None or status in RETRYABLE_5XX:
                last_5xx = status
                tries_5xx += 1
                if tries_5xx >= MAX_5XX_TRIES:
                    raise HarvestUnavailableError(name, status_code=last_5xx) from None
                await self._sleep(BACKOFF_BASE * 2 ** (tries_5xx - 1))
            else:
                self._raise_for_status(status, name)

    @staticmethod
    def _raise_for_status(status: int, name: str) -> None:
        if status == 401:
            raise AuthError(name) from None
        if status == 403:
            raise ForbiddenError(name) from None
        if status == 404:
            raise NotFoundError(name) from None
        raise ExportError(f"Harvest returned unexpected status {status} for {name}") from None

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            raise ExportError("Harvest returned a response that is not JSON") from None
        if not isinstance(data, dict):
            raise ExportError("Harvest returned an unexpected JSON shape") from None
        return data

    async def paginate(
        self, path: str, key: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every record under ``key``, following ``links.next`` until it is null."""
        client = self._require()
        url: str | None = path
        page_params = params
        requested: set[str] = set()
        while url is not None:
            self._check_origin(url)
            page_url = str(client.build_request("GET", url, params=page_params).url)
            if page_url in requested:
                raise ExportError("Harvest pagination loop detected") from None
            if len(requested) >= MAX_PAGES:
                raise ExportError(
                    f"Harvest pagination has too many pages (> {MAX_PAGES})"
                ) from None
            requested.add(page_url)
            data = await self.get(url, page_params, resource=path)
            records = data.get(key)
            if not isinstance(records, list):
                raise ExportError(f"Harvest response for {path} has no '{key}' list") from None
            for record in records:
                yield record
            links = data.get("links")
            nxt = links.get("next") if isinstance(links, dict) else None
            url = nxt if isinstance(nxt, str) and nxt else None
            page_params = None  # the next link already carries the query string

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        send_token: bool,
        max_bytes: int,
        require_prefix: bytes | None = None,
    ) -> int:
        """Stream ``url`` to ``dest`` and return the byte count.

        This deliberately bypasses the same-origin guard of ``get``: receipts live on
        ``*.harvestapp.com`` but are served via redirects to other hosts (object storage), and
        PDFs come from the company ``base_uri``. Safety comes from per-hop rules instead.
        Redirects are followed manually (at most 5) and every hop must be clean https. The
        Bearer token is attached only when ``send_token`` is true AND that hop's host passes
        ``is_harvest_host_url``; any other host gets no Authorization (nor account id).
        The body is capped at ``max_bytes`` (``too_large``) and, if ``require_prefix`` is
        given, must start with it (``not_a_pdf``). A partial file is always deleted.
        """
        client = self._dl_client
        if client is None:
            raise RuntimeError("HarvestClient must be used as an async context manager")
        name = "download"
        tmp = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            total = await self._download_to(
                client, url, tmp, send_token, max_bytes, require_prefix, name
            )
            os.replace(tmp, dest)
            return total
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _hop_headers(self, url: str, send_token: bool) -> dict[str, str]:
        if not (send_token and is_harvest_host_url(url)):
            return {}
        keep = ("Authorization", "Harvest-Account-Id")
        return {k: v for k, v in self._headers.items() if k in keep}

    async def _download_to(
        self,
        client: httpx.AsyncClient,
        url: str,
        tmp: Path,
        send_token: bool,
        max_bytes: int,
        require_prefix: bytes | None,
        name: str,
    ) -> int:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if _origin(current) is None:
                raise DownloadError("bad_redirect") from None
            headers = self._hop_headers(current, send_token)
            target = current

            async def send(
                target: str = target, headers: dict[str, str] = headers
            ) -> httpx.Response:
                try:
                    return await client.send(
                        client.build_request("GET", target, headers=headers), stream=True
                    )
                except httpx.InvalidURL:  # httpx pre-parses the next Location even unfollowed
                    raise DownloadError("bad_redirect") from None

            response = await self._send_with_retries(send, name, allow_redirect=True)
            try:
                if response.status_code < 300:
                    return await self._stream_body(response, tmp, max_bytes, require_prefix)
                location = response.headers.get("Location")
            finally:
                await response.aclose()
            if not location:
                raise DownloadError("bad_redirect") from None
            current = urljoin(current, location)
            if _origin(current) is None or not current.lower().startswith("https://"):
                raise DownloadError("bad_redirect") from None
        raise DownloadError("too_many_redirects") from None

    @staticmethod
    async def _stream_body(
        response: httpx.Response, tmp: Path, max_bytes: int, require_prefix: bytes | None
    ) -> int:
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise DownloadError("too_large") from None
        total = 0
        head = b""
        try:
            with tmp.open("wb") as out:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    total += len(chunk)
                    if total > max_bytes:
                        raise DownloadError("too_large") from None
                    if require_prefix is not None and len(head) < len(require_prefix):
                        head += chunk[: len(require_prefix) - len(head)]
                    out.write(chunk)
        except httpx.TransportError:
            raise HarvestUnavailableError("download", status_code=503) from None
        if require_prefix is not None and not head.startswith(require_prefix):
            raise DownloadError("not_a_pdf") from None
        return total
