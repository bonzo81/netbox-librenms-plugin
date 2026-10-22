"""
The navigation contract of the interfaces tab: every hop keeps the page query or applies a documented reset.

The test renders the real sync page with distinctive filter values (spaces, ``&``, ``+`` and
non-ASCII text), finds every link, form, ``formaction`` and ``hx-*`` URL in the interfaces pane,
and checks each one. A URL the test does not know fails, so a new hop must be classified here.
"""

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable
from netbox_librenms_plugin.tests.conftest import make_device
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


def _assert_keeps(query, keys, where):
    assert {key: query.get(key) for key in keys} == {key: [PAGE_QUERY[key]] for key in keys}, where


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
    if path == routes["refresh"]:
        # Refresh from LibreNMS: the filters stay, the page goes.
        assert "interfaces_page" not in query, url
        _assert_keeps(query, KEEP_ALL_BUT_PAGE, url)
        return "refresh"
    if path in (routes["sync"], routes["rebind"]):
        # A sync or Rebind comes back to the same filters and page.
        _assert_keeps(query, KEEP_ALL, url)
        return "rebind" if path == routes["rebind"] else "sync"
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
