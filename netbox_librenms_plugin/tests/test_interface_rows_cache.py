"""
The interfaces tab reuses its computed rows until an input of them changes, and loads its NetBox-only list on demand.

Every test renders the real tab fragment through a real request from a seeded snapshot with its
fetch time, as a refresh leaves it. NetBox interfaces, MAC addresses, VLANs and rules are real
rows. A spy counts the row computations; the real computation always runs.
"""

import re
from dataclasses import replace

import pytest
from dcim.models import Interface, MACAddress
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from ipam.models import VLAN

from netbox_librenms_plugin.constants import OOB_INVENTORY_SOURCE
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.tests.test_interface_sync_filters import (
    SERVER_KEY,
    _chassis,
    _mixed_device,
    _port,
    _rows,
    _superuser_client,
    _tab,
)
from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

pytestmark = pytest.mark.django_db


@pytest.fixture
def computations(monkeypatch):
    """Count the row computations, and run each one."""
    calls = []
    compute = BaseInterfaceTableView._compute_interface_rows

    def counted(self, *args, **kwargs):
        calls.append(args[1].pk)
        return compute(self, *args, **kwargs)

    monkeypatch.setattr(BaseInterfaceTableView, "_compute_interface_rows", counted)
    return calls


def _refreshed(device):
    """Give the seeded snapshot the fetch time that a refresh writes with it."""
    cache.set(SyncInterfacesView().get_last_fetched_key(device, "ports", SERVER_KEY), timezone.now(), timeout=300)
    return device


@pytest.fixture
def tab(client, settings):
    """Return the device and a function that renders its tab for a query."""
    client = _superuser_client(client, settings, "rows-cache")
    device = _refreshed(_mixed_device("rows-cache"))
    return device, lambda **params: _rows(_tab(client, device, **{"interfaces_show_ignored": "1", **params}))


def test_a_filter_change_reuses_the_computed_rows(tab, computations):
    device, rows = tab

    assert set(rows(interfaces_q="eth")) == {1001, 1002, 1003, 1006, 1007}
    assert set(rows(interfaces_q="eth1")) == {1002}
    assert set(rows(interfaces_sync_state="mismatched")) == {1002}

    assert computations == [device.pk]


def test_a_netbox_interface_change_computes_the_rows_again(tab, computations):
    device, rows = tab
    assert set(rows(interfaces_sync_state="mismatched")) == {1002}

    interface = Interface.objects.get(device=device, name="eth1")
    interface.mtu = 1500
    interface.save()

    assert set(rows(interfaces_sync_state="mismatched")) == set()
    assert len(computations) == 2


def test_a_deleted_netbox_only_interface_computes_the_rows_again(tab, computations, client):
    device, rows = tab
    rows()

    Interface.objects.get(device=device, name="orphan0").delete()
    rows()

    assert len(computations) == 2


def test_a_mac_address_change_computes_the_rows_again(tab, computations):
    device, rows = tab
    rows()

    MACAddress.objects.create(mac_address="aa:bb:cc:00:00:01", assigned_object=Interface.objects.get(name="eth0"))
    rows()

    assert len(computations) == 2


def test_a_vlan_change_computes_the_rows_again(tab, computations):
    _, rows = tab
    rows()

    VLAN.objects.create(vid=100, name="vlan-100")
    rows()

    assert len(computations) == 2


def test_a_new_rule_computes_the_rows_again(tab, computations):
    _, rows = tab
    assert 1001 in rows(interfaces_show_ignored="0")

    InterfaceTypeMapping.objects.create(
        action=InterfaceTypeMapping.ACTION_IGNORE, name_pattern="^eth0$", librenms_type=""
    )

    shown = rows(interfaces_show_ignored="0")
    assert 1001 not in shown and 1002 in shown
    assert len(computations) == 2


def test_a_refresh_computes_the_rows_again(tab, computations):
    device, rows = tab
    rows()

    _refreshed(device)
    rows()

    assert len(computations) == 2


def test_another_netbox_release_does_not_read_the_rows(tab, computations, settings):
    _, rows = tab
    rows()

    # An upgrade can change the cached model classes, so rows another release wrote are never unpickled.
    settings.RELEASE = replace(settings.RELEASE, version="99.0.0")
    rows()

    assert len(computations) == 2


def test_another_viewer_does_not_reuse_the_rows(tab, computations, client):
    _, rows = tab
    rows()

    # make_superuser returns the first superuser, so the second viewer is created here.
    client.force_login(get_user_model().objects.create(username="rows-cache-second", is_superuser=True))
    rows()

    assert len(computations) == 2


def test_each_chassis_member_page_computes_its_own_rows(client, settings, computations):
    client = _superuser_client(client, settings, "rows-cache-vc")
    first, second = _chassis("rows-cache-vc")
    key = SyncInterfacesView().get_cache_key(first, "ports", SERVER_KEY)
    snapshot = cache.get(key)
    snapshot["ports"].append(_port(2009, "iDRAC-NIC", _source=OOB_INVENTORY_SOURCE))
    cache.set(key, snapshot, timeout=300)
    _refreshed(first)

    first_rows = _rows(_tab(client, first))
    second_rows = _rows(_tab(client, second))

    # An OOB row belongs to the member whose page shows it.
    assert f'<option value="{first.pk}" selected>' in first_rows[2009]
    assert f'<option value="{second.pk}" selected>' in second_rows[2009]
    assert computations == [first.pk, second.pk]


def _netbox_only(client, device):
    url = reverse(
        "plugins:netbox_librenms_plugin:netbox_only_interfaces", kwargs={"object_type": "device", "pk": device.pk}
    )
    return client.get(url, {"server_key": SERVER_KEY, "interface_name_field": "ifName"})


def test_the_tab_leaves_the_netbox_only_rows_to_the_dialog(client, settings):
    client = _superuser_client(client, settings, "netbox-only")
    device = _refreshed(_mixed_device("netbox-only"))

    html = _tab(client, device)
    response = _netbox_only(client, device)

    # The tab names the count and loads the rows when the dialog opens; the rows come from their own request.
    assert 'id="netbox-only-interfaces-rows"' in html and "netbox-interface-checkbox" not in html
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    names = re.findall(r'aria-label="Select interface ([^"]+)"', response.content.decode())
    assert "orphan0" in names
    assert f"{len(names)} NetBox only interfaces" in " ".join(html.split())


def test_the_netbox_only_rows_need_a_view_of_the_device(client, settings):
    _superuser_client(client, settings, "netbox-only-denied")
    device = _refreshed(_mixed_device("netbox-only-denied"))
    client.force_login(get_user_model().objects.create(username="netbox-only-no-grant"))

    response = _netbox_only(client, device)

    assert response.status_code in (403, 404)
    assert "orphan0" not in response.content.decode()
