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


@pytest.mark.django_db
@pytest.mark.parametrize("remote_port", [True, 123, ["eth0"], {"name": "eth0"}])
def test_malformed_remote_port_survives_collection_and_enrichment(remote_port):
    """A malformed remote name must not crash or identify an interface."""
    from netbox_librenms_plugin.tests.conftest import make_device, make_interface
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    device = make_device("malformed-remote-port")
    make_interface(device, "eth0")
    rows = _collect([_link(100, "eth0", remote_port, 201)])
    assert len(rows) == 1
    result = BaseCableTableView().enrich_remote_port(rows[0], device, server_key="default")
    assert "netbox_remote_interface_id" not in result


@pytest.mark.django_db
@pytest.mark.parametrize("same_hostname", [False, True])
def test_missing_remote_device_id_groups_by_hostname(same_hostname):
    """Only the same remote hostname can mask a sub-unit without a device ID."""
    physical = _link(100, "eth0", "eth1", 201, remote_device_id=None)
    physical["remote_hostname"] = "peer-a.example"
    sub_unit = _link(100, "eth0", "eth1.100", 202, remote_device_id=None)
    sub_unit["remote_hostname"] = "peer-a.example" if same_hostname else "peer-b.example"
    rows = _collect([physical, sub_unit])
    assert len(rows) == (1 if same_hostname else 2)


@pytest.mark.django_db
def test_hostname_grouping_folds_case_like_the_device_lookup():
    """get_device_by_id_or_name resolves a hostname with name__iexact, so two spellings of one
    neighbour must land in the same group and the physical row must still mask the sub-unit."""
    physical = _link(100, "eth0", "eth1", 201, remote_device_id=None)
    physical["remote_hostname"] = "PEER-A.example"
    sub_unit = _link(100, "eth0", "eth1.100", 202, remote_device_id=None)
    sub_unit["remote_hostname"] = "  peer-a.example  "

    rows = _collect([physical, sub_unit])

    assert [row["remote_port"] for row in rows] == ["eth1"]


@pytest.mark.django_db
@pytest.mark.parametrize("hostname", [None, "", [], {}, 123, True])
def test_unknown_remote_hostname_preserves_rows_and_skips_name_lookup(hostname):
    """Unknown neighbor identities cannot mask rows or identify a device by name."""
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    links = [
        _link(100, "eth0", "eth1", 201, remote_device_id=None),
        _link(100, "eth0", "eth1.100", 202, remote_device_id=None),
    ]
    for link in links:
        link["remote_hostname"] = hostname
    rows = _collect(links)
    assert [row["remote_port"] for row in rows] == ["eth1", "eth1.100"]
    device, matched, _error = BaseCableTableView().get_device_by_id_or_name(None, hostname, "default")
    assert device is None
    assert not matched


@pytest.mark.django_db
def test_remote_device_id_resolves_with_malformed_hostname():
    """A valid remote ID remains usable when the advertised hostname is malformed."""
    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    device = make_device("remote-id-without-name")
    # The canonical writer, so the test pins ID resolution rather than a hand-built field shape.
    set_librenms_device_id(device, 42)
    device.save()
    found, matched, error = BaseCableTableView().get_device_by_id_or_name(42, [], "default")
    assert found == device
    assert matched
    assert error is None


@pytest.mark.django_db
@pytest.mark.parametrize("id_field", ["local_port_id", "remote_device_id"])
@pytest.mark.parametrize("malformed_id", [[], {}, True, 1.5, None, "", 0])
def test_invalid_cable_identifiers_cannot_mask_unrelated_neighbors(id_field, malformed_id):
    """Malformed identifiers must not crash collection or merge unrelated neighbors."""
    links = [
        _link(100, "eth0", "eth1", 201),
        _link(100, "eth0", "eth1.100", 202),
    ]
    for index, link in enumerate(links):
        link[id_field] = malformed_id
        link["remote_hostname"] = f"peer-{index}.example"
    rows = _collect(links)
    assert [row["remote_port"] for row in rows] == ["eth1", "eth1.100"]


@pytest.mark.django_db
@pytest.mark.parametrize("remote_device_id", [[], {}, True, 1.5, None, "", 0])
@pytest.mark.parametrize("hostname", [None, "peer.example"])
def test_invalid_remote_identifier_can_use_a_matching_hostname(remote_device_id, hostname):
    """A valid shared hostname still identifies the physical neighbor when its ID is invalid."""
    links = [
        _link(100, "eth0", "eth1", 201, remote_device_id=remote_device_id),
        _link(100, "eth0", "eth1.100", 202, remote_device_id=remote_device_id),
    ]
    for link in links:
        link["remote_hostname"] = hostname
    expected = ["eth1"] if hostname else ["eth1", "eth1.100"]
    assert [row["remote_port"] for row in _collect(links)] == expected
