from dcim.models import Device
from django.shortcuts import redirect
from utilities.views import ViewTab, register_model_view

from .base_views import (
    BaseCableSyncTableView,
    BaseInterfaceSyncTableView,
    BaseIPAddressSyncTableView,
    BaseLibreNMSSyncView,
)


@register_model_view(Device, name='librenms_sync', path='librenms-sync')
class DeviceLibreNMSSyncView(BaseLibreNMSSyncView):
    """
    View for devices page with LibreNMS sync information.
    """
    queryset = Device.objects.all()
    model = Device
    tab = ViewTab(
        label='LibreNMS Sync',
        permission='dcim.view_device'
    )
    
    def get_interface_context(self, request, obj):
        """
        Get the context data for interface sync for devices.
        """
        interface_sync_view = DeviceInterfaceTableView()
        return interface_sync_view.get_context_data(request, obj)

    def get_cable_context(self, request, obj):
        """
        Get the context data for cable sync for devices.
        """
        cable_sync_view = DeviceCableTableView()
        return cable_sync_view.get_context_data(request, obj)

    def get_ip_context(self, request, obj):
        """
        Get the context data for IP address sync for devices.
        """
        ipaddress_sync_view = DeviceIPAddressTableView()
        return ipaddress_sync_view.get_context_data(request, obj)


class DeviceInterfaceTableView(BaseInterfaceSyncTableView):
    """
    View for device interface synchronization.
    """
    model = Device

    def get_interfaces(self, obj):
        return obj.interfaces.all()

    def get_redirect_url(self, obj):
        return redirect('plugins:netbox_librenms_plugin:device_interface_sync', pk=obj.pk)


class DeviceCableTableView(BaseCableSyncTableView):
    """
    View for device cable synchronization.
    """
    model = Device
    # Device-specific implementations


class DeviceIPAddressTableView(BaseIPAddressSyncTableView):
    """
    View for device IP address synchronization.
    """
    model = Device
    # Device-specific implementations
