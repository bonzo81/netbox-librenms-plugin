import django_tables2 as tables
from django.middleware.csrf import get_token
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from netbox.tables import NetBoxTable, columns
from netbox.tables.columns import BooleanColumn, ToggleColumn
from utilities.paginator import EnhancedPaginator, get_paginate_count
from utilities.templatetags.helpers import humanize_speed

from .models import InterfaceTypeMapping
from .utils import convert_speed_to_kbps, format_mac_address


class LibreNMSInterfaceTable(tables.Table):
    """
    Table for displaying LibreNMS interface data.
    """

    selection = ToggleColumn(
        accessor="ifName",
        orderable=False,
        visible=True,
        attrs={"input": {"name": "select"}},
    )
    ifName = tables.Column(verbose_name="Interface Name")
    ifType = tables.Column(verbose_name="Interface Type")
    ifSpeed = tables.Column(verbose_name="Speed")
    ifPhysAddress = tables.Column(verbose_name="MAC Address")
    ifMtu = tables.Column(verbose_name="MTU")
    enabled = BooleanColumn(verbose_name="Enabled")
    ifDescr = tables.Column(accessor="ifAlias", verbose_name="Description")

    def render_ifType(self, value, record):
        speed = convert_speed_to_kbps(record.get("ifSpeed", 0))
        if speed is None:
            mapping = InterfaceTypeMapping.objects.filter(
                librenms_type=value, librenms_speed__isnull=True
            ).first()
        else:
            mapping = InterfaceTypeMapping.objects.filter(
                librenms_type=value, librenms_speed=speed
            ).first()
        if mapping:
            display_value = mapping.netbox_type
            icon = format_html(
                '<i class="mdi mdi-link-variant" title="Mapped from LibreNMS type: {} (Speed: {})">',
                value,
                speed,
            )
        else:
            display_value = value
            icon = format_html(
                '<i class="mdi mdi-link-variant-off" title="No mapping to NetBox type"></i>'
            )

        display_value = format_html("{} {}", display_value, icon)

        if not record.get("exists_in_netbox"):
            return format_html('<span class="text-danger">{}</span>', display_value)

        netbox_interface = record.get("netbox_interface")
        if netbox_interface:
            netbox_type = getattr(netbox_interface, "type", None)
            if mapping and mapping.netbox_type == netbox_type:
                return format_html(
                    '<span class="text-success">{}</span>', display_value
                )
            elif mapping:
                return format_html(
                    '<span class="text-warning">{}</span>', display_value
                )

        return format_html('<span class="text-danger">{}</span>', display_value)

    def render_ifSpeed(self, value, record):
        # Convert librenms speed to Kbps from bps
        kbps_value = convert_speed_to_kbps(value)
        return self._render_field(
            humanize_speed(kbps_value), record, "ifSpeed", "speed"
        )

    def render_ifName(self, value, record):
        return self._render_field(value, record, "ifName", "name")

    def render_enabled(self, value, record):
        enabled = value.lower() == "up" if isinstance(value, str) else bool(value)
        display_value = "Enabled" if enabled else "Disabled"

        if not record.get("exists_in_netbox"):
            return format_html('<span class="text-danger">{}</span>', display_value)

        netbox_interface = record.get("netbox_interface")
        if netbox_interface:
            netbox_enabled = netbox_interface.enabled
            if enabled == netbox_enabled:
                return format_html(
                    '<span class="text-success">{}</span>', display_value
                )
            else:
                return format_html(
                    '<span class="text-warning">{}</span>', display_value
                )

        return format_html('<span class="text-danger">{}</span>', display_value)

    def render_ifDescr(self, value, record):
        return self._render_field(value, record, "ifAlias", "description")

    def render_ifPhysAddress(self, value, record):
        formatted_mac = format_mac_address(value)
        return self._render_field(formatted_mac, record, "ifPhysAddress", "mac_address")

    def render_ifMtu(self, value, record):
        return self._render_field(value, record, "ifMtu", "mtu")

    def _render_field(self, value, record, librenms_key, netbox_key):
        if not record.get("exists_in_netbox"):
            return mark_safe(f'<span class="text-danger">{value}</span>')
        netbox_value = getattr(record["netbox_interface"], netbox_key, None)
        librenms_value = record.get(librenms_key)
        if librenms_key == "ifSpeed":
            librenms_value = convert_speed_to_kbps(librenms_value)
        if librenms_value != netbox_value:
            return mark_safe(f'<span class="text-warning">{value}</span>')
        return mark_safe(f'<span class="text-success">{value}</span>')

    def configure(self, request):
        paginate = {
            "paginator_class": EnhancedPaginator,
            "per_page": get_paginate_count(request),
        }
        tables.RequestConfig(request, paginate).configure(self)

    class Meta:
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-interface-table",
        }
        empty_text = "No data available"


class InterfaceTypeMappingTable(NetBoxTable):
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


class SiteLocationSyncTable(tables.Table):
    netbox_site = tables.Column(linkify=True)
    latitude = tables.Column(accessor="netbox_site.latitude")
    longitude = tables.Column(accessor="netbox_site.longitude")
    librenms_location = tables.Column(
        accessor="librenms_location.location", verbose_name="LibreNMS Location"
    )
    librenms_latitude = tables.Column(
        accessor="librenms_location.lat", verbose_name="LibreNMS Latitude"
    )
    librenms_longitude = tables.Column(
        accessor="librenms_location.lng", verbose_name="LibreNMS Longitude"
    )
    actions = tables.Column(empty_values=())

    def render_latitude(self, value, record):
        return self.render_coordinate(value, record.is_synced)

    def render_longitude(self, value, record):
        return self.render_coordinate(value, record.is_synced)

    def render_coordinate(self, value, is_synced):
        css_class = "text-success" if is_synced else "text-danger"
        return format_html('<span class="{}">{}</span>', css_class, value)

    def render_actions(self, record):
        csrf_token = get_token(self.request)
        if record.is_synced:
            return mark_safe(
                '<span class="text-success"><i class="mdi mdi-check-circle" aria-hidden="true"></i> Synced</span>'
            )
        elif record.librenms_location:
            return mark_safe(
                f'<form method="post">'
                f'<input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">'
                f'<input type="hidden" name="action" value="update">'
                f'<input type="hidden" name="pk" value="{record.netbox_site.pk}">'
                '<button type="submit" class="btn btn-sm btn-warning">'
                '<i class="mdi mdi-pencil" aria-hidden="true"></i> Update in LibreNMS'
                "</button>"
                "</form>"
            )
        else:
            return mark_safe(
                f'<form method="post">'
                f'<input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">'
                f'<input type="hidden" name="action" value="create">'
                f'<input type="hidden" name="pk" value="{record.netbox_site.pk}">'
                '<button type="submit" class="btn btn-sm btn-primary">'
                '<i class="mdi mdi-plus-thick" aria-hidden="true"></i> Create in LibreNMS'
                "</button>"
                "</form>"
            )

    class Meta:
        fields = (
            "netbox_site",
            "latitude",
            "longitude",
            "librenms_location",
            "librenms_latitude",
            "librenms_longitude",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}


class LibreNMSVMInterfaceTable(LibreNMSInterfaceTable):
    """
    Table for displaying LibreNMS VM interface data.
    """

    class Meta(LibreNMSInterfaceTable.Meta):
        pass

    # Remove the type column since VMs don't have this
    ifType = None
    ifSpeed = None
