"""The exporter ships index.html + viewer-data.js, hashed into MANIFEST.json."""

import hashlib
from pathlib import Path

import respx

from exit_archive.exporter import export_account
from exit_archive.secrets import HarvestToken
from tests.test_exporter import SECRET, leftovers, mock_harvest, read_zip


@respx.mock
async def test_zip_has_viewer_files_listed_in_manifest(tmp_path: Path) -> None:
    mock_harvest()
    result = await export_account(HarvestToken(SECRET), tmp_path)
    members, manifest = read_zip(result.zip_path)
    top = next(n for n in members if n.endswith("/MANIFEST.json")).removesuffix("MANIFEST.json")
    by_path = {m["path"]: m for m in manifest["files"]}
    for rel in ("index.html", "data/viewer-data.js"):
        assert top + rel in members
        assert by_path[rel]["sha256"] == hashlib.sha256(members[top + rel]).hexdigest()
        assert by_path[rel]["bytes"] == len(members[top + rel])
        assert SECRET.encode() not in members[top + rel]
    assert b"EXIT_ARCHIVE_DATA" in members[top + "data/viewer-data.js"]
    assert b"Content-Security-Policy" in members[top + "index.html"]


@respx.mock
async def test_readme_points_to_index_html(tmp_path: Path) -> None:
    mock_harvest()
    result = await export_account(HarvestToken(SECRET), tmp_path)
    members, _ = read_zip(result.zip_path)
    readme = next(v for k, v in members.items() if k.endswith("/README.txt")).decode()
    assert "index.html" in readme


@respx.mock
async def test_viewer_data_reflects_exported_records(tmp_path: Path) -> None:
    mock_harvest()
    result = await export_account(HarvestToken(SECRET), tmp_path)
    members, _ = read_zip(result.zip_path)
    data = next(v for k, v in members.items() if k.endswith("/data/viewer-data.js")).decode()
    assert '"company_name":"' in data and "Fake Co" in data


@respx.mock
async def test_failed_export_still_leaves_nothing(tmp_path: Path) -> None:
    mock_harvest()
    respx.get("https://api.harvestapp.com/v2/clients").respond(500)
    import pytest

    from exit_archive.errors import ExitArchiveError

    with pytest.raises(ExitArchiveError):
        await export_account(HarvestToken(SECRET), tmp_path)
    assert leftovers(tmp_path) == []
