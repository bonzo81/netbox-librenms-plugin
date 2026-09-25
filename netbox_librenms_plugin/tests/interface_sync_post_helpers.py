"""Seed the interface snapshot and post the interface sync as the browser does, for end-to-end sync tests."""

import json
from types import SimpleNamespace

from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.middleware import REQUEST_FAILED_EVENT
from netbox_librenms_plugin.tests.conftest import make_interface
from netbox_librenms_plugin.tests.view_test_helpers import messages_on
from netbox_librenms_plugin.utils import set_librenms_device_id
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

SERVER_KEY = "default"
SYNCED = "Selected interfaces synced successfully."


def sync_port(port_id, name, *, alias="", mac="", if_type="ethernetCsmacd", **extra):
    """Return one cached LibreNMS port row; *extra* adds keys such as the VLAN fields."""
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifType": if_type,
        "ifAdminStatus": "up",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": mac,
        "ifAlias": alias,
        **extra,
    }


def seed_ports(device, ports, *, lag_members=None, sub_interfaces=None, bridge_members=None):
    """Put *ports* in the interface snapshot of *device*, as a refresh of the tab does."""
    relationships = {
        "lag_members": lag_members or {},
        "sub_interfaces": sub_interfaces or {},
        "bridge_members": bridge_members or {},
    }
    payload = {"ports": ports, "port_stack_relationships": relationships}
    cache.set(SyncInterfacesView().get_cache_key(device, "ports", SERVER_KEY), payload, timeout=300)


def bound_interface(device, name, port_id, *, iface_type="other"):
    """Create an interface on *device* that is bound to LibreNMS port *port_id*."""
    interface = make_interface(device, name, iface_type=iface_type)
    set_librenms_device_id(interface, port_id, SERVER_KEY)
    interface.save()
    return interface


def synced_interface(device, name, port_id, **fields):
    """Return an interface that a sync of ``sync_port(port_id, name)`` leaves unchanged, with *fields* set."""
    from dcim.models import Interface

    interface = bound_interface(device, name, port_id)
    Interface.objects.filter(pk=interface.pk).update(speed=1_000_000, mtu=1500, enabled=True, **fields)
    interface.refresh_from_db()
    return interface


def sync_page(device):
    """Return the absolute URL of the interfaces tab of *device*."""
    return "http://testserver" + reverse("dcim:device_librenms_sync", kwargs={"pk": device.pk}) + "?tab=interfaces"


def post_interface_sync(client, device, port_ids, *, htmx, exclude_columns=("vlans", "mac_address")):
    """Post the interface sync form of *device* for *port_ids*."""
    url = (
        reverse(
            "plugins:netbox_librenms_plugin:sync_selected_interfaces",
            kwargs={"object_type": "device", "object_id": device.pk},
        )
        + "?interface_name_field=ifName"
    )
    headers = {"HTTP_REFERER": sync_page(device)}
    if htmx:
        headers["HTTP_HX_REQUEST"] = "true"
    data = {
        "server_key": SERVER_KEY,
        "select": [str(port_id) for port_id in port_ids],
        "exclude_columns": list(exclude_columns),
    }
    return client.post(url, data, **headers)


def count_sync_attempts(monkeypatch):
    """
    Count sync attempts at the owner lock, which each attempt takes once.

    Returns:
        SimpleNamespace: ``count`` is the number of attempts. Set ``before_retry`` to a callable
            that runs when the second attempt starts.

    """
    real_lock = SyncInterfacesView._lock_selected_device_targets
    state = SimpleNamespace(count=0, before_retry=None)

    def counting_lock(self, obj):
        state.count += 1
        if state.count == 2 and state.before_retry is not None:
            state.before_retry()
        return real_lock(self, obj)

    monkeypatch.setattr(SyncInterfacesView, "_lock_selected_device_targets", counting_lock)
    return state


def assert_try_again_answer(response, device, htmx, message):
    """Assert the one visible answer the middleware gives for a lock conflict."""
    if htmx:
        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert json.loads(response["HX-Trigger"]) == {REQUEST_FAILED_EVENT: None}
        assert message in response.content.decode()
        assert messages_on(response.wsgi_request) == []
    else:
        assert response.status_code == 302
        assert response["Location"] == sync_page(device)
        assert messages_on(response.wsgi_request) == [("error", message)]
