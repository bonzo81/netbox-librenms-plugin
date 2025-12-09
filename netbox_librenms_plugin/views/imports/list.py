import logging

from dcim.models import Device
from django.core.cache import cache
from django.shortcuts import render
from netbox.views import generic

from netbox_librenms_plugin.forms import LibreNMSImportFilterForm
from netbox_librenms_plugin.import_utils import (
    empty_virtual_chassis_data,
    get_librenms_devices_for_import,
    validate_device_for_import,
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

    def get(self, request, *args, **kwargs):  # noqa: D401 - inherited doc
        """Render the import table backed by LibreNMS data."""
        self._filter_warning = None
        self._filter_form_data = {}
        self._libre_filters = {}
        self._cache_cleared = False
        self._request = request  # Store request for connection checks

        libre_filter_fields = (
            "librenms_location",
            "librenms_type",
            "librenms_os",
            "librenms_hostname",
            "librenms_sysname",
        )
        filters_present = any(request.GET.get(field) for field in libre_filter_fields)
        filters_submitted = request.GET.get("apply_filters") or filters_present

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

        queryset = self.get_queryset(request)
        table = self.get_table(queryset, request, bulk_actions=True)

        filter_warning = self._filter_warning

        # Load settings for import defaults
        try:
            settings, _ = LibreNMSSettings.objects.get_or_create(
                defaults={
                    "selected_server": "default",
                    "use_sysname_default": True,
                    "strip_domain_default": False,
                }
            )
        except Exception:
            settings = None

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
            "vc_detection_skipped": not getattr(self, "_vc_detection_enabled", False),
            "cache_cleared": getattr(self, "_cache_cleared", False),
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
        table = DeviceImportTable(data)
        return table

    def _get_import_queryset(self):
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

        self._libre_filters = libre_filters

        if not libre_filters:
            self._filter_warning = (
                "Please select at least one LibreNMS filter before applying the search."
            )
            return []

        libre_devices = get_librenms_devices_for_import(
            self.librenms_api,
            filters=libre_filters,
            force_refresh=clear_cache,
        )

        show_disabled = bool(data_source.get("show_disabled"))
        if not show_disabled:
            libre_devices = [d for d in libre_devices if d.get("status") == 1]

        validated_devices = []
        api_for_validation = self.librenms_api if vc_detection_enabled else None

        # Pre-warm VC cache before validation loop to improve performance
        if vc_detection_enabled and libre_devices:
            from netbox_librenms_plugin.import_utils import prefetch_vc_data_for_devices

            device_ids = [d["device_id"] for d in libre_devices]
            try:
                prefetch_vc_data_for_devices(
                    self.librenms_api, device_ids, force_refresh=clear_cache
                )
            except (BrokenPipeError, ConnectionError, IOError) as e:
                logger.info(f"Client disconnected during VC prefetch: {e}")
                return []

        for idx, device in enumerate(libre_devices):
            # Periodically check if client is still connected (every 10 devices)
            if idx % 10 == 0:
                try:
                    # Attempt to detect client disconnect by checking wsgi.input
                    if hasattr(self._request, 'META') and self._request.META.get('wsgi.input'):
                        # Force a check by trying to peek at the connection
                        pass
                except (BrokenPipeError, ConnectionError, IOError):
                    logger.info(f"Client disconnected during validation at device {idx}")
                    return []
            
            # Drop any cached validation/meta keys before recomputing
            device.pop("_validation", None)
            try:
                validation = validate_device_for_import(
                    device,
                    api=api_for_validation,
                    include_vc_detection=vc_detection_enabled,
                    force_vc_refresh=clear_cache,
                )
            except (BrokenPipeError, ConnectionError, IOError) as e:
                logger.info(f"Client disconnected during device validation: {e}")
                return []

            if not vc_detection_enabled:
                validation["virtual_chassis"] = empty_virtual_chassis_data()
                validation["_vc_detection_skipped"] = True
            else:
                validation["_vc_detection_skipped"] = False

            validation_filter = data_source.get("validation_status")
            if validation_filter:
                has_existing = bool(validation["existing_device"])
                if validation_filter == "ready" and not validation["is_ready"]:
                    continue
                elif validation_filter == "needs_review" and (
                    has_existing
                    or validation["is_ready"]
                    or not validation["can_import"]
                ):
                    continue
                elif validation_filter == "cannot_import" and (
                    has_existing or validation["can_import"]
                ):
                    continue
                elif validation_filter == "exists" and not has_existing:
                    continue

            device["_validation"] = validation
            validated_devices.append(device)

            # Cache device data for role/cluster/rack updates
            # Uses configured cache_timeout (default 300s)
            cache_key = f"import_device_data_{device['device_id']}"
            cache.set(cache_key, device, timeout=self.librenms_api.cache_timeout)

        return validated_devices
