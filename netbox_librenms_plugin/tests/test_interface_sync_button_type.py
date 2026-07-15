"""Issue #116 (CodeRabbit): the HTMX "Refresh Interfaces" buttons in _interface_sync.html
must declare type="button". They live inside a <form method="post"> and drive their POST via
hx-post; the HTML default of type="submit" would also fire a native form submit on click.

The device branch is rendered for real against a real Device (the ``meta`` filter resolves
model_name to "device"); a source-structure check covers the VM branch too without heavy VM
scaffolding.
"""

import pathlib
import re

import pytest
from django.contrib.auth.models import AnonymousUser
from django.template.loader import get_template, render_to_string
from django.test import RequestFactory

TEMPLATE = "netbox_librenms_plugin/_interface_sync.html"


def test_every_button_declares_type():
    """Every <button> in the template declares an explicit type (no implicit submit)."""
    src = pathlib.Path(get_template(TEMPLATE).origin.name).read_text()
    buttons = re.findall(r"<button\b[^>]*>", src, re.DOTALL)
    assert buttons, "expected at least one <button> in the template"
    untyped = [b for b in buttons if not re.search(r'\btype\s*=\s*"[^"]+"', b)]
    assert not untyped, f"buttons without an explicit type attribute: {untyped}"


@pytest.mark.django_db
def test_device_refresh_button_renders_type_button():
    """The device Refresh-Interfaces button renders with type="button", not a submit."""
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

    mfr, _ = Manufacturer.objects.get_or_create(name="ACME-116b", slug="acme-116b")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-116b", slug="dt-116b")
    role, _ = DeviceRole.objects.get_or_create(name="Role-116b", slug="role-116b")
    site, _ = Site.objects.get_or_create(name="Site-116b", slug="site-116b")
    device = Device.objects.create(name="btn-type-host", device_type=dt, role=role, site=site, status="active")

    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    html = render_to_string(
        TEMPLATE,
        {"object": device, "has_librenms_id": True, "server_key": "default"},
        request=request,
    )

    # Scope to the Refresh Interfaces button(s) — the included content template renders other
    # buttons (modal close etc.) that are out of scope for this finding.
    refresh_buttons = [b for b in re.findall(r"<button\b.*?</button>", html, re.DOTALL) if "Refresh Interfaces" in b]
    assert refresh_buttons, "device branch should render a Refresh Interfaces button"
    assert all('type="button"' in b for b in refresh_buttons), refresh_buttons
