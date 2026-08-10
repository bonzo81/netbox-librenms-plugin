"""Render the real _cable_sync_content.html template in both modes.

In migrated mode the POST form is replaced by a plain <div> (a migrated donor must not POST a
cable sync). But the cable table still renders interactive controls whose verify-cable fetch
(handleCableChange) reads document.querySelector('[name=csrfmiddlewaretoken]').value and
input[name="server_key"] — so standalone CSRF + server_key hidden inputs must be emitted in
migrated mode too, or those JS requests hit a null token (TypeError/403) / the wrong server.
"""

import pytest


@pytest.mark.django_db
class TestCableSyncContentTemplateMigratedMode:
    def _render(self, *, migrated, server_key="default"):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.tables.cables import LibreNMSCableTable
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("cable-tmpl-dev")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()  # NetBox context processors read request.user
        table = LibreNMSCableTable([], device=device)
        RequestConfig(request).configure(table)
        cable_sync = {
            "object": device,
            "table": table,
            "server_key": server_key,
            "cache_expiry": None,
        }
        ctx = {
            "cable_sync": cable_sync,
            "migrated_to_marker": migrated,
            "migrated_to_winner": None,
            "has_write_permission": False,
        }
        return render_to_string("netbox_librenms_plugin/_cable_sync_content.html", ctx, request=request)

    def test_migrated_mode_drops_form_but_keeps_csrf_and_server_key(self):
        # Non-default server so the assertion proves the actual value is emitted.
        html = self._render(
            migrated={"server_key": "prod", "device_id": 1, "at": "now"},
            server_key="prod",
        )
        # The live POST form must be gone in migrated mode (a donor must not POST a sync).
        assert "<form" not in html
        # ...but the CSRF token AND server_key must remain so the JS-driven verify-cable fetch
        # (handleCableChange) targets the right server with a usable token.
        assert "csrfmiddlewaretoken" in html
        assert 'name="server_key"' in html
        assert 'value="prod"' in html

    def test_normal_mode_emits_form_with_csrf_and_server_key(self):
        html = self._render(migrated=None)
        assert "<form" in html
        assert "csrfmiddlewaretoken" in html
        assert 'name="server_key"' in html

    def test_verify_url_uses_the_active_script_prefix(self):
        from django.urls import get_script_prefix, set_script_prefix

        previous_prefix = get_script_prefix()
        try:
            set_script_prefix("/netbox/")
            html = self._render(migrated=None)
        finally:
            set_script_prefix(previous_prefix)

        assert 'data-cable-verify-url="/netbox/plugins/librenms_plugin/verify-cable/"' in html

    def test_render_local_port_link_branch_normalizes_missing_name(self):
        """A linked local port with no name renders empty text, not the literal 'None'."""
        from netbox_librenms_plugin.tables.cables import LibreNMSCableTable
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("cable-render-dev")
        table = LibreNMSCableTable([], device=device)

        html = table.render_local_port(value=None, record={"local_port_url": "/dcim/interfaces/1/"})

        assert 'href="/dcim/interfaces/1/"' in html
        assert ">None<" not in html

    def _remote_port_table(self):
        from netbox_librenms_plugin.tables.cables import LibreNMSCableTable
        from netbox_librenms_plugin.tests.conftest import make_device

        return LibreNMSCableTable([], device=make_device("cable-render-remote-dev"))

    def test_render_remote_port_link_branch_normalizes_missing_name(self):
        """A linked remote port with no name renders empty link text, not the literal 'None'."""
        html = self._remote_port_table().render_remote_port(
            value=None, record={"remote_port_url": "/dcim/console-ports/1/"}
        )

        assert 'href="/dcim/console-ports/1/"' in html
        assert ">None<" not in html

    def test_render_remote_port_manual_badge_branch_normalizes_missing_name(self):
        """A manually picked remote with no port name renders only the badge, not 'None' + badge."""
        html = self._remote_port_table().render_remote_port(value=None, record={"manual_remote": True})

        assert "mdi-gesture-tap-button" in html
        assert "None" not in html

    def test_render_remote_port_bare_branch_normalizes_missing_name(self):
        """No URL, no badge, no name: the cell renders empty, not the literal 'None'."""
        html = self._remote_port_table().render_remote_port(value=None, record={})

        assert html == ""

    def test_picker_button_carries_accessible_name(self):
        """The icon-only picker button has an aria-label (title alone isn't reliably announced)."""
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.tables.cables import LibreNMSCableTable
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.utils import assign_cable_row_ids

        device = make_device("cable-picker-a11y-dev")
        row = {
            "local_port_id": "serial:1",
            "local_port": "ttyS1",
            "device_id": device.id,
            "picker_url": "/plugins/librenms_plugin/picker/1/",
        }
        table = LibreNMSCableTable(assign_cable_row_ids([row]), device=device)
        request = RequestFactory().get("/")
        RequestConfig(request).configure(table)

        html = table.as_html(request)

        assert 'hx-get="/plugins/librenms_plugin/picker/1/"' in html
        assert 'aria-label="Pick remote end"' in html
