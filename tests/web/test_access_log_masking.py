"""T4.3a: job-id-shaped runs are masked in every logged path (gate finding A1)."""

import pytest

from exit_archive.web.accesslog import mask_path

ID = "Ab3_-xyz9Q" * 3  # 30 chars, job-id shaped


@pytest.mark.parametrize(
    "path",
    [f"/x/{ID}", f"/{ID}", f"/static/{ID}.css", f"//jobs/{ID}", f"/./jobs/{ID}", f"/jobs//{ID}"],
)
def test_id_shaped_run_is_masked(path: str) -> None:
    out = mask_path(path)
    assert ID not in out
    assert ID[:10] not in out
    assert "<x>" in out or "<id>" in out


def test_exact_outputs_for_non_routable_paths() -> None:
    assert mask_path(f"/x/{ID}") == "/x/<x>"
    assert mask_path(f"/static/{ID}.css") == "/static/<x>.css"


def test_long_path_with_id_at_offset_is_masked_before_cap() -> None:
    path = "/" + "a/" * 50 + ID + "/" + "b/" * 100
    assert len(path) > 300 and 100 <= path.index(ID) <= 150
    out = mask_path(path)
    assert ID not in out
    assert ID[:10] not in out
    assert "<x>" in out
    assert len(out) <= 120


def test_id_adjacent_to_non_ascii_is_masked() -> None:
    out = mask_path(f"/x/é{ID}é")
    assert ID[:10] not in out
    assert out == "/x/%E9<x>%E9"


@pytest.mark.parametrize(
    "path,expected",
    [
        (f"/jobs/{ID}", "/jobs/<id>"),
        (f"/jobs/{ID}/download", "/jobs/<id>/download"),
        (f"/jobs/{ID}/x/y", "/jobs/<id>/<x>/<x>"),
    ],
)
def test_routable_jobs_paths_unchanged(path: str, expected: str) -> None:
    assert mask_path(path) == expected


@pytest.mark.parametrize("path", ["/healthz", "/static/app.css", "/start", "/" + "a" * 19])
def test_short_and_plain_paths_unchanged(path: str) -> None:
    assert mask_path(path) == path


def test_twenty_char_run_is_masked() -> None:
    assert mask_path("/" + "a" * 20) == "/<x>"
