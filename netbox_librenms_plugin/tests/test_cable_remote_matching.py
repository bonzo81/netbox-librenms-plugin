"""The far end of a cable row: matching it, and creating it when NetBox has no port for it.

Two defects, both "the row said not found, or said it twice, because the matching is wrong":

* The local end resolves against the displayed port name *and* its ifName/ifDescr counterpart
  (issue #88), while the remote end only ever had the neighbour-advertised string. A remote
  interface that exists in NetBox under the other LibreNMS name field therefore read as
  "Remote Interface Not Found in Netbox".
* LibreNMS returns one row per discovery protocol, so a neighbour seen over both CDP and LLDP
  renders twice and offers two Sync Cable buttons for one physical link.

Then the action that follows from the first: when the neighbour IS modelled and its port is not,
the row can never be synced, so it offers to create the far end and the cable together.
"""

import json
import time

import pytest

from netbox_librenms_plugin.tests.conftest import (
    configured_server_key,
    make_device,
    make_interface,
    map_device_to_librenms,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.test_serial_cables_view import _make_view


def _ports_payload(*ports):
    """A LibreNMS get_ports body."""
    return {"status": "ok", "ports": list(ports)}


def _requested_paths(server):
    """Every path the loopback LibreNMS was asked for, in order."""
    return [request["path"] for request in server.requests]


def _port(port_id, if_name, if_descr):
    """One LibreNMS port record."""
    return {"port_id": port_id, "ifName": if_name, "ifDescr": if_descr}


def _row(**overrides):
    """One collected cable row, as _collect_cable_links builds it."""
    row = {
        "local_port": "eth0",
        "local_port_id": 100,
        "local_port_alt": None,
        "link_id": 1,
        "protocol": "lldp",
        "remote_port": "Gi0/1",
        "remote_device": "peer-switch",
        "remote_port_id": 500,
        "remote_device_id": 9,
        "_source": "main",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# The remote port's other LibreNMS names
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRemotePortAliases:
    """A remote port answers to both ifName and ifDescr; the row must carry both."""

    def test_remote_device_name_and_port_alias_resolve_in_one_row(self):
        """A name-only neighbour still resolves when its port uses an alternate name."""
        server_key = configured_server_key()
        remote_device = make_device("alias-remote-device")
        interface = make_interface(remote_device, "GigabitEthernet0/1")
        link = _row(
            remote_device=" ALIAS-REMOTE-DEVICE.example.test ",
            remote_device_id=None,
            remote_port_aliases=["GigabitEthernet0/1"],
        )
        view = _make_view()

        by_name, by_id, visible_ids = view._load_remote_device_catalog([link], server_key)
        matched = view._resolve_remote_devices([link], by_name, by_id, visible_ids)[id(link)]
        view.enrich_remote_port(link, matched, server_key=server_key)

        assert matched == remote_device
        assert link["netbox_remote_interface_id"] == interface.pk

    def test_the_alternate_name_resolves_the_remote_interface(self):
        """The defect: NetBox holds the ifDescr name, LLDP advertised the ifName one."""
        server_key = configured_server_key()
        remote_device = make_device("alias-remote-a")
        interface = make_interface(remote_device, "GigabitEthernet0/1")

        link = _row(remote_port="Gi0/1", remote_port_aliases=["GigabitEthernet0/1"])
        _make_view().enrich_remote_port(link, remote_device, server_key=server_key)

        assert link["netbox_remote_interface_id"] == interface.pk

    def test_the_advertised_name_still_wins_when_it_matches(self):
        """Positive control: the aliases only widen the fallback, they never replace it."""
        server_key = configured_server_key()
        remote_device = make_device("alias-remote-b")
        interface = make_interface(remote_device, "Gi0/1")

        link = _row(remote_port="Gi0/1", remote_port_aliases=["GigabitEthernet0/1"])
        _make_view().enrich_remote_port(link, remote_device, server_key=server_key)

        assert link["netbox_remote_interface_id"] == interface.pk

    def test_an_unresolved_remote_port_still_names_itself(self):
        """The Remote Port column reads remote_port_name, so an unresolved row rendered blank."""
        server_key = configured_server_key()
        remote_device = make_device("alias-remote-unnamed")

        link = _row(remote_port="Gi0/1")
        _make_view().enrich_remote_port(link, remote_device, server_key=server_key)

        assert link["remote_port_name"] == "Gi0/1"

    def test_an_ambiguous_pair_of_names_resolves_nothing(self):
        """Both names exist as separate interfaces: refuse rather than pick one."""
        server_key = configured_server_key()
        remote_device = make_device("alias-remote-c")
        make_interface(remote_device, "Gi0/1")
        make_interface(remote_device, "GigabitEthernet0/1")

        link = _row(remote_port="Gi0/1", remote_port_aliases=["GigabitEthernet0/1"])
        _make_view().enrich_remote_port(link, remote_device, server_key=server_key)

        assert "netbox_remote_interface_id" not in link


@pytest.mark.django_db
class TestRemotePortAliasesAreFetched:
    """get_links_data reads the neighbour's ports once and carries their names onto the rows."""

    def _device_on_server(self, name, librenms_id, librenms_server, settings):
        """A NetBox device mapped to a loopback LibreNMS."""
        from netbox_librenms_plugin.tests.conftest import bind_librenms_server

        server_key = configured_server_key()
        bind_librenms_server(settings, librenms_server, server_key=server_key)
        device = make_device(name)
        map_device_to_librenms(device, librenms_id, server_key=server_key)
        return device, server_key

    def test_the_other_name_field_lands_on_the_row(self, librenms_server, settings):
        """One /devices/<remote>/ports read turns 'Gi0/1' into a pair of candidate names."""
        device, server_key = self._device_on_server("alias-fetch-a", 11, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/11/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": 1,
                        "protocol": "lldp",
                        "local_port_id": 100,
                        "remote_port": "Gi0/1",
                        "remote_hostname": "peer-switch",
                        "remote_port_id": 500,
                        "remote_device_id": 9,
                    }
                ],
            },
        )
        librenms_server.register("/api/v0/devices/11/ports", _ports_payload(_port(100, "eth0", "eth0")))
        librenms_server.register(
            "/api/v0/devices/9/ports",
            _ports_payload(_port(500, "Gi0/1", "GigabitEthernet0/1")),
        )

        rows = _make_view().get_links_data(device, server_key=server_key)

        assert rows[0]["remote_port_aliases"] == ["GigabitEthernet0/1"]

    def test_a_neighbour_that_is_not_in_librenms_is_never_fetched(self, librenms_server, settings):
        """remote_device_id 0 means LibreNMS has no port record to read."""
        device, server_key = self._device_on_server("alias-fetch-b", 12, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/12/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": 1,
                        "protocol": "lldp",
                        "local_port_id": 100,
                        "remote_port": "Eth1/8",
                        "remote_hostname": "not-monitored",
                        "remote_port_id": None,
                        "remote_device_id": 0,
                    }
                ],
            },
        )
        librenms_server.register("/api/v0/devices/12/ports", _ports_payload(_port(100, "eth0", "eth0")))

        rows = _make_view().get_links_data(device, server_key=server_key)

        assert not rows[0].get("remote_port_aliases")
        assert not any(path.endswith("/devices/0/ports") for path in _requested_paths(librenms_server))

    def test_many_rows_on_one_neighbour_cost_one_fetch(self, librenms_server, settings):
        """A LAG to one neighbour must not issue a ports read per member."""
        device, server_key = self._device_on_server("alias-fetch-c", 13, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/13/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": index,
                        "protocol": "lldp",
                        "local_port_id": 100 + index,
                        "remote_port": f"Gi0/{index}",
                        "remote_hostname": "peer-switch",
                        "remote_port_id": 500 + index,
                        "remote_device_id": 9,
                    }
                    for index in (1, 2, 3)
                ],
            },
        )
        librenms_server.register(
            "/api/v0/devices/13/ports",
            _ports_payload(*(_port(100 + index, f"eth{index}", f"eth{index}") for index in (1, 2, 3))),
        )
        librenms_server.register(
            "/api/v0/devices/9/ports",
            _ports_payload(*(_port(500 + index, f"Gi0/{index}", f"GigabitEthernet0/{index}") for index in (1, 2, 3))),
        )

        rows = _make_view().get_links_data(device, server_key=server_key)

        assert [row["remote_port_aliases"] for row in rows] == [
            ["GigabitEthernet0/1"],
            ["GigabitEthernet0/2"],
            ["GigabitEthernet0/3"],
        ]
        assert _requested_paths(librenms_server).count("/api/v0/devices/9/ports") == 1

    def test_an_advertised_name_that_matches_neither_field_carries_both(self, librenms_server, settings):
        """CDP can advertise a string that is no port field at all; keep both real names."""
        device, server_key = self._device_on_server("alias-fetch-d", 14, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/14/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": 1,
                        "protocol": "cdp",
                        "local_port_id": 100,
                        "remote_port": "0c:42:a1:00:00:01",
                        "remote_hostname": "peer-switch",
                        "remote_port_id": 500,
                        "remote_device_id": 9,
                    }
                ],
            },
        )
        librenms_server.register("/api/v0/devices/14/ports", _ports_payload(_port(100, "eth0", "eth0")))
        librenms_server.register(
            "/api/v0/devices/9/ports",
            _ports_payload(_port(500, "Gi0/1", "GigabitEthernet0/1")),
        )

        rows = _make_view().get_links_data(device, server_key=server_key)

        assert sorted(rows[0]["remote_port_aliases"]) == ["Gi0/1", "GigabitEthernet0/1"]

    def test_the_advertised_name_finds_the_port_when_librenms_gives_no_port_id(self, librenms_server, settings):
        """LibreNMS knows the neighbour but matched no port record: fall back to the name.

        The advertised case differs from the port record's, as neighbour advertisements do.
        """
        device, server_key = self._device_on_server("alias-fetch-h", 18, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/18/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": 1,
                        "protocol": "lldp",
                        "local_port_id": 100,
                        "remote_port": "GI0/1",
                        "remote_hostname": "peer-switch",
                        "remote_port_id": None,
                        "remote_device_id": 9,
                    }
                ],
            },
        )
        librenms_server.register("/api/v0/devices/18/ports", _ports_payload(_port(100, "eth0", "eth0")))
        librenms_server.register(
            "/api/v0/devices/9/ports",
            _ports_payload(_port(500, "Gi0/1", "GigabitEthernet0/1")),
        )

        rows = _make_view().get_links_data(device, server_key=server_key)

        assert sorted(rows[0]["remote_port_aliases"]) == ["Gi0/1", "GigabitEthernet0/1"]

    def test_a_name_two_ports_share_attaches_no_alias(self, librenms_server, settings):
        """A repeated ifDescr ("Ethernet adapter") must never bind the row to the first port."""
        device, server_key = self._device_on_server("alias-fetch-i", 19, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/19/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": 1,
                        "protocol": "lldp",
                        "local_port_id": 100,
                        "remote_port": "Ethernet adapter",
                        "remote_hostname": "peer-host",
                        "remote_port_id": None,
                        "remote_device_id": 9,
                    }
                ],
            },
        )
        librenms_server.register("/api/v0/devices/19/ports", _ports_payload(_port(100, "eth0", "eth0")))
        librenms_server.register(
            "/api/v0/devices/9/ports",
            _ports_payload(
                _port(500, "eth0", "Ethernet adapter"),
                _port(501, "eth1", "Ethernet adapter"),
            ),
        )

        rows = _make_view().get_links_data(device, server_key=server_key)

        assert not rows[0].get("remote_port_aliases")
        assert not rows[0].get("remote_port_key")

    def test_a_shared_name_does_not_spoil_the_port_id_lookup(self, librenms_server, settings):
        """The stable port id still answers, even where the names are ambiguous."""
        device, server_key = self._device_on_server("alias-fetch-j", 20, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/20/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": 1,
                        "protocol": "lldp",
                        "local_port_id": 100,
                        "remote_port": "Ethernet adapter",
                        "remote_hostname": "peer-host",
                        "remote_port_id": 501,
                        "remote_device_id": 9,
                    }
                ],
            },
        )
        librenms_server.register("/api/v0/devices/20/ports", _ports_payload(_port(100, "eth0", "eth0")))
        librenms_server.register(
            "/api/v0/devices/9/ports",
            _ports_payload(
                _port(500, "eth0", "Ethernet adapter"),
                _port(501, "eth1", "Ethernet adapter"),
            ),
        )

        rows = _make_view().get_links_data(device, server_key=server_key)

        assert rows[0]["remote_port_aliases"] == ["eth1"]
        assert rows[0]["remote_port_key"] == 501

    def test_a_malformed_neighbour_payload_is_not_cached(self, librenms_server, settings):
        """A glitched read must not silence the far end until the cache expires."""
        device, server_key = self._device_on_server("alias-fetch-k", 21, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/21/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": 1,
                        "protocol": "lldp",
                        "local_port_id": 100,
                        "remote_port": "Gi0/1",
                        "remote_hostname": "peer-switch",
                        "remote_port_id": 500,
                        "remote_device_id": 9,
                    }
                ],
            },
        )
        librenms_server.register("/api/v0/devices/21/ports", _ports_payload(_port(100, "eth0", "eth0")))
        librenms_server.register(
            "/api/v0/devices/9/ports",
            {"status": "ok", "ports": [None, {"port_id": 500, "ifName": None, "ifDescr": None}]},
        )

        _make_view().get_links_data(device, server_key=server_key)
        librenms_server.register(
            "/api/v0/devices/9/ports",
            _ports_payload(_port(500, "Gi0/1", "GigabitEthernet0/1")),
        )
        rows = _make_view().get_links_data(device, server_key=server_key)

        assert rows[0]["remote_port_aliases"] == ["GigabitEthernet0/1"]

    def test_an_empty_neighbour_port_list_is_cached(self, librenms_server, settings):
        """A neighbour that really has no ports is a valid answer, not a glitch."""
        device, server_key = self._device_on_server("alias-fetch-l", 22, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/22/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": 1,
                        "protocol": "lldp",
                        "local_port_id": 100,
                        "remote_port": "Gi0/1",
                        "remote_hostname": "peer-switch",
                        "remote_port_id": 500,
                        "remote_device_id": 9,
                    }
                ],
            },
        )
        librenms_server.register("/api/v0/devices/22/ports", _ports_payload(_port(100, "eth0", "eth0")))
        librenms_server.register("/api/v0/devices/9/ports", _ports_payload())

        _make_view().get_links_data(device, server_key=server_key)
        _make_view().get_links_data(device, server_key=server_key)

        assert _requested_paths(librenms_server).count("/api/v0/devices/9/ports") == 1

    def test_the_neighbour_reads_stop_at_the_time_budget(self, librenms_server, settings, monkeypatch):
        """A refresh must not sit through one timeout per neighbour on a well-connected device."""
        import netbox_librenms_plugin.views.base.cables_view as module

        device, server_key = self._device_on_server("alias-fetch-m", 23, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/23/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": index,
                        "protocol": "lldp",
                        "local_port_id": 100 + index,
                        "remote_port": "Gi0/1",
                        "remote_hostname": f"peer-{index}",
                        "remote_port_id": 500,
                        "remote_device_id": 8 + index,
                    }
                    for index in (1, 2)
                ],
            },
        )
        librenms_server.register(
            "/api/v0/devices/23/ports",
            _ports_payload(*(_port(100 + index, f"eth{index}", f"eth{index}") for index in (1, 2))),
        )

        def slow_ports(**request):
            time.sleep(0.4)
            return 200, _ports_payload(_port(500, "Gi0/1", "GigabitEthernet0/1"))

        librenms_server.register("/api/v0/devices/9/ports", slow_ports)
        librenms_server.register(
            "/api/v0/devices/10/ports",
            _ports_payload(_port(500, "Gi0/1", "GigabitEthernet0/1")),
        )
        monkeypatch.setattr(module, "REMOTE_ALIAS_FETCH_BUDGET_SECONDS", 0.2)

        rows = _make_view().get_links_data(device, server_key=server_key)

        # The first neighbour spent the budget, so the second is not read at all.
        assert _requested_paths(librenms_server).count("/api/v0/devices/10/ports") == 0
        assert rows[0]["remote_port_aliases"] == ["GigabitEthernet0/1"]
        assert not rows[1].get("remote_port_aliases")

    def test_a_second_refresh_reads_the_neighbour_from_cache(self, librenms_server, settings):
        """One switch is the neighbour of many devices; its names are cached per LibreNMS id."""
        device, server_key = self._device_on_server("alias-fetch-f", 16, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/16/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": 1,
                        "protocol": "lldp",
                        "local_port_id": 100,
                        "remote_port": "Gi0/1",
                        "remote_hostname": "peer-switch",
                        "remote_port_id": 500,
                        "remote_device_id": 9,
                    }
                ],
            },
        )
        librenms_server.register("/api/v0/devices/16/ports", _ports_payload(_port(100, "eth0", "eth0")))
        librenms_server.register(
            "/api/v0/devices/9/ports",
            _ports_payload(_port(500, "Gi0/1", "GigabitEthernet0/1")),
        )

        _make_view().get_links_data(device, server_key=server_key)
        rows = _make_view().get_links_data(device, server_key=server_key)

        assert rows[0]["remote_port_aliases"] == ["GigabitEthernet0/1"]
        assert _requested_paths(librenms_server).count("/api/v0/devices/9/ports") == 1

    def test_a_failed_neighbour_read_is_not_cached(self, librenms_server, settings):
        """An outage must not silence the far end for the whole cache lifetime."""
        device, server_key = self._device_on_server("alias-fetch-g", 17, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/17/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": 1,
                        "protocol": "lldp",
                        "local_port_id": 100,
                        "remote_port": "Gi0/1",
                        "remote_hostname": "peer-switch",
                        "remote_port_id": 500,
                        "remote_device_id": 9,
                    }
                ],
            },
        )
        librenms_server.register("/api/v0/devices/17/ports", _ports_payload(_port(100, "eth0", "eth0")))
        librenms_server.register("/api/v0/devices/9/ports", {"status": "error"}, status=500)

        _make_view().get_links_data(device, server_key=server_key)
        librenms_server.register(
            "/api/v0/devices/9/ports",
            _ports_payload(_port(500, "Gi0/1", "GigabitEthernet0/1")),
        )
        rows = _make_view().get_links_data(device, server_key=server_key)

        assert rows[0]["remote_port_aliases"] == ["GigabitEthernet0/1"]

    def test_a_failed_neighbour_fetch_leaves_the_row_alone(self, librenms_server, settings):
        """A neighbour LibreNMS cannot serve must not break the tab."""
        device, server_key = self._device_on_server("alias-fetch-e", 15, librenms_server, settings)
        librenms_server.register(
            "/api/v0/devices/15/links",
            {
                "status": "ok",
                "links": [
                    {
                        "id": 1,
                        "protocol": "lldp",
                        "local_port_id": 100,
                        "remote_port": "Gi0/1",
                        "remote_hostname": "peer-switch",
                        "remote_port_id": 500,
                        "remote_device_id": 9,
                    }
                ],
            },
        )
        librenms_server.register("/api/v0/devices/15/ports", _ports_payload(_port(100, "eth0", "eth0")))
        librenms_server.register("/api/v0/devices/9/ports", {"status": "error"}, status=404)

        rows = _make_view().get_links_data(device, server_key=server_key)

        assert rows[0]["remote_port"] == "Gi0/1"
        assert not rows[0].get("remote_port_aliases")


# ---------------------------------------------------------------------------
# One physical link reported over two protocols
# ---------------------------------------------------------------------------


def _dedupe(rows):
    """Run the real protocol de-duplication over already-enriched rows."""
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    return BaseCableTableView._dedupe_protocol_duplicates(rows)


class TestProtocolDuplicates:
    """CDP and LLDP describe the same adjacency; the table must show it once."""

    def test_the_same_neighbour_over_two_protocols_collapses(self):
        """The defect: one link, two rows, two Sync Cable buttons."""
        rows = _dedupe([_row(protocol="lldp"), _row(protocol="cdp", remote_port="GigabitEthernet0/1")])

        assert len(rows) == 1

    def test_the_dropped_protocol_stays_visible(self):
        """Never silently discard evidence: the survivor names the protocol it absorbed."""
        rows = _dedupe([_row(protocol="lldp"), _row(protocol="cdp", remote_port="GigabitEthernet0/1")])

        assert rows[0]["also_reported_by"] == ["cdp"]

    def test_the_resolved_row_survives(self):
        """Resolution is the better evidence, whichever protocol carried it."""
        rows = _dedupe(
            [
                _row(protocol="cdp", remote_port="GigabitEthernet0/1"),
                _row(protocol="lldp", netbox_remote_interface_id=77),
            ]
        )

        assert [row["netbox_remote_interface_id"] for row in rows] == [77]

    def test_two_rows_resolving_to_different_interfaces_both_survive(self):
        """Different NetBox ports on one neighbour are two links, not one seen twice."""
        rows = _dedupe(
            [
                _row(protocol="lldp", netbox_remote_interface_id=77),
                _row(protocol="cdp", remote_port="Gi0/2", remote_port_id=501, netbox_remote_interface_id=78),
            ]
        )

        assert sorted(row["netbox_remote_interface_id"] for row in rows) == [77, 78]

    def test_a_malformed_survivor_protocol_does_not_break_the_collapse(self):
        """LibreNMS `protocol` is copied unvalidated: an unhashable one must not 500 the table."""
        rows = _dedupe(
            [
                _row(protocol=[], netbox_remote_interface_id=77),
                _row(protocol="cdp", link_id=2, remote_port="GigabitEthernet0/1"),
            ]
        )

        assert len(rows) == 1
        assert rows[0]["also_reported_by"] == ["cdp"]

    def test_an_unresolved_row_is_kept_beside_two_resolved_ones(self):
        """Two resolved remote interfaces: an unresolved row belongs to neither, so keep it."""
        rows = _dedupe(
            [
                _row(protocol="lldp", netbox_remote_interface_id=77),
                _row(protocol="lldp", remote_port="Gi0/2", remote_port_id=501, netbox_remote_interface_id=78),
                _row(protocol="cdp", remote_port="GigabitEthernet0/3", remote_port_id=502),
            ]
        )

        assert len(rows) == 3

    def test_serial_rows_pass_through(self):
        """A serial row carries no discovery protocol, so it is never a protocol duplicate."""
        rows = _dedupe(
            [
                _row(protocol=None, _source="serial", local_port_id="serial:1"),
                _row(protocol=None, _source="serial", local_port_id="serial:2"),
            ]
        )

        assert len(rows) == 2

    def test_an_unresolved_pair_prefers_a_usable_remote_port_id(self):
        """One endpoint, nothing resolved: keep the row LibreNMS itself pointed at a port."""
        rows = _dedupe(
            [
                _row(protocol="lldp", remote_port_id=None, remote_port_key=500),
                _row(protocol="cdp", remote_port="GigabitEthernet0/1", remote_port_id=500),
            ]
        )

        assert [row["protocol"] for row in rows] == ["cdp"]
        assert rows[0]["also_reported_by"] == ["lldp"]

    def test_an_unresolved_pair_otherwise_prefers_lldp(self):
        """LLDP is the standard; CDP is the fallback."""
        rows = _dedupe(
            [
                _row(protocol="cdp", remote_port="GigabitEthernet0/1", remote_port_id=500),
                _row(protocol="lldp", remote_port_id=500),
            ]
        )

        assert [row["protocol"] for row in rows] == ["lldp"]

    def test_two_neighbours_on_one_local_port_both_survive(self):
        """A hub or a phone-plus-PC port reports two distinct devices."""
        rows = _dedupe([_row(protocol="lldp"), _row(protocol="cdp", remote_device_id=10)])

        assert len(rows) == 2

    def test_two_ports_of_one_neighbour_over_one_protocol_both_survive(self):
        """Same protocol means no duplication to collapse: this is real breakout evidence."""
        rows = _dedupe([_row(protocol="lldp"), _row(protocol="lldp", remote_port="Gi0/2", remote_port_id=501)])

        assert len(rows) == 2

    def test_different_local_ports_never_collapse(self):
        """The adjacency identity includes the local port."""
        rows = _dedupe([_row(protocol="lldp"), _row(protocol="cdp", local_port_id=101)])

        assert len(rows) == 2

    def test_a_row_with_no_remote_identity_is_left_alone(self):
        """Nothing to group on means nothing to collapse."""
        rows = _dedupe(
            [
                _row(protocol="lldp", remote_device_id=0, remote_device=None),
                _row(protocol="cdp", remote_device_id=0, remote_device=None),
            ]
        )

        assert len(rows) == 2

    def test_a_third_port_is_not_swallowed_by_a_protocol_duplicate(self):
        """Adding one CDP row must not cost an independent LLDP report of another port."""
        rows = _dedupe(
            [
                _row(protocol="lldp", remote_port="Gi0/1", remote_port_id=500),
                _row(protocol="lldp", remote_port="Gi0/2", remote_port_id=501),
                _row(protocol="cdp", remote_port="GigabitEthernet0/1", remote_port_id=500),
            ]
        )

        assert sorted(row["remote_port_id"] for row in rows) == [500, 501]

    def test_only_the_row_for_the_same_port_records_the_other_protocol(self):
        """Evidence belongs to the endpoint it was reported for, not to the whole neighbour."""
        rows = _dedupe(
            [
                _row(protocol="lldp", remote_port="Gi0/1", remote_port_id=500),
                _row(protocol="lldp", remote_port="Gi0/2", remote_port_id=501),
                _row(protocol="cdp", remote_port="GigabitEthernet0/1", remote_port_id=500),
            ]
        )

        by_port = {row["remote_port_id"]: row for row in rows}
        assert by_port[500]["also_reported_by"] == ["cdp"]
        assert "also_reported_by" not in by_port[501]

    def test_two_different_ports_never_collapse_across_protocols(self):
        """Different LibreNMS ports are different links, whichever protocol reported them."""
        rows = _dedupe(
            [
                _row(protocol="lldp", remote_port="Gi0/1", remote_port_id=500),
                _row(protocol="cdp", remote_port="Gi0/2", remote_port_id=501),
            ]
        )

        assert len(rows) == 2

    def test_two_rows_naming_one_port_record_collapse_without_a_port_id(self):
        """CDP and LLDP spell the name differently, but both matched the same LibreNMS port."""
        rows = _dedupe(
            [
                _row(protocol="lldp", remote_port="Gi0/1", remote_port_id=None, remote_port_key=500),
                _row(
                    protocol="cdp",
                    remote_port="GigabitEthernet0/1",
                    remote_port_id=None,
                    remote_port_key=500,
                ),
            ]
        )

        assert len(rows) == 1
        assert rows[0]["also_reported_by"] == ["cdp"]

    def test_two_unprovable_rows_both_survive(self):
        """No port id and no matched port record: nothing proves these are one link."""
        rows = _dedupe(
            [
                _row(protocol="lldp", remote_port="Gi0/1", remote_port_id=None),
                _row(protocol="cdp", remote_port="GigabitEthernet0/1", remote_port_id=None),
            ]
        )

        assert len(rows) == 2

    def test_a_resolved_row_absorbs_the_unresolved_row_for_the_same_port(self):
        """One endpoint: the row that resolved wins even when the other is the preferred protocol.

        CDP carries the resolution here, so only the resolved-first rule can decide it.
        """
        rows = _dedupe(
            [
                _row(
                    protocol="cdp", remote_port="GigabitEthernet0/1", remote_port_id=500, netbox_remote_interface_id=77
                ),
                _row(protocol="lldp", remote_port="Gi0/1", remote_port_id=500),
            ]
        )

        assert [row.get("netbox_remote_interface_id") for row in rows] == [77]
        assert rows[0]["also_reported_by"] == ["lldp"]

    def test_an_interface_hidden_by_permissions_keeps_its_own_row(self):
        """A row that resolved nothing must not be absorbed into another port's resolved row."""
        rows = _dedupe(
            [
                _row(protocol="lldp", remote_port="Gi0/1", remote_port_id=500, netbox_remote_interface_id=77),
                _row(protocol="cdp", remote_port="Gi0/2", remote_port_id=501),
            ]
        )

        assert len(rows) == 2

    def test_a_row_with_no_protocol_is_not_a_protocol(self):
        """A links payload without a protocol field must not crash the sort or claim a badge."""
        rows = _dedupe(
            [
                _row(protocol="lldp", remote_port_id=500),
                _row(protocol="cdp", remote_port_id=500),
                _row(protocol=None, remote_port_id=500),
            ]
        )

        assert len(rows) == 1
        assert rows[0]["also_reported_by"] == ["cdp"]

    def test_a_hostname_only_neighbour_still_groups(self):
        """No LibreNMS device id: the case-folded hostname is the identity."""
        rows = _dedupe(
            [
                _row(protocol="lldp", remote_device_id=0, remote_device="Peer-Switch", remote_port_id=500),
                _row(protocol="cdp", remote_device_id=0, remote_device="peer-switch", remote_port_id=500),
            ]
        )

        assert len(rows) == 1


@pytest.mark.django_db
class TestDuplicatesAreGoneFromTheRenderedRows:
    """The wiring, not just the helper: enrichment is what the table and the sync path call."""

    def test_one_adjacency_over_two_protocols_enriches_to_one_row(self):
        """Two LibreNMS rows in, one table row out, with both ends resolved against NetBox."""
        server_key = configured_server_key()
        local_device = make_device("dedupe-e2e-local")
        local = make_interface(local_device, "eth0")
        remote_device = make_device("dedupe-e2e-remote")
        remote = make_interface(remote_device, "Gi0/1")
        map_device_to_librenms(remote_device, 9, server_key=server_key)

        rows = _make_view().enrich_links_data(
            [
                _row(protocol="lldp", local_port="eth0", remote_device=remote_device.name),
                _row(
                    protocol="cdp",
                    local_port="eth0",
                    remote_port="GigabitEthernet0/1",
                    remote_device=remote_device.name,
                ),
            ],
            local_device,
            server_key=server_key,
        )

        assert len(rows) == 1
        assert rows[0]["netbox_local_interface_id"] == local.pk
        assert rows[0]["netbox_remote_interface_id"] == remote.pk
        assert rows[0]["also_reported_by"] == ["cdp"]

    def test_two_real_neighbour_ports_still_render_as_two_rows(self):
        """Positive control: enrichment must not collapse two genuine links."""
        server_key = configured_server_key()
        local_device = make_device("dedupe-e2e-local2")
        make_interface(local_device, "eth0")
        remote_device = make_device("dedupe-e2e-remote2")
        make_interface(remote_device, "Gi0/1")
        make_interface(remote_device, "Gi0/2")
        map_device_to_librenms(remote_device, 9, server_key=server_key)

        rows = _make_view().enrich_links_data(
            [
                _row(protocol="lldp", local_port="eth0", remote_device=remote_device.name),
                _row(
                    protocol="cdp",
                    local_port="eth0",
                    remote_port="Gi0/2",
                    remote_port_id=501,
                    remote_device=remote_device.name,
                ),
            ],
            local_device,
            server_key=server_key,
        )

        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Creating the far end LibreNMS reports and NetBox does not have
# ---------------------------------------------------------------------------


def _create_setup(name, *, remote_port_key=500, local="eth0", remote_iface=None):
    """A page device, a modelled neighbour, and one row whose remote port is missing."""
    server_key = configured_server_key()
    local_device = make_device(f"{name}-local")
    local_interface = make_interface(local_device, local)
    remote_device = make_device(f"{name}-remote")
    if remote_iface:
        make_interface(remote_device, remote_iface)
    row = _row(
        local_port=local,
        remote_device=remote_device.name,
        remote_port_key=remote_port_key,
        row_id="row-1",
        netbox_local_interface_id=local_interface.pk,
        netbox_local_device_id=local_device.pk,
        netbox_remote_device_id=remote_device.pk,
        remote_port_owner_id=remote_device.pk,
        device_id=local_device.pk,
    )
    return server_key, local_device, local_interface, remote_device, row


@pytest.mark.django_db
class TestTheCreateAffordance:
    """One rule decides which rows offer the action, and the endpoint reads the same rule."""

    def _affordance(self, row, device):
        view = _make_view()
        view._set_remote_create_affordance(row, device, configured_server_key())
        return row.get("remote_create_url")

    def test_a_modelled_neighbour_with_no_port_gets_the_action(self):
        """The case the action exists for."""
        _, local_device, _, _, row = _create_setup("create-offered")

        assert self._affordance(row, local_device)

    def test_a_migrated_owner_loses_the_remote_create_action(self):
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        server_key, local_device, _, remote_device, row = _create_setup("create-migrated")
        assert self._affordance(row, local_device)
        mark_librenms_migrated(local_device, remote_device.pk, server_key)
        local_device.save(update_fields=["custom_field_data"])

        _make_view()._disable_actions_for_migrated_owners([row], local_device, local_device, server_key)

        assert row.get("remote_create_url") is None

    def test_a_resolved_remote_interface_gets_nothing(self):
        """There is nothing to create: the row is already syncable."""
        _, local_device, _, _, row = _create_setup("create-resolved")
        row["netbox_remote_interface_id"] = 77

        assert self._affordance(row, local_device) is None

    def test_a_neighbour_that_is_not_in_netbox_gets_nothing(self):
        """No device to attach an interface to, so the action is absent, not failing."""
        _, local_device, _, _, row = _create_setup("create-no-device")
        del row["netbox_remote_device_id"]

        assert self._affordance(row, local_device) is None

    def test_a_row_with_no_local_end_gets_nothing(self):
        """There would be nothing to cable the new interface to."""
        _, local_device, _, _, row = _create_setup("create-no-local")
        del row["netbox_local_interface_id"]

        assert self._affordance(row, local_device) is None

    def test_a_row_with_no_librenms_port_record_gets_nothing(self):
        """Without a port record the name and type would both be guesses."""
        _, local_device, _, _, row = _create_setup("create-no-port", remote_port_key=None)

        assert self._affordance(row, local_device) is None

    def test_an_oob_row_gets_nothing(self):
        """OOB rows are context only and are never syncable in any state."""
        _, local_device, _, _, row = _create_setup("create-oob")
        row["_source"] = "oob"

        assert self._affordance(row, local_device) is None

    def test_a_manual_pick_gets_nothing(self):
        """The user already chose the far end by hand."""
        _, local_device, _, _, row = _create_setup("create-manual")
        row["manual_remote"] = True

        assert self._affordance(row, local_device) is None

    def test_a_read_only_user_gets_nothing(self):
        """The action writes, so it is not offered without the plugin's change permission."""
        from django.contrib.auth import get_user_model
        from uuid import uuid4

        _, local_device, _, _, row = _create_setup("create-readonly")
        view = _make_view()
        view.request.user = get_user_model().objects.create_user(username=f"ro-{uuid4().hex}", password="pw")
        view._set_remote_create_affordance(row, local_device, configured_server_key())

        assert row.get("remote_create_url") is None


def _seed_cable_row(device, row, server_key):
    """
    Put one raw snapshot row in the cables cache, the way a refresh would, and return its row id.

    The identity is derived from the row, not chosen by the caller, so the test submits exactly
    what the rendered table would ([[assign_cable_row_ids]]).
    """
    from django.core.cache import cache

    from netbox_librenms_plugin.utils import assign_cable_row_ids
    from netbox_librenms_plugin.views.base.cables_view import _RAW_LINK_KEYS

    raw = assign_cable_row_ids([{key: value for key, value in row.items() if key in _RAW_LINK_KEYS}])
    cache.set(
        _make_view().get_cache_key(device, "links", server_key),
        {"links": raw, "snapshot_token": "remote-create"},
        timeout=300,
    )
    return raw[0]["row_id"]


def _remote_create_url(device):
    """The endpoint under test."""
    from django.urls import reverse

    return reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[device.pk])


def _messages(response):
    """The flash messages a followed response left behind."""
    from django.contrib.messages import get_messages

    return [str(message) for message in get_messages(response.wsgi_request)]


def _logged_in(user):
    """A real test client for *user*."""
    from django.test import Client

    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
class TestCheckAndCreateTheRemoteEnd:
    """GET reports what would be created; POST creates it and the cable, or neither."""

    def _scenario(self, name, librenms_server, settings, *, port=None, advertised="Gi0/1", aliases=None):
        """A page device, a modelled neighbour with no matching port, and a seeded cable row."""
        from netbox_librenms_plugin.tests.conftest import bind_librenms_server, persist_test_server_mapping

        server_key = configured_server_key()
        bind_librenms_server(settings, librenms_server, server_key=server_key)
        local_device = make_device(f"{name}-local")
        local_interface = make_interface(local_device, "eth0")
        remote_device = make_device(f"{name}-remote")
        persist_test_server_mapping(local_device, server_key)
        map_device_to_librenms(remote_device, 9, server_key=server_key)
        librenms_server.register(
            "/api/v0/ports/500",
            {"status": "ok", "port": [port or {"port_id": 500, "ifName": "Gi0/1", "ifType": "ethernetCsmacd"}]},
        )
        row = _row(
            local_port="eth0",
            remote_device=remote_device.name,
            remote_port=advertised,
            remote_port_aliases=aliases,
            remote_port_key=500,
        )
        row_id = _seed_cable_row(local_device, row, server_key)
        return server_key, local_device, local_interface, remote_device, row_id

    @pytest.mark.parametrize("method", ["get", "post"])
    def test_non_htmx_expired_cache_returns_a_conflict(self, method, librenms_server, settings):
        from django.core.cache import cache
        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local, _, _, row_id = self._scenario("expired-create", librenms_server, settings)
        cache.delete(_make_view().get_cache_key(local, "links", server_key))
        client = _logged_in(make_superuser("expired-create-user"))
        response = getattr(client, method)(_remote_create_url(local), {"row_id": row_id, "server_key": server_key})
        assert response.status_code == 409
        assert "Refresh" in response.content.decode()

    def test_the_check_reports_what_would_be_created(self, librenms_server, settings):
        """Step one: the far end is not modelled, so say what creating it would mean."""
        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local_device, _, remote_device, row_id = self._scenario("chk-a", librenms_server, settings)

        response = _logged_in(make_superuser("remote-create-chk-a")).get(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
        )

        assert response.status_code == 200
        body = response.content.decode()
        assert "Gi0/1" in body
        assert remote_device.name in body

    def test_the_check_names_the_mapped_interface_type(self, librenms_server, settings):
        """The type comes from InterfaceTypeMapping, the same path the interface sync uses."""
        from netbox_librenms_plugin.models import InterfaceTypeMapping
        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local_device, _, _, row_id = self._scenario(
            "chk-b",
            librenms_server,
            settings,
            port={"port_id": 500, "ifName": "Gi0/1", "ifType": "ethernetCsmacd", "ifSpeed": 1000000000},
        )
        InterfaceTypeMapping.objects.create(
            librenms_type="ethernetCsmacd", librenms_speed=1000000, netbox_type="1000base-t"
        )

        response = _logged_in(make_superuser("remote-create-chk-b")).get(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
        )

        assert "1000base-t" in response.content.decode()

    def test_the_check_flags_an_unmapped_type(self, librenms_server, settings):
        """Nothing maps it, so the interface would be created as "other": say so, do not hide it."""
        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local_device, _, _, row_id = self._scenario("chk-c", librenms_server, settings)

        body = (
            _logged_in(make_superuser("remote-create-chk-c"))
            .get(_remote_create_url(local_device), {"row_id": row_id, "server_key": server_key})
            .content.decode()
        )

        assert "no mapping" in body

    def test_the_check_reports_missing_port_without_claiming_a_mapping_failure(self, librenms_server, settings):
        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local_device, _, _, row_id = self._scenario("chk-no-port", librenms_server, settings)
        librenms_server.register("/api/v0/ports/500", {"status": "ok", "port": []})

        response = _logged_in(make_superuser("remote-create-chk-no-port")).get(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
        )

        assert response.status_code == 200
        body = response.content.decode()
        assert "type cannot be derived" in body
        assert "no mapping" not in body
        assert "No InterfaceTypeMapping matches" not in body

    def test_the_check_creates_nothing(self, librenms_server, settings):
        """Step one is read-only."""
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local_device, _, remote_device, row_id = self._scenario("chk-d", librenms_server, settings)

        _logged_in(make_superuser("remote-create-chk-d")).get(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
        )

        assert not Interface.objects.filter(device=remote_device).exists()

    def test_the_create_makes_the_interface_and_the_cable(self, librenms_server, settings):
        """Step two: one transaction, both objects."""
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local_device, local_interface, remote_device, row_id = self._scenario(
            "mk-a", librenms_server, settings
        )

        _logged_in(make_superuser("remote-create-mk-a")).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
        )

        created = Interface.objects.get(device=remote_device, name="Gi0/1")
        local_interface.refresh_from_db()
        assert local_interface.cable is not None
        assert created.cable_id == local_interface.cable_id

    @pytest.mark.parametrize("drift", ["owner", "migration"])
    def test_remote_create_rechecks_the_local_owner_after_proposal_resolution(
        self, librenms_server, settings, monkeypatch, drift
    ):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.utils import mark_librenms_migrated
        from netbox_librenms_plugin.views.sync.cables import CableRemoteCreateView

        server_key, local_device, local_interface, remote_device, row_id = self._scenario(
            f"mk-owner-drift-{drift}", librenms_server, settings
        )
        original = CableRemoteCreateView._remote_port_record

        def drift_after_proposal(view, row):
            port = original(view, row)
            if drift == "owner":
                Interface.objects.filter(pk=local_interface.pk).update(device=make_device("mk-owner-drift-other"))
            else:
                fresh = Device.objects.get(pk=local_device.pk)
                mark_librenms_migrated(fresh, remote_device.pk, server_key)
                fresh.save(update_fields=["custom_field_data"])
            return port

        monkeypatch.setattr(CableRemoteCreateView, "_remote_port_record", drift_after_proposal)
        _logged_in(make_superuser(f"remote-create-mk-owner-drift-{drift}")).post(
            _remote_create_url(local_device), {"row_id": row_id, "server_key": server_key}
        )

        assert not Interface.objects.filter(device=remote_device, name="Gi0/1").exists()
        local_interface.refresh_from_db()
        assert local_interface.cable is None

    def test_remote_create_applies_the_page_cache_transition(
        self, librenms_server, settings, django_capture_on_commit_callbacks
    ):
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        server_key, local_device, _, _, row_id = self._scenario("mk-cache-transition", librenms_server, settings)
        snapshot_key = BaseCableTableView().get_cache_key(local_device, "ip_addresses", server_key)
        cache.set(snapshot_key, {"ip_addresses": []}, timeout=300)

        with django_capture_on_commit_callbacks(execute=True):
            response = _logged_in(make_superuser("remote-create-mk-cache-transition")).post(
                _remote_create_url(local_device),
                {"row_id": row_id, "server_key": server_key},
                HTTP_HX_REQUEST="true",
            )

        assert cache.get(snapshot_key) is None
        assert "librenmsCacheChanged" in response["HX-Trigger"]

    def test_migrated_cable_page_cannot_create_a_remote_interface(self, librenms_server, settings):
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        server_key, local_device, _, remote_device, row_id = self._scenario("mk-migrated", librenms_server, settings)
        mark_librenms_migrated(local_device, remote_device.pk, server_key)
        local_device.save(update_fields=["custom_field_data"])

        response = _logged_in(make_superuser("remote-create-mk-migrated")).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
            follow=True,
        )

        assert not Interface.objects.filter(device=remote_device).exists()
        assert any("migrated and is read-only" in message for message in _messages(response))

    def test_migrated_cache_owner_cannot_create_a_remote_interface(self, librenms_server, settings, monkeypatch):
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.utils import mark_librenms_migrated
        from netbox_librenms_plugin.views.sync.cables import CableRemoteCreateView

        server_key, local_device, _, remote_device, row_id = self._scenario(
            "mk-cache-migrated", librenms_server, settings
        )
        cache_device = make_device("mk-cache-migrated-owner")
        mark_librenms_migrated(cache_device, remote_device.pk, server_key)
        cache_device.save(update_fields=["custom_field_data"])
        original = CableRemoteCreateView.get_cached_links_data

        def cached_from_migrated_owner(view, request, obj):
            links = original(view, request, obj)
            view._cache_device = cache_device
            return links

        monkeypatch.setattr(CableRemoteCreateView, "get_cached_links_data", cached_from_migrated_owner)
        response = _logged_in(make_superuser("remote-create-mk-cache-migrated")).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
            follow=True,
        )

        assert not Interface.objects.filter(device=remote_device).exists()
        assert any("migrated and is read-only" in message for message in _messages(response))

    def test_remote_create_closes_its_htmx_modal_after_the_action(self, librenms_server, settings):
        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local_device, _, _, row_id = self._scenario("mk-modal", librenms_server, settings)

        response = _logged_in(make_superuser("remote-create-mk-modal")).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        body = response.content.decode()
        assert 'id="htmx-modal-content" hx-swap-oob="innerHTML"' in body
        assert "closeHtmxModal()" in body

    def test_verify_keeps_remote_create_action_after_member_selection(self, librenms_server, settings):
        from netbox_librenms_plugin.tests.test_cable_verify import _make_request, _make_view

        server_key, local_device, _, _, row_id = self._scenario("verify-create", librenms_server, settings)
        view = _make_view(server_key)
        request = _make_request({"device_id": local_device.pk, "row_id": row_id, "server_key": server_key})

        response = view.post(request)

        assert response.status_code == 200
        actions = json.loads(response.content)["formatted_row"]["actions"]
        assert "Create the remote interface and the cable" in actions
        assert "data-cable-picker-url" in actions

    @pytest.mark.parametrize("method", ["get", "post"])
    def test_a_cabled_local_end_does_not_offer_or_create_a_remote_interface(self, method, librenms_server, settings):
        from dcim.models import Interface
        from netbox_librenms_plugin.tests.conftest import cable_together, make_superuser
        from netbox_librenms_plugin.tests.test_cable_verify import _make_request, _make_view

        server_key, local_device, local_interface, remote_device, row_id = self._scenario(
            "cabled-create", librenms_server, settings
        )
        cable = cable_together(local_interface, make_interface(make_device("existing-peer"), "eth0"))
        view = _make_view(server_key)
        request = _make_request({"device_id": local_device.pk, "row_id": row_id, "server_key": server_key})
        response = view.post(request)
        assert response.status_code == 200
        actions = json.loads(response.content)["formatted_row"]["actions"]
        assert "Create the remote interface and the cable" not in actions

        response = getattr(_logged_in(make_superuser()), method)(
            _remote_create_url(local_device), {"row_id": row_id, "server_key": server_key}
        )
        assert response.status_code in (404, 409)
        assert not Interface.objects.filter(device=remote_device).exists()
        local_interface.refresh_from_db()
        assert local_interface.cable_id == cable.pk

    def test_a_constrained_cable_add_grant_rolls_back_the_remote_interface(self, librenms_server, settings):
        from dcim.models import Cable, Device, Interface
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        server_key, local_device, local_interface, remote_device, row_id = self._scenario(
            "cable-add-scope", librenms_server, settings
        )
        user = make_user_with_perms(
            "cable-add-scope",
            [("view", Device), ("view", Interface), ("change", Interface), ("add", Interface), ("change", Cable)],
        )
        user = grant(user, "add", Cable, constraints={"label__startswith": "permitted-"})
        response = _logged_in(user).post(
            _remote_create_url(local_device), {"row_id": row_id, "server_key": server_key}, follow=True
        )
        assert response.status_code == 200
        assert not Interface.objects.filter(device=remote_device).exists()
        local_interface.refresh_from_db()
        assert local_interface.cable_id is None
        errors = _messages(response)
        assert errors == ["Failed to create cable: You may not add this cable."]

    def test_remote_creation_without_interface_change_permission_reports_the_permission(
        self, librenms_server, settings
    ):
        from dcim.models import Cable, Device, Interface
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        server_key, local, _, remote, row_id = self._scenario("create-no-change", librenms_server, settings)
        user = make_user_with_perms(
            "create-no-change",
            [("view", Device), ("view", Interface), ("add", Interface), ("add", Cable), ("change", Cable)],
        )
        response = _logged_in(user).post(
            _remote_create_url(local), {"row_id": row_id, "server_key": server_key}, follow=True
        )
        assert any("change_interface" in text for text in _messages(response))
        assert not Interface.objects.filter(device=remote).exists()

    def test_the_interface_is_named_from_the_port_record(self, librenms_server, settings):
        """CDP can advertise a string the device does not use; create the port's own name."""
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local_device, _, remote_device, row_id = self._scenario(
            "mk-h",
            librenms_server,
            settings,
            advertised="0c:42:a1:00:00:01",
            port={"port_id": 500, "ifName": "Gi0/1", "ifType": "ethernetCsmacd"},
        )

        _logged_in(make_superuser("remote-create-mk-h")).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
        )

        assert list(Interface.objects.filter(device=remote_device).values_list("name", flat=True)) == ["Gi0/1"]

    def test_the_created_interface_carries_the_librenms_port_id(self, librenms_server, settings):
        """The row resolves by port id from now on, never by name luck."""
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.utils import get_librenms_device_id

        server_key, local_device, _, remote_device, row_id = self._scenario("mk-b", librenms_server, settings)

        _logged_in(make_superuser("remote-create-mk-b")).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
        )

        created = Interface.objects.get(device=remote_device, name="Gi0/1")
        assert get_librenms_device_id(created, server_key, auto_save=False) == 500

    def test_a_hidden_renamed_remote_port_cannot_be_bound_twice(self, librenms_server, settings):
        from dcim.models import Cable, Device, Interface
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.utils import set_librenms_device_id

        server_key, local, near, remote, row_id = self._scenario("hidden-bound-port", librenms_server, settings)
        existing = make_interface(remote, "renamed-port")
        set_librenms_device_id(existing, 500, server_key)
        existing.save()
        user = make_user_with_perms(
            "hidden-bound-port",
            [
                ("view", Device),
                ("change", Interface),
                ("add", Interface),
                ("add", Cable),
                ("change", Cable),
                ("view", Cable),
            ],
        )
        user = grant(user, "view", Interface, constraints={"pk": near.pk})
        response = _logged_in(user).post(
            _remote_create_url(local), {"row_id": row_id, "server_key": server_key}, follow=True
        )
        assert response.status_code == 200
        assert list(Interface.objects.filter(device=remote).values_list("pk", flat=True)) == [existing.pk]
        assert not Cable.objects.exists()
        assert "renamed-port" not in response.content.decode()

    def test_a_name_that_is_already_taken_is_refused(self, librenms_server, settings):
        """The row was offered on the premise that the port is missing. If it is not, stop.

        Two interfaces make the name ambiguous, so the row does not resolve and still offers the
        action; creating or adopting either one would be a guess.
        """
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local_device, _, remote_device, row_id = self._scenario(
            "mk-c", librenms_server, settings, aliases=["GigabitEthernet0/1"]
        )
        make_interface(remote_device, "Gi0/1", iface_type="10gbase-x-sfpp")
        make_interface(remote_device, "GigabitEthernet0/1")

        _logged_in(make_superuser("remote-create-mk-c")).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
        )

        assert Interface.objects.filter(device=remote_device).count() == 2
        assert Interface.objects.get(device=remote_device, name="Gi0/1").cable is None

    def test_a_name_netbox_will_not_accept_is_refused(self, librenms_server, settings):
        """LibreNMS is not bound by NetBox's field limits; a bad name must not 500 the tab."""
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local_device, _, remote_device, row_id = self._scenario(
            "mk-i",
            librenms_server,
            settings,
            port={"port_id": 500, "ifName": "x" * 200, "ifType": "ethernetCsmacd"},
        )

        response = _logged_in(make_superuser("remote-create-mk-i")).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
            follow=True,
        )

        assert not Interface.objects.filter(device=remote_device).exists()
        assert any("will not accept" in text for text in _messages(response))

    def test_a_failed_cable_rolls_the_interface_back(self, librenms_server, settings):
        """Both or neither: a half-done create leaves a stray interface nobody asked for."""
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import cable_together, make_superuser

        server_key, local_device, local_interface, remote_device, row_id = self._scenario(
            "mk-d", librenms_server, settings
        )
        # The local end is already cabled elsewhere, so the cable create must fail.
        cable_together(local_interface, make_interface(make_device("mk-d-occupier"), "eth9"))

        _logged_in(make_superuser("remote-create-mk-d")).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
        )

        assert not Interface.objects.filter(device=remote_device, name="Gi0/1").exists()

    def test_a_user_without_add_interface_is_refused_by_name(self, librenms_server, settings):
        """The action writes to a device the user is not even looking at."""
        from dcim.models import Cable, Device, Interface

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        server_key, local_device, _, remote_device, row_id = self._scenario("mk-e", librenms_server, settings)
        user = make_user_with_perms(
            "remote-create-mk-e",
            [("view", Device), ("view", Interface), ("change", Interface), ("add", Cable), ("change", Cable)],
        )

        response = _logged_in(user).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
            follow=True,
        )

        assert not Interface.objects.filter(device=remote_device, name="Gi0/1").exists()
        assert any("add_interface" in text for text in _messages(response))

    def test_a_constrained_add_grant_cannot_reach_the_remote_device(self, librenms_server, settings):
        """The model-level grant says nothing about WHICH devices; the constraint does.

        The user passes every model-level gate, so only the re-read of the saved object through
        their own 'add' constraints can stop this.
        """
        from dcim.models import Cable, Device, Interface

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        server_key, local_device, _, remote_device, row_id = self._scenario("mk-f", librenms_server, settings)
        user = make_user_with_perms(
            "remote-create-mk-f",
            [("view", Device), ("view", Interface), ("change", Interface), ("add", Cable), ("change", Cable)],
        )
        # May add interfaces, but only on the device being viewed, never on the neighbour.
        user = grant(user, "add", Interface, constraints={"device__name": local_device.name})

        response = _logged_in(user).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
            follow=True,
        )

        assert not Interface.objects.filter(device=remote_device, name="Gi0/1").exists()
        assert any(remote_device.name in text and "may not add" in text for text in _messages(response))

    def test_a_row_that_never_offered_the_action_is_refused(self, librenms_server, settings):
        """The affordance is the eligibility rule; the endpoint does not re-derive it."""
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key, local_device, _, remote_device, row_id = self._scenario("mk-g", librenms_server, settings)
        # An interface the row resolves to: the row becomes syncable, so the action is absent.
        make_interface(remote_device, "Gi0/1")

        response = _logged_in(make_superuser("remote-create-mk-g")).post(
            _remote_create_url(local_device),
            {"row_id": row_id, "server_key": server_key},
        )

        assert response.status_code == 404
        assert Interface.objects.filter(device=remote_device).count() == 1


# ---------------------------------------------------------------------------
# The inline verify renders the far end the same way the table does
# ---------------------------------------------------------------------------


def _seed_cable_rows(device, rows, server_key, snapshot_token="verify-render"):
    """Put raw snapshot rows in the cables cache, the way a refresh would, and return their ids."""
    from django.core.cache import cache

    from netbox_librenms_plugin.utils import assign_cable_row_ids
    from netbox_librenms_plugin.views.base.cables_view import _RAW_LINK_KEYS

    raw = assign_cable_row_ids([{key: value for key, value in row.items() if key in _RAW_LINK_KEYS} for row in rows])
    cache.set(
        _make_view().get_cache_key(device, "links", server_key),
        {"links": raw, "snapshot_token": snapshot_token},
        timeout=300,
    )
    return [entry["row_id"] for entry in raw]


def _verify(client, device, row_id, server_key):
    """POST one row to the inline verify endpoint and return its formatted row."""
    import json

    from django.urls import reverse

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_cable"),
        data=json.dumps({"device_id": device.pk, "row_id": row_id, "server_key": server_key}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()["formatted_row"]


@pytest.mark.django_db
class TestTheVerifiedRowRendersLikeTheTable:
    """Changing the VC member re-renders one row; it must not lose the badges the table draws."""

    def test_the_verified_row_keeps_the_protocol_badge(self):
        """The defect: an inline verify dropped the "also reported over CDP" evidence."""
        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key = configured_server_key()
        # No local interface: this is the branch that renders an unresolved local end.
        local_device = make_device("verify-badge-local")
        remote_device = make_device("verify-badge-remote")
        map_device_to_librenms(remote_device, 9, server_key=server_key)
        lldp_id, _cdp_id = _seed_cable_rows(
            local_device,
            [
                _row(protocol="lldp", remote_device=remote_device.name),
                _row(
                    protocol="cdp",
                    link_id=2,
                    remote_port="GigabitEthernet0/1",
                    remote_device=remote_device.name,
                ),
            ],
            server_key,
        )

        formatted = _verify(_logged_in(make_superuser("verify-badge-a")), local_device, lldp_id, server_key)

        assert "mdi-lan-connect" in formatted["remote_port"]
        assert "Also reported over CDP" in formatted["remote_port"]

    def test_a_row_reported_once_gets_no_protocol_badge(self):
        """Positive control: the badge claims a second protocol, so it must not appear alone."""
        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key = configured_server_key()
        local_device = make_device("verify-solo-local")
        remote_device = make_device("verify-solo-remote")
        map_device_to_librenms(remote_device, 9, server_key=server_key)
        (row_id,) = _seed_cable_rows(
            local_device, [_row(protocol="lldp", remote_device=remote_device.name)], server_key
        )

        formatted = _verify(_logged_in(make_superuser("verify-solo")), local_device, row_id, server_key)

        assert "mdi-lan-connect" not in formatted["remote_port"]

    def test_the_verified_row_keeps_the_manual_pick_badge(self):
        """The badge the verify path was already dropping before the protocol one existed."""
        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.utils import cable_manual_pick_cache_key

        server_key = configured_server_key()
        local_device = make_device("verify-manual-local")
        make_interface(local_device, "eth0")
        remote_device = make_device("verify-manual-remote")
        picked = make_interface(remote_device, "Gi0/9")
        map_device_to_librenms(remote_device, 9, server_key=server_key)
        (row_id,) = _seed_cable_rows(
            local_device, [_row(remote_device=remote_device.name)], server_key, snapshot_token="verify-manual"
        )

        from django.core.cache import cache

        user = make_superuser("verify-manual")
        cache.set(
            cable_manual_pick_cache_key(
                _make_view().get_cache_key(local_device, "links", server_key),
                "verify-manual",
                user.pk,
                row_id,
            ),
            {"manual_remote_id": picked.pk},
            timeout=300,
        )

        formatted = _verify(_logged_in(user), local_device, row_id, server_key)

        assert "mdi-gesture-tap-button" in formatted["remote_port"]
        assert "Gi0/9" in formatted["remote_port"]

    def test_a_malformed_protocol_does_not_break_the_verified_row(self):
        """LibreNMS `protocol` is copied unvalidated; an unhashable one must not 500 the verify."""
        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key = configured_server_key()
        local_device = make_device("verify-badproto-local")
        remote_device = make_device("verify-badproto-remote")
        map_device_to_librenms(remote_device, 9, server_key=server_key)
        (row_id,) = _seed_cable_rows(local_device, [_row(protocol=[], remote_device=remote_device.name)], server_key)

        formatted = _verify(_logged_in(make_superuser("verify-badproto")), local_device, row_id, server_key)

        assert "mdi-lan-connect" not in formatted["remote_port"]

    def test_the_remote_port_name_is_escaped_with_no_link_and_no_badge(self):
        """The bare cell is injected as HTML by the page, so it escapes like every other branch."""
        from netbox_librenms_plugin.tests.conftest import make_superuser

        server_key = configured_server_key()
        local_device = make_device("verify-escape-local")
        (row_id,) = _seed_cable_rows(
            local_device,
            [_row(remote_port="<img src=x onerror=alert(1)>", remote_device="not-in-netbox", remote_device_id=None)],
            server_key,
        )

        formatted = _verify(_logged_in(make_superuser("verify-escape")), local_device, row_id, server_key)

        assert "<img" not in formatted["remote_port"]
        assert "&lt;img" in formatted["remote_port"]


class TestTheRemotePortCellHasOneDefinition:
    """The table column and the verify formatter must read the same renderer."""

    def test_the_table_column_only_delegates(self):
        """A drift guard: comparing output would pass against a re-inlined copy, so read the source."""
        import ast
        import inspect

        from netbox_librenms_plugin.tables import cables

        (column,) = [
            node
            for node in ast.walk(ast.parse(inspect.getsource(cables)))
            if isinstance(node, ast.FunctionDef) and node.name == "render_remote_port"
        ]
        # Docstring, then one `return remote_port_html(...)`: no second copy of the badge rules.
        body = [node for node in column.body if not isinstance(node, ast.Expr)]

        assert len(body) == 1
        assert isinstance(body[0], ast.Return)
        assert isinstance(body[0].value, ast.Call)
        assert body[0].value.func.id == "remote_port_html"

    def test_the_table_column_renders_what_the_shared_renderer_returns(self):
        """The delegation is real: the column's output is the helper's, badges included."""
        from netbox_librenms_plugin.tables.cables import LibreNMSCableTable
        from netbox_librenms_plugin.utils import remote_port_html

        record = {"manual_remote": True, "also_reported_by": ["cdp"], "remote_port_url": "/dcim/interfaces/1/"}

        table = LibreNMSCableTable([], device=None)

        assert table.render_remote_port("Gi0/1", record) == remote_port_html("Gi0/1", record)

    def test_a_name_with_no_link_and_no_badge_is_escaped(self):
        """The branch that used to return the raw string: the verify path injects it as HTML."""
        from netbox_librenms_plugin.utils import remote_port_html

        assert remote_port_html("<b>x</b>", {}) == "&lt;b&gt;x&lt;/b&gt;"


@pytest.mark.django_db
@pytest.mark.parametrize("hostname", ["missing-neighbour.example.test", ""])
@pytest.mark.parametrize("cabled", [True, False])
def test_an_unmodelled_neighbour_keeps_the_local_cable_report(client, hostname, cabled):
    from django.urls import reverse
    from netbox_librenms_plugin.tests.conftest import cable_together, make_superuser

    server_key = configured_server_key()
    local_device = make_device("unmodelled-cable-local")
    local = make_interface(local_device, "eth0")
    peer = make_interface(make_device("unmodelled-cable-peer"), "eth1")
    cable = cable_together(local, peer) if cabled else None
    row = _row(remote_device=hostname, remote_device_id=None)
    row_id = _seed_cable_row(local_device, row, server_key)
    enriched = _make_view().enrich_links_data([dict(row)], local_device, server_key=server_key)[0]
    if cabled:
        assert enriched["cable_url"] == cable.get_absolute_url()
        assert enriched["cable_status"] == f"Cabled to {peer.device.name}"
    else:
        assert not enriched.get("cable_url")
        if hostname:
            assert enriched["cable_status"] == "Device Not Found in NetBox"
    assert not enriched.get("can_create_cable")

    client.force_login(make_superuser("unmodelled-cable-user"))
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_cable"),
        data=json.dumps({"device_id": local_device.pk, "row_id": row_id, "server_key": server_key}),
        content_type="application/json",
    )
    assert response.status_code == 200
    formatted = response.json()["formatted_row"]
    if cabled:
        assert formatted["cable_status"] == f'<a href="{cable.get_absolute_url()}">Cabled to {peer.device.name}</a>'
    else:
        assert "Cabled to" not in formatted["cable_status"]
    assert not formatted["can_create_cable"]


@transactional_db_with_all_apps()
def test_remote_creation_locks_both_devices_before_inserting_an_interface(librenms_server, settings):
    from django.db import DatabaseError, connection, connections
    from netbox_librenms_plugin.tests.conftest import make_superuser

    server_key, local, _, remote, row_id = TestCheckAndCreateTheRemoteEnd()._scenario(
        "create-owner-lock", librenms_server, settings
    )
    client = _logged_in(make_superuser("create-owner-lock-user"))
    observed = []

    def inspect_owner_locks(execute, sql, params, many, context):
        if sql.startswith('INSERT INTO "dcim_interface"'):
            for owner in (local, remote):
                other = connections.create_connection("default")
                other.set_autocommit(False)
                try:
                    with other.cursor() as cursor:
                        cursor.execute('SELECT id FROM "dcim_device" WHERE id = %s FOR UPDATE NOWAIT', [owner.pk])
                except DatabaseError as exc:
                    observed.append((owner.pk, exc.__cause__.sqlstate))
                finally:
                    other.rollback()
                    other.close()
            assert observed == [(local.pk, "55P03"), (remote.pk, "55P03")]
        return execute(sql, params, many, context)

    with connection.execute_wrapper(inspect_owner_locks):
        response = client.post(_remote_create_url(local), {"row_id": row_id, "server_key": server_key})
    assert response.status_code == 302
    assert len(observed) == 2


@pytest.mark.django_db
@pytest.mark.parametrize("failure", ["manual", "ambiguous"])
def test_specific_remote_failure_survives_a_visible_local_cable(failure):
    from django.core.cache import cache
    from netbox_librenms_plugin.utils import cable_manual_pick_cache_key
    from netbox_librenms_plugin.tests.conftest import cable_together, make_superuser

    key = configured_server_key()
    local = make_device("failure-status-local")
    interface = make_interface(local, "eth0")
    cable_together(interface, make_interface(make_device("failure-status-peer"), "eth1"))
    user = make_superuser("failure-status-user")
    row = _row(remote_device="failure-status-remote", remote_device_id=9)
    if failure == "ambiguous":
        for name in ("failure-status-first", "failure-status-second"):
            map_device_to_librenms(make_device(name), 9, server_key=key)
        expected = "Multiple devices found with the same LibreNMS ID"
    else:
        expected = "Selected remote port is no longer available"
    row_id = _seed_cable_row(local, row, key)
    if failure == "manual":
        raw_key = _make_view().get_cache_key(local, "links", key)
        payload = cache.get(raw_key)
        cache.set(
            cable_manual_pick_cache_key(raw_key, payload["snapshot_token"], user.pk, row_id),
            {"manual_remote_id": 999999999},
            timeout=300,
        )
        enriched = _make_view().enrich_links_data([{**row, "manual_remote_id": 999999999}], local, server_key=key)[0]
        assert enriched["cable_status"] == expected
    formatted = _verify(_logged_in(user), local, row_id, key)
    assert expected in formatted["cable_status"]
    assert "Cabled to" not in formatted["cable_status"]


@pytest.mark.django_db
@pytest.mark.parametrize("port_name", ["Gi2/0/1", "unresolved-port"])
def test_remote_create_uses_the_resolved_chassis_member(librenms_server, settings, port_name):
    from dcim.models import Interface, VirtualChassis
    from netbox_librenms_plugin.tests.conftest import make_superuser

    key, local, local_interface, advertised, _ = TestCheckAndCreateTheRemoteEnd()._scenario(
        "create-resolved-member", librenms_server, settings
    )
    chassis = VirtualChassis.objects.create(name="create-resolved-chassis")
    advertised.virtual_chassis, advertised.vc_position = chassis, 1
    advertised.save()
    member = make_device("create-resolved-member-two")
    member.virtual_chassis, member.vc_position = chassis, 2
    member.save()
    row = _row(remote_device=advertised.name, remote_device_id=9, remote_port=port_name, remote_port_key=500)
    row_id = _seed_cable_row(local, row, key)
    enriched = _make_view().enrich_links_data([dict(row)], local, server_key=key)[0]
    client = _logged_in(make_superuser("create-resolved-member-user"))
    if port_name == "unresolved-port":
        assert not enriched.get("remote_create_url")
    else:
        assert enriched.get("remote_create_url")
    response = client.post(_remote_create_url(local), {"row_id": row_id, "server_key": key})
    assert not Interface.objects.filter(device=advertised).exists()
    local_interface.refresh_from_db()
    if port_name == "unresolved-port":
        assert response.status_code == 404
        assert local_interface.cable_id is None
    else:
        created = Interface.objects.get(device=member, name="Gi0/1")
        assert created.cable_id == local_interface.cable_id is not None


@pytest.mark.django_db
def test_remote_create_refuses_a_port_already_bound_on_another_device(librenms_server, settings):
    from dcim.models import Interface
    from netbox_librenms_plugin.tests.conftest import make_superuser
    from netbox_librenms_plugin.utils import set_librenms_device_id

    key, local, local_interface, remote, row_id = TestCheckAndCreateTheRemoteEnd()._scenario(
        "create-foreign-port", librenms_server, settings
    )
    holder = make_interface(make_device("create-foreign-holder"), "private-held-port")
    set_librenms_device_id(holder, 500, key)
    holder.save()
    response = _logged_in(make_superuser("create-foreign-port-user")).post(
        _remote_create_url(local), {"row_id": row_id, "server_key": key}
    )
    assert response.status_code in (302, 404)
    assert not Interface.objects.filter(device=remote).exists()
    local_interface.refresh_from_db()
    assert local_interface.cable_id is None
