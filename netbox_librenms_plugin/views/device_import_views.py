"""
Views for importing devices from LibreNMS into NetBox.
Uses HTMX for dynamic UI updates following NetBox patterns.
"""

import logging
from typing import Optional

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from netbox_librenms_plugin.import_utils import (
    create_device_from_librenms,
    create_vm_from_librenms,
    get_librenms_device_by_id,
    validate_device_for_import,
)
from netbox_librenms_plugin.tables.device_status import DeviceImportTable
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

logger = logging.getLogger(__name__)


def _fetch_device_role(role_id: str | int | None) -> Optional[object]:
    """Return the DeviceRole instance for the given id or None if unavailable."""
    if role_id is None:
        return None

    try:
        from dcim.models import DeviceRole  # type: ignore[import]
    except ImportError:  # pragma: no cover - NetBox runtime supplies this module
        return None

    try:
        return DeviceRole.objects.get(pk=int(role_id))
    except (DeviceRole.DoesNotExist, ValueError, TypeError):
        return None


def _fetch_cluster(cluster_id: str | int | None) -> Optional[object]:
    """Return the Cluster instance for the given id or None if unavailable."""
    if cluster_id is None:
        return None

    try:
        from virtualization.models import Cluster  # type: ignore[import]
    except ImportError:  # pragma: no cover - NetBox runtime supplies this module
        return None

    try:
        return Cluster.objects.get(pk=int(cluster_id))
    except (Cluster.DoesNotExist, ValueError, TypeError):
        return None


class DeviceImportPreviewView(LibreNMSAPIMixin, View):
    """
    HTMX view to show preview of device import.
    Returns HTML fragment for modal display.
    """

    def get(self, request, device_id):
        """Show preview of what will be imported"""
        # Get device data from LibreNMS
        libre_device = get_librenms_device_by_id(self.librenms_api, device_id)
        if not libre_device:
            return HttpResponse(
                '<div class="alert alert-danger">Device not found in LibreNMS</div>',
                status=404,
            )

        # Validate the device
        validation = validate_device_for_import(libre_device)

        context = {
            "libre_device": libre_device,
            "validation": validation,
        }

        return render(
            request,
            "netbox_librenms_plugin/htmx/device_import_preview.html",
            context,
        )


class DeviceImportExecuteView(LibreNMSAPIMixin, View):
    """
    Execute device/VM import from LibreNMS.
    Handles both single device and VM import based on cluster selection.

    Logic:
    - If cluster is selected → import as VM
    - If no cluster → import as Device
    """

    def post(self, request, device_id):
        """Import the device or VM into NetBox"""
        # Get the selected role and cluster from the form
        role_id = request.POST.get(f"role_{device_id}")
        cluster_id = request.POST.get(f"cluster_{device_id}")

        # Get name options from form
        use_sysname = request.POST.get("use_sysname", "true") == "true"

        # Determine import type based on cluster selection
        is_vm = bool(cluster_id)  # If cluster selected, import as VM

        # Get device data from LibreNMS
        libre_device = get_librenms_device_by_id(self.librenms_api, device_id)
        if not libre_device:
            messages.error(request, f"Device ID {device_id} not found in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:librenms_import")

        # Validate before import with the appropriate import type
        validation = validate_device_for_import(libre_device, import_as_vm=is_vm)

        if is_vm:
            # VM import path
            role = None
            if role_id:
                role = _fetch_device_role(role_id)

            if cluster_id:
                cluster = _fetch_cluster(cluster_id)
                if not cluster:
                    messages.error(
                        request, f"Selected cluster ID {cluster_id} not found"
                    )
                    return redirect("plugins:netbox_librenms_plugin:librenms_import")

                validation["cluster"]["found"] = True
                validation["cluster"]["cluster"] = cluster
                # Recalculate can_import
                validation["issues"] = [
                    issue
                    for issue in validation["issues"]
                    if "cluster" not in issue.lower()
                ]
                validation["can_import"] = len(validation["issues"]) == 0
                validation["is_ready"] = (
                    validation["can_import"] and validation["cluster"]["found"]
                )

            if not validation["can_import"]:
                messages.error(
                    request,
                    f"Cannot import {libre_device['hostname']}: {', '.join(validation['issues'])}",
                )
                return redirect("plugins:netbox_librenms_plugin:librenms_import")

            try:
                # Create the VM with use_sysname setting and optional role
                vm = create_vm_from_librenms(
                    libre_device, validation, use_sysname=use_sysname, role=role
                )

                messages.success(
                    request,
                    f"Successfully imported VM {vm.name} from LibreNMS",
                )

                # If this is an HTMX request, return empty response with triggers
                if request.headers.get("HX-Request"):
                    return HttpResponse(
                        status=204,  # No content
                        headers={
                            "HX-Trigger": "deviceImported",  # Trigger table refresh
                        },
                    )

                return redirect("virtualization:virtualmachine", pk=vm.pk)

            except Exception as e:
                logger.exception(f"Failed to import VM {libre_device['hostname']}: {e}")
                messages.error(
                    request, f"Failed to import {libre_device['hostname']}: {str(e)}"
                )

                if request.headers.get("HX-Request"):
                    return HttpResponse(
                        status=204,  # No content - messages will show as toast
                    )

                return redirect("plugins:netbox_librenms_plugin:librenms_import")

        else:
            # Device import path
            if role_id:
                role = _fetch_device_role(role_id)
                if not role:
                    messages.error(request, f"Selected role ID {role_id} not found")
                    return redirect("plugins:netbox_librenms_plugin:librenms_import")

                validation["device_role"]["found"] = True
                validation["device_role"]["role"] = role
                # Recalculate can_import
                validation["issues"] = [
                    issue
                    for issue in validation["issues"]
                    if "role" not in issue.lower()
                ]
                validation["can_import"] = len(validation["issues"]) == 0
                validation["is_ready"] = (
                    validation["can_import"]
                    and validation["site"]["found"]
                    and validation["device_type"]["found"]
                    and validation["device_role"]["found"]
                )

            if not validation["can_import"]:
                messages.error(
                    request,
                    f"Cannot import {libre_device['hostname']}: {', '.join(validation['issues'])}",
                )
                return redirect("plugins:netbox_librenms_plugin:librenms_import")

            try:
                # Create the device with use_sysname setting
                device = create_device_from_librenms(
                    libre_device, validation, use_sysname=use_sysname
                )

                messages.success(
                    request,
                    f"Successfully imported device {device.name} from LibreNMS",
                )

                # If this is an HTMX request, return empty response with triggers
                if request.headers.get("HX-Request"):
                    return HttpResponse(
                        status=204,  # No content
                        headers={
                            "HX-Trigger": "deviceImported",  # Trigger table refresh
                        },
                    )

                return redirect("dcim:device", pk=device.pk)

            except Exception as e:
                logger.exception(
                    f"Failed to import device {libre_device['hostname']}: {e}"
                )
                messages.error(
                    request, f"Failed to import {libre_device['hostname']}: {str(e)}"
                )

                if request.headers.get("HX-Request"):
                    return HttpResponse(
                        status=204,  # No content - messages will show as toast
                    )

                return redirect("plugins:netbox_librenms_plugin:device_status_list")


class DeviceValidationDetailsView(LibreNMSAPIMixin, View):
    """
    HTMX view to show detailed validation information.
    Explains why a device/VM cannot be imported.
    """

    def get(self, request, device_id):
        """Show validation details"""
        # Get cluster and role from request (can be in GET or POST from hx-include)
        cluster_id = request.GET.get(f"cluster_{device_id}") or request.POST.get(
            f"cluster_{device_id}"
        )
        role_id = request.GET.get(f"role_{device_id}") or request.POST.get(
            f"role_{device_id}"
        )

        # Determine if importing as VM based on cluster selection
        is_vm = bool(cluster_id)

        # Get device data from LibreNMS
        libre_device = get_librenms_device_by_id(self.librenms_api, device_id)
        if not libre_device:
            return HttpResponse(
                '<div class="alert alert-danger">Device not found in LibreNMS</div>',
                status=404,
            )

        # Validate the device with appropriate import type
        validation = validate_device_for_import(libre_device, import_as_vm=is_vm)

        if is_vm:
            # Check if user has selected a cluster from the dropdown
            if cluster_id:
                cluster = _fetch_cluster(cluster_id)
                if cluster:
                    # Update validation to reflect the selected cluster
                    validation["cluster"]["found"] = True
                    validation["cluster"]["cluster"] = cluster

                    # Remove cluster-related issues
                    validation["issues"] = [
                        issue
                        for issue in validation["issues"]
                        if "cluster" not in issue.lower()
                    ]

                    # Recalculate can_import and is_ready
                    validation["can_import"] = len(validation["issues"]) == 0
                    validation["is_ready"] = (
                        validation["can_import"] and validation["cluster"]["found"]
                    )

            # Check if user has selected a role (optional for VMs)
            if role_id:
                role = _fetch_device_role(role_id)
                if role:
                    # Update validation to reflect the selected role
                    validation["device_role"]["found"] = True
                    validation["device_role"]["role"] = role
        else:
            # Check if user has selected a role from the dropdown
            if role_id:
                role = _fetch_device_role(role_id)
                if role:
                    # Update validation to reflect the selected role
                    validation["device_role"]["found"] = True
                    validation["device_role"]["role"] = role

                    # Remove role-related issues
                    validation["issues"] = [
                        issue
                        for issue in validation["issues"]
                        if "role" not in issue.lower()
                    ]

                    # Recalculate can_import and is_ready
                    validation["can_import"] = len(validation["issues"]) == 0
                    validation["is_ready"] = (
                        validation["can_import"]
                        and validation["site"]["found"]
                        and validation["device_type"]["found"]
                        and validation["device_role"]["found"]
                    )

                # Recalculate can_import and is_ready
                validation["can_import"] = len(validation["issues"]) == 0
                validation["is_ready"] = (
                    validation["can_import"]
                    and validation["site"]["found"]
                    and validation["device_type"]["found"]
                    and validation["device_role"]["found"]
                )

        context = {
            "libre_device": libre_device,
            "validation": validation,
        }

        return render(
            request,
            "netbox_librenms_plugin/htmx/device_validation_details.html",
            context,
        )


class DeviceRoleUpdateView(LibreNMSAPIMixin, View):
    """
    HTMX view to update a table row when a role is selected.
    Returns the updated row HTML with recalculated validation.
    """

    def post(self, request, device_id):
        """Update validation with selected role and return updated row"""
        # Get the selected role ID from the dropdown name (role_{device_id})
        role_id = request.POST.get(f"role_{device_id}")

        # Get the cluster ID if provided (sent via hx-include)
        cluster_id = request.POST.get(f"cluster_{device_id}")

        # Get device from LibreNMS
        libre_device = get_librenms_device_by_id(self.librenms_api, device_id)
        if not libre_device:
            return HttpResponse("Device not found", status=404)

        # Determine import type based on cluster selection
        import_as_vm = bool(cluster_id)  # If cluster selected, import as VM

        # Validate the device with the appropriate import type
        validation = validate_device_for_import(libre_device, import_as_vm=import_as_vm)
        validation["import_as_vm"] = import_as_vm

        # If cluster_id provided, update validation
        if cluster_id:
            cluster = _fetch_cluster(cluster_id)
            if cluster:
                # Update validation to reflect the selected cluster
                validation["cluster"]["found"] = True
                validation["cluster"]["cluster"] = cluster

                # Remove cluster-related issues
                validation["issues"] = [
                    issue
                    for issue in validation["issues"]
                    if "cluster" not in issue.lower()
                ]

                # Recalculate can_import and is_ready
                validation["can_import"] = len(validation["issues"]) == 0
                validation["is_ready"] = (
                    validation["can_import"] and validation["cluster"]["found"]
                )

        # If role_id provided, update validation
        if role_id:
            role = _fetch_device_role(role_id)
            if role:
                # Update validation to reflect the selected role
                validation["device_role"]["found"] = True
                validation["device_role"]["role"] = role

                # Remove role-related issues
                validation["issues"] = [
                    issue
                    for issue in validation["issues"]
                    if "role" not in issue.lower()
                ]

                # Recalculate based on import type
                if import_as_vm:
                    # For VMs, role is optional - recalculate without role check
                    validation["can_import"] = len(validation["issues"]) == 0
                    validation["is_ready"] = (
                        validation["can_import"] and validation["cluster"]["found"]
                    )
                else:
                    # For devices, role is required
                    validation["can_import"] = len(validation["issues"]) == 0
                    validation["is_ready"] = (
                        validation["can_import"]
                        and validation["site"]["found"]
                        and validation["device_type"]["found"]
                        and validation["device_role"]["found"]
                    )

        # Add validation to device dict
        libre_device["_validation"] = validation

        # Create a table with just this one device
        table = DeviceImportTable([libre_device])

        context = {
            "record": libre_device,
            "table": table,
            "cluster_id": cluster_id,
            "role_id": role_id,
        }

        return render(
            request,
            "netbox_librenms_plugin/htmx/device_import_row.html",
            context,
        )


class DeviceClusterUpdateView(LibreNMSAPIMixin, View):
    """
    HTMX view to update a table row when a cluster is selected/deselected.
    Returns the updated row HTML with recalculated validation.

    Logic:
    - If cluster is selected (cluster_id not empty) → import as VM
    - If cluster is not selected (empty) → import as Device
    """

    def post(self, request, device_id):
        """Update validation with selected cluster and return updated row"""
        # Get the selected cluster ID from the dropdown name (cluster_{device_id})
        cluster_id = request.POST.get(f"cluster_{device_id}")

        # Get the role ID if provided (sent via hx-include)
        role_id = request.POST.get(f"role_{device_id}")

        # Get device from LibreNMS
        libre_device = get_librenms_device_by_id(self.librenms_api, device_id)
        if not libre_device:
            return HttpResponse("Device not found", status=404)

        # Determine import type based on cluster selection
        import_as_vm = bool(cluster_id)  # If cluster selected, import as VM

        # Validate the device with the appropriate import type
        validation = validate_device_for_import(libre_device, import_as_vm=import_as_vm)
        validation["import_as_vm"] = import_as_vm

        # If cluster_id provided, update validation
        if cluster_id:
            cluster = _fetch_cluster(cluster_id)
            if cluster:
                # Update validation to reflect the selected cluster
                validation["cluster"]["found"] = True
                validation["cluster"]["cluster"] = cluster

                # Remove cluster-related issues
                validation["issues"] = [
                    issue
                    for issue in validation["issues"]
                    if "cluster" not in issue.lower()
                ]

                # Recalculate can_import and is_ready
                validation["can_import"] = len(validation["issues"]) == 0
                validation["is_ready"] = (
                    validation["can_import"] and validation["cluster"]["found"]
                )

        # If role_id provided for either Device or VM, update validation
        if role_id and not import_as_vm:
            # For devices, role is required - update validation
            role = _fetch_device_role(role_id)
            if role:
                validation["device_role"]["found"] = True
                validation["device_role"]["role"] = role
                # Remove role-related issues
                validation["issues"] = [
                    issue
                    for issue in validation["issues"]
                    if "role" not in issue.lower()
                ]
                # Recalculate
                validation["can_import"] = len(validation["issues"]) == 0
                validation["is_ready"] = (
                    validation["can_import"]
                    and validation["site"]["found"]
                    and validation["device_type"]["found"]
                    and validation["device_role"]["found"]
                )
        elif role_id and import_as_vm:
            # For VMs, role is optional - just store it in validation for display
            role = _fetch_device_role(role_id)
            if role:
                validation["device_role"]["found"] = True
                validation["device_role"]["role"] = role

        # Add validation to device dict
        libre_device["_validation"] = validation

        # Create a table with just this one device
        table = DeviceImportTable([libre_device])

        context = {
            "record": libre_device,
            "table": table,
            "cluster_id": cluster_id,
            "role_id": role_id,
        }

        return render(
            request,
            "netbox_librenms_plugin/htmx/device_import_row.html",
            context,
        )
