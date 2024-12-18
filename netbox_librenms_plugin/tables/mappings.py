import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from netbox_librenms_plugin.models import InterfaceTypeMapping


class InterfaceTypeMappingTable(NetBoxTable):
    """
    Table for displaying InterfaceTypeMapping data.
    """

    librenms_type = tables.Column(verbose_name="LibreNMS Type")
    librenms_speed = tables.Column(verbose_name="LibreNMS Speed (Kbps)")
    netbox_type = tables.Column(verbose_name="NetBox Type")
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta:
        model = InterfaceTypeMapping
        fields = ("id", "librenms_type", "librenms_speed", "netbox_type", "actions")
        default_columns = (
            "id",
            "librenms_type",
            "librenms_speed",
            "netbox_type",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}
