"""
A port_stack pair no rule classifies (issue #179 item 10).

LibreNMS reports that two ports are stacked and nothing else: no relationship kind, and no usable
direction either, since a sub-unit sits on the ``low`` side on Junos while a LAG aggregate sits on
either side on SR OS. The plugin infers the kind from a ``.N`` name suffix, from ``ifType`` and from
the per-OS name patterns, and used to drop every pair those three missed. That made a Linux bridge
whose name the pattern rejects look exactly like a device LibreNMS holds no port_stack for.

The device these cases are modelled on is a real one: an EVE-NG host whose bridges are named
``pnet0``-``pnet9`` and ``nat0``, none of which the shipped ``^(vmbr|br|bridge)\\d+$`` accepts. Its
269 port_stack rows collapse to 5 usable pairs, because the other 264 name tap interfaces LibreNMS
does not poll.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface
from netbox_librenms_plugin.utils import set_librenms_device_id

pytestmark = pytest.mark.django_db

SERVER_KEY = "default"
OLD_BRIDGE_PATTERN = r"^(vmbr|br|bridge)\d+$"


def _port(port_id, name, if_type="ethernetCsmacd"):
    """Return the LibreNMS port row shape the resolver and the table both read."""
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifAlias": "",
        "ifType": if_type,
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "ifPhysAddress": "",
        "_source": "main",
    }


# eth0 is enslaved to bridge pnet0, and natmac to bridge nat0. LibreNMS puts the bridge on the LOW
# side of both rows, which is the inverse of what ifStackTable means, so position cannot be read.
EVE_NG_PORTS = [
    _port(7998, "eth0"),
    _port(8002, "pnet0"),
    _port(8012, "natmac"),
    _port(8013, "nat0"),
]

EVE_NG_STACK = [
    {"high_port_id": 7998, "low_port_id": 8002, "high_ifIndex": 2, "low_ifIndex": 6},
    {"high_port_id": 8012, "low_port_id": 8013, "high_ifIndex": 16, "low_ifIndex": 17},
    # A tap interface pair: LibreNMS reports the row but polls neither port, so it has no port id.
    {"high_port_id": None, "low_port_id": None, "high_ifIndex": 139, "low_ifIndex": 115},
]


class TestAPairNoRuleClaims:
    """The resolver carries it instead of dropping it."""

    @staticmethod
    def _resolve(api, bridge_pattern=OLD_BRIDGE_PATTERN):
        return api.resolve_port_relationships(
            EVE_NG_PORTS,
            EVE_NG_STACK,
            lag_patterns={"linux": r"^bond\d+$"},
            bridge_patterns={"linux": bridge_pattern},
            compiled_sap_patterns=[],
            interface_name_field="ifName",
        )

    def test_it_is_reported_on_both_ports(self, mock_librenms_api):
        """Neither side claims to be the composite, so each names the other."""
        relationships = self._resolve(mock_librenms_api)

        assert relationships["bridge_members"] == {}, "the shipped pattern rejects pnet0 and nat0"
        assert relationships["stacked_ports"] == {
            7998: [8002],
            8002: [7998],
            8012: [8013],
            8013: [8012],
        }

    def test_a_classified_pair_is_not_also_stacked(self, mock_librenms_api):
        """A pair a rule claims leaves the untyped map, so the row cannot show both."""
        relationships = self._resolve(mock_librenms_api, r"^(pnet|nat)\d+$")

        assert relationships["bridge_members"] == {7998: 8002, 8012: 8013}
        assert relationships["stacked_ports"] == {}

    def test_a_pair_whose_ends_are_not_polled_is_not_stacked(self, mock_librenms_api):
        """A row naming ports LibreNMS does not poll names nothing this device can show."""
        relationships = self._resolve(mock_librenms_api)

        assert 115 not in relationships["stacked_ports"]
        assert 139 not in relationships["stacked_ports"]


class TestTheDiagnosticsCounters:
    """Why the Relationships column is empty, per device."""

    @staticmethod
    def _diagnostics(api, ports=EVE_NG_PORTS, stack=EVE_NG_STACK, **kwargs):
        return api.resolve_port_relationships(
            ports,
            stack,
            lag_patterns={"linux": r"^bond\d+$"},
            bridge_patterns={"linux": OLD_BRIDGE_PATTERN},
            compiled_sap_patterns=[],
            interface_name_field="ifName",
            **kwargs,
        )["diagnostics"]

    def test_it_separates_a_dropped_row_from_a_row_that_was_never_reported(self, mock_librenms_api):
        """Three rows reported, one unusable, two classified by nothing."""
        diagnostics = self._diagnostics(mock_librenms_api)

        assert diagnostics["pairs_seen"] == 3
        assert diagnostics["pairs_unresolved_end"] == 1
        assert diagnostics["pairs_usable"] == 2
        assert diagnostics["pairs_unclassified"] == 2
        assert diagnostics["ports_seen"] == 4
        assert {kind["key"]: kind["claimed"] for kind in diagnostics["kinds"]} == {
            "sub_interfaces": 0,
            "bridge_members": 0,
            "lag_members": 0,
        }

    def test_no_reported_rows_reads_differently_from_rejected_rows(self, mock_librenms_api):
        """The two cases that both used to render an empty column now say different things."""
        from netbox_librenms_plugin.interface_relationships import relationship_diagnostics_report

        def _report(stack):
            relationships = mock_librenms_api.resolve_port_relationships(
                EVE_NG_PORTS,
                stack,
                lag_patterns={"linux": r"^bond\d+$"},
                bridge_patterns={"linux": OLD_BRIDGE_PATTERN},
                compiled_sap_patterns=[],
                interface_name_field="ifName",
            )
            return relationship_diagnostics_report({"ports": EVE_NG_PORTS, "port_stack_relationships": relationships})

        nothing_reported = _report([])
        rejected = _report(EVE_NG_STACK)

        assert nothing_reported["pairs_seen"] == 0
        assert "no port_stack rows" in nothing_reported["verdict"]
        assert "no rule says what kind" in rejected["verdict"]
        assert rejected["unclassified_pairs"] == [("eth0", "pnet0"), ("natmac", "nat0")]

    def test_it_names_the_patterns_that_were_applied(self, mock_librenms_api):
        """The report shows the regexes in effect, which are what an operator has to change."""
        diagnostics = self._diagnostics(mock_librenms_api)

        assert diagnostics["patterns"]["bridge"] == [OLD_BRIDGE_PATTERN]
        assert diagnostics["patterns"]["lag"] == [r"^bond\d+$"]


class TestTheSeededLinuxPattern:
    """Migration 0021 widens it, reading the row the migration actually left in the database."""

    @staticmethod
    def _seeded_bridge_pattern():
        from netbox_librenms_plugin.models import PortStackLagPattern

        row = next(
            (row for row in PortStackLagPattern.objects.all() if (row.librenms_os or "").strip().lower() == "linux"),
            None,
        )
        assert row is not None, "migration 0019 seeds a linux rule"
        return row.bridge_name_pattern

    def test_it_types_the_bridges_the_old_one_rejected(self, mock_librenms_api):
        """Resolved through the seeded row, not a pattern the test supplies."""
        relationships = mock_librenms_api.resolve_port_relationships(
            EVE_NG_PORTS,
            EVE_NG_STACK,
            device_os="linux",
            interface_name_field="ifName",
        )

        assert relationships["bridge_members"] == {7998: 8002, 8012: 8013}
        assert relationships["stacked_ports"] == {}

    @pytest.mark.parametrize(
        "name", ["vmbr0", "vmbr0v5", "br0", "br-lan", "br-int", "bridge0", "virbr0", "docker0", "pnet0", "nat0"]
    )
    def test_it_accepts_the_common_linux_bridge_names(self, name):
        import re

        assert re.compile(self._seeded_bridge_pattern()).search(name), name

    @pytest.mark.parametrize("name", ["natmac", "virbr0-nic", "eth0", "bond0", "brocade1", "br", "vmbr"])
    def test_it_rejects_a_name_that_is_not_a_bridge(self, name):
        """A pattern matching BOTH sides of a pair claims neither, so a member must not match."""
        import re

        assert not re.compile(self._seeded_bridge_pattern()).search(name), name


class TestTheRefreshReadsThePortStack:
    """
    The whole refresh, through the real view and a real HTTP LibreNMS.

    The fetch used to be skipped unless a port name matched a LAG or bridge pattern, or a port was
    structurally an aggregate or a sub-unit. Neither of this device's two ports is any of those,
    and no seeded pattern of any OS accepts either name, so this fails against that gate: the
    snapshot carries no port_stack_relationships key at all.
    """

    # Deliberately not pnet0: the widened Linux pattern accepts that name, which would give the
    # old gate a name signal and stop this proving anything.
    UNNAMED_PORTS = [_port(7998, "eth0"), _port(8002, "lan0")]
    UNNAMED_STACK = [
        {"high_port_id": 7998, "low_port_id": 8002, "high_ifIndex": 2, "low_ifIndex": 6},
        {"high_port_id": None, "low_port_id": None, "high_ifIndex": 139, "low_ifIndex": 115},
    ]

    def test_no_seeded_pattern_matches_either_port_name(self):
        """The precondition: neither name gives the old gate anything to fire on."""
        from netbox_librenms_plugin.models import PortStackLagPattern

        patterns = [
            *PortStackLagPattern.compiled_patterns_for_os(None),
            *PortStackLagPattern.compiled_bridge_patterns_for_os(None),
        ]
        assert patterns, "the seeded rules must be present, or this proves nothing"
        for port in self.UNNAMED_PORTS:
            assert not any(pattern.search(port["ifName"]) for pattern in patterns), port["ifName"]
            assert port["ifType"] != "ieee8023adLag"
            assert "." not in port["ifName"]

    def test_a_device_with_no_relationship_names_still_gets_its_stack(self, live_librenms):
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.tests.view_test_helpers import post as view_post
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("unclassified-stack-refresh")
        set_librenms_device_id(device, 42, SERVER_KEY)
        device.save()
        live_librenms.api.cache_timeout = 300
        live_librenms.server.device_info_response(42, os="linux")
        live_librenms.server.register("/api/v0/devices/42/port_stack", {"status": "ok", "mappings": self.UNNAMED_STACK})
        live_librenms.server.ports_response(42, self.UNNAMED_PORTS)

        request = make_request("post", {"server_key": SERVER_KEY}, path="/plugins/librenms/sync/")
        request.htmx = True
        view = object.__new__(DeviceInterfaceTableView)
        view.request = request
        view._librenms_api = live_librenms.api
        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 200
        cached = cache.get(view.get_cache_key(device, "ports", SERVER_KEY))
        relationships = cached["port_stack_relationships"]
        assert relationships["diagnostics"]["pairs_seen"] == 2
        assert relationships["diagnostics"]["pairs_unresolved_end"] == 1
        # No rule can type this pair, and the refresh carries it rather than dropping it.
        assert relationships["stacked_ports"] == {7998: [8002], 8002: [7998]}


class TestTheTabRendersIt:
    """The interfaces tab, seeded only through the cache, asked for over HTTP."""

    @staticmethod
    def _cached(stacked_ports, diagnostics):
        return {
            "ports": [dict(port) for port in EVE_NG_PORTS],
            "port_stack_relationships": {
                "lag_members": {},
                "sub_interfaces": {},
                "bridge_members": {},
                "stacked_ports": stacked_ports,
                "diagnostics": diagnostics,
            },
        }

    @staticmethod
    def _diagnostics(**overrides):
        diagnostics = {
            "name_field": "ifName",
            "ports_seen": 19,
            "pairs_seen": 269,
            "pairs_malformed": 0,
            "pairs_unresolved_end": 264,
            "pairs_skipped_sap": 0,
            "pairs_usable": 5,
            "pairs_unclassified": 5,
            "kinds": [
                {"key": key, "claimed": 0, "edges": 0, "conflicted": 0, "from_fallback": False}
                for key in ("sub_interfaces", "bridge_members", "lag_members")
            ],
            "patterns": {"lag": [r"^bond\d+$"], "bridge": [OLD_BRIDGE_PATTERN], "sap": []},
        }
        diagnostics.update(overrides)
        return diagnostics

    def _get_tab(self, device):
        from django.test import Client

        from netbox_librenms_plugin.tests.view_test_helpers import make_superuser

        client = Client()
        client.force_login(make_superuser(f"stacked-{device.pk}-user"))
        response = client.get(f"/dcim/devices/{device.pk}/librenms-sync/", {"tab": "interfaces"})
        assert response.status_code == 200
        return response.content.decode()

    def _seed(self, name, cached):
        from django.core.cache import cache

        from netbox_librenms_plugin.views.mixins import CacheMixin

        device = make_device(name)
        interface = make_interface(device, "pnet0")
        set_librenms_device_id(interface, 8002, SERVER_KEY)
        interface.save()
        set_librenms_device_id(device, 42, SERVER_KEY)
        device.save()
        cache.set(CacheMixin().get_cache_key(device, "ports", SERVER_KEY), cached, 300)
        return device

    def test_an_unclassified_pair_is_shown_on_the_row(self):
        device = self._seed(
            "stacked-pill",
            self._cached({7998: [8002], 8002: [7998]}, self._diagnostics()),
        )

        html = self._get_tab(device)

        assert "</i>1 stacked port</span>" in html
        assert "LibreNMS stacks these with this port, but no rule says how: eth0" in html

    def test_the_tab_says_why_the_column_is_empty(self):
        device = self._seed(
            "stacked-diagnostics",
            self._cached({7998: [8002], 8002: [7998]}, self._diagnostics()),
        )

        html = self._get_tab(device)

        assert "5 of 269 stack rows usable, 5 unclassified" in html
        assert "no rule says what kind of relationship" in html
        assert "dropped: an end names a port LibreNMS does not poll" in html
        assert "<code>eth0</code> &harr; <code>pnet0</code>" in html

    def test_a_device_librenms_reports_nothing_for_reads_differently(self):
        device = self._seed(
            "stacked-no-rows",
            self._cached(
                {}, self._diagnostics(pairs_seen=0, pairs_unresolved_end=0, pairs_usable=0, pairs_unclassified=0)
            ),
        )

        html = self._get_tab(device)

        assert "no port_stack rows for this device" in html
        assert "stacked port</span>" not in html

    def test_a_snapshot_cached_before_this_change_renders_without_it(self):
        device = self._seed(
            "stacked-legacy-cache",
            {
                "ports": [dict(port) for port in EVE_NG_PORTS],
                "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {}, "bridge_members": {}},
            },
        )

        html = self._get_tab(device)

        assert "stack rows usable" not in html
        assert "stacked port</span>" not in html
