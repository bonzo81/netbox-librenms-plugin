import re

from django.shortcuts import get_object_or_404, render
from netbox.views import generic

from netbox_librenms_plugin.forms import AddToLIbreSNMPV2, AddToLIbreSNMPV3
from netbox_librenms_plugin.utils import (
    get_interface_name_field,
    get_librenms_sync_device,
    match_librenms_hardware_to_device_type,
)
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin


class BaseLibreNMSSyncView(LibreNMSAPIMixin, generic.ObjectListView):
    """
    Base view for LibreNMS sync information.
    """

    queryset = None  # Will be set in subclasses
    model = None  # Will be set in subclasses
    tab = None  # Will be set in subclasses
    template_name = "netbox_librenms_plugin/librenms_sync_base.html"

    def get(self, request, pk, context=None):
        """Handle GET request for the LibreNMS sync view."""
        obj = get_object_or_404(self.model, pk=pk)

        # For Virtual Chassis members, determine which device should handle LibreNMS sync
        # NOTE: VC members should NOT have their own librenms_id - LibreNMS only tracks
        # one logical device per VC
        librenms_lookup_device = obj
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            # Check if this device has its own librenms_id
            if not obj.cf.get("librenms_id"):
                # Use helper function to determine the sync device
                sync_device = get_librenms_sync_device(obj)
                if sync_device:
                    librenms_lookup_device = sync_device

        # Get librenms_id using the determined lookup device
        self.librenms_id = self.librenms_api.get_librenms_id(librenms_lookup_device)

        context = self.get_context_data(request, obj)

        return render(request, self.template_name, context)

    def get_context_data(self, request, obj):
        """Get the context data for the LibreNMS sync view."""
        # Get context from parent classes (including LibreNMSAPIMixin)
        context = super().get_context_data()

        # Add our specific context
        context.update(
            {
                "object": obj,
                "tab": self.tab,
                "has_librenms_id": bool(self.librenms_id),
            }
        )

        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            # Use helper function to determine the sync device
            librenms_sync_device = get_librenms_sync_device(obj)

            # Determine sync device status
            sync_device_has_librenms_id = False
            sync_device_has_primary_ip = False

            if librenms_sync_device:
                sync_device_has_librenms_id = bool(
                    librenms_sync_device.cf.get("librenms_id")
                )
                sync_device_has_primary_ip = bool(librenms_sync_device.primary_ip)

            context.update(
                {
                    "is_vc_member": True,
                    "sync_device_has_primary_ip": sync_device_has_primary_ip,
                    "librenms_sync_device": librenms_sync_device,
                    "sync_device_has_librenms_id": sync_device_has_librenms_id,
                }
            )

        librenms_info = self.get_librenms_device_info(obj)

        interface_context = self.get_interface_context(request, obj)
        cable_context = self.get_cable_context(request, obj)
        ip_context = self.get_ip_context(request, obj)

        interface_name_field = get_interface_name_field(request)

        # Get platform info for display and sync
        platform_info = self._get_platform_info(librenms_info, obj)

        # Get manufacturers for platform creation modal
        from dcim.models import Manufacturer

        manufacturers = Manufacturer.objects.all().order_by("name")

        context.update(
            {
                "interface_sync": interface_context,
                "cable_sync": cable_context,
                "ip_sync": ip_context,
                "v2form": AddToLIbreSNMPV2(prefix="v2"),
                "v3form": AddToLIbreSNMPV3(prefix="v3"),
                "librenms_device_id": self.librenms_id,
                "found_in_librenms": librenms_info.get("found_in_librenms"),
                "librenms_device_details": librenms_info.get("librenms_device_details"),
                "mismatched_device": librenms_info.get("mismatched_device"),
                **librenms_info["librenms_device_details"],
                "interface_name_field": interface_name_field,
                "platform_info": platform_info,
                "vc_inventory_serials": librenms_info["librenms_device_details"].get(
                    "vc_inventory_serials", []
                ),
                "manufacturers": manufacturers,
            }
        )

        return context

    def get_librenms_device_info(self, obj):
        """Get the LibreNMS device information for the given object."""
        found_in_librenms = False
        mismatched_device = False
        librenms_device_details = {
            "librenms_device_url": None,
            "librenms_device_hardware": "-",
            "librenms_device_serial": "-",
            "librenms_device_os": "-",
            "librenms_device_version": "-",
            "librenms_device_features": "-",
            "librenms_device_location": "-",
            "librenms_device_hardware_match": None,
            "vc_inventory_serials": [],
        }

        if self.librenms_id:
            success, device_info = self.librenms_api.get_device_info(self.librenms_id)
            if success and device_info:
                # Get NetBox device details
                netbox_ip = str(obj.primary_ip.address.ip) if obj.primary_ip else None
                netbox_hostname = obj.name

                # Get LibreNMS device details
                librenms_hostname = device_info.get("sysName")
                librenms_ip = device_info.get("ip")

                # Extract new fields
                hardware = device_info.get("hardware", "-")
                serial = device_info.get("serial", "-")
                os_name = device_info.get("os", "-")
                version = device_info.get("version", "-")
                features = device_info.get("features", "-")

                # Try to match hardware to NetBox DeviceType
                hardware_match = match_librenms_hardware_to_device_type(hardware)

                # Update device details regardless of match
                librenms_device_details.update(
                    {
                        "librenms_device_url": f"{self.librenms_api.librenms_url}/device/device={self.librenms_id}/",
                        "librenms_device_hardware": hardware,
                        "librenms_device_serial": serial,
                        "librenms_device_os": os_name,
                        "librenms_device_version": version,
                        "librenms_device_features": features,
                        "librenms_device_location": device_info.get("location", "-"),
                        "librenms_device_ip": librenms_ip,
                        "sysName": librenms_hostname,
                        "librenms_device_hardware_match": hardware_match,
                    }
                )

                # For Virtual Chassis, fetch inventory
                if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                    vc_serials = self._get_vc_inventory_serials(obj)
                    librenms_device_details["vc_inventory_serials"] = vc_serials

                # Get just the hostname part from LibreNMS FQDN if present
                librenms_host = (
                    librenms_hostname.split(".")[0].lower()
                    if librenms_hostname
                    else None
                )
                netbox_host = (
                    netbox_hostname.split(".")[0].lower() if netbox_hostname else None
                )

                # Check for matching IP or hostname
                # If IP matches, we have a match
                if netbox_ip == librenms_ip:
                    found_in_librenms = True
                # Check hostname match with normalization for VC suffixes
                elif netbox_host and librenms_host:
                    # Normalize NetBox hostname by removing VC member suffixes like ' (1)', ' (2)', etc.
                    netbox_host_normalized = re.sub(r"\s*\(\d+\)$", "", netbox_host)

                    if netbox_host_normalized == librenms_host:
                        found_in_librenms = True
                    # For VC members with explicit librenms_id, validate hostname similarity
                    elif (
                        hasattr(obj, "virtual_chassis")
                        and obj.virtual_chassis
                        and obj.cf.get("librenms_id")
                    ):
                        # Extract base hostname (before any VC numbering like -1, -2, etc.)
                        # This handles cases where VC members in NetBox (e.g., "switch-1 (1)")
                        # point to the primary device in LibreNMS (e.g., "switch-1")
                        netbox_base = re.sub(r"[-_]?\d+$", "", netbox_host_normalized)
                        librenms_base = re.sub(r"[-_]?\d+$", "", librenms_host)

                        if (
                            netbox_base
                            and librenms_base
                            and netbox_base == librenms_base
                        ):
                            # Base hostnames match (e.g., "switch" matches "switch")
                            found_in_librenms = True
                        else:
                            # Hostnames don't match even after normalization
                            mismatched_device = True
                    else:
                        mismatched_device = True
                else:
                    mismatched_device = True

        return {
            "found_in_librenms": found_in_librenms,
            "librenms_device_details": librenms_device_details,
            "mismatched_device": mismatched_device,
        }

    def get_interface_context(self, request, obj):
        """
        Get the context data for interface sync.
        Subclasses should override this method.
        """
        return None

    def get_cable_context(self, request, obj):
        """
        Get the context data for cable sync.
        Subclasses should override this method if applicable.
        """
        return None

    def get_ip_context(self, request, obj):
        """
        Get the context data for IP address sync.
        Subclasses should override this method.
        """
        return None

    def _get_vc_inventory_serials(self, obj):
        """
        Fetch inventory serials for Virtual Chassis members.

        Args:
            obj: NetBox device object (VC member)

        Returns:
            list: [
                {
                    'description': 'Chassis component description',
                    'serial': 'serial number',
                    'model': 'model name',
                    'assigned_member': Device object or None (if serial matches existing assignment)
                }
            ]
        """
        success, inventory = self.librenms_api.get_device_inventory(self.librenms_id)
        if not success:
            return []

        # Filter for chassis components
        chassis_components = [
            item for item in inventory if item.get("entPhysicalClass") == "chassis"
        ]

        # Get all VC members
        vc_members = obj.virtual_chassis.members.all()

        result = []
        for component in chassis_components:
            serial = component.get("entPhysicalSerialNum", "-")
            if not serial or serial == "-":
                continue

            # Check if this serial is already assigned to a VC member
            assigned_member = None
            for member in vc_members:
                if member.serial and member.serial.strip() == serial.strip():
                    assigned_member = member
                    break

            result.append(
                {
                    "description": component.get("entPhysicalDescr", "-"),
                    "serial": serial,
                    "model": component.get("entPhysicalModelName", "-"),
                    "assigned_member": assigned_member,
                }
            )

        return result

    def _get_platform_info(self, librenms_info, obj):
        """
        Get platform information from LibreNMS.

        Platform matching is based on OS name only (not version).
        Version is displayed separately as informational data.

        Args:
            librenms_info: Dictionary with LibreNMS device info
            obj: NetBox device object

        Returns:
            dict: {
                'netbox_platform': Platform object or None,
                'librenms_os': str (OS name),
                'librenms_version': str (OS version),
                'platform_exists': bool (whether OS platform exists in NetBox),
                'platform_name': str (OS name for platform matching),
                'matching_platform': Platform object or None
            }
        """
        from dcim.models import Platform

        librenms_os = librenms_info["librenms_device_details"].get(
            "librenms_device_os", "-"
        )
        librenms_version = librenms_info["librenms_device_details"].get(
            "librenms_device_version", "-"
        )

        # Platform name is just the OS (not OS + version)
        platform_name = librenms_os if librenms_os != "-" else None

        # Check if platform exists (match by OS name only)
        platform_exists = False
        matching_platform = None
        if platform_name:
            try:
                matching_platform = Platform.objects.get(name__iexact=platform_name)
                platform_exists = True
            except Platform.DoesNotExist:
                pass

        return {
            "netbox_platform": obj.platform,
            "librenms_os": librenms_os,
            "librenms_version": librenms_version,
            "platform_exists": platform_exists,
            "platform_name": platform_name,
            "matching_platform": matching_platform,
        }
