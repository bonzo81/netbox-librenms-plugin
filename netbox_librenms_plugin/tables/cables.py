import re

import django_tables2 as tables
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from netbox.tables.columns import ToggleColumn
from utilities.paginator import EnhancedPaginator

from netbox_librenms_plugin.utils import (
    get_table_paginate_count,
    oob_badge_html,
    render_vc_member_options,
)

# Static trusted markup for the "Serial" console-port badge. The leading space is intentional.
SERIAL_BADGE_HTML = ' <span class="badge bg-teal text-white ms-1" title="Serial console port">Serial</span>'


class LibreNMSCableTable(tables.Table):
    """
    Table for displaying LibreNMS cable data.
    """

    selection = ToggleColumn(
        accessor="row_id",
        orderable=False,
        visible=True,
        attrs={
            "td": {"data-col": "selection"},
            "input": {
                "name": "select",
                "disabled": lambda record: None if record.get("can_create_cable") else "disabled",
            },
        },
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
        {% if record.netbox_local_interface_id %}
            <input type="hidden"
                   name="expected_local_id_{{ record.row_id }}"
                   value="{{ record.netbox_local_interface_id }}">
        {% endif %}
        {% if record.netbox_local_device_id %}
            <input type="hidden"
                   name="expected_local_device_id_{{ record.row_id }}"
                   value="{{ record.netbox_local_device_id }}">
        {% endif %}
        {% if record.netbox_remote_interface_id %}
            <input type="hidden"
                   name="expected_remote_id_{{ record.row_id }}"
                   value="{{ record.netbox_remote_interface_id }}">
        {% endif %}
        {% if record.netbox_remote_device_id %}
            <input type="hidden"
                   name="expected_remote_device_id_{{ record.row_id }}"
                   value="{{ record.netbox_remote_device_id }}">
        {% endif %}
        {% if record.can_create_cable %}
            <button type="submit"
                    class="btn btn-sm btn-primary"
                    name="sync_one"
                    value="{{ record.row_id }}">
                Sync Cable
            </button>
        {% endif %}
        {% if record.picker_url %}
            <button type="button"
                    class="btn btn-sm btn-outline-secondary"
                    title="Pick remote end"
                    aria-label="Pick remote end"
                    hx-get="{{ record.picker_url }}"
                    hx-target="#htmx-modal-content"
                    hx-swap="innerHTML">
                <i class="mdi mdi-connection"></i>
            </button>
        {% endif %}
        """,
        verbose_name="",
        orderable=False,
        attrs={"td": {"data-col": "actions"}},
    )

    def __init__(self, *args, device=None, **kwargs):
        """
        Initialize table with optional device context.

        Rows arrive with the ``row_id`` the cable view assigned to the snapshot. Re-deriving it
        here would compute uniqueness over a list the view may already have filtered, so a row
        could be rendered under one identity and submitted under another.

        Args:
            *args: Positional table arguments (the row data).
            device: The page device, used for the virtual-chassis member selector.
            **kwargs: Keyword table arguments.
        """
        self.device = device
        super().__init__(*args, **kwargs)
        self.tab = "cables"
        self.htmx_url = None
        self.prefix = "cables_"

    def render_remote_device(self, value, record):
        """Render the remote device (preferring the derived display name) as a link if available."""
        # remote_device_display carries the cable's real far end / a manual pick's device name;
        # the raw remote_device (LibreNMS label) stays pristine in the cache for re-enrichment.
        # Normalize None to "" like render_local_port — an unconfigured serial row with no
        # remote device name would otherwise render the literal "None" through format_html.
        display = record.get("remote_device_display") or value or ""
        if url := record.get("remote_device_url"):
            return format_html('<a href="{}">{}</a>', url, display)
        # Serial rows: dim unconfigured ports (label was never customised)
        if record.get("_source") == "serial" and not record.get("is_configured"):
            return format_html('<span class="text-muted fst-italic">{}</span>', display)
        return display

    def render_local_port(self, value, record):
        """Render local port name as a link if URL is available."""
        # Leading space: the badge follows the port name.
        oob_badge = oob_badge_html(record, leading_space=True)
        serial_badge = mark_safe(SERIAL_BADGE_HTML) if record.get("_source") == "serial" else ""  # noqa: S308
        # Normalize None to "" in both branches; otherwise the linked branch
        # renders the literal "None" as the link text when value is missing.
        display_value = value or ""
        if url := record.get("local_port_url"):
            return format_html('<a href="{}">{}</a>{}{}', url, display_value, oob_badge, serial_badge)
        return format_html("{}{}{}", display_value, oob_badge, serial_badge)

    def render_remote_port(self, value, record):
        """Render remote port name as a link if URL is available; flag a manually picked remote."""
        manual_badge = (
            mark_safe(  # noqa: S308  (static trusted markup, mirrors the Serial badge idiom)
                ' <i class="mdi mdi-gesture-tap-button text-muted" title="Remote end picked manually"></i>'
            )
            if record.get("manual_remote")
            else ""
        )
        # Normalize None to "" like render_local_port/render_remote_device — an unset remote
        # port name would otherwise render the literal "None" in every branch below.
        display_value = value or ""
        if url := record.get("remote_port_url"):
            return format_html('<a href="{}">{}</a>{}', url, display_value, manual_badge)
        if manual_badge:
            return format_html("{}{}", display_value, manual_badge)
        return display_value

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
            "data-interface": lambda record: record["row_id"],
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
        accessor="row_id",
        attrs={"td": {"class": "device-selection-col", "data-col": "device_selection"}},
    )

    def __init__(self, *args, device=None, allowed_vc_member_ids=None, **kwargs):
        """Initialize the VC cable table with device context."""
        super().__init__(*args, device=device, **kwargs)
        # Cache the VC member set once so render_device_selection doesn't re-query
        # members.all() (and a members.get per row via get_virtual_chassis_member) for every
        # row in large cable tables. Mirrors VCModuleTable.
        self._vc_members = []
        self._vc_member_by_position = {}
        if getattr(self.device, "virtual_chassis", None):
            members = self.device.virtual_chassis.members.all()
            if allowed_vc_member_ids is not None:
                members = members.filter(pk__in=allowed_vc_member_ids)
            self._vc_members = list(members)
            self._vc_member_by_position = {m.vc_position: m for m in self._vc_members}

    def _selected_member_id(self, port_name):
        """
        Resolve the selected VC member id from the port name.

        Served from the cached member set, mirroring get_virtual_chassis_member's position
        parse but without a per-row members.get() query.

        Args:
            port_name: The LibreNMS local port name (e.g. ``Ethernet3``).

        Returns:
            int: The matched member's id, or the table device's id when no member matches.
        """
        match = re.match(r"^[A-Za-z]+(\d+)", port_name or "")
        if match:
            member = self._vc_member_by_position.get(int(match.group(1)))
            if member is not None:
                return member.id
        return self.device.id

    def render_device_selection(self, value, record):
        """Render a dropdown to select the virtual chassis member for a port."""
        serial_owner_id = record.get("device_id") if record.get("_source") == "serial" else None
        selected_member_id = (
            serial_owner_id
            if serial_owner_id and any(str(member.pk) == str(serial_owner_id) for member in self._vc_members)
            else self._selected_member_id(record["local_port"])
        )
        row_id = record["row_id"]

        if serial_owner_id:
            return format_html(
                '<select id="device_selection_{0}" class="form-select" data-interface="{0}" data-row-id="{0}" disabled aria-disabled="true">{1}</select>'
                '<input type="hidden" name="device_selection_{0}" value="{2}">',
                row_id,
                render_vc_member_options(self._vc_members, selected_member_id),
                selected_member_id,
            )

        return format_html(
            '<select name="device_selection_{0}" id="device_selection_{0}" class="form-select" data-interface="{0}" data-row-id="{0}">{1}</select>',
            row_id,
            render_vc_member_options(self._vc_members, selected_member_id),
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
            "data-interface": lambda record: record["row_id"],
            "data-device": lambda record: record["device_id"],
            "data-name": lambda record: record["local_port"],
            "id": lambda record: record["row_id"],
        }
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-cable-table-vc",
        }
