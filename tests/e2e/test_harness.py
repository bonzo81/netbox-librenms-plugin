"""Checks for the Playwright harness before any live NetBox test runs."""

import pytest

from .conftest import browser, browser_launch_options, page


def test_chromium_arguments_keep_quoted_values(monkeypatch):
    monkeypatch.setenv("E2E_BROWSER_ARGS", '--host-resolver-rules="MAP *.test 127.0.0.1" --no-sandbox')

    assert browser_launch_options()["args"] == [
        "--host-resolver-rules=MAP *.test 127.0.0.1",
        "--no-sandbox",
    ]


def test_live_fixture_identifiers_share_a_run_specific_suffix(monkeypatch):
    monkeypatch.setenv("E2E_TESTS_ENABLED", "1")
    from . import test_module_actions_in_place as module

    suffix = module.DEVICE_NAME.rsplit("-", 1)[-1]
    for value in (
        module.MANUFACTURER_SLUG,
        module.DEVICE_TYPE_MODEL,
        module.SITE_SLUG,
        module.ROLE_SLUG,
    ):
        assert value.endswith(suffix)
        assert value in module.SETUP_CODE


def test_playwright_stops_when_browser_launch_fails(monkeypatch):
    from playwright import sync_api

    class FailedPlaywright:
        def __init__(self):
            self.stopped = False
            self.chromium = self

        def start(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.stopped = True

        def stop(self):
            self.stopped = True

        def launch(self, **_):
            raise RuntimeError("browser launch failed")

    playwright = FailedPlaywright()
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: playwright)

    with pytest.raises(RuntimeError, match="browser launch failed"):
        next(browser.__wrapped__())

    assert playwright.stopped


def test_browser_context_closes_when_login_fails():
    class FailedPage:
        def goto(self, *_args, **_kwargs):
            raise RuntimeError("login page failed")

    class Context:
        def __init__(self):
            self.closed = False

        def new_page(self):
            return FailedPage()

        def close(self):
            self.closed = True

    class Browser:
        def __init__(self, context):
            self.context = context

        def new_context(self, **_kwargs):
            return self.context

    context = Context()
    with pytest.raises(RuntimeError, match="login page failed"):
        next(page.__wrapped__(Browser(context)))

    assert context.closed
