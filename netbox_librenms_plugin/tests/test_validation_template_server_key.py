"""All device_conflict_action forms must carry the active LibreNMS server key.

A structural check of the real template source (the htmx fragment is too branch-heavy to render
standalone under the strict test template settings).
"""

import pathlib
import re

from django.template.loader import get_template

TEMPLATE = "netbox_librenms_plugin/htmx/device_validation_details.html"

HIDDEN_SERVER_KEY_INCLUDE = re.compile(
    r'{%\s*include\s+["\']netbox_librenms_plugin/inc/_hidden_server_key\.html["\']\s*%}'
)


def _named_control_tags(form_html, name):
    for tag in re.findall(r"<(?:input|button)\b[^>]*>", form_html, re.DOTALL | re.IGNORECASE):
        name_pattern = rf"(?:^|\s)name\s*=\s*(['\"]){re.escape(name)}\1(?=\s|/?>)"
        if re.search(name_pattern, tag, re.IGNORECASE):
            yield tag


def _form_actions(form_html):
    actions = set()
    for tag in _named_control_tags(form_html, "action"):
        value = re.search(
            r"(?:^|\s)value\s*=\s*(['\"])(.*?)\1(?=\s|/?>)",
            tag,
            re.DOTALL | re.IGNORECASE,
        )
        if value:
            actions.add(value.group(2))
    return actions


def _form_has_named_control(form_html, name):
    return next(_named_control_tags(form_html, name), None) is not None


def _form_has_hidden_input(form_html, name):
    for tag in _named_control_tags(form_html, name):
        if tag.lstrip().lower().startswith("<input") and re.search(
            r"(?:^|\s)type\s*=\s*(['\"])hidden\1(?=\s|/?>)",
            tag,
            re.IGNORECASE,
        ):
            return True
    return False


def test_form_actions_accepts_attributes_between_name_and_value():
    """An action input stays visible to the server-key scan when it gains another attribute."""
    form = '<input type="hidden" name="action" class="mapping-action" value="link">'

    assert _form_actions(form) == {"link"}


def test_form_actions_ignores_data_attributes():
    """Metadata attributes must not make a non-action input look like an action control."""
    form = '<input type="hidden" data-name="action" data-value="link">'

    assert _form_actions(form) == set()


def test_form_named_control_ignores_data_attributes():
    """A data-name attribute must not satisfy the server-key input scan."""
    form = '<input type="hidden" data-name="server_key" value="secondary">'

    assert not _form_has_named_control(form, "server_key")


def test_server_key_matcher_requires_a_hidden_input():
    """A submit button must not stand in for the server-scoping hidden input."""
    form = '<button name="server_key" value="secondary">Submit</button>'

    assert not _form_has_hidden_input(form, "server_key")


def test_all_conflict_action_forms_include_server_key():
    src = pathlib.Path(get_template(TEMPLATE).origin.name).read_text()
    partial = pathlib.Path(get_template("netbox_librenms_plugin/inc/_hidden_server_key.html").origin.name).read_text()
    assert _form_has_hidden_input(partial, "server_key")

    forms = [f for f in re.findall(r"<form\b.*?</form>", src, re.DOTALL) if "device_conflict_action" in f]

    assert forms, "expected device_conflict_action forms in the template"

    missing = [
        sorted(_form_actions(f))
        for f in forms
        if not _form_has_hidden_input(f, "server_key") and not HIDDEN_SERVER_KEY_INCLUDE.search(f)
    ]
    assert not missing, f"device conflict forms missing a server_key hidden input: {missing}"
