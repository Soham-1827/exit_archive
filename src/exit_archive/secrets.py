"""Customer token wrapper and log redaction. The token lives in memory only."""

import itertools
import logging
import re
import threading
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


def _redact_all(text: str, tokens: tuple[HarvestToken, ...]) -> str:
    for token in tokens:  # longest first, so a token containing another is fully masked
        text = text.replace(token.reveal(), _MASK)
    return _BEARER.sub(_MASK, text)


def _has_secret(text: str, tokens: tuple[HarvestToken, ...]) -> bool:
    return bool(_BEARER.search(text)) or any(t.reveal() in text for t in tokens)


def _scrub_value(value: object, tokens: tuple[HarvestToken, ...]) -> object:
    """Redact a str, or a non-str whose ``str()`` holds a secret; anything else is kept."""
    if isinstance(value, str):
        return _redact_all(value, tokens)
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - an arg that cannot render is left for the fallback
        return value
    return _redact_all(text, tokens) if _has_secret(text, tokens) else value


def _scrub_args(args: object, tokens: tuple[HarvestToken, ...]) -> object:
    """Redact args in place: same tuple length and order, or the mapping's same keys."""
    if isinstance(args, tuple):
        return tuple(_scrub_value(a, tokens) for a in args)
    if isinstance(args, dict):
        return {k: _scrub_value(v, tokens) for k, v in args.items()}
    return args


def _scrub_message(record: logging.LogRecord, tokens: tuple[HarvestToken, ...]) -> None:
    """Redact msg/args without changing the record's shape when that is possible."""
    try:
        message = record.getMessage()
    except (TypeError, ValueError, KeyError):
        record.msg = _redact_all(f"{record.msg!s} args={record.args!r}", tokens)
        record.args = ()
        return
    if not _has_secret(message, tokens):
        return  # nothing to hide: formatters that read record.args keep working
    original = (record.msg, record.args)
    if isinstance(record.msg, str):
        record.msg = _redact_all(record.msg, tokens)
    record.args = _scrub_args(record.args, tokens)  # type: ignore[assignment]
    try:
        rendered = record.getMessage()
    except Exception:  # noqa: BLE001 - any rendering failure means "pre-format instead"
        rendered = None
    if rendered is None or _has_secret(rendered, tokens):
        record.msg, record.args = original
        record.msg = _redact_all(message, tokens)  # e.g. a token split across msg and args
        record.args = ()


def _scrub_record(record: logging.LogRecord, tokens: tuple[HarvestToken, ...]) -> None:
    """Scrub the token values and bearer credentials from a record in place."""
    _scrub_message(record, tokens)
    if record.exc_info and not record.exc_text:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = _redact_all(record.exc_text, tokens)
    if record.stack_info:
        record.stack_info = _redact_all(record.stack_info, tokens)


class _Registry:
    """Process-wide, lock-guarded set of active redactions behind one record-factory hook.

    Each ``register`` call gets its own id, so the same token may be registered many times and
    uninstalls may run in any order. The hook is installed with the first registration and the
    exact original factory is restored when the last one is removed, provided nobody has
    wrapped our hook in the meantime. If someone did, their factory is left alone and our hook
    stays in their chain as a pass-through (it scrubs nothing while no token is registered).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._active: dict[int, HarvestToken | None] = {}
        self._tokens: tuple[HarvestToken, ...] = ()  # read lock-free by the hook
        self._original: Callable[..., logging.LogRecord] | None = None
        self._left_original: Callable[..., logging.LogRecord] | None = None
        self._local = threading.local()
        self._hook_fn: Callable[..., logging.LogRecord] = self._hook

    def _hook(self, *args: object, **kwargs: object) -> logging.LogRecord:
        if getattr(self._local, "busy", False):
            # Our hook is in the chain twice (a wrapper around it was re-wrapped by us):
            # the inner call builds the record from the factory below our first install.
            inner: Callable[..., logging.LogRecord] = self._left_original or logging.LogRecord
            return inner(*args, **kwargs)
        self._local.busy = True
        try:
            base: Callable[..., logging.LogRecord] = (
                self._original or self._left_original or logging.LogRecord
            )
            record = base(*args, **kwargs)
        finally:
            self._local.busy = False
        tokens = self._tokens
        if tokens or self._active:  # with nothing registered the hook is a pass-through
            _scrub_record(record, tokens)
        return record

    def _rebuild(self) -> None:
        unique = {t.reveal(): t for t in self._active.values() if t is not None}
        self._tokens = tuple(sorted(unique.values(), key=lambda t: -len(t.reveal())))

    def register(self, token: HarvestToken | None) -> Callable[[], None]:
        with self._lock:
            if not self._active:
                if logging.getLogRecordFactory() is not self._hook_fn:
                    self._original = logging.getLogRecordFactory()
                    logging.setLogRecordFactory(self._hook_fn)
                else:  # our pass-through hook is still current: reuse the factory below it
                    self._original, self._left_original = self._left_original, None
            reg_id = next(self._ids)
            self._active[reg_id] = token
            self._rebuild()

        def uninstall() -> None:
            self._release(reg_id)

        return uninstall

    def _release(self, reg_id: int) -> None:
        with self._lock:
            if self._active.pop(reg_id, _MISSING) is _MISSING:
                return  # already uninstalled
            self._rebuild()
            if self._active:
                return
            original, self._original = self._original, None
            if logging.getLogRecordFactory() is self._hook_fn:
                self._left_original = None
                if original is not None:
                    logging.setLogRecordFactory(original)
            else:
                self._left_original = original  # someone wrapped us: stay as a pass-through


_MISSING: object = object()
_REGISTRY = _Registry()


def install_log_redaction(token: HarvestToken | None) -> Callable[[], None]:
    """Scrub every record from every logger; returns an idempotent function that undoes it.

    All registrations share one process-wide record-factory hook, so overlapping jobs are safe
    whatever order they finish in. ``None`` scrubs ``Bearer`` credentials only.
    """
    return _REGISTRY.register(token)
