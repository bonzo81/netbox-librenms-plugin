import json

from dcim.models import Device
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View
from utilities.views import ViewTab, register_model_view

from netbox_librenms_plugin.tables import (
    LibreNMSCableTable,
    LibreNMSInterfaceTable,
    VCCableTable,
    VCInterfaceTable,
)

from .base_views import (
    BaseCableTableView,
    BaseInterfaceTableView,
    BaseIPAddressTableView,
    BaseLibreNMSSyncView,
    CacheMixin,
)
from .base_views import (
    BaseCableTableView,
    BaseInterfaceTableView,
    BaseIPAddressTableView,
    BaseLibreNMSSyncView,
    CacheMixin,
)


@register_model_view(Device, name="librenms_sync", path="librenms-sync")
class DeviceLibreNMSSyncView(BaseLibreNMSSyncView):
    """
    View for devices page with LibreNMS sync information.
    """

    queryset = Device.objects.all()
    model = Device
    tab = ViewTab(label="LibreNMS Sync", permission="dcim.view_device")

    def get_interface_context(self, request, obj):
        """
        Get the context data for interface sync for devices.
        """
        interface_table_view = DeviceInterfaceTableView()
        return interface_table_view.get_context_data(request, obj)

    def get_cable_context(self, request, obj):
        """
        Get the context data for cable sync for devices.
        """
        cable_table_view = DeviceCableTableView()
        return cable_table_view.get_context_data(request, obj)

    def get_ip_context(self, request, obj):
        """
        Get the context data for IP address sync for devices.
        """
        ipaddress_table_view = DeviceIPAddressTableView()
        return ipaddress_table_view.get_context_data(request, obj)


class DeviceInterfaceTableView(BaseInterfaceTableView):
    """
    View for device interface synchronization.
    """

    model = Device

    def get_interfaces(self, obj):
        return obj.interfaces.all()

    def get_redirect_url(self, obj):
        return reverse(
            "plugins:netbox_librenms_plugin:vm_interface_sync", kwargs={"pk": obj.pk}
        )

    def get_table(self, data, obj):
        """
        Returns the appropriate table instance for rendering interface data.
        """
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            return VCInterfaceTable(data, device=obj)
        return LibreNMSInterfaceTable(data, device=obj)


class SingleInterfaceVerifyView(CacheMixin, View):
    """
    View for verifying single interface data for a device.
    """

    def post(self, request):
        """
        POST request to return json response with formatted interface data.
        """
        data = json.loads(request.body)
        selected_device_id = data.get("device_id")
        interface_name = data.get("interface_name")

        if not selected_device_id:
            return JsonResponse(
                {"status": "error", "message": "No device ID provided"}, status=400
            )

        selected_device = get_object_or_404(Device, pk=selected_device_id)

        # Get the primary device (master or first with IP) if part of virtual chassis
        if selected_device.virtual_chassis:
            primary_device = selected_device.virtual_chassis.master
            if not primary_device or not primary_device.primary_ip:
                primary_device = next(
                    (
                        member
                        for member in selected_device.virtual_chassis.members.all()
                        if member.primary_ip
                    ),
                    None,
                )
        else:
            primary_device = selected_device

        # Get cached data using primary device
        cached_data = cache.get(self.get_cache_key(primary_device, "ports"))

        if cached_data:
            port_data = next(
                (
                    port
                    for port in cached_data.get("ports", [])
                    if port["ifDescr"] == interface_name
                ),
                None,
            )

            if port_data:
                # Choose appropriate table class based on device type
                table_class = (
                    VCInterfaceTable
                    if selected_device.virtual_chassis
                    else LibreNMSInterfaceTable
                )
                table = table_class([], device=selected_device)
                formatted_row = table.format_interface_data(port_data, selected_device)
                return JsonResponse(
                    {"status": "success", "formatted_row": formatted_row}
                )

        return JsonResponse(
            {"status": "error", "message": "Interface data not found"}, status=404
        )


class DeviceCableTableView(BaseCableTableView):
    """
    View for device cable synchronization.
    """

    model = Device

    def get_table(self, data, obj):
        """
        Returns the appropriate table instance for rendering cable data.
        """
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            return VCCableTable(data, device=obj)
        return LibreNMSCableTable(data, device=obj)


class SingleCableVerifyView(CacheMixin, View):
    def post(self, request):
        data = json.loads(request.body)
        selected_device_id = data.get("device_id")
        local_port = data.get("local_port")

        # Handle empty device selection
        if not selected_device_id:
            return JsonResponse(
                {"status": "success", "local_port": local_port, "local_port_url": None}
            )

        selected_device = get_object_or_404(Device, pk=selected_device_id)
        interface = selected_device.interfaces.filter(name=local_port).first()

        response_data = {
            "status": "success",
            "local_port": local_port,
            "local_port_url": reverse("dcim:interface", args=[interface.pk])
            if interface
            else None,
        }

        return JsonResponse(response_data)


class DeviceIPAddressTableView(BaseIPAddressTableView):
    """
    View for device IP address synchronization.
    """

    model = Device
    #  TODO: Implement the IP Address sync view
