import logging

from core.choices import JobStatusChoices
from core.models import Job
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django_rq import get_queue
from netbox.api.viewsets import NetBoxModelViewSet
from rq.job import Job as RQJob

from netbox_librenms_plugin.models import InterfaceTypeMapping

from .serializers import InterfaceTypeMappingSerializer

logger = logging.getLogger(__name__)


class InterfaceTypeMappingViewSet(NetBoxModelViewSet):
    queryset = InterfaceTypeMapping.objects.all()
    serializer_class = InterfaceTypeMappingSerializer


@require_http_methods(["POST"])
def sync_job_status(request, job_pk):
    """
    Sync database Job status with RQ job status.

    This is needed because NetBox's worker doesn't always update the database
    when a job is stopped before it starts processing.

    Args:
        request: Django request
        job_pk: Primary key of the Job to sync

    Returns:
        JsonResponse with updated status
    """
    try:
        job = Job.objects.get(pk=job_pk)
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
            logger.info(
                f"Synced Job #{job.pk}: DB status updated to failed (RQ: {rq_status})"
            )
            return JsonResponse(
                {"status": "updated", "db_status": job.status, "rq_status": rq_status}
            )
        else:
            # Job still active in RQ
            return JsonResponse(
                {"status": "no_change", "db_status": job.status, "rq_status": rq_status}
            )
    except Exception as e:
        # Job not in RQ queue - mark as failed
        logger.warning(f"Job #{job.pk} not found in RQ: {e}")
        if job.status == JobStatusChoices.STATUS_RUNNING:
            job.status = JobStatusChoices.STATUS_FAILED
            if not job.completed:
                job.completed = timezone.now()
            job.save(update_fields=["status", "completed"])
            return JsonResponse(
                {"status": "updated", "db_status": job.status, "rq_status": "not_found"}
            )
        return JsonResponse(
            {"status": "no_change", "db_status": job.status, "rq_status": "not_found"}
        )
