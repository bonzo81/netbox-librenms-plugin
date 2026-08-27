"""Browser lifecycle shared by the Playwright suites."""

import pytest
from playwright import sync_api as playwright


@pytest.fixture
def page():
    """Yield a page on a fresh browser, closed even when the test fails.

    Deliberately named after the ``pytest-playwright`` fixture and overriding it where that
    plugin happens to be installed: only ``playwright`` itself is declared in
    ``requirements_dev.txt``, so the browser CI job has no plugin fixture to fall back on.
    """
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()
