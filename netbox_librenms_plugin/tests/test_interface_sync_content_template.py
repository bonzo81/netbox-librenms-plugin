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
        # In migrated (move) mode WITH a resolved winner, the modal warning must not threaten deletion.
        from netbox_librenms_plugin.tests.conftest import make_device

        html = self._render(
            migrated={"server_key": "default", "device_id": 1, "at": "now"},
            netbox_only=[{"id": 1, "name": "eth-only"}],
            winner=make_device("iface-warn-winner"),
        )
        assert "Moving an interface reassigns it" in html
        assert "permanently remove them from NetBox" not in html

    def test_migrated_warning_handles_missing_winner(self):
        """With the marker present but the winner gone (stale), the warning must not instruct a Move to a non-existent winner."""
        html = self._render(
            migrated={"server_key": "default", "device_id": 1, "at": "now"},
            netbox_only=[{"id": 1, "name": "eth-only"}],
            winner=None,
        )
        assert "migration winner is unavailable" in html
        assert "Moving an interface reassigns it" not in html  # the move instruction is gated out

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

    @staticmethod
    def _patch_move_url_reverse(*, resolve):
        """Patch ``django.urls.reverse`` so ``interface_move_to_winner`` looks registered
        (``resolve=True`` → a fake path) or unregistered (``resolve=False`` → ``NoReverseMatch``),
        while every other viewname resolves for real. The move URL is only registered up-stack, so
        forcing the state here keeps these guard tests branch-independent. ``django.urls.reverse``
        is the correct target because ``{% url %}`` re-imports ``reverse`` from ``django.urls`` at
        render. Returns a ``patch()`` context manager.
        """
        from unittest.mock import patch

        from django.urls import NoReverseMatch
        from django.urls import reverse as real_reverse

        def _reverse(viewname, *args, **kwargs):
            if str(viewname).endswith("interface_move_to_winner"):
                if resolve:
                    return "/fake/interface-move/1/"
                raise NoReverseMatch(viewname)
            return real_reverse(viewname, *args, **kwargs)

        return patch("django.urls.reverse", _reverse)

    def test_migrated_move_button_hidden_for_read_only_users(self):
        """The migrated 'Move' action is a mutating HTMX POST; without write permission it must not render as a live button (it would only fail at the permission gate) — show muted 'read-only' text instead."""
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("iface-winner-dev")
        iface = {"id": 1, "name": "eth-only", "type": "1000base-t", "enabled": True, "url": "/dcim/x/"}
        marker = {"server_key": "default", "device_id": 1, "at": "now"}

        # Make the move URL resolvable so this test exercises the write-permission gate itself, not
        # the branch-dependent absence of the URL (registered only up-stack); the negative assertion
        # can then actually FAIL if the has_write_permission guard is removed.
        with self._patch_move_url_reverse(resolve=True):
            ro = self._render(migrated=marker, netbox_only=[iface], winner=winner, has_write=False)
        # Assert on the button's own rendered content, not the URL *name* (which never appears in
        # HTML — the template emits the resolved path). The live Move button carries this confirm text.
        assert "Move interface '" not in ro
        assert "/fake/interface-move/1/" not in ro
        assert "read-only" in ro

    def test_migrated_move_button_write_perm_degrades_when_url_unregistered(self):
        """With write perm, an unregistered move-to-winner URL must degrade to read-only, not 500."""
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("iface-winner-wp")
        iface = {"id": 1, "name": "eth-only", "type": "1000base-t", "enabled": True, "url": "/dcim/x/"}
        marker = {"server_key": "default", "device_id": 1, "at": "now"}

        # Force the forward-declared move URL to look unregistered on ANY branch (it IS registered
        # up-stack) so the {% url ... as %} degrade path is exercised wherever this runs. Unfixed
        # (bare {% url %}) this raises NoReverseMatch; the {% url ... as %} guard renders the
        # read-only fallback instead, so the migrated tab never 500s where the URL isn't registered.
        with self._patch_move_url_reverse(resolve=False):
            html = self._render(migrated=marker, netbox_only=[iface], winner=winner, has_write=True)
        # Assert on the button's rendered content (the confirm text), not the URL name: with the URL
        # unresolved, move_url is '' so the live button must be absent and the read-only span shown.
        assert "Move interface '" not in html
        assert "read-only" in html

    def test_migrated_move_button_renders_for_write_users_when_url_registered(self):
        """Positive counterpart: with write perm + a resolvable move URL the live button renders.

        This proves the negative assertions above key off the button's real rendered content — i.e.
        they would actually fail if the button leaked into a read-only / unregistered render.
        """
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("iface-winner-write")
        iface = {"id": 1, "name": "eth-only", "type": "1000base-t", "enabled": True, "url": "/dcim/x/"}
        marker = {"server_key": "default", "device_id": 1, "at": "now"}

        with self._patch_move_url_reverse(resolve=True):
            html = self._render(migrated=marker, netbox_only=[iface], winner=winner, has_write=True)
        assert "Move interface '" in html
        assert 'hx-post="/fake/interface-move/1/"' in html
        assert "read-only" not in html

    def test_move_button_emits_server_key_hx_vals_when_marker_has_key(self):
        # When the migrated marker carries a server_key, the migrated-mode Move button must
        # post it so the move hits the right LibreNMS server/cache (non-default servers).
        # has_write=True so the Move button renders (it's gated on write permission).
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("iface-tmpl-winner")
        # Alphanumeric key so escapejs leaves it intact (it escapes e.g. '-' to -); the
        # guard behaviour, not escapejs, is what this test pins.
        html = self._render(
            migrated={"server_key": "edgelondon", "device_id": 1, "at": "now"},
            winner=winner,
            netbox_only=[{"id": 1, "name": "eth-only"}],
            has_write=True,
        )
        # The Move button renders for the NetBox-only row and carries the server_key. Scope the
        # hx-vals assertion to the Move button's own tag (mirroring the fallback test) so a
        # different element carrying the key can't mask the button dropping its hx-vals.
        assert "mdi-transfer-right" in html
        move_idx = html.index("mdi-transfer-right")
        btn_start = html.rindex("<button", 0, move_idx)
        move_button_tag = html[btn_start : html.index(">", btn_start)]
        assert 'hx-vals=\'{"server_key": "edgelondon"}\'' in move_button_tag

    def test_move_button_falls_back_to_active_server_key_when_marker_has_no_key(self):
        # When the marker carries no server_key, the Move button must fall back to the active
        # interface_sync.server_key (the server the donor is being viewed under) rather than drop
        # the discriminator: omitting it lets the move resolve the marker against the session/default
        # server, which on a multi-server install can be the WRONG server. Never POST an empty key.
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("iface-tmpl-winner-nokey")
        html = self._render(
            migrated={"device_id": 1, "at": "now"},  # marker has NO server_key
            winner=winner,
            netbox_only=[{"id": 1, "name": "eth-only"}],
            has_write=True,
        )
        # The Move button still renders, and never with an empty server_key payload.
        assert "mdi-transfer-right" in html
        assert 'hx-vals=\'{"server_key": ""}\'' not in html
        # Scope to the move button's own opening tag: it must carry the active server_key
        # (interface_sync.server_key == "default" in this harness) as the fallback discriminator.
        move_idx = html.index("mdi-transfer-right")
        btn_start = html.rindex("<button", 0, move_idx)
        move_button_tag = html[btn_start : html.index(">", btn_start)]
        assert 'hx-vals=\'{"server_key": "default"}\'' in move_button_tag
