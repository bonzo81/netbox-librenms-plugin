"""
Integration tests for Virtual Chassis detection using the mock LibreNMS HTTP server.

These tests verify that detect_virtual_chassis_from_inventory(), get_virtual_chassis_data(),
and prefetch_vc_data_for_devices() work correctly end-to-end through real HTTP calls
to a local mock server — no mocking of the detection logic itself.

Run:
    python -m pytest netbox_librenms_plugin/tests/test_integration_virtual_chassis.py -v
"""

import pytest
from unittest.mock import patch

from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


@pytest.fixture
def mock_server():
    with librenms_mock_server() as server:
        yield server


def _make_api(url, token="test-token", server_key="test"):
    """Create a LibreNMSAPI instance pointed at the mock server."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    servers_config = {
        server_key: {
            "librenms_url": url,
            "api_token": token,
            "cache_timeout": 300,
            "verify_ssl": False,
        }
    }

    with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_cfg:
        mock_cfg.side_effect = lambda _plugin, key: servers_config if key == "servers" else None
        api = LibreNMSAPI(server_key=server_key)
    return api


def _chassis(index, serial, model="WS-C3750X", name="", descr="", position=None, contained_in=None):
    """Build a minimal ENTITY-MIB chassis entry."""
    item = {
        "entPhysicalIndex": index,
        "entPhysicalClass": "chassis",
        "entPhysicalSerialNum": serial,
        "entPhysicalModelName": model,
        "entPhysicalName": name or f"Chassis-{index}",
        "entPhysicalDescr": descr or f"Chassis {index}",
    }
    if position is not None:
        item["entPhysicalParentRelPos"] = position
    if contained_in is not None:
        item["entPhysicalContainedIn"] = contained_in
    return item


def _stack_root(index=1):
    """Build a 'stack' class root entry (e.g., Cisco StackWise)."""
    return {
        "entPhysicalIndex": index,
        "entPhysicalClass": "stack",
        "entPhysicalSerialNum": "",
        "entPhysicalModelName": "",
        "entPhysicalName": "StackSub-0/0",
        "entPhysicalDescr": "Cisco StackWise",
        "entPhysicalContainedIn": 0,
    }


class TestDetectVCCiscoStack:
    """Cisco StackWise topology: root has stack-class entry; children are chassis members."""

    def test_three_member_stack(self, mock_server):
        """3 chassis members under a stack root → is_stack=True, member_count=3."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 10

        mock_server.device_info_response(device_id=device_id, hostname="sw-stack", serial="MASTER")

        root_items = [_stack_root(index=1)]
        member_items = [
            _chassis(100, "SN-A", position=1),
            _chassis(200, "SN-B", position=2),
            _chassis(300, "SN-C", position=3),
        ]
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        assert result is not None
        assert result["is_stack"] is True
        assert result["member_count"] == 3
        serials = [m["serial"] for m in result["members"]]
        assert serials == ["SN-A", "SN-B", "SN-C"]

    def test_members_sorted_by_position(self, mock_server):
        """Members returned in position order regardless of API order."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 11

        mock_server.device_info_response(device_id=device_id, hostname="sw-stack-2")
        root_items = [_stack_root(index=5)]
        # Deliberately out of order: 3, 1, 2
        member_items = [
            _chassis(301, "SN-3", position=3),
            _chassis(101, "SN-1", position=1),
            _chassis(201, "SN-2", position=2),
        ]
        mock_server.vc_inventory_callable(device_id, root_items, {5: member_items})

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        assert result is not None
        positions = [m["position"] for m in result["members"]]
        assert positions == [1, 2, 3]
        # Verify serial/position pairs are correctly associated after sorting
        member_pairs = [(m["serial"], m["position"]) for m in result["members"]]
        assert member_pairs == [("SN-1", 1), ("SN-2", 2), ("SN-3", 3)]

    def test_position_zero_falls_back_to_idx_plus_one(self, mock_server):
        """position=0 in entPhysicalParentRelPos → fallback to idx+1 (never 0)."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 12

        mock_server.device_info_response(device_id=device_id, hostname="sw-stack-3")
        root_items = [_stack_root(index=1)]
        member_items = [
            _chassis(100, "SN-X", position=0),  # 0 → fallback to idx+1=1
            _chassis(200, "SN-Y", position=0),  # 0 → fallback to idx+1=2
        ]
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        assert result is not None
        positions = [m["position"] for m in result["members"]]
        # Both had position=0, so they fall back to idx+1: positions [1, 2]
        assert all(p >= 1 for p in positions)
        # Verify each fallback position is uniquely paired with its serial
        member_pairs = [(m["serial"], m["position"]) for m in result["members"]]
        assert member_pairs == [("SN-X", 1), ("SN-Y", 2)]

    def test_member_fields_extracted_correctly(self, mock_server):
        """serial, model, name, description all extracted from chassis entries."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 13

        mock_server.device_info_response(device_id=device_id, hostname="sw-stack-4")
        root_items = [_stack_root(index=1)]
        member_items = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "SERIAL-ABC",
                "entPhysicalModelName": "WS-C3750X-48P",
                "entPhysicalName": "Slot 1",
                "entPhysicalDescr": "48-port PoE switch",
                "entPhysicalParentRelPos": 1,
            },
            {
                "entPhysicalIndex": 200,
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "SERIAL-DEF",
                "entPhysicalModelName": "WS-C3750X-24T",
                "entPhysicalName": "Slot 2",
                "entPhysicalDescr": "24-port switch",
                "entPhysicalParentRelPos": 2,
            },
        ]
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        assert result is not None
        m1, m2 = result["members"]
        assert m1["serial"] == "SERIAL-ABC"
        assert m1["model"] == "WS-C3750X-48P"
        assert m1["name"] == "Slot 1"
        assert m1["description"] == "48-port PoE switch"
        assert m2["serial"] == "SERIAL-DEF"

    def test_suggested_name_uses_master_sysname(self, mock_server):
        """suggested_name generated from master device sysName."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 14

        mock_server.device_info_response(device_id=device_id, hostname="sw-master", serial="MASTER01")
        root_items = [_stack_root(index=1)]
        member_items = [
            _chassis(100, "SN-1", position=1),
            _chassis(200, "SN-2", position=2),
        ]
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        assert result is not None
        # Members should have non-empty suggested names
        for member in result["members"]:
            assert "suggested_name" in member
            assert member["suggested_name"]  # non-empty


class TestDetectVCJuniperStyle:
    """Juniper-style: root has chassis-class entry; children are chassis members."""

    def test_two_member_vc(self, mock_server):
        """2 chassis members under a chassis root → is_stack=True, member_count=2."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 20

        mock_server.device_info_response(device_id=device_id, hostname="vc-switch")
        # Root: a chassis entry (not stack)
        root_items = [
            {
                "entPhysicalIndex": 10,
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "",
                "entPhysicalModelName": "",
                "entPhysicalName": "Virtual Chassis",
                "entPhysicalDescr": "EX4300 Virtual Chassis",
                "entPhysicalContainedIn": 0,
            }
        ]
        member_items = [
            _chassis(100, "JN-SN-1", position=0),  # Juniper uses position=0,1 (1-based after fallback)
            _chassis(200, "JN-SN-2", position=1),
        ]
        mock_server.vc_inventory_callable(device_id, root_items, {10: member_items})

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        assert result is not None
        assert result["is_stack"] is True
        assert result["member_count"] == 2


class TestDetectVCStackPreferredOverChassis:
    """When root has both stack and chassis entries, stack index takes priority."""

    def test_stack_index_used_not_chassis(self, mock_server):
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 30

        mock_server.device_info_response(device_id=device_id, hostname="sw-mixed")
        # Root has BOTH stack (index=5) and chassis (index=6)
        root_items = [
            {
                "entPhysicalIndex": 5,
                "entPhysicalClass": "stack",
                "entPhysicalName": "Stack-0",
                "entPhysicalSerialNum": "",
                "entPhysicalModelName": "",
                "entPhysicalDescr": "",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 6,
                "entPhysicalClass": "chassis",
                "entPhysicalName": "Chassis-0",
                "entPhysicalSerialNum": "",
                "entPhysicalModelName": "",
                "entPhysicalDescr": "",
                "entPhysicalContainedIn": 0,
            },
        ]
        # Stack index=5 has 2 members, chassis index=6 has 0
        children = {
            5: [_chassis(100, "SN-1", position=1), _chassis(200, "SN-2", position=2)],
            6: [],  # chassis has no children
        }
        mock_server.vc_inventory_callable(device_id, root_items, children)

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        # Must have detected 2 members (via stack index), not 0 (via chassis index)
        assert result is not None
        assert result["member_count"] == 2


class TestDetectVCSingleDevice:
    """Non-stack device: only 1 chassis child → returns None."""

    def test_single_chassis_child_returns_none(self, mock_server):
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 40

        mock_server.device_info_response(device_id=device_id, hostname="single-sw")
        root_items = [_stack_root(index=1)]
        # Only 1 chassis child → not a VC
        member_items = [_chassis(100, "SN-ONLY", position=1)]
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        assert result is None

    def test_no_stack_or_chassis_root_returns_none(self, mock_server):
        """Root has only non-stack/chassis entries → returns None."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 41

        mock_server.device_info_response(device_id=device_id, hostname="plain-router")
        root_items = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalClass": "module",
                "entPhysicalName": "Main Module",
                "entPhysicalSerialNum": "SN1",
                "entPhysicalModelName": "ASR1001-X",
                "entPhysicalDescr": "ASR1001-X",
                "entPhysicalContainedIn": 0,
            }
        ]
        # Register root-only, no children needed
        mock_server.vc_inventory_callable(device_id, root_items, {})

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        assert result is None


class TestDetectVCEdgeCases:
    """API errors and empty responses."""

    def test_empty_root_inventory_returns_none(self, mock_server):
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 50

        mock_server.device_info_response(device_id=device_id, hostname="empty-sw")
        mock_server.vc_inventory_callable(device_id, [], {})  # empty root

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        assert result is None

    def test_api_error_on_root_returns_none(self, mock_server):
        """500 error on root inventory → returns None."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 51

        mock_server.device_info_response(device_id=device_id, hostname="error-sw")
        # Register 500 for inventory calls
        mock_server.register(f"/api/v0/inventory/{device_id}", {"status": "error"}, status=500)

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        assert result is None

    def test_empty_serial_included_in_members(self, mock_server):
        """Members with empty entPhysicalSerialNum are included, not skipped."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 52

        mock_server.device_info_response(device_id=device_id, hostname="nosn-sw")
        root_items = [_stack_root(index=1)]
        member_items = [
            _chassis(100, "", position=1),  # empty serial
            _chassis(200, "SN-B", position=2),
        ]
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        assert result is not None
        assert result["member_count"] == 2
        assert result["members"][0]["serial"] == ""  # empty is preserved

    def test_device_info_failure_still_detects_vc(self, mock_server):
        """get_device_info() returning False → detection still works, suggested_name uses fallback."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        api = _make_api(mock_server.url)
        device_id = 53

        # No device_info registered → 404
        root_items = [_stack_root(index=1)]
        member_items = [
            _chassis(100, "SN-1", position=1),
            _chassis(200, "SN-2", position=2),
        ]
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="{master}-m{position}",
        ):
            result = detect_virtual_chassis_from_inventory(api, device_id)

        # Should still detect VC even without device info
        assert result is not None
        assert result["member_count"] == 2
        # Without master name, suggested_name falls back to "Member-{position}"
        for member in result["members"]:
            assert member["suggested_name"].startswith("Member-")


class TestGetVCDataHTTP:
    """get_virtual_chassis_data() integrating with mock HTTP server and patched cache."""

    def test_cache_miss_fetches_via_http(self, mock_server):
        """Cache miss triggers detect_virtual_chassis_from_inventory via HTTP."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        api = _make_api(mock_server.url)
        device_id = 60

        mock_server.device_info_response(device_id=device_id, hostname="cached-sw")
        root_items = [_stack_root(index=1)]
        member_items = [
            _chassis(100, "SN-1", position=1),
            _chassis(200, "SN-2", position=2),
        ]
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache:
            mock_cache.get.return_value = None  # cache miss
            with patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="{master}-m{position}",
            ):
                result = get_virtual_chassis_data(api, device_id)

        # Should have called cache.set to store result
        assert mock_cache.set.called
        assert result is not None
        assert result["is_stack"] is True
        assert result["member_count"] == 2

    def test_cache_hit_returns_without_http(self, mock_server):
        """Cache hit returns immediately without making any HTTP calls."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        api = _make_api(mock_server.url)
        device_id = 61

        # Include detection_error to match what _clone_virtual_chassis_data adds
        cached_data = {"is_stack": True, "member_count": 3, "members": [], "detection_error": None}

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache:
            mock_cache.get.return_value = cached_data
            result = get_virtual_chassis_data(api, device_id)

        # cache.set should NOT be called (no new fetch)
        assert not mock_cache.set.called
        assert result["is_stack"] is True
        assert result["member_count"] == 3

    def test_force_refresh_fetches_even_if_cached(self, mock_server):
        """force_refresh=True bypasses cache and fetches from HTTP."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        api = _make_api(mock_server.url)
        device_id = 62

        mock_server.device_info_response(device_id=device_id, hostname="refresh-sw")
        root_items = [_stack_root(index=1)]
        member_items = [
            _chassis(100, "SN-A", position=1),
            _chassis(200, "SN-B", position=2),
        ]
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        # Even with cached data, force_refresh should hit the API
        old_cached = {"is_stack": True, "member_count": 1, "members": [{"serial": "OLD"}], "detection_error": None}

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache:
            mock_cache.get.return_value = old_cached
            with patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="{master}-m{position}",
            ):
                result = get_virtual_chassis_data(api, device_id, force_refresh=True)

        # Should have fetched fresh data, not used old_cached
        assert result is not None
        assert result["member_count"] == 2  # new data, not old

    def test_non_vc_device_returns_empty_dict(self, mock_server):
        """Single device (not VC) → detect returns None → get_virtual_chassis_data returns empty."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        api = _make_api(mock_server.url)
        device_id = 63

        mock_server.device_info_response(device_id=device_id, hostname="single-sw")
        root_items = [_stack_root(index=1)]
        member_items = [_chassis(100, "SN-ONLY", position=1)]  # only 1 → not VC
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache:
            mock_cache.get.return_value = None
            with patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="{master}-m{position}",
            ):
                result = get_virtual_chassis_data(api, device_id)

        assert result is not None
        assert result.get("is_stack") is False


class TestPrefetchVCHTTP:
    """prefetch_vc_data_for_devices() fetches multiple devices in batch."""

    def test_prefetch_multiple_vc_devices(self, mock_server):
        """Three VC devices → cache populated for all three."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import prefetch_vc_data_for_devices

        api = _make_api(mock_server.url)

        for dev_id, hostname in [(70, "sw-70"), (71, "sw-71"), (72, "sw-72")]:
            mock_server.device_info_response(device_id=dev_id, hostname=hostname)
            root_items = [_stack_root(index=1)]
            member_items = [
                _chassis(100, f"SN-{dev_id}-1", position=1),
                _chassis(200, f"SN-{dev_id}-2", position=2),
            ]
            mock_server.vc_inventory_callable(dev_id, root_items, {1: member_items})

        cache_store = {}

        def mock_cache_set(key, val, timeout=None):
            cache_store[key] = val

        def mock_cache_get(key):
            return cache_store.get(key)

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache:
            mock_cache.get.side_effect = mock_cache_get
            mock_cache.set.side_effect = mock_cache_set
            with patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="{master}-m{position}",
            ):
                prefetch_vc_data_for_devices(api, [70, 71, 72])

        # Cache should have entries for all 3 VC devices
        assert len(cache_store) >= 3

    def test_prefetch_mix_vc_and_single(self, mock_server):
        """Mix of VC and single devices → VC is cached, single is processed without error."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import prefetch_vc_data_for_devices

        api = _make_api(mock_server.url)

        # Device 80 = 2-member VC
        mock_server.device_info_response(device_id=80, hostname="sw-stack")
        root_items_80 = [_stack_root(index=1)]
        member_items_80 = [_chassis(100, "SN-80-1", position=1), _chassis(200, "SN-80-2", position=2)]
        mock_server.vc_inventory_callable(80, root_items_80, {1: member_items_80})

        # Device 81 = single (no VC)
        mock_server.device_info_response(device_id=81, hostname="sw-single")
        root_items_81 = [_stack_root(index=1)]
        member_items_81 = [_chassis(100, "SN-81", position=1)]  # only 1 → not VC
        mock_server.vc_inventory_callable(81, root_items_81, {1: member_items_81})

        cache_store = {}

        def mock_cache_set(key, val, timeout=None):
            cache_store[key] = val

        def mock_cache_get(key):
            return cache_store.get(key)

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache:
            mock_cache.get.side_effect = mock_cache_get
            mock_cache.set.side_effect = mock_cache_set
            with patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="{master}-m{position}",
            ):
                prefetch_vc_data_for_devices(api, [80, 81])

        # Both the VC device (80) and the non-VC device (81) should be cached.
        # Non-VC devices get an empty_virtual_chassis_data() cached so prefetch
        # suppresses repeated API hits on subsequent renders.
        assert len(cache_store) == 2


class TestNegativeVCCaching:
    """Negative results (non-stack, API errors) must be cached to suppress repeated hits."""

    def test_non_vc_device_result_is_cached(self, mock_server):
        """Single device (not a stack) → detect returns None → empty result cached."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        api = _make_api(mock_server.url)
        device_id = 200

        mock_server.device_info_response(device_id=device_id, hostname="single-sw")
        root_items = [_stack_root(index=1)]
        member_items = [_chassis(100, "SN-ONLY", position=1)]  # 1 chassis only → not VC
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        cache_store = {}

        def mock_cache_set(key, val, timeout=None):
            cache_store[key] = val

        def mock_cache_get(key):
            return cache_store.get(key)

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache:
            mock_cache.get.side_effect = mock_cache_get
            mock_cache.set.side_effect = mock_cache_set
            with patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="{master}-m{position}",
            ):
                result = get_virtual_chassis_data(api, device_id)

        assert result is not None
        assert result.get("is_stack") is False
        assert result.get("member_count") == 0
        # The empty result must have been written to cache so a second call is a hit.
        assert len(cache_store) == 1
        cached = list(cache_store.values())[0]
        assert cached.get("is_stack") is False

    def test_api_error_result_is_cached(self, mock_server):
        """API 500 on inventory → detect returns None → empty result still cached."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        api = _make_api(mock_server.url)
        device_id = 201

        # Register a 500 for the root inventory call
        mock_server.routes[f"/api/v0/inventory/{device_id}"] = (500, {"status": "error", "message": "internal"})

        cache_store = {}

        def mock_cache_set(key, val, timeout=None):
            cache_store[key] = val

        def mock_cache_get(key):
            return cache_store.get(key)

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache:
            mock_cache.get.side_effect = mock_cache_get
            mock_cache.set.side_effect = mock_cache_set
            with patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="{master}-m{position}",
            ):
                result = get_virtual_chassis_data(api, device_id)

        assert result is not None
        assert result.get("is_stack") is False
        # Even API failures get cached to suppress repeated hits until TTL expires.
        assert len(cache_store) == 1

    def test_force_refresh_bypasses_negative_cache(self, mock_server):
        """force_refresh=True re-fetches even when a negative result is cached."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        api = _make_api(mock_server.url)
        device_id = 202

        mock_server.device_info_response(device_id=device_id, hostname="single-sw-202")
        root_items = [_stack_root(index=1)]
        member_items = [_chassis(100, "SN-202", position=1)]  # 1 chassis → not VC
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        # Pre-populate cache with an empty (negative) result.
        empty_cached = {"is_stack": False, "member_count": 0, "members": [], "detection_error": None}

        call_count = {"n": 0}

        def mock_cache_get(key):
            return empty_cached  # always returns cached negative

        cache_set_calls = []

        def mock_cache_set(key, val, timeout=None):
            cache_set_calls.append((key, val))
            call_count["n"] += 1

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache:
            mock_cache.get.side_effect = mock_cache_get
            mock_cache.set.side_effect = mock_cache_set
            with patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="{master}-m{position}",
            ):
                # Normal call — should use cache, NOT call set again
                result_cached = get_virtual_chassis_data(api, device_id)
                assert call_count["n"] == 0  # no new set; cache hit returned

                # force_refresh=True — must bypass cache and re-fetch + re-cache
                result_fresh = get_virtual_chassis_data(api, device_id, force_refresh=True)
                assert call_count["n"] == 1  # set called once for the re-fetch

        assert result_cached.get("is_stack") is False
        assert result_fresh.get("is_stack") is False


class TestVCPortFetch:
    """Port fetching for VC master: port names with VC member suffixes."""

    def test_ports_with_vc_suffixes_returned_as_is(self, mock_server):
        """Port names like Gi1/0/1 and Gi2/0/1 preserved from API response."""
        api = _make_api(mock_server.url)

        vc_ports = [
            {
                "port_id": 101,
                "ifName": "GigabitEthernet1/0/1",
                "ifDescr": "GigabitEthernet1/0/1",
                "ifType": "ethernetCsmacd",
                "ifSpeed": 1_000_000_000,
                "ifAdminStatus": "up",
                "ifAlias": "uplink-m1",
                "ifPhysAddress": "aa:bb:cc:dd:ee:01",
                "ifMtu": 1500,
                "ifVlan": 1,
                "ifTrunk": 0,
            },
            {
                "port_id": 201,
                "ifName": "GigabitEthernet2/0/1",
                "ifDescr": "GigabitEthernet2/0/1",
                "ifType": "ethernetCsmacd",
                "ifSpeed": 1_000_000_000,
                "ifAdminStatus": "up",
                "ifAlias": "uplink-m2",
                "ifPhysAddress": "aa:bb:cc:dd:ee:02",
                "ifMtu": 1500,
                "ifVlan": 1,
                "ifTrunk": 0,
            },
            {
                "port_id": 301,
                "ifName": "GigabitEthernet1/0/2",
                "ifDescr": "GigabitEthernet1/0/2",
                "ifType": "ethernetCsmacd",
                "ifSpeed": 1_000_000_000,
                "ifAdminStatus": "down",
                "ifAlias": "",
                "ifPhysAddress": "aa:bb:cc:dd:ee:03",
                "ifMtu": 1500,
                "ifVlan": 10,
                "ifTrunk": 0,
            },
        ]
        mock_server.ports_response(device_id=90, ports=vc_ports)

        ok, data = api.get_ports(90)

        assert ok is True
        names = [p["ifName"] for p in data["ports"]]
        assert "GigabitEthernet1/0/1" in names
        assert "GigabitEthernet2/0/1" in names
        assert "GigabitEthernet1/0/2" in names

    def test_all_port_fields_preserved(self, mock_server):
        """ifName, ifDescr, ifAlias, ifSpeed all preserved from LibreNMS response."""
        api = _make_api(mock_server.url)

        ports_data = [
            {
                "port_id": 111,
                "ifName": "GigabitEthernet1/0/1",
                "ifDescr": "GigabitEthernet1/0/1",
                "ifType": "ethernetCsmacd",
                "ifSpeed": 1_000_000_000,
                "ifAdminStatus": "up",
                "ifAlias": "server-link",
                "ifPhysAddress": "aa:bb:cc:00:00:01",
                "ifMtu": 9000,
                "ifVlan": 100,
                "ifTrunk": 1,
            },
        ]
        mock_server.ports_response(device_id=91, ports=ports_data)

        ok, data = api.get_ports(91)

        assert ok is True
        port = data["ports"][0]
        assert port["ifName"] == "GigabitEthernet1/0/1"
        assert port["ifAlias"] == "server-link"
        assert port["ifSpeed"] == 1_000_000_000
        assert port["ifMtu"] == 9000


class TestCrossServerCacheIsolation:
    """Cache keys are scoped per server_key — data from one server must not bleed into another."""

    def test_different_server_keys_use_isolated_cache_entries(self, mock_server):
        """Data cached via server-a must not be returned when querying the same device via server-b."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        device_id = 300

        api_a = _make_api(mock_server.url, server_key="server-a")
        api_b = _make_api(mock_server.url, server_key="server-b")

        # Register 2-member VC for server-a path
        mock_server.device_info_response(device_id=device_id, hostname="sw-server-a")
        root_items = [_stack_root(index=1)]
        member_items = [_chassis(100, "SN-A1", position=1), _chassis(200, "SN-A2", position=2)]
        mock_server.vc_inventory_callable(device_id, root_items, {1: member_items})

        cache_store = {}

        def mock_cache_set(key, val, timeout=None):
            cache_store[key] = val

        def mock_cache_get(key):
            return cache_store.get(key)

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache:
            mock_cache.get.side_effect = mock_cache_get
            mock_cache.set.side_effect = mock_cache_set
            with patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="{master}-m{position}",
            ):
                # Warm cache via server-a
                result_a = get_virtual_chassis_data(api_a, device_id)

                # Query same device via server-b — cache must miss (different key)
                result_b = get_virtual_chassis_data(api_b, device_id)

        # server-a result is a 2-member VC
        assert result_a is not None
        assert result_a.get("member_count") == 2

        # server-b was registered with the same mock routes, so it also fetches a 2-member VC.
        # The key assertion is that TWO separate cache entries exist — one per server_key.
        server_a_keys = [k for k in cache_store if "server-a" in k]
        server_b_keys = [k for k in cache_store if "server-b" in k]
        assert len(server_a_keys) >= 1, "server-a cache entry expected"
        assert len(server_b_keys) >= 1, "server-b cache entry expected"
        assert server_a_keys[0] != server_b_keys[0], "cache keys must differ between servers"
        assert result_a is not result_b, "must not return the same cached object for different servers"
