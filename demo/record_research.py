"""Regenerate the README screenshot from invented DEMO prices, with Playwright."""

from __future__ import annotations

import os
import runpy
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from alpha_engine.web.api import ApiConfig, ApiState
from alpha_engine.web.server import AppHandler

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        os.environ["ALPHA_DATA_DIR"] = folder
        runpy.run_path(str(ROOT / "scripts" / "load_demo.py"))["main"]()
        AppHandler.state = ApiState(config=ApiConfig(rate_limit_per_min=0))
        server = ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/research")
                page.get_by_role("button", name="Use offline demo").click()
                page.get_by_text("DEMO evidence loaded", exact=False).wait_for()
                page.get_by_role("button", name="Try the first example").click()
                page.get_by_role("button", name="Download reproducible result (JSON)").wait_for()
                page.screenshot(path=str(ROOT / "demo" / "research-workbench.png"), full_page=True)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
