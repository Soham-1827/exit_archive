"""PaymentVerifier seam: fail closed, allow-all bypass, session reuse."""

from pathlib import Path

from tests.web.helpers import RecordingExport, build, post_job, settings_for, wait_idle


class _Verifier:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.seen: list[str] = []

    async def verify(self, session_id: str) -> bool:
        self.seen.append(session_id)
        return self.result


class _Broken:
    async def verify(self, session_id: str) -> bool:
        raise RuntimeError("stripe down")


def _job_count(client: object) -> int:
    store = client.app.state.store  # type: ignore[attr-defined]
    return len(store.active_job_ids())


def test_required_without_verifier_fails_closed(tmp_path: Path) -> None:
    export = RecordingExport()
    with build(settings_for(tmp_path, payment_required=True), export=export) as client:
        resp = post_job(client, session_id="cs_test_1")
        assert resp.status_code == 402
        assert post_job(client).status_code == 402
        wait_idle(client)
        assert _job_count(client) == 0
        store = client.app.state.store  # type: ignore[attr-defined]
        assert store.get("anything") is None
    assert export.calls == []


def test_not_required_creates_job_and_second_use_of_session_is_409(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        first = post_job(client, session_id="cs_test_1")
        assert first.status_code == 303
        second = post_job(client, session_id="cs_test_1")
        assert second.status_code == 409
        assert "already" in second.text.lower()
        assert post_job(client, session_id="cs_test_2").status_code == 303
        assert post_job(client).status_code == 303  # no session: nothing to reuse
        wait_idle(client)


def test_injected_verifier_decides(tmp_path: Path) -> None:
    yes, no = _Verifier(True), _Verifier(False)
    cfg = settings_for(tmp_path, payment_required=True)
    with build(cfg, payments=yes) as client:
        assert post_job(client, session_id="cs_ok").status_code == 303
        assert post_job(client).status_code == 402  # paid mode needs a session id
        wait_idle(client)
    assert yes.seen == ["cs_ok"]
    with build(cfg, payments=no) as client:
        assert post_job(client, session_id="cs_no").status_code == 402
        assert _job_count(client) == 0


def test_verifier_error_is_503_and_creates_nothing(tmp_path: Path) -> None:
    cfg = settings_for(tmp_path, payment_required=True)
    with build(cfg, payments=_Broken()) as client:
        resp = post_job(client, session_id="cs_x")
        assert resp.status_code == 503
        assert "stripe down" not in resp.text
        assert _job_count(client) == 0


class _Truthy:
    def __init__(self, value: object) -> None:
        self.value = value

    async def verify(self, session_id: str) -> object:
        return self.value


def test_only_true_creates_a_job(tmp_path: Path) -> None:
    cfg = settings_for(tmp_path, payment_required=True)
    for value in (1, "yes", None, [1]):
        with build(cfg, payments=_Truthy(value)) as client:  # type: ignore[arg-type]
            resp = post_job(client, session_id="cs_t")
            assert resp.status_code == 402, value
            assert _job_count(client) == 0
