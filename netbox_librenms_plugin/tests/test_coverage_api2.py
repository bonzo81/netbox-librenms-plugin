"""Integration coverage for API job status, filtersets, and model contracts."""

import operator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser, make_vm


pytestmark = pytest.mark.django_db


@pytest.fixture
def real_rq_job():
    from django_rq import get_queue
    from rq.job import Job, JobStatus

    jobs = []

    def _create(status=JobStatus.QUEUED):
        queue = get_queue("default")
        job = Job.create(
            operator.add,
            args=(1, 2),
            connection=queue.connection,
            id=str(uuid4()),
        )
        job.set_status(status)
        job.save()
        jobs.append(job)
        return job

    yield _create

    for job in jobs:
        job.delete()


def _database_job(user, job_id, status):
    from core.models import Job, ObjectType

    from netbox_librenms_plugin.jobs import FilterDevicesJob

    return Job.objects.create(
        object_type=ObjectType.objects.get_for_model(Job),
        name=FilterDevicesJob.Meta.name,
        user=user,
        status=status,
        job_id=job_id,
    )


def _sync_status_url(job_pk):
    return reverse("plugins-api:netbox_librenms_plugin-api:sync_job_status", args=[job_pk])


class TestSyncJobStatus:
    def test_missing_job_returns_404(self, client):
        user = make_superuser("job-status-missing")
        client.force_login(user)

        response = client.post(_sync_status_url(999_999))

        assert response.status_code == 404
        assert response.json() == {"error": "Job not found"}

    def test_foreign_job_returns_404(self, client):
        from core.choices import JobStatusChoices
        from django.contrib.auth import get_user_model

        owner = get_user_model().objects.create_user(username="job-status-owner")
        requester = make_superuser("job-status-foreign")
        database_job = _database_job(owner, uuid4(), JobStatusChoices.STATUS_PENDING)
        client.force_login(requester)

        response = client.post(_sync_status_url(database_job.pk))

        assert response.status_code == 404
        assert response.json() == {"error": "Job not found"}

    def test_live_queued_job_keeps_database_status(self, client, real_rq_job):
        from core.choices import JobStatusChoices

        user = make_superuser("job-status-queued")
        rq_job = real_rq_job()
        database_job = _database_job(user, rq_job.id, JobStatusChoices.STATUS_RUNNING)
        client.force_login(user)

        response = client.post(_sync_status_url(database_job.pk))

        database_job.refresh_from_db()
        assert response.status_code == 200
        assert response.json() == {
            "status": "no_change",
            "db_status": JobStatusChoices.STATUS_RUNNING,
            "rq_status": "queued",
        }
        assert database_job.completed is None

    @pytest.mark.parametrize("rq_status", ["stopped", "failed"])
    def test_terminal_rq_job_marks_running_database_job_failed(self, client, real_rq_job, rq_status):
        from core.choices import JobStatusChoices
        from django.utils import timezone
        from rq.job import JobStatus

        user = make_superuser(f"job-status-{rq_status}")
        rq_job = real_rq_job(JobStatus(rq_status))
        database_job = _database_job(user, rq_job.id, JobStatusChoices.STATUS_RUNNING)
        client.force_login(user)
        started = timezone.now()

        response = client.post(_sync_status_url(database_job.pk))

        database_job.refresh_from_db()
        assert response.status_code == 200
        assert response.json()["status"] == "updated"
        assert response.json()["rq_status"] == rq_status
        assert database_job.status == JobStatusChoices.STATUS_FAILED
        assert database_job.completed is not None
        assert database_job.completed >= started
        assert timezone.is_aware(database_job.completed)

    def test_terminal_database_job_is_not_overwritten(self, client, real_rq_job):
        from core.choices import JobStatusChoices
        from django.utils import timezone
        from rq.job import JobStatus

        user = make_superuser("job-status-complete")
        rq_job = real_rq_job(JobStatus.STOPPED)
        database_job = _database_job(user, rq_job.id, JobStatusChoices.STATUS_COMPLETED)
        completed = timezone.now()
        database_job.completed = completed
        database_job.save(update_fields=["completed"])
        client.force_login(user)

        response = client.post(_sync_status_url(database_job.pk))

        database_job.refresh_from_db()
        assert response.json()["status"] == "no_change"
        assert database_job.status == JobStatusChoices.STATUS_COMPLETED
        assert database_job.completed == completed

    def test_missing_rq_job_marks_pending_database_job_failed(self, client):
        from core.choices import JobStatusChoices

        user = make_superuser("job-status-rq-missing")
        database_job = _database_job(user, uuid4(), JobStatusChoices.STATUS_PENDING)
        client.force_login(user)

        response = client.post(_sync_status_url(database_job.pk))

        database_job.refresh_from_db()
        assert response.json()["status"] == "updated"
        assert response.json()["rq_status"] == "not_found"
        assert database_job.status == JobStatusChoices.STATUS_FAILED
        assert database_job.completed is not None


class TestInterfaceTypeMappingViewSet:
    def test_viewset_uses_the_plugin_permission_and_serializer(self):
        from netbox_librenms_plugin.api.serializers import InterfaceTypeMappingSerializer
        from netbox_librenms_plugin.api.views import InterfaceTypeMappingViewSet, LibreNMSPluginPermission

        assert InterfaceTypeMappingViewSet.permission_classes == [LibreNMSPluginPermission]
        assert InterfaceTypeMappingViewSet.serializer_class is InterfaceTypeMappingSerializer


class TestSiteLocationFilterSet:
    @staticmethod
    def _rows():
        return [
            SimpleNamespace(
                netbox_site=SimpleNamespace(name="Amsterdam", latitude="52.37", longitude="4.89"),
                librenms_location="AMS-Lab",
            ),
            SimpleNamespace(
                netbox_site=SimpleNamespace(name="London", latitude="51.50", longitude="-0.12"),
                librenms_location=None,
            ),
        ]

    @pytest.mark.parametrize(
        ("query", "expected_name"),
        [
            ("amsterdam", "Amsterdam"),
            ("52.37", "Amsterdam"),
            ("ams-lab", "Amsterdam"),
            ("london", "London"),
        ],
    )
    def test_searches_each_displayed_field(self, query, expected_name):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        result = SiteLocationFilterSet(data={"q": query}, queryset=self._rows()).qs

        assert [row.netbox_site.name for row in result] == [expected_name]

    def test_empty_or_missing_query_returns_every_row(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        rows = self._rows()

        assert SiteLocationFilterSet(data={}, queryset=rows).qs == rows
        assert SiteLocationFilterSet(data={"q": ""}, queryset=rows).qs == rows

    def test_nonmatching_query_returns_empty(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        assert SiteLocationFilterSet(data={"q": "no-match"}, queryset=self._rows()).qs == []

    def test_form_binding_matches_input_presence(self):
        from netbox_librenms_plugin.filtersets import SiteLocationFilterSet

        assert SiteLocationFilterSet(data={"q": "test"}, queryset=[]).form.is_bound is True
        assert SiteLocationFilterSet(data=None, queryset=[]).form.is_bound is False


class TestDeviceStatusFilterSet:
    @pytest.mark.parametrize("query", ["search-device", "testsite", "testdt", "testrole"])
    def test_search_runs_against_the_real_device_queryset(self, query):
        from dcim.models import Device

        from netbox_librenms_plugin.filtersets import DeviceStatusFilterSet

        matching = make_device("search-device")
        make_device("unrelated-device")
        filterset = object.__new__(DeviceStatusFilterSet)

        result = filterset.search(Device.objects.all(), "q", query)

        assert matching in result

    def test_whitespace_search_returns_the_original_queryset(self):
        from dcim.models import Device

        from netbox_librenms_plugin.filtersets import DeviceStatusFilterSet

        queryset = Device.objects.all()

        assert object.__new__(DeviceStatusFilterSet).search(queryset, "q", " \t") is queryset


class TestVMStatusFilterSet:
    def test_search_runs_against_real_vm_and_cluster_rows(self):
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.filtersets import VMStatusFilterSet

        matching = make_vm("search-vm")
        make_vm("unrelated-vm")
        filterset = object.__new__(VMStatusFilterSet)

        assert matching in filterset.search(VirtualMachine.objects.all(), "q", "search-vm")
        assert matching in filterset.search(VirtualMachine.objects.all(), "q", "testcluster")

    def test_whitespace_search_returns_the_original_queryset(self):
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.filtersets import VMStatusFilterSet

        queryset = VirtualMachine.objects.all()

        assert object.__new__(VMStatusFilterSet).search(queryset, "q", "\n") is queryset


class TestLibreNMSSettingsModel:
    def test_real_model_url_and_display(self):
        from netbox_librenms_plugin.models import LibreNMSSettings

        settings_row, _ = LibreNMSSettings.objects.update_or_create(
            pk=1,
            defaults={"selected_server": "integration"},
        )

        assert settings_row.get_absolute_url() == reverse("plugins:netbox_librenms_plugin:settings")
        assert str(settings_row) == "LibreNMS Settings - Server: integration"


class TestInterfaceTypeMappingModel:
    @pytest.mark.parametrize("speed", [1_000_000, None])
    def test_real_model_url_and_display(self, speed):
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        mapping = InterfaceTypeMapping.objects.create(
            librenms_type=f"integration-{speed}",
            librenms_speed=speed,
            netbox_type="other",
        )

        assert mapping.get_absolute_url() == reverse(
            "plugins:netbox_librenms_plugin:interfacetypemapping_detail",
            args=[mapping.pk],
        )
        assert str(mapping) == f"integration-{speed} + {speed} -> other"
