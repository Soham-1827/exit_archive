"""Access log: ``METHOD path status Nms`` with no query, headers, client IP or job ids."""

import contextlib
import logging
import re
import sys
import time
from collections.abc import Iterator

from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["ACCESS_LOGGER", "AccessLogMiddleware", "access_logging"]

ACCESS_LOGGER = "exit_archive.access"
_MAX_PATH = 120
_ID_RUN = re.compile(r"[A-Za-z0-9_-]{20,}")
_UNSAFE = re.compile(r"[^\x21-\x7e]")
_log = logging.getLogger(ACCESS_LOGGER)


def mask_path(path: str) -> str:
    """Hide job ids (download capabilities) and make the path safe to log on one line."""
    if path.startswith("/jobs/"):
        _, _, _, *rest = path.split("/")
        path = "/jobs/<id>" + "".join("/download" if s == "download" else "/<x>" for s in rest)
    path = _ID_RUN.sub("<x>", path)  # any id-shaped run, wherever it sits; before cap
    return _UNSAFE.sub(lambda m: f"%{ord(m.group()):02X}", path)[:_MAX_PATH]


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500
        started = time.perf_counter()

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            elapsed = int((time.perf_counter() - started) * 1000)
            _log.info("%s %s %s %dms", scope["method"], mask_path(scope["path"]), status, elapsed)


@contextlib.contextmanager
def access_logging() -> Iterator[None]:
    """Silence ``uvicorn.access`` and make our own access log visible; undone on exit."""
    uvicorn = logging.getLogger("uvicorn.access")
    was_disabled, uvicorn.disabled = uvicorn.disabled, True
    old_level, own_handler = _log.level, None
    _log.setLevel(logging.INFO)
    if not logging.getLogger().handlers:  # bare process (uvicorn sets up none for our logger)
        own_handler = logging.StreamHandler(sys.stderr)
        own_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _log.addHandler(own_handler)
    try:
        yield
    finally:
        if own_handler is not None:
            _log.removeHandler(own_handler)
        _log.setLevel(old_level)
        uvicorn.disabled = was_disabled
