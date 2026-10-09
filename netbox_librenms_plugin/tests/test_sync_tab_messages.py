"""Each Django message renders once per sync-tab response."""

import re

import pytest
from django.urls import reverse

from netbox_librenms_plugin.sync_cache import SyncTab
from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_device, make_superuser

IP_CACHE_MISS = "Cache has expired. Please refresh the IP data."
MODULE_CACHE_MISS = "No cached inventory data. Please refresh modules first."
SERVER_GONE = "Selected LibreNMS server is no longer configured."


@pytest.fixture
def librenms(settings, monkeypatch):
    """Serve the device lookups of the sync page from a loopback LibreNMS."""
    from netbox_librenms_plugin.tests.mock_librenms_server import MockLibreNMSServer

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = MockLibreNMSServer().start()
    configure_librenms_servers(settings, {"primary": {"librenms_url": server.url, "api_token": "test-token"}})
    try:
        yield server
    finally:
        server.stop()


def _mapped_device(server, name, librenms_id):
    device = make_device(name, librenms_cf={"primary": {"id": librenms_id}})
    server.register(
        f"/api/v0/devices/{librenms_id}",
        {"status": "ok", "devices": [{"device_id": librenms_id, "sysName": name, "hostname": name}]},
    )
    return device


def _ip_sync_url(device):
    return reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )


def _assert_rendered_once(response, text):
    html = response.content.decode()
    toasts = re.findall(r'class="toast-body">\s*' + re.escape(text), html)
    assert len(toasts) == 1, f"{text!r} rendered in {len(toasts)} toasts"
    assert html.count('id="django-messages"') == 1


@pytest.mark.django_db
@pytest.mark.parametrize("htmx", [False, True], ids=["page-load", "htmx-tab-switch"])
def test_the_sync_page_renders_a_queued_message_once(client, librenms, htmx):
    """The full page, also when a tab link fetches it over HTMX, carries one messages container."""
    device = _mapped_device(librenms, "messages-once-page", 7701)
    client.force_login(make_superuser("messages-once-page-user"))
    # A plain POST queues the message and redirects to the page that shows it.
    client.post(_ip_sync_url(device), {"server_key": "primary", "select": "198.18.77.1/24"})

    headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"server_key": "primary", "tab": SyncTab.IP_ADDRESSES.value},
        **headers,
    )

    assert response.status_code == 200
    _assert_rendered_once(response, IP_CACHE_MISS)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url_name", "post_data", "message"),
    [
        ("device_vlan_sync", {"server_key": "retired-server"}, SERVER_GONE),
        ("install_selected", {"server_key": "primary", "select": "9001"}, MODULE_CACHE_MISS),
    ],
    ids=["vlans", "modules"],
)
def test_an_htmx_tab_partial_renders_its_message_once(client, librenms, url_name, post_data, message):
    """A tab partial swapped in by HTMX still carries its message, once, in the out-of-band container."""
    device = _mapped_device(librenms, f"messages-once-{url_name}", 7702)
    client.force_login(make_superuser(f"messages-once-{url_name}-user"))

    response = client.post(
        reverse(f"plugins:netbox_librenms_plugin:{url_name}", kwargs={"pk": device.pk}),
        post_data,
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert "HX-Redirect" not in response
    _assert_rendered_once(response, message)
    assert 'id="django-messages" class="toast-container position-fixed bottom-0 end-0 p-3" hx-swap-oob="true"' in (
        response.content.decode()
    )
