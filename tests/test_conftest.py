from pathlib import Path

from tests.conftest import LiveCredentials, load_live_credentials


def test_env_vars_win(tmp_path: Path) -> None:
    creds = load_live_credentials({"HARVEST_TOKEN": "t", "HARVEST_ACCOUNT_ID": "1"}, tmp_path)
    assert creds is not None
    assert (creds.token, creds.account_id) == ("t", "1")
    secret = LiveCredentials(token="sekret-token-value", account_id="987654")  # noqa: S106
    assert "sekret-token-value" not in repr(secret)
    assert "987654" not in repr(secret)
    assert "sekret-token-value" not in str(secret)


def test_file_fallback(tmp_path: Path) -> None:
    (tmp_path / ".harvest_test").write_text("# c\nHARVEST_TOKEN=abc\nHARVEST_ACCOUNT_ID='9'\n")
    creds = load_live_credentials({}, tmp_path)
    assert creds is not None
    assert (creds.token, creds.account_id) == ("abc", "9")


def test_missing_returns_none(tmp_path: Path) -> None:
    assert load_live_credentials({}, tmp_path) is None
