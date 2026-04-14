import logging

from core.choices import JobStatusChoices
from core.models import Job
from django.http import JsonResponse
from django.utils import timezone
from django_rq import get_queue
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import BasePermission, SAFE_METHODS
from rq.exceptions import NoSuchJobError
from rq.job import Job as RQJob

from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN, PERM_VIEW_PLUGIN
from netbox_librenms_plugin.filters import InterfaceTypeMappingFilterSet
from netbox_librenms_plugin.jobs import FilterDevicesJob, ImportDevicesJob
from netbox_librenms_plugin.models import InterfaceTypeMapping

from .serializers import InterfaceTypeMappingSerializer

logger = logging.getLogger(__name__)


class LibreNMSPluginPermission(BasePermission):
    """
    Permission class for LibreNMS plugin API endpoints.

    - Safe requests (GET, HEAD, OPTIONS) require netbox_librenms_plugin.view_librenmssettings
    - All other requests require netbox_librenms_plugin.change_librenmssettings
    """

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return request.user.has_perm(PERM_VIEW_PLUGIN)
        return request.user.has_perm(PERM_CHANGE_PLUGIN)


class InterfaceTypeMappingViewSet(NetBoxModelViewSet):
    """API viewset for InterfaceTypeMapping CRUD operations."""

    permission_classes = [LibreNMSPluginPermission]
    filterset_class = InterfaceTypeMappingFilterSet

    queryset = InterfaceTypeMapping.objects.all()
    serializer_class = InterfaceTypeMappingSerializer


@api_view(["POST"])
@permission_classes([LibreNMSPluginPermission])
def sync_job_status(request, job_pk):
    """
    Sync database Job status with RQ job status.

    This is needed because NetBox's worker doesn't always update the database
    when a job is stopped before it starts processing.

    Only allows users to sync their own LibreNMS jobs.

    Args:
        request: Django request
        job_pk: Primary key of the Job to sync

    Returns:
        JsonResponse with updated status
    """
    _LIBRENMS_JOB_NAMES = (FilterDevicesJob.Meta.name, ImportDevicesJob.Meta.name)
    try:
        job = Job.objects.get(pk=job_pk, user=request.user, name__in=_LIBRENMS_JOB_NAMES)
    except Job.DoesNotExist:
        return JsonResponse({"error": "Job not found"}, status=404)

    # Get RQ job status
    queue = get_queue("default")
    try:
        rq_job = RQJob.fetch(str(job.job_id), connection=queue.connection)
        rq_status = rq_job.get_status()

        # If RQ job is stopped or failed, update database
        if rq_job.is_stopped or rq_job.is_failed:
            job.status = JobStatusChoices.STATUS_FAILED
            if not job.completed:
                job.completed = timezone.now()
            job.save(update_fields=["status", "completed"])
            logger.info("Synced Job #%s: DB status updated to failed (RQ: %s)", job.pk, rq_status)
            return JsonResponse({"status": "updated", "db_status": job.status, "rq_status": rq_status})
        else:
            # Job still active in RQ
            return JsonResponse({"status": "no_change", "db_status": job.status, "rq_status": rq_status})
    except NoSuchJobError:
        # Job not in RQ queue — mark any non-terminal DB job as failed
        logger.warning("Job #%s not found in RQ (NoSuchJobError)", job.pk)
        terminal_states = {
            JobStatusChoices.STATUS_COMPLETED,
            JobStatusChoices.STATUS_FAILED,
            JobStatusChoices.STATUS_ERRORED,
        }
        if job.status not in terminal_states:
            job.status = JobStatusChoices.STATUS_FAILED
            if not job.completed:
                job.completed = timezone.now()
            job.save(update_fields=["status", "completed"])
            return JsonResponse({"status": "updated", "db_status": job.status, "rq_status": "not_found"})
        return JsonResponse({"status": "no_change", "db_status": job.status, "rq_status": "not_found"})
    except Exception as e:
        logger.exception("Unexpected error fetching RQ job for Job #%s: %s", job.pk, e)
        return JsonResponse({"error": "Failed to fetch RQ job status"}, status=500)
