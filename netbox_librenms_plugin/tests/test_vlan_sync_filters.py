"""
Server-side filters on the VLANs sync tab: matching, sync states, invalid filters and redirects.

Each test seeds the LibreNMS VLAN snapshot under the real cache key and renders the real tab
through a real request: the fragment that the sync page loads, the full sync page, or a sync POST.
NetBox VLANs, VLAN groups and sites are real rows.
"""

import re
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.core.cache import cache
from django.urls import reverse
from ipam.models import VLAN, VLANGroup

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
from netbox_librenms_plugin.tests.test_interface_sync_filters import CELL, _visible_text, serve_sync_page
from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

SERVER_KEY = "default"
LIBRENMS_ID = 4242

ROW = re.compile(r"<tr\b[^>]*>(?:(?!</tr>).)*?data-col=\"vlan_id\".*?</tr>", re.S)
FIELD_COLUMNS = {
    "vlan_id": "vlan_id",
    "name": "name",
    "vlan_group_selection": "group",
    "type": "type",
    "state": "state",
}


def _seed(device, rows):
    cache.set(SyncVLANsView().get_cache_key(device, "vlans", SERVER_KEY), rows, timeout=300)


def _vlan(vid, name, **fields):
    return {"vlan_vlan": vid, "vlan_name": name, "vlan_type": "ethernet", "vlan_state": 1, **fields}


def _mixed_device(tag):
    """
    Return a device whose rows cover every VLAN comparison the tab shows.

    The rows are: 10 in sync (global), 20 with another name in NetBox (site group), 30 and 40 not in
    NetBox (40 inactive, of another type), an invalid VID, and VID 50 twice.
    """
    device = make_device(f"vlan-filters-{tag}", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
    campus = VLANGroup.objects.create(name=f"campus-{tag}", slug=f"campus-{tag}", scope=device.site)
    VLANGroup.objects.create(name=f"global-{tag}", slug=f"global-{tag}")
    VLAN.objects.create(vid=10, name="users", status="active")
    VLAN.objects.create(vid=20, name="visitors", group=campus, status="active")
    _seed(
        device,
        [
            _vlan(10, "users"),
            _vlan(20, "guests"),
            _vlan(30, "voice"),
            _vlan(40, "printers", vlan_type="tokenring", vlan_state=0),
            _vlan("abc", "broken"),
            _vlan(50, "dup-a"),
            _vlan(50, "dup-b"),
        ],
    )
    return device


MIXED_STATES = {"users": "matching", "guests": "mismatched", "voice": "not_in_netbox", "printers": "not_in_netbox"}
UNCLASSIFIED = {"broken", "dup-a", "dup-b"}


def _client(client, settings, tag):
    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server

    configure_default_librenms_server(settings)
    client.force_login(make_superuser(f"vlan-filter-{tag}"))
    return client


def _fragment(client, device, **params):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": "vlans"},
    )
    query = {"server_key": SERVER_KEY, "vlans_per_page": "50", **params}
    return client.get(f"{url}?{urlencode(query, doseq=True)}")


def _tab(client, device, **params):
    response = _fragment(client, device, **params)
    assert response.status_code == 200
    return response.content.decode()


def _cell_texts(row):
    return {column: _visible_text(cell) for column, cell in CELL.findall(row) if column != "selection"}


def _rows(html):
    """Return each row's markup of the rendered VLAN table by its VLAN name, in table order."""
    table = re.search(r'<table[^>]*id="librenms-vlan-table".*?</table>', html, re.S)
    body = table.group(0).split("<tbody>", 1)[1] if table else ""
    return {_cell_texts(row)["name"]: row for row in ROW.findall(body)}


@pytest.mark.django_db
class TestMatching:
    """A filter finds rows on every page, by the text each cell shows."""

    def test_a_name_search_finds_a_vlan_on_another_page_with_the_filtered_total(self, client, settings):
        client = _client(client, settings, "page-two")
        device = make_device("vlan-filters-page-two", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
        _seed(device, [_vlan(100 + index, f"vlan-{index:03d}") for index in range(60)])

        assert "vlan-057" not in _rows(_tab(client, device, vlans_per_page="25"))

        html = _tab(client, device, vlans_per_page="25", vlans_q="VLAN-057")
        assert list(_rows(html)) == ["vlan-057"]
        # The browser keys the selection on this text, so a selection never carries to another server.
        assert re.search(r'data-sync-filter-tab="vlans" data-selection-scope="[^"]*server_key=default', html)
        assert re.search(r"Showing 1-1 of 1\s*\(filtered from 60\)", html)

        first_page = _tab(client, device, vlans_per_page="25", vlans_filter_name="vlan-0")
        assert len(_rows(first_page)) == 25
        assert re.search(r"Showing 1-25 of 60\s*\(filtered from 60\)", first_page)
        links = [parse_qs(unescape(href)[1:]) for href in re.findall(r'href="(\?[^"]*)" class="page-link"', first_page)]
        page_two = next(link for link in links if link["vlans_page"] == ["2"])
        assert page_two["vlans_filter_name"] == ["vlan-0"]
        assert list(_rows(_tab(client, device, **{key: values[0] for key, values in page_two.items()})))[0] == (
            "vlan-025"
        )

    def test_every_displayed_column_is_searched_by_the_text_its_cell_shows(self, client, settings):
        client = _client(client, settings, "columns")
        device = _mixed_device("columns")
        texts = {name: _cell_texts(row) for name, row in _rows(_tab(client, device)).items()}
        assert set(texts) == set(MIXED_STATES) | UNCLASSIFIED
        # The group cell shows the selected option, with the scope of a scoped group.
        assert texts["guests"]["vlan_group_selection"] == f"campus-columns ({device.site.name})".casefold()
        assert texts["voice"]["vlan_group_selection"] == "-- no group (global) --"
        assert texts["broken"]["vlan_id"] == "abc invalid vid"
        columns = {column for cells in texts.values() for column in cells}
        checked = set()

        for column in sorted(columns):
            values = sorted({cells[column] for cells in texts.values() if len(cells[column]) > 1})
            for needle in values[:3]:
                any_column = [name for name, cells in texts.items() if any(needle in t for t in cells.values())]
                this_column = [name for name, cells in texts.items() if needle in cells[column]]

                assert list(_rows(_tab(client, device, vlans_q=needle.upper()))) == any_column, (column, needle)
                if column in FIELD_COLUMNS:
                    field = {f"vlans_filter_{FIELD_COLUMNS[column]}": f"  {needle}  "}
                    assert list(_rows(_tab(client, device, **field))) == this_column, (column, needle)
                checked.add(column)

        assert checked == {*FIELD_COLUMNS, "status"}


@pytest.mark.django_db
class TestSyncState:
    """The sync-state filter shows the rows whose colours and actions say that state."""

    def test_state_colour_and_action_agree_on_every_row(self, client, settings):
        client = _client(client, settings, "states")
        device = _mixed_device("states")
        colour = {"matching": "text-success", "mismatched": "text-warning", "not_in_netbox": "text-danger"}
        action = {"matching": "Synced", "mismatched": "btn-warning", "not_in_netbox": "btn-primary"}
        shown = {}
        for state in colour:
            rows = _rows(_tab(client, device, vlans_sync_state=state))
            shown[state] = set(rows)
            for name, row in rows.items():
                cells = dict(CELL.findall(row))
                assert colour[state] in cells["vlan_id"] and colour[state] in cells["name"], name
                assert action[state] in cells["status"], name

        assert shown == {state: {name for name, value in MIXED_STATES.items() if value == state} for state in shown}
        # An invalid or duplicate VID is in no state: it shows only without a state filter.
        assert UNCLASSIFIED <= set(_rows(_tab(client, device)))


@pytest.mark.django_db
class TestWholeSnapshotData:
    """The VID checks run over every row, so a filter that shows one duplicate still marks it."""

    def test_a_filtered_duplicate_vid_stays_a_duplicate(self, client, settings):
        client = _client(client, settings, "duplicates")
        device = _mixed_device("duplicates")

        rows = _rows(_tab(client, device, vlans_q="dup-a"))

        assert list(rows) == ["dup-a"]
        assert _cell_texts(rows["dup-a"])["vlan_id"] == "50 duplicate vid"
        assert 'name="select"' not in rows["dup-a"]


def _sync_url(device):
    return reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans", kwargs={"object_type": "device", "object_id": device.pk}
    )


def _sync_page_url(device):
    return reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])


def _vlan_tab_url(device):
    """Return the path the VLAN sync answers send the browser to: the device's own sync tab."""
    return reverse("dcim:device_librenms_sync", kwargs={"pk": device.pk})


@pytest.mark.django_db
class TestInvalidFilters:
    """An invalid filter shows its errors in the tab and changes nothing else."""

    @pytest.mark.parametrize(
        ("tag", "params"),
        [
            ("state", {"vlans_sync_state": "bogus"}),
            ("long", {"vlans_q": "x" * 257}),
            ("unknown", {"vlans_filter_bogus": "10"}),
            ("no-status-field", {"vlans_filter_status": "Create"}),
            ("twice", {"vlans_filter_name": ["users", "voice"]}),
        ],
    )
    def test_an_invalid_filter_shows_its_errors_in_place_of_the_table(self, client, settings, tag, params):
        client = _client(client, settings, f"invalid-{tag}")
        device = _mixed_device(f"invalid-{tag}")

        html = _tab(client, device, **params)

        assert _rows(html) == {}
        assert 'id="vlans_filter-errors"' in html
        assert 'data-filter-invalid="true"' in html
        # The tab names its selection scope also with no table, so the browser can drop other scopes.
        assert re.search(r'data-sync-filter-tab="vlans" data-selection-scope="[^"]*interface_name_field=', html)
        field = next(iter(params))
        if field in ("vlans_sync_state", "vlans_q"):
            assert re.search(rf'id="id_{field}"[^>]*aria-invalid="true"', html, re.S)

    def test_an_invalid_filter_on_a_device_without_vlans_still_shows_its_errors(self, client, settings):
        client = _client(client, settings, "invalid-empty")
        device = make_device("vlan-filters-invalid-empty", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
        _seed(device, [])

        html = _tab(client, device, vlans_sync_state="bogus")

        assert 'id="vlans_filter-errors"' in html

    def test_an_active_filter_on_an_empty_snapshot_keeps_its_controls(self, client, settings):
        client = _client(client, settings, "active-empty")
        device = make_device("vlan-filters-active-empty", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
        _seed(device, [])

        html = _tab(client, device, vlans_q="voice")

        # The bound form, its Clear link and the filtered (empty) table stay, so the filter can be removed.
        assert re.search(r'<input type="search"[^>]*name="vlans_q" id="id_vlans_q" value="voice"', html)
        assert re.search(r'<form[^>]*id="vlans_filter-form"[^>]*class="sync-filter-form', html)
        clear = [unescape(href) for href in re.findall(r'<a class="[^"]*sync-filter-clear[^"]*" href="([^"]*)"', html)]
        assert len(clear) == 1 and "vlans_q" not in parse_qs(urlsplit(clear[0]).query)
        assert re.search(r'<table[^>]*id="librenms-vlan-table"[^>]*data-filter-active="true"', html)
        assert _rows(html) == {}
        assert "mdi-sync-off" not in html

    def test_full_page_get_answers_400_and_shows_the_errors_on_that_tab_only(self, client, settings, librenms_server):
        device = serve_sync_page(client, settings, librenms_server, _mixed_device("full-page"))

        response = client.get(_sync_page_url(device), {"tab": "vlans", "vlans_sync_state": "bogus"})

        assert response.status_code == 400
        html = response.content.decode()
        assert re.search(r'id="id_vlans_sync_state"[^>]*aria-invalid="true"', html, re.S)
        assert 'id="interfaces_filter-errors"' not in html
        assert 'id="ipaddresses_filter-errors"' not in html
        for content_id in ("interface-sync-content", "ipaddress-sync-content", "vlan-sync-content"):
            assert f'id="{content_id}"' in html

    def test_htmx_tab_navigation_with_an_invalid_filter_keeps_its_status(self, client, settings, librenms_server):
        device = serve_sync_page(client, settings, librenms_server, _mixed_device("htmx-nav"))

        response = client.get(_sync_page_url(device), {"tab": "vlans", "vlans_q": "x" * 300}, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        from netbox_librenms_plugin.tests.view_test_helpers import sync_tab_attributes

        html = response.content.decode()
        attrs = sync_tab_attributes(html)
        assert attrs["data-active-tab"] == "vlans"
        assert attrs["data-server-key"] == SERVER_KEY
        assert 'id="vlans_filter-errors"' in html

    def test_sync_validates_only_its_own_tab_filter_prefix(self, client, settings):
        client = _client(client, settings, "own-prefix")
        device = _mixed_device("own-prefix")

        html = _tab(
            client,
            device,
            interfaces_sync_state="bogus",
            ipaddresses_filter_bogus="1",
            cables_q="x" * 300,
            vlans_q="voice",
        )

        assert 'id="vlans_filter-errors"' not in html
        assert list(_rows(html)) == ["voice"]

    def test_invalid_filter_does_not_refuse_the_sync_action(self, client, settings):
        client = _client(client, settings, "sync-action")
        device = _mixed_device("sync-action")
        query = urlencode({"tab": "vlans", "vlans_sync_state": "bogus"})

        response = client.post(
            f"{_sync_url(device)}?{query}",
            {
                "server_key": SERVER_KEY,
                "action": "create_vlans",
                "select": ["30"],
                "vlan_group_30": "",
                "expected_vlan_name_30": "voice",
            },
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert VLAN.objects.filter(vid=30, name="voice", group__isnull=True).exists()
        assert parse_qs(urlsplit(response.headers["HX-Redirect"]).query)["vlans_sync_state"] == ["bogus"]


# The page query of the tests below: the VLAN tab's filters and page, and another tab's filter.
PAGE_QUERY = {
    "tab": "vlans",
    "server_key": SERVER_KEY,
    "interfaces_q": "eth  a&b+c é",
    "vlans_filter_type": "ethernet",
    "vlans_page": "2",
    "vlans_per_page": "1",
}


def _assert_keeps_the_page_query(url, path):
    assert urlsplit(url).path == path, url
    query = parse_qs(urlsplit(url).query)
    assert {key: query.get(key) for key in PAGE_QUERY} == {key: [value] for key, value in PAGE_QUERY.items()}, url


def _rendered_sync_action(client, device):
    """Return the URL the rendered VLAN sync form posts to."""
    (action,) = re.findall(r'<form method="post"[^>]*action="([^"]*)"', _tab(client, device, **PAGE_QUERY))
    return unescape(action)


@pytest.mark.django_db
class TestRedirectsKeepThePageQuery:
    """Every answer that sends the browser back to the sync page keeps every tab's filters and page."""

    def test_hx_redirect_after_a_sync_keeps_the_filters_and_page(self, client, settings, librenms_server):
        device = serve_sync_page(client, settings, librenms_server, _mixed_device("redirect"))
        action = _rendered_sync_action(client, device)

        response = client.post(
            action,
            {
                "server_key": SERVER_KEY,
                "action": "create_vlans",
                "select": ["30"],
                "vlan_group_30": "",
                "expected_vlan_name_30": "voice",
            },
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        target = response.headers["HX-Redirect"]
        _assert_keeps_the_page_query(target, _vlan_tab_url(device))
        page = client.get(target)
        assert page.status_code == 200
        html = page.content.decode()
        assert _rows(html) and "printers" not in _rows(html)
        assert re.search(r"\(filtered from 7\)", html)

    @pytest.mark.parametrize("htmx", [True, False])
    def test_the_rename_confirmation_keeps_the_filters_and_page(self, client, settings, librenms_server, htmx):
        device = serve_sync_page(client, settings, librenms_server, _mixed_device(f"rename-{htmx}"))
        group = VLANGroup.objects.get(name=f"campus-rename-{htmx}")
        action = _rendered_sync_action(client, device)
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}

        response = client.post(
            action,
            {
                "server_key": SERVER_KEY,
                "action": "create_vlans",
                "select": ["20"],
                "vlan_group_20": str(group.pk),
                "expected_vlan_name_20": "guests",
            },
            **headers,
        )

        assert response.status_code == 200
        html = response.content.decode()
        (confirm_action,) = re.findall(r'<form method="post"[^>]*action="([^"]*)"', html)
        _assert_keeps_the_page_query(unescape(confirm_action), _sync_url(device))
        if not htmx:
            hrefs = [
                unescape(href) for href in re.findall(r'<a href="([^"]*)" class="btn(?:-close| btn-secondary)"', html)
            ]
            # The close button and the Cancel button go back to the tab.
            cancel_links = [url for url in hrefs if urlsplit(url).path == _vlan_tab_url(device)]
            assert len(cancel_links) == 2
            for url in cancel_links:
                _assert_keeps_the_page_query(url, _vlan_tab_url(device))
