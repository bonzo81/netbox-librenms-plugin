"""Render the real _ipaddress_sync_content.html template in normal vs migrated mode.

In migrated mode the POST form is removed (a migrated donor must not submit an IP sync), so the
sync-only 'Set Primary IP' switch must not render as an active control with nowhere to submit.
"""

import pytest


@pytest.mark.django_db
class TestIpAddressSyncContentTemplateMigratedMode:
    def _render(self, *, migrated):
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
                "server_key": "default",
                "set_primary_ip": False,
                "cache_expiry": None,
            },
            "migrated_to_marker": migrated,
            "has_write_permission": False,
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
