"""Adversarial probes for the exporter and CLI (tester-added). respx only; fake token."""

import logging
import os
import stat
import tracemalloc
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from exit_archive.cli import main
from exit_archive.exporter import export_account
from exit_archive.secrets import HarvestToken
from tests.test_exporter import API, ID, SECRET, mock_harvest


@pytest.fixture
def token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARVEST_TOKEN", SECRET)
    monkeypatch.delenv("HARVEST_ACCOUNT_ID", raising=False)


def test_unexpected_exception_prints_type_name_only(
    tmp_path: Path, token_env: None, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    async def boom(*a: Any, **k: Any) -> None:
        raise ValueError(f"Bearer {SECRET} {SECRET}")

    monkeypatch.setattr("exit_archive.cli.export_account", boom)
    assert main(["export", "--out", str(tmp_path)]) == 4
    out, err = capsys.readouterr()
    assert SECRET not in out + err and "Traceback" not in err
    assert "ValueError" in err


@respx.mock
def test_keyboard_interrupt_midexport_propagates_cleanly(
    tmp_path: Path, token_env: None, capsys: Any
) -> None:
    routes = mock_harvest()

    def interrupt(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    routes["time_entries"].mock(side_effect=interrupt)
    with pytest.raises(KeyboardInterrupt) as exc:
        main(["export", "--out", str(tmp_path)])
    # Report: not caught by the CLI (exit via Python's default SIGINT handling, code 130).
    assert SECRET not in repr(exc.value) and SECRET not in str(exc.value)
    out, err = capsys.readouterr()
    assert SECRET not in out + err
    assert list(tmp_path.iterdir()) == []
    assert logging.getLogRecordFactory().__name__ != "factory"  # redaction uninstalled


@respx.mock
@pytest.mark.parametrize("raw", ["  " + SECRET + "  ", SECRET + "\n", "\t" + SECRET + "\r\n"])
def test_token_whitespace_is_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("HARVEST_TOKEN", raw)
    monkeypatch.delenv("HARVEST_ACCOUNT_ID", raising=False)
    routes = mock_harvest()
    assert main(["export", "--out", str(tmp_path)]) == 0
    sent = routes["accounts"].calls[0].request.headers["Authorization"]
    assert sent == f"Bearer {SECRET}"


@respx.mock
def test_out_is_a_file(tmp_path: Path, token_env: None, capsys: Any) -> None:
    mock_harvest()
    target = tmp_path / "afile"
    target.write_text("x")
    code = main(["export", "--out", str(target)])
    err = capsys.readouterr().err
    assert code == 4 and "Traceback" not in err and SECRET not in err
    assert target.read_text() == "x"


@respx.mock
def test_out_read_only_dir(tmp_path: Path, token_env: None, capsys: Any) -> None:
    mock_harvest()
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(ro, os.W_OK):
            pytest.skip("running with privileges that ignore directory modes")
        code = main(["export", "--out", str(ro)])
    finally:
        ro.chmod(stat.S_IRWXU)
    err = capsys.readouterr().err
    assert code == 4 and "Traceback" not in err and SECRET not in err
    assert list(ro.iterdir()) == []


@respx.mock
def test_out_nonexistent_is_created_and_relative_works(
    tmp_path: Path, token_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_harvest()
    monkeypatch.chdir(tmp_path)
    assert main(["export", "--out", "deep/er/out"]) == 0
    assert [p.suffix for p in (tmp_path / "deep/er/out").iterdir()] == [".zip"]


@respx.mock
def test_out_failure_before_download_leaves_no_partial_files(
    tmp_path: Path, token_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_harvest()
    parent = tmp_path / "parent"
    parent.mkdir()
    parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(parent, os.W_OK):
            pytest.skip("privileged")
        assert main(["export", "--out", str(parent / "child")]) == 4
    finally:
        parent.chmod(stat.S_IRWXU)
    assert list(parent.iterdir()) == []


@respx.mock
def test_multi_account_without_id_exits_4_listing_ids_not_token(
    tmp_path: Path, token_env: None, capsys: Any
) -> None:
    mock_harvest(accounts=2)
    assert main(["export", "--out", str(tmp_path)]) == 4
    err = capsys.readouterr().err
    assert "42" in err and "43" in err and "Fake Co 0" in err and "Fake Co 1" in err
    assert SECRET not in err and list(tmp_path.iterdir()) == []


@respx.mock
def test_debug_root_logging_has_no_capability_strings(
    tmp_path: Path, token_env: None, caplog: Any, capsys: Any
) -> None:
    mock_harvest()
    s3 = "https://bucket.s3.amazonaws.com/r?X-Amz-Signature=abcdef&X-Amz-Credential=zzz"
    respx.get("https://fake-co.harvestapp.com/expenses/1/receipt").mock(
        return_value=httpx.Response(302, headers={"Location": s3})
    )
    respx.get(s3).mock(return_value=httpx.Response(200, content=b"IMG"))
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    assert main(["export", "--out", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    everything = out + err + caplog.text
    for needle in (
        "/client/invoices/",
        "/client/estimates/",
        "fakekey101",
        "fakekey102",
        "estkey0",
        "X-Amz-",
        "Bearer",
        "amazonaws",
        SECRET,
    ):
        assert needle not in everything, needle


@respx.mock
async def test_memory_5000_time_entries_streams(tmp_path: Path, token_env: None) -> None:
    routes = mock_harvest()
    total = 5000

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * 1000
        recs = [
            {"id": i, "notes": "n" * 150, "hours": 1.5, "project": {"id": i % 9}}
            for i in range(start, start + 1000)
        ]
        nxt = f"{API}/v2/time_entries?page={page + 1}" if page < total // 1000 else None
        return httpx.Response(200, json={"time_entries": recs, "links": {"next": nxt}})

    routes["time_entries"].mock(side_effect=handler)
    tracemalloc.start()
    result = await export_account(HarvestToken(SECRET), tmp_path)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result.counts["time_entries"] == total
    print("PEAK_BYTES", peak)
    assert peak < 20_000_000, peak


def test_exporter_keeps_only_slim_projections() -> None:
    import inspect

    import exit_archive.exporter as ex

    src = inspect.getsource(ex._Slim.remember)
    assert "time_entries" not in src
    assert '{"id": record.get("id"), "receipt": receipt}' in src
    assert ID  # keep import used
