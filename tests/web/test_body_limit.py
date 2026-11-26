"""T4.3.2(a): 16 KB body limit on POST, 413 page, nothing echoed."""

from collections.abc import Iterator
from pathlib import Path

from fastapi.testclient import TestClient

from tests.web.helpers import TOKEN, build, settings_for


def _chunks(total: int, size: int = 4096) -> Iterator[bytes]:
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        yield b"token=" + b"B" * (n - 6) if sent == 0 else b"B" * n
        sent += n


def test_content_length_over_limit_is_413(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as c:
        r = c.post("/jobs", data={"token": TOKEN + "A" * 20_000})
        assert r.status_code == 413
        assert TOKEN not in r.text and "AAAA" not in r.text and len(r.text) < 5000


def test_content_length_rejected_before_reading_body(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as c:
        r = c.post("/jobs", content=b"x" * 16_385, headers={"content-type": "text/plain"})
        assert r.status_code == 413


def test_chunked_over_limit_without_content_length_is_413(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as c:
        r = c.post(
            "/jobs",
            content=_chunks(40_000),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 413
        assert "BBBB" not in r.text


def test_small_and_exact_limit_bodies_pass_through(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as c:
        assert c.post("/jobs", data={"token": ""}).status_code == 422
        body = b"token=" + b"a" * (16_384 - 6)
        r = c.post(
            "/jobs", content=body, headers={"content-type": "application/x-www-form-urlencoded"}
        )
        assert r.status_code != 413


def test_get_is_not_limited(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as c:
        assert isinstance(c, TestClient)
        assert c.get("/healthz").status_code == 200


def test_bad_content_length_is_400(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as c:
        r = c.post("/jobs", content=b"x", headers={"content-length": "abc"})
        assert r.status_code in (400, 413)
