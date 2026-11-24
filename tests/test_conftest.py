from pathlib import Path

import pytest

from tests.conftest import load_live_credentials


def test_env_vars_win(tmp_path: Path) -> None:
    creds = load_live_credentials({"HARVEST_TOKEN": "t", "HARVEST_ACCOUNT_ID": "1"}, tmp_path)
    assert creds is not None
    assert (creds.token, creds.account_id) == ("t", "1")
    assert "t" not in repr(creds).replace("LiveCredentials", "")


def test_file_fallback(tmp_path: Path) -> None:
    (tmp_path / ".harvest_test").write_text("# c\nHARVEST_TOKEN=abc\nHARVEST_ACCOUNT_ID='9'\n")
    creds = load_live_credentials({}, tmp_path)
    assert creds is not None
    assert (creds.token, creds.account_id) == ("abc", "9")


def test_missing_returns_none(tmp_path: Path) -> None:
    assert load_live_credentials({}, tmp_path) is None


def test_fixture_skips_or_returns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARVEST_TOKEN", "x")
    monkeypatch.setenv("HARVEST_ACCOUNT_ID", "2")
    assert load_live_credentials({"HARVEST_TOKEN": "x", "HARVEST_ACCOUNT_ID": "2"}, tmp_path)
