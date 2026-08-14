"""Browser-level checks for the sync-tab cache state machine."""

import json
from pathlib import Path

from playwright import sync_api as playwright

SCRIPT_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"


def _page_html(initial_state):
    state = json.dumps(initial_state)
    return f"""
        <div id="librenms-sync-cache-state"
             data-status-url="https://plugin.example.com/status"
             data-server-key="primary"
             data-active-tab="interfaces"></div>
        <script id="librenms-sync-cache-initial" type="application/json">{state}</script>
        <ul id="librenmsSync">
          <li><button id="interfaces-tab" class="active" data-bs-toggle="tab" aria-controls="interfaces"></button></li>
          <li><button id="ipaddresses-tab" data-bs-toggle="tab" aria-controls="ipaddresses"></button></li>
        </ul>
        <span id="interfaces-cache-badge" class="d-none"></span>
        <span id="ipaddresses-cache-badge" class="d-none"></span>
        <div id="interfaces" class="tab-pane active" data-tab-id="interfaces"
             data-fragment-url="https://plugin.example.com/fragment/interfaces">
          <div id="interface-sync-content"><button id="interface-action">Sync</button></div>
        </div>
        <div id="ipaddresses" class="tab-pane" data-tab-id="ipaddresses"
             data-fragment-url="https://plugin.example.com/fragment/ipaddresses">
          <div id="ipaddress-sync-content"><button id="ip-action">Sync</button></div>
        </div>
        <div id="htmx-modal-content">
          <form><button id="modal-force-action" type="submit">Force</button></form>
        </div>
    """


def _state(
    revision,
    state="ready",
    *,
    source_tab=None,
    same_user=False,
    available=True,
    reason=None,
    refresh_error=None,
    refresh_error_timestamp=None,
):
    return {
        "revision": revision,
        "state": state,
        "source_tab": source_tab,
        "timestamp": "2026-08-14T00:00:00+00:00",
        "reason": reason,
        "refresh_error": refresh_error,
        "refresh_error_timestamp": refresh_error_timestamp,
        "same_user": same_user,
        "snapshot_available": available,
    }


def test_focus_check_clears_invalidated_rows_and_reports_anonymous_actor():
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": _state("mutation", state="locally_changed", source_tab="interfaces"),
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.route(
            "https://plugin.example.com/status?*",
            lambda route: route.fulfill(json={"tabs": current}),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); window.dispatchEvent(new Event('blur'))")
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        page.wait_for_function("document.querySelector('#ipaddress-sync-content').dataset.cacheEmpty === 'true'")

        assert page.locator("#ip-action").count() == 0
        assert not page.locator("#ipaddresses-cache-badge").evaluate("node => node.classList.contains('d-none')")
        notice = page.locator("#librenms-sync-cache-notices").inner_text()
        assert "Some sync data was cleared because another user synchronized data from LibreNMS." in notice
        assert "Interface data" not in notice
        assert "another user synchronized Interface data" in page.locator("#ipaddress-sync-content").inner_text()
        browser.close()


def test_cache_status_failure_disables_every_loaded_sync_control():
    """A failed status check must fail closed across tabs and an open modal."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.route(
            "https://plugin.example.com/status?*",
            lambda route: route.fulfill(status=503),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
        page.wait_for_function("syncCacheController().lastCheckFailed === true")

        assert page.locator("#interface-action").count() == 0
        assert page.locator("#ip-action").count() == 0
        assert page.locator("#modal-force-action").is_disabled()
        assert "could not be verified" in page.locator("#interface-sync-content").inner_text()
        assert "could not be verified" in page.locator("#ipaddress-sync-content").inner_text()
        browser.close()


def test_changed_source_comparison_restores_from_cache_fragment():
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": _state("after", state="locally_changed", source_tab="interfaces"),
        "ipaddresses": _state("before-ip"),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.route(
            "https://plugin.example.com/status?*",
            lambda route: route.fulfill(json={"tabs": current}),
        )
        page.route(
            "https://plugin.example.com/fragment/interfaces?*",
            lambda route: route.fulfill(body='<div id="restored-comparison">Current comparison</div>'),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")

        assert page.locator("#restored-comparison").inner_text() == "Current comparison"
        browser.close()


def test_countdown_expiry_removes_rows_and_sync_controls():
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            _page_html(initial).replace(
                '<button id="interface-action">Sync</button>',
                '<span id="countdown-timer" data-expiry="2000-01-01T00:00:00Z"></span>'
                '<button id="interface-action">Sync</button>',
            )
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); initializeCountdown('countdown-timer')")

        assert page.locator("#interface-action").count() == 0
        assert "cache expired" in page.locator("#interface-sync-content").inner_text()
        assert page.locator("#interfaces-cache-badge").inner_text() == "Expired"
        browser.close()


def test_failed_retry_updates_an_already_invalidated_tab_without_a_new_revision():
    initial = {
        "interfaces": _state("mutation", state="locally_changed", source_tab="interfaces"),
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }
    current = {
        "interfaces": initial["interfaces"],
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
            refresh_error="The latest LibreNMS refresh failed.",
            refresh_error_timestamp="2026-08-14T00:01:00+00:00",
        ),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
            current,
        )

        text = page.locator("#ipaddress-sync-content").inner_text()
        assert "synchronized Interface data" in text
        assert "latest LibreNMS refresh failed" in text
        browser.close()


def test_initiating_mutation_reports_one_toast_even_when_only_another_server_was_cleared():
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": _state("mutation", state="locally_changed", source_tab="interfaces", same_user=True),
        "ipaddresses": initial["ipaddresses"],
    }
    payload = {
        "transition_id": "mutation",
        "removed": True,
        "cleanup_failed": False,
        "tabs": ["ipaddresses"],
        "revisions": {"primary:interfaces": "mutation"},
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(
            "https://plugin.example.com/page",
            lambda route: route.fulfill(body=_page_html(initial)),
        )
        page.route(
            "https://plugin.example.com/status?*",
            lambda route: route.fulfill(json={"tabs": current}),
        )
        page.goto("https://plugin.example.com/page")
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "payload => { initializeSyncCacheConsistency(); "
            "document.dispatchEvent(new CustomEvent('librenmsCacheChanged', { detail: payload })); }",
            payload,
        )
        page.wait_for_function("document.querySelector('#librenms-sync-cache-notices') !== null")

        notices = page.locator("#librenms-sync-cache-notices .alert")
        assert notices.count() == 1
        assert "Other sync tabs were cleared" in notices.inner_text()
        browser.close()


def test_ready_refresh_restores_an_active_tab_after_local_invalidation():
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    invalidated = {
        "interfaces": _state(
            "mutation",
            state="invalidated",
            source_tab="ipaddresses",
            available=False,
        ),
        "ipaddresses": _state("mutation", state="locally_changed", source_tab="ipaddresses"),
    }
    refreshed = {
        "interfaces": _state("refresh", state="ready", source_tab="interfaces"),
        "ipaddresses": invalidated["ipaddresses"],
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.route(
            "https://plugin.example.com/fragment/interfaces?*",
            lambda route: route.fulfill(body='<div id="ready-comparison">Refreshed comparison</div>'),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "async states => { initializeSyncCacheConsistency(); "
            "await reconcileSyncCacheStatus(states.invalidated); "
            "await reconcileSyncCacheStatus(states.refreshed); }",
            {"invalidated": invalidated, "refreshed": refreshed},
        )

        assert page.locator("#ready-comparison").inner_text() == "Refreshed comparison"
        browser.close()


def test_ready_revision_restores_when_invalidation_and_refresh_collapse():
    """A focus check must reload data even when it misses the invalidated revision."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    refreshed = {
        "interfaces": _state("refresh", state="ready", source_tab="interfaces"),
        "ipaddresses": initial["ipaddresses"],
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.route(
            "https://plugin.example.com/fragment/interfaces?*",
            lambda route: route.fulfill(body='<div id="collapsed-refresh">Refreshed comparison</div>'),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
            refreshed,
        )

        assert page.locator("#collapsed-refresh").inner_text() == "Refreshed comparison"
        browser.close()


def test_missing_snapshot_without_reason_shows_generic_warning():
    """An unexplained cache disappearance must not blame another user."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    missing = {
        "interfaces": _state(None, state="missing", available=False),
        "ipaddresses": initial["ipaddresses"],
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
            missing,
        )

        warning = page.locator("#interface-sync-content").inner_text()
        assert "cache is unavailable" in warning
        assert "another user" not in warning
        browser.close()


def test_cross_user_refresh_failure_shows_shared_failure_reason():
    """A refresh failure must explain the shared state without naming the actor."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    failed = {
        "interfaces": initial["interfaces"],
        "ipaddresses": _state(
            "failure",
            state="refresh_failed",
            available=False,
            reason=(
                "Cached IP address data is unavailable because another user attempted "
                "to refresh it from LibreNMS and the refresh failed."
            ),
        ),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
            failed,
        )

        warning = page.locator("#ipaddress-sync-content").inner_text()
        assert "another user attempted to refresh" in warning
        assert "refresh failed" in warning
        browser.close()


def test_refreshed_hidden_tab_stays_marked_until_navigation():
    """A hidden tab with replaced rows must retain its refresh-required badge."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    invalidated = {
        "interfaces": initial["interfaces"],
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }
    refreshed = {
        "interfaces": initial["interfaces"],
        "ipaddresses": _state("refresh", state="ready", source_tab="ipaddresses"),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.add_script_tag(path=str(SCRIPT_PATH))
        result = page.evaluate(
            "async states => { initializeSyncCacheConsistency(); "
            "await reconcileSyncCacheStatus(states.invalidated); "
            "await reconcileSyncCacheStatus(states.refreshed); "
            "return syncCacheController().reloadTabs.has('ipaddresses'); }",
            {"invalidated": invalidated, "refreshed": refreshed},
        )

        assert result is True
        assert not page.locator("#ipaddresses-cache-badge").evaluate("node => node.classList.contains('d-none')")
        browser.close()


def test_initiating_inline_mutation_rebuilds_its_source_fragment():
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": _state("mutation", state="locally_changed", source_tab="interfaces", same_user=True),
        "ipaddresses": initial["ipaddresses"],
    }
    payload = {
        "transition_id": "mutation",
        "removed": False,
        "cleanup_failed": False,
        "tabs": [],
        "cleanup_tabs": ["ipaddresses"],
        "source_tab": "interfaces",
        "source_fragment_required": True,
        "revisions": {"primary:interfaces": "mutation"},
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("https://plugin.example.com/page", lambda route: route.fulfill(body=_page_html(initial)))
        page.route("https://plugin.example.com/status?*", lambda route: route.fulfill(json={"tabs": current}))
        page.route(
            "https://plugin.example.com/fragment/interfaces?*",
            lambda route: route.fulfill(body='<div id="inline-comparison">Updated inline comparison</div>'),
        )
        page.goto("https://plugin.example.com/page")
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "payload => { initializeSyncCacheConsistency(); "
            "document.dispatchEvent(new CustomEvent('librenmsCacheChanged', { detail: payload })); }",
            payload,
        )
        page.wait_for_selector("#inline-comparison")

        assert page.locator("#inline-comparison").inner_text() == "Updated inline comparison"
        browser.close()


def test_cleanup_failure_removes_controls_from_every_planned_tab():
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    payload = {
        "transition_id": "failed-cleanup",
        "removed": False,
        "cleanup_failed": True,
        "tabs": [],
        "cleanup_tabs": ["ipaddresses"],
        "source_tab": "interfaces",
        "source_fragment_required": False,
        "revisions": {},
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.route("https://plugin.example.com/status?*", lambda route: route.fulfill(json={"tabs": initial}))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "payload => { initializeSyncCacheConsistency(); "
            "document.dispatchEvent(new CustomEvent('librenmsCacheChanged', { detail: payload })); }",
            payload,
        )

        assert page.locator("#ip-action").count() == 0
        assert "cleanup could not be verified" in page.locator("#ipaddress-sync-content").inner_text()
        assert page.locator("#ipaddresses-cache-badge").inner_text() == "Refresh required"
        browser.close()


def test_invalidation_reason_includes_relative_time():
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": initial["interfaces"],
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
            current,
        )

        assert "ago" in page.locator("#ipaddress-sync-content").inner_text()
        browser.close()
