"""Adversarial tester cases for T2.2: CSP hash, whole-zip token scan, failure cleanup, hostile
paths and pagination edges in a real browser (e2e)."""

import base64
import hashlib
import re
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import respx

from exit_archive.exporter import export_account
from exit_archive.secrets import HarvestToken
from exit_archive.viewer import build_viewer
from tests.test_exporter import SECRET, leftovers, mock_harvest, read_zip
from tests.viewer.test_viewer_e2e import (  # noqa: F401 - fixtures + helpers
    _empty,
    assert_clean,
    browser,
    expect_rows,
    open_viewer,
    rows,
    write_data,
)


def _html() -> str:
    return resources.files("exit_archive.viewer").joinpath("index.html").read_text("utf-8")


def test_csp_hashes_recomputed_independently() -> None:
    html = _html()
    csp = re.search(r'Content-Security-Policy" content="([^"]+)"', html)
    assert csp
    script = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    style = re.findall(r"<style>(.*?)</style>", html, re.DOTALL)
    assert len(script) == 1 and len(style) == 1
    for body in (script[0], style[0]):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp.group(1)
    assert "style=" not in html  # inline style attributes would be CSP-blocked
    assert not re.search(r"\son[a-z]+\s*=", html)  # no inline event handlers


@respx.mock
async def test_token_absent_from_every_zip_member_and_name(tmp_path: Path) -> None:
    mock_harvest()
    result = await export_account(HarvestToken(SECRET), tmp_path)
    members, manifest = read_zip(result.zip_path)
    assert SECRET.encode() not in result.zip_path.read_bytes()
    for name, body in members.items():
        assert SECRET not in name
        assert SECRET.encode() not in body, name
    assert SECRET not in str(manifest)


@respx.mock
async def test_failure_inside_viewer_build_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_harvest()

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("exit_archive.exporter.build_viewer", boom)
    with pytest.raises(Exception) as info:  # noqa: PT011 - any failure type, only cleanup matters
        await export_account(HarvestToken(SECRET), tmp_path)
    assert "disk full" in str(info.value)
    assert leftovers(tmp_path) == []


def test_build_viewer_leaves_no_part_file(tmp_path: Path) -> None:
    build_viewer(tmp_path, "x")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data", "index.html"]


NASTY = [
    "JAVASCRIPT:alert(1)",
    "receipts/%2e%2e/x",
    "receipts/..%2fx",
    "receipts/a.pdf\n",
    "\nreceipts/a.pdf",
    " receipts/a.pdf",
    "receipts/a.pdf ",
    "receipts/a b.pdf",
    "receipts/．．",  # fullwidth dots
    "receipts/а.pdf",  # Cyrillic a
    "receipts/..",
    "receipts/.",
    "receipts/a/b.pdf",
    "receipts\\a.pdf",
    "receipts/",
    "Receipts/a.pdf",
    "invoices/a.pdf?x=1",
    "invoices/a.pdf#frag",
    "data:text/html,<b>",
    "//evil.example/a.pdf",
    "receipts/a.pdf\u0000",
    {"x": "receipts/a.pdf"},
    ["receipts/a.pdf"],
    123,
]


@pytest.mark.e2e
def test_nasty_paths_never_become_links(browser: Any, tmp_path: Path) -> None:  # noqa: F811
    index = build_viewer(tmp_path, "x")
    exp = [
        {"id": i, "amount": 1, "receipt": p} for i, p in enumerate([*NASTY, "receipts/ok_1.png"])
    ]
    write_data(index, _empty() | {"expenses": exp})
    page, watch = open_viewer(browser, index)
    page.click('#tabs [data-tab="expenses"]')
    expect_rows(page, len(exp))
    hrefs = page.eval_on_selector_all("#rows a", "els => els.map(e => e.getAttribute('href'))")
    assert hrefs == ["receipts/ok_1.png"]
    assert_clean(watch)


@pytest.mark.e2e
def test_hostile_values_everywhere_render_as_text(browser: Any, tmp_path: Path) -> None:  # noqa: F811
    index = build_viewer(tmp_path, "x")
    bad = "<img src=x onerror=alert(1)>"
    data = _empty() | {
        "company_name": bad,
        "generated_at": bad,
        "clients": [{"id": '"><svg onload=alert(1)>', "name": bad, "currency": bad,
                     "is_active": bad}],
        "invoices": [{"id": 7, "number": bad, "client_id": '"><b>', "client_name": bad,
                      "state": bad, "amount": "NaN", "pdf": bad}],
        "time_entries": [{"id": 1, "date": bad, "client_id": 9, "client_name": bad,
                          "project_id": 5, "project_name": bad, "hours": "1e999",
                          "notes": {"a": 1}, "invoice_id": 7, "billable": bad}],
    }  # fmt: skip
    write_data(index, data)
    page, watch = open_viewer(browser, index)
    for tab in ("time_entries", "invoices", "estimates", "expenses", "clients", "projects"):
        page.click(f'#tabs [data-tab="{tab}"]')
        page.wait_for_timeout(50)
    page.select_option("#filter-client", index=1) if page.locator(
        "#filter-client option"
    ).count() > 1 else None
    page.wait_for_timeout(300)
    assert page.locator("img, svg, #rows b, header b").count() == 0
    assert bad in page.inner_text("h1")
    assert_clean(watch)


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("n", "pages_text", "prev_dis", "next_dis"),
    [(0, "0 results", True, True), (1, "1–1 of 1", True, True), (200, "1–200 of 200", True, True),
     (201, "page 1 of 2", True, False)],
)  # fmt: skip
def test_pagination_edges(
    browser: Any,  # noqa: F811
    tmp_path: Path,
    n: int,
    pages_text: str,
    prev_dis: bool,
    next_dis: bool,
) -> None:
    index = build_viewer(tmp_path, "x")
    entries = [
        {"id": i, "date": "2024-01-01", "client_id": 1, "client_name": "A", "hours": 1,
         "notes": f"n{i}"}
        for i in range(n)
    ]  # fmt: skip
    write_data(index, _empty() | {"time_entries": entries})
    page, watch = open_viewer(browser, index)
    assert rows(page) == min(n, 200)
    assert pages_text in page.inner_text("#page-info")
    assert page.is_disabled("#prev") is prev_dis
    assert page.is_disabled("#next") is next_dis
    assert (page.locator("#no-results").is_visible()) is (n == 0)
    assert f"{n}.00" in page.inner_text("#summary") or n == 0
    if n == 201:
        page.click("#next")
        expect_rows(page, 1)
        # filtering down while on page 2 must clamp back to a valid page
        page.fill("#search", "n5")
        expect_rows(page, 1)
        assert page.is_disabled("#next")
    assert_clean(watch)


@pytest.mark.e2e
@pytest.mark.xfail(
    reason="LOW: plain-object maps break on ids like '__proto__' (not real Harvest ids)",
    strict=False,
)
def test_prototype_key_ids_do_not_crash_the_page(browser: Any, tmp_path: Path) -> None:  # noqa: F811
    index = build_viewer(tmp_path, "x")
    entries = [{"id": 1, "date": "2024-01-01", "client_id": "constructor", "client_name": "A",
                "project_id": "__proto__", "invoice_id": "__proto__", "hours": 1,
                "notes": "n"}]  # fmt: skip
    write_data(index, _empty() | {"time_entries": entries})
    page, watch = open_viewer(browser, index)
    assert rows(page) == 1
    assert_clean(watch)
