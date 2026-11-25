"""Templates and CSS: no inline script/style, no external URLs, only /static/app.css."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from exit_archive.web import app as web_app
from tests.web.helpers import build, make_job_id, post_job, settings_for, wait_idle

WEB_DIR = Path(web_app.__file__).parent
TEMPLATES = sorted((WEB_DIR / "templates").glob("*.html"))


def test_templates_exist() -> None:
    assert {p.name for p in TEMPLATES} >= {"base.html", "index.html", "start.html", "privacy.html"}
    assert (WEB_DIR / "static" / "app.css").is_file()


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_template_has_no_inline_code_or_external_urls(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    low = text.lower()
    assert "<script" not in low
    assert "<style" not in low
    assert " style=" not in low
    assert "http://" not in low
    assert "https://" not in low
    assert not re.search(r"\son[a-z]+\s*=", low)  # no inline event handlers


def test_css_has_no_external_loads() -> None:
    css = (WEB_DIR / "static" / "app.css").read_text(encoding="utf-8").lower()
    assert "@import" not in css
    assert "http://" not in css
    assert "https://" not in css
    assert "url(" not in css
    assert "prefers-color-scheme: dark" in css


class _Collect(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.tags: list[str] = []
        self.forms: list[dict[str, str | None]] = []
        self.token_in_post_form = False
        self.token_inputs = 0
        self._form: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self.tags.append(tag)
        if tag == "link":
            self.links.append(a.get("href") or "")
        if tag == "form":
            self._form = a
            self.forms.append(a)
        if tag == "input" and a.get("name") == "token":
            self.token_inputs += 1
            assert a.get("type") == "password"
            assert a.get("autocomplete") == "off"
            assert self._form is not None
            assert (self._form.get("method") or "").lower() == "post"
            self.token_in_post_form = True
        assert "style" not in a
        assert a.get("src") is None or not str(a["src"]).startswith("http")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None


def _parse(html: str) -> _Collect:
    parser = _Collect()
    parser.feed(html)
    return parser


def test_every_rendered_page_links_only_app_css(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        location = post_job(client).headers["location"]
        wait_idle(client)
        store = client.app.state.store  # type: ignore[attr-defined]
        running = make_job_id()
        store.create(running, None)
        store.mark_running(running)
        paths = ["/", "/start", "/privacy", location, f"/jobs/{running}", "/missing", "/jobs/x"]
        for path in paths:
            page = _parse(client.get(path).text)
            assert page.links.count("/static/app.css") == 1, path
            assert len(page.links) == 1, path
            assert "script" not in page.tags and "style" not in page.tags
        assert client.get("/static/app.css").status_code == 200


def test_token_field_only_in_post_form(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)) as client:
        start = _parse(client.get("/start").text)
        assert start.token_inputs == 1
        assert start.token_in_post_form
        assert [f.get("action") for f in start.forms] == ["/jobs"]
        for path in ("/", "/privacy"):
            assert _parse(client.get(path).text).token_inputs == 0
    for template in TEMPLATES:
        count = len(re.findall(r'name="token"', template.read_text(encoding="utf-8")))
        assert count == (1 if template.name == "start.html" else 0)
