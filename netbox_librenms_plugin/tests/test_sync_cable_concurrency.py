"""PostgreSQL concurrency coverage for cable overwrite decisions."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from django.apps import apps

from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    make_device,
    make_interface,
    make_serial_device,
    make_virtual_chassis,
)

pytestmark = pytest.mark.django_db(
    transaction=True,
    available_apps=[app.name for app in apps.get_app_configs()],
)


@pytest.fixture(autouse=True)
def restore_librenms_id_custom_field():
    """Recreate migration-seeded custom-field state after each transaction flush."""
    from netbox_librenms_plugin import _ensure_librenms_id_custom_field

    executed_aliases = getattr(_ensure_librenms_id_custom_field, "_executed_aliases", set())
    executed_aliases.discard("default")
    _ensure_librenms_id_custom_field(sender=None, using="default")


def test_current_cable_lock_blocks_replacement_after_confirmation():
    from dcim.models import Cable
    from django.db import OperationalError, close_old_connections, transaction

    from netbox_librenms_plugin.tests.test_cable_overwrite import _sync_view

    _device, (csp,), _ = make_serial_device("lock-cable-local", csp_names=["ttyS1"])
    _current_remote, _, (current_cp,) = make_serial_device("lock-cable-current", cp_names=["console"])
    _target_remote, _, (target_cp,) = make_serial_device("lock-cable-target", cp_names=["console"])
    cable = cable_together(csp, current_cp)
    sync = _sync_view()

    def replace_current_cable():
        close_old_connections()
        try:
            with transaction.atomic():
                from django.db import connection

                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '500ms'")
                Cable.objects.get(pk=cable.pk).delete()
        finally:
            close_old_connections()

    with transaction.atomic():
        locked_terms = sync._lock_cable_terminations(csp, target_cp)
        assert locked_terms is not None
        locked = sync._lock_current_cables(*locked_terms)
        assert set(locked) == {cable.pk}
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(replace_current_cable)
            with pytest.raises(OperationalError):
                future.result(timeout=5)

    assert Cable.objects.filter(pk=cable.pk).exists()


def test_cable_sync_locks_device_owners_before_terminations():
    """Cable and relationship writers must acquire shared owner locks in one order."""
    from django.contrib.auth import get_user_model
    from django.db import connection, transaction
    from django.test import RequestFactory
    from django.test.utils import CaptureQueriesContext

    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    _local_device, (csp,), _ = make_serial_device("cable-lock-order-local", csp_names=["ttyS1"])
    _remote_device, _, (cp,) = make_serial_device("cable-lock-order-remote", cp_names=["console"])
    sync = object.__new__(SyncCablesView)
    sync.request = RequestFactory().post("/")
    sync.request.user = get_user_model().objects.create_superuser("cable-lock-order-user", "", "pw")

    with transaction.atomic(), CaptureQueriesContext(connection) as captured:
        assert sync._lock_cable_terminations(csp, cp) is not None

    locking_sql = [query["sql"] for query in captured.captured_queries if "FOR UPDATE" in query["sql"]]
    device_lock = next(index for index, sql in enumerate(locking_sql) if 'FROM "dcim_device"' in sql)
    termination_locks = [
        index
        for index, sql in enumerate(locking_sql)
        if 'FROM "dcim_consoleserverport"' in sql or 'FROM "dcim_consoleport"' in sql
    ]
    assert termination_locks
    assert device_lock < min(termination_locks)


def test_remote_termination_owner_change_invalidates_the_confirmed_target():
    """A stable termination PK must not authorize a cable to its new owner Device."""
    from dcim.models import ConsolePort

    from netbox_librenms_plugin.tests.test_cable_overwrite import _sync_view

    local_device, (csp,), _ = make_serial_device("cable-owner-drift-local", csp_names=["ttyS1"])
    expected_remote, _, (cp,) = make_serial_device("cable-owner-drift-expected", cp_names=["console"])
    moved_remote, _, _ = make_serial_device("cable-owner-drift-current")
    stale_cp = ConsolePort.objects.get(pk=cp.pk)
    ConsolePort.objects.filter(pk=cp.pk).update(device=moved_remote)

    sync = _sync_view()
    sync._initial_device = local_device
    row_id = f"serial:{csp.pk}"
    result = sync._apply_cable_action(
        csp,
        stale_cp,
        {
            "row_id": row_id,
            "netbox_remote_interface_id": cp.pk,
            "netbox_remote_device_id": expected_remote.pk,
        },
        csp.name,
        False,
    )

    assert result["status"] == "stale"
    csp.refresh_from_db()
    cp.refresh_from_db()
    assert csp.cable_id is None
    assert cp.cable_id is None


def test_local_termination_owner_change_invalidates_the_selected_member():
    """A same-chassis move must not replace the exact selected local owner."""
    from dcim.models import ConsoleServerPort

    from netbox_librenms_plugin.tests.test_cable_overwrite import _sync_view

    expected_local, (csp,), _ = make_serial_device("cable-local-drift-expected", csp_names=["ttyS1"])
    moved_local, _, _ = make_serial_device("cable-local-drift-current")
    make_virtual_chassis("cable-local-drift-vc", expected_local, moved_local)
    _remote, _, (cp,) = make_serial_device("cable-local-drift-remote", cp_names=["console"])
    stale_csp = ConsoleServerPort.objects.get(pk=csp.pk)
    ConsoleServerPort.objects.filter(pk=csp.pk).update(device=moved_local)

    sync = _sync_view()
    sync._initial_device = expected_local
    row_id = f"serial:{csp.pk}"
    result = sync._apply_cable_action(
        stale_csp,
        cp,
        {
            "row_id": row_id,
            "device_id": expected_local.pk,
            "netbox_remote_interface_id": cp.pk,
            "netbox_remote_device_id": cp.device_id,
        },
        csp.name,
        False,
    )

    assert result["status"] == "stale"
    csp.refresh_from_db()
    cp.refresh_from_db()
    assert csp.cable_id is None
    assert cp.cable_id is None


def test_termination_change_scope_is_rechecked_after_row_lock():
    from core.models import ObjectType
    from dcim.models import ConsolePort, ConsoleServerPort, Device
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection, transaction
    from users.models import ObjectPermission

    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    _local, (csp,), _ = make_serial_device("lock-scope-local", csp_names=["ttyS1"])
    csp.description = "managed"
    csp.save(update_fields=["description"])
    _remote, _, (cp,) = make_serial_device("lock-scope-remote", cp_names=["console"])
    user = get_user_model().objects.create_user("lock-scope-user")
    scoped_csp = ObjectPermission.objects.create(
        name="lock-scope-csp",
        actions=["change"],
        constraints={"description": "managed"},
    )
    scoped_csp.object_types.add(ObjectType.objects.get_for_model(ConsoleServerPort))
    scoped_csp.users.add(user)
    console_ports = ObjectPermission.objects.create(name="lock-scope-cp", actions=["change"])
    console_ports.object_types.add(ObjectType.objects.get_for_model(ConsolePort))
    console_ports.users.add(user)
    # Owners are locked before terminations, and that lock is gated on Device view scope, so the
    # worker never reaches the termination row without this grant.
    device_view = ObjectPermission.objects.create(name="lock-scope-device", actions=["view"])
    device_view.object_types.add(ObjectType.objects.get_for_model(Device))
    device_view.users.add(user)
    user = get_user_model().objects.get(pk=user.pk)

    sync = object.__new__(SyncCablesView)
    sync.request = SimpleNamespace(user=user)
    backend_pids = Queue()

    def lock_terminations():
        close_old_connections()
        try:
            with transaction.atomic():
                connection.ensure_connection()
                backend_pids.put(connection.connection.info.backend_pid)
                return sync._lock_cable_terminations(csp, cp)
        finally:
            close_old_connections()

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with transaction.atomic():
            ConsoleServerPort.objects.select_for_update().get(pk=csp.pk)
            future = executor.submit(lock_terminations)
            worker_pid = backend_pids.get(timeout=5)
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                        [worker_pid],
                    )
                    row = cursor.fetchone()
                if row and row[0] == "Lock":
                    break
                sleep(0.01)
            else:
                raise AssertionError("worker did not wait for the termination row lock")
            ConsoleServerPort.objects.filter(pk=csp.pk).update(description="restricted")

        assert future.result(timeout=5) is None
    finally:
        executor.shutdown(wait=True)


def test_termination_owner_device_is_locked_with_permission_scope():
    """The owner cannot leave a relation-constrained grant before the cable write."""
    from core.models import ObjectType
    from dcim.models import ConsolePort, ConsoleServerPort, Device, Site
    from django.contrib.auth import get_user_model
    from django.db import OperationalError, close_old_connections, connection, transaction
    from users.models import ObjectPermission

    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    in_scope = Site.objects.create(name="cable-owner-site-a", slug="cable-owner-site-a")
    out_of_scope = Site.objects.create(name="cable-owner-site-b", slug="cable-owner-site-b")
    local_device, (csp,), _ = make_serial_device("cable-owner-local", csp_names=["ttyS1"])
    local_device.site = in_scope
    local_device.save(update_fields=["site"])
    _remote, _, (cp,) = make_serial_device("cable-owner-remote", cp_names=["console"])

    user = get_user_model().objects.create_user("cable-owner-scope-user")
    scoped_csp = ObjectPermission.objects.create(
        name="cable-owner-csp",
        actions=["change"],
        constraints={"device__site_id": in_scope.pk},
    )
    scoped_csp.object_types.add(ObjectType.objects.get_for_model(ConsoleServerPort))
    scoped_csp.users.add(user)
    console_ports = ObjectPermission.objects.create(name="cable-owner-cp", actions=["change"])
    console_ports.object_types.add(ObjectType.objects.get_for_model(ConsolePort))
    console_ports.users.add(user)
    device_view = ObjectPermission.objects.create(name="cable-owner-device-view", actions=["view"])
    device_view.object_types.add(ObjectType.objects.get_for_model(Device))
    device_view.users.add(user)
    user = get_user_model().objects.get(pk=user.pk)

    sync = object.__new__(SyncCablesView)
    sync.request = SimpleNamespace(user=user)
    locked = Queue()
    release = Queue()

    def hold_sync_scope():
        close_old_connections()
        try:
            with transaction.atomic():
                result = sync._lock_cable_terminations(csp, cp)
                locked.put(result)
                release.get(timeout=5)
        finally:
            close_old_connections()

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(hold_sync_scope)
        assert locked.get(timeout=5) is not None
        with pytest.raises(OperationalError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '500ms'")
            Device.objects.filter(pk=local_device.pk).update(site=out_of_scope)
        release.put(True)
        future.result(timeout=5)
    finally:
        if release.empty():
            release.put(True)
        executor.shutdown(wait=True)


def test_locked_local_owner_must_still_belong_to_the_page_virtual_chassis():
    """A stale pre-lock member check must not authorize a former VC member."""
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    page = make_device("cable-vc-scope-page")
    member = make_device("cable-vc-scope-member")
    make_virtual_chassis("cable-vc-scope", page, member)
    local = make_interface(member, "Ethernet1")
    remote = make_interface(make_device("cable-vc-scope-remote"), "Ethernet9")
    user = get_user_model().objects.create_superuser("cable-vc-scope-user", "", "pw")
    sync = object.__new__(SyncCablesView)
    sync.request = SimpleNamespace(user=user)
    sync._initial_device = page

    member.virtual_chassis = None
    member.vc_position = None
    member.save(update_fields=["virtual_chassis", "vc_position"])

    with transaction.atomic():
        locked = sync._lock_cable_terminations(local, remote)

    assert locked is None


def test_concurrent_tag_renames_keep_settings_and_provenance_identity_together():
    """A stale settings form must rename the Tag selected by the current locked row."""
    from queue import Queue

    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.forms import CableSyncSettingsForm
    from netbox_librenms_plugin.models import LibreNMSSettings
    from netbox_librenms_plugin.utils import get_librenms_cable_tag

    settings, _ = LibreNMSSettings.objects.get_or_create()
    tag = get_librenms_cable_tag(sync_settings=settings)
    users = [get_user_model().objects.create_superuser(f"concurrent-tag-{index}", "", "pw") for index in range(2)]
    worker_pids = Queue()

    forms = []
    for user, name in zip(users, ("managed-first", "managed-second"), strict=True):
        form = CableSyncSettingsForm(
            {
                "cable_sync_tag": name,
                "cable_sync_tag_color": "009688",
                "cable_sync_description": "Managed cable",
            },
            instance=LibreNMSSettings.objects.get(pk=settings.pk),
            user=user,
        )
        assert form.is_valid(), form.errors
        forms.append(form)

    def rename_tag(form):
        close_old_connections()
        try:
            connection.ensure_connection()
            worker_pids.put(connection.connection.info.backend_pid)
            form.save()
            return True
        finally:
            close_old_connections()

    def wait_until_blocked(pid, future):
        deadline = monotonic() + 5
        last_state = None
        while monotonic() < deadline:
            if future.done():
                raise AssertionError(f"settings worker {pid} completed early with status {future.result()}")
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state, wait_event_type, wait_event, query FROM pg_stat_activity WHERE pid = %s",
                    [pid],
                )
                row = cursor.fetchone()
            last_state = row
            if row and row[1] == "Lock":
                return
            sleep(0.01)
        raise AssertionError(f"settings worker {pid} did not wait for a database lock: {last_state!r}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        with transaction.atomic():
            LibreNMSSettings.objects.select_for_update().get(pk=settings.pk)
            first = executor.submit(rename_tag, forms[0])
            first_pid = worker_pids.get(timeout=5)
            wait_until_blocked(first_pid, first)
            # Worker 1 is already queued before worker 2 is submitted. Worker 2 therefore
            # cannot acquire the settings lock first, whether it queues now or starts after
            # worker 1 commits.
            second = executor.submit(rename_tag, forms[1])
            worker_pids.get(timeout=5)

        assert first.result(timeout=10) is True
        assert second.result(timeout=10) is True

    settings.refresh_from_db()
    tag.refresh_from_db()
    assert settings.cable_sync_tag == "managed-second"
    assert tag.name == "managed-second"
