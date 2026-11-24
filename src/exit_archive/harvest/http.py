"""Async Harvest HTTP client: auth headers, rate limiting, retries, error mapping, pagination.

This module and ``secrets.py`` are the only places allowed to call ``HarvestToken.reveal()``.
Nothing here logs request headers, and mapped errors are raised ``from None`` so httpx
exceptions (which can carry request details) are never chained.
"""

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlparse, urlsplit

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

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
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
        if client is not None:
            await client.aclose()

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
        response = await self._send_with_retries(client, path, params, name)
        return self._parse(response)

    def _check_origin(self, path: str) -> None:
        if path.startswith("/") and not path.startswith("//") and "\\" not in path:
            return
        if _origin(path) != self._origin or self._origin is None:
            raise ExportError(ORIGIN_ERROR) from None

    async def _send_with_retries(
        self, client: httpx.AsyncClient, path: str, params: dict[str, Any] | None, name: str
    ) -> httpx.Response:
        tries_429 = 0
        tries_5xx = 0
        last_5xx = 503
        while True:
            await self._limiter.acquire()
            try:
                response = await client.get(path, params=params)
            except httpx.TransportError:
                response = None
            status = response.status_code if response is not None else 503
            if response is not None and status < 300:
                return response
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
