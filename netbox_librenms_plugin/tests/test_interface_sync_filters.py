"""
Server-side filters on the interfaces sync tab: matching, sync states, invalid filters and counts.

Every test renders the real tab through a real request from a seeded snapshot: the fragment that
the sync page loads, the full sync page, or the sync POST. NetBox interfaces, rules and Virtual
Chassis members are real rows.
"""

import json
import re
from html import unescape
from urllib.parse import parse_qs, urlencode

import pytest
from dcim.models import Interface
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from extras.models import customfields
from virtualization.models import VMInterface

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.server_selection import get_librenms_sync_device
from netbox_librenms_plugin.sync_filters import normalize_filter_text
from netbox_librenms_plugin.tests.conftest import (
    bind_librenms_server,
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
    map_device_to_librenms,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping, seed_stored_mapping
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

SERVER_KEY = "default"
LIBRENMS_ID = 4242
STALE_PORT = 8679

ROW = re.compile(r'<tr[^>]*data-port-id="(\d+)"[^>]*>.*?</tr>', re.S)
CELL = re.compile(r'<td[^>]*data-col="([^"]+)"[^>]*>(.*?)</td>', re.S)
BLOCK_TAG = re.compile(r"</?(?:div|br|p|li)\b[^>]*>", re.I)
TAG = re.compile(r"<[^>]+>")
OPTION = re.compile(r"<option([^>]*)>(.*?)</option>", re.S)


def _port(port_id, name, **fields):
    port = {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifType": "ethernetCsmacd",
        "ifAdminStatus": "up",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": "",
        "ifAlias": "",
    }
    port.update(fields)
    return port


def _seed(owner, ports, relationships=None):
    cache_owner = get_librenms_sync_device(owner, server_key=SERVER_KEY) or owner
    payload = {"ports": ports, "port_stack_relationships": relationships or {}}
    cache.set(SyncInterfacesView().get_cache_key(cache_owner, "ports", SERVER_KEY), payload, timeout=300)


def _bind(interface, port_id):
    seed_mapping(interface, SERVER_KEY, own=port_id, save=True)
    return interface


def _fragment(client, owner, object_type="device", **params):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": object_type, "pk": owner.pk, "tab": "interfaces"},
    )
    query = {"server_key": SERVER_KEY, "interface_name_field": "ifName", "interfaces_per_page": "50", **params}
    return client.get(f"{url}?{urlencode(query, doseq=True)}")


def _tab(client, owner, object_type="device", **params):
    response = _fragment(client, owner, object_type, **params)
    assert response.status_code == 200
    return response.content.decode()


def _rows(html):
    """Return each rendered row's markup by port ID, in table order."""
    return {int(match.group(1)): match.group(0) for match in ROW.finditer(html)}


def _selected_label(select):
    """Return the label a select shows: its selected option, else its first one."""
    options = OPTION.findall(select.group(0))
    chosen = next((label for attrs, label in options if re.search(r"\bselected\b", attrs)), None)
    return f" {chosen if chosen is not None else options[0][1] if options else ''} "


def _visible_text(cell):
    """Return the text a browser shows for a cell: each select shows its selected option, a block tag breaks words."""
    cell = re.sub(r"<select\b.*?</select>", _selected_label, cell, flags=re.S)
    return normalize_filter_text(unescape(TAG.sub("", BLOCK_TAG.sub(" ", cell))))


def _cell_texts(row):
    """Return the visible text of each textual cell of a row, by column."""
    return {column: _visible_text(cell) for column, cell in CELL.findall(row) if column != "selection"}


def _cell(row, column):
    return dict(CELL.findall(row))[column]


def _superuser_client(client, settings, tag):
    configure_default_librenms_server(settings)
    client.force_login(make_superuser(f"interface-filter-{tag}"))
    return client


def _mixed_device(tag):
    """
    Return a device whose rows cover every sync state, relationship and action the tab shows.

    The rows are: 1001 in sync, 1002 differs (MTU), 1003 not in NetBox with VLANs and other values,
    1004 in sync on an unmapped type, 1005 a LAG with member 1006, 1007 whose name a stale port
    holds, and 1008 ignored by a rule. ``orphan0`` is a NetBox-only interface.
    """
    InterfaceTypeMapping.objects.get_or_create(
        librenms_type="ethernetCsmacd", librenms_speed=None, defaults={"netbox_type": "1000base-t"}
    )
    InterfaceTypeMapping.objects.get_or_create(
        action=InterfaceTypeMapping.ACTION_IGNORE, name_pattern="^loopback", librenms_type=""
    )
    device = make_device(f"filters-{tag}", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
    _bind(Interface.objects.create(device=device, name="eth0", type="1000base-t", speed=1_000_000, mtu=1500), 1001)
    _bind(Interface.objects.create(device=device, name="eth1", type="1000base-t", speed=1_000_000, mtu=9000), 1002)
    _bind(Interface.objects.create(device=device, name="Vlan10", type="virtual", mtu=1500), 1004)
    _bind(make_interface(device, "eth9"), STALE_PORT)
    make_interface(device, "orphan0")
    ports = [
        _port(1001, "eth0"),
        _port(1002, "eth1"),
        _port(
            1003,
            "eth2",
            ifAlias="core uplink",
            ifMtu=9216,
            ifSpeed=10_000_000_000,
            ifPhysAddress="aabbcc000003",
            ifAdminStatus="down",
            untagged_vlan=100,
            tagged_vlans=[200, 300, 400, 500],
        ),
        _port(1004, "Vlan10", ifType="propVirtual", ifSpeed=None),
        _port(1005, "po1", ifType="ieee8023adLag"),
        _port(1006, "eth5"),
        _port(1007, "eth9"),
        _port(1008, "loopback0", ifType="softwareLoopback"),
    ]
    relationships = {"lag_members": {1006: 1005}, "sub_interfaces": {}, "bridge_members": {}}
    _seed(device, ports, relationships)
    return device


MIXED_STATES = {
    1001: "matching",
    1002: "mismatched",
    1003: "not_in_netbox",
    1004: "matching",
    1005: "not_in_netbox",
    1006: "not_in_netbox",
    1007: "not_in_netbox",
}


def _chassis(tag):
    """Return a VC member page whose rows belong to both members, one row with no resolved owner."""
    _vc, (first, second) = make_virtual_chassis_members(f"filters-{tag}", 2)
    seed_stored_mapping(first, {SERVER_KEY: {"id": LIBRENMS_ID}}, save=True)
    _bind(Interface.objects.create(device=second, name="Ethernet2/1", type="other"), 2001)
    _seed(
        first,
        [
            _port(2001, "Ethernet2/1"),
            _port(2002, "Ethernet1/1", ifAlias="first member", ifPhysAddress="aabbcc000002"),
            _port(2003, "mgmt"),
        ],
    )
    return first, second


def _virtual_machine(tag):
    vm = map_device_to_librenms(make_vm(f"filters-{tag}"), LIBRENMS_ID, server_key=SERVER_KEY)
    _bind(VMInterface.objects.create(virtual_machine=vm, name="ens3", mtu=1500, enabled=True), 3001)
    _seed(vm, [_port(3001, "ens3"), _port(3002, "ens4", ifAlias="vm backup", ifMtu=9000, ifPhysAddress="aabbcc000004")])
    return vm


def _variant(kind, tag):
    if kind == "device":
        return _mixed_device(tag), "device"
    if kind == "virtual_chassis":
        return _chassis(tag)[0], "device"
    return _virtual_machine(tag), "virtualmachine"


@pytest.mark.django_db
class TestMatching:
    """A filter finds rows on every page, by the text each cell shows."""

    def test_a_name_search_finds_a_port_on_another_page_with_the_filtered_total(self, client, settings):
        client = _superuser_client(client, settings, "page-two")
        device = make_device("filters-page-two", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
        _seed(device, [_port(5000 + index, f"eth{index:03d}") for index in range(120)])

        assert 5117 not in _rows(_tab(client, device, interfaces_per_page="25"))

        html = _tab(client, device, interfaces_per_page="25", interfaces_q="ETH117")
        assert list(_rows(html)) == [5117]
        # The browser keys the selection on this text, so a selection never carries to another server.
        assert re.search(
            r'<table[^>]*id="librenms-interface-table"[^>]*data-filter-signature="[^"]*server_key=default', html
        )
        assert re.search(r"Showing 1-1 of 1\s*\(filtered from 120\)", html)

        first_page = _tab(client, device, interfaces_per_page="25", interfaces_q="eth0")
        assert len(_rows(first_page)) == 25
        assert re.search(r"Showing 1-25 of 100\s*\(filtered from 120\)", first_page)
        page_links = {
            link["interfaces_page"][0]: link
            for link in (
                parse_qs(unescape(href)[1:]) for href in re.findall(r'href="(\?[^"]*)" class="page-link"', first_page)
            )
        }
        assert {page: link["interfaces_q"] for page, link in page_links.items()} == {
            page: ["eth0"] for page in ("1", "2", "3", "4")
        }

        shown = set(_rows(first_page))
        for page in ("2", "3", "4"):
            shown |= set(_rows(_tab(client, device, **{key: values[0] for key, values in page_links[page].items()})))
        assert shown == set(range(5000, 5100))

    @pytest.mark.parametrize("kind", ["device", "virtual_chassis", "virtual_machine"])
    def test_every_displayed_column_is_searched_by_the_text_its_cell_shows(self, client, settings, kind):
        client = _superuser_client(client, settings, f"columns-{kind}")
        owner, object_type = _variant(kind, f"columns-{kind}")
        unfiltered = _rows(_tab(client, owner, object_type, interfaces_show_ignored="1"))
        texts = {port_id: _cell_texts(row) for port_id, row in unfiltered.items()}
        columns = {column for cells in texts.values() for column in cells}
        checked = set()

        for column in sorted(columns):
            values = sorted({cells[column] for cells in texts.values() if len(cells[column]) > 1})
            for needle in values[:3]:
                any_column = [port_id for port_id, cells in texts.items() if any(needle in t for t in cells.values())]
                this_column = [port_id for port_id, cells in texts.items() if needle in cells[column]]

                searched = _tab(client, owner, object_type, interfaces_show_ignored="1", interfaces_q=needle.upper())
                assert list(_rows(searched)) == any_column, (column, needle)
                if column != "actions":
                    field = {f"interfaces_filter_{column}": f"  {needle}  "}
                    narrowed = _tab(client, owner, object_type, interfaces_show_ignored="1", **field)
                    assert list(_rows(narrowed)) == this_column, (column, needle)
                checked.add(column)

        expected = {"name", "mac_address", "mtu", "enabled", "description", "librenms_id", "actions"}
        expected |= {"type", "speed"} if kind != "virtual_machine" else set()
        expected |= {"device_selection"} if kind == "virtual_chassis" else set()
        expected |= {"vlans", "parent"} if kind == "device" else set()
        assert expected <= checked

    def test_a_type_search_finds_a_kept_type_without_a_state_filter(self, client, settings):
        client = _superuser_client(client, settings, "kept-type")
        InterfaceTypeMapping.objects.get_or_create(
            librenms_type="ethernetCsmacd", librenms_speed=None, defaults={"netbox_type": "1000base-t"}
        )
        device = make_device("filters-kept-type", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
        bond = _bind(Interface.objects.create(device=device, name="bond0", type="lag"), 6001)
        Interface.objects.create(device=device, name="member0", type="1000base-t", lag=bond)
        _seed(device, [_port(6001, "bond0"), _port(6002, "eth0")])

        html = _tab(client, device, interfaces_q="lag")

        assert list(_rows(html)) == [6001]
        assert _visible_text(_cell(_rows(html)[6001], "type")) == "lag"
        assert list(_rows(_tab(client, device, interfaces_filter_type="lag"))) == [6001]


@pytest.mark.django_db
class TestSyncState:
    """The sync-state filter shows the rows whose colours and buttons say that state."""

    def test_state_colour_and_button_agree_on_every_row(self, client, settings):
        client = _superuser_client(client, settings, "states")
        device = _mixed_device("states")
        shown = {}
        for state in ("matching", "mismatched", "not_in_netbox"):
            rows = _rows(_tab(client, device, interfaces_show_ignored="1", interfaces_sync_state=state))
            shown[state] = set(rows)
            for port_id, row in rows.items():
                cells = dict(CELL.findall(row))
                # A plain empty cell shows the table default with no colour, so only coloured cells count.
                fields = {
                    column: cells[column]
                    for column in ("name", "speed", "mtu", "enabled", "librenms_id")
                    if 'class="text-' in cells[column]
                }
                assert fields, port_id
                if state == "matching":
                    assert 'name="sync_one"' not in row
                    assert all("text-success" in cell for cell in fields.values()), port_id
                    # An unmapped port keeps its type red on a matching row: the sync holds no opinion on it.
                    expected_type = "text-danger" if port_id == 1004 else "text-success"
                    assert expected_type in cells["type"], port_id
                elif state == "mismatched":
                    assert 'name="sync_one"' in row
                    assert any("text-warning" in cell for cell in fields.values()), port_id
                else:
                    assert 'name="sync_one"' in row or 'name="rebind_one"' in row
                    assert all("text-danger" in cell for cell in fields.values()), port_id

        assert shown == {
            state: {port_id for port_id, value in MIXED_STATES.items() if value == state} for state in shown
        }
        # The ignored row is in no state: it shows only without a state filter.
        assert 1008 in _rows(_tab(client, device, interfaces_show_ignored="1"))

    def test_an_owner_unresolved_row_is_in_no_state(self, client, settings):
        client = _superuser_client(client, settings, "owner-states")
        first, _second = _chassis("owner-states")

        assert 2003 in _rows(_tab(client, first))
        for state in ("matching", "mismatched", "not_in_netbox"):
            assert 2003 not in _rows(_tab(client, first, interfaces_sync_state=state))


@pytest.mark.django_db
class TestWholeDeviceData:
    """The counts and lists that describe the whole device do not change under a filter."""

    def test_whole_device_counts_do_not_change_under_a_filter(self, client, settings):
        client = _superuser_client(client, settings, "counts")
        device = _mixed_device("counts")

        def whole_device(html):
            return (
                int(re.search(r'id="interfaces-show-ignored-toggle".*?(\d+) ignored', html, re.S).group(1)),
                int(re.search(r"(\d+) NetBox only interfaces", html).group(1)),
                json.loads(unescape(re.search(r'data-blocked-port-ids="([^"]*)"', html).group(1))),
            )

        unfiltered = _tab(client, device)
        filtered = _tab(client, device, interfaces_q="eth0")

        assert list(_rows(filtered)) == [1001]
        assert whole_device(filtered) == whole_device(unfiltered)
        ignored, netbox_only, blocked = whole_device(unfiltered)
        assert (ignored, "1008" in blocked) == (1, True)
        assert netbox_only >= 1


@pytest.mark.django_db
class TestInvalidFilters:
    """An invalid filter shows its errors in the tab and changes nothing else."""

    @pytest.mark.parametrize(
        ("tag", "kind", "params"),
        [
            ("state", "device", {"interfaces_sync_state": "bogus"}),
            ("long", "device", {"interfaces_q": "x" * 257}),
            ("unknown", "device", {"interfaces_filter_bogus": "eth"}),
            ("no-chassis", "device", {"interfaces_filter_device_selection": "m1"}),
            ("twice", "device", {"interfaces_q": ["eth", "po"]}),
            ("vm-type", "virtual_machine", {"interfaces_filter_type": "other"}),
            ("vm-speed", "virtual_machine", {"interfaces_filter_speed": "1"}),
        ],
    )
    def test_an_invalid_filter_shows_its_errors_in_place_of_the_table(self, client, settings, tag, kind, params):
        client = _superuser_client(client, settings, f"invalid-{tag}")
        owner, object_type = _variant(kind, f"invalid-{tag}")

        html = _tab(client, owner, object_type, **params)

        assert _rows(html) == {}
        assert 'id="interfaces_filter-errors"' in html
        assert 'data-filter-invalid="true"' in html
        field = next(iter(params))
        if field in ("interfaces_sync_state", "interfaces_q"):
            assert re.search(rf'id="id_{field}"[^>]*aria-invalid="true"', html, re.S)

    def test_full_page_get_answers_400_and_renders_every_tab(self, client, settings, librenms_server):
        device = _full_page_device(client, settings, librenms_server, "full-page-400")

        response = client.get(_sync_url(device), {"tab": "interfaces", "interfaces_sync_state": "bogus"})

        assert response.status_code == 400
        html = response.content.decode()
        assert re.search(r'id="id_interfaces_sync_state"[^>]*aria-invalid="true"', html, re.S)
        for content_id in ("interface-sync-content", "cable-sync-content", "ipaddress-sync-content"):
            assert f'id="{content_id}"' in html

    def test_htmx_tab_navigation_with_invalid_sibling_filter_renders_tabs_and_errors(
        self, client, settings, librenms_server
    ):
        device = _full_page_device(client, settings, librenms_server, "htmx-nav")

        response = client.get(_sync_url(device), {"tab": "cables", "interfaces_q": "x" * 300}, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        html = response.content.decode()
        assert re.search(r'<div id="librenms-sync-tabs" data-active-tab="cables">', html)
        assert 'id="interfaces_filter-errors"' in html
        assert 'id="cable-sync-content"' in html

    def test_sync_validates_only_its_own_tab_filter_prefix(self, client, settings, librenms_server):
        device = _full_page_device(client, settings, librenms_server, "own-prefix")

        response = client.get(
            _sync_url(device),
            {
                "tab": "interfaces",
                "cables_filter_bogus": "1",
                "ipaddresses_sync_state": "bogus",
                "interfaces_q": "eth1",
            },
        )

        # The page URL holds invalid filters of two other tabs, so the full page answers 400 and those
        # tabs show their errors. The Interfaces tab filters its own rows as usual.
        assert response.status_code == 400
        html = response.content.decode()
        assert 'id="interfaces_filter-errors"' not in html
        assert 'id="cables_filter-errors"' in html and 'id="ipaddresses_filter-errors"' in html
        assert list(_rows(html)) == [1002]

    def test_invalid_filter_does_not_refuse_the_sync_action(self, client, settings):
        client = _superuser_client(client, settings, "sync-action")
        device = _mixed_device("sync-action")
        url = reverse(
            "plugins:netbox_librenms_plugin:sync_selected_interfaces",
            kwargs={"object_type": "device", "object_id": device.pk},
        )
        query = urlencode({"interface_name_field": "ifName", "interfaces_sync_state": "bogus"})

        response = client.post(
            f"{url}?{query}",
            {"server_key": SERVER_KEY, "select": ["1003"], "exclude_columns": ["vlans"]},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert Interface.objects.filter(device=device, name="eth2").exists()
        html = response.content.decode()
        assert 'id="interfaces_filter-errors"' in html
        assert _rows(html) == {}


def _sync_url(obj):
    return reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[obj.pk])


def serve_sync_page(client, settings, librenms_server, owner):
    """Point the plugin at a loopback LibreNMS that answers the page header's lookup of *owner*, and log in."""
    bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
    librenms_server.register(
        f"/api/v0/devices/{LIBRENMS_ID}",
        {"status": "ok", "devices": [{"device_id": LIBRENMS_ID, "sysName": owner.name, "hostname": owner.name}]},
    )
    client.force_login(make_superuser("interface-filter-page"))
    return owner


def _full_page_device(client, settings, librenms_server, tag):
    """Return the mixed device, with its sync page served."""
    return serve_sync_page(client, settings, librenms_server, _mixed_device(tag))


# Measured queries per row whose rule type differs from its NetBox type, under a filter: 0 (100 and
# 1024 rows each cost 44 queries). It was 1 (143 and 1067) before the LAG members were prefetched.
QUERIES_PER_TYPE_CHANGE = 0

# Before NetBox caches custom fields per request, its own Interface validation looks them up on every row.
NETBOX_UNCACHED_LOOKUPS = () if hasattr(customfields, "query_cache") else ('"extras_customfield"', '"core_objecttype"')


@pytest.mark.django_db
def test_interface_filter_query_budget_with_refused_type_changes(client, settings, librenms_server):
    """
    A filter computes every row's state, so every planned type change is checked on every row.

    Every row's rule type differs from its NetBox type: one row in ten is a LAG aggregate whose
    change NetBox refuses, the rest take the change. The search matches no row, so the cost is the
    filter's alone. The page renders with the VLAN tab active, so the search runs on a tab the user
    does not see. The assertion fixes the growth per row at QUERIES_PER_TYPE_CHANGE.
    """
    bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
    InterfaceTypeMapping.objects.get_or_create(
        librenms_type="ethernetCsmacd", librenms_speed=None, defaults={"netbox_type": "1000base-t"}
    )
    client.force_login(make_superuser("interface-filter-budget"))

    def page_queries(size):
        device = make_device(f"filters-budget-{size}", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID + size}})
        librenms_server.register(
            f"/api/v0/devices/{LIBRENMS_ID + size}",
            {"status": "ok", "devices": [{"device_id": LIBRENMS_ID + size, "sysName": device.name}]},
        )
        interfaces = []
        for index in range(size):
            interface = Interface(
                device=device,
                name=f"eth{index:04d}",
                type="lag" if index % 10 == 0 else "virtual",
                speed=1_000_000,
                mtu=1500,
            )
            seed_mapping(interface, SERVER_KEY, own=7000 + index, save=False)
            interfaces.append(interface)
        Interface.objects.bulk_create(interfaces)
        Interface.objects.bulk_create(
            Interface(device=device, name=f"member{index}", type="1000base-t", lag=aggregate)
            for index, aggregate in enumerate(Interface.objects.filter(device=device, type="lag"))
        )
        _seed(device, [_port(7000 + index, f"eth{index:04d}") for index in range(size)])
        with CaptureQueriesContext(connection) as queries:
            response = client.get(_sync_url(device), {"tab": "vlans", "interfaces_q": "no such port"})
        assert response.status_code == 200
        assert 'data-filter-active="true"' in response.content.decode()
        # NetBox reloads its config revision when its process cache expires, whatever the page holds.
        ignored = ("core_configrevision", *NETBOX_UNCACHED_LOOKUPS)
        return len([query for query in queries.captured_queries if not any(name in query["sql"] for name in ignored)])

    # The first page load of a test process fills process-wide caches, so it is not measured.
    page_queries(10)
    small, large = page_queries(100), page_queries(1024)

    assert large - small == QUERIES_PER_TYPE_CHANGE * (1024 - 100), (small, large)
