"""Fake-value checks that live credentials never leak via repr, str or pytest failure output."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.conftest import LiveCredentials, load_live_credentials

FAKE_TOKEN = "fake-token-VALUE-0123456789"  # noqa: S105
FAKE_ACCOUNT = "fake-acct-98765"


def _creds(tmp_path: Path) -> LiveCredentials:
    (tmp_path / ".harvest_test").write_text(
        f"HARVEST_TOKEN={FAKE_TOKEN}\nHARVEST_ACCOUNT_ID={FAKE_ACCOUNT}\n", encoding="utf-8"
    )
    creds = load_live_credentials({}, tmp_path)
    assert creds is not None
    return creds


def test_repr_str_format_hide_values(tmp_path: Path) -> None:
    creds = _creds(tmp_path)
    for text in (repr(creds), str(creds), f"{creds}", "%s %r" % (creds, creds), f"{creds!r}"):  # noqa: UP031
        assert FAKE_TOKEN not in text
        assert FAKE_ACCOUNT not in text


def test_malformed_file_error_paths_do_not_leak(tmp_path: Path) -> None:
    (tmp_path / ".harvest_test").write_text(f"HARVEST_TOKEN={FAKE_TOKEN}\n", encoding="utf-8")
    assert load_live_credentials({}, tmp_path) is None


def test_failing_assertion_output_hides_values(tmp_path: Path) -> None:
    (tmp_path / ".harvest_test").write_text(
        f"HARVEST_TOKEN={FAKE_TOKEN}\nHARVEST_ACCOUNT_ID={FAKE_ACCOUNT}\n", encoding="utf-8"
    )
    inner = tmp_path / "test_inner.py"
    root = Path(__file__).resolve().parent.parent
    inner.write_text(
        textwrap.dedent(
            f"""
            import sys
            from pathlib import Path
            sys.path.insert(0, {str(root)!r})
            from tests.conftest import load_live_credentials

            def test_boom():
                creds = load_live_credentials({{}}, Path({str(tmp_path)!r}))
                assert creds is None, creds
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", str(inner)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "LiveCredentials" in out
    assert FAKE_TOKEN not in out
    assert FAKE_ACCOUNT not in out


@pytest.mark.parametrize("missing", ["HARVEST_TOKEN", "HARVEST_ACCOUNT_ID"])
def test_partial_env_without_file_returns_none(tmp_path: Path, missing: str) -> None:
    env = {"HARVEST_TOKEN": FAKE_TOKEN, "HARVEST_ACCOUNT_ID": FAKE_ACCOUNT}
    del env[missing]
    assert load_live_credentials(env, tmp_path) is None
