"""
Tests for netbox_librenms_plugin.import_validation_helpers module.

Phase 2 tests covering validation state updates, model retrieval,
and selection extraction functions.
"""

import pytest
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device, make_vm


pytestmark = pytest.mark.django_db


def _role(name="Access Switch"):
    from dcim.models import DeviceRole

    return DeviceRole.objects.create(name=name, slug=name.lower().replace(" ", "-"))


# =============================================================================
# TestGetModelById - 4 tests
# =============================================================================


class TestFetchModelById:
    """Test generic model retrieval helper."""

    def test_fetch_model_by_id_success(self):
        """Return model instance when found."""
        from dcim.models import DeviceRole

        from netbox_librenms_plugin.import_validation_helpers import fetch_model_by_id

        role = _role()
        result = fetch_model_by_id(DeviceRole, role.pk)

        assert result == role

    def test_fetch_model_by_id_not_found(self):
        """Return None when ID doesn't exist."""
        from dcim.models import DeviceRole

        from netbox_librenms_plugin.import_validation_helpers import fetch_model_by_id

        result = fetch_model_by_id(DeviceRole, 999999)

        assert result is None

    def test_fetch_model_by_id_invalid_id(self):
        """Handle invalid ID gracefully."""
        from dcim.models import DeviceRole

        from netbox_librenms_plugin.import_validation_helpers import fetch_model_by_id

        result = fetch_model_by_id(DeviceRole, "not-a-number")

        assert result is None

    def test_fetch_model_by_id_none_id(self):
        """Handle None ID gracefully."""
        from dcim.models import DeviceRole

        from netbox_librenms_plugin.import_validation_helpers import fetch_model_by_id

        result = fetch_model_by_id(DeviceRole, None)

        assert result is None


# =============================================================================
# TestExtractSelections - 4 tests
# =============================================================================


class TestExtractDeviceSelections:
    """Test extraction of device selections from request."""

    def test_extract_selections_all_present(self):
        """All selections extracted from POST request."""
        from netbox_librenms_plugin.import_validation_helpers import (
            extract_device_selections,
        )

        request = RequestFactory().post(
            "/",
            {
                "cluster_1234": "5",
                "role_1234": "10",
                "rack_1234": "15",
            },
        )

        result = extract_device_selections(request, device_id=1234)

        assert result["cluster_id"] == "5"
        assert result["role_id"] == "10"
        assert result["rack_id"] == "15"

    def test_extract_selections_partial(self):
        """Missing fields return None."""
        from netbox_librenms_plugin.import_validation_helpers import (
            extract_device_selections,
        )

        request = RequestFactory().post(
            "/",
            {
                "role_1234": "10",
            },
        )

        result = extract_device_selections(request, device_id=1234)

        assert result["cluster_id"] is None
        assert result["role_id"] == "10"
        assert result["rack_id"] is None

    def test_extract_selections_from_get(self):
        """Selections extracted from GET request."""
        from netbox_librenms_plugin.import_validation_helpers import (
            extract_device_selections,
        )

        request = RequestFactory().get(
            "/",
            {
                "cluster_999": "3",
                "role_999": "7",
                "rack_999": "11",
            },
        )

        result = extract_device_selections(request, device_id=999)

        assert result["cluster_id"] == "3"
        assert result["role_id"] == "7"
        assert result["rack_id"] == "11"

    def test_extract_selections_empty_values(self):
        """Empty strings handled correctly."""
        from netbox_librenms_plugin.import_validation_helpers import (
            extract_device_selections,
        )

        request = RequestFactory().post(
            "/",
            {
                "cluster_1234": "",
                "role_1234": "",
                "rack_1234": "",
            },
        )

        result = extract_device_selections(request, device_id=1234)

        # Empty strings are returned as-is (caller decides meaning)
        assert result["cluster_id"] == ""
        assert result["role_id"] == ""
        assert result["rack_id"] == ""


# =============================================================================
# TestValidationStateUpdates - 10 tests
# =============================================================================


class TestValidationStateUpdates:
    """Test validation state mutation functions."""

    def test_apply_role_to_validation_success(self):
        """Role selection updates state correctly."""
        from netbox_librenms_plugin.import_validation_helpers import (
            apply_role_to_validation,
        )

        role = _role("Access Role")
        validation = {
            "device_role": {"found": False, "role": None},
            "issues": ["Device role must be manually selected before import"],
            "can_import": False,
            "is_ready": False,
            "site": {"found": True},
            "device_type": {"found": True},
        }

        apply_role_to_validation(validation, role, is_vm=False)

        assert validation["device_role"]["found"] is True
        assert validation["device_role"]["role"] == role

    def test_apply_role_to_validation_clears_issue(self):
        """Selecting role should clear 'role' related validation issue."""
        from netbox_librenms_plugin.import_validation_helpers import (
            apply_role_to_validation,
        )

        role = _role("Cleared Role")
        validation = {
            "device_role": {"found": False, "role": None},
            "issues": ["Device role must be manually selected before import"],
            "can_import": False,
            "is_ready": False,
            "site": {"found": True},
            "device_type": {"found": True},
        }

        apply_role_to_validation(validation, role, is_vm=False)

        assert len(validation["issues"]) == 0

    def test_apply_cluster_to_validation_success(self):
        """Cluster selection updates state for VM import."""
        from netbox_librenms_plugin.import_validation_helpers import (
            apply_cluster_to_validation,
        )

        cluster = make_vm("validation-cluster").cluster
        validation = {
            "cluster": {"found": False, "cluster": None},
            "issues": ["VM placement requires a matching site, selected cluster, or selected host device"],
            "can_import": False,
            "is_ready": False,
        }

        apply_cluster_to_validation(validation, cluster)

        assert validation["cluster"]["found"] is True
        assert validation["cluster"]["cluster"] == cluster
        assert validation["vm_placement"] == {
            "method": "cluster",
            "found": True,
            "host_device": None,
        }
        assert validation["issues"] == []
        assert validation["is_ready"] is True

    def test_apply_rack_to_validation_success(self):
        """Rack selection updates state for device import."""
        from netbox_librenms_plugin.import_validation_helpers import (
            apply_rack_to_validation,
        )

        from dcim.models import Rack

        device = make_device("validation-rack-device")
        rack = Rack.objects.create(name="Rack A1", site=device.site, status="active")
        validation = {
            "issues": [],
            "can_import": True,
            "is_ready": True,
        }

        apply_rack_to_validation(validation, rack)

        assert validation["rack"]["found"] is True
        assert validation["rack"]["rack"] == rack

    def test_remove_validation_issue_single(self):
        """Remove single issue by keyword."""
        from netbox_librenms_plugin.import_validation_helpers import (
            remove_validation_issue,
        )

        validation = {
            "issues": [
                "Device role must be manually selected before import",
                "Site not found for location 'DC1'",
            ]
        }

        remove_validation_issue(validation, "role")

        assert len(validation["issues"]) == 1
        assert "Site not found" in validation["issues"][0]

    def test_remove_validation_issue_multiple(self):
        """Remove multiple matching issues."""
        from netbox_librenms_plugin.import_validation_helpers import (
            remove_validation_issue,
        )

        validation = {
            "issues": [
                "Device role must be selected",
                "Role is required for import",
                "Site not found",
            ]
        }

        remove_validation_issue(validation, "role")

        assert len(validation["issues"]) == 1
        assert "Site not found" in validation["issues"][0]

    def test_remove_validation_issue_no_match(self):
        """No change when keyword not found."""
        from netbox_librenms_plugin.import_validation_helpers import (
            remove_validation_issue,
        )

        validation = {
            "issues": [
                "Site not found for location 'DC1'",
                "Device type not matched",
            ]
        }

        remove_validation_issue(validation, "cluster")

        assert len(validation["issues"]) == 2

    def test_recalculate_can_import_all_ready_device(self):
        """can_import=True when all requirements met for device."""
        from netbox_librenms_plugin.import_validation_helpers import (
            recalculate_validation_status,
        )

        validation = {
            "issues": [],
            "can_import": False,
            "is_ready": False,
            "site": {"found": True},
            "device_type": {"found": True},
            "device_role": {"found": True},
        }

        recalculate_validation_status(validation, is_vm=False)

        assert validation["can_import"] is True
        assert validation["is_ready"] is True

    def test_recalculate_can_import_missing_required_device(self):
        """can_import=False when required field missing for device."""
        from netbox_librenms_plugin.import_validation_helpers import (
            recalculate_validation_status,
        )

        validation = {
            "issues": ["Site not found"],
            "can_import": True,  # Should become False
            "is_ready": True,
            "site": {"found": False},
            "device_type": {"found": True},
            "device_role": {"found": True},
        }

        recalculate_validation_status(validation, is_vm=False)

        assert validation["can_import"] is False
        assert validation["is_ready"] is False

    def test_recalculate_can_import_vm_placement_ready(self):
        """VM import is ready with one resolved placement."""
        from netbox_librenms_plugin.import_validation_helpers import (
            recalculate_validation_status,
        )

        validation = {
            "issues": [],
            "can_import": False,
            "is_ready": False,
            "vm_placement": {"found": True},
        }

        recalculate_validation_status(validation, is_vm=True)

        assert validation["can_import"] is True
        assert validation["is_ready"] is True

    def test_recalculate_can_import_vm_missing_placement(self):
        """VM import is not ready without a resolved placement."""
        from netbox_librenms_plugin.import_validation_helpers import (
            recalculate_validation_status,
        )

        validation = {
            "issues": [],
            "can_import": False,
            "is_ready": False,
            "vm_placement": {"found": False},
        }

        recalculate_validation_status(validation, is_vm=True)

        assert validation["can_import"] is True  # No issues
        assert validation["is_ready"] is False  # But not ready without placement


# =============================================================================
# TestApplyOobDetectionResult - 6 tests
# =============================================================================


class TestApplyOobDetectionResult:
    """Tests for apply_oob_detection_result helper."""

    def _base_result(self):
        return {
            "serial_action": None,
            "oob_candidate": None,
            "promote_to_host": None,
            "serial_role_choice_available": False,
            "warnings": [],
        }

    def test_sets_serial_action(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_oob_detection_result

        result = self._base_result()
        apply_oob_detection_result(
            result,
            serial_action="oob_candidate",
            oob_candidate=None,
            promote_to_host=None,
            serial_role_choice_available=False,
        )
        assert result["serial_action"] == "oob_candidate"

    def test_clears_stale_merge_candidates(self):
        """Non-merge path must drop merge-only state so a reused dict can't keep stale merge UI data from a prior evaluation."""
        from netbox_librenms_plugin.import_validation_helpers import apply_oob_detection_result

        result = self._base_result()
        result["merge_candidates"] = {"host_named": {"pk": 1}, "oob_named": {"pk": 2}}
        apply_oob_detection_result(
            result,
            serial_action="oob_candidate",
            oob_candidate=None,
            promote_to_host=None,
            serial_role_choice_available=False,
        )
        assert result["merge_candidates"] is None

    def test_sets_oob_candidate_when_provided(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_oob_detection_result

        result = self._base_result()
        candidate = {"device": object(), "type": "idrac", "version": None, "ip": "10.0.0.1"}
        apply_oob_detection_result(
            result,
            serial_action="oob_candidate",
            oob_candidate=candidate,
            promote_to_host=None,
            serial_role_choice_available=False,
        )
        assert result["oob_candidate"] is candidate

    def test_clears_oob_candidate_when_none(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_oob_detection_result

        existing = {"device": object(), "type": "ilo", "version": None, "ip": None}
        result = self._base_result()
        result["oob_candidate"] = existing
        apply_oob_detection_result(
            result,
            serial_action="link",
            oob_candidate=None,
            promote_to_host=None,
            serial_role_choice_available=False,
        )
        # oob_candidate=None means "no candidate" -- field must be cleared to None
        # so stale values from a previous call do not persist.
        assert result["oob_candidate"] is None

    def test_clears_promote_to_host_when_none(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_oob_detection_result

        # Seed a stale promote_to_host so a regression that forgot to clear it (or stored
        # a None sentinel instead of removing the key) would be caught here.
        stale = {"existing_libre_id": 9, "existing_oob_type": "idrac", "existing_device": object()}
        result = self._base_result()
        result["promote_to_host"] = stale
        apply_oob_detection_result(
            result,
            serial_action="link",
            oob_candidate=None,
            promote_to_host=None,
            serial_role_choice_available=False,
        )
        # promote_to_host=None must clear the field entirely (the "absent otherwise"
        # contract), not preserve the stale value nor store a None sentinel.
        assert "promote_to_host" not in result

    def test_sets_promote_to_host_when_provided(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_oob_detection_result

        result = self._base_result()
        promo = {"existing_libre_id": 7, "existing_oob_type": "idrac", "existing_device": object()}
        apply_oob_detection_result(
            result,
            serial_action="promote_to_host",
            oob_candidate=None,
            promote_to_host=promo,
            serial_role_choice_available=False,
        )
        assert result["promote_to_host"] is promo

    def test_serial_role_choice_available_flag(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_oob_detection_result

        result = self._base_result()
        apply_oob_detection_result(
            result,
            serial_action="oob_candidate",
            oob_candidate={"device": object(), "type": "oob", "version": None, "ip": None},
            promote_to_host={"existing_libre_id": 3, "existing_oob_type": "oob", "existing_device": object()},
            serial_role_choice_available=True,
        )
        assert result["serial_role_choice_available"] is True

    def test_appends_warnings(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_oob_detection_result

        result = self._base_result()
        result["warnings"] = ["pre-existing"]
        apply_oob_detection_result(
            result,
            serial_action="link",
            oob_candidate=None,
            promote_to_host=None,
            serial_role_choice_available=False,
            warnings=["new warning 1", "new warning 2"],
        )
        assert result["warnings"] == ["pre-existing", "new warning 1", "new warning 2"]


# =============================================================================
# TestApplyMergeCandidates - 8 tests
# =============================================================================


class TestApplyMergeCandidates:
    """Tests for apply_merge_candidates helper."""

    def _base_result(self):
        return {
            "serial_action": None,
            "merge_candidates": None,
            "can_import": True,
            "warnings": [],
        }

    def test_sets_serial_action_to_merge(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_merge_candidates

        result = self._base_result()
        apply_merge_candidates(
            result,
            host_named={"pk": 1, "name": "router-01", "librenms_link": {"host_id": 10}},
            oob_named={"pk": 2, "name": "router-01-idrac", "librenms_link": None},
            warning="Two devices found",
        )
        assert result["serial_action"] == "merge_netbox_devices"

    def test_sets_merge_candidates_dict(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_merge_candidates

        result = self._base_result()
        host = {"pk": 1, "name": "router-01", "librenms_link": {"host_id": 10}}
        oob = {"pk": 2, "name": "router-01-idrac", "librenms_link": None}
        apply_merge_candidates(result, host_named=host, oob_named=oob, warning="w")
        assert result["merge_candidates"] == {"host_named": host, "oob_named": oob}

    def test_sets_can_import_false(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_merge_candidates

        result = self._base_result()
        result["can_import"] = True
        apply_merge_candidates(
            result,
            host_named={"pk": 1, "name": "h", "librenms_link": None},
            oob_named={"pk": 2, "name": "o", "librenms_link": None},
            warning="merge needed",
        )
        assert result["can_import"] is False

    def test_sets_is_ready_false(self):
        """Merge mode blocks import, so a stale is_ready=True (e.g. carried over from hostname-first processing) must be cleared."""
        from netbox_librenms_plugin.import_validation_helpers import apply_merge_candidates

        result = self._base_result()
        result["is_ready"] = True
        apply_merge_candidates(
            result,
            host_named={"pk": 1, "name": "h", "librenms_link": None},
            oob_named={"pk": 2, "name": "o", "librenms_link": None},
            warning="merge needed",
        )
        assert result["is_ready"] is False
        assert result["can_import"] is False

    def test_resets_stale_warnings_to_merge_warning(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_merge_candidates

        # Stale serial/hostname-detection warnings present from earlier in the pass are
        # dropped: merge mode supersedes them, leaving only the merge warning.
        result = self._base_result()
        result["warnings"] = ["hostname differs", "already has an OOB controller linked"]
        apply_merge_candidates(
            result,
            host_named={"pk": 1, "name": "h", "librenms_link": None},
            oob_named={"pk": 2, "name": "o", "librenms_link": None},
            warning="merge warning",
        )
        assert result["warnings"] == ["merge warning"]

    def test_clears_oob_candidate(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_merge_candidates

        result = self._base_result()
        result["oob_candidate"] = {"device": object(), "type": "idrac", "version": None, "ip": None}
        apply_merge_candidates(
            result,
            host_named={"pk": 1, "name": "h", "librenms_link": None},
            oob_named={"pk": 2, "name": "o", "librenms_link": None},
            warning="merge needed",
        )
        assert result["oob_candidate"] is None

    def test_clears_serial_conflict_flags(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_merge_candidates

        result = self._base_result()
        result["serial_duplicate"] = True
        result["serial_confirmed"] = True
        apply_merge_candidates(
            result,
            host_named={"pk": 1, "name": "h", "librenms_link": None},
            oob_named={"pk": 2, "name": "o", "librenms_link": None},
            warning="merge needed",
        )
        assert result["serial_duplicate"] is False
        assert result["serial_confirmed"] is False

    def test_removes_promote_to_host(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_merge_candidates

        result = self._base_result()
        result["promote_to_host"] = {"existing_libre_id": 5, "existing_oob_type": "idrac", "existing_device": object()}
        apply_merge_candidates(
            result,
            host_named={"pk": 1, "name": "h", "librenms_link": None},
            oob_named={"pk": 2, "name": "o", "librenms_link": None},
            warning="merge needed",
        )
        assert "promote_to_host" not in result

    def test_recalculate_status_keeps_merge_block(self):
        """A later recalculation (e.g. after the rest of the row validates cleanly) must preserve the merge block."""
        from netbox_librenms_plugin.import_validation_helpers import (
            apply_merge_candidates,
            recalculate_validation_status,
        )

        result = self._base_result()
        # Otherwise fully import-ready: empty issues + all required fields found. The ONLY thing
        # that should keep this blocked is the merge state.
        result["issues"] = []
        result["site"] = {"found": True}
        result["device_type"] = {"found": True}
        result["device_role"] = {"found": True}
        apply_merge_candidates(
            result,
            host_named={"pk": 1, "name": "h", "librenms_link": None},
            oob_named={"pk": 2, "name": "o", "librenms_link": None},
            warning="merge needed",
        )

        recalculate_validation_status(result, is_vm=False)

        assert result["can_import"] is False
        assert result["is_ready"] is False

    def test_disables_serial_role_choice(self):
        from netbox_librenms_plugin.import_validation_helpers import apply_merge_candidates

        result = self._base_result()
        result["serial_role_choice_available"] = True
        apply_merge_candidates(
            result,
            host_named={"pk": 1, "name": "h", "librenms_link": None},
            oob_named={"pk": 2, "name": "o", "librenms_link": None},
            warning="merge needed",
        )
        assert result["serial_role_choice_available"] is False

    def test_previously_ready_result_clears_all_stale_importable_state(self):
        """Regression for issue #85: a result that an earlier validation stage already marked importable (hostname-first row processing) carries is_ready=True alongside oob_candidate, promote_to_host, and serial_role_choice_available simultaneously."""
        from netbox_librenms_plugin.import_validation_helpers import apply_merge_candidates

        result = self._base_result()
        result["is_ready"] = True
        result["can_import"] = True
        result["oob_candidate"] = {"device": object(), "type": "idrac", "version": None, "ip": None}
        result["promote_to_host"] = {
            "existing_libre_id": 7,
            "existing_oob_type": "idrac",
            "existing_device": object(),
        }
        result["serial_role_choice_available"] = True

        apply_merge_candidates(
            result,
            host_named={"pk": 1, "name": "router-01", "librenms_link": {"host_id": 10}},
            oob_named={"pk": 2, "name": "router-01-idrac", "librenms_link": None},
            warning="Two devices likely represent one physical box",
        )

        # Every stale importable signal must be reset by the single merge pass.
        assert result["is_ready"] is False
        assert result["can_import"] is False
        assert result["oob_candidate"] is None
        assert "promote_to_host" not in result
        assert result["serial_role_choice_available"] is False
        # And the merge path is now the single source of truth.
        assert result["serial_action"] == "merge_netbox_devices"


class TestMergeCandidatePks:
    """merge_candidate_pks must fail closed on corrupt pk values, never crash on an unhashable one."""

    def test_returns_positive_pks_from_both_slots(self):
        from netbox_librenms_plugin.import_validation_helpers import merge_candidate_pks

        validation = {"merge_candidates": {"host_named": {"pk": 5}, "oob_named": {"pk": 9}}}
        assert merge_candidate_pks(validation) == {5, 9}

    def test_corrupt_unhashable_pk_is_skipped_not_crashing(self):
        from netbox_librenms_plugin.import_validation_helpers import merge_candidate_pks

        # {"pk": []} is unhashable; the old set.add(pk) raised TypeError on this row.
        validation = {"merge_candidates": {"host_named": {"pk": []}, "oob_named": {"pk": 7}}}
        assert merge_candidate_pks(validation) == {7}

    def test_non_positive_and_non_int_pks_are_dropped(self):
        from netbox_librenms_plugin.import_validation_helpers import merge_candidate_pks

        validation = {"merge_candidates": {"host_named": {"pk": 0}, "oob_named": {"pk": "not-int"}}}
        assert merge_candidate_pks(validation) == set()

    def test_non_dict_validation_is_rejected(self):
        from netbox_librenms_plugin.import_validation_helpers import merge_candidate_pks

        assert merge_candidate_pks(["malformed"]) == set()
        assert merge_candidate_pks("malformed") == set()

    def test_non_dict_merge_candidates_is_rejected(self):
        from netbox_librenms_plugin.import_validation_helpers import merge_candidate_pks

        assert merge_candidate_pks({"merge_candidates": ["host_named"]}) == set()

    def test_non_dict_slot_entry_is_skipped(self):
        from netbox_librenms_plugin.import_validation_helpers import merge_candidate_pks

        validation = {"merge_candidates": {"host_named": "corrupt", "oob_named": {"pk": 3}}}
        assert merge_candidate_pks(validation) == {3}
