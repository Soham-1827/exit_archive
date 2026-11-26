"""T4.3.1: security headers on every response; no-store where pages carry capabilities."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exit_archive.web.app import create_app
from tests.web.helpers import RecordingExport, post_job, settings_for, wait_idle

EXPECTED = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    ),
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


def _check(r: object) -> None:
    headers = r.headers  # type: ignore[attr-defined]
    for name, value in EXPECTED.items():
        assert headers.get(name) == value, name


@pytest.fixture
def client(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = create_app(settings_for(tmp_path), export=RecordingExport())

    async def boom() -> None:
        raise RuntimeError("boom")

    app.add_api_route("/boom", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _job(client: TestClient) -> str:
    r = post_job(client)
    wait_idle(client)
    return str(r.headers["location"])


def test_headers_on_every_kind_of_response(client: TestClient) -> None:
    loc = _job(client)
    for path in (
        "/",
        "/start",
        "/privacy",
        "/healthz",
        loc,
        loc + "/download",
        "/static/app.css",
        "/nope",
        "/boom",
        "/jobs/bad",
        "/jobs",
    ):
        _check(client.get(path))
    _check(client.post("/jobs", data={"token": "x"}))
    _check(client.post("/jobs", content=b"a" * 20_000))  # 413


def test_status_codes_covered(client: TestClient) -> None:
    assert client.get("/nope").status_code == 404
    assert client.get("/boom").status_code == 500
    assert client.post("/jobs", content=b"a" * 20_000).status_code == 413


def test_no_store_on_start_jobs_downloads_and_posts(client: TestClient) -> None:
    loc = _job(client)
    for path in ("/start", loc, loc + "/download", "/jobs/missing"):
        assert client.get(path).headers["cache-control"] == "no-store", path
    assert client.post("/jobs", data={"token": ""}).headers["cache-control"] == "no-store"
    assert client.post("/jobs", content=b"a" * 20_000).headers["cache-control"] == "no-store"
    assert client.post("/privacy").headers["cache-control"] == "no-store"


def test_no_store_not_forced_on_public_pages(client: TestClient) -> None:
    assert client.get("/").headers.get("cache-control") != "no-store"


def test_templates_work_under_csp(client: TestClient) -> None:
    for path in ("/", "/start", "/privacy", _job(client)):
        html = client.get(path).text.lower()
        assert "<script" not in html and "<style" not in html and " style=" not in html
        assert "onclick=" not in html and "javascript:" not in html
    assert "/static/app.css" in client.get("/").text
    css = client.get("/static/app.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
