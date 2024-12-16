import django_tables2 as tables
from django.middleware.csrf import get_token
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from netbox.tables import NetBoxTable, columns
from netbox.tables.columns import BooleanColumn, ToggleColumn
from utilities.paginator import EnhancedPaginator, get_paginate_count
from utilities.templatetags.helpers import humanize_speed

from .models import InterfaceTypeMapping
from .utils import (
    convert_speed_to_kbps,
    format_mac_address,
    get_interface_name_field,
    get_table_paginate_count,
    get_virtual_chassis_member,
)


class LibreNMSInterfaceTable(tables.Table):
    """
    Table for displaying LibreNMS interface data.
    """

    class Meta:
        sequence = [
            "selection",
            "name",
            "type",
            "speed",
            "mac_address",
            "mtu",
            "enabled",
            "description",
            "librenms_id",
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-interface-table",
        }

    def __init__(self, *args, device=None, interface_name_field=None, **kwargs):
        self.device = device
        self.interface_name_field = interface_name_field or get_interface_name_field()

        # Update column accessors after initialization
        for column in ["selection", "name"]:
            self.base_columns[column].accessor = self.interface_name_field

        # Set row attributes using interface_name_field
        self._meta.row_attrs = {
            "data-interface": lambda record: record.get(self.interface_name_field),
            "data-name": lambda record: record.get(self.interface_name_field),
            "data-enabled": lambda record: record.get("ifAdminStatus", "").lower()
            if record.get("ifAdminStatus")
            else "",
        }

        super().__init__(*args, **kwargs)
        self.tab = "interfaces"
        self.htmx_url = None
        self.prefix = "interfaces_"

    selection = ToggleColumn(
        orderable=False,
        visible=True,
        attrs={"td": {"data-col": "selection"}, "input": {"name": "select"}},
    )
    name = tables.Column(verbose_name="Name", attrs={"td": {"data-col": "name"}})
    type = tables.Column(
        accessor="ifType",
        verbose_name="Interface Type",
        attrs={"td": {"data-col": "type"}},
    )
    speed = tables.Column(
        accessor="ifSpeed", verbose_name="Speed", attrs={"td": {"data-col": "speed"}}
    )
    mac_address = tables.Column(
        accessor="ifPhysAddress",
        verbose_name="MAC Address",
        attrs={"td": {"data-col": "mac_address"}},
    )
    mtu = tables.Column(
        accessor="ifMtu", verbose_name="MTU", attrs={"td": {"data-col": "mtu"}}
    )
    enabled = BooleanColumn(
        verbose_name="Enabled", attrs={"td": {"data-col": "enabled"}}
    )
    description = tables.Column(
        accessor="ifAlias",
        verbose_name="Description",
        attrs={"td": {"data-col": "description"}},
    )
    librenms_id = tables.Column(
        accessor="port_id",
        verbose_name="LibreNMS ID",
        attrs={"td": {"data-col": "librenms_id"}},
    )

    def render_speed(self, value, record):
        kbps_value = convert_speed_to_kbps(value)
        return self._render_field(
            humanize_speed(kbps_value), record, "ifSpeed", "speed"
        )

    def render_name(self, value, record):
        return self._render_field(value, record, self.interface_name_field, "name")

    def _get_interface_status_display(self, enabled, record):
        """
        Determine interface status display and CSS class based on enabled state and NetBox comparison.

        Returns:
            tuple: (display_value, css_class)
        """
        display_value = "Enabled" if enabled else "Disabled"

        if not record.get("exists_in_netbox"):
            return display_value, "text-danger"

        netbox_interface = record.get("netbox_interface")
        if netbox_interface:
            netbox_enabled = netbox_interface.enabled
            if enabled == netbox_enabled:
                return display_value, "text-success"
            return display_value, "text-warning"

        return display_value, "text-danger"

    def _parse_enabled_status(self, value):
        """Convert interface status value to boolean enabled state"""
        if isinstance(value, str):
            return value.lower() == "up"
        return bool(value)

    def render_enabled(self, value, record):
        enabled = self._parse_enabled_status(value)
        display_value, css_class = self._get_interface_status_display(enabled, record)
        return format_html('<span class="{}">{}</span>', css_class, display_value)

    def render_description(self, value, record):
        return self._render_field(value, record, "ifAlias", "description")

    def render_mac_address(self, value, record):
        formatted_mac = format_mac_address(value)
        return self._render_field(formatted_mac, record, "ifPhysAddress", "mac_address")

    def render_mtu(self, value, record):
        return self._render_field(value, record, "ifMtu", "mtu")

    def render_librenms_id(self, value, record):
        """
        Render the 'librenms_id' field with appropriate styling based on comparison with NetBox.
        """
        # Early return with danger styling if interface doesn't exist in NetBox
        if not record.get("exists_in_netbox"):
            return mark_safe(f'<span class="text-danger">{value}</span>')

        # Proceed only if we have a valid NetBox interface
        netbox_interface = record.get("netbox_interface")
        if not netbox_interface:
            return mark_safe(f'<span class="text-danger">{value}</span>')

        # Get the 'librenms_id' from NetBox custom fields
        netbox_librenms_id = netbox_interface.custom_field_data.get("librenms_id")

        # Check if the custom field exists
        if netbox_librenms_id is None:
            return mark_safe(
                f'<span class="text-danger" title="No librenms_id custom field value found">{value}</span>'
            )

        # Compare the IDs
        if str(value) != str(netbox_librenms_id):
            # IDs do not match
            return mark_safe(
                f'<span class="text-warning" title="Existing LibreNMS ID: {netbox_librenms_id}">{value}</span>'
            )
        else:
            # IDs match
            return mark_safe(f'<span class="text-success">{value}</span>')

    def _render_field(self, value, record, librenms_key, netbox_key):
        """
        Render a field value with appropriate styling based on the comparison with NetBox.
        """
        # Early return with danger styling if interface doesn't exist in NetBox
        if not record.get("exists_in_netbox"):
            return mark_safe(f'<span class="text-danger">{value}</span>')

        # Only proceed with comparison if we have a valid NetBox interface
        netbox_interface = record.get("netbox_interface")
        if not netbox_interface:
            return mark_safe(f'<span class="text-danger">{value}</span>')

        netbox_value = getattr(netbox_interface, netbox_key, None)
        librenms_value = record.get(librenms_key)

        if librenms_key == "ifSpeed":
            librenms_value = convert_speed_to_kbps(librenms_value)

        if librenms_value != netbox_value:
            return mark_safe(f'<span class="text-warning">{value}</span>')

        return mark_safe(f'<span class="text-success">{value}</span>')

    def render_type(self, value, record):
        speed = convert_speed_to_kbps(record.get("ifSpeed", 0))
        mapping = self.get_interface_mapping(value, speed)
        tooltip_value, icon = self.render_mapping_tooltip(value, speed, mapping)

        combined_display = format_html("{} {}", tooltip_value, icon)

        if not record.get("exists_in_netbox"):
            return format_html('<span class="text-danger">{}</span>', combined_display)

        netbox_interface = record.get("netbox_interface")

        if netbox_interface:
            netbox_type = getattr(netbox_interface, "type", None)
            if mapping and mapping.netbox_type == netbox_type:
                return format_html(
                    '<span class="text-success">{}</span>', combined_display
                )
            elif mapping:
                return format_html(
                    '<span class="text-warning">{}</span>', combined_display
                )

        return format_html('<span class="text-danger">{}</span>', combined_display)

    def get_interface_mapping(self, librenms_type, speed):
        """
        Get interface type mapping based on type and speed
        """
        # First try exact match with type and speed
        mapping = InterfaceTypeMapping.objects.filter(
            librenms_type=librenms_type, librenms_speed=speed
        ).first()

        # If no match found, fall back to type-only match
        if not mapping:
            mapping = InterfaceTypeMapping.objects.filter(
                librenms_type=librenms_type, librenms_speed__isnull=True
            ).first()

        return mapping

    def render_mapping_tooltip(self, value, speed, mapping):
        """
        Render tooltip for interface type mapping
        """
        if mapping:
            display = mapping.netbox_type
            icon = format_html(
                '<i class="mdi mdi-link-variant" title="Mapped from LibreNMS type: {} (Speed: {})"></i>',
                value,
                speed,
            )
        else:
            display = value
            icon = format_html(
                '<i class="mdi mdi-link-variant-off" title="No mapping to NetBox type"></i>'
            )
        return display, icon

    def format_interface_data(self, port_data, device):
        """
        Format single interface data using table rendering logic
        """

        # Add NetBox interface data
        interface_name = port_data.get(self.interface_name_field)

        port_data["netbox_interface"] = device.interfaces.filter(
            name=interface_name
        ).first()
        port_data["exists_in_netbox"] = bool(port_data["netbox_interface"])

        # Clear description if it matches interface name
        if (
            port_data["ifAlias"] == port_data["ifName"]
            or port_data["ifAlias"] == port_data["ifDescr"]
        ):
            port_data["ifAlias"] = ""

        formatted_data = {
            "name": self.render_name(interface_name, port_data),
            "type": self.render_type(port_data["ifType"], port_data),
            "speed": self.render_speed(port_data["ifSpeed"], port_data),
            "mac_address": self.render_mac_address(
                port_data["ifPhysAddress"], port_data
            ),
            "mtu": self.render_mtu(port_data["ifMtu"], port_data),
            "enabled": self.render_enabled(port_data["ifAdminStatus"], port_data),
            "description": self.render_description(port_data["ifAlias"], port_data),
        }

        return formatted_data

    def configure(self, request):
        paginate = {
            "paginator_class": EnhancedPaginator,
            "per_page": get_table_paginate_count(request, self.prefix),
        }

        tables.RequestConfig(request, paginate).configure(self)


class VCInterfaceTable(LibreNMSInterfaceTable):
    """
    Table for displaying Virtual Chassis interface data.
    """

    device_selection = tables.Column(
        verbose_name="Virtual Chassis member",
        accessor="device",
        orderable=False,
        empty_values=[],
        attrs={"td": {"data-col": "device_selection"}},
    )

    def __init__(self, *args, device=None, interface_name_field=None, **kwargs):
        super().__init__(
            *args, device=device, interface_name_field=interface_name_field, **kwargs
        )
        # Ensure device_selection column is visible
        if hasattr(self.device, "virtual_chassis") and self.device.virtual_chassis:
            self.columns.show("device_selection")
            # Update selection column accessor to match interface_name_field
            self.base_columns["selection"].accessor = self.interface_name_field

    def render_device_selection(self, value, record):
        """
        Renders a device selection dropdown for virtual chassis members.
        Determines the selected member based on interface type and name.
        Returns an HTML select element with appropriate member options.
        """
        members = self.device.virtual_chassis.members.all()
        if_type = record.get("ifType", "").lower()
        interface_name = record.get(self.interface_name_field)

        if "ethernet" in if_type:
            chassis_member = get_virtual_chassis_member(self.device, interface_name)
            selected_member_id = chassis_member.id if chassis_member else self.device.id
        else:
            selected_member_id = self.device.id

        # Create unique base ID for TomSelect components
        base_id = f"device_selection_{interface_name}_{hash(interface_name)}"

        options = [
            f'<option value="{member.id}"{" selected" if member.id == selected_member_id else ""}>{member.name}</option>'
            for member in members
        ]

        return format_html(
            '<select name="device_selection_{0}" id="{1}" class="form-select vc-member-select" data-interface="{0}" data-row-id="{0}">{2}</select>',
            interface_name,
            base_id,
            mark_safe("".join(options))
        )

    def format_interface_data(self, port_data, device):
        formatted_data = super().format_interface_data(port_data, device)
        formatted_data["device_selection"] = self.render_device_selection(
            None, port_data
        )
        return formatted_data

    class Meta:
        sequence = [
            "selection",
            "device_selection",
            "name",
            "type",
            "speed",
            "mac_address",
            "mtu",
            "enabled",
            "description",
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-interface-table",
        }


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


class SiteLocationSyncTable(tables.Table):
    """
    Table for displaying Netbox Site and Librenms Location data.
    """

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
        if record.librenms_location:
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

    def configure(self, request):
        paginate = {
            "paginator_class": EnhancedPaginator,
            "per_page": get_paginate_count(request),
        }
        tables.RequestConfig(request, paginate).configure(self)

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
        sequence = [
            "selection",
            "name",
            "mac_address",
            "mtu",
            "enabled",
            "description",
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-interface-table-vm",
        }

    # Remove the type and speed column for VMs
    type = None
    speed = None


class LibreNMSCableTable(tables.Table):
    """
    Table for displaying LibreNMS cable data.
    """

    selection = ToggleColumn(
        accessor="local_port",
        orderable=False,
        visible=True,
        attrs={"td": {"data-col": "selection"}, "input": {"name": "select"}},
    )

    local_port = tables.Column(
        verbose_name="Local Port", attrs={"td": {"data-col": "local_port"}}
    )
    remote_port = tables.Column(
        accessor="remote_port_name",
        verbose_name="Remote Port",
        attrs={"td": {"data-col": "remote_port"}},
    )
    remote_device = tables.Column(
        verbose_name="Remote Device", attrs={"td": {"data-col": "remote_device"}}
    )
    cable_status = tables.Column(
        verbose_name="Cable Status", attrs={"td": {"data-col": "cable_status"}}
    )
    actions = tables.TemplateColumn(
        template_code="""
        {% if record.can_create_cable %}
            <button type="submit"
                    class="btn btn-sm btn-primary"
                    onclick="document.getElementById('selected_port').value='{{ record.local_port }}'">
                Sync Cable
            </button>
        {% endif %}
        """,
        verbose_name="",
        orderable=False,
        attrs={"td": {"data-col": "actions"}},
    )

    def __init__(self, *args, device=None, **kwargs):
        self.device = device
        super().__init__(*args, **kwargs)
        self.tab = "cables"
        self.htmx_url = None
        self.prefix = "cables_"

    def render_remote_device(self, value, record):
        if url := record.get("remote_device_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_local_port(self, value, record):
        if url := record.get("local_port_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_remote_port(self, value, record):
        if url := record.get("remote_port_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_cable_status(self, value, record):
        if url := record.get("cable_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def configure(self, request):
        paginate = {
            "paginator_class": EnhancedPaginator,
            "per_page": get_table_paginate_count(request, self.prefix),
        }
        tables.RequestConfig(request, paginate).configure(self)

    class Meta:
        sequence = [
            "selection",
            "local_port",
            "remote_port",
            "remote_device",
            "cable_status",
            "actions",
        ]
        row_attrs = {
            "data-interface": lambda record: record["local_port"],
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
        accessor="local_port",
        attrs={"td": {"class": "device-selection-col", "data-col": "device_selection"}},
    )

    def __init__(self, *args, device=None, **kwargs):
        super().__init__(*args, device=device, **kwargs)

    def render_device_selection(self, value, record):
        members = self.device.virtual_chassis.members.all()
        chassis_member = get_virtual_chassis_member(self.device, record["local_port"])
        selected_member_id = chassis_member.id if chassis_member else self.device.id

        options = [
            f'<option value="{member.id}"{" selected" if member.id == selected_member_id else ""}>{member.name}</option>'
            for member in members
        ]

        return format_html(
            '<select name="device_selection_{0}" id="device_selection_{0}" class="form-select" data-interface="{0}" data-row-id="{0}">{1}</select>',
            record["local_port"],
            mark_safe("".join(options)),
        )

    class Meta(LibreNMSCableTable.Meta):
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
            "data-interface": lambda record: record["local_port"],
            "data-device": lambda record: record["device_id"],
            "data-name": lambda record: record["local_port"],
            "id": lambda record: record["local_port"],
        }
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-cable-table-vc",
        }
