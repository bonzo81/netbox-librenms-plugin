from dcim.models import Device, Manufacturer, Platform
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin


class UpdateDeviceSerialView(LibreNMSAPIMixin, View):
    """
    Update device serial number from LibreNMS to NetBox.
    """

    def post(self, request, pk):
        """Handle POST request to update device serial."""
        device = get_object_or_404(Device, pk=pk)

        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        # Fetch device info from LibreNMS
        success, device_info = self.librenms_api.get_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        serial = device_info.get("serial")

        if not serial or serial == "-":
            messages.warning(request, "No serial number available in LibreNMS")
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        # Update device serial
        old_serial = device.serial
        device.serial = serial
        device.save()

        if old_serial:
            messages.success(
                request,
                f"Device serial updated from '{old_serial}' to '{serial}'",
            )
        else:
            messages.success(
                request,
                f"Device serial set to '{serial}'",
            )

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class UpdateDeviceTypeView(LibreNMSAPIMixin, View):
    """
    Update device type from LibreNMS hardware match to NetBox.
    """

    def post(self, request, pk):
        """Handle POST request to update device type."""
        device = get_object_or_404(Device, pk=pk)

        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        # Fetch device info
        success, device_info = self.librenms_api.get_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        hardware = device_info.get("hardware")

        if not hardware:
            messages.warning(request, "No hardware information available in LibreNMS")
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        # Try to match device type using utility function
        match_result = match_librenms_hardware_to_device_type(hardware)
        
        if not match_result['matched']:
            messages.error(
                request,
                f"No matching DeviceType found for hardware '{hardware}'",
            )
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        # Update device type
        device_type = match_result['device_type']
        old_device_type = device.device_type
        device.device_type = device_type
        device.save()

        messages.success(
            request,
            f"Device type updated from '{old_device_type}' to '{device_type}'",
        )

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class UpdateDevicePlatformView(LibreNMSAPIMixin, View):
    """
    Update device platform from LibreNMS OS info to NetBox.
    Platform must already exist in NetBox.
    Platform matching is based on OS name only (not version).
    """

    def post(self, request, pk):
        """Handle POST request to update device platform."""
        device = get_object_or_404(Device, pk=pk)

        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        # Fetch device info
        success, device_info = self.librenms_api.get_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        os_name = device_info.get("os")

        if not os_name:
            messages.warning(request, "No OS information available in LibreNMS")
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        # Platform name is just the OS (not OS + version)
        platform_name = os_name

        # Find platform
        try:
            platform = Platform.objects.get(name__iexact=platform_name)
        except Platform.DoesNotExist:
            messages.error(
                request,
                f"Platform '{platform_name}' does not exist in NetBox. Use 'Create & Sync' button to create it first.",
            )
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        # Update device platform
        old_platform = device.platform
        device.platform = platform
        device.save()

        if old_platform:
            messages.success(
                request,
                f"Device platform updated from '{old_platform}' to '{platform}'",
            )
        else:
            messages.success(
                request,
                f"Device platform set to '{platform}'",
            )

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class CreateAndAssignPlatformView(LibreNMSAPIMixin, View):
    """
    Create a new platform from LibreNMS OS info and assign to device.
    """

    def post(self, request, pk):
        """Handle POST request to create platform and assign to device."""
        device = get_object_or_404(Device, pk=pk)

        platform_name = request.POST.get("platform_name")
        manufacturer_id = request.POST.get("manufacturer")

        if not platform_name:
            messages.error(request, "Platform name is required")
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        # Check if platform already exists
        if Platform.objects.filter(name__iexact=platform_name).exists():
            messages.warning(
                request,
                f"Platform '{platform_name}' already exists. Use the regular sync button.",
            )
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        # Get manufacturer if provided
        manufacturer = None
        if manufacturer_id:
            try:
                manufacturer = Manufacturer.objects.get(pk=manufacturer_id)
            except Manufacturer.DoesNotExist:
                pass

        # Create platform
        platform = Platform.objects.create(
            name=platform_name,
            manufacturer=manufacturer,
        )

        # Assign to device
        device.platform = platform
        device.save()

        messages.success(
            request,
            f"Created platform '{platform}' and assigned to device",
        )

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class AssignVCSerialView(LibreNMSAPIMixin, View):
    """
    View to assign multiple serial numbers to VC members from LibreNMS inventory.
    Handles batch assignment of all serials in one POST request.
    """

    def post(self, request, pk):
        """Handle POST request to assign multiple serial numbers to VC members."""
        device = get_object_or_404(Device, pk=pk)

        # Verify device is part of VC
        if not device.virtual_chassis:
            messages.error(request, "Device is not part of a virtual chassis")
            return redirect(
                "plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk
            )

        # Process all serial assignments from the form
        # Form submits: serial_1, member_id_1, serial_2, member_id_2, etc.
        assignments_made = 0
        errors = []

        # Get all the serial/member pairs from POST data
        counter = 1
        while f"serial_{counter}" in request.POST:
            serial = request.POST.get(f"serial_{counter}")
            member_id = request.POST.get(f"member_id_{counter}")

            # Skip if no member selected for this serial
            if not member_id:
                counter += 1
                continue

            try:
                # Get member device
                member = Device.objects.get(pk=member_id)

                # Verify member is part of same VC
                if (
                    not member.virtual_chassis
                    or member.virtual_chassis.pk != device.virtual_chassis.pk
                ):
                    errors.append(
                        f"{member.name} is not part of the same virtual chassis"
                    )
                    counter += 1
                    continue

                # Update member serial
                member.serial = serial
                member.save()

                assignments_made += 1

            except Device.DoesNotExist:
                errors.append(f"Device with ID {member_id} not found")
            except Exception as e:
                errors.append(f"Error assigning serial to member {member_id}: {str(e)}")

            counter += 1

        # Show results
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
