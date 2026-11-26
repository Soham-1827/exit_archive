"""T4.3.2(b): rate limit on POST /jobs, client key, proxy hops, settings."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exit_archive.web.app import create_app
from exit_archive.web.security import RateLimiter, client_key
from exit_archive.web.settings import Settings, SettingsError
from tests.web.helpers import RecordingExport, settings_for


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _client(tmp_path: Path, clock: Clock, peer: str = "9.9.9.9", **over: object) -> TestClient:
    cfg = settings_for(tmp_path, jobs_rate_limit=5, **over)
    app = create_app(cfg, export=RecordingExport(), clock=clock)
    return TestClient(app, raise_server_exceptions=False, client=(peer, 1234))


def _post(c: TestClient, **headers: str) -> int:
    return c.post("/jobs", data={"token": ""}, headers=headers).status_code


def test_sixth_post_in_window_is_429_with_retry_after(tmp_path: Path) -> None:
    clock = Clock()
    with _client(tmp_path, clock) as c:
        assert [_post(c) for _ in range(5)] == [422] * 5
        clock.now += 100
        r = c.post("/jobs", data={"token": ""})
        assert r.status_code == 429
        assert r.headers["retry-after"] == "500"
        assert r.headers["cache-control"] == "no-store"
        assert "content-security-policy" in r.headers


def test_window_expires_with_fake_clock(tmp_path: Path) -> None:
    clock = Clock()
    with _client(tmp_path, clock) as c:
        for _ in range(5):
            _post(c)
        assert _post(c) == 429
        clock.now += 601
        assert _post(c) == 422


def test_different_client_still_accepted(tmp_path: Path) -> None:
    clock = Clock()
    cfg = settings_for(tmp_path, jobs_rate_limit=5)
    app = create_app(cfg, export=RecordingExport(), clock=clock)
    with TestClient(app, client=("1.1.1.1", 1)) as a, TestClient(app, client=("2.2.2.2", 1)) as b:
        for _ in range(5):
            _post(a)
        assert _post(a) == 429
        assert _post(b) == 422


def test_other_routes_and_methods_not_limited(tmp_path: Path) -> None:
    clock = Clock()
    with _client(tmp_path, clock) as c:
        for _ in range(8):
            assert c.get("/healthz").status_code == 200
        for _ in range(5):
            _post(c)
        assert c.get("/start").status_code == 200


def test_limiter_off_when_not_configured(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path), export=RecordingExport())
    with TestClient(app) as c:
        assert [_post(c) for _ in range(8)] == [422] * 8


def test_hops_one_keys_on_rightmost_entry(tmp_path: Path) -> None:
    clock = Clock()
    with _client(tmp_path, clock, trusted_proxy_hops=1) as c:
        for i in range(5):  # rotating the spoofable left-most value does not help
            assert _post(c, **{"X-Forwarded-For": f"10.0.0.{i}, 2.2.2.2"}) == 422
        assert _post(c, **{"X-Forwarded-For": "10.0.0.99, 2.2.2.2"}) == 429
        assert _post(c, **{"X-Forwarded-For": "1.1.1.1, 3.3.3.3"}) == 422


def test_hops_zero_ignores_xff(tmp_path: Path) -> None:
    clock = Clock()
    with _client(tmp_path, clock) as c:
        for i in range(5):
            _post(c, **{"X-Forwarded-For": f"10.0.0.{i}"})
        assert _post(c, **{"X-Forwarded-For": "10.0.0.77"}) == 429


@pytest.mark.parametrize("xff", [None, "", "garbage", "1.1.1.1", ",", "not-an-ip, also-bad"])
def test_bad_or_short_xff_falls_back_to_peer(tmp_path: Path, xff: str | None) -> None:
    clock = Clock()
    with _client(tmp_path, clock, trusted_proxy_hops=2) as c:
        h = {} if xff is None else {"X-Forwarded-For": xff}
        for _ in range(5):
            _post(c, **h)
        assert _post(c, **h) == 429


def _scope(peer: object, xff: str | None) -> dict[str, object]:
    headers = [] if xff is None else [(b"x-forwarded-for", xff.encode())]
    return {"type": "http", "client": peer, "headers": headers}


def test_client_key_unit() -> None:
    assert client_key(_scope(("5.5.5.5", 1), "1.1.1.1, 2.2.2.2"), 0) == "5.5.5.5"
    assert client_key(_scope(("5.5.5.5", 1), "1.1.1.1, 2.2.2.2"), 1) == "2.2.2.2"
    assert client_key(_scope(("5.5.5.5", 1), "1.1.1.1, 2.2.2.2"), 2) == "1.1.1.1"
    assert client_key(_scope(("5.5.5.5", 1), "1.1.1.1, 2.2.2.2"), 3) == "5.5.5.5"
    assert client_key(_scope(("5.5.5.5", 1), "::1, 2001:DB8::1"), 1) == "2001:db8::1"
    assert client_key(_scope(None, None), 0) == "unknown"


def test_limiter_prunes_and_caps_keys() -> None:
    clock = Clock()
    lim = RateLimiter(600, max_keys=10, clock=clock)
    for i in range(50):
        lim.check(f"k{i}", 5)
    assert lim.size <= 10
    clock.now += 601
    lim.check("fresh", 5)
    assert lim.size == 1


def test_limiter_blocked_attempts_do_not_extend_window() -> None:
    clock = Clock()
    lim = RateLimiter(600, max_keys=10, clock=clock)
    for _ in range(5):
        assert lim.check("a", 5) is None
    for _ in range(20):
        assert lim.check("a", 5) is not None
    clock.now += 601
    assert lim.check("a", 5) is None


def test_from_env_enables_limiter_by_default(tmp_path: Path) -> None:
    cfg = Settings.from_env({"DATA_DIR": str(tmp_path), "PAYMENT_REQUIRED": "false"})
    assert cfg.jobs_rate_limit == 5
    assert cfg.trusted_proxy_hops == 0
    assert Settings(data_dir=tmp_path).jobs_rate_limit is None


@pytest.mark.parametrize("raw", ["0", "1", "2", "3"])
def test_trusted_proxy_hops_valid(tmp_path: Path, raw: str) -> None:
    env = {"DATA_DIR": str(tmp_path), "PAYMENT_REQUIRED": "false", "TRUSTED_PROXY_HOPS": raw}
    assert Settings.from_env(env).trusted_proxy_hops == int(raw)


@pytest.mark.parametrize("raw", ["9", "4", "-1", "abc", "1.5"])
def test_trusted_proxy_hops_invalid_names_variable(tmp_path: Path, raw: str) -> None:
    env = {"DATA_DIR": str(tmp_path), "PAYMENT_REQUIRED": "false", "TRUSTED_PROXY_HOPS": raw}
    with pytest.raises(SettingsError, match="TRUSTED_PROXY_HOPS") as exc:
        Settings.from_env(env)
    assert raw not in str(exc.value) or raw in "TRUSTED_PROXY_HOPS"


def test_blank_hops_means_default(tmp_path: Path) -> None:
    env = {"DATA_DIR": str(tmp_path), "PAYMENT_REQUIRED": "false", "TRUSTED_PROXY_HOPS": ""}
    assert Settings.from_env(env).trusted_proxy_hops == 0
