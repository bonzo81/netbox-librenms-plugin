"""Browser-level checks for object server selector navigation."""

from urllib.parse import parse_qs, urlparse

from playwright import sync_api as playwright


def test_server_switch_keeps_the_tab_and_marks_the_destination_active():
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

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(destination, lambda route: route.fulfill(body=destination_html, content_type="text/html"))
        page.set_content(initial_html)

        page.locator("#switch-primary").click()
        page.wait_for_url(destination)

        query = parse_qs(urlparse(page.url).query)
        assert query == {"tab": ["modules"], "server_key": ["primary"]}
        assert page.locator("#librenms-server-selector").get_attribute("data-active-server-key") == "primary"
        assert page.locator("#librenms-sync-tabs").get_attribute("data-active-tab") == "modules"
        assert page.locator('[aria-current="true"]').inner_text() == "Primary LibreNMS"
        browser.close()


def test_preferred_star_is_reversible_without_changing_the_active_server():
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

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
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
        browser.close()
