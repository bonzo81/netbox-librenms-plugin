"""Browser-level checks for the sync-tab cache state machine."""

import json
from pathlib import Path

from playwright import sync_api as playwright

SCRIPT_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"
STYLE_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "css" / "librenms_sync.css"


def _replace_fixture_markup(html, old, new):
    """Replace fixture markup, failing when _page_html no longer emits the source markup."""
    assert old in html, f"fixture markup is gone, so the replacement would be a no-op: {old}"
    return html.replace(old, new)


def _page_html(initial_state, contract=None, active_tab="interfaces", valid_states=None):
    state = json.dumps(initial_state)
    contract = contract or {
        "interfaces": {"content_id": "interface-sync-content", "label": "Interface"},
        "ipaddresses": {"content_id": "ipaddress-sync-content", "label": "IP address"},
    }
    states = (
        ["ready", "invalidated", "refresh_failed", "locally_changed", "missing"]
        if valid_states is None
        else valid_states
    )
    serialized_contract = {
        "tabs": contract,
        "states": states,
    }
    contract_json = json.dumps(serialized_contract)
    interface_content_id = contract["interfaces"]["content_id"]
    ip_content_id = contract["ipaddresses"]["content_id"]
    return f"""
        <div id="librenms-sync-cache-state"
             data-status-url="https://plugin.example.com/status"
             data-server-key="primary"></div>
        <script id="librenms-sync-cache-initial" type="application/json">{state}</script>
        <script id="librenms-sync-cache-contract" type="application/json">{contract_json}</script>
        <div id="librenms-sync-tabs" data-active-tab="{active_tab}">
          <ul id="librenmsSync">
            <li><a id="interfaces-tab" class="nav-link {"active" if active_tab == "interfaces" else ""}"
                   data-tab="interfaces" href="https://plugin.example.com/page?tab=interfaces"
                   data-tab-label="Interfaces"
                   aria-controls="interfaces">Interfaces</a></li>
            <li><a id="ipaddresses-tab" class="nav-link {"active" if active_tab == "ipaddresses" else ""}"
                   data-tab="ipaddresses" href="https://plugin.example.com/page?tab=ipaddresses"
                   data-tab-label="IP Addresses"
                   aria-controls="ipaddresses">IP Addresses</a></li>
          </ul>
        <div id="interfaces" class="tab-pane{" active" if active_tab == "interfaces" else ""}"
             data-tab-id="interfaces"
             data-fragment-url="https://plugin.example.com/fragment/interfaces">
          <div id="{interface_content_id}"><button id="interface-action">Sync</button></div>
        </div>
        <div id="ipaddresses" class="tab-pane{" active" if active_tab == "ipaddresses" else ""}"
             data-tab-id="ipaddresses"
             data-fragment-url="https://plugin.example.com/fragment/ipaddresses">
          <div id="{ip_content_id}"><button id="ip-action">Sync</button></div>
        </div>
        </div>
        <div id="htmx-modal-content">
          <form><button id="modal-force-action" type="submit">Force</button></form>
        </div>
    """


def test_server_contract_drives_cache_content_replacement():
    """The browser must use the tab metadata supplied by the server."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        **initial,
        "interfaces": _state(
            "after-interfaces",
            state="invalidated",
            source_tab="ipaddresses",
            available=False,
        ),
    }
    contract = {
        "interfaces": {"content_id": "custom-interface-content", "label": "Network port"},
        "ipaddresses": {"content_id": "ipaddress-sync-content", "label": "IP address"},
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial, contract))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
            current,
        )

        assert page.locator("#interface-action").count() == 0
        assert "Network port data" in page.locator("#custom-interface-content").inner_text()
        browser.close()


def test_server_contract_defines_valid_wire_states():
    """The browser must validate status states against the server contract."""
    initial = {
        "interfaces": _state("before-interfaces", state="source_ready"),
        "ipaddresses": _state("before-ip", state="source_ready"),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial, valid_states=["source_ready"]))
        page.route(
            "https://plugin.example.com/status?*",
            lambda route: route.fulfill(json={"tabs": initial}),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
        page.wait_for_function("syncCacheController().checking === null")

        assert page.evaluate("syncCacheController().lastCheckFailed") is False
        assert page.locator("#interface-action").count() == 1
        assert not page.locator("#modal-force-action").is_disabled()
        browser.close()


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
    attention_required=None,
):
    state = {
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
    if attention_required is not None:
        state["attention_required"] = attention_required
    return state


def test_outer_tab_navigation_uses_the_rendered_status_as_the_next_baseline():
    """A status response started before navigation must not overwrite server-rendered state."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ipaddresses"),
    }
    rendered = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("rendered-ipaddresses"),
    }
    stale = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state(
            "stale-ipaddresses",
            state="locally_changed",
            source_tab="ipaddresses",
        ),
    }
    fragment_requests = []

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("document.dispatchEvent(new Event('DOMContentLoaded')); initializeScripts();")
        page.evaluate(
            """() => {
                const realFetch = window.fetch;
                window.statusRequestStarted = false;
                window.releaseStatus = null;
                window.fetch = (input, init) => {
                    if (String(input).includes('/status?')) {
                        window.statusRequestStarted = true;
                        return new Promise(resolve => {
                            window.releaseStatus = () => resolve(new Response(
                                JSON.stringify({tabs: window.staleStatus}),
                                {status: 200, headers: {'Content-Type': 'application/json'}}
                            ));
                        });
                    }
                    return realFetch(input, init);
                };
            }"""
        )
        page.route(
            "https://plugin.example.com/fragment/ipaddresses?*",
            lambda route: (fragment_requests.append(route.request.url), route.fulfill(body="<p>Reloaded</p>")),
        )
        page.evaluate(
            """() => {
                window.inFlightStatus = checkSyncCacheStatus();
                window.statusRequestSettled = false;
                window.inFlightStatus.then(() => { window.statusRequestSettled = true; });
            }"""
        )
        page.wait_for_function("window.statusRequestStarted === true")

        page.evaluate(
            """status => {
                const region = document.querySelector('#librenms-sync-tabs');
                region.outerHTML = '<div id="librenms-sync-tabs" data-active-tab="ipaddresses">'
                    + '<script id="librenms-sync-rendered-status" type="application/json">'
                    + JSON.stringify(status)
                    + '</script>'
                    + '<div id="interfaces" class="tab-pane" data-tab-id="interfaces" '
                    + 'data-fragment-url="https://plugin.example.com/fragment/interfaces"></div>'
                    + '<div id="ipaddresses" class="tab-pane active" data-tab-id="ipaddresses" '
                    + 'data-fragment-url="https://plugin.example.com/fragment/ipaddresses">'
                    + '<div id="ipaddress-sync-content"><p>Server-rendered IP rows</p></div>'
                    + '</div></div>';
                document.querySelector('#librenms-sync-tabs').dispatchEvent(
                    new CustomEvent('htmx:afterSwap', {bubbles: true})
                );
            }""",
            rendered,
        )
        assert page.evaluate("syncCacheController().status.ipaddresses.revision") == "rendered-ipaddresses"

        page.evaluate("status => { window.staleStatus = status; window.releaseStatus(); }", stale)
        page.wait_for_function("window.statusRequestSettled === true")

        assert fragment_requests == []
        assert page.locator("#ipaddress-sync-content").inner_text() == "Server-rendered IP rows"
        assert page.evaluate("syncCacheController().status.ipaddresses.revision") == "rendered-ipaddresses"
        browser.close()


def test_cold_tab_does_not_show_stale_state_before_first_refresh():
    """A tab with no snapshot history must retain its initial refresh state."""
    initial = {
        "interfaces": _state(None, state="missing", available=False),
        "ipaddresses": _state(None, state="missing", available=False),
    }
    initial["interfaces"]["timestamp"] = None
    initial["ipaddresses"]["timestamp"] = None
    html = _replace_fixture_markup(
        _page_html(initial),
        '<button id="interface-action">Sync</button>',
        '<button id="interface-refresh">Refresh Interfaces</button>',
    )

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency()")

        assert page.locator("#interface-refresh").count() == 1
        assert not page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")
        assert "cache is unavailable" not in page.locator("#interface-sync-content").inner_text()
        browser.close()


def test_cold_tab_does_not_flash_stale_during_tab_navigation():
    """Checking a never-refreshed tab must not briefly mark it stale."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state(None, state="missing", available=False),
    }
    initial["ipaddresses"]["timestamp"] = None

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(
            "https://plugin.example.com/page*",
            lambda route: route.fulfill(body=_page_html(initial), content_type="text/html"),
        )
        page.route(
            "https://plugin.example.com/status?*",
            lambda route: route.fulfill(json={"tabs": initial}),
        )
        page.goto("https://plugin.example.com/page")
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            """
            () => {
                initializeSyncCacheConsistency();
                const tab = document.querySelector('#ipaddresses-tab');
                new MutationObserver(() => {
                    if (tab.classList.contains('sync-cache-unavailable')) {
                        sessionStorage.setItem('ip-state-flashed', 'true');
                    }
                }).observe(tab, { attributes: true, attributeFilter: ['class'] });
            }
            """
        )
        page.locator("#ipaddresses-tab").click()
        page.wait_for_url("**/page?tab=ipaddresses")

        assert page.evaluate("sessionStorage.getItem('ip-state-flashed')") is None
        assert not page.locator("#ipaddresses-tab").evaluate(
            "node => node.classList.contains('sync-cache-unavailable')"
        )
        browser.close()


def test_status_check_queues_one_follow_up_while_a_request_is_in_flight():
    """A second trigger must observe state that changed during the first request."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state("ipaddresses-ready"),
    }
    changed = {
        **initial,
        "ipaddresses": _state(
            "ipaddresses-invalidated",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }
    payloads = [initial, changed]
    requests = []

    def serve_status(route):
        requests.append(route.request.url)
        route.fulfill(json={"tabs": payloads[min(len(requests) - 1, 1)]})

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.route("https://plugin.example.com/status?*", serve_status)
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            """
            () => {
                initializeSyncCacheConsistency();
                checkSyncCacheStatus();
                checkSyncCacheStatus();
            }
            """
        )
        page.wait_for_function(
            "document.querySelector('#ipaddresses-tab').classList.contains('sync-cache-unavailable')"
        )

        assert len(requests) == 2
        assert page.locator("#ipaddresses-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")
        browser.close()


def test_healthy_tab_click_navigates_without_bootstrap_global():
    """A ready tab must load when NetBox does not expose Bootstrap as a global."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state("ipaddresses-ready"),
    }

    def serve_page(route):
        active_tab = "ipaddresses" if "tab=ipaddresses" in route.request.url else "interfaces"
        route.fulfill(body=_page_html(initial, active_tab=active_tab), content_type="text/html")

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("https://plugin.example.com/page*", serve_page)
        page.route(
            "https://plugin.example.com/status?*",
            lambda route: route.fulfill(json={"tabs": initial}),
        )
        page.goto("https://plugin.example.com/page")
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency()")

        assert page.evaluate("typeof bootstrap") == "undefined"
        page.locator("#ipaddresses-tab").click()
        page.wait_for_url("**/page?tab=ipaddresses")

        assert page.locator("#ipaddresses").evaluate("node => node.classList.contains('active')")
        browser.close()


def test_refreshing_one_tab_does_not_mark_never_refreshed_tabs_stale():
    """A successful refresh must not change untouched cold-tab affordances."""
    initial = {
        "interfaces": _state(None, state="missing", available=False),
        "ipaddresses": _state(None, state="missing", available=False),
    }
    initial["interfaces"]["timestamp"] = None
    initial["ipaddresses"]["timestamp"] = None
    refreshed = {
        "interfaces": _state("interfaces-refresh"),
        "ipaddresses": initial["ipaddresses"],
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
            refreshed,
        )

        assert not page.locator("#ipaddresses-tab").evaluate(
            "node => node.classList.contains('sync-cache-unavailable')"
        )
        browser.close()


def test_cache_state_uses_theme_rails_without_resizing():
    """Available and unavailable states must not add text or change tab dimensions."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state("ipaddresses-ready"),
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

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.add_style_tag(path=str(STYLE_PATH))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            """
            () => {
                document.documentElement.style.setProperty('--tblr-warning', 'rgb(245, 159, 0)');
                document.documentElement.style.setProperty('--tblr-success', 'rgb(47, 179, 68)');
                document.documentElement.style.setProperty('--tblr-border-radius', '4px');
                initializeSyncCacheConsistency();
            }
            """
        )
        width_before = page.locator("#ipaddresses-tab").evaluate("node => node.getBoundingClientRect().width")
        tab = page.locator("#ipaddresses-tab")

        assert tab.evaluate("node => node.classList.contains('sync-cache-ready')")
        assert tab.evaluate("node => getComputedStyle(node, '::before').backgroundColor") == "rgb(47, 179, 68)"

        page.evaluate("status => reconcileSyncCacheStatus(status)", invalidated)
        width_after = tab.evaluate("node => node.getBoundingClientRect().width")

        assert width_after == width_before
        assert tab.inner_text() == "IP Addresses"
        assert tab.evaluate("node => node.classList.contains('sync-cache-unavailable')")
        assert tab.get_attribute("title") == "Cached data is unavailable. Refresh this tab."
        assert tab.get_attribute("aria-label") == "IP Addresses. Cached data is unavailable."
        assert tab.evaluate("node => getComputedStyle(node, '::before').height") == "3px"
        assert tab.evaluate("node => getComputedStyle(node, '::before').backgroundColor") == "rgb(245, 159, 0)"

        page.evaluate("document.documentElement.style.setProperty('--tblr-warning', 'rgb(255, 193, 7)')")
        assert tab.evaluate("node => getComputedStyle(node, '::before').backgroundColor") == "rgb(255, 193, 7)"
        browser.close()


def test_selecting_an_unavailable_tab_acknowledges_its_attention_state():
    initial = {
        "interfaces": _state("interfaces-ready"),
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
        page.route(
            "https://plugin.example.com/page*",
            lambda route: route.fulfill(
                body=_page_html(
                    initial,
                    active_tab="ipaddresses" if "tab=ipaddresses" in route.request.url else "interfaces",
                ),
                content_type="text/html",
            ),
        )
        page.goto("https://plugin.example.com/page")
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency()")
        tab = page.locator("#ipaddresses-tab")
        assert tab.evaluate("node => node.classList.contains('sync-cache-unavailable')")

        tab.click()
        page.wait_for_url("**/page?tab=ipaddresses")
        selected_tab = page.locator("#ipaddresses-tab")

        assert not selected_tab.evaluate("node => node.classList.contains('sync-cache-unavailable')")
        assert selected_tab.get_attribute("title") is None
        browser.close()


def test_acknowledged_status_does_not_restore_unavailable_attention():
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
            attention_required=False,
        ),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency()")

        assert not page.locator("#ipaddresses-tab").evaluate(
            "node => node.classList.contains('sync-cache-unavailable')"
        )
        browser.close()


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
        assert page.locator("#ipaddresses-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")
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
        console_errors = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
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
        assert any("HTTP 503" in message for message in console_errors), console_errors
        browser.close()


def test_successful_cache_status_check_restores_only_fail_closed_modal_controls():
    """A transient status failure must not leave an open modal permanently disabled."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    attempts = 0

    def status_response(route):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            route.fulfill(status=503)
        else:
            route.fulfill(json={"tabs": initial})

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.evaluate(
            """() => {
                const control = document.createElement('button');
                control.id = 'modal-already-disabled';
                control.disabled = true;
                document.querySelector('#htmx-modal-content form').append(control);
            }"""
        )
        page.route("https://plugin.example.com/status?*", status_response)
        page.route(
            "https://plugin.example.com/fragment/interfaces?*",
            lambda route: route.fulfill(body='<button id="restored-interface-action">Sync</button>'),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
        page.wait_for_function(
            "syncCacheController().lastCheckFailed === true && syncCacheController().checking === null"
        )

        assert page.locator("#modal-force-action").is_disabled()
        assert page.locator("#modal-already-disabled").is_disabled()

        page.evaluate("checkSyncCacheStatus()")
        page.wait_for_function(
            "syncCacheController().lastCheckFailed === false && syncCacheController().checking === null"
        )

        assert not page.locator("#modal-force-action").is_disabled()
        assert page.locator("#modal-already-disabled").is_disabled()
        assert page.locator("#restored-interface-action").count() == 1
        browser.close()


def test_available_status_without_a_usable_fragment_keeps_modal_controls_disabled():
    """A status response alone must not restore controls after fail-closed content loss."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    attempts = 0

    def status_response(route):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            route.fulfill(status=503)
        else:
            route.fulfill(json={"tabs": initial})

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.route("https://plugin.example.com/status?*", status_response)
        page.route(
            "https://plugin.example.com/fragment/interfaces?*",
            lambda route: route.fulfill(status=503),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
        page.wait_for_function(
            "syncCacheController().lastCheckFailed === true && syncCacheController().checking === null"
        )

        page.evaluate("checkSyncCacheStatus()")
        page.wait_for_function(
            "syncCacheController().lastCheckFailed === false && syncCacheController().checking === null"
        )

        assert page.locator("#modal-force-action").is_disabled()
        assert "could not be restored" in page.locator("#interface-sync-content").inner_text()
        browser.close()


def test_hung_cache_status_request_times_out_and_fails_closed():
    """A stalled status request must release the controller and remove stale controls."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.clock.install()
        page.set_content(_page_html(initial))
        stalled_routes = []
        page.route("https://plugin.example.com/status?*", lambda route: stalled_routes.append(route))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("() => { initializeSyncCacheConsistency(); checkSyncCacheStatus(); }")
        assert page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")

        page.clock.fast_forward(20_000)
        page.clock.resume()
        page.wait_for_function("syncCacheController().lastCheckFailed === true", timeout=1_000)

        assert page.evaluate("syncCacheController().checking") is None
        assert page.locator("#interface-action").count() == 0
        assert not page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")
        for route in stalled_routes:
            route.abort()
        browser.close()


def test_hung_cache_fragment_request_times_out_and_fails_closed():
    """A stalled fragment request must release the controller and remove stale controls."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        **initial,
        "interfaces": _state("after-interfaces"),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.clock.install()
        page.set_content(_page_html(initial))
        stalled_routes = []
        page.route(
            "https://plugin.example.com/status?*",
            lambda route: route.fulfill(json={"tabs": current}),
        )
        page.route("https://plugin.example.com/fragment/interfaces?*", lambda route: stalled_routes.append(route))
        page.add_script_tag(path=str(SCRIPT_PATH))
        with page.expect_request("https://plugin.example.com/fragment/interfaces?*"):
            page.evaluate("() => { initializeSyncCacheConsistency(); checkSyncCacheStatus(); }")
        page.wait_for_function("syncCacheController().status.interfaces.revision === 'after-interfaces'")
        assert page.evaluate("syncCacheController().checking !== null")
        assert page.evaluate("syncCacheController().lastCheckFailed") is False
        assert page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")

        page.clock.fast_forward(20_000)
        page.clock.resume()
        page.wait_for_function("syncCacheController().lastCheckFailed === true", timeout=1_000)

        assert page.evaluate("syncCacheController().checking") is None
        assert page.locator("#interface-action").count() == 0
        assert not page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")
        assert stalled_routes
        for route in stalled_routes:
            route.abort()
        browser.close()


def test_valid_status_recovers_when_the_initial_state_is_malformed():
    """The stable tab contract must validate status when initial state parsing fails."""
    current = {
        "interfaces": _state("current-interfaces"),
        "ipaddresses": _state("current-ipaddresses"),
    }
    html = _replace_fixture_markup(
        _page_html(current),
        f'<script id="librenms-sync-cache-initial" type="application/json">{json.dumps(current)}</script>',
        '<script id="librenms-sync-cache-initial" type="application/json">{malformed</script>',
    )

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        page.route(
            "https://plugin.example.com/status?*",
            lambda route: route.fulfill(json={"tabs": current}),
        )
        page.route(
            "https://plugin.example.com/fragment/interfaces?*",
            lambda route: route.fulfill(body='<button id="interface-action">Sync</button>'),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
        page.wait_for_function("syncCacheController().checking === null")

        assert page.evaluate("syncCacheController().lastCheckFailed") is False
        assert page.locator("#interface-action").count() == 1
        assert page.locator("#ip-action").count() == 1
        browser.close()


def test_fragment_failure_logs_the_http_status_and_clears_the_content():
    """A fragment failure must retain its diagnostic while failing closed."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        **initial,
        "interfaces": _state("after-interfaces", state="locally_changed", source_tab="interfaces"),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        console_errors = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.set_content(_page_html(initial))
        page.route(
            "https://plugin.example.com/status?*",
            lambda route: route.fulfill(json={"tabs": current}),
        )
        page.route(
            "https://plugin.example.com/fragment/interfaces?*",
            lambda route: route.fulfill(status=503),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
        page.wait_for_function("document.querySelector('#interface-sync-content').dataset.cacheEmpty === 'true'")

        assert any("HTTP 503" in message for message in console_errors), console_errors
        assert "could not be restored" in page.locator("#interface-sync-content").inner_text()
        browser.close()


def test_malformed_cache_status_disables_every_loaded_sync_control():
    """A successful response with an invalid schema must fail closed."""
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
            lambda route: route.fulfill(json={"tabs": None}),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
        page.wait_for_function("syncCacheController().checking === null")

        assert page.locator("#interface-action").count() == 0
        assert page.locator("#ip-action").count() == 0
        assert page.locator("#modal-force-action").is_disabled()
        assert page.evaluate("syncCacheController().lastCheckFailed") is True
        browser.close()


def test_null_cache_status_disables_every_loaded_sync_control():
    """A null JSON response must fail closed like every other invalid schema."""
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
            lambda route: route.fulfill(body="null", content_type="application/json"),
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
        page.wait_for_function("syncCacheController().checking === null")

        assert page.locator("#interface-action").count() == 0
        assert page.locator("#ip-action").count() == 0
        assert page.locator("#modal-force-action").is_disabled()
        assert page.evaluate("syncCacheController().lastCheckFailed") is True
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
            _replace_fixture_markup(
                _page_html(initial),
                '<button id="interface-action">Sync</button>',
                '<span id="countdown-timer" data-expiry="2000-01-01T00:00:00Z"></span>'
                '<button id="interface-action">Sync</button>',
            )
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); initializeCountdown('countdown-timer')")

        assert page.locator("#interface-action").count() == 0
        assert "cache expired" in page.locator("#interface-sync-content").inner_text()
        assert page.locator("#interfaces-tab").evaluate(
            "node => !node.classList.contains('sync-cache-ready') && !node.classList.contains('sync-cache-unavailable')"
        )
        assert page.locator("#interfaces-tab").get_attribute("title") is None
        browser.close()


def test_hidden_tab_countdown_changes_available_rail_to_unavailable_without_interaction():
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            _replace_fixture_markup(
                _page_html(initial),
                '<button id="ip-action">Sync</button>',
                '<span id="ip-countdown-timer" data-expiry="2000-01-01T00:00:00Z"></span>'
                '<button id="ip-action">Sync</button>',
            )
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncCacheConsistency(); initializeCountdown('ip-countdown-timer')")
        tab = page.locator("#ipaddresses-tab")

        assert not tab.evaluate("node => node.classList.contains('sync-cache-ready')")
        assert tab.evaluate("node => node.classList.contains('sync-cache-unavailable')")
        assert page.locator("#ip-action").count() == 0
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
            lambda route: route.fulfill(body=_page_html(initial), content_type="text/html"),
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


def test_refreshed_hidden_tab_stays_marked_until_server_rendered_navigation():
    """A hidden tab with replaced rows must retain its unavailable state."""
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
        page.evaluate(
            "async states => { initializeSyncCacheConsistency(); "
            "await reconcileSyncCacheStatus(states.invalidated); "
            "await reconcileSyncCacheStatus(states.refreshed); }",
            {"invalidated": invalidated, "refreshed": refreshed},
        )

        assert page.locator("#ipaddresses-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")
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
        page.route(
            "https://plugin.example.com/page",
            lambda route: route.fulfill(body=_page_html(initial), content_type="text/html"),
        )
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
        assert page.locator("#ipaddresses-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")
        browser.close()


def test_cleanup_failure_notice_survives_a_later_informational_event():
    """A later success must not hide the reload instruction from a failed cleanup."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    failed = {
        "transition_id": "failed-cleanup",
        "removed": False,
        "cleanup_failed": True,
        "cleanup_tabs": ["ipaddresses"],
        "revisions": {},
    }
    succeeded = {
        "transition_id": "later-success",
        "removed": True,
        "cleanup_failed": False,
        "tabs": ["ipaddresses"],
        "revisions": {},
    }

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_page_html(initial))
        page.route("https://plugin.example.com/status?*", lambda route: route.fulfill(json={"tabs": initial}))
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate(
            "payloads => { initializeSyncCacheConsistency(); "
            "payloads.forEach(payload => document.dispatchEvent("
            "new CustomEvent('librenmsCacheChanged', { detail: payload }))); }",
            [failed, succeeded],
        )

        notice = page.locator("#librenms-sync-cache-notices .alert")
        assert notice.count() == 1
        assert notice.evaluate("node => node.classList.contains('alert-danger')")
        assert "related cache cleanup failed" in notice.inner_text()
        assert "Other sync tabs were cleared" not in notice.inner_text()
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
