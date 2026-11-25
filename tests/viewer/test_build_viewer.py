"""build_viewer writes index.html next to the data bundle; the wheel ships the template."""

import json
from pathlib import Path

from exit_archive.viewer import build_viewer
from exit_archive.viewer.bundle import OUTPUT_REL


def test_build_viewer_writes_index_and_data(tmp_path: Path) -> None:
    index = build_viewer(tmp_path, "Acme & Co")
    assert index == tmp_path / "index.html"
    assert index.is_file()
    data = tmp_path.joinpath(*OUTPUT_REL)
    assert data.is_file()
    assert "Acme" in data.read_text("utf-8")


def test_index_is_the_packaged_template_unchanged(tmp_path: Path) -> None:
    from importlib import resources

    template = resources.files("exit_archive.viewer").joinpath("index.html").read_bytes()
    assert build_viewer(tmp_path, None).read_bytes() == template


def test_company_name_is_not_templated_into_the_html(tmp_path: Path) -> None:
    name = "</script><img src=x onerror=alert(1)>"
    html = build_viewer(tmp_path, name).read_text("utf-8")
    assert name not in html
    payload = tmp_path.joinpath(*OUTPUT_REL).read_text("utf-8")
    assert "<" not in payload
    body = payload.removeprefix("window.EXIT_ARCHIVE_DATA = ").removesuffix(";\n")
    assert json.loads(body)["company_name"] == name


def test_build_viewer_is_idempotent_and_leaves_no_part_files(tmp_path: Path) -> None:
    build_viewer(tmp_path, "A")
    build_viewer(tmp_path, "B")
    assert not list(tmp_path.rglob("*.part"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data", "index.html"]


def test_wheel_includes_the_template() -> None:
    import tomllib

    config = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text("utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "src/exit_archive" in wheel["packages"]
