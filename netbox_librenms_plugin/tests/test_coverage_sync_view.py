"""Integration coverage for the shared LibreNMS object sync page."""

from copy import deepcopy

import pytest
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
)
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


SERVER_KEY = "default"
SECONDARY_KEY = "secondary"


def _configure_servers(settings, server, *keys):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        key: {
            "display_name": f"{key.title()} sync server",
            "librenms_url": server.url,
            "api_token": f"{key}-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        }
        for key in (keys or (SERVER_KEY, SECONDARY_KEY))
    }
    plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def librenms_server(settings):
    with librenms_mock_server() as server:
        _configure_servers(settings, server)
        yield server


@pytest.fixture
def logged_in_client(client):
    client.force_login(make_superuser("sync-view-superuser"))
    return client


def _sync_url(obj):
    name = "vm_librenms_sync" if obj._meta.model_name == "virtualmachine" else "device_librenms_sync"
    return reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs={"pk": obj.pk})


def _register_device(server, device_id, name, *, inventory=None, **overrides):
    payload = {
        "device_id": device_id,
        "hostname": name,
        "sysName": name,
        "hardware": "TestDT",
        "serial": "SYNC-SERIAL",
        "os": "linux",
        "ip": "198.18.50.1",
        "version": "1.0",
        "features": "-",
        "location": "TestSite",
    }
    payload.update(overrides)
    server.register(
        f"/api/v0/devices/{device_id}",
        {"status": "ok", "devices": [payload]},
    )
    inventory = inventory or []
    server.register(
        f"/api/v0/inventory/{device_id}/all",
        {"status": "ok", "inventory": inventory},
    )
    server.register(
        f"/api/v0/inventory/{device_id}",
        {"status": "ok", "inventory": inventory},
    )
    server.register(f"/api/v0/devices/{device_id}/ports", {"status": "ok", "ports": []})
    server.register(f"/api/v0/devices/{device_id}/links", {"status": "ok", "links": []})
    server.register(f"/api/v0/devices/{device_id}/ip", {"status": "ok", "addresses": []})
    server.register(f"/api/v0/devices/{device_id}/transceivers", {"status": "ok", "transceivers": []})
    return payload


def _device_view(server_key=SERVER_KEY):
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

    view = DeviceLibreNMSSyncView()
    view._librenms_api = LibreNMSAPI(server_key)
    return view


@pytest.mark.django_db
class TestSyncPageRouting:
    def test_non_vc_device_uses_its_real_mapping_and_live_details(self, logged_in_client, librenms_server):
        device = make_device("sync-page-device", librenms_cf={SERVER_KEY: 6601})
        _register_device(
            librenms_server,
            6601,
            device.name,
            hardware=device.device_type.model,
            location=device.site.name,
            serial="PAGE-SERIAL",
        )

        response = logged_in_client.get(_sync_url(device))

        assert response.status_code == 200
        assert response.context["lookup_device_pk"] == device.pk
        assert response.context["librenms_device_id"] == 6601
        assert response.context["found_in_librenms"] is True
        assert response.context["librenms_device_serial"] == "PAGE-SERIAL"
        assert response.context["mismatched_device"] is False

    def test_requested_server_rebinds_header_tabs_and_refresh_links(self, logged_in_client, librenms_server):
        device = make_device(
            "sync-secondary-device",
            librenms_cf={SERVER_KEY: 6602, SECONDARY_KEY: 6603},
        )
        _register_device(librenms_server, 6603, device.name)

        response = logged_in_client.get(_sync_url(device), {"server_key": SECONDARY_KEY})

        assert response.status_code == 200
        assert response.context["server_key"] == SECONDARY_KEY
        assert response.context["librenms_server_info"]["server_key"] == SECONDARY_KEY
        assert response.context["librenms_device_id"] == 6603
        assert f"server_key={SECONDARY_KEY}" in response.content.decode()

    def test_stale_requested_server_renders_a_fail_closed_selection_shell(
        self,
        logged_in_client,
        librenms_server,
    ):
        _vc, members = make_virtual_chassis_members("sync-stale", count=2)
        viewed, mapped = members
        mapped.custom_field_data["librenms_id"] = {SERVER_KEY: 6604}
        mapped.save()

        response = logged_in_client.get(_sync_url(viewed), {"server_key": "retired"})

        assert response.status_code == 200
        assert response.context["server_selection_blocked"] is True
        assert response.context["server_key"] == "retired"
        assert response.context["lookup_device_pk"] == mapped.pk
        assert response.context["has_librenms_id"] is False
        assert [mapping.server_key for mapping in response.context["all_server_mappings"]] == [SERVER_KEY]

    def test_vc_member_without_mapping_delegates_to_the_linked_member(
        self,
        logged_in_client,
        librenms_server,
    ):
        _vc, members = make_virtual_chassis_members("sync-vc-delegate", count=2)
        linked, viewed = members
        linked.custom_field_data["librenms_id"] = {SERVER_KEY: 6605}
        linked.save()
        _register_device(librenms_server, 6605, linked.name)

        response = logged_in_client.get(_sync_url(viewed))

        assert response.status_code == 200
        assert response.context["lookup_device_pk"] == linked.pk
        assert response.context["librenms_sync_device"] == linked
        assert response.context["sync_device_has_librenms_id"] is True

    def test_vc_member_with_own_mapping_remains_the_lookup_device(self, logged_in_client, librenms_server):
        _vc, members = make_virtual_chassis_members("sync-vc-own", count=2)
        viewed = members[1]
        viewed.custom_field_data["librenms_id"] = {SERVER_KEY: 6606}
        viewed.save()
        _register_device(librenms_server, 6606, viewed.name)

        response = logged_in_client.get(_sync_url(viewed))

        assert response.context["lookup_device_pk"] == viewed.pk
        assert response.context["librenms_device_id"] == 6606

    def test_vc_context_reports_the_linked_members_primary_ip(self, logged_in_client, librenms_server):
        _vc, members = make_virtual_chassis_members("sync-vc-ip", count=2)
        linked, viewed = members
        linked.custom_field_data["librenms_id"] = {SERVER_KEY: 6607}
        interface = make_interface(linked, "management")
        primary_ip = make_ip("198.18.50.7/32", assigned_object=interface)
        linked.primary_ip4 = primary_ip
        linked.save()
        _register_device(librenms_server, 6607, linked.name, ip="198.18.50.7")

        response = logged_in_client.get(_sync_url(viewed))

        assert response.context["is_vc_member"] is True
        assert response.context["sync_device_has_primary_ip"] is True

    def test_vm_page_uses_the_vm_mapping_and_vm_tabs(self, logged_in_client, librenms_server):
        vm = make_vm("sync-page-vm")
        vm.custom_field_data["librenms_id"] = {SERVER_KEY: 6608}
        vm.save()
        _register_device(librenms_server, 6608, vm.name)

        response = logged_in_client.get(_sync_url(vm))

        assert response.status_code == 200
        assert response.context["object_model_name"] == "virtualmachine"
        assert response.context["lookup_device_model_name"] == "virtualmachine"
        assert response.context["librenms_device_id"] == 6608

    def test_all_applicable_tabs_and_active_tab_are_exposed(self, logged_in_client, librenms_server):
        device = make_device("sync-tab-device", librenms_cf={SERVER_KEY: 6609})
        _register_device(librenms_server, 6609, device.name)

        response = logged_in_client.get(_sync_url(device), {"tab": "cables"})

        assert response.context["active_sync_tab"] == "cables"
        assert response.context["interface_name_selector_visible"] is True
        assert {"interfaces", "cables", "ipaddresses", "vlans", "modules"}.issubset(response.context["sync_tab_urls"])
        assert response.context["module_sync"] is not None

    def test_invalid_tab_falls_back_to_interfaces(self, logged_in_client, librenms_server):
        device = make_device("sync-invalid-tab", librenms_cf={SERVER_KEY: 6610})
        _register_device(librenms_server, 6610, device.name)

        response = logged_in_client.get(_sync_url(device), {"tab": "not-a-tab"})

        assert response.context["active_sync_tab"] == "interfaces"

    def test_bare_integer_mapping_is_marked_as_legacy(self, logged_in_client, settings, librenms_server):
        _configure_servers(settings, librenms_server, SERVER_KEY)
        device = make_device("sync-legacy", serial="LEGACY-SERIAL", librenms_cf=6611)
        _register_device(librenms_server, 6611, device.name, serial=device.serial)

        response = logged_in_client.get(_sync_url(device))

        assert response.context["librenms_id_is_legacy"] is True
        assert response.context["librenms_id_serial_confirmed"] is True


@pytest.mark.django_db
class TestServerMappingContext:
    def _mappings(self, obj, active=SERVER_KEY):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        return BaseLibreNMSSyncView._build_all_server_mappings(obj, active)

    @pytest.mark.parametrize("value", [None, 42, "42", [], True, {}])
    def test_non_scoped_or_empty_values_have_no_mapping_list(self, librenms_server, value):
        device = make_device(f"mapping-none-{type(value).__name__}")
        device.custom_field_data["librenms_id"] = value
        device.save()

        assert self._mappings(device) is None

    def test_scoped_entries_include_urls_display_names_and_active_state(self, librenms_server):
        device = make_device(
            "mapping-context",
            librenms_cf={SERVER_KEY: "6621", SECONDARY_KEY: {"id": 6622}},
        )

        mappings = self._mappings(device, SECONDARY_KEY)

        assert [mapping["server_key"] for mapping in mappings] == [SECONDARY_KEY, SERVER_KEY]
        assert [mapping["device_id"] for mapping in mappings] == [6622, 6621]
        assert mappings[0]["display_name"] == "Secondary sync server"
        assert mappings[0]["device_url"].endswith("/device/device=6622/")
        assert mappings[0]["is_active"] is True
        assert mappings[1]["is_active"] is False
        assert all(mapping["is_configured"] for mapping in mappings)

    def test_oob_only_mapping_is_surfaced_without_a_host_id(self, librenms_server):
        device = make_device(
            "mapping-oob-only",
            librenms_cf={SERVER_KEY: {"id": "invalid", "oob": {"id": "6623", "type": "idrac"}}},
        )

        mappings = self._mappings(device)

        assert mappings[0]["device_id"] == 6623
        assert mappings[0]["is_oob_only"] is True

    def test_invalid_mapping_entries_are_omitted(self, librenms_server):
        device = make_device(
            "mapping-invalid-entries",
            librenms_cf={SERVER_KEY: True, SECONDARY_KEY: "not-an-id", "retired": None},
        )

        assert self._mappings(device) is None

    def test_unconfigured_mapping_remains_visible_but_not_selectable(self, librenms_server):
        device = make_device("mapping-retired", librenms_cf={"retired": 6624})

        mappings = self._mappings(device, "retired")

        assert mappings[0]["server_key"] == "retired"
        assert mappings[0]["is_configured"] is False
        assert mappings[0]["is_selectable"] is False
        assert mappings[0]["device_url"] is None


@pytest.mark.django_db
class TestLibreNMSDeviceInfo:
    def test_absent_id_returns_the_default_not_found_contract(self, librenms_server):
        device = make_device("info-without-id")
        view = _device_view()
        view.librenms_id = None

        result = view.get_librenms_device_info(device)

        assert result["found_in_librenms"] is False
        assert result["device_info_unavailable"] is False
        assert result["mismatched_device"] is False
        assert result["librenms_device_details"]["librenms_device_hardware"] == "-"

    def test_real_live_info_populates_details_and_matches_short_hostname(self, librenms_server):
        device = make_device("info-matched")
        _register_device(
            librenms_server,
            6631,
            "info-matched.example.test",
            hardware=device.device_type.model,
            serial="INFO-SERIAL",
            os="info-os",
            version="2.0",
            location=device.site.name,
        )
        view = _device_view()
        view.librenms_id = 6631

        result = view.get_librenms_device_info(device, RequestFactory().get("/"))

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False
        details = result["librenms_device_details"]
        assert details["librenms_device_hardware"] == device.device_type.model
        assert details["librenms_device_serial"] == "INFO-SERIAL"
        assert details["librenms_device_os"] == "info-os"
        assert details["librenms_device_version"] == "2.0"

    def test_unrelated_names_and_addresses_are_mismatched(self, librenms_server):
        device = make_device("netbox-unrelated-name")
        _register_device(
            librenms_server,
            6632,
            "librenms-unrelated-name",
            ip="198.18.50.32",
        )
        view = _device_view()
        view.librenms_id = 6632

        result = view.get_librenms_device_info(device)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    def test_primary_ip_dns_name_can_establish_identity(self, librenms_server):
        device = make_device("different-netbox-name")
        interface = make_interface(device, "management")
        primary_ip = make_ip("198.18.50.33/32", assigned_object=interface)
        primary_ip.dns_name = "dns-identity.example.test"
        primary_ip.save()
        device.primary_ip4 = primary_ip
        device.save()
        device.refresh_from_db()
        _register_device(librenms_server, 6633, "dns-identity.example.test", ip="198.18.50.99")
        view = _device_view()
        view.librenms_id = 6633

        result = view.get_librenms_device_info(device)

        assert result["mismatched_device"] is False
        assert result["librenms_device_details"]["netbox_dns_name"] == "dns-identity.example.test"


@pytest.mark.django_db
class TestVirtualChassisInventory:
    def test_real_inventory_links_chassis_serials_to_members(self, librenms_server):
        _vc, members = make_virtual_chassis_members("inventory-members", count=2)
        members[0].serial = "VC-SERIAL-A"
        members[0].save()
        inventory = [
            {
                "entPhysicalClass": "chassis",
                "entPhysicalDescr": "First member",
                "entPhysicalSerialNum": " VC-SERIAL-A ",
                "entPhysicalModelName": "Member model A",
            },
            {
                "entPhysicalClass": "chassis",
                "entPhysicalDescr": "Unassigned member",
                "entPhysicalSerialNum": "VC-SERIAL-B",
                "entPhysicalModelName": "Member model B",
            },
            {
                "entPhysicalClass": "module",
                "entPhysicalSerialNum": "IGNORED-MODULE",
            },
            {
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "-",
            },
        ]
        _register_device(librenms_server, 6641, members[0].name, inventory=inventory)
        view = _device_view()
        view.librenms_id = 6641

        result = view._get_vc_inventory_serials(members[0])

        assert len(result) == 2
        assert result[0]["assigned_member"] == members[0]
        assert result[0]["model"] == "Member model A"
        assert result[1]["assigned_member"] is None

    def test_failed_inventory_lookup_returns_an_empty_list(self, librenms_server):
        _vc, members = make_virtual_chassis_members("inventory-failure", count=1)
        librenms_server.register("/api/v0/inventory/6642/all", {"status": "error"}, status=404)
        view = _device_view()
        view.librenms_id = 6642

        assert view._get_vc_inventory_serials(members[0]) == []


@pytest.mark.django_db
class TestPlatformAndPatternHelpers:
    def test_platform_info_uses_an_exact_real_platform(self, librenms_server):
        from dcim.models import Platform

        device = make_device("platform-info")
        platform = Platform.objects.create(name="SyncOS", slug="syncos")
        details = {
            "librenms_device_details": {
                "librenms_device_os": "SyncOS",
                "librenms_device_version": "9.1",
            }
        }

        result = _device_view()._get_platform_info(details, device)

        assert result["platform_exists"] is True
        assert result["matching_platform"] == platform
        assert result["platform_name"] == "SyncOS"
        assert result["librenms_version"] == "9.1"

    def test_missing_platform_name_has_no_match(self, librenms_server):
        device = make_device("platform-info-missing")
        details = {
            "librenms_device_details": {
                "librenms_device_os": "-",
                "librenms_device_version": "-",
            }
        }

        result = _device_view()._get_platform_info(details, device)

        assert result["platform_exists"] is False
        assert result["matching_platform"] is None
        assert result["platform_name"] is None

    def test_vc_pattern_strips_configured_suffixes(self, librenms_server):
        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        settings_row = LibreNMSSettings.objects.order_by("pk").first() or LibreNMSSettings()
        settings_row.vc_member_name_pattern = "-STACK{position}"
        settings_row.save()
        assert BaseLibreNMSSyncView._strip_vc_pattern("switch01-stack2") == "switch01"

        settings_row.vc_member_name_pattern = "-{serial}"
        settings_row.save()
        assert BaseLibreNMSSyncView._strip_vc_pattern("switch-SERIAL-12") == "switch"
        assert BaseLibreNMSSyncView._strip_vc_pattern("switch") is None

    def test_base_context_hooks_are_explicitly_empty(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        view = BaseLibreNMSSyncView()
        request = RequestFactory().get("/")
        device = object()

        assert view.get_interface_context(request, device) is None
        assert view.get_cable_context(request, device) is None
        assert view.get_ip_context(request, device) is None
        assert view.get_vlan_context(request, device) is None
        assert view.get_module_context(request, device) is None
