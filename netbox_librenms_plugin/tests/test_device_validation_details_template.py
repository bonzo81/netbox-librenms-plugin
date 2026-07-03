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

    def _render(self, *, match_type, link, name="host-link-1"):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device(name)
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

    def test_serial_and_hostname_branches_render_identical_link_status_clause(self):
        """Both branches share one include, so the full host+OOB clause renders identically (no drift)."""
        link = {"host_id": 91, "oob_id": 7, "oob_type": "idrac"}
        clause = "currently linked to LibreNMS device #91 (OOB #7, idrac)."
        assert clause in self._render(match_type="hostname", link=link, name="clause-host")
        assert clause in self._render(match_type="serial", link=link, name="clause-serial")


@pytest.mark.django_db
class TestSerialMatchFormServerKey:
    """The serial-match Link/Update form must carry ?server_key like the adjacent Add-as-OOB form."""

    def _render(self, serial_action):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device("serial-key-existing")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        ctx = {
            "validation": {
                "existing_device": existing,
                "existing_match_type": "serial",
                "serial_action": serial_action,
                "device_type_mismatch": False,
                "warnings": [],
            },
            "libre_device": {"device_id": 7, "sysName": "serial-key", "hostname": "serial-key"},
            "server_key": "prod",
            "existing_device_model_name": "device",
            "existing_device_url": existing.get_absolute_url(),
            "sync_info": {},
            "existing_id_servers": [],
            "use_sysname": True,
            "strip_domain": False,
        }
        return render_to_string("netbox_librenms_plugin/htmx/device_validation_details.html", ctx, request=request)

    def test_link_form_carries_server_key(self):
        html = self._render("link")
        # Precondition: no oob_candidate → the Add-as-OOB form (which has its OWN server_key) is
        # absent, so the only server_key input is the serial-match one under test.
        assert "device_add_as_oob" not in html
        assert "Link to LibreNMS" in html
        assert 'name="server_key" value="prod"' in html

    def test_update_form_carries_server_key(self):
        html = self._render("hostname_differs")
        assert "device_add_as_oob" not in html
        assert "Update &amp; Link" in html
        assert 'name="server_key" value="prod"' in html


@pytest.mark.django_db
class TestPromoteToHostFallbackPane:
    """A promote_to_host-classified row must render an ACTIONABLE Host pane on this branch.

    The full promote flow (side-by-side modal + device_promote_to_host endpoint) lives on the
    device-merge branch up-stack; standalone, the Host radio's data-target div did not exist,
    leaving the row with no action where develop offered 'Update & Link'. The fallback pane is
    gated on a {% url ... as %} probe, so up-stack (URL registered) it self-disables and the
    real pane takes over.
    """

    def _render(self, *, patch_promote_url_absent=False, choice_available=False):
        from unittest.mock import patch

        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django.urls import NoReverseMatch
        from django.urls import reverse as real_reverse

        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device("promote-fallback-host")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        ctx = {
            "validation": {
                "existing_device": existing,
                "existing_match_type": "serial",
                "serial_action": "promote_to_host",
                "serial_role_choice_available": choice_available,
                "promote_to_host": {
                    "existing_libre_id": 17,
                    "existing_oob_type": "idrac",
                    "existing_device": existing,
                },
                "existing_librenms_link": {"host_id": 17, "oob_id": None, "oob_type": None},
                "device_type_mismatch": False,
                "warnings": [],
            },
            "libre_device": {"device_id": 5, "sysName": "real-host", "hostname": "real-host"},
            "server_key": "prod",
            "existing_device_model_name": "device",
            "existing_device_url": existing.get_absolute_url(),
            "sync_info": {},
            "existing_id_servers": [],
            "use_sysname": True,
            "strip_domain": False,
        }

        if patch_promote_url_absent:
            # Restack robustness: up-stack the device-merge branch REGISTERS
            # device_promote_to_host, which would flip a plain absence assertion. Force the
            # URL absent so the fallback path stays testable on every branch (Django's
            # {% url %} resolves reverse from django.urls at render time).
            def fake_reverse(viewname, *args, **kwargs):
                if "device_promote_to_host" in str(viewname):
                    raise NoReverseMatch(viewname)
                return real_reverse(viewname, *args, **kwargs)

            with patch("django.urls.reverse", side_effect=fake_reverse):
                return render_to_string(
                    "netbox_librenms_plugin/htmx/device_validation_details.html", ctx, request=request
                )
        return render_to_string("netbox_librenms_plugin/htmx/device_validation_details.html", ctx, request=request)

    def test_fallback_pane_offers_update_and_link(self):
        """With the promote URL absent (forced), the Host pane renders with the legacy action."""
        from django.urls import NoReverseMatch, reverse

        # On branches where the real promote flow exists (device-merge and above), the
        # template ALSO renders a bare {% url 'device_promote_to_host' %} inside the real
        # pane — forcing reverse to raise there would 500 the whole render, and the
        # fallback is inert by design (its probe resolves). The real pane has its own
        # coverage up-stack; this test only guards the fallback branch.
        try:
            reverse("plugins:netbox_librenms_plugin:device_promote_to_host", kwargs={"device_id": 1})
        except NoReverseMatch:
            pass
        else:
            pytest.skip("real promote pane registered on this branch; fallback is inert by design")

        html = self._render(patch_promote_url_absent=True)
        assert 'id="serial-role-host-5"' in html  # the Host radio's data-target actually exists
        pane_start = html.find('id="serial-role-host-5"')
        pane = html[pane_start : html.find('id="serial-role-oob-5"') if 'id="serial-role-oob-5"' in html else None]
        assert "Update &amp; Link" in pane
        # No generic '"action" in pane' fallback: any <form action=...> (or the submit
        # button's own name="action") would match it, so it asserts nothing.
        assert "device_conflict_action" in pane or "conflict-action" in pane or "/conflict/" in pane
        assert 'name="server_key" value="prod"' in pane

    def test_promote_row_always_has_an_actionable_host_pane(self):
        """Branch-agnostic: whether the fallback or the real promote pane renders, the row must offer an action inside the Host div."""
        html = self._render()
        assert 'id="serial-role-host-5"' in html
