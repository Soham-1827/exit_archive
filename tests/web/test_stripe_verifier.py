"""StripeVerifier against respx mocks only. Nothing here touches the network."""

import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from exit_archive.secrets import Secret
from exit_archive.web.payments import (
    MalformedSessionError,
    PaymentProviderError,
    StripeVerifier,
)

KEY = "rk_test_FAKEFAKE"
SID = "cs_test_a1B2c3"
URL = f"https://api.stripe.com/v1/checkout/sessions/{SID}"


def _body(**over: Any) -> dict[str, Any]:
    return {"id": SID, "payment_status": "paid", "status": "complete", "livemode": False, **over}


@pytest.fixture
def mock() -> Any:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as m:
        yield m


@pytest.fixture
async def verifier() -> AsyncIterator[StripeVerifier]:
    async with httpx.AsyncClient() as client:
        yield StripeVerifier(Secret(KEY), client)


async def test_paid_and_request_shape(mock: Any, verifier: StripeVerifier) -> None:
    route = mock.get(URL).respond(200, json=_body())
    assert await verifier.verify(SID) is True
    assert route.call_count == 1 and mock.calls.call_count == 1
    req = route.calls[0].request
    assert req.method == "GET"
    assert str(req.url) == URL
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.content == b""
    timeout = req.extensions["timeout"]
    values = [timeout[k] for k in ("connect", "read", "write", "pool")]
    assert all(v is not None for v in values)
    assert sum(values) <= 10


@pytest.mark.parametrize(
    "body",
    [
        _body(payment_status="unpaid"),
        _body(payment_status="no_payment_required"),
        _body(status="open"),
        _body(status="expired"),
        _body(livemode=True),
    ],
)
async def test_not_paid_returns_false(mock: Any, verifier: StripeVerifier, body: dict) -> None:
    mock.get(URL).respond(200, json=body)
    assert await verifier.verify(SID) is False


@pytest.mark.parametrize("code", [404, 400])
async def test_stripe_404_400_is_false(mock: Any, verifier: StripeVerifier, code: int) -> None:
    mock.get(URL).respond(code, json={"error": {"message": "No such session"}})
    assert await verifier.verify(SID) is False


@pytest.mark.parametrize(
    "sid",
    ["", "cs_", "cs_test_", "pi_test_abc", "cs_prod_abc", "cs_test_a-b", "cs_test_a b",
     "cs_test_" + "a" * 251, "cs_test_abc\n", "../cs_test_a", "cs_test_a/../x", "CS_test_a",
     "cs_test_é"],
)  # fmt: skip
async def test_malformed_ids_make_no_call(mock: Any, verifier: StripeVerifier, sid: str) -> None:
    with pytest.raises(MalformedSessionError):
        await verifier.verify(sid)
    assert mock.calls.call_count == 0


async def test_max_length_id_is_accepted(mock: Any, verifier: StripeVerifier) -> None:
    sid = "cs_test_" + "a" * 250
    mock.get(f"https://api.stripe.com/v1/checkout/sessions/{sid}").respond(200, json=_body())
    assert await verifier.verify(sid) is True


async def test_mode_mismatch_makes_no_call(mock: Any, verifier: StripeVerifier) -> None:
    assert await verifier.verify("cs_live_abc") is False
    async with httpx.AsyncClient() as client:
        live = StripeVerifier(Secret("sk_live_FAKE"), client)
        assert await live.verify(SID) is False
    assert mock.calls.call_count == 0


async def test_live_key_live_session(mock: Any) -> None:
    mock.get("https://api.stripe.com/v1/checkout/sessions/cs_live_x").respond(
        200, json=_body(livemode=True)
    )
    async with httpx.AsyncClient() as client:
        v = StripeVerifier(Secret("sk_live_FAKE"), client)
        assert await v.verify("cs_live_x") is True
        mock.get("https://api.stripe.com/v1/checkout/sessions/cs_live_y").respond(
            200, json=_body(livemode=False)
        )
        assert await v.verify("cs_live_y") is False


@pytest.mark.parametrize("code", [401, 403])
async def test_key_rejected_raises_and_logs_no_secret(
    mock: Any, verifier: StripeVerifier, caplog: pytest.LogCaptureFixture, code: int
) -> None:
    mock.get(URL).respond(code, json={"error": {"message": f"bad key {KEY}"}})
    with caplog.at_level(logging.DEBUG), pytest.raises(PaymentProviderError):
        await verifier.verify(SID)
    assert "stripe key rejected" in caplog.text
    assert KEY not in caplog.text and SID not in caplog.text


@pytest.mark.parametrize("code", [429, 500, 502, 503, 402, 302])
async def test_other_statuses_raise(mock: Any, verifier: StripeVerifier, code: int) -> None:
    mock.get(URL).respond(code, headers={"location": "https://evil.example/"})
    with pytest.raises(PaymentProviderError):
        await verifier.verify(SID)
    assert mock.calls.call_count == 1  # no retry, no redirect followed


@pytest.mark.parametrize(
    "effect",
    [httpx.ReadTimeout("t"), httpx.ConnectTimeout("t"), httpx.ConnectError("c"),
     httpx.RemoteProtocolError("p")],
)  # fmt: skip
async def test_transport_errors_raise(
    mock: Any, verifier: StripeVerifier, effect: Exception
) -> None:
    mock.get(URL).mock(side_effect=effect)
    with pytest.raises(PaymentProviderError) as info:
        await verifier.verify(SID)
    assert mock.calls.call_count == 1
    assert info.value.__cause__ is None and info.value.__suppress_context__


@pytest.mark.parametrize(
    "content",
    [b"<html>", b"[]", b"null", b'"paid"',
     b'{"payment_status": "paid"}',
     b'{"payment_status": "paid", "status": "complete"}',
     b'{"payment_status": "paid", "status": "complete", "livemode": "false"}',
     b'{"payment_status": 1, "status": "complete", "livemode": false}'],
)  # fmt: skip
async def test_malformed_bodies_raise(mock: Any, verifier: StripeVerifier, content: bytes) -> None:
    mock.get(URL).respond(200, content=content)
    with pytest.raises(PaymentProviderError):
        await verifier.verify(SID)


async def test_verify_returns_strict_bool(mock: Any, verifier: StripeVerifier) -> None:
    mock.get(URL).respond(200, json=_body())
    assert type(await verifier.verify(SID)) is bool


async def test_no_info_logs_even_on_success(
    mock: Any, verifier: StripeVerifier, caplog: pytest.LogCaptureFixture
) -> None:
    mock.get(URL).respond(200, json=_body())
    with caplog.at_level(logging.DEBUG):
        await verifier.verify(SID)
    assert not re.search(r"cs_test|api\.stripe|rk_test", caplog.text)


def test_non_stripe_key_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="Stripe"):
        StripeVerifier(Secret("pk_test_FAKE"), httpx.AsyncClient())
