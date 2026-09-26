"""
The interface sync POST runs as one retried transaction and publishes only what its committed attempt reports.

Every conflict here is a real PostgreSQL conflict with a second connection. The patches only
observe and call through: a counting wrapper around the owner lock (each attempt calls it once, so
a test counts attempts and releases the second connection's lock between them), a recorder of the
outcome the committed attempt returns to ``post()``, and a counter of relationship passes.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from dcim.models import Device, Interface
from django.contrib.contenttypes.models import ContentType
from django.db import OperationalError, connection, transaction

from netbox_librenms_plugin.middleware import FOLLOW_UP_FAILED_MESSAGE, TRY_AGAIN_MESSAGE
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_superuser,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.interface_sync_post_helpers import (
    SERVER_KEY,
    SYNCED,
    assert_try_again_answer,
    bound_interface,
    count_sync_attempts,
    post_interface_sync,
    seed_ports,
    sync_port,
)
from netbox_librenms_plugin.tests.lock_conflict_helpers import (
    backend_pid,
    lock_row,
    lock_row_nowait,
    lock_timeout,
    second_connection,
    wait_for_lock_wait,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms, messages_on
from netbox_librenms_plugin.views.sync import interfaces as interfaces_view
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView, _InterfaceSyncOutcome

# Long enough for a blocked statement to be a real lock wait, short enough for two attempts.
LOCK_TIMEOUT_MS = 200
PLAIN_AND_HTMX = pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])


def _outcome(*, synced_count, skipped_conflicts=(), kept_name_conflicts=(), warnings=()):
    """The outcome of a committed attempt that changed NetBox."""
    return _InterfaceSyncOutcome(
        skipped_conflicts=skipped_conflicts,
        kept_name_conflicts=kept_name_conflicts,
        synced_count=synced_count,
        mutated=True,
        warnings=warnings,
    )


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


@pytest.fixture
def attempts(monkeypatch):
    """Count sync attempts; ``before_retry`` runs when the second attempt starts."""
    return count_sync_attempts(monkeypatch)


@pytest.fixture
def committed_outcomes(monkeypatch):
    """Record the outcome that the committed attempt of each sync returns to ``post()``."""
    real_run = interfaces_view.run_transaction
    outcomes = []

    def recording_run(work):
        outcome = real_run(work)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(interfaces_view, "run_transaction", recording_run)
    return outcomes


@transactional_db_with_all_apps()
@PLAIN_AND_HTMX
def test_a_lock_conflict_on_the_first_attempt_is_retried_once(client, attempts, committed_outcomes, htmx):
    device = make_device(f"sync-retry-once-{htmx}", librenms_cf={SERVER_KEY: {"id": 91}})
    seed_ports(device, [sync_port(10, "eth10")])
    client.force_login(make_superuser("sync-retry-once-user"))

    with second_connection() as other:
        lock_row(other, Device, device.pk)
        attempts.before_retry = other.rollback
        with lock_timeout(LOCK_TIMEOUT_MS):
            response = post_interface_sync(client, device, [10], htmx=htmx)

    assert attempts.count == 2
    assert committed_outcomes == [_outcome(synced_count=1)]
    assert response.status_code == (200 if htmx else 302)
    assert Interface.objects.filter(device=device, name="eth10").exists()
    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]


@transactional_db_with_all_apps()
@PLAIN_AND_HTMX
def test_conflicts_on_both_attempts_give_one_try_again_answer_and_change_nothing(client, attempts, htmx):
    device = make_device(f"sync-retry-exhausted-{htmx}", librenms_cf={SERVER_KEY: {"id": 92}})
    seed_ports(device, [sync_port(10, "eth10")])
    client.force_login(make_superuser("sync-retry-exhausted-user"))

    with second_connection() as other:
        lock_row(other, Device, device.pk)
        with lock_timeout(LOCK_TIMEOUT_MS):
            response = post_interface_sync(client, device, [10], htmx=htmx)

    assert attempts.count == 2
    assert_try_again_answer(response, device, htmx, TRY_AGAIN_MESSAGE)
    assert not Interface.objects.filter(device=device).exists()


@transactional_db_with_all_apps()
def test_a_real_deadlock_on_the_first_attempt_is_retried_once(client, attempts, committed_outcomes):
    """The sync holds its Device and waits for an interface; the other session holds that interface and waits for the Device."""
    device = make_device("sync-deadlock", librenms_cf={SERVER_KEY: {"id": 96}})
    interface = bound_interface(device, "eth10", 10)
    seed_ports(device, [sync_port(10, "eth10", alias="uplink")])
    client.force_login(make_superuser("sync-deadlock-user"))
    sync_pid = backend_pid(connection)
    interface_held = Event()

    def hold_the_interface_then_lock_the_device():
        with second_connection() as other:
            with other.cursor() as cursor:
                # PostgreSQL aborts the session whose deadlock check runs first: always the sync.
                cursor.execute("SET deadlock_timeout = '10s'")
            lock_row(other, Interface, interface.pk)
            interface_held.set()
            wait_for_lock_wait(other, sync_pid)
            lock_row(other, Device, device.pk)
            other.commit()

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_the_interface_then_lock_the_device)
        assert interface_held.wait(5), "the second session did not lock the interface"
        response = post_interface_sync(client, device, [10], htmx=False)
        holder.result(timeout=10)

    assert attempts.count == 2
    assert committed_outcomes == [_outcome(synced_count=1)]
    assert response.status_code == 302
    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]
    interface.refresh_from_db()
    assert interface.description == "uplink"


@transactional_db_with_all_apps()
def test_a_retried_sync_reports_its_warnings_skips_and_success_once(client, attempts, committed_outcomes):
    """The attempt that conflicts has already warned, skipped a row, kept a name and counted syncs; none of it is published."""
    from ipam.models import VLAN

    device = make_device("sync-retry-report", librenms_cf={SERVER_KEY: {"id": 93}})
    interface = bound_interface(device, "eth10", 10)
    # Port 11 reports the name of another interface on the device, so its bound interface keeps its name.
    bound_interface(device, "old11", 11)
    make_interface(device, "eth12")
    # Port 7 has no binding, but its reported name belongs to port 8, so every attempt skips it.
    skipped = bound_interface(device, "eth7", 8)
    skipped_before = Interface.objects.filter(pk=skipped.pk).values().get()
    # A VLAN the user cannot view, so every attempt warns that the VLAN scope is incomplete.
    VLAN.objects.create(vid=812, name="sync-retry-report-hidden")
    user = make_user_with_perms(
        "sync-retry-report-user", [("view", Device), ("view", Interface), ("add", Interface), ("change", Interface)]
    )
    client.force_login(user)
    seed_ports(
        device, [sync_port(7, "eth7"), sync_port(11, "eth12", alias="kept"), sync_port(10, "eth10", alias="uplink")]
    )

    with second_connection() as other:
        # The first attempt warns, skips and keeps a name, then waits for this row at its write.
        lock_row(other, Interface, interface.pk)
        attempts.before_retry = other.rollback
        with lock_timeout(LOCK_TIMEOUT_MS):
            response = post_interface_sync(client, device, [7, 11, 10], htmx=False, exclude_columns=("mac_address",))

    vlan_warning = (
        "VLANs were not synced for the selected interfaces: your account is missing ipam.view_vlan. "
        "Existing VLAN assignments were left unchanged."
    )
    assert attempts.count == 2
    assert committed_outcomes == [
        _outcome(
            synced_count=2,
            skipped_conflicts=("eth7 (reported name belongs to a different LibreNMS port 8)",),
            kept_name_conflicts=(("old11", "eth12", None),),
            warnings=(vlan_warning,),
        )
    ]
    assert messages_on(response.wsgi_request) == [
        ("warning", vlan_warning),
        ("warning", "1 interface(s) skipped: eth7 (reported name belongs to a different LibreNMS port 8)."),
        (
            "warning",
            "Interface 'old11' kept its current name because the reported name is in use on the same "
            "interface owner: 'eth12'.",
        ),
        ("success", SYNCED),
    ]
    interface.refresh_from_db()
    assert interface.description == "uplink"
    assert Interface.objects.filter(pk=skipped.pk).values().get() == skipped_before


def _seed_relationship_sync(device, base, mac):
    """Seed one device for a sync whose attribute pass writes every row before the relationship pass runs."""
    aggregate = bound_interface(device, "Po1", base + 100, iface_type="lag")
    bound_interface(device, "old11", base + 11)
    make_interface(device, "eth12")
    bound_interface(device, "eth7", base + 8)
    ports = [
        sync_port(base + 7, "eth7"),
        sync_port(base + 11, "eth12", alias="kept"),
        sync_port(base + 1, "eth1", mac=mac),
        sync_port(base + 100, "Po1", if_type="ieee8023adLag"),
    ]
    seed_ports(device, ports, lag_members={base + 1: base + 100})
    return aggregate, [base + 7, base + 11, base + 1]


def _one_sync_result(device, aggregate):
    """Return what one sync left on the member interface: its LAG, MAC rows and change records."""
    from core.models import ObjectChange
    from dcim.models import MACAddress

    member = Interface.objects.get(device=device, name="eth1")
    interface_type = ContentType.objects.get_for_model(Interface)
    changes = ObjectChange.objects.filter(changed_object_type=interface_type, changed_object_id=member.pk)
    return {
        "lag": member.lag_id == aggregate.pk,
        "macs": MACAddress.objects.filter(assigned_object_type=interface_type, assigned_object_id=member.pk).count(),
        "primary_mac": str(member.primary_mac_address.mac_address).lower(),
        "changes": sorted(changes.values_list("action", flat=True)),
    }


@transactional_db_with_all_apps()
def test_a_relationship_conflict_after_a_written_attribute_pass_leaves_one_sync_result(
    client, attempts, committed_outcomes, monkeypatch
):
    """Attempt one writes every row, then its relationship pass meets a lock; the result equals one clean sync."""
    from django.test import Client

    user = make_superuser("sync-relationship-retry-user")
    client.force_login(user)
    # Its own session, so the control's messages do not carry over into the retried sync's messages.
    control_client = Client()
    control_client.force_login(user)
    control = make_device("sync-relationship-control", librenms_cf={SERVER_KEY: {"id": 97}})
    control_aggregate, control_ports = _seed_relationship_sync(control, 1000, "00:11:22:33:44:01")
    device = make_device("sync-relationship-retry", librenms_cf={SERVER_KEY: {"id": 98}})
    aggregate, ports = _seed_relationship_sync(device, 2000, "00:11:22:33:44:01")
    skipped_before = list(Interface.objects.filter(device__in=[control, device], name="eth7").order_by("pk").values())
    real_relationships = SyncInterfacesView._sync_interface_relationships
    relationship_passes = []

    def counting_relationships(self, *args, **kwargs):
        relationship_passes.append(Interface.objects.filter(device=self.object, name="eth1").exists())
        return real_relationships(self, *args, **kwargs)

    monkeypatch.setattr(SyncInterfacesView, "_sync_interface_relationships", counting_relationships)
    control_response = post_interface_sync(
        control_client, control, control_ports, htmx=False, exclude_columns=("vlans",)
    )
    assert (attempts.count, relationship_passes) == (1, [True]), "precondition: the control sync is one clean attempt"
    attempts.count = 0
    relationship_passes.clear()

    with second_connection() as other:
        # Only the relationship pass locks the aggregate: the attribute pass does not write it.
        lock_row(other, Interface, aggregate.pk)
        attempts.before_retry = other.rollback
        with lock_timeout(LOCK_TIMEOUT_MS):
            response = post_interface_sync(client, device, ports, htmx=False, exclude_columns=("vlans",))

    assert attempts.count == 2
    assert relationship_passes == [True, True], "each attempt wrote the member before its relationship pass"
    assert committed_outcomes == [
        _outcome(
            synced_count=2,
            skipped_conflicts=(f"eth7 (reported name belongs to a different LibreNMS port {base + 8})",),
            kept_name_conflicts=(("old11", "eth12", None),),
        )
        for base in (1000, 2000)
    ]
    assert _one_sync_result(device, aggregate) == _one_sync_result(control, control_aggregate)
    assert _one_sync_result(device, aggregate)["lag"] is True
    assert _one_sync_result(device, aggregate)["macs"] == 1
    for sync_response, base in ((control_response, 1000), (response, 2000)):
        assert messages_on(sync_response.wsgi_request) == [
            (
                "warning",
                f"1 interface(s) skipped: eth7 (reported name belongs to a different LibreNMS port {base + 8}).",
            ),
            (
                "warning",
                "Interface 'old11' kept its current name because the reported name is in use on the same "
                "interface owner: 'eth12'.",
            ),
            ("success", SYNCED),
        ]
    assert (
        list(Interface.objects.filter(device__in=[control, device], name="eth7").order_by("pk").values())
        == skipped_before
    )


@transactional_db_with_all_apps()
@PLAIN_AND_HTMX
def test_a_follow_up_conflict_after_the_commit_is_reported_and_not_retried(client, attempts, monkeypatch, htmx):
    device = make_device(f"sync-follow-up-{htmx}", librenms_cf={SERVER_KEY: {"id": 94}})
    locked = make_device(f"sync-follow-up-locked-{htmx}")
    seed_ports(device, [sync_port(10, "eth10")])
    client.force_login(make_superuser("sync-follow-up-user"))
    real_relationships = SyncInterfacesView._sync_interface_relationships

    def follow_up():
        # Like NetBox's rename callback, it runs its own transaction after the sync committed.
        with transaction.atomic():
            lock_row_nowait(Device, locked.pk)

    def with_follow_up(self, *args, **kwargs):
        transaction.on_commit(follow_up)
        return real_relationships(self, *args, **kwargs)

    monkeypatch.setattr(SyncInterfacesView, "_sync_interface_relationships", with_follow_up)
    with second_connection() as other:
        lock_row(other, Device, locked.pk)
        response = post_interface_sync(client, device, [10], htmx=htmx)

    assert attempts.count == 1
    assert_try_again_answer(response, device, htmx, FOLLOW_UP_FAILED_MESSAGE)
    assert Interface.objects.filter(device=device, name="eth10").exists(), "the committed sync stays"


@pytest.mark.django_db
def test_an_integrity_error_after_a_swallowed_conflict_reaches_the_sync_handler(client, attempts, monkeypatch):
    """A swallowed 55P03 does not make a later 23505 a retry; the sync's own handler reports it."""
    device = make_device("sync-mixed-error", librenms_cf={SERVER_KEY: {"id": 95}})
    seed_ports(device, [sync_port(10, "eth10")])
    client.force_login(make_superuser("sync-mixed-error-user"))
    # Migrations committed this row, so the second connection can lock it in this non-transactional test.
    locked_pk = ContentType.objects.get_for_model(ContentType).pk

    def swallow_then_violate(self, obj, *args, **kwargs):
        try:
            with transaction.atomic():
                lock_row_nowait(ContentType, locked_pk)
        except OperationalError:
            pass  # a broad row handler in a savepoint swallows the lock conflict
        Interface.objects.create(device=obj, name="eth10")

    monkeypatch.setattr(SyncInterfacesView, "_sync_interface_relationships", swallow_then_violate)
    with second_connection() as other:
        lock_row(other, ContentType, locked_pk)
        response = post_interface_sync(client, device, [10], htmx=False)

    assert attempts.count == 1
    assert response.status_code == 302
    assert "tab=interfaces" in response["Location"]
    assert messages_on(response.wsgi_request) == [
        (
            "error",
            "The sync was rolled back by a concurrent change to a related interface. "
            "Refresh the LibreNMS data and try again.",
        )
    ]
    assert not Interface.objects.filter(device=device).exists()


def test_only_the_steps_outside_the_transaction_add_messages():
    """A message added inside the transaction is published once per attempt; the attempt reports through its outcome."""
    import ast
    import inspect

    from netbox_librenms_plugin.views.sync import interfaces

    module = ast.parse(inspect.getsource(interfaces))
    view = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "SyncInterfacesView")
    adders = {
        method.name
        for method in view.body
        if isinstance(method, ast.FunctionDef)
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "messages"
    }

    # post() adds messages before run_transaction() and after it returns; the other two run before it.
    assert adders == {"post", "get_selected_port_ids", "get_cached_ports_data"}
