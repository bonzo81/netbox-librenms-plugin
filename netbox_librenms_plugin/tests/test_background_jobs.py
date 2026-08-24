"""
Tests for background job implementation.

Tests the FilterDevicesJob, ImportDevicesJob, should_use_background_job logic,
job result loading, and graceful fallback behavior.

Refactored to use pure pytest without Django database dependencies.
All tests use mocking and direct attribute manipulation instead of HTTP requests.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.http import QueryDict


@pytest.fixture(autouse=True)
def _configured_job_server_keys():
    """Give job tests explicit usable server keys."""
    with patch(
        "netbox_librenms_plugin.server_selection.LibreNMSAPI.get_available_servers",
        return_value={
            "default": "Default",
            "primary": "Primary",
            "secondary": "Secondary",
            "non-default": "Non-default",
            "resolved-default": "Resolved default",
        },
    ):
        yield


class TestShouldUseBackgroundJob:
    """Test background job decision logic."""

    def test_checkbox_checked_returns_true(self):
        """When use_background_job form field is True, return True for superusers."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = LibreNMSImportView()
        view._filter_form_data = {"use_background_job": True}
        view.request = MagicMock()
        view.request.user.is_superuser = True

        assert view.should_use_background_job() is True

    def test_checkbox_unchecked_returns_false(self):
        """When use_background_job form field is False, return False."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = LibreNMSImportView()
        view._filter_form_data = {"use_background_job": False}
        view.request = MagicMock()
        view.request.user.is_superuser = True

        assert view.should_use_background_job() is False

    def test_default_when_field_missing(self):
        """When field is missing, default to True for superusers."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = LibreNMSImportView()
        view._filter_form_data = {"some_other_field": "value"}
        view.request = MagicMock()
        view.request.user.is_superuser = True

        assert view.should_use_background_job() is True

    def test_empty_form_data_returns_default(self):
        """Empty form data returns default True for superusers."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = LibreNMSImportView()
        view._filter_form_data = {}
        view.request = MagicMock()
        view.request.user.is_superuser = True

        assert view.should_use_background_job() is True

    def test_non_superuser_always_returns_false(self):
        """Non-superuser users always get synchronous mode."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = LibreNMSImportView()
        view._filter_form_data = {"use_background_job": True}
        view.request = MagicMock()
        view.request.user.is_superuser = False

        # Even when checkbox is True, non-superusers get False
        assert view.should_use_background_job() is False


def create_mock_job_runner(job_class, job_pk=123):
    """Create a mock job runner instance without invoking real __init__."""
    # Create instance without calling __init__
    job = object.__new__(job_class)
    # Set up required attributes
    job.job = MagicMock()
    job.job.pk = job_pk
    job.job.data = {}
    job.logger = MagicMock()
    return job


class TestFilterDevicesJob:
    """Test FilterDevicesJob background job."""

    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    @patch("netbox_librenms_plugin.import_utils.process_device_filters")
    def test_run_processes_filters_successfully(self, mock_process, mock_api_class):
        """Job runs and processes filters correctly."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        # Setup mocks
        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api.server_key = "default"
        mock_api_class.return_value = mock_api

        validated_devices = [
            {"device_id": 1, "hostname": "test1", "_validation": {}},
            {"device_id": 2, "hostname": "test2", "_validation": {}},
        ]
        mock_process.return_value = validated_devices

        # Create job instance without calling real __init__
        job = create_mock_job_runner(FilterDevicesJob)

        # Run job
        filters = {"location": "site1"}
        job.run(
            filters=filters,
            vc_detection_enabled=True,
            clear_cache=False,
            show_disabled=False,
            server_key="default",
        )

        # Verify process_device_filters was called with correct args
        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args.kwargs
        assert call_kwargs["filters"] == filters
        assert call_kwargs["vc_detection_enabled"] is True
        assert call_kwargs["clear_cache"] is False
        assert call_kwargs["job"] == job

    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    @patch("netbox_librenms_plugin.import_utils.process_device_filters")
    def test_run_with_vc_detection_enabled(self, mock_process, mock_api_class):
        """vc_detection_enabled=True passed to processor."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api.server_key = "secondary"
        mock_api_class.return_value = mock_api
        mock_process.return_value = []

        job = create_mock_job_runner(FilterDevicesJob)

        job.run(
            filters={},
            vc_detection_enabled=True,
            clear_cache=False,
            show_disabled=False,
            server_key="default",
        )

        call_kwargs = mock_process.call_args.kwargs
        assert call_kwargs["vc_detection_enabled"] is True

    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    @patch("netbox_librenms_plugin.import_utils.process_device_filters")
    def test_run_with_clear_cache(self, mock_process, mock_api_class):
        """clear_cache=True triggers cache refresh."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api.server_key = "secondary"
        mock_api_class.return_value = mock_api
        mock_process.return_value = []

        job = create_mock_job_runner(FilterDevicesJob)

        job.run(
            filters={},
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=False,
            server_key="default",
        )

        call_kwargs = mock_process.call_args.kwargs
        assert call_kwargs["clear_cache"] is True

    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    @patch("netbox_librenms_plugin.import_utils.process_device_filters")
    def test_run_with_show_disabled(self, mock_process, mock_api_class):
        """show_disabled=True includes disabled devices."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api.server_key = "secondary"
        mock_api_class.return_value = mock_api
        mock_process.return_value = []

        job = create_mock_job_runner(FilterDevicesJob)

        job.run(
            filters={},
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=True,
            server_key="default",
        )

        call_kwargs = mock_process.call_args.kwargs
        assert call_kwargs["show_disabled"] is True

    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    @patch("netbox_librenms_plugin.import_utils.process_device_filters")
    def test_run_with_exclude_existing(self, mock_process, mock_api_class):
        """exclude_existing=True filters out NetBox devices."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api.server_key = "secondary"
        mock_api_class.return_value = mock_api
        mock_process.return_value = []

        job = create_mock_job_runner(FilterDevicesJob)

        job.run(
            filters={},
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=False,
            exclude_existing=True,
            server_key="default",
        )

        call_kwargs = mock_process.call_args.kwargs
        assert call_kwargs["exclude_existing"] is True

    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    @patch("netbox_librenms_plugin.import_utils.process_device_filters")
    def test_run_with_custom_server_key(self, mock_process, mock_api_class):
        """Non-default server_key used for API."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api.server_key = "secondary"
        mock_api_class.return_value = mock_api
        mock_process.return_value = [{"device_id": 1, "hostname": "test1"}]

        job = create_mock_job_runner(FilterDevicesJob)

        job.run(
            filters={},
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=False,
            server_key="secondary",
        )

        # Verify API was initialized with correct server_key
        mock_api_class.assert_called_once_with(server_key="secondary")
        # Verify server_key stored in job data
        assert job.job.data["server_key"] == "secondary"

    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    @patch("netbox_librenms_plugin.import_utils.process_device_filters")
    def test_filter_job_stores_server_key(self, mock_process, mock_api_class):
        """Job stores resolved api.server_key, not raw input parameter."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api.server_key = "resolved-default"
        mock_api_class.return_value = mock_api
        mock_process.return_value = [{"device_id": 1, "hostname": "test1"}]

        job = create_mock_job_runner(FilterDevicesJob)
        job.run(
            filters={},
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=False,
            server_key="default",
        )

        assert job.job.data["server_key"] == "resolved-default"

    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    @patch("netbox_librenms_plugin.import_utils.process_device_filters")
    def test_run_stores_job_data_correctly(self, mock_process, mock_api_class):
        """Job stores expected data structure."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api.server_key = "secondary"
        mock_api_class.return_value = mock_api

        mock_process.return_value = [
            {"device_id": 1, "hostname": "test1"},
            {"device_id": 2, "hostname": "test2"},
        ]

        job = create_mock_job_runner(FilterDevicesJob, job_pk=456)

        job.run(
            filters={"location": "dc1"},
            vc_detection_enabled=True,
            clear_cache=False,
            show_disabled=False,
            server_key="secondary",
        )

        # Verify job.data structure
        assert job.job.data["device_ids"] == [1, 2]
        assert job.job.data["total_processed"] == 2
        assert job.job.data["filters"] == {"location": "dc1"}
        assert job.job.data["server_key"] == "secondary"
        assert job.job.data["vc_detection_enabled"] is True
        assert job.job.data["cache_timeout"] == 300
        assert "cached_at" in job.job.data
        assert job.job.data["completed"] is True
        job.job.save.assert_called()

    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    @patch("netbox_librenms_plugin.import_utils.process_device_filters")
    def test_run_handles_empty_results(self, mock_process, mock_api_class):
        """Empty filter results handled gracefully."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api_class.return_value = mock_api

        mock_process.return_value = []

        job = create_mock_job_runner(FilterDevicesJob, job_pk=789)

        job.run(
            filters={"location": "nonexistent"},
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=False,
            server_key="default",
        )

        # Verify job data shows zero devices
        assert job.job.data["device_ids"] == []
        assert job.job.data["total_processed"] == 0
        assert job.job.data["completed"] is True

    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    @patch("netbox_librenms_plugin.import_utils.process_device_filters")
    def test_run_logs_progress(self, mock_process, mock_api_class):
        """Logger called with expected messages."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api_class.return_value = mock_api
        mock_process.return_value = [{"device_id": 1, "hostname": "test1"}]

        job = create_mock_job_runner(FilterDevicesJob)

        job.run(
            filters={"location": "site1"},
            vc_detection_enabled=True,
            clear_cache=False,
            show_disabled=False,
            server_key="default",
        )

        # Verify logger was called with expected messages
        assert job.logger.info.call_count >= 3
        info_calls = [call[0][0] for call in job.logger.info.call_args_list]
        assert any("Starting" in msg for msg in info_calls)
        assert any("completed" in msg.lower() for msg in info_calls)

    def test_job_meta_name(self):
        """Job has correct Meta.name."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        assert FilterDevicesJob.Meta.name == "LibreNMS Device Filter"


class TestImportDevicesJob:
    """Test ImportDevicesJob background job."""

    # Two LibreNMS ids (>=2) trigger the job's collision pre-check, which runs the real
    # validate_device_for_import against the DB (as the production rqworker does). The
    # fabricated hostnames match no existing NetBox object, so the batch resolves cleanly.
    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_device_only_import(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Import devices without VMs."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        # Pin server_key to a real string: the collision pre-check uses api.server_key as a
        # cache/Q discriminator, so a bare MagicMock() would let server-scoped logic pass on a
        # non-string sentinel and miss the multi-server regression this path protects.
        mock_api_class.return_value = MagicMock(server_key="default")
        # The collision pre-check fetches each device; return distinct devices so the batch
        # resolves cleanly with no collision (collision/unresolved handling has its own tests).
        # A "not found" here would now fail the batch closed (unresolved id), not skip silently.
        mock_api_class.return_value.get_device_info.side_effect = lambda did, **_kwargs: (
            True,
            {"device_id": did, "hostname": f"job-import-dev-{did}", "sysName": f"job-import-dev-{did}"},
        )

        # Mock successful device imports
        mock_device_1 = MagicMock()
        mock_device_1.pk = 100
        mock_device_2 = MagicMock()
        mock_device_2.pk = 101

        mock_bulk_devices.return_value = {
            "success": [
                {"device": mock_device_1, "device_id": 1},
                {"device": mock_device_2, "device_id": 2},
            ],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
        }

        job = create_mock_job_runner(ImportDevicesJob, job_pk=789)

        job.run(
            device_ids=[1, 2],
            vm_imports={},
            server_key="default",
            sync_options={"sync_interfaces": True},
        )

        # Verify device import was called
        mock_bulk_devices.assert_called_once()
        # VM import should not be called with empty dict
        mock_bulk_vms.assert_not_called()

        # Verify job.data
        assert job.job.data["imported_device_pks"] == [100, 101]
        assert job.job.data["imported_vm_pks"] == []
        assert job.job.data["success_count"] == 2
        assert job.job.data["failed_count"] == 0

    # Two VM ids (>=2) now trigger the job's collision pre-check too — it runs over the WHOLE
    # batch (devices + VMs), so configure the api the same way the device-batch tests do.
    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_vm_only_import(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Import VMs without devices."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock(server_key="default")
        # The batch-wide collision pre-check now fetches each VM id; return distinct devices so the
        # VM-only batch resolves cleanly (no collision) and the VM import still proceeds.
        mock_api_class.return_value.get_device_info.side_effect = lambda did, **_kwargs: (
            True,
            {"device_id": did, "hostname": f"job-vm-{did}", "sysName": f"job-vm-{did}"},
        )

        # Mock successful VM imports
        mock_vm_1 = MagicMock()
        mock_vm_1.pk = 200
        mock_vm_2 = MagicMock()
        mock_vm_2.pk = 201

        mock_bulk_vms.return_value = {
            "success": [
                {"device": mock_vm_1, "device_id": 10},
                {"device": mock_vm_2, "device_id": 11},
            ],
            "failed": [],
            "skipped": [],
        }

        job = create_mock_job_runner(ImportDevicesJob, job_pk=790)

        job.run(
            device_ids=[],
            vm_imports={10: {"cluster_id": 1}, 11: {"cluster_id": 1}},
            server_key="default",
        )

        # Verify device import was not called with empty list
        mock_bulk_devices.assert_not_called()
        # VM import should be called
        mock_bulk_vms.assert_called_once()

        # Verify job.data
        assert job.job.data["imported_device_pks"] == []
        assert job.job.data["imported_vm_pks"] == [200, 201]
        assert job.job.data["success_count"] == 2

    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_vm_only_batch_is_collision_gated(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """A VM-only batch skips unverifiable rows through the shared collision pre-check."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock(server_key="default")
        # get_device_info fails for the VM ids → they can't be collision-checked → the batch-wide
        # pre-check must skip those rows and the VM import (before the fix it only saw device_ids,
        # so a VM-only batch bypassed it entirely and bulk_import_vms ran unchecked).
        mock_api_class.return_value.get_device_info.side_effect = lambda did, **_kwargs: (False, None)

        job = create_mock_job_runner(ImportDevicesJob, job_pk=801)

        job.run(
            device_ids=[],
            vm_imports={10: {"cluster_id": 1}, 11: {"cluster_id": 1}},
            server_key="default",
        )

        # The VM import must NOT run because every submitted row was skipped.
        mock_bulk_vms.assert_not_called()
        mock_bulk_devices.assert_not_called()
        errors = job.job.data["errors"]
        assert {error["device_id"] for error in errors} == {10, 11}
        assert all("Skipped" in error["error"] and "verify collisions" in error["error"] for error in errors)
        assert job.job.data["failed_count"] == 2
        assert job.job.data["success_count"] == 0

    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_cancelled_during_precheck_blocks_batch_with_cancel_message(
        self, mock_api_class, mock_bulk_devices, mock_bulk_vms
    ):
        """A job cancelled during the collision pre-check stops scanning immediately, imports nothing, and reports the block as a cancellation — not as the fetch-failure message the genuine unresolved path uses."""
        from netbox_librenms_plugin.import_utils import bulk_import as bulk_import_module
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock(server_key="default")
        job = create_mock_job_runner(ImportDevicesJob, job_pk=802)

        # _is_job_cancelled reads RQ/Redis job state — patch that one external boundary; the
        # pre-check loop and the jobs.py message branch both read it through this module attr.
        with patch.object(bulk_import_module, "_is_job_cancelled", return_value=True):
            job.run(device_ids=[1, 2], vm_imports={10: {"cluster_id": 1}}, server_key="default")

        mock_bulk_devices.assert_not_called()
        mock_bulk_vms.assert_not_called()
        # Cancelled at the first poll → the scan issued ZERO LibreNMS calls.
        mock_api_class.return_value.get_device_info.assert_not_called()
        errors = job.job.data["errors"]
        assert errors, "blocked rows must surface as errors"
        assert all("cancelled during the collision pre-check" in e["error"] for e in errors)
        assert job.job.data["failed_count"] == 3  # 2 device rows + 1 VM row fail closed
        assert job.job.data["success_count"] == 0

    # device + VM = 2 ids → the batch-wide collision pre-check runs over both; configure the api.
    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_mixed_device_and_vm_import(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Import both devices and VMs."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api = MagicMock()
        mock_api.server_key = "non-default"
        # Distinct devices so the mixed batch resolves cleanly and both imports proceed.
        mock_api.get_device_info.side_effect = lambda did, **_kwargs: (
            True,
            {"device_id": did, "hostname": f"job-mix-{did}", "sysName": f"job-mix-{did}"},
        )
        mock_api_class.return_value = mock_api

        # Mock device imports
        mock_device = MagicMock()
        mock_device.pk = 100

        mock_bulk_devices.return_value = {
            "success": [{"device": mock_device, "device_id": 1}],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
            "cancelled": False,
        }

        # Mock VM imports
        mock_vm = MagicMock()
        mock_vm.pk = 200

        mock_bulk_vms.return_value = {
            "success": [{"device": mock_vm, "device_id": 10}],
            "failed": [],
            "skipped": [],
        }

        job = create_mock_job_runner(ImportDevicesJob, job_pk=791)

        job.run(
            device_ids=[1],
            vm_imports={10: {"cluster_id": 1}},
            server_key="non-default",
        )

        # Both should be called
        mock_bulk_devices.assert_called_once()
        mock_bulk_vms.assert_called_once()

        # Verify server_key (via api.server_key) is forwarded to bulk_import_devices_shared
        bulk_devices_kwargs = mock_bulk_devices.call_args[1]
        assert bulk_devices_kwargs.get("server_key") == "non-default"

        # Verify bulk_import_vms received the api with the correct server_key
        bulk_vms_positional = mock_bulk_vms.call_args[0]
        assert bulk_vms_positional[1].server_key == "non-default"

        # Verify combined results
        assert job.job.data["imported_device_pks"] == [100]
        assert job.job.data["imported_vm_pks"] == [200]
        assert job.job.data["success_count"] == 2
        assert job.job.data["total"] == 2

    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_with_sync_options(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Sync options passed to bulk import."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock()

        mock_bulk_devices.return_value = {
            "success": [],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
        }

        job = create_mock_job_runner(ImportDevicesJob, job_pk=792)

        sync_options = {
            "sync_interfaces": True,
            "sync_cables": False,
            "use_sysname": True,
            "strip_domain": True,
        }

        job.run(
            device_ids=[1],
            vm_imports={},
            server_key="default",
            sync_options=sync_options,
        )

        # Verify sync_options passed to bulk_import_devices_shared
        call_kwargs = mock_bulk_devices.call_args.kwargs
        assert call_kwargs["sync_options"] == sync_options

    # >=2 ids → the collision pre-check runs real validation against the DB (see note above).
    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_with_manual_mappings(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Manual mappings passed correctly."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        # Pin server_key to a real string so the collision pre-check's server-scoped cache/Q
        # logic runs on a real discriminator, not a MagicMock sentinel.
        mock_api_class.return_value = MagicMock(server_key="default")
        # Collision pre-check fetches each device; return distinct devices so the batch resolves
        # cleanly (a "not found" would now fail the batch closed as an unresolved id).
        mock_api_class.return_value.get_device_info.side_effect = lambda did, **_kwargs: (
            True,
            {"device_id": did, "hostname": f"job-mm-dev-{did}", "sysName": f"job-mm-dev-{did}"},
        )

        mock_bulk_devices.return_value = {
            "success": [],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
        }

        job = create_mock_job_runner(ImportDevicesJob, job_pk=793)

        manual_mappings = {
            1: {"site_id": 10, "device_role_id": 5},
            2: {"site_id": 11, "device_role_id": 6},
        }

        job.run(
            device_ids=[1, 2],
            vm_imports={},
            manual_mappings_per_device=manual_mappings,
            server_key="default",
        )

        # Verify manual_mappings passed to bulk_import_devices_shared
        call_kwargs = mock_bulk_devices.call_args.kwargs
        assert call_kwargs["manual_mappings_per_device"] == manual_mappings

    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_stores_imported_pks(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Imported device/VM PKs stored in job.data."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock()

        mock_device = MagicMock()
        mock_device.pk = 100

        mock_bulk_devices.return_value = {
            "success": [{"device": mock_device, "device_id": 1}],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
        }

        job = create_mock_job_runner(ImportDevicesJob, job_pk=794)

        job.run(device_ids=[1], vm_imports={}, server_key="default")

        assert 100 in job.job.data["imported_device_pks"]

    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_stores_libre_device_ids(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """LibreNMS device IDs stored for re-render."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock()

        mock_device = MagicMock()
        mock_device.pk = 100

        mock_bulk_devices.return_value = {
            "success": [{"device": mock_device, "device_id": 42}],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
        }

        job = create_mock_job_runner(ImportDevicesJob, job_pk=795)

        job.run(device_ids=[42], vm_imports={}, server_key="default")

        assert 42 in job.job.data["imported_libre_device_ids"]

    # device + VM = 2 ids → the batch-wide collision pre-check runs over both; configure the api so
    # the batch resolves cleanly and the mocked device/VM error payloads are actually exercised.
    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_aggregates_errors(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Device and VM errors are combined in job.data."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock(server_key="default")
        mock_api_class.return_value.get_device_info.side_effect = lambda did, **_kwargs: (
            True,
            {"device_id": did, "hostname": f"job-agg-{did}", "sysName": f"job-agg-{did}"},
        )

        # Mock mixed results
        mock_bulk_devices.return_value = {
            "success": [],
            "failed": [{"device_id": 1, "error": "Device type not found"}],
            "skipped": [],
            "virtual_chassis_created": 0,
        }
        mock_bulk_vms.return_value = {
            "success": [],
            "failed": [{"device_id": 10, "error": "Cluster not specified"}],
            "skipped": [],
        }

        job = create_mock_job_runner(ImportDevicesJob, job_pk=999)

        job.run(
            device_ids=[1],
            vm_imports={10: {"cluster": None}},
            server_key="default",
        )

        # Verify errors aggregated
        assert len(job.job.data["errors"]) == 2
        assert job.job.data["failed_count"] == 2
        assert job.job.data["success_count"] == 0

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_handles_all_failures(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """All imports fail gracefully."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        # Pin server_key to a real string so the collision pre-check runs server-scoped cache/Q
        # logic on a real discriminator rather than a MagicMock sentinel.
        mock_api_class.return_value = MagicMock(server_key="default")
        # Collision pre-check fetches each device; return distinct devices so the batch resolves
        # cleanly (no collision/unresolved) and the import actually reaches
        # bulk_import_devices_shared. A "not found" here would fail the batch closed in the
        # unresolved branch, so the mocked all-failure payload below would never be exercised.
        mock_api_class.return_value.get_device_info.side_effect = lambda did, **_kwargs: (
            True,
            {"device_id": did, "hostname": f"job-fail-dev-{did}", "sysName": f"job-fail-dev-{did}"},
        )

        mock_bulk_devices.return_value = {
            "success": [],
            "failed": [
                {"device_id": 1, "error": "Error 1"},
                {"device_id": 2, "error": "Error 2"},
            ],
            "skipped": [],
            "virtual_chassis_created": 0,
        }

        job = create_mock_job_runner(ImportDevicesJob, job_pk=800)

        job.run(device_ids=[1, 2], vm_imports={}, server_key="default")

        # The import path is actually exercised, and the bulk-import failures (not an
        # unresolved/collision block message) are the stored errors.
        mock_bulk_devices.assert_called_once()
        assert job.job.data["errors"] == [
            {"device_id": 1, "error": "Error 1"},
            {"device_id": 2, "error": "Error 2"},
        ]
        assert job.job.data["success_count"] == 0
        assert job.job.data["failed_count"] == 2
        assert job.job.data["completed"] is True
        job.job.save.assert_called()

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_unresolved_row_is_skipped_not_batch_blocked(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """An unresolved id is SKIPPED (not imported) while the rest of the batch — devices AND VMs — imports. A transient fetch miss on one row must not drop the whole submission (the old whole-batch block)."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api = MagicMock()
        mock_api.server_key = "default"

        # Real collision gate against the DB: ids 1 and 10 resolve + validate cleanly (match no
        # existing NetBox device); id 2 is a get_device_info miss → unresolved → skipped, not a block.
        def _get_device_info(did, **_kwargs):
            if did == 2:
                return (False, None)
            return (True, {"device_id": did, "hostname": f"job-skip-dev-{did}", "sysName": f"job-skip-dev-{did}"})

        mock_api.get_device_info.side_effect = _get_device_info
        mock_api_class.return_value = mock_api
        mock_bulk_devices.return_value = {"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0}
        mock_bulk_vms.return_value = {"success": [], "failed": [], "skipped": []}

        job = create_mock_job_runner(ImportDevicesJob, job_pk=810)
        job.run(device_ids=[1, 2], vm_imports={10: {"cluster_id": 1}}, server_key="default")

        # The fetchable rows import; only the unresolved id 2 is dropped. The importer is called
        # with just the importable device id, and the VM section still runs.
        mock_bulk_devices.assert_called_once()
        assert mock_bulk_devices.call_args.kwargs["device_ids"] == [1]
        mock_bulk_vms.assert_called_once()
        assert mock_bulk_vms.call_args.args[0] == {10: {"cluster_id": 1}}

        errors = job.job.data["errors"]
        # Only the unresolved row is reported (as a skip) — not the whole batch.
        assert {e["device_id"] for e in errors} == {2}
        assert all("Skipped" in e["error"] and "verify collisions" in e["error"] for e in errors)
        # Object-neutral wording: "row(s)", never "device(s)".
        assert all("device(s)" not in e["error"] for e in errors)
        assert any("row(s)" in e["error"] for e in errors)
        assert job.job.data["failed_count"] == 1
        assert job.job.data["success_count"] == 0
        assert job.job.data["completed"] is True

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_vm_row_is_collision_checked_in_vm_mode_not_device_mode(
        self, mock_api_class, mock_bulk_devices, mock_bulk_vms
    ):
        """End-to-end through the job's REAL collision gate: a VM row whose serial happens to equal an existing Device's serial must not be Device-serial-matched onto it — that would fabricate a collision with the device row legitimately targeting that Device and block a valid batch that the real VM import (which skips serial matching) would import fine."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("job-phantom-host", serial="ZZSER-JOB-PHANTOM")

        mock_api = MagicMock()
        mock_api.server_key = "default"

        def _get_device_info(did, **_kwargs):
            if did == 1:  # device import row → hostname-matches the existing device (fine alone)
                return (
                    True,
                    {"device_id": 1, "hostname": "job-phantom-host", "sysName": "job-phantom-host", "serial": ""},
                )
            # VM import row → hostname matches nothing; serial equals the Device's.
            return (
                True,
                {
                    "device_id": did,
                    "hostname": "job-vm-unique",
                    "sysName": "job-vm-unique",
                    "serial": "ZZSER-JOB-PHANTOM",
                },
            )

        mock_api.get_device_info.side_effect = _get_device_info
        mock_api_class.return_value = mock_api
        mock_bulk_devices.return_value = {"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0}
        mock_bulk_vms.return_value = {"success": [], "failed": [], "skipped": []}

        job = create_mock_job_runner(ImportDevicesJob, job_pk=811)
        job.run(device_ids=[1], vm_imports={10: {"cluster_id": 1}}, server_key="default")

        # No fabricated collision → the clean batch imports BOTH halves.
        mock_bulk_devices.assert_called_once()
        mock_bulk_vms.assert_called_once()
        assert job.job.data["errors"] == []
        assert job.job.data["failed_count"] == 0

    def test_job_meta_name(self):
        """Job has correct Meta.name."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        assert ImportDevicesJob.Meta.name == "LibreNMS Device Import"

    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_import_job_stores_server_key(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Import job stores resolved api.server_key in job metadata and forwards it to bulk_import_devices_shared."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api = MagicMock()
        mock_api.server_key = "resolved-default"
        mock_api_class.return_value = mock_api
        mock_bulk_devices.return_value = {
            "success": [],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
        }
        mock_bulk_vms.return_value = {"success": [], "failed": [], "skipped": []}

        job = create_mock_job_runner(ImportDevicesJob)
        job.run(device_ids=[1], vm_imports={}, server_key="default")

        assert job.job.data["server_key"] == "resolved-default"
        mock_bulk_devices.assert_called_once()
        call_kwargs = mock_bulk_devices.call_args[1]
        assert call_kwargs.get("server_key") == "resolved-default"

    @staticmethod
    def _real_user(username, *vm_or_device_perms):
        """Create a real NetBox user, granting perms the NetBox way (ObjectPermission).

        NetBox's only enforcement backend is ObjectPermissionBackend, so Django's
        ``user_permissions`` would not make ``has_perm`` pass — the gate must be
        exercised through real ObjectPermission rows, not a mocked ``has_perm``.
        """
        from core.models import ObjectType
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        user = get_user_model().objects.create_user(username=username)
        for app_label, model, action in vm_or_device_perms:
            perm = ObjectPermission.objects.create(name=f"{username}-{app_label}.{action}_{model}", actions=[action])
            perm.object_types.add(ObjectType.objects.get_by_natural_key(app_label, model))
            perm.users.add(user)
        # Refetch: the permission backend caches per instance on first has_perm call.
        return get_user_model().objects.get(pk=user.pk)

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_unauthorized_user_is_blocked_before_any_librenms_call(
        self, mock_api_class, mock_bulk_devices, mock_bulk_vms
    ):
        """A submitter without import permissions is rejected BEFORE the collision pre-check — no API client, no LibreNMS queries, no collision details computed (the per-path checks inside the import helpers only run after the scan)."""
        from django.core.exceptions import PermissionDenied

        from netbox_librenms_plugin.jobs import ImportDevicesJob

        # Even if the gate were bypassed, keep the pre-check viable so the failure mode
        # on unfixed code is "scan ran + import mocks reached", not an unpacking error.
        mock_api_class.return_value = MagicMock(server_key="default")
        mock_api_class.return_value.get_device_info.side_effect = lambda did, **_kwargs: (
            True,
            {"device_id": did, "hostname": f"authgate-{did}", "sysName": f"authgate-{did}"},
        )

        job = create_mock_job_runner(ImportDevicesJob, job_pk=803)
        job.job.user = self._real_user("import-noperms")

        with pytest.raises(PermissionDenied):
            job.run(device_ids=[1, 2], vm_imports={}, server_key="default")

        mock_api_class.assert_not_called()
        mock_api_class.return_value.get_device_info.assert_not_called()
        mock_bulk_devices.assert_not_called()
        mock_bulk_vms.assert_not_called()

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_device_batch_rejected_with_only_the_vm_permission(
        self, mock_api_class, mock_bulk_devices, mock_bulk_vms
    ):
        """A device batch needs add/change Device permissions; the VM permission alone must not open the pre-check."""
        from django.core.exceptions import PermissionDenied

        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock(server_key="default")

        job = create_mock_job_runner(ImportDevicesJob, job_pk=804)
        job.job.user = self._real_user("import-vm-perm-only", ("virtualization", "virtualmachine", "add"))

        with pytest.raises(PermissionDenied):
            job.run(device_ids=[1, 2], vm_imports={}, server_key="default")

        mock_api_class.assert_not_called()
        mock_bulk_devices.assert_not_called()

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_device_batch_passes_without_vm_permission(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """A device-only batch reaches the importer with add/change Device permissions."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock(server_key="default")
        mock_bulk_devices.return_value = {
            "success": [],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
        }

        job = create_mock_job_runner(ImportDevicesJob, job_pk=806)
        job.job.user = self._real_user(
            "import-device-without-vm",
            ("dcim", "device", "add"),
            ("dcim", "device", "change"),
        )

        job.run(device_ids=[1], vm_imports={}, server_key="default")

        mock_api_class.assert_called_once()
        mock_bulk_devices.assert_called_once()
        mock_bulk_vms.assert_not_called()

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_vm_only_batch_passes_with_only_the_vm_permission(
        self, mock_api_class, mock_bulk_devices, mock_bulk_vms
    ):
        """The gate scopes to the batch: a VM-only submission requires only virtualization.add_virtualmachine, so that single grant reaches the pre-check and the VM import."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock(server_key="default")
        mock_api_class.return_value.get_device_info.side_effect = lambda did, **_kwargs: (
            True,
            {"device_id": did, "hostname": f"vmgate-{did}", "sysName": f"vmgate-{did}"},
        )
        mock_bulk_vms.return_value = {"success": [], "failed": [], "skipped": []}

        job = create_mock_job_runner(ImportDevicesJob, job_pk=805)
        job.job.user = self._real_user("import-vm-only", ("virtualization", "virtualmachine", "add"))

        job.run(device_ids=[], vm_imports={10: {"cluster_id": 1}, 11: {"cluster_id": 1}}, server_key="default")

        # The gate let the batch through: the pre-check scanned it and the VM import ran.
        mock_bulk_vms.assert_called_once()
        mock_bulk_devices.assert_not_called()


class TestLoadJobResults:
    """Test loading results from completed background jobs."""

    @pytest.fixture(autouse=True)
    def _configured_job_servers(self):
        """Give persisted-job tests explicit usable server keys."""
        with patch(
            "netbox_librenms_plugin.server_selection.LibreNMSAPI.get_available_servers",
            return_value={"default": "Default", "primary": "Primary", "secondary": "Secondary"},
        ):
            yield

    @patch("netbox_librenms_plugin.views.imports.list.cache")
    @patch("netbox_librenms_plugin.import_utils.get_validated_device_cache_key")
    @patch("core.models.Job")
    def test_load_success_uses_correct_cache_keys(self, mock_job_class, mock_get_key, mock_cache):
        """Load uses get_validated_device_cache_key with job data."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        # Setup mock job
        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.data = {
            "device_ids": [1, 2],
            "filters": {"location": "dc1"},
            "server_key": "primary",
            "vc_detection_enabled": True,
            "cached_at": "2026-01-20T10:00:00Z",
            "cache_timeout": 600,
            "use_sysname": True,
            "strip_domain": False,
        }
        mock_job_class.objects.get.return_value = mock_job

        # Mock cache key generation
        mock_get_key.side_effect = lambda **kw: f"key_{kw['device_id']}"

        # Mock cache returns
        mock_cache.get.side_effect = [
            {"device_id": 1, "hostname": "test1"},
            {"device_id": 2, "hostname": "test2"},
        ]

        view = LibreNMSImportView()
        view.rebind_api_for_server = MagicMock(return_value="primary")
        results = view._load_job_results(123, MagicMock())

        # Verify cache key function called with correct params
        assert mock_get_key.call_count == 2
        mock_get_key.assert_any_call(
            server_key="primary",
            filters={"location": "dc1"},
            device_id=1,
            vc_enabled=True,
            use_sysname=True,
            strip_domain=False,
        )
        mock_get_key.assert_any_call(
            server_key="primary",
            filters={"location": "dc1"},
            device_id=2,
            vc_enabled=True,
            use_sysname=True,
            strip_domain=False,
        )

        assert len(results) == 2
        view.rebind_api_for_server.assert_called_once_with("primary")

    @patch("netbox_librenms_plugin.views.imports.list.cache")
    @patch("netbox_librenms_plugin.import_utils.get_validated_device_cache_key")
    @patch("core.models.Job")
    def test_load_extracts_filters_from_job_data(self, mock_job_class, mock_get_key, mock_cache):
        """Filters, server_key, vc_enabled extracted from job data."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.data = {
            "device_ids": [1],
            "filters": {"location": "dc2", "type": "router"},
            "server_key": "secondary",
            "vc_detection_enabled": False,
            "cached_at": "2026-01-20T10:00:00Z",
            "cache_timeout": 300,
            "use_sysname": True,
            "strip_domain": False,
        }
        mock_job_class.objects.get.return_value = mock_job
        mock_get_key.return_value = "test_key"
        mock_cache.get.return_value = {"device_id": 1}

        view = LibreNMSImportView()
        view.rebind_api_for_server = MagicMock(return_value="secondary")
        view._load_job_results(456, MagicMock())

        # Verify get_validated_device_cache_key called with extracted values
        mock_get_key.assert_called_once_with(
            server_key="secondary",
            filters={"location": "dc2", "type": "router"},
            device_id=1,
            vc_enabled=False,
            use_sysname=True,
            strip_domain=False,
        )
        view.rebind_api_for_server.assert_called_once_with("secondary")

    @patch("netbox_librenms_plugin.views.imports.list.cache")
    @patch("netbox_librenms_plugin.import_utils.get_validated_device_cache_key")
    @patch("core.models.Job")
    def test_load_returns_cached_devices(self, mock_job_class, mock_get_key, mock_cache):
        """Devices retrieved from cache."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.data = {
            "device_ids": [1, 2],
            "filters": {},
            "server_key": "default",
            "vc_detection_enabled": False,
            "cached_at": "2026-01-20T10:00:00Z",
            "cache_timeout": 300,
        }
        mock_job_class.objects.get.return_value = mock_job
        mock_get_key.side_effect = lambda **kw: f"key_{kw['device_id']}"
        mock_cache.get.side_effect = [
            {"device_id": 1, "hostname": "device1"},
            {"device_id": 2, "hostname": "device2"},
        ]

        view = LibreNMSImportView()
        results = view._load_job_results(789, MagicMock())

        assert len(results) == 2
        assert results[0]["hostname"] == "device1"
        assert results[1]["hostname"] == "device2"

    @patch("netbox_librenms_plugin.views.imports.list.cache")
    @patch("netbox_librenms_plugin.import_utils.get_validated_device_cache_key")
    @patch("core.models.Job")
    def test_load_sets_cache_metadata(self, mock_job_class, mock_get_key, mock_cache):
        """Load sets _cache_timestamp and _cache_timeout on view."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.data = {
            "device_ids": [1],
            "filters": {},
            "server_key": "default",
            "vc_detection_enabled": False,
            "cached_at": "2026-01-20T12:00:00Z",
            "cache_timeout": 900,
        }
        mock_job_class.objects.get.return_value = mock_job
        mock_get_key.return_value = "test_key"
        mock_cache.get.return_value = {"device_id": 1}

        view = LibreNMSImportView()
        view._load_job_results(456, MagicMock())

        assert view._cache_timestamp == "2026-01-20T12:00:00Z"
        assert view._cache_timeout == 900

    @patch("core.models.Job")
    def test_load_job_not_found_returns_empty(self, mock_job_class):
        """Non-existent job returns empty list."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        # Create a mock DoesNotExist exception
        mock_job_class.DoesNotExist = Exception
        mock_job_class.objects.get.side_effect = mock_job_class.DoesNotExist

        view = LibreNMSImportView()
        results = view._load_job_results(999, MagicMock())

        assert results == []

    @patch("core.models.Job")
    def test_load_job_not_completed_returns_empty(self, mock_job_class):
        """Running job returns empty list."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        mock_job = MagicMock()
        mock_job.status = "running"
        mock_job_class.objects.get.return_value = mock_job

        view = LibreNMSImportView()
        results = view._load_job_results(123, MagicMock())

        assert results == []

    @patch("netbox_librenms_plugin.views.imports.list.cache")
    @patch("netbox_librenms_plugin.import_utils.get_validated_device_cache_key")
    @patch("core.models.Job")
    def test_load_expired_cache_returns_empty(self, mock_job_class, mock_get_key, mock_cache):
        """All cache misses returns empty list."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.data = {
            "device_ids": [1, 2],
            "filters": {},
            "server_key": "default",
            "vc_detection_enabled": False,
            "cached_at": "2026-01-20T10:00:00Z",
            "cache_timeout": 300,
        }
        mock_job_class.objects.get.return_value = mock_job
        mock_get_key.side_effect = lambda **kw: f"key_{kw['device_id']}"

        # Simulate expired cache (returns None)
        mock_cache.get.return_value = None

        view = LibreNMSImportView()
        results = view._load_job_results(123, MagicMock())

        assert results == []

    @patch("netbox_librenms_plugin.views.imports.list.cache")
    @patch("netbox_librenms_plugin.import_utils.get_validated_device_cache_key")
    @patch("core.models.Job")
    def test_load_partial_cache_returns_available(self, mock_job_class, mock_get_key, mock_cache):
        """Some expired, returns available devices."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.data = {
            "device_ids": [1, 2, 3],
            "filters": {},
            "server_key": "default",
            "vc_detection_enabled": False,
            "cached_at": "2026-01-20T10:00:00Z",
            "cache_timeout": 300,
        }
        mock_job_class.objects.get.return_value = mock_job
        mock_get_key.side_effect = lambda **kw: f"key_{kw['device_id']}"

        # First device in cache, second expired, third in cache
        mock_cache.get.side_effect = [
            {"device_id": 1, "hostname": "device1"},
            None,  # Expired
            {"device_id": 3, "hostname": "device3"},
        ]

        view = LibreNMSImportView()
        results = view._load_job_results(123, MagicMock())

        # Should return available devices only
        assert len(results) == 2
        assert results[0]["device_id"] == 1
        assert results[1]["device_id"] == 3


class TestGracefulFallback:
    """Test graceful fallback when RQ workers unavailable."""

    def _make_view_with_request(self, superuser=True, query_params=None):
        """Helper to set up a LibreNMSImportView with a mock request."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        request = MagicMock()
        request.user.is_superuser = superuser
        request.user.username = "testuser"
        request.GET = QueryDict("", mutable=True)
        request.GET.update(query_params or {})
        view.request = request
        return view, request

    @patch("netbox_librenms_plugin.views.imports.list.get_workers_for_queue")
    def test_no_workers_triggers_synchronous_processing(self, mock_get_workers):
        """No RQ workers: view falls back to synchronous processing, FilterDevicesJob.enqueue not called."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        mock_get_workers.return_value = 0

        view, request = self._make_view_with_request(
            superuser=True,
            query_params={"apply_filters": "1", "librenms_location": "DC1"},
        )
        mock_api = MagicMock()
        mock_api.server_key = "default"

        with (
            patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)),
            patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings,
            patch("netbox_librenms_plugin.views.imports.list.get_user_pref", return_value=None),
            patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache,
            patch("netbox_librenms_plugin.import_utils.get_cache_metadata_key", return_value="meta_key"),
            patch("netbox_librenms_plugin.import_utils.get_device_count_for_filters", return_value=5),
            patch("netbox_librenms_plugin.views.imports.list.render") as mock_render,
            patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"),
            patch(
                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches_for_servers",
                return_value=[],
            ),
            patch("netbox_librenms_plugin.jobs.FilterDevicesJob") as mock_job_cls,
            patch("netbox_librenms_plugin.views.imports.list.messages"),
            patch("netbox_librenms_plugin.views.imports.list.process_device_filters") as mock_pdf,
        ):
            mock_settings.objects.first.return_value = None
            mock_settings.objects.get_or_create.return_value = (None, False)
            mock_cache.get.return_value = None
            mock_render.return_value = MagicMock()

            mock_form_cls = MagicMock()
            mock_form = MagicMock()
            mock_form.is_valid.return_value = True
            mock_form.cleaned_data = {"enable_vc_detection": False, "clear_cache": False, "use_background_job": True}
            mock_form_cls.return_value = mock_form
            view.filterset_form = mock_form_cls
            mock_pdf.return_value = ([], False)

            with patch.object(view, "get_server_info", return_value={}):
                view.get(request)

        # Workers == 0 means synchronous fallback — enqueue must not be called
        mock_job_cls.enqueue.assert_not_called()
        mock_render.assert_called_once()
        mock_pdf.assert_called_once()

    @patch("netbox_librenms_plugin.views.imports.list.get_workers_for_queue")
    def test_workers_available_allows_background_job(self, mock_get_workers):
        """Available workers: view enqueues FilterDevicesJob and returns JSON response."""
        from django.http import JsonResponse

        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        mock_get_workers.return_value = 2

        view, request = self._make_view_with_request(
            superuser=True,
            query_params={"apply_filters": "1", "librenms_location": "DC1"},
        )
        mock_api = MagicMock()
        mock_api.server_key = "server-2"

        with (
            patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)),
            patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings,
            patch("netbox_librenms_plugin.views.imports.list.get_user_pref", return_value=None),
            patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache,
            patch("netbox_librenms_plugin.import_utils.get_cache_metadata_key", return_value="meta_key"),
            patch("netbox_librenms_plugin.import_utils.get_device_count_for_filters", return_value=10),
            patch("netbox_librenms_plugin.jobs.FilterDevicesJob") as mock_job_cls,
        ):
            mock_settings.objects.first.return_value = None
            mock_cache.get.return_value = None
            mock_job = MagicMock()
            mock_job.pk = 99
            mock_job.job_id = "uuid-99"
            mock_job_cls.enqueue.return_value = mock_job

            mock_form_cls = MagicMock()
            mock_form = MagicMock()
            mock_form.is_valid.return_value = True
            mock_form.cleaned_data = {"enable_vc_detection": False, "clear_cache": False, "use_background_job": True}
            mock_form_cls.return_value = mock_form
            view.filterset_form = mock_form_cls

            result = view.get(request)

        # Workers > 0 means background job should have been enqueued
        mock_job_cls.enqueue.assert_called_once()
        # Verify the non-default server_key was forwarded to the background job
        assert mock_job_cls.enqueue.call_args.kwargs["server_key"] == "server-2"
        assert isinstance(result, JsonResponse)

    @patch("netbox_librenms_plugin.views.imports.list.get_workers_for_queue")
    @patch("netbox_librenms_plugin.views.imports.list.messages")
    def test_fallback_logs_warning(self, mock_messages, mock_get_workers):
        """No workers: view logs a warning message when falling back to synchronous mode."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        mock_get_workers.return_value = 0

        view, request = self._make_view_with_request(
            superuser=True,
            query_params={"apply_filters": "1", "librenms_location": "DC1"},
        )
        mock_api = MagicMock()
        mock_api.server_key = "default"
        # Answer the way LibreNMS does: a search that matches nothing is 200 and an empty list.
        mock_api.list_devices.return_value = (True, [])

        with (
            patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)),
            patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings,
            patch("netbox_librenms_plugin.views.imports.list.get_user_pref", return_value=None),
            patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache,
            patch("netbox_librenms_plugin.import_utils.get_cache_metadata_key", return_value="meta_key"),
            patch("netbox_librenms_plugin.import_utils.get_device_count_for_filters", return_value=3),
            patch("netbox_librenms_plugin.views.imports.list.render") as mock_render,
            patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"),
            patch(
                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches_for_servers",
                return_value=[],
            ),
            patch("netbox_librenms_plugin.jobs.FilterDevicesJob"),
        ):
            mock_settings.objects.first.return_value = None
            mock_settings.objects.get_or_create.return_value = (None, False)
            mock_cache.get.return_value = None
            mock_render.return_value = MagicMock()

            mock_form_cls = MagicMock()
            mock_form = MagicMock()
            mock_form.is_valid.return_value = True
            mock_form.cleaned_data = {"enable_vc_detection": False, "clear_cache": False, "use_background_job": True}
            mock_form_cls.return_value = mock_form
            view.filterset_form = mock_form_cls

            with patch.object(view, "get_server_info", return_value={}):
                view.get(request)

        # A warning should be emitted when falling back to sync due to no workers
        mock_messages.warning.assert_called_once()
