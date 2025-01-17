from dcim.models import Device
from django.db.models import BooleanField, Case, Value, When
from netbox.views import generic

from netbox_librenms_plugin.filtersets import DeviceStatusFilterSet
from netbox_librenms_plugin.forms import DeviceStatusFilterForm
from netbox_librenms_plugin.tables.device_status import DeviceStatusTable
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin


class DeviceStatusListView(LibreNMSAPIMixin, generic.ObjectListView):
    queryset = Device.objects.none()  # Start with empty queryset
    table = DeviceStatusTable
    filterset = DeviceStatusFilterSet
    filterset_form = DeviceStatusFilterForm
    template_name = "netbox_librenms_plugin/device_status.html"
    actions = {"export": {}}
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
