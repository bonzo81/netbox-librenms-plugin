"""
Views for importing devices from LibreNMS to NetBox.

This module provides views for:
- Single device import
- Bulk device import
- Import preview
- Import configuration
- Validation details
"""

import logging

from django.contrib import messages
from django.shortcuts import render
from django.views import View

from netbox_librenms_plugin.forms import DeviceImportConfigForm
from netbox_librenms_plugin.import_utils import (
    bulk_import_devices,
    import_single_device,
    validate_device_for_import,
)
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

logger = logging.getLogger(__name__)


class ImportSingleDeviceView(LibreNMSAPIMixin, View):
    """
    Import a single device from LibreNMS to NetBox.

    Handles AJAX/HTMX requests to import a device.
    Returns HTML partial for success or error message.
    """

    def post(self, request):
        """Handle device import request."""
        try:
            device_id = request.POST.get("device_id")
            if not device_id:
                return render(
                    request,
                    "netbox_librenms_plugin/partials/import_error.html",
                    {"error_message": "Device ID is required"},
                    status=400,
                )

            device_id = int(device_id)

            # Get manual mappings if provided
            manual_mappings = {}
            if request.POST.get("site_id"):
                manual_mappings["site_id"] = int(request.POST.get("site_id"))
            if request.POST.get("device_type_id"):
                manual_mappings["device_type_id"] = int(
                    request.POST.get("device_type_id")
                )
            if request.POST.get("device_role_id"):
                manual_mappings["device_role_id"] = int(
                    request.POST.get("device_role_id")
                )
            if request.POST.get("platform_id"):
                manual_mappings["platform_id"] = int(request.POST.get("platform_id"))

            # Get sync options
            sync_options = {
                "sync_interfaces": request.POST.get("sync_interfaces") == "on",
                "sync_cables": request.POST.get("sync_cables") == "on",
                "sync_ips": request.POST.get("sync_ips") == "on",
            }

            # Perform import
            result = import_single_device(
                device_id=device_id,
                server_key=self.server_key,
                manual_mappings=manual_mappings if manual_mappings else None,
                sync_options=sync_options,
            )

            if result["success"]:
                return render(
                    request,
                    "netbox_librenms_plugin/partials/import_success.html",
                    {
                        "device": result["device"],
                        "message": result["message"],
                        "synced": result.get("synced", {}),
                    },
                )
            else:
                return render(
                    request,
                    "netbox_librenms_plugin/partials/import_error.html",
                    {"error_message": result["error"]},
                    status=400,
                )

        except ValueError:
            logger.exception("Invalid device ID format")
            return render(
                request,
                "netbox_librenms_plugin/partials/import_error.html",
                {"error_message": "Invalid device ID format"},
                status=400,
            )
        except Exception as e:
            logger.exception("Error importing device")
            return render(
                request,
                "netbox_librenms_plugin/partials/import_error.html",
                {"error_message": str(e)},
                status=500,
            )


class BulkImportDevicesView(LibreNMSAPIMixin, View):
    """
    Import multiple devices from LibreNMS to NetBox.

    Handles bulk import of selected devices.
    Returns HTML partial with results summary.
    """

    def post(self, request):
        """Handle bulk import request."""
        try:
            device_ids = request.POST.getlist("select")
            if not device_ids:
                return render(
                    request,
                    "netbox_librenms_plugin/partials/import_error.html",
                    {"error_message": "No devices selected for import"},
                    status=400,
                )

            # Convert to integers
            device_ids = [int(did) for did in device_ids]

            # Get sync options (applied to all devices)
            sync_options = {
                "sync_interfaces": request.POST.get("sync_interfaces") == "on",
                "sync_cables": request.POST.get("sync_cables") == "on",
                "sync_ips": request.POST.get("sync_ips") == "on",
                "use_sysname": request.POST.get("use_sysname") == "true",
                "strip_domain": request.POST.get("strip_domain") == "true",
            }

            # Collect manual role mappings for each device
            manual_mappings_per_device = {}
            for device_id in device_ids:
                role_id = request.POST.get(f"role_{device_id}")
                if role_id:
                    manual_mappings_per_device[device_id] = {
                        "device_role_id": int(role_id)
                    }

            # Perform bulk import
            result = bulk_import_devices(
                device_ids=device_ids,
                server_key=self.librenms_api.server_key,
                sync_options=sync_options,
                manual_mappings_per_device=manual_mappings_per_device,
            )

            # Add success/error messages
            if result["success"]:
                messages.success(
                    request,
                    f"Successfully imported {len(result['success'])} device(s)",
                )
            if result["failed"]:
                messages.error(
                    request,
                    f"Failed to import {len(result['failed'])} device(s)",
                )
            if result["skipped"]:
                messages.warning(
                    request,
                    f"Skipped {len(result['skipped'])} device(s) (already exist)",
                )

            return render(
                request,
                "netbox_librenms_plugin/partials/bulk_import_results.html",
                {
                    "total": result["total"],
                    "success_list": result["success"],
                    "failed_list": result["failed"],
                    "skipped_list": result["skipped"],
                },
            )

        except ValueError:
            logger.exception("Invalid device ID in selection")
            return render(
                request,
                "netbox_librenms_plugin/partials/import_error.html",
                {"error_message": "Invalid device ID in selection"},
                status=400,
            )
        except Exception as e:
            logger.exception("Error during bulk import")
            return render(
                request,
                "netbox_librenms_plugin/partials/import_error.html",
                {"error_message": str(e)},
                status=500,
            )


class DeviceImportPreviewView(LibreNMSAPIMixin, View):
    """
    Preview what will be imported for a specific device.

    Shows device details, mapped objects, and counts of related data.
    Returns HTML partial for modal display.
    """

    def get(self, request, device_id):
        """Show import preview."""
        try:
            # Get device info from LibreNMS
            success, libre_device = self.librenms_api.get_device_info(device_id)
            if not success or not libre_device:
                return render(
                    request,
                    "netbox_librenms_plugin/partials/import_error.html",
                    {"error_message": f"Device {device_id} not found in LibreNMS"},
                    status=404,
                )

            # Validate device
            validation = validate_device_for_import(libre_device)

            # Get interface/cable/IP counts if available
            counts = {"interfaces": 0, "cables": 0, "ip_addresses": 0}

            try:
                # Get ports count
                ports_response = self.librenms_api.get_ports(device_id)
                if ports_response and "ports" in ports_response:
                    counts["interfaces"] = len(ports_response["ports"])
            except Exception:
                pass

            try:
                # Get links count
                success, links = self.librenms_api.get_device_links(device_id)
                if success and links:
                    counts["cables"] = len(links)
            except Exception:
                pass

            try:
                # Get IP addresses count
                success, ips = self.librenms_api.get_device_ips(device_id)
                if success and ips:
                    counts["ip_addresses"] = len(ips)
            except Exception:
                pass

            return render(
                request,
                "netbox_librenms_plugin/partials/import_preview.html",
                {
                    "libre_device": libre_device,
                    "validation": validation,
                    "counts": counts,
                    "device_id": device_id,
                },
            )

        except Exception as e:
            logger.exception(f"Error generating preview for device {device_id}")
            return render(
                request,
                "netbox_librenms_plugin/partials/import_error.html",
                {"error_message": str(e)},
                status=500,
            )


class DeviceImportConfigureView(LibreNMSAPIMixin, View):
    """
    Configure import settings for devices with validation issues.

    GET: Shows configuration form with manual mapping options
    POST: Processes configuration and imports device
    Returns HTML partial for modal display.
    """

    def get(self, request, device_id):
        """Show configuration form."""
        try:
            # Get device info from LibreNMS
            success, libre_device = self.librenms_api.get_device_info(device_id)
            if not success or not libre_device:
                return render(
                    request,
                    "netbox_librenms_plugin/partials/import_error.html",
                    {"error_message": f"Device {device_id} not found in LibreNMS"},
                    status=404,
                )

            # Validate device
            validation = validate_device_for_import(libre_device)

            # Initialize form with validation results
            form = DeviceImportConfigForm(
                libre_device=libre_device,
                validation=validation,
            )

            return render(
                request,
                "netbox_librenms_plugin/partials/configure_import.html",
                {
                    "form": form,
                    "libre_device": libre_device,
                    "validation": validation,
                    "device_id": device_id,
                },
            )

        except Exception as e:
            logger.exception(f"Error loading configuration form for device {device_id}")
            return render(
                request,
                "netbox_librenms_plugin/partials/import_error.html",
                {"error_message": str(e)},
                status=500,
            )

    def post(self, request, device_id):
        """Process configuration and import device."""
        try:
            # Get device info from LibreNMS
            success, libre_device = self.librenms_api.get_device_info(device_id)
            if not success or not libre_device:
                return render(
                    request,
                    "netbox_librenms_plugin/partials/import_error.html",
                    {"error_message": f"Device {device_id} not found in LibreNMS"},
                    status=404,
                )

            # Validate device
            validation = validate_device_for_import(libre_device)

            # Process form
            form = DeviceImportConfigForm(
                request.POST,
                libre_device=libre_device,
                validation=validation,
            )

            if form.is_valid():
                # Extract manual mappings
                manual_mappings = {
                    "site_id": form.cleaned_data["site"].id,
                    "device_type_id": form.cleaned_data["device_type"].id,
                    "device_role_id": form.cleaned_data["device_role"].id,
                }
                if form.cleaned_data.get("platform"):
                    manual_mappings["platform_id"] = form.cleaned_data["platform"].id

                # Extract sync options
                sync_options = {
                    "sync_interfaces": form.cleaned_data.get("sync_interfaces", True),
                    "sync_cables": form.cleaned_data.get("sync_cables", True),
                    "sync_ips": form.cleaned_data.get("sync_ips", True),
                }

                # Perform import
                result = import_single_device(
                    device_id=device_id,
                    server_key=self.server_key,
                    validation=validation,
                    manual_mappings=manual_mappings,
                    sync_options=sync_options,
                )

                if result["success"]:
                    return render(
                        request,
                        "netbox_librenms_plugin/partials/import_success.html",
                        {
                            "device": result["device"],
                            "message": result["message"],
                            "synced": result.get("synced", {}),
                        },
                    )
                else:
                    return render(
                        request,
                        "netbox_librenms_plugin/partials/import_error.html",
                        {"error_message": result["error"]},
                        status=400,
                    )
            else:
                # Form validation failed
                return render(
                    request,
                    "netbox_librenms_plugin/partials/configure_import.html",
                    {
                        "form": form,
                        "libre_device": libre_device,
                        "validation": validation,
                        "device_id": device_id,
                    },
                    status=400,
                )

        except Exception as e:
            logger.exception(f"Error configuring import for device {device_id}")
            return render(
                request,
                "netbox_librenms_plugin/partials/import_error.html",
                {"error_message": str(e)},
                status=500,
            )


class DeviceValidationDetailView(LibreNMSAPIMixin, View):
    """
    Show detailed validation information for a device.

    Displays all validation issues, warnings, and matching results.
    Returns HTML partial for modal display.
    """

    def get(self, request, device_id):
        """Show validation details."""
        try:
            # Get device info from LibreNMS
            success, libre_device = self.librenms_api.get_device_info(device_id)
            if not success or not libre_device:
                return render(
                    request,
                    "netbox_librenms_plugin/partials/import_error.html",
                    {"error_message": f"Device {device_id} not found in LibreNMS"},
                    status=404,
                )

            # Validate device
            validation = validate_device_for_import(libre_device)

            return render(
                request,
                "netbox_librenms_plugin/partials/validation_detail.html",
                {
                    "libre_device": libre_device,
                    "validation": validation,
                    "device_id": device_id,
                },
            )

        except Exception as e:
            logger.exception(f"Error loading validation details for device {device_id}")
            return render(
                request,
                "netbox_librenms_plugin/partials/import_error.html",
                {"error_message": str(e)},
                status=500,
            )
