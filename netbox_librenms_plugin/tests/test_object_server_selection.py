"""Request-level coverage for object-scoped LibreNMS server selection."""

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.sync_cache import sync_snapshot_key
from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
)
from netbox_librenms_plugin.tests.import_server_helpers import selector_html
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


def _server_config(server, display_name):
    return {
        "display_name": display_name,
        "librenms_url": server.url,
        "api_token": "object-server-test-token",
        "cache_timeout": 300,
        "verify_ssl": False,
    }


def _configure_servers(settings, **servers):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        key: _server_config(server, display_name) for key, (server, display_name) in servers.items()
    }
    plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def servers(settings, monkeypatch):
    """Provide two configured LibreNMS servers over real loopback HTTP."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with ExitStack() as stack:
        primary = stack.enter_context(librenms_mock_server())
        secondary = stack.enter_context(librenms_mock_server())
        _configure_servers(
            settings,
            primary=(primary, "Primary LibreNMS"),
            secondary=(secondary, "Secondary LibreNMS"),
        )
        yield SimpleNamespace(primary=primary, secondary=secondary)


def _register_device(server, device_id, name, observed=None, *, status=200, payload=None):
    """Register one device lookup and optionally record the real request."""
    if payload is None:
        payload = {
            "status": "ok",
            "devices": [
                {
                    "device_id": device_id,
                    "sysName": name,
                    "hostname": name,
                    "hardware": "Test appliance",
                }
            ],
        }

    def response(**request):
        if observed is not None:
            observed.append(request)
        return status, payload

    server.register(f"/api/v0/devices/{device_id}", response)


def _sync_url(obj):
    name = "vm_librenms_sync" if obj._meta.model_name == "virtualmachine" else "device_librenms_sync"
    return reverse(f"plugins:netbox_librenms_plugin:{name}", args=[obj.pk])


@pytest.mark.django_db
@pytest.mark.parametrize("object_kind", ["device", "virtualmachine"])
def test_single_non_default_mapping_redirects_before_query_and_then_uses_that_server(
    client,
    servers,
    object_kind,
):
    from dcim.models import Device

    if object_kind == "virtualmachine":
        obj = make_vm("one-mapped-vm")
        obj.custom_field_data["librenms_id"] = {"secondary": {"id": 13402}}
        obj.save(update_fields=["custom_field_data"])
    else:
        obj = make_device("one-mapped-server", librenms_cf={"secondary": {"id": 13401}})
    assert Device.objects.filter(name="one-mapped-server").exists() is (object_kind == "device")
    device_id = 13401 if object_kind == "device" else 13402
    observed = []
    _register_device(servers.secondary, device_id, obj.name, observed)
    client.force_login(make_superuser(f"single-mapping-{object_kind}-user"))
    url = _sync_url(obj)

    redirect_response = client.get(url)

    assert observed == []
    assert redirect_response.status_code == 302
    assert redirect_response.url == f"{url}?server_key=secondary"

    response = client.get(redirect_response.url)

    assert response.status_code == 200
    assert [(request["method"], request["path"]) for request in observed] == [("GET", f"/api/v0/devices/{device_id}")]
    assert b"Secondary LibreNMS" in response.content


@pytest.mark.django_db
def test_virtual_chassis_member_uses_the_mapping_owners_server(client, servers):
    _virtual_chassis, (mapping_owner, viewed_member) = make_virtual_chassis_members("object-server", count=2)
    mapping_owner.custom_field_data["librenms_id"] = {"secondary": {"id": 13403}}
    mapping_owner.save(update_fields=["custom_field_data"])
    observed = []
    _register_device(servers.secondary, 13403, viewed_member.name, observed)

    def inventory(**request):
        observed.append(request)
        return 200, {"status": "ok", "inventory": []}

    servers.secondary.register("/api/v0/inventory/13403/all", inventory)
    client.force_login(make_superuser("object-server-vc-user"))
    url = _sync_url(viewed_member)

    redirect_response = client.get(url)

    assert observed == []
    assert redirect_response.url == f"{url}?server_key=secondary"

    response = client.get(redirect_response.url)

    assert response.status_code == 200
    assert [request["path"] for request in observed] == [
        "/api/v0/devices/13403",
        "/api/v0/inventory/13403/all",
    ]
    assert b"ID 13403" in response.content


@pytest.mark.django_db
def test_explicit_mapping_is_active_and_server_switch_keeps_only_the_tab(client, servers):
    device = make_device(
        "explicit-mapped-server",
        librenms_cf={"primary": {"id": 13404}, "secondary": {"id": 13405}},
    )
    _register_device(servers.secondary, 13405, device.name)
    client.force_login(make_superuser("object-server-explicit-user"))
    url = _sync_url(device)

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
    assert "interfaces_page" not in selector_html(html)
    assert '<input type="hidden" name="server_key" value="secondary">' in html
    assert f"{url}?tab=interfaces&amp;server_key=secondary" in html


@pytest.mark.django_db
def test_configured_but_unmapped_server_fails_closed_without_discovery(client, servers):
    device = make_device("configured-unmapped-server", librenms_cf={"secondary": {"id": 13406}})
    observed = []
    _register_device(servers.primary, 13406, device.name, observed)
    client.force_login(make_superuser("object-server-unmapped-user"))

    response = client.get(_sync_url(device), {"server_key": "primary", "tab": "interfaces"})

    assert observed == []
    assert response.status_code == 200
    assert b"is not an available mapping for this object" in response.content
    assert b'id="add-device-modal"' not in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": {"id": 13406}}


@pytest.mark.django_db
@pytest.mark.parametrize("stored_key", ["contains", "dc__west"])
def test_malformed_stored_server_key_does_not_break_the_page(client, servers, stored_key):
    device = make_device(
        f"malformed-stored-{stored_key}",
        librenms_cf={
            stored_key: {"_migrated_to": {"device_id": 4242, "server_key": stored_key}},
            "secondary": 13409,
        },
    )
    _register_device(
        servers.secondary,
        13409,
        device.name,
        payload={"status": "ok", "devices": []},
    )
    client.force_login(make_superuser(f"object-server-stored-{stored_key}-user"))

    response = client.get(_sync_url(device), {"server_key": "secondary"})

    assert response.status_code == 200
    html = response.content.decode()
    assert 'data-active-server-key="secondary"' in html
    assert stored_key not in selector_html(html)


@pytest.mark.django_db
@pytest.mark.parametrize("requested_key", ["contains", "dc__west", "_preferred_server"])
def test_malformed_requested_server_key_renders_the_blocked_page(client, servers, requested_key):
    device = make_device("malformed-server-key", librenms_cf={"secondary": {"id": 13408}})
    observed = []
    _register_device(servers.secondary, 13408, device.name, observed)
    client.force_login(make_superuser(f"object-server-malformed-{requested_key}-user"))

    response = client.get(_sync_url(device), {"server_key": requested_key})

    assert observed == []
    assert response.status_code == 200
    assert b"is not an available mapping for this object" in response.content


@pytest.mark.django_db
@pytest.mark.parametrize("requested_key", ["contains", "dc__west", "_preferred_server"])
@pytest.mark.parametrize(
    ("url_name", "tab_argument"),
    [("sync_cache_status", []), ("sync_cache_fragment", ["interfaces"])],
)
def test_cache_only_endpoints_reject_malformed_server_keys(
    client,
    servers,
    requested_key,
    url_name,
    tab_argument,
):
    label = f"{url_name}-{requested_key}"
    device = make_device(f"malformed-cache-{label}", librenms_cf={"secondary": {"id": 13410}})
    client.force_login(make_superuser(f"object-server-cache-{label}-user"))
    url = reverse(f"plugins:netbox_librenms_plugin:{url_name}", args=["device", device.pk, *tab_argument])

    response = client.get(url, {"server_key": requested_key})

    assert response.status_code == 404


@pytest.mark.django_db
def test_unconfigured_mapping_is_removable_but_not_selectable(client, servers):
    device = make_device("stale-server-mapping", librenms_cf={"retired": {"id": 13407}})
    client.force_login(make_superuser("object-server-stale-user"))

    response = client.get(_sync_url(device), {"server_key": "retired"})

    assert response.status_code == 200
    html = response.content.decode()
    assert "LibreNMS server &#x27;retired&#x27; is not an available mapping" in html
    assert "Not configured" in html
    assert 'name="server_key" value="retired"' in html
    selector = selector_html(html)
    assert '<span class="dropdown-item disabled"' in selector
    assert "server_key=retired" not in selector


@pytest.mark.django_db
def test_multiple_non_default_mappings_require_selection_without_querying(client, settings, servers):
    with librenms_mock_server() as tertiary:
        _configure_servers(
            settings,
            primary=(servers.primary, "Primary LibreNMS"),
            secondary=(servers.secondary, "Secondary LibreNMS"),
            tertiary=(tertiary, "Tertiary LibreNMS"),
        )
        device = make_device(
            "ambiguous-server-mappings",
            librenms_cf={"secondary": {"id": 13408}, "tertiary": {"id": 13409}},
        )
        observed = []
        _register_device(servers.secondary, 13408, device.name, observed)
        _register_device(tertiary, 13409, device.name, observed)
        client.force_login(make_superuser("object-server-ambiguous-user"))
        url = _sync_url(device)

        response = client.get(url, {"tab": "modules"})

    assert observed == []
    assert response.status_code == 200
    assert b"Select a LibreNMS server to continue" in response.content
    selector = selector_html(response.content.decode())
    assert f'href="{url}?tab=modules&amp;server_key=secondary"' in selector
    assert f'href="{url}?tab=modules&amp;server_key=tertiary"' in selector


@pytest.mark.django_db
def test_mapped_installation_default_wins_when_several_mappings_exist(client, servers):
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    device = make_device(
        "mapped-installation-default",
        librenms_cf={"primary": {"id": 13410}, "secondary": {"id": 13411}},
    )
    observed = []
    _register_device(servers.primary, 13410, device.name, observed)
    client.force_login(make_superuser("object-server-default-user"))

    response = client.get(_sync_url(device))

    assert response.status_code == 200
    assert response.request["QUERY_STRING"] == ""
    assert [request["path"] for request in observed] == ["/api/v0/devices/13410"]
    assert 'data-active-server-key="primary"' in response.content.decode()


@pytest.mark.django_db
def test_valid_object_preference_beats_default_and_is_not_a_mapping_row(client, servers):
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    device = make_device(
        "preferred-object-server",
        librenms_cf={
            "primary": {"id": 13412},
            "secondary": {"id": 13413},
            "_preferred_server": "secondary",
        },
    )
    observed = []
    _register_device(servers.secondary, 13413, device.name, observed)
    client.force_login(make_superuser("object-server-preference-user"))
    url = _sync_url(device)

    redirect_response = client.get(url, {"tab": "cables"})

    assert observed == []
    assert redirect_response.url == f"{url}?tab=cables&server_key=secondary"

    response = client.get(redirect_response.url)

    assert response.status_code == 200
    assert [request["path"] for request in observed] == ["/api/v0/devices/13413"]
    html = response.content.decode()
    assert 'data-active-server-key="secondary"' in html
    assert "_preferred_server" not in html
    assert "ID 13412" in html
    assert "ID 13413" in html


@pytest.mark.django_db
def test_legacy_integer_mapping_keeps_the_installation_default(client, servers):
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    device = make_device("legacy-object-server", librenms_cf=13414)
    observed = []
    _register_device(servers.primary, 13414, device.name, observed)
    client.force_login(make_superuser("object-server-legacy-user"))

    response = client.get(_sync_url(device))

    assert response.status_code == 200
    assert response.request["QUERY_STRING"] == ""
    assert [request["path"] for request in observed] == ["/api/v0/devices/13414"]
    assert b"Active LibreNMS Server" in response.content


@pytest.mark.django_db
def test_unavailable_explicit_server_stays_active_without_fallback(client, servers):
    device = make_device(
        "unavailable-object-server",
        librenms_cf={"primary": {"id": 13415}, "secondary": {"id": 13416}},
    )
    observed = []
    primary_requests = []
    _register_device(servers.primary, 13415, device.name, primary_requests)
    _register_device(
        servers.secondary,
        13416,
        device.name,
        observed,
        status=503,
        payload={"status": "error", "message": "Service unavailable"},
    )
    client.force_login(make_superuser("object-server-unavailable-user"))

    response = client.get(_sync_url(device), {"server_key": "secondary"})

    assert response.status_code == 200
    assert [request["path"] for request in observed] == ["/api/v0/devices/13416"]
    assert primary_requests == []
    html = response.content.decode()
    assert 'data-active-server-key="secondary"' in html
    assert b"Details unavailable" in response.content


@pytest.mark.django_db
def test_interface_paginator_keeps_the_active_server(client, servers):
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
    _register_device(servers.secondary, 13417, device.name)
    client.force_login(make_superuser("object-server-paginator-user"))

    response = client.get(
        _sync_url(device),
        {"server_key": "secondary", "tab": "interfaces", "interfaces_per_page": "25"},
    )

    assert response.status_code == 200
    html = response.content.decode()
    assert "interfaces_page=2" in html
    paginator_start = html.index('aria-label="Page selection"')
    paginator_end = html.index("</nav>", paginator_start)
    assert "server_key=secondary" in html[paginator_start:paginator_end]


@pytest.mark.django_db
def test_add_device_forms_carry_the_installation_default_server(client, servers):
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    device = make_device("add-on-active-server")
    servers.primary.register(
        f"/api/v0/devices/{device.name}",
        {"status": "error", "message": "Device not found"},
        status=404,
    )
    servers.primary.register(
        "/api/v0/poller_group",
        {"status": "ok", "get_poller_group": []},
    )
    client.force_login(make_superuser("object-server-add-user"))

    response = client.get(_sync_url(device))

    assert response.status_code == 200
    html = response.content.decode()
    for form_id in ("snmpv1v2-form", "snmpv3-form"):
        start = html.index(f'id="{form_id}"')
        end = html.index("</form>", start)
        assert 'name="server_key" value="primary"' in html[start:end]


@pytest.mark.django_db
def test_fallback_mapping_rows_render_the_active_server_the_same_way(settings, servers):
    from django.template.loader import render_to_string

    from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

    device = make_device("fallback-selector-device", librenms_cf={"primary": 51001, "secondary": 51002})

    rows = BaseLibreNMSSyncView._build_all_server_mappings(device, "primary")
    markup = render_to_string(
        "netbox_librenms_plugin/inc/_server_selector.html",
        {"all_server_mappings": rows, "server_key": "primary"},
    )

    assert rows
    assert "dropdown-item active" in markup
    assert "dropdown-item disabled" not in markup
