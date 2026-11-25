"""No response body from any route contains the submitted token."""

from pathlib import Path

import pytest

from tests.web.helpers import (
    TOKEN,
    build,
    make_job_id,
    post_job,
    raising_export,
    settings_for,
    wait_idle,
)


def test_422_missing_field_is_4xx_without_token(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        resp = client.post("/jobs", data={"account_id": TOKEN})  # token field missing
        assert 400 <= resp.status_code < 500
        assert TOKEN not in resp.text
        resp = client.post("/jobs", json={"token": TOKEN})
        assert 400 <= resp.status_code < 500
        assert TOKEN not in resp.text


def test_400_path_does_not_echo(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        resp = client.post("/jobs", data={"token": TOKEN, "account_id": f"{TOKEN}x"})
        assert resp.status_code == 400
        assert TOKEN not in resp.text
        resp = client.post("/jobs", data={"token": TOKEN, "session_id": f"{TOKEN} !"})
        assert resp.status_code == 400
        assert TOKEN not in resp.text


def test_409_and_402_paths(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        post_job(client, session_id="cs_1")
        resp = post_job(client, session_id="cs_1")
        assert resp.status_code == 409
        assert TOKEN not in resp.text
        wait_idle(client)
    with build(settings_for(tmp_path / "b", payment_required=True)) as client:
        resp = post_job(client, session_id="cs_1")
        assert resp.status_code == 402
        assert TOKEN not in resp.text


def test_500_path_generic_and_logs_only_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with build(settings_for(tmp_path)) as client:

        async def boom(*args: object, **kwargs: object) -> str:
            raise RuntimeError(f"explode {TOKEN}")

        monkeypatch.setattr(client.app.state.runner, "submit", boom)  # type: ignore[attr-defined]
        with caplog.at_level("DEBUG"):
            resp = post_job(client)
        assert resp.status_code == 500
        assert TOKEN not in resp.text
        assert "explode" not in resp.text
        assert TOKEN not in caplog.text
        assert "RuntimeError" in caplog.text


def test_exporter_exception_with_token_never_surfaces(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("DEBUG"), build(settings_for(tmp_path), export=raising_export) as client:
        resp = post_job(client)
        wait_idle(client)
        bodies = [resp.text, client.get(resp.headers["location"]).text]
        bodies.append(client.get(resp.headers["location"] + "/download").text)
        bodies.append(client.get(f"/jobs/{make_job_id()}").text)
    assert all(TOKEN not in b for b in bodies)
    assert TOKEN not in caplog.text


def test_http_errors_use_generic_pages(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        nf = client.get(f"/nope?token={TOKEN}")
        assert nf.status_code == 404
        assert TOKEN not in nf.text
        assert "/nope" not in nf.text
        assert client.get("/jobs").status_code in (404, 405)
        bad = client.get("/jobs", params={"token": TOKEN})
        assert TOKEN not in bad.text
        method = client.put("/healthz")
        assert method.status_code == 405
        assert "text/html" in method.headers["content-type"]
