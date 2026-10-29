"""Stack rows in a bulk import: unreadable stacks fail closed, and stacks without a master import standalone."""

import pytest

from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids
from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

# Two members with no serial: a stack whose master cannot be identified.
CHASSIS_MEMBERS = [
    {
        "entPhysicalClass": "chassis",
        "entPhysicalIndex": 10,
        "entPhysicalContainedIn": 1,
        "entPhysicalParentRelPos": 1,
        "entPhysicalSerialNum": "",
        "entPhysicalName": "Switch 1",
        "entPhysicalModelName": "C9300-48P",
        "entPhysicalDescr": "stack member",
    },
    {
        "entPhysicalClass": "chassis",
        "entPhysicalIndex": 11,
        "entPhysicalContainedIn": 1,
        "entPhysicalParentRelPos": 2,
        "entPhysicalSerialNum": "",
        "entPhysicalName": "Switch 2",
        "entPhysicalModelName": "C9300-48P",
        "entPhysicalDescr": "stack member",
    },
]


class _StackBoundary:
    """Real-shape stand-in for the LibreNMS HTTP boundary serving identical serial-less stacks."""

    server_key = "default"
    # 0 disables the VC cache, so each device runs the real detection instead of a warm clone.
    cache_timeout = 0

    def __init__(self, rows, members=None):
        self.rows = rows
        self.members = CHASSIS_MEMBERS if members is None else members

    def get_device_info(self, device_id, **_kwargs):
        row = self.rows.get(device_id)
        return (row is not None, row)

    def get_inventory_filtered(self, _device_id, **kwargs):
        if kwargs.get("ent_physical_contained_in") == 0:
            return True, [{"entPhysicalClass": "stack", "entPhysicalIndex": 1, "entPhysicalContainedIn": 0}]
        return True, list(self.members)


class _TransientFailureStackBoundary(_StackBoundary):
    """Fail the first root-inventory read for one device, then serve its stack."""

    cache_timeout = 300

    def __init__(self, rows, failed_device_id):
        super().__init__(rows)
        self.failed_device_id = failed_device_id
        self.root_attempts = {}

    def get_inventory_filtered(self, device_id, **kwargs):
        if kwargs.get("ent_physical_contained_in") == 0:
            attempts = self.root_attempts.get(device_id, 0) + 1
            self.root_attempts[device_id] = attempts
            if device_id == self.failed_device_id and attempts == 1:
                return False, []
        return super().get_inventory_filtered(device_id, **kwargs)


def _row(device_id, hostname):
    return {
        "device_id": device_id,
        "hostname": hostname,
        "sysName": hostname,
        "serial": "",
        "hardware": "C9300-48P",
        "os": "ios",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("duplicate_identity", ["hostname", "serial"])
def test_cached_device_ambiguity_adds_a_reason_when_issues_are_absent(duplicate_identity):
    from dcim.models import Device, Site

    from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
    from netbox_librenms_plugin.tests.conftest import make_device

    first = make_device("ambiguous-cache-a", serial="AMB-CACHE-SERIAL")
    second = Device.objects.create(
        name="ambiguous-cache-b" if duplicate_identity == "serial" else first.name,
        serial="AMB-CACHE-SERIAL" if duplicate_identity == "serial" else "",
        device_type=first.device_type,
        role=first.role,
        site=Site.objects.create(name="Ambiguous Cache Site", slug="ambiguous-cache-site"),
        status="active",
    )
    assert second.pk != first.pk
    libre_device = {"device_id": 835771, "hostname": "unmatched-cache-name", "sysName": "unmatched-cache-name"}
    if duplicate_identity == "hostname":
        libre_device["hostname"] = first.name
    else:
        libre_device["serial"] = "AMB-CACHE-SERIAL"
    validation = {"existing_device": None, "existing_vm": None, "import_as_vm": False, "can_import": True}

    _refresh_existing_device(validation, libre_device=libre_device)

    assert validation["existing_match_type"] == "ambiguous_hostname_or_serial"
    assert validation["can_import"] is False
    assert any("Multiple NetBox devices" in issue for issue in validation["issues"])


@pytest.mark.django_db
def test_two_look_alike_serialless_stacks_both_import_standalone(client, librenms_server, settings):
    """Neither stack can name its master, so each imports as a single device with a warning; the batch runs."""
    from dcim.models import Device, VirtualChassis
    from django.urls import reverse

    from netbox_librenms_plugin.import_utils.virtual_chassis import VC_MASTER_UNKNOWN_WARNING
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_device, make_superuser

    configure_librenms_servers(
        settings,
        {"default": {"librenms_url": librenms_server.url, "api_token": "test-token", "verify_ssl": False}},
    )
    infrastructure = make_device("look-alike-stack-infrastructure")
    device_ids = [97101, 97102]
    for device_id in device_ids:
        row = {
            **_row(device_id, f"look-alike-stack-{device_id}"),
            "hardware": infrastructure.device_type.model,
            "location": infrastructure.site.name,
        }
        librenms_server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]})
        librenms_server.vc_inventory_callable(
            device_id, [{"entPhysicalClass": "stack", "entPhysicalIndex": 1}], {1: CHASSIS_MEMBERS}
        )
    client.force_login(make_superuser("look-alike-stack-importer"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
        {
            "select": [str(device_id) for device_id in device_ids],
            "server_key": "default",
            **{f"role_{device_id}": str(infrastructure.role_id) for device_id in device_ids},
        },
        headers={"HX-Request": "true"},
    )

    body = response.content.decode()
    assert response.status_code == 200
    assert "Bulk import blocked" not in body
    for device_id in device_ids:
        assert VC_MASTER_UNKNOWN_WARNING.format(device_id=device_id) in body
        assert Device.objects.get(name=f"look-alike-stack-{device_id}").virtual_chassis is None
    assert not VirtualChassis.objects.filter(domain__in=[f"librenms-default-{i}" for i in device_ids]).exists()


@pytest.mark.django_db
def test_transient_vc_detection_failure_fails_closed_without_poisoning_retry():
    """An unreadable stack is unresolved, and its next detection retries the inventory API."""
    from django.core.cache import cache

    from netbox_librenms_plugin.import_utils.virtual_chassis import _vc_cache_key

    failed_id = 97111
    rows = {failed_id: _row(failed_id, "stack-failed"), 97112: _row(97112, "stack-readable")}
    api = _TransientFailureStackBoundary(rows, failed_id)
    cache.delete(_vc_cache_key(api, failed_id))

    _collisions, unresolved = detect_collisions_for_device_ids(
        list(rows),
        api,
        libre_devices_cache=rows,
        sync_options={"use_sysname": True},
    )
    retried = get_virtual_chassis_data(api, failed_id)

    assert unresolved == [failed_id]
    assert retried["is_stack"] is True
    assert api.root_attempts[failed_id] == 2


@pytest.mark.django_db
def test_a_lone_row_whose_stack_read_failed_is_not_imported_by_the_view(client, librenms_server, settings):
    """The synchronous import runs the same pre-check for one row as for many."""
    from dcim.models import Device
    from django.urls import reverse

    from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.tests.conftest import (
        configure_librenms_servers,
        make_device,
        make_superuser,
    )

    configure_librenms_servers(
        settings,
        {"default": {"librenms_url": librenms_server.url, "api_token": "test-token", "verify_ssl": False}},
    )
    infrastructure = make_device("lone-stack-view-infrastructure")
    device_id = 97131
    hostname = "lone-stack-view-target"
    row = {
        **_row(device_id, hostname),
        "hardware": infrastructure.device_type.model,
        "location": infrastructure.site.name,
    }
    librenms_server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]})
    # A 500 is a failed read, unlike the 404 that means "this device holds no inventory".
    librenms_server.register(f"/api/v0/inventory/{device_id}", {"status": "error"}, status=500)

    # Precondition: the row really is an unreadable stack candidate. A fixture that served the
    # inventory would import the device and read as the gate failing rather than the fixture.
    detection = get_virtual_chassis_data(LibreNMSAPI(server_key="default"), device_id)
    assert detection["detection_failed"] is True
    assert detection["is_stack"] is False

    client.force_login(make_superuser("lone-stack-view-importer"))
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
        {
            "select": [str(device_id)],
            "server_key": "default",
            f"role_{device_id}": str(infrastructure.role_id),
        },
        headers={"HX-Request": "true"},
    )

    # Assert the skip, not only the absent device: an error response or a failed save would
    # satisfy the database check on its own and hide a gate that never ran.
    assert response.status_code == 200
    body = response.content.decode()
    assert f"Skipped 1 selected row(s) (id(s): {device_id})" in body
    assert "were not imported" in body
    assert not Device.objects.filter(name=hostname).exists()


@pytest.mark.django_db
def test_direct_bulk_writer_refuses_a_failed_stack_read(librenms_server, settings):
    from dcim.models import Device

    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_device, make_superuser

    configure_librenms_servers(
        settings,
        {"default": {"librenms_url": librenms_server.url, "api_token": "test-token", "verify_ssl": False}},
    )
    infrastructure = make_device("direct-stack-fault-infrastructure")
    device_id = 97132
    row = {
        **_row(device_id, "direct-stack-fault-target"),
        "hardware": infrastructure.device_type.model,
        "location": infrastructure.site.name,
    }
    librenms_server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]})
    librenms_server.register(f"/api/v0/inventory/{device_id}", {"status": "error"}, status=500)

    result = bulk_import_devices(
        [device_id],
        server_key="default",
        manual_mappings_per_device={
            device_id: {
                "site_id": infrastructure.site_id,
                "device_type_id": infrastructure.device_type_id,
                "device_role_id": infrastructure.role_id,
            }
        },
        libre_devices_cache={device_id: row},
        user=make_superuser("direct-stack-fault-importer"),
    )

    assert result["success"] == []
    assert result["failed"]
    assert not Device.objects.filter(name=row["hostname"]).exists()


@pytest.mark.django_db
def test_a_missing_device_does_not_turn_inventory_404_into_a_non_stack(settings, librenms_server):
    from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

    configure_librenms_servers(
        settings,
        {"default": {"librenms_url": librenms_server.url, "api_token": "test-token", "verify_ssl": False}},
    )

    result = get_virtual_chassis_data(LibreNMSAPI(server_key="default"), 97133)

    assert result["detection_failed"] is True


@pytest.mark.django_db
def test_an_empty_inventory_reads_as_empty_through_the_client_side_fallback(librenms_server, settings):
    """A device that holds no inventory must not read as a failed detection."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

    configure_librenms_servers(
        settings,
        {"default": {"librenms_url": librenms_server.url, "api_token": "test-token", "verify_ssl": False}},
    )
    device_id = 97141
    # The filtered route answers with no rows, which sends the client to the /all fallback, and
    # LibreNMS answers 404 there for a device that holds no inventory at all.
    librenms_server.register(f"/api/v0/inventory/{device_id}", {"status": "ok", "inventory": []})
    librenms_server.register(f"/api/v0/inventory/{device_id}/all", {"status": "error"}, status=404)

    api = LibreNMSAPI(server_key="default")

    assert api.get_inventory_filtered(device_id, ent_physical_contained_in=0, missing_is_empty=True) == (True, [])
    # The default stays fail-closed: a caller that has not established the device exists must
    # still see the 404 as a failure rather than an empty inventory.
    success, _payload = api.get_inventory_filtered(device_id, ent_physical_contained_in=0)
    assert success is False


@pytest.mark.django_db
def test_single_writer_refuses_failed_stack_detection_with_manual_mappings(librenms_server, settings):
    from dcim.models import Device
    from netbox_librenms_plugin.import_utils.device_operations import import_single_device, validate_device_for_import
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_device, make_superuser

    configure_librenms_servers(settings, {"default": {"librenms_url": librenms_server.url, "api_token": "test-token"}})
    infrastructure = make_device("single-stack-fault-infrastructure")
    row = {
        **_row(97150, "single-stack-fault-target"),
        "hardware": infrastructure.device_type.model,
        "location": infrastructure.site.name,
    }
    librenms_server.register("/api/v0/devices/97150", {"status": "ok", "devices": [row]})
    librenms_server.register("/api/v0/inventory/97150", {"status": "error"}, status=500)
    result = import_single_device(
        97150,
        server_key="default",
        libre_device=row,
        manual_mappings={
            "site_id": infrastructure.site_id,
            "device_type_id": infrastructure.device_type_id,
            "device_role_id": infrastructure.role_id,
        },
        sync_options={"sync_interfaces": False, "sync_cables": False, "sync_fields": False},
        user=make_superuser("single-stack-fault-importer"),
    )
    assert result["success"] is False
    assert "stack" in result["error"].lower()
    assert not Device.objects.filter(name=row["hostname"]).exists()
    validation = validate_device_for_import(row, api=LibreNMSAPI(server_key="default"))
    assert validation["can_import"] is False
    assert any("stack" in issue.lower() for issue in validation["issues"])
