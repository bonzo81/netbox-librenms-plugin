"""
Integration tests for the synchronization write views.

These tests drive real requests through Django, use the real NetBox models and cache,
and use a loopback HTTP server for the LibreNMS boundary. Detailed edge cases live in
the feature-specific test modules; this file verifies that the main write paths remain
wired together.
"""

from decimal import Decimal

import pytest
from dcim.models import Cable, Interface, Site
from django.contrib import messages
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse
from ipam.models import IPAddress, VLAN

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_superuser
from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts
from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id
from netbox_librenms_plugin.views.sync.cables import SyncCablesView
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView
from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView
from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView


SERVER_KEY = "default"


def _login(client, username):
    user = make_superuser(username)
    client.force_login(user)
    return user


def _response_messages(response, level=None):
    wanted = None if level is None else getattr(messages, level.upper())
    return [
        str(message) for message in get_messages(response.wsgi_request) if wanted is None or message.level == wanted
    ]


def _set_librenms_id(obj, value):
    set_librenms_device_id(obj, value, SERVER_KEY)
    obj.save(update_fields=["custom_field_data"])


def _seed_snapshot(view, obj, data_type, payload):
    cache.set(view.get_cache_key(obj, data_type, SERVER_KEY), payload, timeout=300)


@pytest.mark.django_db
class TestCableSynchronization:
    def test_selected_rows_preserve_client_identity_guards(self):
        device = make_device("cable-selection")
        request = make_request(
            data={
                "sync_one": "row-a",
                "device_selection_row-a": "17",
                "expected_local_id_row-a": "101",
                "expected_local_device_id_row-a": str(device.pk),
                "expected_remote_id_row-a": "202",
                "expected_remote_device_id_row-a": "18",
                "expected_cable_intent_row-a": "signed-state",
            }
        )

        selected = SyncCablesView().get_selected_interfaces(request, device)

        assert selected == [
            {
                "device_id": "17",
                "row_id": "row-a",
                "expected_local_id": 101,
                "expected_local_device_id": device.pk,
                "expected_remote_id": 202,
                "expected_remote_device_id": 18,
                "expected_cable_intent": "signed-state",
            }
        ]

    def test_empty_selection_is_a_clean_noop(self):
        device = make_device("cable-empty-selection")
        request = make_request(data={"select": ["", ""]})

        assert SyncCablesView().get_selected_interfaces(request, device) is None

    def test_cached_link_creates_a_real_provenance_cable(self, client, live_librenms):
        local_device = make_device("cable-local", librenms_cf={SERVER_KEY: {"id": 61}})
        remote_device = make_device("cable-remote", librenms_cf={SERVER_KEY: {"id": 62}})
        local = make_interface(local_device, "Ethernet1", iface_type="1000base-t")
        remote = make_interface(remote_device, "Ethernet2", iface_type="1000base-t")
        _set_librenms_id(local, 7201)
        _set_librenms_id(remote, 7202)
        payload = {
            "links": [
                {
                    "local_port_id": 7201,
                    "local_port": local.name,
                    "remote_port_id": 7202,
                    "remote_port": remote.name,
                    "remote_device": remote_device.name,
                    "remote_device_id": 62,
                }
            ]
        }
        _seed_snapshot(SyncCablesView(), local_device, "links", payload)
        _login(client, "cable-sync-user")

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_device_cables",
                kwargs={"pk": local_device.pk},
            ),
            {
                "server_key": SERVER_KEY,
                "select": "7201",
                "expected_local_id_7201": local.pk,
                "expected_local_device_id_7201": local_device.pk,
                "expected_remote_id_7201": remote.pk,
                "expected_remote_device_id_7201": remote_device.pk,
            },
        )

        assert response.status_code == 302
        local.refresh_from_db()
        remote.refresh_from_db()
        assert local.cable_id is not None
        assert remote.cable_id == local.cable_id
        cable = Cable.objects.get(pk=local.cable_id)
        assert cable.status == "connected"
        assert cable.description.endswith("(default)")
        assert cable.tags.filter(name="librenms").exists()

    def test_unexpected_row_failure_is_collected_as_failed(self):
        class FailingCableSyncView(SyncCablesView):
            def process_single_interface(self, interface, cached_links, force=False):
                raise RuntimeError("sync failed")

        results = FailingCableSyncView().process_interface_sync([{"row_id": "row-7"}], [])

        assert results["failed"] == ["row-7"]
        assert results["invalid"] == []


@pytest.mark.django_db
class TestInterfaceSynchronization:
    def test_cached_port_creates_a_real_interface(self, client, live_librenms):
        device = make_device("interface-create", librenms_cf={SERVER_KEY: {"id": 71}})
        payload = {
            "ports": [
                {
                    "port_id": 8101,
                    "ifName": "Ethernet1/1",
                    "ifDescr": "Server uplink",
                    "ifType": "ethernetCsmacd",
                    "ifAdminStatus": "up",
                    "ifSpeed": 1_000_000_000,
                    "ifMtu": 1500,
                    "ifPhysAddress": "",
                }
            ],
            "port_stack_relationships": {},
        }
        _seed_snapshot(SyncInterfacesView(), device, "ports", payload)
        _login(client, "interface-create-user")

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_selected_interfaces",
                kwargs={"object_type": "device", "object_id": device.pk},
            ),
            {
                "server_key": SERVER_KEY,
                "interface_name_field": "ifName",
                "select": "8101",
                "exclude_columns": "vlans",
            },
        )

        assert response.status_code == 302
        interface = Interface.objects.get(device=device, name="Ethernet1/1")
        assert get_librenms_device_id(interface, SERVER_KEY, auto_save=False) == 8101
        assert interface.enabled is True
        assert interface.mtu == 1500

    def test_empty_selection_does_not_change_interfaces(self, client, live_librenms):
        device = make_device("interface-no-selection", librenms_cf={SERVER_KEY: {"id": 72}})
        payload = {"ports": [], "port_stack_relationships": {}}
        _seed_snapshot(SyncInterfacesView(), device, "ports", payload)
        _login(client, "interface-no-selection-user")

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_selected_interfaces",
                kwargs={"object_type": "device", "object_id": device.pk},
            ),
            {"server_key": SERVER_KEY, "interface_name_field": "ifName"},
        )

        assert response.status_code == 302
        assert not Interface.objects.filter(device=device).exists()
        assert _response_messages(response, "error") == ["No interfaces selected for synchronization."]

    def test_delete_endpoint_removes_only_the_selected_interface(self, client, live_librenms):
        device = make_device("interface-delete", librenms_cf={SERVER_KEY: {"id": 73}})
        selected = make_interface(device, "Ethernet1")
        retained = make_interface(device, "Ethernet2")
        _login(client, "interface-delete-user")

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:delete_netbox_interfaces",
                kwargs={"object_type": "device", "object_id": device.pk},
            ),
            {"server_key": SERVER_KEY, "interface_ids": [selected.pk]},
        )

        assert response.status_code == 200
        assert response.json()["deleted_count"] == 1
        assert not Interface.objects.filter(pk=selected.pk).exists()
        assert Interface.objects.filter(pk=retained.pk).exists()

    def test_delete_endpoint_rejects_an_empty_selection(self, client, live_librenms):
        device = make_device("interface-delete-empty", librenms_cf={SERVER_KEY: {"id": 74}})
        _login(client, "interface-delete-empty-user")

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:delete_netbox_interfaces",
                kwargs={"object_type": "device", "object_id": device.pk},
            ),
            {"server_key": SERVER_KEY},
        )

        assert response.status_code == 400
        assert response.json() == {"error": "No interfaces selected for deletion"}


@pytest.mark.django_db
class TestIPAddressSynchronization:
    def test_cached_ip_is_created_and_assigned_by_port_identity(self, client, live_librenms):
        device = make_device("ip-create", librenms_cf={SERVER_KEY: {"id": 81}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 8201)
        row_id = "198.18.81.10/24"
        payload = {
            "ip_addresses": [
                {
                    "ip_address": "198.18.81.10",
                    "prefix_length": 24,
                    "ip_with_mask": row_id,
                    "port_id": 8201,
                    "interface_name": interface.name,
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {
                8201: {
                    "port_id": 8201,
                    "ifName": interface.name,
                    "ifDescr": interface.name,
                    "ifType": "ethernetCsmacd",
                }
            },
            "interface_name_field": "ifName",
        }
        _seed_snapshot(SyncIPAddressesView(), device, "ip_addresses", payload)
        _login(client, "ip-create-user")

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
                kwargs={"object_type": "device", "pk": device.pk},
            ),
            {"server_key": SERVER_KEY, "select": row_id, f"vrf_{row_id}": ""},
        )

        assert response.status_code == 302
        address = IPAddress.objects.get(address=row_id)
        assert address.assigned_object == interface
        assert _response_messages(response, "success") == [f"Created IP addresses: {row_id}"]

    def test_expired_snapshot_does_not_create_an_address(self, client, live_librenms):
        device = make_device("ip-cache-miss", librenms_cf={SERVER_KEY: {"id": 82}})
        _login(client, "ip-cache-miss-user")

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
                kwargs={"object_type": "device", "pk": device.pk},
            ),
            {"server_key": SERVER_KEY, "select": "198.18.82.10/24"},
        )

        assert response.status_code == 302
        assert not IPAddress.objects.filter(address="198.18.82.10/24").exists()
        assert _response_messages(response, "error") == ["Cache has expired. Please refresh the IP data."]


@pytest.mark.django_db
class TestVLANSynchronization:
    def test_cached_vlan_is_created(self, client, live_librenms):
        device = make_device("vlan-create", librenms_cf={SERVER_KEY: {"id": 91}})
        view = SyncVLANsView()
        _seed_snapshot(view, device, "vlans", [{"vlan_vlan": 3091, "vlan_name": "Application"}])
        _login(client, "vlan-create-user")
        url = reverse(
            "plugins:netbox_librenms_plugin:sync_selected_vlans",
            kwargs={"object_type": "device", "object_id": device.pk},
        )

        created = client.post(
            url,
            {"server_key": SERVER_KEY, "action": "create_vlans", "select": "3091"},
        )

        assert created.status_code == 302
        vlan = VLAN.objects.get(vid=3091, group__isnull=True)
        assert vlan.name == "Application"
        assert _response_messages(created, "success") == ["VLANs synced: 1 created."]

    def test_cached_vlan_updates_an_existing_global_vlan(self, client, live_librenms):
        device = make_device("vlan-update", librenms_cf={SERVER_KEY: {"id": 93}})
        vlan = VLAN.objects.create(vid=3093, name="Old Name", status="active")
        view = SyncVLANsView()
        _login(client, "vlan-update-user")
        url = reverse(
            "plugins:netbox_librenms_plugin:sync_selected_vlans",
            kwargs={"object_type": "device", "object_id": device.pk},
        )
        _seed_snapshot(view, device, "vlans", [{"vlan_vlan": 3093, "vlan_name": "Application Servers"}])
        updated = client.post(
            url,
            {"server_key": SERVER_KEY, "action": "create_vlans", "select": "3093"},
        )

        assert updated.status_code == 302
        vlan.refresh_from_db()
        assert vlan.name == "Application Servers"
        assert _response_messages(updated, "success") == ["VLANs synced: 1 updated."]

    def test_missing_snapshot_fails_without_creating_a_vlan(self, client, live_librenms):
        device = make_device("vlan-cache-miss", librenms_cf={SERVER_KEY: {"id": 92}})
        _login(client, "vlan-cache-miss-user")

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_selected_vlans",
                kwargs={"object_type": "device", "object_id": device.pk},
            ),
            {"server_key": SERVER_KEY, "action": "create_vlans", "select": "3092"},
        )

        assert response.status_code == 302
        assert not VLAN.objects.filter(vid=3092).exists()
        assert _response_messages(response, "error") == ["No cached VLAN data. Please refresh VLANs first."]


@pytest.mark.django_db
class TestSiteLocationSynchronization:
    def test_coordinate_matching_uses_the_declared_tolerance(self):
        view = SyncSiteLocationView()

        assert view.check_coordinates_match("52.10000", "4.30000", "52.10005", "4.30005") is True
        assert view.check_coordinates_match("52.10000", "4.30000", "52.10020", "4.30000") is False
        assert view.check_coordinates_match(None, "4.30000", "52.10000", "4.30000") is False

    def test_site_matches_a_location_by_name_or_slug(self):
        site = Site.objects.create(name="Location Alpha", slug="location-alpha")
        view = SyncSiteLocationView()
        locations = [
            {"location": "unrelated"},
            {"location": "LOCATION ALPHA", "lat": "52", "lng": "4"},
        ]

        assert view.match_site_with_location(site, locations) == locations[1]
        assert view.match_site_with_location(site, [{"location": "location-alpha"}]) == {"location": "location-alpha"}

    def test_create_action_sends_real_site_coordinates_to_librenms(self, client, live_librenms):
        site = Site.objects.create(
            name="Location Create",
            slug="location-create",
            latitude=Decimal("52.100000"),
            longitude=Decimal("4.300000"),
        )
        received = []

        def create_location(**request):
            received.append(request)
            return 200, {"status": "ok", "message": "Location created #101"}

        live_librenms.server.register("/api/v0/locations", create_location, method="POST")
        _login(client, "location-create-user")

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:site_location_sync"),
            {"action": "create", "pk": site.pk},
        )

        assert response.status_code == 302
        assert [request["body"] for request in received] == [
            {"location": site.name, "lat": "52.100000", "lng": "4.300000"}
        ]
        assert _response_messages(response, "success") == [
            "Location 'Location Create' created in LibreNMS successfully."
        ]

    def test_update_action_reads_the_match_then_patches_coordinates(self, client, live_librenms):
        site = Site.objects.create(
            name="Location Update",
            slug="location-update",
            latitude=Decimal("51.900000"),
            longitude=Decimal("4.500000"),
        )
        live_librenms.server.register(
            "/api/v0/resources/locations",
            {
                "status": "ok",
                "locations": [{"location": site.name, "lat": "0", "lng": "0"}],
            },
            method="GET",
        )
        received = []

        def update_location(**request):
            received.append(request)
            return 200, {"status": "ok", "message": "Location updated"}

        live_librenms.server.register(
            "/api/v0/locations/Location%20Update",
            update_location,
            method="PATCH",
        )
        _login(client, "location-update-user")

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:site_location_sync"),
            {"action": "update", "pk": site.pk},
        )

        assert response.status_code == 302
        assert [(request["path"], request["body"]) for request in received] == [
            ("/api/v0/locations/Location%20Update", {"lat": "51.900000", "lng": "4.500000"})
        ]
        assert _response_messages(response, "success") == [
            "Location 'Location Update' updated in LibreNMS successfully."
        ]

    def test_missing_coordinates_stop_before_the_http_boundary(self, live_librenms):
        site = Site.objects.create(name="Location Incomplete", slug="location-incomplete")
        request = make_request(data={"action": "create", "pk": site.pk})
        view = SyncSiteLocationView()
        view._librenms_api = live_librenms.api
        view.setup(request)

        response = view.post(request)

        assert response.status_code == 302
        assert live_librenms.server.requests == []
        assert message_texts(request, "warning") == [
            "Latitude and/or longitude is missing. Cannot create location 'Location Incomplete' in LibreNMS."
        ]


@pytest.mark.django_db
def test_ambiguous_port_id_is_skipped_despite_matching_name(client, live_librenms):
    """An ambiguous port ID cannot select one owner through a cached name."""
    device = make_device("ambiguous-port-sync", librenms_cf={SERVER_KEY: {"id": 81}})
    named = make_interface(device, "eth0")
    other = make_interface(device, "eth1")
    for interface in (named, other):
        _set_librenms_id(interface, 8201)
    row_id = "198.18.81.10/24"
    _seed_snapshot(
        SyncIPAddressesView(),
        device,
        "ip_addresses",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.81.10",
                    "prefix_length": 24,
                    "ip_with_mask": row_id,
                    "port_id": 8201,
                    "interface_name": named.name,
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {},
            "interface_name_field": "ifName",
        },
    )
    _login(client, "ambiguous-port-sync-user")
    response = client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses", kwargs={"object_type": "device", "pk": device.pk}
        ),
        {"server_key": SERVER_KEY, "select": row_id, f"vrf_{row_id}": ""},
    )
    assert response.status_code == 302
    assert not IPAddress.objects.filter(address=row_id).exists()
    assert any(row_id in text and "no matching NetBox interface" in text for text in _response_messages(response))
