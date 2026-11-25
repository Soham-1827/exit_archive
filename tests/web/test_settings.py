"""Settings.from_env: validation and messages that name the variable, never a value."""

import subprocess
import sys
from pathlib import Path

import pytest

from exit_archive.web.settings import Settings, SettingsError


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"DATA_DIR": str(tmp_path), **extra}


_PAID = {
    "STRIPE_SECRET_KEY": "rk_test_FAKEFAKE",
    "STRIPE_PAYMENT_LINK_URL": "https://buy.stripe.com/test_FAKE",
}


def test_defaults(tmp_path: Path) -> None:
    s = Settings.from_env(_env(tmp_path, **_PAID))
    assert s.data_dir == tmp_path
    assert s.max_concurrent_jobs == 2
    assert s.payment_required is True
    assert s.stripe_payment_link_url == "https://buy.stripe.com/test_FAKE"
    assert s.public_base_url is None
    assert s.stripe_secret_key is not None
    assert s.stripe_secret_key.reveal() == "rk_test_FAKEFAKE"


def test_all_values_read(tmp_path: Path) -> None:
    s = Settings.from_env(
        _env(
            tmp_path,
            MAX_CONCURRENT_JOBS="8",
            PAYMENT_REQUIRED="false",
            STRIPE_PAYMENT_LINK_URL="https://buy.example/abc",
            PUBLIC_BASE_URL="https://app.example",
            STRIPE_SECRET_KEY="sk_test_x",  # noqa: S106
        )
    )
    assert (s.max_concurrent_jobs, s.payment_required) == (8, False)
    assert s.stripe_payment_link_url == "https://buy.example/abc"
    assert s.public_base_url == "https://app.example"
    assert s.stripe_secret_key is not None
    assert s.stripe_secret_key.reveal() == "sk_test_x"


def test_secret_key_hidden_in_repr(tmp_path: Path) -> None:
    s = Settings.from_env(
        _env(
            tmp_path,
            STRIPE_SECRET_KEY="sk_test_hidden",  # noqa: S106
            STRIPE_PAYMENT_LINK_URL="https://buy.stripe.com/test_FAKE",
        )
    )
    assert "sk_test_hidden" not in repr(s)
    assert "sk_test_hidden" not in str(s)


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ({}, "DATA_DIR"),
        ({"DATA_DIR": "  "}, "DATA_DIR"),
        ({"MAX_CONCURRENT_JOBS": "0"}, "MAX_CONCURRENT_JOBS"),
        ({"MAX_CONCURRENT_JOBS": "9"}, "MAX_CONCURRENT_JOBS"),
        ({"MAX_CONCURRENT_JOBS": "abc"}, "MAX_CONCURRENT_JOBS"),
        ({"PAYMENT_REQUIRED": "yes"}, "PAYMENT_REQUIRED"),
        ({"PAYMENT_REQUIRED": "True"}, "PAYMENT_REQUIRED"),
        ({"STRIPE_PAYMENT_LINK_URL": "http://buy.example/x"}, "STRIPE_PAYMENT_LINK_URL"),
        ({"STRIPE_PAYMENT_LINK_URL": "https://"}, "STRIPE_PAYMENT_LINK_URL"),
        ({"PUBLIC_BASE_URL": "http://example.com"}, "PUBLIC_BASE_URL"),
        ({"PUBLIC_BASE_URL": "ftp://localhost"}, "PUBLIC_BASE_URL"),
        ({"PUBLIC_BASE_URL": "http://localhost:abc"}, "PUBLIC_BASE_URL"),
    ],
)
def test_invalid_names_the_variable_and_hides_the_value(
    tmp_path: Path, env: dict[str, str], name: str
) -> None:
    full = {"DATA_DIR": str(tmp_path), **env} if "DATA_DIR" not in env else env
    if name == "DATA_DIR" and not env:
        full = {}
    with pytest.raises(SettingsError) as info:
        Settings.from_env(full)
    assert name in str(info.value)
    for value in env.values():
        if len(value.strip()) > 2:
            assert value not in str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://app.example",
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:8765",
    ],
)
def test_public_base_url_accepted(tmp_path: Path, url: str) -> None:
    assert (
        Settings.from_env(
            _env(tmp_path, PAYMENT_REQUIRED="false", PUBLIC_BASE_URL=url)
        ).public_base_url
        == url
    )


def test_empty_optional_values_count_as_unset(tmp_path: Path) -> None:
    s = Settings.from_env(
        _env(
            tmp_path,
            PAYMENT_REQUIRED="false",
            STRIPE_PAYMENT_LINK_URL="",
            PUBLIC_BASE_URL="",
            MAX_CONCURRENT_JOBS="",
        )
    )
    assert s.stripe_payment_link_url is None
    assert s.max_concurrent_jobs == 2


def test_importing_app_module_with_empty_environment_does_not_raise(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import exit_archive.web.app as m; print(m.app.title)"],
        env={},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
