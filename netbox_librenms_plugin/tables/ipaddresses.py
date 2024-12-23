import django_tables2 as tables
from django.utils.html import format_html
from netbox.tables.columns import ToggleColumn
from utilities.paginator import EnhancedPaginator
from netbox_librenms_plugin.utils import get_table_paginate_count


class IPAddressTable(tables.Table):
    """
    Table for displaying LibreNMS IP address data.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    class Meta:
        sequence = [
            "selection",
            "address",
            "prefix_length",
            "device",
            "interface_name",
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-ipaddress-table",
        }
        row_attrs = {
            "data-interface": lambda record: record["ipv4_address"],
            "data-name": lambda record: record["ipv4_address"],
        }

    selection = ToggleColumn(
        orderable=False,
        visible=True,
        attrs={"td": {"data-col": "selection"}, "input": {"name": "select"}},
        accessor="ipv4_address",
    )

    address = tables.Column(
        accessor="ipv4_address",
        verbose_name="IP Address",
        linkify=lambda record: record.get("ip_url"),
        attrs={"td": {"data-col": "address"}},
    )
    prefix_length = tables.Column(
        accessor="ipv4_prefixlen",
        verbose_name="Prefix Length",
        attrs={"td": {"data-col": "prefix"}},
    )
    device = tables.Column(
        linkify=lambda record: record.get("device_url"),
        attrs={"td": {"data-col": "device"}},
    )
    interface_name = tables.Column(
        accessor="interface_name",
        verbose_name="Interface",
        linkify=lambda record: record.get("interface_url"),
        attrs={"td": {"data-col": "interface"}},
    )
    status = tables.Column(
        verbose_name="Status",
        attrs={"td": {"data-col": "status"}},
    )

    def render_status(self, value, record):
        if value == "matched":
            return format_html(
                '<span class="text-success"><i class="mdi mdi-check-circle"></i> Synced</span>'
            )
        elif value == "update":
            return format_html(
                '<button type="submit" class="btn btn-sm btn-warning" onclick="document.getElementById(\'selected_ip\').value=\'{}\'">'
                '<i class="mdi mdi-pencil" aria-hidden="true"></i> Update</button>',
                record["ipv4_address"],
            )
        elif record.get("interface_url"):
            return format_html(
                '<button type="submit" class="btn btn-sm btn-primary" onclick="document.getElementById(\'selected_ip\').value=\'{}\'">'
                '<i class="mdi mdi-plus-thick" aria-hidden="true"></i> Create</button>',
                record["ipv4_address"],
            )
        return format_html('<span class="text-muted">Missing NetBox Object</span>')

    def render_device(self, value, record):
        if url := record.get("device_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_interface_name(self, value, record):
        if url := record.get("interface_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def configure(self, request):
        paginate = {
            "paginator_class": EnhancedPaginator,
            "per_page": get_table_paginate_count(request, self.prefix),
        }

        tables.RequestConfig(request, paginate).configure(self)
