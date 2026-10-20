"""Render the pages in a real browser and assert what a human would see.

**Opt-in.** Skips unless Playwright and a browser are installed:

    pip install -e ".[browser]" && playwright install chromium
    pytest tests/test_browser.py

Not in the default suite on purpose. It needs a ~150 MB browser download and
runs in seconds rather than milliseconds, and the rest of the suite is
network-free and fast enough to run on every save. CI keeps that property; this
is what you run before shipping a frontend change.

It exists because the web app was once shipped without anyone rendering it. The
HTML parsed, every id resolved, every endpoint returned 200 — and the dashboard
still laid out at 13,171px tall with rows 1,571px high, because a `<pre>` full of
thesis prose had no line clamp. No amount of parsing finds that. A browser finds
it immediately.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

from alpha_engine.web.api import ApiConfig, ApiState
from alpha_engine.web.server import AppHandler

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="install with: pip install -e '.[browser]'"
)

VIEWPORTS = [("desktop", 1440, 900), ("laptop", 1024, 768), ("mobile", 390, 844)]
PAGES = ["/", "/dashboard", "/terminal", "/research"]

#: An element wider than the viewport is only a bug when nothing between it and
#: the root can scroll. A wide table inside `overflow-x: auto` is a deliberate,
#: usable pattern, and flagging it is noise that hides real breakage.
OVERFLOW_PROBE = """
() => {
  const problems = [];
  const de = document.documentElement;
  if (de.scrollWidth > de.clientWidth + 1) {
    problems.push(`page scrolls horizontally: ${de.scrollWidth}px in ${de.clientWidth}px`);
  }
  const vw = de.clientWidth;
  document.querySelectorAll('*').forEach((el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || r.width === 0) return;
    if (r.right <= vw + 1) return;
    for (let a = el.parentElement; a; a = a.parentElement) {
      const ov = getComputedStyle(a).overflowX;
      if (ov === 'auto' || ov === 'scroll') return;
    }
    const id = el.id ? '#' + el.id : el.tagName;
    problems.push(`overflows the viewport by ${Math.round(r.right - vw)}px: ${id}`);
  });
  return [...new Set(problems)].slice(0, 8);
}
"""

SCROLL_THROUGH = """
async () => {
  for (let y = 0; y < document.body.scrollHeight; y += 400) {
    window.scrollTo(0, y);
    await new Promise((r) => setTimeout(r, 40));
  }
  window.scrollTo(0, 0);
}
"""


@pytest.fixture(scope="module")
def server():
    AppHandler.state = ApiState(config=ApiConfig(rate_limit_per_min=0))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    AppHandler.state = ApiState()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001 - browser binary not downloaded
            pytest.skip(f"chromium unavailable ({e}); run: playwright install chromium")
        yield b
        b.close()


def _open(browser, server, path, width, height, theme="dark"):
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"JS ERROR: {e}"))
    page.on(
        "console",
        lambda m: errors.append(f"CONSOLE {m.type}: {m.text}") if m.type == "error" else None,
    )
    page.on("requestfailed", lambda r: errors.append(f"REQUEST FAILED: {r.url}"))
    page.on(
        "response",
        lambda r: errors.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None,
    )
    page.goto(server + path, wait_until="networkidle")
    page.wait_for_timeout(500)
    # Cards below the fold are revealed by an IntersectionObserver, so a check
    # that never scrolls inspects a mostly-empty document.
    page.evaluate(SCROLL_THROUGH)
    page.wait_for_timeout(400)
    return ctx, page, errors


@pytest.mark.parametrize("path", PAGES)
@pytest.mark.parametrize(("name", "width", "height"), VIEWPORTS)
def test_page_renders_without_errors_or_overflow(browser, server, path, name, width, height):
    ctx, page, errors = _open(browser, server, path, width, height)
    try:
        problems = errors + page.evaluate(OVERFLOW_PROBE)
        assert not problems, f"{path} at {name} ({width}x{height}): {problems}"
    finally:
        ctx.close()


def test_the_signal_feed_stays_readable(browser, server):
    """The regression that a 13,171px page was made of: an unclamped `<pre>` of
    thesis prose gave rows 1,571px tall — one signal per screenful."""
    ctx, page, _ = _open(browser, server, "/dashboard", 1024, 768)
    try:
        tallest = page.evaluate(
            """() => {
                const rows = [...document.querySelectorAll('#signals-table tbody tr')];
                return rows.length ? Math.max(...rows.map(r => r.getBoundingClientRect().height)) : 0;
            }"""
        )
        assert tallest < 400, f"a feed row is {tallest}px tall; the thesis clamp is gone"
    finally:
        ctx.close()


def test_the_composer_placeholder_is_not_clipped(browser, server):
    """It used to carry the keyboard hint too, which wrapped to two lines and was
    cut off mid-word on a 390px screen."""
    for name, width, height in VIEWPORTS:
        ctx, page, _ = _open(browser, server, "/terminal", width, height)
        try:
            clipped = page.evaluate(
                """() => { const t = document.getElementById('input');
                           return t.scrollHeight > t.clientHeight + 1; }"""
            )
            assert not clipped, f"the composer placeholder is clipped at {name} ({width}px)"
        finally:
            ctx.close()


def test_below_the_fold_content_becomes_visible(browser, server):
    """`.card.reveal` starts at opacity 0. If the observer ever stops firing, the
    dashboard is a header and a lot of white space."""
    ctx, page, _ = _open(browser, server, "/dashboard", 1440, 900)
    try:
        hidden = page.evaluate(
            """() => [...document.querySelectorAll('.card.reveal')]
                 .filter(c => getComputedStyle(c).opacity === '0')
                 .map(c => (c.querySelector('.label')?.textContent || '').trim().slice(0, 30))
                 .filter(label => label && label !== 'Signal History')"""
        )
        assert not hidden, f"cards never became visible after scrolling: {hidden}"
    finally:
        ctx.close()


def test_both_themes_keep_text_visible(browser, server):
    for theme in ("dark", "light"):
        ctx, page, _ = _open(browser, server, "/dashboard", 1440, 900, theme=theme)
        try:
            invisible = page.evaluate(
                """() => [...document.querySelectorAll('h1,h2,p,td,th,button,a,label')]
                     .filter(el => el.textContent.trim())
                     .filter(el => { const cs = getComputedStyle(el);
                        return cs.color === cs.backgroundColor
                            && cs.backgroundColor !== 'rgba(0, 0, 0, 0)'; })
                     .map(el => el.tagName)"""
            )
            assert not invisible, f"{theme}: text the same colour as its background: {invisible}"
        finally:
            ctx.close()


def test_research_guided_demo_downloads_reproducible_result(browser, server, tmp_path, monkeypatch):
    import json
    import runpy
    from pathlib import Path

    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    script = Path(__file__).resolve().parents[1] / "scripts" / "load_demo.py"
    runpy.run_path(str(script))["main"]()
    ctx = browser.new_context(accept_downloads=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(server + "/research")
        page.get_by_role("button", name="Use offline demo").click()
        page.get_by_text("DEMO evidence loaded", exact=False).wait_for()
        assert page.locator("#options-section").is_hidden()
        assert "invented candles" in page.locator("#source-status").inner_text()
        page.get_by_role("button", name="Try the first example").click()
        page.get_by_role("button", name="Download reproducible result (JSON)").wait_for()
        assert "buy and hold" in page.locator("#strategy-result").inner_text().lower()
        with page.expect_download() as event:
            page.get_by_role("button", name="Download reproducible result (JSON)").click()
        destination = tmp_path / "experiment.json"
        event.value.save_as(destination)
        result = json.loads(destination.read_text())
        assert result["experiment"]["price_source"] == "synthetic-demo"
        assert "Synthetic demo candles are not market data" in result["experiment"]["limitations"]
        assert len(result["experiment"]["price_snapshot"]["candles"]) == 260
        assert not errors
    finally:
        ctx.close()


# --------------------------------------------------------------------------
# The markdown renderer
#
# Tested through a real browser because it is DOM-building code — the thing that
# matters is what the page ends up containing, not what a shim thinks it built.
#
# Every case here is a shape a live model actually emitted during testing against
# OpenRouter, and every one of them rendered wrong at some point: `**bold**` and
# `### headings` came out as literal punctuation, then a fix for that split every
# list so numbering restarted at 1, then blank lines between items split them
# again. Formatting is most of what an LLM reply is, so a broken renderer makes
# every answer look broken.
# --------------------------------------------------------------------------


def _render(page, markdown: str):
    """Run the page's own renderProse over `markdown` and return the DOM shape."""
    return page.evaluate(
        """(md) => {
            const host = document.createElement('div');
            document.body.appendChild(host);
            // renderProse lives in terminal.js's IIFE, so drive it the way the
            // app does: through a fake reply rendered into the log.
            window.__test_render(md, host);
            const shape = {
                html: host.innerHTML,
                text: host.innerText,
                strong: host.querySelectorAll('strong').length,
                em: host.querySelectorAll('em').length,
                code: host.querySelectorAll('code').length,
                headings: host.querySelectorAll('h4, h5').length,
                topLists: host.querySelectorAll(':scope > ol, :scope > ul').length,
                nested: host.querySelectorAll('li ol, li ul').length,
                olCounts: [...host.querySelectorAll('ol')].map(o => o.children.length),
                rules: host.querySelectorAll('hr').length,
                injected: host.querySelectorAll('img, script, iframe, svg, object').length,
            };
            host.remove();
            return shape;
        }""",
        markdown,
    )


@pytest.fixture()
def rendered(browser, server):
    ctx = browser.new_context(viewport={"width": 1200, "height": 800})
    page = ctx.new_page()
    page.goto(server + "/terminal", wait_until="networkidle")
    page.wait_for_timeout(300)
    if not page.evaluate("typeof window.__test_render === 'function'"):
        ctx.close()
        pytest.skip("terminal.js does not expose __test_render")
    yield page
    ctx.close()


def test_bold_and_italics_render_as_elements(rendered):
    shape = _render(rendered, "**Direction**: Bullish and *research only*.")
    assert shape["strong"] == 1
    assert shape["em"] == 1
    assert "**" not in shape["text"]


def test_headings_render_even_without_a_blank_line_before_them(rendered):
    """Models do not reliably leave a blank line before a heading."""
    shape = _render(rendered, "Some text\n### Contributing Factors\nmore text")
    assert shape["headings"] == 1
    assert "###" not in shape["text"]


def test_a_tight_numbered_list_is_one_list(rendered):
    shape = _render(rendered, "1. A\n2. B\n3. C")
    assert shape["olCounts"] == [3]


def test_a_loose_numbered_list_is_still_one_list(rendered):
    """Blank lines between items are a 'loose list' in markdown, not three
    lists. Splitting them made the numbering render 1. 1. 1."""
    shape = _render(rendered, "1. A\n\n2. B\n\n3. C")
    assert shape["olCounts"] == [3]


def test_indented_bullets_nest_under_their_numbered_item(rendered):
    shape = _render(
        rendered,
        "1. **Equity Trend**\n   - Direction: Bullish\n   - Weight: 0.61\n2. **RSI**\n   - Weight: 0.23",
    )
    assert shape["olCounts"] == [2], "the outer list must keep both items"
    assert shape["nested"] == 2, "each item should carry its own sub-list"


def test_a_list_followed_by_prose_ends_the_list(rendered):
    shape = _render(rendered, "Intro:\n1. A\n2. B\n\nClosing thought.")
    assert shape["olCounts"] == [2]
    assert "Closing thought." in shape["text"]


def test_inline_code_and_fenced_blocks_both_render(rendered):
    shape = _render(rendered, "Use `rsi(14)`.\n\n```\nscan BTC\n```")
    assert shape["code"] >= 2


def test_a_horizontal_rule_renders(rendered):
    assert _render(rendered, "before\n\n---\n\nafter")["rules"] == 1


def test_model_output_can_never_inject_html(rendered):
    """The XSS invariant, exercised through the real renderer rather than
    asserted about the source: this is a page where the user pasted an API key."""
    payload = "<img src=x onerror=alert(1)> and <script>alert(2)</script>"
    shape = _render(rendered, payload)

    # The right question is whether ELEMENTS were created, not whether the
    # substring "onerror" appears — it appears escaped, as `&lt;img ... onerror`,
    # which is the correct outcome: inert text, not an attribute.
    assert shape["injected"] == 0, "model output created real DOM elements"
    assert "&lt;" in shape["html"], "the markup should be escaped, not stripped"
    assert "alert(1)" in shape["text"], "and still readable as text"
