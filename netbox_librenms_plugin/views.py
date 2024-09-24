from django.views import View
from django.core.cache import cache
from django.utils import timezone
from django.db import transaction
from django.contrib import messages
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.db.models import Q
from django.core.paginator import Paginator
from django_tables2 import SingleTableView
from collections import namedtuple

from utilities.htmx import htmx_partial
from dcim.models import Device, Interface, Site
from netbox.views import generic
from utilities.views import ViewTab, register_model_view

from .tables import LibreNMSInterfaceTable, InterfaceTypeMappingTable, SiteLocationSyncTable
from .models import InterfaceTypeMapping
from .forms import InterfaceTypeMappingForm, InterfaceTypeMappingFilterForm
from .filters import InterfaceTypeMappingFilterSet
from .utils import convert_speed_to_kbps, LIBRENMS_TO_NETBOX_MAPPING
from .librenms_api import LibreNMSAPI


@register_model_view(Device, name='device_interfaces_sync', path='librenms-sync')
class DeviceInterfacesSyncView(generic.ObjectListView):
    """
    Sync view for interfaces from LibreNMS to NetBox, as a tab on the device view.
    """
    queryset = Device.objects.none()
    tab = ViewTab(
        label='LibreNMS Sync',
        permission='dcim.view_device'
    )

    template_name = 'netbox_librenms_plugin/interface_sync.html'

    def get_interface_type_mappings(self):
        """
        Get all interface type mappings.
        """
        mappings = {mapping.librenms_type: mapping.netbox_type for mapping in InterfaceTypeMapping.objects.all()}
        return mappings

    def _prepare_context(self, device, cached_data=None, last_fetched=None):
        ports_data = []
        table = None
        device_exists = False
        librenms_device_id = None
        librenms_device_url = None

        if cached_data:
            ports_data = cached_data.get('ports', [])
            netbox_interfaces = device.interfaces.all()

            for port in ports_data:
                port['enabled'] = port['ifAdminStatus'].lower() == 'up' if isinstance(port['ifAdminStatus'], str) else bool(port['ifAdminStatus'])
                netbox_interface = netbox_interfaces.filter(name=port['ifName']).first()
                port['exists_in_netbox'] = bool(netbox_interface)
                port['netbox_interface'] = netbox_interface

            table = LibreNMSInterfaceTable(ports_data)
            table.configure(self.request)

        if device.primary_ip:
            device_exists, librenms_device_data = LibreNMSAPI().get_device_info(str(device.primary_ip.address.ip))
            if device_exists:
                librenms_device_id = librenms_device_data.get('device_id')
                librenms_device_hardware = librenms_device_data.get('hardware')
                librenms_device_location = librenms_device_data.get('location')
                librenms_device_url = f"{LibreNMSAPI().librenms_url}/device/device={librenms_device_id}/"

        cache_timeout = cache.ttl(f'librenms_ports_{device.pk}')
        cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_timeout)

        return {
            "tab": self.tab,
            "object": device,
            "table": table,
            "last_fetched": last_fetched,
            "cache_expiry": cache_expiry,
            "has_primary_ip": bool(device.primary_ip),
            'device_in_librenms': device_exists,
            'librenms_device_id': librenms_device_id,
            'librenms_device_url': librenms_device_url,
            'librenms_device_hardware': librenms_device_hardware,
            'librenms_device_location': librenms_device_location,
            }

    def get(self, request, pk):
        device = get_object_or_404(Device, pk=pk)
        cached_data = cache.get(f'librenms_ports_{device.pk}')
        last_fetched = cache.get(f'librenms_ports_last_fetched_{device.pk}')

        context = self._prepare_context(device, cached_data, last_fetched)
        return render(request, self.template_name, context)

    def post(self, request, pk):
        device = get_object_or_404(Device, pk=pk)
        if not device.primary_ip:
            messages.error(request, "This device has no primary IP set. Unable to fetch data from LibreNMS.")
            return redirect('plugins:netbox_librenms_plugin:device_interfaces_sync', pk=pk)

        librenms_api = LibreNMSAPI()
        ip_address = str(device.primary_ip.address.ip)

        librenms_data = librenms_api.get_ports(ip_address)

        if 'error' in librenms_data:
            messages.error(request, librenms_data['error'])
            return redirect('plugins:netbox_librenms_plugin:device_interfaces_sync', pk=pk)

        cache.set(f'librenms_ports_{device.pk}', librenms_data, timeout=300)
        last_fetched = timezone.now()
        cache.set(f'librenms_ports_last_fetched_{device.pk}', last_fetched, timeout=300)

        context = self._prepare_context(device, librenms_data, last_fetched)
        return render(request, self.template_name, context)


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
        selected_interfaces = request.POST.getlist('selection')

        if not selected_interfaces:
            messages.error(request, "No interfaces selected for synchronization.")
            return redirect('plugins:netbox_librenms_plugin:device_interfaces_sync', pk=device_id)

        # Retrieve the cached data
        cached_data = cache.get(f'librenms_ports_{device.pk}')

        if not cached_data:
            messages.warning(request, "No cached data found. Please refresh the data before syncing.")
            return redirect('plugins:netbox_librenms_plugin:device_interfaces_sync', pk=device_id)

        ports_data = cached_data.get('ports', [])

        with transaction.atomic():
            for port in ports_data:
                if port['ifName'] in selected_interfaces:
                    self.sync_interface(device, port)

        messages.success(request, "Selected interfaces synced successfully.")
        return redirect('plugins:netbox_librenms_plugin:device_interfaces_sync', pk=device_id)

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
        mapping = InterfaceTypeMapping.objects.filter(librenms_type=librenms_interface['ifType']).first()
        netbox_type = mapping.netbox_type if mapping else 'other'

        for librenms_key, netbox_key in LIBRENMS_TO_NETBOX_MAPPING.items():
            if librenms_key == 'ifSpeed':
                setattr(interface, netbox_key, convert_speed_to_kbps(librenms_interface[librenms_key]))
            elif librenms_key == 'ifType':
                setattr(interface, netbox_key, netbox_type)
            else:
                setattr(interface, netbox_key, librenms_interface[librenms_key])

        interface.enabled = librenms_interface['ifAdminStatus'].lower() == 'up'
        interface.save()


def add_device_modal(request, pk):
    device = get_object_or_404(Device, pk=pk)
    if htmx_partial(request):
        return render(request, 'netbox_librenms_plugin/htmx/add_device_form.html', {
            'device': device,
            'form_url': reverse('plugins:netbox_librenms_plugin:add_device_to_librenms'),
        })
    return render(request, 'netbox_librenms_plugin/add_device_modal.html', {'device': device})


def add_device_to_librenms(request):
    if request.method == 'POST':
        hostname = request.POST.get('hostname')
        community = request.POST.get('community')
        version = request.POST.get('version')
        site = request.POST.get('site')

        librenms_api = LibreNMSAPI()
        result, message = librenms_api.add_device(hostname, community, version, site)

        if result:
            messages.success(request, 'Device added successfully to LibreNMS')
        else:
            messages.error(request, f'Error adding device to LibreNMS: {message}')

                # Return a response that closes the modal and triggers a page refresh
        # Redirect to the LibreNMS sync page
        return redirect(reverse('plugins:netbox_librenms_plugin:device_interfaces_sync', kwargs={'pk': request.POST.get('device_id')}))


class InterfaceTypeMappingListView(generic.ObjectListView):
    """
        Provides a view for listing all `InterfaceTypeMapping` objects.
    """
    queryset = InterfaceTypeMapping.objects.all()
    table = InterfaceTypeMappingTable
    filterset = InterfaceTypeMappingFilterSet
    filterset_form = InterfaceTypeMappingFilterForm
    template_name = 'netbox_librenms_plugin/interfacetypemapping_list.html'


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
    template_name = 'netbox_librenms_plugin/interfacetypemapping.html'


class InterfaceTypeMappingEditView(generic.ObjectEditView):
    """
    Provides a view for editing a specific `InterfaceTypeMapping` object.
    """
    queryset = InterfaceTypeMapping.objects.all()
    form = InterfaceTypeMappingForm
    template_name = 'netbox_librenms_plugin/interfacetypemapping_edit.html'


class InterfaceTypeMappingDeleteView(generic.ObjectDeleteView):
    """
    Provides a view for deleting a specific `InterfaceTypeMapping` object.
    """
    queryset = InterfaceTypeMapping.objects.all()
    template_name = 'netbox_librenms_plugin/interfacetypemapping_delete.html'


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
    template_name = 'netbox_librenms_plugin/interfacetypemapping_changelog.html'


class SiteLocationSyncView(SingleTableView):
    table_class = SiteLocationSyncTable
    template_name = 'netbox_librenms_plugin/site_location_sync.html'
    paginate_by = 25

    def get_queryset(self):
        netbox_sites = Site.objects.all()
        librenms_api = LibreNMSAPI()
        success, librenms_locations = librenms_api.get_locations()
        SyncData = namedtuple('SyncData', ['netbox_site', 'librenms_location', 'is_synced'])
        sync_data = []

        COORDINATE_TOLERANCE = 0.0001

        if success and isinstance(librenms_locations, list):
            for site in netbox_sites:
                matched_location = next((loc for loc in librenms_locations if loc['location'].lower() == site.name.lower()), None)
                if matched_location:
                    lat_match = lng_match = False
                    if site.latitude is not None and site.longitude is not None:
                        lat_match = abs(float(site.latitude) - float(matched_location.get('lat', 0))) < COORDINATE_TOLERANCE
                        lng_match = abs(float(site.longitude) - float(matched_location.get('lng', 0))) < COORDINATE_TOLERANCE

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

    def post(self, request, *args, **kwargs):
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
        librenms_api = LibreNMSAPI()
        location_data = {
            "location": site.name,
            "lat": str(site.latitude),
            "lng": str(site.longitude)
        }
        success, message = librenms_api.add_location(location_data)

        if success:
            messages.success(request, f"Location '{site.name}' created in LibreNMS successfully.")
        else:
            messages.error(request, f"Failed to create location '{site.name}' in LibreNMS: {message}")

        return redirect('plugins:netbox_librenms_plugin:site_location_sync')

    def update_librenms_location(self, request, site):

        if site.latitude is None or site.longitude is None:
            messages.warning(request, f"Latitude and/or longitude is missing. Cannot update location '{site.name}' in LibreNMS.")
            return redirect('plugins:netbox_librenms_plugin:site_location_sync')

        librenms_api = LibreNMSAPI()
        location_data = {
            "lat": str(site.latitude),
            "lng": str(site.longitude)
        }
        success, message = librenms_api.update_location(site.name, location_data)

        if success:
            messages.success(request, f"Location '{site.name}' updated in LibreNMS successfully.")
        else:
            messages.error(request, f"Failed to update location '{site.name}' in LibreNMS: {message}")

        return redirect('plugins:netbox_librenms_plugin:site_location_sync')
