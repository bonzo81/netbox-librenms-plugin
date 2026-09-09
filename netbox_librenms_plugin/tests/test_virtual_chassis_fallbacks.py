"""Fallback and failure paths in import_utils/virtual_chassis.py.

The primary home for that module is test_import_utils.py. These cases live in their own
file so they do not collide at that shared file's tail when the stack is restacked.
"""

import logging

import pytest

from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_device


class _FailingCache:
    """The real Django cache, except that one key raises on read.

    Redis is a true external boundary: a local test cannot take it down for one key
    only, so the failure is injected here and every other key still round-trips.
    """

    def __init__(self, failing_key, error):
        from django.core.cache import cache

        self._cache = cache
        self._failing_key = failing_key
        self._error = error

    def get(self, key, *args, **kwargs):
        if key == self._failing_key:
            raise self._error
        return self._cache.get(key, *args, **kwargs)

    def set(self, *args, **kwargs):
        return self._cache.set(*args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._cache.delete(*args, **kwargs)


def _chassis(index, serial, position=None):
    """Build one ENTITY-MIB chassis row."""
    item = {
        "entPhysicalIndex": index,
        "entPhysicalClass": "chassis",
        "entPhysicalSerialNum": serial,
        "entPhysicalModelName": "C9300-48U",
        "entPhysicalName": f"Switch {index}",
        "entPhysicalDescr": f"Chassis {index}",
    }
    if position is not None:
        item["entPhysicalParentRelPos"] = position
    return item


def _stack_root(index=1):
    """Build the StackWise-style root entry the detection walks from."""
    return {
        "entPhysicalIndex": index,
        "entPhysicalClass": "stack",
        "entPhysicalSerialNum": "",
        "entPhysicalModelName": "",
        "entPhysicalName": "StackSub-0/0",
        "entPhysicalDescr": "Stack",
        "entPhysicalContainedIn": 0,
    }


def _api(settings, server, server_key):
    """Bind a real LibreNMSAPI to the loopback server under *server_key*."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    configure_librenms_servers(
        settings,
        {
            server_key: {
                "librenms_url": server.url,
                "api_token": "token",
                "cache_timeout": 300,
                "verify_ssl": False,
            }
        },
    )
    return LibreNMSAPI(server_key=server_key)


def _seed_stack(server, device_id, serials=("SN-A", "SN-B")):
    """Register a two-member stack for *device_id* on the loopback server."""
    server.device_info_response(device_id=device_id, hostname=f"sw-{device_id}", serial=serials[0])
    server.vc_inventory_callable(
        device_id,
        [_stack_root(index=1)],
        {1: [_chassis(100 + offset, serial, position=offset + 1) for offset, serial in enumerate(serials)]},
    )


def _inventory_paths(server, device_id):
    """Return the inventory requests the server received for *device_id*."""
    prefix = f"/api/v0/inventory/{device_id}"
    return [r for r in server.requests if r["path"] == prefix or r["path"] == f"{prefix}/all"]


def _name_pattern(pattern="-M{position}"):
    """Store the VC member naming pattern the production code reads back."""
    from netbox_librenms_plugin.models import LibreNMSSettings

    return LibreNMSSettings.objects.create(vc_member_name_pattern=pattern)


@pytest.mark.django_db
class TestStackDetectionCarriesItsMembers:
    """A detected stack always carries one member per child chassis, so members is never empty."""

    def test_a_single_child_chassis_is_not_reported_as_a_stack(self, settings, librenms_server):
        """One chassis under the root is a standalone switch, so detection returns nothing."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        _seed_stack(librenms_server, 900, serials=("SN-ONLY",))
        api = _api(settings, librenms_server, "vc_single_child")

        assert detect_virtual_chassis_from_inventory(api, 900) is None
        # The negative must come from the one child chassis, not from an unreachable server.
        assert _inventory_paths(librenms_server, 900)

    def test_a_detected_stack_carries_one_member_per_child_chassis(self, settings, librenms_server):
        """is_stack is only ever returned with the full member list behind it."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        _seed_stack(librenms_server, 901, serials=("SN-A", "SN-B", "SN-C"))
        api = _api(settings, librenms_server, "default")

        detected = detect_virtual_chassis_from_inventory(api, 901)

        assert detected["is_stack"] is True
        assert detected["member_count"] == 3
        assert len(detected["members"]) == 3

    def test_members_survive_even_when_no_chassis_reports_a_serial(self, settings, librenms_server):
        """The serial-less path still carries members, which is what keeps the domain key stable."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        _seed_stack(librenms_server, 902, serials=("", ""))
        api = _api(settings, librenms_server, "default")

        detected = detect_virtual_chassis_from_inventory(api, 902)

        assert detected["is_stack"] is True
        assert [m["serial"] for m in detected["members"]] == ["", ""]
        assert len(detected["members"]) == 2


@pytest.mark.django_db
class TestPrefetchVcData:
    def test_an_empty_device_list_skips_librenms_but_a_populated_one_does_not(self, settings, librenms_server):
        from netbox_librenms_plugin.import_utils.virtual_chassis import prefetch_vc_data_for_devices

        api = _api(settings, librenms_server, "vc_prefetch_guard")
        _seed_stack(librenms_server, 7001)

        prefetch_vc_data_for_devices(api, [], force_refresh=True)
        assert librenms_server.requests == []

        prefetch_vc_data_for_devices(api, [7001], force_refresh=True)
        assert _inventory_paths(librenms_server, 7001)

    def test_a_broken_connection_abandons_the_remaining_devices(self, settings, librenms_server, monkeypatch, caplog):
        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        api = _api(settings, librenms_server, "vc_prefetch_stop")
        for device_id in (7101, 7102, 7103):
            _seed_stack(librenms_server, device_id)
        monkeypatch.setattr(
            vc_module,
            "cache",
            _FailingCache(vc_module._vc_cache_key(api, 7102), BrokenPipeError("client went away")),
        )

        with caplog.at_level(logging.WARNING, logger=vc_module.__name__):
            vc_module.prefetch_vc_data_for_devices(api, [7101, 7102, 7103])

        assert _inventory_paths(librenms_server, 7101)
        assert _inventory_paths(librenms_server, 7103) == []
        assert "Connection error during VC prefetch at device 1" in caplog.text

    def test_one_unusable_device_is_skipped_and_the_rest_still_load(
        self, settings, librenms_server, monkeypatch, caplog
    ):
        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        api = _api(settings, librenms_server, "vc_prefetch_skip")
        for device_id in (7201, 7202, 7203):
            _seed_stack(librenms_server, device_id)
        monkeypatch.setattr(
            vc_module,
            "cache",
            _FailingCache(vc_module._vc_cache_key(api, 7202), ValueError("unreadable cache entry")),
        )

        with caplog.at_level(logging.WARNING, logger=vc_module.__name__):
            vc_module.prefetch_vc_data_for_devices(api, [7201, 7202, 7203])

        assert _inventory_paths(librenms_server, 7201)
        assert _inventory_paths(librenms_server, 7202) == []
        assert _inventory_paths(librenms_server, 7203)
        assert "Error prefetching VC data for device 7202" in caplog.text


@pytest.mark.django_db
class TestDetectVirtualChassisFailures:
    def test_a_failed_member_lookup_is_a_clean_negative(self, settings, librenms_server, caplog):
        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        api = _api(settings, librenms_server, "vc_detect_children")
        device_id = 7301
        librenms_server.device_info_response(device_id=device_id, hostname="sw-children")

        def _inventory(method, path, query, headers, body):
            if query.get("entPhysicalContainedIn", [None])[0] == "0":
                return 200, {"status": "ok", "inventory": [_stack_root(index=1)]}
            return 500, {"status": "error", "message": "inventory unavailable"}

        librenms_server.register(f"/api/v0/inventory/{device_id}", _inventory, method="GET")

        with caplog.at_level(logging.ERROR, logger=vc_module.__name__):
            result = vc_module.detect_virtual_chassis_from_inventory(api, device_id)

        assert result is None
        # A refused member lookup must not be reported as a detection crash.
        assert "Error detecting virtual chassis" not in caplog.text

    def test_unparseable_member_positions_fall_back_to_the_inventory_order(self, settings, librenms_server):
        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        api = _api(settings, librenms_server, "vc_detect_positions")
        device_id = 7302
        _name_pattern()
        librenms_server.device_info_response(device_id=device_id, hostname="sw-positions", serial="SN-A")
        first = _chassis(101, "SN-A")  # entPhysicalParentRelPos absent entirely
        second = _chassis(102, "SN-B", position="not-a-number")
        librenms_server.vc_inventory_callable(device_id, [_stack_root(index=1)], {1: [first, second]})

        result = vc_module.detect_virtual_chassis_from_inventory(api, device_id)

        assert result is not None
        assert [member["position"] for member in result["members"]] == [1, 2]
        assert [member["suggested_name"] for member in result["members"]] == [
            "sw-positions-M1",
            "sw-positions-M2",
        ]

    def test_a_cache_outage_during_detection_is_contained(self, settings, librenms_server, monkeypatch, caplog):
        from netbox_librenms_plugin import librenms_api as api_module
        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        server_key = "vc_detect_cache"
        api = _api(settings, librenms_server, server_key)
        device_id = 7303
        _seed_stack(librenms_server, device_id)
        monkeypatch.setattr(
            api_module,
            "cache",
            _FailingCache(f"librenms_device_info_{server_key}_{device_id}", ConnectionError("redis unreachable")),
        )

        with caplog.at_level(logging.ERROR, logger=vc_module.__name__):
            result = vc_module.detect_virtual_chassis_from_inventory(api, device_id)

        assert result is None
        assert "Error detecting virtual chassis" in caplog.text


@pytest.mark.django_db
class TestUpdateVcMemberSuggestedNames:
    def test_an_unparseable_stored_position_falls_back_to_the_member_order(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import update_vc_member_suggested_names

        _name_pattern()
        vc_data = {
            "is_stack": True,
            "member_count": 2,
            "members": [
                {"serial": "SN-A", "position": "not-a-number"},
                {"serial": "SN-B", "position": None},
            ],
        }

        result = update_vc_member_suggested_names(vc_data, "sw1")

        assert [member["position"] for member in result["members"]] == [1, 2]
        assert [member["suggested_name"] for member in result["members"]] == ["sw1-M1", "sw1-M2"]


@pytest.mark.django_db
class TestCreateVirtualChassisWithMembers:
    def test_a_taken_master_name_keeps_the_original_name(self, caplog):
        from dcim.models import Device
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        _name_pattern()
        master = make_device("vc-keep-name", serial="MASTER1")
        make_device("vc-keep-name-M1")

        with caplog.at_level(logging.WARNING, logger=vc_module.__name__):
            vc = create_virtual_chassis_with_members(master, [], {"device_id": 8001})

        master.refresh_from_db()
        assert master.name == "vc-keep-name"
        assert master.virtual_chassis == vc
        assert vc.name == "vc-keep-name"
        assert "name already exists" in caplog.text
        assert Device.objects.filter(name="vc-keep-name-M1").count() == 1

    def test_a_member_serial_already_in_netbox_is_skipped(self, caplog):
        from dcim.models import Device
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        _name_pattern()
        master = make_device("vc-dup-serial", serial="MASTER2")
        make_device("vc-dup-serial-elsewhere", serial="TAKEN2")
        members_info = [
            {"serial": "MASTER2", "position": 1, "name": "Switch 1", "is_master": True},
            {"serial": "TAKEN2", "position": 2, "name": "Switch 2"},
        ]

        with caplog.at_level(logging.WARNING, logger=vc_module.__name__):
            vc = create_virtual_chassis_with_members(master, members_info, {"device_id": 8002})

        assert sorted(vc.members.values_list("name", flat=True)) == ["vc-dup-serial-M1"]
        assert Device.objects.filter(serial="TAKEN2").count() == 1
        assert "Device with serial 'TAKEN2' already exists" in caplog.text
        assert "Created 0 members but expected 1" in caplog.text

    def test_a_member_name_already_in_netbox_is_skipped(self, caplog):
        from dcim.models import Device
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        _name_pattern()
        master = make_device("vc-dup-name", serial="MASTER3")
        make_device("vc-dup-name-M2")
        members_info = [
            {"serial": "MASTER3", "position": 1, "name": "Switch 1", "is_master": True},
            {"serial": "FREE3", "position": 2, "name": "Switch 2"},
        ]

        with caplog.at_level(logging.WARNING, logger=vc_module.__name__):
            vc = create_virtual_chassis_with_members(master, members_info, {"device_id": 8003})

        assert sorted(vc.members.values_list("name", flat=True)) == ["vc-dup-name-M1"]
        assert not Device.objects.filter(serial="FREE3").exists()
        assert "Device with name 'vc-dup-name-M2' already exists" in caplog.text

    def test_a_failed_creation_restores_the_master_and_reraises(self):
        from dcim.models import Device, VirtualChassis
        from django.core.exceptions import ValidationError
        from django.db import DatabaseError
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        _name_pattern()
        # 63 characters: the "-M1" suffix pushes the renamed master past Device.name's 64.
        original_name = "vc-overflow-" + "x" * 51
        master = make_device(original_name, serial="MASTER4")

        with pytest.raises((DatabaseError, ValidationError)):
            create_virtual_chassis_with_members(master, [], {"device_id": 8004})

        assert master.name == original_name
        assert master.virtual_chassis is None
        assert master.vc_position is None
        assert not VirtualChassis.objects.filter(name=original_name).exists()
        assert Device.objects.get(pk=master.pk).virtual_chassis is None
