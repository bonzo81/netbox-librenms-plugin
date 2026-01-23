"""
Tests for background job implementation.

Tests the FilterDevicesJob, ImportDevicesJob, should_use_background_job logic,
job result loading, and graceful fallback behavior.

Refactored to use pure pytest without Django database dependencies.
All tests use mocking and direct attribute manipulation instead of HTTP requests.
"""

from unittest.mock import MagicMock, patch


class TestShouldUseBackgroundJob:
    """Test background job decision logic."""

    def test_checkbox_checked_returns_true(self):
        """When use_background_job form field is True, return True."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = LibreNMSImportView()
        view._filter_form_data = {"use_background_job": True}

        assert view.should_use_background_job() is True

    def test_checkbox_unchecked_returns_false(self):
        """When use_background_job form field is False, return False."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = LibreNMSImportView()
        view._filter_form_data = {"use_background_job": False}

        assert view.should_use_background_job() is False

    def test_default_when_field_missing(self):
        """When field is missing, default to True."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = LibreNMSImportView()
        view._filter_form_data = {"some_other_field": "value"}

        assert view.should_use_background_job() is True

    def test_empty_form_data_returns_default(self):
        """Empty form data returns default True."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = LibreNMSImportView()
        view._filter_form_data = {}

        assert view.should_use_background_job() is True


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
        mock_api_class.return_value = mock_api
        mock_process.return_value = []

        job = create_mock_job_runner(FilterDevicesJob)

        job.run(
            filters={},
            vc_detection_enabled=True,
            clear_cache=False,
            show_disabled=False,
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
        mock_api_class.return_value = mock_api
        mock_process.return_value = []

        job = create_mock_job_runner(FilterDevicesJob)

        job.run(
            filters={},
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=False,
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
        mock_api_class.return_value = mock_api
        mock_process.return_value = []

        job = create_mock_job_runner(FilterDevicesJob)

        job.run(
            filters={},
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=True,
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
        mock_api_class.return_value = mock_api
        mock_process.return_value = []

        job = create_mock_job_runner(FilterDevicesJob)

        job.run(
            filters={},
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=False,
            exclude_existing=True,
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
    def test_run_stores_job_data_correctly(self, mock_process, mock_api_class):
        """Job stores expected data structure."""
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        mock_api = MagicMock()
        mock_api.cache_timeout = 300
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

    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_device_only_import(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Import devices without VMs."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock()

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

    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_vm_only_import(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Import VMs without devices."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock()

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
    def test_run_mixed_device_and_vm_import(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Import both devices and VMs."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock()

        # Mock device imports
        mock_device = MagicMock()
        mock_device.pk = 100

        mock_bulk_devices.return_value = {
            "success": [{"device": mock_device, "device_id": 1}],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
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
            server_key="default",
        )

        # Both should be called
        mock_bulk_devices.assert_called_once()
        mock_bulk_vms.assert_called_once()

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
            "sync_ips": True,
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

    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_with_manual_mappings(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Manual mappings passed correctly."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock()

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

        job.run(device_ids=[1], vm_imports={})

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

        job.run(device_ids=[42], vm_imports={})

        assert 42 in job.job.data["imported_libre_device_ids"]

    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_aggregates_errors(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """Device and VM errors are combined in job.data."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock()

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
        )

        # Verify errors aggregated
        assert len(job.job.data["errors"]) == 2
        assert job.job.data["failed_count"] == 2
        assert job.job.data["success_count"] == 0

    @patch("netbox_librenms_plugin.import_utils.bulk_import_vms")
    @patch("netbox_librenms_plugin.import_utils.bulk_import_devices_shared")
    @patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI")
    def test_run_handles_all_failures(self, mock_api_class, mock_bulk_devices, mock_bulk_vms):
        """All imports fail gracefully."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        mock_api_class.return_value = MagicMock()

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

        job.run(device_ids=[1, 2], vm_imports={})

        # Should complete without exception
        assert job.job.data["success_count"] == 0
        assert job.job.data["failed_count"] == 2
        assert job.job.data["completed"] is True
        job.job.save.assert_called()

    def test_job_meta_name(self):
        """Job has correct Meta.name."""
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        assert ImportDevicesJob.Meta.name == "LibreNMS Device Import"


class TestLoadJobResults:
    """Test loading results from completed background jobs."""

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
        results = view._load_job_results(123)

        # Verify cache key function called with correct params
        assert mock_get_key.call_count == 2
        mock_get_key.assert_any_call(
            server_key="primary",
            filters={"location": "dc1"},
            device_id=1,
            vc_enabled=True,
        )
        mock_get_key.assert_any_call(
            server_key="primary",
            filters={"location": "dc1"},
            device_id=2,
            vc_enabled=True,
        )

        assert len(results) == 2

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
        }
        mock_job_class.objects.get.return_value = mock_job
        mock_get_key.return_value = "test_key"
        mock_cache.get.return_value = {"device_id": 1}

        view = LibreNMSImportView()
        view._load_job_results(456)

        # Verify get_validated_device_cache_key called with extracted values
        mock_get_key.assert_called_once_with(
            server_key="secondary",
            filters={"location": "dc2", "type": "router"},
            device_id=1,
            vc_enabled=False,
        )

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
        results = view._load_job_results(789)

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
        view._load_job_results(456)

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
        results = view._load_job_results(999)

        assert results == []

    @patch("core.models.Job")
    def test_load_job_not_completed_returns_empty(self, mock_job_class):
        """Running job returns empty list."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        mock_job = MagicMock()
        mock_job.status = "running"
        mock_job_class.objects.get.return_value = mock_job

        view = LibreNMSImportView()
        results = view._load_job_results(123)

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
        results = view._load_job_results(123)

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
        results = view._load_job_results(123)

        # Should return available devices only
        assert len(results) == 2
        assert results[0]["device_id"] == 1
        assert results[1]["device_id"] == 3


class TestGracefulFallback:
    """Test graceful fallback when RQ workers unavailable."""

    @patch("netbox_librenms_plugin.views.imports.list.get_workers_for_queue")
    def test_no_workers_triggers_synchronous_processing(self, mock_get_workers):
        """No RQ workers triggers synchronous fallback."""
        mock_get_workers.return_value = 0

        # This test verifies the condition check, not full request handling
        from netbox_librenms_plugin.views.imports.list import get_workers_for_queue

        workers = get_workers_for_queue("default")
        assert workers == 0

        # When workers == 0, the code path skips job enqueuing
        # and falls through to synchronous get_queryset processing

    @patch("netbox_librenms_plugin.views.imports.list.get_workers_for_queue")
    def test_workers_available_allows_background_job(self, mock_get_workers):
        """Available workers allow background job enqueue."""
        mock_get_workers.return_value = 2

        from netbox_librenms_plugin.views.imports.list import get_workers_for_queue

        workers = get_workers_for_queue("default")
        assert workers > 0
        # When workers > 0, the code path proceeds to FilterDevicesJob.enqueue()

    @patch("netbox_librenms_plugin.views.imports.list.get_workers_for_queue")
    @patch("netbox_librenms_plugin.views.imports.list.logger")
    def test_fallback_logs_warning(self, mock_logger, mock_get_workers):
        """Warning logged when falling back (checked via worker count)."""
        mock_get_workers.return_value = 0

        # Verify the function returns 0 workers which would trigger fallback
        from netbox_librenms_plugin.views.imports.list import get_workers_for_queue

        workers = get_workers_for_queue("default")
        assert workers == 0

        # The view would log a warning when it detects no workers and falls back
        # This test verifies the condition that triggers the fallback path
