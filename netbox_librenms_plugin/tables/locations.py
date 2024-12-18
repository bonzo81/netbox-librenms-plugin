import django_tables2 as tables
from django.middleware.csrf import get_token
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from utilities.paginator import EnhancedPaginator, get_paginate_count


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
