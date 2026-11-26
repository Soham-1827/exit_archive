"""T4.3.2(c): Origin/Referer check on POST."""

from pathlib import Path

import pytest

from tests.web.helpers import build, settings_for


def _post(c, **headers):  # type: ignore[no-untyped-def]
    return c.post("/jobs", data={"token": ""}, headers=headers)  # 422 when allowed


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://testserver:9999"},
        {"Origin": ""},
        {"Referer": "https://evil.example/page"},
        {"Origin": "http://evil.example", "Referer": "http://testserver/"},
    ],
)
def test_foreign_origin_is_403(tmp_path: Path, headers: dict[str, str]) -> None:
    with build(settings_for(tmp_path)) as c:
        r = _post(c, **headers)
        assert r.status_code == 403, headers
        assert "evil" not in r.text


def test_same_host_and_missing_headers_allowed(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as c:
        assert _post(c).status_code == 422
        assert _post(c, Origin="http://testserver").status_code == 422
        assert _post(c, Referer="http://testserver/start?session_id=1").status_code == 422
        assert _post(c, Origin="HTTP://TESTSERVER").status_code == 422


def test_public_base_url_origin_is_authoritative(tmp_path: Path) -> None:
    cfg = settings_for(tmp_path, public_base_url="https://archive.example.com")
    with build(cfg) as c:
        assert _post(c, Origin="https://archive.example.com").status_code == 422
        assert _post(c, Referer="https://archive.example.com/start").status_code == 422
        assert _post(c, Origin="http://testserver").status_code == 403
        assert _post(c, Origin="https://archive.example.com:8443").status_code == 403
        assert _post(c, Origin="http://archive.example.com").status_code == 403


def test_get_requests_are_not_checked(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as c:
        assert c.get("/healthz", headers={"Origin": "https://evil.example"}).status_code == 200
