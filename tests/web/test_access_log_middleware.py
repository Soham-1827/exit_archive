"""T4.3.3: own access log (path only, ids masked) and a silent uvicorn.access."""

import logging
import re
from pathlib import Path

import pytest

from tests.web.helpers import TOKEN, build, post_job, settings_for, wait_idle

ACCESS = "exit_archive.access"


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == ACCESS]


def test_query_strings_and_job_ids_never_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with build(settings_for(tmp_path)) as c:
        r = post_job(c)
        wait_idle(c)
        loc = r.headers["location"]
        job_id = loc.rsplit("/", 1)[1]
        c.get("/jobs?token=SECRET&x=1")
        c.get("/start?session_id=cs_test_abc")
        c.get(loc)
        c.get(loc + "/download")
        c.get("/healthz")
    lines = _lines(caplog)
    blob = "\n".join(rec.getMessage() for rec in caplog.records) + caplog.text
    for needle in ("SECRET", "cs_test_abc", job_id, TOKEN, "x=1", "?"):
        assert needle not in blob, needle
    assert any(re.fullmatch(r"GET /jobs 405 \d+ms", ln) for ln in lines), lines
    assert any(re.fullmatch(r"GET /start 200 \d+ms", ln) for ln in lines)
    assert any(re.fullmatch(r"GET /jobs/<id> 200 \d+ms", ln) for ln in lines)
    assert any(re.fullmatch(r"GET /jobs/<id>/download 200 \d+ms", ln) for ln in lines)
    assert any(re.fullmatch(r"POST /jobs 303 \d+ms", ln) for ln in lines)


def test_record_args_hold_only_method_path_status_duration(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with build(settings_for(tmp_path)) as c:
        c.get(
            "/healthz", headers={"Authorization": "Bearer " + TOKEN, "X-Forwarded-For": "8.8.8.8"}
        )
    rec = next(r for r in caplog.records if r.name == ACCESS)
    assert rec.levelno == logging.INFO
    assert rec.msg == "%s %s %s %dms"
    assert rec.args[:3] == ("GET", "/healthz", 200)
    text = caplog.text
    assert TOKEN not in text and "8.8.8.8" not in text and "testclient" not in text


def test_unknown_and_hostile_paths_are_sanitised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with build(settings_for(tmp_path)) as c:
        c.get("/jobs/abc/extra/stuff")
        c.get("/x%0aFAKE LINE Bearer%20" + TOKEN)
        c.get("/" + "a" * 5000)
    blob = caplog.text
    assert TOKEN not in blob
    assert all("\n" not in ln for ln in _lines(caplog))
    assert all(len(ln) < 400 for ln in _lines(caplog))
    assert "/jobs/<id>/<x>/<x>" in "\n".join(_lines(caplog))


def test_rejected_requests_are_logged_with_status(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with build(settings_for(tmp_path)) as c:
        c.post("/jobs", content=b"a" * 20_000)
        c.post("/jobs", data={"token": ""}, headers={"Origin": "https://evil.example"})
    lines = _lines(caplog)
    assert any(ln.startswith("POST /jobs 413 ") for ln in lines)
    assert any(ln.startswith("POST /jobs 403 ") for ln in lines)


def test_uvicorn_access_is_silent_and_restored(tmp_path: Path) -> None:
    uv = logging.getLogger("uvicorn.access")
    seen: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    handler = _Grab()
    uv.addHandler(handler)
    uv.setLevel(logging.INFO)
    before = (uv.disabled, uv.propagate, list(uv.handlers))
    try:
        with build(settings_for(tmp_path)) as c:
            c.get("/healthz")
            uv.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/x?token=SECRET", "1.1", 200)
        assert seen == []
        assert (uv.disabled, uv.propagate, list(uv.handlers)) == before
    finally:
        uv.removeHandler(handler)
