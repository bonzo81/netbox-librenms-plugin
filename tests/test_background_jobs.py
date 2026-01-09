"""
Tests for background job implementation.

Tests the FilterDevicesJob, should_use_background_job logic, job result loading,
and graceful fallback behavior.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from netbox_librenms_plugin.jobs import FilterDevicesJob
from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

User = get_user_model()


class TestShouldUseBackgroundJob(TestCase):
    """Test the should_use_background_job decision logic."""

    def setUp(self):
        self.factory = RequestFactory()
        self.view = LibreNMSImportView()

    def test_vc_detection_triggers_background_job(self):
        """Test that VC detection triggers background job."""
        request = self.factory.get("/?enable_vc_detection=on")
        result = self.view.should_use_background_job(request)
        self.assertTrue(result)

    def test_clear_cache_triggers_background_job(self):
        """Test that cache clearing triggers background job."""
        request = self.factory.get("/?clear_cache=on")
        result = self.view.should_use_background_job(request)
        self.assertTrue(result)

    def test_both_vc_and_clear_cache_triggers_background_job(self):
        """Test that both VC detection and cache clear trigger background job."""
        request = self.factory.get("/?enable_vc_detection=on&clear_cache=on")
        result = self.view.should_use_background_job(request)
        self.assertTrue(result)

    def test_simple_filter_uses_synchronous(self):
        """Test that simple filters use synchronous processing."""
        request = self.factory.get("/?librenms_location=site1")
        result = self.view.should_use_background_job(request)
        self.assertFalse(result)

    def test_no_filters_uses_synchronous(self):
        """Test that no filters use synchronous processing."""
        request = self.factory.get("/")
        result = self.view.should_use_background_job(request)
        self.assertFalse(result)


@pytest.mark.django_db
class TestFilterDevicesJob:
    """Test the FilterDevicesJob background job."""

    @patch("netbox_librenms_plugin.jobs.LibreNMSAPI")
    @patch("netbox_librenms_plugin.jobs.process_device_filters")
    def test_job_run_processes_filters(self, mock_process, mock_api_class):
        """Test that job runs and processes filters correctly."""
        # Setup mocks
        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api.server_key = "default"
        mock_api_class.return_value = mock_api

        # Mock validated devices
        validated_devices = [
            {"device_id": 1, "hostname": "test1", "_validation": {}},
            {"device_id": 2, "hostname": "test2", "_validation": {}},
        ]
        mock_process.return_value = validated_devices

        # Create job instance
        job = FilterDevicesJob()
        job.job = MagicMock()
        job.job.pk = 123
        job.job.data = {}  # Initialize job.data to avoid AttributeError
        job.logger = MagicMock()

        # Run job
        filters = {"location": "site1"}
        job.run(
            filters=filters,
            vc_detection_enabled=True,
            clear_cache=False,
            show_disabled=False,
        )

        # Verify process_device_filters was called
        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args.kwargs
        assert call_kwargs["filters"] == filters
        assert call_kwargs["vc_detection_enabled"] is True
        assert call_kwargs["clear_cache"] is False
        assert call_kwargs["job"] == job

        # Verify job data was saved
        assert job.job.data["device_ids"] == [1, 2]
        assert job.job.data["total_processed"] == 2
        assert "cache_key_prefix" in job.job.data
        job.job.save.assert_called()

    @patch("netbox_librenms_plugin.jobs.LibreNMSAPI")
    @patch("netbox_librenms_plugin.jobs.process_device_filters")
    @patch("netbox_librenms_plugin.jobs.cache")
    def test_job_caches_results_individually(
        self, mock_cache, mock_process, mock_api_class
    ):
        """Test that job caches each device individually."""
        # Setup mocks
        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api_class.return_value = mock_api

        validated_devices = [
            {"device_id": 1, "hostname": "test1"},
            {"device_id": 2, "hostname": "test2"},
        ]
        mock_process.return_value = validated_devices

        # Create job
        job = FilterDevicesJob()
        job.job = MagicMock()
        job.job.pk = 456
        job.job.data = {}  # Initialize job.data to avoid AttributeError
        job.logger = MagicMock()

        # Run job
        job.run(
            filters={"location": "site1"},
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=False,
        )

        # Verify cache.set was called for each device
        assert mock_cache.set.call_count == 2
        call_args_list = mock_cache.set.call_args_list

        # Check first device was cached
        assert call_args_list[0][0][0] == "job_456_device_1"
        assert call_args_list[0][0][1] == validated_devices[0]
        assert call_args_list[0][1]["timeout"] == 300

        # Check second device was cached
        assert call_args_list[1][0][0] == "job_456_device_2"
        assert call_args_list[1][0][1] == validated_devices[1]

    @patch("netbox_librenms_plugin.jobs.LibreNMSAPI")
    @patch("netbox_librenms_plugin.jobs.process_device_filters")
    def test_job_handles_empty_results(self, mock_process, mock_api_class):
        """Test that job handles empty results gracefully."""
        mock_api = MagicMock()
        mock_api.cache_timeout = 300
        mock_api_class.return_value = mock_api

        mock_process.return_value = []

        job = FilterDevicesJob()
        job.job = MagicMock()
        job.job.pk = 789
        job.job.data = {}  # Initialize job.data to avoid AttributeError
        job.logger = MagicMock()

        job.run(
            filters={"location": "nonexistent"},
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=False,
        )

        # Verify job data shows zero devices
        assert job.job.data["device_ids"] == []
        assert job.job.data["total_processed"] == 0


class TestJobResultLoading(TestCase):
    """Test loading results from completed background jobs."""

    def setUp(self):
        self.view = LibreNMSImportView()

    @patch("netbox_librenms_plugin.views.imports.list.Job")
    @patch("netbox_librenms_plugin.views.imports.list.cache")
    def test_load_job_results_success(self, mock_cache, mock_job_class):
        """Test successfully loading results from completed job."""
        # Mock completed job
        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.data = {
            "device_ids": [1, 2],
            "cache_key_prefix": "job_123_device_",
        }
        mock_job_class.objects.get.return_value = mock_job

        # Mock cached devices
        device1 = {"device_id": 1, "hostname": "test1"}
        device2 = {"device_id": 2, "hostname": "test2"}
        mock_cache.get.side_effect = [device1, device2]

        # Load results
        results = self.view._load_job_results(123)

        # Verify results
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["device_id"], 1)
        self.assertEqual(results[1]["device_id"], 2)

        # Verify cache was queried correctly
        mock_cache.get.assert_any_call("job_123_device_1")
        mock_cache.get.assert_any_call("job_123_device_2")

    @patch("netbox_librenms_plugin.views.imports.list.Job")
    def test_load_job_results_job_not_found(self, mock_job_class):
        """Test handling of non-existent job."""
        from core.models import Job

        mock_job_class.objects.get.side_effect = Job.DoesNotExist
        mock_job_class.DoesNotExist = Job.DoesNotExist

        results = self.view._load_job_results(999)
        self.assertEqual(results, [])

    @patch("netbox_librenms_plugin.views.imports.list.Job")
    def test_load_job_results_job_not_completed(self, mock_job_class):
        """Test handling of job that is not yet completed."""
        mock_job = MagicMock()
        mock_job.status = "running"
        mock_job_class.objects.get.return_value = mock_job

        results = self.view._load_job_results(123)
        self.assertEqual(results, [])

    @patch("netbox_librenms_plugin.views.imports.list.Job")
    @patch("netbox_librenms_plugin.views.imports.list.cache")
    def test_load_job_results_cache_expired(self, mock_cache, mock_job_class):
        """Test handling of expired cache entries."""
        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.data = {
            "device_ids": [1, 2],
            "cache_key_prefix": "job_123_device_",
        }
        mock_job_class.objects.get.return_value = mock_job

        # Simulate expired cache (returns None)
        mock_cache.get.return_value = None

        results = self.view._load_job_results(123)

        # Should return empty list when all cache entries expired
        self.assertEqual(results, [])


@pytest.mark.django_db
class TestGracefulFallback:
    """Test graceful fallback when RQ workers are not running."""

    @patch("netbox_librenms_plugin.views.imports.list.get_workers_for_queue")
    @patch("netbox_librenms_plugin.views.imports.list.process_device_filters")
    def test_fallback_to_synchronous_when_no_workers(
        self, mock_process, mock_get_workers
    ):
        """Test that view falls back to synchronous when no RQ workers."""
        # Simulate no RQ workers running
        mock_get_workers.return_value = 0

        # Mock process_device_filters to return empty list
        mock_process.return_value = []

        # Create view with mocked API
        view = LibreNMSImportView()
        view.librenms_api = MagicMock()
        view.librenms_api.cache_timeout = 300

        factory = RequestFactory()
        user = User.objects.create_user("testuser")

        # Request with VC detection (would normally trigger job)
        request = factory.get(
            "/?apply_filters=1&enable_vc_detection=on&librenms_location=site1"
        )
        request.user = user

        # This should NOT return JSON, should process synchronously
        response = view.get(request)

        # Should be HTML response, not JSON
        assert response["Content-Type"] == "text/html; charset=utf-8"

        # process_device_filters should have been called synchronously
        mock_process.assert_called_once()
