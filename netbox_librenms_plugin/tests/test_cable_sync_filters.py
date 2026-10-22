"""
Server-side filters on the Cables sync tab: matching, the cable comparison, invalid filters and redirects.

Each test seeds the cable snapshot that a refresh stores, and then renders the real tab through a
real request: the fragment that the sync page loads, the full sync page, a sync POST, or the remote
picker. NetBox devices, interfaces, console ports, patch panels and cables are real rows.
"""

import ast
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from dcim.models import Cable, ConsolePort, ConsoleServerPort, Device, FrontPort, Interface, RearPort
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.models import InterfaceTypeMapping, LibreNMSSettings
from netbox_librenms_plugin.server_selection import get_librenms_sync_device
from netbox_librenms_plugin.constants import MATCHING, MISMATCHED, NOT_IN_NETBOX
from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    librenms_cable_tag,
    make_device,
    make_interface,
    make_patch_panel,
    make_serial_row,
    make_virtual_chassis_members,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping
from netbox_librenms_plugin.tests.test_interface_sync_filters import (
    CELL,
    LIBRENMS_ID,
    SERVER_KEY,
    _superuser_client,
    _visible_text,
    serve_sync_page,
)
from netbox_librenms_plugin.tests.test_interface_rule_writes import _port as _rule_port
from netbox_librenms_plugin.tests.view_test_helpers import grant
from netbox_librenms_plugin.views.base.cables_view import port_record
from netbox_librenms_plugin.views.sync.cables import SyncCablesView

ROW = re.compile(r'<tr[^>]*data-interface="([^"]+)"[^>]*>.*?</tr>', re.S)
TABLE = re.compile(r'<table[^>]*id="librenms-cable-table(?:-vc)?".*?</table>', re.S)
# The data-col of each column with its own filter field, and the field name.
FIELD_COLUMNS = {
    "device_selection": "vc_member",
    "local_port": "local_port",
    "remote_port": "remote_port",
    "remote_device": "remote_device",
}


def _seed(owner, links):
    """Store *links* as the cable snapshot of *owner*, under the device that a refresh caches it for."""
    cache_owner = get_librenms_sync_device(owner, server_key=SERVER_KEY) or owner
    cache_key = object.__new__(SyncCablesView).get_cache_key(cache_owner, "links", SERVER_KEY)
    payload = {"links": links, "snapshot_token": f"filters-{cache_owner.pk}", "incomplete_sources": []}
    cache.set(cache_key, payload, timeout=300)


def _link(port_id, local, remote_device, remote_port):
    return {
        "_source": "main",
        "local_port_id": port_id,
        "local_port": local,
        "remote_device": remote_device,
        "remote_port": remote_port,
        "remote_port_id": 50000 + port_id,
    }


def _fragment(client, device, **params):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": "cables"},
    )
    query = {"server_key": SERVER_KEY, "cables_per_page": "50", **params}
    return client.get(f"{url}?{urlencode(query, doseq=True)}")


def _tab(client, device, **params):
    response = _fragment(client, device, **params)
    assert response.status_code == 200
    return response.content.decode()


def _rows(html):
    """Return each row's markup of the rendered cable table by row ID, in table order."""
    table = TABLE.search(html)
    return {unescape(match.group(1)): match.group(0) for match in ROW.finditer(table.group(0) if table else "")}


def _cell_texts(row):
    return {column: _visible_text(cell) for column, cell in CELL.findall(row) if column != "selection"}


def _table_attr(html, name):
    (table,) = re.findall(r'<table[^>]*id="librenms-cable-table(?:-vc)?"[^>]*>', html)
    return unescape(re.search(rf'{name}="([^"]*)"', table).group(1))


def _device(tag):
    return make_device(f"cables-{tag}", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})


def _mixed_device(tag):
    """
    Return a device whose rows cover each cable comparison of the normal, patch and console paths.

    Returns:
        tuple[Device, dict[str, str | None]]: The device, and the comparison of each row by row ID.

    """
    device = _device(tag)
    peer = make_device(f"cables-{tag}-peer")
    console = make_device(f"cables-{tag}-console")
    local = {n: make_interface(device, f"Gi0/{n}") for n in range(1, 8)}
    far = {n: make_interface(peer, f"Ethernet{n}") for n in (1, 2, 3, 4, 5, 8, 9)}
    cable_together(local[2], far[2]).tags.add(librenms_cable_tag())
    cable_together(local[3], far[3])
    _panel, front, rear = make_patch_panel(f"cables-{tag}-panel")
    cable_together(local[4], front)
    cable_together(rear, far[4])
    cable_together(local[5], far[9])
    links = [
        _link(1001, "Gi0/1", peer.name, "Ethernet1"),
        _link(1002, "Gi0/2", peer.name, "Ethernet2"),
        _link(1003, "Gi0/3", peer.name, "Ethernet3"),
        _link(1004, "Gi0/4", peer.name, "Ethernet4"),
        _link(1005, "Gi0/5", peer.name, "Ethernet5"),
        _link(1006, "Gi0/6", "no-such-device", "xe-0/0/6"),
        _link(1007, "Gi0/7", peer.name, "Ethernet7"),
    ]
    csps = [ConsoleServerPort.objects.create(device=device, name=f"ttyS{n}") for n in range(1, 5)]
    con = {n: ConsolePort.objects.create(device=console, name=f"con{n}") for n in range(1, 4)}
    cable_together(csps[1], con[2])
    _serial_panel, serial_front, serial_rear = make_patch_panel(f"cables-{tag}-serial-panel")
    cable_together(csps[2], serial_front)
    cable_together(serial_rear, con[3])
    links += [
        make_serial_row(csps[0], console.name, device, sensor_index_int=1),
        make_serial_row(csps[1], console.name, device, sensor_index_int=2),
        make_serial_row(csps[2], console.name, device, sensor_index_int=3),
        make_serial_row(csps[3], console.name, device, sensor_index_int=4, is_configured=False),
    ]
    _seed(device, links)
    serial = [f"serial:{csp.pk}" for csp in csps]
    return device, {
        # Both ends are in NetBox and neither has a cable.
        "1001": NOT_IN_NETBOX,
        # The tagged direct cable is the desired connection.
        "1002": MATCHING,
        # The direct cable has no LibreNMS tag, so the sync still has to adopt it.
        "1003": MISMATCHED,
        # The patch path reaches the desired far end.
        "1004": MATCHING,
        # The cable reaches another interface.
        "1005": MISMATCHED,
        # The remote device or the remote interface is not resolved.
        "1006": None,
        "1007": None,
        # The label device has a free console port and the server port has no cable.
        serial[0]: NOT_IN_NETBOX,
        serial[1]: MISMATCHED,
        serial[2]: MATCHING,
        # An unconfigured label names no remote device to compare with.
        serial[3]: None,
    }


def _records(response):
    return {str(row.record["row_id"]): row.record for row in response.context["cable_sync"]["table"].rows}


def _breakout_device(tag):
    """
    Return a channelized device whose channel rows the breakout gate decides, and each row's comparison.

    Channel 1 has the plugin's profiled cable, channel 2 can extend it, and channel 3 must wait for channel 2.
    """
    device = _device(tag)
    parent = Interface.objects.create(device=device, name="et-0/0/1", type="100gbase-x-qsfp28", channels=4)
    channels = [
        Interface.objects.create(
            device=device, name=f"et-0/0/1:{lane}", type="25gbase-x-sfp28", parent=parent, channel_id=lane + 1
        )
        for lane in range(4)
    ]
    peer = make_device(f"cables-{tag}-peer")
    far = [make_interface(peer, f"Ethernet{lane + 1}") for lane in range(4)]
    cable = Cable(a_terminations=[parent], b_terminations=[far[0]], profile="breakout-1c4p-4c1p")
    cable.save()
    cable.tags.add(librenms_cable_tag())
    _seed(device, [_link(100 + n, channels[n - 1].name, peer.name, f"Ethernet{n}") for n in (1, 2, 3)])
    return device, {"101": MATCHING, "102": NOT_IN_NETBOX, "103": None}


@pytest.mark.django_db
class TestMatching:
    """A filter finds rows on every page, by the text each cell shows."""

    def test_a_port_search_finds_a_row_on_another_page_with_the_filtered_total(self, client, settings):
        client = _superuser_client(client, settings, "cable-page-two")
        device = _device("page-two")
        _seed(device, [_link(3000 + n, f"eth{n:02d}", "peer-not-in-netbox", f"xe-{n}") for n in range(1, 61)])

        assert "3057" not in _rows(_tab(client, device, cables_per_page="25"))

        html = _tab(client, device, cables_per_page="25", cables_q="ETH57")
        assert list(_rows(html)) == ["3057"]
        assert re.search(r"Showing 1-1 of 1\s*\(filtered from 60\)", html)

        first_page = _tab(client, device, cables_per_page="25", cables_filter_local_port="eth")
        assert len(_rows(first_page)) == 25
        assert re.search(r"Showing 1-25 of 60\s*\(filtered from 60\)", first_page)
        links = [parse_qs(unescape(href)[1:]) for href in re.findall(r'href="(\?[^"]*)" class="page-link"', first_page)]
        page_two = next(link for link in links if link["cables_page"] == ["2"])
        assert page_two["cables_filter_local_port"] == ["eth"]
        assert list(_rows(_tab(client, device, **{key: values[0] for key, values in page_two.items()})))[0] == "3026"

    @pytest.mark.parametrize("kind", ["device", "virtual_chassis"])
    def test_every_displayed_column_is_searched_by_the_text_its_cell_shows(self, client, settings, kind):
        client = _superuser_client(client, settings, f"cable-columns-{kind}")
        device = _mixed_device(f"columns-{kind}")[0] if kind == "device" else _chassis(f"columns-{kind}")
        texts = {row_id: _cell_texts(row) for row_id, row in _rows(_tab(client, device)).items()}
        columns = {column for cells in texts.values() for column in cells}
        checked = set()

        for column in sorted(columns):
            values = sorted({cells[column] for cells in texts.values() if len(cells[column]) > 1})
            for needle in values[:4]:
                any_column = [row_id for row_id, cells in texts.items() if any(needle in t for t in cells.values())]
                this_column = [row_id for row_id, cells in texts.items() if needle in cells[column]]

                assert list(_rows(_tab(client, device, cables_q=needle.upper()))) == any_column, (column, needle)
                if column in FIELD_COLUMNS:
                    params = {f"cables_filter_{FIELD_COLUMNS[column]}": f"  {needle}  "}
                    assert list(_rows(_tab(client, device, **params))) == this_column, (column, needle)
                checked.add(column)

        expected = {"local_port", "remote_port", "remote_device", "cable_status", "actions"}
        assert checked == (expected | {"device_selection"} if kind == "virtual_chassis" else expected)


def _chassis(tag):
    """Return a VC member page whose rows belong to both members: the member select shows each row's owner."""
    _vc, (first, second) = make_virtual_chassis_members(f"cables-{tag}", 2)
    seed_stored_mapping(first, {SERVER_KEY: {"id": LIBRENMS_ID}}, save=True)
    peer = make_device(f"cables-{tag}-peer")
    for member, name in ((first, "Ethernet1/1"), (second, "Ethernet2/1"), (second, "Ethernet2/2")):
        make_interface(member, name)
    for name in ("xe-1", "xe-2"):
        make_interface(peer, name)
    _seed(
        first,
        [
            _link(4001, "Ethernet1/1", peer.name, "xe-1"),
            _link(4002, "Ethernet2/1", peer.name, "xe-2"),
            _link(4003, "Ethernet2/2", "no-such-device", "xe-3"),
        ],
    )
    return first


@pytest.mark.django_db
class TestCableComparison:
    """The comparison of each row decides the sync-state filter, on every enrichment path."""

    @pytest.mark.parametrize(
        "path",
        [
            "normal-patch-console",
            pytest.param(
                "breakout",
                marks=pytest.mark.skipif(
                    not hasattr(Interface, "channel_id"), reason="NetBox before 4.7 has no channel interfaces"
                ),
            ),
        ],
    )
    def test_cable_comparison_covers_normal_console_breakout_and_patch_paths(self, client, settings, path):
        client = _superuser_client(client, settings, f"cable-comparison-{path}")
        device, expected = _mixed_device("comparison") if path != "breakout" else _breakout_device("comparison")

        response = _fragment(client, device)

        assert response.status_code == 200
        records = _records(response)
        assert {row_id: record.get("cable_comparison") for row_id, record in records.items()} == {
            row_id: state or "unclassified" for row_id, state in expected.items()
        }
        for state in (MATCHING, MISMATCHED, NOT_IN_NETBOX):
            shown = set(_rows(_tab(client, device, cables_sync_state=state)))
            assert shown == {row_id for row_id, value in expected.items() if value == state}, state

    def test_status_and_sync_action_agree_with_the_state_filter(self, client, settings):
        client = _superuser_client(client, settings, "cable-state-agree")
        device, _expected = _mixed_device("state-agree")

        for state in (MATCHING, MISMATCHED, NOT_IN_NETBOX):
            rows = _rows(_tab(client, device, cables_sync_state=state))
            assert rows, state
            for row_id, row in rows.items():
                cells = dict(CELL.findall(row))
                status, action = _visible_text(cells["cable_status"]), _visible_text(cells["actions"])
                if state == MATCHING:
                    assert status in ("cable found", "connected via patch path") and "sync cable" not in action, row_id
                elif state == MISMATCHED:
                    assert status in ("cable found", "cable mismatch") and "sync cable" in action, row_id
                else:
                    assert status == "no cable" and "sync cable" in action, row_id

    def test_a_hidden_cable_is_in_no_state(self, client, settings):
        configure = _superuser_client(client, settings, "cable-hidden-admin")
        device, _expected = _mixed_device("hidden")
        configure.logout()
        user = grant(_plain_user("cable-hidden"), "view", LibreNMSSettings)
        for model in (Device, Interface, ConsoleServerPort, ConsolePort, FrontPort, RearPort):
            user = grant(user, "view", model)
        client.force_login(user)

        records = _records(_fragment(client, device))

        # Without view on Cable, every row with a cable shows no cable state.
        hidden = {row_id for row_id, record in records.items() if record["cable_status"] == "Cable State Not Available"}
        assert {"1002", "1003", "1004", "1005"} <= hidden
        assert {records[row_id]["cable_comparison"] for row_id in hidden} == {"unclassified"}
        assert records["1001"]["cable_comparison"] == NOT_IN_NETBOX

    def test_a_tagged_serial_cable_whose_path_ends_at_a_panel_is_in_no_state(self, client, settings):
        client = _superuser_client(client, settings, "cable-open-path")
        device = _device("open-path")
        console = make_device("cables-open-path-console")
        ConsolePort.objects.create(device=console, name="con1")
        csp = ConsoleServerPort.objects.create(device=device, name="ttyS1")
        # The tagged cable ends at a patch panel whose rear port has no cable, so no console is reached.
        _panel, front, _rear = make_patch_panel("cables-open-path-panel")
        cable_together(csp, front).tags.add(librenms_cable_tag())
        _seed(device, [make_serial_row(csp, console.name, device)])

        record = _records(_fragment(client, device))[f"serial:{csp.pk}"]

        assert record["cable_comparison"] == "unclassified"
        # The tag still keeps the cable: the row offers no sync that re-points it.
        assert (record["cable_status"], record["can_create_cable"]) == ("Cable Found", False)
        assert record.get("netbox_remote_interface_id") is None
        assert _rows(_tab(client, device, cables_sync_state=MATCHING)) == {}

    def test_a_row_the_rules_refuse_is_in_no_state(self, client, settings):
        client = _superuser_client(client, settings, "cable-rule-refused")
        device = _device("rule-refused")
        peer = make_device("cables-rule-refused-peer")
        links = []
        for n in (1, 2):
            make_interface(device, f"Gi0/{n}")
            make_interface(peer, f"Ethernet{n}")
            record = port_record(_rule_port(8000 + n, f"Gi0/{n}"))
            links.append({**_link(8000 + n, f"Gi0/{n}", peer.name, f"Ethernet{n}"), "local_port_record": record})
        _seed(device, links)
        InterfaceTypeMapping.objects.create(action=InterfaceTypeMapping.ACTION_IGNORE, name_pattern="^Gi0/1$")

        records = _records(_fragment(client, device))

        assert records["8001"]["rule_block"] and records["8001"]["cable_comparison"] == "unclassified"
        assert records["8002"]["cable_comparison"] == NOT_IN_NETBOX
        assert set(_rows(_tab(client, device, cables_sync_state=NOT_IN_NETBOX))) == {"8002"}

    def test_every_link_status_write_sets_the_comparison(self):
        """A status write outside the one helper could leave a row with the comparison of an older status."""
        source = Path(__file__).resolve().parents[1] / "views" / "base" / "cables_view.py"
        tree = ast.parse(source.read_text())
        writers = []
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            for node in ast.walk(function):
                targets = node.targets if isinstance(node, ast.Assign) else []
                keys = [
                    target.value.id
                    for target in targets
                    if isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "cable_status"
                    and isinstance(target.value, ast.Name)
                ]
                if isinstance(node, ast.Dict) and any(
                    isinstance(key, ast.Constant) and key.value == "cable_status" for key in node.keys
                ):
                    keys.append("<dict>")
                writers.extend((function.name, key) for key in keys)

        # The verify answer builds its own output row; every enriched row goes through the helper.
        assert {(name, key) for name, key in writers if key != "formatted_row"} - {("post", "<dict>")} == {
            ("_set_cable_state", "link")
        }


def _plain_user(username):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(username=username, password="x")


@pytest.mark.django_db
class TestWholeSnapshotData:
    """Row identities and the free-port claims describe the whole snapshot, also under a filter."""

    def test_row_ids_stay_the_ids_of_the_whole_snapshot_after_filtering(self, client, settings):
        client = _superuser_client(client, settings, "cable-row-ids")
        device = _device("row-ids")
        # Two neighbours on one port: the row IDs come from the whole snapshot, not from the shown rows.
        links = [
            {**_link(6001, "Gi0/1", "peer-a", "xe-1"), "protocol": "lldp"},
            {**_link(6001, "Gi0/1", "peer-b", "xe-2"), "protocol": "lldp"},
            _link(6002, "Gi0/2", "peer-a", "xe-3"),
        ]
        _seed(device, links)
        unfiltered = _rows(_tab(client, device))
        (peer_b,) = [row_id for row_id, row in unfiltered.items() if "peer-b" in row]

        filtered = _tab(client, device, cables_filter_remote_device="peer-b")

        assert list(_rows(filtered)) == [peer_b]
        assert f'name="select" value="{peer_b}"' in filtered
        assert _table_attr(filtered, "data-filter-active") == "true"
        # The selection belongs to the tab, so a filter keeps the rows already selected.
        assert _table_attr(filtered, "data-selection-scope") == "interface_name_field=ifName&server_key=default"
        assert _table_attr(filtered, "data-selection-scope") == _table_attr(
            _tab(client, device), "data-selection-scope"
        )

    def test_the_selection_signature_changes_with_the_interface_name_field(self, client, settings):
        client = _superuser_client(client, settings, "cable-signature")
        device = _device("signature")
        _seed(device, [_link(6101, "uplink", "peer-a", "xe-1")])

        signatures = {
            field: _table_attr(
                _tab(client, device, cables_filter_local_port="uplink", interface_name_field=field),
                "data-selection-scope",
            )
            for field in ("ifName", "ifDescr")
        }

        # The local port names follow the name field, so one filter can hold other rows under each field.
        assert signatures["ifName"] != signatures["ifDescr"]
        assert all(f"interface_name_field={field}" in signature for field, signature in signatures.items())

    def test_whole_device_data_does_not_change_under_a_filter(self, client, settings):
        client = _superuser_client(client, settings, "cable-whole-device")
        device = _device("whole-device")
        console = make_device("cables-whole-device-console")
        csps = [ConsoleServerPort.objects.create(device=device, name=f"ttyS{n}") for n in (1, 2)]
        for name in ("con1", "con2"):
            ConsolePort.objects.create(device=console, name=name)
        links = [make_serial_row(csp, console.name, device, sensor_index_int=n) for n, csp in enumerate(csps, 1)]
        _seed(device, links)
        cache_key = object.__new__(SyncCablesView).get_cache_key(device, "links", SERVER_KEY)
        cache.set(cache_key, {**cache.get(cache_key), "incomplete_sources": ["OOB"]}, timeout=300)

        def remote_ports(html):
            return {row_id: _cell_texts(row)["remote_port"] for row_id, row in _rows(html).items()}

        unfiltered = _tab(client, device)
        filtered = _tab(client, device, cables_filter_local_port="ttyS2")

        # The second server port still targets the second free console port: the first row claims con1.
        assert remote_ports(filtered) == {f"serial:{csps[1].pk}": "con2"}
        assert remote_ports(unfiltered)[f"serial:{csps[1].pk}"] == "con2"
        # The incomplete-source warning describes the snapshot, so the filter keeps it.
        for html in (unfiltered, filtered):
            assert "These LibreNMS sources failed: OOB" in html


def _sync_page_url(device):
    return reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])


def _sync_url(device):
    return reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[device.pk])


def _sync_data(response, row_id):
    """Return the endpoint-bound fields that the rendered cable row submits."""
    record = _records(response)[row_id]
    return {
        "select": row_id,
        "server_key": SERVER_KEY,
        "origin_device_id": record["device_id"],
        f"expected_local_id_{row_id}": record["netbox_local_interface_id"],
        f"expected_local_device_id_{row_id}": record["netbox_local_device_id"],
        f"expected_remote_id_{row_id}": record["netbox_remote_interface_id"],
        f"expected_remote_device_id_{row_id}": record["netbox_remote_device_id"],
    }


@pytest.mark.django_db
class TestInvalidFilters:
    """An invalid filter shows its errors in the tab and changes nothing else."""

    @pytest.mark.parametrize(
        ("tag", "params"),
        [
            ("state", {"cables_sync_state": "bogus"}),
            ("long", {"cables_q": "x" * 257}),
            ("unknown", {"cables_filter_bogus": "eth"}),
            ("member-on-a-device", {"cables_filter_vc_member": "m1"}),
            ("status-field", {"cables_filter_cable_status": "No Cable"}),
            ("twice", {"cables_q": ["eth", "Gi"]}),
        ],
    )
    def test_an_invalid_filter_shows_its_errors_in_place_of_the_table(self, client, settings, tag, params):
        client = _superuser_client(client, settings, f"cable-invalid-{tag}")
        device, _expected = _mixed_device(f"invalid-{tag}")

        html = _tab(client, device, **params)

        assert _rows(html) == {}
        assert 'id="cables_filter-errors"' in html
        assert 'data-filter-invalid="true"' in html
        field = next(iter(params))
        if field in ("cables_sync_state", "cables_q"):
            assert re.search(rf'id="id_{field}"[^>]*aria-invalid="true"', html, re.S)

    def test_full_page_get_answers_400_and_shows_the_errors_on_that_tab_only(self, client, settings, librenms_server):
        device, _expected = _mixed_device("full-page")
        serve_sync_page(client, settings, librenms_server, device)

        response = client.get(_sync_page_url(device), {"tab": "cables", "cables_sync_state": "bogus"})

        assert response.status_code == 400
        html = response.content.decode()
        assert re.search(r'id="id_cables_sync_state"[^>]*aria-invalid="true"', html, re.S)
        assert 'id="interfaces_filter-errors"' not in html
        for content_id in ("interface-sync-content", "cable-sync-content", "ipaddress-sync-content"):
            assert f'id="{content_id}"' in html

        # An htmx tab swap with the same filter keeps its status and shows the same errors.
        swapped = client.get(
            _sync_page_url(device), {"tab": "cables", "cables_sync_state": "bogus"}, HTTP_HX_REQUEST="true"
        )
        assert swapped.status_code == 200
        assert 'id="cables_filter-errors"' in swapped.content.decode()

    def test_sync_validates_only_its_own_tab_filter_prefix(self, client, settings):
        client = _superuser_client(client, settings, "cable-own-prefix")
        device, _expected = _mixed_device("own-prefix")

        html = _tab(
            client,
            device,
            interfaces_sync_state="bogus",
            vlans_filter_bogus="1",
            ipaddresses_q="x" * 300,
            cables_filter_local_port="Gi0/6",
        )

        assert 'id="cables_filter-errors"' not in html
        assert list(_rows(html)) == ["1006"]

    def test_a_sync_with_an_invalid_filter_of_another_tab_writes_and_shows_its_own_table(self, client, settings):
        client = _superuser_client(client, settings, "cable-post-prefix")
        device, _expected = _mixed_device("post-prefix")
        data = _sync_data(_fragment(client, device), "1001")
        query = urlencode(
            {
                "tab": "cables",
                "server_key": SERVER_KEY,
                "ipaddresses_sync_state": "bogus",
                "interfaces_q": "x" * 300,
                "cables_filter_local_port": "Gi0/1",
            }
        )

        response = client.post(f"{_sync_url(device)}?{query}", data, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        assert Interface.objects.get(device=device, name="Gi0/1").cable is not None
        html = response.content.decode()
        assert 'id="cables_filter-errors"' not in html
        assert list(_rows(html)) == ["1001"]

    def test_invalid_filter_does_not_refuse_the_sync_action(self, client, settings):
        client = _superuser_client(client, settings, "cable-sync-action")
        device, _expected = _mixed_device("sync-action")
        data = _sync_data(_fragment(client, device), "1001")
        query = urlencode({"tab": "cables", "server_key": SERVER_KEY, "cables_sync_state": "bogus"})

        response = client.post(f"{_sync_url(device)}?{query}", data, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        assert Interface.objects.get(device=device, name="Gi0/1").cable is not None
        html = response.content.decode()
        assert 'id="cables_filter-errors"' in html
        assert _rows(html) == {}


# The page query of the tests below: the cable tab's filter, page and size, and another tab's filter.
PAGE_QUERY = {
    "tab": "cables",
    "server_key": SERVER_KEY,
    "interfaces_q": "eth  a&b+c é",
    "cables_filter_remote_device": "peer",
    "cables_page": "2",
    "cables_per_page": "2",
}


def _assert_keeps_the_page_query(url, path, page_query=PAGE_QUERY):
    assert urlsplit(url).path == path, url
    query = parse_qs(urlsplit(url).query)
    assert {key: query.get(key) for key in page_query} == {key: [value] for key, value in page_query.items()}, url


def _picker_device(tag):
    """Return a device with ten rows to one peer and two to another device, and a free peer interface to pick."""
    device = _device(tag)
    peer = make_device(f"cables-{tag}-peer")
    for n in range(1, 13):
        make_interface(device, f"Gi0/{n}")
        make_interface(peer, f"Ethernet{n}")
    free = make_interface(peer, "Ethernet99")
    links = [_link(7000 + n, f"Gi0/{n}", peer.name, f"Ethernet{n}") for n in range(1, 11)]
    links += [_link(7000 + n, f"Gi0/{n}", "remote-x", f"Ethernet{n}") for n in (11, 12)]
    _seed(device, links)
    return device, free


def _assert_shows_the_page_of(html, client, device, page_query, rows_not_shown):
    """Assert that *html* shows the rows and the count of the tab for *page_query*, as a cache restore does."""
    expected = _tab(client, device, **page_query)
    assert list(_rows(html)) == list(_rows(expected))
    count = re.compile(r"Showing \d+-\d+ of 10\s*\(filtered from 12\)")
    assert count.search(html).group(0) == count.search(expected).group(0)
    assert not set(rows_not_shown) & set(_rows(html))


def _picker_post(client, device, row_id, page_query):
    """Open the picker as the page does (its URL plus the page query) and return the URL its form posts to."""
    picker = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[device.pk])
    response = client.get(picker, {**page_query, "row_id": row_id})
    assert response.status_code == 200
    (post_url,) = re.findall(r'<form hx-post="([^"]*)"', response.content.decode())
    return unescape(post_url)


@pytest.mark.django_db
class TestFollowUpsKeepThePageQuery:
    """Every answer that re-renders the cable tab, or sends the browser back to it, keeps the filters and page."""

    def test_a_sync_rerender_keeps_the_filter_and_the_clamped_page(self, client, settings):
        client = _superuser_client(client, settings, "cable-sync-rerender")
        device, _free = _picker_device("sync-rerender")
        page = _fragment(client, device, **{**PAGE_QUERY, "cables_page": "9"})
        (action,) = re.findall(r'<form method="post"[^>]*action="([^"]*)"', page.content.decode())
        _assert_keeps_the_page_query(unescape(action), _sync_url(device), {**PAGE_QUERY, "cables_page": "9"})

        response = client.post(unescape(action), _sync_data(page, "7010"), HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        assert Interface.objects.get(device=device, name="Gi0/10").cable is not None
        # Page 9 is past the end, so the answer shows the last page of the filtered rows.
        html = response.content.decode()
        _assert_shows_the_page_of(html, client, device, {**PAGE_QUERY, "cables_page": "9"}, ["7001", "7011"])
        assert "7010" in _rows(html)

    def test_a_plain_sync_redirect_keeps_the_page_query(self, client, settings):
        client = _superuser_client(client, settings, "cable-sync-redirect")
        device, _free = _picker_device("sync-redirect")
        page = _fragment(client, device)

        response = client.post(f"{_sync_url(device)}?{urlencode(PAGE_QUERY)}", _sync_data(page, "7001"))

        assert response.status_code == 302
        _assert_keeps_the_page_query(response["Location"], _sync_page_url(device))

    def test_the_picker_posts_with_the_page_query_and_its_answer_keeps_the_filtered_page(self, client, settings):
        client = _superuser_client(client, settings, "cable-picker")
        device, free = _picker_device("picker")
        post_url = _picker_post(client, device, "7003", PAGE_QUERY)
        picker_path = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[device.pk])
        _assert_keeps_the_page_query(post_url, picker_path)
        assert "row_id" not in parse_qs(urlsplit(post_url).query)

        response = client.post(
            post_url,
            {"row_id": "7003", "server_key": SERVER_KEY, "remote_interface_id": free.pk},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        html = response.content.decode()
        _assert_shows_the_page_of(html, client, device, PAGE_QUERY, ["7001", "7011"])
        assert "7003" in _rows(html)
        assert "closeHtmxModal()" in html

    def test_invalid_cable_filter_from_picker_is_visible(self, client, settings):
        client = _superuser_client(client, settings, "cable-picker-invalid")
        device, free = _picker_device("picker-invalid")
        page_query = {**PAGE_QUERY, "cables_sync_state": "bogus"}
        post_url = _picker_post(client, device, "7003", page_query)

        response = client.post(
            post_url,
            {"row_id": "7003", "server_key": SERVER_KEY, "remote_interface_id": free.pk},
            HTTP_HX_REQUEST="true",
        )

        # The pick is stored and the modal closes as usual; the tab shows the filter errors.
        assert response.status_code == 200
        html = response.content.decode()
        assert "closeHtmxModal()" in html
        assert 'id="cables_filter-errors"' in html
        assert re.search(r'id="id_cables_sync_state"[^>]*aria-invalid="true"', html, re.S)
        assert _records(_fragment(client, device))["7003"]["netbox_remote_interface_id"] == free.pk


@pytest.mark.django_db
def test_the_remote_create_modal_posts_with_the_page_query(client, librenms_server, settings):
    from netbox_librenms_plugin.tests.conftest import make_superuser
    from netbox_librenms_plugin.tests.test_cable_remote_matching import TestCheckAndCreateTheRemoteEnd

    server_key, device, local, remote, row_id = TestCheckAndCreateTheRemoteEnd()._scenario(
        "filters-create", librenms_server, settings
    )
    client.force_login(make_superuser("cable-filter-remote-create"))
    page_query = {"tab": "cables", "server_key": server_key, "interfaces_q": "a&b  é", "cables_q": "eth0"}
    create_path = reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[device.pk])
    # The page adds the page query to the row's create URL.
    modal = client.get(create_path, {**page_query, "row_id": row_id, "expected_local_id": local.pk})
    assert modal.status_code == 200
    (post_url,) = re.findall(r'<form hx-post="([^"]*)"', modal.content.decode())
    post_url = unescape(post_url)
    _assert_keeps_the_page_query(post_url, create_path, page_query)
    assert not {"row_id", "expected_local_id"} & set(parse_qs(urlsplit(post_url).query))

    response = client.post(
        post_url, {"expected_local_id": local.pk, "row_id": row_id, "server_key": server_key}, HTTP_HX_REQUEST="true"
    )

    assert response.status_code == 200
    html = response.content.decode()
    assert "closeHtmxModal()" in html
    assert Interface.objects.filter(device=remote).count() == 1
    assert _table_attr(html, "data-filter-active") == "true"
    assert re.search(r'name="cables_q" id="id_cables_q" value="eth0"', html)
