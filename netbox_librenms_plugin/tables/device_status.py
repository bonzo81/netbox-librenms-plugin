from dcim.models import Device
from dcim.tables import DeviceTable
from django.urls import reverse
from django.utils.safestring import mark_safe
from django_tables2 import Column

from netbox_librenms_plugin.utils import get_librenms_sync_device


class DeviceStatusTable(DeviceTable):
    """
    Table for displaying device LibreNMS status.
    """

    librenms_status = Column(
        verbose_name="LibreNMS Status",
        empty_values=(),
        accessor="librenms_status",
        orderable=False,
    )

    def render_librenms_status(self, value, record):
        sync_url = reverse(
            "plugins:netbox_librenms_plugin:device_librenms_sync",
            kwargs={"pk": record.pk},
        )

        # Check if device is VC member and redirect to sync device if different
        if hasattr(record, "virtual_chassis") and record.virtual_chassis:
            sync_device = get_librenms_sync_device(record)
            if sync_device and record.pk != sync_device.pk:
                sync_device_url = reverse(
                    "plugins:netbox_librenms_plugin:device_librenms_sync",
                    kwargs={"pk": sync_device.pk},
                )
                return mark_safe(
                    f'<a href="{sync_device_url}"><span class="text-info">'
                    f'<i class="mdi mdi-server-network"></i> See {sync_device.name}</span></a>'
                )
        if value:
            status = '<span class="text-success"><i class="mdi mdi-check-circle"></i> Found</span>'
        elif value is False:
            status = '<span class="text-danger"><i class="mdi mdi-close-circle"></i> Not Found</span>'
        else:
            status = '<span class="text-secondary"><i class="mdi mdi-help-circle"></i> Unknown</span>'

        return mark_safe(f'<a href="{sync_url}">{status}</a>')

    class Meta(DeviceTable.Meta):
        model = Device
        fields = (
            "pk",
            "name",
            "status",
            "tenant",
            "site",
            "location",
            "rack",
            "role",
            "manufacturer",
            "device_type",
            "device_role",
            "librenms_status",
        )
        default_columns = (
            "name",
            "status",
            "site",
            "location",
            "rack",
            "device_type",
            "device_role",
            "librenms_status",
        )
