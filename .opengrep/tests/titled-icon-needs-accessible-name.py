from django.utils.html import format_html
from django.utils.safestring import mark_safe


def badges(value, reason):
    # ruleid: titled-icon-needs-accessible-name
    format_html('{} <i class="mdi mdi-lock-outline" title="{}"></i>', value, reason)
    mark_safe(
        # ruleid: titled-icon-needs-accessible-name
        '<i class="mdi mdi-alert text-warning ms-1" '
        'title="VID exists in multiple groups."></i>'
    )
    # ruleid: titled-icon-needs-accessible-name
    format_html('<i class="mdi mdi-alert" title="{}" aria-hidden="false"></i>', reason)
    # ruleid: titled-icon-needs-accessible-name
    format_html('<i class="mdi mdi-alert" role="img" title="{}" aria-label=""></i>', reason)
    # ruleid: titled-icon-needs-accessible-name
    format_html('<i class="mdi mdi-alert" role="img" title="{}" aria-label=" "></i>', reason)
    # ok:titled-icon-needs-accessible-name
    format_html('{} <i class="mdi mdi-lock-outline" role="img" title="{}" aria-label="{}"></i>', value, reason, reason)
    format_html(
        # ok: titled-icon-needs-accessible-name
        ' <i class="mdi mdi-alert text-danger" role="img" '
        'title="{}" aria-label="{}"></i>',
        reason,
        reason,
    )
    # ok: titled-icon-needs-accessible-name
    format_html('<i class="mdi mdi-plus-thick" aria-hidden="true" title="Add"></i> Add')
    # ok: titled-icon-needs-accessible-name
    format_html('<span title="{}"><i class="mdi mdi-wrench-outline"></i> {}</span>', reason, value)
    # ok: titled-icon-needs-accessible-name
    format_html('<i class="mdi mdi-alert" data-bs-title="{}"></i>', reason)
