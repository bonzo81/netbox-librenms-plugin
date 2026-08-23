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
