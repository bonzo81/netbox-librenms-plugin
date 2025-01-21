from django.urls import reverse
from django.utils.safestring import mark_safe
from django_tables2 import Column
from virtualization.models import VirtualMachine
from virtualization.tables import VirtualMachineTable


class VMStatusTable(VirtualMachineTable):
    librenms_status = Column(
        verbose_name="LibreNMS Status",
        empty_values=(),
        accessor="librenms_status",
        orderable=True,
    )

    def render_librenms_status(self, value, record):
        sync_url = reverse(
            "plugins:netbox_librenms_plugin:vm_librenms_sync",
            kwargs={"pk": record.pk},
        )

        if value:
            status = '<span class="text-success"><i class="mdi mdi-check-circle"></i> Synced</span>'
        elif value is False:
            status = '<span class="text-danger"><i class="mdi mdi-close-circle"></i> Not Found</span>'
        else:
            status = '<span class="text-secondary"><i class="mdi mdi-help-circle"></i> Unknown</span>'

        return mark_safe(f'<a href="{sync_url}">{status}</a>')

    class Meta(VirtualMachineTable.Meta):
        model = VirtualMachine
        fields = (
            "pk",
            "name",
            "status",
            "cluster",
            "cluster_type",
            "cluster_group",
            "librenms_status",
        )
        default_columns = (
            "name",
            "status",
            "cluster",
            "cluster_type",
            "cluster_group",
            "librenms_status",
        )
