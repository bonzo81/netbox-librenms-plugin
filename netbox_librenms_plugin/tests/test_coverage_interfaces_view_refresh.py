"""
Refresh-path coverage for ``views/base/interfaces_view.py``.

The module's primary home is ``test_coverage_sync_interfaces.py`` (sync actions) and
``test_payload_hardening.py`` (cached render guards). This file covers the POST refresh
branches instead: the stale server key, the fail-closed lookup/fetch exits, and the OOB
controller merge. Every test drives the real view with a real request against the in-repo
loopback LibreNMS, so the real ``LibreNMSAPI`` performs the HTTP calls.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    make_device,
    make_interface,
    make_ip,
    make_vm,
)
from netbox_librenms_plugin.tests.view_test_helpers import (
    make_request,
    make_superuser,
    message_texts,
    post as _post,
)


pytestmark = pytest.mark.django_db

SERVER_KEY = "alpha"


def _bind_server(settings, server):
    """Point the plugin at the loopback LibreNMS and return a client bound to it."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    configure_librenms_servers(settings, {SERVER_KEY: {"librenms_url": server.url, "api_token": "test-token"}})
    return LibreNMSAPI(server_key=SERVER_KEY)


def _map_device(device, librenms_id, *, oob=None):
    """Persist the device's LibreNMS mapping, optionally with an OOB controller sub-entry."""
    entry = {"id": librenms_id}
    if oob is not None:
        entry["oob"] = oob
    device.custom_field_data["librenms_id"] = {SERVER_KEY: entry}
    device.save(update_fields=["custom_field_data"])
    return device


def _refresh(view_class, device, server, settings, post_data=None):
    """Run the real interface refresh POST and return ``(response, view, request)``."""
    view = view_class()
    view._librenms_api = _bind_server(settings, server)
    request = make_request("post", {"server_key": SERVER_KEY, **(post_data or {})}, user=make_superuser())
    response = _post(view, request, pk=device.pk)
    return response, view, request


def _cached_ports(view, device):
    """Read back the ports snapshot the refresh cached for *device*."""
    from django.core.cache import cache

    return cache.get(view.get_cache_key(device, "ports", SERVER_KEY))


def _tab_state(device):
    """Read the interfaces tab's sync-cache state record."""
    from django.core.cache import cache

    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab

    coordinator = SyncCacheConsistency(device)
    return cache.get(coordinator.state_key(SyncTab.INTERFACES, SERVER_KEY))


@pytest.mark.django_db
class TestInterfaceViewIpAddress:
    def test_primary_ip_is_reported_and_a_device_without_one_reports_none(self):
        """get_ip_address returns the bare primary IP, and None when the device has none."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        from dcim.models import Device

        with_ip = make_device("iface-ip-present")
        without_ip = make_device("iface-ip-absent")
        interface = make_interface(with_ip, "eth0")
        address = make_ip("10.42.0.7/24", assigned_object=interface)
        with_ip.primary_ip4 = address
        with_ip.save()
        # Re-read so primary_ip.address is the stored IPNetwork, as it is on a real request.
        with_ip = Device.objects.get(pk=with_ip.pk)

        view = DeviceInterfaceTableView()

        assert view.get_ip_address(with_ip) == "10.42.0.7"
        assert view.get_ip_address(without_ip) is None


@pytest.mark.django_db
class TestInterfaceViewSelectRelatedField:
    def test_vm_interface_index_joins_the_virtual_machine_owner(self):
        """The VM view must select_related virtual_machine; the device field would be a FieldError."""
        from netbox_librenms_plugin.views.object_sync.vms import VMInterfaceTableView

        from virtualization.models import VMInterface

        vm = make_vm("iface-vm-owner")
        VMInterface.objects.create(virtual_machine=vm, name="eth0")

        view = VMInterfaceTableView()

        assert view.get_select_related_field(vm) == "virtual_machine"
        maps = view._build_interface_lookup_maps(vm)
        assert set(maps["by_name"]) == {"eth0"}


@pytest.mark.django_db
class TestInterfaceRefreshStaleServerKey:
    def test_unconfigured_posted_key_renders_the_error_partial(self, librenms_server, settings):
        """A POSTed key that no longer resolves errors and re-renders the partial, not a redirect."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = _map_device(make_device("iface-stale-key"), 5)
        view = DeviceInterfaceTableView()
        view._librenms_api = _bind_server(settings, librenms_server)
        request = make_request("post", {"server_key": "ghost"}, user=make_superuser())

        response = _post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert "Location" not in response
        assert message_texts(request, "error") == ["Selected LibreNMS server is no longer configured."]
        # The partial is rendered under the still-bound client's key, not the rejected one.
        assert view.active_server_key == SERVER_KEY
        assert librenms_server.requests == []


@pytest.mark.django_db
class TestInterfaceRefreshFailClosedExits:
    def test_unmapped_device_redirects_and_records_a_failed_refresh(self, librenms_server, settings):
        """No LibreNMS mapping: error, refresh_failed state, and a server_key-preserving redirect."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("iface-unmapped")

        response, _view, request = _refresh(DeviceInterfaceTableView, device, librenms_server, settings)

        assert response.status_code == 302
        assert response["Location"] == (
            f"/plugins/librenms_plugin/devices/{device.pk}/interface-sync/?server_key={SERVER_KEY}"
        )
        assert message_texts(request, "error") == ["Device not found in LibreNMS."]
        assert _tab_state(device)["state"] == "refresh_failed"

    def test_ports_fetch_failure_surfaces_the_librenms_error(self, librenms_server, settings):
        """A 404 on the ports endpoint redirects with the client's error text."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = _map_device(make_device("iface-ports-404"), 11)

        response, _view, request = _refresh(DeviceInterfaceTableView, device, librenms_server, settings)

        assert response.status_code == 302
        assert message_texts(request, "error") == ["Device not found in LibreNMS"]
        assert _tab_state(device)["state"] == "refresh_failed"

    def test_malformed_ports_payload_fails_closed(self, librenms_server, settings):
        """A 200 whose "ports" is not a list of dicts must redirect, not 500 on enrichment."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = _map_device(make_device("iface-ports-malformed"), 12)
        librenms_server.register("/api/v0/devices/12/ports", {"status": "ok", "ports": "not-a-list"})

        response, view, request = _refresh(DeviceInterfaceTableView, device, librenms_server, settings)

        assert response.status_code == 302
        assert message_texts(request, "error") == ["Unexpected response from LibreNMS (malformed ports payload)."]
        assert _cached_ports(view, device) is None


@pytest.mark.django_db
class TestInterfaceRefreshOobMerge:
    def test_corrupt_oob_id_warns_and_tags_the_snapshot_incomplete(self, librenms_server, settings):
        """A linked OOB controller with an uncoercible id must not be silently skipped."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = _map_device(make_device("iface-oob-corrupt"), 21, oob={"id": "not-an-id", "type": "idrac"})
        librenms_server.ports_response(device_id=21, ports=[{"port_id": 1, "ifName": "eth0"}])

        _response, view, request = _refresh(DeviceInterfaceTableView, device, librenms_server, settings)

        assert message_texts(request, "warning") == [
            "Interfaces refreshed, but OOB controller ports fetch failed; "
            "showing host interfaces only. See server logs for details."
        ]
        assert message_texts(request, "success") == ["Host interface data refreshed successfully."]
        snapshot = _cached_ports(view, device)
        assert snapshot["oob_incomplete"] is True
        # The corrupt id must never be turned into a request path.
        assert [r["path"] for r in librenms_server.requests] == ["/api/v0/devices/21/ports"]

    def test_oob_ports_merge_and_only_real_shared_macs_are_flagged(self, librenms_server, settings):
        """OOB rows merge in tagged _source=oob, and only a real MAC on both sides is a conflict."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = _map_device(make_device("iface-oob-merge"), 31, oob={"id": 32, "type": "idrac"})
        librenms_server.ports_response(
            device_id=31,
            ports=[
                # Shared with the OOB side once the formatting is normalized.
                {"port_id": 1, "ifName": "eth0", "ifPhysAddress": "AA-BB-CC-DD-EE-01"},
                # A placeholder MAC present on both sides must not be a conflict.
                {"port_id": 2, "ifName": "eth1", "ifPhysAddress": "00:00:00:00:00:00"},
                # A null MAC must be treated as absent, not crash the refresh.
                {"port_id": 3, "ifName": "eth2", "ifPhysAddress": None},
                # Too short to be a MAC.
                {"port_id": 4, "ifName": "eth3", "ifPhysAddress": "aa:bb"},
            ],
        )
        librenms_server.ports_response(
            device_id=32,
            ports=[
                {"port_id": 91, "ifName": "idrac0", "ifPhysAddress": "aa:bb:cc:dd:ee:01"},
                {"port_id": 92, "ifName": "idrac1", "ifPhysAddress": "00-00-00-00-00-00"},
                {"port_id": 93, "ifName": "idrac2", "ifPhysAddress": "aa:bb"},
            ],
        )

        _response, view, request = _refresh(DeviceInterfaceTableView, device, librenms_server, settings)

        assert message_texts(request, "success") == ["Interface data refreshed successfully."]
        snapshot = _cached_ports(view, device)
        assert "oob_incomplete" not in snapshot
        by_port_id = {port["port_id"]: port for port in snapshot["ports"]}
        assert [by_port_id[pid]["_source"] for pid in (1, 2, 3, 4)] == ["main"] * 4
        assert [by_port_id[pid]["_source"] for pid in (91, 92, 93)] == ["oob"] * 3
        conflicts = {pid for pid, port in by_port_id.items() if port.get("_dedup_conflict")}
        assert conflicts == {1, 91}

    def test_oob_ports_fetch_failure_warns_and_keeps_the_host_snapshot(self, librenms_server, settings):
        """A failed OOB ports fetch keeps host rows, warns, and tags the snapshot oob_incomplete."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = _map_device(make_device("iface-oob-fetch-fail"), 41, oob={"id": 42, "type": "idrac"})
        librenms_server.ports_response(device_id=41, ports=[{"port_id": 1, "ifName": "eth0"}])
        # /api/v0/devices/42/ports stays unregistered, so the loopback answers 404.

        _response, view, request = _refresh(DeviceInterfaceTableView, device, librenms_server, settings)

        assert message_texts(request, "warning") == [
            "Interfaces refreshed, but OOB controller ports fetch failed; "
            "showing host interfaces only. See server logs for details."
        ]
        assert message_texts(request, "success") == ["Host interface data refreshed successfully."]
        snapshot = _cached_ports(view, device)
        assert snapshot["oob_incomplete"] is True
        assert [port["port_id"] for port in snapshot["ports"]] == [1]
        assert "/api/v0/devices/42/ports" in [r["path"] for r in librenms_server.requests]


@pytest.mark.django_db
class TestInterfaceContextNameField:
    def test_context_resolves_the_name_field_when_the_caller_passes_none(self, librenms_server, settings):
        """get_context_data resolves the interface name field from the request when given None."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = _map_device(make_device("iface-name-field"), 51)
        view = DeviceInterfaceTableView()
        view._librenms_api = _bind_server(settings, librenms_server)
        request = make_request("get", path="/?interface_name_field=ifDescr", user=make_superuser())
        request.GET = request.GET.copy()
        request.GET["interface_name_field"] = "ifDescr"
        view.setup(request)

        context = view.get_context_data(request, device, None, server_key=SERVER_KEY)

        assert context["interface_name_field"] == "ifDescr"
