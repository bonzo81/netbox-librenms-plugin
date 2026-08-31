"""Real HTTP and ORM tests for sync-page identity matching."""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip, make_virtual_chassis


pytestmark = pytest.mark.django_db


def _register_device_info(live_librenms, device_info, *, status=200):
    body = {"status": "ok", "devices": [device_info]} if status == 200 else {"status": "error"}
    live_librenms.server.register(
        "/api/v0/devices/42",
        body,
        status=status,
        method="GET",
    )


def _netbox_device(tag, *, name=None, primary_ip="198.18.0.1", dns_name=""):
    device = make_device(f"device-{tag}"[:64])
    if name is not None:
        device.name = name
        device.save(update_fields=["name"])
    if primary_ip:
        interface = make_interface(device, f"management-{device.pk}")
        address = make_ip(f"{primary_ip}/24", assigned_object=interface)
        address.dns_name = dns_name
        address.save(update_fields=["dns_name"])
        device.primary_ip4 = address
        device.save(update_fields=["primary_ip4"])
        device.refresh_from_db()
    return device


def _device_info(view, device):
    return view.get_librenms_device_info(device)


def _view(live_librenms, librenms_id=42):
    from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

    view = object.__new__(BaseLibreNMSSyncView)
    view.librenms_id = librenms_id
    view._librenms_api = live_librenms.api
    return view


class TestMismatchDetection:
    def test_missing_mapping_does_not_contact_librenms(self, live_librenms):
        result = _device_info(_view(live_librenms, None), _netbox_device("missing"))

        assert result["found_in_librenms"] is False
        assert result["mismatched_device"] is False
        assert live_librenms.server.requests == []

    def test_failed_real_http_lookup_is_not_reported_as_a_match(self, live_librenms):
        _register_device_info(live_librenms, {}, status=500)

        result = _device_info(_view(live_librenms), _netbox_device("failure"))

        assert result["found_in_librenms"] is False
        assert result["mismatched_device"] is False
        assert live_librenms.server.requests[0]["path"] == "/api/v0/devices/42"

    @pytest.mark.parametrize(
        ("netbox_name", "netbox_ip", "dns_name", "librenms", "mismatch"),
        [
            ("switch-01", "198.18.0.1", "", {"sysName": "SWITCH-01", "ip": "198.18.0.2"}, False),
            (
                "switch-01",
                "198.18.0.1",
                "",
                {"sysName": "different", "hostname": "switch-01", "ip": "198.18.0.2"},
                False,
            ),
            ("switch-01.example", "198.18.0.1", "", {"sysName": "switch-01.example", "ip": "198.18.0.2"}, False),
            ("switch-01", "198.18.0.1", "", {"sysName": "different", "ip": "198.18.0.1"}, False),
            (
                "switch-01",
                "198.18.0.1",
                "",
                {"sysName": "different", "hostname": "198.18.0.1", "ip": "198.18.0.2"},
                False,
            ),
            (
                "switch-01",
                "198.18.0.1",
                "switch-01.example",
                {"sysName": "switch-01.example", "ip": "198.18.0.2"},
                False,
            ),
            (
                "switch-01",
                "198.18.0.1",
                "switch-01.example",
                {"sysName": "other", "hostname": "switch-01.example", "ip": "198.18.0.2"},
                False,
            ),
            (
                "switch-05",
                "198.18.0.1",
                "switch-05.example",
                {"sysName": "router-01", "hostname": "router-01.example", "ip": "198.18.0.2"},
                True,
            ),
            ("switch-01", "198.18.0.1", "", {"sysName": "switch-01.example", "ip": "198.18.0.2"}, False),
            (
                "switch-01.example",
                "198.18.0.1",
                "",
                {"sysName": "switch-01.other", "ip": "198.18.0.2"},
                True,
            ),
            ("switch-01", "198.18.0.1", "", {"sysName": None, "hostname": None, "ip": "198.18.0.2"}, True),
            ("switch-01", None, "", {"sysName": None, "hostname": None, "ip": None}, True),
            ("switch-01", "198.18.0.1", "", {"sysName": "router-01.example", "ip": "198.18.0.2"}, True),
        ],
    )
    def test_identity_matching_uses_real_device_and_http_response(
        self,
        live_librenms,
        netbox_name,
        netbox_ip,
        dns_name,
        librenms,
        mismatch,
        request,
    ):
        _register_device_info(
            live_librenms,
            {
                "device_id": 42,
                "hardware": "-",
                "serial": "",
                "os": "linux",
                "version": "1",
                "features": "-",
                "location": "Lab",
                **librenms,
            },
        )
        device = _netbox_device(request.node.callspec.id, name=netbox_name, primary_ip=netbox_ip, dns_name=dns_name)

        result = _device_info(_view(live_librenms), device)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is mismatch

    def test_parenthesized_virtual_chassis_suffix_is_ignored(self, live_librenms):
        _register_device_info(live_librenms, {"device_id": 42, "sysName": "switch-01", "ip": "198.18.0.2"})
        live_librenms.server.inventory_response(42, [])
        device = _netbox_device("parenthesized", name="switch-01 (1)")
        make_virtual_chassis("mismatch-parenthesized", device)

        result = _device_info(_view(live_librenms), device)

        assert result["mismatched_device"] is False

    @pytest.mark.parametrize(
        ("pattern", "netbox_name", "librenms_name", "mismatch"),
        [
            ("-M{position}", "switch-01-M2", "switch-01", False),
            ("-SW{position}", "switch-01-SW3", "switch-01", False),
            ("-M{position}", "switch-99", "switch-01", True),
        ],
    )
    def test_database_backed_vc_name_pattern(self, live_librenms, pattern, netbox_name, librenms_name, mismatch):
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"vc_member_name_pattern": pattern})
        _register_device_info(live_librenms, {"device_id": 42, "sysName": librenms_name, "ip": "198.18.0.2"})

        result = _device_info(_view(live_librenms), _netbox_device(pattern, name=netbox_name))

        assert result["mismatched_device"] is mismatch


class TestBuildAllServerMappings:
    @staticmethod
    def _mappings(device, active="production"):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        return BaseLibreNMSSyncView._build_all_server_mappings(device, active)

    @pytest.mark.parametrize("stored", [42, None])
    def test_legacy_or_missing_mapping_has_no_server_rows(self, stored):
        device = _netbox_device(f"legacy-{stored}")
        device.custom_field_data["librenms_id"] = stored

        assert self._mappings(device) is None

    def test_host_id_is_extracted_from_the_nested_mapping(self, configure_librenms):
        configure_librenms({"production": {"librenms_url": "https://production.example", "api_token": "token"}})
        device = _netbox_device("nested")
        device.custom_field_data["librenms_id"] = {"production": {"id": 42, "oob": {"id": 99, "type": "controller"}}}

        assert self._mappings(device)[0]["device_id"] == 42

    def test_migrated_only_mapping_has_no_server_row(self, configure_librenms):
        configure_librenms({"production": {"librenms_url": "https://production.example", "api_token": "token"}})
        device = _netbox_device("migrated")
        device.custom_field_data["librenms_id"] = {
            "production": {"_migrated_to": {"device_id": 7, "server_key": "production"}}
        }

        assert self._mappings(device) is None

    def test_configured_active_mapping_contains_navigation_metadata(self, configure_librenms):
        configure_librenms(
            {
                "production": {
                    "display_name": "Production LibreNMS",
                    "librenms_url": "https://production.example",
                    "api_token": "token",
                }
            }
        )
        device = _netbox_device("configured")
        device.custom_field_data["librenms_id"] = {"production": " 42 "}

        mapping = self._mappings(device)[0]

        assert mapping["server_key"] == "production"
        assert mapping["device_id"] == 42
        assert mapping["display_name"] == "Production LibreNMS"
        assert mapping["is_configured"] is True
        assert mapping["is_active"] is True
        assert mapping["device_url"] == "https://production.example/device/device=42/"

    def test_invalid_float_is_dropped_and_orphan_is_not_selectable(self, configure_librenms):
        configure_librenms({"production": {"librenms_url": "https://production.example", "api_token": "token"}})
        device = _netbox_device("invalid")
        device.custom_field_data["librenms_id"] = {
            "production": 42,
            "staging": 1.9,
            "deleted-server": 77,
        }

        mappings = self._mappings(device)

        assert [mapping["server_key"] for mapping in mappings] == ["production", "deleted-server"]
        assert mappings[1]["is_configured"] is False
        assert mappings[1]["is_active"] is False
        assert mappings[1]["device_url"] is None

    def test_active_then_configured_then_orphaned_sort_order(self, configure_librenms):
        configure_librenms(
            {
                "production": {"librenms_url": "https://production.example", "api_token": "token"},
                "development": {"librenms_url": "https://development.example", "api_token": "token"},
            }
        )
        device = _netbox_device("sort")
        device.custom_field_data["librenms_id"] = {
            "development": 99,
            "production": 42,
            "old-server": 11,
        }

        assert [mapping["server_key"] for mapping in self._mappings(device)] == [
            "production",
            "development",
            "old-server",
        ]


class TestVirtualChassisLookup:
    def test_explicit_server_mapping_on_another_member_wins_over_legacy_id(self):
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        viewed = _netbox_device("vc-viewed")
        sync_member = _netbox_device("vc-sync")
        make_virtual_chassis("mismatch-lookup", viewed, sync_member)
        viewed.custom_field_data["librenms_id"] = 41
        viewed.save(update_fields=["custom_field_data"])
        sync_member.custom_field_data["librenms_id"] = {"default": 42}
        sync_member.save(update_fields=["custom_field_data"])

        assert get_librenms_sync_device(viewed, server_key="default") == sync_member

    def test_non_vc_device_is_its_own_lookup_device(self):
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = _netbox_device("standalone")

        assert get_librenms_sync_device(device, server_key="default") == device
