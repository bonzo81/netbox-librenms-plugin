import logging

from dcim.models import Device
from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from netbox.views import generic
from utilities.rqworker import get_workers_for_queue

from netbox_librenms_plugin.forms import LibreNMSImportFilterForm
from netbox_librenms_plugin.import_utils import (
    get_active_cached_searches,
    process_device_filters,
)
from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tables.device_status import DeviceImportTable
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

logger = logging.getLogger(__name__)


class LibreNMSImportView(LibreNMSAPIMixin, generic.ObjectListView):
    """Import devices from LibreNMS into NetBox with validation metadata."""

    queryset = Device.objects.none()
    table = DeviceImportTable
    filterset = None
    filterset_form = LibreNMSImportFilterForm
    template_name = "netbox_librenms_plugin/librenms_import.html"
    actions = {}
    title = "Import Devices from LibreNMS"

    def get_required_permission(self):
        from utilities.permissions import get_permission_for_model

        return get_permission_for_model(Device, "view")

    def should_use_background_job(self):
        """
        Determine if filter operation should run as background job.

        Background jobs provide active cancellation and keep the browser responsive
        during long-running operations.

        The main benefits of background jobs are:
        - Active cancellation capability
        - Browser responsiveness (no "page loading" hang)
        - Job tracking in NetBox Jobs interface
        - Results cached for later retrieval

        Returns:
            bool: True if background job should be used, False for synchronous
        """
        return self._filter_form_data.get("use_background_job", True)

    def _load_job_results(self, job_id):
        """
        Load cached results from a completed background job.

        Args:
            job_id: ID of the completed FilterDevicesJob

        Returns:
            List[dict]: Validated devices from job cache, or [] if cache expired
        """
        from core.models import Job

        try:
            job = Job.objects.get(pk=job_id)
        except Job.DoesNotExist:
            logger.warning(f"Job {job_id} not found")
            return []

        if job.status != "completed":
            logger.warning(f"Job {job_id} status is {job.status}, not completed")
            return []

        # Load cached devices from job using shared cache keys
        from netbox_librenms_plugin.import_utils import get_validated_device_cache_key

        job_data = job.data or {}
        device_ids = job_data.get("device_ids", [])
        filters = job_data.get("filters", {})
        server_key = job_data.get("server_key", "default")
        vc_enabled = job_data.get("vc_detection_enabled", False)

        # Extract cache metadata for frontend warnings
        self._cache_timestamp = job_data.get("cached_at")
        self._cache_timeout = job_data.get("cache_timeout", 300)

        if not device_ids:
            logger.warning(f"Job {job_id} missing device_ids")
            return []

        # Fetch devices from cache using shared keys
        validated_devices = []
        for device_id in device_ids:
            cache_key = get_validated_device_cache_key(
                server_key=server_key,
                filters=filters,
                device_id=device_id,
                vc_enabled=vc_enabled,
            )
            device = cache.get(cache_key)
            if device:
                validated_devices.append(device)
            else:
                logger.warning(
                    f"Device {device_id} from job {job_id} not in cache (may have expired)"
                )

        if not validated_devices and device_ids:
            logger.error(
                f"Job {job_id} cache expired. Processed {len(device_ids)} devices but none in cache."
            )

        return validated_devices

    def get(self, request, *args, **kwargs):  # noqa: D401 - inherited doc
        """Render the import table backed by LibreNMS data."""
        self._filter_warning = None
        self._filter_form_data = {}
        self._libre_filters = {}
        self._cache_cleared = False
        self._request = request  # Store request for connection checks
        self._job_results_loaded = False
        self._from_cache = False
        self._cache_timestamp = None
        self._cache_timeout = 300
        self._cache_metadata_missing = False

        # Determine if new filters are being submitted
        libre_filter_fields = (
            "librenms_location",
            "librenms_type",
            "librenms_os",
            "librenms_hostname",
            "librenms_sysname",
            "librenms_hardware",
        )
        filters_present = any(request.GET.get(field) for field in libre_filter_fields)
        filters_submitted = request.GET.get("apply_filters") or filters_present

        # Check if loading results from completed background job
        # Only load job results if NOT submitting new filters
        job_id = request.GET.get("job_id")
        if job_id and not filters_submitted:
            try:
                job_id = int(job_id)
                logger.info(f"Loading results from job {job_id}")
                validated_devices = self._load_job_results(job_id)
                if validated_devices:
                    self._import_data = validated_devices
                    self._job_results_loaded = True
                    # Job results are cached data, so mark as from_cache
                    self._from_cache = True
                    # Extract filter info from first device's cache or job data
                    # This allows the filter form to show what was searched
                else:
                    messages.warning(
                        request,
                        "Job results have expired. Please re-apply your filters.",
                    )
            except (ValueError, TypeError):
                logger.warning(f"Invalid job_id parameter: {request.GET.get('job_id')}")

        raw_enable_flag = request.GET.get("enable_vc_detection")
        legacy_skip_flag = request.GET.get("skip_vc_detection")
        truthy_values = {"1", "true", "on", "True"}

        if raw_enable_flag is not None:
            self._vc_detection_enabled = raw_enable_flag in truthy_values
        elif legacy_skip_flag is not None:
            legacy_skip = legacy_skip_flag in truthy_values
            self._vc_detection_enabled = not legacy_skip
        else:
            self._vc_detection_enabled = False

        filter_form = self.filterset_form(request.GET) if self.filterset_form else None
        form_valid = False  # Track form validity

        if filter_form:
            form_valid = filter_form.is_valid()

            if form_valid:
                self._filter_form_data = filter_form.cleaned_data
                self._vc_detection_enabled = self._filter_form_data.get(
                    "enable_vc_detection"
                )
                self._cache_cleared = self._filter_form_data.get("clear_cache")
            elif filters_submitted:
                non_field_errors = filter_form.non_field_errors()
                if non_field_errors:
                    self._filter_warning = non_field_errors[0]

        self._filters_submitted = filters_submitted

        # Check if this should be processed as a background job
        # Skip if we're loading results from a completed job (job_id in URL)
        # IMPORTANT: Only process if form is valid (filter requirement enforced)
        if (
            filters_submitted
            and form_valid
            and not self._job_results_loaded
            and not request.GET.get("job_id")
        ):
            # Build filter dict
            libre_filters = {}

            if location := request.GET.get("librenms_location"):
                libre_filters["location"] = location
            if device_type := request.GET.get("librenms_type"):
                libre_filters["type"] = device_type
            if os := request.GET.get("librenms_os"):
                libre_filters["os"] = os
            if hostname := request.GET.get("librenms_hostname"):
                libre_filters["hostname"] = hostname
            if sysname := request.GET.get("librenms_sysname"):
                libre_filters["sysname"] = sysname
            if hardware := request.GET.get("librenms_hardware"):
                libre_filters["hardware"] = hardware

            # Check if data is already cached before deciding on background job
            # This prevents creating a job when cached results are available
            from netbox_librenms_plugin.import_utils import (
                get_device_count_for_filters,
                get_librenms_devices_for_import,
            )

            # Quick check: are the raw devices already cached?
            devices_cached = False
            if not self._cache_cleared:
                try:
                    _, devices_from_cache = get_librenms_devices_for_import(
                        api=self.librenms_api,
                        filters=libre_filters,
                        force_refresh=False,
                        return_cache_status=True,
                    )
                    devices_cached = devices_from_cache
                except Exception:
                    # Cache check failed; proceed with background job decision based on device_count
                    pass

            # Get device count for background job decision
            try:
                device_count = get_device_count_for_filters(
                    api=self.librenms_api,
                    filters=libre_filters,
                    clear_cache=self._cache_cleared,
                    show_disabled=bool(
                        self._filter_form_data.get("show_disabled", False)
                    ),
                )
            except Exception as e:
                logger.error(f"Error getting device count: {e}")
                device_count = 0

            # Load settings for background job decision
            settings = None
            # Decide whether to use background job
            # Skip background job if data is already cached
            if not devices_cached and self.should_use_background_job():
                # Check if RQ workers are available
                if get_workers_for_queue("default") > 0:
                    from netbox_librenms_plugin.jobs import FilterDevicesJob

                    # Enqueue background job
                    job = FilterDevicesJob.enqueue(
                        user=request.user,
                        filters=libre_filters,
                        vc_detection_enabled=self._vc_detection_enabled,
                        clear_cache=self._cache_cleared,
                        show_disabled=bool(self._filter_form_data.get("show_disabled")),
                        exclude_existing=bool(
                            self._filter_form_data.get("exclude_existing")
                        ),
                        server_key=self.librenms_api.server_key,
                    )

                    logger.info(
                        f"Enqueued FilterDevicesJob {job.pk} (UUID: {job.job_id}) for user {request.user} - {device_count} devices"
                    )

                    # Return JSON for AJAX polling
                    # Use background-tasks endpoint to poll Redis queue (where job actually runs)
                    # IMPORTANT: Use job.job_id (UUID) for background-tasks API, but job.pk for result loading
                    return JsonResponse(
                        {
                            "job_id": str(job.job_id),  # UUID for API polling
                            "job_pk": job.pk,  # Integer PK for result loading
                            "use_polling": True,
                            "poll_url": f"/api/core/background-tasks/{job.job_id}/",
                            "device_count": device_count,
                        }
                    )
                else:
                    # Fallback to synchronous processing
                    logger.warning(
                        "RQ workers not running, falling back to synchronous processing"
                    )
                    messages.warning(
                        request,
                        "Background job system unavailable. Processing may take longer than usual.",
                    )

        queryset = self.get_queryset(request)
        table = self.get_table(queryset, request, bulk_actions=True)

        filter_warning = self._filter_warning

        # Load settings for import defaults
        try:
            settings, _ = LibreNMSSettings.objects.get_or_create()
        except Exception:
            settings = None

        # Get active cached searches for this server
        cached_searches = get_active_cached_searches(self.librenms_api.server_key)

        context = {
            "model": Device,
            "table": table,
            "filter_form": filter_form,
            "title": self.title,
            "filter_warning": filter_warning,
            "filters_submitted": filters_submitted,
            "show_filter_warning": bool(filter_warning),
            "settings": settings,
            "vc_detection_enabled": getattr(self, "_vc_detection_enabled", False),
            "cache_cleared": getattr(self, "_cache_cleared", False),
            "from_cache": getattr(self, "_from_cache", False),
            "cache_timestamp": getattr(self, "_cache_timestamp", None),
            "cache_timeout": getattr(self, "_cache_timeout", 300),
            "cache_metadata_missing": getattr(self, "_cache_metadata_missing", False),
            "cached_searches": cached_searches,
            "librenms_server_info": self.get_server_info(),
        }
        return render(request, self.template_name, context)

    def get_queryset(self, request):  # noqa: D401 - inherited doc
        import_data = self._get_import_queryset()
        self._import_data = import_data
        return Device.objects.none()

    def get_table(self, data, request, bulk_actions=True):
        if not hasattr(self, "_import_data"):
            self._import_data = self._get_import_queryset()

        data = self._import_data
        table = DeviceImportTable(data, order_by=request.GET.get("sort"))
        return table

    def _get_import_queryset(self):
        # Return job results if already loaded
        if getattr(self, "_job_results_loaded", False):
            return getattr(self, "_import_data", [])

        if not getattr(self, "_filters_submitted", False):
            self._libre_filters = {}
            return []

        if self._filter_warning:
            self._libre_filters = {}
            return []

        data_source = getattr(self, "_filter_form_data", None) or {}
        libre_filters = {}
        vc_detection_enabled = (
            data_source.get("enable_vc_detection")
            if "enable_vc_detection" in data_source
            else getattr(self, "_vc_detection_enabled", False)
        )
        clear_cache = (
            data_source.get("clear_cache")
            if "clear_cache" in data_source
            else getattr(self, "_cache_cleared", False)
        )
        self._vc_detection_enabled = vc_detection_enabled
        self._cache_cleared = clear_cache

        if location := data_source.get("librenms_location"):
            libre_filters["location"] = location
        if device_type := data_source.get("librenms_type"):
            libre_filters["type"] = device_type
        if os := data_source.get("librenms_os"):
            libre_filters["os"] = os
        if hostname := data_source.get("librenms_hostname"):
            libre_filters["hostname"] = hostname
        if sysname := data_source.get("librenms_sysname"):
            libre_filters["sysname"] = sysname
        if hardware := data_source.get("librenms_hardware"):
            libre_filters["hardware"] = hardware

        self._libre_filters = libre_filters

        # Form validation already ensures at least one filter is present
        # No need for redundant check here

        # Use shared processing function (same logic as background job)
        show_disabled = bool(data_source.get("show_disabled"))
        exclude_existing = bool(data_source.get("exclude_existing"))

        validated_devices, from_cache = process_device_filters(
            api=self.librenms_api,
            filters=libre_filters,
            vc_detection_enabled=vc_detection_enabled,
            clear_cache=clear_cache,
            show_disabled=show_disabled,
            exclude_existing=exclude_existing,
            request=self._request,
            return_cache_status=True,
        )

        self._from_cache = from_cache

        # Retrieve cache metadata (timestamp) for countdown display
        # This works for both new caches and existing caches
        if validated_devices:
            from netbox_librenms_plugin.import_utils import get_cache_metadata_key

            cache_metadata_key = get_cache_metadata_key(
                server_key=self.librenms_api.server_key,
                filters=libre_filters,
                vc_enabled=vc_detection_enabled,
            )
            cache_metadata = cache.get(cache_metadata_key)
            if cache_metadata:
                self._cache_timestamp = cache_metadata.get("cached_at")
                self._cache_timeout = cache_metadata.get("cache_timeout", 300)
                self._cache_metadata_missing = False
                logger.info(
                    f"Retrieved cache metadata: timestamp={self._cache_timestamp}, "
                    f"timeout={self._cache_timeout}, from_cache={from_cache}"
                )
            else:
                self._cache_metadata_missing = True
                logger.warning(
                    f"Cache metadata not found for key: {cache_metadata_key}, from_cache={from_cache}. "
                    f"This may indicate cache key mismatch or metadata expiration."
                )

        # Mark each device's validation with VC detection flag for downstream views
        for device in validated_devices:
            if "_validation" in device:
                device["_validation"]["_vc_detection_enabled"] = vc_detection_enabled

        return validated_devices
