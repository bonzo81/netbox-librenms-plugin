"""Request-level tests for cross-tab cache consistency."""

import json
from copy import deepcopy
from unittest.mock import patch

import pytest
from dcim.models import Cable, Module, ModuleBay, ModuleBayTemplate, ModuleType
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from ipam.models import IPAddress, VLAN
from requests import Response

from netbox_librenms_plugin.sync_cache import CacheMutationTransition, SyncCacheConsistency, SyncTab
from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
)
from netbox_librenms_plugin.utils import set_librenms_device_id


def _configure_servers(settings):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "primary": {"librenms_url": "https://primary.example.com", "api_token": "test-token"},
        "secondary": {"librenms_url": "https://secondary.example.com", "api_token": "test-token"},
        "unmapped": {"librenms_url": "https://unmapped.example.com", "api_token": "test-token"},
    }
    settings.PLUGINS_CONFIG = plugin_config


def _cache_key(data_type, obj, server_key):
    return f"librenms_{data_type}_{obj._meta.model_name}_{obj.pk}_{server_key}"


def _last_fetched_key(data_type, obj, server_key):
    return f"librenms_{data_type}_last_fetched_{obj._meta.model_name}_{obj.pk}_{server_key}"


def _vlan_overrides_key(obj, server_key):
    return f"librenms_vlan_group_overrides_{obj._meta.model_name}_{obj.pk}_{server_key}"


def _seed_snapshot(data_type, obj, server_key, payload=None):
    cache.set(_cache_key(data_type, obj, server_key), payload or {"snapshot": data_type}, timeout=300)


def _json_response(url, payload, status=200):
    """Return a concrete requests response for the LibreNMS HTTP boundary."""
    response = Response()
    response.status_code = status
    response.url = url
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    return response


@pytest.mark.django_db
def test_committed_interface_sync_invalidates_only_mapped_page_and_shared_snapshots(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A VC mutation must keep its source and clear only mapped page/shared namespaces."""
    _configure_servers(settings)
    _chassis, (page_device, secondary_owner, sibling) = make_virtual_chassis_members("cache-scope", count=3)
    page_device.custom_field_data["librenms_id"] = {"primary": {"id": 41}}
    page_device.save(update_fields=["custom_field_data"])
    secondary_owner.custom_field_data["librenms_id"] = {"secondary": {"id": 42}}
    secondary_owner.save(update_fields=["custom_field_data"])

    source_payload = {
        "ports": [
            {
                "port_id": 7001,
                "ifName": "Ethernet1/1",
                "ifDescr": "Ethernet1/1",
                "ifType": "ethernetCsmacd",
                "ifAdminStatus": "up",
            }
        ],
        "port_stack_relationships": {},
    }
    _seed_snapshot("ports", page_device, "primary", source_payload)
    cache.set(_last_fetched_key("ports", page_device, "primary"), "source-time", timeout=300)
    cache.set(_vlan_overrides_key(page_device, "primary"), {"100": "1"}, timeout=300)
    for data_type in ("links", "ip_addresses", "inventory", "vlans"):
        _seed_snapshot(data_type, page_device, "primary")
    cache.set(_last_fetched_key("vlans", page_device, "primary"), "vlan-time", timeout=300)

    for data_type in ("ports", "links", "inventory"):
        _seed_snapshot(data_type, secondary_owner, "secondary")
        _seed_snapshot(data_type, page_device, "secondary")
    for data_type in ("ip_addresses", "vlans"):
        _seed_snapshot(data_type, page_device, "secondary")
        _seed_snapshot(data_type, sibling, "secondary")
    cache.set(_last_fetched_key("ports", secondary_owner, "secondary"), "secondary-time", timeout=300)
    cache.set(_vlan_overrides_key(secondary_owner, "secondary"), {"200": "2"}, timeout=300)
    cache.set(_last_fetched_key("vlans", page_device, "secondary"), "secondary-vlan-time", timeout=300)

    for data_type in ("ports", "links", "ip_addresses", "inventory", "vlans"):
        _seed_snapshot(data_type, page_device, "unmapped")

    client.force_login(make_superuser("cache-scope-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": "device", "object_id": page_device.pk},
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            sync_url,
            {
                "server_key": "primary",
                "interface_name_field": "ifName",
                "select": "7001",
                "exclude_columns": "vlans",
            },
        )

    assert response.status_code == 302
    assert cache.get(_cache_key("ports", page_device, "primary")) == source_payload
    assert cache.get(_last_fetched_key("ports", page_device, "primary")) == "source-time"
    assert cache.get(_vlan_overrides_key(page_device, "primary")) == {"100": "1"}
    for data_type in ("links", "ip_addresses", "inventory", "vlans"):
        assert cache.get(_cache_key(data_type, page_device, "primary")) is None

    for data_type in ("ports", "links", "inventory"):
        assert cache.get(_cache_key(data_type, secondary_owner, "secondary")) is None
        assert cache.get(_cache_key(data_type, page_device, "secondary")) is None
    for data_type in ("ip_addresses", "vlans"):
        assert cache.get(_cache_key(data_type, page_device, "secondary")) is None
        assert cache.get(_cache_key(data_type, sibling, "secondary")) is not None

    for data_type in ("ports", "links", "ip_addresses", "inventory", "vlans"):
        assert cache.get(_cache_key(data_type, page_device, "unmapped")) is not None

    coordinator = SyncCacheConsistency(page_device, cache_timeout=300)
    primary_state = cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary"))
    assert primary_state["state"] == "locally_changed"
    assert primary_state["source_tab"] == "interfaces"
    assert primary_state["actor_id"] is not None
    secondary_state = cache.get(coordinator.state_key(SyncTab.INTERFACES, "secondary"))
    assert secondary_state["state"] == "invalidated"
    assert secondary_state["revision"] == primary_state["revision"]

    status_url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_status",
        kwargs={"object_type": "device", "pk": page_device.pk},
    )
    same_user_status = client.get(status_url, {"server_key": "primary"})
    assert same_user_status.status_code == 200
    source_status = same_user_status.json()["tabs"]["interfaces"]
    assert source_status["state"] == "locally_changed"
    assert source_status["snapshot_available"] is True
    assert source_status["same_user"] is True
    ip_status = same_user_status.json()["tabs"]["ipaddresses"]
    assert ip_status["state"] == "invalidated"
    assert ip_status["snapshot_available"] is False
    assert ip_status["same_user"] is True

    other_user = get_user_model().objects.create_user(
        username="cache-scope-other-user",
        is_superuser=True,
        is_active=True,
    )
    client.force_login(other_user)
    other_user_status = client.get(status_url, {"server_key": "primary"})
    assert other_user_status.status_code == 200
    assert other_user_status.json()["tabs"]["ipaddresses"]["same_user"] is False


@pytest.mark.django_db
def test_fully_skipped_interface_request_preserves_all_snapshots(client, settings):
    """A request that writes nothing must not invalidate another tab."""
    _configure_servers(settings)
    _chassis, (device, _sibling) = make_virtual_chassis_members("cache-noop", count=2)
    device.custom_field_data["librenms_id"] = {"primary": {"id": 51}}
    device.save(update_fields=["custom_field_data"])
    source_payload = {"ports": [], "port_stack_relationships": {}}
    ip_payload = {"ip_addresses": [{"ip_with_mask": "198.18.20.10/24"}]}
    _seed_snapshot("ports", device, "primary", source_payload)
    _seed_snapshot("ip_addresses", device, "primary", ip_payload)

    client.force_login(make_superuser("cache-noop-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )
    response = client.post(
        sync_url,
        {
            "server_key": "primary",
            "interface_name_field": "ifName",
        },
    )

    assert response.status_code == 302
    assert cache.get(_cache_key("ports", device, "primary")) == source_payload
    assert cache.get(_cache_key("ip_addresses", device, "primary")) == ip_payload
    coordinator = SyncCacheConsistency(device, cache_timeout=300)
    assert cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary")) is None


@pytest.mark.django_db
def test_selected_interface_that_already_matches_preserves_other_snapshots(client, settings):
    """A selected row with no semantic change must not clear another tab."""
    _configure_servers(settings)
    device = make_device("cache-interface-unchanged", librenms_cf={"primary": {"id": 52}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    interface.custom_field_data["librenms_id"] = {"primary": 7002}
    interface.save(update_fields=["custom_field_data"])
    source_payload = {
        "ports": [{"port_id": 7002, "ifName": interface.name}],
        "port_stack_relationships": {},
    }
    ip_payload = {"ip_addresses": [{"ip_with_mask": "198.18.20.11/24"}]}
    _seed_snapshot("ports", device, "primary", source_payload)
    _seed_snapshot("ip_addresses", device, "primary", ip_payload)

    client.force_login(make_superuser("cache-interface-unchanged-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )
    response = client.post(
        sync_url,
        {
            "server_key": "primary",
            "interface_name_field": "ifName",
            "select": "7002",
            "exclude_columns": ["name", "type", "speed", "description", "mtu", "enabled", "mac_address", "vlans"],
        },
    )

    assert response.status_code == 302
    assert cache.get(_cache_key("ip_addresses", device, "primary")) == ip_payload
    coordinator = SyncCacheConsistency(device, cache_timeout=300)
    assert cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary")) is None


@pytest.mark.django_db
def test_ip_sync_invalidates_other_tabs_after_creating_an_address(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A committed IP row must retain its source snapshot and clear other tabs."""
    _configure_servers(settings)
    device = make_device("cache-ip-writer", librenms_cf={"primary": {"id": 61}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    interface.custom_field_data["librenms_id"] = {"primary": 7101}
    interface.save(update_fields=["custom_field_data"])
    address = "198.18.61.10/24"
    ip_payload = {
        "ip_addresses": [
            {
                "ip_with_mask": address,
                "port_id": 7101,
                "interface_name": interface.name,
            }
        ]
    }
    _seed_snapshot("ip_addresses", device, "primary", ip_payload)
    _seed_snapshot("ports", device, "primary")

    client.force_login(make_superuser("cache-ip-writer-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {"server_key": "primary", "select": address, f"vrf_{address}": ""},
        )

    assert response.status_code == 302
    assert IPAddress.objects.filter(address=address, assigned_object_id=interface.pk).exists()
    assert cache.get(_cache_key("ip_addresses", device, "primary")) == ip_payload
    assert cache.get(_cache_key("ports", device, "primary")) is None


@pytest.mark.django_db(
    transaction=True,
    available_apps=tuple(app.name for app in django_apps.get_app_configs()),
)
def test_htmx_mutation_does_not_repeat_cache_notice_on_next_navigation(client, settings):
    """An HTMX mutation must report its cache transition only in the current response."""
    _configure_servers(settings)
    device = make_device("cache-parent-immediate-notice", librenms_cf={"primary": {"id": 611}})
    parent = make_interface(device, "Ethernet1", iface_type="1000base-t")
    child = make_interface(device, "Ethernet1.100", iface_type="virtual")
    set_librenms_device_id(parent, 7111, "primary")
    set_librenms_device_id(child, 7112, "primary")
    parent.save(update_fields=["custom_field_data"])
    child.save(update_fields=["custom_field_data"])
    ports_payload = {
        "ports": [
            {"port_id": 7111, "ifName": parent.name},
            {"port_id": 7112, "ifName": child.name},
        ],
        "port_stack_relationships": {
            "lag_members": {},
            "sub_interfaces": {7112: 7111},
        },
    }
    _seed_snapshot("ports", device, "primary", ports_payload)
    _seed_snapshot("ip_addresses", device, "primary")
    client.force_login(make_superuser("cache-parent-single-notice-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_interface_parent",
        kwargs={"object_type": "device", "object_id": device.pk},
    )

    response = client.post(
        sync_url,
        {
            "server_key": "primary",
            "port_id": "7112",
            "parent_port_id": "7111",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert "librenmsCacheChanged" in response["HX-Trigger"]
    child.refresh_from_db()
    assert child.parent == parent

    next_page = client.get(device.get_absolute_url())
    assert next_page.status_code == 200
    assert b"Other sync tabs were cleared" not in next_page.content


@pytest.mark.django_db
def test_vlan_sync_invalidates_other_tabs_after_creating_a_vlan(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A committed VLAN row must publish the same cache transition as other writers."""
    _configure_servers(settings)
    device = make_device("cache-vlan-writer", librenms_cf={"primary": {"id": 62}})
    vlan_payload = [{"vlan_vlan": 3062, "vlan_name": "Cache Test VLAN"}]
    _seed_snapshot("vlans", device, "primary", vlan_payload)
    _seed_snapshot("links", device, "primary")

    client.force_login(make_superuser("cache-vlan-writer-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans",
        kwargs={"object_type": "device", "object_id": device.pk},
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {"server_key": "primary", "action": "create_vlans", "select": "3062"},
        )

    assert response.status_code == 302
    assert VLAN.objects.filter(vid=3062, name="Cache Test VLAN").exists()
    assert cache.get(_cache_key("vlans", device, "primary")) == vlan_payload
    assert cache.get(_cache_key("links", device, "primary")) is None


@pytest.mark.django_db
def test_cable_sync_invalidates_other_tabs_after_creating_a_cable(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A committed cable must clear comparisons that depend on current topology."""
    _configure_servers(settings)
    device = make_device("cache-cable-writer", librenms_cf={"primary": {"id": 63}})
    remote_device = make_device("cache-cable-remote")
    local = make_interface(device, "Ethernet1", iface_type="1000base-t")
    remote = make_interface(remote_device, "Ethernet2", iface_type="1000base-t")
    links_payload = {
        "links": [
            {
                "local_port_id": 7201,
                "local_port": local.name,
                "netbox_local_interface_id": local.pk,
                "netbox_remote_device_id": remote_device.pk,
                "netbox_remote_interface_id": remote.pk,
            }
        ]
    }
    _seed_snapshot("links", device, "primary", links_payload)
    _seed_snapshot("inventory", device, "primary")

    client.force_login(make_superuser("cache-cable-writer-user"))
    url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", kwargs={"pk": device.pk})
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"server_key": "primary", "select": "7201"})

    assert response.status_code == 302
    assert Cable.objects.filter(terminations__termination_id=local.pk).exists()
    assert cache.get(_cache_key("links", device, "primary")) == links_payload
    assert cache.get(_cache_key("inventory", device, "primary")) is None


@pytest.mark.django_db
def test_module_install_invalidates_other_tabs_after_creating_a_module(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A committed module action must retain inventory and clear other snapshots."""
    _configure_servers(settings)
    device = make_device("cache-module-writer", librenms_cf={"primary": {"id": 64}})
    bay = ModuleBay.objects.create(device=device, name="Slot 1")
    module_type = ModuleType.objects.create(
        manufacturer=device.device_type.manufacturer,
        model="Cache Test Module",
    )
    inventory_payload = {
        "inventory": [
            {
                "entPhysicalIndex": 8101,
                "entPhysicalClass": "module",
                "entPhysicalModelName": module_type.model,
                "entPhysicalContainedIn": 0,
                "entPhysicalName": bay.name,
            }
        ]
    }
    _seed_snapshot("inventory", device, "primary", inventory_payload)
    _seed_snapshot("ip_addresses", device, "primary")

    client.force_login(make_superuser("cache-module-writer-user"))
    url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk})
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {
                "server_key": "primary",
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "serial": "CACHE-TEST-SERIAL",
            },
        )

    assert response.status_code == 302
    assert Module.objects.filter(device=device, module_bay=bay, module_type=module_type).exists()
    assert cache.get(_cache_key("inventory", device, "primary")) == inventory_payload
    assert cache.get(_cache_key("ip_addresses", device, "primary")) is None


@pytest.mark.django_db
def test_unchanged_module_serial_preserves_other_snapshots(client, settings):
    """Submitting the existing serial must not publish a cache mutation."""
    _configure_servers(settings)
    device = make_device("cache-module-unchanged", librenms_cf={"primary": {"id": 641}})
    bay = ModuleBay.objects.create(device=device, name="Slot 1")
    module_type = ModuleType.objects.create(
        manufacturer=device.device_type.manufacturer,
        model="Cache Existing Module",
    )
    module = Module.objects.create(
        device=device,
        module_bay=bay,
        module_type=module_type,
        serial="UNCHANGED-SERIAL",
    )
    inventory_payload = {"inventory": [{"entPhysicalIndex": 8102}]}
    _seed_snapshot("inventory", device, "primary", inventory_payload)
    _seed_snapshot("ports", device, "primary")
    client.force_login(make_superuser("cache-module-unchanged-user"))
    url = reverse("plugins:netbox_librenms_plugin:update_module_serial", kwargs={"pk": device.pk})

    response = client.post(
        url,
        {
            "server_key": "primary",
            "module_id": str(module.pk),
            "serial": module.serial,
        },
    )

    assert response.status_code == 302
    assert cache.get(_cache_key("ports", device, "primary")) is not None
    coordinator = SyncCacheConsistency(device, cache_timeout=300)
    assert cache.get(coordinator.state_key(SyncTab.MODULES, "primary")) is None


@pytest.mark.django_db
def test_module_mutation_without_posted_server_uses_active_namespace(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A module writer must resolve its active namespace before it mutates."""
    _configure_servers(settings)
    device = make_device("cache-module-missing-server", librenms_cf={"primary": {"id": 642}})
    bay = ModuleBay.objects.create(device=device, name="Slot 1")
    module_type = ModuleType.objects.create(
        manufacturer=device.device_type.manufacturer,
        model="Cache Scoped Module",
    )
    module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, serial="OLD")
    _seed_snapshot("ports", device, "primary")
    client.force_login(make_superuser("cache-module-missing-server-user"))
    url = reverse("plugins:netbox_librenms_plugin:update_module_serial", kwargs={"pk": device.pk})

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"module_id": str(module.pk), "serial": "NEW"})

    assert response.status_code == 302
    module.refresh_from_db()
    assert module.serial == "NEW"
    assert cache.get(_cache_key("ports", device, "primary")) is None


@pytest.mark.django_db
def test_interface_delete_without_posted_server_uses_active_namespace(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """The delete writer must validate its server namespace before deleting a row."""
    _configure_servers(settings)
    device = make_device("cache-delete-missing-server", librenms_cf={"primary": {"id": 643}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    _seed_snapshot("ports", device, "primary")
    _seed_snapshot("ip_addresses", device, "primary")
    client.force_login(make_superuser("cache-delete-missing-server-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:delete_netbox_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"interface_ids": [str(interface.pk)]})

    assert response.status_code == 200
    assert not type(interface).objects.filter(pk=interface.pk).exists()
    assert cache.get(_cache_key("ports", device, "primary")) is not None
    assert cache.get(_cache_key("ip_addresses", device, "primary")) is None


@pytest.mark.django_db
def test_inline_relationship_noop_preserves_other_snapshots(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """An already-applied relationship must not publish a second mutation."""
    _configure_servers(settings)
    device = make_device("cache-parent-noop", librenms_cf={"primary": {"id": 644}})
    parent = make_interface(device, "Ethernet1", iface_type="1000base-t")
    child = make_interface(device, "Ethernet1.100", iface_type="virtual")
    set_librenms_device_id(parent, 7441, "primary")
    set_librenms_device_id(child, 7442, "primary")
    parent.save(update_fields=["custom_field_data"])
    child.parent = parent
    child.save(update_fields=["custom_field_data", "parent"])
    ports_payload = {
        "ports": [
            {"port_id": 7441, "ifName": parent.name},
            {"port_id": 7442, "ifName": child.name},
        ],
        "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {7442: 7441}},
    }
    _seed_snapshot("ports", device, "primary", ports_payload)
    _seed_snapshot("ip_addresses", device, "primary")
    client.force_login(make_superuser("cache-parent-noop-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_interface_parent",
        kwargs={"object_type": "device", "object_id": device.pk},
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {
                "server_key": "primary",
                "port_id": "7442",
                "parent_port_id": "7441",
            },
        )

    assert response.status_code == 200
    assert cache.get(_cache_key("ip_addresses", device, "primary")) is not None
    coordinator = SyncCacheConsistency(device, cache_timeout=300)
    assert cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary")) is None


@pytest.mark.django_db
def test_htmx_ip_cache_miss_replaces_active_tab_with_warning(client, settings):
    """An HTMX writer cache miss must not navigate away from the active tab."""
    _configure_servers(settings)
    device = make_device("cache-ip-missing", librenms_cf={"primary": {"id": 645}})
    client.force_login(make_superuser("cache-ip-missing-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )

    response = client.post(
        url,
        {"server_key": "primary", "select": "198.18.45.1/24"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert "HX-Redirect" not in response
    assert b"Refresh IP Addresses" in response.content
    assert b'name="select"' not in response.content


@pytest.mark.django_db
def test_htmx_module_cache_miss_replaces_active_tab_with_warning(client, settings):
    """An HTMX module writer cache miss must render an empty refresh state."""
    _configure_servers(settings)
    device = make_device("cache-module-missing", librenms_cf={"primary": {"id": 646}})
    client.force_login(make_superuser("cache-module-missing-user"))
    url = reverse("plugins:netbox_librenms_plugin:install_selected", kwargs={"pk": device.pk})

    response = client.post(
        url,
        {"server_key": "primary", "select": "9001"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert "HX-Redirect" not in response
    assert b"Refresh Modules" in response.content
    assert b'name="select"' not in response.content


@pytest.mark.django_db
def test_partial_cable_refresh_renders_no_syncable_rows(client, settings):
    """An incomplete cable refresh must not show rows without a backing snapshot."""
    _configure_servers(settings)
    device = make_device(
        "cache-cable-partial",
        librenms_cf={"primary": {"id": 647, "oob": {"id": 649, "type": "oob"}}},
    )
    remote_device = make_device("cache-cable-partial-remote", librenms_cf={"primary": {"id": 648}})
    local = make_interface(device, "Ethernet1", iface_type="1000base-t")
    remote = make_interface(remote_device, "Ethernet2", iface_type="1000base-t")
    set_librenms_device_id(local, 7471, "primary")
    set_librenms_device_id(remote, 7472, "primary")
    local.save(update_fields=["custom_field_data"])
    remote.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("cache-cable-partial-user"))

    def librenms_response(url, **_kwargs):
        if url.endswith("/api/v0/devices/647/links"):
            return _json_response(
                url,
                {
                    "status": "ok",
                    "links": [
                        {
                            "local_port_id": 7471,
                            "local_port": local.name,
                            "remote_port_id": 7472,
                            "remote_port": remote.name,
                            "remote_hostname": remote_device.name,
                            "remote_device_id": 648,
                        }
                    ],
                },
            )
        if url.endswith("/api/v0/devices/647/ports"):
            return _json_response(
                url,
                {"status": "ok", "ports": [{"port_id": 7471, "ifName": local.name, "ifDescr": local.name}]},
            )
        if url.endswith("/api/v0/devices/649/links"):
            return _json_response(url, {"message": "Unavailable"}, status=503)
        raise AssertionError(f"Unexpected LibreNMS request: {url}")

    url = reverse("plugins:netbox_librenms_plugin:device_cable_sync", kwargs={"pk": device.pk})
    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.post(url, {"server_key": "primary"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    assert b"Cable refresh was incomplete" in response.content
    assert b"Sync Selected Cables" not in response.content
    assert b'name="select"' not in response.content
    assert cache.get(_cache_key("links", device, "primary")) is None


@pytest.mark.django_db
def test_cleanup_failure_invalidates_every_dependent_tab(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A partial cache-backend failure must leave all dependent tabs fail-closed."""
    _configure_servers(settings)
    device = make_device("cache-cleanup-failure", librenms_cf={"primary": {"id": 650}})
    coordinator = SyncCacheConsistency(device, cache_timeout=300)
    for data_type in ("ports", "links", "ip_addresses", "inventory", "vlans"):
        _seed_snapshot(data_type, device, "primary")
    cache.set(f"{_cache_key('links', device, 'primary')}:manual-remote:test-row", 1, timeout=300)

    with (
        patch.object(cache, "delete_pattern", side_effect=RuntimeError("cache backend failed")),
        django_capture_on_commit_callbacks(execute=True),
    ):
        transition = coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)

    assert transition.error is not None
    assert cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary"))["state"] == "locally_changed"
    for tab in (SyncTab.CABLES, SyncTab.IP_ADDRESSES, SyncTab.MODULES, SyncTab.VLANS):
        state = cache.get(coordinator.state_key(tab, "primary"))
        assert state["state"] == "invalidated"
        assert "cleanup did not complete" in state["reason"]

    assert cache.get(_cache_key("ip_addresses", device, "primary")) is not None
    assert coordinator.status("primary")[SyncTab.IP_ADDRESSES.value]["snapshot_available"] is False
    client.force_login(make_superuser("cache-cleanup-failure-user"))
    fragment_url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={
            "object_type": "device",
            "pk": device.pk,
            "tab": SyncTab.IP_ADDRESSES.value,
        },
    )
    response = client.get(fragment_url, {"server_key": "primary"})
    assert response.status_code == 404


@pytest.mark.django_db
def test_cleanup_failure_does_not_mark_tabs_without_cache_state(
    settings,
    django_capture_on_commit_callbacks,
):
    """Cleanup failure reasons must be limited to tabs that had snapshot-bound state."""
    _configure_servers(settings)
    device = make_device("cache-cleanup-sparse", librenms_cf={"primary": {"id": 651}})
    coordinator = SyncCacheConsistency(device, cache_timeout=300)
    _seed_snapshot("ports", device, "primary")
    _seed_snapshot("links", device, "primary")
    cache.set(f"{_cache_key('links', device, 'primary')}:manual-remote:test-row", 1, timeout=300)

    with (
        patch.object(cache, "delete_pattern", side_effect=RuntimeError("cache backend failed")),
        django_capture_on_commit_callbacks(execute=True),
    ):
        transition = coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)

    assert transition.error is not None
    assert cache.get(coordinator.state_key(SyncTab.CABLES, "primary"))["state"] == "invalidated"
    for tab in (SyncTab.IP_ADDRESSES, SyncTab.MODULES, SyncTab.VLANS):
        assert cache.get(coordinator.state_key(tab, "primary")) is None


@pytest.mark.django_db
def test_legacy_ip_fragment_never_backfills_from_librenms(client, settings):
    """A cache-only fragment must not upgrade an old IP snapshot through live HTTP."""
    _configure_servers(settings)
    device = make_device("cache-ip-fragment-legacy", librenms_cf={"primary": {"id": 652}})
    _seed_snapshot(
        "ip_addresses",
        device,
        "primary",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.52.10",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.52.10/24",
                    "port_id": 7521,
                }
            ]
        },
    )
    client.force_login(make_superuser("cache-ip-fragment-legacy-user"))
    fragment_url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": SyncTab.IP_ADDRESSES.value},
    )

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("The IP cache fragment contacted LibreNMS"),
    ):
        response = client.get(fragment_url, {"server_key": "primary"})

    assert response.status_code == 200
    assert b"No IP address data loaded" in response.content


@pytest.mark.django_db
def test_refresh_failure_reason_is_anonymous_across_users(settings):
    """The shared failure reason must identify only whether this user initiated it."""
    _configure_servers(settings)
    device = make_device("cache-refresh-failure-reason", librenms_cf={"primary": {"id": 653}})
    coordinator = SyncCacheConsistency(device, cache_timeout=300)
    coordinator.mark_refresh_failure(SyncTab.IP_ADDRESSES, "primary", actor_id=101)

    same_user = coordinator.status("primary", actor_id=101)[SyncTab.IP_ADDRESSES.value]
    other_user = coordinator.status("primary", actor_id=102)[SyncTab.IP_ADDRESSES.value]

    assert "your latest LibreNMS refresh failed" in same_user["reason"]
    assert "another user attempted to refresh" in other_user["reason"]


def test_cleanup_failure_payload_names_tabs_that_must_fail_closed():
    """A failed cleanup must tell the browser which stale controls to remove."""
    transition = CacheMutationTransition(
        source_tab=SyncTab.INTERFACES,
        cleanup_tabs={SyncTab.IP_ADDRESSES, SyncTab.MODULES},
        source_fragment_required=True,
        error="failed",
    )

    assert transition.browser_payload() == {
        "transition_id": transition.transition_id,
        "removed": False,
        "cleanup_failed": True,
        "tabs": [],
        "cleanup_tabs": ["ipaddresses", "modules"],
        "source_tab": "interfaces",
        "source_fragment_required": True,
        "revisions": {},
    }


@pytest.mark.django_db
def test_fragment_restores_from_cache_without_calling_librenms(client, settings):
    """The fragment endpoint must rebuild comparison HTML from cache and ORM only."""
    _configure_servers(settings)
    device = make_device("cache-fragment", librenms_cf={"primary": {"id": 65}})
    payload = {
        "ports": [
            {
                "port_id": 7301,
                "ifName": "Ethernet1",
                "ifDescr": "Ethernet1",
                "ifType": "ethernetCsmacd",
                "ifAdminStatus": "up",
            }
        ],
        "port_stack_relationships": {},
    }
    _seed_snapshot("ports", device, "primary", payload)
    client.force_login(make_superuser("cache-fragment-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": "interfaces"},
    )

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("The cache fragment contacted LibreNMS"),
    ):
        response = client.get(url, {"server_key": "primary"})

    assert response.status_code == 200
    assert b"Ethernet1" in response.content


@pytest.mark.django_db
def test_rolled_back_mutation_keeps_snapshots_and_publishes_no_state(
    settings,
    django_capture_on_commit_callbacks,
):
    """A rollback must discard the invalidation callback."""
    _configure_servers(settings)
    device = make_device("cache-rollback", librenms_cf={"primary": {"id": 66}})
    _seed_snapshot("ports", device, "primary")
    _seed_snapshot("ip_addresses", device, "primary")
    coordinator = SyncCacheConsistency(device, cache_timeout=300)

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError, match="roll back"):
            with transaction.atomic():
                coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)
                raise RuntimeError("roll back")

    assert cache.get(_cache_key("ports", device, "primary")) is not None
    assert cache.get(_cache_key("ip_addresses", device, "primary")) is not None
    assert cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary")) is None


@pytest.mark.django_db
def test_failed_refresh_retains_an_existing_invalidation_reason(
    settings,
    django_capture_on_commit_callbacks,
):
    """A failed retry must not erase why another tab cleared this snapshot."""
    _configure_servers(settings)
    device = make_device("cache-failed-refresh", librenms_cf={"primary": {"id": 67}})
    coordinator = SyncCacheConsistency(device, cache_timeout=300)
    _seed_snapshot("ports", device, "primary")
    _seed_snapshot("ip_addresses", device, "primary")
    with django_capture_on_commit_callbacks(execute=True):
        coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)
    prior = cache.get(coordinator.state_key(SyncTab.IP_ADDRESSES, "primary"))

    failed = coordinator.mark_refresh_failure(SyncTab.IP_ADDRESSES, "primary", actor_id=2)

    assert failed["revision"] == prior["revision"]
    assert failed["reason"] == prior["reason"]
    assert failed["refresh_error"] == "The latest LibreNMS refresh failed."


@pytest.mark.django_db
def test_add_bay_template_preserves_server_scope_and_invalidates_other_tabs(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """The modal must carry the active server through its committed module mutation."""
    _configure_servers(settings)
    device = make_device("cache-add-bay", librenms_cf={"secondary": {"id": 68}})
    inventory_payload = {"inventory": [{"entPhysicalIndex": 8201}]}
    _seed_snapshot("inventory", device, "secondary", inventory_payload)
    _seed_snapshot("ports", device, "secondary")
    client.force_login(make_superuser("cache-add-bay-user"))
    url = reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": device.pk})

    response = client.get(
        url,
        {
            "server_key": "secondary",
            "target_kind": "device_type",
            "target_pk": device.device_type_id,
            "suggested_name": "Expansion Bay",
            "librenms_name": "Expansion Bay",
        },
    )

    assert response.status_code == 200
    assert b'name="server_key" value="secondary"' in response.content

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {
                "server_key": "secondary",
                "target_kind": "device_type",
                "target_pk": str(device.device_type_id),
                "name": "Expansion Bay",
                "librenms_name": "Expansion Bay",
                "librenms_class": "module",
            },
        )

    assert response.status_code == 302
    assert ModuleBayTemplate.objects.filter(device_type=device.device_type, name="Expansion Bay").exists()
    assert cache.get(_cache_key("inventory", device, "secondary")) == inventory_payload
    assert cache.get(_cache_key("ports", device, "secondary")) is None
