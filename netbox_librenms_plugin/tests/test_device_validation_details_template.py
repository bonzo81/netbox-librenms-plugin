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
        import re

        html = self._render()
        assert "Two NetBox devices" in html
        # The badge must carry an explicit text colour with its bg-warning fill; a bare
        # "bg-warning" (the bug) renders grey-on-yellow, unreadable in both themes.
        # Order-agnostic: require badge + bg-warning + text-dark on the *same* class attribute,
        # regardless of the order they appear in (an exact-string check is fragile to reordering).
        assert re.search(
            r'class="(?=[^"]*\bbadge\b)(?=[^"]*\bbg-warning\b)(?=[^"]*\btext-dark\b)[^"]*"',
            html,
        ), "merge badge must pair bg-warning with text-dark on one element"


@pytest.mark.django_db
class TestSerialActionBadges:
    """The serial-match section must render a dedicated badge for each serial_action."""

    def _render(self, serial_action):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device("host-1")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        ctx = {
            "validation": {
                "existing_device": existing,
                "existing_match_type": "serial",
                "serial_action": serial_action,
                "warnings": ["Device 'host-1' already has an OOB controller linked."],
            },
            "libre_device": {
                "device_id": 5,
                "sysName": "host-1",
                "hostname": "host-1",
                "serial": "ABC123",
                "hardware": "iDRAC9",
                "os": "idrac",
                "ip": "10.0.0.1",
                "location": "lab",
                "status": True,
            },
            "server_key": "default",
            "existing_device_model_name": "device",
            "existing_device_url": existing.get_absolute_url(),
            "sync_info": {},
            "existing_id_servers": [],
            "use_sysname": True,
            "strip_domain": False,
        }
        return render_to_string("netbox_librenms_plugin/htmx/device_validation_details.html", ctx, request=request)

    def test_oob_already_linked_shows_dedicated_badge(self):
        html = self._render("oob_already_linked")
        assert "OOB Already Linked" in html
        # It must not fall through to the generic serial-match badge.
        assert "Serial match" not in html

    def test_other_serial_action_still_shows_serial_match_badge(self):
        # A serial_action without its own badge still renders the generic fallback — confirms
        # the new branch didn't displace the default.
        html = self._render("some_other_action")
        assert "Serial match" in html
        assert "OOB Already Linked" not in html


@pytest.mark.django_db
class TestExistingLinkStateText:
    """The 'Exists as …' status line must reflect the existing device's LibreNMS link state for both serial- and hostname-matches: a host link, an OOB-only link, or genuinely unlinked."""

    def _render(self, *, match_type, link):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device("host-link-1")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        ctx = {
            "validation": {
                "existing_device": existing,
                "existing_match_type": match_type,
                "existing_librenms_link": link,
                "serial_action": None,
                "warnings": [],
            },
            "libre_device": {
                "device_id": 5,
                "sysName": "host-link-1",
                "hostname": "host-link-1",
                "serial": "ABC123",
                "hardware": "Model-X",
                "os": "ios",
                "ip": "10.0.0.1",
                "location": "lab",
                "status": True,
            },
            "server_key": "default",
            "existing_device_model_name": "device",
            "existing_device_url": existing.get_absolute_url(),
            "sync_info": {},
            "existing_id_servers": [],
            "use_sysname": True,
            "strip_domain": False,
        }
        return render_to_string("netbox_librenms_plugin/htmx/device_validation_details.html", ctx, request=request)

    def test_serial_match_oob_only_link_is_not_labelled_unlinked(self):
        html = self._render(match_type="serial", link={"host_id": None, "oob_id": 77, "oob_type": "idrac"})
        assert "currently linked to LibreNMS as OOB #77" in html
        assert "not linked to LibreNMS" not in html

    def test_hostname_match_linked_host_is_not_labelled_unlinked(self):
        html = self._render(match_type="hostname", link={"host_id": 42, "oob_id": None})
        assert "currently linked to LibreNMS device #42" in html
        assert "not linked to LibreNMS" not in html

    def test_hostname_match_genuinely_unlinked_still_says_not_linked(self):
        html = self._render(match_type="hostname", link={"host_id": None, "oob_id": None})
        assert "not linked to LibreNMS" in html
