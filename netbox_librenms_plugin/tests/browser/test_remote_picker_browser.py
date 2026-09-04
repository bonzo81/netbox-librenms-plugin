"""Browser-level checks for remote cable endpoint selection."""

from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

HTMX_PATH = Path(__file__).parent / "vendor" / "htmx.min.js"
HTMX_SCRIPT = f"window.htmxTest = (function () {{\n{HTMX_PATH.read_text()}\n; return htmx; }})();"
PICKER_URL = "https://plugin.example.com/remote-picker/"
SEARCH_URL = f"{PICKER_URL}?action=search"
PORTS_URL = f"{PICKER_URL}?action=ports"


def _render_template(template_name, context):
    """Render a production picker template with its real includes."""
    from django.conf import settings
    from django.template import Context, Engine

    # The browser suite runs without DJANGO_SETTINGS_MODULE, and rendering an integer pk
    # reaches Django's number localization, which reads settings.
    if not settings.configured:
        settings.configure(USE_I18N=False, USE_TZ=True)
    template_root = Path(__file__).parents[2] / "templates"
    template = Engine(dirs=[template_root]).get_template(template_name)
    return template.render(Context(context))


def _device(pk, name):
    return SimpleNamespace(pk=pk, name=name, site=None)


def _render_picker(devices):
    row = SimpleNamespace(
        row_id="main:link:1",
        local_port="Ethernet 1",
        remote_device="old-device",
        remote_port="Ethernet 9",
    )
    return _render_template(
        "netbox_librenms_plugin/htmx/cable_remote_picker_modal.html",
        {
            "csrf_token": "test-csrf",
            "row": row,
            "object": SimpleNamespace(pk=1),
            "server_key": "primary",
            "post_url": PICKER_URL,
            "search_url": SEARCH_URL,
            "ports_url": PORTS_URL,
            "initial_devices": devices,
        },
    )


def _render_ports(device, port_pk):
    return _render_template(
        "netbox_librenms_plugin/htmx/_remote_picker_ports.html",
        {
            "device": device,
            "free_ports": [SimpleNamespace(pk=port_pk, name="Ethernet 1")],
            "cabled_ports": [],
            "port_noun": "interfaces",
        },
    )


def _load_picker(page, devices):
    page.set_content(_render_picker(devices))
    page.add_script_tag(content=HTMX_SCRIPT)


def test_device_search_clears_the_selected_port_before_the_response(page):
    """A pending search must remove the old port and disable Save."""
    device_a = _device(1, "Device A")
    search_routes = []

    page.route(
        f"{PORTS_URL}*",
        lambda route: route.fulfill(body=_render_ports(device_a, 101), content_type="text/html"),
    )
    page.route(f"{SEARCH_URL}*", lambda route: search_routes.append(route))
    _load_picker(page, [device_a])

    with page.expect_request(f"{PORTS_URL}*"):
        page.get_by_role("button", name="Device A").click()
    page.locator("#remote-picker-port-select").select_option("101")
    assert not page.locator("#remote-picker-save").is_disabled()

    with page.expect_request(f"{SEARCH_URL}*"):
        page.locator("#remote-picker-search").fill("Device B")

    # The search route is held, so its response cannot have cleared anything yet.
    page.wait_for_function("document.getElementById('remote-picker-ports').children.length === 0", timeout=2_000)
    assert page.locator("#remote-picker-save").is_disabled()
    for route in search_routes:
        route.abort()


def test_new_search_rejects_a_late_port_response(page):
    """A late port response must not restore the old device selection."""
    device_a = _device(1, "Device A")
    old_port_routes = []
    search_routes = []

    page.route(f"{PORTS_URL}*", lambda route: old_port_routes.append(route))
    page.route(f"{SEARCH_URL}*", lambda route: search_routes.append(route))
    _load_picker(page, [device_a])

    with page.expect_request(f"{PORTS_URL}*"):
        page.get_by_role("button", name="Device A").click()
    assert old_port_routes

    try:
        with page.expect_event(
            "requestfailed",
            lambda request: request.url.startswith(PORTS_URL),
            timeout=2_000,
        ):
            page.locator("#remote-picker-search").fill("Device B")
        old_request_aborted = True
    except PlaywrightTimeoutError:
        old_request_aborted = False

    if not old_request_aborted:
        old_port_routes[0].fulfill(body=_render_ports(device_a, 101), content_type="text/html")
        page.locator("#remote-picker-port-select").wait_for()

    assert old_request_aborted
    assert page.locator("#remote-picker-port-select").count() == 0
    assert page.locator("#remote-picker-save").is_disabled()
    for route in search_routes:
        route.abort()


def test_last_device_click_wins_when_port_responses_arrive_out_of_order(page):
    """A late first response must not replace the second device ports."""
    device_a = _device(1, "Device A")
    device_b = _device(2, "Device B")
    port_routes = {}

    def hold_port_route(route):
        device_id = route.request.url.rsplit("device_id=", 1)[1]
        port_routes[device_id] = route

    page.route(f"{PORTS_URL}*", hold_port_route)
    _load_picker(page, [device_a, device_b])

    with page.expect_request(f"{PORTS_URL}*&device_id=1"):
        page.get_by_role("button", name="Device A").click()
    assert "1" in port_routes

    try:
        with page.expect_event(
            "requestfailed",
            lambda request: request.url.endswith("device_id=1"),
            timeout=2_000,
        ):
            page.get_by_role("button", name="Device B").click()
        first_request_aborted = True
    except PlaywrightTimeoutError:
        first_request_aborted = False

    assert "2" in port_routes
    port_routes["2"].fulfill(body=_render_ports(device_b, 202), content_type="text/html")
    page.locator("#remote-picker-ports strong").wait_for()
    if not first_request_aborted:
        port_routes["1"].fulfill(body=_render_ports(device_a, 101), content_type="text/html")
        page.locator("#remote-picker-ports strong", has_text="Device A").wait_for()

    assert first_request_aborted
    assert page.locator("#remote-picker-ports strong").inner_text() == "Device B"
    assert page.locator("#remote-picker-save").is_disabled()
