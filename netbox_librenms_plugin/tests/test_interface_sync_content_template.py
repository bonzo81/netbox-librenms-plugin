"""Render the real _interface_sync_content.html template in both modes.

In migrated mode the POST form is replaced by a plain <div> (a migrated donor must not be
able to POST an interface sync). The form-only hidden inputs (server_key) must travel with
the <form> and never the inert <div>. The CSRF token is the deliberate exception: the
interface table still renders interactive relationship/VC-member dropdowns whose
verify-interface POST reads document.querySelector('[name=csrfmiddlewaretoken]').value, so
a standalone token must be emitted in migrated mode too — otherwise those JS requests hit a
null token (TypeError/403).
"""

import pytest


@pytest.mark.django_db
class TestInterfaceSyncContentTemplateMigratedMode:
    def _render(self, *, migrated):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("iface-tmpl-dev")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()  # NetBox context processors read request.user
        table = LibreNMSInterfaceTable([], device=device, server_key="default")
        RequestConfig(request).configure(table)
        interface_sync = {
            "object": device,
            "table": table,
            "server_key": "default",
            "netbox_only_interfaces": [],
            "virtual_chassis_members": [],
            "cache_expiry": None,
            "oob_incomplete": False,
        }
        ctx = {
            "interface_sync": interface_sync,
            "interface_name_field": "ifName",
            "migrated_to_marker": migrated,
            "migrated_to_winner": None,
            "has_write_permission": False,
        }
        return render_to_string("netbox_librenms_plugin/_interface_sync_content.html", ctx, request=request)

    def test_migrated_mode_drops_form_and_server_key_but_keeps_csrf_token(self):
        html = self._render(migrated={"server_key": "default", "device_id": 1, "at": "now"})
        # The live POST form and its form-only hidden input must be gone in migrated mode.
        assert "<form" not in html
        assert 'name="server_key"' not in html
        # ...but the CSRF token must remain so JS-driven verify-interface POSTs still work.
        assert "csrfmiddlewaretoken" in html

    def test_normal_mode_emits_form_with_csrf_and_server_key(self):
        html = self._render(migrated=None)
        assert "<form" in html
        assert "csrfmiddlewaretoken" in html
        assert 'name="server_key"' in html
