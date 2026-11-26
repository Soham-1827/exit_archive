"""T4.3 tester: adversarial checks on secrets state, access log, body limit, rate limit, origin."""

import asyncio
import copy
import logging
import pickle
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from exit_archive.secrets import HarvestToken, Secret, redact
from exit_archive.web.app import create_app
from exit_archive.web.security import (
    SECURITY_HEADERS,
    RateLimiter,
    client_key,
)
from tests.web.helpers import RecordingExport, build, make_job_id, settings_for

RAW = "RAW-VALUE-zzz-12345"
ACCESS = "exit_archive.access"
ALL_HEADERS = [name for name, _ in SECURITY_HEADERS]


def _has_headers(r: Any) -> None:
    for name in ALL_HEADERS:
        assert name in r.headers, name


# ---------------------------------------------------------------- 1. Secret state paths


@pytest.mark.parametrize("cls", [Secret, HarvestToken])
def test_secret_state_paths_never_yield_raw(cls: type) -> None:
    s = cls(RAW)
    assert s.__getstate__() is None
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError):
            pickle.dumps(s, protocol=proto)
    with pytest.raises(TypeError):
        copy.copy(s)
    with pytest.raises(TypeError):
        copy.deepcopy(s)
    with pytest.raises(TypeError):
        s.__reduce__()
    with pytest.raises(TypeError):
        s.__reduce_ex__(4)
    with pytest.raises(TypeError):
        vars(s)  # slotted: no __dict__
    assert not hasattr(s, "__dict__")
    shown = [repr(s), str(s), format(s), f"{s}", f"{s!r}", "%s" % s, str([s]), str({"k": s})]  # noqa: UP031
    assert all(RAW not in x for x in shown)
    assert RAW not in repr(dir(s))


def test_secret_slots_introspection_is_the_only_known_raw_path() -> None:
    """Document: getattr/slot descriptors read the value; that is inherent to Python slots."""
    s = Secret(RAW)
    assert type(s).__slots__ == ("_value",)
    # Explicit attribute access to the private slot is not a leak through any *state protocol*.
    assert s._value == RAW


def test_secret_not_serialisable_by_json_or_logging() -> None:
    import json

    s = Secret(RAW)
    with pytest.raises(TypeError):
        json.dumps({"k": s})
    assert RAW not in json.dumps({"k": str(s)})


@pytest.mark.parametrize(
    "text",
    ["Bearer%20abc", "bearer%20abc", "BEARER%20abc", "Bearer+abc", "bEaReR+abc", "Bearer%20%20abc"],
)
def test_bearer_encoded_forms_scrubbed(text: str) -> None:
    out = redact(f"Authorization: {text} tail", None)
    assert "abc" not in out


# ---------------------------------------------------------------- 2. Access log lifecycle


def _run_lifespan(app: Any) -> None:
    async def go() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(go())


def test_repeated_lifespans_do_not_stack_handlers_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()  # simulate a bare process
    own = logging.getLogger(ACCESS)
    uv = logging.getLogger("uvicorn.access")
    before = (list(own.handlers), own.level, uv.disabled)
    try:
        app = create_app(settings_for(tmp_path), export=RecordingExport())
        for _ in range(3):
            _run_lifespan(app)
            assert own.handlers == before[0]
            assert own.level == before[1]
            assert uv.disabled == before[2]
    finally:
        root.handlers[:] = saved


def test_lifespan_adds_exactly_one_handler_while_running_and_disables_uvicorn(
    tmp_path: Path,
) -> None:
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    own = logging.getLogger(ACCESS)
    uv = logging.getLogger("uvicorn.access")
    seen: dict[str, Any] = {}
    try:
        app = create_app(settings_for(tmp_path), export=RecordingExport())

        async def go() -> None:
            async with app.router.lifespan_context(app):
                seen["n"] = len(own.handlers)
                seen["uv"] = uv.disabled
                seen["lvl"] = own.level

        asyncio.run(go())
    finally:
        root.handlers[:] = saved
    assert seen == {"n": 1, "uv": True, "lvl": logging.INFO}
    assert own.handlers == [] and uv.disabled is False


def test_uvicorn_access_stays_disabled_if_it_already_was(tmp_path: Path) -> None:
    uv = logging.getLogger("uvicorn.access")
    uv.disabled = True
    try:
        _run_lifespan(create_app(settings_for(tmp_path), export=RecordingExport()))
        assert uv.disabled is True
    finally:
        uv.disabled = False


def test_access_lines_are_redacted_by_installed_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    try:
        with build(settings_for(tmp_path)) as c:
            c.get("/Bearer%20abcSECRETxyz")
            c.get("/Bearer+abcSECRETxyz")
    finally:
        root.handlers[:] = saved
    err = capsys.readouterr().err
    assert "GET /" in err
    assert "abcSECRETxyz" not in err


# ---------------------------------------------------------------- 3. Hostile paths


HOSTILE = [
    "/x\r\nFORGED 200 0ms",
    "/x%0d%0aFORGED",
    "/x%0D%0AFORGED%20GET%20/",
    "/café/‮",
    "/nul%00byte",
    "/" + "a" * 5000,
    "/%e2%80%a8LS",  # unicode line separator
    "/jobs/%0d%0aFORGED",
    "/jobs/AAAA/../x",
    "/jobs/AAAA/%2e%2e/x",
    "/jobs/%41%41%41%41",
    "/jobs/%2541%2541/download",
]


@pytest.mark.parametrize("path", HOSTILE)
def test_hostile_paths_cannot_forge_or_leak(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    from urllib.parse import unquote

    caplog.set_level(logging.DEBUG)
    # uvicorn hands the app the percent-decoded path, so feed raw ASGI (httpx refuses CR/LF)
    _asgi(_app(tmp_path), "GET", unquote(path), [], [])
    assert any(r.name == ACCESS for r in caplog.records)
    for rec in caplog.records:
        if rec.name != ACCESS:
            continue
        line = rec.getMessage()
        assert "\n" not in line and "\r" not in line and "\x00" not in line
        assert re.fullmatch(r"GET [\x21-\x7e]{1,120} \d{3} \d+ms", line), repr(line)
        assert "FORGED" not in line.split(" ")[0:1]  # method token is ours


def test_masking_covers_job_id_variants(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    jid = make_job_id()
    with build(settings_for(tmp_path)) as c:
        for p in (f"/jobs/{jid}", f"/jobs/{jid}/download", f"/jobs/{jid}/x/y", f"/jobs/{jid}/"):
            c.get(p)
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == ACCESS)
    assert jid not in text


@pytest.mark.xfail(
    strict=True,
    reason="FINDING-A1 (LOW): mask_path only masks paths starting with /jobs/; a non-routable "
    "request path such as //jobs/<id> or /./jobs/<id> (404) is logged with the id verbatim",
)
def test_double_slash_prefix_does_not_leak_job_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """//jobs/<id> is not under /jobs/ for the masker; the id must still not be logged."""
    caplog.set_level(logging.DEBUG)
    jid = make_job_id()
    app = _app(tmp_path)
    for p in (f"//jobs/{jid}", f"/./jobs/{jid}", f"/jobs//{jid}", f"/static/../jobs/{jid}"):
        _asgi(app, "GET", p, [], [])
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == ACCESS)
    assert jid not in text


# ---------------------------------------------------------------- 4. Error pages, bodies


def _assert_clean_error(r: Any, status: int, *needles: str) -> None:
    assert r.status_code == status
    _has_headers(r)
    assert r.headers["cache-control"] == "no-store"
    for n in needles:
        assert n not in r.text
        assert n not in str(r.headers)


@pytest.fixture
def cl(tmp_path: Path):  # type: ignore[no-untyped-def]
    cfg = settings_for(tmp_path, jobs_rate_limit=2)
    app = create_app(cfg, export=RecordingExport())
    with TestClient(app, raise_server_exceptions=False, client=("7.7.7.7", 1)) as c:
        yield c


def test_403_413_429_pages_have_headers_and_echo_nothing(cl: TestClient) -> None:
    r = cl.post("/jobs", data={"token": "ECHOME"}, headers={"Origin": "https://ECHOEVIL.test"})
    _assert_clean_error(r, 403, "ECHOME", "ECHOEVIL", "evil")
    r = cl.post("/jobs", content=b"ECHOBIG" * 5000, headers={"X-Echo": "ECHOHDR"})
    _assert_clean_error(r, 413, "ECHOBIG", "ECHOHDR")
    for _ in range(2):
        cl.post("/jobs", data={"token": ""})
    r = cl.post("/jobs", data={"token": "ECHO429"}, headers={"X-Echo": "ECHOHDR"})
    _assert_clean_error(r, 429, "ECHO429", "ECHOHDR")
    assert "retry-after" in r.headers


def test_403_does_not_consume_rate_budget(cl: TestClient) -> None:
    for _ in range(10):
        assert cl.post("/jobs", data={"token": ""}, headers={"Origin": "null"}).status_code == 403
    assert cl.post("/jobs", data={"token": ""}).status_code == 422


@pytest.mark.parametrize("cl_value", ["-1", "-0", "+5", "1e3", "0x10", " 5", "5 ", ""])
def test_bad_content_length_is_400_with_headers(cl: TestClient, cl_value: str) -> None:
    r = cl.post("/jobs", content=b"a=b", headers={"Content-Length": cl_value})
    # h11 may refuse before us; whatever answers must be a 4xx with no echo
    assert 400 <= r.status_code < 500


def _asgi(
    app: Any, method: str, path: str, headers: list[tuple[bytes, bytes]], chunks: list[bytes]
):  # type: ignore[no-untyped-def]
    """Drive the app with a raw ASGI scope (needed for duplicate / chunked headers)."""
    sent: list[dict[str, Any]] = []
    queue = list(chunks) or [b""]

    async def receive() -> dict[str, Any]:
        if queue:
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(m: dict[str, Any]) -> None:
        sent.append(m)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": [(b"host", b"testserver"), *headers],
        "client": ("5.5.5.5", 1), "server": ("testserver", 80),
    }  # fmt: skip

    async def go() -> None:
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), 10)

    asyncio.run(go())
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}


def _app(tmp_path: Path, **over: Any) -> Any:
    return create_app(settings_for(tmp_path, **over), export=RecordingExport())


FORM = (b"content-type", b"application/x-www-form-urlencoded")


def test_duplicate_content_length_cannot_bypass(tmp_path: Path) -> None:
    app = _app(tmp_path)
    big = [b"token=" + b"a" * 20_000]
    st, h = _asgi(
        app, "POST", "/jobs", [FORM, (b"content-length", b"10"), (b"content-length", b"20006")], big
    )
    assert st in (400, 413)
    assert "content-security-policy" in h
    st, h = _asgi(
        app, "POST", "/jobs", [FORM, (b"content-length", b"20006"), (b"content-length", b"10")], big
    )
    assert st in (400, 413)


def test_huge_content_length_413(tmp_path: Path) -> None:
    st, h = _asgi(
        _app(tmp_path), "POST", "/jobs", [FORM, (b"content-length", b"9" * 40)], [b"token=x"]
    )
    assert st == 413 and "content-security-policy" in h


def test_chunked_boundary(tmp_path: Path) -> None:
    app = _app(tmp_path)
    limit = 16 * 1024
    pad = b"token=" + b"a" * (limit - 6)
    assert len(pad) == limit
    st, _ = _asgi(app, "POST", "/jobs", [FORM], [pad[:5000], pad[5000:]])
    assert st != 413  # exactly at the limit is allowed (422/303 depending on token)
    over = pad + b"a"
    st, h = _asgi(app, "POST", "/jobs", [FORM], [over[:5000], over[5000:]])
    assert st == 413 and "content-security-policy" in h
    # many tiny chunks totalling over the limit
    st, _ = _asgi(app, "POST", "/jobs", [FORM], [b"a"] * (limit + 1))
    assert st == 413


def test_get_with_body_is_not_limited_or_crashing(tmp_path: Path) -> None:
    st, h = _asgi(
        _app(tmp_path), "GET", "/healthz", [(b"content-length", b"50000")], [b"x" * 50000]
    )
    assert st == 200 and "content-security-policy" in h


def test_oversized_body_other_post_paths(cl: TestClient) -> None:
    r = cl.post("/anything", content=b"a" * 20000)
    assert r.status_code == 413
    _has_headers(r)


# ---------------------------------------------------------------- 5. Rate limiter bounds


def test_eviction_does_not_reset_single_key_without_flood() -> None:
    now = [0.0]
    rl = RateLimiter(window=600, max_keys=100, clock=lambda: now[0])
    for _ in range(5):
        assert rl.check("victim", 5) is None
    assert rl.check("victim", 5) is not None
    # 98 other keys: cap not hit, victim stays limited
    for i in range(98):
        rl.check(f"k{i}", 5)
    now[0] += 1
    assert rl.check("victim", 5) is not None
    # churn of the *same* other keys does not evict victim either
    for _ in range(50):
        for i in range(98):
            rl.check(f"k{i}", 5)
    assert rl.check("victim", 5) is not None


def test_memory_bounded_under_flood() -> None:
    now = [0.0]
    rl = RateLimiter(window=600, max_keys=1000, clock=lambda: now[0])
    for i in range(50_000):
        rl.check(f"k{i}", 5)
        now[0] += 0.001
    assert rl.size <= 1000


def test_flood_of_cap_keys_is_the_only_eviction_route() -> None:
    now = [0.0]
    rl = RateLimiter(window=600, max_keys=50, clock=lambda: now[0])
    for _ in range(5):
        rl.check("victim", 5)
    assert rl.check("victim", 5) is not None
    for i in range(50):  # flood with exactly cap distinct keys
        rl.check(f"f{i}", 5)
    assert rl.check("victim", 5) is None  # documented limit of an LRU: needs the cap in keys


def test_hit_during_limit_does_not_extend_window() -> None:
    now = [0.0]
    rl = RateLimiter(window=600, max_keys=10, clock=lambda: now[0])
    for _ in range(5):
        rl.check("a", 5)
    for _ in range(100):
        now[0] += 5
        rl.check("a", 5)  # rejected hits must not be recorded
    now[0] = 601
    assert rl.check("a", 5) is None


# ---------------------------------------------------------------- 6. Headers on all kinds


def test_headers_on_500_404_405_static(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path), export=RecordingExport())

    @app.get("/boom2")
    async def boom2() -> None:
        raise RuntimeError("kaboom SECRETX")

    with TestClient(app, raise_server_exceptions=False) as c:
        r500 = c.get("/boom2")
        assert r500.status_code == 500 and "SECRETX" not in r500.text
        for r in (
            r500,
            c.get("/nope"),
            c.get("/jobs"),
            c.delete("/healthz"),
            c.get("/static/app.css"),
            c.get("/static/nope.css"),
            c.get("/static/../app.py"),
            c.head("/healthz"),
            c.options("/jobs"),
        ):
            _has_headers(r)


# ---------------------------------------------------------------- 7. XFF spoofing


def _scope(xff: list[str], peer: str = "10.0.0.1") -> dict[str, Any]:
    return {
        "client": (peer, 1),
        "headers": [(b"x-forwarded-for", v.encode()) for v in xff],
    }


def test_hops1_left_entries_ignored() -> None:
    keys = {client_key(_scope([f"{i}.{i}.{i}.{i}, 8.8.8.8"]), 1) for i in range(1, 50)}
    assert keys == {"8.8.8.8"}
    # spoofed entries in a separate header line are still left of the proxy's
    keys = {client_key(_scope([f"{i}.1.1.1", "8.8.8.8"]), 1) for i in range(1, 50)}
    assert keys == {"8.8.8.8"}


def test_hops2_takes_second_from_right() -> None:
    assert client_key(_scope(["1.1.1.1, 2.2.2.2, 3.3.3.3"]), 2) == "2.2.2.2"


@pytest.mark.parametrize(
    "xff",
    [
        "",
        ",",
        " , ",
        "garbage",
        "1.2.3.4:80",
        "999.1.1.1",
        "1.2.3.4,",
        "1.2.3.4, ",
        "%00",
        "a" * 9000,
    ],
)
def test_malformed_xff_falls_back_to_peer(xff: str) -> None:
    assert client_key(_scope([xff]), 1) == "10.0.0.1"


def test_ipv6_normalised_and_no_xff_falls_back() -> None:
    assert client_key(_scope(["x, 2001:DB8:0:0:0:0:0:1"]), 1) == "2001:db8::1"
    assert client_key(_scope(["x, 2001:db8::1"]), 1) == "2001:db8::1"
    assert client_key(_scope([]), 1) == "10.0.0.1"
    assert client_key(_scope(["1.1.1.1"]), 2) == "10.0.0.1"  # short chain


def test_hops0_ignores_xff() -> None:
    assert client_key(_scope(["8.8.8.8"]), 0) == "10.0.0.1"
    assert client_key({"headers": [], "client": None}, 0) == "unknown"


def test_hops1_rotating_left_xff_cannot_escape(tmp_path: Path) -> None:
    cfg = settings_for(tmp_path, jobs_rate_limit=3, trusted_proxy_hops=1)
    app = create_app(cfg, export=RecordingExport())
    with TestClient(app, raise_server_exceptions=False, client=("10.0.0.1", 1)) as c:
        codes = [
            c.post(
                "/jobs", data={"token": ""}, headers={"X-Forwarded-For": f"{i}.9.9.9, 8.8.8.8"}
            ).status_code
            for i in range(6)
        ]
    assert codes == [422, 422, 422, 429, 429, 429]


def test_hops0_rotating_xff_cannot_escape(tmp_path: Path) -> None:
    cfg = settings_for(tmp_path, jobs_rate_limit=3)
    app = create_app(cfg, export=RecordingExport())
    with TestClient(app, raise_server_exceptions=False, client=("10.0.0.1", 1)) as c:
        codes = [
            c.post(
                "/jobs", data={"token": ""}, headers={"X-Forwarded-For": f"{i}.9.9.9"}
            ).status_code
            for i in range(5)
        ]
    assert codes == [422, 422, 422, 429, 429]


# ---------------------------------------------------------------- 8. Origin check


def _post_origin(tmp_path: Path, base: str | None, **headers: str) -> int:
    cfg = settings_for(tmp_path, public_base_url=base)
    app = create_app(cfg, export=RecordingExport())
    with TestClient(app, raise_server_exceptions=False, base_url="http://testserver") as c:
        return c.post("/jobs", data={"token": ""}, headers=headers).status_code


@pytest.mark.parametrize(
    ("headers", "want"),
    [
        ({"Origin": "null"}, 403),
        ({"Origin": "NULL"}, 403),
        ({"Origin": ""}, 403),
        ({"Origin": "http://testserver"}, 422),
        ({"Origin": "HTTP://TESTSERVER"}, 422),
        ({"Origin": "http://testserver:80"}, 422),
        ({"Origin": "https://testserver"}, 422),  # host mode: scheme not compared, default ports ok
        ({"Origin": "http://testserver/"}, 422),
        ({"Origin": "http://testserver/path?q=1"}, 422),
        ({"Origin": "http://testserver.evil.test"}, 403),
        ({"Origin": "http://evil.test@testserver"}, 422),  # host is testserver (userinfo ignored)
        ({"Origin": "http://testserver@evil.test"}, 403),
        ({"Origin": "ftp://testserver"}, 403),
        ({"Origin": "javascript:alert(1)"}, 403),
        ({"Origin": "http://[::1"}, 403),
        ({"Origin": "http://testserver:99999"}, 403),
        ({"Referer": "http://testserver/start?x=1"}, 422),
        ({"Referer": "http://evil.test/testserver"}, 403),
        ({"Referer": "garbage"}, 403),
        ({"Origin": "http://evil.test", "Referer": "http://testserver/"}, 403),
        ({"Origin": "http://testserver", "Referer": "http://evil.test/"}, 422),
        ({}, 422),
    ],
)
def test_origin_matrix_host_mode(tmp_path: Path, headers: dict[str, str], want: int) -> None:
    assert _post_origin(tmp_path, None, **headers) == want


@pytest.mark.parametrize(
    ("base", "origin", "want"),
    [
        ("https://app.example.com", "https://app.example.com", 422),
        ("https://app.example.com", "https://app.example.com:443", 422),
        ("https://app.example.com", "https://APP.Example.COM", 422),
        ("https://app.example.com/", "https://app.example.com", 422),
        ("https://app.example.com", "http://app.example.com", 403),
        ("https://app.example.com", "https://app.example.com:8443", 403),
        ("https://app.example.com", "http://testserver", 403),  # Host ignored when base is set
        ("https://app.example.com", "null", 403),
        ("https://xn--bcher-kva.example", "https://xn--bcher-kva.example", 422),
    ],
)
def test_origin_matrix_base_url_mode(tmp_path: Path, base: str, origin: str, want: int) -> None:
    assert _post_origin(tmp_path, base, Origin=origin) == want


def test_get_requests_never_origin_checked(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path), export=RecordingExport())
    with TestClient(app) as c:
        assert c.get("/healthz", headers={"Origin": "null"}).status_code == 200


# ---------------------------------------------------------------- 9. Settings


def test_from_env_rate_limit_and_hops() -> None:
    from exit_archive.web.settings import Settings, SettingsError

    base = {"DATA_DIR": "./data-unused", "PAYMENT_REQUIRED": "false"}
    assert Settings.from_env(base).jobs_rate_limit == 5
    assert Settings.from_env(base).trusted_proxy_hops == 0
    assert Settings.from_env({**base, "TRUSTED_PROXY_HOPS": "3"}).trusted_proxy_hops == 3
    for bad in ("4", "-1", "x", "1.5", "١"):
        with pytest.raises(SettingsError):
            Settings.from_env({**base, "TRUSTED_PROXY_HOPS": bad})


def test_unicode_origin_host_fails_closed(tmp_path: Path) -> None:
    app = _app(tmp_path, public_base_url="https://xn--bcher-kva.example")
    st, _ = _asgi(
        app, "POST", "/jobs", [FORM, (b"origin", "https://b\u00fccher.example".encode())], [b"t=1"]
    )
    assert st == 403
