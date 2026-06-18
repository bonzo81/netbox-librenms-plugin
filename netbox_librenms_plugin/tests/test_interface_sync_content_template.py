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
    def _render(self, *, migrated, netbox_only=(), winner=None):
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
            # Caller-controlled: an item makes the NetBox-only modal (and its trigger link, whose
            # title we assert) render. Kept empty by default so form-presence tests aren't perturbed
            # by the modal's own <form>.
            "netbox_only_interfaces": list(netbox_only),
            "virtual_chassis_members": [],
            "cache_expiry": None,
            "oob_incomplete": False,
        }
        ctx = {
            "interface_sync": interface_sync,
            "interface_name_field": "ifName",
            "migrated_to_marker": migrated,
            "migrated_to_winner": winner,
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

    def test_netbox_only_link_title_is_move_in_migrated_mode(self):
        # Migrated mode is transfer-only, so the NetBox-only modal trigger must advertise "move",
        # not "delete" (the modal has no delete action for a donor).
        html = self._render(
            migrated={"server_key": "default", "device_id": 1, "at": "now"},
            netbox_only=[{"id": 1, "name": "eth-only"}],
        )
        assert "Click to view and move NetBox-only interfaces" in html
        assert "Click to view and delete NetBox-only interfaces" not in html

    def test_netbox_only_link_title_is_delete_in_normal_mode(self):
        html = self._render(migrated=None, netbox_only=[{"id": 1, "name": "eth-only"}])
        assert "Click to view and delete NetBox-only interfaces" in html
        assert "Click to view and move NetBox-only interfaces" not in html

    def test_move_button_emits_server_key_hx_vals_when_marker_has_key(self):
        # When the migrated marker carries a server_key, the migrated-mode Move button must
        # post it so the move hits the right LibreNMS server/cache (non-default servers).
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("iface-tmpl-winner")
        # Alphanumeric key so escapejs leaves it intact (it escapes e.g. '-' to -); the
        # guard behaviour, not escapejs, is what this test pins.
        html = self._render(
            migrated={"server_key": "edgelondon", "device_id": 1, "at": "now"},
            winner=winner,
            netbox_only=[{"id": 1, "name": "eth-only"}],
        )
        # The Move button renders for the NetBox-only row and carries the server_key.
        assert "mdi-transfer-right" in html
        assert 'hx-vals=\'{"server_key": "edgelondon"}\'' in html

    def test_move_button_omits_server_key_hx_vals_when_marker_has_no_key(self):
        # Mirror the form-mode guard (lines 29/35): when the marker has no server_key, the Move
        # button must omit hx-vals entirely rather than POST an empty {"server_key": ""}, which
        # would override the active/default server with a blank key on a non-default install.
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("iface-tmpl-winner-nokey")
        html = self._render(
            migrated={"device_id": 1, "at": "now"},  # no server_key
            winner=winner,
            netbox_only=[{"id": 1, "name": "eth-only"}],
        )
        # The Move button still renders...
        assert "mdi-transfer-right" in html
        # ...but never with an empty server_key payload.
        assert '"server_key": ""' not in html
        assert 'hx-vals=\'{"server_key": ""}\'' not in html
