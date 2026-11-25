"""Static checks on the viewer template: CSP, banned APIs, no external URLs, script hashes."""

import base64
import hashlib
import re
from importlib import resources

import pytest

BANNED = (
    "fetch(",
    "XMLHttpRequest",
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "WebSocket",
    "sendBeacon",
    "importScripts",
)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)
INLINE_STYLE = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL)


@pytest.fixture(scope="module")
def html() -> str:
    return resources.files("exit_archive.viewer").joinpath("index.html").read_text("utf-8")


def sha256_source(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def csp_of(html: str) -> dict[str, list[str]]:
    match = re.search(
        r'<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"', html, re.IGNORECASE
    )
    assert match, "CSP meta missing"
    directives: dict[str, list[str]] = {}
    for part in match.group(1).split(";"):
        words = part.split()
        if words:
            directives[words[0]] = words[1:]
    return directives


def test_csp_meta_is_first_element_in_head(html: str) -> None:
    head = html.split("<head>", 1)[1]
    first_tag = re.search(r"<(\w+)", head)
    assert first_tag and first_tag.group(1) == "meta"
    assert head.lstrip().lower().startswith('<meta http-equiv="content-security-policy"')


def test_charset_is_declared_early(html: str) -> None:
    assert re.search(r'<meta charset="utf-8">', html[:1024], re.IGNORECASE)


def test_csp_locks_everything_down(html: str) -> None:
    csp = csp_of(html)
    assert csp["default-src"] == ["'none'"]
    assert csp["connect-src"] == ["'none'"]
    assert csp["object-src"] == ["'none'"]
    assert csp["base-uri"] == ["'none'"]
    assert csp["form-action"] == ["'none'"]
    assert "img-src" not in csp or csp["img-src"] == ["'none'"]


def test_csp_script_src_has_no_unsafe_keywords(html: str) -> None:
    script_src = csp_of(html)["script-src"]
    assert "'unsafe-inline'" not in script_src
    assert "'unsafe-eval'" not in script_src
    assert "*" not in script_src
    assert not any(s.startswith(("http:", "https:", "data:", "blob:")) for s in script_src)


def test_csp_script_hash_matches_the_single_inline_script(html: str) -> None:
    scripts = INLINE_SCRIPT.findall(html)
    assert len(scripts) == 1
    assert sha256_source(scripts[0]) in csp_of(html)["script-src"]


def test_csp_style_hash_matches_the_single_inline_style(html: str) -> None:
    styles = INLINE_STYLE.findall(html)
    assert len(styles) == 1
    style_src = csp_of(html)["style-src"]
    assert sha256_source(styles[0]) in style_src
    assert "'unsafe-inline'" not in style_src


def test_data_script_is_loaded_by_relative_path_before_the_inline_script(html: str) -> None:
    data_at = html.index('<script src="data/viewer-data.js"></script>')
    inline_at = html.index("<script>")
    assert data_at < inline_at


def test_no_external_urls_in_src_or_href(html: str) -> None:
    for match in re.finditer(r"""\b(?:src|href|action|data)\s*=\s*["']([^"']*)""", html):
        assert not re.match(r"(?i)(https?:)?//", match.group(1)), match.group(0)
    assert "http://" not in html
    assert "https://" not in html
    assert "url(" not in html


@pytest.mark.parametrize("token", BANNED)
def test_banned_apis_absent(html: str, token: str) -> None:
    assert token not in html


def test_inline_script_has_no_script_closer_inside(html: str) -> None:
    body = INLINE_SCRIPT.findall(html)[0]
    assert "</script" not in body.lower()
    assert "<!--" not in body


def test_pagination_and_selectors_the_e2e_relies_on(html: str) -> None:
    assert re.search(r"\bPAGE_SIZE\s*=\s*200\b", html)
    for ident in (
        "search",
        "filter-client",
        "filter-project",
        "filter-from",
        "filter-to",
        "filter-invoice",
        "rows",
        "summary",
        "page-info",
        "prev",
        "next",
        "reset",
        "tabs",
    ):
        assert f'id="{ident}"' in html, ident
    for tab in ("time_entries", "invoices", "estimates", "expenses", "clients", "projects"):
        assert tab in html


def test_link_allowlist_regex_is_present(html: str) -> None:
    assert r"^(receipts|invoices|estimates)\/[A-Za-z0-9._-]+$" in html or (
        r"^(receipts|invoices|estimates)/[A-Za-z0-9._-]+$" in html
    )
    assert "noopener" in html
