"""A one-sided cable row must still report the cable NetBox already has.

``check_cable_status`` only inspected cables inside ``if local_interface_id and
remote_interface_id``. When only one end resolved it fell to the bare "… Not Found in Netbox"
status with no ``cable_url``, so a real NetBox cable on a port whose LibreNMS neighbour is not
modelled (a management switch, the fxp0 case) read as "nothing is connected".

The report is scoped: it names the peer device only when the request may view it, and it uses
a generic unavailable status for a cable the request may not view.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import cable_together, make_device, make_interface
from netbox_librenms_plugin.tests.test_serial_cables_view import _make_view


def _one_sided_link(local=None, remote=None):
    """Build a row where at most one end resolved to a NetBox interface."""
    return {
        "netbox_local_interface_id": local.pk if local else None,
        "netbox_remote_interface_id": remote.pk if remote else None,
    }


@pytest.mark.django_db
class TestOneSidedRowReportsAnExistingCable:
    """The end that did resolve is still worth reading a cable off."""

    def test_an_uncabled_local_port_still_reports_not_found(self):
        """Positive control: with no cable there is nothing to add to the old status."""
        local = make_interface(make_device("one-sided-bare"), "fxp0")

        link = _make_view().check_cable_status(_one_sided_link(local=local))

        assert link["cable_status"] == "Remote Interface Not Found in Netbox"
        assert not link.get("cable_url")
        assert link["can_create_cable"] is False

    def test_a_cabled_local_port_reports_the_cable_and_its_peer(self):
        """The defect: a real cable on fxp0 was invisible when the neighbour is not modelled."""
        local = make_interface(make_device("one-sided-cabled"), "fxp0")
        peer_device = make_device("one-sided-mgmt-sw")
        peer = make_interface(peer_device, "ge-0/0/5")
        cable = cable_together(local, peer)
        local.refresh_from_db()

        link = _make_view().check_cable_status(_one_sided_link(local=local))

        assert peer_device.name in link["cable_status"]
        assert link["cable_url"] == f"/dcim/cables/{cable.pk}/"

    def test_the_row_still_offers_no_sync_action(self):
        """There is no NetBox remote to cable to, so the affordance must stay off."""
        local = make_interface(make_device("one-sided-noaction"), "fxp0")
        peer = make_interface(make_device("one-sided-noaction-peer"), "ge-0/0/5")
        cable_together(local, peer)
        local.refresh_from_db()

        link = _make_view().check_cable_status(_one_sided_link(local=local))

        assert link["can_create_cable"] is False

    def test_a_cabled_remote_port_is_reported_when_the_local_end_is_missing(self):
        """The mirror case: only the remote resolved, and it carries a cable."""
        remote = make_interface(make_device("one-sided-remote"), "ge-0/0/1")
        peer_device = make_device("one-sided-remote-peer")
        cable_together(remote, make_interface(peer_device, "ge-0/0/2"))
        remote.refresh_from_db()

        link = _make_view().check_cable_status(_one_sided_link(remote=remote))

        assert peer_device.name in link["cable_status"]
        assert link["cable_url"]

    def test_neither_end_resolved_reports_nothing_extra(self):
        """With no NetBox interface at all there is no cable to read."""
        link = _make_view().check_cable_status(_one_sided_link())

        assert link["cable_status"] == "Both Interfaces Not Found in Netbox"
        assert not link.get("cable_url")


@pytest.mark.django_db
class TestOneSidedCableReportRespectsViewScope:
    """The report must never disclose an object the request cannot view."""

    @staticmethod
    def _view_for(user):
        from netbox_librenms_plugin.tests.view_test_helpers import make_request

        view = _make_view()
        view.request = make_request("get", user=user)
        return view

    @staticmethod
    def _user(username):
        from django.contrib.auth import get_user_model

        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.tests.view_test_helpers import grant

        user = get_user_model().objects.create_user(username=username, password="x")
        return grant(user, "view", LibreNMSSettings, name=f"{username}-plugin-view")

    def test_a_hidden_peer_is_not_named(self):
        """The cable is visible but its far end is not, so the status must stay generic."""
        from dcim.models import Cable, Device, Interface

        from netbox_librenms_plugin.tests.view_test_helpers import grant

        local = make_interface(make_device("scope-visible"), "fxp0")
        hidden_device = make_device("scope-hidden-peer")
        cable_together(local, make_interface(hidden_device, "ge-0/0/5"))
        local.refresh_from_db()

        user = self._user("cable-scope-hidden-peer")
        # Only the local device and its interfaces are in scope; the peer device is not.
        user = grant(user, "view", Device, constraints={"name": "scope-visible"})
        user = grant(user, "view", Interface, constraints={"device__name": "scope-visible"})
        user = grant(user, "view", Cable)

        link = self._view_for(user).check_cable_status(_one_sided_link(local=local))

        assert link["cable_status"] == "Cabled in Netbox"

    def test_a_hidden_cable_is_not_linked(self):
        """A cable outside the request's scope must not leak its pk through cable_url."""
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.tests.view_test_helpers import grant

        local = make_interface(make_device("scope-hidden-cable"), "fxp0")
        cable_together(local, make_interface(make_device("scope-hidden-cable-peer"), "ge-0/0/5"))
        local.refresh_from_db()

        # Devices and interfaces are viewable; Cable is not granted at all.
        user = self._user("cable-scope-no-cable")
        user = grant(user, "view", Device)
        user = grant(user, "view", Interface)

        link = self._view_for(user).check_cable_status(_one_sided_link(local=local))

        assert not link.get("cable_url")
        assert link["cable_status"] == "Cable State Not Available"
