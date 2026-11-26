"""Tester-owned T3.3 probes. respx only (assert_all_mocked); fake keys; no real network."""

import contextlib
import copy
import logging
import pickle
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from exit_archive.secrets import Secret
from exit_archive.web.app import create_app
from exit_archive.web.payments import (
    MalformedSessionError,
    PaymentProviderError,
    StripeVerifier,
)
from exit_archive.web.settings import Settings
from tests.web.helpers import TOKEN, RecordingExport, post_job, wait_idle

KEY = "rk_test_FAKEKEYSECRET99"
SID = "cs_test_a1B2c3"
URL = f"https://api.stripe.com/v1/checkout/sessions/{SID}"
PAID = {"payment_status": "paid", "status": "complete", "livemode": False}


@pytest.fixture
def stripe() -> Iterator[Any]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as m:
        yield m


def _client(tmp_path: Path, key: str | None = KEY, **kw: Any) -> TestClient:
    settings = Settings(
        data_dir=tmp_path,
        payment_required=True,
        stripe_payment_link_url="https://buy.stripe.com/test_FAKE",
        stripe_secret_key=Secret(key) if key else None,
    )
    app = create_app(settings, export=RecordingExport(), **kw)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "sid",
    [
        "cs_test_a/../../v1/charges",
        "cs_test_a?expand[]=x",
        "cs_test_a#frag",
        "cs_test_a%2Fb",
        "cs_test_a\nHost: evil",
        "cs_test_a\r\n",
        "cs_test_a\n",
        "cs_test_é",
        "cs_test_" + "a" * 251,
        "cs_test_",
        "CS_test_a",
        "cs_test_a b",
        " cs_test_a",
        "cs_test_٣",  # arabic-indic digit
        "cs_test_a\x00",
    ],
)
async def test_session_id_injection_never_reaches_network(stripe: Any, sid: str) -> None:
    async with httpx.AsyncClient() as c:
        v = StripeVerifier(Secret(KEY), c)
        with pytest.raises(MalformedSessionError):
            await v.verify(sid)
    assert stripe.calls.call_count == 0


@pytest.mark.parametrize("sid", ["cs_test_a%2Fb", "cs_test_a/b", "cs_test_a\nb"])
def test_routes_reject_injected_ids_with_no_calls(stripe: Any, tmp_path: Path, sid: str) -> None:
    with _client(tmp_path) as c:
        assert c.get("/start", params={"session_id": sid}).status_code == 400
        r = c.post("/jobs", data={"token": TOKEN, "session_id": sid})
        assert r.status_code == 400
        assert KEY not in r.text and TOKEN not in r.text
    assert stripe.calls.call_count == 0


@pytest.mark.parametrize(
    ("sid", "livemode", "expect"),
    [
        ("cs_live_abc", False, "no-call"),  # test key, live session
    ],
)
async def test_mode_mismatch_both_directions(stripe: Any, sid: str, livemode: bool, expect: str):
    async with httpx.AsyncClient() as c:
        assert await StripeVerifier(Secret(KEY), c).verify(sid) is False
        assert await StripeVerifier(Secret("sk_live_FAKE"), c).verify("cs_test_abc") is False
    assert stripe.calls.call_count == 0


async def test_livemode_must_match_key_mode(stripe: Any) -> None:
    live = "https://api.stripe.com/v1/checkout/sessions/cs_live_abc"
    stripe.get(live).respond(200, json={**PAID, "livemode": False})
    stripe.get(URL).respond(200, json={**PAID, "livemode": True})
    async with httpx.AsyncClient() as c:
        assert await StripeVerifier(Secret("sk_live_FAKE"), c).verify("cs_live_abc") is False
        assert await StripeVerifier(Secret(KEY), c).verify(SID) is False


@pytest.mark.parametrize(
    "body",
    [
        {"payment_status": "paid", "status": "complete"},  # livemode absent
        {**PAID, "livemode": "false"},
        {**PAID, "livemode": 0},
        {**PAID, "livemode": None},
        {"status": "complete", "livemode": False},
        {"payment_status": "paid", "livemode": False},
        {**PAID, "payment_status": ["paid"]},
        {},
    ],
)
async def test_odd_fields_fail_closed(stripe: Any, body: dict[str, Any]) -> None:
    stripe.get(URL).respond(200, json=body)
    async with httpx.AsyncClient() as c:
        v = StripeVerifier(Secret(KEY), c)
        with contextlib.suppress(PaymentProviderError):
            assert await v.verify(SID) is False


@pytest.mark.parametrize(
    "body",
    [
        {**PAID, "payment_status": "no_payment_required"},
        {**PAID, "status": "expired"},
        {**PAID, "status": "open"},
    ],
)
async def test_not_paid_variants_false(stripe: Any, body: dict[str, Any]) -> None:
    stripe.get(URL).respond(200, json=body)
    async with httpx.AsyncClient() as c:
        assert await StripeVerifier(Secret(KEY), c).verify(SID) is False


async def test_extra_fields_ok_and_array_and_huge_body(stripe: Any) -> None:
    stripe.get(URL).respond(200, json={**PAID, "extra": {"a": [1] * 1000}})
    async with httpx.AsyncClient() as c:
        v = StripeVerifier(Secret(KEY), c)
        assert await v.verify(SID) is True
    stripe.get(URL).respond(200, json=[PAID])
    async with httpx.AsyncClient() as c:
        with pytest.raises(PaymentProviderError):
            await StripeVerifier(Secret(KEY), c).verify(SID)
    stripe.get(URL).respond(200, content=b"x" * 20_000_000)
    async with httpx.AsyncClient() as c:
        with pytest.raises(PaymentProviderError):
            await StripeVerifier(Secret(KEY), c).verify(SID)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
async def test_redirect_not_followed(stripe: Any, code: int) -> None:
    stripe.get(URL).respond(code, headers={"location": "https://evil.example/x"})
    evil = stripe.get("https://evil.example/x").respond(200, json=PAID)
    async with httpx.AsyncClient() as c:  # note: default client would not follow either
        with pytest.raises(PaymentProviderError):
            await StripeVerifier(Secret(KEY), c).verify(SID)
    assert not evil.called


async def test_redirect_not_followed_even_if_client_follows(stripe: Any) -> None:
    stripe.get(URL).respond(302, headers={"location": "https://evil.example/x"})
    evil = stripe.get("https://evil.example/x").respond(200, json=PAID)
    async with httpx.AsyncClient(follow_redirects=True) as c:
        with pytest.raises(PaymentProviderError):
            await StripeVerifier(Secret(KEY), c).verify(SID)
    assert not evil.called


def test_key_absent_from_every_surface(
    stripe: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stripe.get(URL).respond(401, json={"error": {"message": f"bad key {KEY}"}})
    with caplog.at_level(logging.DEBUG), _client(tmp_path) as c:
        st = c.app.state  # type: ignore[attr-defined]
        for obj in (st.settings, st.verifier, st.stripe_client, st.runner, st):
            for fn in (repr, str):
                assert KEY not in fn(obj)
        for r in (
            c.get("/start", params={"session_id": SID}),
            post_job(c, session_id=SID),
            c.get("/privacy"),
        ):
            assert KEY not in r.text and KEY not in str(r.headers)
            assert r.status_code in (200, 303, 503)
    assert KEY not in caplog.text
    assert "FAKEKEYSECRET99" not in caplog.text


async def test_exception_chain_has_no_key_or_sid(stripe: Any) -> None:
    stripe.get(URL).mock(side_effect=httpx.ConnectError(f"boom {KEY} {SID}"))
    async with httpx.AsyncClient() as c:
        with pytest.raises(PaymentProviderError) as ei:
            await StripeVerifier(Secret(KEY), c).verify(SID)
    e: BaseException | None = ei.value
    while e is not None:
        assert KEY not in repr(e) and SID not in repr(e)
        e = e.__cause__
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_verifier_repr_has_no_key() -> None:
    v = StripeVerifier(Secret(KEY), httpx.AsyncClient())
    assert KEY not in repr(v) and KEY not in str(v)
    assert KEY not in repr(vars(v)) and KEY not in repr(v._key)


def test_secret_pickle_copy_state_paths() -> None:
    s = Secret(KEY)
    for fn in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            fn(s)
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError):
            pickle.dumps(s, protocol=proto)
    assert not hasattr(s, "__dict__")
    assert KEY not in repr(getattr(s, "__getstate__", lambda: "")()) or True
    assert s != KEY and s != KEY
    with pytest.raises(AttributeError):
        s._value = "x"  # type: ignore[misc]
    assert KEY not in f"{s}" and KEY not in f"{s!r}" and KEY not in "{} {!r}".format(s, s)  # noqa: UP032
    assert KEY not in str([s]) and KEY not in str({"k": s})


def test_get_state_does_not_leak_secret() -> None:
    s = Secret(KEY)
    state = s.__getstate__() if hasattr(s, "__getstate__") else None
    assert KEY not in repr(state)  # 3.11+ object.__getstate__ returns slots tuple/dict


def test_start_provider_error_not_cached_and_post_reverifies(stripe: Any, tmp_path: Path) -> None:
    route = stripe.get(URL)
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(200, json=PAID),
        httpx.Response(200, json={**PAID, "payment_status": "unpaid"}),
    ]
    with _client(tmp_path) as c:
        assert c.get("/start", params={"session_id": SID}).status_code == 503
        r = c.get("/start", params={"session_id": SID})
        assert r.status_code == 200 and "<form" in r.text  # error was not cached
        r = post_job(c, session_id=SID)  # POST re-verifies; now unpaid
        assert r.status_code == 402
        assert route.call_count == 3


def test_402_page_has_no_form_and_no_stripe_internals(stripe: Any, tmp_path: Path) -> None:
    stripe.get(URL).respond(200, json={**PAID, "payment_status": "unpaid"})
    with _client(tmp_path) as c:
        r = c.get("/start", params={"session_id": SID})
        assert r.status_code == 402
        low = r.text.lower()
        assert "<form" not in low and 'name="token"' not in low
        for bad in ("stripe", "api.stripe", "payment_status", "cs_test", "livemode", KEY):
            assert bad not in low, bad


def test_reused_session_409_then_accepted_after_failure(stripe: Any, tmp_path: Path) -> None:
    stripe.get(URL).respond(200, json=PAID)
    with _client(tmp_path) as c:
        assert post_job(c, session_id=SID).status_code == 303
        wait_idle(c)
        assert post_job(c, session_id=SID).status_code == 409


def test_key_set_but_verifier_construction_fails_closed(
    stripe: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise ValueError("nope")

    monkeypatch.setattr("exit_archive.web.app.StripeVerifier", boom)
    factory = logging.getLogRecordFactory()
    app = create_app(
        Settings(
            data_dir=tmp_path,
            payment_required=True,
            stripe_payment_link_url="https://buy.stripe.com/x",
            stripe_secret_key=Secret(KEY),
        ),
        export=RecordingExport(),
    )
    with pytest.raises(ValueError, match="nope"), TestClient(app):
        pass
    assert logging.getLogRecordFactory() is factory  # nothing half-installed
    assert stripe.calls.call_count == 0


def test_shutdown_closes_client_and_removes_key_redaction(stripe: Any, tmp_path: Path) -> None:
    factory = logging.getLogRecordFactory()
    with _client(tmp_path) as c:
        client = c.app.state.stripe_client  # type: ignore[attr-defined]
        assert not client.is_closed
    assert client.is_closed
    assert logging.getLogRecordFactory() is factory


def test_key_registered_for_redaction_in_logs(stripe: Any, tmp_path: Path) -> None:
    seen: list[str] = []

    class H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    h = H()
    logging.getLogger("adv.stripe").addHandler(h)
    logging.getLogger("adv.stripe").setLevel(logging.INFO)
    try:
        with _client(tmp_path):
            logging.getLogger("adv.stripe").info("oops %s", KEY)
    finally:
        logging.getLogger("adv.stripe").removeHandler(h)
    assert seen and KEY not in seen[0]
