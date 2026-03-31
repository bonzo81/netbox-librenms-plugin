"""
Coverage tests for:
- netbox_librenms_plugin/api/views.py (sync_job_status, InterfaceTypeMappingViewSet)
- netbox_librenms_plugin/filtersets.py
- netbox_librenms_plugin/models.py (lines 45, 48, 68, 76)
"""

import json
from unittest.mock import MagicMock, patch


# ===========================================================================
# Helpers
# ===========================================================================


def _make_post_request():
    """Return a minimal Django HttpRequest suitable for DRF view tests."""
    from django.http import HttpRequest

    request = HttpRequest()
    request.method = "POST"
    request.META["SERVER_NAME"] = "localhost"
    request.META["SERVER_PORT"] = "80"
    return request


def _call_sync_job_status(job_pk, job_patch, rq_patch=None, queue_patch=None):
    """
    Call sync_job_status view, bypassing DRF auth/permission layer.

    Returns the raw Django response object.
    """
    from netbox_librenms_plugin.api.views import sync_job_status

    request = _make_post_request()

    # Bypass DRF initial() so we skip auth/permissions entirely
    with patch("rest_framework.views.APIView.initial"):
        with patch("netbox_librenms_plugin.api.views.Job", job_patch):
            if rq_patch is not None and queue_patch is not None:
                with patch("netbox_librenms_plugin.api.views.RQJob", rq_patch):
                    with patch("netbox_librenms_plugin.api.views.get_queue", queue_patch):
                        return sync_job_status(request, job_pk=job_pk)
            return sync_job_status(request, job_pk=job_pk)


# ===========================================================================
# api/views.py – sync_job_status
# ===========================================================================


class TestSyncJobStatusJobNotFound:
    """Test sync_job_status when the DB job does not exist."""

    def test_returns_404_when_job_missing(self):
        class _DoesNotExist(Exception):
            pass

        mock_job_cls = MagicMock()
        mock_job_cls.DoesNotExist = _DoesNotExist
        mock_job_cls.objects.get.side_effect = _DoesNotExist

        response = _call_sync_job_status(job_pk=999, job_patch=mock_job_cls)

        assert response.status_code == 404
        data = json.loads(response.content)
        assert data["error"] == "Job not found"


class TestSyncJobStatusRQJobActive:
    """Test sync_job_status when RQ job is still active (no update needed)."""

    def test_no_change_when_rq_job_running(self):
        from core.choices import JobStatusChoices

        mock_db_job = MagicMock()
        mock_db_job.pk = 1
        mock_db_job.status = JobStatusChoices.STATUS_RUNNING
        mock_db_job.completed = None

        class _DoesNotExist(Exception):
            pass

        mock_job_cls = MagicMock()
        mock_job_cls.DoesNotExist = _DoesNotExist
        mock_job_cls.objects.get.return_value = mock_db_job

        mock_rq_job = MagicMock()
        mock_rq_job.is_stopped = False
        mock_rq_job.is_failed = False
        mock_rq_job.get_status.return_value = "started"

        mock_rq_cls = MagicMock()
        mock_rq_cls.fetch.return_value = mock_rq_job

        mock_queue = MagicMock()
        mock_queue_fn = MagicMock(return_value=mock_queue)

        response = _call_sync_job_status(
            job_pk=1,
            job_patch=mock_job_cls,
            rq_patch=mock_rq_cls,
            queue_patch=mock_queue_fn,
        )

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "no_change"
        assert data["rq_status"] == "started"
        mock_db_job.save.assert_not_called()


class TestSyncJobStatusRQJobStopped:
    """Test sync_job_status when RQ job is stopped/failed."""

    def test_updates_db_when_rq_stopped_and_not_yet_completed(self):
        from core.choices import JobStatusChoices

        mock_db_job = MagicMock()
        mock_db_job.pk = 2
        mock_db_job.status = JobStatusChoices.STATUS_RUNNING
        mock_db_job.completed = None

        class _DoesNotExist(Exception):
            pass

        mock_job_cls = MagicMock()
        mock_job_cls.DoesNotExist = _DoesNotExist
        mock_job_cls.objects.get.return_value = mock_db_job

        mock_rq_job = MagicMock()
        mock_rq_job.is_stopped = True
        mock_rq_job.is_failed = False
        mock_rq_job.get_status.return_value = "stopped"

        mock_rq_cls = MagicMock()
        mock_rq_cls.fetch.return_value = mock_rq_job

        mock_queue = MagicMock()
        mock_queue_fn = MagicMock(return_value=mock_queue)

        with patch("netbox_librenms_plugin.api.views.timezone") as mock_tz:
            mock_tz.now.return_value = "2024-01-01"
            response = _call_sync_job_status(
                job_pk=2,
                job_patch=mock_job_cls,
                rq_patch=mock_rq_cls,
                queue_patch=mock_queue_fn,
            )

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "updated"
        assert data["rq_status"] == "stopped"
        mock_db_job.save.assert_called_once_with(update_fields=["status", "completed"])
        assert mock_db_job.completed == "2024-01-01"

    def test_updates_db_when_rq_failed(self):
        from core.choices import JobStatusChoices

        mock_db_job = MagicMock()
        mock_db_job.pk = 3
        mock_db_job.status = JobStatusChoices.STATUS_RUNNING
        mock_db_job.completed = None

        class _DoesNotExist(Exception):
            pass

        mock_job_cls = MagicMock()
        mock_job_cls.DoesNotExist = _DoesNotExist
        mock_job_cls.objects.get.return_value = mock_db_job

        mock_rq_job = MagicMock()
        mock_rq_job.is_stopped = False
        mock_rq_job.is_failed = True
        mock_rq_job.get_status.return_value = "failed"

        mock_rq_cls = MagicMock()
        mock_rq_cls.fetch.return_value = mock_rq_job

        mock_queue = MagicMock()
        mock_queue_fn = MagicMock(return_value=mock_queue)

        with patch("netbox_librenms_plugin.api.views.timezone") as mock_tz:
            mock_tz.now.return_value = "2024-01-02"
            response = _call_sync_job_status(
                job_pk=3,
                job_patch=mock_job_cls,
                rq_patch=mock_rq_cls,
                queue_patch=mock_queue_fn,
            )

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "updated"
        assert data["rq_status"] == "failed"
        assert mock_db_job.completed == "2024-01-02"
        mock_db_job.save.assert_called_once_with(update_fields=["status", "completed"])

    def test_does_not_overwrite_existing_completed_timestamp(self):
        from core.choices import JobStatusChoices

        mock_db_job = MagicMock()
        mock_db_job.pk = 4
        mock_db_job.status = JobStatusChoices.STATUS_RUNNING
        mock_db_job.completed = "2024-01-01T10:00:00"  # already set

        class _DoesNotExist(Exception):
            pass

        mock_job_cls = MagicMock()
        mock_job_cls.DoesNotExist = _DoesNotExist
        mock_job_cls.objects.get.return_value = mock_db_job

        mock_rq_job = MagicMock()
        mock_rq_job.is_stopped = True
        mock_rq_job.is_failed = False
        mock_rq_job.get_status.return_value = "stopped"

        mock_rq_cls = MagicMock()
        mock_rq_cls.fetch.return_value = mock_rq_job

        mock_queue = MagicMock()
        mock_queue_fn = MagicMock(return_value=mock_queue)

        with patch("netbox_librenms_plugin.api.views.timezone") as mock_tz:
            response = _call_sync_job_status(
                job_pk=4,
                job_patch=mock_job_cls,
                rq_patch=mock_rq_cls,
                queue_patch=mock_queue_fn,
            )

        # timezone.now() should NOT have been called since completed is already set
        mock_tz.now.assert_not_called()
        assert response.status_code == 200
        from core.choices import JobStatusChoices

        assert mock_db_job.status == JobStatusChoices.STATUS_FAILED
        mock_db_job.save.assert_called_once_with(update_fields=["status", "completed"])


class TestSyncJobStatusRQJobNotInQueue:
    """Test sync_job_status when RQ.fetch raises NoSuchJobError."""

    def test_updates_db_to_failed_when_running_and_not_in_rq(self):
        from core.choices import JobStatusChoices
        from rq.exceptions import NoSuchJobError

        mock_db_job = MagicMock()
        mock_db_job.pk = 5
        mock_db_job.status = JobStatusChoices.STATUS_RUNNING
        mock_db_job.completed = None

        class _DoesNotExist(Exception):
            pass

        mock_job_cls = MagicMock()
        mock_job_cls.DoesNotExist = _DoesNotExist
        mock_job_cls.objects.get.return_value = mock_db_job

        mock_rq_cls = MagicMock()
        mock_rq_cls.fetch.side_effect = NoSuchJobError("not found in redis")

        mock_queue = MagicMock()
        mock_queue_fn = MagicMock(return_value=mock_queue)

        with patch("netbox_librenms_plugin.api.views.timezone") as mock_tz:
            mock_tz.now.return_value = "2024-01-03"
            response = _call_sync_job_status(
                job_pk=5,
                job_patch=mock_job_cls,
                rq_patch=mock_rq_cls,
                queue_patch=mock_queue_fn,
            )

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "updated"
        assert data["rq_status"] == "not_found"
        mock_db_job.save.assert_called_once_with(update_fields=["status", "completed"])

    def test_no_change_when_not_running_and_not_in_rq(self):
        from core.choices import JobStatusChoices
        from rq.exceptions import NoSuchJobError

        mock_db_job = MagicMock()
        mock_db_job.pk = 6
        mock_db_job.status = JobStatusChoices.STATUS_COMPLETED
        mock_db_job.completed = "2024-01-01"

        class _DoesNotExist(Exception):
            pass

        mock_job_cls = MagicMock()
        mock_job_cls.DoesNotExist = _DoesNotExist
        mock_job_cls.objects.get.return_value = mock_db_job

        mock_rq_cls = MagicMock()
        mock_rq_cls.fetch.side_effect = NoSuchJobError("not found in redis")

        mock_queue = MagicMock()
        mock_queue_fn = MagicMock(return_value=mock_queue)

        response = _call_sync_job_status(
            job_pk=6,
            job_patch=mock_job_cls,
            rq_patch=mock_rq_cls,
            queue_patch=mock_queue_fn,
        )

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "no_change"
        assert data["rq_status"] == "not_found"
        mock_db_job.save.assert_not_called()

    def test_does_not_overwrite_completed_when_not_in_rq(self):
        from core.choices import JobStatusChoices
        from rq.exceptions import NoSuchJobError

        mock_db_job = MagicMock()
        mock_db_job.pk = 7
        mock_db_job.status = JobStatusChoices.STATUS_RUNNING
        mock_db_job.completed = "2024-01-01T08:00:00"  # already set

        class _DoesNotExist(Exception):
            pass

        mock_job_cls = MagicMock()
        mock_job_cls.DoesNotExist = _DoesNotExist
        mock_job_cls.objects.get.return_value = mock_db_job

        from rq.exceptions import NoSuchJobError

        mock_rq_cls = MagicMock()
        mock_rq_cls.fetch.side_effect = NoSuchJobError("gone")

        mock_queue = MagicMock()
        mock_queue_fn = MagicMock(return_value=mock_queue)

        with patch("netbox_librenms_plugin.api.views.timezone") as mock_tz:
            response = _call_sync_job_status(
                job_pk=7,
                job_patch=mock_job_cls,
                rq_patch=mock_rq_cls,
                queue_patch=mock_queue_fn,
            )

        mock_tz.now.assert_not_called()
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "updated"
        assert data["rq_status"] == "not_found"
        # Verify the DB job was marked failed and persisted
        assert mock_db_job.status == JobStatusChoices.STATUS_FAILED
        mock_db_job.save.assert_called_once()
        # completed must NOT be overwritten — it was already set
        assert mock_db_job.completed == "2024-01-01T08:00:00"


# ===========================================================================
# api/views.py – InterfaceTypeMappingViewSet (class attributes)
# ===========================================================================


class TestInterfaceTypeMappingViewSet:
    """Test that InterfaceTypeMappingViewSet has expected class-level attributes."""

    def test_viewset_has_correct_permission_classes(self):
        from netbox_librenms_plugin.api.views import InterfaceTypeMappingViewSet, LibreNMSPluginPermission

        assert LibreNMSPluginPermission in InterfaceTypeMappingViewSet.permission_classes

    def test_viewset_has_serializer_class(self):
        from netbox_librenms_plugin.api.views import InterfaceTypeMappingViewSet
        from netbox_librenms_plugin.api.serializers import InterfaceTypeMappingSerializer

        assert InterfaceTypeMappingViewSet.serializer_class is InterfaceTypeMappingSerializer


# ===========================================================================
# filtersets.py – SiteLocationFilterSet
# ===========================================================================


class TestSiteLocationFilterSet:
    """Tests for SiteLocationFilterSet."""

    def test_qs_returns_full_queryset_when_no_q(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        mock_item = MagicMock()
        queryset = [mock_item]
        fs = SiteLocationFilterSet(data={}, queryset=queryset)
        assert fs.qs == queryset

    def test_qs_filters_when_q_provided(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        matching_item = MagicMock()
        matching_item.netbox_site.name = "Amsterdam"
        matching_item.netbox_site.latitude = "52.37"
        matching_item.netbox_site.longitude = "4.89"
        matching_item.librenms_location = "AMS-DC1"

        non_matching_item = MagicMock()
        non_matching_item.netbox_site.name = "London"
        non_matching_item.netbox_site.latitude = "51.5"
        non_matching_item.netbox_site.longitude = "-0.12"
        non_matching_item.librenms_location = "LON-DC1"

        fs = SiteLocationFilterSet(data={"q": "amsterdam"}, queryset=[matching_item, non_matching_item])
        result = fs.qs
        assert len(result) == 1
        assert result[0] is matching_item

    def test_qs_empty_q_returns_all(self):
        """Empty string for q is falsy – should return the full queryset."""
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        items = [MagicMock(), MagicMock()]
        fs = SiteLocationFilterSet(data={"q": ""}, queryset=items)
        assert fs.qs == items

    def test_matches_by_site_name(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        item = MagicMock()
        item.netbox_site.name = "TestSite"
        item.netbox_site.latitude = "0"
        item.netbox_site.longitude = "0"
        item.librenms_location = None

        fs = SiteLocationFilterSet(data={"q": "testsite"}, queryset=[item])
        assert fs.qs == [item]

    def test_matches_by_latitude(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        item = MagicMock()
        item.netbox_site.name = "Nowhere"
        item.netbox_site.latitude = "48.8566"
        item.netbox_site.longitude = "0.0"
        item.librenms_location = None

        fs = SiteLocationFilterSet(data={"q": "48.8566"}, queryset=[item])
        assert fs.qs == [item]

    def test_matches_by_librenms_location(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        item = MagicMock()
        item.netbox_site.name = "X"
        item.netbox_site.latitude = "0"
        item.netbox_site.longitude = "0"
        item.librenms_location = "Paris-DC"

        fs = SiteLocationFilterSet(data={"q": "paris"}, queryset=[item])
        assert fs.qs == [item]

    def test_no_match_returns_empty(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        item = MagicMock()
        item.netbox_site.name = "Tokyo"
        item.netbox_site.latitude = "35.0"
        item.netbox_site.longitude = "139.0"
        item.librenms_location = "TKY-1"

        fs = SiteLocationFilterSet(data={"q": "berlin"}, queryset=[item])
        assert fs.qs == []

    def test_librenms_location_none_does_not_raise(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        item = MagicMock()
        item.netbox_site.name = "NoLocation"
        item.netbox_site.latitude = "10"
        item.netbox_site.longitude = "20"
        item.librenms_location = None

        fs = SiteLocationFilterSet(data={"q": "nolocation"}, queryset=[item])
        # Should not raise, librenms_location treated as empty string
        result = fs.qs
        assert len(result) == 1

    def test_form_property_returns_bound_form(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet
        from django import forms

        fs = SiteLocationFilterSet(data={"q": "test"}, queryset=[])
        form = fs.form
        assert isinstance(form, forms.Form)
        assert form.is_bound
        assert "q" in form.fields

    def test_form_property_returns_unbound_form_when_no_data(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet
        from django import forms

        fs = SiteLocationFilterSet(data=None, queryset=[])
        form = fs.form
        assert isinstance(form, forms.Form)
        assert not form.is_bound


# ===========================================================================
# filtersets.py – DeviceStatusFilterSet.search()
# ===========================================================================


class TestDeviceStatusFilterSetSearch:
    """Tests for DeviceStatusFilterSet.search()."""

    def test_search_empty_value_returns_queryset_unchanged(self):
        from netbox_librenms_plugin.filtersets import DeviceStatusFilterSet

        fs = object.__new__(DeviceStatusFilterSet)
        mock_qs = MagicMock()
        result = fs.search(mock_qs, "name", "   ")
        assert result is mock_qs
        mock_qs.filter.assert_not_called()

    def test_search_with_value_calls_filter(self):
        from netbox_librenms_plugin.filtersets import DeviceStatusFilterSet

        fs = object.__new__(DeviceStatusFilterSet)
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs

        result = fs.search(mock_qs, "name", "router01")
        mock_qs.filter.assert_called_once()
        assert result is mock_qs

    def test_search_builds_q_filter_for_name(self):
        from netbox_librenms_plugin.filtersets import DeviceStatusFilterSet

        fs = object.__new__(DeviceStatusFilterSet)
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs

        fs.search(mock_qs, "name", "router")

        call_args = mock_qs.filter.call_args
        assert call_args is not None
        q_obj = call_args[0][0]
        q_str = str(q_obj)
        assert "name__icontains" in q_str
        assert "site__name__icontains" in q_str
        assert "device_type__model__icontains" in q_str
        assert "role__name__icontains" in q_str
        assert "rack__name__icontains" in q_str

    def test_search_whitespace_only_returns_qs(self):
        from netbox_librenms_plugin.filtersets import DeviceStatusFilterSet

        fs = object.__new__(DeviceStatusFilterSet)
        mock_qs = MagicMock()
        result = fs.search(mock_qs, "q", "\t\n")
        assert result is mock_qs


# ===========================================================================
# filtersets.py – VMStatusFilterSet.search()
# ===========================================================================


class TestVMStatusFilterSetSearch:
    """Tests for VMStatusFilterSet.search()."""

    def test_search_empty_value_returns_queryset_unchanged(self):
        from netbox_librenms_plugin.filtersets import VMStatusFilterSet

        fs = object.__new__(VMStatusFilterSet)
        mock_qs = MagicMock()
        result = fs.search(mock_qs, "name", "")
        assert result is mock_qs
        mock_qs.filter.assert_not_called()

    def test_search_with_value_calls_filter(self):
        from netbox_librenms_plugin.filtersets import VMStatusFilterSet

        fs = object.__new__(VMStatusFilterSet)
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs

        result = fs.search(mock_qs, "name", "vm-prod-01")
        mock_qs.filter.assert_called_once()
        assert result is mock_qs

    def test_search_builds_filter_with_name_site_cluster_platform(self):
        from netbox_librenms_plugin.filtersets import VMStatusFilterSet

        fs = object.__new__(VMStatusFilterSet)
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs

        fs.search(mock_qs, "q", "production")

        call_args = mock_qs.filter.call_args
        assert call_args is not None
        q_obj = call_args[0][0]
        q_str = str(q_obj)
        assert "name__icontains" in q_str
        assert "site__name__icontains" in q_str
        assert "cluster__name__icontains" in q_str
        assert "platform__name__icontains" in q_str

    def test_search_whitespace_only_returns_qs(self):
        from netbox_librenms_plugin.filtersets import VMStatusFilterSet

        fs = object.__new__(VMStatusFilterSet)
        mock_qs = MagicMock()
        result = fs.search(mock_qs, "q", "   ")
        assert result is mock_qs


# ===========================================================================
# models.py – missing lines 45, 48, 68, 76
# ===========================================================================


class TestLibreNMSSettingsModel:
    """Tests for LibreNMSSettings model methods (lines 45, 48)."""

    def test_get_absolute_url_calls_reverse(self):
        """Line 45: get_absolute_url() returns the settings page URL."""
        from netbox_librenms_plugin.models import LibreNMSSettings

        instance = object.__new__(LibreNMSSettings)
        instance.selected_server = "default"

        with patch("netbox_librenms_plugin.models.reverse") as mock_reverse:
            mock_reverse.return_value = "/plugins/librenms/settings/"
            url = instance.get_absolute_url()

        mock_reverse.assert_called_once_with("plugins:netbox_librenms_plugin:settings")
        assert url == "/plugins/librenms/settings/"

    def test_str_returns_formatted_string(self):
        """Line 48: __str__() includes selected_server name."""
        from netbox_librenms_plugin.models import LibreNMSSettings

        instance = object.__new__(LibreNMSSettings)
        instance.selected_server = "my_server"

        result = str(instance)
        assert result == "LibreNMS Settings - Server: my_server"

    def test_str_with_default_server(self):
        """__str__() works with 'default' server."""
        from netbox_librenms_plugin.models import LibreNMSSettings

        instance = object.__new__(LibreNMSSettings)
        instance.selected_server = "default"

        assert str(instance) == "LibreNMS Settings - Server: default"


class TestInterfaceTypeMappingModel:
    """Tests for InterfaceTypeMapping model methods (lines 68, 76)."""

    def test_get_absolute_url_calls_reverse_with_pk(self):
        """Line 68: get_absolute_url() passes self.pk to reverse."""
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        instance = object.__new__(InterfaceTypeMapping)
        instance.pk = 42

        with patch("netbox_librenms_plugin.models.reverse") as mock_reverse:
            mock_reverse.return_value = "/plugins/librenms/mappings/42/"
            url = instance.get_absolute_url()

        mock_reverse.assert_called_once_with(
            "plugins:netbox_librenms_plugin:interfacetypemapping_detail",
            args=[42],
        )
        assert url == "/plugins/librenms/mappings/42/"

    def test_str_returns_type_speed_netbox_type(self):
        """Line 76: __str__() formats librenms_type + librenms_speed -> netbox_type."""
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        instance = object.__new__(InterfaceTypeMapping)
        instance.librenms_type = "ethernet"
        instance.librenms_speed = 1000000
        instance.netbox_type = "1000base-t"

        result = str(instance)
        assert result == "ethernet + 1000000 -> 1000base-t"

    def test_str_with_none_speed(self):
        """__str__() works when librenms_speed is None."""
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        instance = object.__new__(InterfaceTypeMapping)
        instance.librenms_type = "fiber"
        instance.librenms_speed = None
        instance.netbox_type = "other"

        result = str(instance)
        assert result == "fiber + None -> other"

    def test_get_absolute_url_with_different_pk(self):
        """get_absolute_url() works for any pk value."""
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        instance = object.__new__(InterfaceTypeMapping)
        instance.pk = 1

        with patch("netbox_librenms_plugin.models.reverse") as mock_reverse:
            mock_reverse.return_value = "/plugins/librenms/mappings/1/"
            url = instance.get_absolute_url()

        assert url == "/plugins/librenms/mappings/1/"
