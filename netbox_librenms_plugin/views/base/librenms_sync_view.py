import re

from django.shortcuts import get_object_or_404, render
from netbox.views import generic

from netbox_librenms_plugin.forms import AddToLIbreSNMPV2, AddToLIbreSNMPV3
from netbox_librenms_plugin.utils import (
    get_interface_name_field,
    get_librenms_sync_device,
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

        context.update(
            {
                "interface_sync": interface_context,
                "cable_sync": cable_context,
                "ip_sync": ip_context,
                "v2form": AddToLIbreSNMPV2(),
                "v3form": AddToLIbreSNMPV3(),
                "librenms_device_id": self.librenms_id,
                "found_in_librenms": librenms_info.get("found_in_librenms"),
                "librenms_device_details": librenms_info.get("librenms_device_details"),
                "mismatched_device": librenms_info.get("mismatched_device"),
                **librenms_info["librenms_device_details"],
                "interface_name_field": interface_name_field,
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
            "librenms_device_location": "-",
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

                # Update device details regardless of match
                librenms_device_details.update(
                    {
                        "librenms_device_url": f"{self.librenms_api.librenms_url}/device/device={self.librenms_id}/",
                        "librenms_device_hardware": device_info.get("hardware", "-"),
                        "librenms_device_location": device_info.get("location", "-"),
                        "librenms_device_ip": librenms_ip,
                        "sysName": librenms_hostname,
                    }
                )

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
