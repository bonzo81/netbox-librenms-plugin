import django_tables2 as tables
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from netbox.tables.columns import ToggleColumn
from utilities.paginator import EnhancedPaginator

from netbox_librenms_plugin.utils import (
    get_table_paginate_count,
    get_virtual_chassis_member,
)


class LibreNMSCableTable(tables.Table):
    """
    Table for displaying LibreNMS cable data.
    """

    selection = ToggleColumn(
        accessor="local_port_id",
        orderable=False,
        visible=True,
        attrs={"td": {"data-col": "selection"}, "input": {"name": "select"}},
    )

    local_port = tables.Column(verbose_name="Local Port", attrs={"td": {"data-col": "local_port"}})
    remote_port = tables.Column(
        accessor="remote_port_name",
        verbose_name="Remote Port",
        attrs={"td": {"data-col": "remote_port"}},
    )
    remote_device = tables.Column(verbose_name="Remote Device", attrs={"td": {"data-col": "remote_device"}})
    cable_status = tables.Column(verbose_name="Cable Status", attrs={"td": {"data-col": "cable_status"}})
    actions = tables.TemplateColumn(
        template_code="""
        {% if record.can_create_cable %}
            <button type="submit"
                    class="btn btn-sm btn-primary"
                    onclick="document.getElementById('selected_port').value='{{ record.local_port_id }}'">
                Sync Cable
            </button>
        {% endif %}
        """,
        verbose_name="",
        orderable=False,
        attrs={"td": {"data-col": "actions"}},
    )

    def __init__(self, *args, device=None, **kwargs):
        """Initialize table with optional device context."""
        self.device = device
        super().__init__(*args, **kwargs)
        self.tab = "cables"
        self.htmx_url = None
        self.prefix = "cables_"

    def render_remote_device(self, value, record):
        """Render remote device name as a link if URL is available."""
        if url := record.get("remote_device_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_local_port(self, value, record):
        """Render local port name as a link if URL is available."""
        if url := record.get("local_port_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_remote_port(self, value, record):
        """Render remote port name as a link if URL is available."""
        if url := record.get("remote_port_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_cable_status(self, value, record):
        """Render cable status as a link if cable URL is available."""
        if url := record.get("cable_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def configure(self, request):
        """Configure pagination for the table using the current request."""
        paginate = {
            "paginator_class": EnhancedPaginator,
            "per_page": get_table_paginate_count(request, self.prefix),
        }
        tables.RequestConfig(request, paginate).configure(self)

    class Meta:
        """Define column sequence, row attributes, and table styling."""

        sequence = [
            "selection",
            "local_port",
            "remote_port",
            "remote_device",
            "cable_status",
            "actions",
        ]
        row_attrs = {
            "data-interface": lambda record: record["local_port_id"],
            "data-device": lambda record: record["device_id"],
            "data-name": lambda record: record["local_port"],
        }
        attrs = {"class": "table table-hover object-list", "id": "librenms-cable-table"}


class VCCableTable(LibreNMSCableTable):
    """
    Table for displaying LibreNMS cable data for Virtual Chassis devices.
    """

    device_selection = tables.Column(
        verbose_name="Virtual Chassis Member",
        accessor="local_port_id",
        attrs={"td": {"class": "device-selection-col", "data-col": "device_selection"}},
    )

    def __init__(self, *args, device=None, **kwargs):
        """Initialize the VC cable table with device context."""
        super().__init__(*args, device=device, **kwargs)

    def render_device_selection(self, value, record):
        """Render a dropdown to select the virtual chassis member for a port."""
        members = self.device.virtual_chassis.members.all()
        chassis_member = get_virtual_chassis_member(self.device, record["local_port"])
        selected_member_id = chassis_member.id if chassis_member else self.device.id
        port_id = record["local_port_id"]

        options = [
            f'<option value="{member.id}"{" selected" if member.id == selected_member_id else ""}>{member.name}</option>'
            for member in members
        ]

        return format_html(
            '<select name="device_selection_{0}" id="device_selection_{0}" class="form-select" data-interface="{0}" data-row-id="{0}">{1}</select>',
            port_id,
            mark_safe("".join(options)),
        )

    class Meta(LibreNMSCableTable.Meta):
        """Define column sequence and attributes for the VC cable table."""

        sequence = [
            "selection",
            "device_selection",
            "local_port",
            "remote_port",
            "remote_device",
            "cable_status",
            "actions",
        ]
        row_attrs = {
            "data-interface": lambda record: record["local_port_id"],
            "data-device": lambda record: record["device_id"],
            "data-name": lambda record: record["local_port"],
            "id": lambda record: record["local_port_id"],
        }
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-cable-table-vc",
        }
