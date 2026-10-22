"""
The tab filter endpoint: a filter form swaps only its tab, and the response updates the rest of the page.

Every test sends a real request to the endpoint, renders the real tab from a seeded snapshot, and reads
the response as htmx does: the tab content, the out-of-band tab links and Refresh button, and the
``HX-Push-Url`` header.
"""

import re
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.tests.test_interface_sync_filters import (
    SERVER_KEY,
    _mixed_device,
    _rows,
    _superuser_client,
    _virtual_machine,
)
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

pytestmark = pytest.mark.django_db

QUERY = {"tab": "interfaces", "server_key": SERVER_KEY, "interfaces_q": "eth1", "cables_q": "keep"}


def _filter(client, owner, object_type="device", tab="interfaces", query=QUERY):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_tab_filter",
        kwargs={"object_type": object_type, "pk": owner.pk, "tab": tab},
    )
    return client.get(f"{url}?{urlencode(query)}", HTTP_HX_REQUEST="true")


def _out_of_band(html, element_id):
    match = re.search(rf'<(\w+) id="{element_id}" hx-swap-oob="innerHTML">(.*?)</\1>', html, re.S)
    assert match, element_id
    return match.group(2)


def _query(url):
    return parse_qs(urlsplit(unescape(url)).query)


def test_a_filter_answers_with_its_tab_the_tab_links_and_the_refresh_button(client, settings):
    client = _superuser_client(client, settings, "tab-filter")
    device = _mixed_device("tab-filter")

    response = _filter(client, device)

    assert response.status_code == 200
    html = response.content.decode()
    assert list(_rows(html)) == [1002]
    page_path = reverse("dcim:device_librenms_sync", args=[device.pk])
    assert response["HX-Push-Url"] == f"{page_path}?{urlencode(QUERY)}"
    # Every tab link opens its tab with the new filters; the Refresh button keeps them.
    links = re.findall(r'hx-get="([^"]+)"', _out_of_band(html, "librenmsSync"))
    assert {_query(link)["tab"][0] for link in links} == {"interfaces", "cables", "ipaddresses", "vlans", "modules"}
    assert all(urlsplit(unescape(link)).path == page_path for link in links)
    assert all(_query(link)["interfaces_q"] == ["eth1"] and _query(link)["cables_q"] == ["keep"] for link in links)
    (refresh,) = re.findall(r'hx-post="([^"]+)"', _out_of_band(html, "interfaces-refresh-action"))
    assert _query(refresh)["interfaces_q"] == ["eth1"]


def test_a_virtual_machine_filter_pushes_the_virtual_machine_page(client, settings):
    client = _superuser_client(client, settings, "tab-filter-vm")
    vm = _virtual_machine("tab-filter-vm")

    response = _filter(client, vm, "virtualmachine", query={**QUERY, "interfaces_q": "ens4"})

    assert response.status_code == 200
    assert list(_rows(response.content.decode())) == [3002]
    assert urlsplit(response["HX-Push-Url"]).path == reverse(
        "virtualization:virtualmachine_librenms_sync", args=[vm.pk]
    )
    links = re.findall(r'hx-get="([^"]+)"', _out_of_band(response.content.decode(), "librenmsSync"))
    assert {_query(link)["tab"][0] for link in links} == {"interfaces", "ipaddresses"}


def test_a_tab_with_no_snapshot_answers_with_its_empty_state(client, settings):
    client = _superuser_client(client, settings, "tab-filter-empty")
    device = _mixed_device("tab-filter-empty")
    cache.delete(SyncInterfacesView().get_cache_key(device, "ports", SERVER_KEY))

    response = _filter(client, device)

    assert response.status_code == 200
    html = response.content.decode()
    assert "No interface data loaded" in html
    assert 'id="id_interfaces_q"' not in html


def test_the_modules_tab_has_no_filter(client, settings):
    client = _superuser_client(client, settings, "tab-filter-modules")
    device = _mixed_device("tab-filter-modules")

    assert _filter(client, device, tab="modules").status_code == 404


def test_a_user_who_cannot_view_the_device_gets_no_rows(client, settings):
    _superuser_client(client, settings, "tab-filter-denied")
    device = _mixed_device("tab-filter-denied")
    client.force_login(get_user_model().objects.create(username="tab-filter-no-grant"))

    response = _filter(client, device)

    assert response.status_code in (403, 404)
    assert "eth1" not in response.content.decode()
