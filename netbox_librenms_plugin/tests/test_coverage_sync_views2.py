"""Authorization and failure-path integration tests for synchronization writes."""

from decimal import Decimal

import pytest
from dcim.models import Cable, Site
from django.contrib import messages
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse
from ipam.models import IPAddress, VLAN, VLANGroup, VRF
from virtualization.models import VMInterface, VirtualMachine

from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    make_device,
    make_interface,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
from netbox_librenms_plugin.utils import set_librenms_device_id
from netbox_librenms_plugin.views.sync.cables import SyncCablesView
from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView
from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView


SERVER_KEY = "default"


def _login(client, username):
    client.force_login(make_superuser(username))


def _response_messages(response, level=None):
    wanted = None if level is None else getattr(messages, level.upper())
    return [
        str(message) for message in get_messages(response.wsgi_request) if wanted is None or message.level == wanted
    ]


def _seed(view, obj, data_type, payload):
    cache.set(view.get_cache_key(obj, data_type, SERVER_KEY), payload, timeout=300)


def _set_id(obj, value):
    set_librenms_device_id(obj, value, SERVER_KEY)
    obj.save(update_fields=["custom_field_data"])


def _cable_url(device):
    return reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[device.pk])


def _ip_url(obj):
    object_type = "virtualmachine" if isinstance(obj, VirtualMachine) else "device"
    return reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": object_type, "pk": obj.pk},
    )


def _vlan_url(device):
    return reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans",
        kwargs={"object_type": "device", "object_id": device.pk},
    )


def _ip_snapshot(row, interface):
    port_id = row["port_id"]
    return {
        "ip_addresses": [row],
        "mgmt_ip": "",
        "ports_by_id": {
            port_id: {
                "port_id": port_id,
                "ifName": interface.name,
                "ifDescr": interface.name,
                "ifType": "ethernetCsmacd",
            }
        },
        "interface_name_field": "ifName",
    }


@pytest.mark.django_db
class TestCableWriteFailures:
    def test_cache_miss_reports_an_error_without_creating_a_cable(self, client, live_librenms):
        device = make_device("cable-cache-miss", librenms_cf={SERVER_KEY: {"id": 101}})
        _login(client, "cable-cache-miss-user")

        response = client.post(
            _cable_url(device),
            {"server_key": SERVER_KEY, "select": "missing-row"},
        )

        assert response.status_code == 302
        assert Cable.objects.count() == 0
        assert _response_messages(response, "error") == [
            "Cache has expired. Please refresh the cable data before syncing."
        ]

    def test_empty_selection_reports_an_error_without_mutation(self, client, live_librenms):
        device = make_device("cable-no-selection", librenms_cf={SERVER_KEY: {"id": 102}})
        interface = make_interface(device, "Ethernet1")
        _seed(
            SyncCablesView(),
            device,
            "links",
            {"links": [{"local_port_id": 7321, "local_port": interface.name}]},
        )
        _login(client, "cable-no-selection-user")

        response = client.post(_cable_url(device), {"server_key": SERVER_KEY})

        assert response.status_code == 302
        assert Cable.objects.count() == 0
        assert _response_messages(response, "error") == ["No interfaces selected for synchronization."]

    def test_user_without_cable_permissions_cannot_write(self, client, live_librenms):
        device = make_device("cable-denied", librenms_cf={SERVER_KEY: {"id": 103}})
        remote_device = make_device("cable-denied-remote", librenms_cf={SERVER_KEY: {"id": 104}})
        local = make_interface(device, "Ethernet1")
        remote = make_interface(remote_device, "Ethernet2")
        _set_id(local, 7301)
        _set_id(remote, 7302)
        _seed(
            SyncCablesView(),
            device,
            "links",
            {
                "links": [
                    {
                        "local_port_id": 7301,
                        "local_port": local.name,
                        "remote_port_id": 7302,
                        "remote_port": remote.name,
                        "remote_device": remote_device.name,
                        "remote_device_id": 104,
                    }
                ]
            },
        )
        client.force_login(make_user_with_perms("cable-denied-user", []))

        response = client.post(
            _cable_url(device),
            {
                "server_key": SERVER_KEY,
                "select": "7301",
                "expected_local_id_7301": local.pk,
                "expected_local_device_id_7301": device.pk,
                "expected_remote_id_7301": remote.pk,
                "expected_remote_device_id_7301": remote_device.pk,
            },
        )

        assert response.status_code == 302
        assert Cable.objects.count() == 0
        assert any("dcim.view_device" in text for text in _response_messages(response, "error"))

    def test_matching_existing_cable_is_not_duplicated(self, client, live_librenms):
        device = make_device("cable-existing", librenms_cf={SERVER_KEY: {"id": 105}})
        remote_device = make_device("cable-existing-remote", librenms_cf={SERVER_KEY: {"id": 106}})
        local = make_interface(device, "Ethernet1")
        remote = make_interface(remote_device, "Ethernet2")
        _set_id(local, 7311)
        _set_id(remote, 7312)
        existing = cable_together(local, remote)
        _seed(
            SyncCablesView(),
            device,
            "links",
            {
                "links": [
                    {
                        "local_port_id": 7311,
                        "local_port": local.name,
                        "remote_port_id": 7312,
                        "remote_port": remote.name,
                        "remote_device": remote_device.name,
                        "remote_device_id": 106,
                    }
                ]
            },
        )
        _login(client, "cable-existing-user")

        response = client.post(
            _cable_url(device),
            {
                "server_key": SERVER_KEY,
                "select": "7311",
                "expected_local_id_7311": local.pk,
                "expected_local_device_id_7311": device.pk,
                "expected_remote_id_7311": remote.pk,
                "expected_remote_device_id_7311": remote_device.pk,
            },
        )

        assert response.status_code == 302
        assert list(Cable.objects.values_list("pk", flat=True)) == [existing.pk]
        existing.refresh_from_db()
        assert existing.tags.filter(name="librenms").exists()


@pytest.mark.django_db
class TestDeviceHTTPWriteFailures:
    def test_add_device_permission_denial_stops_before_http(self, client, live_librenms):
        device = make_device("add-device-denied")
        client.force_login(make_user_with_perms("add-device-denied-user", []))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:add_device_to_librenms", args=[device.pk]),
            {
                "object_type": "device",
                "v1v2-snmp_version": "v2c",
                "v1v2-hostname": "router.example.test",
                "v1v2-community": "test-community",
            },
            HTTP_REFERER=device.get_absolute_url(),
        )

        assert response.status_code == 302
        assert live_librenms.server.requests == []
        assert _response_messages(response, "error") == ["Missing permissions: dcim.change_device"]

    def test_invalid_add_form_stops_before_http(self, client, live_librenms):
        device = make_device("add-device-invalid")
        _login(client, "add-device-invalid-user")

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:add_device_to_librenms", args=[device.pk]),
            {"object_type": "device", "v1v2-snmp_version": "v2c"},
        )

        assert response.status_code == 302
        assert not any(request["method"] == "POST" for request in live_librenms.server.requests)
        errors = _response_messages(response, "error")
        assert any(text.startswith("hostname:") for text in errors)
        assert any(text.startswith("community:") for text in errors)

    def test_location_update_with_unknown_server_stops_before_http(self, client, live_librenms):
        device = make_device("device-location-stale-server", librenms_cf={SERVER_KEY: {"id": 111}})
        _login(client, "device-location-stale-server-user")

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:update_device_location", args=[device.pk]),
            {"server_key": "removed"},
        )

        assert response.status_code == 302
        assert live_librenms.server.requests == []
        assert _response_messages(response, "error") == ["Selected LibreNMS server is no longer configured."]


@pytest.mark.django_db
class TestIPAddressWriteFailures:
    def test_unknown_server_key_fails_closed(self, client, live_librenms):
        device = make_device("ip-unknown-server", librenms_cf={SERVER_KEY: {"id": 121}})
        _login(client, "ip-unknown-server-user")

        response = client.post(
            _ip_url(device),
            {"server_key": "removed", "select": "198.18.121.10/24"},
        )

        assert response.status_code == 302
        assert IPAddress.objects.count() == 0
        assert _response_messages(response, "error") == ["Selected LibreNMS server is no longer configured."]

    def test_empty_selection_does_not_create_an_address(self, client, live_librenms):
        device = make_device("ip-no-selection", librenms_cf={SERVER_KEY: {"id": 122}})
        interface = make_interface(device, "Ethernet1")
        row = {
            "ip_address": "198.18.122.10",
            "prefix_length": 24,
            "ip_with_mask": "198.18.122.10/24",
            "port_id": 8321,
            "interface_name": interface.name,
        }
        _seed(SyncIPAddressesView(), device, "ip_addresses", _ip_snapshot(row, interface))
        _login(client, "ip-no-selection-user")

        response = client.post(_ip_url(device), {"server_key": SERVER_KEY})

        assert response.status_code == 302
        assert IPAddress.objects.count() == 0
        assert _response_messages(response, "error") == ["No IP addresses selected for synchronization."]

    def test_existing_address_on_another_interface_requires_confirmation(self, client, live_librenms):
        device = make_device("ip-conflict", librenms_cf={SERVER_KEY: {"id": 123}})
        intended = make_interface(device, "Ethernet1")
        current = make_interface(device, "Ethernet2")
        _set_id(intended, 8331)
        row_id = "198.18.123.10/24"
        address = IPAddress.objects.create(address=row_id, assigned_object=current, status="active")
        row = {
            "ip_address": "198.18.123.10",
            "prefix_length": 24,
            "ip_with_mask": row_id,
            "port_id": 8331,
            "interface_name": intended.name,
        }
        _seed(SyncIPAddressesView(), device, "ip_addresses", _ip_snapshot(row, intended))
        _login(client, "ip-conflict-user")

        response = client.post(
            _ip_url(device),
            {"server_key": SERVER_KEY, "select": row_id, f"vrf_{row_id}": ""},
        )

        assert response.status_code == 200
        assert row_id.encode() in response.content
        assert b"Reassign the existing IP address" in response.content
        address.refresh_from_db()
        assert address.assigned_object == current

    def test_selected_vrf_is_persisted_with_the_address(self, client, live_librenms):
        device = make_device("ip-vrf", librenms_cf={SERVER_KEY: {"id": 124}})
        interface = make_interface(device, "Ethernet1")
        _set_id(interface, 8341)
        vrf = VRF.objects.create(name="Application VRF", rd="64512:124")
        row_id = "198.18.124.10/24"
        row = {
            "ip_address": "198.18.124.10",
            "prefix_length": 24,
            "ip_with_mask": row_id,
            "port_id": 8341,
            "interface_name": interface.name,
        }
        _seed(SyncIPAddressesView(), device, "ip_addresses", _ip_snapshot(row, interface))
        _login(client, "ip-vrf-user")

        response = client.post(
            _ip_url(device),
            {"server_key": SERVER_KEY, "select": row_id, f"vrf_{row_id}": vrf.pk},
        )

        assert response.status_code == 302
        address = IPAddress.objects.get(address=row_id, vrf=vrf)
        assert address.assigned_object == interface

    def test_vm_address_is_assigned_to_a_real_vm_interface(self, client, live_librenms):
        vm = make_vm("ip-vm")
        vm.custom_field_data["librenms_id"] = {SERVER_KEY: {"id": 125}}
        vm.save(update_fields=["custom_field_data"])
        interface = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        _set_id(interface, 8351)
        row_id = "198.18.125.10/24"
        row = {
            "ip_address": "198.18.125.10",
            "prefix_length": 24,
            "ip_with_mask": row_id,
            "port_id": 8351,
            "interface_name": interface.name,
        }
        _seed(SyncIPAddressesView(), vm, "ip_addresses", _ip_snapshot(row, interface))
        _login(client, "ip-vm-user")

        response = client.post(
            _ip_url(vm),
            {"server_key": SERVER_KEY, "select": row_id, f"vrf_{row_id}": ""},
        )

        assert response.status_code == 302
        assert IPAddress.objects.get(address=row_id).assigned_object == interface


@pytest.mark.django_db
class TestVLANWriteFailures:
    def test_grouped_vlan_is_created_in_the_selected_scope(self, client, live_librenms):
        device = make_device("vlan-group", librenms_cf={SERVER_KEY: {"id": 131}})
        group = VLANGroup.objects.create(name="Application Group", slug="application-group")
        _seed(SyncVLANsView(), device, "vlans", [{"vlan_vlan": 3131, "vlan_name": "Application"}])
        _login(client, "vlan-group-user")

        response = client.post(
            _vlan_url(device),
            {
                "server_key": SERVER_KEY,
                "action": "create_vlans",
                "select": "3131",
                "vlan_group_3131": group.pk,
            },
        )

        assert response.status_code == 302
        assert VLAN.objects.filter(vid=3131, name="Application", group=group).exists()

    def test_created_vlan_outside_add_grant_is_rolled_back(self, client, live_librenms):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.view_test_helpers import grant

        device = make_device("vlan-add-scope", librenms_cf={SERVER_KEY: {"id": 135}})
        _seed(SyncVLANsView(), device, "vlans", [{"vlan_vlan": 3136, "vlan_name": "Outside add scope"}])
        user = make_user_with_perms(
            "vlan-add-scope-user",
            [("view", Device), ("change", VLAN)],
        )
        user = grant(user, "add", VLAN, constraints={"vid": 3137})
        client.force_login(user)

        response = client.post(
            _vlan_url(device),
            {
                "server_key": SERVER_KEY,
                "action": "create_vlans",
                "select": "3136",
            },
        )

        assert response.status_code == 302
        assert not VLAN.objects.filter(vid=3136).exists()
        assert _response_messages(response, "error") == [
            "VLAN 3136: the new VLAN is outside your add permission constraints; skipped."
        ]

    def test_missing_group_fails_closed_instead_of_creating_global_vlan(self, client, live_librenms):
        device = make_device("vlan-group-missing", librenms_cf={SERVER_KEY: {"id": 132}})
        _seed(SyncVLANsView(), device, "vlans", [{"vlan_vlan": 3132, "vlan_name": "Application"}])
        _login(client, "vlan-group-missing-user")

        response = client.post(
            _vlan_url(device),
            {
                "server_key": SERVER_KEY,
                "action": "create_vlans",
                "select": "3132",
                "vlan_group_3132": 2_147_483_647,
            },
        )

        assert response.status_code == 302
        assert not VLAN.objects.filter(vid=3132).exists()
        assert any("no longer exists" in text for text in _response_messages(response, "error"))

    def test_invalid_rows_do_not_abort_a_valid_vlan_in_the_batch(self, client, live_librenms):
        device = make_device("vlan-invalid-batch", librenms_cf={SERVER_KEY: {"id": 133}})
        too_long = "x" * (VLAN._meta.get_field("name").max_length + 1)
        _seed(
            SyncVLANsView(),
            device,
            "vlans",
            [
                {"vlan_vlan": 0, "vlan_name": "Invalid VID"},
                {"vlan_vlan": 3133, "vlan_name": too_long},
                {"vlan_vlan": 3134, "vlan_name": "Valid VLAN"},
            ],
        )
        _login(client, "vlan-invalid-batch-user")

        response = client.post(
            _vlan_url(device),
            {
                "server_key": SERVER_KEY,
                "action": "create_vlans",
                "select": ["not-a-vid", "0", "3133", "3134"],
            },
        )

        assert response.status_code == 302
        assert not VLAN.objects.filter(vid__in=[0, 3133]).exists()
        assert VLAN.objects.filter(vid=3134, name="Valid VLAN").exists()
        errors = _response_messages(response, "error")
        assert any("VID is invalid" in text for text in errors)
        assert any("name is invalid" in text for text in errors)

    def test_unchanged_vlan_is_not_rewritten(self, client, live_librenms):
        device = make_device("vlan-unchanged", librenms_cf={SERVER_KEY: {"id": 134}})
        vlan = VLAN.objects.create(vid=3135, name="Stable VLAN", status="active")
        before = vlan.last_updated
        _seed(SyncVLANsView(), device, "vlans", [{"vlan_vlan": 3135, "vlan_name": "Stable VLAN"}])
        _login(client, "vlan-unchanged-user")

        response = client.post(
            _vlan_url(device),
            {"server_key": SERVER_KEY, "action": "create_vlans", "select": "3135"},
        )

        assert response.status_code == 302
        vlan.refresh_from_db()
        assert vlan.last_updated == before
        assert _response_messages(response, "success") == ["VLANs synced: 1 unchanged."]


@pytest.mark.django_db
class TestSiteLocationWriteFailures:
    def test_site_permission_denial_stops_before_http(self, client, live_librenms):
        site = Site.objects.create(
            name="Location Denied",
            slug="location-denied",
            latitude=Decimal("52.100000"),
            longitude=Decimal("4.300000"),
        )
        client.force_login(make_user_with_perms("location-denied-user", []))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:site_location_sync"),
            {"action": "create", "pk": site.pk},
        )

        assert response.status_code == 302
        assert live_librenms.server.requests == []
        assert _response_messages(response, "error") == ["Missing permissions: dcim.view_site"]

    def test_create_failure_surfaces_librenms_message(self, client, live_librenms):
        site = Site.objects.create(
            name="Location Rejected",
            slug="location-rejected",
            latitude=Decimal("52.200000"),
            longitude=Decimal("4.400000"),
        )
        live_librenms.server.register(
            "/api/v0/locations",
            {"status": "error", "message": "Coordinates rejected"},
            status=422,
            method="POST",
        )
        _login(client, "location-rejected-user")

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:site_location_sync"),
            {"action": "create", "pk": site.pk},
        )

        assert response.status_code == 302
        assert any("Coordinates rejected" in text for text in _response_messages(response, "error"))

    def test_update_requires_a_matching_librenms_location(self, client, live_librenms):
        site = Site.objects.create(
            name="Location Missing",
            slug="location-missing",
            latitude=Decimal("52.300000"),
            longitude=Decimal("4.500000"),
        )
        live_librenms.server.register(
            "/api/v0/resources/locations",
            {"status": "ok", "locations": [{"location": "Different Location"}]},
            method="GET",
        )
        _login(client, "location-missing-user")

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:site_location_sync"),
            {"action": "update", "pk": site.pk},
        )

        assert response.status_code == 302
        assert _response_messages(response, "error") == ["Could not find matching location for site 'Location Missing'"]
        assert [request["method"] for request in live_librenms.server.requests] == ["GET"]


def _snmp_post(prefix, **fields):
    """POST data for the real SNMP form, prefixed the way the template renders it."""
    data = {"object_type": "device", f"{prefix}-snmp_version": fields.pop("snmp_version", "v2c")}
    data.update({f"{prefix}-{k}": v for k, v in fields.items()})
    return data


@pytest.mark.django_db
def test_match_interface_stops_on_ambiguous_port_id():
    """An ambiguous stable identity must not fall through to a cached name."""
    dev = make_device("ipres-ambig-name")
    named = make_interface(dev, "eth0")
    result = SyncIPAddressesView()._match_interface(
        {"port_id": 7, "interface_name": "eth0"}, {"7": None}, {"eth0": named}
    )
    assert result is None


@pytest.mark.django_db
@pytest.mark.parametrize("form_id", ["snmpv1v2-form", "snmpv3-form"])
def test_rendered_snmp_form_carries_one_server_selection(client, settings, librenms_server, form_id):
    """The browser must submit one server value to the strict device-add endpoint."""
    from django.urls import reverse

    from netbox_librenms_plugin.tests._html_helpers import open_tags
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_superuser

    configure_librenms_servers(
        settings,
        {"primary": {"librenms_url": librenms_server.url, "api_token": "test-token", "verify_ssl": False}},
    )
    device = make_device("unmapped-snmp-form.example")
    librenms_server.register(
        f"/api/v0/devices/{device.name}", {"status": "error", "message": "Device not found"}, status=404
    )
    librenms_server.register("/api/v0/poller_group", {"status": "ok", "get_poller_group": []})
    client.force_login(make_superuser("rendered-snmp-form-user"))

    response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]))

    assert response.status_code == 200
    html = response.content.decode()
    marker = html.index(f'id="{form_id}"')
    form_start = html.rindex("<form", 0, marker)
    form_end = html.index("</form>", marker)
    inputs = open_tags(html[form_start:form_end], "input")
    server_values = [field.get("value") for field in inputs if field.get("name") == "server_key"]
    assert server_values == ["primary"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "prefix,version,credentials",
    [
        ("v1v2", "v2c", {"community": "test-community"}),
        ("v3", "v3", {"authlevel": "noAuthNoPriv", "authname": "test-user"}),
    ],
)
def test_add_device_posts_to_the_submitted_server(
    client, settings, live_librenms, librenms_server, prefix, version, credentials
):
    """Poller validation and the add request must use the server shown in the form."""
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

    primary = live_librenms.server
    secondary = librenms_server
    configure_librenms_servers(
        settings,
        {
            "primary": {"librenms_url": primary.url, "api_token": "test-token", "verify_ssl": False},
            "secondary": {"librenms_url": secondary.url, "api_token": "test-token", "verify_ssl": False},
        },
    )
    for server, group in ((primary, 11), (secondary, 22)):
        server.register(
            "/api/v0/poller_group", {"status": "ok", "get_poller_group": [{"id": group, "group_name": "Test pollers"}]}
        )
    secondary.register("/api/v0/devices", {"status": "ok"}, method="POST")
    device = make_device("snmp-server-device")
    _login(client, "snmp-server-user")
    data = _snmp_post(prefix, snmp_version=version, hostname="router.example", poller_group="22", **credentials)
    data["server_key"] = "secondary"
    response = client.post(reverse("plugins:netbox_librenms_plugin:add_device_to_librenms", args=[device.pk]), data)
    assert response.status_code == 302
    assert primary.requests == []
    posted = [request for request in secondary.requests if request["method"] == "POST"]
    assert len(posted) == 1
    assert posted[0]["path"] == "/api/v0/devices"
    assert posted[0]["body"]["poller_group"] == 22


@pytest.mark.django_db
@pytest.mark.parametrize("server_keys", [["primary", "secondary"], ["retired"]])
def test_add_device_rejects_ambiguous_or_removed_server_before_api_calls(
    client, settings, live_librenms, librenms_server, server_keys
):
    """A stale or ambiguous server selector must never send device credentials."""
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

    primary = live_librenms.server
    secondary = librenms_server
    configure_librenms_servers(
        settings,
        {
            "primary": {"librenms_url": primary.url, "api_token": "test-token", "verify_ssl": False},
            "secondary": {"librenms_url": secondary.url, "api_token": "test-token", "verify_ssl": False},
        },
    )
    device = make_device("snmp-invalid-server")
    _login(client, "snmp-invalid-server-user")
    data = _snmp_post("v1v2", hostname="router.example", community="test-community")
    data["server_key"] = server_keys
    response = client.post(reverse("plugins:netbox_librenms_plugin:add_device_to_librenms", args=[device.pk]), data)
    assert response.status_code == 302
    assert primary.requests == []
    assert secondary.requests == []
