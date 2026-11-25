"""Payment seam and the Stripe-backed verifier.

This module is the only place the Stripe key is revealed (to build the Authorization header).
Nothing here logs the URL, the response body or the session id.
"""

import asyncio
import logging
import re
from typing import Protocol

import httpx

from exit_archive.secrets import Secret

__all__ = [
    "AllowAllVerifier",
    "MalformedSessionError",
    "PaymentProviderError",
    "PaymentVerifier",
    "StripeVerifier",
]

_log = logging.getLogger(__name__)
_SESSION_ID = re.compile(r"cs_(test|live)_[A-Za-z0-9]{1,250}")
_KEY_MODE = re.compile(r"(?:sk|rk)_(test|live)_")
_BASE = "https://api.stripe.com/v1/checkout/sessions/"
# connect + read + write + pool = 10 s at most; the wall-clock guard below enforces it too.
_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=1.0)
_TOTAL_SECONDS = 10.0


class MalformedSessionError(ValueError):
    """The checkout session id is not a Stripe id; no request was made (HTTP 400)."""


class PaymentProviderError(Exception):
    """Stripe could not give a trustworthy answer (HTTP 503). Messages are fixed text."""


class PaymentVerifier(Protocol):
    async def verify(self, session_id: str) -> bool:
        """Return True when ``session_id`` is a paid checkout session."""
        ...


class AllowAllVerifier:
    """Used only when ``PAYMENT_REQUIRED=false`` (development and tests)."""

    async def verify(self, session_id: str) -> bool:
        return True


class StripeVerifier:
    """Checks a Checkout Session with one read-only GET to Stripe."""

    def __init__(self, secret_key: Secret, client: httpx.AsyncClient) -> None:
        match = _KEY_MODE.match(secret_key.reveal())
        if match is None:
            raise ValueError("not a Stripe secret or restricted key")
        self._mode = match.group(1)
        self._key = secret_key
        self._client = client
        for name in ("httpx", "httpcore"):  # their INFO lines contain the request URL
            logging.getLogger(name).setLevel(logging.WARNING)

    async def verify(self, session_id: str) -> bool:
        match = _SESSION_ID.fullmatch(session_id)
        if match is None:
            raise MalformedSessionError("not a checkout session id")
        if match.group(1) != self._mode:
            return False
        response = await self._get(session_id)
        if response.status_code in (400, 404):
            return False
        if response.status_code in (401, 403):
            _log.warning("stripe key rejected (%s)", PaymentProviderError.__name__)
            raise PaymentProviderError("payment provider rejected our credentials")
        if response.status_code != 200:
            raise PaymentProviderError("payment provider unavailable")
        return self._is_paid(response)

    async def _get(self, session_id: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._key.reveal()}"}
        try:
            async with asyncio.timeout(_TOTAL_SECONDS):
                return await self._client.get(
                    _BASE + session_id,
                    headers=headers,
                    timeout=_TIMEOUT,
                    follow_redirects=False,
                )
        except (httpx.HTTPError, TimeoutError):
            # Drop the cause: httpx errors can carry the request URL (and so the session id).
            raise PaymentProviderError("payment provider unreachable") from None

    def _is_paid(self, response: httpx.Response) -> bool:
        try:
            body = response.json()
        except ValueError:
            raise PaymentProviderError("payment provider sent an unreadable answer") from None
        if not isinstance(body, dict):
            raise PaymentProviderError("payment provider sent an unreadable answer")
        payment_status, status, livemode = (
            body.get("payment_status"),
            body.get("status"),
            body.get("livemode"),
        )
        if (
            not isinstance(payment_status, str)
            or not isinstance(status, str)
            or not isinstance(livemode, bool)
        ):
            raise PaymentProviderError("payment provider sent an unreadable answer")
        return (
            payment_status == "paid" and status == "complete" and livemode is (self._mode == "live")
        )
