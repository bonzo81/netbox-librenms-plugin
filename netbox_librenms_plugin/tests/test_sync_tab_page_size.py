"""Each sync tab remembers the page size the user picked, per tab, in the user's preferences."""

import re
from copy import deepcopy

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_device
from netbox_librenms_plugin.tests.test_interface_sync_htmx_swap import FIRST_PORT, _ports, _post, _sync_data
from netbox_librenms_plugin.tests.test_interface_sync_htmx_swap import _seed as _seed_ports
from netbox_librenms_plugin.tests.test_ip_address_sync_safety import _configure_test_server, _serve_librenms_ip_rows
from netbox_librenms_plugin.tests.test_sync_option_preferences import SERVER_KEY, _seed, _user
from netbox_librenms_plugin.utils import get_table_paginate_count

INTERFACES_PREFERENCE = "plugins.netbox_librenms_plugin.per_page.interfaces"
IP_PREFERENCE = "plugins.netbox_librenms_plugin.per_page.ipaddresses"
IP_ROWS = [
    {"address": f"198.18.42.{n}", "prefix_length": 24, "port_id": 7200 + n, "interface": f"Ethernet{n}"}
    for n in range(1, 4)
]

pytestmark = pytest.mark.django_db


def _stored(user, path):
    return get_user_model().objects.get(pk=user.pk).config.get(path)


def _set_netbox_page_size(user, size):
    """Change the user's NetBox page size the way the NetBox preferences form does."""
    config = get_user_model().objects.get(pk=user.pk).config
    config.data = deepcopy(config.data)
    config.set("pagination.per_page", size, commit=True)


def _paged_device(name):
    """Return a device with 30 interface rows, so a page size of 10 renders 3 pages."""
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": 42}})
    _seed_ports(device, _ports())
    return device


def _device(name):
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": 42}})
    _seed(device)
    return device


def _interfaces(client, device, **params):
    """Render the interfaces tab through the fragment the sync page loads."""
    response = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": "interfaces"},
        ),
        {"server_key": SERVER_KEY, "interface_name_field": "ifName", **params},
    )
    assert response.status_code == 200
    (size,) = re.findall(r'<input type="hidden" name="interfaces_per_page" value="(\d+)">', response.content.decode())
    return int(size)


def _ip_refresh(client, device, live_librenms, **params):
    """Refresh the IP tab through its real HTMX endpoint, which renders the table."""
    _serve_librenms_ip_rows(live_librenms.server, IP_ROWS, device_name=device.name)
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk]),
        {"server_key": SERVER_KEY, "interface_name_field": "ifName", **params},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    return response.content.decode()


@pytest.fixture(autouse=True)
def _server(settings):
    _configure_test_server(settings)


def test_a_chosen_page_size_is_kept_for_the_next_device(client):
    user = _user("page-size-next-device")
    client.force_login(user)
    default_size = _interfaces(client, _device("page-size-baseline"))

    assert _interfaces(client, _device("page-size-first"), interfaces_per_page="7") == 7
    assert _interfaces(client, _device("page-size-second")) == 7
    assert default_size != 7
    assert _stored(user, INTERFACES_PREFERENCE) == 7


def test_each_tab_keeps_its_own_page_size(client, live_librenms):
    user = _user("page-size-per-tab")
    client.force_login(user)
    device = _device("page-size-per-tab-device")
    assert _interfaces(client, device, interfaces_per_page="7") == 7

    # 9 is not a default page length, so the Per Page menu lists it only while it is the page size.
    assert "ipaddresses_per_page=9&" in _ip_refresh(client, device, live_librenms, ipaddresses_per_page="9")
    assert "ipaddresses_per_page=9&" in _ip_refresh(client, device, live_librenms)
    assert _interfaces(client, device) == 7
    assert (_stored(user, INTERFACES_PREFERENCE), _stored(user, IP_PREFERENCE)) == (7, 9)


@pytest.mark.parametrize("raw", ["0", "-3", "seven", "2.5", ""])
def test_an_invalid_page_size_is_not_saved(client, raw):
    user = _user(f"page-size-invalid-{raw or 'empty'}")
    client.force_login(user)
    device = _device(f"page-size-invalid-device-{raw or 'empty'}")

    default_size = _interfaces(client, device, interfaces_per_page=raw)
    assert _stored(user, INTERFACES_PREFERENCE) is None
    assert _interfaces(client, device, interfaces_per_page="7") == 7

    assert _interfaces(client, device, interfaces_per_page=raw) == 7
    assert _stored(user, INTERFACES_PREFERENCE) == 7
    assert default_size != 7


def test_a_page_size_above_the_ceiling_is_clamped_before_it_is_saved(client, settings):
    settings.MAX_PAGE_SIZE = 20
    user = _user("page-size-ceiling")
    client.force_login(user)
    device = _device("page-size-ceiling-device")
    _set_netbox_page_size(user, 10)

    assert _interfaces(client, device, interfaces_per_page="500") == 20
    assert _stored(user, INTERFACES_PREFERENCE) == 20


def test_a_saved_page_size_is_not_shown_to_another_user(client):
    client.force_login(_user("page-size-owner"))
    device = _device("page-size-owner-device")
    _interfaces(client, device, interfaces_per_page="7")

    client.force_login(_user("page-size-viewer"))

    assert _interfaces(client, device) != 7


def test_an_anonymous_request_gets_the_page_size_it_asked_for():
    """The sync tabs require a login, so an anonymous request only reaches the helper directly."""
    anonymous = RequestFactory().get("/", {"interfaces_per_page": "7"})
    anonymous.user = AnonymousUser()

    assert get_table_paginate_count(anonymous, "interfaces_") == 7


def test_a_page_link_that_carries_the_rendered_size_saves_nothing(client):
    """Each page link carries the rendered size, so paging does not pin NetBox's page size on the tab."""
    user = _user("page-size-paging")
    client.force_login(user)
    device = _paged_device("page-size-paging-device")
    _set_netbox_page_size(user, 10)
    assert _interfaces(client, device) == 10

    assert _interfaces(client, device, interfaces_page="2", interfaces_per_page="10") == 10
    assert _stored(user, INTERFACES_PREFERENCE) is None

    _set_netbox_page_size(user, 25)
    assert _interfaces(client, device) == 25


def test_a_sync_post_that_carries_the_rendered_size_saves_nothing(client):
    user = _user("page-size-sync-post")
    client.force_login(user)
    device = _paged_device("page-size-sync-post-device")
    _set_netbox_page_size(user, 10)

    response = _post(client, device, _sync_data(FIRST_PORT + 12, page=2, per_page=10))

    assert response.status_code == 200
    assert "Showing 11-20 of 30" in response.content.decode()
    assert _stored(user, INTERFACES_PREFERENCE) is None


def test_selecting_the_netbox_page_size_after_another_size_is_saved(client):
    """The selected size is kept for the tab, also when it is equal to the NetBox page size at that time."""
    user = _user("page-size-back-to-netbox")
    client.force_login(user)
    device = _paged_device("page-size-back-to-netbox-device")
    _set_netbox_page_size(user, 10)
    assert _interfaces(client, device, interfaces_per_page="7") == 7

    assert _interfaces(client, device, interfaces_per_page="10") == 10
    assert _stored(user, INTERFACES_PREFERENCE) == 10

    _set_netbox_page_size(user, 25)
    assert _interfaces(client, device) == 10
