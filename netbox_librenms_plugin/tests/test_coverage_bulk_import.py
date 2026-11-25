"""Bulk-import behavior tests against real HTTP, cache, and NetBox rows."""

import pytest
from django.core.cache import cache

from netbox_librenms_plugin.tests.conftest import (
    delete_keeping_pk,
    make_device,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping, seed_stored_mapping, stored_mapping_for_test


class TestStackKey:
    """One key per physical stack, and never one key shared by unrelated stacks."""

    def test_member_serials_key_the_stack(self):
        from netbox_librenms_plugin.import_utils.bulk_import import stack_key
        from netbox_librenms_plugin.serial_identity import plain_context

        members = [{"serial": "FOC2"}, {"serial": "FOC1"}]
        context = plain_context()

        assert stack_key({"members": members}, 7, context) == stack_key({"members": members[::-1]}, 8, context)
        assert stack_key({"members": members}, 7, context) != stack_key({"members": members[:1]}, 7, context)
        other = [{"serial": "FOC3"}, {"serial": "FOC4"}]
        assert stack_key({"members": members}, 7, context) != stack_key({"members": other}, 7, context)

    def test_stacks_without_member_serials_get_distinct_keys(self):
        """A shared key would let the first such stack suppress chassis creation for every other one."""
        from netbox_librenms_plugin.import_utils.bulk_import import stack_key
        from netbox_librenms_plugin.serial_identity import plain_context

        first = stack_key({"members": [{"serial": "-"}]}, 7, plain_context())
        second = stack_key({"members": [{"serial": "-"}]}, 8, plain_context())

        assert first != second


pytestmark = pytest.mark.django_db


def _libre_device(
    device_id,
    hostname,
    *,
    hardware="TestDT",
    serial="",
    location="TestSite",
    disabled=0,
):
    return {
        "device_id": device_id,
        "hostname": hostname,
        "sysName": hostname,
        "hardware": hardware,
        "serial": serial,
        "os": "linux",
        "version": "1",
        "features": "-",
        "location": location,
        "type": "network",
        "status": 1,
        "disabled": disabled,
        "ip": f"198.18.{device_id % 200}.1",
    }


def _register_device(live_librenms, row):
    device_id = row["device_id"]
    live_librenms.server.register(
        f"/api/v0/devices/{device_id}",
        {"status": "ok", "devices": [row]},
    )
    live_librenms.server.inventory_response(device_id, [])


def _validation(existing=None, *, import_as_vm=False):
    return {
        "resolved_name": getattr(existing, "name", "new-device"),
        "existing_device": existing,
        "existing_match_type": "hostname" if existing else None,
        "existing_librenms_link": None,
        "import_as_vm": import_as_vm,
        "is_ready": existing is None,
        "can_import": existing is None,
        "status": "active",
        "issues": [],
        "serial_action": None,
        "oob_candidate": None,
        "merge_candidates": [],
        "promote_to_host_candidate": None,
        "librenms_id_needs_migration": False,
        "device_type_mismatch": False,
        "site": {"found": True},
        "device_type": {"found": True},
        "device_role": {"found": True, "role": getattr(existing, "role", None), "available_roles": []},
        "platform": {"found": False, "platform": None},
        "cluster": {
            "found": bool(getattr(existing, "cluster_id", None)),
            "cluster": getattr(existing, "cluster", None),
            "available_clusters": [],
        },
        "vm_placement": {
            "method": "cluster" if getattr(existing, "cluster_id", None) else "site",
            "found": True,
            "host_device": None,
        },
        "virtual_chassis": {"is_stack": False, "members": []},
    }


class TestBulkPrecheckDecision:
    def test_clean_batch_keeps_every_device_and_vm(self):
        from netbox_librenms_plugin.import_utils.bulk_import import classify_bulk_precheck

        outcome = classify_bulk_precheck([], {}, [1, 2], {3: {"cluster_id": 9}})

        assert outcome.blocked is False
        assert outcome.importable_device_ids == [1, 2]
        assert outcome.importable_vm_imports == {3: {"cluster_id": 9}}
        assert outcome.skip_message == ""
        assert outcome.block_message == ""

    def test_unresolved_rows_are_skipped_without_blocking_the_rest(self):
        from netbox_librenms_plugin.import_utils.bulk_import import (
            LIBRENMS_ROW_UNREADABLE,
            NETBOX_SERIAL_UNVERIFIED,
            classify_bulk_precheck,
        )

        unresolved = {2: LIBRENMS_ROW_UNREADABLE, 4: NETBOX_SERIAL_UNVERIFIED}
        outcome = classify_bulk_precheck([], unresolved, [1, 2, 3], {4: {}, 5: {}})

        assert outcome.blocked is False
        assert outcome.skipped_ids == [2, 4]
        assert outcome.importable_device_ids == [1, 3]
        assert outcome.importable_vm_imports == {5: {}}
        assert f"(id(s): 2): {LIBRENMS_ROW_UNREADABLE}," in outcome.skip_message
        assert f"(id(s): 4): {NETBOX_SERIAL_UNVERIFIED}," in outcome.skip_message
        assert "Retry rows with unreadable LibreNMS data individually." in outcome.skip_message

    def test_only_unverified_serial_rows_get_no_retry_hint(self):
        from netbox_librenms_plugin.import_utils.bulk_import import NETBOX_SERIAL_UNVERIFIED, classify_bulk_precheck

        outcome = classify_bulk_precheck([], {4: NETBOX_SERIAL_UNVERIFIED}, [1, 3], {})

        assert outcome.skipped_ids == [4]
        assert f"(id(s): 4): {NETBOX_SERIAL_UNVERIFIED}," in outcome.skip_message
        assert "Retry rows" not in outcome.skip_message

    def test_collision_blocks_the_whole_batch_and_names_visible_targets(self):
        from netbox_librenms_plugin.import_utils.bulk_import import LIBRENMS_ROW_UNREADABLE, classify_bulk_precheck

        collisions = [
            {
                "nb_device_pk": 17,
                "target_visible": True,
                "librenms_devices": [{"device_id": 1}, {"device_id": 2}],
            }
        ]

        outcome = classify_bulk_precheck(collisions, {3: LIBRENMS_ROW_UNREADABLE}, [1, 2, 3], {})

        assert outcome.blocked is True
        assert outcome.importable_device_ids == [1, 2]
        assert "Visible pk(s): 17" in outcome.block_message
        assert outcome.skip_message == ""

    def test_hidden_collision_does_not_disclose_the_target(self):
        from netbox_librenms_plugin.import_utils.bulk_import import classify_bulk_precheck

        outcome = classify_bulk_precheck(
            [{"nb_device_pk": None, "target_visible": False, "librenms_devices": []}],
            {},
            [1],
            {},
        )

        assert outcome.blocked is True
        assert "outside your view scope" in outcome.block_message
        assert "Visible pk" not in outcome.block_message


class TestCollisionDetection:
    def test_two_live_librenms_rows_resolving_to_one_device_are_blocked(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        target = make_device("collision-target")
        first = _libre_device(41, target.name)
        second = _libre_device(42, target.name)
        _register_device(live_librenms, first)
        _register_device(live_librenms, second)
        shared_cache = {}

        collisions, unresolved = detect_collisions_for_device_ids(
            [41, 42],
            live_librenms.api,
            libre_devices_cache=shared_cache,
        )

        assert unresolved == {}
        assert len(collisions) == 1
        assert collisions[0]["nb_device_pk"] == target.pk
        assert {row["device_id"] for row in collisions[0]["librenms_rows"]} == {41, 42}
        assert shared_cache == {41: first, 42: second}
        # The inventory read per device fails a row closed when its stack cannot be read, and the
        # import reuses its cache.
        assert [request["path"] for request in live_librenms.server.requests] == [
            "/api/v0/devices/41",
            "/api/v0/inventory/41",
            "/api/v0/devices/42",
            "/api/v0/inventory/42",
        ]

    def test_distinct_real_targets_do_not_collide(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        first_target = make_device("collision-distinct-a")
        second_target = make_device("collision-distinct-b")
        first = _libre_device(51, first_target.name)
        second = _libre_device(52, second_target.name)
        _register_device(live_librenms, first)
        _register_device(live_librenms, second)

        collisions, unresolved = detect_collisions_for_device_ids([51, 52], live_librenms.api)

        assert collisions == []
        assert unresolved == {}

    def test_a_legacy_row_with_an_unreadable_holder_lookup_still_collides_by_its_legacy_id(self, live_librenms):
        """The legacy ID names the Device, so another maker's broken rule must not drop the row from the grouping."""
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids
        from netbox_librenms_plugin.tests.test_coverage_device_operations import _broken_serial_rule

        target = make_device("collision-legacy-target", serial="LEGACY-COLLIDE", librenms_cf=53)
        _broken_serial_rule("collision-legacy")
        legacy = _libre_device(53, "collision-legacy-other-name", serial="LEGACY-COLLIDE")
        by_name = _libre_device(54, target.name)
        _register_device(live_librenms, legacy)
        _register_device(live_librenms, by_name)

        collisions, unresolved = detect_collisions_for_device_ids([53, 54], live_librenms.api)

        assert unresolved == {}
        assert [collision["nb_device_pk"] for collision in collisions] == [target.pk]
        assert {row["device_id"] for row in collisions[0]["librenms_rows"]} == {53, 54}

    def test_missing_live_device_is_unresolved(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import (
            LIBRENMS_ROW_UNREADABLE,
            detect_collisions_for_device_ids,
        )

        collisions, unresolved = detect_collisions_for_device_ids([404], live_librenms.api)

        assert collisions == []
        assert unresolved == {404: LIBRENMS_ROW_UNREADABLE}

    def test_mis_keyed_cached_row_fails_closed_without_http(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import (
            LIBRENMS_ROW_UNREADABLE,
            detect_collisions_for_device_ids,
        )

        collisions, unresolved = detect_collisions_for_device_ids(
            [61],
            live_librenms.api,
            libre_devices_cache={61: _libre_device(62, "wrong-row")},
        )

        assert collisions == []
        assert unresolved == {61: LIBRENMS_ROW_UNREADABLE}
        assert live_librenms.server.requests == []

    def test_fetched_identity_mismatch_is_not_written_to_shared_cache(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import (
            LIBRENMS_ROW_UNREADABLE,
            detect_collisions_for_device_ids,
        )

        live_librenms.server.register(
            "/api/v0/devices/71",
            {"status": "ok", "devices": [_libre_device(72, "wrong-live-row")]},
        )
        shared_cache = {}

        collisions, unresolved = detect_collisions_for_device_ids(
            [71],
            live_librenms.api,
            libre_devices_cache=shared_cache,
        )

        assert collisions == []
        assert unresolved == {71: LIBRENMS_ROW_UNREADABLE}
        assert shared_cache == {}


class TestBulkImportIntegration:
    def _prerequisites(self):
        seed = make_device("bulk-import-prerequisites")
        result = {
            "site_id": seed.site_id,
            "device_type_id": seed.device_type_id,
            "device_role_id": seed.role_id,
            "hardware": seed.device_type.model,
            "location": seed.site.name,
        }
        delete_keeping_pk(seed)
        return result

    def test_real_cached_device_is_imported_into_netbox(self, live_librenms):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices

        prerequisites = self._prerequisites()
        row = _libre_device(
            81,
            "bulk-new-device",
            hardware=prerequisites["hardware"],
            location=prerequisites["location"],
            serial="TEST-SERIAL-81",
        )
        _register_device(live_librenms, row)

        result = bulk_import_devices(
            [81],
            server_key="default",
            manual_mappings_per_device={
                81: {
                    "site_id": prerequisites["site_id"],
                    "device_type_id": prerequisites["device_type_id"],
                    "device_role_id": prerequisites["device_role_id"],
                }
            },
            libre_devices_cache={81: row},
            user=make_superuser("bulk-import-user"),
        )

        assert result["failed"] == []
        assert result["cancelled"] is False
        assert len(result["success"]) == 1
        imported = Device.objects.get(name="bulk-new-device")
        assert imported.serial == "TEST-SERIAL-81"
        assert stored_mapping_for_test(imported)["default"] == 81

    def test_a_later_import_row_sees_the_serial_created_earlier_in_its_batch(self, live_librenms):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices
        from netbox_librenms_plugin.models import NormalizationRule

        prerequisites = self._prerequisites()
        NormalizationRule.objects.filter(scope="serial").delete()
        NormalizationRule.objects.create(scope="serial", match_pattern=r"^PREFIX(KEY-.+)$", replacement=r"\1")
        rows = {
            device_id: _libre_device(
                device_id,
                f"live-serial-batch-{device_id}",
                serial=serial,
                hardware=prerequisites["hardware"],
                location=prerequisites["location"],
            )
            for device_id, serial in [(781, "PREFIXKEY-LIVE"), (782, "KEY-LIVE")]
        }
        for row in rows.values():
            _register_device(live_librenms, row)

        result = bulk_import_devices(
            list(rows),
            server_key="default",
            manual_mappings_per_device={device_id: prerequisites for device_id in rows},
            libre_devices_cache=rows,
            user=make_superuser("live-serial-batch-user"),
        )

        assert result["failed"] == []
        assert [row["device_id"] for row in result["success"]] == [781]
        assert [row["device_id"] for row in result["skipped"]] == [782]
        assert list(Device.objects.filter(name__startswith="live-serial-batch-").values_list("serial", flat=True)) == [
            "PREFIXKEY-LIVE"
        ]

    def test_a_batch_reuses_its_serial_rules_and_the_next_batch_sees_a_change(self, live_librenms):
        from dcim.models import Device, DeviceRole, DeviceType, Site
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.serial_identity import SerialRules
        from netbox_librenms_plugin.tests.test_virtual_chassis_fallbacks import _seed_stack
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        prerequisites = self._prerequisites()
        NormalizationRule.objects.filter(scope="serial").delete()
        rule = NormalizationRule.objects.create(
            scope="serial",
            match_pattern=r"^S/N\s+(.+)$",
            replacement=r"\1",
        )
        user = make_user_with_perms(
            "batch-serial-reader",
            [
                ("add", Device),
                ("change", Device),
                ("view", Device),
                ("view", DeviceType),
                ("view", DeviceRole),
                ("view", Site),
            ],
        )
        rows = {}
        for device_id in (181, 182, 183):
            serial = f"BATCH-{device_id}"
            rows[device_id] = _libre_device(
                device_id,
                f"batch-serial-{device_id}",
                serial=serial,
                hardware=prerequisites["hardware"],
                location=prerequisites["location"],
            )
            _seed_stack(live_librenms.server, device_id, serials=(f"S/N {serial}", f"PEER-{device_id}"))
            assert get_virtual_chassis_data(live_librenms.api, device_id, force_refresh=True)["is_stack"]
        table = f'"{NormalizationRule._meta.db_table}"'

        def run_batch():
            with CaptureQueriesContext(connection) as queries:
                result = bulk_import_devices(
                    list(rows),
                    server_key="default",
                    user=user,
                    manual_mappings_per_device={device_id: prerequisites for device_id in rows},
                    libre_devices_cache=dict(rows),
                )
            serial_reads = [q for q in queries if table in q["sql"] and "'serial'" in q["sql"]]
            return result, serial_reads

        with CaptureQueriesContext(connection) as key_queries:
            SerialRules.load().candidates()
        one_key_read_count = len([q for q in key_queries if table in q["sql"] and "'serial'" in q["sql"]])
        assert one_key_read_count > 0

        first, reads = run_batch()
        assert len(first["failed"]) == 3
        assert all("dcim.add_virtualchassis" in row["error"] for row in first["failed"])
        assert len(reads) == one_key_read_count
        rule.match_pattern = r"^SN:\s+(.+)$"
        rule.save()

        second, reads = run_batch()
        assert second["failed"] == []
        assert len(second["success"]) == 3
        assert second["virtual_chassis_created"] == 0
        assert len(reads) == one_key_read_count
        assert Device.objects.filter(name__startswith="batch-serial-", virtual_chassis__isnull=True).count() == 3

    def test_missing_live_device_is_reported_failed(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices

        result = bulk_import_devices([404], server_key="default", user=make_superuser("bulk-missing"))

        assert result["success"] == []
        assert result["failed"] == [{"device_id": 404, "error": "Failed to retrieve device 404 from LibreNMS"}]

    def test_mis_keyed_cache_is_refetched_before_import(self, live_librenms):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices

        prerequisites = self._prerequisites()
        live_row = _libre_device(
            91,
            "bulk-refetched-device",
            hardware=prerequisites["hardware"],
            location=prerequisites["location"],
        )
        _register_device(live_librenms, live_row)
        shared_cache = {91: _libre_device(92, "wrong-cached-device")}

        result = bulk_import_devices(
            [91],
            server_key="default",
            manual_mappings_per_device={91: prerequisites},
            libre_devices_cache=shared_cache,
            user=make_superuser("bulk-refetch"),
        )

        assert result["failed"] == []
        assert Device.objects.filter(name="bulk-refetched-device").exists()
        assert shared_cache[91] == live_row
        assert live_librenms.server.requests[0]["path"] == "/api/v0/devices/91"


class TestRefreshLibreNMSLinkage:
    def test_host_mapping_promotes_a_stale_hostname_match(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        device = make_device("refresh-host-link")
        seed_stored_mapping(device, {"default": {"id": 101}}, save=True)
        validation = _validation(device)

        _refresh_librenms_linkage(validation, device, {"device_id": 101}, "default")

        assert validation["existing_match_type"] == "librenms_id"
        assert validation["existing_librenms_link"]["host_id"] == 101

    def test_oob_mapping_is_classified_separately(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        device = make_device("refresh-oob-link")
        seed_stored_mapping(device, {"default": {"id": 101, "oob": {"id": 102, "type": "controller"}}}, save=True)
        validation = _validation(device)

        _refresh_librenms_linkage(validation, device, {"device_id": 102}, "default")

        assert validation["existing_match_type"] == "librenms_oob"
        assert validation["existing_librenms_link"]["oob_id"] == 102

    def test_repointed_mapping_neutralizes_a_stale_id_match(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        device = make_device("refresh-repointed-link")
        seed_mapping(device, "default", own=103)
        validation = _validation(device)
        validation["existing_match_type"] = "librenms_id"

        _refresh_librenms_linkage(validation, device, {"device_id": 104}, "default")

        assert validation["existing_match_type"] is None

    def test_missing_scanned_id_preserves_a_live_mapping(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        device = make_device("refresh-missing-scan-id")
        seed_mapping(device, "default", own=105)
        validation = _validation(device)
        validation["existing_match_type"] = "librenms_id"

        _refresh_librenms_linkage(validation, device, {}, "default")

        assert validation["existing_match_type"] == "librenms_id"


class TestRefreshExistingDevice:
    def test_real_device_is_reloaded_and_stays_non_importable(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        device = make_device("refresh-existing")
        validation = _validation(device)
        device.serial = "UPDATED-SERIAL"
        device.save(update_fields=["serial"])

        _refresh_existing_device(validation, _libre_device(111, device.name), "default")

        assert validation["existing_device"].serial == "UPDATED-SERIAL"
        assert validation["can_import"] is False
        assert validation["is_ready"] is False
        assert validation["device_role"]["role"] == device.role

    def test_deleted_device_clears_match_actions_and_reasserts_role_blocker(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        device = make_device("refresh-deleted")
        validation = _validation(device)
        validation.update(
            {
                "serial_action": "merge_netbox_devices",
                "oob_candidate": device,
                "merge_candidates": [device],
                "promote_to_host": {"existing_device": device},
            }
        )
        delete_keeping_pk(device)

        _refresh_existing_device(validation, None, "default")

        assert validation["existing_device"] is None
        assert validation["existing_match_type"] is None
        assert validation["serial_action"] is None
        assert validation["merge_candidates"] is None
        assert "promote_to_host" not in validation
        assert validation["device_role"]["role"] is None
        assert "Device role must be manually selected before import" in validation["issues"]
        assert validation["can_import"] is False

    def test_deleted_vm_clears_cluster_and_uses_matched_site_placement(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        vm = make_vm("refresh-deleted-vm")
        validation = _validation(vm, import_as_vm=True)
        delete_keeping_pk(vm)

        _refresh_existing_device(validation, None, "default")

        assert validation["existing_device"] is None
        assert validation["cluster"]["cluster"] is None
        assert validation["vm_placement"] == {"method": "site", "found": True, "host_device": None}
        assert validation["issues"] == []
        assert validation["can_import"] is True

    def test_newly_imported_device_is_found_by_current_librenms_id(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        device = make_device("refresh-newly-imported")
        seed_mapping(device, "default", own=121)
        validation = _validation()

        _refresh_existing_device(validation, _libre_device(121, "different-name"), "default")

        assert validation["existing_device"] == device
        assert validation["existing_match_type"] == "librenms_id"
        assert validation["can_import"] is False


class TestRefreshStateHelpers:
    def test_clear_match_fields_removes_every_derived_action(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _clear_existing_match_derived_fields

        validation = {
            "serial_action": "merge_netbox_devices",
            "oob_candidate": object(),
            "merge_candidates": [object()],
            "promote_to_host": object(),
            "librenms_id_needs_migration": True,
            "device_type_mismatch": True,
        }

        _clear_existing_match_derived_fields(validation)

        assert validation["serial_action"] is None
        assert validation["oob_candidate"] is None
        assert validation["merge_candidates"] is None
        assert "promote_to_host" not in validation
        assert validation["librenms_id_needs_migration"] is False
        assert validation["device_type_mismatch"] is False

    def test_role_and_cluster_resets_preserve_available_choices(self):
        from netbox_librenms_plugin.import_validation_helpers import reset_cluster, reset_device_role

        validation = {
            "device_role": {"found": True, "role": object(), "available_roles": ["role"]},
            "cluster": {"found": True, "cluster": object(), "available_clusters": ["cluster"]},
        }

        reset_device_role(validation)
        reset_cluster(validation)

        assert validation["device_role"] == {
            "found": False,
            "role": None,
            "available_roles": ["role"],
        }
        assert validation["cluster"] == {
            "found": False,
            "cluster": None,
            "available_clusters": ["cluster"],
        }

    @pytest.mark.parametrize(
        ("import_as_vm", "selection_key", "message"),
        [
            (False, "device_role", "Device role must be manually selected before import"),
            (True, "vm_placement", "VM placement requires a matching site, selected cluster, or selected host device"),
        ],
    )
    def test_new_import_blocker_matches_the_import_mode(self, import_as_vm, selection_key, message):
        from netbox_librenms_plugin.import_utils.bulk_import import _reassert_new_import_blockers

        validation = {
            "import_as_vm": import_as_vm,
            "issues": [],
            selection_key: {"found": False},
        }

        _reassert_new_import_blockers(validation)
        _reassert_new_import_blockers(validation)

        assert validation["issues"] == [message]


class TestProcessDeviceFilters:
    @pytest.mark.parametrize("unmatched_count", [2, 8])
    @pytest.mark.parametrize("rewrite", [False, True], ids=["indexed", "rewritten"])
    def test_serial_filter_batch_loads_each_unrelated_candidate_at_most_once(
        self, live_librenms, monkeypatch, unmatched_count, rewrite
    ):
        from collections import Counter

        from dcim.models import Device
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.filter(scope="serial").delete()
        if rewrite:
            NormalizationRule.objects.create(scope="serial", match_pattern=r"^PREFIX(KEY-.+)$", replacement=r"\1")
        candidates = [
            make_device(f"filter-serial-candidate-{index}", serial=f"UNRELATED-{index}") for index in range(12)
        ]
        holder = make_device("filter-serial-rewrite-holder", serial="PREFIXKEY-MATCH" if rewrite else "KEY-MATCH")
        rows = [
            _libre_device(6900 + index, f"filter-serial-incoming-{index}", serial=f"MISSING-{index}")
            for index in range(unmatched_count)
        ]
        rows.append(_libre_device(6999, "filter-serial-incoming-match", serial="KEY-MATCH"))
        for row in rows:
            _register_device(live_librenms, row)
        live_librenms.server.register("/api/v0/devices", {"status": "ok", "devices": rows})
        candidate_pks = {device.pk for device in candidates}
        visits = Counter()
        from_db = Device.from_db

        def count_candidate_loads(cls, db, field_names, values, **kwargs):
            device = from_db(db, field_names, values, **kwargs)
            if device.pk in candidate_pks:
                visits[device.pk] += 1
            return device

        monkeypatch.setattr(Device, "from_db", classmethod(count_candidate_loads))

        with CaptureQueriesContext(connection) as captured:
            validated = process_device_filters(
                live_librenms.api, {}, vc_detection_enabled=False, clear_cache=True, show_disabled=True
            )

        # A serial predicate in the WHERE clause is a candidate read: the batch LIKE ANY form and an exact lookup.
        candidate_queries = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].startswith("SELECT")
            and '"dcim_device"."serial"' in query["sql"].partition(" FROM ")[2].partition(" WHERE ")[2]
        ]
        # The rewritten holder stores another form under another name, so its Update & Link offer runs the write lookup.
        assert len(candidate_queries) == (2 if rewrite else 1)
        assert all(row["_validation"]["existing_device"] is None for row in validated[:-1])
        assert validated[-1]["_validation"]["existing_device"].pk == holder.pk
        assert all(count <= 1 for count in visits.values()), dict(visits)
        if not rewrite:
            assert not visits
        visits.clear()

        cached = process_device_filters(
            live_librenms.api, {}, vc_detection_enabled=False, clear_cache=False, show_disabled=True
        )

        assert len(cached) == len(validated)
        assert all(count <= 1 for count in visits.values()), dict(visits)
        if not rewrite:
            assert not visits

    @pytest.mark.parametrize("context", ["holder", "vc_master"])
    def test_serial_filter_uses_each_holders_manufacturer_context(self, live_librenms, context):
        from dcim.models import DeviceType, Manufacturer, VirtualChassis

        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.filter(scope="serial").delete()
        manufacturer = Manufacturer.objects.create(name="Serial Context Maker", slug="serial-context-maker")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Serial Context", slug="serial-context"
        )
        NormalizationRule.objects.create(
            scope="serial", manufacturer=manufacturer, match_pattern=r"^PREFIX(KEY-.+)$", replacement=r"\1"
        )
        holder = make_device("filter-context-holder", serial="PREFIXKEY-CONTEXT")
        if context == "holder":
            holder.device_type = device_type
            holder.save(update_fields=["device_type"])
        else:
            master = make_device("filter-context-master", serial="MASTER-CONTEXT")
            master.device_type = device_type
            master.save(update_fields=["device_type"])
            chassis = VirtualChassis.objects.create(name="Filter serial context", master=master)
            for position, device in enumerate([master, holder], 1):
                device.virtual_chassis = chassis
                device.vc_position = position
                device.save()
        row = _libre_device(791, "filter-context-incoming", serial="KEY-CONTEXT")
        _register_device(live_librenms, row)
        live_librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [row]})

        (validated,) = process_device_filters(
            live_librenms.api, {}, vc_detection_enabled=False, clear_cache=True, show_disabled=True
        )

        assert validated["_validation"]["existing_device"].pk == holder.pk
        assert validated["_validation"]["existing_match_type"] == "serial"

    def test_serial_filter_applies_its_duplicate_limit_after_excluding_the_named_holder(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.filter(scope="serial").delete()
        NormalizationRule.objects.create(scope="serial", match_pattern=r"^PREFIX(KEY-.+)$", replacement=r"\1")
        named = make_device("filter-serial-named", serial="PREFIXKEY-DUPLICATE")
        make_device("filter-serial-first-peer", serial="PREFIXKEY-DUPLICATE")
        make_device("filter-serial-second-peer", serial="KEY-DUPLICATE")
        row = _libre_device(792, named.name, serial="KEY-DUPLICATE")
        _register_device(live_librenms, row)
        live_librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [row]})

        (validated,) = process_device_filters(
            live_librenms.api, {}, vc_detection_enabled=False, clear_cache=True, show_disabled=True
        )

        assert validated["_validation"]["existing_device"].pk == named.pk
        assert validated["_validation"]["merge_candidates"] is None
        assert any(
            "Multiple NetBox devices share serial" in warning for warning in validated["_validation"]["warnings"]
        )

    def test_live_list_is_validated_cached_and_reused(self, live_librenms, monkeypatch):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters
        from netbox_librenms_plugin.import_utils.cache import get_validated_device_cache_key
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.create(scope="serial", match_pattern=r"^PREFIX(KEY-.+)$", replacement=r"\1")
        unrelated = make_device("filter-cached-unrelated", serial="UNRELATED-CACHED")
        live_librenms.api.cache_timeout = 300
        existing = make_device("filter-existing")
        seed_mapping(existing, "default", own=131)
        row = _libre_device(
            131,
            existing.name,
            hardware=existing.device_type.model,
            serial="KEY-CACHED",
            location=existing.site.name,
        )
        live_librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [row]})
        filters = {"hostname": existing.name}

        first, first_from_cache = process_device_filters(
            live_librenms.api,
            filters,
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=True,
            return_cache_status=True,
        )
        key = get_validated_device_cache_key(live_librenms.api.server_key, filters, 131, vc_enabled=False)
        cached = cache.get(key)
        cached["_validation"]["cache_marker"] = True
        cache.set(key, cached, timeout=live_librenms.api.cache_timeout)
        request_count = len(live_librenms.server.requests)
        visits = []
        from_db = Device.from_db

        def count_candidate_loads(cls, db, field_names, values, **kwargs):
            device = from_db(db, field_names, values, **kwargs)
            if device.pk == unrelated.pk:
                visits.append(device.pk)
            return device

        monkeypatch.setattr(Device, "from_db", classmethod(count_candidate_loads))
        second, second_from_cache = process_device_filters(
            live_librenms.api,
            filters,
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=True,
            return_cache_status=True,
        )

        assert first_from_cache is False
        assert second_from_cache is True
        assert first[0]["_validation"]["existing_device"] == existing
        assert second[0]["_validation"].get("cache_marker") is True
        assert second[0]["_validation"]["existing_device"] == existing
        assert len(live_librenms.server.requests) == request_count
        assert not visits

    def test_disabled_rows_are_removed_before_validation(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

        enabled = _libre_device(141, "enabled-device")
        disabled = _libre_device(142, "disabled-device", disabled=1)
        live_librenms.server.register(
            "/api/v0/devices",
            {"status": "ok", "devices": [enabled, disabled]},
        )

        rows = process_device_filters(
            live_librenms.api,
            {},
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=False,
        )

        assert [row["device_id"] for row in rows] == [141]

    def test_exclude_existing_removes_a_real_matched_device(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

        existing = make_device("filter-excluded")
        seed_mapping(existing, "default", own=151)
        row = _libre_device(
            151,
            existing.name,
            hardware=existing.device_type.model,
            location=existing.site.name,
        )
        live_librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [row]})

        rows = process_device_filters(
            live_librenms.api,
            {"hostname": existing.name},
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=True,
            exclude_existing=True,
        )

        assert rows == []

    def test_fresh_validation_writes_metadata_and_server_index(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters
        from netbox_librenms_plugin.import_utils.cache import get_cache_index_key, get_cache_metadata_key

        live_librenms.api.cache_timeout = 300
        row = _libre_device(161, "filter-metadata")
        live_librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [row]})
        filters = {"hostname": "filter-metadata"}
        metadata_key = get_cache_metadata_key(
            server_key="default",
            filters=filters,
            vc_enabled=False,
            use_sysname=True,
            strip_domain=False,
        )

        rows = process_device_filters(
            live_librenms.api,
            filters,
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=True,
        )

        metadata = cache.get(metadata_key)
        assert len(rows) == 1
        assert metadata["device_count"] == 1
        assert metadata["server_key"] == "default"
        assert metadata_key in cache.get(get_cache_index_key("default"))


@pytest.mark.django_db
def test_import_validation_reads_location_parse_settings_once():
    """Site and rack validation must share the same parsed location."""
    from django.apps import apps
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

    settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
    settings_model.objects.update_or_create(
        pk=1, defaults={"location_parse_pattern": "", "location_parse_is_regex": False}
    )
    device = make_device("import-location-source")
    with CaptureQueriesContext(connection) as queries:
        result = validate_device_for_import(
            {
                "device_id": 8100,
                "hostname": "import-location-target",
                "sysName": "import-location-target",
                "location": device.site.name,
                "hardware": device.device_type.model,
            },
            include_vc_detection=False,
        )
    assert result["site"]["found"]
    assert result["site"]["site"] == device.site
    settings_reads = [
        query["sql"]
        for query in queries
        if f'FROM "{settings_model._meta.db_table}"' in query["sql"] and "location_parse_pattern" in query["sql"]
    ]
    assert len(settings_reads) == 1, settings_reads


@pytest.mark.django_db
def test_bulk_confirmation_reuses_serial_rules_and_refreshes_them_next_request(client, live_librenms):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from django.urls import reverse

    from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key
    from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data
    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.test_virtual_chassis_fallbacks import _seed_stack

    prerequisites = TestBulkImportIntegration()._prerequisites()
    NormalizationRule.objects.filter(scope="serial").delete()
    rule = NormalizationRule.objects.create(scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1")
    rows = {}
    for device_id in (281, 282, 283):
        serial = f"CONFIRM-{device_id}"
        rows[device_id] = _libre_device(
            device_id,
            f"confirm-rules-{device_id}",
            serial=serial,
            hardware=prerequisites["hardware"],
            location=prerequisites["location"],
        )
        _seed_stack(live_librenms.server, device_id, serials=(f"S/N {serial}", f"PEER-{device_id}"))
        cache.set(get_import_device_cache_key(device_id, "default"), rows[device_id], timeout=300)
        assert get_virtual_chassis_data(live_librenms.api, device_id, force_refresh=True)["is_stack"]
    client.force_login(make_superuser("confirm-rules-user"))
    table = f'"{NormalizationRule._meta.db_table}"'

    def confirm(device_ids):
        with CaptureQueriesContext(connection) as queries:
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:bulk_import_confirm"),
                {"server_key": "default", "select": [str(device_id) for device_id in device_ids]},
                HTTP_HX_REQUEST="true",
            )
        assert response.status_code == 200
        entries = response.context["devices"]
        assert len(entries) == len(device_ids)
        reads = [q for q in queries if table in q["sql"] and "'serial'" in q["sql"]]
        return entries, len(reads)

    _single, one_key_reads = confirm([281])
    entries, batch_reads = confirm(list(rows))
    assert all(entry["validation"]["virtual_chassis"]["master_identified"] for entry in entries)
    assert batch_reads == one_key_reads > 0
    rule.match_pattern = r"^SN:\s+(.+)$"
    rule.save()
    entries, batch_reads = confirm(list(rows))
    assert all(not entry["validation"]["virtual_chassis"]["master_identified"] for entry in entries)
    assert batch_reads == one_key_reads


@pytest.mark.django_db
@pytest.mark.parametrize("fits", [True, False])
def test_the_bulk_confirmation_blocks_a_new_device_whose_serial_does_not_fit(client, live_librenms, fits):
    """The import stores the LibreNMS serial, and the field holds 50 characters."""
    from django.urls import reverse
    from django.utils.html import escape

    from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key

    prerequisites = TestBulkImportIntegration()._prerequisites()
    device_id = 291 if fits else 292
    row = _libre_device(
        device_id,
        f"confirm-fit-{fits}",
        serial="N" * (50 if fits else 51),
        hardware=prerequisites["hardware"],
        location=prerequisites["location"],
    )
    _register_device(live_librenms, row)
    cache.set(get_import_device_cache_key(device_id, "default"), row, timeout=300)
    client.force_login(make_superuser(f"confirm-fit-user-{fits}"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_confirm"),
        {"server_key": "default", "select": [str(device_id)]},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    (entry,) = response.context["devices"]
    reason = "The LibreNMS serial has 51 characters; the field holds at most 50."
    assert (reason in entry["validation"]["issues"]) is (not fits)
    assert (escape(reason) in response.content.decode()) is (not fits)


@pytest.mark.django_db
@pytest.mark.parametrize("fits", [True, False])
def test_the_bulk_confirmation_blocks_a_stack_whose_member_serial_does_not_fit(client, live_librenms, fits):
    """The import creates each stack member with its LibreNMS serial, and the field holds 50 characters."""
    from django.urls import reverse

    from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key
    from netbox_librenms_plugin.tests.test_virtual_chassis_fallbacks import _seed_stack

    prerequisites = TestBulkImportIntegration()._prerequisites()
    device_id = 293 if fits else 294
    row = _libre_device(
        device_id,
        f"confirm-stack-fit-{fits}",
        serial=f"STACK-MASTER-{device_id}",
        hardware=prerequisites["hardware"],
        location=prerequisites["location"],
    )
    _seed_stack(live_librenms.server, device_id, serials=(row["serial"], "N" * (50 if fits else 51)))
    cache.set(get_import_device_cache_key(device_id, "default"), row, timeout=300)
    client.force_login(make_superuser(f"confirm-stack-fit-user-{fits}"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_confirm"),
        {"server_key": "default", "select": [str(device_id)]},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    (entry,) = response.context["devices"]
    assert entry["validation"]["virtual_chassis"]["master_identified"]
    reason = "Stack member 2: The LibreNMS serial has 51 characters; the field holds at most 50."
    assert (reason in entry["validation"]["issues"]) is (not fits)


@pytest.mark.django_db
@pytest.mark.parametrize("held", [True, False])
def test_the_bulk_confirmation_checks_only_the_stack_members_that_the_import_creates(client, live_librenms, held):
    """A member serial that keys to an existing Device is skipped at import, so its length blocks nothing."""
    from django.urls import reverse

    from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key
    from netbox_librenms_plugin.tests.test_virtual_chassis_fallbacks import _seed_stack, _seeded_serial_rule

    _seeded_serial_rule()
    prerequisites = TestBulkImportIntegration()._prerequisites()
    if held:
        make_device("confirm-stack-holder", serial="N" * 47)
    device_id = 295 if held else 296
    row = _libre_device(
        device_id,
        f"confirm-stack-held-{held}",
        serial=f"STACK-MASTER-{device_id}",
        hardware=prerequisites["hardware"],
        location=prerequisites["location"],
    )
    _seed_stack(live_librenms.server, device_id, serials=(row["serial"], f"S/N {'N' * 47}"))
    cache.set(get_import_device_cache_key(device_id, "default"), row, timeout=300)
    client.force_login(make_superuser(f"confirm-stack-held-user-{held}"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_confirm"),
        {"server_key": "default", "select": [str(device_id)]},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    (entry,) = response.context["devices"]
    assert entry["validation"]["virtual_chassis"]["master_identified"]
    reason = "Stack member 2: The LibreNMS serial has 51 characters; the field holds at most 50."
    assert (reason in entry["validation"]["issues"]) is (not held)


@pytest.mark.django_db
@pytest.mark.parametrize("unavailable", [True, False])
def test_the_bulk_confirmation_blocks_a_stack_member_whose_serial_lookup_is_unavailable(
    client, live_librenms, unavailable
):
    """The member writer refuses a member serial that a broken chassis master link hides; validation says so."""
    from django.urls import reverse

    from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key
    from netbox_librenms_plugin.serial_identity import UNAVAILABLE_MASTER_LINK
    from netbox_librenms_plugin.tests.test_coverage_device_operations import _stray_chassis_member
    from netbox_librenms_plugin.tests.test_virtual_chassis_fallbacks import _seed_stack

    prerequisites = TestBulkImportIntegration()._prerequisites()
    member_serial = f"STRAY-MEMBER-{unavailable}"
    if unavailable:
        _stray_chassis_member("confirm-stray", member_serial)
    device_id = 297 if unavailable else 298
    row = _libre_device(
        device_id,
        f"confirm-stack-stray-{unavailable}",
        serial=f"STACK-MASTER-{device_id}",
        hardware=prerequisites["hardware"],
        location=prerequisites["location"],
    )
    _seed_stack(live_librenms.server, device_id, serials=(row["serial"], member_serial))
    cache.set(get_import_device_cache_key(device_id, "default"), row, timeout=300)
    client.force_login(make_superuser(f"confirm-stack-stray-user-{unavailable}"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_confirm"),
        {"server_key": "default", "select": [str(device_id)]},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    (entry,) = response.context["devices"]
    assert entry["validation"]["virtual_chassis"]["master_identified"]
    # The text of the member writer's refusal.
    issue = f"Stack member 2: Cannot check VC member serial '{member_serial}': {UNAVAILABLE_MASTER_LINK}"
    assert (issue in entry["validation"]["issues"]) is unavailable
