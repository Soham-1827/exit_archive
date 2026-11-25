"""Browser test: open the exported index.html from file:// in Chromium.

Needs the optional ``e2e`` dependency group (``uv run --offline --group e2e pytest -m e2e``);
skips when Playwright or Chromium is unavailable. generated_at is never pinned.
"""

import asyncio
import json
import os
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import respx

from exit_archive.exporter import export_account
from exit_archive.secrets import HarvestToken
from exit_archive.viewer import build_viewer
from exit_archive.viewer.bundle import OUTPUT_REL
from tests.test_exporter import SECRET, fixture, mock_harvest

pytestmark = pytest.mark.e2e

XSS = "</script><img src=x onerror=alert(1)>"
HOSTILE = ["javascript:alert(1)", "../x", "/etc/passwd", "https://evil.example/x.pdf", "a/b.pdf"]
SCREENSHOT_ENV = "EXIT_ARCHIVE_E2E_SCREENSHOT"


def _page_of(name: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    return fixture(name) | {name: records}


def _rich_routes(routes: dict[str, respx.Route]) -> None:
    """Replace the sparse fixture payloads with records the viewer can filter on."""
    acme, globex = {"id": 1, "name": "Acme Corp"}, {"id": 2, "name": "Globex"}
    web, rocket = {"id": 11, "name": "Website"}, {"id": 12, "name": "Rocket"}
    clients = [
        {"id": 1, "name": "Acme Corp", "is_active": True, "currency": "USD"},
        {"id": 2, "name": "Globex", "is_active": False, "currency": "EUR"},
    ]
    projects = [
        {"id": 11, "name": "Website", "code": "WEB", "client": acme, "is_active": True},
        {"id": 12, "name": "Rocket", "code": "RKT", "client": globex, "is_active": True},
    ]
    entries = [
        {"id": 1, "spent_date": "2024-01-10", "client": acme, "project": web, "hours": 2.5,
         "task": {"name": "Dev"}, "user": {"name": "Ana"}, "notes": "Fixed login bug",
         "billable": True, "invoice": {"id": 101}},
        {"id": 2, "spent_date": "2024-02-11", "client": acme, "project": web, "hours": 0.5,
         "task": {"name": "Dev"}, "user": {"name": "Ana"}, "notes": XSS,
         "billable": False, "invoice": None},
        {"id": 3, "spent_date": "2024-03-12", "client": globex, "project": rocket, "hours": 1,
         "task": {"name": "Ops"}, "user": {"name": "Bo"}, "notes": "Rocket launch",
         "billable": True, "invoice": {"id": 102}},
    ]  # fmt: skip
    invoices = [
        {"id": 101, "number": "INV-001", "client": acme, "issue_date": "2024-01-31",
         "due_date": "2024-02-28", "amount": 1250.5, "state": "paid", "client_key": "fakekey101"},
        {"id": 102, "number": "INV-002", "client": globex, "issue_date": "2024-03-31",
         "due_date": "2024-04-30", "amount": 99, "state": "open", "client_key": "fakekey102"},
    ]  # fmt: skip
    expenses = [
        {"id": 1, "spent_date": "2024-01-15", "client": acme, "project": web, "total_cost": 12.34,
         "notes": "Team lunch", "expense_category": {"name": "Meals"},
         "receipt": {"url": "https://fake-co.harvestapp.com/expenses/1/receipt",
                     "file_name": "lunch.png"}},
        {"id": 2, "spent_date": "2024-03-20", "client": globex, "project": rocket,
         "total_cost": 50, "notes": "Fuel", "expense_category": {"name": "Travel"},
         "receipt": None},
    ]  # fmt: skip
    for name, records in (
        ("clients", clients),
        ("projects", projects),
        ("time_entries", entries),
        ("invoices", invoices),
        ("expenses", expenses),
    ):
        routes[name].respond(200, json=_page_of(name, records))


@pytest.fixture(scope="module")
def archive_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("export")
    with respx.mock:
        _rich_routes(mock_harvest())
        # Playwright's sync API leaves a loop running in this thread; export in a fresh one.
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(asyncio.run, export_account(HarvestToken(SECRET), out)).result()
    extract = tmp_path_factory.mktemp("extracted")
    with zipfile.ZipFile(result.zip_path) as zf:
        zf.extractall(extract)
    (top,) = [p for p in extract.iterdir() if p.is_dir()]
    return top / "index.html"


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure means "no browser here"
            pytest.skip(f"Chromium cannot launch: {type(exc).__name__}")
        yield instance
        instance.close()


@dataclass
class Watch:
    """What the page tried to do: dialogs, console errors, network attempts."""

    dialogs: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    blocked_requests: list[str] = field(default_factory=list)


def open_viewer(browser: Any, index: Path, *, width: int = 1280) -> tuple[Any, Watch]:
    context = browser.new_context(locale="en-US", viewport={"width": width, "height": 900})
    watch = Watch()

    def guard(route: Any) -> None:
        if route.request.url.startswith("file:"):
            route.continue_()
        else:
            watch.blocked_requests.append(route.request.url)
            route.abort()

    context.route("**/*", guard)
    page = context.new_page()
    page.on("dialog", lambda d: (watch.dialogs.append(d.message), d.dismiss()))
    page.on(
        "console",
        lambda m: m.type == "error" and watch.console_errors.append(m.text),
    )
    page.on("pageerror", lambda e: watch.page_errors.append(str(e)))
    page.on(
        "request", lambda r: not r.url.startswith("file:") and watch.blocked_requests.append(r.url)
    )
    page.goto(index.as_uri())
    page.wait_for_selector("#app:not([hidden])")
    return page, watch


def assert_clean(watch: Watch) -> None:
    assert watch.dialogs == []
    assert watch.blocked_requests == []
    assert watch.page_errors == []
    assert [e for e in watch.console_errors if "Content Security Policy" in e] == []
    assert watch.console_errors == []


def expect_rows(page: Any, count: int) -> None:
    # Locator assertions poll via CDP; page.wait_for_function would need 'unsafe-eval'.
    expect = pytest.importorskip("playwright.sync_api").expect
    expect(page.locator("#rows tr")).to_have_count(count)


def rows(page: Any) -> int:
    return int(page.locator("#rows tr").count())


def test_loads_from_file_and_shows_header(browser: Any, archive_index: Path) -> None:
    page, watch = open_viewer(browser, archive_index)
    assert "Fake Co" in page.inner_text("header")
    assert page.inner_text("header").strip() != ""
    assert "Archive generated" in page.inner_text("header")
    assert rows(page) == 3
    assert_clean(watch)


def test_search_for_a_fixture_note_shows_one_row(browser: Any, archive_index: Path) -> None:
    page, watch = open_viewer(browser, archive_index)
    page.fill("#search", "login")
    expect_rows(page, 1)
    assert "Fixed login bug" in page.inner_text("#rows")
    assert_clean(watch)


def test_client_filter_gives_expected_rows_and_totals(browser: Any, archive_index: Path) -> None:
    page, watch = open_viewer(browser, archive_index)
    page.select_option("#filter-client", label="Acme Corp")
    expect_rows(page, 2)
    assert "3.00" in page.inner_text("#summary")
    page.select_option("#filter-client", label="Globex")
    expect_rows(page, 1)
    assert "1.00" in page.inner_text("#summary")
    page.click("#reset")
    expect_rows(page, 3)
    assert_clean(watch)


def test_date_range_and_invoice_filters(browser: Any, archive_index: Path) -> None:
    page, _ = open_viewer(browser, archive_index)
    page.fill("#filter-from", "2024-02-01")
    page.fill("#filter-to", "2024-02-28")
    expect_rows(page, 1)
    page.click("#reset")
    page.fill("#filter-invoice", "INV-002")
    expect_rows(page, 1)
    assert "Rocket launch" in page.inner_text("#rows")


def test_tabs_show_each_record_type_with_links(browser: Any, archive_index: Path) -> None:
    page, watch = open_viewer(browser, archive_index)
    expected = {"invoices": 2, "estimates": 2, "expenses": 2, "clients": 2, "projects": 2}
    for tab, count in expected.items():
        page.click(f'#tabs [data-tab="{tab}"]')
        expect_rows(page, count)
    page.click('#tabs [data-tab="invoices"]')
    page.wait_for_selector('#rows a[href="invoices/INV-001_101.pdf"]')
    link = page.locator('#rows a[href^="invoices/"]').first
    assert "noopener" in link.get_attribute("rel")
    page.click('#tabs [data-tab="expenses"]')
    page.wait_for_selector('#rows a[href="receipts/1_lunch.png"]')
    assert_clean(watch)


def test_xss_note_renders_as_text(browser: Any, archive_index: Path) -> None:
    page, watch = open_viewer(browser, archive_index)
    page.fill("#search", "onerror")
    expect_rows(page, 1)
    assert XSS in page.inner_text("#rows")
    assert page.locator("#rows img").count() == 0
    assert page.locator("img").count() == 0
    page.wait_for_timeout(300)  # let any onerror handler fire if it were wired up
    assert_clean(watch)


def write_data(index: Path, payload: dict[str, Any]) -> None:
    data_file = index.parent.joinpath(*OUTPUT_REL)
    data_file.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload).replace("<", "\\u003c").replace(">", "\\u003e")
    data_file.write_text(f"window.EXIT_ARCHIVE_DATA = {body};\n", encoding="utf-8")


def _empty() -> dict[str, Any]:
    keys = ("time_entries", "invoices", "estimates", "expenses", "clients", "projects")
    return {"company_name": "Hostile & Co", "generated_at": "2025-01-01T00:00:00+00:00"} | {
        k: [] for k in keys
    }


def test_hostile_paths_never_become_links(browser: Any, tmp_path: Path) -> None:
    index = build_viewer(tmp_path, "x")
    invoices = [
        {"id": i, "number": f"N{i}", "client_name": "c", "amount": 1, "pdf": path}
        for i, path in enumerate([*HOSTILE, "invoices/ok_1.pdf"])
    ]
    expenses = [
        {"id": i, "amount": 1, "receipt": path}
        for i, path in enumerate([*HOSTILE, "receipts/ok.png"])
    ]
    write_data(index, _empty() | {"invoices": invoices, "expenses": expenses})
    page, watch = open_viewer(browser, index)
    page.click('#tabs [data-tab="invoices"]')
    expect_rows(page, len(invoices))
    hrefs = page.eval_on_selector_all("#rows a", "els => els.map(e => e.getAttribute('href'))")
    assert hrefs == ["invoices/ok_1.pdf"]
    for bad in HOSTILE:
        assert bad in page.inner_text("#rows")
    page.click('#tabs [data-tab="expenses"]')
    expect_rows(page, len(expenses))
    hrefs = page.eval_on_selector_all("#rows a", "els => els.map(e => e.getAttribute('href'))")
    assert hrefs == ["receipts/ok.png"]
    assert page.locator("a[href^='javascript:']").count() == 0
    assert_clean(watch)


def test_pagination_is_200_rows_per_page(browser: Any, tmp_path: Path) -> None:
    index = build_viewer(tmp_path, "x")
    entries = [
        {"id": i, "date": "2024-01-01", "client_id": 1, "client_name": "A", "hours": 1.5,
         "notes": f"n{i}"}
        for i in range(450)
    ]  # fmt: skip
    write_data(index, _empty() | {"time_entries": entries})
    page, watch = open_viewer(browser, index)
    assert rows(page) == 200
    assert "675.00" in page.inner_text("#summary")
    page.click("#next")
    pytest.importorskip("playwright.sync_api").expect(page.locator("#page-info")).to_contain_text(
        "201"
    )
    assert rows(page) == 200
    page.click("#next")
    expect_rows(page, 50)
    assert page.is_disabled("#next")
    page.click("#prev")
    expect_rows(page, 200)
    assert_clean(watch)


def test_missing_data_file_shows_a_message_not_a_crash(browser: Any, tmp_path: Path) -> None:
    index = build_viewer(tmp_path, "x")
    index.parent.joinpath(*OUTPUT_REL).unlink()
    context = browser.new_context()
    page = context.new_page()
    page.goto(index.as_uri())
    page.wait_for_selector("#empty-state:not([hidden])")
    assert "viewer-data.js" in page.inner_text("#empty-state")


def test_narrow_screen_has_no_horizontal_overflow(
    browser: Any, archive_index: Path, tmp_path: Path
) -> None:
    page, _ = open_viewer(browser, archive_index, width=360)
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0
    shot = os.environ.get(SCREENSHOT_ENV)
    if shot:
        page.screenshot(path=str(Path(shot).with_name("viewer-narrow.png")), full_page=True)


def test_screenshot_for_review(browser: Any, archive_index: Path) -> None:
    shot = os.environ.get(SCREENSHOT_ENV)
    if not shot:
        pytest.skip(f"set {SCREENSHOT_ENV} to save a screenshot")
    page, _ = open_viewer(browser, archive_index)
    page.click('#tabs [data-tab="invoices"]')
    page.wait_for_selector("#rows a")
    page.screenshot(path=shot, full_page=True)
