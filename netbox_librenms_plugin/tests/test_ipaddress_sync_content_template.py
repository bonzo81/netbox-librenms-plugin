"""Render the real _ipaddress_sync_content.html template in normal vs migrated mode.

In migrated mode the POST form is removed (a migrated donor must not submit an IP sync), so the
sync-only 'Set Primary IP' switch must not render as an active control with nowhere to submit.
"""

import pytest


@pytest.mark.django_db
class TestIpAddressSyncContentTemplateMigratedMode:
    def _render(self, *, migrated, movable=(), winner=None, has_write=False, server_key="default"):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("ip-tmpl-dev")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        table = IPAddressTable([])
        RequestConfig(request).configure(table)
        ctx = {
            "ip_sync": {
                "object": device,
                "table": table,
                "server_key": server_key,
                "set_primary_ip": False,
                "cache_expiry": None,
                "movable_ips": list(movable),
            },
            "migrated_to_marker": migrated,
            "migrated_to_winner": winner,
            "has_write_permission": has_write,
        }
        return render_to_string("netbox_librenms_plugin/_ipaddress_sync_content.html", ctx, request=request)

    def test_migrated_mode_hides_set_primary_ip_switch(self):
        # The switch control itself must be gone (the "Set Primary IP" string also appears in an
        # ungated JS comment, so assert on the checkbox input id, not the label text).
        html = self._render(migrated={"server_key": "default", "device_id": 1, "at": "now"})
        assert 'id="set-primary-ip-toggle-cb"' not in html

    def test_normal_mode_shows_set_primary_ip_switch(self):
        html = self._render(migrated=None)
        assert 'id="set-primary-ip-toggle-cb"' in html

    def test_migrated_mode_renders_move_button_targeting_the_ip_view(self):
        """A write-permitted migrated donor shows a Move button posting to ipaddress_move_to_winner for the IP's pk."""
        from netbox_librenms_plugin.tests._html_helpers import patch_move_url_reverse
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("ip-tmpl-winner")
        movable = [{"id": 4321, "address": "10.0.0.5/24", "interface_name": "eth0"}]
        with patch_move_url_reverse("ipaddress_move_to_winner", resolve=True):
            html = self._render(
                migrated={"server_key": "default", "device_id": winner.pk, "at": "now"},
                movable=movable,
                winner=winner,
                has_write=True,
            )
        assert "/fake/ipaddress_move_to_winner/4321/" in html
        assert "10.0.0.5/24" in html
        assert f"to {winner.name}" in html  # the card header names the winner

    def test_move_button_hidden_for_read_only_users(self):
        """The Move action is a mutating POST; a read-only user sees muted 'read-only' text, not a live button."""
        from netbox_librenms_plugin.tests._html_helpers import patch_move_url_reverse
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("ip-tmpl-winner-ro")
        movable = [{"id": 77, "address": "10.0.0.9/24", "interface_name": "eth1"}]
        with patch_move_url_reverse("ipaddress_move_to_winner", resolve=True):
            html = self._render(
                migrated={"server_key": "default", "device_id": winner.pk, "at": "now"},
                movable=movable,
                winner=winner,
                has_write=False,
            )
        assert "/fake/ipaddress_move_to_winner/77/" not in html
        assert "read-only" in html

    def test_missing_winner_is_explained_above_the_move_table(self):
        """A deleted migration winner produces a clear warning instead of only per-row placeholders."""
        movable = [{"id": 78, "address": "10.0.0.10/24", "interface_name": "eth1"}]

        html = self._render(
            migrated={"server_key": "default", "device_id": 999999, "at": "now"},
            movable=movable,
            winner=None,
            has_write=True,
        )

        assert "migration winner is unavailable" in html.lower()
        assert "winner missing" in html

    def test_move_button_degrades_to_read_only_when_url_unregistered(self):
        """A missing/restacked ipaddress_move_to_winner route must degrade to read-only, not 500.

        The shared include uses ``{% url ... as move_url %}`` + ``and move_url``, so an unresolved
        route yields an empty move_url and the read-only fallback instead of a bare {% url %} that
        would NoReverseMatch and 500 the whole IP tab.
        """
        from netbox_librenms_plugin.tests._html_helpers import patch_move_url_reverse
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("ip-tmpl-winner-degrade")
        movable = [{"id": 88, "address": "10.0.0.11/24", "interface_name": "eth2"}]

        with patch_move_url_reverse("ipaddress_move_to_winner", resolve=False):
            html = self._render(
                migrated={"server_key": "default", "device_id": winner.pk, "at": "now"},
                movable=movable,
                winner=winner,
                has_write=True,
            )
        assert "Move IP '" not in html  # route unresolved → move_url empty → no live button
        assert "read-only" in html

    def test_no_move_card_outside_migrated_mode(self):
        """Without a migration marker the IP tab must not render the move card at all."""
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("ip-tmpl-winner-norm")
        movable = [{"id": 5, "address": "10.0.0.1/24", "interface_name": "eth0"}]
        html = self._render(migrated=None, movable=movable, winner=winner, has_write=True)
        assert "Move IP addresses" not in html
        assert "move-to-winner" not in html

    def test_no_move_card_when_no_movable_ips(self):
        """A migrated donor with no interface-assigned IPs renders no (empty) move card."""
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("ip-tmpl-winner-empty")
        html = self._render(
            migrated={"server_key": "default", "device_id": winner.pk, "at": "now"},
            movable=[],
            winner=winner,
            has_write=True,
        )
        assert "Move IP addresses" not in html

    def test_move_button_emits_marker_server_key_hx_vals(self):
        """The Move button posts the marker's server_key so the move resolves under the right server."""
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("ip-tmpl-winner-key")
        html = self._render(
            migrated={"server_key": "edgelondon", "device_id": winner.pk, "at": "now"},
            movable=[{"id": 9, "address": "10.0.0.2/24", "interface_name": "eth0"}],
            winner=winner,
            has_write=True,
        )
        # Scope the hx-vals assertion to the Move button's own opening tag so another element
        # carrying the key can't mask the button dropping its discriminator.
        from netbox_librenms_plugin.tests._html_helpers import extract_enclosing_tag

        move_button_tag = extract_enclosing_tag(html, "move-to-winner")
        assert 'hx-vals=\'{"server_key": "edgelondon"}\'' in move_button_tag

    def test_move_button_falls_back_to_active_server_key_when_marker_has_no_key(self):
        """With no marker server_key the button falls back to ip_sync.server_key, never an empty key."""
        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("ip-tmpl-winner-nokey")
        html = self._render(
            migrated={"device_id": winner.pk, "at": "now"},  # marker carries NO server_key
            movable=[{"id": 11, "address": "10.0.0.3/24", "interface_name": "eth0"}],
            winner=winner,
            has_write=True,
            server_key="default",
        )
        assert 'hx-vals=\'{"server_key": ""}\'' not in html
        from netbox_librenms_plugin.tests._html_helpers import extract_enclosing_tag

        move_button_tag = extract_enclosing_tag(html, "move-to-winner")
        assert 'hx-vals=\'{"server_key": "default"}\'' in move_button_tag
