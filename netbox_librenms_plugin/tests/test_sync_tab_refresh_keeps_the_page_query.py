"""
A refresh or sync of any tab keeps the page query, so the next page link keeps every tab's filters.

Each test renders the real sync page with an Interfaces filter, takes the Refresh URL that the
rendered button posts to, refreshes the tab from a loopback LibreNMS, and follows the page links
and the sync form of the answer.
"""

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import bind_librenms_server, make_device, make_superuser
from netbox_librenms_plugin.tests.test_interface_filter_navigation_contract import _hops
from netbox_librenms_plugin.tests.test_interface_sync_filters import _port
from netbox_librenms_plugin.tests.test_interface_sync_filters import _seed as _seed_interfaces
from netbox_librenms_plugin.tests.test_ip_address_sync_safety import _serve_librenms_ip_rows

SERVER_KEY = "default"
DEVICE_ID = 42
ROWS = 8
PAGE_QUERY = {"server_key": SERVER_KEY, "interfaces_q": "eth  a&b+c é", "interfaces_page": "2"}


def _serve_cables(server, device):
    links = [
        {
            "local_port_id": 7300 + index,
            "local_port": f"eth{index}",
            "remote_port_id": 7400 + index,
            "remote_port": f"Ethernet{index}",
            "remote_hostname": f"remote-{index}",
            "remote_device_id": 900 + index,
        }
        for index in range(ROWS)
    ]
    server.register(f"/api/v0/devices/{DEVICE_ID}/links", {"status": "ok", "links": links})
    ports = [{"port_id": 7300 + index, "ifName": f"eth{index}", "ifDescr": f"eth{index}"} for index in range(ROWS)]
    server.register(f"/api/v0/devices/{DEVICE_ID}/ports", {"status": "ok", "ports": ports})


def _serve_ip_addresses(server, device):
    rows = [
        {"address": f"198.18.7.{index + 1}", "prefix_length": 24, "port_id": 7500 + index, "interface": f"eth{index}"}
        for index in range(ROWS)
    ]
    _serve_librenms_ip_rows(server, rows, device_name=device.name)


def _serve_vlans(server, device):
    vlans = [{"device_id": DEVICE_ID, "vlan_vlan": 100 + index, "vlan_name": f"vlan{index}"} for index in range(ROWS)]
    server.register("/api/v0/resources/vlans", {"status": "ok", "vlans": vlans})


TABS = {
    "cables": ("device_cable_sync", _serve_cables),
    "ipaddresses": ("device_ipaddress_sync", _serve_ip_addresses),
    "vlans": ("device_vlan_sync", _serve_vlans),
}


def _query(url):
    return parse_qs(urlsplit(url).query)


def _assert_keeps_the_page_query(url):
    query = _query(url)
    assert {key: query.get(key) for key in PAGE_QUERY} == {key: [value] for key, value in PAGE_QUERY.items()}, url


@pytest.mark.django_db
@pytest.mark.parametrize("tab", sorted(TABS))
def test_a_refresh_keeps_the_other_tabs_filters_on_its_page_links(client, settings, librenms_server, tab):
    refresh_name, serve = TABS[tab]
    bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
    device = make_device(f"refresh-query-{tab}", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
    librenms_server.register(
        f"/api/v0/devices/{DEVICE_ID}",
        {"status": "ok", "devices": [{"device_id": DEVICE_ID, "sysName": device.name, "hostname": device.name}]},
    )
    serve(librenms_server, device)
    client.force_login(make_superuser("refresh-query-user"))
    page_path = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])
    page = client.get(page_path, {"tab": tab, **PAGE_QUERY, f"{tab}_per_page": "1"})
    assert page.status_code == 200

    refresh_path = reverse(f"plugins:netbox_librenms_plugin:{refresh_name}", kwargs={"pk": device.pk})
    (refresh_url,) = [
        hop["url"]
        for hop in _hops(page.content.decode(), tab).urls
        if hop["tag"] == "button" and urlsplit(hop["url"]).path == refresh_path
    ]
    _assert_keeps_the_page_query(refresh_url)

    response = client.post(
        refresh_url, {"server_key": SERVER_KEY, "interface_name_field": "ifName"}, HTTP_HX_REQUEST="true"
    )

    assert response.status_code == 200
    hops = _hops(f'<div id="partial">{response.content.decode()}</div>', "partial").urls
    page_links = [hop["url"] for hop in hops if "page-link" in (hop["attrs"].get("class") or "")]
    assert len(page_links) >= 2
    for url in page_links:
        _assert_keeps_the_page_query(url)
    sync_forms = [hop["url"] for hop in hops if hop["tag"] == "form" and hop["attrs"].get("method") == "post"]
    assert sync_forms
    for url in sync_forms:
        _assert_keeps_the_page_query(url)


def _interface_page(client, settings, librenms_server, tag, query):
    """Render the Interfaces tab with *query* and return the device and the Refresh URL its button posts to."""
    bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
    device = make_device(f"refresh-page-{tag}", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
    librenms_server.register(
        f"/api/v0/devices/{DEVICE_ID}",
        {"status": "ok", "devices": [{"device_id": DEVICE_ID, "sysName": device.name, "os": "linux"}]},
    )
    ports = [
        {**_port(6000 + index, f"eth{index:02d}"), "ifAlias": "uplink" if index % 2 else "spare"} for index in range(40)
    ]
    librenms_server.register(f"/api/v0/devices/{DEVICE_ID}/ports", {"status": "ok", "ports": ports})
    librenms_server.register(f"/api/v0/devices/{DEVICE_ID}/port_stack", {"status": "ok", "port_stack": []})
    _seed_interfaces(device, ports)
    client.force_login(make_superuser("refresh-page-user"))
    page_path = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])
    page = client.get(page_path, query)
    assert page.status_code == 200
    refresh_path = reverse("plugins:netbox_librenms_plugin:device_interface_sync", kwargs={"pk": device.pk})
    (refresh_url,) = [
        hop["url"]
        for hop in _hops(page.content.decode(), "interfaces").urls
        if hop["tag"] == "button" and urlsplit(hop["url"]).path == refresh_path
    ]
    return device, refresh_url


def _rows_and_count(html):
    rows = [int(port_id) for port_id in re.findall(r'<tr[^>]*data-port-id="(\d+)"', html)]
    return rows, re.search(r"Showing (\d+)-(\d+) of (\d+)", html).groups()


@pytest.mark.django_db
@pytest.mark.parametrize(("page", "shown"), [("2", ("6", "10", "20")), ("9", ("11", "20", "20"))])
def test_a_refresh_keeps_the_filtered_page_and_a_cache_restore_shows_the_same_page(
    client, settings, librenms_server, page, shown
):
    query = {"tab": "interfaces", "server_key": SERVER_KEY, "interfaces_q": "uplink", "interfaces_per_page": "5"}
    query["interfaces_page"] = page
    device, refresh_url = _interface_page(client, settings, librenms_server, f"page-{page}", query)
    assert _query(refresh_url)["interfaces_page"] == [page]

    refreshed = client.post(refresh_url, {"server_key": SERVER_KEY}, HTTP_HX_REQUEST="true")

    assert refreshed.status_code == 200
    rows, count = _rows_and_count(refreshed.content.decode())
    # A page past the end is clamped to the last page (NetBox folds 5 orphan rows into it), as after a sync.
    assert count == shown
    # The browser URL still holds the page, so a cache restore of the same query shows the same rows.
    fragment = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": "interfaces"},
        ),
        query,
        HTTP_HX_REQUEST="true",
    )
    assert fragment.status_code == 200
    assert _rows_and_count(fragment.content.decode()) == (rows, count)
