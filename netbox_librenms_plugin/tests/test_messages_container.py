"""Each plugin page and HTMX partial that shows Django messages carries one messages container."""

import pytest
from django.urls import reverse

from netbox_librenms_plugin.sync_cache import SyncTab
from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_superuser
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.test_ip_address_sync_safety import _refresh_ip_snapshot
from netbox_librenms_plugin.tests.test_vlan_sync_conflicts import _seed_vlan_snapshot

SERVER = "default"
IP_CACHE_MISS = "Cache has expired"
MODULE_CACHE_MISS = "No cached inventory data"
SERVER_GONE = "no longer configured"
NOTHING_SELECTED = "No devices selected"
HTMX = {"HTTP_HX_REQUEST": "true"}


def _mapped_device(live_librenms, name):
    device = make_device(name, librenms_cf={SERVER: {"id": 42}})
    live_librenms.server.register(
        "/api/v0/devices/42",
        {"status": "ok", "devices": [{"device_id": 42, "sysName": name, "hostname": name}]},
    )
    return device


def _sync_page(client, device, headers):
    # A plain POST queues the message and redirects to the page that shows it.
    client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_ip_addresses", args=["device", device.pk]),
        {"server_key": SERVER, "select": "198.18.77.1/24"},
    )
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])
    return client.get(url, {"server_key": SERVER, "tab": SyncTab.IP_ADDRESSES.value}, **headers)


def _sync_page_load(client, live_librenms):
    return _sync_page(client, _mapped_device(live_librenms, "messages-page"), {}), IP_CACHE_MISS


def _sync_tab_switch(client, live_librenms):
    """A tab link fetches the full page over HTMX."""
    return _sync_page(client, _mapped_device(live_librenms, "messages-tab-switch"), HTMX), IP_CACHE_MISS


def _vlan_tab_partial(client, live_librenms):
    device = _mapped_device(live_librenms, "messages-vlan-tab")
    url = reverse("plugins:netbox_librenms_plugin:device_vlan_sync", args=[device.pk])
    return client.post(url, {"server_key": "retired-server"}, **HTMX), SERVER_GONE


def _module_tab_partial(client, live_librenms):
    device = _mapped_device(live_librenms, "messages-module-tab")
    url = reverse("plugins:netbox_librenms_plugin:install_selected", args=[device.pk])
    return client.post(url, {"server_key": SERVER, "select": "9001"}, **HTMX), MODULE_CACHE_MISS


def _import_page_load(client, live_librenms):
    client.post(reverse("plugins:netbox_librenms_plugin:bulk_import_devices"), {"server_key": SERVER})
    return client.get(reverse("plugins:netbox_librenms_plugin:librenms_import")), NOTHING_SELECTED


def _import_partial(client, live_librenms):
    url = reverse("plugins:netbox_librenms_plugin:bulk_import_devices")
    return client.post(url, {"server_key": "retired-server", "select": "1"}, **HTMX), SERVER_GONE


def _vlan_conflicts(client, live_librenms, headers):
    from ipam.models import VLAN, VLANGroup

    device = _mapped_device(live_librenms, "messages-vlan-conflict")
    group = VLANGroup.objects.create(name="Messages group", slug="messages-group")
    VLAN.objects.create(vid=104, group=group, name="Current name", status="active")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 104, "vlan_name": "Proposed name"}], SERVER)
    url = reverse("plugins:netbox_librenms_plugin:sync_selected_vlans", args=["device", device.pk])
    data = {"server_key": SERVER, "action": "create_vlans", "select": "104", "vlan_group_104": str(group.pk)}
    response = client.post(url, data, **headers)
    assert b"Confirm VLAN changes" in response.content
    return response, None


def _ip_conflicts(client, live_librenms, headers):
    from ipam.models import IPAddress

    device = _mapped_device(live_librenms, "messages-ip-conflict")
    seed_mapping(make_interface(device, "Ethernet1", iface_type="1000base-t"), SERVER, own=7001)
    IPAddress.objects.create(address="198.18.2.20/24", assigned_object=make_interface(device, "Ethernet2"))
    assert _refresh_ip_snapshot(client, device, "198.18.2.20", 24, live_librenms).status_code == 200
    url = reverse("plugins:netbox_librenms_plugin:sync_device_ip_addresses", args=["device", device.pk])
    data = {"server_key": SERVER, "select": "198.18.2.20/24", "vrf_198.18.2.20/24": ""}
    response = client.post(url, data, **headers)
    assert b"Confirm IP address changes" in response.content
    return response, None


CASES = {
    "sync-page": _sync_page_load,
    "sync-page-htmx-tab-switch": _sync_tab_switch,
    "sync-vlan-tab-partial": _vlan_tab_partial,
    "sync-module-tab-partial": _module_tab_partial,
    "import-page": _import_page_load,
    "import-htmx-partial": _import_partial,
    "vlan-conflicts-page": lambda client, live: _vlan_conflicts(client, live, {}),
    "vlan-conflicts-htmx-modal": lambda client, live: _vlan_conflicts(client, live, HTMX),
    "ip-conflicts-page": lambda client, live: _ip_conflicts(client, live, {}),
    "ip-conflicts-htmx-modal": lambda client, live: _ip_conflicts(client, live, HTMX),
}


@pytest.mark.django_db
@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_a_response_carries_one_messages_container(client, live_librenms, case):
    """A full page relies on the container of base.html, and a partial carries one out-of-band container."""
    client.force_login(make_superuser("messages-once-user"))

    response, message = case(client, live_librenms)

    assert response.status_code == 200
    html = response.content.decode()
    assert html.count('id="django-messages"') == 1
    if message is not None:
        assert html.count('class="toast-body"') == 1
        assert message in html
