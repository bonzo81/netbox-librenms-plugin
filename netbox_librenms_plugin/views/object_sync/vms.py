import copy

from django.urls import reverse
from utilities.views import ViewTab, register_model_view
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.constants import PERM_VIEW_PLUGIN
from netbox_librenms_plugin.tables.interfaces import LibreNMSVMInterfaceTable
from netbox_librenms_plugin.utils import get_interface_name_field

from ..base.interfaces_view import BaseInterfaceTableView
from ..base.ip_addresses_view import BaseIPAddressTableView
from ..base.librenms_sync_view import BaseLibreNMSSyncView


@register_model_view(VirtualMachine, name="librenms_sync", path="librenms-sync")
class VMLibreNMSSyncView(BaseLibreNMSSyncView):
    """Virtual machine detail tab for LibreNMS sync data."""

    queryset = VirtualMachine.objects.all()
    model = VirtualMachine
    tab = ViewTab(
        label="LibreNMS Sync",
        permission=PERM_VIEW_PLUGIN,
    )

    def get_interface_context(self, request, obj):
        """Return interface sync context for the virtual machine."""
        interface_name_field = get_interface_name_field(request)
        interface_sync_view = VMInterfaceTableView()
        interface_sync_view.request = copy.copy(request)
        return interface_sync_view.get_context_data(interface_sync_view.request, obj, interface_name_field)

    def get_cable_context(self, request, obj):
        """Return None; VMs do not support cable sync."""
        return None  # VMs do not expose cable sync data

    def get_vlan_context(self, request, obj):
        """Return None; VMs do not support VLAN sync."""
        return None

    def get_ip_context(self, request, obj):
        """Return IP address sync context for the virtual machine."""
        ipaddress_sync_view = VMIPAddressTableView()
        ipaddress_sync_view.request = copy.copy(request)
        return ipaddress_sync_view.get_context_data(ipaddress_sync_view.request, obj)


class VMInterfaceTableView(BaseInterfaceTableView):
    """Interface synchronization view for Virtual Machines."""

    model = VirtualMachine

    def get_table(self, data, obj, interface_name_field, vlan_groups=None):
        """Return a VM interface table for the given data."""
        return LibreNMSVMInterfaceTable(
            data,
            device=obj,
            vlan_groups=vlan_groups,
            server_key=self.librenms_api.server_key,
            interface_name_field=interface_name_field,
        )

    def get_interfaces(self, obj):
        """Return all interfaces for the virtual machine."""
        return obj.interfaces.all()

    def get_redirect_url(self, obj):
        """Return the VM interface sync redirect URL."""
        return reverse("plugins:netbox_librenms_plugin:vm_interface_sync", kwargs={"pk": obj.pk})


class VMIPAddressTableView(BaseIPAddressTableView):
    """IP address synchronization view for Virtual Machines."""

    model = VirtualMachine
    # VM-specific implementations
