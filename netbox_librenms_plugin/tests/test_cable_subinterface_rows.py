"""Sub-interface neighbour rows on the cable tab.

The primary home for the cable view is test_cable_verify.py. These cases live in their
own file so they do not collide at that shared file's tail when the stack is restacked.

A router advertises LLDP from a physical port and from each sub-unit configured on it, so
one local port can report the same neighbour three times. A cable terminates on the
physical port only, so the sub-unit rows can never be anything but a mismatch.
"""

import pytest


def _link(local_id, local_name, remote_port, remote_port_id, *, remote_device_id=55):
    """One LibreNMS LLDP link row."""
    return {
        "local_port_id": local_id,
        "local_port": local_name,
        "remote_port": remote_port,
        "remote_port_id": remote_port_id,
        "remote_hostname": "prod-lab03a-ra9-8201h",
        "remote_device_id": remote_device_id,
    }


def _collect(links):
    """Run the real row collector."""
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    return BaseCableTableView._collect_cable_links(links, {}, {}, "main")


@pytest.mark.django_db
class TestSubInterfaceNeighbourRows:
    """Only the physical remote port can carry a cable."""

    def test_sub_unit_rows_are_dropped_when_their_physical_port_is_present(self):
        """The reported case: one cable, plus two sub-units that can only ever mismatch."""
        rows = _collect(
            [
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8", 201),
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8.100", 202),
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8.1", 203),
            ]
        )

        assert [r["remote_port"] for r in rows] == ["FourHundredGigE0/0/0/8"]

    def test_a_sub_unit_is_kept_when_its_physical_port_was_not_reported(self):
        """Never drop the only evidence of a neighbour: keep it and let the user judge."""
        rows = _collect([_link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8.100", 202)])

        assert [r["remote_port"] for r in rows] == ["FourHundredGigE0/0/0/8.100"]

    def test_two_real_neighbours_on_one_local_port_both_survive(self):
        """A breakout or a hub can legitimately show two physical neighbours."""
        rows = _collect(
            [
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8", 201),
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/9", 204),
            ]
        )

        assert sorted(r["remote_port"] for r in rows) == [
            "FourHundredGigE0/0/0/8",
            "FourHundredGigE0/0/0/9",
        ]

    def test_a_sub_unit_of_a_different_neighbour_is_not_dropped(self):
        """The physical port must belong to the SAME remote device to mask a sub-unit."""
        rows = _collect(
            [
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8", 201),
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8.100", 202, remote_device_id=77),
            ]
        )

        assert sorted(r["remote_port"] for r in rows) == [
            "FourHundredGigE0/0/0/8",
            "FourHundredGigE0/0/0/8.100",
        ]

    def test_a_sub_unit_on_a_different_local_port_is_not_dropped(self):
        """Masking is per local port: another port's cable says nothing about this one."""
        rows = _collect(
            [
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8", 201),
                _link(101, "1/1/c28/2", "FourHundredGigE0/0/0/8.100", 202),
            ]
        )

        assert len(rows) == 2
