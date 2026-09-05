"""Remote-end device resolution for cable rows.

The primary home for the cable view is test_cable_remote_picker.py. These cases live in
their own file so they do not collide at that shared file's tail when the stack is
restacked.

LibreNMS reports neighbour hostnames as the device advertises them, which is often all
lower case, while NetBox stores whatever the operator typed. Matching those exactly meant
a remote end only ever resolved through librenms_id.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device


def _view():
    """The real cable view, with no server key resolved yet."""
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    view = BaseCableTableView()
    view._active_server_key = "default"
    return view


@pytest.mark.django_db
class TestRemoteDeviceNameMatching:
    """A neighbour hostname must resolve regardless of the case LibreNMS reports."""

    def test_a_lower_case_hostname_matches_an_upper_case_netbox_name(self):
        """LibreNMS advertises lower case; NetBox holds the operator's capitalisation."""
        device = make_device("PROD-LAB03-CRPD-SW01")

        found, ok, error = _view().get_device_by_id_or_name(None, "prod-lab03-crpd-sw01", server_key="default")

        assert error is None
        assert ok is True
        assert found == device

    def test_a_lower_case_fqdn_matches_after_the_domain_is_dropped(self):
        """The domain-stripping fallback must be case insensitive too."""
        device = make_device("PROD-LAB03-CRPD-SW02")

        found, ok, error = _view().get_device_by_id_or_name(
            None, "prod-lab03-crpd-sw02.example.net", server_key="default"
        )

        assert error is None
        assert ok is True
        assert found == device

    def test_an_exact_name_still_matches(self):
        """The repair must not disturb the case that already worked."""
        device = make_device("exact-case-switch")

        found, ok, error = _view().get_device_by_id_or_name(None, "exact-case-switch", server_key="default")

        assert error is None
        assert found == device

    def test_two_names_differing_only_by_case_are_reported_as_ambiguous(self):
        """Case-insensitive matching must fail closed rather than pick one arbitrarily.

        NetBox enforces unique (lower(name), site) per site, so the only way to hold both
        spellings is to put them in different sites.
        """
        from dcim.models import Device, Site

        first = make_device("Ambiguous-Case-SW")
        other_site = Site.objects.create(name="cable-match-site-b", slug="cable-match-site-b")
        Device.objects.create(
            name="ambiguous-case-sw",
            device_type=first.device_type,
            role=first.role,
            site=other_site,
        )

        found, ok, error = _view().get_device_by_id_or_name(None, "AMBIGUOUS-CASE-SW", server_key="default")

        assert found is None
        assert ok is False
        assert "Multiple devices" in error

    def test_an_unknown_hostname_still_resolves_to_nothing(self):
        """A genuine miss must stay a miss, not become a loose match."""
        make_device("some-other-device")

        found, ok, error = _view().get_device_by_id_or_name(None, "not-a-device-here", server_key="default")

        assert found is None
        assert ok is False
        assert error is None
