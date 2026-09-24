"""
Every port-driven interface write asks the interface rules before it writes anything.

The tests drive real requests against real NetBox objects and real ``InterfaceTypeMapping`` rows:
the bulk and single-row sync, the relationship edges, auto-select, Rebind, the IP tab's interface
create and the cable far-end create. The last tests pin one rule query per request and the AST
guard that keeps rule selection in ``interface_rules``.
"""

import ast
import json
import re
from html import unescape
from pathlib import Path

import pytest
from dcim.models import Interface, Platform
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from ipam.models import IPAddress

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.sync_cache import TAB_SPECS, SyncTab, sync_snapshot_key
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
)
from netbox_librenms_plugin.utils import get_librenms_device_id, get_librenms_sync_device, set_librenms_device_id
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

SERVER_KEY = "default"
IGNORE = InterfaceTypeMapping.ACTION_IGNORE
RULE_TABLE = "netbox_librenms_plugin_interfacetypemapping"
PACKAGE = Path(__file__).resolve().parents[1]


def _port(port_id, name, *, if_type="ethernetCsmacd", descr=None, speed=10_000_000_000):
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name if descr is None else descr,
        "ifType": if_type,
        "ifAdminStatus": "up",
        "ifSpeed": speed,
        "ifMtu": 1500,
        "ifPhysAddress": "",
        "ifAlias": "",
    }


def _platform(slug):
    return Platform.objects.create(name=slug.upper(), slug=slug)


def _device(name, platform=None):
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": 64}})
    device.platform = platform
    device.save()
    return device


def _bound(device, name, port_id, iface_type="other"):
    interface = make_interface(device, name, iface_type=iface_type)
    set_librenms_device_id(interface, port_id, SERVER_KEY)
    interface.save()
    return interface


def _seed(owner, ports, relationships=None):
    cache_owner = get_librenms_sync_device(owner, server_key=SERVER_KEY) or owner
    payload = {"ports": ports, "port_stack_relationships": relationships or {}}
    cache.set(SyncInterfacesView().get_cache_key(cache_owner, "ports", SERVER_KEY), payload, timeout=300)


def _sync(client, owner, *, select=(), sync_one=None, extra=None, htmx=False):
    data = {"server_key": SERVER_KEY, "select": [str(port_id) for port_id in select], **(extra or {})}
    if sync_one is not None:
        data["sync_one"] = str(sync_one)
    url = (
        reverse(
            "plugins:netbox_librenms_plugin:sync_selected_interfaces",
            kwargs={"object_type": "device", "object_id": owner.pk},
        )
        + "?interface_name_field=ifName"
    )
    return client.post(url, data, **({"HTTP_HX_REQUEST": "true"} if htmx else {}))


def _messages(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def _messages_of(request):
    return [str(message) for message in get_messages(request)]


class _PlatformMovesAfterTheRead(SyncInterfacesView):
    """The real sync view, with a concurrent platform change right after it reads the page device."""

    def __init__(self, *, moved_to, **kwargs):
        super().__init__(**kwargs)
        self.moved_to = moved_to

    def get_object(self, object_type, object_id):
        obj = super().get_object(object_type, object_id)
        type(obj).objects.filter(pk=obj.pk).update(platform=self.moved_to)
        return obj


def _tab_row(client, owner, port_id):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": owner.pk, "tab": "interfaces"},
    )
    html = unescape(client.get(url, {"server_key": SERVER_KEY, "interface_name_field": "ifName"}).content.decode())
    match = re.search(rf'<tr[^>]*data-port-id="{port_id}"[^>]*>.*?</tr>', html, flags=re.S)
    assert match is not None
    return match.group(0)


def _type_cell(row):
    return re.search(r'<td[^>]*data-col="type"[^>]*>(.*?)</td>', row, flags=re.S).group(1)


def _shown_type(row):
    """Return the type the Type cell names, the text before its icon."""
    return re.search(r'<span class="[^"]*">(\S+) <i ', _type_cell(row)).group(1)


def _state(interface):
    """Every field a port-driven write can touch, read back from the database."""
    interface = type(interface).objects.get(pk=interface.pk)
    fields = ("name", "type", "speed", "description", "mtu", "enabled", "mode", "lag_id", "parent_id", "bridge_id")
    state = {field: getattr(interface, field) for field in fields}
    state["custom_field_data"] = interface.custom_field_data
    state["primary_mac_address_id"] = interface.primary_mac_address_id
    state["macs"] = sorted(interface.mac_addresses.values_list("pk", flat=True))
    state["untagged_vlan_id"] = interface.untagged_vlan_id
    state["tagged_vlans"] = sorted(interface.tagged_vlans.values_list("pk", flat=True))
    return state


@pytest.fixture
def superuser_client(client, settings):
    configure_default_librenms_server(settings)
    client.force_login(make_superuser("interface-rule-writes-user"))
    return client


def _ignore_vlans(tag):
    platform = _platform(tag)
    rule = InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
    return _device(tag, platform), rule


@pytest.mark.django_db
class TestAnIgnoredPortIsNeverWritten:
    """Acceptance 2: a forged POST that names an ignored port creates and changes nothing."""

    def test_a_bulk_post_does_not_create_a_missing_interface(self, superuser_client):
        device, rule = _ignore_vlans("rule-write-bulk")
        _seed(device, [_port(100, "eth0"), _port(101, "Vlan10", if_type="propVirtual")])

        response = _sync(superuser_client, device, select=[100, 101])

        assert list(Interface.objects.filter(device=device).values_list("name", flat=True)) == ["eth0"]
        assert any(f"Vlan10 (ignored by interface rule {rule.pk} ({rule}))" in text for text in _messages(response))

    def test_a_sync_one_post_does_not_change_an_existing_interface(self, superuser_client):
        device, rule = _ignore_vlans("rule-write-one")
        existing = _bound(device, "Vlan10", 101, iface_type="virtual")
        existing.description = "keep"
        existing.mtu = 9000
        existing.enabled = False
        existing.save()
        before = _state(existing)
        _seed(device, [_port(101, "Vlan10", if_type="propVirtual")])

        response = _sync(superuser_client, device, sync_one=101)

        assert _state(existing) == before
        assert any(f"Vlan10 (ignored by interface rule {rule.pk}" in text for text in _messages(response))

    def test_column_exclusions_do_not_bypass_the_rules(self, superuser_client):
        device, _rule = _ignore_vlans("rule-write-excluded")
        _seed(device, [_port(101, "Vlan10", if_type="propVirtual")])
        excluded = ["name", "type", "speed", "vlans", "mac_address", "mtu", "enabled", "description"]

        _sync(superuser_client, device, select=[101], extra={"exclude_columns": excluded})

        assert not Interface.objects.filter(device=device).exists()

    def test_an_incomplete_port_record_asks_for_a_refresh(self, superuser_client):
        device = _device("rule-write-incomplete", _platform("rule-write-incomplete"))
        port = _port(100, "eth0")
        del port["ifDescr"]
        _seed(device, [port])

        response = _sync(superuser_client, device, select=[100])

        assert not Interface.objects.filter(device=device).exists()
        assert any("record has no ifDescr; refresh the data" in text for text in _messages(response))


@pytest.mark.django_db
class TestAnAmbiguousPortIsNeverWritten:
    """Acceptance 5, write side: a tie creates nothing and changes nothing, in either insertion order."""

    @pytest.mark.parametrize("reverse_order", [False, True], ids=["insertion-order", "reverse-order"])
    def test_a_tie_blocks_the_create_and_the_update(self, superuser_client, reverse_order):
        platform = _platform(f"rule-write-tie-{int(reverse_order)}")
        specs = [("^Te", "10gbase-x-sfpp"), ("1/1$", "10gbase-x-xfp")]
        for pattern, netbox_type in reversed(specs) if reverse_order else specs:
            InterfaceTypeMapping.objects.create(platform=platform, name_pattern=pattern, netbox_type=netbox_type)
        device = _device(f"rule-write-tie-{int(reverse_order)}", platform)
        existing = _bound(device, "Te2/1/1", 401, iface_type="10gbase-t")
        before = _state(existing)
        _seed(device, [_port(400, "Te1/1"), _port(401, "Te2/1/1")])

        response = _sync(superuser_client, device, select=[400, 401])

        assert not Interface.objects.filter(device=device, name="Te1/1").exists()
        assert _state(existing) == before
        (skipped,) = [text for text in _messages(response) if "skipped" in text]
        assert skipped.count("match with equal rank") == 2


@pytest.mark.django_db
class TestASetTypeRuleAgreesWithTheTable:
    """Acceptance 3: the Type cell shows the type the sync writes, and the next render is in sync."""

    @pytest.mark.parametrize(
        ("if_name", "if_descr"),
        [("Te1/1", "Te1/1"), ("uplink1", "Te1/1")],
        ids=["ifname-matches", "only-ifdescr-matches"],
    )
    def test_the_rule_type_is_shown_written_and_then_in_sync(self, superuser_client, if_name, if_descr):
        platform = _platform(f"rule-write-set-{if_name.lower().replace('/', '-')}")
        InterfaceTypeMapping.objects.create(librenms_type="ethernetCsmacd", netbox_type="1000base-t")
        InterfaceTypeMapping.objects.create(platform=platform, name_pattern="^Te", netbox_type="10gbase-x-sfpp")
        device = _device(f"rule-write-set-{if_name.lower().replace('/', '-')}", platform)
        _seed(device, [_port(500, if_name, descr=if_descr)])

        before = _tab_row(superuser_client, device, 500)
        _sync(superuser_client, device, sync_one=500)
        after = _tab_row(superuser_client, device, 500)

        assert _shown_type(before) == "10gbase-x-sfpp"
        assert Interface.objects.get(device=device, name=if_name).type == "10gbase-x-sfpp"
        assert 'name="sync_one"' in before
        assert 'name="sync_one"' not in after
        assert "text-success" in _type_cell(after)


@pytest.mark.django_db
class TestLegacyRulesKeepTheirResult:
    """Acceptance 4: ifType and speed rows give the old result through the table and the writer."""

    @pytest.mark.parametrize(
        ("speed_bps", "expected"),
        [(100_000_000, "other"), (1_000_000_000, "1000base-t"), (25_000_000_000, "10gbase-x-sfpp")],
    )
    def test_the_highest_threshold_at_or_below_the_speed_wins(self, superuser_client, speed_bps, expected):
        InterfaceTypeMapping.objects.create(librenms_type="ethernetCsmacd", netbox_type="other")
        InterfaceTypeMapping.objects.create(
            librenms_type="ethernetCsmacd", librenms_speed=1_000_000, netbox_type="1000base-t"
        )
        InterfaceTypeMapping.objects.create(
            librenms_type="ethernetCsmacd", librenms_speed=10_000_000, netbox_type="10gbase-x-sfpp"
        )
        device = _device(f"rule-write-legacy-{speed_bps}", _platform(f"rule-write-legacy-{speed_bps}"))
        existing = make_interface(device, "eth0", iface_type="virtual")
        _seed(device, [_port(600, "eth0", speed=speed_bps)])

        shown = _shown_type(_tab_row(superuser_client, device, 600))
        _sync(superuser_client, device, sync_one=600)

        assert shown == expected
        existing.refresh_from_db()
        assert existing.type == expected


@pytest.mark.django_db
class TestAChassisRowNeedsItsOwner:
    """An unresolved chassis owner blocks the row; a selected member writes with its platform."""

    def test_the_row_is_refused_without_a_member_and_written_with_one(self, superuser_client):
        _vc, (first, second) = make_virtual_chassis_members("rule-write-vc")
        first.custom_field_data["librenms_id"] = {SERVER_KEY: {"id": 65}}
        first.save()
        _seed(first, [_port(700, "Vlan10", if_type="propVirtual")])

        refused = _sync(superuser_client, first, select=[700])
        _sync(superuser_client, first, select=[700], extra={"device_selection_700": str(second.pk)})

        assert any("Vlan10 (select the Virtual Chassis member that owns it)" in text for text in _messages(refused))
        assert list(Interface.objects.filter(name="Vlan10").values_list("device_id", flat=True)) == [second.pk]

    def test_two_members_posted_for_one_row_refuse_the_row(self, superuser_client):
        _vc, (first, second) = make_virtual_chassis_members("rule-write-vc-twice")
        first.custom_field_data["librenms_id"] = {SERVER_KEY: {"id": 67}}
        first.save()
        _seed(first, [_port(702, "Vlan10", if_type="propVirtual")])

        response = _sync(
            superuser_client, first, select=[702], extra={"device_selection_702": [str(second.pk), str(first.pk)]}
        )

        assert not Interface.objects.filter(name="Vlan10").exists()
        assert any(
            "Vlan10 (the request names more than one Virtual Chassis member for it)" in text
            for text in _messages(response)
        )

    @pytest.mark.parametrize("posted_parent_member", ["other-member", "both-members"])
    def test_auto_select_decides_an_added_row_with_the_owner_the_writer_uses(
        self, superuser_client, posted_parent_member
    ):
        _vc, (first, second) = make_virtual_chassis_members(f"rule-walk-owner-{posted_parent_member}")
        platform = _platform(f"rule-walk-owner-{posted_parent_member}")
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Po1$")
        first.custom_field_data["librenms_id"] = {SERVER_KEY: {"id": 69}}
        first.platform = platform
        first.save()
        aggregate = _bound(first, "Po1", 20, iface_type="lag")
        aggregate_before = _state(aggregate)
        _seed(
            first,
            [
                _port(10, "Gi1/0/1"),
                _port(20, "Po1", if_type="ieee8023adLag"),
                _port(30, "Po1.100", if_type="l2vlan"),
            ],
            {"lag_members": {10: 20}, "sub_interfaces": {30: 20}, "bridge_members": {}},
        )
        # A forged member for the auto-added parent: the writer never reads it for that row.
        forged = [str(second.pk)] if posted_parent_member == "other-member" else [str(first.pk), str(second.pk)]

        _sync(
            superuser_client,
            first,
            select=[30],
            extra={
                "auto_select_lag_members": "1",
                "device_selection_30": str(second.pk),
                "device_selection_20": forged,
            },
        )

        # Po1 is ignored on its inferred member, so neither it nor its member behind it is walked to.
        assert set(Interface.objects.filter(device__in=[first, second]).values_list("name", "device_id")) == {
            ("Po1", first.pk),
            ("Po1.100", second.pk),
        }
        assert _state(aggregate) == aggregate_before

    def test_a_related_row_the_walk_refuses_is_reported_and_the_selected_row_syncs(self, superuser_client):
        _vc, (first, second) = make_virtual_chassis_members("rule-walk-refused")
        first.custom_field_data["librenms_id"] = {SERVER_KEY: {"id": 70}}
        first.save()
        _seed(
            first,
            [_port(21, "Ethernet2.100", if_type="l2vlan"), _port(22, "Vlan100", if_type="propVirtual")],
            {"lag_members": {}, "sub_interfaces": {21: 22}, "bridge_members": {}},
        )

        response = _sync(
            superuser_client,
            first,
            select=[21],
            extra={"auto_select_lag_members": "1", "device_selection_21": str(second.pk)},
        )

        # No member can be inferred for the logical parent, so the walk refuses it, and says so.
        assert list(Interface.objects.filter(device__in=[first, second]).values_list("name", "device_id")) == [
            ("Ethernet2.100", second.pk)
        ]
        assert any(
            "related row Vlan100 not synced: selected target unavailable" in text for text in _messages(response)
        )

    def test_the_selected_members_platform_decides(self, superuser_client):
        _vc, (first, second) = make_virtual_chassis_members("rule-write-vc-platform")
        platform = _platform("rule-write-vc-platform")
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
        first.custom_field_data["librenms_id"] = {SERVER_KEY: {"id": 66}}
        first.platform = platform
        first.save()
        _seed(first, [_port(701, "Vlan10", if_type="propVirtual")])

        _sync(superuser_client, first, select=[701], extra={"device_selection_701": str(first.pk)})
        _sync(superuser_client, first, select=[701], extra={"device_selection_701": str(second.pk)})

        assert list(Interface.objects.filter(name="Vlan10").values_list("device_id", flat=True)) == [second.pk]


def _lag_scenario(tag, ignored_pattern):
    platform = _platform(tag)
    InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern=ignored_pattern)
    device = _device(tag, platform)
    aggregate = _bound(device, "Po1", 20, iface_type="lag")
    other_lag = make_interface(device, "Po2", iface_type="lag")
    member = _bound(device, "Gi0/1", 10, iface_type="1000base-t")
    member.lag = other_lag
    member.save()
    _seed(
        device,
        [_port(10, "Gi0/1"), _port(20, "Po1", if_type="ieee8023adLag")],
        {"lag_members": {10: 20}, "sub_interfaces": {}, "bridge_members": {}},
    )
    return device, member, aggregate, other_lag


@pytest.mark.django_db
class TestRelationshipEdges:
    """An edge with an ignored end is skipped and the existing link stays."""

    def test_an_ignored_aggregate_keeps_the_members_current_link(self, superuser_client):
        device, member, aggregate, other_lag = _lag_scenario("rule-edge-aggregate", "^Po")
        aggregate_before = _state(aggregate)

        response = _sync(superuser_client, device, select=[10], extra={"auto_select_lag_members": "1"})

        member.refresh_from_db()
        assert member.lag_id == other_lag.pk
        assert _state(aggregate) == aggregate_before
        assert any("LAG link to Po1 not synced; Po1: ignored by interface rule" in text for text in _messages(response))

    def test_an_ignored_member_is_not_linked(self, superuser_client):
        device, member, _aggregate, other_lag = _lag_scenario("rule-edge-member", "^Gi")

        _sync(superuser_client, device, select=[10, 20])

        member.refresh_from_db()
        assert member.lag_id == other_lag.pk

    def test_the_single_row_endpoint_refuses_an_ignored_end(self, superuser_client):
        device, member, _aggregate, other_lag = _lag_scenario("rule-edge-single", "^Po")

        response = superuser_client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_interface_lag",
                kwargs={"object_type": "device", "object_id": device.pk},
            ),
            {"port_id": "10", "lag_port_id": "20", "server_key": SERVER_KEY},
        )

        assert response.status_code == 409
        assert "Po1: ignored by interface rule" in response.json()["error"]
        member.refresh_from_db()
        assert member.lag_id == other_lag.pk

    @pytest.mark.parametrize("ignored", [True, False], ids=["parent-ignored", "control"])
    def test_auto_select_never_adds_or_walks_through_an_ignored_port(self, superuser_client, ignored):
        platform = _platform(f"rule-edge-walk-{int(ignored)}")
        if ignored:
            InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Po1$")
        device = _device(f"rule-edge-walk-{int(ignored)}", platform)
        _seed(
            device,
            [
                _port(10, "Gi0/1"),
                _port(11, "Gi0/2"),
                _port(20, "Po1", if_type="ieee8023adLag"),
                _port(30, "Po1.100", if_type="l2vlan"),
            ],
            {"lag_members": {10: 20, 11: 20}, "sub_interfaces": {30: 20}, "bridge_members": {}},
        )

        _sync(superuser_client, device, select=[30], extra={"auto_select_lag_members": "1"})

        created = set(Interface.objects.filter(device=device).values_list("name", flat=True))
        assert created == ({"Po1.100"} if ignored else {"Po1.100", "Po1", "Gi0/1", "Gi0/2"})


def _relink(client, device, relation, port_id, related_port_id):
    """POST one single-row relationship sync, the way the row's inline button does."""
    return client.post(
        reverse(
            f"plugins:netbox_librenms_plugin:sync_interface_{relation}",
            kwargs={"object_type": "device", "object_id": device.pk},
        ),
        {"port_id": str(port_id), f"{relation}_port_id": str(related_port_id), "server_key": SERVER_KEY},
    )


def _promotion_scenario(tag, *, relation, rule_type):
    """A member (or child) port 10 and its aggregate (or parent) port 20, both bound in NetBox."""
    platform = _platform(tag)
    device = _device(tag, platform)
    if relation == "lag":
        rule = InterfaceTypeMapping.objects.create(platform=platform, name_pattern="^Po", netbox_type=rule_type)
        source = _bound(device, "Gi0/1", 10, iface_type="1000base-t")
        target = _bound(device, "Po1", 20, iface_type="other")
        ports = [_port(10, "Gi0/1"), _port(20, "Po1", if_type="ieee8023adLag")]
        relationships = {"lag_members": {10: 20}, "sub_interfaces": {}, "bridge_members": {}}
    else:
        rule = InterfaceTypeMapping.objects.create(platform=platform, name_pattern=r"\.10$", netbox_type=rule_type)
        source = _bound(device, "Gi0/1.10", 10, iface_type="other")
        target = _bound(device, "Gi0/1", 20, iface_type="1000base-t")
        ports = [_port(10, "Gi0/1.10", if_type="l2vlan"), _port(20, "Gi0/1")]
        relationships = {"lag_members": {}, "sub_interfaces": {10: 20}, "bridge_members": {}}
    _seed(device, ports, relationships)
    return device, rule, source, target


@pytest.mark.django_db
class TestPromotionFollowsTheSetType:
    """A promotion runs only to the type a Set type rule sets; any other Set type refuses the edge."""

    @pytest.mark.parametrize("single_row", [False, True], ids=["bulk", "single-row"])
    def test_an_aggregate_set_to_another_type_refuses_the_lag_link(self, superuser_client, single_row):
        device, rule, member, aggregate = _promotion_scenario(
            f"rule-promote-lag-{int(single_row)}", relation="lag", rule_type="1000base-t"
        )
        aggregate.type = "1000base-t"
        aggregate.save()
        member_before, aggregate_before = _state(member), _state(aggregate)
        reason = f"Po1: interface rule {rule.pk} ({rule}) sets type 1000base-t, not lag"

        if single_row:
            response = _relink(superuser_client, device, "lag", 10, 20)
            assert response.status_code == 409
            assert reason in response.json()["error"]
        else:
            response = _sync(superuser_client, device, select=[10], extra={"exclude_columns": ["vlans"]})
            assert any(f"LAG link to Po1 not synced; {reason}" in text for text in _messages(response))

        # The bulk pass still syncs the member's own fields; its link and the aggregate stay.
        assert _state(member)["lag_id"] == member_before["lag_id"] is None
        assert _state(aggregate) == aggregate_before
        if single_row:
            assert _state(member) == member_before

    @pytest.mark.parametrize("single_row", [False, True], ids=["bulk", "single-row"])
    def test_a_child_set_to_a_physical_type_refuses_the_parent_link(self, superuser_client, single_row):
        device, rule, child, parent = _promotion_scenario(
            f"rule-promote-parent-{int(single_row)}", relation="parent", rule_type="1000base-t"
        )
        child.type = "1000base-t"
        child.save()
        child_before, parent_before = _state(child), _state(parent)
        reason = f"interface rule {rule.pk} ({rule}) sets type 1000base-t, not virtual"

        if single_row:
            response = _relink(superuser_client, device, "parent", 10, 20)
            assert response.status_code == 409
            assert f"Gi0/1.10: {reason}" in response.json()["error"]
        else:
            response = _sync(superuser_client, device, select=[10], extra={"exclude_columns": ["vlans"]})
            assert any(f"parent link to Gi0/1 not synced; {reason}" in text for text in _messages(response))

        # The bulk pass still syncs the child's own fields; its link, its type and the parent stay.
        child_after = _state(child)
        assert (child_after["parent_id"], child_after["type"]) == (None, "1000base-t")
        assert _state(parent) == parent_before
        if single_row:
            assert child_after == child_before

    @pytest.mark.parametrize("single_row", [False, True], ids=["bulk", "single-row"])
    @pytest.mark.parametrize(("relation", "rule_type"), [("lag", "lag"), ("parent", "virtual")], ids=["lag", "parent"])
    def test_a_set_type_equal_to_the_promotion_is_still_promoted(
        self, superuser_client, single_row, relation, rule_type
    ):
        device, _rule, source, target = _promotion_scenario(
            f"rule-promote-ok-{relation}-{int(single_row)}", relation=relation, rule_type=rule_type
        )

        if single_row:
            assert _relink(superuser_client, device, relation, 10, 20).status_code == 200
        else:
            # Only the source row is synced, so the promoted end is not written by an attribute pass.
            _sync(superuser_client, device, select=[10], extra={"exclude_columns": ["vlans"]})

        source.refresh_from_db()
        target.refresh_from_db()
        if relation == "lag":
            assert (source.lag_id, target.type) == (target.pk, "lag")
        else:
            assert (source.parent_id, source.type) == (target.pk, "virtual")


def _verify_row(client, device, port_id):
    """Return the cells the verify repaint renders for one row."""
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_interface"),
        json.dumps(
            {"device_id": device.pk, "port_id": port_id, "interface_name_field": "ifName", "server_key": SERVER_KEY}
        ),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return {column: unescape(str(cell)) for column, cell in response.json()["formatted_row"].items()}


def _planning_rule(tag, platform, name, rule, netbox_type):
    """Plan *netbox_type* on port *name*: a legacy ifType mapping, or a platform Set type rule on its name."""
    if rule == "legacy":
        return InterfaceTypeMapping.objects.create(librenms_type=f"{tag}Type", netbox_type=netbox_type)
    return InterfaceTypeMapping.objects.create(
        platform=platform, name_pattern=f"^{re.escape(name)}$", netbox_type=netbox_type
    )


# link: (planned port name, its type, the type the rule plans, its partner name, the partner's type, NetBox's refusal)
_KEPT_LINKS = {
    "aggregate": ("Po1", "lag", "1000base-t", "Gi0/1", "1000base-t", "with LAG members"),
    "lag-member": ("Gi0/1", "1000base-t", "virtual", "Po1", "lag", "cannot have a parent LAG interface"),
    "child": ("Gi0/1.10", "virtual", "1000base-t", "Gi0/1", "1000base-t", "assigned to a parent interface"),
}


def _kept_link_scenario(tag, link, rule):
    """A linked pair in NetBox, and a rule that plans a type the link refuses on port 10."""
    planned_name, planned_type, rule_type, partner_name, partner_type, _refusal = _KEPT_LINKS[link]
    platform = _platform(tag)
    device = _device(tag, platform)
    planned = _bound(device, planned_name, 10, iface_type=planned_type)
    partner = _bound(device, partner_name, 20, iface_type=partner_type)
    if link == "aggregate":
        partner.lag = planned
        partner.save()
        relationships = {"lag_members": {20: 10}, "sub_interfaces": {}, "bridge_members": {}}
    elif link == "lag-member":
        planned.lag = partner
        planned.save()
        relationships = {"lag_members": {10: 20}, "sub_interfaces": {}, "bridge_members": {}}
    else:
        planned.parent = partner
        planned.save()
        relationships = {"lag_members": {}, "sub_interfaces": {10: 20}, "bridge_members": {}}
    created = _planning_rule(tag, platform, planned_name, rule, rule_type)
    _seed(device, [_port(10, planned_name, if_type=f"{tag}Type"), _port(20, partner_name)], relationships)
    return device, planned, partner, created


@pytest.mark.django_db
class TestATypeTheLinksRefuseIsKept:
    """A planned type that breaks a persisted LAG or parent link is kept, the same way in the table and the sync."""

    @pytest.mark.parametrize("rule", ["legacy", "set"], ids=["legacy-mapping", "set-type-rule"])
    @pytest.mark.parametrize("link", list(_KEPT_LINKS))
    def test_the_table_the_sync_and_the_repaint_keep_the_type(self, superuser_client, link, rule):
        tag = f"kept{link.replace('-', '')}{rule}"
        device, planned, partner, created_rule = _kept_link_scenario(tag, link, rule)
        planned_type, rule_type, refusal = _KEPT_LINKS[link][1], _KEPT_LINKS[link][2], _KEPT_LINKS[link][5]
        before_links = (_state(planned), _state(partner))
        note = f"type kept: interface rule {created_rule.pk} ({created_rule}) sets {rule_type}: "

        before = _tab_row(superuser_client, device, 10)
        _sync(superuser_client, device, select=[10, 20])
        after = _tab_row(superuser_client, device, 10)
        repaint = _verify_row(superuser_client, device, 10)

        planned_after, partner_after = _state(planned), _state(partner)
        assert planned_after["type"] == planned_type
        for field in ("lag_id", "parent_id"):
            assert (planned_after[field], partner_after[field]) == (before_links[0][field], before_links[1][field])
        for cell in (_type_cell(before), _type_cell(after), repaint["type"]):
            assert note in cell and refusal in cell, cell
        assert _shown_type(after) == planned_type
        # A kept type is not a difference: the synced row is in sync, in the tab and in the repaint.
        assert "text-success" in _type_cell(after)
        assert 'name="sync_one"' not in after
        assert 'name="sync_one"' not in repaint["actions"]

    def test_an_unrelated_netbox_error_holds_the_type_change(self, superuser_client):
        from netbox_librenms_plugin.tests.conftest import make_required_interface_custom_field

        platform = _platform("kept-unrelated")
        device = _device("kept-unrelated", platform)
        interface = _bound(device, "Gi0/1", 10, iface_type="other")
        custom_field = make_required_interface_custom_field("kept_unrelated_code")
        InterfaceTypeMapping.objects.create(librenms_type="keptUnrelatedType", netbox_type="1000base-t")
        _seed(device, [_port(10, "Gi0/1", if_type="keptUnrelatedType")])

        _sync(superuser_client, device, select=[10])
        cell = _type_cell(_tab_row(superuser_client, device, 10))

        assert _state(interface)["type"] == "other"
        # NetBox words the custom field error differently per release, so read its own message.
        with pytest.raises(ValidationError) as refused:
            Interface.objects.get(pk=interface.pk).clean()
        assert f"'{custom_field.name}'" in refused.value.messages[0]
        assert f"sets 1000base-t: {refused.value.messages[0]}" in cell

    def test_a_created_interface_takes_the_planned_type_unchecked(self, superuser_client):
        """A new row has no links, so NetBox's clean() is not asked; an unrelated error must not hold its type."""
        from netbox_librenms_plugin.tests.conftest import make_required_interface_custom_field

        device = _device("kept-created", _platform("kept-created"))
        make_required_interface_custom_field("kept_created_code")
        InterfaceTypeMapping.objects.create(librenms_type="keptCreatedType", netbox_type="1000base-t")
        _seed(device, [_port(10, "Gi0/1", if_type="keptCreatedType")])

        _sync(superuser_client, device, select=[10])

        assert Interface.objects.get(device=device, name="Gi0/1").type == "1000base-t"


@pytest.mark.django_db
def test_the_walk_decides_with_the_platform_read_under_the_lock():
    """A platform change after the request read the device, and before the lock, must not split the walk and the writer."""
    from types import SimpleNamespace

    from dcim.models import Device

    from netbox_librenms_plugin.tests.view_test_helpers import make_request, post

    platform_p, platform_q = _platform("rule-walk-lock-p"), _platform("rule-walk-lock-q")
    InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform_q, name_pattern="^Po1$")
    device = _device("rule-walk-lock", platform_p)
    view = _PlatformMovesAfterTheRead(moved_to=platform_q)
    view._librenms_api = SimpleNamespace(server_key=SERVER_KEY)
    cache.set(
        view.get_cache_key(device, "ports", SERVER_KEY),
        {
            "ports": [
                _port(10, "Gi0/1"),
                _port(20, "Po1", if_type="ieee8023adLag"),
                _port(30, "Po1.100", if_type="l2vlan"),
            ],
            "port_stack_relationships": {"lag_members": {10: 20}, "sub_interfaces": {30: 20}, "bridge_members": {}},
        },
        timeout=300,
    )
    request = make_request("post", {"server_key": SERVER_KEY, "select": ["30"], "auto_select_lag_members": "1"})
    request.GET = request.GET.copy()
    request.GET["interface_name_field"] = "ifName"

    post(view, request, object_type="device", object_id=device.pk)

    assert Device.objects.get(pk=device.pk).platform_id == platform_q.pk
    # Under the lock the device is on Q, where Po1 is ignored: neither Po1 nor its member behind it is synced.
    assert set(Interface.objects.filter(device=device).values_list("name", flat=True)) == {"Po1.100"}
    assert any("related row Po1 not synced: ignored by interface rule" in text for text in _messages_of(request))


@pytest.mark.django_db
@pytest.mark.parametrize("object_type", ["device", "virtualmachine"])
def test_the_swapped_tab_renders_the_platform_the_writer_read(object_type):
    """The htmx response is rendered from the object as the sync locked it, not as the request first read it."""
    from types import SimpleNamespace

    from netbox_librenms_plugin.tests.conftest import make_vm
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, post

    platform_p, platform_q = _platform(f"rule-swap-{object_type}-p"), _platform(f"rule-swap-{object_type}-q")
    InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform_q, name_pattern="^Vlan")
    if object_type == "device":
        owner = _device("rule-swap-device", platform_p)
    else:
        owner = make_vm("rule-swap-vm")
        owner.platform = platform_p
        owner.custom_field_data["librenms_id"] = {SERVER_KEY: {"id": 71}}
        owner.save()
    view = _PlatformMovesAfterTheRead(moved_to=platform_q)
    view._librenms_api = SimpleNamespace(server_key=SERVER_KEY)
    cache.set(
        view.get_cache_key(owner, "ports", SERVER_KEY),
        {"ports": [_port(40, "eth0"), _port(41, "Vlan10", if_type="propVirtual")], "port_stack_relationships": {}},
        timeout=300,
    )
    request = make_request("post", {"server_key": SERVER_KEY, "select": ["40"]}, HTTP_HX_REQUEST="true")
    request.GET = request.GET.copy()
    request.GET["interface_name_field"] = "ifName"

    response = post(view, request, object_type=object_type, object_id=owner.pk)

    html = unescape(response.content.decode())
    assert response.status_code == 200
    # Under the lock the owner is on Q, where Vlan10 is ignored: the fragment hides it and counts it.
    assert "1 ignored" in html
    assert 'data-port-id="41"' not in html


@pytest.mark.django_db
def test_rebind_refuses_an_ignored_port(superuser_client):
    platform = _platform("rule-rebind")
    rule = InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^eth")
    device = _device("rule-rebind", platform)
    holder = _bound(device, "eth0", 8679)
    _seed(device, [_port(9001, "eth0")])

    response = superuser_client.post(
        reverse(
            "plugins:netbox_librenms_plugin:rebind_interface_port",
            kwargs={"object_type": "device", "object_id": device.pk},
        )
        + "?interface_name_field=ifName",
        {"server_key": SERVER_KEY, "rebind_one": "9001", "rebind_expected_port_9001": "8679"},
    )

    holder.refresh_from_db()
    assert get_librenms_device_id(holder, SERVER_KEY, auto_save=False) == 8679
    assert any(
        f"Rebind is refused for LibreNMS port 9001: ignored by interface rule {rule.pk}" in text
        for text in _messages(response)
    )


@pytest.mark.django_db
def test_the_ip_tab_does_not_create_an_interface_for_an_ignored_port(superuser_client):
    platform = _platform("rule-ip-create")
    rule = InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Ethernet")
    device = _device("rule-ip-create", platform)
    cache.set(
        sync_snapshot_key(device, TAB_SPECS[SyncTab.IP_ADDRESSES].data_type, SERVER_KEY),
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.30.10",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.30.10/24",
                    "port_id": 7030,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {7030: _port(7030, "Ethernet1")},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )

    response = superuser_client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": "device", "pk": device.pk},
        ),
        {
            "server_key": SERVER_KEY,
            "create-missing-interfaces-toggle": "on",
            "select": "198.18.30.10/24",
            "vrf_198.18.30.10/24": "",
        },
    )

    assert not Interface.objects.filter(device=device).exists()
    assert not IPAddress.objects.filter(address="198.18.30.10/24").exists()
    texts = _messages(response)
    assert any(
        f"Skipped (the interface rules refuse the interface): 198.18.30.10/24 (ignored by interface rule {rule.pk}"
        in text
        for text in texts
    )
    assert not any("Failed to sync" in text for text in texts)


@pytest.mark.django_db
class TestTheCableFarEndCreate:
    """The far end is decided with the remote device's platform, and is neither offered nor created."""

    def _scenario(self, name, librenms_server, settings, *, ignored):
        from netbox_librenms_plugin.tests.conftest import (
            bind_librenms_server,
            configured_server_key,
            map_device_to_librenms,
        )
        from netbox_librenms_plugin.tests.test_cable_remote_matching import _row, _seed_cable_row

        server_key = configured_server_key()
        bind_librenms_server(settings, librenms_server, server_key=server_key)
        platform = _platform(f"{name}-remote")
        if ignored:
            InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Gi")
        local_device = make_device(f"{name}-local")
        make_interface(local_device, "eth0")
        remote_device = make_device(f"{name}-remote")
        remote_device.platform = platform
        remote_device.save()
        # Fixed ids, never the device pk, so the neighbour's id cannot be the local device's.
        map_device_to_librenms(local_device, 8, server_key=server_key)
        map_device_to_librenms(remote_device, 9, server_key=server_key)
        librenms_server.register("/api/v0/ports/500", {"status": "ok", "port": [_port(500, "Gi0/1")]})
        row_id = _seed_cable_row(
            local_device, _row(local_port="eth0", remote_device=remote_device.name, remote_port_key=500), server_key
        )
        url = reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[local_device.pk])
        return url, {"row_id": row_id, "server_key": server_key}, remote_device

    @pytest.mark.parametrize("ignored", [True, False], ids=["remote-ignored", "control"])
    def test_an_ignored_remote_port_is_not_offered_or_created(self, client, librenms_server, settings, ignored):
        url, data, remote_device = self._scenario(
            f"rule-cable-{int(ignored)}", librenms_server, settings, ignored=ignored
        )
        client.force_login(make_superuser("rule-cable-user"))

        offer = client.get(url, data)
        client.post(url, data)

        if ignored:
            assert offer.status_code == 409
            assert "The remote port is not created: ignored by interface rule" in offer.content.decode()
            assert not Interface.objects.filter(device=remote_device).exists()
        else:
            assert offer.status_code == 200
            assert Interface.objects.filter(device=remote_device, name="Gi0/1").exists()


@pytest.mark.django_db
def test_a_sync_post_and_the_tab_it_renders_read_the_rules_once(superuser_client):
    platform = _platform("rule-write-queries")
    InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
    InterfaceTypeMapping.objects.create(platform=platform, name_pattern="^eth", netbox_type="1000base-t")
    device = _device("rule-write-queries", platform)
    ports = [_port(800 + index, f"eth{index}") for index in range(10)] + [_port(900, "Vlan10", if_type="propVirtual")]
    _seed(device, ports)

    with CaptureQueriesContext(connection) as queries:
        response = _sync(superuser_client, device, select=[800, 801, 900], htmx=True)

    assert response.status_code == 200
    assert "Showing 1-10 of 10" in response.content.decode()
    assert sum(RULE_TABLE in query["sql"] for query in queries.captured_queries) == 1
    assert Interface.objects.filter(device=device).count() == 2


# The only production functions that assign a ``.type`` attribute, and why each one may.
_TYPE_WRITERS = {
    ("interface_sync.py", "update_interface_from_port"): "the writer; the value comes from planned_interface_type",
    ("interface_diff.py", "type_change_refusal"): "the unsaved copy that NetBox validates",
    ("views/sync/modules.py", "_apply_module_interface_type"): "module apply, after type_change_refusal",
    ("views/sync/interfaces.py", "_promote_lag_aggregate"): "the ratified LAG promotion and its restore",
    ("views/sync/interfaces.py", "_promote_parent_child"): "the ratified parent promotion and its restore",
    ("__init__.py", "_ensure_librenms_id_custom_field"): "a CustomField, not an interface",
}


def _type_writes(source):
    """Return ``(function, line, value)`` for each ``x.type = value`` or ``setattr(x, "type", value)`` in *source*."""
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                elements = [e for t in targets for e in (t.elts if isinstance(t, ast.Tuple) else [t])]
                if any(isinstance(e, ast.Attribute) and e.attr == "type" for e in elements):
                    found.append((function, child.lineno, ast.unparse(child.value)))
            elif (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "setattr"
                and len(child.args) == 3
                and isinstance(child.args[1], ast.Constant)
                and child.args[1].value == "type"
            ):
                found.append((function, child.lineno, ast.unparse(child.args[2])))
            visit(child, function)

    visit(ast.parse(source), None)
    return found


def test_the_type_guard_finds_both_write_forms():
    source = "def f(i):\n    i.type = 'lag'\n    g = lambda: setattr(i, 'type', old)\n"

    assert _type_writes(source) == [("f", 2, "'lag'"), ("f", 3, "old")]


def test_only_the_planned_writers_assign_an_interface_type():
    """A new type write must go through planned_interface_type or type_change_refusal, or be listed with a reason."""
    sites = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).as_posix()
        if relative.startswith(("tests/", "migrations/")):
            continue
        for function, _line, value in _type_writes(path.read_text()):
            sites.setdefault((relative, function), set()).add(value)

    assert set(sites) == set(_TYPE_WRITERS), sites
    assert sites[("interface_sync.py", "update_interface_from_port")] == {"planned_type.value"}


# Modules allowed to read InterfaceTypeMapping rows: the matcher, and the rule management surfaces.
_RULE_READERS = {
    "interface_rules.py",
    "models.py",
    "forms.py",
    "filters.py",
    "tables/mappings.py",
    "api/serializers.py",
    "api/views.py",
    "views/mapping_views.py",
}


def _rule_queries(source):
    """Return the line numbers where *source* reads ``InterfaceTypeMapping.objects``."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and node.attr == "objects"
        and isinstance(node.value, ast.Name)
        and node.value.id == "InterfaceTypeMapping"
    ]


def test_the_guard_finds_a_rule_query():
    assert _rule_queries("from x import InterfaceTypeMapping\nrows = InterfaceTypeMapping.objects.all()\n") == [2]


def test_only_the_matcher_and_the_rule_management_read_rules():
    """One definition of rule selection: no other production module queries the rules."""
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).as_posix()
        if relative.startswith(("tests/", "migrations/")) or relative in _RULE_READERS:
            continue
        offenders.extend(f"{relative}:{line}" for line in _rule_queries(path.read_text()))

    assert offenders == []
