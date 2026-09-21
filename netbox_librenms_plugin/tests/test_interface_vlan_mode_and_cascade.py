"""802.1Q mode follows the reported ifTrunk, and LAG VLAN data fills holes in both directions.

The mode was inferred from the VLAN lists alone, so a trunk carrying one untagged VLAN and no
tagged VLANs was written as ``access`` even though LibreNMS reported ``ifTrunk = dot1Q``. The
cascade is fill-only: a row that carries its own VLAN data is never rewritten, which keeps the
rule vendor-neutral (Juniper reports VLANs on the aggregate, other platforms on the members).
"""

import pytest


def _fixture(tag, vlans=()):
    """Return a mixin, a real interface and the lookup maps the production indexer builds."""
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.conftest import make_device, make_interface
    from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

    interface = make_interface(make_device(f"vlan-mode-{tag}"), "eth0")
    created = [VLAN.objects.create(vid=vid, name=f"VLAN-MODE-{tag}-{vid}") for vid in vlans]
    return VlanAssignmentMixin(), interface, VlanAssignmentMixin._index_vlans(created), created


@pytest.mark.django_db
class TestReportedModeIsAuthoritative:
    """LibreNMS states the mode in ifTrunk; the VLAN lists only refine it."""

    def test_a_trunk_carrying_one_untagged_vlan_is_written_as_tagged(self):
        """The defect: ifTrunk said dot1Q, but the list-derived rule wrote ``access``."""
        mixin, interface, maps, _ = _fixture("trunk-one-untagged", [100])

        mixin._update_interface_vlan_assignment(
            interface,
            {"mode": "tagged", "untagged_vlan": 100, "tagged_vlans": []},
            None,
            maps,
        )

        interface.refresh_from_db()
        assert interface.mode == "tagged"
        assert interface.untagged_vlan.vid == 100

    def test_a_reported_access_port_stays_access(self):
        """Positive control: a real access port must not become a trunk."""
        mixin, interface, maps, _ = _fixture("access", [100])

        mixin._update_interface_vlan_assignment(
            interface,
            {"mode": "access", "untagged_vlan": 100, "tagged_vlans": []},
            None,
            maps,
        )

        interface.refresh_from_db()
        assert interface.mode == "access"

    def test_tagged_vlans_still_force_tagged_when_the_report_says_access(self):
        """The lists refine the report: tagged VLANs cannot belong to an access port."""
        mixin, interface, maps, _ = _fixture("refine", [100, 200])

        mixin._update_interface_vlan_assignment(
            interface,
            {"mode": "access", "untagged_vlan": 100, "tagged_vlans": [200]},
            None,
            maps,
        )

        interface.refresh_from_db()
        assert interface.mode == "tagged"

    def test_an_unreported_mode_falls_back_to_the_vlan_lists(self):
        """Older LibreNMS versions report no ifVlan, so the lists remain the only signal."""
        mixin, interface, maps, _ = _fixture("unreported", [100])

        mixin._update_interface_vlan_assignment(
            interface,
            {"mode": None, "untagged_vlan": 100, "tagged_vlans": []},
            None,
            maps,
        )

        interface.refresh_from_db()
        assert interface.mode == "access"

    def test_a_row_with_no_vlans_at_all_clears_the_mode(self):
        """NetBox stores "no mode" as NULL; writing "" would report a change every sync."""
        mixin, interface, maps, (vlan,) = _fixture("none", [100])
        interface.mode = "access"
        interface.untagged_vlan = vlan
        interface.save()

        mixin._update_interface_vlan_assignment(
            interface,
            {"mode": None, "untagged_vlan": None, "tagged_vlans": []},
            None,
            maps,
        )

        interface.refresh_from_db()
        assert interface.mode is None
        assert interface.untagged_vlan is None

    def test_a_reported_trunk_with_no_vlans_at_all_is_not_given_a_mode(self):
        """The report is only authoritative about a port it also gives VLAN data for."""
        mixin, interface, maps, _ = _fixture("trunk-empty")

        mixin._update_interface_vlan_assignment(
            interface,
            {"mode": "access", "untagged_vlan": None, "tagged_vlans": []},
            None,
            maps,
        )

        interface.refresh_from_db()
        assert interface.mode is None


@pytest.mark.django_db
class TestTheViewPassesTheReportedMode:
    """The mode is on every enriched row; the sync view has to hand it to the writer."""

    def test_the_row_mode_reaches_netbox(self):
        """A writer-only fix would pass while the view still dropped the reported mode."""
        from ipam.models import VLAN

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("vlan-mode-view")
        interface = make_interface(device, "Ethernet1")
        vlan = VLAN.objects.create(vid=100, name="VLAN-MODE-VIEW-100", status="active")
        view = object.__new__(SyncInterfacesView)
        view.request = make_request("post", {})
        view._lookup_maps = view._index_vlans([vlan])
        view._lookup_maps_by_owner = None
        view._vlan_owners_by_id = {}

        view._sync_interface_vlans(
            interface,
            {"port_id": 10, "mode": "tagged", "untagged_vlan": 100, "tagged_vlans": []},
        )

        interface.refresh_from_db()
        assert interface.mode == "tagged"
        assert interface.untagged_vlan == vlan


def _port(port_id, name, **overrides):
    port = {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "mode": None,
        "untagged_vlan": None,
        "tagged_vlans": [],
    }
    port.update(overrides)
    return port


class TestFillOnlyLagVlanCascade:
    """Both directions, hole-filling only, so the rule cannot overwrite correct data."""

    @staticmethod
    def _apply(ports, lag_members):
        from netbox_librenms_plugin.utils import apply_lag_vlan_fill

        apply_lag_vlan_fill(ports, lag_members, interface_name_field="ifName")
        return {port["port_id"]: port for port in ports}

    def test_a_member_with_no_vlans_inherits_from_its_aggregate(self):
        """The Juniper shape: LibreNMS reports the VLANs on ae0 only."""
        ports = [
            _port(1, "ae0", mode="tagged", untagged_vlan=10, tagged_vlans=[20, 30]),
            _port(2, "ge-0/0/0"),
        ]

        rows = self._apply(ports, {2: 1})

        assert rows[2]["untagged_vlan"] == 10
        assert rows[2]["tagged_vlans"] == [20, 30]
        assert rows[2]["mode"] == "tagged"
        assert rows[2]["vlan_inherited_from"] == "ae0"

    def test_a_member_with_its_own_vlans_is_left_alone(self):
        """Fill-only: the cascade must never overwrite what LibreNMS reported for the row."""
        ports = [
            _port(1, "ae0", mode="tagged", untagged_vlan=10, tagged_vlans=[20]),
            _port(2, "ge-0/0/0", mode="access", untagged_vlan=99, tagged_vlans=[]),
        ]

        rows = self._apply(ports, {2: 1})

        assert rows[2]["untagged_vlan"] == 99
        assert rows[2]["tagged_vlans"] == []
        assert rows[2]["mode"] == "access"
        assert "vlan_inherited_from" not in rows[2]

    def test_an_aggregate_with_no_vlans_takes_the_set_its_members_agree_on(self):
        """The switch shape: LibreNMS reports the VLANs on the member ports."""
        ports = [
            _port(1, "Port-channel1"),
            _port(2, "Gi1/0/1", mode="tagged", untagged_vlan=10, tagged_vlans=[20, 30]),
            _port(3, "Gi1/0/2", mode="tagged", untagged_vlan=10, tagged_vlans=[30, 20]),
        ]

        rows = self._apply(ports, {2: 1, 3: 1})

        assert rows[1]["untagged_vlan"] == 10
        assert rows[1]["tagged_vlans"] == [20, 30]
        assert rows[1]["mode"] == "tagged"
        assert rows[1]["vlan_inherited_from"] == "Gi1/0/1"

    def test_members_that_disagree_leave_the_aggregate_empty(self):
        """Rolling up a set the members do not share would invent data."""
        ports = [
            _port(1, "Port-channel1"),
            _port(2, "Gi1/0/1", mode="access", untagged_vlan=10, tagged_vlans=[]),
            _port(3, "Gi1/0/2", mode="access", untagged_vlan=20, tagged_vlans=[]),
        ]

        rows = self._apply(ports, {2: 1, 3: 1})

        assert rows[1]["untagged_vlan"] is None
        assert rows[1]["tagged_vlans"] == []
        assert "vlan_inherited_from" not in rows[1]

    def test_a_member_without_vlans_blocks_the_roll_up(self):
        """ "All members agree" cannot be satisfied by the subset that happens to have data."""
        ports = [
            _port(1, "Port-channel1"),
            _port(2, "Gi1/0/1", mode="access", untagged_vlan=10, tagged_vlans=[]),
            _port(3, "Gi1/0/2"),
        ]

        rows = self._apply(ports, {2: 1, 3: 1})

        assert rows[1]["untagged_vlan"] is None
        assert "vlan_inherited_from" not in rows[1]

    def test_a_filled_member_does_not_then_feed_a_roll_up(self):
        """Both directions read the original state, so a fill can never cascade onwards."""
        ports = [
            _port(1, "ae0", mode="tagged", untagged_vlan=10, tagged_vlans=[20]),
            _port(2, "ge-0/0/0"),
            _port(3, "ae1"),
        ]

        rows = self._apply(ports, {2: 1, 3: 2})

        assert rows[2]["vlan_inherited_from"] == "ae0"
        assert rows[3]["untagged_vlan"] is None
        assert "vlan_inherited_from" not in rows[3]

    def test_an_aggregate_outside_the_port_list_is_ignored(self):
        """A port_stack edge can name a port the snapshot does not contain."""
        ports = [_port(2, "ge-0/0/0")]

        rows = self._apply(ports, {2: 999})

        assert rows[2]["untagged_vlan"] is None
        assert "vlan_inherited_from" not in rows[2]

    def test_no_relationships_changes_nothing(self):
        """The common case must not pay for the cascade."""
        ports = [_port(1, "eth0", mode="access", untagged_vlan=10)]

        rows = self._apply(ports, {})

        assert rows[1]["untagged_vlan"] == 10
        assert "vlan_inherited_from" not in rows[1]


@pytest.mark.django_db
class TestInheritedVlansAreMarkedInTheTable:
    """An inherited value must never read as something LibreNMS reported for the row."""

    @staticmethod
    def _table():
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        return LibreNMSInterfaceTable(data=[], device=None, interface_name_field="ifName", server_key="default")

    def test_an_inherited_row_names_its_donor(self):
        rendered = str(
            self._table().render_vlans(
                None,
                {
                    "port_id": 2,
                    "ifName": "ge-0/0/0",
                    "untagged_vlan": 10,
                    "tagged_vlans": [20],
                    "vlan_inherited_from": "ae0",
                    "sync_target_resolvable": False,
                },
            )
        )

        assert "Inherited from ae0" in rendered

    def test_a_reported_row_carries_no_badge(self):
        rendered = str(
            self._table().render_vlans(
                None,
                {
                    "port_id": 2,
                    "ifName": "ge-0/0/0",
                    "untagged_vlan": 10,
                    "tagged_vlans": [20],
                    "sync_target_resolvable": False,
                },
            )
        )

        assert "Inherited from" not in rendered
