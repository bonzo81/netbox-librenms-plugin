from collections import namedtuple
from django.views.generic import FormView
from django.urls import reverse_lazy
from dcim.models import Cable, Device, Interface, Site
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django_tables2 import SingleTableView
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.filtersets import SiteLocationFilterSet
from netbox_librenms_plugin.forms import AddToLIbreSNMPV2, AddToLIbreSNMPV3

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.tables import SiteLocationSyncTable
from netbox_librenms_plugin.utils import (
    LIBRENMS_TO_NETBOX_MAPPING,
    convert_speed_to_kbps,
)

from .base_views import CacheMixin, LibreNMSAPIMixin


class SyncInterfacesView(CacheMixin, View):
    """
    Sync selected interfaces from LibreNMS to NetBox for Devices and Virtual Machines.
    """

    def post(self, request, object_type, object_id):
        """
        Handle POST request to sync interfaces.
        """
        # Use the correct URL name based on object type
        url_name = (
            "device_librenms_sync" if object_type == "device" else "vm_librenms_sync"
        )
        obj = self.get_object(object_type, object_id)

        selected_interfaces = self.get_selected_interfaces(request)
        exclude_columns = request.POST.getlist('exclude_columns')

        if selected_interfaces is None:
            return redirect(f"plugins:netbox_librenms_plugin:{url_name}", pk=object_id)

        ports_data = self.get_cached_ports_data(request, obj)
        if ports_data is None:
            return redirect(f"plugins:netbox_librenms_plugin:{url_name}", pk=object_id)

        self.sync_selected_interfaces(obj, selected_interfaces, ports_data, exclude_columns)

        messages.success(request, "Selected interfaces synced successfully.")

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

    def get_selected_interfaces(self, request):
        """
        Retrieve and validate selected interfaces from the request.
        """
        selected_interfaces = request.POST.getlist("select")
        if not selected_interfaces:
            messages.error(request, "No interfaces selected for synchronization.")
            return None

        return selected_interfaces

    def get_cached_ports_data(self, request, obj):
        """
        Retrieve and validate cached ports data.
        """
        cached_data = cache.get(self.get_cache_key(obj, "ports"))
        if not cached_data:
            messages.warning(
                request,
                "No cached data found. Please refresh the data before syncing.",
            )
            return None
        return cached_data.get("ports", [])

    def sync_selected_interfaces(self, obj, selected_interfaces, ports_data, exclude_columns):
        """
        Sync the selected interfaces.
        """
        with transaction.atomic():
            for port in ports_data:
                if port["ifDescr"] in selected_interfaces:
                    self.sync_interface(obj, port, exclude_columns)

    def sync_interface(self, obj, librenms_interface, exclude_columns):
        """
        Sync a single interface from LibreNMS to NetBox.
        """
        if isinstance(obj, Device):
            # Get the selected device ID from POST data
            device_selection_key = f"device_selection_{librenms_interface['ifDescr']}"
            selected_device_id = self.request.POST.get(device_selection_key)

            if selected_device_id:
                target_device = Device.objects.get(id=selected_device_id)
            else:
                target_device = obj

            interface, _ = Interface.objects.get_or_create(
                device=target_device, name=librenms_interface["ifDescr"]
            )
        elif isinstance(obj, VirtualMachine):
            interface, _ = VMInterface.objects.get_or_create(
                virtual_machine=obj, name=librenms_interface["ifDescr"]
            )
        else:
            raise ValueError("Invalid object type.")

        # Determine NetBox interface type (only for devices)
        netbox_type = None
        if isinstance(obj, Device):
            netbox_type = self.get_netbox_interface_type(librenms_interface)

        # Update interface attributes
        self.update_interface_attributes(interface, librenms_interface, netbox_type, exclude_columns)

        if 'enabled' not in exclude_columns:
            interface.enabled = (
                True
                if librenms_interface["ifAdminStatus"] is None
                else (
                    librenms_interface["ifAdminStatus"].lower() == "up"
                    if isinstance(librenms_interface["ifAdminStatus"], str)
                    else bool(librenms_interface["ifAdminStatus"])
                )
            )
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

    def update_interface_attributes(self, interface, librenms_interface, netbox_type, exclude_columns):
        """
        Update the attributes of the NetBox interface based on LibreNMS data.
        """
        # Check if the interface is a Device interface or VM interface
        is_device_interface = isinstance(interface, Interface)

        for librenms_key, netbox_key in LIBRENMS_TO_NETBOX_MAPPING.items():
            if netbox_key in exclude_columns:
                continue

            if librenms_key == "ifSpeed":
                speed = convert_speed_to_kbps(librenms_interface.get(librenms_key))
                setattr(interface, netbox_key, speed)
            elif librenms_key == "ifType":
                # Only set the 'type' attribute if it's a Device interface
                if is_device_interface and hasattr(interface, netbox_key):
                    setattr(interface, netbox_key, netbox_type)
            elif librenms_key == "ifAlias":
                if librenms_interface["ifAlias"] != librenms_interface["ifDescr"]:
                    setattr(interface, netbox_key, librenms_interface[librenms_key])
            else:
                setattr(interface, netbox_key, librenms_interface.get(librenms_key))


class AddDeviceToLibreNMSView(LibreNMSAPIMixin, FormView):
    template_name = "add_device_modal.html"
    success_url = reverse_lazy("device_list")

    def get_form_class(self):
        if self.request.POST.get("snmp_version") == "v2c":
            return AddToLIbreSNMPV2
        return AddToLIbreSNMPV3

    def get_object(self, object_id):
        try:
            return Device.objects.get(pk=object_id)
        except Device.DoesNotExist:
            return VirtualMachine.objects.get(pk=object_id)

    def post(self, request, object_id):
        self.object = self.get_object(object_id)
        form_class = self.get_form_class()
        form = form_class(request.POST)
        if form.is_valid():
            return self.form_valid(form)
        return self.form_invalid(form)

    def form_valid(self, form):
        data = form.cleaned_data
        device_data = {
            'hostname': data.get('hostname'),
            'snmp_version': data.get('snmp_version')
        }

        if device_data['snmp_version'] == 'v2c':
            device_data['community'] = data.get('community')
        elif device_data['snmp_version'] == 'v3':
            device_data.update({
                'authlevel': data.get('authlevel'),
                'authname': data.get('authname'),
                'authpass': data.get('authpass'),
                'authalgo': data.get('authalgo'),
                'cryptopass': data.get('cryptopass'),
                'cryptoalgo': data.get('cryptoalgo')
            })
        else:
            messages.error(self.request, "Unknown SNMP version.")
            return redirect(self.object.get_absolute_url())

        result = self.librenms_api.add_device(device_data)

        if result["success"]:
            messages.success(self.request, result["message"])
        else:
            messages.error(self.request, result["message"])
        return redirect(self.object.get_absolute_url())


class UpdateDeviceLocationView(LibreNMSAPIMixin, View):
    """
    Update the device location in LibreNMS based on the device's site in NetBox.
    """

    def post(self, request, pk):
        """
        Handle the POST request to update the device location in LibreNMS.
        """
        device = get_object_or_404(Device, pk=pk)

        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if device.site:
            librenms_api = self.librenms_api
            field_data = {
                "field": ["location", "override_sysLocation"],
                "data": [device.site.name, "1"],
            }
            success, message = librenms_api.update_device_field(
                self.librenms_id, field_data
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
    filterset = SiteLocationFilterSet

    COORDINATE_TOLERANCE = 0.0001
    SyncData = namedtuple("SyncData", ["netbox_site", "librenms_location", "is_synced"])

    def get_table(self, *args, **kwargs):
        table = super().get_table(*args, **kwargs)
        table.configure(self.request)
        return table

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queryset = self.get_queryset()
        context["filter_form"] = self.filterset(
            self.request.GET, queryset=queryset
        ).form
        return context

    def get_queryset(self):
        netbox_sites = Site.objects.all()
        success, librenms_locations = self.get_librenms_locations()
        if not success or not isinstance(librenms_locations, list):
            return []

        sync_data = [
            self.create_sync_data(site, librenms_locations) for site in netbox_sites
        ]
        # Initialize the filterset correctly
        if self.request.GET and self.filterset:
            return self.filterset(self.request.GET, queryset=sync_data).qs

        # Handle quicksearch
        if "q" in self.request.GET:
            q = self.request.GET.get("q", "").lower()
            sync_data = [
                item for item in sync_data if q in item.netbox_site.name.lower()
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
        except ObjectDoesNotExist:
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


class SyncCablesView(CacheMixin, View):
    """
    View for creating cables in NetBox from LibreNMS data.
    """

    def get_selected_interfaces(self, request, initial_device):
        """
        Retrieve and validate selected interfaces from the request.
        Include device information with each selected interface.
        """
        selected_data = request.POST.getlist("select")
        if not selected_data:
            return None

        # Parse device and interface information from selected data
        selected_interfaces = []

        for interface in selected_data:
            # For VC members, get the device_id from the selection dropdown
            device_id = request.POST.get(f"device_selection_{interface}")
            # For standalone devices, use the original device id
            if not device_id:
                device_id = initial_device.id
            selected_interfaces.append({"device_id": device_id, "interface": interface})
        return selected_interfaces

    def get_cached_links_data(self, request, obj):
        cached_data = cache.get(self.get_cache_key(obj, "links"))
        if not cached_data:
            return None
        return cached_data.get("links", [])

    def create_cable(self, local_interface, remote_device, remote_interface, request):
        try:
            Cable.objects.create(
                a_terminations=[local_interface],
                b_terminations=[remote_interface],
                status="connected",
            )
        except Exception as e:
            messages.error(request, f"Failed to create cable: {str(e)}")

    def check_existing_cable(self, local_interface, remote_interface):
        return Cable.objects.filter(
            Q(terminations__termination_id=local_interface.pk)
            | Q(terminations__termination_id=remote_interface.pk)
        ).exists()

    def validate_prerequisites(self, cached_links, selected_interfaces, device):
        """
        Validates required data before processing cable creation
        """
        if not cached_links:
            messages.error(
                self.request,
                "Cache has expired. Please refresh the cable data before syncing.",
            )
            return False

        if selected_interfaces is None:
            messages.error(self.request, "No interfaces selected for synchronization.")
            return False

        return True

    def display_result_messages(
        self, request, valid_interfaces, invalid_interfaces, duplicate_interfaces
    ):
        """
        Display appropriate messages for cable creation results
        """
        if duplicate_interfaces:
            messages.warning(
                request,
                f"Cable already exist for interfaces: {', '.join(duplicate_interfaces)}",
            )
        if invalid_interfaces:
            messages.error(
                request,
                f"Cannot create cable - device or interface not found in NetBox: {', '.join(invalid_interfaces)}",
            )
        if valid_interfaces:
            messages.success(
                request,
                f"Successfully created cable for interfaces: {', '.join(valid_interfaces)}",
            )

    @transaction.atomic()
    def post(self, request, pk):
        initial_device = get_object_or_404(Device, pk=pk)
        selected_interfaces = self.get_selected_interfaces(request, initial_device)
        cached_links = self.get_cached_links_data(request, initial_device)

        if not self.validate_prerequisites(
            cached_links, selected_interfaces, initial_device
        ):
            return redirect(
                f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}?tab=cables"
            )

        valid_interfaces = []
        invalid_interfaces = []
        duplicate_interfaces = []

        for interface in selected_interfaces:
            try:
                # Find the matching link data from cache
                link_data = next(
                    link
                    for link in cached_links
                    if link["local_port"] == interface["interface"]
                )

                # Get local interface
                local_device = Device.objects.get(pk=interface["device_id"])
                local_interface = local_device.interfaces.get(
                    name=link_data["local_port"]
                )

                # Get remote interface
                remote_device = Device.objects.get(pk=link_data["remote_device_id"])
                remote_interface = remote_device.interfaces.get(
                    pk=link_data["remote_port_id"]
                )

                if self.check_existing_cable(local_interface, remote_interface):
                    duplicate_interfaces.append(interface["interface"])
                    continue

                self.create_cable(
                    local_interface, remote_device, remote_interface, request
                )
                valid_interfaces.append(interface["interface"])

            except (Device.DoesNotExist, Interface.DoesNotExist, StopIteration):
                invalid_interfaces.append(interface["interface"])

        self.display_result_messages(
            request, valid_interfaces, invalid_interfaces, duplicate_interfaces
        )
        return redirect(
            f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}?tab=cables"
        )
