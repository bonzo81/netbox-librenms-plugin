from dcim.models import Device, Interface
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.utils import (
    LIBRENMS_TO_NETBOX_MAPPING,
    convert_speed_to_kbps,
)
from netbox_librenms_plugin.views.mixins import CacheMixin


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
        exclude_columns = request.POST.getlist("exclude_columns")

        if selected_interfaces is None:
            return redirect(f"plugins:netbox_librenms_plugin:{url_name}", pk=object_id)

        ports_data = self.get_cached_ports_data(request, obj)
        if ports_data is None:
            return redirect(f"plugins:netbox_librenms_plugin:{url_name}", pk=object_id)

        self.sync_selected_interfaces(
            obj, selected_interfaces, ports_data, exclude_columns
        )

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

    def sync_selected_interfaces(
        self, obj, selected_interfaces, ports_data, exclude_columns
    ):
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
        self.update_interface_attributes(
            interface, librenms_interface, netbox_type, exclude_columns
        )

        if "enabled" not in exclude_columns:
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

    def update_interface_attributes(
        self, interface, librenms_interface, netbox_type, exclude_columns
    ):
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