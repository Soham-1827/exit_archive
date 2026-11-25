"""Tester-owned: a registered token must not survive any way a record can be rendered."""

import logging
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from exit_archive.secrets import HarvestToken, install_log_redaction

TOK = "tok-ZZ-9f8e7d6c5b4a3210"  # noqa: S105


@pytest.fixture
def seen() -> Iterator[list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []

    class H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    h = H()
    log = logging.getLogger("adv.secrets")
    log.addHandler(h)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    uninstall = install_log_redaction(HarvestToken(TOK))
    try:
        yield records
    finally:
        uninstall()
        log.removeHandler(h)


def _everything(record: logging.LogRecord) -> str:
    """Every textual rendering a handler or formatter could use."""
    parts = [record.getMessage(), str(record.msg), repr(record.args), str(record.exc_text)]
    parts.append(logging.Formatter("%(message)s").format(record))
    return "\n".join(parts)


class Strs:
    def __str__(self) -> str:
        return f"obj-{TOK}"

    __repr__ = __str__


class ReprOnly:
    def __str__(self) -> str:
        return "clean"

    def __repr__(self) -> str:
        return f"<R {TOK}>"


class Flaky:
    """__str__ is clean on the first call and leaks afterwards."""

    def __init__(self) -> None:
        self.n = 0

    def __str__(self) -> str:
        self.n += 1
        return "clean" if self.n == 1 else f"leak-{TOK}"


@pytest.mark.parametrize(
    ("msg", "args"),
    [
        ("v=%s", (TOK,)),
        ("v=%r", (TOK,)),
        ("v=%s n=%d", (TOK, 1)),
        ("v=%(t)s", ({"t": TOK},)),
        ("v=%s", (Strs(),)),
        ("v=%r", (ReprOnly(),)),
        ("v=%s", ([TOK],)),
        ("v=%s", ((1, TOK),)),
        ("v=%s", ({"k": TOK},)),
        ("tok" + "%s", (TOK[3:],)),  # split across msg and args
        ("%s%s", (TOK[:8], TOK[8:])),  # split across two args
        ("v=%x", (int.from_bytes(b"A", "big"),)),
    ],
)
def test_token_absent_from_every_rendering(
    seen: list[logging.LogRecord], msg: str, args: tuple[Any, ...]
) -> None:
    logging.getLogger("adv.secrets").info(msg, *args)
    (record,) = seen
    out = _everything(record)
    if TOK in msg + repr(args):  # sanity: only assert when the test really carries the token
        pass
    # the split cases only leak once rendered; render is what we check
    assert TOK not in out


def test_int_formatted_token(seen: list[logging.LogRecord]) -> None:
    n = 424242
    uninstall = install_log_redaction(HarvestToken(str(n)))
    try:
        logging.getLogger("adv.secrets").info("id=%d hex=%s", n, n)
    finally:
        uninstall()
    assert "424242" not in _everything(seen[-1])


def test_exc_info_and_stack_info(seen: list[logging.LogRecord]) -> None:
    try:
        raise RuntimeError(f"bad {TOK}")
    except RuntimeError:
        logging.getLogger("adv.secrets").error("failed", exc_info=sys.exc_info(), stack_info=True)
    (record,) = seen
    assert TOK not in _everything(record)
    assert TOK not in logging.Formatter("%(message)s").format(record)
    assert TOK not in str(record.stack_info)


def test_exception_object_as_arg(seen: list[logging.LogRecord]) -> None:
    logging.getLogger("adv.secrets").warning("err: %s / %r", ValueError(TOK), ValueError(TOK))
    assert TOK not in _everything(seen[0])


def test_exc_info_via_logger_exception_with_cause(seen: list[logging.LogRecord]) -> None:
    try:
        try:
            raise KeyError(TOK)
        except KeyError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError:
        logging.getLogger("adv.secrets").exception("boom")
    assert TOK not in _everything(seen[0])


@pytest.mark.xfail(
    strict=True,
    reason="FINDING-W2 (LOW): fast path renders once; an arg whose __str__ changes between "
    "calls leaks on the handler's later render",
)
def test_fast_path_not_fooled_by_flaky_str(seen: list[logging.LogRecord]) -> None:
    """__str__ differs between calls: whatever the hook decides, the final render is clean
    or this is reported as a known limit (non-deterministic args cannot be made safe)."""
    logging.getLogger("adv.secrets").info("v=%s", Flaky())
    (record,) = seen
    rendered_twice = [record.getMessage(), record.getMessage()]
    assert all(TOK not in r for r in rendered_twice)


def test_bearer_scrubbed_and_args_shape_kept_when_clean(seen: list[logging.LogRecord]) -> None:
    logging.getLogger("adv.secrets").info("%s %s", "a", "b")
    assert seen[0].args == ("a", "b")  # fast path untouched
    logging.getLogger("adv.secrets").info("auth %s", "Bearer SECRETX")
    assert "SECRETX" not in _everything(seen[1])


def test_percent_in_token_and_malformed_call(seen: list[logging.LogRecord]) -> None:
    logging.getLogger("adv.secrets").info("100%% " + TOK)  # token in msg itself
    assert TOK not in _everything(seen[0])
    logging.getLogger("adv.secrets").info("bad %d fmt " + TOK, "notint")  # render raises
    assert TOK not in _everything(seen[1])
