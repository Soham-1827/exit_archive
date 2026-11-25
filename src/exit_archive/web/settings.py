"""Environment settings. Errors name the variable and never print its value."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

__all__ = ["Settings", "SettingsError"]

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
MIN_JOBS, MAX_JOBS, DEFAULT_JOBS = 1, 8, 2


class SettingsError(ValueError):
    """A configuration variable is missing or invalid; the message holds its name only."""


def _bad(name: str, rule: str) -> SettingsError:
    return SettingsError(f"{name} {rule}")


def _parts(name: str, value: str) -> tuple[str, str]:
    """Return (scheme, hostname) of a URL, or raise naming ``name``."""
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError on a bad port
    except ValueError:
        raise _bad(name, "is not a valid URL") from None
    if parts.username or parts.password or not parts.hostname or any(c.isspace() for c in value):
        raise _bad(name, "is not a valid URL")
    return parts.scheme, parts.hostname


def _optional(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name, "").strip()
    return value or None


def _jobs(environ: Mapping[str, str]) -> int:
    raw = _optional(environ, "MAX_CONCURRENT_JOBS")
    if raw is None:
        return DEFAULT_JOBS
    if not (raw.isascii() and raw.isdigit()) or not MIN_JOBS <= int(raw) <= MAX_JOBS:
        raise _bad("MAX_CONCURRENT_JOBS", f"must be a whole number from {MIN_JOBS} to {MAX_JOBS}")
    return int(raw)


def _payment_required(environ: Mapping[str, str]) -> bool:
    raw = _optional(environ, "PAYMENT_REQUIRED")
    if raw is None or raw == "true":
        return True
    if raw == "false":
        return False
    raise _bad("PAYMENT_REQUIRED", "must be exactly true or false")


def _link(environ: Mapping[str, str]) -> str | None:
    value = _optional(environ, "STRIPE_PAYMENT_LINK_URL")
    if value is not None and _parts("STRIPE_PAYMENT_LINK_URL", value)[0] != "https":
        raise _bad("STRIPE_PAYMENT_LINK_URL", "must be an https URL")
    return value


def _base_url(environ: Mapping[str, str]) -> str | None:
    value = _optional(environ, "PUBLIC_BASE_URL")
    if value is None:
        return None
    scheme, host = _parts("PUBLIC_BASE_URL", value)
    if scheme == "https" or (scheme == "http" and host in _LOCAL_HOSTS):
        return value
    raise _bad("PUBLIC_BASE_URL", "must be https (or http on localhost / 127.0.0.1)")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_concurrent_jobs: int = DEFAULT_JOBS
    payment_required: bool = True
    stripe_payment_link_url: str | None = None
    public_base_url: str | None = None
    stripe_secret_key: str | None = field(default=None, repr=False)  # validated in T3.3

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        data_dir = _optional(environ, "DATA_DIR")
        if data_dir is None:
            raise _bad("DATA_DIR", "is required")
        return cls(
            data_dir=Path(data_dir),
            max_concurrent_jobs=_jobs(environ),
            payment_required=_payment_required(environ),
            stripe_payment_link_url=_link(environ),
            public_base_url=_base_url(environ),
            stripe_secret_key=_optional(environ, "STRIPE_SECRET_KEY"),
        )
