"""Browser lifecycle shared by the Playwright suites."""

import pytest
from playwright import sync_api as playwright


# HTMX resolves every request path against document.location, which about:blank cannot serve as a
# base, so the page starts on a real origin. set_content() rewrites this document and keeps the URL.
FIXTURE_URL = "https://plugin.example.com/browser-fixture"

# A fetch settles when its JSON body resolves or the fetch rejects (an abort included).
TRACK_FETCHES = """() => {
    window.__sentFetches = 0;
    window.__settledFetches = 0;
    const settle = () => { window.__settledFetches += 1; };
    const realFetch = window.fetch.bind(window);
    window.fetch = (...args) => {
        window.__sentFetches += 1;
        return realFetch(...args).then(response => {
            const realJson = response.json.bind(response);
            response.json = () => realJson().finally(settle);
            return response;
        }, error => { settle(); throw error; });
    };
}"""


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


class FetchTracker:
    """Count the page's fetches, so a test can wait until the page has handled every response."""

    def __init__(self, page):
        self.page = page

    def install(self):
        """Wrap ``fetch`` in the current document; call it after the page loads its scripts."""
        self.page.evaluate(TRACK_FETCHES)

    def wait_until_settled(self):
        """Wait until every fetch sent since ``install`` has delivered its JSON body or failed."""
        self.page.wait_for_function(
            "() => window.__sentFetches > 0 && window.__settledFetches === window.__sentFetches"
        )


@pytest.fixture
def fetches(page):
    """Return a FetchTracker for the test page."""
    return FetchTracker(page)
