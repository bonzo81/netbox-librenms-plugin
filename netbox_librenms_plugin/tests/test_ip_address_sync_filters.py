"""
Server-side filters on the IP Addresses sync tab: matching, sync states, invalid filters and redirects.

Each test refreshes the real tab from a loopback LibreNMS and then renders it through a real
request: the fragment that the sync page loads, the full sync page, or a sync POST. NetBox
interfaces, IP addresses and VRFs are real rows.
"""

import json
import re
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from dcim.models import Interface
from django.urls import reverse
from ipam.models import VRF, IPAddress
from virtualization.models import VMInterface

from netbox_librenms_plugin.tests.conftest import (
    bind_librenms_server,
    make_device,
    make_superuser,
    make_vm,
    map_device_to_librenms,
)
from netbox_librenms_plugin.tests.test_interface_sync_filters import CELL, _visible_text
from netbox_librenms_plugin.tests.test_ip_address_sync_safety import _serve_librenms_ip_rows

SERVER_KEY = "default"
# The loopback helper serves the LibreNMS device as ID 42.
LIBRENMS_ID = 42

ROW = re.compile(r'<tr[^>]*data-interface="([^"]+)"[^>]*>.*?</tr>', re.S)
FIELD_COLUMNS = ("address", "prefix", "device", "interface")


def _row(address, port_id, interface, prefix_length=24):
    return {"address": address, "prefix_length": prefix_length, "port_id": port_id, "interface": interface}


def _client(client, tag):
    client.force_login(make_superuser(f"ip-filter-{tag}"))
    return client


def _refresh(client, owner, server, rows, *, management_ip="198.18.0.254"):
    """Serve *rows* from the loopback LibreNMS and refresh the tab, so the snapshot is cached as a user does it."""
    _serve_librenms_ip_rows(server, rows, device_name=owner.name, management_ip=management_ip)
    server.register(
        f"/api/v0/devices/{LIBRENMS_ID}",
        {
            "status": "ok",
            "devices": [{"device_id": LIBRENMS_ID, "ip": management_ip, "hostname": owner.name, "sysName": owner.name}],
        },
    )
    name = "device_ipaddress_sync" if owner._meta.model_name == "device" else "vm_ipaddress_sync"
    response = client.post(
        reverse(f"plugins:netbox_librenms_plugin:{name}", args=[owner.pk]),
        {"server_key": SERVER_KEY, "interface_name_field": "ifName"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    return owner


def _fragment(client, owner, **params):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": owner._meta.model_name, "pk": owner.pk, "tab": "ipaddresses"},
    )
    query = {"server_key": SERVER_KEY, "ipaddresses_per_page": "50", **params}
    return client.get(f"{url}?{urlencode(query, doseq=True)}")


def _tab(client, owner, **params):
    response = _fragment(client, owner, **params)
    assert response.status_code == 200
    return response.content.decode()


def _rows(html):
    """Return each row's markup of the rendered IP table by row ID, in table order."""
    table = re.search(r'<table[^>]*id="librenms-ipaddress-table".*?</table>', html, re.S)
    return {unescape(match.group(1)): match.group(0) for match in ROW.finditer(table.group(0) if table else "")}


def _cell_texts(row):
    return {column: _visible_text(cell) for column, cell in CELL.findall(row) if column != "selection"}


def _table_attr(html, name):
    (table,) = re.findall(r'<table[^>]*id="librenms-ipaddress-table"[^>]*>', html)
    return unescape(re.search(rf'{name}="([^"]*)"', table).group(1))


def _mixed_owner(kind, tag, client, server):
    """
    Return a device or VM whose rows cover every status the tab shows.

    The rows are: .1 matched (in VRF blue, on eth1), .2 to update (NetBox holds it unassigned),
    .3 to create on eth3, 2001:db8::4 with no NetBox interface, and .9 twice (eth5 and eth6),
    whose first row is the management address.
    """
    blue = VRF.objects.create(name=f"blue-{tag}")
    if kind == "device":
        owner = make_device(f"ip-filters-{tag}", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})

        def interface(name):
            return Interface.objects.create(device=owner, name=name, type="1000base-t")
    else:
        owner = map_device_to_librenms(make_vm(f"ip-filters-{tag}"), LIBRENMS_ID, server_key=SERVER_KEY)

        def interface(name):
            return VMInterface.objects.create(virtual_machine=owner, name=name)

    IPAddress.objects.create(address="198.18.7.1/24", vrf=blue, assigned_object=interface("eth1"))
    IPAddress.objects.create(address="198.18.7.2/24")
    interface("eth3")
    rows = [
        _row("198.18.7.1", 7501, "eth1"),
        _row("198.18.7.2", 7502, "eth2"),
        _row("198.18.7.3", 7503, "eth3"),
        _row("2001:db8::4", 7504, "eth4", prefix_length=64),
        _row("198.18.7.9", 7505, "eth5"),
        _row("198.18.7.9", 7506, "eth6"),
    ]
    return _refresh(client, owner, server, rows, management_ip="198.18.7.9")


MIXED_STATES = {
    "198.18.7.1/24": "matching",
    "198.18.7.2/24": "mismatched",
    "198.18.7.3/24": "not_in_netbox",
    "2001:db8::4/64": "not_in_netbox",
    "198.18.7.9/24@7505": "not_in_netbox",
    "198.18.7.9/24@7506": "not_in_netbox",
}


@pytest.mark.django_db
class TestMatching:
    """A filter finds rows on every page, by the text each cell shows."""

    def test_an_address_search_finds_a_row_on_another_page_with_the_filtered_total(
        self, client, settings, librenms_server
    ):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client = _client(client, "page-two")
        device = make_device("ip-filters-page-two", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
        _refresh(client, device, librenms_server, [_row(f"198.18.9.{n}", 7600 + n, f"eth{n}") for n in range(1, 61)])

        assert "198.18.9.57/24" not in _rows(_tab(client, device, ipaddresses_per_page="25"))

        html = _tab(client, device, ipaddresses_per_page="25", ipaddresses_q="198.18.9.57")
        assert list(_rows(html)) == ["198.18.9.57/24"]
        assert re.search(r"Showing 1-1 of 1\s*\(filtered from 60\)", html)

        first_page = _tab(client, device, ipaddresses_per_page="25", ipaddresses_filter_interface="eth")
        assert len(_rows(first_page)) == 25
        assert re.search(r"Showing 1-25 of 60\s*\(filtered from 60\)", first_page)
        links = [parse_qs(unescape(href)[1:]) for href in re.findall(r'href="(\?[^"]*)" class="page-link"', first_page)]
        page_two = next(link for link in links if link["ipaddresses_page"] == ["2"])
        assert page_two["ipaddresses_filter_interface"] == ["eth"]
        assert list(_rows(_tab(client, device, **{key: values[0] for key, values in page_two.items()})))[0] == (
            "198.18.9.26/24"
        )

    @pytest.mark.parametrize("kind", ["device", "virtual_machine"])
    def test_every_displayed_column_is_searched_by_the_text_its_cell_shows(
        self, client, settings, librenms_server, kind
    ):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client = _client(client, f"columns-{kind}")
        owner = _mixed_owner(kind, f"columns-{kind}", client, librenms_server)
        texts = {row_id: _cell_texts(row) for row_id, row in _rows(_tab(client, owner)).items()}
        assert set(texts) == set(MIXED_STATES)
        columns = {column for cells in texts.values() for column in cells}
        checked = set()

        for column in sorted(columns):
            values = sorted({cells[column] for cells in texts.values() if len(cells[column]) > 1})
            for needle in values[:3]:
                any_column = [row_id for row_id, cells in texts.items() if any(needle in t for t in cells.values())]
                this_column = [row_id for row_id, cells in texts.items() if needle in cells[column]]

                assert list(_rows(_tab(client, owner, ipaddresses_q=needle.upper()))) == any_column, (column, needle)
                if column in FIELD_COLUMNS:
                    narrowed = _tab(client, owner, **{f"ipaddresses_filter_{column}": f"  {needle}  "})
                    assert list(_rows(narrowed)) == this_column, (column, needle)
                checked.add(column)

        assert checked == {*FIELD_COLUMNS, "vrf", "status"}


@pytest.mark.django_db
class TestSyncState:
    """The sync-state filter shows the rows whose colours and actions say that state."""

    def test_state_colour_and_action_agree_on_every_row(self, client, settings, librenms_server):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client = _client(client, "states")
        device = _mixed_owner("device", "states", client, librenms_server)
        shown = {}
        for state in ("matching", "mismatched", "not_in_netbox"):
            rows = _rows(_tab(client, device, ipaddresses_sync_state=state))
            shown[state] = set(rows)
            for row_id, row in rows.items():
                status = dict(CELL.findall(row))["status"]
                if state == "matching":
                    assert "text-success" in status and 'name="sync_one"' not in status, row_id
                elif state == "mismatched":
                    assert "btn-warning" in status and 'name="sync_one"' in status, row_id
                else:
                    assert "btn-primary" in status or "Missing NetBox Object" in status, row_id

        assert shown == {state: {row_id for row_id, value in MIXED_STATES.items() if value == state} for state in shown}


@pytest.mark.django_db
class TestWholeSnapshotData:
    """Row identities and the management rows describe the whole snapshot, also under a filter."""

    def test_filtering_duplicate_ip_addresses_preserves_snapshot_row_ids_and_management_rows(
        self, client, settings, librenms_server
    ):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client = _client(client, "duplicates")
        device = _mixed_owner("device", "duplicates", client, librenms_server)
        unfiltered = _tab(client, device)

        filtered = _tab(client, device, ipaddresses_q="eth6")

        # The one shown duplicate keeps its source-port key, so its checkbox and VRF field still name that row.
        assert list(_rows(filtered)) == ["198.18.7.9/24@7506"]
        assert 'value="198.18.7.9/24@7506"' in filtered
        assert 'name="vrf_198.18.7.9/24@7506"' in filtered
        # Set Primary IP selects the management row, which the filter does not show.
        assert json.loads(_table_attr(filtered, "data-mgmt-rows")) == ["198.18.7.9/24@7505"]
        assert _table_attr(filtered, "data-mgmt-rows") == _table_attr(unfiltered, "data-mgmt-rows")
        assert _table_attr(filtered, "data-filter-active") == "true"
        assert _table_attr(unfiltered, "data-filter-active") == "false"
        assert _table_attr(filtered, "data-filter-signature") == "ipaddresses_q=eth6"


def _sync_page_url(owner):
    name = "vm_librenms_sync" if owner._meta.model_name == "virtualmachine" else "device_librenms_sync"
    return reverse(f"plugins:netbox_librenms_plugin:{name}", args=[owner.pk])


def _sync_url(owner):
    return reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": owner._meta.model_name, "pk": owner.pk},
    )


@pytest.mark.django_db
class TestInvalidFilters:
    """An invalid filter shows its errors in the tab and changes nothing else."""

    @pytest.mark.parametrize(
        ("tag", "params"),
        [
            ("state", {"ipaddresses_sync_state": "bogus"}),
            ("long", {"ipaddresses_q": "x" * 257}),
            ("unknown", {"ipaddresses_filter_bogus": "eth"}),
            ("no-vrf-field", {"ipaddresses_filter_vrf": "blue"}),
            ("twice", {"ipaddresses_q": ["eth", "198"]}),
        ],
    )
    def test_an_invalid_filter_shows_its_errors_in_place_of_the_table(
        self, client, settings, librenms_server, tag, params
    ):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client = _client(client, f"invalid-{tag}")
        device = _mixed_owner("device", f"invalid-{tag}", client, librenms_server)

        html = _tab(client, device, **params)

        assert _rows(html) == {}
        assert 'id="ipaddresses_filter-errors"' in html
        assert 'data-filter-invalid="true"' in html
        field = next(iter(params))
        if field in ("ipaddresses_sync_state", "ipaddresses_q"):
            assert re.search(rf'id="id_{field}"[^>]*aria-invalid="true"', html, re.S)

    def test_full_page_get_answers_400_and_shows_the_errors_on_that_tab_only(self, client, settings, librenms_server):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client = _client(client, "full-page")
        device = _mixed_owner("device", "full-page", client, librenms_server)

        response = client.get(_sync_page_url(device), {"tab": "ipaddresses", "ipaddresses_sync_state": "bogus"})

        assert response.status_code == 400
        html = response.content.decode()
        assert re.search(r'id="id_ipaddresses_sync_state"[^>]*aria-invalid="true"', html, re.S)
        assert 'id="interfaces_filter-errors"' not in html
        for content_id in ("interface-sync-content", "cable-sync-content", "ipaddress-sync-content"):
            assert f'id="{content_id}"' in html

    def test_htmx_tab_navigation_with_an_invalid_filter_keeps_its_status(self, client, settings, librenms_server):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client = _client(client, "htmx-nav")
        device = _mixed_owner("device", "htmx-nav", client, librenms_server)

        response = client.get(
            _sync_page_url(device), {"tab": "ipaddresses", "ipaddresses_q": "x" * 300}, HTTP_HX_REQUEST="true"
        )

        assert response.status_code == 200
        html = response.content.decode()
        assert re.search(r'<div id="librenms-sync-tabs" data-active-tab="ipaddresses">', html)
        assert 'id="ipaddresses_filter-errors"' in html

    def test_sync_validates_only_its_own_tab_filter_prefix(self, client, settings, librenms_server):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client = _client(client, "own-prefix")
        device = _mixed_owner("device", "own-prefix", client, librenms_server)

        html = _tab(
            client,
            device,
            interfaces_sync_state="bogus",
            vlans_filter_bogus="1",
            cables_q="x" * 300,
            ipaddresses_q="eth3",
        )

        assert 'id="ipaddresses_filter-errors"' not in html
        assert list(_rows(html)) == ["198.18.7.3/24"]

    def test_invalid_filter_does_not_refuse_the_sync_action(self, client, settings, librenms_server):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client = _client(client, "sync-action")
        device = _mixed_owner("device", "sync-action", client, librenms_server)
        query = urlencode({"tab": "ipaddresses", "ipaddresses_sync_state": "bogus"})

        response = client.post(
            f"{_sync_url(device)}?{query}",
            {"server_key": SERVER_KEY, "select": ["198.18.7.3/24"], "vrf_198.18.7.3/24": ""},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert IPAddress.objects.get(address="198.18.7.3/24").assigned_object.name == "eth3"
        assert parse_qs(urlsplit(response.headers["HX-Redirect"]).query)["ipaddresses_sync_state"] == ["bogus"]


# The page query of the tests below: the IP tab's filters and page, and another tab's filter.
PAGE_QUERY = {
    "tab": "ipaddresses",
    "server_key": SERVER_KEY,
    "interfaces_q": "eth  a&b+c é",
    "ipaddresses_filter_address": "198.18.7",
    "ipaddresses_page": "2",
    "ipaddresses_per_page": "1",
}


def _assert_keeps_the_page_query(url, path):
    assert urlsplit(url).path == path, url
    query = parse_qs(urlsplit(url).query)
    assert {key: query.get(key) for key in PAGE_QUERY} == {key: [value] for key, value in PAGE_QUERY.items()}, url


def _rendered_sync_action(client, owner):
    """Return the URL the rendered IP sync form posts to."""
    html = _tab(client, owner, **PAGE_QUERY)
    (action,) = re.findall(r'<form method="post"\s+action="([^"]*)"', html)
    return unescape(action)


@pytest.mark.django_db
class TestRedirectsKeepThePageQuery:
    """Every answer that sends the browser back to the sync page keeps every tab's filters and page."""

    @pytest.mark.parametrize("kind", ["device", "virtual_machine"])
    def test_hx_redirect_after_a_sync_keeps_the_filters_and_page(self, client, settings, librenms_server, kind):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client = _client(client, f"redirect-{kind}")
        owner = _mixed_owner(kind, f"redirect-{kind}", client, librenms_server)
        action = _rendered_sync_action(client, owner)

        response = client.post(
            action,
            {"server_key": SERVER_KEY, "select": ["198.18.7.3/24"], "vrf_198.18.7.3/24": ""},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        target = response.headers["HX-Redirect"]
        _assert_keeps_the_page_query(target, _sync_page_url(owner))
        # The page the browser loads shows the IP tab with the same filter.
        page = client.get(target)
        assert page.status_code == 200
        html = page.content.decode()
        assert _rows(html) and all(row_id.startswith("198.18.7.") for row_id in _rows(html))
        assert re.search(r"\(filtered from 6\)", html)

    def test_a_conflict_page_cancel_link_keeps_the_filters_and_page(self, client, settings, librenms_server):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client = _client(client, "conflict")
        device = _mixed_owner("device", "conflict", client, librenms_server)
        # NetBox now holds .3 on another interface, so a sync of .3 asks for a confirmation.
        IPAddress.objects.create(
            address="198.18.7.3/24", assigned_object=Interface.objects.create(device=device, name="eth9", type="other")
        )
        action = _rendered_sync_action(client, device)

        response = client.post(action, {"server_key": SERVER_KEY, "select": ["198.18.7.3/24"], "vrf_198.18.7.3/24": ""})

        assert response.status_code == 200
        html = response.content.decode()
        hrefs = [unescape(href) for href in re.findall(r'<a href="([^"]*)" class="btn(?:-close| btn-secondary)"', html)]
        # The close button and the Cancel button go back to the tab.
        cancel_links = [url for url in hrefs if urlsplit(url).path == _sync_page_url(device)]
        assert len(cancel_links) == 2
        for url in cancel_links:
            _assert_keeps_the_page_query(url, _sync_page_url(device))
        (confirm_action,) = re.findall(r'<form method="post"\s+action="([^"]*)"', html)
        _assert_keeps_the_page_query(unescape(confirm_action), _sync_url(device))
