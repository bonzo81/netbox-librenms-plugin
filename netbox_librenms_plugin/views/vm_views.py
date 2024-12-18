from django.urls import reverse
from utilities.views import ViewTab, register_model_view
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.tables.interfaces import LibreNMSVMInterfaceTable
from netbox_librenms_plugin.utils import get_interface_name_field

from .base.interfaces_view import BaseInterfaceTableView
from .base.ip_addresses_view import BaseIPAddressTableView
from .base.librenms_sync_view import BaseLibreNMSSyncView


@register_model_view(VirtualMachine, name="librenms_sync", path="librenms-sync")
class VMLibreNMSSyncView(BaseLibreNMSSyncView):
    """
    View for virtual machine page with LibreNMS sync information.
    """

    queryset = VirtualMachine.objects.all()
    model = VirtualMachine
    tab = ViewTab(
        label="LibreNMS Sync",
        permission="virtualization.view_virtualmachine",
    )

    def get_interface_context(self, request, obj):
        """
        Get the context data for interface sync for virtual machines.
        """
        interface_name_field = get_interface_name_field(request)
        interface_sync_view = VMInterfaceTableView()
        return interface_sync_view.get_context_data(request, obj, interface_name_field)

    def get_cable_context(self, request, obj):
        """
        Virtual machines don't have physical cables, return None.
        """
        return None

    def get_ip_context(self, request, obj):
        """
        Get the context data for IP address sync for virtual machines.
        """
        ipaddress_sync_view = VMIPAddressTableView()
        return ipaddress_sync_view.get_context_data(request, obj)


class VMInterfaceTableView(BaseInterfaceTableView):
    """
    View for VM interface synchronization.
    """

    model = VirtualMachine

    def get_table(self, data, obj, interface_name_field):
        return LibreNMSVMInterfaceTable(data)

    def get_interfaces(self, obj):
        return obj.interfaces.all()

    def get_redirect_url(self, obj):
        return reverse(
            "plugins:netbox_librenms_plugin:vm_interface_sync", kwargs={"pk": obj.pk}
        )


class VMIPAddressTableView(BaseIPAddressTableView):
    """
    View for VM IP address synchronization.
    """

    model = VirtualMachine
    # VM-specific implementations
