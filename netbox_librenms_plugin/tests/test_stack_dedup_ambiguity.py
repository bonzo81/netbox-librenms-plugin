"""Two serial-less stacks that fingerprint alike must block the batch, not silently share one VC."""

import pytest

from netbox_librenms_plugin.import_utils.bulk_import import (
    classify_bulk_precheck,
    detect_collisions_for_device_ids,
    stack_identity,
)
from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

# Two members with no serial and identical name/model/position: the shape that makes the
# fingerprint branch of stack_identity collapse two unrelated stacks onto one key.
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
            return True, [{"entPhysicalClass": "stack", "entPhysicalIndex": 1}]
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
def test_two_serialless_stacks_that_fingerprint_alike_block_the_batch():
    """Without serials the fingerprint is a guess, so a second stack must not import VC-less."""
    rows = {97101: _row(97101, "stack-a"), 97102: _row(97102, "stack-b")}
    api = _StackBoundary(rows)

    # Assert the precondition first: a fixture that stopped producing two same-keyed stacks
    # would make the block assertion below pass for the wrong reason.
    vc_a = get_virtual_chassis_data(api, 97101)
    vc_b = get_virtual_chassis_data(api, 97102)
    assert vc_a.get("is_stack") and vc_b.get("is_stack"), "the fixture must detect two stacks"
    identity_a = stack_identity(vc_a, 97101)
    identity_b = stack_identity(vc_b, 97102)
    assert identity_a.basis == identity_b.basis == "fingerprint"
    assert identity_a.key == identity_b.key, "the fixture must reproduce the shared serial-less key"

    collisions, unresolved, stack_ambiguities = detect_collisions_for_device_ids(
        [97101, 97102],
        api,
        libre_devices_cache=rows,
        sync_options={"use_sysname": True},
    )
    outcome = classify_bulk_precheck(collisions, unresolved, stack_ambiguities, [97101, 97102], {})

    assert outcome.blocked is True, "two same-keyed serial-less stacks must block the batch"
    assert "stack" in outcome.block_message.lower()


@pytest.mark.django_db
def test_transient_vc_detection_failure_fails_closed_without_poisoning_retry():
    """An unreadable stack is unresolved, and its next detection retries the inventory API."""
    from django.core.cache import cache

    from netbox_librenms_plugin.import_utils.virtual_chassis import _vc_cache_key

    failed_id = 97111
    rows = {failed_id: _row(failed_id, "stack-failed"), 97112: _row(97112, "stack-readable")}
    api = _TransientFailureStackBoundary(rows, failed_id)
    cache.delete(_vc_cache_key(api, failed_id))

    _collisions, unresolved, _stack_ambiguities = detect_collisions_for_device_ids(
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
def test_placeholder_member_serials_fall_back_to_ambiguous_fingerprint():
    """Placeholder serials do not identify a stack, so indistinguishable stacks block."""
    rows = {97121: _row(97121, "stack-placeholder-a"), 97122: _row(97122, "stack-placeholder-b")}
    members = [{**member, "entPhysicalSerialNum": " N/A "} for member in CHASSIS_MEMBERS]
    api = _StackBoundary(rows, members=members)

    collisions, unresolved, stack_ambiguities = detect_collisions_for_device_ids(
        list(rows),
        api,
        libre_devices_cache=rows,
        sync_options={"use_sysname": True},
    )
    outcome = classify_bulk_precheck(collisions, unresolved, stack_ambiguities, list(rows), {})

    assert unresolved == []
    assert outcome.blocked is True
    assert stack_ambiguities[0]["device_ids"] == [97121, 97122]


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
    client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
        {
            "select": [str(device_id)],
            "server_key": "default",
            f"role_{device_id}": str(infrastructure.role_id),
        },
        headers={"HX-Request": "true"},
    )

    assert not Device.objects.filter(name=hostname).exists()
