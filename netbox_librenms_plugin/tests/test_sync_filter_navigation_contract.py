"""
The navigation contract of the filtered sync tabs: every hop keeps the page query or applies a documented reset.

Each test renders the real sync page with distinctive filter values (spaces, ``&``, ``+`` and
non-ASCII text), finds every link, form, ``formaction`` and ``hx-*`` URL in one tab pane, and
checks each one. A URL the test does not know fails, so a new hop must be classified here.
"""

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable
from netbox_librenms_plugin.server_selection import get_librenms_sync_device
from netbox_librenms_plugin.tests.conftest import (
    bind_librenms_server,
    configure_librenms_servers,
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
    map_device_to_librenms,
)
from netbox_librenms_plugin.tests.test_interface_sync_filters import (
    LIBRENMS_ID,
    SERVER_KEY,
    STALE_PORT,
    _bind,
    _chassis,
    _port,
    _seed,
    _virtual_machine,
    serve_sync_page,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping
from netbox_librenms_plugin.tests.test_ip_address_sync_filters import _refresh
from netbox_librenms_plugin.views.sync.cables import SyncCablesView
from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

URL_ATTRIBUTES = ("href", "action", "formaction", "hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete")
VOID_TAGS = frozenset({"input", "br", "img", "hr", "meta", "link", "source", "wbr"})

# This tab's filters and another tab's: every hop must keep the other tab's state as well.
PAGE_FILTERS = {
    "interfaces_q": "uplink  a&b+c é",
    "interfaces_filter_name": "eth",
    "cables_q": "keep  this & that",
}
PAGE_QUERY = {
    "tab": "interfaces",
    "server_key": SERVER_KEY,
    "interfaces_per_page": "2",
    "interfaces_page": "2",
    "interfaces_sort": "name",
    "interfaces_show_ignored": "1",
    **PAGE_FILTERS,
}
INTERFACE_FILTERS = tuple(key for key in PAGE_FILTERS if key.startswith("interfaces_"))
KEEP_ALL = (*PAGE_FILTERS, "interfaces_page", "interfaces_per_page", "interfaces_sort", "interfaces_show_ignored")
KEEP_ALL_BUT_PAGE = tuple(key for key in KEEP_ALL if key != "interfaces_page")


class _Hops(HTMLParser):
    """Collect every URL attribute inside the element with *root_id*, and the fields of each GET form."""

    def __init__(self, root_id):
        super().__init__(convert_charrefs=True)
        self.root_id = root_id
        self.depth = 0
        self.urls = []
        self.get_forms = []
        self._get_form = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if not self.depth:
            self.depth = 1 if attributes.get("id") == self.root_id else 0
            return
        if tag not in VOID_TAGS:
            self.depth += 1
        if tag == "form" and (attributes.get("method") or "get").lower() == "get":
            self._get_form = {"attrs": attributes, "fields": []}
            self.get_forms.append(self._get_form)
        if self._get_form is not None and tag in ("input", "select") and attributes.get("name"):
            self._get_form["fields"].append((attributes["name"], attributes.get("value") or ""))
        for name in URL_ATTRIBUTES:
            if name in attributes:
                self.urls.append(
                    {"tag": tag, "attrs": attributes, "url": attributes[name], "in_get_form": bool(self._get_form)}
                )

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "form":
            self._get_form = None
        self.depth -= 1


def _hops(html, root_id):
    parser = _Hops(root_id)
    parser.feed(html)
    assert parser.urls, f"no hop found inside #{root_id}"
    return parser


def _query(url):
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _assert_keeps(query, keys, where, page_query=PAGE_QUERY):
    assert {key: query.get(key) for key in keys} == {key: [page_query[key]] for key in keys}, where


def _routes(owner, object_type):
    def plugin_url(name, **kwargs):
        return reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs=kwargs)

    object_kwargs = {"object_type": object_type, "object_id": owner.pk}
    return {
        "sync": plugin_url("sync_selected_interfaces", **object_kwargs),
        "rebind": plugin_url("rebind_interface_port", **object_kwargs),
        "refresh": plugin_url(f"{'device' if object_type == 'device' else 'vm'}_interface_sync", pk=owner.pk),
        "fragment": plugin_url("sync_cache_fragment", object_type=object_type, pk=owner.pk, tab="interfaces"),
    }


def _classify(hop, page_path, routes):
    """Check one hop against the transition table and return its kind."""
    url, attrs = hop["url"], hop["attrs"]
    path, query = urlsplit(url).path, _query(url)
    on_page = path in ("", page_path)
    if url.startswith("#"):
        return "anchor"
    if path == routes["fragment"]:
        # The loader states only its tab; the browser adds the page query (the browser test checks it).
        return "fragment"
    if "/sync-interface-" in path or path.startswith("/dcim/interfaces/"):
        # A relationship sync answers JSON and repaints one button; an object link leaves the page.
        return "follow-up"
    if on_page and hop["in_get_form"]:
        # Clear filters: this tab's filters and page go, every other state stays.
        assert not any(key in query for key in (*INTERFACE_FILTERS, "interfaces_page")), url
        _assert_keeps(query, ("cables_q", "interfaces_per_page", "interfaces_sort", "interfaces_show_ignored"), url)
        return "clear"
    if on_page and attrs.get("id") == "interfaces-show-ignored-toggle":
        # Show ignored: the filters stay, the page goes, the toggle flips.
        assert "interfaces_page" not in query and "interfaces_show_ignored" not in query, url
        _assert_keeps(query, (*PAGE_FILTERS, "interfaces_per_page", "interfaces_sort"), url)
        return "toggle"
    if on_page:
        # Paging, page size and sort: each states its own key, and everything else stays.
        assert query["tab"] == ["interfaces"], url
        _assert_keeps(query, (*PAGE_FILTERS, "interfaces_show_ignored"), url)
        return "page link"
    if path in (routes["refresh"], routes["sync"], routes["rebind"]):
        # A refresh, sync or Rebind comes back to the same filters and page, clamped to the last page.
        _assert_keeps(query, KEEP_ALL, url)
        return {routes["refresh"]: "refresh", routes["sync"]: "sync", routes["rebind"]: "rebind"}[path]
    pytest.fail(f"An unclassified hop in the interfaces pane: <{hop['tag']}> {url}")


def _check_filter_form(hops):
    (form,) = [form for form in hops.get_forms if "sync-filter-form" in (form["attrs"].get("class") or "")]
    submitted = {}
    for key, value in form["fields"]:
        if value:
            submitted.setdefault(key, []).append(value)
    # A filter change keeps every other state and starts at page 1.
    assert "interfaces_page" not in submitted
    _assert_keeps(submitted, KEEP_ALL_BUT_PAGE, form)


def _render_page(client, owner):
    name = "vm_librenms_sync" if owner._meta.model_name == "virtualmachine" else "device_librenms_sync"
    page_path = reverse(f"plugins:netbox_librenms_plugin:{name}", args=[owner.pk])
    response = client.get(page_path, PAGE_QUERY)
    assert response.status_code == 200
    return page_path, response.content.decode()


def _contract_ports():
    """Return rows that match the page filters, and a row of each kind the filters drop."""
    alias = {"ifAlias": "Uplink a&b+c É"}
    return [
        *(_port(9100 + index, f"eth{index}", **alias) for index in range(12)),
        _port(1007, "eth100", **alias),
        _port(1008, "loopback0", ifType="softwareLoopback", **alias),
        _port(9200, "po9", **alias),
    ]


def _contract_owner(kind):
    if kind == "device":
        device = make_device("contract-device", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
        # A stale port holds the name of one row, so that row offers Rebind; by name it sorts onto page 2.
        _bind(device.interfaces.create(name="eth100", type="other"), STALE_PORT)
        return device, "device"
    if kind == "virtual_chassis":
        return _chassis("contract")[0], "device"
    return _virtual_machine("contract"), "virtualmachine"


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["device", "virtual_chassis", "virtual_machine"])
def test_every_interface_tab_hop_keeps_the_page_query_or_applies_its_reset(client, settings, librenms_server, kind):
    owner, object_type = _contract_owner(kind)
    _seed(owner, _contract_ports())
    serve_sync_page(client, settings, librenms_server, owner)

    page_path, html = _render_page(client, owner)
    hops = _hops(html, "interfaces")
    kinds = {_classify(hop, page_path, _routes(owner, object_type)) for hop in hops.urls}
    _check_filter_form(hops)

    required = {"fragment", "clear", "toggle", "page link", "refresh", "sync"} | (
        {"rebind"} if kind == "device" else set()
    )
    assert required <= kinds, required - kinds


@pytest.mark.django_db
def test_the_tab_links_keep_every_tab_state(client, settings, librenms_server):
    owner, _object_type = _contract_owner("device")
    _seed(owner, _contract_ports())
    serve_sync_page(client, settings, librenms_server, owner)

    _page_path, html = _render_page(client, owner)
    tab_links = [hop["url"] for hop in _hops(html, "librenmsSync").urls if hop["tag"] == "a"]

    assert len(tab_links) >= 4
    for url in tab_links:
        _assert_keeps(_query(url), (*PAGE_FILTERS, "interfaces_page", "interfaces_per_page"), url)


@pytest.mark.django_db
def test_the_module_paginator_keeps_the_interface_filters():
    """The Modules tab shares the paginator, so its links carry the other tabs' state too."""
    rows = [{"ent_physical_index": index, "name": f"module {index}", "depth": 0} for index in range(60)]
    table = LibreNMSModuleTable(rows, server_key=SERVER_KEY)
    request = RequestFactory().get("/", {**PAGE_QUERY, "tab": "modules", "modules_per_page": "25"})
    request.user = AnonymousUser()
    table.configure(request)

    html = render_to_string("netbox_librenms_plugin/inc/paginator.html", {"table": table}, request=request)
    links = [hop["url"] for hop in _hops(f'<div id="paginator">{html}</div>', "paginator").urls]

    assert len(links) >= 3
    for url in links:
        query = _query(url)
        assert query["tab"] == ["modules"], url
        _assert_keeps(query, (*PAGE_FILTERS, "interfaces_page", "interfaces_per_page"), url)


# The IP Addresses and VLANs tabs: this tab's filters and another tab's, on page 2 of a sorted table.
OTHER_TAB_FILTER = {"interfaces_q": "keep  this & that"}
TAB_PAGE_QUERIES = {
    "ipaddresses": {
        "ipaddresses_q": "a&b+c  é",
        "ipaddresses_filter_address": "198.18",
        "ipaddresses_per_page": "2",
        "ipaddresses_page": "2",
        "ipaddresses_sort": "address",
    },
    "vlans": {
        "vlans_q": "a&b+c  é",
        "vlans_filter_type": "ethernet",
        "vlans_per_page": "2",
        "vlans_page": "2",
        "vlans_sort": "name",
    },
    "cables": {
        "cables_q": "a&b+c  é",
        "cables_filter_local_port": "Gi",
        "cables_per_page": "2",
        "cables_page": "2",
        "cables_sort": "local_port",
    },
}
# An object detail link leaves the sync page, so it carries no page state.
OBJECT_LINK = re.compile(r"^/(?:ipam|dcim|virtualization)/[\w-]+/\d+/$")


def _tab_routes(owner, tab):
    def plugin_url(name, **kwargs):
        return reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs=kwargs)

    object_type = owner._meta.model_name
    kind = "device" if object_type == "device" else "vm"
    fragment = plugin_url("sync_cache_fragment", object_type=object_type, pk=owner.pk, tab=tab)
    if tab == "ipaddresses":
        posts = {
            plugin_url(f"{kind}_ipaddress_sync", pk=owner.pk): "refresh",
            plugin_url("sync_device_ip_addresses", object_type=object_type, pk=owner.pk): "sync",
            plugin_url("create_ip_row_vrf", object_type=object_type, pk=owner.pk): "create vrf",
        }
    elif tab == "cables":
        posts = {
            plugin_url("device_cable_sync", pk=owner.pk): "refresh",
            plugin_url("sync_device_cables", pk=owner.pk): "sync",
        }
    else:
        posts = {
            plugin_url("device_vlan_sync", pk=owner.pk): "refresh",
            plugin_url("sync_selected_vlans", object_type=object_type, object_id=owner.pk): "sync",
        }
    return fragment, posts


def _classify_tab_hop(hop, page_path, tab, owner, page_query):
    """Check one hop of the IP Addresses or VLANs pane against the transition table and return its kind."""
    url = hop["url"]
    path, query = urlsplit(url).path, _query(url)
    prefix = f"{tab}_"
    own_filters = [key for key in page_query if key.startswith(prefix) and ("_filter_" in key or key.endswith("_q"))]
    fragment, posts = _tab_routes(owner, tab)
    on_page = path in ("", page_path)
    if url.startswith("#"):
        return "anchor"
    if path == fragment:
        # The loader states only its tab; the browser adds the page query.
        return "fragment"
    if tab == "cables" and path == reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[owner.pk]):
        # The picker loader takes each row's picker URL, and the browser adds the page query (the browser test checks it).
        return "picker loader"
    if OBJECT_LINK.match(path):
        return "object link"
    if on_page and hop["in_get_form"]:
        # Clear filters: this tab's filters and page go, every other state stays.
        assert not any(key in query for key in (*own_filters, f"{prefix}page")), url
        _assert_keeps(query, (*OTHER_TAB_FILTER, f"{prefix}per_page", f"{prefix}sort"), url, page_query)
        return "clear"
    if on_page:
        # Paging, page size and sort: each states its own key, and every filter stays.
        assert query["tab"] == [tab], url
        _assert_keeps(query, (*OTHER_TAB_FILTER, *own_filters), url, page_query)
        return "page link"
    if path in posts:
        # A refresh, sync or VRF create comes back to the same filters and page, clamped to the last page.
        _assert_keeps(query, tuple(page_query), url, page_query)
        return posts[path]
    pytest.fail(f"An unclassified hop in the {tab} pane: <{hop['tag']}> {url}")


def _check_tab_filter_form(hops, tab, page_query):
    (form,) = [form for form in hops.get_forms if "sync-filter-form" in (form["attrs"].get("class") or "")]
    submitted = {}
    for key, value in form["fields"]:
        if value:
            submitted.setdefault(key, []).append(value)
    # A filter change keeps every other state and starts at page 1.
    assert f"{tab}_page" not in submitted
    _assert_keeps(submitted, tuple(key for key in page_query if key != f"{tab}_page"), form, page_query)


def _ip_owner(kind, client, server):
    """Return a device or VM with IP rows whose interface names match the IP filters of the page query."""
    if kind == "device":
        owner = make_device(f"contract-ip-{kind}", librenms_cf={"default": {"id": 42}})
    else:
        owner = map_device_to_librenms(make_vm(f"contract-ip-{kind}"), 42, server_key=SERVER_KEY)
    rows = [
        {"address": f"198.18.8.{n}", "prefix_length": 24, "port_id": 7700 + n, "interface": f"Gi a&b+c É{n}"}
        for n in range(1, 13)
    ]
    rows.append({"address": "203.0.113.1", "prefix_length": 24, "port_id": 7799, "interface": "Gi a&b+c É99"})
    return _refresh(client, owner, server, rows)


def _vlan_owner(client, settings, server):
    """Return a device with VLAN rows whose names match the VLAN filters of the page query."""
    device = make_device("contract-vlan", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
    rows = [
        {"vlan_vlan": 100 + n, "vlan_name": f"v{n:02d} a&b+c É", "vlan_type": "ethernet", "vlan_state": 1}
        for n in range(12)
    ]
    rows.append({"vlan_vlan": 999, "vlan_name": "v99 a&b+c É", "vlan_type": "tokenring", "vlan_state": 1})
    cache.set(SyncVLANsView().get_cache_key(device, "vlans", SERVER_KEY), rows, timeout=300)
    return serve_sync_page(client, settings, server, device)


def _cable_owner(kind, client, settings, server):
    """Return a device or VC member with cable rows whose remote device and local ports match the cable filters."""
    if kind == "device":
        owner = make_device("contract-cables", librenms_cf={SERVER_KEY: {"id": LIBRENMS_ID}})
        members = [owner]
    else:
        _vc, members = make_virtual_chassis_members("contract-cables", 2)
        owner = members[0]
        seed_stored_mapping(owner, {SERVER_KEY: {"id": LIBRENMS_ID}}, save=True)
    peer = make_device("contract-cables-peer")
    links = []
    for n in range(1, 13):
        member = members[(n - 1) % len(members)]
        local = make_interface(member, f"Gi{member.vc_position or 1}/0/{n}")
        make_interface(peer, f"Ethernet{n}")
        links.append(
            {
                "_source": "main",
                "local_port_id": 9600 + n,
                "local_port": local.name,
                "remote_device": f"Peer a&b+c É {n}" if n % 3 else peer.name,
                "remote_port": f"Ethernet{n}",
            }
        )
    links.append(
        {
            "_source": "main",
            "local_port_id": 9699,
            "local_port": "xe-9",
            "remote_device": "Peer a&b+c É",
            "remote_port": "xe-9",
        }
    )
    sync_device = get_librenms_sync_device(owner, server_key=SERVER_KEY) or owner
    cache.set(
        object.__new__(SyncCablesView).get_cache_key(sync_device, "links", SERVER_KEY),
        {"links": links, "snapshot_token": "contract-cables", "incomplete_sources": []},
        timeout=300,
    )
    return serve_sync_page(client, settings, server, owner)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "variant",
    ["ipaddresses-device", "ipaddresses-virtual_machine", "vlans-device", "cables-device", "cables-virtual_chassis"],
)
def test_every_ip_vlan_and_cable_tab_hop_keeps_the_page_query_or_applies_its_reset(
    client, settings, librenms_server, variant
):
    tab, kind = variant.split("-")
    if tab == "ipaddresses":
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client.force_login(make_superuser("contract-ip-user"))
        owner = _ip_owner(kind, client, librenms_server)
    elif tab == "cables":
        owner = _cable_owner(kind, client, settings, librenms_server)
    else:
        owner = _vlan_owner(client, settings, librenms_server)
    page_query = {"tab": tab, "server_key": SERVER_KEY, **OTHER_TAB_FILTER, **TAB_PAGE_QUERIES[tab]}
    name = "vm_librenms_sync" if owner._meta.model_name == "virtualmachine" else "device_librenms_sync"
    page_path = reverse(f"plugins:netbox_librenms_plugin:{name}", args=[owner.pk])

    response = client.get(page_path, page_query)

    assert response.status_code == 200
    html = response.content.decode()
    assert 'data-filter-active="true"' in html
    hops = _hops(html, tab)
    kinds = {_classify_tab_hop(hop, page_path, tab, owner, page_query) for hop in hops.urls}
    _check_tab_filter_form(hops, tab, page_query)
    required = {"fragment", "clear", "page link", "refresh", "sync"} | ({"picker loader"} if tab == "cables" else set())
    assert required <= kinds, kinds


@pytest.mark.django_db
def test_a_server_switch_keeps_only_the_tab_page_sizes_and_sorts(client, settings, librenms_server):
    owner, _object_type = _contract_owner("device")
    _seed(owner, _contract_ports())
    serve_sync_page(client, settings, librenms_server, owner)
    configure_librenms_servers(
        settings,
        {
            SERVER_KEY: {"librenms_url": librenms_server.url, "api_token": "test-token"},
            "secondary": {"librenms_url": librenms_server.url, "api_token": "test-token"},
        },
    )
    seed_stored_mapping(owner, {SERVER_KEY: {"id": LIBRENMS_ID}, "secondary": {"id": LIBRENMS_ID + 1}}, save=True)
    page_query = {**PAGE_QUERY, "interface_name_field": "ifDescr", "cables_page": "3", "cables_per_page": "25"}

    response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[owner.pk]), page_query)

    assert response.status_code == 200
    (url,) = [hop["url"] for hop in _hops(response.content.decode(), "librenms-server-selector").urls]
    query = _query(url)
    assert query["server_key"] == ["secondary"]
    kept = ("tab", "interfaces_per_page", "interfaces_sort", "cables_per_page")
    _assert_keeps(query, kept, url, page_query)
    assert set(query) == {*kept, "server_key"}, url


def _signatures(client, owner, field):
    """Return the selection signature of each tab table that the full sync page renders, by table ID."""
    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[owner.pk]),
        {"server_key": SERVER_KEY, "interface_name_field": field},
    )
    assert response.status_code == 200
    return dict(re.findall(r'<table[^>]*id="([^"]+)"[^>]*data-filter-signature="([^"]*)"', response.content.decode()))


@pytest.mark.django_db
def test_every_tab_signs_its_selection_with_the_interface_name_field(client, settings, librenms_server):
    """A name field change is a new selection on every tab, so no tab can keep rows that its filter now drops."""
    bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
    client.force_login(make_superuser("signature-user"))
    # The IP rows come from a refresh against the loopback LibreNMS, which serves the device as ID 42.
    owner = make_device("signature-device", librenms_cf={SERVER_KEY: {"id": 42}})
    _seed(owner, _contract_ports())
    cache.set(
        SyncVLANsView().get_cache_key(owner, "vlans", SERVER_KEY),
        [{"vlan_vlan": 100, "vlan_name": "v100", "vlan_type": "ethernet", "vlan_state": 1}],
        timeout=300,
    )
    cache.set(
        object.__new__(SyncCablesView).get_cache_key(owner, "links", SERVER_KEY),
        {
            "links": [
                {
                    "_source": "main",
                    "local_port_id": 9601,
                    "local_port": "eth0",
                    "remote_device": "peer",
                    "remote_port": "xe-1",
                }
            ],
            "snapshot_token": "signature",
            "incomplete_sources": [],
        },
        timeout=300,
    )
    _refresh(
        client,
        owner,
        librenms_server,
        [{"address": "198.18.0.1", "prefix_length": 24, "port_id": 9100, "interface": "eth0"}],
    )

    signatures = {field: _signatures(client, owner, field) for field in ("ifName", "ifDescr")}

    tables = {"librenms-interface-table", "librenms-cable-table", "librenms-ipaddress-table", "librenms-vlan-table"}
    assert set(signatures["ifName"]) == tables
    for table in tables:
        assert signatures["ifName"][table] != signatures["ifDescr"][table], table
