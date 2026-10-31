import json

import django_tables2 as tables
from django.utils.html import format_html, mark_safe
from netbox.tables.columns import ToggleColumn

from netbox_librenms_plugin.utils import identify_ip_sync_rows, paginate_sync_table, rule_block_html


def _shown_vrf_id(row):
    """Return the VRF that the row's VRF select shows, or "" for Global."""
    choices = {vrf.pk for vrf in row.get("vrfs") or ()}
    # The view sets vrf_id or suggested_vrf_id, never both.
    shown = next((vrf_id for vrf_id in (row.get("vrf_id"), row.get("suggested_vrf_id")) if vrf_id in choices), None)
    return "" if shown is None else str(shown)


class IPAddressTable(tables.Table):
    """Table for displaying LibreNMS IP address data."""

    def __init__(self, *args, **kwargs):
        """Initialize IP address table."""
        if args:
            args = (identify_ip_sync_rows(list(args[0])), *args[1:])
        elif kwargs.get("data") is not None:
            kwargs["data"] = identify_ip_sync_rows(list(kwargs["data"]))
        super().__init__(*args, **kwargs)
        # Set Primary IP selects the management row also from a page that does not render it, with the VRF it shows.
        mgmt_row_vrfs = {
            row["row_id"]: _shown_vrf_id(row) for row in self.data if row.get("is_mgmt_ip") and row["row_id"]
        }
        self.attrs["data-mgmt-rows"] = json.dumps(list(mgmt_row_vrfs))
        self.attrs["data-mgmt-row-vrfs"] = json.dumps(mgmt_row_vrfs)
        # Identify the owning sync tab so the paginator links (inc/paginator.html builds
        # ?tab={{ table.tab }}) keep the user on the IP Addresses tab. Without this, table.tab
        # renders empty and paging falls back to the default (Interfaces) tab.
        self.tab = "ipaddresses"
        # Give this table its own pagination namespace. configure() passes self.prefix to
        # paginate_sync_table() and get_table_paginate_count(), which key per_page on
        # "{prefix}per_page"; left empty, the IP table shares the generic per-page param with
        # other tabs instead of reading "ipaddresses_per_page". Orthogonal to self.tab. Matches
        # the cables/modules/interfaces/vlans tables; preserve an explicit caller override.
        if not self.prefix:
            self.prefix = "ipaddresses_"

    class Meta:
        """Meta options for IPAddressTable."""

        sequence = [
            "selection",
            "address",
            "prefix_length",
            "device",
            "interface_name",
            "vrf",
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-ipaddress-table",
        }
        row_attrs = {
            "data-interface": lambda record: record["row_id"],
            "data-name": lambda record: record["ip_with_mask"],
            "data-mgmt-ip": lambda record: "true" if record.get("is_mgmt_ip") else "",
        }

    selection = ToggleColumn(
        orderable=False,
        visible=True,
        attrs={
            "td": {"data-col": "selection"},
            # The writer refuses an address an IP mapping ignores, so the row offers no selection.
            "input": {"name": "select", "disabled": lambda record: "disabled" if record.get("ip_rule_block") else None},
        },
        accessor="row_id",
    )

    address = tables.Column(
        accessor="ip_address",
        verbose_name="IP Address",
        linkify=lambda record: record.get("ip_url"),
        attrs={"td": {"data-col": "address"}},
    )
    prefix_length = tables.Column(
        accessor="prefix_length",
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
    vrf = tables.TemplateColumn(
        template_code="""
        <div class="d-flex align-items-center gap-1">
            <select id="vrf_select_{{ record.row_id }}" class="form-select vrf-select" data-ip="{{ record.ip_address }}" data-prefix="{{ record.prefix_length }}" data-row-id="{{ record.row_id }}" name="vrf_{{ record.row_id }}">
                <option value="">Global</option>
                {% for vrf in record.vrfs %}
                    <option value="{{ vrf.pk }}" {% if record.vrf_id == vrf.pk or record.suggested_vrf_id == vrf.pk %}selected{% endif %}>
                        {{ vrf.name }}
                    </option>
                {% endfor %}
            </select>
            {% if record.vrf_suggested_from %}
                <i class="mdi mdi-lightbulb-on-outline text-muted" role="img"
                   title="Suggested from LibreNMS VRF {{ record.vrf_suggested_from.name }} by {{ record.vrf_suggested_from.matched_by }} match"
                   aria-label="Suggested from LibreNMS VRF {{ record.vrf_suggested_from.name }} by {{ record.vrf_suggested_from.matched_by }} match"></i>
            {% endif %}
            {% if record.vrf_create_url and not record.ip_rule_block %}
                <button type="submit" class="btn btn-sm btn-outline-primary text-nowrap" name="create_vrf" value="{{ record.row_id }}"
                        formaction="{{ record.vrf_create_url }}"
                        data-confirm="Create NetBox VRF '{{ record.librenms_vrf.name }}' with {% if record.librenms_vrf.rd %}route distinguisher {{ record.librenms_vrf.rd }}{% else %}no route distinguisher{% endif %}? The IP address is not synced."
                        title="LibreNMS VRF {{ record.librenms_vrf.name }} is not in NetBox. Create it."
                        aria-label="Create NetBox VRF {{ record.librenms_vrf.name }}"><i class="mdi mdi-plus-thick" aria-hidden="true"></i> VRF</button>
            {% endif %}
        </div>
        """,
        attrs={"td": {"data-col": "vrf"}},
        verbose_name="VRF",
    )
    status = tables.Column(
        verbose_name="Status",
        attrs={"td": {"data-col": "status"}},
    )

    def render_status(self, value, record):
        """Render the status column with appropriate buttons or text styling."""
        row_id = record.get("row_id", record.get("ip_with_mask"))
        if row_id is None:
            return "Ambiguous source row"
        rule_block = record.get("ip_rule_block") or record.get("rule_block")
        if rule_block and value != "matched":
            return rule_block_html(rule_block)
        if value == "update":
            return format_html(
                '<button type="submit" class="btn btn-sm btn-warning" name="sync_one" value="{}">'
                '<i class="mdi mdi-pencil" aria-hidden="true"></i> Update</button>',
                row_id,
            )
        elif value == "matched":
            return mark_safe('<span class="text-success"><i class="mdi mdi-check-circle"></i> Synced</span>')
        elif record.get("interface_url"):
            return format_html(
                '<button type="submit" class="btn btn-sm btn-primary" name="sync_one" value="{}">'
                '<i class="mdi mdi-plus-thick" aria-hidden="true"></i> Create</button>',
                row_id,
            )
        return mark_safe('<span class="text-muted">Missing NetBox Object</span>')

    def render_device(self, value, record):
        """Render the device column with a link if available."""
        if url := record.get("device_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_interface_name(self, value, record):
        """Render the interface column with a link if available."""
        if url := record.get("interface_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def configure(self, request):
        """Configure the table."""
        paginate_sync_table(self, request)
