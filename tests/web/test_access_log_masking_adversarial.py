"""T4.3a tester: adversarial checks of access-log id masking."""

import logging
import re
from pathlib import Path
from urllib.parse import quote

import pytest

from exit_archive.web.accesslog import mask_path
from tests.web.helpers import build, post_job, settings_for, wait_idle

ID = "Qz8_-Lm3Nd" * 3
ACCESS = "exit_archive.access"
_UNSAFE = re.compile(r"[^\x21-\x7e]")


def _old_mask(path: str) -> str:
    """Pre-T4.3a implementation (HEAD b2a7640), reproduced for differential testing."""
    if path.startswith("/jobs/"):
        _, _, _, *rest = path.split("/")
        path = "/jobs/<id>" + "".join("/download" if s == "download" else "/<x>" for s in rest)
    return _UNSAFE.sub(lambda m: f"%{ord(m.group()):02X}", path)[:120]


@pytest.mark.parametrize(
    "path",
    [
        "/jobs/" + ID,
        "/jobs/" + ID + "/download",
        "/jobs/" + ID + "/x/y",
        "/jobs/short",
        "/jobs/",
        "/jobs/abc/download/",
        "/jobs/" + ID + "/" + ID,
        "/jobs/é/download",
        "/jobs/a b/download",
        "/jobs/" + "z" * 400,
        "/jobs/" + ID + "/" + "d" * 200,
    ],
)
def test_jobs_prefixed_output_identical_to_old_implementation(path: str) -> None:
    # none of these outputs can contain a 20-char run, so the new step must be a no-op
    assert mask_path(path) == _old_mask(path)


@pytest.mark.parametrize("sep", [".", "~", "=", ":", "+", "%", ",", ";", "@"])
def test_id_next_to_punctuation_is_masked(sep: str) -> None:
    for path in (f"/a{sep}{ID}", f"/{ID}{sep}b", f"/a{sep}{ID}{sep}b"):
        out = mask_path(path)
        assert ID[:10] not in out, out
        assert "<x>" in out


def test_id_with_dot_or_tilde_inside_is_not_one_run_but_halves_stay_short_or_masked() -> None:
    # a 30-char id cannot contain '.', so a split path is not a real id; each half >=20 is masked
    out = mask_path("/" + "A" * 25 + "." + "B" * 25)
    assert out == "/<x>.<x>"


@pytest.mark.parametrize("cut", range(100, 125))
def test_id_straddling_cap_boundary_never_partially_logged(cut: int) -> None:
    path = "/" + "a/" * ((cut - 1) // 2) + ID + "/tail"
    out = mask_path(path)
    for n in range(8, len(ID) + 1):
        assert ID[:n] not in out
        assert ID[-n:] not in out
    assert len(out) <= 120


def test_cap_boundary_with_unsafe_expansion_still_masks() -> None:
    path = "/" + "\x01" * 30 + ID
    out = mask_path(path)
    assert ID[:10] not in out
    assert len(out) <= 120


def test_multiple_ids_and_adjacent_runs() -> None:
    assert mask_path(f"/{ID}/{ID}") == "/<x>/<x>"
    assert mask_path(f"/{ID}{ID}") == "/<x>"


def test_nineteen_and_twenty_char_boundary() -> None:
    assert mask_path("/" + "a" * 19 + "/" + "b" * 19) == "/" + "a" * 19 + "/" + "b" * 19
    assert mask_path("/" + "a" * 20) == "/<x>"


def test_non_id_charset_neighbours_do_not_swallow_short_names() -> None:
    assert mask_path("/static/app.css") == "/static/app.css"
    assert mask_path("/static/js/app.min.js") == "/static/js/app.min.js"


@pytest.mark.parametrize(
    "raw",
    [
        "/x/" + ID,
        "/x/" + ID.replace("-", "%2D"),  # encoded dash in the middle
        "/x/" + quote(ID, safe=""),
        "/x/" + ID[:12] + "%" + format(ord(ID[12]), "02X") + ID[13:],  # one encoded char
        "//jobs/" + ID.replace("_", "%5F"),
    ],
)
def test_real_middleware_logs_decoded_path_and_masks(
    raw: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with build(settings_for(tmp_path)) as c:
        c.get(raw)
    blob = caplog.text
    assert ID[:10] not in blob and ID[-10:] not in blob, blob
    assert ID[:12] not in blob
    assert "GET /" in blob


def test_encoded_slash_split_leaves_only_halves_below_threshold(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # attacker-supplied path only: halves of 15 chars are not job ids and are logged as sent
    caplog.set_level(logging.INFO)
    with build(settings_for(tmp_path)) as c:
        c.get("/x/" + ID[:15] + "%2F" + ID[15:])
    lines = [r.getMessage() for r in caplog.records if r.name == ACCESS]
    assert lines and lines[0].startswith("GET /x/")


def test_routable_job_id_via_encoded_chars_masked_and_real_flow_unchanged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with build(settings_for(tmp_path)) as c:
        r = post_job(c)
        wait_idle(c)
        loc = r.headers["location"]
        job_id = loc.rsplit("/", 1)[1]
        c.get(loc)
        c.get(loc + "/download")
        c.get("/jobs/" + quote(job_id, safe="") + "/download")
        c.get("//jobs/" + job_id)
        c.get("/static/../jobs/" + job_id)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert job_id not in blob
    assert job_id[:10] not in blob
    lines = [r.getMessage() for r in caplog.records if r.name == ACCESS]
    assert any(re.fullmatch(r"GET /jobs/<id> 200 \d+ms", ln) for ln in lines)
    assert any(re.fullmatch(r"GET /jobs/<id>/download 200 \d+ms", ln) for ln in lines)
