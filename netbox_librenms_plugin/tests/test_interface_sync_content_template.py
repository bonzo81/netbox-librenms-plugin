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
    def _render(self, *, migrated, netbox_only=(), winner=None, has_write=False):
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
            "has_write_permission": has_write,
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

    def test_migrated_mode_hides_destructive_delete_controls(self):
        # Migrated mode is move-only: the donor-side bulk-delete UI (select-all + per-row
        # checkboxes + "Delete Selected Interfaces") must not render next to the Move actions,
        # or a donor could destructively delete interfaces mid-migration.
        html = self._render(
            migrated={"server_key": "default", "device_id": 1, "at": "now"},
            netbox_only=[{"id": 1, "name": "eth-only"}],
        )
        assert "select-all-netbox-interfaces" not in html
        assert "netbox-interface-checkbox" not in html
        assert "Delete Selected Interfaces" not in html

    def test_normal_mode_keeps_delete_controls(self):
        # Without a migration marker the bulk-delete UI is the intended affordance and must render.
        html = self._render(migrated=None, netbox_only=[{"id": 1, "name": "eth-only"}])
        assert "select-all-netbox-interfaces" in html
        assert "netbox-interface-checkbox" in html
        assert "Delete Selected Interfaces" in html

    def test_migrated_warning_describes_move_not_delete(self):
        # In migrated (move) mode the modal warning must not threaten permanent deletion.
        html = self._render(
            migrated={"server_key": "default", "device_id": 1, "at": "now"},
            netbox_only=[{"id": 1, "name": "eth-only"}],
        )
        assert "Moving an interface reassigns it" in html
        assert "permanently remove them from NetBox" not in html

    def test_normal_warning_describes_delete(self):
        html = self._render(migrated=None, netbox_only=[{"id": 1, "name": "eth-only"}])
        assert "permanently remove them from NetBox" in html
        assert "Moving an interface reassigns it" not in html

    def test_delete_checkboxes_have_accessible_names(self):
        # The select-all and per-row checkboxes must carry aria-labels for screen-reader users.
        html = self._render(migrated=None, netbox_only=[{"id": 1, "name": "eth-only"}])
        assert 'aria-label="Select all NetBox-only interfaces"' in html
        assert 'aria-label="Select interface eth-only"' in html

    def test_migrated_mode_hides_exclude_from_sync_controls(self):
        # The POST form is gone in migrated mode, so the sync-only "Exclude from Sync" checkboxes
        # must not render as active controls with nowhere to submit.
        html = self._render(migrated={"server_key": "default", "device_id": 1, "at": "now"})
        assert "Exclude from Sync:" not in html

    def test_normal_mode_shows_exclude_from_sync_controls(self):
        html = self._render(migrated=None)
        assert "Exclude from Sync:" in html

    def test_migrated_move_button_hidden_for_read_only_users(self):
        """The migrated 'Move' action is a mutating HTMX POST; without write permission it must not render as a live button (it would only fail at the permission gate) — show muted 'read-only' text instead."""
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("iface-winner-dev")
        iface = {"id": 1, "name": "eth-only", "type": "1000base-t", "enabled": True, "url": "/dcim/x/"}
        marker = {"server_key": "default", "device_id": 1, "at": "now"}

        # Read-only must NOT render the Move button. (The button + its interface_move_to_winner
        # URL belong to the migrate feature on feat/device-merge; the write-permission render is
        # covered there, where that URL is registered — on this branch rendering it would
        # NoReverseMatch, which is itself how this asserts the button stays hidden.)
        ro = self._render(migrated=marker, netbox_only=[iface], winner=winner, has_write=False)
        assert "interface_move_to_winner" not in ro  # no live mutating button for read-only users
        assert "read-only" in ro
