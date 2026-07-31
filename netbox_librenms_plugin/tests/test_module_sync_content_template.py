"""Render the real _module_sync_content.html template in both modes.

In migrated mode the Install-Selected POST form is dropped (a migrated donor must not be able
to install modules). But the module table still renders interactive VC-member dropdowns whose
verify-module POST reads document.querySelector('[name=csrfmiddlewaretoken]').value (no optional
chaining → TypeError/403 if missing) and the server_key input. So standalone csrf + server_key
hidden inputs must be emitted in migrated mode too, mirroring the interface/IP/VLAN fragments.
"""

import pytest


@pytest.mark.django_db
class TestModuleSyncContentTemplateMigratedMode:
    def _render(self, *, migrated, has_write_permission=False, server_key="default"):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("module-tmpl-dev")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()  # NetBox context processors read request.user
        table = LibreNMSModuleTable([], device=device, server_key=server_key)
        RequestConfig(request).configure(table)
        module_sync = {
            "object": device,
            "table": table,
            "server_key": server_key,
            "cache_expiry": None,
        }
        ctx = {
            "module_sync": module_sync,
            "migrated_to_marker": migrated,
            "has_write_permission": has_write_permission,
        }
        return render_to_string("netbox_librenms_plugin/_module_sync_content.html", ctx, request=request)

    def test_migrated_mode_drops_install_form_but_keeps_csrf_and_server_key(self):
        html = self._render(
            migrated={"server_key": "default", "device_id": 1, "at": "now"},
            has_write_permission=True,
        )
        # The Install-Selected form must be gone in migrated mode.
        assert 'id="install-selected-form"' not in html
        assert "install_selected" not in html
        # ...but the standalone CSRF token + server_key must remain so the JS-driven verify-module
        # POSTs (handleModuleChange) still authenticate and scope to the right server.
        assert "csrfmiddlewaretoken" in html
        assert 'name="server_key"' in html

    def test_normal_mode_emits_install_form_with_csrf_and_server_key(self):
        html = self._render(migrated=None, has_write_permission=True)
        assert 'id="install-selected-form"' in html
        assert "csrfmiddlewaretoken" in html
        assert 'name="server_key"' in html

    def test_normal_mode_omits_server_key_input_when_absent(self):
        # Single-server / default-server deployments have no server_key; the hidden input must be
        # omitted entirely (guarded with {% if %}) rather than emitted as value="", matching the
        # interface/IP/VLAN sync fragments so a blank key can't be POSTed as an empty string.
        html = self._render(migrated=None, has_write_permission=True, server_key="")
        assert 'id="install-selected-form"' in html
        assert 'name="server_key"' not in html

    def test_migrated_mode_omits_server_key_input_when_absent(self):
        html = self._render(
            migrated={"device_id": 1, "at": "now"},
            has_write_permission=True,
            server_key="",
        )
        # The standalone CSRF token still renders for the verify-module POST, but the server_key
        # input is omitted when there is no key.
        assert "csrfmiddlewaretoken" in html
        assert 'name="server_key"' not in html
