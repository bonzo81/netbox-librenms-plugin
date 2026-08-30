"""A failed LibreNMS lookup must say which failure it was.

A device that LibreNMS does not have, a device LibreNMS errors on, and a server that cannot be
reached are three different problems with three different fixes. Reporting all of them as
"device not found" sends the user to remove a custom field that is correct.
"""

from html import unescape

import pytest
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse
from ipam.models import IPAddress

from netbox_librenms_plugin.sync_cache import TAB_SPECS, SyncTab, sync_snapshot_key
from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    make_device,
    make_interface,
    make_superuser,
)

ABSENT_DEVICE_ID = 4041
ERRORING_DEVICE_ID = 1255
CONFLICTING_DEVICE_ID = 1266


def _point_plugin_at(settings, url):
    """Configure one server and return the key, so no test hardcodes an environment's key."""
    configure_librenms_servers(
        settings, {"default": {"librenms_url": url, "api_token": "test-token", "verify_ssl": False}}
    )
    return "default"


def _api_for(server_key):
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    return LibreNMSAPI(server_key=server_key)


@pytest.mark.django_db
def test_a_missing_device_is_still_reported_as_missing(librenms_server, settings):
    """The existing contract: LibreNMS answering 404 means the device is genuinely absent."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    librenms_server.register(
        f"/api/v0/devices/{ABSENT_DEVICE_ID}",
        {"status": "error", "message": "Device does not exist"},
        status=404,
    )

    assert _api_for(server_key).get_device_info(ABSENT_DEVICE_ID) == (False, None)


@pytest.mark.django_db
def test_a_server_error_is_not_reported_as_a_missing_device(librenms_server, settings):
    """LibreNMS answering 500 says nothing about whether the device exists."""
    from netbox_librenms_plugin.librenms_api import LibreNMSLookupError

    server_key = _point_plugin_at(settings, librenms_server.url)
    librenms_server.register(
        f"/api/v0/devices/{ERRORING_DEVICE_ID}",
        {"message": "Server Error"},
        status=500,
    )

    success, failure = _api_for(server_key).get_device_info(ERRORING_DEVICE_ID)

    assert success is False
    assert isinstance(failure, LibreNMSLookupError)
    assert failure.status_code == 500


@pytest.mark.django_db
def test_an_unreachable_server_is_not_reported_as_a_missing_device(librenms_server, settings):
    """Nothing answered at all, so the device's existence is unknown rather than denied."""
    from netbox_librenms_plugin.librenms_api import LibreNMSLookupError

    # Take a port that was really bound, then stop listening on it, so the refusal is not a
    # guess about which ports happen to be free.
    server_key = _point_plugin_at(settings, librenms_server.url)
    librenms_server.stop()

    success, failure = _api_for(server_key).get_device_info(ERRORING_DEVICE_ID)

    assert success is False
    assert isinstance(failure, LibreNMSLookupError)
    assert failure.status_code is None, "an HTTP answer arrived, so this did not test a transport failure"


@pytest.mark.django_db
def test_the_sync_page_does_not_tell_the_user_to_clear_a_correct_custom_field(client, librenms_server, settings):
    """End to end: the page must not blame the custom field for a LibreNMS server error."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    librenms_server.register(
        f"/api/v0/devices/{ERRORING_DEVICE_ID}",
        {"message": "Server Error"},
        status=500,
    )
    device = make_device("librenms-500-device", librenms_cf={server_key: ERRORING_DEVICE_ID})
    client.force_login(make_superuser("librenms-500-user"))

    response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]))
    body = response.content.decode()

    assert response.status_code == 200
    assert "LibreNMS lookup failed" in body
    assert str(ERRORING_DEVICE_ID) in body
    # Every place the page could still call the device missing, not just the banner.
    assert "Device not found" not in body, "a server error was reported as a missing device"
    assert "Remove the custom field value" not in body, "the page told the user to clear a correct value"
    assert "Not found" not in body, "the status row still called the device missing"
    # The control itself, not the phrase: the phrase also appears in a template comment.
    assert 'data-bs-target="#add-device-modal"' not in body, "the page offered to add a device it could not look up"
    assert 'id="add-device-modal"' not in body, "the add-device modal was rendered for a failed lookup"


@pytest.mark.django_db
def test_the_sync_page_still_reports_a_genuinely_missing_device(client, librenms_server, settings):
    """The 404 path must keep its message, including the advice to clear the custom field."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    librenms_server.register(
        f"/api/v0/devices/{ABSENT_DEVICE_ID}",
        {"status": "error", "message": "Device does not exist"},
        status=404,
    )
    device = make_device("librenms-404-device", librenms_cf={server_key: ABSENT_DEVICE_ID})
    client.force_login(make_superuser("librenms-404-user"))

    response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]))
    body = response.content.decode()

    assert response.status_code == 200
    assert "Device not found" in body
    assert "Remove the custom field value" in body


@pytest.mark.django_db
def test_the_sync_page_reports_a_discovered_id_conflict(client, librenms_server, settings):
    """A discovery conflict must render its owner instead of raising a server error."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    owner = make_device("librenms-conflict-owner", librenms_cf={server_key: CONFLICTING_DEVICE_ID})
    target = make_device("librenms-conflict-target.example.com", librenms_cf={server_key: None})
    librenms_server.register(
        f"/api/v0/devices/{target.name}",
        {"status": "ok", "devices": [{"device_id": CONFLICTING_DEVICE_ID}]},
        method="GET",
    )
    client.force_login(make_superuser("librenms-conflict-page-user"))

    response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[target.pk]))
    body = unescape(response.content.decode())

    assert response.status_code == 200
    assert f"LibreNMS ID {CONFLICTING_DEVICE_ID} is already assigned to device '{owner.name}'" in body


@pytest.mark.django_db
def test_location_update_reports_a_discovered_id_conflict(client, librenms_server, settings):
    """A POST action must redirect with the discovery conflict and make no LibreNMS write."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    owner = make_device("librenms-action-conflict-owner", librenms_cf={server_key: CONFLICTING_DEVICE_ID})
    target = make_device("librenms-action-conflict-target.example.com", librenms_cf={server_key: None})
    librenms_server.register(
        f"/api/v0/devices/{target.name}",
        {"status": "ok", "devices": [{"device_id": CONFLICTING_DEVICE_ID}]},
        method="GET",
    )
    client.force_login(make_superuser("librenms-conflict-action-user"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:update_device_location", args=[target.pk]),
        {"server_key": server_key},
    )
    rendered_messages = [str(message) for message in get_messages(response.wsgi_request)]

    assert response.status_code == 302
    assert f"LibreNMS ID {CONFLICTING_DEVICE_ID} is already assigned to device '{owner.name}'" in rendered_messages


@pytest.mark.django_db
def test_device_status_reports_a_discovered_id_conflict(client, librenms_server, settings):
    """A status lookup must identify the conflicting owner instead of only showing unlinked."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    owner = make_device("librenms-status-conflict-owner", librenms_cf={server_key: CONFLICTING_DEVICE_ID})
    target = make_device("librenms-status-conflict-target.example.com", librenms_cf={server_key: None})
    librenms_server.register(
        f"/api/v0/devices/{target.name}",
        {"status": "ok", "devices": [{"device_id": CONFLICTING_DEVICE_ID}]},
        method="GET",
    )
    client.force_login(make_superuser("librenms-conflict-status-user"))

    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_status_list"),
        {"device": target.pk},
    )
    rendered_messages = [str(message) for message in get_messages(response.wsgi_request)]

    assert response.status_code == 200
    assert f"LibreNMS ID {CONFLICTING_DEVICE_ID} is already assigned to device '{owner.name}'" in rendered_messages


@pytest.mark.django_db
def test_primary_ip_sync_reports_a_discovered_id_conflict_without_writes(client, librenms_server, settings):
    """A management-ID conflict must abort the real IP sync transaction before its first write."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    owner = make_device("librenms-ip-conflict-owner", librenms_cf={server_key: CONFLICTING_DEVICE_ID})
    target = make_device("librenms-ip-conflict-target.example.com", librenms_cf={server_key: None})
    make_interface(target, "Ethernet1", iface_type="1000base-t")
    row_id = "198.18.20.10/24"
    port_id = 7020
    cache_key = sync_snapshot_key(target, TAB_SPECS[SyncTab.IP_ADDRESSES].data_type, server_key)
    cache.set(
        cache_key,
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.20.10",
                    "prefix_length": 24,
                    "ip_with_mask": row_id,
                    "port_id": port_id,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {port_id: {"port_id": port_id, "ifName": "Ethernet1", "ifDescr": "Ethernet1"}},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    librenms_server.register(
        f"/api/v0/devices/{target.name}",
        {"status": "ok", "devices": [{"device_id": CONFLICTING_DEVICE_ID}]},
        method="GET",
    )
    client.force_login(make_superuser("librenms-conflict-ip-user"))

    response = client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": "device", "pk": target.pk},
        ),
        {
            "server_key": server_key,
            "set-primary-ip-toggle": "on",
            "select": row_id,
            f"vrf_{row_id}": "",
        },
    )
    rendered_messages = [str(message) for message in get_messages(response.wsgi_request)]

    assert response.status_code == 302
    assert f"LibreNMS ID {CONFLICTING_DEVICE_ID} is already assigned to device '{owner.name}'" in rendered_messages
    assert not IPAddress.objects.filter(address=row_id).exists()
