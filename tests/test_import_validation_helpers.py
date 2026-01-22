"""Tests for netbox_librenms_plugin.import_validation_helpers module.

Phase 2 tests covering validation state updates, model retrieval,
and selection extraction functions.
"""

from unittest.mock import MagicMock

# =============================================================================
# TestGetModelById - 4 tests
# =============================================================================


class TestFetchModelById:
    """Test generic model retrieval helper."""

    def test_fetch_model_by_id_success(self):
        """Return model instance when found."""
        mock_model_class = MagicMock()
        mock_instance = MagicMock(id=1, name="Access Switch")
        mock_model_class.objects.get.return_value = mock_instance

        from netbox_librenms_plugin.import_validation_helpers import fetch_model_by_id

        result = fetch_model_by_id(mock_model_class, 1)

        assert result == mock_instance
        mock_model_class.objects.get.assert_called_once_with(pk=1)

    def test_fetch_model_by_id_not_found(self):
        """Return None when ID doesn't exist."""
        mock_model_class = MagicMock()
        mock_model_class.DoesNotExist = Exception
        mock_model_class.objects.get.side_effect = mock_model_class.DoesNotExist

        from netbox_librenms_plugin.import_validation_helpers import fetch_model_by_id

        result = fetch_model_by_id(mock_model_class, 999)

        assert result is None

    def test_fetch_model_by_id_invalid_id(self):
        """Handle invalid ID gracefully."""
        mock_model_class = MagicMock()
        mock_model_class.DoesNotExist = type("DoesNotExist", (Exception,), {})

        from netbox_librenms_plugin.import_validation_helpers import fetch_model_by_id

        result = fetch_model_by_id(mock_model_class, "not-a-number")

        assert result is None

    def test_fetch_model_by_id_none_id(self):
        """Handle None ID gracefully."""
        mock_model_class = MagicMock()

        from netbox_librenms_plugin.import_validation_helpers import fetch_model_by_id

        result = fetch_model_by_id(mock_model_class, None)

        assert result is None
        mock_model_class.objects.get.assert_not_called()


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

        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.POST = {
            "cluster_1234": "5",
            "role_1234": "10",
            "rack_1234": "15",
        }

        result = extract_device_selections(mock_request, device_id=1234)

        assert result["cluster_id"] == "5"
        assert result["role_id"] == "10"
        assert result["rack_id"] == "15"

    def test_extract_selections_partial(self):
        """Missing fields return None."""
        from netbox_librenms_plugin.import_validation_helpers import (
            extract_device_selections,
        )

        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.POST = {
            "role_1234": "10",
        }

        result = extract_device_selections(mock_request, device_id=1234)

        assert result["cluster_id"] is None
        assert result["role_id"] == "10"
        assert result["rack_id"] is None

    def test_extract_selections_from_get(self):
        """Selections extracted from GET request."""
        from netbox_librenms_plugin.import_validation_helpers import (
            extract_device_selections,
        )

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.GET = {
            "cluster_999": "3",
            "role_999": "7",
            "rack_999": "11",
        }

        result = extract_device_selections(mock_request, device_id=999)

        assert result["cluster_id"] == "3"
        assert result["role_id"] == "7"
        assert result["rack_id"] == "11"

    def test_extract_selections_empty_values(self):
        """Empty strings handled correctly."""
        from netbox_librenms_plugin.import_validation_helpers import (
            extract_device_selections,
        )

        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.POST = {
            "cluster_1234": "",
            "role_1234": "",
            "rack_1234": "",
        }

        result = extract_device_selections(mock_request, device_id=1234)

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

        mock_role = MagicMock(id=1, name="Access Switch")
        validation = {
            "device_role": {"found": False, "role": None},
            "issues": ["Device role must be manually selected before import"],
            "can_import": False,
            "is_ready": False,
            "site": {"found": True},
            "device_type": {"found": True},
        }

        apply_role_to_validation(validation, mock_role, is_vm=False)

        assert validation["device_role"]["found"] is True
        assert validation["device_role"]["role"] == mock_role

    def test_apply_role_to_validation_clears_issue(self):
        """Selecting role should clear 'role' related validation issue."""
        from netbox_librenms_plugin.import_validation_helpers import (
            apply_role_to_validation,
        )

        mock_role = MagicMock(id=1, name="Access Switch")
        validation = {
            "device_role": {"found": False, "role": None},
            "issues": ["Device role must be manually selected before import"],
            "can_import": False,
            "is_ready": False,
            "site": {"found": True},
            "device_type": {"found": True},
        }

        apply_role_to_validation(validation, mock_role, is_vm=False)

        assert len(validation["issues"]) == 0

    def test_apply_cluster_to_validation_success(self):
        """Cluster selection updates state for VM import."""
        from netbox_librenms_plugin.import_validation_helpers import (
            apply_cluster_to_validation,
        )

        mock_cluster = MagicMock(id=1, name="VMware Cluster 1")
        validation = {
            "cluster": {"found": False, "cluster": None},
            "issues": ["Cluster must be manually selected before import"],
            "can_import": False,
            "is_ready": False,
        }

        apply_cluster_to_validation(validation, mock_cluster)

        assert validation["cluster"]["found"] is True
        assert validation["cluster"]["cluster"] == mock_cluster

    def test_apply_rack_to_validation_success(self):
        """Rack selection updates state for device import."""
        from netbox_librenms_plugin.import_validation_helpers import (
            apply_rack_to_validation,
        )

        mock_rack = MagicMock(id=1, name="Rack A1")
        validation = {
            "issues": [],
            "can_import": True,
            "is_ready": True,
        }

        apply_rack_to_validation(validation, mock_rack)

        assert validation["rack"]["found"] is True
        assert validation["rack"]["rack"] == mock_rack

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

    def test_recalculate_can_import_vm_cluster_required(self):
        """VM import requires cluster to be ready."""
        from netbox_librenms_plugin.import_validation_helpers import (
            recalculate_validation_status,
        )

        validation = {
            "issues": [],
            "can_import": False,
            "is_ready": False,
            "cluster": {"found": True},
        }

        recalculate_validation_status(validation, is_vm=True)

        assert validation["can_import"] is True
        assert validation["is_ready"] is True

    def test_recalculate_can_import_vm_missing_cluster(self):
        """VM import not ready without cluster."""
        from netbox_librenms_plugin.import_validation_helpers import (
            recalculate_validation_status,
        )

        validation = {
            "issues": [],
            "can_import": False,
            "is_ready": False,
            "cluster": {"found": False},
        }

        recalculate_validation_status(validation, is_vm=True)

        assert validation["can_import"] is True  # No issues
        assert validation["is_ready"] is False  # But not ready without cluster
