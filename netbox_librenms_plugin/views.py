# Python imports
from collections import namedtuple

# Django imports
from django.views import View
from django.core.cache import cache
from django.utils import timezone
from django.db import transaction
from django.contrib import messages
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django_tables2 import SingleTableView

# NetBox imports
from utilities.htmx import htmx_partial
from utilities.views import ViewTab, register_model_view
from dcim.models import Device, Interface, Site
from netbox.views import generic

# Plugin imports
from .tables import LibreNMSInterfaceTable, InterfaceTypeMappingTable, SiteLocationSyncTable
from .models import InterfaceTypeMapping
from .forms import InterfaceTypeMappingForm, InterfaceTypeMappingFilterForm
from .filters import InterfaceTypeMappingFilterSet
from .utils import convert_speed_to_kbps, LIBRENMS_TO_NETBOX_MAPPING
from .librenms_api import LibreNMSAPI


class LibreNMSAPIMixin:
    """
    A mixin class that provides access to the LibreNMS API.

    This mixin initializes a LibreNMSAPI instance and provides a property
    to access it. It's designed to be used with other view classes that
    need to interact with the LibreNMS API.

    Attributes:
        _librenms_api (LibreNMSAPI): An instance of the LibreNMSAPI class.

    Properties:
        librenms_api (LibreNMSAPI): A property that returns the LibreNMSAPI instance,
                                    creating it if it doesn't exist.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._librenms_api = None

    @property
    def librenms_api(self):
        """
        Get or create an instance of LibreNMSAPI.

        This property ensures that only one instance of LibreNMSAPI is created
        and reused for subsequent calls.

        Returns:
            LibreNMSAPI: An instance of the LibreNMSAPI class.
        """
        if self._librenms_api is None:
            self._librenms_api = LibreNMSAPI()
        return self._librenms_api


class InterfaceSyncView(LibreNMSAPIMixin, View):
    """
    View for fetching interface data from LibreNMS (post) and generateing table data (get_context_data).
    """
    partial_template_name = 'netbox_librenms_plugin/_interface_sync_content.html'

    def post(self, request, device_id):
        """
        Handle POST request to fetch and cache LibreNMS interface data for a device.
        """
        device = get_object_or_404(Device, pk=device_id)

        if not device.primary_ip:
            messages.error(request, "This device has no primary IP set. Unable to fetch data from LibreNMS.")
            return redirect('plugins:netbox_librenms_plugin:interface_sync', device_id=device_id)

        ip_address = str(device.primary_ip.address.ip)

        librenms_data = self.librenms_api.get_ports(ip_address)

        if 'error' in librenms_data:
            messages.error(request, librenms_data['error'])
            return redirect('plugins:netbox_librenms_plugin:interface_sync', device_id=device_id)

        # Store data in cache
        cache.set(f'librenms_ports_{device.pk}', librenms_data, timeout=300)
        last_fetched = timezone.now()
        cache.set(f'librenms_ports_last_fetched_{device.pk}', last_fetched, timeout=300)

        messages.success(request, "Data refreshed successfully.")

        context = self.get_context_data(request, device)
        context = {'interface_sync': context}

        return render(request, self.partial_template_name, context)

    def get_context_data(self, request, device):
        """
        Get the context data for the interface sync view.
        """
        ports_data = []
        table = None

        cached_data = cache.get(f'librenms_ports_{device.pk}')

        last_fetched = cache.get(f'librenms_ports_last_fetched_{device.pk}')

        if cached_data:
            ports_data = cached_data.get('ports', [])
            netbox_interfaces = device.interfaces.all()

            for port in ports_data:
                port['enabled'] = (
                    port['ifAdminStatus'].lower() == 'up'
                    if isinstance(port['ifAdminStatus'], str)
                    else bool(port['ifAdminStatus'])
                )
                netbox_interface = netbox_interfaces.filter(name=port['ifName']).first()
                port['exists_in_netbox'] = bool(netbox_interface)
                port['netbox_interface'] = netbox_interface

                # Add this check to ignore when description is the same as interface name
                if port['ifAlias'] == port['ifName']:
                    port['ifAlias'] = ''

            table = LibreNMSInterfaceTable(ports_data)
            table.configure(request)

        cache_timeout = cache.ttl(f'librenms_ports_{device.pk}')
        cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_timeout)

        return {
            "object": device,
            "table": table,
            "last_fetched": last_fetched,
            "cache_expiry": cache_expiry,
        }


class CableSyncView(LibreNMSAPIMixin, View):
    """
    View for synchronizing cable information from LibreNMS.
    """
    template_name = 'netbox_librenms_plugin/_cable_sync.html'

    def get_context_data(self, request, device):
        """
        Get context data for cable sync view.
        """
        context = {
            'device': device,
            'cable_sync_message': 'Cable sync coming soon',
        }
        return context


class IPAddressSyncView(LibreNMSAPIMixin, View):
    """
    View for synchronizing IP address information from LibreNMS.
    """
    template_name = 'netbox_librenms_plugin/_ipaddress_sync.html'

    def get_context_data(self, request, device):
        """
        Get context data for IP address sync view.
        """
        context = {
            'device': device,
            'ip_sync_message': 'IP address sync coming soon',
        }
        return context


@register_model_view(Device, name='librenms_sync', path='librenms-sync')
class DeviceLibreNMSSyncView(LibreNMSAPIMixin, generic.ObjectListView):
    """
    Base view for devices page with LibreNMS sync information.
    """
    queryset = Device.objects.none()

    template_name = 'netbox_librenms_plugin/librenms_sync_base.html'
    tab = ViewTab(
        label='LibreNMS Sync',
        permission='dcim.view_device'
    )

    def get(self, request, pk, context=None):
        """
        Handle GET request for the Device Librenms sync view.
        """
        device = get_object_or_404(Device, pk=pk)
        context = self.get_context_data(request, device)
        return render(request, self.template_name, context)

    def get_context_data(self, request, device):
        """
        Get the context data for the Device Librenms sync view.
        """
        device_exists = False
        librenms_device_info = {
            'librenms_device_id': None,
            'librenms_device_url': None,
            'librenms_device_hardware': "-",
            'librenms_device_location': "-",
        }

        if device.primary_ip:
            device_exists, librenms_device_data = self.librenms_api.get_device_info(
                str(device.primary_ip.address.ip)
            )
            if device_exists:
                librenms_device_info.update({
                    'librenms_device_id': librenms_device_data.get('device_id'),
                    'librenms_device_hardware': librenms_device_data.get('hardware'),
                    'librenms_device_location': librenms_device_data.get('location'),
                })
                librenms_device_info['librenms_device_url'] = (
                    f"{self.librenms_api.librenms_url}/device/device="
                    f"{librenms_device_info['librenms_device_id']}/"
                )

        # Get contexts from helper methods
        interface_context = self.get_interface_context(request, device)
        cable_context = self.get_cable_context(request, device)
        ip_context = self.get_ip_context(request, device)

        context = {
            'object': device,
            'tab': self.tab,
            'has_primary_ip': bool(device.primary_ip),
            'device_in_librenms': device_exists,
            'interface_sync': interface_context,
            'cable_sync': cable_context,
            'ip_sync': ip_context,
            **librenms_device_info,
        }
        return context

    def get_interface_context(self, request, device):
        """
        Get the context data for interface sync.
        """
        interface_sync_view = InterfaceSyncView()
        return interface_sync_view.get_context_data(request, device)

    def get_cable_context(self, request, device):
        """
        Get the context data for cable sync.
        """
        cable_sync_view = CableSyncView()
        return cable_sync_view.get_context_data(request, device)

    def get_ip_context(self, request, device):
        """
        Get the context data for IP address sync.
        """
        ipaddress_sync_view = IPAddressSyncView()
        return ipaddress_sync_view.get_context_data(request, device)


class SyncInterfacesView(View):
    """
    Sync selected interfaces from LibreNMS to NetBox.
    """
    def post(self, request, device_id):
        """
        Check selected interfaces and sync them to NetBox.
        Check if the data is cached, if not, display a warning message.
        Redirect back to the sync page after synchronization.
        """

        device = get_object_or_404(Device, pk=device_id)
        selected_interfaces = request.POST.getlist('select')

        if not selected_interfaces:
            messages.error(request, "No interfaces selected for synchronization.")
            return redirect('plugins:netbox_librenms_plugin:librenms_sync', pk=device_id)

        # Retrieve the cached data
        cached_data = cache.get(f'librenms_ports_{device.pk}')

        if not cached_data:
            messages.warning(request, "No cached data found. Please refresh the data before syncing.")
            return redirect('plugins:netbox_librenms_plugin:librenms_sync', pk=device_id)

        ports_data = cached_data.get('ports', [])

        with transaction.atomic():
            for port in ports_data:
                if port['ifName'] in selected_interfaces:
                    self.sync_interface(device, port)

        messages.success(request, "Selected interfaces synced successfully.")
        return redirect('plugins:netbox_librenms_plugin:librenms_sync', pk=device_id)

    @staticmethod
    def sync_interface(device, librenms_interface):
        """
        Sync a single interface from LibreNMS to NetBox.
        """
        interface, created = Interface.objects.get_or_create(
            device=device,
            name=librenms_interface['ifName']
        )

        # Fetch the corresponding NetBox interface type
        speed = convert_speed_to_kbps(librenms_interface['ifSpeed'])
        mappings = InterfaceTypeMapping.objects.filter(librenms_type=librenms_interface['ifType'])

        if speed is not None:
            speed_mapping = mappings.filter(librenms_speed__lte=speed).order_by('-librenms_speed').first()
            if speed_mapping:
                mapping = speed_mapping
            else:
                mapping = mappings.filter(librenms_speed__isnull=True).first()
        else:
            mapping = mappings.filter(librenms_speed__isnull=True).first()

        netbox_type = mapping.netbox_type if mapping else 'other'

        for librenms_key, netbox_key in LIBRENMS_TO_NETBOX_MAPPING.items():
            if librenms_key == 'ifSpeed' and not None:
                speed = convert_speed_to_kbps(librenms_interface[librenms_key])
                setattr(interface, netbox_key, speed)
            elif librenms_key == 'ifType':
                setattr(interface, netbox_key, netbox_type)
            elif librenms_key == 'ifAlias':
                if librenms_interface['ifAlias'] != librenms_interface['ifName']:
                    setattr(interface, netbox_key, librenms_interface[librenms_key])
            else:
                setattr(interface, netbox_key, librenms_interface[librenms_key])
        interface.enabled = librenms_interface['ifAdminStatus'].lower() == 'up'
        interface.save()


class AddDeviceToLibreNMSView(LibreNMSAPIMixin, View):
    """
    Handle adding a device to LibreNMS, both displaying the form and processing the submission.
    """
    def get(self, request, pk):
        """
        Display the form to add a device to LibreNMS.
        """
        device = get_object_or_404(Device, pk=pk)
        if htmx_partial(request):
            return render(request, 'netbox_librenms_plugin/htmx/add_device_form.html', {
                'device': device,
                'form_url': reverse('plugins:netbox_librenms_plugin:add_device_to_librenms', kwargs={'pk': pk}),
            })
        return render(request, 'netbox_librenms_plugin/add_device_modal.html', {'device': device})

    def post(self, request, pk):
        """
        Handle the submission of the form to add a device to LibreNMS.
        """
        device = get_object_or_404(Device, pk=pk)
        hostname = request.POST.get('hostname')
        community = request.POST.get('community')
        version = request.POST.get('version')

        librenms_api = self.librenms_api
        result, message = librenms_api.add_device(hostname, community, version)

        if result:
            messages.success(request, 'Device added successfully to LibreNMS. Allow time for discovery & polling')
        else:
            messages.error(request, f'Error adding device to LibreNMS: {message}')

        return redirect(reverse('plugins:netbox_librenms_plugin:librenms_sync', kwargs={'pk': device.pk}))


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
                "data": [device.site.name, "1"]
            }
            success, message = librenms_api.update_device_field(str(device.primary_ip.address.ip), field_data)

            if success:
                messages.success(request, f"Device location updated in LibreNMS to {device.site.name}")
            else:
                messages.error(request, f"Failed to update device location in LibreNMS: {message}")
        else:
            messages.warning(request, "Device has no associated site in NetBox")

        return redirect('plugins:netbox_librenms_plugin:librenms_sync', pk=pk)


class InterfaceTypeMappingListView(generic.ObjectListView):
    """
    Provides a view for listing all `InterfaceTypeMapping` objects.
    """
    queryset = InterfaceTypeMapping.objects.all()
    table = InterfaceTypeMappingTable
    filterset = InterfaceTypeMappingFilterSet
    filterset_form = InterfaceTypeMappingFilterForm


class InterfaceTypeMappingCreateView(generic.ObjectEditView):
    """
    Provides a view for creating a new `InterfaceTypeMapping` object.
    """
    queryset = InterfaceTypeMapping.objects.all()
    form = InterfaceTypeMappingForm


class InterfaceTypeMappingView(generic.ObjectView):
    """
    Provides a view for displaying details of a specific `InterfaceTypeMapping` object.
    """
    queryset = InterfaceTypeMapping.objects.all()


class InterfaceTypeMappingEditView(generic.ObjectEditView):
    """
    Provides a view for editing a specific `InterfaceTypeMapping` object.
    """
    queryset = InterfaceTypeMapping.objects.all()
    form = InterfaceTypeMappingForm


class InterfaceTypeMappingDeleteView(generic.ObjectDeleteView):
    """
    Provides a view for deleting a specific `InterfaceTypeMapping` object.
    """
    queryset = InterfaceTypeMapping.objects.all()


class InterfaceTypeMappingBulkDeleteView(generic.BulkDeleteView):
    """
    Provides a view for deleting multiple `InterfaceTypeMapping` objects.
    """
    queryset = InterfaceTypeMapping.objects.all()
    table = InterfaceTypeMappingTable


class InterfaceTypeMappingChangeLogView(generic.ObjectChangeLogView):
    """
    Provides a view for displaying the change log of a specific `InterfaceTypeMapping` object.
    """
    queryset = InterfaceTypeMapping.objects.all()


class SiteLocationSyncView(LibreNMSAPIMixin, SingleTableView):
    """
    Provides a view for synchronizing site locations with LibreNMS.
    """
    table_class = SiteLocationSyncTable
    template_name = 'netbox_librenms_plugin/site_location_sync.html'
    paginate_by = 25

    def get_queryset(self):
        """
        Returns the queryset of Netbox sites to be checked with LibreNMS.
        """
        netbox_sites = Site.objects.all()
        success, librenms_locations = self.librenms_api.get_locations()
        SyncData = namedtuple('SyncData', ['netbox_site', 'librenms_location', 'is_synced'])
        sync_data = []

        COORDINATE_TOLERANCE = 0.0001

        if success and isinstance(librenms_locations, list):
            for site in netbox_sites:
                matched_location = next(
                    (loc for loc in librenms_locations if loc['location'].lower() == site.name.lower()),
                    None
                )
                if matched_location:
                    lat_match = lng_match = False
                    if site.latitude is not None and site.longitude is not None:
                        librenms_lat = matched_location.get('lat')
                        librenms_lng = matched_location.get('lng')
                        if librenms_lat is not None and librenms_lng is not None:
                            lat_match = abs(float(site.latitude) - float(librenms_lat)) < COORDINATE_TOLERANCE
                            lng_match = abs(float(site.longitude) - float(librenms_lng)) < COORDINATE_TOLERANCE

                    is_synced = lat_match and lng_match

                    sync_data.append(SyncData(
                        netbox_site=site,
                        librenms_location=matched_location,
                        is_synced=is_synced
                    ))
                else:
                    sync_data.append(SyncData(
                        netbox_site=site,
                        librenms_location=None,
                        is_synced=False
                    ))

        return sync_data

    def post(self, request):
        """
        Handles the POST request for synchronizing Netbox site with LibreNMS locations.
        """
        action = request.POST.get('action')
        pk = request.POST.get('pk')
        if not pk:
            messages.error(request, "No site ID provided.")
            return redirect('plugins:netbox_librenms_plugin:site_location_sync')

        site = get_object_or_404(Site, pk=pk)

        if action == 'update':
            return self.update_librenms_location(request, site)
        elif action == 'create':
            return self.create_librenms_location(request, site)

    def create_librenms_location(self, request, site):
        """
        Creates a new location in LibreNMS based on the site's coordinates.
        """
        location_data = {
            "location": site.name,
            "lat": str(site.latitude),
            "lng": str(site.longitude)
        }
        success, message = self.librenms_api.add_location(location_data)

        if success:
            messages.success(request, f"Location '{site.name}' created in LibreNMS successfully.")
        else:
            messages.error(request, f"Failed to create location '{site.name}' in LibreNMS: {message}")

        return redirect('plugins:netbox_librenms_plugin:site_location_sync')

    def update_librenms_location(self, request, site):
        """
        Updates an existing location in LibreNMS based on the site's coordinates.
        """
        if site.latitude is None or site.longitude is None:
            messages.warning(request, f"Latitude and/or longitude is missing. "
                             f"Cannot update location '{site.name}' in LibreNMS.")
            return redirect('plugins:netbox_librenms_plugin:site_location_sync')

        location_data = {
            "lat": str(site.latitude),
            "lng": str(site.longitude)
        }
        success, message = self.librenms_api.update_location(site.name, location_data)

        if success:
            messages.success(request, f"Location '{site.name}' updated in LibreNMS successfully.")
        else:
            messages.error(request, f"Failed to update location '{site.name}' in LibreNMS: {message}")

        return redirect('plugins:netbox_librenms_plugin:site_location_sync')
