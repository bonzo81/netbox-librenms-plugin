"""Browser lifecycle shared by the Playwright suites."""

import pytest
from playwright import sync_api as playwright


@pytest.fixture
def page():
    """Yield a fresh browser page, then close its browser even on failure, without requiring ``pytest-playwright``."""
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()
