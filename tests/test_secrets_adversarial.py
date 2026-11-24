# ruff: noqa: UP031, UP032, S105
"""Adversarial probes for HarvestToken and log redaction (tester-added)."""

import copy
import dataclasses
import json
import logging
import pickle

import pytest

from exit_archive.secrets import HarvestToken, install_log_redaction

RAW = "fake-adversarial-secret-value-123"


@pytest.fixture
def token() -> HarvestToken:
    return HarvestToken(RAW)


@pytest.mark.parametrize(
    "render",
    [
        lambda t: "%r" % t,
        lambda t: "%s" % t,
        lambda t: "{!r}".format(t),
        lambda t: "{!s}".format(t),
        lambda t: "{:>20}".format(t),
        lambda t: f"{t:>40}",
        lambda t: repr([t]),
        lambda t: str({"k": t}),
    ],
)
def test_text_forms_hide_value(token, render):
    assert RAW not in render(token)


@pytest.mark.parametrize("op", [copy.copy, copy.deepcopy, pickle.dumps, json.dumps])
def test_copy_pickle_json_refused(token, op):
    with pytest.raises(TypeError) as exc:
        op(token)
    assert RAW not in str(exc.value)


def test_failed_assert_message_hides_value(token):
    with pytest.raises(AssertionError) as exc:
        assert token == "x"
    assert RAW not in str(exc.value)


def test_introspection_no_longer_leaks(token):
    # Fix loop 1: HarvestToken is a slotted plain class, so dict/asdict introspection fails.
    with pytest.raises(TypeError):
        vars(token)
    with pytest.raises(TypeError):
        dataclasses.asdict(token)
    with pytest.raises(AttributeError):
        token.__dict__  # noqa: B018
    with pytest.raises(AttributeError):
        token._value = "x"
    with pytest.raises(AttributeError):
        token.other = "x"
    with pytest.raises(AttributeError):
        del token._value


def test_malformed_call_with_raw_or_token_arg_is_scrubbed(token, redaction, caplog):
    caplog.set_level(logging.DEBUG)
    log = logging.getLogger("m2")
    log.warning("%s %s", RAW)
    log.warning("%s %s", token)
    log.warning("%(a)s %(b)s", {"a": RAW})
    assert RAW not in caplog.text
    assert len(caplog.records) == 3


def test_failed_token_equality_assert_hides_value(token):
    other = HarvestToken("fake-other-value-456")
    with pytest.raises(AssertionError) as exc:
        assert token == other
    assert RAW not in str(exc.value)
    assert "fake-other-value-456" not in str(exc.value)


@pytest.fixture
def redaction(token):
    undo = install_log_redaction(token)
    yield
    undo()


def test_exception_stack_percent_args_third_party(token, redaction, caplog):
    caplog.set_level(logging.DEBUG)
    log = logging.getLogger("httpx")
    try:
        raise RuntimeError(f"boom {RAW}")
    except RuntimeError:
        log.exception("failed")
    log.info("t=%s", RAW)
    log.warning("x", stack_info=True, extra={})
    log.info("auth Bearer %s", RAW)
    assert RAW not in caplog.text
    assert RAW not in "".join(r.exc_text or "" for r in caplog.records)


def test_stack_info_with_secret_in_message(token, redaction, caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("a.b").error("v=%s", RAW, stack_info=True)
    assert RAW not in caplog.text


def test_malformed_log_call_does_not_raise(redaction):
    logging.raiseExceptions = True
    logging.getLogger("m").warning("%s %s", 1)


def test_uninstall_restores_factory(token):
    before = logging.getLogRecordFactory()
    undo = install_log_redaction(token)
    assert logging.getLogRecordFactory() is not before
    undo()
    assert logging.getLogRecordFactory() is before


def test_error_status_contracts():
    from exit_archive.errors import HarvestUnavailableError, NotFoundError

    with pytest.raises(TypeError):
        NotFoundError("x", 500)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        HarvestUnavailableError(status_code=404)
    assert str(HarvestUnavailableError("r", 502)) == (
        "Harvest returned 502 for r: Harvest is unavailable after retries"
    )
