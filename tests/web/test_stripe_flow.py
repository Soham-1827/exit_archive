"""Web flow with the real StripeVerifier, Stripe mocked by respx. No real network."""

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from exit_archive.secrets import Secret
from exit_archive.web.app import create_app
from exit_archive.web.payments import StripeVerifier
from exit_archive.web.settings import Settings
from tests.web.helpers import RecordingExport, post_job, wait_idle

KEY = "rk_test_FAKEFAKE"
SID = "cs_test_a1B2c3"
URL = f"https://api.stripe.com/v1/checkout/sessions/{SID}"
PAID = {"payment_status": "paid", "status": "complete", "livemode": False}


@pytest.fixture
def stripe() -> Iterator[Any]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as m:
        yield m


def _settings(tmp_path: Path, key: str | None = KEY) -> Settings:
    return Settings(
        data_dir=tmp_path,
        payment_required=True,
        stripe_payment_link_url="https://buy.stripe.com/test_FAKE",
        stripe_secret_key=Secret(key) if key else None,
    )


def _client(tmp_path: Path, export: Any = None) -> TestClient:
    app = create_app(_settings(tmp_path), export=export or RecordingExport())
    return TestClient(app, raise_server_exceptions=False)


def _jobs(client: TestClient) -> int:
    """Count every job row, whatever its status."""
    db = client.app.state.settings.data_dir / "jobs.sqlite3"  # type: ignore[attr-defined]
    with closing(sqlite3.connect(db)) as conn:
        return int(conn.execute("SELECT count(*) FROM jobs").fetchone()[0])


def test_paid_creates_job(stripe: Any, tmp_path: Path) -> None:
    stripe.get(URL).respond(200, json=PAID)
    with _client(tmp_path) as client:
        resp = post_job(client, session_id=SID)
        assert resp.status_code == 303
        wait_idle(client)
        assert _jobs(client) == 1
    assert stripe.calls.call_count == 1


@pytest.mark.parametrize("body", [{**PAID, "payment_status": "unpaid"}, {**PAID, "status": "open"}])
def test_unpaid_or_open_402_no_job(stripe: Any, tmp_path: Path, body: dict) -> None:
    stripe.get(URL).respond(200, json=body)
    with _client(tmp_path) as client:
        assert post_job(client, session_id=SID).status_code == 402
        assert _jobs(client) == 0


def test_stripe_404_402(stripe: Any, tmp_path: Path) -> None:
    stripe.get(URL).respond(404, json={"error": {}})
    with _client(tmp_path) as client:
        assert post_job(client, session_id=SID).status_code == 402
        assert _jobs(client) == 0


@pytest.mark.parametrize("sid", ["cs_test_", "pi_test_abc", "cs_test_a-b"])
def test_malformed_400_no_calls(stripe: Any, tmp_path: Path, sid: str) -> None:
    with _client(tmp_path) as client:
        assert post_job(client, session_id=sid).status_code == 400
        assert _jobs(client) == 0
    assert stripe.calls.call_count == 0


def test_mode_mismatch_402_no_calls(stripe: Any, tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert post_job(client, session_id="cs_live_abc").status_code == 402
        assert _jobs(client) == 0
    assert stripe.calls.call_count == 0


def test_401_503_and_no_key_in_log_or_response(
    stripe: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stripe.get(URL).respond(401, json={"error": {"message": f"Invalid API Key {KEY}"}})
    with caplog.at_level(logging.DEBUG), _client(tmp_path) as client:
        resp = post_job(client, session_id=SID)
        page = client.get(f"/start?session_id={SID}")
        assert _jobs(client) == 0
    assert resp.status_code == 503 and page.status_code == 503
    for text in (caplog.text, resp.text, page.text, str(resp.headers)):
        assert KEY not in text
    assert "stripe key rejected" in caplog.text


@pytest.mark.parametrize(
    "effect",
    [httpx.ReadTimeout("t"), httpx.ConnectError("c"), None],
)
def test_5xx_timeout_transport_503(stripe: Any, tmp_path: Path, effect: Exception | None) -> None:
    if effect is None:
        stripe.get(URL).respond(500, text="boom")
    else:
        stripe.get(URL).mock(side_effect=effect)
    with _client(tmp_path) as client:
        assert post_job(client, session_id=SID).status_code == 503
        assert _jobs(client) == 0


def test_second_submit_409_after_done_and_accepted_after_failed(
    stripe: Any, tmp_path: Path
) -> None:
    stripe.get(URL).respond(200, json=PAID)
    with _client(tmp_path) as client:
        first = post_job(client, session_id=SID)
        assert first.status_code == 303
        wait_idle(client)
        job_id = first.headers["location"].rsplit("/", 1)[1]
        store = client.app.state.store  # type: ignore[attr-defined]
        assert store.get(job_id).status == "done"
        assert post_job(client, session_id=SID).status_code == 409
        store.mark_failed(job_id, "boom")
        again = post_job(client, session_id=SID)
        assert again.status_code == 303
        wait_idle(client)


def test_start_paid_shows_form_with_hidden_session(stripe: Any, tmp_path: Path) -> None:
    stripe.get(URL).respond(200, json=PAID)
    with _client(tmp_path) as client:
        resp = client.get(f"/start?session_id={SID}")
    assert resp.status_code == 200
    assert 'type="password"' in resp.text
    assert re.search(rf'<input[^>]*type="hidden"[^>]*value="{SID}"', resp.text)


def test_start_unpaid_402_without_form(stripe: Any, tmp_path: Path) -> None:
    stripe.get(URL).respond(200, json={**PAID, "payment_status": "unpaid"})
    with _client(tmp_path) as client:
        resp = client.get(f"/start?session_id={SID}")
    assert resp.status_code == 402
    assert 'name="token"' not in resp.text and 'type="password"' not in resp.text
    text = resp.text.lower()
    assert "payment" in text and "confirm" in text
    assert "stripe" not in text and KEY not in resp.text


@pytest.mark.parametrize("query", ["", "?session_id=", "?session_id=cs_test_", "?session_id=x%00"])
def test_start_malformed_or_missing_400(stripe: Any, tmp_path: Path, query: str) -> None:
    with _client(tmp_path) as client:
        resp = client.get(f"/start{query}")
    assert resp.status_code == 400
    assert 'name="token"' not in resp.text
    assert stripe.calls.call_count == 0


def test_start_provider_error_503(stripe: Any, tmp_path: Path) -> None:
    stripe.get(URL).respond(500)
    with _client(tmp_path) as client:
        resp = client.get(f"/start?session_id={SID}")
    assert resp.status_code == 503
    assert 'name="token"' not in resp.text


def test_start_not_required_unchanged(stripe: Any, tmp_path: Path) -> None:
    cfg = Settings(data_dir=tmp_path, payment_required=False)
    with TestClient(create_app(cfg, export=RecordingExport())) as client:
        resp = client.get("/start")
    assert resp.status_code == 200 and 'type="password"' in resp.text
    assert stripe.calls.call_count == 0


def test_no_key_and_no_verifier_fails_closed(stripe: Any, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, key=None), export=RecordingExport())
    with TestClient(app, raise_server_exceptions=False) as client:
        assert post_job(client, session_id=SID).status_code == 402
        assert client.get(f"/start?session_id={SID}").status_code == 402
        assert _jobs(client) == 0
    assert stripe.calls.call_count == 0


class _Yes:
    async def verify(self, session_id: str) -> bool:
        return True


def test_injected_verifier_wins(stripe: Any, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), export=RecordingExport(), payments=_Yes())
    with TestClient(app, raise_server_exceptions=False) as client:
        assert isinstance(client.app.state.verifier, _Yes)  # type: ignore[attr-defined]
        assert post_job(client, session_id="cs_anything").status_code == 303
        wait_idle(client)
    assert stripe.calls.call_count == 0


def test_lifespan_builds_verifier_and_closes_its_client(stripe: Any, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), export=RecordingExport())
    with TestClient(app) as client:
        verifier = client.app.state.verifier  # type: ignore[attr-defined]
        assert isinstance(verifier, StripeVerifier)
        http = client.app.state.stripe_client  # type: ignore[attr-defined]
        assert not http.is_closed
    assert http.is_closed


def test_lifespan_registers_key_for_redaction_and_removes_it(tmp_path: Path) -> None:
    original = logging.getLogRecordFactory()
    app = create_app(_settings(tmp_path), export=RecordingExport())
    with TestClient(app):
        record = logging.getLogger("x").makeRecord("x", 30, "f", 1, f"oops {KEY}", (), None)
        assert KEY not in record.getMessage()
    assert logging.getLogRecordFactory() is original
