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
HIDDEN_SERVER_KEY_INCLUDE = re.compile(
    r'{%\s*include\s+["\']netbox_librenms_plugin/inc/_hidden_server_key\.html["\']\s*%}'
)


def _form_actions(form_html):
    actions = set()
    for tag in re.findall(r"<(?:input|button)\b[^>]*>", form_html, re.DOTALL | re.IGNORECASE):
        if not re.search(r"(?:^|\s)name\s*=\s*(['\"])action\1(?=\s|/?>)", tag, re.IGNORECASE):
            continue
        value = re.search(
            r"(?:^|\s)value\s*=\s*(['\"])(.*?)\1(?=\s|/?>)",
            tag,
            re.DOTALL | re.IGNORECASE,
        )
        if value:
            actions.add(value.group(2))
    return actions


def test_form_actions_accepts_attributes_between_name_and_value():
    """An action input stays visible to the server-key scan when it gains another attribute."""
    form = '<input type="hidden" name="action" class="mapping-action" value="link">'

    assert _form_actions(form) == {"link"}


def test_form_actions_ignores_data_attributes():
    """Metadata attributes must not make a non-action input look like an action control."""
    form = '<input type="hidden" data-name="action" data-value="link">'

    assert _form_actions(form) == set()


def test_mapping_writing_conflict_forms_include_server_key():
    src = pathlib.Path(get_template(TEMPLATE).origin.name).read_text()
    partial = pathlib.Path(get_template("netbox_librenms_plugin/inc/_hidden_server_key.html").origin.name).read_text()
    assert 'name="server_key"' in partial

    forms = [f for f in re.findall(r"<form\b.*?</form>", src, re.DOTALL) if "device_conflict_action" in f]

    mapping_forms = [f for f in forms if _form_actions(f) & MAPPING_ACTIONS]
    assert mapping_forms, "expected mapping-writing device_conflict_action forms in the template"

    missing = [
        sorted(_form_actions(f))
        for f in mapping_forms
        if 'name="server_key"' not in f and not HIDDEN_SERVER_KEY_INCLUDE.search(f)
    ]
    assert not missing, f"mapping-writing forms missing a server_key hidden input: {missing}"
