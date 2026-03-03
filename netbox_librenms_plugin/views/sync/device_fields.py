from dcim.models import Device, Manufacturer, Platform
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin


class UpdateDeviceNameView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Update NetBox device name from LibreNMS sysName."""

    required_object_permissions = {
        "POST": [("change", Device)],
    }

    def post(self, request, pk):
        """Sync the device name from LibreNMS sysName."""
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        success, device_info = self.librenms_api.get_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        sys_name = device_info.get("sysName")

        if not sys_name:
            messages.warning(request, "No sysName available in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        old_name = device.name
        device.name = sys_name
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.name = old_name
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to update device name to '{sys_name}': {error_msg}")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        messages.success(request, f"Device name updated from '{old_name}' to '{sys_name}'")

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class UpdateDeviceSerialView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Update NetBox device serial number from LibreNMS."""

    required_object_permissions = {
        "POST": [("change", Device)],
    }

    def post(self, request, pk):
        """Sync the device serial number from LibreNMS."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        success, device_info = self.librenms_api.get_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        serial = device_info.get("serial")

        if not serial or serial == "-":
            messages.warning(request, "No serial number available in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        old_serial = device.serial
        device.serial = serial
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.serial = old_serial
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to update serial to '{serial}': {error_msg}")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        if old_serial:
            messages.success(
                request,
                f"Device serial updated from '{old_serial}' to '{serial}'",
            )
        else:
            messages.success(request, f"Device serial set to '{serial}'")

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class UpdateDeviceTypeView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Update NetBox DeviceType using LibreNMS hardware metadata."""

    required_object_permissions = {
        "POST": [("change", Device)],
    }

    def post(self, request, pk):
        """Sync the device type from LibreNMS hardware info."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        success, device_info = self.librenms_api.get_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        hardware = device_info.get("hardware")

        if not hardware:
            messages.warning(request, "No hardware information available in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        match_result = match_librenms_hardware_to_device_type(hardware)

        if not match_result["matched"]:
            messages.error(
                request,
                f"No matching DeviceType found for hardware '{hardware}'",
            )
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        device_type = match_result["device_type"]
        old_device_type = device.device_type
        device.device_type = device_type
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.device_type = old_device_type
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to update device type to '{device_type}': {error_msg}")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        messages.success(
            request,
            f"Device type updated from '{old_device_type}' to '{device_type}'",
        )

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class UpdateDevicePlatformView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Update NetBox Platform based on LibreNMS OS info."""

    required_object_permissions = {
        "POST": [("change", Device)],
    }

    def post(self, request, pk):
        """Sync the device platform from LibreNMS OS name."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        success, device_info = self.librenms_api.get_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        os_name = device_info.get("os")

        if not os_name:
            messages.warning(request, "No OS information available in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        platform_name = os_name

        try:
            platform = Platform.objects.get(name__iexact=platform_name)
        except Platform.DoesNotExist:
            messages.error(
                request,
                "Platform '{}' does not exist in NetBox. Use 'Create & Sync' button to create it first.".format(
                    platform_name
                ),
            )
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        old_platform = device.platform
        device.platform = platform
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.platform = old_platform
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to update platform to '{platform}': {error_msg}")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        if old_platform:
            messages.success(
                request,
                f"Device platform updated from '{old_platform}' to '{platform}'",
            )
        else:
            messages.success(request, f"Device platform set to '{platform}'")

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class CreateAndAssignPlatformView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Create a new Platform and assign it to the device."""

    required_object_permissions = {
        "POST": [
            ("change", Device),
            ("add", Platform),
        ],
    }

    def post(self, request, pk):
        """Create a new platform and assign it to the device."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)

        platform_name = request.POST.get("platform_name")
        manufacturer_id = request.POST.get("manufacturer")

        if not platform_name:
            messages.error(request, "Platform name is required")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        if Platform.objects.filter(name__iexact=platform_name).exists():
            messages.warning(
                request,
                f"Platform '{platform_name}' already exists. Use the regular sync button.",
            )
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        manufacturer = None
        if manufacturer_id:
            try:
                manufacturer = Manufacturer.objects.get(pk=manufacturer_id)
            except Manufacturer.DoesNotExist:
                pass

        try:
            platform = Platform.objects.create(
                name=platform_name,
                manufacturer=manufacturer,
            )
        except IntegrityError:
            messages.error(
                request,
                f"Platform '{platform_name}' could not be created (slug collision). Try a different name.",
            )
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        old_platform = device.platform
        device.platform = platform
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.platform = old_platform
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to assign platform '{platform}': {error_msg}")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        messages.success(
            request,
            f"Created platform '{platform}' and assigned to device",
        )

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class AssignVCSerialView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Assign serial numbers to each virtual chassis member."""

    required_object_permissions = {
        "POST": [("change", Device)],
    }

    def post(self, request, pk):
        """Sync serial numbers to virtual chassis member devices."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)

        if not device.virtual_chassis:
            messages.error(request, "Device is not part of a virtual chassis")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        assignments_made = 0
        errors = []

        counter = 1
        while f"serial_{counter}" in request.POST:
            serial = request.POST.get(f"serial_{counter}")
            member_id = request.POST.get(f"member_id_{counter}")

            if not member_id:
                counter += 1
                continue

            try:
                member = Device.objects.get(pk=member_id)

                if not member.virtual_chassis or member.virtual_chassis.pk != device.virtual_chassis.pk:
                    errors.append(f"{member.name} is not part of the same virtual chassis")
                    counter += 1
                    continue

                old_serial = member.serial
                member.serial = serial
                try:
                    member.full_clean()
                    member.save()
                except (ValidationError, IntegrityError) as e:
                    member.serial = old_serial
                    error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
                    errors.append(f"Failed to set serial on {member.name}: {error_msg}")
                    counter += 1
                    continue

                assignments_made += 1

            except Device.DoesNotExist:
                errors.append(f"Device with ID {member_id} not found")
            except Exception as exc:  # pragma: no cover - defensive guard
                errors.append(f"Error assigning serial to member {member_id}: {str(exc)}")

            counter += 1

        if assignments_made > 0:
            messages.success(
                request,
                f"Successfully assigned {assignments_made} serial number(s) to VC members",
            )

        if errors:
            for error in errors:
                messages.error(request, error)

        if assignments_made == 0 and not errors:
            messages.info(request, "No serial assignments were made")

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)
