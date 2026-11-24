"""Shared pytest fixtures. Credential values are never printed."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_KEYS = ("HARVEST_TOKEN", "HARVEST_ACCOUNT_ID")


@dataclass(frozen=True)
class LiveCredentials:
    """Harvest credentials for live tests; repr hides the values."""

    token: str = field(repr=False)
    account_id: str = field(repr=False)


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_live_credentials(
    environ: dict[str, str] | os._Environ[str], home: Path
) -> LiveCredentials | None:
    """Read credentials from the environment, else from ``home/.harvest_test``."""
    values = {k: environ[k] for k in _KEYS if environ.get(k)}
    if len(values) < len(_KEYS):
        path = home / ".harvest_test"
        if path.is_file():
            values = {**_parse_env_file(path), **values}
    if all(values.get(k) for k in _KEYS):
        return LiveCredentials(
            token=values["HARVEST_TOKEN"], account_id=values["HARVEST_ACCOUNT_ID"]
        )
    return None


@pytest.fixture
def harvest_live_credentials() -> LiveCredentials:
    creds = load_live_credentials(os.environ, Path.home())
    if creds is None:
        pytest.skip("no Harvest live credentials (env vars or ~/.harvest_test)")
    return creds


def pytest_configure(config: pytest.Config) -> None:
    """Live-only runs select no unit tests, so the coverage gate is meaningless there."""
    expr = config.getoption("markexpr", default="") or ""
    if expr.strip() == "live":
        config.option.cov_fail_under = 0
        plugin = config.pluginmanager.get_plugin("_cov")
        if plugin is not None:
            plugin.options.cov_fail_under = 0
