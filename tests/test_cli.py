import logging
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import respx

from exit_archive import __version__
from exit_archive.cli import main
from tests.test_exporter import ID, SECRET, mock_harvest


def test_version_constant() -> None:
    assert __version__ == "0.1.0"


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_no_arguments_returns_zero() -> None:
    assert main([]) == 0


# ---- T1.6.2: export command -------------------------------------------------------------

FORBIDDEN_IN_LOGS = ("/client/invoices/", "/client/estimates/", "fakekey101", "estkey0", "X-Amz-")


@pytest.fixture
def token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARVEST_TOKEN", SECRET)
    monkeypatch.delenv("HARVEST_ACCOUNT_ID", raising=False)


@respx.mock
def test_export_ok_exits_zero_and_prints_zip(
    tmp_path: Path, token_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_harvest()
    assert main(["export", "--out", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    zips = list(tmp_path.glob("*.zip"))
    assert len(zips) == 1 and str(zips[0]) in out
    assert "records" in err and "files" in err  # progress on stderr
    assert SECRET not in out + err
    assert SECRET.encode() not in zips[0].read_bytes()
    with zipfile.ZipFile(zips[0]) as zf:
        assert all(SECRET.encode() not in zf.read(n) for n in zf.namelist())


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_token_exits_2_naming_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("HARVEST_TOKEN", raising=False)
    else:
        monkeypatch.setenv("HARVEST_TOKEN", value)
    assert main(["export", "--out", str(tmp_path)]) == 2
    assert "HARVEST_TOKEN" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_missing_token_via_subprocess_exits_2(tmp_path: Path) -> None:
    env = {**os.environ, "HARVEST_TOKEN": ""}
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "exit_archive.cli", "export", "--out", str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert proc.returncode == 2
    assert "HARVEST_TOKEN" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_help_shows_out_and_account_id_but_no_token_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["export", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--out" in text and "--account-id" in text
    assert "--token" not in text


def test_token_flag_is_rejected_and_out_is_required(
    tmp_path: Path, token_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["export", "--out", str(tmp_path), "--token", "abc"])
    assert exc.value.code == 2
    assert "abc" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc2:
        main(["export"])
    assert exc2.value.code == 2


@respx.mock
def test_401_on_discovery_exits_3_without_token(
    tmp_path: Path, token_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{ID}/api/v2/accounts").respond(401)
    assert main(["export", "--out", str(tmp_path)]) == 3
    out, err = capsys.readouterr()
    assert SECRET not in out + err
    assert "Traceback" not in err and "rejected" in err
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_401_mid_export_exits_3(tmp_path: Path, token_env: None) -> None:
    routes = mock_harvest()
    routes["pay102"].respond(401)
    assert main(["export", "--out", str(tmp_path)]) == 3
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_other_export_error_exits_4(
    tmp_path: Path, token_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_harvest(accounts=2)
    assert main(["export", "--out", str(tmp_path)]) == 4
    err = capsys.readouterr().err
    assert "Several Harvest accounts" in err and "Traceback" not in err


@respx.mock
def test_account_id_flag_and_env_default(
    tmp_path: Path, token_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = mock_harvest(accounts=2)
    assert main(["export", "--out", str(tmp_path / "a"), "--account-id", "43"]) == 0
    monkeypatch.setenv("HARVEST_ACCOUNT_ID", "42")
    assert main(["export", "--out", str(tmp_path / "b")]) == 0
    ids = [c.request.headers["Harvest-Account-Id"] for c in routes["company"].calls]
    assert ids == ["43", "42"]


def test_bad_account_id_env_exits_2(
    tmp_path: Path, token_env: None, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setenv("HARVEST_ACCOUNT_ID", "not-a-number")
    assert main(["export", "--out", str(tmp_path)]) == 2
    assert "HARVEST_ACCOUNT_ID" in capsys.readouterr().err


@respx.mock
def test_filesystem_error_exits_4_without_traceback(
    tmp_path: Path, token_env: None, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    mock_harvest()

    def broken(self: zipfile.ZipFile, *args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(zipfile.ZipFile, "write", broken)
    assert main(["export", "--out", str(tmp_path)]) == 4
    assert "Traceback" not in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_unexpected_error_exits_4_with_type_only(
    tmp_path: Path, token_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = mock_harvest()
    routes["time_entries"].mock(side_effect=RuntimeError(f"secret {SECRET}"))
    assert main(["export", "--out", str(tmp_path)]) == 4
    out, err = capsys.readouterr()
    assert SECRET not in out + err and "Traceback" not in err


@respx.mock
def test_default_logging_hides_capability_urls(
    tmp_path: Path,
    token_env: None,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_harvest()
    caplog.set_level(logging.DEBUG)  # as permissive as a user could make the root logger
    assert main(["export", "--out", str(tmp_path)]) == 0
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    out, err = capsys.readouterr()
    everything = out + err + caplog.text
    assert SECRET not in everything
    for needle in FORBIDDEN_IN_LOGS:
        assert needle not in everything
