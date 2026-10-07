"""
The interfaces tab under interface rules: hidden ignored rows, the toggle, ambiguity and VC owners.

Every test renders the real tab through a real request from a seeded snapshot. The rule decisions
come from real ``InterfaceTypeMapping`` rows and the real owner platform.
"""

import json
import re
from html import unescape

import pytest
from dcim.models import Interface, Platform
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
    seed_own_mapping,
)
from netbox_librenms_plugin.server_mappings import get_librenms_sync_device
from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

SERVER_KEY = "default"
IGNORE = InterfaceTypeMapping.ACTION_IGNORE
RULE_TABLE = "netbox_librenms_plugin_interfacetypemapping"
# The short pill label stays on one line; its title says which member to select and why.
OWNER_PILL = "Select\u00a0member"
OWNER_HINT = "Select the Virtual Chassis member this interface belongs to"


def _port(port_id, name, *, if_type="ethernetCsmacd", descr=None):
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name if descr is None else descr,
        "ifType": if_type,
        "ifAdminStatus": "up",
        "ifSpeed": 10_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": "",
        "ifAlias": "",
    }


def _platform(slug):
    return Platform.objects.create(name=slug.upper(), slug=slug)


def _device(name, platform=None):
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": 61}})
    device.platform = platform
    device.save()
    return device


def _seed(owner, ports, relationships=None):
    cache_owner = get_librenms_sync_device(owner, server_key=SERVER_KEY) or owner
    payload = {"ports": ports, "port_stack_relationships": relationships or {}}
    cache.set(SyncInterfacesView().get_cache_key(cache_owner, "ports", SERVER_KEY), payload, timeout=300)


def _tab(client, owner, object_type="device", **params):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": object_type, "pk": owner.pk, "tab": "interfaces"},
    )
    response = client.get(url, {"server_key": SERVER_KEY, "interface_name_field": "ifName", **params})
    assert response.status_code == 200
    return unescape(response.content.decode())


def _rows(html):
    return {
        int(match.group(1)): match.group(0)
        for match in re.finditer(r'<tr[^>]*data-port-id="(\d+)"[^>]*>.*?</tr>', html, flags=re.S)
    }


def _row_tag(row):
    return row[: row.index(">") + 1]


def _cell(row, column):
    return re.search(rf'<td[^>]*data-col="{column}"[^>]*>(.*?)</td>', row, flags=re.S).group(1)


def _lag_of(member_id, aggregate_id):
    return {"lag_members": {member_id: aggregate_id}, "sub_interfaces": {}, "bridge_members": {}}


def _vlan_ports():
    return [
        _port(100, "eth0"),
        _port(101, "Vlan10", if_type="propVirtual"),
        _port(102, "Vlan20", if_type="propVirtual"),
    ]


@pytest.fixture
def superuser_client(client, settings):
    configure_default_librenms_server(settings)
    client.force_login(make_superuser("interface-rule-tab-user"))
    return client


@pytest.mark.django_db
class TestIgnoredRowsInTheTab:
    """Acceptance 1: a platform Ignore rule hides its ports on that platform only."""

    def test_an_ignored_port_is_hidden_on_its_platform_and_shown_on_another(self, superuser_client):
        platform_p, platform_q = _platform("rule-tab-p"), _platform("rule-tab-q")
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform_p, name_pattern="^Vlan")
        on_p, on_q = _device("rule-tab-on-p", platform_p), _device("rule-tab-on-q", platform_q)
        _seed(on_p, _vlan_ports())
        _seed(on_q, _vlan_ports())

        html_p = _tab(superuser_client, on_p)
        html_q = _tab(superuser_client, on_q)

        assert sorted(_rows(html_p)) == [100]
        assert "2 ignored" in html_p
        assert sorted(_rows(html_q)) == [100, 101, 102]
        assert 'id="interfaces-show-ignored-toggle"' not in html_q

    def test_the_toggle_shows_ignored_rows_greyed_with_the_rule_and_no_controls(self, superuser_client):
        platform = _platform("rule-tab-toggle")
        rule = InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
        device = _device("rule-tab-toggle", platform)
        _seed(device, _vlan_ports())

        html = _tab(superuser_client, device, interfaces_show_ignored="1")
        rows = _rows(html)

        assert sorted(rows) == [100, 101, 102]
        ignored = rows[101]
        assert 'data-rule-state="ignored"' in _row_tag(ignored)
        assert f"Not synced: ignored by interface rule {rule.pk} ({rule})" in ignored
        assert 'name="select"' not in ignored
        assert 'name="sync_one"' not in ignored
        assert 'data-rule-state=""' in _row_tag(rows[100])
        assert 'name="select"' in rows[100]
        # The sync form carries the toggle, so a sync re-renders the tab with the rows still shown.
        assert '<input type="hidden" name="interfaces_show_ignored" value="1">' in html
        assert 'aria-pressed="true"' in html

    def test_the_toggle_icon_shows_an_open_eye_only_while_ignored_rows_are_shown(self, superuser_client):
        platform = _platform("rule-tab-eye")
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
        device = _device("rule-tab-eye", platform)
        _seed(device, _vlan_ports())

        def toggle(html):
            return re.search(r'<a [^>]*id="interfaces-show-ignored-toggle".*?</a>', html, re.S).group(0)

        hidden, shown = (
            toggle(_tab(superuser_client, device)),
            toggle(_tab(superuser_client, device, interfaces_show_ignored="1")),
        )

        assert "mdi-eye-off-outline" in hidden and "mdi-eye-outline" not in hidden
        assert "mdi-eye-outline" in shown and "mdi-eye-off-outline" not in shown
        assert " active" not in hidden and " active" in shown

    def test_the_count_covers_the_whole_snapshot_and_pages_count_only_shown_rows(self, superuser_client):
        platform = _platform("rule-tab-pages")
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
        device = _device("rule-tab-pages", platform)
        ports = [_port(200 + index, f"eth{index:02d}") for index in range(30)]
        ports += [_port(300 + index, f"Vlan{index:02d}", if_type="propVirtual") for index in range(12)]
        _seed(device, ports)

        hidden = _tab(superuser_client, device, interfaces_per_page="10", interfaces_page="3")
        shown = _tab(superuser_client, device, interfaces_per_page="10", interfaces_show_ignored="1")

        assert "Showing 21-30 of 30" in hidden
        assert "12 ignored" in hidden
        assert "interfaces_show_ignored" not in re.search(r'<ul class="pagination.*?</ul>', hidden, re.S).group(0)
        assert "Showing 1-10 of 42" in shown
        assert "12 ignored" in shown
        page_links = re.findall(r'href="(\?tab=interfaces[^"]*)" class="page-link"', shown)
        assert page_links and all("&interfaces_show_ignored=1" in link for link in page_links)
        toggle = re.search(r'<a href="([^"]*)"\s+class="[^"]*"\s+id="interfaces-show-ignored-toggle"', shown).group(1)
        # Turning the toggle off starts again at page 1.
        assert "interfaces_page" not in toggle and "interfaces_show_ignored" not in toggle

    def test_a_bound_ignored_port_keeps_its_interface_out_of_netbox_only(self, superuser_client):
        platform = _platform("rule-tab-netbox-only")
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
        device = _device("rule-tab-netbox-only", platform)
        bound = make_interface(device, "Vlan10", iface_type="virtual")
        seed_own_mapping(bound, 101, SERVER_KEY)
        bound.save()
        make_interface(device, "Vlan20", iface_type="virtual")
        make_interface(device, "stale0")
        _seed(device, _vlan_ports())

        html = _tab(superuser_client, device)

        assert "1 NetBox only interfaces" in html
        assert "stale0" in html
        assert sorted(_rows(html)) == [100]

    def test_a_virtual_machine_reads_its_own_platform(self, superuser_client):
        platform = _platform("rule-tab-vm")
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
        vm = make_vm("rule-tab-vm")
        vm.platform = platform
        seed_stored_mapping(vm, {SERVER_KEY: {"id": 62}})
        vm.save()
        _seed(vm, _vlan_ports())

        html = _tab(superuser_client, vm, object_type="virtualmachine")

        assert sorted(_rows(html)) == [100]
        assert "2 ignored" in html


@pytest.mark.django_db
class TestAmbiguousRowsInTheTab:
    """Acceptance 5, render side: a tie is visible, names both rules, and offers no sync."""

    @pytest.mark.parametrize("reverse_order", [False, True], ids=["insertion-order", "reverse-order"])
    def test_a_tie_shows_the_row_as_ambiguous(self, superuser_client, reverse_order):
        platform = _platform(f"rule-tab-tie-{int(reverse_order)}")
        specs = [("^Te", "10gbase-x-sfpp"), ("1/1$", "10gbase-x-xfp")]
        rules = [
            InterfaceTypeMapping.objects.create(platform=platform, name_pattern=pattern, netbox_type=netbox_type)
            for pattern, netbox_type in (reversed(specs) if reverse_order else specs)
        ]
        device = _device(f"rule-tab-tie-{int(reverse_order)}", platform)
        _seed(device, [_port(400, "Te1/1")])

        row = _rows(_tab(superuser_client, device))[400]

        assert 'data-rule-state="ambiguous"' in _row_tag(row)
        assert "Ambiguous rules" in row
        for rule in rules:
            assert f"{rule.pk} ({rule})" in row
        assert 'name="select"' not in row
        assert 'name="sync_one"' not in row


@pytest.mark.django_db
class TestVirtualChassisOwners:
    """A chassis row reads its owner's platform, and an unresolved owner decides nothing."""

    def _chassis(self, tag):
        platform_p, platform_q = _platform(f"{tag}-p"), _platform(f"{tag}-q")
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform_p, name_pattern="^Gi")
        _vc, (first, second) = make_virtual_chassis_members(tag)
        first.platform = platform_p
        seed_stored_mapping(first, {SERVER_KEY: {"id": 63}})
        first.save()
        second.platform = platform_q
        second.save()
        return first, second

    def test_each_member_row_uses_its_own_platform_and_an_unresolved_row_asks_for_a_member(self, superuser_client):
        first, second = self._chassis("rule-tab-vc")
        _seed(
            first,
            [
                _port(500, "Gi1/0/1"),
                _port(501, "Gi2/0/1"),
                _port(502, "Vlan10", if_type="propVirtual"),
            ],
        )

        html = _tab(superuser_client, first, interfaces_show_ignored="1")
        rows = _rows(html)

        assert 'data-rule-state="ignored"' in _row_tag(rows[500])
        assert 'data-rule-state=""' in _row_tag(rows[501])
        unresolved = rows[502]
        assert 'data-rule-state="owner_unresolved"' in _row_tag(unresolved)
        assert OWNER_PILL in unresolved
        assert '<option value="" selected>Select a member</option>' in unresolved
        assert f'<option value="{first.pk}" selected>' not in unresolved
        assert 'name="sync_one"' not in unresolved

    def _members_on(self, tag, *platforms):
        _vc, members = make_virtual_chassis_members(tag, count=len(platforms))
        for member, platform in zip(members, platforms, strict=True):
            member.platform = platform
            member.save()
        members[0].custom_field_data["librenms_id"] = {SERVER_KEY: {"id": 64}}
        members[0].save()
        return members[0]

    @pytest.mark.parametrize("scoped", [False, True], ids=["global-rule", "platform-rule"])
    def test_an_unresolved_row_is_ignored_when_every_member_shares_the_ignoring_platform(
        self, superuser_client, scoped
    ):
        platform = _platform(f"rule-tab-null0-{int(scoped)}")
        rule = InterfaceTypeMapping.objects.create(
            action=IGNORE, platform=platform if scoped else None, name_pattern="^null0$"
        )
        first = self._members_on(f"rule-tab-null0-{int(scoped)}", platform, platform)
        _seed(first, [_port(700, "Gi1/0/1"), _port(701, "null0", if_type="other")])

        hidden = _tab(superuser_client, first)
        shown = _rows(_tab(superuser_client, first, interfaces_show_ignored="1"))

        assert sorted(_rows(hidden)) == [700]
        assert "1 ignored" in hidden
        assert 'data-rule-state="ignored"' in _row_tag(shown[701])
        assert f"Not synced: ignored by interface rule {rule.pk} ({rule})" in shown[701]
        assert OWNER_PILL not in shown[701]

    def test_mixed_member_platforms_ignore_an_unresolved_row_by_global_rules_only(self, superuser_client):
        platform_p, platform_q = _platform("rule-tab-mixed-p"), _platform("rule-tab-mixed-q")
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform_p, name_pattern="^null0$")
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=None, name_pattern="^lo0$")
        first = self._members_on("rule-tab-mixed", platform_p, platform_q)
        _seed(first, [_port(710, "null0", if_type="other"), _port(711, "lo0", if_type="softwareLoopback")])

        hidden = _tab(superuser_client, first)
        shown = _rows(_tab(superuser_client, first, interfaces_show_ignored="1"))

        assert sorted(_rows(hidden)) == [710]
        assert "1 ignored" in hidden
        assert 'data-rule-state="owner_unresolved"' in _row_tag(shown[710])
        assert 'data-rule-state="ignored"' in _row_tag(shown[711])

    def test_the_verify_repaint_decides_with_the_selected_member(self, superuser_client):
        first, second = self._chassis("rule-tab-verify")
        _seed(first, [_port(600, "Vlan10", if_type="propVirtual")])
        InterfaceTypeMapping.objects.create(
            action=IGNORE, platform=first.platform, name_pattern="^Vlan", librenms_type="propVirtual"
        )

        def _verify(member):
            response = superuser_client.post(
                reverse("plugins:netbox_librenms_plugin:verify_interface"),
                json.dumps(
                    {
                        "device_id": member.pk,
                        "origin_device_id": first.pk,
                        "port_id": 600,
                        "interface_name_field": "ifName",
                        "server_key": SERVER_KEY,
                    }
                ),
                content_type="application/json",
            )
            assert response.status_code == 200
            return response.json()["formatted_row"]

        on_first, on_second = _verify(first), _verify(second)

        assert on_first["rule_state"] == "ignored"
        assert on_first["selection"] == ""
        assert on_second["rule_state"] == ""
        assert 'name="select"' in on_second["selection"]


@pytest.mark.django_db
class TestAnIncompleteRecordIsBlocked:
    """The table and the verify repaint run the writer's check, so they never offer a sync it refuses."""

    @staticmethod
    def _incomplete_port(port_id):
        port = _port(port_id, "eth0")
        del port["ifDescr"]
        return port

    def test_the_tab_asks_for_a_refresh_and_offers_no_sync(self, superuser_client):
        InterfaceTypeMapping.objects.create(librenms_type="ethernetCsmacd", netbox_type="1000base-t")
        device = _device("rule-tab-incomplete", _platform("rule-tab-incomplete"))
        _seed(device, [self._incomplete_port(800)])

        row = _rows(_tab(superuser_client, device))[800]

        assert 'data-rule-state="incomplete"' in _row_tag(row)
        assert "Refresh needed" in row
        assert "the cached LibreNMS port record has no ifDescr; refresh the data" in row
        assert "1000base-t" not in row
        assert 'name="select"' not in row
        assert 'name="sync_one"' not in row

    def test_the_verify_repaint_asks_for_a_refresh(self, superuser_client):
        _vc, (first, _second) = make_virtual_chassis_members("rule-tab-incomplete-verify")
        seed_stored_mapping(first, {SERVER_KEY: {"id": 68}})
        first.save()
        _seed(first, [self._incomplete_port(801)])

        response = superuser_client.post(
            reverse("plugins:netbox_librenms_plugin:verify_interface"),
            json.dumps(
                {
                    "device_id": first.pk,
                    "port_id": 801,
                    "interface_name_field": "ifName",
                    "server_key": SERVER_KEY,
                }
            ),
            content_type="application/json",
        )

        formatted = response.json()["formatted_row"]
        assert formatted["rule_state"] == "incomplete"
        assert formatted["selection"] == ""
        assert "Refresh needed" in formatted["actions"]
        assert 'name="sync_one"' not in formatted["actions"]


@pytest.mark.django_db
class TestTheRulePillSitsInTheActionsCell:
    """A blocked row keeps its relationships in their cell and shows why it is blocked where Sync would be."""

    @staticmethod
    def _member(state):
        member = _port(21, "Gi1/0/1")
        if state == "incomplete":
            del member["ifDescr"]
        return member

    @pytest.mark.parametrize(
        ("state", "label"),
        [("ignored", "Ignored"), ("ambiguous", "Ambiguous rules"), ("incomplete", "Refresh needed")],
    )
    def test_a_blocked_lag_member_shows_its_lag_and_the_pill_in_the_actions_cell(self, superuser_client, state, label):
        platform = _platform(f"rule-tab-pill-{state}")
        if state == "ignored":
            rule = InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Gi")
        elif state == "ambiguous":
            InterfaceTypeMapping.objects.create(platform=platform, name_pattern="^Gi", netbox_type="1000base-t")
            InterfaceTypeMapping.objects.create(platform=platform, name_pattern="/1$", netbox_type="10gbase-t")
        device = _device(f"rule-tab-pill-{state}", platform)
        _seed(device, [_port(20, "ae0", if_type="ieee8023adLag"), self._member(state)], _lag_of(21, 20))

        row = _rows(_tab(superuser_client, device, interfaces_show_ignored="1"))[21]

        assert f'data-rule-state="{state}"' in _row_tag(row)
        relationships, actions = _cell(row, "parent"), _cell(row, "actions")
        assert "LAG" in relationships and "ae0" in relationships
        assert label not in relationships
        assert label in actions
        if state == "ignored":
            assert f"Not synced: ignored by interface rule {rule.pk} ({rule})" in actions
        assert "lag-sync-btn" not in row
        assert 'name="sync_one"' not in row

    def test_an_unresolved_owner_pill_sits_in_the_actions_cell(self, superuser_client):
        _vc, (first, _second) = make_virtual_chassis_members("rule-tab-pill-owner")
        seed_stored_mapping(first, {SERVER_KEY: {"id": 69}})
        first.save()
        _seed(first, [_port(30, "ae0", if_type="ieee8023adLag"), _port(31, "Vlan10")], _lag_of(31, 30))

        row = _rows(_tab(superuser_client, first))[31]

        assert 'data-rule-state="owner_unresolved"' in _row_tag(row)
        assert "LAG" in _cell(row, "parent") and "ae0" in _cell(row, "parent")
        assert OWNER_PILL not in _cell(row, "parent")
        assert OWNER_PILL in _cell(row, "actions")
        assert f'title="{OWNER_HINT}' in _cell(row, "actions")

    def test_the_verify_repaint_puts_the_pill_in_the_actions_cell(self, superuser_client):
        platform = _platform("rule-tab-pill-verify")
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Gi")
        _vc, (first, _second) = make_virtual_chassis_members("rule-tab-pill-verify")
        first.platform = platform
        seed_stored_mapping(first, {SERVER_KEY: {"id": 70}})
        first.save()
        _seed(first, [_port(40, "ae0", if_type="ieee8023adLag"), _port(41, "Gi1/0/1")], _lag_of(41, 40))

        response = superuser_client.post(
            reverse("plugins:netbox_librenms_plugin:verify_interface"),
            json.dumps(
                {
                    "device_id": first.pk,
                    "origin_device_id": first.pk,
                    "port_id": 41,
                    "interface_name_field": "ifName",
                    "server_key": SERVER_KEY,
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        formatted = response.json()["formatted_row"]
        assert formatted["rule_state"] == "ignored"
        assert "LAG" in formatted["parent"] and "ae0" in formatted["parent"]
        assert "Ignored" not in formatted["parent"]
        assert "Ignored" in formatted["actions"]
        assert "lag-sync-btn" not in formatted["parent"]


@pytest.mark.django_db
def test_the_tab_renders_every_blocked_port_for_the_browser_walk(superuser_client):
    platform = _platform("rule-tab-blocked")
    InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
    InterfaceTypeMapping.objects.create(platform=platform, name_pattern="^Te", netbox_type="10gbase-x-sfpp")
    InterfaceTypeMapping.objects.create(platform=platform, name_pattern="1/1$", netbox_type="10gbase-x-xfp")
    device = _device("rule-tab-blocked", platform)
    incomplete = _port(903, "eth9")
    del incomplete["ifSpeed"]
    _seed(device, [_port(900, "eth0"), _port(901, "Vlan10", if_type="propVirtual"), _port(902, "Te1/1"), incomplete])

    html = _tab(superuser_client, device)

    blocked = json.loads(re.search(r'data-blocked-port-ids="(\[.*?\])"', html).group(1))
    assert sorted(blocked) == ["901", "902", "903"]


@pytest.mark.django_db
def test_one_tab_render_reads_the_rules_once(superuser_client):
    platform = _platform("rule-tab-queries")
    InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
    InterfaceTypeMapping.objects.create(platform=platform, name_pattern="^eth", netbox_type="1000base-t")
    device = _device("rule-tab-queries", platform)
    _seed(device, [_port(700 + index, f"eth{index}") for index in range(20)] + _vlan_ports())

    with CaptureQueriesContext(connection) as queries:
        _tab(superuser_client, device, interfaces_show_ignored="1")

    assert sum(RULE_TABLE in query["sql"] for query in queries.captured_queries) == 1
    assert not Interface.objects.filter(device=device).exists()
