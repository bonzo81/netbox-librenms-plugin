"""Browser lifecycle shared by the Playwright suites."""

from pathlib import Path

import pytest
from playwright import sync_api as playwright


# NetBox 4.6.5 and 4.7 bundle htmx 2.0.10 (4.4.0 bundles 2.0.6), so the vendored copy tracks NetBox.
HTMX_PATH = Path(__file__).parent / "vendor" / "htmx.min.js"
# htmx is a UMD bundle and Playwright evaluates an init script in its own scope, so export the global.
HTMX_INIT_SCRIPT = f"{HTMX_PATH.read_text()}\nwindow.htmx = htmx;"
# An init script only runs on a document the page navigates to, and page.set_content keeps this origin.
FIXTURE_URL = "https://plugin.example.com/browser-fixture"


@pytest.fixture
def page():
    """Yield a fresh page carrying the real ``htmx`` global, then close its browser even on failure, without requiring ``pytest-playwright``."""
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            fixture_page = browser.new_page()
            fixture_page.add_init_script(script=HTMX_INIT_SCRIPT)
            fixture_page.route(
                FIXTURE_URL,
                lambda route: route.fulfill(status=200, content_type="text/html", body="<html><body></body></html>"),
            )
            fixture_page.goto(FIXTURE_URL)
            yield fixture_page
        finally:
            browser.close()
