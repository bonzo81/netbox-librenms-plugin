"""Render the real _vlan_sync_content.html template in both modes.

In migrated mode the POST form is replaced by a plain <div> (a migrated donor must not POST a
VLAN sync). But the VLAN table still renders interactive per-row group selects whose verify JS
(librenms_sync.js verify-vlan-group / verify-vlan-sync-group) reads
document.querySelector('[name=csrfmiddlewaretoken]').value and posts server_key — so standalone
CSRF + server_key hidden inputs must be emitted in migrated mode too, or those JS requests hit a
null token (TypeError/403) / the wrong server. Only the form-submit ``action`` input is form-only.
"""

import re

import pytest


@pytest.mark.django_db
class TestVlanSyncContentTemplateMigratedMode:
    def _render(self, *, migrated, server_key="default"):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("vlan-tmpl-dev")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()  # NetBox context processors read request.user
        # At least one row so vlan_table.rows is truthy and the form/CSRF branch renders.
        table = LibreNMSVLANTable(
            [{"vlan_id": 10, "name": "v10", "type": "ethernet", "state": "active"}],
            vlan_groups=[],
        )
        RequestConfig(request).configure(table)
        vlan_sync = {
            "object": device,
            "vlan_table": table,
            "server_key": server_key,
            "cache_expiry": None,
        }
        ctx = {
            "vlan_sync": vlan_sync,
            "migrated_to_marker": migrated,
            "migrated_to_winner": None,
            "has_write_permission": False,
        }
        return render_to_string("netbox_librenms_plugin/_vlan_sync_content.html", ctx, request=request)

    def test_migrated_mode_drops_form_but_keeps_csrf_and_server_key(self):
        # Non-default server so the assertion proves the actual value is emitted.
        html = self._render(
            migrated={"server_key": "prod", "device_id": 1, "at": "now"},
            server_key="prod",
        )
        # The live POST form must be gone in migrated mode (a donor must not POST a sync).
        assert "<form" not in html
        # ...but CSRF + server_key must remain so the JS verify-vlan-group fetch targets the
        # right server with a USABLE token — an empty value would still POST X-CSRFToken: ''
        # and 403, so pin a non-empty value, not mere input presence.
        assert re.search(r'name="csrfmiddlewaretoken" value="[^"]+"', html)
        assert 'name="server_key"' in html
        assert 'value="prod"' in html
        # The form-submit action input is form-only and must NOT render in migrated mode.
        assert 'value="create_vlans"' not in html

    def test_normal_mode_emits_form_with_csrf_server_key_and_action(self):
        html = self._render(migrated=None)
        assert "<form" in html
        assert re.search(r'name="csrfmiddlewaretoken" value="[^"]+"', html)
        assert 'name="server_key"' in html
        assert 'value="create_vlans"' in html
