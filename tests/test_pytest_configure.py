"""pytest_configure disables the coverage gate for marker-only runs (live, e2e)."""

from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import pytest_configure


def fake_config(markexpr: str) -> tuple[Any, SimpleNamespace]:
    plugin_options = SimpleNamespace(cov_fail_under=80)
    plugin = SimpleNamespace(options=plugin_options)
    config = SimpleNamespace(
        getoption=lambda name, default="": markexpr if name == "markexpr" else default,
        option=SimpleNamespace(cov_fail_under=80),
        pluginmanager=SimpleNamespace(get_plugin=lambda _name: plugin),
    )
    return config, plugin_options


@pytest.mark.parametrize("expr", ["live", " live ", "e2e", "e2e "])
def test_marker_only_runs_disable_the_gate(expr: str) -> None:
    config, plugin_options = fake_config(expr)
    pytest_configure(config)
    assert config.option.cov_fail_under == 0
    assert plugin_options.cov_fail_under == 0


@pytest.mark.parametrize("expr", ["", "not live and not e2e", "live or e2e", "slow"])
def test_other_runs_keep_the_gate(expr: str) -> None:
    config, plugin_options = fake_config(expr)
    pytest_configure(config)
    assert config.option.cov_fail_under == 80
    assert plugin_options.cov_fail_under == 80


def test_missing_cov_plugin_is_tolerated() -> None:
    config = SimpleNamespace(
        getoption=lambda name, default="": "e2e",
        option=SimpleNamespace(cov_fail_under=80),
        pluginmanager=SimpleNamespace(get_plugin=lambda _name: None),
    )
    pytest_configure(config)  # type: ignore[arg-type]
    assert config.option.cov_fail_under == 0
