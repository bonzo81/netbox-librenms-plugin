"""
Fallback and failure paths in import_utils/virtual_chassis.py.

The primary home for that module is test_import_utils.py. These cases live in their own
file so they do not collide at that shared file's tail when the stack is restacked.
"""

import logging

import pytest

from netbox_librenms_plugin.tests.conftest import (
    _shared_infra,
    configure_librenms_servers,
    create_prepared_stack,
    make_device,
    prepared_stack,
)


class _FailingCache:
    """
    The real Django cache, except that one key raises on read.

    Redis is a true external boundary: a local test cannot take it down for one key
    only, so the failure is injected here and every other key still round-trips.
    """

    def __init__(self, failing_key, error, failing_ops=("get",)):
        from django.core.cache import cache

        self._cache = cache
        self._failing_key = failing_key
        self._error = error
        self._failing_ops = failing_ops

    def get(self, key, *args, **kwargs):
        if key == self._failing_key and "get" in self._failing_ops:
            raise self._error
        return self._cache.get(key, *args, **kwargs)

    def set(self, key, *args, **kwargs):
        if key == self._failing_key and "set" in self._failing_ops:
            raise self._error
        return self._cache.set(key, *args, **kwargs)

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


def _seed_stack(server, device_id, serials=("SN-A", "SN-B"), *, hardware="WS-C3560X-24T-S"):
    """Register a two-member stack for *device_id* on the loopback server."""
    server.device_info_response(device_id=device_id, hostname=f"sw-{device_id}", hardware=hardware, serial=serials[0])
    server.vc_inventory_callable(
        device_id,
        [_stack_root(index=1)],
        {1: [_chassis(100 + offset, serial, position=offset + 1) for offset, serial in enumerate(serials)]},
    )


def _inventory_paths(server, device_id):
    """Return the inventory requests the server received for *device_id*."""
    prefix = f"/api/v0/inventory/{device_id}"
    return [r for r in server.requests if r["path"] == prefix or r["path"] == f"{prefix}/all"]


def _seeded_serial_rule():
    """Create the global rule that migration 0017 seeds: strip Juniper's "S/N " mark."""
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.get_or_create(
        scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
    )


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

    def test_a_detected_stack_reports_the_documented_detection_fields(self, settings, librenms_server):
        """The docstring documents detection_failed/detection_error on every return of this function.

        get_virtual_chassis_data() normalizes the payload, so only a direct caller of the exported
        detector sees the success return, and it must carry the same keys as the empty and failed
        payloads.
        """
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        _seed_stack(librenms_server, 903, serials=("SN-DOC-A", "SN-DOC-B"))
        api = _api(settings, librenms_server, "default")

        detected = detect_virtual_chassis_from_inventory(api, 903)

        assert detected["is_stack"] is True
        assert detected["detection_failed"] is False
        assert detected["detection_error"] is None

    def test_members_survive_even_when_no_chassis_reports_a_serial(self, settings, librenms_server):
        """A serial-less stack is still detected, so the import can say that it has no master."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import (
            detect_virtual_chassis_from_inventory,
            identify_vc_master,
        )
        from netbox_librenms_plugin.serial_identity import SerialRules

        _seed_stack(librenms_server, 902, serials=("", ""))
        api = _api(settings, librenms_server, "default")

        detected = detect_virtual_chassis_from_inventory(api, 902)

        assert detected["is_stack"] is True
        assert [m["serial"] for m in detected["members"]] == ["", ""]
        assert len(detected["members"]) == 2
        assert identify_vc_master(detected["members"], "", SerialRules.load().context(None)) is None


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

    @pytest.mark.parametrize("entry", ["prefetch", "collision-scan"])
    def test_a_batch_reads_the_serial_rules_once_and_the_next_batch_reads_them_again(
        self, settings, librenms_server, entry
    ):
        from django.core.cache import cache
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.serial_identity import SerialRules

        api = _api(settings, librenms_server, f"vc_batch_rules_{entry}")
        device_ids = [7401, 7402, 7403]
        for device_id in device_ids:
            _seed_stack(librenms_server, device_id)
        rows = {device_id: {"device_id": device_id, "hostname": f"sw-{device_id}"} for device_id in device_ids}
        table = f'"{NormalizationRule._meta.db_table}"'
        current_rules = SerialRules.load()
        cache_keys = [vc_module._vc_cache_key(api, device_id, current_rules) for device_id in device_ids]

        def run_batch():
            if entry == "prefetch":
                vc_module.prefetch_vc_data_for_devices(api, device_ids, force_refresh=True)
                return
            cache.delete_many(cache_keys)
            detect_collisions_for_device_ids(device_ids, api, libre_devices_cache=dict(rows))

        def rule_reads(call):
            with CaptureQueriesContext(connection) as queries:
                call()
            return [query for query in queries.captured_queries if table in query["sql"]]

        one_key_set = rule_reads(lambda: SerialRules.load().candidates())
        for _batch in range(2):
            assert len(rule_reads(run_batch)) == len(one_key_set)
        assert all(vc_module.get_virtual_chassis_data(api, device_id)["is_stack"] for device_id in device_ids)

    @pytest.mark.parametrize("entry", ["confirm", "bulk-import", "filters"])
    def test_a_validating_batch_without_a_vc_cache_reads_the_serial_rules_as_often_as_one_device(
        self, settings, librenms_server, client, entry
    ):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.urls import reverse

        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared, process_device_filters
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key = f"vc_validate_rules_{entry}".replace("-", "_")
        api = _api(settings, librenms_server, server_key)
        user = make_superuser(f"vc-validate-rules-{entry}")
        client.force_login(user)
        table = f'"{NormalizationRule._meta.db_table}"'
        serial_scope = f"'{NormalizationRule.SCOPE_SERIAL}'"

        def run_batch(device_ids):
            for device_id in device_ids:
                _seed_stack(librenms_server, device_id)
            rows = [{"device_id": i, "hostname": f"sw-{i}", "sysName": f"sw-{i}", "disabled": 0} for i in device_ids]
            if entry == "confirm":
                url = reverse("plugins:netbox_librenms_plugin:bulk_import_confirm")
                response = client.post(url, {"select": device_ids, "server_key": server_key}, HTTP_HX_REQUEST="true")
                assert response.status_code == 200
            elif entry == "bulk-import":
                bulk_import_devices_shared(
                    device_ids,
                    server_key=server_key,
                    libre_devices_cache={row["device_id"]: row for row in rows},
                    user=user,
                )
            else:
                # No VC cache: each validation detects the stack again after the prefetch.
                api.cache_timeout = 0
                librenms_server.register("/api/v0/devices", {"status": "ok", "devices": rows})
                validated = process_device_filters(
                    api, {"hostname": "sw-"}, vc_detection_enabled=True, clear_cache=True, show_disabled=True
                )
                assert [row["device_id"] for row in validated] == device_ids

        def rule_reads(device_ids):
            with CaptureQueriesContext(connection) as queries:
                run_batch(device_ids)
            return [
                query for query in queries.captured_queries if table in query["sql"] and serial_scope in query["sql"]
            ]

        one_device = rule_reads([7601])
        assert one_device
        assert len(rule_reads([7611, 7612, 7613])) == len(one_device)


@pytest.mark.django_db
class TestDetectVirtualChassisFailures:
    @pytest.mark.parametrize("failing_op", ["get", "set"])
    def test_a_cache_outage_on_one_rows_vc_key_marks_only_that_row_unresolved(
        self, settings, librenms_server, monkeypatch, failing_op
    ):
        from redis.exceptions import ConnectionError as RedisConnectionError

        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        api = _api(settings, librenms_server, f"vc_precheck_cache_{failing_op}")
        device_ids = [7501, 7502]
        for device_id in device_ids:
            _seed_stack(librenms_server, device_id, serials=(f"SN-{device_id}-A", f"SN-{device_id}-B"))
        rows = {device_id: {"device_id": device_id, "hostname": f"sw-{device_id}"} for device_id in device_ids}
        error = RedisConnectionError("redis unreachable")
        monkeypatch.setattr(
            vc_module, "cache", _FailingCache(vc_module._vc_cache_key(api, 7501), error, failing_ops=(failing_op,))
        )

        _collisions, unresolved = detect_collisions_for_device_ids(device_ids, api, libre_devices_cache=rows)

        assert list(unresolved) == [7501]
        assert vc_module.get_virtual_chassis_data(api, 7502)["is_stack"] is True

    def test_a_failed_member_lookup_reports_detection_failure(self, settings, librenms_server, caplog):
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

        assert result["detection_failed"] is True
        assert result["detection_error"] == "LibreNMS child inventory request failed"
        # A refused member lookup is a handled API failure, not a detection crash.
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

    @pytest.mark.parametrize("pattern", ["-M{position}", "-{serial}"], ids=["position", "serial"])
    def test_import_validation_checks_serial_rules_only_for_serial_names(self, settings, librenms_server, pattern):
        from netbox_librenms_plugin.import_utils.device_operations import (
            get_librenms_device_by_id,
            validate_device_for_import,
        )
        from netbox_librenms_plugin.models import NormalizationRule

        api = _api(settings, librenms_server, "vc_detect_blank_serials")
        device_id = 7307
        _name_pattern(pattern)
        device_type = _shared_infra()[2]
        _seed_stack(librenms_server, device_id, serials=("", ""), hardware=device_type.model)
        rule = NormalizationRule.objects.create(scope="serial", match_pattern=r"^(.+)$", replacement=r"\1")
        NormalizationRule.objects.filter(pk=rule.pk).update(replacement=r"X\1")

        device = get_librenms_device_by_id(api, device_id, use_cache=False)
        validation = validate_device_for_import(device, api=api, server_key=api.server_key, force_vc_refresh=True)

        result = validation["virtual_chassis"]
        assert result["detection_failed"] is (pattern == "-{serial}")
        if pattern == "-M{position}":
            assert result["is_stack"] is True
            assert result["device_type_id"] == device_type.pk
            assert [member["suggested_name"] for member in result["members"]] == ["sw-7307-M1", "sw-7307-M2"]

    def test_a_detection_payload_cached_before_this_shape_is_not_trusted(self, settings, librenms_server):
        """A v3 payload stored a master decision; reading it now would import a stack standalone for good."""
        from django.core.cache import cache

        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        api = _api(settings, librenms_server, "vc_detect_version")
        device_id = 7304
        _seed_stack(librenms_server, device_id, serials=("SN-V-A", "SN-V-B"))
        stale = {
            "is_stack": True,
            "member_count": 2,
            "members": [{"serial": "SN-V-A", "position": 1, "is_master": False}],
            "master_identified": False,
            "detection_failed": False,
            "detection_error": None,
        }
        cache.set(f"librenms_vc_detection_v3_{api.server_key}_{device_id}", stale, timeout=300)

        result = vc_module.get_virtual_chassis_data(api, device_id)

        assert [member["serial"] for member in result["members"]] == ["SN-V-A", "SN-V-B"]

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

        assert result["detection_failed"] is True
        assert result["detection_error"] == "redis unreachable"
        assert "Error detecting virtual chassis" in caplog.text


@pytest.mark.django_db
class TestCreateVirtualChassisWithMembers:
    def test_a_taken_master_name_keeps_the_original_name(self, caplog):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        _name_pattern()
        master = make_device("vc-keep-name", serial="MASTER1")
        make_device("vc-keep-name-M1")

        with caplog.at_level(logging.WARNING, logger=vc_module.__name__):
            members = [{"serial": "MASTER1", "position": 1}]
            vc = create_prepared_stack(master, members, {"device_id": 8001})

        master.refresh_from_db()
        assert master.name == "vc-keep-name"
        assert master.virtual_chassis == vc
        assert vc.name == "vc-keep-name"
        assert "name already exists" in caplog.text
        assert Device.objects.filter(name="vc-keep-name-M1").count() == 1

    def test_a_member_stores_the_raw_librenms_serial(self):
        """Juniper reports "S/N BCFB9751"; the member stores it as reported, and readers apply the rules."""
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        from dcim.models import Device

        _name_pattern()
        master = make_device("vc-marker", serial="BCFB9793")
        members = [
            {"serial": "S/N BCFB9793", "position": 1, "name": "FPC0"},
            {"serial": "S/N BCFB9751", "position": 2, "name": "FPC1"},
        ]

        create_prepared_stack(master, members, {"device_id": 8101})

        created = Device.objects.get(virtual_chassis__name="vc-marker", vc_position=2)
        assert created.serial == "S/N BCFB9751"

    def test_the_master_is_found_from_a_decorated_serial(self, settings, librenms_server):
        """
        Juniper decorates the ENTITY-MIB serial ("S/N BCFB9793"); the device serial is plain.

        Compared raw they never match, so no master is found and the import would create no
        chassis. The master match reads the serial rules before it compares.
        """
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.virtual_chassis import (
            detect_virtual_chassis_from_inventory,
            identify_vc_master,
        )
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.serial_identity import SerialRules

        _name_pattern()
        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        api = _api(settings, librenms_server, "decorated-master")
        librenms_server.device_info_response(device_id=8103, hostname="vc-master-pos", serial="BCFB9793")
        librenms_server.vc_inventory_callable(
            8103,
            [_stack_root(index=1)],
            {1: [_chassis(101, "S/N BCFB9751", position=1), _chassis(102, "S/N BCFB9793", position=2)]},
        )

        detected = detect_virtual_chassis_from_inventory(api, 8103)
        master = make_device("vc-master-pos", serial="BCFB9793")
        master_member = identify_vc_master(detected["members"], "BCFB9793", SerialRules.load().context(None))
        vc = create_prepared_stack(master, detected["members"], {"device_id": 8103}, master_index=1)

        assert master_member is detected["members"][1]
        master.refresh_from_db()
        assert master.vc_position == 2
        assert Device.objects.filter(virtual_chassis=vc).count() == 2

    def test_the_master_row_is_skipped_when_its_serial_carries_the_marker(self):
        """
        The master's own chassis row comes back with the marker, its stored serial without.

        Comparing the two raw makes them differ, so the master is created a second time as a
        member of its own virtual chassis.
        """
        from dcim.models import Device

        _name_pattern()
        master = make_device("vc-master-marker", serial="BCFB9793")
        members = [
            {"serial": "S/N BCFB9793", "position": 1, "name": "FPC0"},
            {"serial": "S/N BCFB9751", "position": 2, "name": "FPC1"},
        ]

        vc = create_prepared_stack(master, members, {"device_id": 8102})

        assert Device.objects.filter(virtual_chassis=vc, serial="BCFB9793").count() == 1
        assert Device.objects.filter(virtual_chassis=vc).count() == 2

    def test_a_master_member_that_is_not_one_of_the_members_is_refused(self):
        """A copy of the master row is not the caller's decision, so creation stops before any write."""
        from dcim.models import Device, VirtualChassis

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        _name_pattern()
        master = make_device("vc-master-copy", serial="COPY1")
        members = [
            {"serial": "COPY1", "position": 1, "name": "Switch 1"},
            {"serial": "COPY2", "position": 2, "name": "Switch 2"},
        ]

        prepared, master_member = prepared_stack(master, members)

        with pytest.raises(ValueError, match="master_member must be one of the prepared members"):
            create_virtual_chassis_with_members(
                master, prepared, {"device_id": 8105}, master_member=dict(master_member)
            )

        master.refresh_from_db()
        assert master.name == "vc-master-copy"
        assert master.virtual_chassis is None
        assert not VirtualChassis.objects.exists()
        assert not Device.objects.filter(serial="COPY2").exists()

    def test_a_member_serial_already_in_netbox_is_skipped(self, caplog):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        _name_pattern()
        master = make_device("vc-dup-serial", serial="MASTER2")
        make_device("vc-dup-serial-elsewhere", serial="TAKEN2")
        members_info = [
            {"serial": "MASTER2", "position": 1, "name": "Switch 1"},
            {"serial": "TAKEN2", "position": 2, "name": "Switch 2"},
        ]

        with caplog.at_level(logging.WARNING, logger=vc_module.__name__):
            vc = create_prepared_stack(master, members_info, {"device_id": 8002})

        assert sorted(vc.members.values_list("name", flat=True)) == ["vc-dup-serial-M1"]
        assert Device.objects.filter(serial="TAKEN2").count() == 1
        assert "Device with serial 'TAKEN2' already exists" in caplog.text
        assert "Created 0 members but expected 1" in caplog.text

    def test_a_member_serial_matches_a_padded_stored_value(self, caplog):
        """Legacy padding in NetBox must not permit a duplicate normalized member serial."""
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        _name_pattern()
        master = make_device("vc-padded-serial", serial="MASTER-PADDED")
        existing = make_device("vc-padded-serial-elsewhere", serial="placeholder")
        Device.objects.filter(pk=existing.pk).update(serial=" PADDED-MEMBER ")
        members_info = [
            {"serial": "MASTER-PADDED", "position": 1, "name": "Switch 1"},
            {"serial": "PADDED-MEMBER", "position": 2, "name": "Switch 2"},
        ]

        with caplog.at_level(logging.WARNING, logger=vc_module.__name__):
            vc = create_prepared_stack(master, members_info, {"device_id": 8004})

        assert sorted(vc.members.values_list("name", flat=True)) == ["vc-padded-serial-M1"]
        assert not Device.objects.filter(serial="PADDED-MEMBER").exists()
        assert "Device with serial 'PADDED-MEMBER' already exists" in caplog.text

    def test_a_legacy_keyed_member_blocks_its_decorated_serial(self):
        """A member stored as "ABC" by an earlier release still blocks an incoming "S/N ABC"."""
        from dcim.models import Device

        _seeded_serial_rule()
        _name_pattern()
        master = make_device("vc-legacy-keyed", serial="MASTER-LK")
        make_device("vc-legacy-keyed-elsewhere", serial="ABC-LK")
        members = [{"serial": "MASTER-LK", "position": 1}, {"serial": "S/N ABC-LK", "position": 2}]

        vc = create_prepared_stack(master, members, {"device_id": 8006})

        assert vc.members.count() == 1
        assert not Device.objects.filter(serial="S/N ABC-LK").exists()

    def test_a_member_stored_raw_by_an_earlier_import_blocks_the_same_serial(self):
        from dcim.models import Device

        _seeded_serial_rule()
        _name_pattern()
        master = make_device("vc-earlier-raw", serial="MASTER-ER")
        make_device("vc-earlier-raw-elsewhere", serial="S/N ABC-ER")
        members = [{"serial": "MASTER-ER", "position": 1}, {"serial": "S/N ABC-ER", "position": 2}]

        vc = create_prepared_stack(master, members, {"device_id": 8007})

        assert vc.members.count() == 1
        assert Device.objects.filter(serial="S/N ABC-ER").count() == 1

    def test_one_batch_creates_one_member_for_two_forms_of_one_serial(self):

        _seeded_serial_rule()
        _name_pattern()
        master = make_device("vc-batch-forms", serial="MASTER-BF")
        members = [
            {"serial": "MASTER-BF", "position": 1},
            {"serial": "S/N ABC-BF", "position": 2},
            {"serial": "ABC-BF", "position": 3},
        ]

        vc = create_prepared_stack(master, members, {"device_id": 8008})

        assert sorted(vc.members.exclude(pk=master.pk).values_list("serial", flat=True)) == ["S/N ABC-BF"]

    @pytest.mark.parametrize("earlier", ["batch", "legacy"])
    def test_a_rule_that_matches_its_own_output_keeps_two_serials_apart_and_the_report_lists_them(self, earlier):
        """
        Under ^X(.+)$ a stored "X123" keys to "123" and "XX123" to "X123": two identities, a documented limit.

        One batch creates both. A device that already stores "X123" can be an earlier keyed member,
        so the import skips "XX123".
        """
        from dcim.models import Device

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.serial_identity import report_rows

        NormalizationRule.objects.create(scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1")
        _name_pattern()
        master = make_device(f"vc-rematch-{earlier}", serial="MASTER-UC")
        members = [{"serial": "MASTER-UC", "position": 1}]
        if earlier == "batch":
            members.append({"serial": "X123", "position": 2})
        else:
            make_device(f"vc-rematch-{earlier}-elsewhere", serial="X123")
        members.append({"serial": "XX123", "position": 3})

        vc = create_prepared_stack(master, members, {"device_id": 8009})

        assert Device.objects.filter(serial="XX123").exists() is (earlier == "batch")
        assert vc.members.count() == (3 if earlier == "batch" else 1)
        reported = {stored for _row, stored, _key, _reason in report_rows()}
        assert "X123" in reported
        assert ("XX123" in reported) is (earlier == "batch")

    def test_a_member_name_already_in_netbox_is_skipped(self, caplog):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils import virtual_chassis as vc_module

        _name_pattern()
        master = make_device("vc-dup-name", serial="MASTER3")
        make_device("vc-dup-name-M2")
        members_info = [
            {"serial": "MASTER3", "position": 1, "name": "Switch 1"},
            {"serial": "FREE3", "position": 2, "name": "Switch 2"},
        ]

        with caplog.at_level(logging.WARNING, logger=vc_module.__name__):
            vc = create_prepared_stack(master, members_info, {"device_id": 8003})

        assert sorted(vc.members.values_list("name", flat=True)) == ["vc-dup-name-M1"]
        assert not Device.objects.filter(serial="FREE3").exists()
        assert "Device with name 'vc-dup-name-M2' already exists" in caplog.text

    def test_a_failed_creation_restores_the_master_and_reraises(self):
        from dcim.models import Device, VirtualChassis
        from django.core.exceptions import ValidationError
        from django.db import DatabaseError

        _name_pattern()
        # 63 characters: the "-M1" suffix pushes the renamed master past Device.name's 64.
        original_name = "vc-overflow-" + "x" * 51
        master = make_device(original_name, serial="MASTER4")

        with pytest.raises((DatabaseError, ValidationError)):
            members = [{"serial": "MASTER4", "position": 1}]
            create_prepared_stack(master, members, {"device_id": 8004})

        assert master.name == original_name
        assert master.virtual_chassis is None
        assert master.vc_position is None
        assert not VirtualChassis.objects.filter(name=original_name).exists()
        assert Device.objects.get(pk=master.pk).virtual_chassis is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "serial", ["-", " N/A ", "NA", "none", "not available", "notavailable", "null", "UnKnOwN", "unspecified"]
)
def test_placeholder_stack_detection_and_creation_preserve_distinct_members(settings, librenms_server, serial):
    from netbox_librenms_plugin.import_utils.virtual_chassis import (
        detect_virtual_chassis_from_inventory,
        identify_vc_master,
    )
    from netbox_librenms_plugin.serial_identity import SerialRules, display

    api = _api(settings, librenms_server, "placeholder-stack")
    _seed_stack(librenms_server, 981, serials=(serial, serial, serial))
    detected = detect_virtual_chassis_from_inventory(api, 981)
    assert detected["member_count"] == 3
    # A placeholder names no master, so the import creates no chassis.
    assert identify_vc_master(detected["members"], serial, SerialRules.load().context(None)) is None
    assert display(serial) == ""


@pytest.mark.django_db
@pytest.mark.parametrize("malformed", [True, ["SECOND-CARD"]], ids=["boolean", "list"])
def test_a_malformed_member_serial_is_never_the_stack_master(malformed):
    """The device stores the text "True"; a member that reports JSON `true` has that text but no identity."""
    from netbox_librenms_plugin.import_utils.virtual_chassis import identify_vc_master
    from netbox_librenms_plugin.serial_identity import SerialRules

    members = [{"serial": malformed, "position": 1}, {"serial": "SECOND-CARD", "position": 2}]

    assert identify_vc_master(members, str(malformed), SerialRules.load().context(None)) is None


@pytest.mark.django_db
@pytest.mark.parametrize("text_holder", [False, True], ids=["no-holder", "holder-of-the-text"])
def test_a_malformed_member_serial_refuses_the_stack_whatever_device_stores_its_text(text_holder):
    """A member that reports JSON `true` has no serial to store, also when a device stores the text "True"."""
    from dcim.models import Device, VirtualChassis
    from django.core.exceptions import ValidationError

    from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

    _name_pattern()
    master = make_device("malformed-member-master", serial="MALFORMED-MEMBER-MASTER")
    if text_holder:
        make_device("malformed-member-text-holder", serial="True")
    members = [{"serial": master.serial, "position": 1}, {"serial": True, "position": 2}]
    prepared, master_member = prepared_stack(master, members)

    with pytest.raises(ValidationError):
        create_virtual_chassis_with_members(master, prepared, {"device_id": 8020}, master_member=master_member)

    assert not VirtualChassis.objects.exists()
    assert Device.objects.get(pk=master.pk).virtual_chassis_id is None


@pytest.mark.django_db
@pytest.mark.parametrize("held", [True, False], ids=["held", "created"])
def test_only_a_member_that_the_create_writes_refuses_a_serial_that_does_not_fit(held):
    """The "S/N " rule keys the 51-character serial to a 47-character one; a held member is skipped, not written."""
    from dcim.models import Device, VirtualChassis
    from django.core.exceptions import ValidationError

    from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

    _seeded_serial_rule()
    _name_pattern()
    master = make_device("long-member-master", serial="LONG-MEMBER-MASTER")
    if held:
        holder = make_device("long-member-holder", serial="N" * 47)
    members = [
        {"serial": master.serial, "position": 1},
        {"serial": f"S/N {'N' * 47}", "position": 2},
        {"serial": "LONG-MEMBER-THIRD", "position": 3},
    ]
    prepared, master_member = prepared_stack(master, members)

    if not held:
        with pytest.raises(ValidationError):
            create_virtual_chassis_with_members(master, prepared, {"device_id": 8021}, master_member=master_member)
        assert not VirtualChassis.objects.exists()
        return
    vc = create_virtual_chassis_with_members(master, prepared, {"device_id": 8021}, master_member=master_member)

    assert sorted(vc.members.values_list("serial", flat=True)) == ["LONG-MEMBER-MASTER", "LONG-MEMBER-THIRD"]
    assert Device.objects.get(pk=holder.pk).virtual_chassis_id is None


@pytest.mark.django_db
def test_a_serial_shorter_than_three_letters_or_digits_is_no_stack_identity(settings, librenms_server):
    """A one-digit member serial names no master, so the stack imports as one device; the members keep their text."""
    from netbox_librenms_plugin.import_utils.bulk_import import stack_key
    from netbox_librenms_plugin.import_utils.virtual_chassis import (
        detect_virtual_chassis_from_inventory,
        identify_vc_master,
    )
    from netbox_librenms_plugin.serial_identity import SerialRules

    api = _api(settings, librenms_server, "zero-stack")
    _seed_stack(librenms_server, 982, serials=(0, "REAL-MEMBER"))
    detected = detect_virtual_chassis_from_inventory(api, 982)
    context = SerialRules.load().context(None)
    assert identify_vc_master(detected["members"], "0", context) is None
    assert identify_vc_master(detected["members"], "REAL-MEMBER", context) is detected["members"][1]
    # The stack key counts only the member that has an identity.
    assert stack_key(detected, 982, context) == stack_key({"members": [{"serial": "REAL-MEMBER"}]}, 1, context)
    master = make_device("zero-stack-master", serial="REAL-MEMBER")
    vc = create_prepared_stack(master, detected["members"], {"device_id": 982}, master_index=1)
    assert sorted(vc.members.values_list("serial", flat=True)) == ["0", "REAL-MEMBER"]


@pytest.mark.django_db
@pytest.mark.parametrize("use_manufacturer_rule", [False, True])
def test_placeholder_members_do_not_match_an_unrelated_device(use_manufacturer_rule):
    from netbox_librenms_plugin.models import NormalizationRule

    master = make_device("placeholder-members-master", serial="REAL-MASTER")
    make_device("placeholder-members-other", serial="unknown")
    serial = "unknown"
    if use_manufacturer_rule:
        serial = "VENDOR-PLACEHOLDER"
        # The group takes no part in the match, so the rule leaves no identity.
        NormalizationRule.objects.create(
            scope="serial",
            manufacturer=master.device_type.manufacturer,
            match_pattern="^VENDOR-PLACEHOLDER(X)?$",
            replacement=r"\1",
        )
    members = [
        {"serial": master.serial, "position": 1},
        {"serial": serial, "position": 2},
        {"serial": serial, "position": 3},
    ]
    vc = create_prepared_stack(master, members, {"device_id": 983})
    assert sorted(vc.members.values_list("vc_position", flat=True)) == [1, 2, 3]
    # A rule that blanks a real raw serial only stops comparisons; the member keeps the raw value.
    stored = "VENDOR-PLACEHOLDER" if use_manufacturer_rule else ""
    assert list(vc.members.exclude(pk=master.pk).values_list("serial", flat=True)) == [stored, stored]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("patterns", "legacy", "incoming"),
    [([r"^0(.+)$"], "0ABC12", "00ABC12"), ([r"^ABC-(.+)$", r"^RAW-(.+)$"], "ABC-123", "RAW-ABC-123")],
    ids=["rule-matches-its-output", "later-rule-feeds-an-earlier-one"],
)
def test_a_member_is_not_created_beside_a_legacy_row_that_stores_its_rule_output(patterns, legacy, incoming):
    """The rules key the legacy row again, so it does not match; it can still be the member, so the import skips it."""
    from dcim.models import Device

    from netbox_librenms_plugin.models import NormalizationRule

    for priority, pattern in enumerate(patterns, start=1):
        NormalizationRule.objects.create(scope="serial", match_pattern=pattern, replacement=r"\1", priority=priority)
    master = make_device("legacy-output-master", serial="MASTER-LEGACY-OUTPUT")
    make_device("legacy-output-member", serial=legacy)
    members = [{"serial": master.serial, "position": 1}, {"serial": incoming, "position": 2}]

    vc = create_prepared_stack(master, members, {"device_id": 985})

    assert list(vc.members.values_list("pk", flat=True)) == [master.pk]
    assert not Device.objects.filter(serial=incoming).exists()


@pytest.mark.django_db
def test_the_new_stack_master_is_no_legacy_row_for_another_member():
    """Under ^X(.+)$ the master "X123" keys to "123" and the member "XX123" to "X123": the master holds its own serial."""
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.create(scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1")
    _name_pattern()
    master = make_device("vc-raw-master", serial="X123")
    members = [{"serial": "X123", "position": 1}, {"serial": "XX123", "position": 2}]

    vc = create_prepared_stack(master, members, {"device_id": 8018})

    assert sorted(vc.members.values_list("serial", flat=True)) == ["X123", "XX123"]


@pytest.mark.django_db
def test_a_member_serial_that_only_the_master_rules_blank_is_not_created_beside_its_holder():
    """The master's maker blanks "VENDOR-PLACEHOLDER", but a device of another maker holds it as its serial."""
    from dcim.models import Device, DeviceType, Manufacturer

    from netbox_librenms_plugin.models import NormalizationRule

    master = make_device("blanked-member-master", serial="REAL-MASTER-BLANKED")
    NormalizationRule.objects.create(
        scope="serial",
        manufacturer=master.device_type.manufacturer,
        match_pattern="^VENDOR-PLACEHOLDER(X)?$",
        replacement=r"\1",
    )
    holder = make_device("blanked-member-holder", serial="VENDOR-PLACEHOLDER")
    other_maker = Manufacturer.objects.create(name="Blanked member other maker", slug="blanked-member-other-maker")
    holder.device_type = DeviceType.objects.create(
        manufacturer=other_maker, model="Blanked member other model", slug="blanked-member-other-model"
    )
    holder.save()
    members = [{"serial": master.serial, "position": 1}, {"serial": "VENDOR-PLACEHOLDER", "position": 2}]

    vc = create_prepared_stack(master, members, {"device_id": 8019})

    assert list(vc.members.values_list("pk", flat=True)) == [master.pk]
    assert list(Device.objects.filter(serial="VENDOR-PLACEHOLDER").values_list("pk", flat=True)) == [holder.pk]


@pytest.mark.django_db
def test_a_serial_lookup_keeps_two_matches_and_adds_no_queries_past_them():
    """Devices that share one serial past two add no queries, and every count is Ambiguous."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from netbox_librenms_plugin.serial_identity import ResolutionKind, resolve_device

    found = {}

    def queries_for(serial, count):
        for n in range(count):
            make_device(f"{serial.lower()}-{n}", serial=serial)
        with CaptureQueriesContext(connection) as queries:
            found[serial] = resolve_device(serial)
            assert found[serial].kind is ResolutionKind.AMBIGUOUS
        return len(queries)

    assert queries_for("FEW-SN", 2) == queries_for("MANY-SN", 6)
    # One query reads every candidate row; the resolver stops comparing rows at the second match.
    assert (len(found["MANY-SN"].matches), found["MANY-SN"].complete) == (2, False)


@pytest.mark.django_db
def test_a_batch_write_lookup_compares_no_rows_beside_many_duplicate_holders(monkeypatch):
    """Thirty holders of one serial and a serial without a holder: the read goes on, and each row costs one check."""
    from django.db.models import Model

    from netbox_librenms_plugin.serial_identity import ResolutionKind, SerialRules, resolve_devices_write

    for n in range(30):
        make_device(f"duplicate-holder-{n}", serial="DUP-HOLDER-SN")
    rules = SerialRules.load()
    compare_rows = Model.__eq__
    comparisons = []

    def counted(row, other):
        comparisons.append(row.pk)
        return compare_rows(row, other)

    with monkeypatch.context() as patch:
        patch.setattr(Model, "__eq__", counted)
        found = resolve_devices_write(["DUP-HOLDER-SN", "NO-HOLDER-SN"], rules.context(None), rules=rules)

    assert len(found["DUP-HOLDER-SN"].matches) == 30
    assert found["NO-HOLDER-SN"].kind is ResolutionKind.NO_MATCH
    # A list scan per row compares each new holder with every earlier one: 435 comparisons for 30 rows.
    assert len(comparisons) < 30


@pytest.mark.django_db
@pytest.mark.parametrize(("stored", "incoming"), [("S/N ABC-LOOKUP", "ABC-LOOKUP"), ("ABC-LOOKUP", "S/N ABC-LOOKUP")])
def test_serial_lookup_finds_raw_and_keyed_values_before_limiting(stored, incoming):
    from netbox_librenms_plugin.serial_identity import resolve_device

    _seeded_serial_rule()
    holder = make_device("decorated-lookup-holder", serial=stored)
    assert [row.pk for row in resolve_device(incoming).matches] == [holder.pk]
    assert resolve_device(incoming, exclude_pks=(holder.pk,)).matches == ()


@pytest.mark.django_db
def test_a_decorated_stored_member_blocks_a_keyed_incoming_member():
    from dcim.models import Device

    _seeded_serial_rule()
    _name_pattern()
    master = make_device("decorated-holder-master", serial="MASTER-DHL")
    holder = make_device("decorated-holder-existing", serial="S/N ABC-DHL")
    members = [{"serial": "MASTER-DHL", "position": 1}, {"serial": "ABC-DHL", "position": 2}]

    vc = create_prepared_stack(master, members, {"device_id": 8017})

    assert vc.members.count() == 1
    assert not Device.objects.filter(serial="ABC-DHL").exists()
    assert Device.objects.filter(pk=holder.pk).exists()


@pytest.mark.django_db
def test_serial_lookup_refuses_a_legacy_nonextracting_replacement():
    from netbox_librenms_plugin.models import NormalizationRule, RuleConfigurationError
    from netbox_librenms_plugin.serial_identity import resolve_device

    NormalizationRule.objects.filter(scope="serial").delete()
    rule = NormalizationRule.objects.create(scope="serial", match_pattern=r"^PREFIX(.+)$", replacement=r"\1")
    make_device("serial-candidate-rewrite-holder", serial="PREFIXABC")
    # A legacy stored rule must fail closed instead of using the former general rewrite contract.
    NormalizationRule.objects.filter(pk=rule.pk).update(replacement=r"KEY-\1")

    with pytest.raises(RuleConfigurationError):
        resolve_device("KEY-ABC")
