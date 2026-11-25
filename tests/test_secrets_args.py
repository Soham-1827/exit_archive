"""_scrub_record keeps the record shape (msg, args) unless a secret is present (W1)."""

import logging
from typing import Any

import pytest

from exit_archive.secrets import HarvestToken, install_log_redaction

TOKEN = "tok-ARGS-0123456789abcdef"  # noqa: S105


@pytest.fixture
def hooked() -> Any:
    uninstall = install_log_redaction(HarvestToken(TOKEN))
    yield
    uninstall()


def _record(msg: object, args: object) -> logging.LogRecord:
    return logging.getLogger("t").makeRecord("t", logging.INFO, __file__, 1, msg, args, None)


def test_clean_record_keeps_msg_and_args_identically(hooked: None) -> None:
    args = ("127.0.0.1:1", "GET", "/healthz", "1.1", 200)
    rec = _record('%s - "%s %s HTTP/%s" %d', args)
    assert rec.msg == '%s - "%s %s HTTP/%s" %d'
    assert rec.args is args or rec.args == args
    assert rec.getMessage().endswith("200")


def test_secret_in_one_of_five_args_keeps_five_args(hooked: None) -> None:
    rec = _record("%s %s %s %s %d", ("a", f"x {TOKEN} y", "c", "d", 5))
    assert isinstance(rec.args, tuple)
    assert len(rec.args) == 5
    assert rec.args[0] == "a"
    assert rec.args[4] == 5
    assert TOKEN not in rec.getMessage()
    assert TOKEN not in repr(rec.args)


def test_secret_in_msg_with_args_is_redacted_in_place(hooked: None) -> None:
    rec = _record(f"{TOKEN} says %s", ("hi",))
    assert rec.args == ("hi",)
    assert rec.getMessage() == "*** says hi"


def test_mapping_arg_values_are_redacted(hooked: None) -> None:
    rec = _record("%(who)s %(n)d", {"who": f"Bearer {TOKEN}", "n": 3})
    assert isinstance(rec.args, dict)
    assert set(rec.args) == {"who", "n"}
    assert rec.getMessage() == "*** 3"


def test_token_split_across_msg_and_args_falls_back_to_preformatting(hooked: None) -> None:
    head, tail = TOKEN[:10], TOKEN[10:]
    rec = _record(f"{head}%s", (tail,))
    assert TOKEN not in rec.getMessage()
    assert rec.args == ()
    assert "***" in rec.getMessage()


def test_non_str_arg_whose_str_holds_the_token_is_redacted(hooked: None) -> None:
    class Holder:
        def __str__(self) -> str:
            return f"holder({TOKEN})"

    rec = _record("value=%s", (Holder(),))
    assert TOKEN not in rec.getMessage()
    assert len(rec.args) == 1  # type: ignore[arg-type]


def test_non_str_arg_with_numeric_format_falls_back(hooked: None) -> None:
    class Num:
        def __int__(self) -> int:
            return 1

        def __str__(self) -> str:
            return TOKEN

    rec = _record("%d", (Num(),))
    assert TOKEN not in rec.getMessage()


def test_malformed_call_still_never_raises_and_is_scrubbed(hooked: None) -> None:
    rec = _record(f"%s %s {TOKEN}", ("only-one",))
    assert TOKEN not in rec.getMessage()
