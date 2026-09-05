"""Bulk-import behavior tests against real HTTP, cache, and NetBox rows."""

import pytest
from django.core.cache import cache

from netbox_librenms_plugin.tests.conftest import (
    delete_keeping_pk,
    make_device,
    make_superuser,
    make_vm,
)


class TestStackDedupKey:
    """One key per physical stack, and never one key shared by unrelated stacks."""

    def _members(self, **overrides):
        member = {"name": "sw1", "model": "C9300", "position": 1, "serial": "FOC1"}
        member.update(overrides)
        return [member]

    def test_member_serials_key_the_stack(self):
        from netbox_librenms_plugin.import_utils.bulk_import import stack_dedup_key

        assert stack_dedup_key({"members": self._members()}, 7) == "librenms-stack-FOC1"

    def test_every_device_of_one_stack_shares_the_key(self):
        """Each stack member is its own LibreNMS device, so the key must not depend on which."""
        from netbox_librenms_plugin.import_utils.bulk_import import stack_dedup_key

        vc_data = {"members": self._members(serial=None)}

        assert stack_dedup_key(vc_data, 7) == stack_dedup_key(vc_data, 8)

    def test_stacks_without_member_identity_get_distinct_keys(self):
        """An empty member list fingerprints to a constant, so a shared key let the first such
        stack suppress virtual-chassis creation for every other one in the batch."""
        from netbox_librenms_plugin.import_utils.bulk_import import stack_dedup_key

        first = stack_dedup_key({"members": []}, 7)
        second = stack_dedup_key({"members": []}, 8)

        assert first != second
        assert str(7) in first and str(8) in second


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

        outcome = classify_bulk_precheck([], [], [1, 2], {3: {"cluster_id": 9}})

        assert outcome.blocked is False
        assert outcome.importable_device_ids == [1, 2]
        assert outcome.importable_vm_imports == {3: {"cluster_id": 9}}
        assert outcome.skip_message == ""
        assert outcome.block_message == ""

    def test_unresolved_rows_are_skipped_without_blocking_the_rest(self):
        from netbox_librenms_plugin.import_utils.bulk_import import classify_bulk_precheck

        outcome = classify_bulk_precheck([], [2, 4], [1, 2, 3], {4: {}, 5: {}})

        assert outcome.blocked is False
        assert outcome.skipped_ids == [2, 4]
        assert outcome.importable_device_ids == [1, 3]
        assert outcome.importable_vm_imports == {5: {}}
        assert "id(s): 2, 4" in outcome.skip_message

    def test_collision_blocks_the_whole_batch_and_names_visible_targets(self):
        from netbox_librenms_plugin.import_utils.bulk_import import classify_bulk_precheck

        collisions = [
            {
                "nb_device_pk": 17,
                "target_visible": True,
                "librenms_devices": [{"device_id": 1}, {"device_id": 2}],
            }
        ]

        outcome = classify_bulk_precheck(collisions, [3], [1, 2, 3], {})

        assert outcome.blocked is True
        assert outcome.importable_device_ids == [1, 2]
        assert "Visible pk(s): 17" in outcome.block_message
        assert outcome.skip_message == ""

    def test_hidden_collision_does_not_disclose_the_target(self):
        from netbox_librenms_plugin.import_utils.bulk_import import classify_bulk_precheck

        outcome = classify_bulk_precheck(
            [{"nb_device_pk": None, "target_visible": False, "librenms_devices": []}],
            [],
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

        assert unresolved == []
        assert len(collisions) == 1
        assert collisions[0]["nb_device_pk"] == target.pk
        assert {row["device_id"] for row in collisions[0]["librenms_rows"]} == {41, 42}
        assert shared_cache == {41: first, 42: second}
        assert [request["path"] for request in live_librenms.server.requests] == [
            "/api/v0/devices/41",
            "/api/v0/devices/42",
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
        assert unresolved == []

    def test_missing_live_device_is_unresolved(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        collisions, unresolved = detect_collisions_for_device_ids([404], live_librenms.api)

        assert collisions == []
        assert unresolved == [404]

    def test_mis_keyed_cached_row_fails_closed_without_http(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        collisions, unresolved = detect_collisions_for_device_ids(
            [61],
            live_librenms.api,
            libre_devices_cache={61: _libre_device(62, "wrong-row")},
        )

        assert collisions == []
        assert unresolved == [61]
        assert live_librenms.server.requests == []

    def test_fetched_identity_mismatch_is_not_written_to_shared_cache(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

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
        assert unresolved == [71]
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
        assert imported.custom_field_data["librenms_id"]["default"] == 81

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
        device.custom_field_data["librenms_id"] = {"default": {"id": 101}}
        device.save(update_fields=["custom_field_data"])
        validation = _validation(device)

        _refresh_librenms_linkage(validation, device, {"device_id": 101}, "default")

        assert validation["existing_match_type"] == "librenms_id"
        assert validation["existing_librenms_link"]["host_id"] == 101

    def test_oob_mapping_is_classified_separately(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        device = make_device("refresh-oob-link")
        device.custom_field_data["librenms_id"] = {"default": {"id": 101, "oob": {"id": 102, "type": "controller"}}}
        device.save(update_fields=["custom_field_data"])
        validation = _validation(device)

        _refresh_librenms_linkage(validation, device, {"device_id": 102}, "default")

        assert validation["existing_match_type"] == "librenms_oob"
        assert validation["existing_librenms_link"]["oob_id"] == 102

    def test_repointed_mapping_neutralizes_a_stale_id_match(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        device = make_device("refresh-repointed-link")
        _set_mapping = {"default": 103}
        device.custom_field_data["librenms_id"] = _set_mapping
        device.save(update_fields=["custom_field_data"])
        validation = _validation(device)
        validation["existing_match_type"] = "librenms_id"

        _refresh_librenms_linkage(validation, device, {"device_id": 104}, "default")

        assert validation["existing_match_type"] is None

    def test_missing_scanned_id_preserves_a_live_mapping(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        device = make_device("refresh-missing-scan-id")
        device.custom_field_data["librenms_id"] = {"default": 105}
        device.save(update_fields=["custom_field_data"])
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
        device.custom_field_data["librenms_id"] = {"default": 121}
        device.save(update_fields=["custom_field_data"])
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
        from netbox_librenms_plugin.import_utils.bulk_import import _reset_cluster, _reset_device_role

        validation = {
            "device_role": {"found": True, "role": object(), "available_roles": ["role"]},
            "cluster": {"found": True, "cluster": object(), "available_clusters": ["cluster"]},
        }

        _reset_device_role(validation)
        _reset_cluster(validation)

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
    def test_live_list_is_validated_cached_and_reused(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

        live_librenms.api.cache_timeout = 300
        existing = make_device("filter-existing")
        existing.custom_field_data["librenms_id"] = {"default": 131}
        existing.save(update_fields=["custom_field_data"])
        row = _libre_device(
            131,
            existing.name,
            hardware=existing.device_type.model,
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

        # Count only data requests: the reachability pre-flight asks /system once per call,
        # which says nothing about whether the device list came from cache.
        def data_requests():
            return [r for r in live_librenms.server.requests if r["path"] != "/api/v0/system"]

        request_count = len(data_requests())
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
        assert second[0]["_validation"]["existing_device"] == existing
        assert len(data_requests()) == request_count

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
        existing.custom_field_data["librenms_id"] = {"default": 151}
        existing.save(update_fields=["custom_field_data"])
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
    settings_reads = [query["sql"] for query in queries if f'FROM "{settings_model._meta.db_table}"' in query["sql"]]
    assert len(settings_reads) == 1, settings_reads
