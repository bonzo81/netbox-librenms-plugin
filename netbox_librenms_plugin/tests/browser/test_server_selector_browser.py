"""Browser-level checks for object server selector navigation."""

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from django.template import Context, Engine


def _render_server_selector(active_server_key):
    """Render the production selector include with two transient server options."""
    template_root = Path(__file__).parents[2] / "templates"
    template = Engine(dirs=[template_root]).get_template("netbox_librenms_plugin/inc/_server_selector.html")
    options = [
        SimpleNamespace(
            server_key=server_key,
            display_name=f"{server_key.title()} LibreNMS",
            is_selectable=True,
            is_active=server_key == active_server_key,
        )
        for server_key in ("primary", "secondary")
    ]
    return template.render(
        Context(
            {
                "request": SimpleNamespace(path="/import/"),
                "server_key": active_server_key,
                "server_selection_active_name": f"{active_server_key.title()} LibreNMS",
                "all_server_mappings": options,
            }
        )
    )


def _render_cached_search_links():
    """Render production cached-search links for two servers with identical filters."""
    template_root = Path(__file__).parents[2] / "templates"
    template = Engine(dirs=[template_root]).get_template("netbox_librenms_plugin/inc/_cached_search_links.html")
    searches = [
        SimpleNamespace(
            server_key="primary",
            server_display_name="Primary LibreNMS",
            filters={"hostname": "shared-edge"},
            display_filters={"hostname": "shared-edge"},
            vc_enabled=False,
            use_sysname=True,
            strip_domain=False,
            device_count=1,
            cached_at="2026-08-23T10:00:00+00:00",
            cache_timeout=300,
            remaining_seconds=240,
        ),
        SimpleNamespace(
            server_key="secondary",
            server_display_name="Secondary LibreNMS",
            filters={"hostname": "shared-edge"},
            display_filters={"hostname": "shared-edge"},
            vc_enabled=False,
            use_sysname=False,
            strip_domain=True,
            device_count=1,
            cached_at="2026-08-23T10:01:00+00:00",
            cache_timeout=300,
            remaining_seconds=299,
        ),
    ]
    return template.render(Context({"cached_searches": searches}, use_l10n=False))


def test_cached_search_labels_its_server_and_activates_that_server_when_opened(page):
    """A cached-search click carries its server and complete search state."""
    base_url = "https://plugin.example.com/import/"
    destination = (
        f"{base_url}?server_key=primary&apply_filters=1&librenms_hostname=shared-edge&use_sysname=1&strip_domain=0"
    )
    initial_html = f'<div id="cached-searches">{_render_cached_search_links()}</div>'
    destination_html = """
        <div id="librenms-server-selector" data-active-server-key="primary"></div>
        <div id="search-results">primary-shared-edge</div>
    """

    def handle_route(route):
        if route.request.url == base_url:
            route.fulfill(body=initial_html, content_type="text/html")
            return
        if route.request.url == destination:
            route.fulfill(body=destination_html, content_type="text/html")
            return
        raise AssertionError(f"Unexpected browser request: {route.request.url}")

    page.route("**/*", handle_route)
    page.goto(base_url)

    assert page.locator('[data-cached-server-key="primary"] .cached-search-server-label').inner_text() == (
        "Primary LibreNMS"
    )
    assert page.locator('[data-cached-server-key="secondary"] .cached-search-server-label').inner_text() == (
        "Secondary LibreNMS"
    )
    page.locator('[data-cached-server-key="primary"]').click()
    page.wait_for_url(destination)

    assert parse_qs(urlparse(page.url).query) == {
        "server_key": ["primary"],
        "apply_filters": ["1"],
        "librenms_hostname": ["shared-edge"],
        "use_sysname": ["1"],
        "strip_domain": ["0"],
    }
    assert page.locator("#librenms-server-selector").get_attribute("data-active-server-key") == "primary"
    assert page.locator("#search-results").inner_text() == "primary-shared-edge"


def test_server_switch_keeps_the_tab_and_marks_the_destination_active(page):
    """A selector click replaces transient state with the chosen server and current tab."""
    destination = "https://plugin.example.com/device/1/librenms-sync/?tab=modules&server_key=primary"
    initial_html = f"""
        <div id="librenms-server-selector" data-active-server-key="secondary">
          <button type="button">Secondary LibreNMS</button>
          <ul>
            <li><a id="switch-primary" href="{destination}">Primary LibreNMS</a></li>
            <li><span class="active" aria-current="true">Secondary LibreNMS</span></li>
          </ul>
        </div>
    """
    destination_html = """
        <div id="librenms-server-selector" data-active-server-key="primary">
          <button type="button">Primary LibreNMS</button>
          <ul><li><span class="active" aria-current="true">Primary LibreNMS</span></li></ul>
        </div>
        <div id="librenms-sync-tabs" data-active-tab="modules"></div>
    """

    page.route(destination, lambda route: route.fulfill(body=destination_html, content_type="text/html"))
    page.set_content(initial_html)

    page.locator("#switch-primary").click()
    page.wait_for_url(destination)

    query = parse_qs(urlparse(page.url).query)
    assert query == {"tab": ["modules"], "server_key": ["primary"]}
    assert page.locator("#librenms-server-selector").get_attribute("data-active-server-key") == "primary"
    assert page.locator("#librenms-sync-tabs").get_attribute("data-active-tab") == "modules"
    assert page.locator('[aria-current="true"]').inner_text() == "Primary LibreNMS"


def test_import_server_switch_clears_search_without_persisting_selection(page):
    """An import selector click only requests the current and transient destination URLs."""
    base_url = "https://plugin.example.com/import/"
    initial_url = f"{base_url}?server_key=primary&apply_filters=1&librenms_hostname=old-search&job_id=42"
    destination = f"{base_url}?server_key=secondary"
    requested_urls = []
    initial_html = f"""
        {_render_server_selector("primary")}
        <form><input name="librenms_hostname" value="old-search"></form>
        <div id="search-results">Old result</div>
    """
    destination_html = f"""
        {_render_server_selector("secondary")}
        <form><input name="librenms_hostname" value=""></form>
        <div id="search-results"></div>
    """

    def handle_route(route):
        requested_urls.append(route.request.url)
        if route.request.url == initial_url:
            route.fulfill(body=initial_html, content_type="text/html")
            return
        if route.request.url == destination:
            route.fulfill(body=destination_html, content_type="text/html")
            return
        raise AssertionError(f"Unexpected browser request: {route.request.url}")

    page.route("**/*", handle_route)
    page.goto(initial_url)

    page.get_by_role("link", name="Secondary LibreNMS").click()
    page.wait_for_url(destination)

    query = parse_qs(urlparse(page.url).query)
    assert query == {"server_key": ["secondary"]}
    assert page.locator('[name="librenms_hostname"]').input_value() == ""
    assert page.locator("#search-results").inner_text() == ""
    assert page.locator("#librenms-server-selector").get_attribute("data-active-server-key") == "secondary"
    assert requested_urls == [initial_url, destination]


def test_import_server_switch_has_no_local_settings_stand_in():
    """Do not let a local stand-in make the server-selector browser test pass."""
    source = Path(__file__).read_text()

    assert 'installation_settings = {"selected_server": "primary"}' not in source


def test_stopped_import_filter_job_returns_to_the_active_server(page):
    """A stopped background search clears filters but retains its transient server."""
    base_url = "https://plugin.example.com/import/"
    active_server_url = f"{base_url}?server_key=secondary"
    poll_url = "https://plugin.example.com/api/jobs/active-filter/"
    script_path = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "js" / "librenms_import.js"
    initial_html = f"""
        <form id="librenms-import-filter-form" action="{base_url}">
          <input name="server_key" value="secondary">
          <input name="librenms_hostname" value="background-edge">
        </form>
        <div id="filter-processing-modal">
          <h5>Applying Filters</h5>
          <span class="spinner-border"></span>
          <div id="filter-progress-message"></div>
          <button id="cancel-filter-btn" type="button">Cancel</button>
        </div>
    """

    def handle_route(route):
        if route.request.url == poll_url:
            route.fulfill(json={"status": {"value": "stopped"}})
            return
        if route.request.url.startswith(f"{base_url}?") and "librenms_hostname" in route.request.url:
            route.fulfill(
                json={
                    "use_polling": True,
                    "job_id": "active-filter",
                    "job_pk": 42,
                    "poll_url": poll_url,
                    "device_count": 1,
                }
            )
            return
        if route.request.url == base_url:
            route.fulfill(body=initial_html, content_type="text/html")
            return
        if route.request.url == active_server_url:
            route.fulfill(body='<div id="redirect-destination"></div>', content_type="text/html")
            return
        raise AssertionError(f"Unexpected browser request: {route.request.url}")

    page.route("**/*", handle_route)
    page.goto(base_url)
    page.add_script_tag(path=script_path)

    page.locator("#librenms-import-filter-form").evaluate("form => form.requestSubmit()")
    page.wait_for_url(active_server_url)

    assert page.url == active_server_url


def test_preferred_star_is_reversible_without_changing_the_active_server(page):
    """Two star actions reverse preference while preserving transient active state."""
    action = "https://plugin.example.com/devices/1/preferred-server/"
    destination = "https://plugin.example.com/device/1/librenms-sync/?tab=modules&server_key=primary"
    preferred = {"key": "secondary"}
    submissions = []
    dialogs = []

    def page_html():
        other = "primary" if preferred["key"] == "secondary" else "secondary"
        return f"""
            <div id="librenms-server-selector" data-active-server-key="primary"></div>
            <div id="librenms-connections" data-preferred-server-key="{preferred["key"]}">
              <span data-server-key="{preferred["key"]}"><i class="mdi mdi-star"></i></span>
              <form method="post" action="{action}">
                <input type="hidden" name="server_key" value="{other}">
                <input type="hidden" name="active_server_key" value="primary">
                <input type="hidden" name="tab" value="modules">
                <input type="hidden" name="object_type" value="device">
                <button id="set-{other}-preferred" type="submit"><i class="mdi mdi-star-outline"></i></button>
              </form>
            </div>
        """

    def handle_preference(route):
        submitted = parse_qs(route.request.post_data or "")
        submissions.append(submitted)
        preferred["key"] = submitted["server_key"][0]
        route.fulfill(body=page_html(), content_type="text/html")

    def handle_route(route):
        if route.request.url == action:
            handle_preference(route)
            return
        if route.request.url.startswith("https://plugin.example.com/device/1/librenms-sync/"):
            route.fulfill(body=page_html(), content_type="text/html")
            return
        raise AssertionError(f"Unexpected browser request: {route.request.url}")

    page.on("dialog", lambda dialog: dialogs.append(dialog.message))
    page.route("**/*", handle_route)
    page.goto(destination)

    page.locator("#set-primary-preferred").click()
    page.wait_for_load_state()
    assert page.locator("#librenms-connections").get_attribute("data-preferred-server-key") == "primary"
    assert page.locator("#librenms-server-selector").get_attribute("data-active-server-key") == "primary"

    page.locator("#set-secondary-preferred").click()
    page.wait_for_load_state()
    assert page.locator("#librenms-connections").get_attribute("data-preferred-server-key") == "secondary"
    assert page.locator("#librenms-server-selector").get_attribute("data-active-server-key") == "primary"
    assert submissions == [
        {
            "server_key": ["primary"],
            "active_server_key": ["primary"],
            "tab": ["modules"],
            "object_type": ["device"],
        },
        {
            "server_key": ["secondary"],
            "active_server_key": ["primary"],
            "tab": ["modules"],
            "object_type": ["device"],
        },
    ]
    assert dialogs == []
