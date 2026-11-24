"""Customer token wrapper and log redaction. The token lives in memory only."""

import logging
import re
from collections.abc import Callable
from typing import NoReturn

_MASK = "***"
_BEARER = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


class HarvestToken:
    """A Harvest API token whose text form never shows the value."""

    __slots__ = ("_value",)
    _value: str

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("token must not be empty")
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("HarvestToken is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("HarvestToken is immutable")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, HarvestToken) and self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return "HarvestToken(***)"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return "HarvestToken(***)"

    def __reduce__(self) -> NoReturn:
        raise TypeError("HarvestToken cannot be pickled or copied")

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError("HarvestToken cannot be pickled or copied")

    def reveal(self) -> str:
        """Return the raw token. Only the HTTP client should call this."""
        return self._value


def redact(text: str, token: HarvestToken | None) -> str:
    """Replace the token value and any ``Bearer <credential>`` with ``***``."""
    if token is not None:
        text = text.replace(token.reveal(), _MASK)
    return _BEARER.sub(_MASK, text)


class RedactionFilter(logging.Filter):
    """Scrub the token from a log record's message and traceback text."""

    def __init__(self, token: HarvestToken | None) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError, KeyError):
            message = f"{record.msg!s} args={record.args!r}"
        record.msg = redact(message, self._token)
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self._token)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, self._token)
        return True


def install_log_redaction(token: HarvestToken | None) -> Callable[[], None]:
    """Scrub every record from every logger; returns a function that undoes it.

    Wraps the log record factory, so it applies regardless of logger or handler.
    """
    previous = logging.getLogRecordFactory()
    scrubber = RedactionFilter(token)

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        scrubber.filter(record)
        return record

    logging.setLogRecordFactory(factory)

    def uninstall() -> None:
        logging.setLogRecordFactory(previous)

    return uninstall
