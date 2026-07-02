"""Issue #106: device_conflict_action forms that write a server-scoped LibreNMS mapping
(link / update / update_serial — the serial/hostname-match forms) must carry a server_key
hidden input, or on a non-default server the POST rebind silently falls back to "default" and
writes the wrong mapping/cache scope. Field-sync actions (sync_name, sync_device_type,
sync_platform, …) only touch NetBox fields and need no server_key, so they are exempt.

A structural check of the real template source (the htmx fragment is too branch-heavy to render
standalone under the strict test template settings).
"""

import pathlib
import re

from django.template.loader import get_template

TEMPLATE = "netbox_librenms_plugin/htmx/device_validation_details.html"

# Actions that persist a server-scoped librenms_id mapping (need the active server_key).
MAPPING_ACTIONS = {"link", "update", "update_serial", "migrate_librenms_id"}


def _form_actions(form_html):
    return set(
        re.findall(r'name="action"\s+value="([^"]+)"', form_html)
        + re.findall(r'value="([^"]+)"\s+name="action"', form_html)
    )


def test_mapping_writing_conflict_forms_include_server_key():
    src = pathlib.Path(get_template(TEMPLATE).origin.name).read_text()
    forms = [f for f in re.findall(r"<form\b.*?</form>", src, re.DOTALL) if "device_conflict_action" in f]

    mapping_forms = [f for f in forms if _form_actions(f) & MAPPING_ACTIONS]
    assert mapping_forms, "expected mapping-writing device_conflict_action forms in the template"

    missing = [sorted(_form_actions(f)) for f in mapping_forms if 'name="server_key"' not in f]
    assert not missing, f"mapping-writing forms missing a server_key hidden input: {missing}"
