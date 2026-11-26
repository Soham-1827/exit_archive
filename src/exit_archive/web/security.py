"""Pure-ASGI hardening: response headers, body limit, Origin check and a POST /jobs rate limit.

Error responses are produced through an injected ``respond`` callable (the app's error page),
so nothing from the request body or headers is ever echoed.
"""

import contextlib
import ipaddress
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "BodyLimitMiddleware",
    "OriginCheckMiddleware",
    "RateLimitMiddleware",
    "RateLimiter",
    "SecurityHeadersMiddleware",
    "client_key",
]

MAX_BODY_BYTES = 16 * 1024
RATE_WINDOW_SECONDS = 600
MAX_RATE_KEYS = 10_000
Respond = Callable[[Request, int], Response]

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "content-security-policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
    ),
    ("strict-transport-security", "max-age=31536000; includeSubDomains"),
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("permissions-policy", "camera=(), microphone=(), geolocation=()"),
)
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _needs_no_store(method: str, path: str) -> bool:
    return method == "POST" or path == "/start" or path.startswith("/jobs/")


class SecurityHeadersMiddleware:
    """Add the security headers to every HTTP response, error pages and static files included."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        no_store = _needs_no_store(scope["method"], scope["path"])

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                extra = list(SECURITY_HEADERS)
                if no_store:
                    extra.append(("cache-control", "no-store"))
                names = {name for name, _ in extra}
                kept = [
                    (k, v)
                    for k, v in message["headers"]
                    if k.decode("latin-1").lower() not in names
                ]
                kept.extend((k.encode("latin-1"), v.encode("latin-1")) for k, v in extra)
                message = {**message, "headers": kept}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLargeError(Exception):
    pass


class BodyLimitMiddleware:
    """Reject POST bodies over the limit: by Content-Length up front, else by counting bytes."""

    def __init__(self, app: ASGIApp, respond: Respond, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.respond = respond
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared is not None:
            if not declared.isascii() or not declared.isdigit():
                await self._reject(scope, receive, send, 400)
                return
            if int(declared) > self.limit:
                await self._reject(scope, receive, send, 413)
                return
        seen = 0
        exceeded = False
        replied = False

        async def counted_receive() -> Message:
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    exceeded = True
                    raise _BodyTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal replied
            if not exceeded:
                await send(message)
            elif not replied:  # FastAPI turns the read failure into a 400: replace it with 413
                replied = True
                await self._reject(scope, receive, send, 413)

        with contextlib.suppress(_BodyTooLargeError):
            await self.app(scope, counted_receive, guarded_send)
        if exceeded and not replied:
            await self._reject(scope, receive, send, 413)

    async def _reject(self, scope: Scope, receive: Receive, send: Send, status: int) -> None:
        await self.respond(Request(scope, receive), status)(scope, receive, send)


def _header(scope: Scope, name: bytes) -> str | None:
    values = [v for k, v in scope["headers"] if k == name]
    return values[0].decode("latin-1") if values else None


def _origin_of(value: str) -> tuple[str, str, int] | None:
    """Return (scheme, host, port) of an Origin/Referer value, or None if unusable."""
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    return scheme, host.lower(), port or _DEFAULT_PORTS[scheme]


class OriginCheckMiddleware:
    """403 a POST whose Origin (or Referer) is not our own origin; absent headers are allowed."""

    def __init__(self, app: ASGIApp, respond: Respond) -> None:
        self.app = app
        self.respond = respond

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and not self._allowed(scope):
            await self.respond(Request(scope, receive), 403)(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _allowed(self, scope: Scope) -> bool:
        sent = _header(scope, b"origin")
        if sent is None:
            sent = _header(scope, b"referer")
        if sent is None:
            return True
        origin = _origin_of(sent)
        if origin is None:  # includes "Origin: null"
            return False
        settings = getattr(scope["app"].state, "settings", None)
        base = getattr(settings, "public_base_url", None)
        if base:
            return origin == _origin_of(base)
        host = _header(scope, b"host")
        if not host:
            return False
        try:
            h = urlsplit("//" + host)
            return bool(
                h.hostname
                and h.hostname.lower() == origin[1]
                and (h.port or _DEFAULT_PORTS[origin[0]]) == origin[2]
            )
        except ValueError:
            return False


def client_key(scope: Scope, hops: int) -> str:
    """Rate-limit key: socket peer, or the ``hops``-th X-Forwarded-For entry from the right."""
    client = scope.get("client")
    peer = str(client[0]) if client else "unknown"
    if hops <= 0:
        return peer
    values = [v.decode("latin-1") for k, v in scope["headers"] if k == b"x-forwarded-for"]
    entries = [e.strip() for e in ",".join(values).split(",")] if values else []
    if len(entries) < hops:
        return peer
    try:
        return str(ipaddress.ip_address(entries[-hops]))
    except ValueError:
        return peer


class RateLimiter:
    """Rolling-window counter per key: in memory, pruned, bounded by ``max_keys``."""

    def __init__(
        self,
        window: float = RATE_WINDOW_SECONDS,
        max_keys: int = MAX_RATE_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window
        self._max_keys = max_keys
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def size(self) -> int:
        return len(self._hits)

    def check(self, key: str, limit: int) -> int | None:
        """Record a hit; return None if allowed, else whole seconds until a slot frees up."""
        now = self._clock()
        hits = self._hits.get(key)
        if hits is None:
            self._make_room(now)
            hits = self._hits[key] = deque()
        self._hits.move_to_end(key)
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) >= limit:
            return max(1, math.ceil(hits[0] + self._window - now))
        hits.append(now)
        return None

    def _make_room(self, now: float) -> None:
        if len(self._hits) < self._max_keys:
            return
        cutoff = now - self._window
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            del self._hits[key]
        while len(self._hits) >= self._max_keys:
            self._hits.popitem(last=False)  # least recently used


class RateLimitMiddleware:
    """429 + Retry-After once a client exceeds ``Settings.jobs_rate_limit`` POSTs to /jobs."""

    def __init__(self, app: ASGIApp, respond: Respond, limiter: RateLimiter) -> None:
        self.app = app
        self.respond = respond
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/jobs":
            settings = getattr(scope["app"].state, "settings", None)
            limit = getattr(settings, "jobs_rate_limit", None)
            if limit is not None:
                key = client_key(scope, int(getattr(settings, "trusted_proxy_hops", 0)))
                wait = self.limiter.check(key, limit)
                if wait is not None:
                    response = self.respond(Request(scope, receive), 429)
                    response.headers["Retry-After"] = str(wait)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)
