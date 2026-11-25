"""Generic Secret base: same guarantees as HarvestToken."""

import copy
import logging
import pickle

import pytest

from exit_archive.secrets import HarvestToken, Secret, install_log_redaction

RAW = "rk_test_FAKEFAKE"


def test_masked_everywhere() -> None:
    s = Secret(RAW)
    assert RAW not in repr(s) + str(s) + f"{s}" + f"{s:>40}" + "%s %r" % (s, s)  # noqa: UP031
    assert s.reveal() == RAW


def test_empty_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        Secret("")


def test_immutable_slotted_unpicklable() -> None:
    s = Secret(RAW)
    with pytest.raises(AttributeError):
        s._value = "x"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del s._value
    assert not hasattr(s, "__dict__")
    for op in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            op(s)


def test_equality_is_type_strict_and_hashable() -> None:
    assert Secret(RAW) == Secret(RAW)
    assert Secret(RAW) != Secret("other")
    assert Secret(RAW) != RAW
    assert Secret(RAW) != HarvestToken(RAW)
    assert hash(Secret(RAW)) == hash(Secret(RAW))


def test_harvest_token_unchanged() -> None:
    t = HarvestToken(RAW)
    assert isinstance(t, Secret)
    assert repr(t) == str(t) == f"{t}" == "HarvestToken(***)"
    assert t.reveal() == RAW


def test_log_redaction_accepts_a_plain_secret(caplog: pytest.LogCaptureFixture) -> None:
    undo = install_log_redaction(Secret(RAW))
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger("x").warning("key is %s here", RAW)
    finally:
        undo()
    assert RAW not in caplog.text
