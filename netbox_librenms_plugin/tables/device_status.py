from dcim.models import Device
from dcim.tables import DeviceTable
from django.urls import reverse
from django.utils.safestring import mark_safe
from django_tables2 import Column


class DeviceStatusTable(DeviceTable):
    librenms_status = Column(
        verbose_name="LibreNMS Status",
        empty_values=(),
        accessor="librenms_status",
        orderable=True,
    )

    def render_librenms_status(self, value, record):
        sync_url = reverse(
            "plugins:netbox_librenms_plugin:device_librenms_sync",
            kwargs={"pk": record.pk},
        )

        # Check if device is VC member and get master if applicable
        if hasattr(record, "virtual_chassis") and record.virtual_chassis:
            vc_master = record.virtual_chassis.master
            if not vc_master or not vc_master.primary_ip:
                vc_master = next(
                    (
                        member
                        for member in record.virtual_chassis.members.all()
                        if member.primary_ip
                    ),
                    None,
                )
            if vc_master and record.pk != vc_master.pk:
                master_url = reverse(
                    f"plugins:netbox_librenms_plugin:device_librenms_sync",
                    kwargs={"pk": vc_master.pk},
                )
                return mark_safe(
                    f'<a href="{master_url}"><span class="text-info">'
                    f'<i class="mdi mdi-server-network"></i> See {vc_master.name}</span></a>'
                )
        if value:
            status = '<span class="text-success"><i class="mdi mdi-check-circle"></i> Synced</span>'
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
