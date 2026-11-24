import dataclasses
import json
import logging
import pickle
from collections.abc import Iterator

import pytest

from exit_archive.secrets import HarvestToken, install_log_redaction, redact

RAW = "fake-token-1234567890.abcdef"


@pytest.fixture
def token() -> HarvestToken:
    return HarvestToken(RAW)


@pytest.fixture
def redaction(token: HarvestToken) -> Iterator[None]:
    uninstall = install_log_redaction(token)
    try:
        yield
    finally:
        uninstall()


def test_repr_str_format_hide_value(token: HarvestToken) -> None:
    assert repr(token) == "HarvestToken(***)"
    assert str(token) == "HarvestToken(***)"
    assert f"{token}" == "HarvestToken(***)"
    assert f"{token:>40}" == "HarvestToken(***)"
    assert "%s|%r" % (token, token) == "HarvestToken(***)|HarvestToken(***)"  # noqa: UP031


def test_reveal_returns_raw(token: HarvestToken) -> None:
    assert token.reveal() == RAW


def test_frozen(token: HarvestToken) -> None:
    with pytest.raises(AttributeError):
        token.value = "x"  # type: ignore[attr-defined]


def test_equality_and_hash(token: HarvestToken) -> None:
    assert token == HarvestToken(RAW)
    assert token != HarvestToken("other")
    assert hash(token) == hash(HarvestToken(RAW))


def test_pickle_raises(token: HarvestToken) -> None:
    with pytest.raises(TypeError):
        pickle.dumps(token)


def test_copy_raises(token: HarvestToken) -> None:
    import copy

    with pytest.raises(TypeError):
        copy.deepcopy(token)


def test_json_raises(token: HarvestToken) -> None:
    with pytest.raises(TypeError):
        json.dumps(token)
    with pytest.raises(TypeError):
        json.dumps({"t": token})


def test_empty_token_rejected() -> None:
    with pytest.raises(ValueError):
        HarvestToken("")


def test_redact_value_and_bearer(token: HarvestToken) -> None:
    text = f"x {RAW} y Authorization: Bearer abc.DEF-123 z"
    out = redact(text, token)
    assert RAW not in out
    assert "abc.DEF-123" not in out
    assert out == "x *** y Authorization: *** z"


def test_redact_without_token_still_scrubs_bearer() -> None:
    assert "secret" not in redact("Bearer secret", None)


def test_redact_leaves_clean_text() -> None:
    assert redact("hello world", HarvestToken(RAW)) == "hello world"


def test_logging_never_shows_value(
    token: HarvestToken, redaction: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    log = logging.getLogger("some.random.logger")
    log.info(f"{token}")
    log.info(repr(token))
    log.info("%s", token)
    log.info("Bearer %s", RAW)
    log.warning("Authorization: Bearer %s failed", RAW)
    log.error("raw %s here", RAW)
    log.error("dict %s", {"k": RAW})
    assert RAW not in caplog.text
    assert "HarvestToken(***)" in caplog.text


def test_filter_scrubs_any_logger_and_exceptions(
    redaction: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("", "httpx", "a.b.c", "uvicorn.access"):
        logging.getLogger(name).warning("tok=%s", RAW)
    try:
        raise RuntimeError(f"boom {RAW}")
    except RuntimeError:
        logging.getLogger("x").exception("failed")
    assert RAW not in caplog.text


def test_uninstall_restores_factory(token: HarvestToken) -> None:
    before = logging.getLogRecordFactory()
    uninstall = install_log_redaction(token)
    assert logging.getLogRecordFactory() is not before
    uninstall()
    assert logging.getLogRecordFactory() is before


def test_malformed_log_call_does_not_raise(
    token: HarvestToken, redaction: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger("m").warning("%s %s", 1)
    logging.getLogger("m").warning("%(a)s", {"b": RAW})
    logging.getLogger("m").warning("%s %s", RAW)
    assert RAW not in caplog.text


def test_no_instance_dict(token: HarvestToken) -> None:
    with pytest.raises(TypeError):
        vars(token)
    with pytest.raises(AttributeError):
        token.__dict__  # noqa: B018
    with pytest.raises(TypeError):
        dataclasses.asdict(token)  # type: ignore[call-overload]


def test_setattr_delattr_raise(token: HarvestToken) -> None:
    with pytest.raises(AttributeError):
        token._value = "x"
    with pytest.raises(AttributeError):
        token.other = "x"  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        del token._value
