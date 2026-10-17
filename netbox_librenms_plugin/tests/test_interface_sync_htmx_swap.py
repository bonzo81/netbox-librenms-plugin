"""The interface sync and Rebind POSTs answer an htmx submit with the tab fragment, not a redirect."""

import json
import re

import pytest
from dcim.models import Interface
from django.core.cache import cache
from django.urls import reverse
from virtualization.models import VMInterface

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

SERVER_KEY = "default"
FIRST_PORT = 5000
PORT_COUNT = 30


def _port(port_id, name):
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifType": "ethernetCsmacd",
        "ifAdminStatus": "up",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": "",
        "ifAlias": "",
    }


def _ports():
    # Zero-padded names keep the table's name order equal to the port order.
    return [_port(FIRST_PORT + index, f"eth{index:02d}") for index in range(PORT_COUNT)]


def _seed(owner, ports):
    payload = {"ports": ports, "port_stack_relationships": {}}
    cache.set(SyncInterfacesView().get_cache_key(owner, "ports", SERVER_KEY), payload, timeout=300)


def _url(name, owner, object_type):
    return (
        reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs={"object_type": object_type, "object_id": owner.pk})
        + "?interface_name_field=ifName"
    )


def _post(client, owner, data, *, name="sync_selected_interfaces", object_type="device", htmx=True):
    headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
    return client.post(_url(name, owner, object_type), {"server_key": SERVER_KEY, **data}, **headers)


def _sync_data(*port_ids, page=None, per_page=None):
    data = {
        "select": [str(port_id) for port_id in port_ids],
        "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
    }
    if page is not None:
        data["interfaces_page"] = str(page)
    if per_page is not None:
        data["interfaces_per_page"] = str(per_page)
    return data


def _row_names(html):
    return re.findall(r'<tr[^>]*data-name="([^"]+)"', html)


def _hidden_value(html, name):
    match = re.search(rf'<input type="hidden" name="{name}" value="([^"]*)">', html)
    return match.group(1) if match else None


@pytest.mark.django_db
class TestHtmxSync:
    """An htmx sync swaps the tab in place at the page and page size the user was on."""

    @pytest.fixture(autouse=True)
    def _server(self, settings):
        configure_default_librenms_server(settings)

    def test_the_fragment_keeps_the_page_and_the_page_size(self, client):
        device = make_device("htmx-sync-page", librenms_cf={SERVER_KEY: {"id": 81}})
        _seed(device, _ports())
        client.force_login(make_superuser("htmx-sync-page-user"))
        synced_port = FIRST_PORT + 12

        response = _post(client, device, _sync_data(synced_port, page=2, per_page=10))

        assert response.status_code == 200
        assert "Location" not in response
        html = response.content.decode()
        assert "Showing 11-20 of 30" in html
        assert _row_names(html) == [f"eth{index:02d}" for index in range(10, 20)]
        assert (_hidden_value(html, "interfaces_page"), _hidden_value(html, "interfaces_per_page")) == ("2", "10")
        # The swapped form posts over htmx again, so the next sync also stays on this page.
        assert f'hx-post="{_url("sync_selected_interfaces", device, "device")}"' in html
        assert "Selected interfaces synced successfully." in html
        interface = Interface.objects.get(device=device, name="eth12")
        assert get_librenms_device_id(interface, SERVER_KEY, auto_save=False) == synced_port

    def test_the_cache_transition_rides_on_the_fragment(self, client, django_capture_on_commit_callbacks):
        device = make_device("htmx-sync-transition", librenms_cf={SERVER_KEY: {"id": 82}})
        _seed(device, _ports())
        client.force_login(make_superuser("htmx-sync-transition-user"))

        with django_capture_on_commit_callbacks(execute=True):
            response = _post(client, device, _sync_data(FIRST_PORT))

        transition = json.loads(response["X-LibreNMS-Cache-Transition"])
        assert transition["source_tab"] == "interfaces"
        assert json.loads(response["HX-Trigger"])["librenmsCacheChanged"] == transition

    def test_a_rejected_submit_reports_in_the_fragment(self, client):
        device = make_device("htmx-sync-empty", librenms_cf={SERVER_KEY: {"id": 83}})
        _seed(device, _ports())
        client.force_login(make_superuser("htmx-sync-empty-user"))

        response = _post(client, device, _sync_data(page=3, per_page=10))

        assert response.status_code == 200
        html = response.content.decode()
        assert "No interfaces selected for synchronization." in html
        assert "Showing 21-30 of 30" in html
        assert not Interface.objects.filter(device=device).exists()

    def test_an_unknown_server_renders_an_empty_tab(self, client):
        device = make_device("htmx-sync-server", librenms_cf={SERVER_KEY: {"id": 84}})
        _seed(device, _ports())
        client.force_login(make_superuser("htmx-sync-server-user"))

        response = _post(client, device, {**_sync_data(FIRST_PORT), "server_key": "gone"})

        assert response.status_code == 200
        html = response.content.decode()
        assert "Selected LibreNMS server is no longer configured." in html
        assert "No interface data loaded." in html
        assert not Interface.objects.filter(device=device).exists()

    def test_a_virtual_machine_gets_its_own_fragment(self, client):
        vm = make_vm("htmx-sync-vm")
        _seed(vm, _ports())
        client.force_login(make_superuser("htmx-sync-vm-user"))

        response = _post(client, vm, _sync_data(FIRST_PORT + 1, page=1, per_page=10), object_type="virtualmachine")

        assert response.status_code == 200
        html = response.content.decode()
        assert 'id="librenms-interface-table-vm"' in html
        assert "Selected interfaces synced successfully." in html
        assert VMInterface.objects.filter(virtual_machine=vm, name="eth01").exists()

    def test_a_plain_submit_still_redirects(self, client):
        device = make_device("htmx-sync-plain", librenms_cf={SERVER_KEY: {"id": 85}})
        _seed(device, _ports())
        client.force_login(make_superuser("htmx-sync-plain-user"))

        response = _post(client, device, _sync_data(FIRST_PORT), htmx=False)

        assert response.status_code == 302
        assert "tab=interfaces" in response["Location"]


@pytest.mark.django_db
def test_an_htmx_rebind_swaps_the_tab_in_place(client, settings, django_capture_on_commit_callbacks):
    configure_default_librenms_server(settings)
    device = make_device("htmx-rebind", librenms_cf={SERVER_KEY: {"id": 86}})
    interface = make_interface(device, "eth12")
    set_librenms_device_id(interface, 8679, SERVER_KEY)
    interface.save()
    _seed(device, _ports())
    client.force_login(make_superuser("htmx-rebind-user"))
    port_id = FIRST_PORT + 12

    with django_capture_on_commit_callbacks(execute=True):
        response = _post(
            client,
            device,
            {
                "rebind_one": str(port_id),
                f"rebind_expected_port_{port_id}": "8679",
                "interfaces_page": "2",
                "interfaces_per_page": "10",
            },
            name="rebind_interface_port",
        )

    assert response.status_code == 200
    html = response.content.decode()
    assert "Showing 11-20 of 30" in html
    assert f"is now bound to LibreNMS port {port_id}" in html
    assert 'name="rebind_one"' not in html
    interface.refresh_from_db()
    assert get_librenms_device_id(interface, SERVER_KEY, auto_save=False) == port_id
    assert json.loads(response["HX-Trigger"])["librenmsCacheChanged"]["source_tab"] == "interfaces"
