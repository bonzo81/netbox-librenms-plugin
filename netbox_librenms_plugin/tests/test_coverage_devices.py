"""Integration tests for device synchronization tables and JSON endpoints."""

import json

import pytest
from django.core.cache import cache
from django.urls import reverse
from ipam.models import VLAN, VLANGroup

from netbox_librenms_plugin.tables.cables import LibreNMSCableTable, VCCableTable
from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable, VCInterfaceTable
from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable, VCModuleTable
from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
from netbox_librenms_plugin.utils import set_librenms_device_id
from netbox_librenms_plugin.views.object_sync.devices import (
    DeviceCableTableView,
    DeviceInterfaceTableView,
    DeviceModuleTableView,
    SaveVlanGroupOverridesView,
    SingleInterfaceVerifyView,
)


SERVER_KEY = "default"


def _json_post(client, route_name, body):
    return client.post(
        reverse(f"plugins:netbox_librenms_plugin:{route_name}"),
        data=json.dumps(body),
        content_type="application/json",
    )


def _login(client, username):
    client.force_login(make_superuser(username))


def _bind_api(view, request, live_librenms):
    view.request = request
    view._librenms_api = live_librenms.api
    return view


def _port(port_id=4001, name="Ethernet1"):
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifAlias": "Server uplink",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifPhysAddress": "",
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "ifOperStatus": "up",
    }


def _set_id(obj, value):
    set_librenms_device_id(obj, value, SERVER_KEY)
    obj.save(update_fields=["custom_field_data"])


@pytest.mark.django_db
class TestDeviceTables:
    def test_interface_view_reads_real_device_interfaces(self, live_librenms):
        device = make_device("device-interface-table")
        first = make_interface(device, "Ethernet1")
        second = make_interface(device, "Ethernet2")
        request = make_request("get", user=make_superuser("device-interface-table-user"), path="/sync/")
        view = _bind_api(DeviceInterfaceTableView(), request, live_librenms)

        assert list(view.get_interfaces(device)) == [first, second]
        assert view.get_redirect_url(device) == reverse(
            "plugins:netbox_librenms_plugin:device_interface_sync",
            kwargs={"pk": device.pk},
        )

    def test_non_chassis_device_uses_standard_interface_table(self, live_librenms):
        device = make_device("device-interface-standard")
        request = make_request("get", user=make_superuser("device-interface-standard-user"), path="/sync/")
        view = _bind_api(DeviceInterfaceTableView(), request, live_librenms)

        table = view.get_table([_port()], device, "ifName")

        assert isinstance(table, LibreNMSInterfaceTable)
        assert table.htmx_url == "/sync/?tab=interfaces&server_key=default"

    def test_chassis_member_uses_virtual_chassis_interface_table(self, live_librenms):
        _chassis, (device, _sibling) = make_virtual_chassis_members("device-interface-vc")
        request = make_request("get", user=make_superuser("device-interface-vc-user"), path="/sync/")
        view = _bind_api(DeviceInterfaceTableView(), request, live_librenms)

        table = view.get_table([_port()], device, "ifName")

        assert isinstance(table, VCInterfaceTable)
        assert table.htmx_url == "/sync/?tab=interfaces&server_key=default"

    def test_cable_table_type_follows_virtual_chassis_membership(self, live_librenms):
        plain = make_device("device-cable-standard")
        _chassis, (member, _sibling) = make_virtual_chassis_members("device-cable-vc")
        user = make_superuser("device-cable-table-user")
        request = make_request("get", user=user, path="/sync/")
        view = _bind_api(DeviceCableTableView(), request, live_librenms)

        assert isinstance(view.get_table([], plain), LibreNMSCableTable)
        assert isinstance(view.get_table([], member), VCCableTable)

    def test_module_table_type_and_write_flags_follow_real_user_permissions(self, live_librenms):
        plain = make_device("device-module-standard")
        _chassis, (member, _sibling) = make_virtual_chassis_members("device-module-vc")
        request = make_request("get", user=make_superuser("device-module-table-user"), path="/sync/")
        view = _bind_api(DeviceModuleTableView(), request, live_librenms)

        plain_table = view.get_table([], plain)
        chassis_table = view.get_table([], member)

        assert isinstance(plain_table, LibreNMSModuleTable)
        assert isinstance(chassis_table, VCModuleTable)
        assert plain_table.htmx_url == "/sync/?tab=modules&server_key=default"
        assert plain_table.can_add_module is True
        assert plain_table.can_change_interface is True

    def test_module_table_uses_the_active_render_server(self, live_librenms):
        device = make_device("device-module-active-server")
        request = make_request("get", user=make_superuser("device-module-active-server-user"), path="/sync/")
        view = _bind_api(DeviceModuleTableView(), request, live_librenms)
        view._active_server_key = "secondary"

        table = view.get_table([], device)

        assert table.server_key == "secondary"
        assert table.htmx_url == "/sync/?tab=modules&server_key=secondary"

    def test_read_only_user_gets_no_module_mutation_flags(self, live_librenms):
        device = make_device("device-module-read-only")
        user = make_user_with_perms("device-module-read-only-user", [], plugin_write=False)
        request = make_request("get", user=user, path="/sync/")
        view = _bind_api(DeviceModuleTableView(), request, live_librenms)

        table = view.get_table([], device)

        assert table.has_write_permission is False
        assert table.can_add_module is False
        assert table.can_change_module is False
        assert table.can_delete_module is False


@pytest.mark.django_db
class TestSingleInterfaceVerifyEndpoint:
    def test_missing_device_id_returns_structured_400(self, client, live_librenms):
        _login(client, "verify-interface-missing-device-user")

        response = _json_post(
            client,
            "verify_interface",
            {"port_id": 4001, "server_key": SERVER_KEY},
        )

        assert response.status_code == 400
        assert response.json() == {"status": "error", "message": "No device ID provided"}

    def test_malformed_cache_returns_controlled_404_and_purges_it(self, client, live_librenms):
        device = make_device("verify-interface-malformed", librenms_cf={SERVER_KEY: {"id": 141}})
        view = SingleInterfaceVerifyView()
        key = view.get_cache_key(device, "ports", SERVER_KEY)
        cache.set(key, {"ports": "not-a-list"}, timeout=300)
        _login(client, "verify-interface-malformed-user")

        response = _json_post(
            client,
            "verify_interface",
            {"device_id": device.pk, "port_id": 4001, "server_key": SERVER_KEY},
        )

        assert response.status_code == 404
        assert response.json() == {"status": "error", "message": "Interface data not found"}
        assert cache.get(key) is None

    def test_cached_port_returns_a_formatted_real_row(self, client, live_librenms):
        device = make_device("verify-interface-success", librenms_cf={SERVER_KEY: {"id": 142}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_id(interface, 4001)
        view = SingleInterfaceVerifyView()
        cache.set(
            view.get_cache_key(device, "ports", SERVER_KEY),
            {"ports": [_port()], "port_stack_relationships": {}},
            timeout=300,
        )
        _login(client, "verify-interface-success-user")

        response = _json_post(
            client,
            "verify_interface",
            {
                "device_id": device.pk,
                "port_id": 4001,
                "server_key": SERVER_KEY,
                "interface_name_field": "ifName",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "Ethernet1" in data["formatted_row"]["name"]

    def test_duplicate_cached_port_id_is_rejected_as_ambiguous(self, client, live_librenms):
        device = make_device("verify-interface-duplicate", librenms_cf={SERVER_KEY: {"id": 143}})
        view = SingleInterfaceVerifyView()
        cache.set(
            view.get_cache_key(device, "ports", SERVER_KEY),
            {
                "ports": [_port(name="Ethernet1"), _port(name="Ethernet2")],
                "port_stack_relationships": {},
            },
            timeout=300,
        )
        _login(client, "verify-interface-duplicate-user")

        response = _json_post(
            client,
            "verify_interface",
            {"device_id": device.pk, "port_id": 4001, "server_key": SERVER_KEY},
        )

        assert response.status_code == 404
        assert response.json()["message"] == "Interface data is ambiguous. Refresh and retry."

    def test_user_without_device_view_permission_is_rejected_before_lookup(self, client, live_librenms):
        client.force_login(make_user_with_perms("verify-interface-denied-user", []))

        response = _json_post(
            client,
            "verify_interface",
            {"device_id": 999_999_999, "port_id": 4001, "server_key": SERVER_KEY},
        )

        assert response.status_code == 403
        assert "view_device" in response.json()["error"]


@pytest.mark.django_db
class TestVLANVerifyEndpoints:
    def test_vlan_sync_verify_reports_global_name_match(self, client):
        VLAN.objects.create(vid=3201, name="Application", status="active")
        _login(client, "verify-vlan-global-user")

        response = _json_post(
            client,
            "verify_vlan_sync_group",
            {"vid": 3201, "name": "Application", "vlan_group_id": ""},
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "success",
            "exists_in_netbox": True,
            "name_matches": True,
            "css_class": "text-success",
            "netbox_vlan_name": "Application",
            "status_html": '<span class="text-success"><i class="mdi mdi-check-circle"></i> Synced</span>',
        }

    def test_vlan_sync_verify_scopes_lookup_to_selected_group(self, client):
        first = VLANGroup.objects.create(name="First Group", slug="first-group")
        second = VLANGroup.objects.create(name="Second Group", slug="second-group")
        VLAN.objects.create(vid=3202, name="Application", group=first, status="active")
        _login(client, "verify-vlan-group-user")

        response = _json_post(
            client,
            "verify_vlan_sync_group",
            {"vid": 3202, "name": "Application", "vlan_group_id": second.pk},
        )

        assert response.status_code == 200
        assert response.json()["exists_in_netbox"] is False
        assert response.json()["netbox_vlan_name"] is None

    def test_interface_vlan_verify_compares_real_untagged_assignment(self, client):
        device = make_device("verify-interface-vlan")
        interface = make_interface(device, "Ethernet1")
        group = VLANGroup.objects.create(name="Interface Group", slug="interface-group")
        vlan = VLAN.objects.create(vid=3203, name="Users", group=group, status="active")
        interface.mode = "access"
        interface.untagged_vlan = vlan
        interface.save(update_fields=["mode", "untagged_vlan"])
        _login(client, "verify-interface-vlan-user")

        response = _json_post(
            client,
            "verify_vlan_group",
            {
                "device_id": device.pk,
                "interface_name": interface.name,
                "vid": 3203,
                "vlan_group_id": group.pk,
                "vlan_type": "U",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["is_missing"] is False
        assert data["css_class"] == "text-success"

    @pytest.mark.parametrize(
        ("route_name", "body", "message"),
        [
            ("verify_vlan_sync_group", {"name": "Missing"}, "No VID provided"),
            ("verify_vlan_sync_group", {"vid": "not-a-vid"}, "Invalid VID"),
        ],
    )
    def test_invalid_vlan_verify_payloads_return_structured_400(self, client, route_name, body, message):
        _login(client, f"{route_name}-{message}-user")

        response = _json_post(client, route_name, body)

        assert response.status_code == 400
        assert response.json() == {"status": "error", "message": message}

    def test_invalid_interface_vlan_group_id_returns_structured_400(self, client):
        device = make_device("verify-interface-vlan-invalid-group")
        _login(client, "verify-interface-vlan-invalid-group-user")

        response = _json_post(
            client,
            "verify_vlan_group",
            {"device_id": device.pk, "vid": 3204, "vlan_group_id": "not-an-id"},
        )

        assert response.status_code == 400
        assert response.json() == {"status": "error", "message": "Invalid VLAN group ID"}


@pytest.mark.django_db
class TestSaveVLANGroupOverridesEndpoint:
    def test_real_cache_ttl_is_reused_for_saved_overrides(self, client, live_librenms):
        device = make_device("save-vlan-overrides", librenms_cf={SERVER_KEY: {"id": 151}})
        view = SaveVlanGroupOverridesView()
        ports_key = view.get_cache_key(device, "ports", SERVER_KEY)
        overrides_key = view.get_vlan_overrides_key(device, SERVER_KEY)
        cache.set(ports_key, {"ports": [_port()]}, timeout=300)
        _login(client, "save-vlan-overrides-user")

        response = _json_post(
            client,
            "save_vlan_group_overrides",
            {"device_id": device.pk, "server_key": SERVER_KEY, "vid_group_map": {"3205": "17"}},
        )

        assert response.status_code == 200
        assert response.json() == {"status": "success"}
        assert cache.get(overrides_key) == {"3205": "17"}
        ports_ttl = cache.ttl(ports_key)
        overrides_ttl = cache.ttl(overrides_key)
        assert ports_ttl > 0
        assert overrides_ttl > 0
        assert abs(overrides_ttl - ports_ttl) <= 2

    def test_missing_ports_snapshot_returns_400_without_saving(self, client, live_librenms):
        device = make_device("save-vlan-overrides-miss", librenms_cf={SERVER_KEY: {"id": 152}})
        view = SaveVlanGroupOverridesView()
        overrides_key = view.get_vlan_overrides_key(device, SERVER_KEY)
        _login(client, "save-vlan-overrides-miss-user")

        response = _json_post(
            client,
            "save_vlan_group_overrides",
            {"device_id": device.pk, "server_key": SERVER_KEY, "vid_group_map": {"3206": "18"}},
        )

        assert response.status_code == 400
        assert response.json()["message"] == "No cached port data; refresh interfaces first"
        assert cache.get(overrides_key) is None

    def test_a_user_without_plugin_view_is_denied_before_the_view_runs(self, client, live_librenms):
        """
        LibreNMSPermissionMixin denies in dispatch(), so the view body never runs.

        make_user_with_perms(plugin_write=False) grants neither plugin view nor plugin
        change, and the mixin requires plugin view. The view's own
        require_write_permission_json() gate is therefore NOT what answers here: that gate
        is covered by test_vlan_verify_endpoints.TestSaveVlanGroupOverridesBranches, whose
        user holds plugin view but not plugin change.
        """
        from django.core.cache import cache

        from netbox_librenms_plugin.views.object_sync.devices import SaveVlanGroupOverridesView

        device = make_device("save-vlan-overrides-denied", librenms_cf={SERVER_KEY: {"id": 153}})
        overrides_key = SaveVlanGroupOverridesView().get_vlan_overrides_key(device, SERVER_KEY)
        cache.delete(overrides_key)
        client.force_login(
            make_user_with_perms(
                "save-vlan-overrides-denied-user",
                [("view", type(device))],
                plugin_write=False,
            )
        )

        response = _json_post(
            client,
            "save_vlan_group_overrides",
            {"device_id": device.pk, "server_key": SERVER_KEY, "vid_group_map": {"3207": "19"}},
        )

        assert response.status_code == 403
        assert cache.get(overrides_key) is None


@pytest.mark.django_db
def test_superuser_helper_reactivates_an_existing_user():
    from django.contrib.auth import get_user_model

    get_user_model().objects.filter(is_superuser=True, is_active=True).delete()
    user = get_user_model().objects.create(
        username="reactivated-superuser",
        is_active=False,
        is_superuser=False,
    )

    restored = make_superuser("reactivated-superuser")

    assert restored.pk == user.pk
    assert restored.is_active is True
    assert restored.is_superuser is True
