import pytest

from exit_archive import __version__
from exit_archive.cli import main


def test_version_constant() -> None:
    assert __version__ == "0.1.0"


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_no_arguments_returns_zero() -> None:
    assert main([]) == 0
