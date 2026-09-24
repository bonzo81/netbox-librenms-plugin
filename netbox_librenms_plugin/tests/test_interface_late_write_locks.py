"""
The row lock of the late write: which rows it locks, in which mode, and where in the save.

The late write takes its lock in ``pre_save``, after NetBox's tagged-VLAN clear, in the mode the
``UPDATE`` would take. So it adds no wait that today's ``UPDATE`` does not have. The two schedules
here are real two-connection lock schedules that deadlock when the lock comes before the clear
(round 1, finding 1.1) or when a key-column change first takes the weaker mode (round 2, finding 2.1).
"""

import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from dcim.models import Interface
from django.db import connection
from django.test.utils import CaptureQueriesContext
from ipam.models import VLAN
from virtualization.models import VMInterface

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_superuser,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.interface_sync_post_helpers import (
    SERVER_KEY,
    SYNCED,
    bound_interface,
    count_sync_attempts,
    post_interface_sync,
    seed_ports,
    sync_port,
)
from netbox_librenms_plugin.tests.lock_conflict_helpers import (
    backend_pid,
    commit_row_change,
    raised_sqlstates,
    second_connection,
    wait_for_lock_wait,
)
from netbox_librenms_plugin.tests.view_test_helpers import messages_on
from netbox_librenms_plugin.transactions import key_columns, save_at_version

INTERFACE_TABLE = Interface._meta.db_table
THROUGH_TABLE = Interface.tagged_vlans.through._meta.db_table
MAC = "00:11:22:33:44:55"


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


@pytest.fixture
def attempts(monkeypatch):
    return count_sync_attempts(monkeypatch)


def _synced_interface(device, name, port_id, **fields):
    """Return an interface that a sync of ``sync_port(port_id, name)`` leaves unchanged, with *fields* set."""
    interface = bound_interface(device, name, port_id)
    Interface.objects.filter(pk=interface.pk).update(speed=1_000_000, mtu=1500, enabled=True, **fields)
    interface.refresh_from_db()
    return interface


def _interface_row_locks(queries):
    """Return the lock clause of each captured statement that locks a row of the interface table."""
    table = re.escape(f'FROM "{INTERFACE_TABLE}"')
    return [
        match.group(1)
        for query in queries.captured_queries
        if (match := re.search(rf"{table} .*\bFOR (UPDATE|NO KEY UPDATE)\b", query["sql"]))
    ]


def _interface_updates(queries):
    return [
        query["sql"] for query in queries.captured_queries if query["sql"].startswith(f'UPDATE "{INTERFACE_TABLE}"')
    ]


# ---------------------------------------------------------------------------
# AC3 and the lock mode: no change takes no lock; a key-column change takes FOR UPDATE
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("port", "locks"),
    [
        pytest.param(sync_port(10, "eth10"), [], id="no-change"),
        pytest.param(sync_port(10, "eth10", alias="uplink"), ["NO KEY UPDATE"], id="description"),
        pytest.param(sync_port(10, "eth11"), ["UPDATE"], id="name"),
        pytest.param(sync_port(10, "eth10", mac=MAC), ["UPDATE"], id="primary-mac"),
    ],
)
def test_a_row_is_locked_only_when_it_changes_and_in_the_mode_of_its_update(client, port, locks):
    device = make_device("late-lock", librenms_cf={SERVER_KEY: {"id": 11}})
    _synced_interface(device, "eth10", 10)
    seed_ports(device, [port])
    client.force_login(make_superuser("late-lock-user"))

    with CaptureQueriesContext(connection) as queries:
        response = post_interface_sync(client, device, [10], htmx=False, exclude_columns=())

    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]
    assert _interface_row_locks(queries) == locks
    assert len(_interface_updates(queries)) == len(locks)


# ---------------------------------------------------------------------------
# The two lock schedules complete without a deadlock
# ---------------------------------------------------------------------------


def _run_schedule(client, device, interface, hold_sql, *, exclude_columns):
    """
    Run the sync while another session holds a lock from *hold_sql*, then updates the interface and commits.

    The other session updates the interface only once the sync waits for a lock. Its deadlock check
    comes last, so a deadlock always aborts the sync, never the other session.

    Returns:
        tuple: The sync response and the SQLSTATEs that the sync's statements raised.

    """
    sync_pid = backend_pid(connection)
    held = Event()

    def hold_then_update():
        with second_connection() as other:
            with other.cursor() as cursor:
                cursor.execute("SET deadlock_timeout = '10s'")
                cursor.execute(hold_sql, [interface.pk])
                assert cursor.fetchall(), "the other session holds no row"
            held.set()
            wait_for_lock_wait(other, sync_pid)
            commit_row_change(other, Interface, interface.pk, {"label": "set by another operation"})

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_then_update)
        assert held.wait(5), "the other session did not take its lock"
        with raised_sqlstates() as sqlstates:
            response = post_interface_sync(client, device, [10], htmx=False, exclude_columns=exclude_columns)
        holder.result(timeout=10)
    return response, sqlstates


@transactional_db_with_all_apps()
@pytest.mark.parametrize("writer", ["attribute-writer", "vlan-helper"])
def test_the_tagged_vlan_clear_schedule_completes_without_a_deadlock(client, attempts, writer):
    """
    Finding 1.1: the other session holds the tagged-VLAN rows, then updates the interface.

    NetBox's ``save()`` clears the tagged VLANs of a row that is not in tagged mode, before the
    ``UPDATE``. A lock before ``save()`` would hold the interface while the clear waits for the
    tagged-VLAN rows, and the other session's update would close the cycle.
    """
    device = make_device(f"late-lock-clear-{writer}", librenms_cf={SERVER_KEY: {"id": 12}})
    tagged = VLAN.objects.create(vid=100, name=f"late-lock-clear-{writer}-100")
    if writer == "vlan-helper":
        # The sync takes the row out of tagged mode; the attribute writer has nothing to write.
        interface = _synced_interface(device, "eth10", 10, mode="tagged")
        untagged = VLAN.objects.create(vid=200, name=f"late-lock-clear-{writer}-200")
        port = sync_port(10, "eth10", untagged_vlan=200, tagged_vlans=[])
        exclude_columns = ("mac_address",)
    else:
        # An access row that still has a tagged VLAN: the attribute write's save() clears it.
        interface = _synced_interface(device, "eth10", 10, mode="access")
        port = sync_port(10, "eth10", alias="uplink")
        exclude_columns = ("vlans", "mac_address")
    interface.tagged_vlans.add(tagged)
    seed_ports(device, [port])
    client.force_login(make_superuser("late-lock-clear-user"))

    response, sqlstates = _run_schedule(
        client,
        device,
        interface,
        f'SELECT 1 FROM "{THROUGH_TABLE}" WHERE interface_id = %s FOR UPDATE',
        exclude_columns=exclude_columns,
    )

    assert "40P01" not in sqlstates
    # The other session's update made the first attempt's row version stale.
    assert attempts.count == 2
    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]
    interface.refresh_from_db()
    assert interface.label == "set by another operation"
    assert not interface.tagged_vlans.exists()
    if writer == "vlan-helper":
        assert (interface.mode, interface.untagged_vlan_id) == ("access", untagged.pk)
    else:
        assert interface.description == "uplink"


@transactional_db_with_all_apps()
def test_the_primary_mac_schedule_completes_without_a_deadlock(client, attempts):
    """
    Finding 2.1: the other session holds ``FOR KEY SHARE`` on the interface, then updates it.

    A new primary MAC changes a key column, so the ``UPDATE`` needs ``FOR UPDATE``. A lock taken
    first in the weaker ``FOR NO KEY UPDATE`` mode and then upgraded would close the cycle.
    """
    device = make_device("late-lock-mac", librenms_cf={SERVER_KEY: {"id": 13}})
    interface = _synced_interface(device, "eth10", 10)
    seed_ports(device, [sync_port(10, "eth10", mac=MAC)])
    client.force_login(make_superuser("late-lock-mac-user"))

    response, sqlstates = _run_schedule(
        client,
        device,
        interface,
        f'SELECT 1 FROM "{INTERFACE_TABLE}" WHERE id = %s FOR KEY SHARE',
        exclude_columns=("vlans",),
    )

    assert "40P01" not in sqlstates
    assert attempts.count == 2
    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]
    interface.refresh_from_db()
    assert interface.label == "set by another operation"
    assert str(interface.primary_mac_address.mac_address).lower() == MAC
    assert interface.mac_addresses.count() == 1


# ---------------------------------------------------------------------------
# key_columns agrees with PostgreSQL; a versioned save is always checked
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("model", [Interface, VMInterface], ids=["interface", "vminterface"])
def test_key_columns_are_the_columns_of_the_unique_indexes_postgresql_uses_for_foreign_keys(model):
    """PostgreSQL's key columns: the key columns of each unique, immediate index with no predicate and no expression."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT attribute.attname
            FROM pg_index AS index
            CROSS JOIN LATERAL unnest(index.indkey::int2[]) WITH ORDINALITY AS key(attnum, position)
            JOIN pg_attribute AS attribute
              ON attribute.attrelid = index.indrelid AND attribute.attnum = key.attnum
            WHERE index.indrelid = %s::regclass
              AND index.indisunique
              AND index.indimmediate
              AND index.indpred IS NULL
              AND index.indexprs IS NULL
              AND key.position <= index.indnkeyatts
            """,
            [model._meta.db_table],
        )
        database_key_columns = {row[0] for row in cursor.fetchall()}

    assert "primary_mac_address_id" in database_key_columns, "precondition: the one-to-one MAC is a key column"
    assert key_columns(model) == database_key_columns


@pytest.mark.django_db
def test_a_versioned_save_of_a_model_with_no_version_check_fails():
    """Only the interface models have the pre_save check; a save that nothing checked must not pass as checked."""
    from dcim.models import Site

    site = Site.objects.create(name="late-lock-unchecked", slug="late-lock-unchecked")
    site.description = "changed"

    with pytest.raises(RuntimeError, match="No row-version check is connected for Site"):
        save_at_version(site, version="0", changed_columns={"description"}, name=site.name)
