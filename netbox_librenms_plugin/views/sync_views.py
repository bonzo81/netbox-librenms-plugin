from collections import namedtuple

from dcim.models import Device, Interface, Site
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django_tables2 import SingleTableView
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.tables import SiteLocationSyncTable
from netbox_librenms_plugin.utils import (LIBRENMS_TO_NETBOX_MAPPING,
                                          convert_speed_to_kbps)

from .base_views import CacheMixin, LibreNMSAPIMixin


class SyncInterfacesView(CacheMixin, View):
    """
    Sync selected interfaces from LibreNMS to NetBox for Devices and Virtual Machines.
    """

    def post(self, request, object_type, object_id):
        """
        Handle POST request to sync interfaces.
        """
        obj = self.get_object(object_type, object_id)
        selected_interfaces = self.get_selected_interfaces(
            request, object_type, object_id
        )
        ports_data = self.get_cached_ports_data(request, obj)

        self.sync_selected_interfaces(obj, selected_interfaces, ports_data)

        messages.success(request, "Selected interfaces synced successfully.")

        # Use the correct URL name based on object type
        url_name = (
            "device_librenms_sync" if object_type == "device" else "vm_librenms_sync"
        )
        return redirect(f"plugins:netbox_librenms_plugin:{url_name}", pk=object_id)

    def get_object(self, object_type, object_id):
        """
        Retrieve the object (Device or VirtualMachine).
        """
        if object_type == "device":
            return get_object_or_404(Device, pk=object_id)
        elif object_type == "virtualmachine":
            return get_object_or_404(VirtualMachine, pk=object_id)
        else:
            raise Http404("Invalid object type.")

    def get_selected_interfaces(self, request, object_type, object_id):
        """
        Retrieve and validate selected interfaces from the request.
        """
        selected_interfaces = request.POST.getlist("select")
        if not selected_interfaces:
            messages.error(request, "No interfaces selected for synchronization.")

            # Use the correct URL name based on object type
            url_name = (
                "device_librenms_sync"
                if object_type == "device"
                else "vm_librenms_sync"
            )
            return redirect(f"plugins:netbox_librenms_plugin:{url_name}", pk=object_id)

        return selected_interfaces

    def get_cached_ports_data(self, request, obj):
        """
        Retrieve and validate cached ports data.
        """
        cached_data = cache.get(self.get_cache_key(obj))
        if not cached_data:
            print(f"No cached data found for device {obj}")
            messages.warning(
                self.request,
                "No cached data found. Please refresh the data before syncing.",
            )
            return redirect(request.path)
        return cached_data.get("ports", [])

    def sync_selected_interfaces(self, obj, selected_interfaces, ports_data):
        """
        Sync the selected interfaces.
        """
        print(f"port data: {ports_data}")
        with transaction.atomic():
            for port in ports_data:
                if port["ifName"] in selected_interfaces:
                    self.sync_interface(obj, port)

    def sync_interface(self, obj, librenms_interface):
        """
        Sync a single interface from LibreNMS to NetBox.
        """
        if isinstance(obj, Device):
            interface, _ = Interface.objects.get_or_create(
                device=obj, name=librenms_interface["ifName"]
            )
        elif isinstance(obj, VirtualMachine):
            interface, _ = VMInterface.objects.get_or_create(
                virtual_machine=obj, name=librenms_interface["ifName"]
            )
        else:
            raise ValueError("Invalid object type.")

        # Determine NetBox interface type (only for devices)
        netbox_type = None
        if isinstance(obj, Device):
            netbox_type = self.get_netbox_interface_type(librenms_interface)

        # Update interface attributes
        self.update_interface_attributes(interface, librenms_interface, netbox_type)

        interface.enabled = librenms_interface["ifAdminStatus"].lower() == "up"
        interface.save()

    def get_netbox_interface_type(self, librenms_interface):
        """
        Determine the NetBox interface type based on LibreNMS data and mappings.
        """
        speed = convert_speed_to_kbps(librenms_interface["ifSpeed"])
        mappings = InterfaceTypeMapping.objects.filter(
            librenms_type=librenms_interface["ifType"]
        )

        if speed is not None:
            speed_mapping = (
                mappings.filter(librenms_speed__lte=speed)
                .order_by("-librenms_speed")
                .first()
            )
            mapping = (
                speed_mapping or mappings.filter(librenms_speed__isnull=True).first()
            )
        else:
            mapping = mappings.filter(librenms_speed__isnull=True).first()

        return mapping.netbox_type if mapping else "other"

    def update_interface_attributes(self, interface, librenms_interface, netbox_type):
        """
        Update the attributes of the NetBox interface based on LibreNMS data.
        """
        # Check if the interface is a Device interface or VM interface
        is_device_interface = isinstance(interface, Interface)

        for librenms_key, netbox_key in LIBRENMS_TO_NETBOX_MAPPING.items():
            if librenms_key == "ifSpeed":
                speed = convert_speed_to_kbps(librenms_interface.get(librenms_key))
                setattr(interface, netbox_key, speed)
            elif librenms_key == "ifType":
                # Only set the 'type' attribute if it's a Device interface
                if is_device_interface and hasattr(interface, netbox_key):
                    setattr(interface, netbox_key, netbox_type)
            elif librenms_key == "ifAlias":
                if librenms_interface["ifAlias"] != librenms_interface["ifName"]:
                    setattr(interface, netbox_key, librenms_interface[librenms_key])
            else:
                setattr(interface, netbox_key, librenms_interface.get(librenms_key))


class AddDeviceToLibreNMSView(LibreNMSAPIMixin, View):
    """
    Handle adding a device to LibreNMS.
    """

    def post(self, request, object_type, object_id):
        """
        Handle the submission of the form to add a device to LibreNMS.
        """
        hostname = request.POST.get("hostname")
        community = request.POST.get("community")
        version = request.POST.get("version")

        result, message = self.librenms_api.add_device(hostname, community, version)

        if result:
            messages.success(
                request,
                "Device added successfully to LibreNMS. Allow time for discovery & polling",
            )
        else:
            messages.error(request, f"Error adding device to LibreNMS: {message}")

        # Use the correct URL name based on object type
        url_name = (
            "device_librenms_sync" if object_type == "device" else "vm_librenms_sync"
        )
        return redirect(f"plugins:netbox_librenms_plugin:{url_name}", pk=object_id)


class UpdateDeviceLocationView(LibreNMSAPIMixin, View):
    """
    Update the device location in LibreNMS based on the device's site in NetBox.
    """

    def post(self, request, pk):
        """
        Handle the POST request to update the device location in LibreNMS.
        """
        device = get_object_or_404(Device, pk=pk)
        if device.site:
            librenms_api = self.librenms_api
            field_data = {
                "field": ["location", "override_sysLocation"],
                "data": [device.site.name, "1"],
            }
            success, message = librenms_api.update_device_field(
                str(device.primary_ip.address.ip), field_data
            )

            if success:
                messages.success(
                    request,
                    f"Device location updated in LibreNMS to {device.site.name}",
                )
            else:
                messages.error(
                    request, f"Failed to update device location in LibreNMS: {message}"
                )
        else:
            messages.warning(request, "Device has no associated site in NetBox")

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class SyncSiteLocationView(LibreNMSAPIMixin, SingleTableView):
    """
    Provides a view for synchronizing Netbox site with LibreNMS locations.
    """

    table_class = SiteLocationSyncTable
    template_name = "netbox_librenms_plugin/site_location_sync.html"
    paginate_by = 25

    COORDINATE_TOLERANCE = 0.0001
    SyncData = namedtuple("SyncData", ["netbox_site", "librenms_location", "is_synced"])

    def get_queryset(self):
        netbox_sites = Site.objects.all()
        success, librenms_locations = self.get_librenms_locations()
        if not success or not isinstance(librenms_locations, list):
            return []

        sync_data = [
            self.create_sync_data(site, librenms_locations) for site in netbox_sites
        ]
        return sync_data

    def get_librenms_locations(self):
        """
        Retrieve locations from LibreNMS.
        """
        return self.librenms_api.get_locations()

    def create_sync_data(self, site, librenms_locations):
        """
        Create a SyncData object for a given site.
        """
        matched_location = self.match_site_with_location(site, librenms_locations)
        if matched_location:
            is_synced = self.check_coordinates_match(
                site.latitude,
                site.longitude,
                matched_location.get("lat"),
                matched_location.get("lng"),
            )
            return self.SyncData(site, matched_location, is_synced)
        else:
            return self.SyncData(site, None, False)

    def match_site_with_location(self, site, librenms_locations):
        """
        Match a NetBox site with a LibreNMS location.
        """
        for location in librenms_locations:
            if location["location"].lower() == site.name.lower():
                return location
        return None

    def check_coordinates_match(self, site_lat, site_lng, librenms_lat, librenms_lng):
        """
        Check if the coordinates of the site and LibreNMS location match.
        """
        if None in (site_lat, site_lng, librenms_lat, librenms_lng):
            return False
        lat_match = (
            abs(float(site_lat) - float(librenms_lat)) < self.COORDINATE_TOLERANCE
        )
        lng_match = (
            abs(float(site_lng) - float(librenms_lng)) < self.COORDINATE_TOLERANCE
        )
        return lat_match and lng_match

    def post(self, request):
        """
        Handle the POST request for synchronizing Netbox site with LibreNMS locations.
        """
        action = request.POST.get("action")
        pk = request.POST.get("pk")
        if not pk:
            messages.error(request, "No site ID provided.")
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

        site = self.get_site_by_pk(pk)
        if not site:
            messages.error(request, "Site not found.")
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

        if action == "update":
            return self.update_librenms_location(request, site)
        elif action == "create":
            return self.create_librenms_location(request, site)
        else:
            messages.error(request, f"Unknown action '{action}'.")
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

    def get_site_by_pk(self, pk):
        """
        Retrieve a Site object by its primary key.
        """
        try:
            return Site.objects.get(pk=pk)
        except Site.DoesNotExist:
            return None

    def create_librenms_location(self, request, site):
        """
        Create a new location in LibreNMS based on the site's coordinates.
        """
        location_data = self.build_location_data(site)
        success, message = self.librenms_api.add_location(location_data)
        if success:
            messages.success(
                request, f"Location '{site.name}' created in LibreNMS successfully."
            )
        else:
            messages.error(
                request,
                f"Failed to create location '{site.name}' in LibreNMS: {message}",
            )
        return redirect("plugins:netbox_librenms_plugin:site_location_sync")

    def update_librenms_location(self, request, site):
        """
        Update LibreNMS api with the site's updated coordinates.
        """
        if site.latitude is None or site.longitude is None:
            messages.warning(
                request,
                f"Latitude and/or longitude is missing. Cannot update location '{site.name}' in LibreNMS.",
            )
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

        location_data = self.build_location_data(site, include_name=False)
        success, message = self.librenms_api.update_location(site.name, location_data)
        if success:
            messages.success(
                request, f"Location '{site.name}' updated in LibreNMS successfully."
            )
        else:
            messages.error(
                request,
                f"Failed to update location '{site.name}' in LibreNMS: {message}",
            )
        return redirect("plugins:netbox_librenms_plugin:site_location_sync")

    def build_location_data(self, site, include_name=True):
        """
        Build the location data for a given site object.
        """
        data = {"lat": str(site.latitude), "lng": str(site.longitude)}
        if include_name:
            data["location"] = site.name
        return data
