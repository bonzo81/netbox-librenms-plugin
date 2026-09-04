"""Real-DB tests for the cached import row's existing-device refresh.

Companion to ``test_coverage_bulk_import.py``; see that file for the primary
``import_utils/bulk_import.py`` coverage.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip, make_vm

pytestmark = pytest.mark.django_db


def _validation(existing=None, *, import_as_vm=False):
    """A cached import-row validation shaped like validate_device_for_import's output."""
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
        "warnings": [],
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
        "virtual_chassis": {"is_stack": False, "members": []},
    }


def _refresh(validation, libre_device=None, server_key="default"):
    from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

    _refresh_existing_device(validation, libre_device=libre_device, server_key=server_key)


def _link(device, payload):
    device.custom_field_data["librenms_id"] = {"default": payload}
    device.save(update_fields=["custom_field_data"])
    return device


class TestLinkageRefresh:
    """A cached link match survives a missing scan id, but not a missing NetBox link."""

    def test_a_vanished_link_clears_the_cached_match_when_the_scan_id_is_missing(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        device = make_device("linkage-gone")
        validation = _validation(device)
        validation["existing_match_type"] = "librenms_id"

        _refresh_librenms_linkage(validation, device, {"hostname": "linkage-gone"}, "default")

        assert validation["existing_match_type"] is None
        assert validation["existing_librenms_link"]["host_id"] is None


class TestRefreshPromotesACachedMatch:
    """A row cached under a weaker match is promoted once NetBox carries the link."""

    def test_a_serial_match_promoted_to_a_host_link_drops_its_stale_action(self):
        device = _link(make_device("promote-host"), {"id": 6001})
        validation = _validation(device)
        validation["existing_match_type"] = "serial"
        validation["serial_action"] = "oob_candidate"
        validation["oob_candidate"] = {"device_id": 6001}

        _refresh(validation, {"device_id": 6001, "hostname": "promote-host"})

        assert validation["existing_match_type"] == "librenms_id"
        assert validation["serial_action"] is None
        assert validation["oob_candidate"] is None

    def test_a_serial_match_promoted_to_an_oob_link_drops_its_merge_candidates(self):
        device = _link(make_device("promote-oob"), {"id": 6001, "oob": {"id": 6002, "type": "drac"}})
        validation = _validation(device)
        validation["existing_match_type"] = "serial"
        validation["serial_action"] = "merge_netbox_devices"
        validation["merge_candidates"] = [{"pk": device.pk}]

        _refresh(validation, {"device_id": 6002, "hostname": "promote-oob"})

        assert validation["existing_match_type"] == "librenms_oob"
        assert validation["serial_action"] is None
        assert not validation["merge_candidates"]

    def test_a_row_already_cached_as_a_link_keeps_its_derived_action(self):
        device = _link(make_device("promote-noop"), {"id": 6003})
        validation = _validation(device)
        validation["existing_match_type"] = "librenms_id"
        validation["serial_action"] = "update_serial"

        _refresh(validation, {"device_id": 6003, "hostname": "promote-noop"})

        assert validation["existing_match_type"] == "librenms_id"
        assert validation["serial_action"] == "update_serial"


class TestRefreshDropsAVanishedLink:
    """A librenms_id link removed in NetBox drops the match and re-asserts the create blocker."""

    def _vanished(self, name, libre_device):
        device = make_device(name)
        validation = _validation(device)
        validation["existing_match_type"] = "librenms_id"
        validation["resolved_name"] = "no-such-netbox-name"
        _refresh(validation, libre_device)
        return validation

    def test_the_match_is_cleared_and_the_role_blocker_returns(self):
        validation = self._vanished(
            "vanished-link", {"device_id": 50, "hostname": "no-such-netbox-name", "sysName": "no-such-netbox-name"}
        )

        assert validation["existing_device"] is None
        assert validation["existing_match_type"] is None
        assert validation["existing_librenms_link"] is None
        assert "Device role must be manually selected before import" in validation["issues"]
        assert validation["can_import"] is False

    def test_without_a_scanned_row_the_blocker_is_still_re_asserted(self):
        device = make_device("vanished-no-scan")
        validation = _validation(device)
        validation["existing_match_type"] = "librenms_id"

        _refresh(validation, None)

        assert validation["existing_device"] is None
        assert "Device role must be manually selected before import" in validation["issues"]
        assert validation["can_import"] is False

    def test_a_vm_row_loses_its_stale_cluster_selection(self):
        from netbox_librenms_plugin.tests.conftest import make_cluster

        vm = make_vm("vanished-vm")
        cluster = make_cluster("vanished-vm-cluster")
        validation = _validation(vm, import_as_vm=True)
        validation["existing_match_type"] = "librenms_id"
        validation["cluster"] = {"found": True, "cluster": cluster, "available_clusters": [cluster]}

        _refresh(validation, None)

        assert validation["existing_device"] is None
        assert validation["cluster"]["found"] is False
        assert validation["cluster"]["available_clusters"] == [cluster]
        assert "Cluster must be manually selected before importing as VM" in validation["issues"]


class TestRefreshFailsClosedOnADatabaseError:
    """A refresh whose query fails reports it and leaves the cached row untouched."""

    def test_a_database_error_mid_refresh_is_logged_and_the_row_is_left_alone(self, caplog):
        from django.db import DatabaseError, connection

        device = make_device("refresh-db-error")
        validation = _validation(device)
        validation["existing_match_type"] = "hostname"
        validation["serial_action"] = "update_serial"

        def _fail_device_reads(execute, sql, params, many, context):
            if "dcim_device" in sql and sql.lstrip().upper().startswith("SELECT"):
                raise DatabaseError("connection lost mid-refresh")
            return execute(sql, params, many, context)

        with (
            caplog.at_level("ERROR", logger="netbox_librenms_plugin.import_utils.bulk_import"),
            connection.execute_wrapper(_fail_device_reads),
        ):
            _refresh(validation, {"device_id": 1, "hostname": device.name})

        assert any(
            f"Failed to refresh existing device (pk={device.pk})" in record.getMessage()
            for record in caplog.records
            if record.name == "netbox_librenms_plugin.import_utils.bulk_import"
        )
        # The guard returns instead of falling through, so nothing derived from the match moved.
        assert validation["existing_device"] == device
        assert validation["existing_match_type"] == "hostname"
        assert validation["serial_action"] == "update_serial"


class TestRefreshFreshLookup:
    """The fresh re-check binds by id, name, and IP, and fails closed on duplicates."""

    def _unmatched(self, name, **overrides):
        validation = _validation(None)
        validation["resolved_name"] = name
        validation["device_role"] = {"found": False, "role": None, "available_roles": []}
        validation.update(overrides)
        return validation

    def test_a_hostname_shared_by_two_sites_blocks_without_binding(self):
        from dcim.models import Device, Site

        anchor = make_device("dup-hostname")
        other_site = Site.objects.create(name="refresh-dup-site", slug="refresh-dup-site")
        Device.objects.create(
            name=anchor.name,
            device_type=anchor.device_type,
            role=anchor.role,
            site=other_site,
            status="active",
        )
        validation = self._unmatched(anchor.name)

        _refresh(validation, {"device_id": 999, "hostname": anchor.name, "sysName": anchor.name})

        assert validation["existing_device"] is None
        assert validation["existing_match_type"] == "ambiguous_hostname_or_serial"
        assert validation["can_import"] is False
        assert any("hostname/serial" in issue for issue in validation["issues"])
        # Terminal: only the duplicate blocker, never the create-time role blocker.
        assert not any("role must be manually selected" in issue.lower() for issue in validation["issues"])

    def test_a_librenms_id_bound_to_both_a_device_and_a_vm_blocks_the_row(self):
        _link(make_device("collide-device"), {"id": 77})
        vm = make_vm("collide-vm")
        vm.custom_field_data["librenms_id"] = {"default": {"id": 77}}
        vm.save(update_fields=["custom_field_data"])
        validation = self._unmatched("collide-device")

        _refresh(validation, {"device_id": 77, "hostname": "collide-device", "sysName": "collide-device"})

        assert validation["ambiguous_librenms_id"] is True
        assert validation["existing_match_type"] == "ambiguous_librenms_id"
        assert validation["can_import"] is False
        marker = "matches more than one existing NetBox record"
        assert any(marker in warning for warning in validation["warnings"])
        assert any(marker in issue for issue in validation["issues"])
        # Deduplicated: a second refresh must not stack the same message.
        _refresh(validation, {"device_id": 77, "hostname": "collide-device", "sysName": "collide-device"})
        assert sum(marker in issue for issue in validation["issues"]) == 1

    def test_a_name_that_only_exists_on_the_opposite_model_rebinds_the_row(self):
        vm = make_vm("cross-model-name")
        validation = self._unmatched(vm.name)

        _refresh(validation, {"device_id": 99, "hostname": vm.name, "sysName": vm.name})

        assert validation["existing_device"] == vm
        assert validation["import_as_vm"] is True
        assert validation["can_import"] is False

    def test_an_interface_ip_binds_the_row_by_management_address(self):
        device = make_device("refresh-ip-interface")
        interface = make_interface(device, "mgmt0")
        # A decoy row with the same address proves the resolver scans every net_host row.
        make_ip("198.51.100.9/24")
        make_ip("198.51.100.9/24", assigned_object=interface)
        validation = self._unmatched("no-such-name")

        _refresh(validation, {"device_id": 52, "hostname": "h2", "sysName": "h2", "ip": "198.51.100.9"})

        assert validation["existing_device"] == device
        assert validation["existing_match_type"] == "primary_ip"
        assert validation["can_import"] is False

    def test_an_oob_ip_binds_the_row_by_management_address(self):
        device = make_device("refresh-ip-oob")
        oob = make_ip("198.51.100.21/24")
        device.oob_ip = oob
        device.save(update_fields=["oob_ip"])
        validation = self._unmatched("no-such-oob-name")

        _refresh(validation, {"device_id": 53, "hostname": "h3", "sysName": "h3", "ip": "198.51.100.21"})

        assert validation["existing_device"] == device
        assert validation["existing_match_type"] == "primary_ip"
