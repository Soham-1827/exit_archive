"""Tester-owned adversarial tests for T3.2 (token echo, traversal, 410/404, seam, lifespan)."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from exit_archive.jobs.store import JobStore, to_iso, utcnow
from exit_archive.web.app import create_app
from tests.web.helpers import (
    TOKEN,
    RecordingExport,
    build,
    make_job_id,
    post_job,
    settings_for,
    wait_idle,
)


def _no_token(resp: Any) -> None:
    assert TOKEN not in resp.text
    assert TOKEN not in str(resp.headers)
    assert TOKEN not in resp.request.url.path


def test_multipart_and_urlencoded_and_duplicates_no_echo(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        # multipart, missing token, token smuggled in another field and filename
        r = client.post(
            "/jobs",
            files={"account_id": (TOKEN, TOKEN.encode()), "other": (None, TOKEN)},
        )
        assert 400 <= r.status_code < 500
        _no_token(r)
        # duplicate token fields, one blank
        r = client.post("/jobs", data={"token": ["", TOKEN], "account_id": TOKEN})
        assert 400 <= r.status_code < 500
        _no_token(r)
        # query token + form without token
        r = client.post(f"/jobs?token={TOKEN}", data={"account_id": "1"})
        assert r.status_code == 422
        _no_token(r)
        # query token + form with other bad field
        r = client.post(f"/jobs?token={TOKEN}", data={"token": TOKEN, "account_id": "x"})
        assert r.status_code == 400
        _no_token(r)
        # garbage / wrong content types
        for kw in (
            {"content": TOKEN, "headers": {"content-type": "text/plain"}},
            {"content": "token=" + TOKEN, "headers": {"content-type": "application/json"}},
            {"content": f'{{"token": "{TOKEN}"', "headers": {"content-type": "application/json"}},
        ):
            r = client.post("/jobs", **kw)
            assert 400 <= r.status_code < 500
            _no_token(r)


def test_query_token_never_reaches_runner(tmp_path: Path) -> None:
    export = RecordingExport()
    with build(settings_for(tmp_path), export=export) as client:
        r = client.post(f"/jobs?token={TOKEN}", data={"account_id": "1"})
        assert r.status_code == 422
        wait_idle(client)
    assert export.calls == []


def test_oversized_token_and_body_no_echo(tmp_path: Path) -> None:
    big = TOKEN + "A" * 2_000_000
    with build(settings_for(tmp_path)) as client:
        r = client.post("/jobs", data={"token": big})
        assert r.status_code == 413
        _no_token(r)
        assert len(r.text) < 5000
        r = client.post("/jobs", data={"token": TOKEN, "account_id": "9" * 5_000_000})
        assert r.status_code == 413
        _no_token(r)


def test_redirect_location_is_server_generated(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        r = client.post(
            "/jobs?next=//evil.example",
            data={"token": TOKEN, "session_id": "cs_x", "next": "//evil.example"},
            headers={"Host": "evil.example", "Referer": "https://evil.example/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        assert loc.startswith("/jobs/") and "evil" not in loc and "//" not in loc
        assert "/" not in loc[len("/jobs/") :]
        wait_idle(client)


@pytest.mark.parametrize(
    "path",
    [
        "/jobs/..%2f..%2fetc%2fpasswd/download",
        "/jobs/%2e%2e%2f%2e%2e%2fjobs.sqlite3/download",
        "/jobs/..%5c..%5cx/download",
        "/jobs/" + "a" * 19 + "/download",
        "/jobs/" + "a" * 129 + "/download",
        "/jobs/" + "a" * 19 + "%00/download",
        "/jobs/" + "é" * 30 + "/download",
        "/jobs/" + "a" * 30 + "%0a/download",
    ],
)
def test_download_traversal_and_malformed_404(tmp_path: Path, path: str) -> None:
    with build(settings_for(tmp_path)) as client:
        assert client.get(path).status_code == 404
        assert client.get(path.replace("/download", "")).status_code == 404


def _done_job(client: TestClient, archive_path: str, expires: str) -> str:
    store: JobStore = client.app.state.store  # type: ignore[attr-defined]
    jid = make_job_id()
    store.create(jid, None)
    store.mark_running(jid)
    store.mark_done(jid, archive_path.replace("@", jid), to_iso(utcnow()), expires)
    return jid


def _real_zip(data: Path, jid: str) -> None:
    """Zip under archives/<jid>/ (row exists first, as with the runner, so sweeps keep it)."""
    d = data / "archives" / jid
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.zip").write_bytes(b"PK")


FUTURE = "2999-01-01T00:00:00Z"


def test_download_refuses_escaping_and_symlinked_paths(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with build(settings_for(data)) as client:
        secret = tmp_path / "secret.zip"
        secret.write_bytes(b"PK-secret")
        arch = data / "archives"
        # stored path escaping via .. and absolute
        for bad in (
            "archives/../../secret.zip",
            str(secret),
            "other/x.zip",
            "../secret.zip",
            "archives/x/../../../secret.zip",
            "",
        ):
            jid = _done_job(client, bad, FUTURE)
            assert client.get(f"/jobs/{jid}/download").status_code == 404, bad
        # symlinked file inside job dir
        jid = _done_job(client, "archives/@/a.zip", FUTURE)
        (arch / jid).mkdir()
        os.symlink(secret, arch / jid / "a.zip")
        r = client.get(f"/jobs/{jid}/download")
        assert r.status_code == 404 and b"PK-secret" not in r.content
        # symlinked job dir
        jid = _done_job(client, "archives/@/secret.zip", FUTURE)
        os.symlink(tmp_path, arch / jid)
        r = client.get(f"/jobs/{jid}/download")
        assert r.status_code == 404 and b"PK-secret" not in r.content
        # directory, not file
        jid = _done_job(client, "archives/@", FUTURE)
        (arch / jid).mkdir()
        assert client.get(f"/jobs/{jid}/download").status_code == 404


def test_download_symlinked_archives_root_refused(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with build(settings_for(data)) as client:
        jid = _done_job(client, "archives/@/a.zip", FUTURE)
        real = tmp_path / "real"
        (real / jid).mkdir(parents=True)
        (real / jid / "a.zip").write_bytes(b"PK-x")
        arch = data / "archives"
        arch.rmdir()
        os.symlink(real, arch)
        assert client.get(f"/jobs/{jid}/download").status_code == 404


def test_410_vs_404_semantics(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        store: JobStore = client.app.state.store  # type: ignore[attr-defined]
        past = "2000-01-01T00:00:00Z"
        # done but past expiry, file still present -> 410
        jid = _done_job(client, "archives/@/a.zip", past)
        _real_zip(tmp_path, jid)
        assert client.get(f"/jobs/{jid}/download").status_code == 410
        # status expired -> 410
        store.mark_expired(jid)
        assert client.get(f"/jobs/{jid}/download").status_code == 410
        # failed / queued / running -> 404 (never 410)
        for fn in ("failed", "queued", "running"):
            j = make_job_id()
            store.create(j, None)
            if fn == "running":
                store.mark_running(j)
            if fn == "failed":
                store.mark_failed(j, "x")
            assert client.get(f"/jobs/{j}/download").status_code == 404
        # done, future expiry -> 200 zip attachment
        ok = _done_job(client, "archives/@/a.zip", FUTURE)
        _real_zip(tmp_path, ok)
        r = client.get(f"/jobs/{ok}/download")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "attachment" in r.headers["content-disposition"]


class _Raises:
    async def verify(self, session_id: str) -> bool:
        raise RuntimeError(f"stripe down {TOKEN}")


class _Hangs:
    async def verify(self, session_id: str) -> bool:
        await asyncio.sleep(3600)
        return True


class _Truthy:
    async def verify(self, session_id: str) -> Any:
        return "no"  # non-bool truthy


class _False:
    async def verify(self, session_id: str) -> bool:
        return False


def test_verifier_exception_fails_closed_no_job_no_leak(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    export = RecordingExport()
    with (
        caplog.at_level("DEBUG"),
        build(
            settings_for(tmp_path, payment_required=True), export=export, payments=_Raises()
        ) as client,
    ):
        r = post_job(client, session_id="cs_1")
        assert r.status_code == 503
        _no_token(r)
        assert "stripe down" not in r.text
        assert client.app.state.store.get  # store open
        wait_idle(client)
    assert export.calls == []
    assert TOKEN not in caplog.text


def test_payment_required_blank_session_and_false_verifier(tmp_path: Path) -> None:
    for verifier in (_False(), None):
        with build(
            settings_for(tmp_path / str(id(verifier)), payment_required=True), payments=verifier
        ) as client:
            assert post_job(client).status_code == 402
            assert post_job(client, session_id="cs_1").status_code == 402
            assert post_job(client, session_id="   ").status_code == 402


def test_allow_all_ignores_injected_verifier(tmp_path: Path) -> None:
    with build(settings_for(tmp_path), payments=_False()) as client:
        assert post_job(client, session_id="cs_1").status_code == 303
        wait_idle(client)


def test_hanging_verifier_has_no_timeout_in_seam(tmp_path: Path) -> None:
    """FINDING probe: the route imposes no timeout; a hung verifier hangs the request.

    Documented, not failed: T3.3's verifier must carry its own httpx timeout.
    """
    import inspect

    from exit_archive.web import app as app_mod

    src = inspect.getsource(app_mod.create_app)
    assert "wait_for" not in src and "timeout" not in src  # records current behaviour


def test_truthy_nonbool_verifier_is_refused(tmp_path: Path) -> None:
    """LOW 4 fix: only `paid is True` creates a job; a truthy non-bool is a 402 with no job."""
    export = RecordingExport()
    with build(
        settings_for(tmp_path, payment_required=True), export=export, payments=_Truthy()
    ) as client:
        r = post_job(client, session_id="cs_1")
        assert r.status_code == 402
        _no_token(r)
        wait_idle(client)
        assert client.app.state.store.get  # store usable
    assert export.calls == []


def test_session_id_xss_on_start_is_escaped(tmp_path: Path) -> None:
    evil = '"><script>alert(1)</script><img src=x onerror=1>'
    with build(settings_for(tmp_path)) as client:
        r = client.get("/start", params={"session_id": evil})
        assert "<script>alert" not in r.text and "onerror=1>" not in r.text.replace("&", "")
        assert "&#34;" in r.text or "&quot;" in r.text
        r = client.get("/start", params={"session_id": "'" + " autofocus onfocus=alert(1) x='"})
        assert "onfocus=alert(1) x='\"" not in r.text
        # all pages: no inline script/style, only /static/app.css
        for p in ("/", "/start", "/privacy"):
            t = client.get(p).text.lower()
            assert "<script" not in t and "<style" not in t and " style=" not in t
            assert "http://" not in t


def test_error_page_autoescape_and_no_echo_of_path(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        r = client.get("/<script>alert(1)</script>")
        assert r.status_code == 404 and "<script>alert" not in r.text
        r = client.post("/healthz")
        assert r.status_code == 405


def test_progress_page_escapes_failed_error(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        store: JobStore = client.app.state.store  # type: ignore[attr-defined]
        j = make_job_id()
        store.create(j, None)
        store.mark_failed(j, "<script>alert(1)</script>")
        r = client.get(f"/jobs/{j}")
        assert "<script>alert" not in r.text


def test_access_log_query_string_note(tmp_path: Path) -> None:
    """Uvicorn's access logger prints the raw request line; starlette never reads ?token=.

    We cannot run uvicorn here; this pins that the app itself never logs the query string.
    """
    records: list[logging.LogRecord] = []

    class H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    h = H()
    logging.getLogger().addHandler(h)
    try:
        with build(settings_for(tmp_path)) as client:
            client.post(f"/jobs?token={TOKEN}", data={"account_id": "1"})
    finally:
        logging.getLogger().removeHandler(h)
    assert all(TOKEN not in r.getMessage() for r in records)


def test_startup_failure_leaves_nothing_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = logging.getLogRecordFactory()

    def boom(*a: object, **k: object) -> None:
        raise OSError("sweep failed")

    monkeypatch.setattr("exit_archive.web.app.sweep", boom)
    app = create_app(settings_for(tmp_path), export=RecordingExport())
    with pytest.raises(OSError, match=".*"), TestClient(app):
        pass
    assert logging.getLogRecordFactory() is factory
    assert not hasattr(app.state, "runner")


def test_shutdown_restores_factory_and_cancels_cleanup(tmp_path: Path) -> None:
    factory = logging.getLogRecordFactory()
    app = create_app(settings_for(tmp_path), export=RecordingExport())
    with TestClient(app) as client:
        assert logging.getLogRecordFactory() is not factory
        post_job(client)
        wait_idle(client)
    assert logging.getLogRecordFactory() is factory


def test_startup_failure_in_store_open(tmp_path: Path) -> None:
    """DATA_DIR that is a file: startup raises and nothing stays installed."""
    factory = logging.getLogRecordFactory()
    f = tmp_path / "afile"
    f.write_text("x")
    app = create_app(settings_for(f), export=RecordingExport())
    with pytest.raises(OSError, match=".*"), TestClient(app):
        pass
    assert logging.getLogRecordFactory() is factory


def test_redaction_hook_keeps_uvicorn_access_log_formattable(tmp_path: Path) -> None:
    from uvicorn.logging import AccessFormatter

    with build(settings_for(tmp_path)):
        record = logging.getLogger("uvicorn.access").makeRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", "/healthz", "1.1", 200),
            None,
        )
        text = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(
            record
        )
        assert "GET /healthz" in text
