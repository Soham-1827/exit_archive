"""Settings.from_env Stripe rules: names the variable, never the value."""

from pathlib import Path

import pytest

from exit_archive.secrets import Secret
from exit_archive.web.settings import Settings, SettingsError

KEY = "rk_test_FAKEFAKE"
LINK = "https://buy.stripe.com/test_FAKE"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"DATA_DIR": str(tmp_path), **extra}


@pytest.mark.parametrize(
    "key",
    ["sk_test_AbC123", "sk_live_AbC123", "rk_test_FAKEFAKE", "rk_live_Z"],
    ids=["sk-test", "sk-live", "rk-test", "rk-live"],
)
def test_valid_keys(tmp_path: Path, key: str) -> None:
    s = Settings.from_env(_env(tmp_path, STRIPE_SECRET_KEY=key, STRIPE_PAYMENT_LINK_URL=LINK))
    assert isinstance(s.stripe_secret_key, Secret)
    assert s.stripe_secret_key.reveal() == key
    assert key not in repr(s) + str(s)


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ({"STRIPE_PAYMENT_LINK_URL": LINK}, "STRIPE_SECRET_KEY"),
        (
            {"STRIPE_SECRET_KEY": "pk_test_FAKEFAKE", "STRIPE_PAYMENT_LINK_URL": LINK},
            "STRIPE_SECRET_KEY",
        ),
        (
            {"STRIPE_SECRET_KEY": "rk_prod_FAKE", "STRIPE_PAYMENT_LINK_URL": LINK},
            "STRIPE_SECRET_KEY",
        ),
        (
            {"STRIPE_SECRET_KEY": "rk_test_FAKE-X", "STRIPE_PAYMENT_LINK_URL": LINK},
            "STRIPE_SECRET_KEY",
        ),
        ({"STRIPE_SECRET_KEY": KEY}, "STRIPE_PAYMENT_LINK_URL"),
    ],
)
def test_payment_required_needs_valid_vars(tmp_path: Path, env: dict[str, str], name: str) -> None:
    with pytest.raises(SettingsError) as info:
        Settings.from_env(_env(tmp_path, PAYMENT_REQUIRED="true", **env))
    assert name in str(info.value)
    for value in env.values():
        assert value not in str(info.value)


def test_default_payment_required_also_enforced(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="STRIPE_SECRET_KEY"):
        Settings.from_env(_env(tmp_path))


def test_not_required_needs_nothing(tmp_path: Path) -> None:
    s = Settings.from_env(_env(tmp_path, PAYMENT_REQUIRED="false"))
    assert s.stripe_secret_key is None


def test_pk_key_rejected_even_when_payment_not_required(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="STRIPE_SECRET_KEY") as info:
        Settings.from_env(_env(tmp_path, PAYMENT_REQUIRED="false", STRIPE_SECRET_KEY="pk_test_X"))  # noqa: S106
    assert "pk_test_X" not in str(info.value)


def test_existing_variables_are_reported_before_stripe_ones(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="PUBLIC_BASE_URL"):
        Settings.from_env(_env(tmp_path, PUBLIC_BASE_URL="http://example.com"))
