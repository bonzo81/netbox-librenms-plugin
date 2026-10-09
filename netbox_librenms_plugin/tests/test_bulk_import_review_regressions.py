"""Regression coverage for bulk-import collision review findings."""

from uuid import uuid4

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key
from netbox_librenms_plugin.tests.conftest import make_device, make_superuser, make_vm, transactional_db_with_all_apps
from netbox_librenms_plugin.tests.lock_conflict_helpers import (
    failing_statement,
    lock_row,
    lock_timeout,
    second_connection,
)
from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    make_request,
    make_user_with_perms,
    messages_on,
    post,
    queued_request,
)


@pytest.fixture(autouse=True)
def _configured_default_server(monkeypatch):
    """Give these import regressions one explicit usable server."""
    from netbox_librenms_plugin.server_selection import LibreNMSAPI

    monkeypatch.setattr(
        LibreNMSAPI,
        "get_available_servers",
        classmethod(lambda _cls: {"default": "Default"}),
    )


class _LibreNMSBoundary:
    """Real-shape stand-in for the external LibreNMS HTTP boundary."""

    server_key = "default"
    cache_timeout = 300

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_device_info(self, device_id, **kwargs):
        self.calls.append((device_id, kwargs))
        row = self.rows.get(device_id)
        return (row is not None, row)

    def get_inventory_filtered(self, _device_id, **_kwargs):
        return True, []


def _cache_rows(rows):
    for device_id, row in rows.items():
        cache.set(get_import_device_cache_key(device_id, "default"), row, timeout=300)


def _collision_rows(first_id, second_id, target_name):
    return {
        first_id: {
            "device_id": first_id,
            "hostname": target_name,
            "sysName": target_name,
            "serial": "",
            "hardware": "Review hardware",
            "location": "Review location",
            "os": "review-os",
        },
        second_id: {
            "device_id": second_id,
            "hostname": target_name,
            "sysName": target_name,
            "serial": "",
            "hardware": "Review hardware",
            "location": "Review location",
            "os": "review-os",
        },
    }


def _serial_collision_rows(first_id, second_id, serial, suffix):
    return {
        first_id: {
            "device_id": first_id,
            "hostname": f"librenms-row-a-{suffix}",
            "sysName": f"librenms-row-a-{suffix}",
            "serial": serial,
            "hardware": "Review hardware",
            "location": "Review location",
        },
        second_id: {
            "device_id": second_id,
            "hostname": f"librenms-row-b-{suffix}",
            "sysName": f"librenms-row-b-{suffix}",
            "serial": serial,
            "hardware": "Review hardware",
            "location": "Review location",
        },
    }


def _scoped_import_user(visible_device, username):
    from dcim.models import Device

    user = make_user_with_perms(username, [])
    user = grant(user, "add", Device)
    user = grant(user, "change", Device, constraints={"pk": visible_device.pk})
    return grant(user, "view", Device, constraints={"pk": visible_device.pk})


@pytest.mark.django_db
@pytest.mark.parametrize("view_name", ["confirm", "direct"])
def test_collision_response_redacts_target_outside_view_scope(view_name):
    """Collision responses must block without exposing a hidden target's name, PK, or URL."""
    from netbox_librenms_plugin.views.imports.actions import BulkImportConfirmView, BulkImportDevicesView

    visible = make_device(f"visible-collision-scope-{view_name}")
    hidden = make_device(f"hidden-collision-target-{view_name}", serial=f"HIDDEN-SERIAL-{view_name}")
    user = _scoped_import_user(visible, f"collision-scope-{view_name}")
    rows = _serial_collision_rows(96001, 96002, hidden.serial, view_name)
    _cache_rows(rows)
    api = _LibreNMSBoundary(rows)
    request = make_request(
        data={"select": ["96001", "96002"], "server_key": "default"},
        user=user,
        path="/bulk-import/",
        HTTP_HX_REQUEST="true",
    )
    view_class = BulkImportConfirmView if view_name == "confirm" else BulkImportDevicesView
    view = view_class()
    view._librenms_api = api

    response = post(view, request)

    html = response.content.decode()
    assert response.status_code == 200
    assert "Bulk import blocked" in html
    assert "restricted NetBox object" in html
    assert hidden.name not in html
    assert f"(pk {hidden.pk})" not in html
    assert reverse("dcim:device", kwargs={"pk": hidden.pk}) not in html


@pytest.mark.django_db
def test_collision_scope_handles_virtual_machines_with_real_permissions():
    """VM collision details follow the requester's constrained view grant."""
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.import_utils.collisions import scope_bulk_collisions

    visible = make_vm("visible-collision-vm")
    hidden = make_vm("hidden-collision-vm")
    user = make_user_with_perms("collision-vm-scope", [], plugin_write=False)
    user = grant(user, "view", VirtualMachine, constraints={"pk": visible.pk})
    collisions = [
        {
            "nb_device_pk": visible.pk,
            "nb_device_name": visible.name,
            "nb_model_name": "virtualmachine",
            "nb_kind": "virtual machine",
            "librenms_rows": [],
        },
        {
            "nb_device_pk": hidden.pk,
            "nb_device_name": hidden.name,
            "nb_model_name": "virtualmachine",
            "nb_kind": "virtual machine",
            "librenms_rows": [],
        },
    ]

    scoped = scope_bulk_collisions(collisions, user)

    assert scoped[0]["target_visible"] is True
    assert scoped[0]["nb_device_pk"] == visible.pk
    assert scoped[0]["nb_device_name"] == visible.name
    assert scoped[1]["target_visible"] is False
    assert scoped[1]["nb_device_pk"] is None
    assert scoped[1]["nb_device_name"] == "restricted NetBox object"
    assert scoped[1]["nb_model_name"] is None


@pytest.mark.django_db
def test_background_collision_gate_uses_job_user_scope(monkeypatch):
    """Verify the real job runner blocks device and VM imports without exposing targets outside the user's view scope."""
    from core.models import Job
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin import librenms_api as librenms_api_module
    from netbox_librenms_plugin.jobs import ImportDevicesJob

    target = make_device("visible-job-collision-target")
    hidden = make_device("hidden-job-collision-target")
    user = make_user_with_perms("job-collision-scope", [])
    user = grant(user, "add", Device)
    user = grant(user, "change", Device, constraints={"pk": target.pk})
    user = grant(user, "view", Device, constraints={"pk": target.pk})
    user = grant(user, "add", VirtualMachine)
    rows = _collision_rows(96301, 96302, target.name)
    rows.update(_collision_rows(96304, 96305, hidden.name))
    rows[96303] = {
        "device_id": 96303,
        "hostname": "unique-job-vm-row",
        "sysName": "unique-job-vm-row",
        "serial": "",
        "hardware": "Review hardware",
        "location": "Review location",
        "os": "review-os",
    }
    api = _LibreNMSBoundary(rows)
    monkeypatch.setattr(librenms_api_module, "LibreNMSAPI", lambda server_key=None: api)
    job_row = Job.objects.create(
        name="Bulk collision scope regression",
        user=user,
        job_id=uuid4(),
        data={},
    )
    device_count = Device.objects.count()
    vm_count = VirtualMachine.objects.count()

    ImportDevicesJob(job_row).run(
        request=queued_request(job_row.user),
        import_plans=[
            {
                "source_device_id": device_id,
                "object_type": "device",
                "role_id": None,
                "rack_id": None,
            }
            for device_id in (96301, 96302, 96304, 96305)
        ]
        + [
            {
                "source_device_id": 96303,
                "object_type": "virtualmachine",
                "placement": {"method": "cluster", "cluster_id": 1},
                "role_id": None,
            }
        ],
        server_key="default",
        libre_devices_cache=rows,
    )

    job_row.refresh_from_db()
    errors = job_row.data["errors"]
    assert Device.objects.count() == device_count
    assert VirtualMachine.objects.count() == vm_count
    assert {entry["device_id"] for entry in errors} == {96301, 96302, 96303, 96304, 96305}
    assert all("Bulk import blocked" in entry["error"] for entry in errors)
    # Both collisions block the batch...
    assert all("2 NetBox object collision(s)" in entry["error"] for entry in errors)
    # ...and the trailing period pins the list to the visible pk alone.
    assert all(f"Visible pk(s): {target.pk}." in entry["error"] for entry in errors)
    assert all(hidden.name not in entry["error"] for entry in errors)
    assert job_row.data["failed_count"] == 5
    assert job_row.data["success_count"] == 0


@pytest.mark.django_db
def test_unresolved_warning_does_not_claim_an_existing_row_imported(monkeypatch):
    """The precheck warning must not report success before the real importer finishes."""
    from dcim.models import Device

    from netbox_librenms_plugin.import_utils import bulk_import as bulk_import_module
    from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

    existing = make_device("existing-row-is-skipped")
    rows = _collision_rows(96101, 96102, existing.name)
    rows.pop(96102)
    _cache_rows(rows)
    api = _LibreNMSBoundary(rows)
    monkeypatch.setattr(bulk_import_module, "LibreNMSAPI", lambda server_key=None: api)
    request = make_request(
        data={"select": ["96101", "96102"], "server_key": "default"},
        user=make_superuser("bulk-result-superuser"),
        path="/bulk-import/",
        HTTP_HX_REQUEST="true",
    )
    view = BulkImportDevicesView()
    view._librenms_api = api
    device_count = Device.objects.count()

    response = post(view, request)

    html = response.content.decode()
    assert response.status_code == 200
    assert Device.objects.count() == device_count
    assert "Successfully imported" not in html
    assert "Skipped 1 selected row(s) (id(s): 96102)" in html
    assert "Skipped 1 existing device" in html
    assert "remaining rows were imported" not in html
    assert "continue through normal import checks" in html


CHASSIS_CONFLICT = (
    "Imported device {}, but another operation was changing the same NetBox objects, "
    "so its virtual chassis was not created."
)


def _importable_row(live_librenms, device_id, tag, *, members=None):
    """Serve one importable LibreNMS device; with *members* it is a stack. Returns its mappings and row."""
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import (
        _libre_device,
        _prerequisites,
        _register_stack,
    )

    prerequisites = _prerequisites(tag)
    # A stack's LibreNMS device serial is the master member's serial.
    serial = members[0]["entPhysicalSerialNum"] if members else ""
    row = _libre_device(
        device_id,
        f"{tag}-{device_id}",
        hardware=prerequisites["hardware"],
        serial=serial,
        location=prerequisites["location"],
    )
    if members is None:
        live_librenms.server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]})
        live_librenms.server.vc_inventory_callable(device_id, [], {})
    else:
        _register_stack(live_librenms, device_id, row, members)
    return prerequisites, row


@transactional_db_with_all_apps()
def test_a_lock_conflict_in_a_device_create_fails_only_that_row_with_the_try_again_text(live_librenms):
    """The device's own transaction rolls back; its row never shows PostgreSQL's text."""
    from dcim.models import Device, Site

    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

    prerequisites, row = _importable_row(live_librenms, 96301, "import-conflict")

    with second_connection() as other:
        # The new device's site key check waits for this lock.
        lock_row(other, Site, prerequisites["site_id"])
        with lock_timeout(200):
            result = bulk_import_devices_shared(
                [96301],
                server_key="default",
                manual_mappings_per_device={96301: prerequisites},
                libre_devices_cache={96301: row},
                user=make_superuser("import-conflict-user"),
            )

    assert result["success"] == []
    assert result["failed"] == [{"device_id": 96301, "error": TRY_AGAIN_MESSAGE}]
    assert not Device.objects.filter(name=row["hostname"]).exists()


@pytest.mark.django_db
def test_a_lock_conflict_in_the_chassis_create_reports_the_imported_device(live_librenms, caplog):
    """The device committed in its own transaction, so the warning says it was imported and the chassis was not."""
    from dcim.models import Device, VirtualChassis

    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    members = [_chassis(100, "SN-CONFLICT-A", position=1), _chassis(200, "SN-CONFLICT-B", position=2)]
    prerequisites, row = _importable_row(live_librenms, 96311, "stack-conflict", members=members)

    with (
        caplog.at_level("WARNING", logger="netbox_librenms_plugin.import_utils.bulk_import"),
        failing_statement(lambda sql, params: sql.startswith('INSERT INTO "dcim_virtualchassis"'), "40P01"),
    ):
        result = bulk_import_devices_shared(
            [96311],
            server_key="default",
            manual_mappings_per_device={96311: prerequisites},
            libre_devices_cache={96311: row},
            user=make_superuser("stack-conflict-user"),
        )

    assert [entry["device_id"] for entry in result["success"]] == [96311]
    assert result["virtual_chassis_created"] == 0
    assert Device.objects.filter(name=row["hostname"], virtual_chassis__isnull=True).exists()
    assert not VirtualChassis.objects.filter(domain="librenms-default-96311").exists()
    assert CHASSIS_CONFLICT.format(96311) in caplog.messages


@pytest.mark.django_db
@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_the_import_answer_names_a_chassis_that_a_lock_conflict_left_uncreated(client, live_librenms, htmx):
    """The device is imported, so the answer says so; it must also say that the chassis is missing."""
    from dcim.models import Device

    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    device_id = 96341 if htmx else 96331
    members = [_chassis(100, f"SN-ANSWER-A-{htmx}", position=1), _chassis(200, f"SN-ANSWER-B-{htmx}", position=2)]
    prerequisites, row = _importable_row(live_librenms, device_id, f"stack-answer-{int(htmx)}", members=members)
    client.force_login(make_superuser(f"stack-answer-{int(htmx)}-user"))
    headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}

    with failing_statement(lambda sql, params: sql.startswith('INSERT INTO "dcim_virtualchassis"'), "40P01"):
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
            {
                "server_key": "default",
                "select": [str(device_id)],
                f"role_{device_id}": str(prerequisites["device_role_id"]),
            },
            **headers,
        )

    assert Device.objects.filter(name=row["hostname"], virtual_chassis__isnull=True).exists()
    warning = CHASSIS_CONFLICT.format(device_id)
    if htmx:
        assert response.status_code == 200
        assert "Successfully imported 1 LibreNMS device" in response.content.decode()
        assert warning in response.content.decode()
    else:
        assert response.status_code == 302
        assert messages_on(response.wsgi_request) == [
            ("success", "Successfully imported 1 LibreNMS device"),
            ("warning", warning),
        ]


MASTER_UNKNOWN = "Imported device {} without a virtual chassis: the stack master could not be identified by serial."


@pytest.mark.django_db
@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_a_stack_whose_master_is_unknown_imports_standalone_and_says_why(client, live_librenms, htmx):
    """No member carries the device serial, so the import creates no chassis and no member devices."""
    from dcim.models import Device, VirtualChassis

    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    device_id = 96361 if htmx else 96351
    member_serial = f"SN-UNKNOWN-A-{int(htmx)}"
    members = [_chassis(100, member_serial, position=1), _chassis(200, "", position=2)]
    prerequisites, row = _importable_row(live_librenms, device_id, f"stack-unknown-{int(htmx)}", members=members)
    row["serial"] = "ROOT"
    live_librenms.server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]})
    client.force_login(make_superuser(f"stack-unknown-{int(htmx)}-user"))
    headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
        {
            "server_key": "default",
            "select": [str(device_id)],
            f"role_{device_id}": str(prerequisites["device_role_id"]),
        },
        **headers,
    )

    imported = Device.objects.get(serial="ROOT")
    assert imported.name == row["hostname"]
    assert imported.virtual_chassis is None
    assert not VirtualChassis.objects.filter(domain=f"librenms-default-{device_id}").exists()
    assert not Device.objects.filter(serial=member_serial).exists()
    warning = MASTER_UNKNOWN.format(device_id)
    if htmx:
        assert warning in response.content.decode()
    else:
        assert ("warning", warning) in messages_on(response.wsgi_request)


def _import_stack(live_librenms, device_id, tag, members, *, serial, hardware=None, device_type_id=True):
    """Import one LibreNMS stack row through the real bulk import; return the result and its row."""
    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

    prerequisites, row = _importable_row(live_librenms, device_id, tag, members=members)
    row["serial"] = serial
    if hardware is not None:
        row["hardware"] = hardware
    live_librenms.server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]})
    mappings = dict(prerequisites)
    if not device_type_id:
        mappings.pop("device_type_id")
    result = bulk_import_devices_shared(
        [device_id],
        server_key="default",
        manual_mappings_per_device={device_id: mappings},
        libre_devices_cache={device_id: row},
        user=make_superuser(f"{tag}-user"),
    )
    return result, prerequisites, row


@pytest.mark.django_db
def test_two_members_with_the_master_serial_import_standalone_without_a_duplicate(live_librenms):
    """Two chassis rows report the device serial: no single master, so no chassis and no second device."""
    from dcim.models import Device, VirtualChassis

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    NormalizationRule.objects.get_or_create(
        scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
    )
    members = [_chassis(100, "S/N DUP-A", position=1), _chassis(200, "S/N DUP-A", position=2)]

    result, _prerequisites, _row = _import_stack(live_librenms, 96371, "stack-dup-master", members, serial="S/N DUP-A")

    assert [entry["device_id"] for entry in result["success"]] == [96371]
    assert result["virtual_chassis_created"] == 0
    assert result["warnings"] == [MASTER_UNKNOWN.format(96371)]
    assert not VirtualChassis.objects.filter(domain="librenms-default-96371").exists()
    assert Device.objects.filter(serial__in=["DUP-A", "S/N DUP-A"]).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("device_type_source", ["mapped", "manual"])
def test_a_manufacturer_serial_rule_identifies_the_master(live_librenms, device_type_source):
    """A rule scoped to the DeviceType's manufacturer strips the member serials, so the master matches."""
    from dcim.models import Device, DeviceType, VirtualChassis

    from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    device_id = 96381 if device_type_source == "mapped" else 96382
    members = [_chassis(100, "PREFIX:SN1", position=1), _chassis(200, f"PREFIX:SN2-{device_id}", position=2)]
    hardware = f"HW-{device_type_source.upper()}-{device_id}"
    prerequisites, _row = _importable_row(live_librenms, device_id, f"stack-mfg-{device_type_source}", members=members)
    device_type = DeviceType.objects.get(pk=prerequisites["device_type_id"])
    NormalizationRule.objects.create(
        scope="serial", manufacturer=device_type.manufacturer, match_pattern=r"^PREFIX:(.+)$", replacement=r"\1"
    )
    if device_type_source == "mapped":
        DeviceTypeMapping.objects.create(librenms_hardware=hardware.lower(), netbox_device_type=device_type)

    result, _prerequisites, _row = _import_stack(
        live_librenms,
        device_id,
        f"stack-mfg-{device_type_source}-row",
        members,
        serial="SN1",
        hardware=hardware,
        device_type_id=device_type_source == "manual",
    )

    assert [entry["device_id"] for entry in result["success"]] == [device_id]
    assert result["virtual_chassis_created"] == 1
    chassis = VirtualChassis.objects.get(domain=f"librenms-default-{device_id}")
    assert sorted(Device.objects.filter(virtual_chassis=chassis).values_list("serial", flat=True)) == [
        "SN1",
        f"SN2-{device_id}",
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("override", [None, 0, "not-an-id"], ids=["none", "missing", "garbage"])
def test_an_unusable_device_type_override_fails_the_row_in_precheck_and_import(live_librenms, override):
    """The precheck and the device write read one effective DeviceType, so both reject the override."""
    from dcim.models import Device, DeviceType

    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared
    from netbox_librenms_plugin.import_utils.device_operations import import_single_device
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    device_id = 96391
    members = [_chassis(100, "OVR-A", position=1), _chassis(200, "OVR-B", position=2)]
    prerequisites, row = _importable_row(live_librenms, device_id, f"stack-override-{override}", members=members)
    if override == 0:
        override = DeviceType.objects.order_by("-pk").values_list("pk", flat=True).first() + 100000
    mappings = {**prerequisites, "device_type_id": override}

    result = bulk_import_devices_shared(
        [device_id],
        server_key="default",
        manual_mappings_per_device={device_id: mappings},
        libre_devices_cache={device_id: row},
        user=make_superuser(f"stack-override-{device_id}-user"),
    )
    single = import_single_device(
        device_id, server_key="default", manual_mappings=mappings, libre_device=row, user=make_superuser("ovr-single")
    )

    assert result["success"] == []
    assert result["failed"] == [{"device_id": device_id, "error": "Selected device type is unavailable"}]
    assert single["success"] is False
    assert single["error"] == "Selected device type is unavailable"
    assert not Device.objects.filter(name=row["hostname"]).exists()


@pytest.mark.django_db
def test_a_failed_module_bay_count_write_fails_the_chassis_create():
    """The count write is part of the chassis transaction: its error must fail the create, not break it silently."""
    from dcim.models import VirtualChassis
    from django.db import OperationalError

    from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
    from netbox_librenms_plugin.transactions import classify_conflict

    master = make_device("bay-count-conflict-master", serial="MASTER-SERIAL")
    master.module_bay_count = 5  # Differs from the real bay count, so the create writes the count.

    with (
        pytest.raises(OperationalError) as caught,
        failing_statement(
            lambda sql, params: sql.startswith('UPDATE "dcim_device" SET "module_bay_count"'), "40P01"
        ) as failed,
    ):
        create_virtual_chassis_with_members(
            master,
            [{"serial": "MASTER-SERIAL", "position": 1}, {"serial": "MEMBER-SERIAL", "position": 2}],
            {"device_id": 96321},
            server_key="default",
        )

    assert failed, "precondition: the count write ran"
    assert classify_conflict(caught.value)
    assert not VirtualChassis.objects.filter(domain="librenms-default-96321").exists()


@pytest.mark.django_db
def test_collision_precheck_skips_import_prerequisite_queries():
    """Collision matching must not query site, type, role, or platform import prerequisites."""
    from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

    target_a = make_device("collision-query-target-a")
    make_device("collision-query-target-b")
    rows = {
        96201: _collision_rows(96201, 96202, "collision-query-target-a")[96201],
        96202: _collision_rows(96201, 96202, "collision-query-target-b")[96202],
        96203: {
            "device_id": 96203,
            "hostname": "unmatched-collision-query-vm",
            "sysName": "unmatched-collision-query-vm",
            "serial": "",
            "hardware": "Review hardware",
            "location": "Review location",
            "os": "review-os",
        },
    }
    rows[96201]["location"] = target_a.site.name
    api = _LibreNMSBoundary(rows)

    with CaptureQueriesContext(connection) as captured:
        collisions, unresolved = detect_collisions_for_device_ids(
            [96201, 96202, 96203],
            api,
            libre_devices_cache=rows,
            sync_options={"use_sysname": True},
            vm_device_ids={96203},
        )

    assert collisions == []
    assert unresolved == []
    import_prerequisite_tables = {
        "dcim_devicetype",
        "dcim_devicerole",
        "dcim_platform",
        "dcim_rack",
        "dcim_site",
        "netbox_librenms_plugin_devicetypemapping",
        "netbox_librenms_plugin_normalizationrule",
        "netbox_librenms_plugin_platformmapping",
        "virtualization_cluster",
    }
    unexpected = [
        query["sql"]
        for query in captured.captured_queries
        if any(table in query["sql"].lower() for table in import_prerequisite_tables)
    ]
    assert unexpected == []
