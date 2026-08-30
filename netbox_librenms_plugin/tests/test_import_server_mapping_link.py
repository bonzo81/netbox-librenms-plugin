"""Request-level coverage for adding server mappings through import."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_vm,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.import_server_helpers import librenms_device
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


class _MappingClaimBarrier:
    """Hold competing mapping claims at the server-scoped ID advisory lock."""

    def __init__(self, claim_barrier):
        self.claim_barrier = claim_barrier
        self.target_lock_seen = False
        self.advisory_lock_seen = False
        self.claim_lock_preceded_the_target_lock = None

    def __call__(self, execute, sql, params, many, context):
        if "pg_advisory_xact_lock" in sql:
            self.claim_barrier.wait(timeout=5)
            result = execute(sql, params, many, context)
            self.advisory_lock_seen = True
            return result

        result = execute(sql, params, many, context)
        if (
            not self.target_lock_seen
            and "FOR UPDATE" in sql
            and ('FROM "dcim_device"' in sql or 'FROM "virtualization_virtualmachine"' in sql)
        ):
            self.target_lock_seen = True
            self.claim_lock_preceded_the_target_lock = self.advisory_lock_seen
        return result


def _configure_servers(settings, primary, secondary):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "primary": {
            "display_name": "Primary LibreNMS",
            "librenms_url": primary.url,
            "api_token": "mapping-link-test-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        },
        "secondary": {
            "display_name": "Secondary LibreNMS",
            "librenms_url": secondary.url,
            "api_token": "mapping-link-test-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        },
    }
    plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def servers(settings, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with ExitStack() as stack:
        primary = stack.enter_context(librenms_mock_server())
        secondary = stack.enter_context(librenms_mock_server())
        _configure_servers(settings, primary, secondary)
        yield SimpleNamespace(primary=primary, secondary=secondary)


def _register_import_device(server, device, *, include_inventory=True):
    server.register(
        f"/api/v0/devices/{device['device_id']}",
        {"status": "ok", "devices": [device]},
    )
    if include_inventory:
        for suffix in ("", "/all"):
            server.register(
                f"/api/v0/inventory/{device['device_id']}{suffix}",
                {"status": "ok", "inventory": []},
            )


def _action_url(device_id):
    from django.urls import reverse

    return reverse(
        "plugins:netbox_librenms_plugin:device_conflict_action",
        kwargs={"device_id": device_id},
    )


def _validation_url(device_id):
    from django.urls import reverse

    return reverse(
        "plugins:netbox_librenms_plugin:device_validation_details",
        kwargs={"device_id": device_id},
    )


def _link_payload(target, *, action="link"):
    return {
        "action": action,
        "existing_device_id": target.pk,
        "existing_device_type": target._meta.model_name,
        "server_key": "secondary",
        "use-sysname-toggle": "on",
        "strip-domain-toggle": "off",
    }


@pytest.mark.django_db
def test_device_link_adds_secondary_mapping_and_prefers_the_previous_sole_mapping(client, servers):
    device = make_device(
        "edge-link-device",
        librenms_cf={
            "primary": {
                "id": 48101,
                "oob": {"id": 48102, "type": "bmc", "version": "1.0"},
            }
        },
    )
    _register_import_device(servers.secondary, librenms_device(48201, device.name))
    client.force_login(make_superuser("second-server-device-linker"))

    response = client.post(
        _action_url(48201),
        _link_payload(device),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {
        "primary": {
            "id": 48101,
            "oob": {"id": 48102, "type": "bmc", "version": "1.0"},
        },
        "secondary": 48201,
        "_preferred_server": "primary",
    }


@pytest.mark.django_db
def test_vm_link_uses_the_same_second_server_mapping_contract(client, servers):
    vm = make_vm("edge-link-vm")
    vm.custom_field_data["librenms_id"] = {"primary": 48301}
    vm.save(update_fields=["custom_field_data"])
    _register_import_device(
        servers.secondary,
        librenms_device(48401, vm.name),
        include_inventory=False,
    )
    client.force_login(make_superuser("second-server-vm-linker"))

    response = client.post(
        _action_url(48401),
        _link_payload(vm),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    vm.refresh_from_db()
    assert vm.custom_field_data["librenms_id"] == {
        "primary": 48301,
        "secondary": 48401,
        "_preferred_server": "primary",
    }


@pytest.mark.django_db
def test_vm_validation_is_read_only_and_offers_an_explicit_link(client, servers):
    vm = make_vm("edge-link-vm-preview")
    vm.custom_field_data["librenms_id"] = {"primary": 48501}
    vm.save(update_fields=["custom_field_data"])
    _register_import_device(
        servers.secondary,
        librenms_device(48601, vm.name),
        include_inventory=False,
    )
    client.force_login(make_superuser("second-server-vm-preview"))

    response = client.get(_validation_url(48601), {"server_key": "secondary"})

    assert response.status_code == 200
    html = response.content.decode()
    assert 'name="existing_device_type" value="virtualmachine"' in html
    assert 'name="action" value="link"' in html
    vm.refresh_from_db()
    assert vm.custom_field_data["librenms_id"] == {"primary": 48501}


@pytest.mark.django_db
@pytest.mark.parametrize("object_kind", ["device", "virtualmachine"])
def test_primary_ip_match_changes_only_after_explicit_update_and_link(client, servers, object_kind):
    if object_kind == "device":
        target = make_device("existing-primary-ip-device", librenms_cf={"primary": 48701})
        interface = make_interface(target, "mgmt0")
        device_id = 48801
        address = "198.18.1.10/32"
        renamed = "renamed-primary-ip-device"
    else:
        from virtualization.models import VMInterface

        target = make_vm("existing-primary-ip-vm")
        target.custom_field_data["librenms_id"] = {"primary": 48901}
        interface = VMInterface.objects.create(virtual_machine=target, name="eth0")
        device_id = 49001
        address = "198.18.1.20/32"
        renamed = "renamed-primary-ip-vm"

    primary_ip = make_ip(address, assigned_object=interface)
    target.primary_ip4 = primary_ip
    target.save(update_fields=["custom_field_data", "primary_ip4"])
    libre_device = librenms_device(device_id, renamed)
    libre_device["ip"] = address.removesuffix("/32")
    _register_import_device(servers.secondary, libre_device)
    client.force_login(make_superuser(f"primary-ip-{object_kind}-linker"))

    preview = client.get(_validation_url(device_id), {"server_key": "secondary"})

    target.refresh_from_db()
    expected_primary_id = 48701 if object_kind == "device" else 48901
    assert target.custom_field_data["librenms_id"] == {"primary": expected_primary_id}
    preview_html = preview.content.decode()
    assert 'name="action" value="update"' in preview_html
    assert f'name="existing_device_type" value="{target._meta.model_name}"' in preview_html

    response = client.post(
        _action_url(device_id),
        _link_payload(target, action="update"),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    target.refresh_from_db()
    assert target.name == renamed
    assert target.custom_field_data["librenms_id"] == {
        "primary": expected_primary_id,
        "secondary": device_id,
        "_preferred_server": "primary",
    }


@pytest.mark.django_db
def test_serial_match_changes_only_after_explicit_update_and_link(client, servers):
    device = make_device(
        "existing-serial-device",
        serial="TEST-SERIAL-49101",
        librenms_cf={"primary": 49101},
    )
    libre_device = librenms_device(49201, "renamed-serial-device")
    libre_device["serial"] = "TEST-SERIAL-49101"
    _register_import_device(servers.secondary, libre_device)
    client.force_login(make_superuser("serial-device-linker"))

    preview = client.get(_validation_url(49201), {"server_key": "secondary"})

    device.refresh_from_db()
    assert device.name == "existing-serial-device"
    assert device.custom_field_data["librenms_id"] == {"primary": 49101}
    assert "Serial match" in preview.content.decode()
    assert 'name="action" value="update"' in preview.content.decode()

    response = client.post(
        _action_url(49201),
        _link_payload(device, action="update"),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    device.refresh_from_db()
    assert device.name == "renamed-serial-device"
    assert device.custom_field_data["librenms_id"] == {
        "primary": 49101,
        "secondary": 49201,
        "_preferred_server": "primary",
    }


@pytest.mark.django_db
def test_link_reloads_locked_mapping_state_before_adding_the_active_server(client, servers):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    device = make_device("concurrent-link-device", librenms_cf={"primary": 49101})
    _register_import_device(servers.secondary, librenms_device(49201, device.name))
    client.force_login(make_superuser("concurrent-server-linker"))
    concurrent_change_applied = False

    def add_mapping_before_target_lock(execute, sql, params, many, context):
        nonlocal concurrent_change_applied
        if not concurrent_change_applied and 'FROM "dcim_device"' in sql and "FOR UPDATE" in sql:
            concurrent_change_applied = True
            current = type(device).objects.get(pk=device.pk)
            current.custom_field_data["librenms_id"]["concurrent"] = {
                "id": 49102,
                "oob": {"id": 49103, "type": "bmc"},
            }
            current.save(update_fields=["custom_field_data"])
        return execute(sql, params, many, context)

    with connection.execute_wrapper(add_mapping_before_target_lock), CaptureQueriesContext(connection) as queries:
        response = client.post(
            _action_url(49201),
            _link_payload(device),
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    assert concurrent_change_applied
    assert any('FROM "dcim_device"' in query["sql"] and "FOR UPDATE" in query["sql"] for query in queries)
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {
        "primary": 49101,
        "concurrent": {
            "id": 49102,
            "oob": {"id": 49103, "type": "bmc"},
        },
        "secondary": 49201,
        "_preferred_server": "primary",
    }


@pytest.mark.django_db
def test_vm_link_rechecks_device_id_collisions_after_validation(client, servers):
    from django.db import connection

    vm = make_vm("cross-model-link-target")
    vm.custom_field_data["librenms_id"] = {"primary": 49501}
    vm.save(update_fields=["custom_field_data"])
    device = make_device("cross-model-link-owner", librenms_cf={"primary": 49502})
    _register_import_device(
        servers.secondary,
        librenms_device(49601, vm.name),
        include_inventory=False,
    )
    client.force_login(make_superuser("cross-model-linker"))
    concurrent_mapping_added = False

    def add_device_mapping_before_target_lock(execute, sql, params, many, context):
        nonlocal concurrent_mapping_added
        if not concurrent_mapping_added and 'FROM "virtualization_virtualmachine"' in sql and "FOR UPDATE" in sql:
            concurrent_mapping_added = True
            type(device).objects.filter(pk=device.pk).update(
                custom_field_data={"librenms_id": {"primary": 49502, "secondary": 49601}}
            )
        return execute(sql, params, many, context)

    with connection.execute_wrapper(add_device_mapping_before_target_lock):
        response = client.post(
            _action_url(49601),
            _link_payload(vm),
            headers={"HX-Request": "true"},
        )

    assert concurrent_mapping_added
    assert response.status_code == 200
    assert b"already assigned to device" in response.content
    vm.refresh_from_db()
    assert vm.custom_field_data["librenms_id"] == {"primary": 49501}


@transactional_db_with_all_apps()
def test_device_and_vm_links_serialize_one_cross_model_id_claim(servers):
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection
    from django.test import Client

    device = make_device("concurrent-cross-model-device", librenms_cf={"primary": 49701})
    vm = make_vm("concurrent-cross-model-vm")
    vm.custom_field_data["librenms_id"] = {"primary": 49702}
    vm.save(update_fields=["custom_field_data"])
    first_fetch_completed = Event()
    fetch_barrier = Barrier(2)
    response_names = iter((device.name, vm.name))

    def device_response(**_request):
        name = next(response_names)
        first_fetch_completed.set()
        fetch_barrier.wait(timeout=10)
        return 200, {"status": "ok", "devices": [librenms_device(49801, name)]}

    servers.secondary.register("/api/v0/devices/49801", device_response)
    for suffix in ("", "/all"):
        servers.secondary.register(
            f"/api/v0/inventory/49801{suffix}",
            {"status": "ok", "inventory": []},
        )
    user = make_superuser("concurrent-cross-model-linker")
    claim_barrier = Barrier(2)
    wrappers = [_MappingClaimBarrier(claim_barrier) for _target in range(2)]

    def link(target_pk, target_type, wrapper, *, wait_for_first_fetch=False):
        close_old_connections()
        try:
            if wait_for_first_fetch:
                assert first_fetch_completed.wait(timeout=10)
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '30s'")
                cursor.execute("SET statement_timeout = '45s'")
            thread_client = Client()
            thread_client.force_login(get_user_model().objects.get(pk=user.pk))
            with connection.execute_wrapper(wrapper):
                response = thread_client.post(
                    _action_url(49801),
                    {
                        "action": "link",
                        "existing_device_id": target_pk,
                        "existing_device_type": target_type,
                        "server_key": "secondary",
                        "use-sysname-toggle": "on",
                        "strip-domain-toggle": "off",
                    },
                    headers={"HX-Request": "true"},
                )
            return response.status_code, response.content
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(link, device.pk, "device", wrappers[0]),
            executor.submit(link, vm.pk, "virtualmachine", wrappers[1], wait_for_first_fetch=True),
        ]
        outcomes = [future.result(timeout=60) for future in futures]

    device.refresh_from_db()
    vm.refresh_from_db()
    mappings = [device.custom_field_data["librenms_id"], vm.custom_field_data["librenms_id"]]
    owners = [mapping for mapping in mappings if mapping.get("secondary") == 49801]
    assert [status for status, _content in outcomes] == [200, 200]
    assert all(wrapper.target_lock_seen for wrapper in wrappers)
    assert all(wrapper.advisory_lock_seen for wrapper in wrappers)
    assert all(wrapper.claim_lock_preceded_the_target_lock for wrapper in wrappers)
    assert len(owners) == 1
    assert sum(b"LibreNMS ID conflict" in content for _status, content in outcomes) == 1


@pytest.mark.django_db
def test_link_preserves_established_preference_and_every_other_mapping(client, servers):
    device = make_device(
        "preferred-link-device",
        librenms_cf={
            "primary": 49301,
            "archive": {"id": 49302, "oob": {"id": 49303, "type": "bmc"}},
            "_preferred_server": "archive",
        },
    )
    _register_import_device(servers.secondary, librenms_device(49401, device.name))
    client.force_login(make_superuser("preferred-server-linker"))

    response = client.post(
        _action_url(49401),
        _link_payload(device),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {
        "primary": 49301,
        "archive": {"id": 49302, "oob": {"id": 49303, "type": "bmc"}},
        "secondary": 49401,
        "_preferred_server": "archive",
    }
