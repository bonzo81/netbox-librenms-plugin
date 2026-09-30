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


def _importable_row(live_librenms, device_id, tag, *, members=None):
    """Serve one importable LibreNMS device; with *members* it is a stack. Returns its mappings and row."""
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import (
        _libre_device,
        _prerequisites,
        _register_stack,
    )

    prerequisites = _prerequisites(tag)
    row = _libre_device(
        device_id, f"{tag}-{device_id}", hardware=prerequisites["hardware"], location=prerequisites["location"]
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
    assert (
        "Imported device 96311, but another operation was changing the same NetBox objects, "
        "so its virtual chassis was not created."
    ) in caplog.messages


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
        collisions, unresolved, _stack_ambiguities = detect_collisions_for_device_ids(
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
