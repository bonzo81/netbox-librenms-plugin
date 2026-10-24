"""Browser checks that the sync page scripts post the server key of the page, not of another form."""

import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest


SCRIPT_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"
PAGE_URL = "https://plugin.example.com/dcim/devices/1/librenms-sync/"
CABLE_VERIFY_URL = "https://plugin.example.com/verify-cable/"
LAG_SYNC_URL = "https://plugin.example.com/sync-lag/"

# The server panel renders a "make preferred" form for each other server before the sync tabs.
PAGE_HTML = f"""<!doctype html><html><body>
    <form method="post" action="/set-preferred/">
      <input type="hidden" name="server_key" value="primary">
    </form>
    <input type="radio" name="interface_name_field" value="ifName" checked>
    <div id="librenms-sync-tabs" data-active-tab="interfaces" data-server-key="secondary">
      <form id="sync-form" method="post">
        <input type="hidden" name="csrfmiddlewaretoken" value="test-token">
        <input type="hidden" name="server_key" value="secondary">
        <table id="librenms-interface-table"><tbody>
          <tr data-port-id="10">
            <td><select class="vc-member-select" data-interface="et-0/0/0">
              <option value="1" selected>m1</option><option value="2">m2</option></select></td>
            <td><button class="lag-sync-btn" data-port-id="10" data-lag-port-id="20"
                        data-sync-url="{LAG_SYNC_URL}" data-object-id="1">LAG</button></td>
          </tr>
        </tbody></table>
      </form>
      <div data-cable-verify-url="{CABLE_VERIFY_URL}" data-cable-origin-device-id="1">
        <table><tbody><tr><td><select id="cable-select" data-interface="row-1">
          <option value="1" selected>m1</option><option value="2">m2</option></select></td></tr></tbody></table>
      </div>
      <table><tbody><tr data-ent-index="5" data-depth="0"><td>
        <select id="module-select" data-row-id="5"><option value="1">m1</option></select>
      </td></tr></tbody></table>
      <select id="vrf-select" data-ip="192.0.2.1" data-prefix="24" data-row-id="ip-1"><option value="3">v</option></select>
      <button id="confirm-delete-interfaces">Delete</button>
    </div>
    </body></html>"""


def _load(page):
    """Serve the two-server sync page and answer every other request with an empty JSON body."""
    page.route("**/*", lambda route: route.fulfill(status=200, content_type="application/json", body="{}"))
    page.route(PAGE_URL, lambda route: route.fulfill(status=200, content_type="text/html", body=PAGE_HTML))
    page.goto(PAGE_URL)
    page.add_script_tag(path=str(SCRIPT_PATH))


def _json_server_key(request):
    return json.loads(request.post_data)["server_key"]


def _form_server_key(request):
    return parse_qs(request.post_data)["server_key"][0]


@pytest.mark.parametrize(
    ("url", "action", "read"),
    [
        (
            "**/verify-interface/",
            "handleInterfaceChange(document.querySelector('.vc-member-select'), '2')",
            _json_server_key,
        ),
        (CABLE_VERIFY_URL, "handleCableChange(document.getElementById('cable-select'), '2')", _json_server_key),
        ("**/verify-module/", "handleModuleChange(document.getElementById('module-select'), '1')", _json_server_key),
        ("**/verify-ipaddress/", "handleVRFChange(document.getElementById('vrf-select'), '3')", _json_server_key),
        (LAG_SYNC_URL, "document.querySelector('.lag-sync-btn').click()", _form_server_key),
    ],
    ids=["interface-verify", "cable-verify", "module-verify", "ip-vrf-verify", "relationship-sync"],
)
def test_a_request_posts_the_server_key_of_the_sync_page(page, url, action, read):
    """Another server's form earlier in the page does not decide the server of a tab request."""
    _load(page)
    with page.expect_request(lambda request: request.method == "POST" and _matches(request.url, url)) as info:
        page.evaluate(action)
    assert read(info.value) == "secondary"


def test_the_netbox_only_delete_posts_the_server_key_of_the_sync_page(page):
    _load(page)
    with page.expect_request("**/delete-netbox-interfaces/") as info:
        page.evaluate("deleteSelectedInterfaces([{value: '7'}])")
    assert "secondary" in (info.value.post_data or "")
    assert "primary" not in (info.value.post_data or "")


def test_the_selection_store_is_scoped_to_the_server_of_the_sync_page(page):
    _load(page)
    key = page.evaluate("_selectionStorageKey(document.getElementById('librenms-interface-table'))")
    assert key.endswith(":secondary")


def _matches(request_url, pattern):
    return request_url.endswith(pattern.removeprefix("**")) if pattern.startswith("**") else request_url == pattern
