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


CHASSIS_CONFLICT = f"Cannot import stack device {{}}: {TRY_AGAIN_MESSAGE}"


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


@pytest.mark.django_db
def test_bulk_import_reports_duplicate_serial_as_terminal_ambiguity(live_librenms):
    from dcim.models import Device, Site

    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

    device_id = 96301
    prerequisites, row = _importable_row(live_librenms, device_id, "bulk-duplicate-serial")
    serial = "BULK-SHARED-SERIAL"
    owner = make_device("bulk-duplicate-serial-owner", serial=serial)
    other_site = Site.objects.create(name="Bulk duplicate comparison", slug="bulk-duplicate-comparison")
    Device.objects.create(
        name=owner.name, serial=serial, site=other_site, device_type=owner.device_type, role=owner.role, status="active"
    )
    import_site = Site.objects.create(name="Bulk duplicate destination", slug="bulk-duplicate-destination")
    user = make_superuser("bulk-duplicate-serial-user")

    def import_row(payload):
        live_librenms.server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [payload]})
        return bulk_import_devices_shared(
            [device_id],
            server_key="default",
            manual_mappings_per_device={
                device_id: {
                    "site_id": import_site.pk,
                    "device_type_id": prerequisites["device_type_id"],
                    "device_role_id": prerequisites["device_role_id"],
                }
            },
            user=user,
        )

    serial_conflict = import_row({**row, "serial": serial})
    hostname_conflict = import_row({**row, "hostname": owner.name, "sysName": owner.name})

    assert serial_conflict["success"] == []
    assert serial_conflict["skipped"] == []
    assert not Device.objects.filter(name=row["hostname"]).exists()
    assert len(hostname_conflict["failed"]) == 1
    assert serial_conflict["failed"] == hostname_conflict["failed"]

    Device.objects.filter(serial=serial).update(serial="")
    resolved = import_row({**row, "serial": serial})

    assert resolved["failed"] == []
    assert [entry["device_id"] for entry in resolved["success"]] == [device_id]
    imported = Device.objects.get(name=row["hostname"])
    assert imported.serial == serial
    assert imported.role_id == prerequisites["device_role_id"]


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["device", "stack", "vm", "missing-os"])
def test_a_platform_warning_failure_leaves_the_import_retryable(client, live_librenms, kind, flushed_events):
    from dcim.models import Device, DeviceRole, DeviceType, Platform, Site, VirtualChassis
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.models import LibreNMSSettings
    from netbox_librenms_plugin.server_mappings import MappingRole, find_mapping
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    device_id = 96302
    tag = f"platform-warning-{kind}"
    members = None
    if kind == "stack":
        members = [_chassis(100, "WARNING-MASTER", position=1), _chassis(200, "WARNING-MEMBER", position=2)]
    prerequisites, row = _importable_row(live_librenms, device_id, tag, members=members)
    row.update(os=f"{tag}-os", version="7.2.3")
    live_librenms.server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]})

    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"platform_version_tracking": True})
    platform = None
    slug = f"{tag}-scope"
    if kind != "missing-os":
        platform = Platform.objects.create(name=row["os"], slug=slug)
    user = make_user_with_perms(
        tag,
        [
            ("add", Device),
            ("change", Device),
            ("view", Site),
            ("view", DeviceType),
            ("view", DeviceRole),
            ("add", VirtualChassis),
            ("add", VirtualMachine),
        ],
    )
    client.force_login(grant(user, "view", Platform, constraints={"slug": slug}))

    model = VirtualMachine if kind == "vm" else Device
    data = {"server_key": "default", "select": [str(device_id)], f"role_{device_id}": prerequisites["device_role_id"]}
    if kind == "vm":
        data.update({f"object_type_{device_id}": "virtualmachine", f"vm_placement_{device_id}": "site"})
    url = reverse("plugins:netbox_librenms_plugin:bulk_import_devices")

    def mapped_object():
        return find_mapping(model.objects.all(), server="default", identity=device_id, roles=[MappingRole.OWN])

    def is_visibility_check(sql, params):
        return 'AS "a" FROM "dcim_platform"' in sql and slug in params

    with failing_statement(is_visibility_check, "57014") as failed:
        response = client.post(url, data)

    assert response.status_code == 302
    if platform is not None:
        assert len(failed) == 1
        assert [level for level, _ in messages_on(response.wsgi_request)] == ["error"]
        assert mapped_object() is None
        assert not model.objects.filter(name__startswith=row["hostname"]).exists()
        if kind == "vm":
            assert not any(event_model == "virtualmachine" for event_model, _pk in flushed_events)
        if members:
            assert not VirtualChassis.objects.filter(master__serial="WARNING-MASTER").exists()

        response = client.post(url, data)
        assert response.status_code == 302
        assert [level for level, _ in messages_on(response.wsgi_request)][-2:] == ["success", "warning"]
    else:
        assert failed == []
        assert [level for level, _ in messages_on(response.wsgi_request)] == ["success"]

    imported = mapped_object()
    assert imported.platform == platform
    if kind == "vm":
        assert [pk for event_model, pk in flushed_events if event_model == "virtualmachine"] == [imported.pk]
    if members:
        chassis = imported.virtual_chassis
        assert chassis.master_id == imported.pk
        assert set(chassis.members.values_list("serial", flat=True)) == {"WARNING-MASTER", "WARNING-MEMBER"}


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
def test_a_lock_conflict_in_the_chassis_create_fails_the_stack_row_and_a_retry_imports_both(live_librenms):
    """The device and its chassis are one rollback unit, so the failed row leaves nothing and a retry creates both."""
    from dcim.models import Device, VirtualChassis

    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    members = [_chassis(100, "SN-CONFLICT-A", position=1), _chassis(200, "SN-CONFLICT-B", position=2)]
    prerequisites, row = _importable_row(live_librenms, 96311, "stack-conflict", members=members)

    def run():
        return bulk_import_devices_shared(
            [96311],
            server_key="default",
            manual_mappings_per_device={96311: prerequisites},
            libre_devices_cache={96311: row},
            user=make_superuser("stack-conflict-user"),
        )

    with failing_statement(lambda sql, params: sql.startswith('INSERT INTO "dcim_virtualchassis"'), "40P01") as failed:
        result = run()

    assert failed
    assert result["success"] == []
    assert result["warnings"] == []
    assert result["failed"] == [{"device_id": 96311, "error": CHASSIS_CONFLICT.format(96311)}]
    assert not Device.objects.filter(serial__in=["SN-CONFLICT-A", "SN-CONFLICT-B"]).exists()
    assert not VirtualChassis.objects.filter(domain="librenms-default-96311").exists()

    retry = run()

    assert [entry["device_id"] for entry in retry["success"]] == [96311]
    assert retry["virtual_chassis_created"] == 1
    chassis = VirtualChassis.objects.get(domain="librenms-default-96311")
    assert set(chassis.members.values_list("serial", flat=True)) == {"SN-CONFLICT-A", "SN-CONFLICT-B"}


@pytest.mark.django_db
def test_a_failed_last_member_insert_rolls_back_the_saved_members_and_a_retry_imports_the_stack(live_librenms):
    """The master, the chassis and the first member are saved before the last member fails, so all of them roll back."""
    from dcim.models import Device, Interface, InterfaceTemplate, VirtualChassis

    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    serials = ["SN-ROLLBACK-A", "SN-ROLLBACK-B", "SN-ROLLBACK-C"]
    members = [_chassis(100 * position, serial, position=position) for position, serial in enumerate(serials, start=1)]
    prerequisites, row = _importable_row(live_librenms, 96312, "stack-member-rollback", members=members)
    # Each saved device gets this interface from its type, so the rollback has an interface row to remove.
    InterfaceTemplate.objects.create(device_type_id=prerequisites["device_type_id"], name="eth0", type="1000base-t")
    written = (Device, VirtualChassis, Interface)
    before = {model: model.objects.count() for model in written}

    def run():
        return bulk_import_devices_shared(
            [96312],
            server_key="default",
            manual_mappings_per_device={96312: prerequisites},
            libre_devices_cache={96312: row},
            user=make_superuser("stack-member-rollback-user"),
        )

    device_insert = 'INSERT INTO "dcim_device"'
    chassis_insert = 'INSERT INTO "dcim_virtualchassis"'
    interface_insert = 'INSERT INTO "dcim_interface"'
    inserts = []
    interface_inserts = []

    def record_insert(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if sql.startswith((device_insert, chassis_insert)):
            inserts.append((sql, params))
        elif sql.startswith(interface_insert):
            interface_inserts.append(len(inserts))
        return result

    def last_member_insert(sql, params):
        return sql.startswith(device_insert) and serials[-1] in params

    with connection.execute_wrapper(record_insert), failing_statement(last_member_insert, "40P01") as failed:
        result = run()

    assert failed, "the last member INSERT never ran"
    master, chassis, member = inserts
    assert master[0].startswith(device_insert) and serials[0] in master[1]
    assert chassis[0].startswith(chassis_insert)
    assert member[0].startswith(device_insert) and serials[1] in member[1]
    # The master's interface insert ran before the last member failed.
    assert interface_inserts and interface_inserts[0] == 1
    assert result["success"] == []
    assert [entry["device_id"] for entry in result["failed"]] == [96312]
    assert {model: model.objects.count() for model in written} == before
    assert not Device.objects.filter(serial__in=serials).exists()
    assert not Device.objects.filter(name=row["hostname"]).exists()
    assert not VirtualChassis.objects.filter(domain="librenms-default-96312").exists()

    retry = run()

    assert [entry["device_id"] for entry in retry["success"]] == [96312]
    assert retry["virtual_chassis_created"] == 1
    chassis = VirtualChassis.objects.get(domain="librenms-default-96312")
    assert set(chassis.members.values_list("serial", flat=True)) == set(serials)


@pytest.mark.django_db
def test_an_unavailable_member_serial_rolls_back_the_stack_until_the_master_link_is_repaired(live_librenms):
    from dcim.models import Device, VirtualChassis

    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared
    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.mapping_fixtures import stored_mapping_for_test
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis
    from netbox_librenms_plugin.tests.test_coverage_device_operations import _stray_chassis_member

    serials = ["BROKEN-LINK-AAA", "BROKEN-LINK-BBB", "BROKEN-LINK-CCC"]
    members = [_chassis(100 * position, serial, position=position) for position, serial in enumerate(serials, start=1)]
    prerequisites, row = _importable_row(live_librenms, 96313, "stack-broken-member-link", members=members)
    stray = _stray_chassis_member("stack-stray", f"OLD-{serials[-1]}")
    NormalizationRule.objects.create(scope="serial", match_pattern=r"^OLD-(.+)$", replacement=r"\1")
    before = {model: set(model.objects.values_list("pk", flat=True)) for model in (Device, VirtualChassis)}
    user = make_superuser("stack-broken-member-link-user")

    def run():
        return bulk_import_devices_shared(
            [96313],
            server_key="default",
            manual_mappings_per_device={96313: prerequisites},
            libre_devices_cache={96313: row},
            user=user,
        )

    result = run()

    assert result["success"] == []
    assert [entry["device_id"] for entry in result["failed"]] == [96313]
    assert result["virtual_chassis_created"] == 0
    assert {model: set(model.objects.values_list("pk", flat=True)) for model in before} == before

    VirtualChassis.objects.filter(pk=stray.virtual_chassis_id).update(master=stray)
    Device.objects.filter(pk=stray.pk).update(serial="REPAIRED-HOLDER")
    retry = run()

    assert [entry["device_id"] for entry in retry["success"]] == [96313]
    assert retry["virtual_chassis_created"] == 1
    chassis = VirtualChassis.objects.get(domain="librenms-default-96313")
    assert stored_mapping_for_test(chassis.master) == {"default": 96313}
    assert set(chassis.members.values_list("serial", flat=True)) == set(serials)


@pytest.mark.django_db
def test_a_standalone_bulk_import_without_a_serial_skips_the_broken_rule(client, live_librenms):
    from dcim.models import Device, DeviceType

    from netbox_librenms_plugin.tests.mapping_fixtures import stored_mapping_for_test
    from netbox_librenms_plugin.tests.test_coverage_device_operations import _broken_serial_rule

    device_id = 96316
    prerequisites, row = _importable_row(live_librenms, device_id, "standalone-broken-rule")
    row["serial"] = "-"
    live_librenms.server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]})
    device_type = DeviceType.objects.get(pk=prerequisites["device_type_id"])
    _broken_serial_rule("standalone", manufacturer=device_type.manufacturer)
    client.force_login(make_superuser("standalone-broken-rule-user"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
        {
            "server_key": "default",
            "select": [str(device_id)],
            f"role_{device_id}": str(prerequisites["device_role_id"]),
        },
    )

    assert response.status_code == 302
    device = Device.objects.get(name=row["hostname"])
    assert device.device_type_id == device_type.pk
    assert device.serial == ""
    assert device.virtual_chassis_id is None
    assert stored_mapping_for_test(device) == {"default": device_id}


@pytest.mark.django_db
@pytest.mark.parametrize("stray_first", [False, True], ids=["holders-first", "stray-first"])
def test_an_unavailable_member_serial_rolls_back_the_stack_even_with_two_holders(
    client, live_librenms, stray_first, caplog
):
    from dcim.models import Device, VirtualChassis

    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis
    from netbox_librenms_plugin.tests.test_coverage_device_operations import _stray_chassis_member

    device_id = 96315
    serial = "STACK-MEMBER-BBB"
    members = [_chassis(100, "STACK-MASTER-AAA", position=1), _chassis(200, serial, position=2)]
    prerequisites, _ = _importable_row(live_librenms, device_id, "stack-ambiguous-member-link", members=members)
    if stray_first:
        stray = _stray_chassis_member("stack-stray", serial)
    holders = [make_device(f"stack-holder-{index}", serial=serial) for index in range(2)]
    if not stray_first:
        stray = _stray_chassis_member("stack-stray", serial)
    assert (stray.pk < holders[0].pk) if stray_first else (stray.pk > holders[-1].pk)
    before = {model: set(model.objects.values_list("pk", flat=True)) for model in (Device, VirtualChassis)}
    client.force_login(make_superuser("stack-ambiguous-member-link-user"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
        {
            "server_key": "default",
            "select": [str(device_id)],
            f"role_{device_id}": str(prerequisites["device_role_id"]),
        },
    )

    assert response.status_code == 302
    assert any(
        f"Cannot check VC member serial '{serial}'" in record.getMessage()
        for record in caplog.records
        if record.name == "netbox_librenms_plugin.import_utils.bulk_import"
    )
    assert {model: set(model.objects.values_list("pk", flat=True)) for model in before} == before


@pytest.mark.django_db
def test_a_stack_with_a_nul_in_a_member_serial_is_blocked_at_validation(live_librenms):
    """PostgreSQL text cannot hold a NUL, so the search shows the stack blocked and the import writes nothing."""
    from dcim.models import Device, VirtualChassis

    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared, process_device_filters
    from netbox_librenms_plugin.serial_identity import stored_form_refusal
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    nul_serial = "SN-NUL-B\x00X"
    members = [_chassis(100, "SN-NUL-A", position=1), _chassis(200, nul_serial, position=2)]
    prerequisites, row = _importable_row(live_librenms, 96321, "stack-nul", members=members)
    live_librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [row]})
    refusal = stored_form_refusal(nul_serial, Device)
    assert refusal

    (validated,) = process_device_filters(
        live_librenms.api, {}, vc_detection_enabled=True, clear_cache=True, show_disabled=True
    )

    # The validation itself names the member serial, so the search never offers the stack as ready.
    validation = validated["_validation"]
    assert (validation["can_import"], validation["is_ready"]) == (False, False)
    assert any(refusal in str(issue) for issue in validation["issues"])

    result = bulk_import_devices_shared(
        [96321],
        server_key="default",
        manual_mappings_per_device={96321: prerequisites},
        libre_devices_cache={96321: row},
        user=make_superuser("stack-nul-user"),
    )

    assert result["success"] == []
    assert [entry["device_id"] for entry in result["failed"]] == [96321]
    assert refusal in result["failed"][0]["error"]
    assert not Device.objects.filter(name=row["hostname"]).exists()
    assert not VirtualChassis.objects.filter(domain="librenms-default-96321").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_the_import_answer_fails_a_stack_row_whose_chassis_create_failed(client, live_librenms, htmx):
    """The failed chassis rolls the device back, so the answer reports a failed row and no import."""
    from dcim.models import Device

    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    device_id = 96341 if htmx else 96331
    members = [_chassis(100, f"SN-ANSWER-A-{htmx}", position=1), _chassis(200, f"SN-ANSWER-B-{htmx}", position=2)]
    prerequisites, row = _importable_row(live_librenms, device_id, f"stack-answer-{int(htmx)}", members=members)
    client.force_login(make_superuser(f"stack-answer-{int(htmx)}-user"))
    headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}

    with failing_statement(lambda sql, params: sql.startswith('INSERT INTO "dcim_virtualchassis"'), "40P01") as failed:
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
            {
                "server_key": "default",
                "select": [str(device_id)],
                f"role_{device_id}": str(prerequisites["device_role_id"]),
            },
            **headers,
        )

    assert failed
    assert not Device.objects.filter(serial__in=[f"SN-ANSWER-A-{htmx}", f"SN-ANSWER-B-{htmx}"]).exists()
    if htmx:
        assert response.status_code == 200
        assert "Successfully imported" not in response.content.decode()
        assert "Failed to import 1 device" in response.content.decode()
    else:
        assert response.status_code == 302
        assert messages_on(response.wsgi_request) == [("error", "Failed to import 1 device")]


@pytest.mark.django_db
@pytest.mark.parametrize("holder_count", [1, 2])
def test_a_confirmed_member_serial_conflict_skips_only_that_member(live_librenms, caplog, holder_count):
    from dcim.models import Device, VirtualChassis

    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared
    from netbox_librenms_plugin.tests.mapping_fixtures import stored_mapping_for_test
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    serials = ["TAKEN-STACK-AAA", "TAKEN-STACK-BBB", "TAKEN-STACK-CCC"]
    members = [_chassis(100 * position, serial, position=position) for position, serial in enumerate(serials, start=1)]
    device_id = 96314
    prerequisites, row = _importable_row(live_librenms, device_id, "stack-taken-member", members=members)
    holders = {make_device(f"stack-taken-holder-{index}", serial=serials[-1]).pk for index in range(holder_count)}

    result = bulk_import_devices_shared(
        [device_id],
        server_key="default",
        manual_mappings_per_device={device_id: prerequisites},
        libre_devices_cache={device_id: row},
        user=make_superuser("stack-taken-member-user"),
    )

    assert result["failed"] == []
    assert [entry["device_id"] for entry in result["success"]] == [device_id]
    assert result["virtual_chassis_created"] == 1
    chassis = VirtualChassis.objects.get(domain=f"librenms-default-{device_id}")
    assert stored_mapping_for_test(chassis.master) == {"default": device_id}
    assert set(chassis.members.values_list("serial", flat=True)) == set(serials[:-1])
    assert set(Device.objects.filter(serial=serials[-1]).values_list("pk", flat=True)) == holders
    assert any(
        record.name == "netbox_librenms_plugin.import_utils.virtual_chassis"
        and record.levelname == "WARNING"
        and serials[-1] in record.getMessage()
        for record in caplog.records
    )


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
    """A rule scoped to the DeviceType's manufacturer keys the member serials, so the master matches; members store raw."""
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
        f"PREFIX:SN2-{device_id}",
        "SN1",
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

    from netbox_librenms_plugin.tests.conftest import create_prepared_stack
    from netbox_librenms_plugin.transactions import classify_conflict

    master = make_device("bay-count-conflict-master", serial="MASTER-SERIAL")
    master.module_bay_count = 5  # Differs from the real bay count, so the create writes the count.

    with (
        pytest.raises(OperationalError) as caught,
        failing_statement(
            lambda sql, params: sql.startswith('UPDATE "dcim_device" SET "module_bay_count"'), "40P01"
        ) as failed,
    ):
        members = [{"serial": "MASTER-SERIAL", "position": 1}, {"serial": "MEMBER-SERIAL", "position": 2}]
        create_prepared_stack(master, members, {"device_id": 96321}, server_key="default")

    assert failed, "precondition: the count write ran"
    assert classify_conflict(caught.value)
    assert not VirtualChassis.objects.filter(domain="librenms-default-96321").exists()


@pytest.mark.django_db
def test_the_collision_scan_leaves_a_row_with_an_unavailable_serial_unresolved(live_librenms):
    """The scan cannot check a serial whose lookup is Unavailable, so the precheck must skip that row."""
    from netbox_librenms_plugin.import_utils.bulk_import import (
        NETBOX_SERIAL_UNVERIFIED,
        classify_bulk_precheck,
        detect_collisions_for_device_ids,
    )
    from netbox_librenms_plugin.tests.test_coverage_device_operations import _stray_chassis_member

    _stray_chassis_member("scan-stray", "STRAY-SCAN-1")
    rows = {
        96401: {
            "device_id": 96401,
            "hostname": "scan-stray-row",
            "sysName": "scan-stray-row",
            "serial": "STRAY-SCAN-1",
            "hardware": "Review hardware",
            "location": "Review location",
            "os": "review-os",
        }
    }

    collisions, unresolved = detect_collisions_for_device_ids([96401], live_librenms.api, libre_devices_cache=rows)

    assert (collisions, unresolved) == ([], {96401: NETBOX_SERIAL_UNVERIFIED})
    assert classify_bulk_precheck(collisions, unresolved, [96401], {}).importable_device_ids == []
    assert not live_librenms.server.requests


@pytest.mark.django_db
@pytest.mark.parametrize("background", [False, True], ids=["view", "job"])
def test_bulk_import_reports_the_unverified_serial_without_netbox_details(client, live_librenms, caplog, background):
    from core.models import Job
    from dcim.models import Device, DeviceRole, DeviceType, Location, Manufacturer, Platform, Site, VirtualChassis

    from netbox_librenms_plugin.import_utils.bulk_import import NETBOX_SERIAL_UNVERIFIED
    from netbox_librenms_plugin.jobs import ImportDevicesJob
    from netbox_librenms_plugin.tests.mapping_fixtures import stored_mapping_for_test
    from netbox_librenms_plugin.tests.test_coverage_device_operations import _stray_chassis_member

    blocked_id, ready_id = 96402, 96403
    member = _stray_chassis_member("private-precheck", "STRAY-PRECHECK")
    _, blocked_row = _importable_row(live_librenms, blocked_id, "unverified-serial")
    blocked_row["serial"] = member.serial
    live_librenms.server.register(f"/api/v0/devices/{blocked_id}", {"status": "ok", "devices": [blocked_row]})
    prerequisites, ready_row = _importable_row(live_librenms, ready_id, "checked-serial")
    role_id = prerequisites["device_role_id"]
    if background:
        user = make_superuser("unverified-serial-user")
    else:
        # The requester may import the ready row but may not view the holder or its chassis.
        user = make_user_with_perms(
            "unverified-serial-user",
            [("view", Device), ("add", Device), ("change", Device)],
            constraints={"name": ready_row["hostname"]},
        )
        for model in (Site, Location, DeviceType, DeviceRole, Manufacturer, Platform):
            user = grant(user, "view", model)
        chassis = member.virtual_chassis
        hidden_pks = (member.pk, chassis.master_id)
        assert not Device.objects.restrict(user, "view").filter(pk__in=hidden_pks).exists()
        assert not VirtualChassis.objects.restrict(user, "view").filter(pk=chassis.pk).exists()

    if background:
        job = Job.objects.create(name="Bulk serial precheck", user=user, job_id=uuid4(), data={})
        ImportDevicesJob(job).run(
            request=queued_request(user),
            import_plans=[
                {"source_device_id": device_id, "object_type": "device", "role_id": role_id, "rack_id": None}
                for device_id in (blocked_id, ready_id)
            ],
            server_key="default",
            libre_devices_cache={},
        )
        job.refresh_from_db()
        (error,) = job.data["errors"]
        assert error["device_id"] == blocked_id
        assert error["error"] == NETBOX_SERIAL_UNVERIFIED
        warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    else:
        client.force_login(user)
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
            {
                "server_key": "default",
                "select": [str(blocked_id), str(ready_id)],
                f"role_{blocked_id}": str(role_id),
                f"role_{ready_id}": str(role_id),
            },
        )
        assert response.status_code == 302
        warnings = [message for level, message in messages_on(response.wsgi_request) if level == "warning"]

    assert not Device.objects.filter(name=blocked_row["hostname"]).exists()
    assert stored_mapping_for_test(Device.objects.get(name=ready_row["hostname"])) == {"default": ready_id}
    (warning,) = (message for message in warnings if str(blocked_id) in message)
    assert NETBOX_SERIAL_UNVERIFIED in warning
    chassis = member.virtual_chassis
    chassis.refresh_from_db()
    hidden_names = (member.name, chassis.name, chassis.master.name)
    assert [(message, name) for message in warnings for name in hidden_names if name in message] == []


@pytest.mark.django_db
def test_collision_precheck_reads_the_serial_rules_once_for_the_batch(live_librenms):
    """Each Device row with a real serial needs the serial rules, so the batch must share one read of them."""
    from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids
    from netbox_librenms_plugin.models import NormalizationRule

    make_device("serial-rules-once-a", serial="SERIAL-RULES-ONCE-A")
    make_device("serial-rules-once-b", serial="SERIAL-RULES-ONCE-B")
    rows = _collision_rows(96211, 96212, "serial-rules-once-a")
    rows[96212].update(hostname="serial-rules-once-b", sysName="serial-rules-once-b")
    rows[96211]["serial"] = "SERIAL-RULES-ONCE-A"
    rows[96212]["serial"] = "SERIAL-RULES-ONCE-B"
    for device_id, row in rows.items():
        live_librenms.server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]})
        live_librenms.server.vc_inventory_callable(device_id, [], {})

    with CaptureQueriesContext(connection) as reference:
        list(NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL))
    (serial_only_query,) = (query["sql"] for query in reference.captured_queries)
    with CaptureQueriesContext(connection) as captured:
        collisions, unresolved = detect_collisions_for_device_ids(
            [96211, 96212], live_librenms.api, libre_devices_cache=rows, sync_options={"use_sysname": True}
        )

    assert (collisions, unresolved) == ([], {})
    rule_table = f'"{NormalizationRule._meta.db_table}"'
    assert [query["sql"] for query in captured.captured_queries if rule_table in query["sql"]] == [serial_only_query]
    assert [
        (request["path"], request["query"])
        for request in live_librenms.server.requests
        if request["path"].startswith("/api/v0/inventory/")
    ] == [
        (f"/api/v0/inventory/{device_id}{suffix}", query)
        for device_id in rows
        for suffix, query in [("", {"entPhysicalContainedIn": ["0"]}), ("/all", {})]
    ]


@pytest.mark.django_db
def test_collision_precheck_continues_when_the_vc_cache_stamp_cannot_read_rules(live_librenms):
    from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids
    from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data
    from netbox_librenms_plugin.tests.lock_conflict_helpers import aborting_statement
    from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis

    first_id, second_id = 96213, 96214
    members = [_chassis(100, "STAMP-AAA", position=1), _chassis(200, "STAMP-BBB", position=2)]
    _, first = _importable_row(live_librenms, first_id, "vc-stamp-first", members=members)
    _, second = _importable_row(live_librenms, second_id, "vc-stamp-second")
    first["serial"] = ""
    live_librenms.api.cache_timeout = 300
    assert get_virtual_chassis_data(live_librenms.api, first_id, force_refresh=True)["is_stack"]
    live_librenms.server.requests.clear()

    with aborting_statement(
        lambda sql, params: sql.startswith("SELECT") and '"netbox_librenms_plugin_normalizationrule"' in sql
    ) as failed:
        collisions, unresolved = detect_collisions_for_device_ids(
            [first_id, second_id], live_librenms.api, libre_devices_cache={first_id: first, second_id: second}
        )

    assert failed
    assert (collisions, list(unresolved)) == ([], [first_id])
    assert {request["path"] for request in live_librenms.server.requests} == {
        f"/api/v0/devices/{second_id}",
        f"/api/v0/inventory/{second_id}",
        f"/api/v0/inventory/{second_id}/all",
    }


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
    assert unresolved == {}
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
    # The serial rules are identity input, not a prerequisite: the batch reads them once, in the serial-only query.
    from netbox_librenms_plugin.models import NormalizationRule

    with CaptureQueriesContext(connection) as reference:
        list(NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL))
    (serial_only_query,) = (query["sql"] for query in reference.captured_queries)
    serial_rule_reads = [query["sql"] for query in captured.captured_queries if query["sql"] == serial_only_query]
    assert len(serial_rule_reads) <= 1
    unexpected = [
        query["sql"]
        for query in captured.captured_queries
        if any(table in query["sql"].lower() for table in import_prerequisite_tables)
        and query["sql"] not in serial_rule_reads
    ]
    assert unexpected == []
