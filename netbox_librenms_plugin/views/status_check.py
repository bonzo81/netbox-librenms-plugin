from dcim.models import Device
from django.db.models import BooleanField, Case, Value, When
from netbox.views import generic
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.filtersets import DeviceStatusFilterSet, VMStatusFilterSet
from netbox_librenms_plugin.forms import (
    DeviceStatusFilterForm,
    VirtualMachineStatusFilterForm,
)
from netbox_librenms_plugin.tables.device_status import DeviceStatusTable
from netbox_librenms_plugin.tables.VM_status import VMStatusTable
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin


class DeviceStatusListView(LibreNMSAPIMixin, generic.ObjectListView):
    queryset = Device.objects.none()  # Start with empty queryset
    table = DeviceStatusTable
    filterset = DeviceStatusFilterSet
    filterset_form = DeviceStatusFilterForm
    template_name = "netbox_librenms_plugin/status_check.html"
    actions = {}
    title = "Device LibreNMS Status"

    def get_queryset(self, request):
        """
        Override get_queryset to return filtered devices and check LibreNMS status
        """
        # Only get devices if filters are applied
        if self.request.GET:
            queryset = Device.objects.select_related(
                "device_type__manufacturer"
            ).prefetch_related(
                "site",
                "location",
                "rack",
            )

            # Create a list to store device IDs and their status
            device_status_map = {}

            # Apply filters
            queryset = self.filterset(self.request.GET, queryset=queryset).qs

            # Check LibreNMS status for each device
            for device in queryset:
                try:
                    librenms_id = self.librenms_api.get_librenms_id(device)
                    device_status_map[device.pk] = bool(librenms_id)
                except Exception:
                    device_status_map[device.pk] = False

            # Annotate the queryset with the status values
            case_when = []
            for device_id, status in device_status_map.items():
                case_when.append(When(pk=device_id, then=Value(status)))

            queryset = queryset.annotate(
                librenms_status=Case(
                    *case_when, default=Value(None), output_field=BooleanField()
                )
            )

            return queryset

        return Device.objects.none()


class VMStatusListView(LibreNMSAPIMixin, generic.ObjectListView):
    queryset = VirtualMachine.objects.select_related("cluster", "site")
    table = VMStatusTable
    filterset = VMStatusFilterSet
    filterset_form = VirtualMachineStatusFilterForm
    template_name = "netbox_librenms_plugin/status_check.html"
    actions = {}
    title = "Virtual Machine LibreNMS Status"

    def get_queryset(self, request):
        if self.request.GET:
            queryset = VirtualMachine.objects.select_related("cluster", "site")

            # Create a list to store VM IDs and their status
            vm_status_map = {}

            # Apply filters
            queryset = self.filterset(self.request.GET, queryset=queryset).qs

            # Check LibreNMS status for each VM
            for vm in queryset:
                try:
                    librenms_id = self.librenms_api.get_librenms_id(vm)
                    vm_status_map[vm.pk] = bool(librenms_id)
                except Exception:
                    vm_status_map[vm.pk] = False

            # Annotate the queryset with the status values
            case_when = []
            for vm_id, status in vm_status_map.items():
                case_when.append(When(pk=vm_id, then=Value(status)))

            queryset = queryset.annotate(
                librenms_status=Case(
                    *case_when, default=Value(None), output_field=BooleanField()
                )
            )

            return queryset

        return VirtualMachine.objects.none()
