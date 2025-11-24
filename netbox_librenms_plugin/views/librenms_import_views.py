import logging

from dcim.models import Device
from django.shortcuts import render
from netbox.views import generic

from netbox_librenms_plugin.forms import LibreNMSImportFilterForm
from netbox_librenms_plugin.import_utils import (
    get_librenms_devices_for_import,
    validate_device_for_import,
)
from netbox_librenms_plugin.tables.device_status import DeviceImportTable
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

logger = logging.getLogger(__name__)


class LibreNMSImportView(LibreNMSAPIMixin, generic.ObjectListView):
    """
    Import devices from LibreNMS into NetBox.
    Shows devices from LibreNMS with validation status against NetBox.
    """

    queryset = Device.objects.none()  # Empty queryset - we use LibreNMS data
    table = DeviceImportTable
    filterset = None  # No filterset - we filter via LibreNMS API
    filterset_form = LibreNMSImportFilterForm
    template_name = "netbox_librenms_plugin/librenms_import.html"
    actions = {}
    title = "Import Devices from LibreNMS"

    def get_required_permission(self):
        """
        Always require 'view' permission for Device model.
        """
        from utilities.permissions import get_permission_for_model

        return get_permission_for_model(Device, "view")

    def get(self, request, *args, **kwargs):
        """
        Override to bypass table config logic.
        We use plain django_tables2.Table without a model,
        which doesn't support NetBox's table configuration features.
        """
        # Get the filtered queryset (returns Device.objects.none())
        queryset = self.get_queryset(request)

        # Get the table with import data
        table = self.get_table(queryset, request, bulk_actions=True)

        # Render the template with required context
        context = {
            "model": Device,  # Required by applied_filters template tag
            "table": table,
            "filter_form": self.filterset_form(request.GET)
            if self.filterset_form
            else None,
            "title": self.title,
        }
        return render(request, self.template_name, context)

    def get_queryset(self, request):
        """Get LibreNMS devices for import with validation"""
        import_data = self._get_import_queryset()
        # Store import data for later use
        self._import_data = import_data
        # Return empty Device queryset for permission checks
        # The actual data will be used by get_table()
        return Device.objects.none()

    def get_table(self, data, request, bulk_actions=True):
        """Override to pass import data to table"""
        # Ensure import data is loaded
        if not hasattr(self, "_import_data"):
            self._import_data = self._get_import_queryset()

        data = self._import_data
        # DeviceImportTable doesn't accept user parameter
        table = DeviceImportTable(data)
        return table

    def _get_import_queryset(self):
        """Get LibreNMS devices for import with validation"""
        # Don't load anything if no filters are provided at all
        if not self.request.GET:
            return []

        # Build filter dict from request
        libre_filters = {}

        # LibreNMS-specific filters (these are the meaningful filters)
        if location := self.request.GET.get("librenms_location"):
            libre_filters["location"] = location
        if device_type := self.request.GET.get("librenms_type"):
            libre_filters["type"] = device_type
        if os := self.request.GET.get("librenms_os"):
            libre_filters["os"] = os
        if hostname := self.request.GET.get("librenms_hostname"):
            libre_filters["hostname"] = hostname
        if sysname := self.request.GET.get("librenms_sysname"):
            libre_filters["sysname"] = sysname

        # CRITICAL: Require at least one meaningful filter to prevent loading ALL devices
        # Only 'validation_status' and 'show_disabled' are in GET params by default
        # We need at least one LibreNMS filter (location, type, os, hostname, or sysname)
        if not libre_filters:
            # Return empty list - user must select at least one filter
            return []

        # Get devices from LibreNMS with filters
        libre_devices = get_librenms_devices_for_import(
            self.librenms_api, filters=libre_filters
        )

        # Filter by disabled status if requested
        # Checkbox: if checked (on), include all devices; if unchecked, exclude disabled
        show_disabled = self.request.GET.get("show_disabled") == "on"
        if not show_disabled:  # Checkbox unchecked - hide disabled devices
            libre_devices = [d for d in libre_devices if d.get("status") == 1]
        # If checkbox is checked, show all devices (enabled and disabled)

        # Validate each device
        validated_devices = []
        for device in libre_devices:
            validation = validate_device_for_import(device)

            # Filter by validation status if requested
            validation_filter = self.request.GET.get("validation_status")
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

            # Add validation info to device dict
            device["_validation"] = validation
            validated_devices.append(device)

        return validated_devices
