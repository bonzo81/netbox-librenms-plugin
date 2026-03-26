"""
Tests for netbox_librenms_plugin.import_utils.vm_operations module.

Covers create_vm_from_librenms and bulk_import_vms.
All DB interactions are mocked — no @pytest.mark.django_db used.
"""

import pytest
from unittest.mock import MagicMock, patch


class TestCreateVmFromLibrenms:
    """Tests for create_vm_from_librenms function."""

    def test_success_with_computed_name(self):
        """VM is created using pre-computed _computed_name when present."""
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        libre_device = {
            "device_id": 1,
            "hostname": "vm01.example.com",
            "_computed_name": "vm01-computed",
        }
        mock_cluster = MagicMock()
        mock_platform = MagicMock()
        validation = {
            "can_import": True,
            "cluster": {"cluster": mock_cluster},
            "platform": {"platform": mock_platform},
        }
        mock_vm = MagicMock()
        mock_vm.name = "vm01-computed"
        mock_vm.pk = 10

        with (
            patch("django.db.transaction.atomic"),
            patch("virtualization.models.VirtualMachine") as mock_vm_class,
        ):
            mock_vm_class.objects.create.return_value = mock_vm
            result = create_vm_from_librenms(libre_device, validation)

        assert result == mock_vm
        call_kwargs = mock_vm_class.objects.create.call_args[1]
        assert call_kwargs["name"] == "vm01-computed"
        assert call_kwargs["cluster"] == mock_cluster
        assert call_kwargs["platform"] == mock_platform

    def test_fallback_to_determine_device_name_when_no_computed_name(self):
        """Falls back to _determine_device_name when _computed_name is absent."""
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        libre_device = {"device_id": 2, "hostname": "vm02.example.com"}
        validation = {
            "can_import": True,
            "cluster": {"cluster": MagicMock()},
            "platform": {"platform": None},
        }
        mock_vm = MagicMock()
        mock_vm.name = "vm02-determined"
        mock_vm.pk = 11

        with (
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations._determine_device_name",
                return_value="vm02-determined",
            ) as mock_det,
            patch("django.db.transaction.atomic"),
            patch("virtualization.models.VirtualMachine") as mock_vm_class,
        ):
            mock_vm_class.objects.create.return_value = mock_vm
            result = create_vm_from_librenms(libre_device, validation)

        mock_det.assert_called_once()
        call_kwargs = mock_vm_class.objects.create.call_args[1]
        assert call_kwargs["name"] == "vm02-determined"
        assert result == mock_vm

    def test_can_import_false_raises_value_error(self):
        """Raises ValueError immediately when validation['can_import'] is False."""
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        libre_device = {"device_id": 3, "hostname": "vm03"}
        validation = {
            "can_import": False,
            "issues": ["No cluster assigned", "Missing role"],
        }

        with pytest.raises(ValueError, match="VM cannot be imported"):
            create_vm_from_librenms(libre_device, validation)

    def test_server_key_stored_in_custom_field(self):
        """librenms_id custom field uses the provided server_key via set_librenms_device_id."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        libre_device = {"device_id": 5, "hostname": "vm05", "_computed_name": "vm05"}
        validation = {
            "can_import": True,
            "cluster": {"cluster": MagicMock()},
            "platform": {"platform": None},
        }
        mock_vm = MagicMock()
        mock_vm.name = "vm05"
        mock_vm.pk = 50
        mock_vm.custom_field_data = {}

        with patch("virtualization.models.VirtualMachine") as mock_vm_class:
            with patch("django.db.transaction.atomic"):
                with patch("netbox_librenms_plugin.utils.set_librenms_device_id") as mock_setter:
                    mock_vm_class.objects.create.return_value = mock_vm
                    create_vm_from_librenms(libre_device, validation, server_key="secondary")

        mock_setter.assert_called_once_with(mock_vm, 5, "secondary")
        mock_vm.save.assert_called_once()

    def test_role_is_passed_to_create(self):
        """Optional role parameter is forwarded to VirtualMachine.objects.create."""
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        libre_device = {"device_id": 6, "hostname": "vm06", "_computed_name": "vm06"}
        mock_role = MagicMock()
        validation = {
            "can_import": True,
            "cluster": {"cluster": MagicMock()},
            "platform": {"platform": None},
        }
        mock_vm = MagicMock()
        mock_vm.name = "vm06"
        mock_vm.pk = 60

        with (
            patch("django.db.transaction.atomic"),
            patch("virtualization.models.VirtualMachine") as mock_vm_class,
        ):
            mock_vm_class.objects.create.return_value = mock_vm
            create_vm_from_librenms(libre_device, validation, role=mock_role)

        call_kwargs = mock_vm_class.objects.create.call_args[1]
        assert call_kwargs["role"] == mock_role

    def test_platform_none_when_not_in_validation(self):
        """Platform is None when validation['platform'] has no 'platform' key."""
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        libre_device = {"device_id": 7, "hostname": "vm07", "_computed_name": "vm07"}
        validation = {
            "can_import": True,
            "cluster": {"cluster": MagicMock()},
            "platform": {},  # no 'platform' key — .get() returns None
        }
        mock_vm = MagicMock()
        mock_vm.name = "vm07"
        mock_vm.pk = 70

        with (
            patch("django.db.transaction.atomic"),
            patch("virtualization.models.VirtualMachine") as mock_vm_class,
        ):
            mock_vm_class.objects.create.return_value = mock_vm
            create_vm_from_librenms(libre_device, validation)

        call_kwargs = mock_vm_class.objects.create.call_args[1]
        assert call_kwargs["platform"] is None

    def test_import_comment_contains_device_id(self):
        """The comments field contains a reference to LibreNMS and device_id."""
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        libre_device = {"device_id": 8, "hostname": "vm08", "_computed_name": "vm08"}
        validation = {
            "can_import": True,
            "cluster": {"cluster": MagicMock()},
            "platform": {"platform": None},
        }
        mock_vm = MagicMock()
        mock_vm.name = "vm08"
        mock_vm.pk = 80

        with (
            patch("django.db.transaction.atomic"),
            patch("virtualization.models.VirtualMachine") as mock_vm_class,
        ):
            mock_vm_class.objects.create.return_value = mock_vm
            create_vm_from_librenms(libre_device, validation)

        call_kwargs = mock_vm_class.objects.create.call_args[1]
        assert "LibreNMS" in call_kwargs["comments"]
        assert str(libre_device["device_id"]) in call_kwargs["comments"]


class TestBulkImportVms:
    """Tests for bulk_import_vms function."""

    def test_empty_vm_imports_returns_empty_result(self):
        """Empty vm_imports dict returns empty success/failed/skipped lists."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"):
            result = bulk_import_vms({}, mock_api, user=MagicMock())

        assert result == {"success": [], "failed": [], "skipped": []}

    def test_permission_denied_propagates(self):
        """PermissionDenied from require_permissions propagates to the caller."""
        from django.core.exceptions import PermissionDenied

        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch(
            "netbox_librenms_plugin.import_utils.vm_operations.require_permissions",
            side_effect=PermissionDenied("No permission"),
        ):
            with pytest.raises(PermissionDenied):
                bulk_import_vms({1: {}}, mock_api, user=MagicMock())

    def test_device_not_found_added_to_failed(self):
        """When fetch_device_with_cache returns None, device is appended to failed."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                return_value=None,
            ),
        ):
            result = bulk_import_vms({99: {}}, mock_api, user=MagicMock())

        assert len(result["failed"]) == 1
        assert result["failed"][0]["device_id"] == 99
        assert "not found" in result["failed"][0]["error"].lower()

    def test_existing_device_added_to_skipped(self):
        """When validation reports existing_device, device is appended to skipped."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        mock_existing = MagicMock()
        mock_existing.name = "existing-vm"
        libre_device = {"device_id": 10, "hostname": "existing-vm"}
        mock_validation = {
            "existing_device": mock_existing,
            "can_import": False,
            "issues": [],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                return_value=libre_device,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.validate_device_for_import",
                return_value=mock_validation,
            ),
        ):
            result = bulk_import_vms({10: {}}, mock_api, user=MagicMock())

        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["device_id"] == 10
        assert "existing-vm" in result["skipped"][0]["reason"]

    def test_success_path_vm_created(self):
        """Happy path: VM is created and appended to success list."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        libre_device = {"device_id": 20, "hostname": "new-vm"}
        mock_validation = {
            "existing_device": None,
            "can_import": True,
            "cluster": {"cluster": MagicMock()},
            "platform": {"platform": None},
            "issues": [],
        }
        mock_vm = MagicMock()
        mock_vm.name = "new-vm"

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                return_value=libre_device,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.validate_device_for_import",
                return_value=mock_validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations._determine_device_name",
                return_value="new-vm",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.create_vm_from_librenms",
                return_value=mock_vm,
            ),
            patch("netbox_librenms_plugin.import_utils.vm_operations.Cluster"),
            patch("netbox_librenms_plugin.import_utils.vm_operations.DeviceRole"),
            patch("netbox_librenms_plugin.import_validation_helpers.apply_cluster_to_validation"),
            patch("netbox_librenms_plugin.import_validation_helpers.apply_role_to_validation"),
        ):
            result = bulk_import_vms({20: {}}, mock_api, user=MagicMock())

        assert len(result["success"]) == 1
        assert result["success"][0]["device_id"] == 20
        assert result["success"][0]["device"] == mock_vm
        assert len(result["failed"]) == 0
        assert len(result["skipped"]) == 0

    def test_cluster_assignment_applied(self):
        """apply_cluster_to_validation is called when cluster_id is provided and found."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        mock_cluster = MagicMock()
        libre_device = {"device_id": 30, "hostname": "clustered-vm"}
        mock_validation = {
            "existing_device": None,
            "can_import": True,
            "cluster": {"cluster": mock_cluster},
            "platform": {"platform": None},
            "issues": [],
        }
        mock_vm = MagicMock()
        mock_vm.name = "clustered-vm"

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                return_value=libre_device,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.validate_device_for_import",
                return_value=mock_validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations._determine_device_name",
                return_value="clustered-vm",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.create_vm_from_librenms",
                return_value=mock_vm,
            ),
            patch("netbox_librenms_plugin.import_utils.vm_operations.Cluster") as mock_cluster_cls,
            patch("netbox_librenms_plugin.import_utils.vm_operations.DeviceRole"),
            patch("netbox_librenms_plugin.import_validation_helpers.apply_cluster_to_validation") as mock_apply_cluster,
            patch("netbox_librenms_plugin.import_validation_helpers.apply_role_to_validation"),
        ):
            mock_cluster_cls.objects.filter.return_value.first.return_value = mock_cluster
            bulk_import_vms({30: {"cluster_id": 5}}, mock_api, user=MagicMock())

        mock_apply_cluster.assert_called_once_with(mock_validation, mock_cluster)

    def test_role_assignment_applied(self):
        """apply_role_to_validation is called when role_id is provided and found."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        mock_role = MagicMock()
        libre_device = {"device_id": 40, "hostname": "role-vm"}
        mock_validation = {
            "existing_device": None,
            "can_import": True,
            "cluster": {"cluster": MagicMock()},
            "platform": {"platform": None},
            "issues": [],
        }
        mock_vm = MagicMock()
        mock_vm.name = "role-vm"

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                return_value=libre_device,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.validate_device_for_import",
                return_value=mock_validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations._determine_device_name",
                return_value="role-vm",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.create_vm_from_librenms",
                return_value=mock_vm,
            ),
            patch("netbox_librenms_plugin.import_utils.vm_operations.Cluster"),
            patch("netbox_librenms_plugin.import_utils.vm_operations.DeviceRole") as mock_role_cls,
            patch("netbox_librenms_plugin.import_validation_helpers.apply_cluster_to_validation"),
            patch("netbox_librenms_plugin.import_validation_helpers.apply_role_to_validation") as mock_apply_role,
        ):
            mock_role_cls.objects.filter.return_value.first.return_value = mock_role
            bulk_import_vms({40: {"device_role_id": 3}}, mock_api, user=MagicMock())

        mock_apply_role.assert_called_once_with(mock_validation, mock_role, is_vm=True)

    def test_exception_in_inner_loop_added_to_failed(self):
        """Exception during VM processing is caught and added to failed list."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                side_effect=RuntimeError("Connection error"),
            ),
        ):
            result = bulk_import_vms({50: {}}, mock_api, user=MagicMock())

        assert len(result["failed"]) == 1
        assert result["failed"][0]["device_id"] == 50
        assert "Connection error" in result["failed"][0]["error"]

    def test_job_cancellation_breaks_loop(self):
        """Loop exits early when job status becomes 'failed' at the 5th-iteration check."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        mock_job = MagicMock()
        mock_job.logger = MagicMock()
        # Start as "running"; switches to "failed" on the 2nd refresh (idx=5 check)
        mock_job.job.status = "running"
        refresh_calls = [0]

        def _refresh():
            refresh_calls[0] += 1
            if refresh_calls[0] >= 2:
                mock_job.job.status = "failed"

        mock_job.job.refresh_from_db.side_effect = _refresh

        # 5 VMs: idx=1 check fires (running → no cancel); idx=5 fires (failed → cancel)
        vm_imports = {i: {} for i in range(1, 6)}

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                return_value=None,  # VMs 1-4 → failed; VM-5 never reached
            ),
        ):
            result = bulk_import_vms(vm_imports, mock_api, job=mock_job)

        # VMs 1-4 added to failed; 5th cancelled before processing
        assert len(result["failed"]) == 4

    def test_job_cancellation_with_errored_status(self):
        """Loop also exits for 'errored' job status."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        mock_job = MagicMock()
        mock_job.logger = MagicMock()
        # Start as "running"; switches to "errored" on the 2nd refresh (idx=5 check)
        mock_job.job.status = "running"
        refresh_calls = [0]

        def _refresh():
            refresh_calls[0] += 1
            if refresh_calls[0] >= 2:
                mock_job.job.status = "errored"

        mock_job.job.refresh_from_db.side_effect = _refresh

        vm_imports = {i: {} for i in range(1, 6)}

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                return_value=None,
            ),
        ):
            result = bulk_import_vms(vm_imports, mock_api, job=mock_job)

        assert len(result["failed"]) == 4

    def test_user_extracted_from_job_when_not_provided(self):
        """User is extracted from job.job.user when the user param is None."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_user = MagicMock()
        mock_job = MagicMock()
        mock_job.job.user = mock_user
        mock_job.logger = MagicMock()

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions") as mock_require:
            bulk_import_vms({}, mock_api, job=mock_job, user=None)

        mock_require.assert_called_once_with(mock_user, ["virtualization.add_virtualmachine"], "import VMs")

    def test_sync_options_use_sysname_and_strip_domain_forwarded(self):
        """sync_options use_sysname/strip_domain are passed to validate_device_for_import."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        libre_device = {"device_id": 60, "hostname": "opts-vm"}
        # existing_device set → triggers skipped path (avoids more mocking)
        mock_validation = {
            "existing_device": MagicMock(name="opts-vm"),
            "can_import": False,
            "issues": [],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                return_value=libre_device,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.validate_device_for_import",
                return_value=mock_validation,
            ) as mock_validate,
        ):
            bulk_import_vms(
                {60: {}},
                mock_api,
                sync_options={"use_sysname": False, "strip_domain": True},
                user=MagicMock(),
            )

        mock_validate.assert_called_once()
        call_kwargs = mock_validate.call_args[1]
        assert call_kwargs["use_sysname"] is False
        assert call_kwargs["strip_domain"] is True

    def test_no_cluster_id_skips_cluster_lookup(self):
        """Cluster lookup is skipped when cluster_id is absent from vm_mappings."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        libre_device = {"device_id": 70, "hostname": "no-cluster-vm"}
        mock_validation = {
            "existing_device": None,
            "can_import": True,
            "cluster": {"cluster": MagicMock()},
            "platform": {"platform": None},
            "issues": [],
        }
        mock_vm = MagicMock()
        mock_vm.name = "no-cluster-vm"

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                return_value=libre_device,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.validate_device_for_import",
                return_value=mock_validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations._determine_device_name",
                return_value="no-cluster-vm",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.create_vm_from_librenms",
                return_value=mock_vm,
            ),
            patch("netbox_librenms_plugin.import_utils.vm_operations.Cluster") as mock_cluster_cls,
            patch("netbox_librenms_plugin.import_utils.vm_operations.DeviceRole"),
            patch("netbox_librenms_plugin.import_validation_helpers.apply_cluster_to_validation") as mock_apply_cluster,
            patch("netbox_librenms_plugin.import_validation_helpers.apply_role_to_validation"),
        ):
            # No cluster_id in vm_mappings
            bulk_import_vms({70: {}}, mock_api, user=MagicMock())

        mock_cluster_cls.objects.filter.assert_not_called()
        mock_apply_cluster.assert_not_called()

    def test_job_status_value_attribute_used_when_present(self):
        """Status enum .value is used for cancellation check when present."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        mock_job = MagicMock()
        mock_job.logger = MagicMock()
        # Status object with .value attribute (simulates Django choices enum)
        # Initially "running", switches to "failed" on 2nd refresh (idx=5 check)
        mock_running = MagicMock()
        mock_running.value = "running"
        mock_failed = MagicMock()
        mock_failed.value = "failed"
        mock_job.job.status = mock_running
        refresh_calls = [0]

        def _refresh():
            refresh_calls[0] += 1
            if refresh_calls[0] >= 2:
                mock_job.job.status = mock_failed

        mock_job.job.refresh_from_db.side_effect = _refresh

        vm_imports = {i: {} for i in range(1, 6)}

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                return_value=None,
            ),
        ):
            result = bulk_import_vms(vm_imports, mock_api, job=mock_job)

        assert len(result["failed"]) == 4

    def test_job_log_info_when_not_cancelled_at_checkpoint(self):
        """log.info is called at a non-cancelling 5-iteration checkpoint."""
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        mock_api = MagicMock()
        mock_api.server_key = "default"

        # Status is "running" at first checkpoint (idx=5), "failed" at second (idx=10)
        statuses = iter(["running", "running", "failed"])
        mock_job = MagicMock()
        mock_job.logger = MagicMock()
        mock_job.job.status = "running"

        def _refresh():
            try:
                mock_job.job.status = next(statuses)
            except StopIteration:
                mock_job.job.status = "failed"

        mock_job.job.refresh_from_db.side_effect = _refresh

        # 10 VMs: checkpoint at idx=5 (running → log.info) and idx=10 (failed → break)
        vm_imports = {i: {} for i in range(1, 11)}

        with (
            patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions"),
            patch(
                "netbox_librenms_plugin.import_utils.vm_operations.fetch_device_with_cache",
                return_value=None,
            ),
        ):
            bulk_import_vms(vm_imports, mock_api, job=mock_job)

        # log.info called at idx=5 checkpoint
        mock_job.logger.info.assert_called()
