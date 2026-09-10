"""Browser lifecycle shared by the Playwright suites."""

import pytest
from playwright import sync_api as playwright


# HTMX resolves every request path against document.location, which about:blank cannot serve as a
# base, so the page starts on a real origin. set_content() rewrites this document and keeps the URL.
FIXTURE_URL = "https://plugin.example.com/browser-fixture"


@pytest.fixture
def page():
    """Yield a fresh browser page on the plugin origin, then close its browser even on failure, without requiring ``pytest-playwright``."""
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            fixture_page = browser.new_page()
            fixture_page.route(
                FIXTURE_URL,
                lambda route: route.fulfill(status=200, content_type="text/html", body="<html><body></body></html>"),
            )
            fixture_page.goto(FIXTURE_URL)
            yield fixture_page
        finally:
            browser.close()
