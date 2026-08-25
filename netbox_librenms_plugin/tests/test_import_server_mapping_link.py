"""Request-level tests for adding LibreNMS server mappings through import."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
from unittest.mock import patch

import pytest

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_vm,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.import_server_helpers import (
    configure_servers,
    json_response,
    librenms_device,
)


class _MappingClaimBarrier:
    """Hold competing mapping claims at the target lock and completed conflict scan."""

    def __init__(self, claim_barrier, target_barrier, conflict_barrier):
        self.claim_barrier = claim_barrier
        self.target_barrier = target_barrier
        self.conflict_barrier = conflict_barrier
        self.target_lock_seen = False
        self.advisory_lock_seen = False
        self.conflict_queries = 0

    @staticmethod
    def _wait(barrier):
        barrier.wait(timeout=5)

    def __call__(self, execute, sql, params, many, context):
        if "pg_advisory_xact_lock" in sql:
            self._wait(self.claim_barrier)
            result = execute(sql, params, many, context)
            self.advisory_lock_seen = True
            return result

        result = execute(sql, params, many, context)
        if "FOR UPDATE" in sql and ('FROM "dcim_device"' in sql or 'FROM "virtualization_virtualmachine"' in sql):
            self.target_lock_seen = True
            if not self.advisory_lock_seen:
                self._wait(self.target_barrier)
        elif (
            self.target_lock_seen
            and sql.lstrip().upper().startswith("SELECT")
            and '"custom_field_data"' in sql
            and ('FROM "dcim_device"' in sql or 'FROM "virtualization_virtualmachine"' in sql)
        ):
            self.conflict_queries += 1
            if self.conflict_queries == 2 and not self.advisory_lock_seen:
                self._wait(self.conflict_barrier)
        return result


@pytest.mark.django_db
def test_device_link_adds_secondary_mapping_and_prefers_the_previous_sole_mapping(client, settings):
    """An explicit second-server link preserves the first mapping as object preference."""
    from django.urls import reverse

    configure_servers(settings)
    device = make_device(
        "edge-link-device",
        librenms_cf={
            "primary": {
                "id": 48101,
                "oob": {"id": 48102, "type": "bmc", "version": "1.0"},
            }
        },
    )
    client.force_login(make_superuser("second-server-device-linker"))
    action_url = reverse(
        "plugins:netbox_librenms_plugin:device_conflict_action",
        kwargs={"device_id": 48201},
    )
    libre_device = librenms_device(48201, device.name)

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/48201":
            return json_response(request_url, {"status": "ok", "devices": [libre_device]})
        if request_url.startswith("https://secondary.example.com/api/v0/inventory/48201"):
            return json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.post(
            action_url,
            {
                "action": "link",
                "existing_device_id": device.pk,
                "existing_device_type": "device",
                "server_key": "secondary",
                "use-sysname-toggle": "on",
                "strip-domain-toggle": "off",
            },
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
def test_vm_link_uses_the_same_second_server_mapping_contract(client, settings):
    """A hostname-matched VM can add a server mapping through the import action."""
    from django.urls import reverse

    configure_servers(settings)
    vm = make_vm("edge-link-vm")
    vm.custom_field_data["librenms_id"] = {"primary": 48301}
    vm.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("second-server-vm-linker"))
    action_url = reverse(
        "plugins:netbox_librenms_plugin:device_conflict_action",
        kwargs={"device_id": 48401},
    )
    libre_device = librenms_device(48401, vm.name)

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/48401":
            return json_response(request_url, {"status": "ok", "devices": [libre_device]})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.post(
            action_url,
            {
                "action": "link",
                "existing_device_id": vm.pk,
                "existing_device_type": "virtualmachine",
                "server_key": "secondary",
                "use-sysname-toggle": "on",
                "strip-domain-toggle": "off",
            },
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
def test_vm_validation_is_read_only_and_offers_an_explicit_link(client, settings):
    """Validation exposes the VM link action without changing its stored mappings."""
    from django.urls import reverse

    configure_servers(settings)
    vm = make_vm("edge-link-vm-preview")
    vm.custom_field_data["librenms_id"] = {"primary": 48501}
    vm.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("second-server-vm-preview"))
    validation_url = reverse(
        "plugins:netbox_librenms_plugin:device_validation_details",
        kwargs={"device_id": 48601},
    )
    libre_device = librenms_device(48601, vm.name)

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/48601":
            return json_response(request_url, {"status": "ok", "devices": [libre_device]})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(validation_url, {"server_key": "secondary"})

    assert response.status_code == 200
    html = response.content.decode()
    assert 'name="existing_device_type" value="virtualmachine"' in html
    assert 'name="action" value="link"' in html
    vm.refresh_from_db()
    assert vm.custom_field_data["librenms_id"] == {"primary": 48501}


@pytest.mark.django_db
def test_primary_ip_matched_device_changes_only_after_explicit_update_and_link(client, settings):
    """A primary-IP match stays read-only until its update-and-link POST."""
    from django.urls import reverse

    configure_servers(settings)
    device = make_device("existing-primary-ip-device", librenms_cf={"primary": 48701})
    interface = make_interface(device, "mgmt0")
    primary_ip = make_ip("198.18.1.10/32", assigned_object=interface)
    device.primary_ip4 = primary_ip
    device.save(update_fields=["primary_ip4"])
    client.force_login(make_superuser("primary-ip-device-linker"))
    validation_url = reverse(
        "plugins:netbox_librenms_plugin:device_validation_details",
        kwargs={"device_id": 48801},
    )
    action_url = reverse(
        "plugins:netbox_librenms_plugin:device_conflict_action",
        kwargs={"device_id": 48801},
    )
    libre_device = librenms_device(48801, "renamed-primary-ip-device")
    libre_device["ip"] = "198.18.1.10"

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/48801":
            return json_response(request_url, {"status": "ok", "devices": [libre_device]})
        if request_url.startswith("https://secondary.example.com/api/v0/inventory/48801"):
            return json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        preview = client.get(validation_url, {"server_key": "secondary"})
        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == {"primary": 48701}
        assert 'name="action" value="update"' in preview.content.decode()

        response = client.post(
            action_url,
            {
                "action": "update",
                "existing_device_id": device.pk,
                "existing_device_type": "device",
                "server_key": "secondary",
                "use-sysname-toggle": "on",
                "strip-domain-toggle": "off",
            },
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    device.refresh_from_db()
    assert device.name == "renamed-primary-ip-device"
    assert device.custom_field_data["librenms_id"] == {
        "primary": 48701,
        "secondary": 48801,
        "_preferred_server": "primary",
    }


@pytest.mark.django_db
def test_primary_ip_matched_vm_changes_only_after_explicit_update_and_link(client, settings):
    """A VM primary-IP match uses the same explicit mapping action as a Device."""
    from django.urls import reverse
    from virtualization.models import VMInterface

    configure_servers(settings)
    vm = make_vm("existing-primary-ip-vm")
    vm.custom_field_data["librenms_id"] = {"primary": 48901}
    vm_interface = VMInterface.objects.create(virtual_machine=vm, name="eth0")
    primary_ip = make_ip("198.18.1.20/32", assigned_object=vm_interface)
    vm.primary_ip4 = primary_ip
    vm.save(update_fields=["custom_field_data", "primary_ip4"])
    client.force_login(make_superuser("primary-ip-vm-linker"))
    validation_url = reverse(
        "plugins:netbox_librenms_plugin:device_validation_details",
        kwargs={"device_id": 49001},
    )
    action_url = reverse(
        "plugins:netbox_librenms_plugin:device_conflict_action",
        kwargs={"device_id": 49001},
    )
    libre_device = librenms_device(49001, "renamed-primary-ip-vm")
    libre_device["ip"] = "198.18.1.20"

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/49001":
            return json_response(request_url, {"status": "ok", "devices": [libre_device]})
        if request_url.startswith("https://secondary.example.com/api/v0/inventory/49001"):
            return json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        preview = client.get(validation_url, {"server_key": "secondary"})
        vm.refresh_from_db()
        assert vm.custom_field_data["librenms_id"] == {"primary": 48901}
        preview_html = preview.content.decode()
        assert 'name="existing_device_type" value="virtualmachine"' in preview_html
        assert 'name="action" value="update"' in preview_html

        response = client.post(
            action_url,
            {
                "action": "update",
                "existing_device_id": vm.pk,
                "existing_device_type": "virtualmachine",
                "server_key": "secondary",
                "use-sysname-toggle": "on",
                "strip-domain-toggle": "off",
            },
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    vm.refresh_from_db()
    assert vm.name == "renamed-primary-ip-vm"
    assert vm.custom_field_data["librenms_id"] == {
        "primary": 48901,
        "secondary": 49001,
        "_preferred_server": "primary",
    }


@pytest.mark.django_db
def test_serial_matched_device_changes_only_after_explicit_update_and_link(client, settings):
    """A serial match remains read-only until its update-and-link POST."""
    from django.urls import reverse

    configure_servers(settings)
    device = make_device(
        "existing-serial-device",
        serial="TEST-SERIAL-49101",
        librenms_cf={"primary": 49101},
    )
    client.force_login(make_superuser("serial-device-linker"))
    validation_url = reverse(
        "plugins:netbox_librenms_plugin:device_validation_details",
        kwargs={"device_id": 49201},
    )
    action_url = reverse(
        "plugins:netbox_librenms_plugin:device_conflict_action",
        kwargs={"device_id": 49201},
    )
    libre_device = librenms_device(49201, "renamed-serial-device")
    libre_device["serial"] = "TEST-SERIAL-49101"

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/49201":
            return json_response(request_url, {"status": "ok", "devices": [libre_device]})
        if request_url.startswith("https://secondary.example.com/api/v0/inventory/49201"):
            return json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        preview = client.get(validation_url, {"server_key": "secondary"})
        device.refresh_from_db()
        assert device.name == "existing-serial-device"
        assert device.custom_field_data["librenms_id"] == {"primary": 49101}
        preview_html = preview.content.decode()
        assert "Serial match" in preview_html
        assert 'name="action" value="update"' in preview_html

        response = client.post(
            action_url,
            {
                "action": "update",
                "existing_device_id": device.pk,
                "existing_device_type": "device",
                "server_key": "secondary",
                "use-sysname-toggle": "on",
                "strip-domain-toggle": "off",
            },
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
def test_link_reloads_locked_mapping_state_before_adding_the_active_server(client, settings):
    """A mapping change after the request starts is preserved by the locked write."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from django.urls import reverse

    configure_servers(settings)
    device = make_device("concurrent-link-device", librenms_cf={"primary": 49101})
    client.force_login(make_superuser("concurrent-server-linker"))
    action_url = reverse(
        "plugins:netbox_librenms_plugin:device_conflict_action",
        kwargs={"device_id": 49201},
    )
    libre_device = librenms_device(49201, device.name)
    concurrent_change_applied = False

    def librenms_response(request_url, **_kwargs):
        nonlocal concurrent_change_applied
        if request_url == "https://secondary.example.com/api/v0/devices/49201":
            if not concurrent_change_applied:
                current = type(device).objects.get(pk=device.pk)
                current.custom_field_data["librenms_id"]["concurrent"] = {
                    "id": 49102,
                    "oob": {"id": 49103, "type": "bmc"},
                }
                current.save(update_fields=["custom_field_data"])
                concurrent_change_applied = True
            return json_response(request_url, {"status": "ok", "devices": [libre_device]})
        if request_url.startswith("https://secondary.example.com/api/v0/inventory/49201"):
            return json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with (
        patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response),
        CaptureQueriesContext(connection) as queries,
    ):
        response = client.post(
            action_url,
            {
                "action": "link",
                "existing_device_id": device.pk,
                "existing_device_type": "device",
                "server_key": "secondary",
                "use-sysname-toggle": "on",
                "strip-domain-toggle": "off",
            },
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
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
def test_vm_link_rechecks_device_id_collisions_after_validation(client, settings):
    """A Device mapping added after VM validation must block the VM mapping write."""
    from django.db import connection
    from django.urls import reverse

    configure_servers(settings)
    vm = make_vm("cross-model-link-target")
    vm.custom_field_data["librenms_id"] = {"primary": 49501}
    vm.save(update_fields=["custom_field_data"])
    device = make_device("cross-model-link-owner", librenms_cf={"primary": 49502})
    client.force_login(make_superuser("cross-model-linker"))
    action_url = reverse(
        "plugins:netbox_librenms_plugin:device_conflict_action",
        kwargs={"device_id": 49601},
    )
    libre_device = librenms_device(49601, vm.name)
    concurrent_mapping_added = False

    def add_device_mapping_before_target_lock(execute, sql, params, many, context):
        nonlocal concurrent_mapping_added
        if not concurrent_mapping_added and 'FROM "virtualization_virtualmachine"' in sql and "FOR UPDATE" in sql:
            concurrent_mapping_added = True
            type(device).objects.filter(pk=device.pk).update(
                custom_field_data={"librenms_id": {"primary": 49502, "secondary": 49601}}
            )
        return execute(sql, params, many, context)

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/49601":
            return json_response(request_url, {"status": "ok", "devices": [libre_device]})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with (
        connection.execute_wrapper(add_device_mapping_before_target_lock),
        patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response),
    ):
        response = client.post(
            action_url,
            {
                "action": "link",
                "existing_device_id": vm.pk,
                "existing_device_type": "virtualmachine",
                "server_key": "secondary",
                "use-sysname-toggle": "on",
                "strip-domain-toggle": "off",
            },
            headers={"HX-Request": "true"},
        )

    assert concurrent_mapping_added
    assert response.status_code == 200
    assert b"already assigned to device" in response.content
    vm.refresh_from_db()
    assert vm.custom_field_data["librenms_id"] == {"primary": 49501}


@transactional_db_with_all_apps()
def test_device_and_vm_links_serialize_one_cross_model_id_claim(settings):
    """Competing Device and VM transactions must produce one server-scoped ID owner."""
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection
    from django.test import Client
    from django.urls import reverse

    configure_servers(settings)
    device = make_device("concurrent-cross-model-device", librenms_cf={"primary": 49701})
    vm = make_vm("concurrent-cross-model-vm")
    vm.custom_field_data["librenms_id"] = {"primary": 49702}
    vm.save(update_fields=["custom_field_data"])
    user = make_superuser("concurrent-cross-model-linker")
    claim_barrier = Barrier(2)
    target_barrier = Barrier(2)
    conflict_barrier = Barrier(2)
    fetch_barrier = Barrier(2)
    wrappers = [_MappingClaimBarrier(claim_barrier, target_barrier, conflict_barrier) for _target in range(2)]
    request_context = local()

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/49801":
            libre_device = librenms_device(49801, request_context.hostname)
            _MappingClaimBarrier._wait(fetch_barrier)
            return json_response(request_url, {"status": "ok", "devices": [libre_device]})
        if request_url.startswith("https://secondary.example.com/api/v0/inventory/49801"):
            return json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    def link(target_pk, target_type, hostname, wrapper):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '30s'")
                cursor.execute("SET statement_timeout = '45s'")
            request_context.hostname = hostname
            thread_client = Client()
            thread_client.force_login(get_user_model().objects.get(pk=user.pk))
            action_url = reverse(
                "plugins:netbox_librenms_plugin:device_conflict_action",
                kwargs={"device_id": 49801},
            )
            with connection.execute_wrapper(wrapper):
                response = thread_client.post(
                    action_url,
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

    with (
        patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        futures = [
            executor.submit(link, device.pk, "device", device.name, wrappers[0]),
            executor.submit(link, vm.pk, "virtualmachine", vm.name, wrappers[1]),
        ]
        outcomes = [future.result(timeout=60) for future in futures]

    device.refresh_from_db()
    vm.refresh_from_db()
    mappings = [device.custom_field_data["librenms_id"], vm.custom_field_data["librenms_id"]]
    owners = [mapping for mapping in mappings if mapping.get("secondary") == 49801]
    assert [status for status, _content in outcomes] == [200, 200]
    assert all(wrapper.target_lock_seen for wrapper in wrappers)
    assert all(wrapper.advisory_lock_seen for wrapper in wrappers)
    assert len(owners) == 1
    assert sum(b"LibreNMS ID conflict" in content for _status, content in outcomes) == 1


@pytest.mark.django_db
def test_link_preserves_an_established_preference_and_every_other_mapping(client, settings):
    """Adding the active server does not rewrite existing preference or mapping state."""
    from django.urls import reverse

    configure_servers(settings)
    device = make_device(
        "preferred-link-device",
        librenms_cf={
            "primary": 49301,
            "archive": {"id": 49302, "oob": {"id": 49303, "type": "bmc"}},
            "_preferred_server": "archive",
        },
    )
    client.force_login(make_superuser("preferred-server-linker"))
    action_url = reverse(
        "plugins:netbox_librenms_plugin:device_conflict_action",
        kwargs={"device_id": 49401},
    )
    libre_device = librenms_device(49401, device.name)

    def librenms_response(request_url, **_kwargs):
        if request_url == "https://secondary.example.com/api/v0/devices/49401":
            return json_response(request_url, {"status": "ok", "devices": [libre_device]})
        if request_url.startswith("https://secondary.example.com/api/v0/inventory/49401"):
            return json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.post(
            action_url,
            {
                "action": "link",
                "existing_device_id": device.pk,
                "existing_device_type": "device",
                "server_key": "secondary",
                "use-sysname-toggle": "on",
                "strip-domain-toggle": "off",
            },
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
