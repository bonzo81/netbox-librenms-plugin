import json
from typing import NamedTuple

import django_tables2 as tables
from django.utils.html import format_html, mark_safe
from netbox.tables.columns import ToggleColumn

from netbox_librenms_plugin.sync_filters import MATCHING, MISMATCHED, NOT_IN_NETBOX, FilterColumn, FilterSpec
from netbox_librenms_plugin.utils import paginate_sync_table, rule_block_html, rule_block_label

# The sync-state filter value of each row status; every other status is unclassified.
_FILTER_SYNC_STATE = {"matched": MATCHING, "update": MISMATCHED, "sync": NOT_IN_NETBOX}

# The values a plain column shows as the table default ("—") instead of calling its render method.
_EMPTY_VALUES = (None, "")


class _StatusCell(NamedTuple):
    """The status cell of one row: its sync-state filter value, the text it shows, and its markup."""

    sync_state: str | None
    label: str
    markup: str


def management_row_vrfs(rows):
    """Return the VRF that the VRF select of each management row shows, by row ID; "" is Global."""
    vrfs = {}
    for row in rows:
        if not (row.get("is_mgmt_ip") and row["row_id"]):
            continue
        choices = {vrf.pk for vrf in row.get("vrfs") or ()}
        # The view sets vrf_id or suggested_vrf_id, never both.
        shown = next((vrf_id for vrf_id in (row.get("vrf_id"), row.get("suggested_vrf_id")) if vrf_id in choices), None)
        vrfs[row["row_id"]] = "" if shown is None else str(shown)
    return vrfs


class IPAddressTable(tables.Table):
    """Table for displaying LibreNMS IP address data."""

    def __init__(self, data, *, mgmt_row_vrfs, **kwargs):
        """
        Initialize IP address table.

        Args:
            data (list[dict]): The rows, which ``enrich_ip_data`` identified over the whole snapshot.
            mgmt_row_vrfs (dict[str, str]): The VRF that each management row of the whole snapshot shows, by row ID.
            **kwargs: Other ``tables.Table`` options.

        """
        super().__init__(data, **kwargs)
        # Set Primary IP selects the management row also from a page or a filter that does not show it, with its VRF.
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
        {% load librenms_sync_query %}
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
                        formaction="{% sync_tab_url record.vrf_create_url %}"
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

    def filter_spec(self):
        """Return the filter columns of this table, each with the text its cell shows."""
        columns = self.columns
        return FilterSpec(
            prefix=self.prefix,
            columns=(
                FilterColumn("address", str(columns["address"].header), self.text_address),
                FilterColumn("prefix", str(columns["prefix_length"].header), self.text_prefix),
                FilterColumn("device", str(columns["device"].header), self.text_device),
                FilterColumn("interface", str(columns["interface_name"].header), self.text_interface),
                FilterColumn("vrf", str(columns["vrf"].header), self.text_vrf, has_field=False),
                FilterColumn("status", str(columns["status"].header), self.text_status, has_field=False),
            ),
            sync_state=self.filter_sync_state,
        )

    def filter_sync_state(self, record):
        """Return the row's sync-state filter value, or None for a row no sync-state filter shows."""
        value = record.get("status")
        return None if value in _EMPTY_VALUES else self._status_cell(value, record).sync_state

    def _cell_text(self, value):
        """Return the text a plain cell shows for *value*: the table default for an empty value."""
        return self.default if value in _EMPTY_VALUES else str(value)

    def text_address(self, record):
        """Return the visible text of the address cell."""
        return self._cell_text(record.get("ip_address"))

    def text_prefix(self, record):
        """Return the visible text of the prefix length cell."""
        return self._cell_text(record.get("prefix_length"))

    def text_device(self, record):
        """Return the visible text of the device cell."""
        return self._cell_text(record.get("device"))

    def text_interface(self, record):
        """Return the visible text of the interface cell."""
        return self._cell_text(record.get("interface_name"))

    def text_vrf(self, record):
        """Return the visible text of the VRF cell: the option its select shows."""
        chosen = record.get("vrf_id"), record.get("suggested_vrf_id")
        names = [vrf.name for vrf in record.get("vrfs") or () if vrf.pk in chosen]
        # The template marks each matching option selected, and a browser shows the last one.
        return names[-1] if names else "Global"

    def text_status(self, record):
        """Return the visible text of the status cell: the action, the rule pill or the row state."""
        value = record.get("status")
        return self.default if value in _EMPTY_VALUES else self._status_cell(value, record).label

    @staticmethod
    def _status_cell(value, record):
        """Return the status cell of one row, which the render method and the filters both read."""
        state = _FILTER_SYNC_STATE.get(value)
        row_id = record.get("row_id", record.get("ip_with_mask"))
        if row_id is None:
            return _StatusCell(None, "Ambiguous source row", "Ambiguous source row")
        rule_block = record.get("ip_rule_block") or record.get("rule_block")
        if rule_block and value != "matched":
            return _StatusCell(None, rule_block_label(rule_block), rule_block_html(rule_block))
        if value == "update":
            markup = format_html(
                '<button type="submit" class="btn btn-sm btn-warning" name="sync_one" value="{}">'
                '<i class="mdi mdi-pencil" aria-hidden="true"></i> Update</button>',
                row_id,
            )
            return _StatusCell(state, "Update", markup)
        if value == "matched":
            return _StatusCell(
                state,
                "Synced",
                mark_safe('<span class="text-success"><i class="mdi mdi-check-circle"></i> Synced</span>'),
            )
        if record.get("interface_url"):
            markup = format_html(
                '<button type="submit" class="btn btn-sm btn-primary" name="sync_one" value="{}">'
                '<i class="mdi mdi-plus-thick" aria-hidden="true"></i> Create</button>',
                row_id,
            )
            return _StatusCell(state, "Create", markup)
        return _StatusCell(
            state, "Missing NetBox Object", mark_safe('<span class="text-muted">Missing NetBox Object</span>')
        )

    def render_status(self, value, record):
        """Render the status column with appropriate buttons or text styling."""
        return self._status_cell(value, record).markup

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
