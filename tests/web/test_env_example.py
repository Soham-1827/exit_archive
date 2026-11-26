"""`.env.example` lists names only."""

from pathlib import Path

EXPECTED = {
    "HARVEST_TOKEN",
    "HARVEST_ACCOUNT_ID",
    "DATA_DIR",
    "MAX_CONCURRENT_JOBS",
    "PAYMENT_REQUIRED",
    "STRIPE_PAYMENT_LINK_URL",
    "STRIPE_SECRET_KEY",
    "PUBLIC_BASE_URL",
    "TRUSTED_PROXY_HOPS",
}


def test_env_example_has_names_with_empty_values() -> None:
    path = Path(__file__).resolve().parents[2] / ".env.example"
    pairs = [line.partition("=") for line in path.read_text().splitlines() if line.strip()]
    assert {name for name, _, _ in pairs} == EXPECTED
    assert all(value == "" for _, _, value in pairs)
