"""End-to-end flow with a stubbed exporter, plus input-handling rules."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exit_archive.jobs.store import to_iso
from exit_archive.secrets import HarvestToken
from tests.web.helpers import (
    TOKEN,
    ZIP_NAME,
    RecordingExport,
    build,
    make_job_id,
    post_job,
    raising_export,
    settings_for,
    wait_idle,
)


def test_full_flow_post_progress_download(tmp_path: Path) -> None:
    export = RecordingExport()
    with build(settings_for(tmp_path), export=export) as client:
        resp = post_job(client, account_id="42")
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert re.fullmatch(r"/jobs/[A-Za-z0-9_-]{20,128}", location)
        wait_idle(client)

        page = client.get(location)
        assert page.status_code == 200
        assert f"{location}/download" in page.text
        assert 'http-equiv="refresh"' not in page.text
        job_id = location.rsplit("/", 1)[1]
        job = client.app.state.store.get(job_id)  # type: ignore[attr-defined]
        assert job is not None
        assert job.expires_at is not None
        assert job.expires_at[:10] in page.text

        dl = client.get(f"{location}/download")
        assert dl.status_code == 200
        assert dl.headers["content-type"] == "application/zip"
        disposition = dl.headers["content-disposition"]
        assert disposition.startswith("attachment")
        assert ZIP_NAME in disposition
        assert dl.content[:2] == b"PK"
    assert export.calls == [(HarvestToken(TOKEN), 42)]


def test_progress_page_refreshes_while_running(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        store = client.app.state.store  # type: ignore[attr-defined]
        job_id = make_job_id()
        store.create(job_id, None)
        store.mark_running(job_id)
        store.update_progress(job_id, "records", 5, 10)
        page = client.get(f"/jobs/{job_id}")
        assert 'http-equiv="refresh"' in page.text
        assert "records" in page.text
        assert "5" in page.text
        assert "/download" not in page.text


def test_failed_job_shows_safe_error_and_retry_hint(tmp_path: Path) -> None:
    with build(settings_for(tmp_path), export=raising_export) as client:
        location = post_job(client).headers["location"]
        wait_idle(client)
        page = client.get(location)
        assert page.status_code == 200
        assert "RuntimeError" in page.text
        assert TOKEN not in page.text
        assert "again" in page.text.lower()
        assert 'http-equiv="refresh"' not in page.text


def test_unknown_and_malformed_job_ids_are_404(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        for path in (f"/jobs/{make_job_id()}", f"/jobs/{make_job_id()}/download"):
            assert client.get(path).status_code == 404
        for bad in ("short", "a" * 129, "x" * 25 + "%2e%2e", "a" * 20 + "%0A"):
            assert client.get(f"/jobs/{bad}").status_code == 404
            assert client.get(f"/jobs/{bad}/download").status_code == 404


def test_malformed_job_id_never_reaches_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with build(settings_for(tmp_path)) as client:
        store = client.app.state.store  # type: ignore[attr-defined]
        lookups: list[str] = []
        monkeypatch.setattr(store, "get", lambda job_id: lookups.append(job_id))
        assert client.get("/jobs/short").status_code == 404
        assert client.get("/jobs/short/download").status_code == 404
        assert lookups == []
        assert client.get(f"/jobs/{make_job_id()}").status_code == 404
        assert len(lookups) == 1


def test_download_not_done_is_404(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        store = client.app.state.store  # type: ignore[attr-defined]
        job_id = make_job_id()
        store.create(job_id, None)
        assert client.get(f"/jobs/{job_id}/download").status_code == 404


def test_download_expired_is_410(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        store = client.app.state.store  # type: ignore[attr-defined]
        job_id = make_job_id()
        store.create(job_id, None)
        store.mark_done(
            job_id, f"archives/{job_id}/x.zip", "2026-01-01T00:00:00Z", "2026-01-08T00:00:00Z"
        )
        assert client.get(f"/jobs/{job_id}/download").status_code == 410  # past expires_at
        other = make_job_id()
        store.create(other, None)
        store.mark_done(
            other,
            f"archives/{other}/x.zip",
            "2026-01-01T00:00:00Z",
            to_iso(datetime.now(UTC) + timedelta(days=1)),
        )
        store.mark_expired(other)
        assert client.get(f"/jobs/{other}/download").status_code == 410
        assert client.get(f"/jobs/{other}").status_code == 200


def _done_job(client: object, tmp_path: Path, relative: str) -> str:
    store = client.app.state.store  # type: ignore[attr-defined]
    job_id = make_job_id()
    store.create(job_id, None)
    future = to_iso(datetime.now(UTC) + timedelta(days=3))
    store.mark_done(job_id, relative, "2026-11-25T00:00:00Z", future)
    return job_id


def test_download_refuses_symlink_and_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"PK-secret")
    with build(settings_for(tmp_path / "data")) as client:
        archives = tmp_path / "data" / "archives"
        job_id = make_job_id()
        (archives / job_id).mkdir(parents=True)
        (archives / job_id / "x.zip").symlink_to(outside)
        store = client.app.state.store  # type: ignore[attr-defined]
        store.create(job_id, None)
        future = to_iso(datetime.now(UTC) + timedelta(days=3))
        store.mark_done(job_id, f"archives/{job_id}/x.zip", "2026-11-25T00:00:00Z", future)
        assert client.get(f"/jobs/{job_id}/download").status_code == 404

        escape = _done_job(client, tmp_path, "../outside.zip")
        assert client.get(f"/jobs/{escape}/download").status_code == 404
        missing = _done_job(client, tmp_path, f"archives/{make_job_id()}/none.zip")
        assert client.get(f"/jobs/{missing}/download").status_code == 404


@pytest.mark.parametrize(
    "fields",
    [
        {"token": ""},
        {"token": "   "},
        {"token": "x" * 513},
        {"token": TOKEN, "account_id": "12a"},
        {"token": TOKEN, "account_id": "-5"},
        {"token": TOKEN, "account_id": "1" * 40},
        {"token": TOKEN, "session_id": "bad session!"},
        {"token": TOKEN, "session_id": "s" * 300},
    ],
)
def test_bad_fields_are_400_and_run_nothing(tmp_path: Path, fields: dict[str, str]) -> None:
    export = RecordingExport()
    with build(settings_for(tmp_path), export=export) as client:
        resp = client.post("/jobs", data=fields, follow_redirects=False)
        assert resp.status_code in (400, 422)  # an empty token is "missing" to FastAPI (422)
        assert export.calls == []
        assert client.app.state.store.active_job_ids() == set()  # type: ignore[attr-defined]


def test_token_is_stripped_and_empty_account_id_is_none(tmp_path: Path) -> None:
    export = RecordingExport()
    with build(settings_for(tmp_path), export=export) as client:
        resp = client.post(
            "/jobs", data={"token": f"  {TOKEN}\n", "account_id": " "}, follow_redirects=False
        )
        assert resp.status_code == 303
        wait_idle(client)
    assert export.calls == [(HarvestToken(TOKEN), None)]


def test_token_in_query_string_is_never_used(tmp_path: Path) -> None:
    export = RecordingExport()
    with build(settings_for(tmp_path), export=export) as client:
        resp = client.post(f"/jobs?token={TOKEN}", follow_redirects=False)
        assert 400 <= resp.status_code < 500
        assert TOKEN not in resp.text
        assert TOKEN not in str(resp.headers)
        resp2 = client.post(f"/jobs?token={TOKEN}", data={"account_id": "1"})
        assert 400 <= resp2.status_code < 500
        assert TOKEN not in resp2.text
        wait_idle(client)
        for path in ("/", "/start", "/privacy"):
            assert TOKEN not in client.get(f"{path}?token={TOKEN}").text
    assert export.calls == []


def test_session_id_goes_into_hidden_field_escaped(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        page = client.get('/start?session_id="><script>alert(1)</script>')
        assert "<script" not in page.text
        ok = client.get("/start?session_id=cs_test_a1B2")
        assert re.search(r'<input[^>]*type="hidden"[^>]*value="cs_test_a1B2"', ok.text)
        assert 'type="password"' in ok.text


def test_healthz_and_privacy(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        privacy = client.get("/privacy")
        assert privacy.status_code == 200
        assert "draft" in privacy.text.lower()


def test_landing_buy_button_target(tmp_path: Path) -> None:
    free = settings_for(tmp_path, payment_required=False)
    with build(free) as client:
        assert 'href="/start"' in client.get("/").text
    paid = settings_for(
        tmp_path, payment_required=True, stripe_payment_link_url="https://buy.example/abc"
    )
    with build(paid) as client:
        page = client.get("/").text
        assert 'href="https://buy.example/abc"' in page
        assert "$49" in page
    unconfigured = settings_for(tmp_path, payment_required=True)
    with build(unconfigured) as client:
        assert client.get("/").status_code == 200
