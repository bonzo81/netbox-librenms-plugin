"""Render the real device_validation_details.html badge for the Stage-2 merge case.

The "Two NetBox devices" badge must pair its colour fill with a text colour: a bare
``bg-warning`` leaves muted/inherited text, which is unreadable in NetBox's light AND dark
themes (measured ~1.2–2.3:1). ``bg-warning text-dark`` clears WCAG AA in both. Rendering the
real template guards against a regression to the bare class.
"""

import pytest


@pytest.mark.django_db
class TestDeviceValidationDetailsMergeBadge:
    def _render(self):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("merge-badge-winner")
        donor = make_device("merge-badge-donor")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()  # NetBox context processors read request.user
        ctx = {
            "validation": {
                "existing_device": winner,
                "serial_action": "merge_netbox_devices",
                "merge_candidates": {
                    "host_named": {"pk": winner.pk, "name": winner.name},
                    "oob_named": {"pk": donor.pk, "name": donor.name},
                },
            },
            "libre_device": {
                "device_id": 5,
                "sysName": "merge-badge-winner",
                "hostname": "merge-badge-winner",
                "serial": "ABC123",
                "hardware": "Model-X",
                "os": "ios",
                "ip": "10.0.0.1",
                "location": "lab",
                "status": True,
            },
            "server_key": "default",
            "existing_device_model_name": "device",
            "existing_device_url": winner.get_absolute_url(),
            "sync_info": {},
            "existing_id_servers": [],
            "use_sysname": True,
            "strip_domain": False,
        }
        return render_to_string("netbox_librenms_plugin/htmx/device_validation_details.html", ctx, request=request)

    def test_merge_badge_pairs_background_with_text_colour(self):
        html = self._render()
        assert "Two NetBox devices" in html
        # The badge must carry an explicit text colour with its bg-warning fill; a bare
        # "bg-warning" (the bug) renders grey-on-yellow, unreadable in both themes.
        assert "badge bg-warning text-dark" in html
