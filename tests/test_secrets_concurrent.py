"""Redaction stays correct when several jobs install and uninstall in any order (T3.1.5)."""

import logging

import pytest

from exit_archive.secrets import HarvestToken, install_log_redaction


@pytest.fixture
def original_factory() -> object:
    return logging.getLogRecordFactory()


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_overlapping_jobs_a_finishes_first_b_still_scrubbed(
    original_factory: object, caplog: pytest.LogCaptureFixture
) -> None:
    token_a, token_b = HarvestToken("tok-AAAA-1111"), HarvestToken("tok-BBBB-2222")
    undo_a = install_log_redaction(token_a)
    undo_b = install_log_redaction(token_b)
    undo_a()
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("b is %s", "tok-BBBB-2222")
    assert "tok-BBBB-2222" not in _log_text(caplog)
    undo_b()
    assert logging.getLogRecordFactory() is original_factory


def test_both_tokens_scrubbed_while_both_active(caplog: pytest.LogCaptureFixture) -> None:
    undo_a = install_log_redaction(HarvestToken("tok-AAAA-1111"))
    undo_b = install_log_redaction(HarvestToken("tok-BBBB-2222"))
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger("t").info("x tok-AAAA-1111 y tok-BBBB-2222")
        text = _log_text(caplog)
        assert "tok-AAAA-1111" not in text and "tok-BBBB-2222" not in text
    finally:
        undo_b()
        undo_a()


def test_removed_token_is_no_longer_scrubbed_but_factory_still_installed(
    original_factory: object, caplog: pytest.LogCaptureFixture
) -> None:
    undo_a = install_log_redaction(HarvestToken("tok-AAAA-1111"))
    undo_b = install_log_redaction(HarvestToken("tok-BBBB-2222"))
    undo_a()
    assert logging.getLogRecordFactory() is not original_factory
    undo_b()
    assert logging.getLogRecordFactory() is original_factory


def test_same_token_twice_is_refcounted(caplog: pytest.LogCaptureFixture) -> None:
    undo_1 = install_log_redaction(HarvestToken("tok-SAME-3333"))
    undo_2 = install_log_redaction(HarvestToken("tok-SAME-3333"))
    undo_1()
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("still %s", "tok-SAME-3333")
    assert "tok-SAME-3333" not in _log_text(caplog)
    undo_2()


def test_uninstall_is_idempotent_and_does_not_drop_others(
    original_factory: object, caplog: pytest.LogCaptureFixture
) -> None:
    undo_1 = install_log_redaction(HarvestToken("tok-ONE-4444"))
    undo_2 = install_log_redaction(HarvestToken("tok-ONE-4444"))
    undo_1()
    undo_1()  # second call must not release undo_2's registration
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("v %s", "tok-ONE-4444")
    assert "tok-ONE-4444" not in _log_text(caplog)
    undo_2()
    undo_2()
    assert logging.getLogRecordFactory() is original_factory


def test_none_token_scrubs_bearer_only(caplog: pytest.LogCaptureFixture) -> None:
    undo = install_log_redaction(None)
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger("t").info("Authorization: Bearer abc.def")
        assert "abc.def" not in _log_text(caplog)
    finally:
        undo()


def test_single_hook_regardless_of_registrations(original_factory: object) -> None:
    undo_a = install_log_redaction(HarvestToken("tok-AAAA-1111"))
    hook = logging.getLogRecordFactory()
    undo_b = install_log_redaction(HarvestToken("tok-BBBB-2222"))
    assert logging.getLogRecordFactory() is hook
    undo_b()
    undo_a()
    assert logging.getLogRecordFactory() is original_factory


def test_malformed_log_call_never_raises_with_many_registrations() -> None:
    undo_a = install_log_redaction(HarvestToken("tok-AAAA-1111"))
    undo_b = install_log_redaction(None)
    try:
        logging.getLogger("t").warning("%s %s", "only-one-arg")
        logging.getLogger("t").warning("%d", "not-int")
    finally:
        undo_b()
        undo_a()


def test_reinstall_after_full_uninstall_works(original_factory: object) -> None:
    install_log_redaction(HarvestToken("tok-AAAA-1111"))()
    assert logging.getLogRecordFactory() is original_factory
    undo = install_log_redaction(HarvestToken("tok-AAAA-1111"))
    assert logging.getLogRecordFactory() is not original_factory
    undo()
    assert logging.getLogRecordFactory() is original_factory


def test_third_party_wrapper_is_preserved_and_reinstall_does_not_recurse(
    original_factory: object, caplog: pytest.LogCaptureFixture
) -> None:
    undo = install_log_redaction(HarvestToken("tok-AAAA-1111"))
    ours = logging.getLogRecordFactory()

    def third(*a: object, **k: object) -> logging.LogRecord:
        record = ours(*a, **k)
        record.thirdparty = True  # type: ignore[attr-defined]
        return record

    logging.setLogRecordFactory(third)
    try:
        undo()
        assert logging.getLogRecordFactory() is third
        # our hook is a pass-through now: no token registered, record untouched
        record = logging.getLogRecordFactory()("n", logging.INFO, "f", 1, "hi %s", ("x",), None)
        assert record.msg == "hi %s" and record.args == ("x",)
        # registering again must not recurse or double-wrap
        undo2 = install_log_redaction(HarvestToken("tok-BBBB-2222"))
        assert logging.getLogRecordFactory() is not third
        with caplog.at_level(logging.INFO):
            logging.getLogger("t").info("leak tok-BBBB-2222")
        assert "tok-BBBB-2222" not in _log_text(caplog)
        assert all(getattr(r, "thirdparty", False) for r in caplog.records)
        undo2()
    finally:
        logging.setLogRecordFactory(original_factory)  # type: ignore[arg-type]
