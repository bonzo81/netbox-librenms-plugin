import django_tables2 as tables
from netbox.tables import NetBoxTable, columns
from netbox.tables.columns import BooleanColumn
from utilities.templatetags.helpers import humanize_speed
from utilities.paginator import EnhancedPaginator, get_paginate_count
from .utils import convert_speed_to_kbps, LIBRENMS_TO_NETBOX_MAPPING
from django.utils.safestring import mark_safe
from django.utils.html import format_html
from .models import InterfaceTypeMapping
from django_tables2 import CheckBoxColumn


class LibreNMSInterfaceTable(tables.Table):
    """
    Table for displaying LibreNMS interface data.
    """
    selection = CheckBoxColumn(accessor='ifName', orderable=False)
    ifName = tables.Column(verbose_name="Interface Name")
    ifType = tables.Column(verbose_name="Interface Type")
    ifSpeed = tables.Column(verbose_name="Speed")
    enabled = BooleanColumn(verbose_name="Enabled")
    ifDescr = tables.Column(accessor="ifAlias", verbose_name="Description")

    def render_ifType(self, value, record):
        mapping = InterfaceTypeMapping.objects.filter(librenms_type=value).first()
        
        if mapping:
            display_value = mapping.netbox_type
            icon = format_html('<i class="mdi mdi-link-variant" title="Mapped from LibreNMS type: {}"></i>', value)
        else:
            display_value = value
            icon = format_html('<i class="mdi mdi-link-variant-off" title="No mapping to NetBox type"></i>')
        
        display_value = format_html("{} {}", display_value, icon)
        
        if not record.get('exists_in_netbox'):
            return format_html('<span class="text-danger">{}</span>', display_value)
        
        netbox_interface = record.get('netbox_interface')
        if netbox_interface:
            netbox_type = getattr(netbox_interface, 'type', None)
            if mapping and mapping.netbox_type == netbox_type:
                return format_html('<span class="text-success">{}</span>', display_value)
            elif mapping:
                return format_html('<span class="text-warning">{}</span>', display_value)
            
        return format_html('<span class="text-danger">{}</span>', display_value)

    def render_ifSpeed(self, value, record):
        # Convert librenms speed to Kbps from bps
        kbps_value = convert_speed_to_kbps(value)
        return self._render_field(humanize_speed(kbps_value), record, 'ifSpeed', 'speed')

    def render_ifName(self, value, record):
        return self._render_field(value, record, 'ifName', 'name')

    # Uncomment to use 'True' or 'False' with coloring instead of tick or cross
    def render_enabled(self, value, record):
        enabled = value.lower() == 'up' if isinstance(value, str) else bool(value)
        display_value = 'Enabled' if enabled else 'Disabled'

        if not record.get('exists_in_netbox'):
            return format_html('<span class="text-danger">{}</span>', display_value)

        netbox_interface = record.get('netbox_interface')
        if netbox_interface:
            netbox_enabled = netbox_interface.enabled
            if enabled == netbox_enabled:
                return format_html('<span class="text-success">{}</span>', display_value)
            else:
                return format_html('<span class="text-warning">{}</span>', display_value)

        return format_html('<span class="text-danger">{}</span>', display_value)
    
    def render_ifDescr(self, value, record):
        return self._render_field(value, record, 'ifAlias', 'description')

    def _render_field(self, value, record, librenms_key, netbox_key):
        if not record.get('exists_in_netbox'):
            return mark_safe(f'<span class="text-danger">{value}</span>')
        netbox_value = getattr(record['netbox_interface'], netbox_key, None)
        librenms_value = record.get(librenms_key)
        if librenms_key == 'ifSpeed':
            librenms_value = convert_speed_to_kbps(librenms_value)
        if librenms_value != netbox_value:
            return mark_safe(f'<span class="text-warning">{value}</span>')
        return mark_safe(f'<span class="text-success">{value}</span>')

    def configure(self, request):
        paginate = {
            'paginator_class': EnhancedPaginator,
            'per_page': get_paginate_count(request)
        }
        tables.RequestConfig(request, paginate).configure(self)

    class Meta:
        attrs = {
            'class': 'table table-hover table-headings table-striped',
            'id': 'librenms-interface-table'
        }
        empty_text = "No data available"

    @staticmethod
    def get_row_class(record):
        print("get_row_class called")
        if not record.get('exists_in_netbox'):
            return 'table-danger'

        netbox_interface = record.get('netbox_interface')
        if netbox_interface:
            for librenms_key, netbox_key in LIBRENMS_TO_NETBOX_MAPPING.items():
                if record.get(librenms_key) != getattr(netbox_interface, netbox_key, None):
                    return 'table-warning'

        return 'table-success'


class InterfaceTypeMappingTable(NetBoxTable):
    librenms_type = tables.Column(
        verbose_name='LibreNMS Type'
    )
    netbox_type = tables.Column(
        verbose_name='NetBox Type'
    )
    actions = columns.ActionsColumn(
        actions=('edit', 'delete')
    )

    class Meta:
        model = InterfaceTypeMapping
        fields = ('id', 'librenms_type', 'netbox_type', 'actions')
        default_columns = ('librenms_type', 'netbox_type', 'actions')
        attrs = {
            'class': 'table table-hover table-headings table-striped'
        }
