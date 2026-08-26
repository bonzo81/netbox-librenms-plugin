"""Request-level tests for selecting a mapped server on object sync pages."""

import json
from copy import deepcopy
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.urls import reverse
from requests import Response

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.sync_cache import sync_snapshot_key
from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
)


def _configure_servers(settings):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "primary": {
            "display_name": "Primary LibreNMS",
            "librenms_url": "https://primary.example.com",
            "api_token": "test-token",
        },
        "secondary": {
            "display_name": "Secondary LibreNMS",
            "librenms_url": "https://secondary.example.com",
            "api_token": "test-token",
        },
    }
    settings.PLUGINS_CONFIG = plugin_config


def _json_response(url, payload, status=200):
    """Return a concrete response from the external LibreNMS HTTP boundary."""
    response = Response()
    response.status_code = status
    response.url = url
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    return response


def _device_info_response(url, device_id, name):
    return _json_response(
        url,
        {
            "status": "ok",
            "devices": [
                {
                    "device_id": device_id,
                    "sysName": name,
                    "hostname": name,
                    "hardware": "Test appliance",
                }
            ],
        },
    )


def _selector_html(html):
    """Return the rendered server selector region."""
    start = html.index('id="librenms-server-selector"')
    return html[start : html.index("</ul>", start)]


@pytest.mark.django_db
def test_device_with_one_non_default_mapping_redirects_and_queries_that_server(client, settings):
    """A sole mapped server becomes active before the first LibreNMS query."""
    _configure_servers(settings)
    device = make_device("one-mapped-server", librenms_cf={"secondary": {"id": 13401}})
    client.force_login(make_superuser("object-server-device-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    with patch("netbox_librenms_plugin.librenms_api.requests.get") as requests_get:
        redirect_response = client.get(url)

    requests_get.assert_not_called()
    assert redirect_response.status_code == 302
    assert redirect_response.url == f"{url}?server_key=secondary"

    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url == "https://secondary.example.com/api/v0/devices/13401":
            return _device_info_response(request_url, 13401, device.name)
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(redirect_response.url)

    assert response.status_code == 200
    assert requested_urls == ["https://secondary.example.com/api/v0/devices/13401"]
    assert b"Secondary LibreNMS" in response.content


@pytest.mark.django_db
def test_vm_with_one_non_default_mapping_redirects_and_queries_that_server(client, settings):
    """VM sync pages use the same object mapping selection as device pages."""
    _configure_servers(settings)
    vm = make_vm("one-mapped-vm")
    vm.custom_field_data["librenms_id"] = {"secondary": {"id": 13402}}
    vm.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("object-server-vm-user"))
    url = reverse("plugins:netbox_librenms_plugin:vm_librenms_sync", args=[vm.pk])

    with patch("netbox_librenms_plugin.librenms_api.requests.get") as requests_get:
        redirect_response = client.get(url)

    requests_get.assert_not_called()
    assert redirect_response.status_code == 302
    assert redirect_response.url == f"{url}?server_key=secondary"

    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url == "https://secondary.example.com/api/v0/devices/13402":
            return _device_info_response(request_url, 13402, vm.name)
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(redirect_response.url)

    assert response.status_code == 200
    assert requested_urls == ["https://secondary.example.com/api/v0/devices/13402"]


@pytest.mark.django_db
def test_virtual_chassis_member_uses_the_mapping_owners_server(client, settings):
    """A VC member resolves the server and ID from the member that owns the mapping."""
    _configure_servers(settings)
    _virtual_chassis, (mapping_owner, viewed_member) = make_virtual_chassis_members("object-server", count=2)
    mapping_owner.custom_field_data["librenms_id"] = {"secondary": {"id": 13403}}
    mapping_owner.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("object-server-vc-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[viewed_member.pk])

    with patch("netbox_librenms_plugin.librenms_api.requests.get") as requests_get:
        redirect_response = client.get(url)

    requests_get.assert_not_called()
    assert redirect_response.status_code == 302
    assert redirect_response.url == f"{url}?server_key=secondary"

    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url == "https://secondary.example.com/api/v0/devices/13403":
            return _device_info_response(request_url, 13403, viewed_member.name)
        if request_url == "https://secondary.example.com/api/v0/inventory/13403/all":
            return _json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(redirect_response.url)

    assert response.status_code == 200
    assert requested_urls == [
        "https://secondary.example.com/api/v0/devices/13403",
        "https://secondary.example.com/api/v0/inventory/13403/all",
    ]
    assert b"ID 13403" in response.content


@pytest.mark.django_db
def test_explicit_mapping_is_active_and_server_switch_keeps_only_the_tab(client, settings):
    """The selector and page controls carry the explicit mapped server."""
    _configure_servers(settings)
    device = make_device(
        "explicit-mapped-server",
        librenms_cf={"primary": {"id": 13404}, "secondary": {"id": 13405}},
    )
    client.force_login(make_superuser("object-server-explicit-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/13405":
            return _device_info_response(request_url, 13405, device.name)
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(
            url,
            {
                "tab": "ipaddresses",
                "server_key": "secondary",
                "interfaces_page": "3",
                "interface_name_field": "ifAlias",
            },
        )

    assert response.status_code == 200
    html = response.content.decode()
    assert 'id="librenms-server-selector"' in html
    assert 'data-active-server-key="secondary"' in html
    assert f'href="{url}?tab=ipaddresses&amp;server_key=primary"' in html
    assert "interfaces_page" not in _selector_html(html)
    assert '<input type="hidden" name="server_key" value="secondary">' in html
    assert f"{url}?tab=interfaces&amp;server_key=secondary" in html


@pytest.mark.django_db
def test_configured_but_unmapped_server_fails_closed_without_discovery(client, settings):
    """A configured server cannot query or create identity without an object mapping."""
    _configure_servers(settings)
    device = make_device("configured-unmapped-server", librenms_cf={"secondary": {"id": 13406}})
    client.force_login(make_superuser("object-server-unmapped-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("The configured but unmapped server contacted LibreNMS"),
    ) as requests_get:
        response = client.get(url, {"server_key": "primary", "tab": "interfaces"})

    requests_get.assert_not_called()
    assert response.status_code == 200
    assert b"is not an available mapping for this object" in response.content
    assert b'id="add-device-modal"' not in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": {"id": 13406}}


@pytest.mark.django_db
def test_unconfigured_mapping_is_removable_but_not_selectable(client, settings):
    """A stale mapping remains visible for cleanup and cannot become an API target."""
    _configure_servers(settings)
    device = make_device("stale-server-mapping", librenms_cf={"retired": {"id": 13407}})
    client.force_login(make_superuser("object-server-stale-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("The unconfigured server contacted LibreNMS"),
    ) as requests_get:
        response = client.get(url, {"server_key": "retired"})

    requests_get.assert_not_called()
    assert response.status_code == 200
    html = response.content.decode()
    assert "LibreNMS server &#x27;retired&#x27; is not an available mapping" in html
    assert "Not configured" in html
    assert 'name="server_key" value="retired"' in html
    selector = _selector_html(html)
    assert '<span class="dropdown-item disabled"' in selector
    assert "server_key=retired" not in selector


@pytest.mark.django_db
def test_multiple_non_default_mappings_require_selection_without_querying(client, settings):
    """Ambiguous mappings do not silently fall back to an unmapped installation default."""
    _configure_servers(settings)
    settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"]["tertiary"] = {
        "display_name": "Tertiary LibreNMS",
        "librenms_url": "https://tertiary.example.com",
        "api_token": "test-token",
    }
    device = make_device(
        "ambiguous-server-mappings",
        librenms_cf={"secondary": {"id": 13408}, "tertiary": {"id": 13409}},
    )
    client.force_login(make_superuser("object-server-ambiguous-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("An ambiguous server selection contacted LibreNMS"),
    ) as requests_get:
        response = client.get(url, {"tab": "modules"})

    requests_get.assert_not_called()
    assert response.status_code == 200
    html = response.content.decode()
    assert b"Select a LibreNMS server to continue" in response.content
    selector = _selector_html(html)
    assert f'href="{url}?tab=modules&amp;server_key=secondary"' in selector
    assert f'href="{url}?tab=modules&amp;server_key=tertiary"' in selector


@pytest.mark.django_db
def test_mapped_installation_default_wins_when_several_mappings_exist(client, settings):
    """A mapped installation default resolves ambiguity without adding a URL key."""
    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    device = make_device(
        "mapped-installation-default",
        librenms_cf={"primary": {"id": 13410}, "secondary": {"id": 13411}},
    )
    client.force_login(make_superuser("object-server-default-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url == "https://primary.example.com/api/v0/devices/13410":
            return _device_info_response(request_url, 13410, device.name)
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(url)

    assert response.status_code == 200
    assert response.request["QUERY_STRING"] == ""
    assert requested_urls == ["https://primary.example.com/api/v0/devices/13410"]
    assert 'data-active-server-key="primary"' in response.content.decode()


@pytest.mark.django_db
def test_valid_object_preference_beats_default_and_is_not_rendered_as_mapping(client, settings):
    """Read-only preference metadata selects its mapping and stays out of mapping rows."""
    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    device = make_device(
        "preferred-object-server",
        librenms_cf={
            "primary": {"id": 13412},
            "secondary": {"id": 13413},
            "_preferred_server": "secondary",
        },
    )
    client.force_login(make_superuser("object-server-preference-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    with patch("netbox_librenms_plugin.librenms_api.requests.get") as requests_get:
        redirect_response = client.get(url, {"tab": "cables"})

    requests_get.assert_not_called()
    assert redirect_response.status_code == 302
    assert redirect_response.url == f"{url}?tab=cables&server_key=secondary"

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/13413":
            return _device_info_response(request_url, 13413, device.name)
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(redirect_response.url)

    assert response.status_code == 200
    html = response.content.decode()
    assert 'data-active-server-key="secondary"' in html
    assert "_preferred_server" not in html
    assert "ID 13412" in html
    assert "ID 13413" in html


@pytest.mark.django_db
def test_legacy_integer_mapping_keeps_the_installation_default_workflow(client, settings):
    """Legacy object identity remains scoped to the installation default server."""
    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    device = make_device("legacy-object-server", librenms_cf=13414)
    client.force_login(make_superuser("object-server-legacy-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url == "https://primary.example.com/api/v0/devices/13414":
            return _device_info_response(request_url, 13414, device.name)
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(url)

    assert response.status_code == 200
    assert response.request["QUERY_STRING"] == ""
    assert requested_urls == ["https://primary.example.com/api/v0/devices/13414"]
    assert b"Active LibreNMS Server" in response.content


@pytest.mark.django_db
def test_unavailable_explicit_server_stays_active_without_fallback(client, settings):
    """A live lookup failure never changes the selected object mapping."""
    _configure_servers(settings)
    device = make_device(
        "unavailable-object-server",
        librenms_cf={"primary": {"id": 13415}, "secondary": {"id": 13416}},
    )
    client.force_login(make_superuser("object-server-unavailable-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])
    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url == "https://secondary.example.com/api/v0/devices/13416":
            return _json_response(
                request_url,
                {"status": "error", "message": "Service unavailable"},
                status=503,
            )
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(url, {"server_key": "secondary"})

    assert response.status_code == 200
    assert requested_urls == ["https://secondary.example.com/api/v0/devices/13416"]
    html = response.content.decode()
    assert 'data-active-server-key="secondary"' in html
    assert "https://primary.example.com/api/" not in html
    assert b"Details unavailable" in response.content


@pytest.mark.django_db
def test_interface_paginator_keeps_the_active_server(client, settings):
    """Following a table page link stays in the active server namespace."""
    _configure_servers(settings)
    device = make_device("server-scoped-paginator", librenms_cf={"secondary": {"id": 13417}})
    ports = [
        {
            "port_id": number,
            "ifName": f"Ethernet{number}",
            "ifDescr": f"Ethernet{number}",
            "ifAlias": "",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 1_000_000_000,
            "ifPhysAddress": "",
            "ifMtu": 1500,
            "ifAdminStatus": "up",
        }
        for number in range(1, 56)
    ]
    cache.set(sync_snapshot_key(device, "ports", "secondary"), {"ports": ports}, timeout=300)
    client.force_login(make_superuser("object-server-paginator-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/13417":
            return _device_info_response(request_url, 13417, device.name)
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(
            url,
            {"server_key": "secondary", "tab": "interfaces", "interfaces_per_page": "25"},
        )

    assert response.status_code == 200
    html = response.content.decode()
    assert "interfaces_page=2" in html
    paginator_start = html.index('aria-label="Page selection"')
    paginator_end = html.index("</nav>", paginator_start)
    paginator = html[paginator_start:paginator_end]
    assert "server_key=secondary" in paginator


@pytest.mark.django_db
def test_add_device_forms_carry_the_installation_default_server(client, settings):
    """Adding an unmapped object cannot drift from the server used for discovery."""
    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    device = make_device("add-on-active-server")
    client.force_login(make_superuser("object-server-add-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    def librenms_response(request_url, **_kwargs):
        if request_url == f"https://primary.example.com/api/v0/devices/{device.name}":
            return _json_response(
                request_url,
                {"status": "error", "message": "Device not found"},
                status=404,
            )
        if request_url == "https://primary.example.com/api/v0/poller_group":
            return _json_response(request_url, {"status": "ok", "get_poller_group": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(url)

    assert response.status_code == 200
    html = response.content.decode()
    for form_id in ("snmpv1v2-form", "snmpv3-form"):
        start = html.index(f'id="{form_id}"')
        end = html.index("</form>", start)
        assert 'name="server_key" value="primary"' in html[start:end]


@pytest.mark.django_db
def test_fallback_mapping_rows_render_the_active_server_the_same_way(settings):
    """Both producers of all_server_mappings feed one template, so their rows carry one shape."""
    from django.template.loader import render_to_string

    from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

    _configure_servers(settings)
    device = make_device("fallback-selector-device", librenms_cf={"primary": 51001, "secondary": 51002})

    rows = BaseLibreNMSSyncView._build_all_server_mappings(device, "primary")

    assert rows, "no mapping row was built, so the render below would assert nothing"
    markup = render_to_string(
        "netbox_librenms_plugin/inc/_server_selector.html",
        {"all_server_mappings": rows, "server_key": "primary"},
    )

    assert "dropdown-item active" in markup
    assert "dropdown-item disabled" not in markup
