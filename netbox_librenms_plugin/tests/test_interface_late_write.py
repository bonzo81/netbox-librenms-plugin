"""
A sync writes an existing interface row from a fresh read and saves it only if no other operation changed it since.

Every concurrent change here is a real commit on a second database connection. It lands at a real
point of the sync, between the sync's read of the row and its write: when the writer starts (after
the view read the row), or right after the late write read the row fresh (at its ``snapshot()``).
The patches only call through and commit; nothing replaces the database or the ORM.
"""

import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest
from core.models import ObjectChange
from dcim.models import Interface
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from ipam.models import VLAN, VRF, IPAddress
from utilities.ordering import naturalize_interface

from netbox_librenms_plugin.interface_sync import InterfaceWrite, update_interface_from_port
from netbox_librenms_plugin.middleware import TRY_AGAIN_MESSAGE
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.sync_cache import TAB_SPECS, SyncTab, sync_snapshot_key
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
from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change, second_connection
from netbox_librenms_plugin.tests.view_test_helpers import messages_on
from netbox_librenms_plugin.utils import get_librenms_device_id
from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

BEFORE_THE_FRESH_READ = "before_the_fresh_read"
AFTER_THE_FRESH_READ = "after_the_fresh_read"
COMMIT_POINTS = pytest.mark.parametrize("commit_point", [BEFORE_THE_FRESH_READ, AFTER_THE_FRESH_READ])
PLAIN_AND_HTMX = pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
ATTRIBUTE_WRITER = "update_interface_attributes"
VLAN_WRITER = "_update_interface_vlan_assignment"
CHANGED_TEXT = "NetBox interface {} was changed by another operation. Refresh and try again."


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


@pytest.fixture
def attempts(monkeypatch):
    return count_sync_attempts(monkeypatch)


def commit_at_the_fresh_read(monkeypatch, pk, commit, *, times=1, armed=lambda: True):
    """Run *commit* right after the late write reads interface *pk* fresh, at most *times* times."""
    state = SimpleNamespace(commits=0)
    real_snapshot = Interface.snapshot

    def snapshot(self):
        real_snapshot(self)
        if self.pk == pk and armed() and state.commits < times:
            state.commits += 1
            commit()

    monkeypatch.setattr(Interface, "snapshot", snapshot)
    return state


def commit_during_the_writer(monkeypatch, writer_name, pk, commit, *, point, times=1):
    """
    Run *commit* while the sync runs its writer *writer_name* for interface *pk*, at most *times* times.

    ``BEFORE_THE_FRESH_READ`` commits when the writer starts: the sync has read the row, and the late
    write has not. ``AFTER_THE_FRESH_READ`` commits after the late write of that writer read the row.
    """
    real_writer = getattr(SyncInterfacesView, writer_name)
    state = SimpleNamespace(commits=0, in_writer=False)

    def writer(self, interface, *args, **kwargs):
        if point == BEFORE_THE_FRESH_READ and interface.pk == pk and state.commits < times:
            state.commits += 1
            commit()
        state.in_writer = True
        try:
            return real_writer(self, interface, *args, **kwargs)
        finally:
            state.in_writer = False

    monkeypatch.setattr(SyncInterfacesView, writer_name, writer)
    if point == AFTER_THE_FRESH_READ:
        return commit_at_the_fresh_read(monkeypatch, pk, commit, times=times, armed=lambda: state.in_writer)
    return state


def _unowned_columns(device):
    """Return values for columns that neither writer writes: the links, the VRF, a flag and a label."""
    return {
        "lag_id": make_interface(device, "Po1", iface_type="lag").pk,
        "parent_id": make_interface(device, "eth9", iface_type="1000base-t").pk,
        "bridge_id": make_interface(device, "br0", iface_type="bridge").pk,
        "vrf_id": VRF.objects.create(name=f"{device.name}-vrf").pk,
        "mark_connected": True,
        "label": "set by another operation",
    }


def _column_values(interface, columns):
    row = Interface.objects.filter(pk=interface.pk).values(*columns).get()
    return {column: row[column] for column in columns}


def _synced_interface(device, name, port_id, **fields):
    """Return an interface that a sync of ``sync_port(port_id, name)`` leaves unchanged, with *fields* set."""
    interface = bound_interface(device, name, port_id)
    Interface.objects.filter(pk=interface.pk).update(speed=1_000_000, mtu=1500, enabled=True, **fields)
    interface.refresh_from_db()
    return interface


# ---------------------------------------------------------------------------
# AC1: a concurrent change to a column the writer does not write survives
# ---------------------------------------------------------------------------


@transactional_db_with_all_apps()
@COMMIT_POINTS
def test_a_concurrent_change_to_unowned_columns_survives_the_attribute_write(
    client, attempts, monkeypatch, commit_point
):
    device = make_device(f"late-write-attr-{commit_point}", librenms_cf={SERVER_KEY: {"id": 1}})
    interface = bound_interface(device, "eth10", 10)
    concurrent = _unowned_columns(device)
    seed_ports(device, [sync_port(10, "eth10", alias="uplink")])
    client.force_login(make_superuser("late-write-attr-user"))

    with second_connection() as other:
        commits = commit_during_the_writer(
            monkeypatch,
            ATTRIBUTE_WRITER,
            interface.pk,
            lambda: commit_row_change(other, Interface, interface.pk, concurrent),
            point=commit_point,
        )
        response = post_interface_sync(client, device, [10], htmx=False)

    assert commits.commits == 1
    # A change after the fresh read makes the row version stale: the attempt is run once more.
    assert attempts.count == (2 if commit_point == AFTER_THE_FRESH_READ else 1)
    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]
    assert _column_values(interface, concurrent) == concurrent
    assert _column_values(interface, ["description", "speed", "mtu"]) == {
        "description": "uplink",
        "speed": 1_000_000,
        "mtu": 1500,
    }


@transactional_db_with_all_apps()
@COMMIT_POINTS
def test_a_concurrent_change_to_unowned_columns_survives_the_vlan_write(client, attempts, monkeypatch, commit_point):
    """Only the mode and the untagged VLAN change, so the attribute writer does not write and the VLAN helper does."""
    device = make_device(f"late-write-vlan-{commit_point}", librenms_cf={SERVER_KEY: {"id": 2}})
    interface = _synced_interface(device, "eth10", 10)
    vlan = VLAN.objects.create(vid=100, name=f"late-write-vlan-{commit_point}")
    concurrent = _unowned_columns(device)
    seed_ports(device, [sync_port(10, "eth10", untagged_vlan=100, tagged_vlans=[])])
    client.force_login(make_superuser("late-write-vlan-user"))

    with second_connection() as other:
        commits = commit_during_the_writer(
            monkeypatch,
            VLAN_WRITER,
            interface.pk,
            lambda: commit_row_change(other, Interface, interface.pk, concurrent),
            point=commit_point,
        )
        response = post_interface_sync(client, device, [10], htmx=False, exclude_columns=("mac_address",))

    assert commits.commits == 1
    assert attempts.count == (2 if commit_point == AFTER_THE_FRESH_READ else 1)
    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]
    assert _column_values(interface, concurrent) == concurrent
    assert _column_values(interface, ["mode", "untagged_vlan_id"]) == {"mode": "access", "untagged_vlan_id": vlan.pk}


# ---------------------------------------------------------------------------
# AC2: the type is checked against the row's links as they are at the write
# ---------------------------------------------------------------------------


@transactional_db_with_all_apps()
@COMMIT_POINTS
def test_a_concurrent_lag_keeps_a_planned_virtual_type_from_being_stored(client, attempts, monkeypatch, commit_point):
    InterfaceTypeMapping.objects.create(librenms_type="propVirtual", netbox_type="virtual")
    device = make_device(f"late-write-type-{commit_point}", librenms_cf={SERVER_KEY: {"id": 3}})
    interface = bound_interface(device, "eth10", 10)
    lag = make_interface(device, "Po1", iface_type="lag")
    seed_ports(device, [sync_port(10, "eth10", if_type="propVirtual")])
    client.force_login(make_superuser("late-write-type-user"))

    with second_connection() as other:
        commit_during_the_writer(
            monkeypatch,
            ATTRIBUTE_WRITER,
            interface.pk,
            lambda: commit_row_change(other, Interface, interface.pk, {"lag_id": lag.pk}),
            point=commit_point,
        )
        post_interface_sync(client, device, [10], htmx=False)

    assert attempts.count == (2 if commit_point == AFTER_THE_FRESH_READ else 1)
    assert _column_values(interface, ["type", "lag_id"]) == {"type": "other", "lag_id": lag.pk}


# ---------------------------------------------------------------------------
# AC7: _name, last_updated and the change record of a written row
# ---------------------------------------------------------------------------


@transactional_db_with_all_apps()
def test_a_written_row_keeps_its_ordering_name_timestamp_and_change_record(client, monkeypatch):
    device = make_device("late-write-changelog", librenms_cf={SERVER_KEY: {"id": 4}})
    interface = bound_interface(device, "eth10", 10)
    Interface.objects.filter(pk=interface.pk).update(description="old")
    interface.refresh_from_db()
    seed_ports(device, [sync_port(10, "eth11", alias="uplink")])
    client.force_login(make_superuser("late-write-changelog-user"))

    with second_connection() as other:
        commit_during_the_writer(
            monkeypatch,
            ATTRIBUTE_WRITER,
            interface.pk,
            lambda: commit_row_change(other, Interface, interface.pk, {"label": "set by another operation"}),
            point=BEFORE_THE_FRESH_READ,
        )
        post_interface_sync(client, device, [10], htmx=False)

    written = Interface.objects.get(pk=interface.pk)
    assert written.name == "eth11"
    assert written._name == naturalize_interface("eth11", max_length=100)
    assert written.last_updated > interface.last_updated
    change = ObjectChange.objects.get(
        changed_object_type=ContentType.objects.get_for_model(Interface), changed_object_id=interface.pk
    )
    assert change.action == "update"
    # The before-state is the row as the late write read it, with the other operation's label.
    assert {key: change.prechange_data[key] for key in ("name", "description", "label")} == {
        "name": "eth10",
        "description": "old",
        "label": "set by another operation",
    }
    assert {key: change.postchange_data[key] for key in ("name", "description", "label")} == {
        "name": "eth11",
        "description": "uplink",
        "label": "set by another operation",
    }


# ---------------------------------------------------------------------------
# A stale row version is a lock conflict of the whole attempt
# ---------------------------------------------------------------------------


@transactional_db_with_all_apps()
@PLAIN_AND_HTMX
def test_a_row_that_is_stale_on_both_attempts_gives_the_try_again_answer_and_writes_nothing(
    client, attempts, monkeypatch, htmx
):
    device = make_device(f"late-write-stale-twice-{htmx}", librenms_cf={SERVER_KEY: {"id": 5}})
    interface = bound_interface(device, "eth10", 10)
    seed_ports(device, [sync_port(10, "eth10", alias="uplink")])
    client.force_login(make_superuser("late-write-stale-twice-user"))
    labels = iter(["first change", "second change"])

    with second_connection() as other:
        commits = commit_during_the_writer(
            monkeypatch,
            ATTRIBUTE_WRITER,
            interface.pk,
            lambda: commit_row_change(other, Interface, interface.pk, {"label": next(labels)}),
            point=AFTER_THE_FRESH_READ,
            times=2,
        )
        response = post_interface_sync(client, device, [10], htmx=htmx)

    assert (attempts.count, commits.commits) == (2, 2)
    assert_try_again_answer(response, device, htmx, TRY_AGAIN_MESSAGE)
    assert _column_values(interface, ["label", "description", "speed"]) == {
        "label": "second change",
        "description": "",
        "speed": None,
    }


@transactional_db_with_all_apps()
def test_a_stale_row_version_that_a_broad_handler_swallows_is_still_retried(client, attempts, monkeypatch):
    device = make_device("late-write-swallowed", librenms_cf={SERVER_KEY: {"id": 6}})
    interface = bound_interface(device, "eth10", 10)
    seed_ports(device, [sync_port(10, "eth10", alias="uplink")])
    client.force_login(make_superuser("late-write-swallowed-user"))
    real_writer = SyncInterfacesView.update_interface_attributes
    swallowed = []

    def writer_in_a_broad_row_handler(self, interface, *args, **kwargs):
        try:
            with transaction.atomic():
                return real_writer(self, interface, *args, **kwargs)
        except Exception as exc:
            swallowed.append(type(exc).__name__)
            return InterfaceWrite(interface, False)

    monkeypatch.setattr(SyncInterfacesView, ATTRIBUTE_WRITER, writer_in_a_broad_row_handler)
    with second_connection() as other:
        commit_during_the_writer(
            monkeypatch,
            ATTRIBUTE_WRITER,
            interface.pk,
            lambda: commit_row_change(other, Interface, interface.pk, {"label": "set by another operation"}),
            point=AFTER_THE_FRESH_READ,
        )
        response = post_interface_sync(client, device, [10], htmx=False)

    assert swallowed == ["ConcurrentRowChange"]
    assert attempts.count == 2
    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]
    assert _column_values(interface, ["label", "description"]) == {
        "label": "set by another operation",
        "description": "uplink",
    }


@transactional_db_with_all_apps()
def test_a_row_that_left_its_owner_after_the_read_is_not_written(client, attempts, monkeypatch):
    """The fresh read finds no row of the expected owner; the retry resolves the port again and skips it."""
    device = make_device("late-write-moved", librenms_cf={SERVER_KEY: {"id": 7}})
    elsewhere = make_device("late-write-moved-elsewhere")
    interface = bound_interface(device, "eth10", 10)
    seed_ports(device, [sync_port(10, "eth10", alias="uplink")])
    client.force_login(make_superuser("late-write-moved-user"))

    with second_connection() as other:
        commit_during_the_writer(
            monkeypatch,
            ATTRIBUTE_WRITER,
            interface.pk,
            lambda: commit_row_change(other, Interface, interface.pk, {"device_id": elsewhere.pk}),
            point=BEFORE_THE_FRESH_READ,
        )
        response = post_interface_sync(client, device, [10], htmx=False)

    assert attempts.count == 2
    assert messages_on(response.wsgi_request) == [
        ("warning", "1 interface(s) skipped: eth10 (port already mapped elsewhere or ambiguous).")
    ]
    assert _column_values(interface, ["device_id", "description"]) == {"device_id": elsewhere.pk, "description": ""}


# ---------------------------------------------------------------------------
# The IP tab's create-missing path runs outside the runner: a stale row fails alone
# ---------------------------------------------------------------------------


def _ip_row(address, port_id):
    # No interface name: the IP tab does not match the row to an interface, so it resolves one from the port.
    return {"ip_address": address, "prefix_length": 24, "ip_with_mask": f"{address}/24", "port_id": port_id}


@transactional_db_with_all_apps()
def test_a_stale_row_in_the_ip_tab_fails_with_the_fixed_text_and_the_other_rows_sync(client, monkeypatch):
    device = make_device("late-write-ip", librenms_cf={SERVER_KEY: {"id": 42}})
    stale = make_interface(device, "Ethernet1", iface_type="1000base-t")
    fine = make_interface(device, "Ethernet2", iface_type="1000base-t")
    ports = {
        port_id: sync_port(port_id, name, alias=f"{name} uplink")
        for port_id, name in ((7020, "Ethernet1"), (7021, "Ethernet2"))
    }
    cache.set(
        sync_snapshot_key(device, TAB_SPECS[SyncTab.IP_ADDRESSES].data_type, SERVER_KEY),
        {
            "ip_addresses": [_ip_row("198.18.20.10", 7020), _ip_row("198.18.21.10", 7021)],
            "mgmt_ip": "",
            "ports_by_id": ports,
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    client.force_login(make_superuser("late-write-ip-user"))

    with second_connection() as other:
        commit_at_the_fresh_read(
            monkeypatch,
            stale.pk,
            lambda: commit_row_change(other, Interface, stale.pk, {"label": "set by another operation"}),
        )
        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
                kwargs={"object_type": "device", "pk": device.pk},
            ),
            {
                "server_key": SERVER_KEY,
                "create-missing-interfaces-toggle": "on",
                "select": ["198.18.20.10/24", "198.18.21.10/24"],
                "vrf_198.18.20.10/24": "",
                "vrf_198.18.21.10/24": "",
            },
        )

    assert response.status_code == 302
    errors = [text for level, text in messages_on(response.wsgi_request) if level == "error"]
    assert errors == [f"Failed to sync IP addresses: 198.18.20.10/24 ({CHANGED_TEXT.format('Ethernet1')})"]
    stale.refresh_from_db()
    assert (stale.label, stale.description) == ("set by another operation", "")
    assert get_librenms_device_id(stale, SERVER_KEY, auto_save=False) is None
    assert not IPAddress.objects.filter(address="198.18.20.10/24").exists()
    fine.refresh_from_db()
    assert fine.description == "Ethernet2 uplink"
    assert IPAddress.objects.get(address="198.18.21.10/24").assigned_object == fine


# ---------------------------------------------------------------------------
# Guard: the two writers write an existing row only through the late write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "writer",
    [update_interface_from_port, VlanAssignmentMixin._update_interface_vlan_assignment],
    ids=["attribute-writer", "vlan-helper"],
)
def test_the_writers_save_an_existing_interface_row_only_through_the_late_write(writer):
    tree = ast.parse(textwrap.dedent(inspect.getsource(writer)))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    # A save or a queryset write here would skip the fresh read and the row-version check.
    row_writes = [
        ast.unparse(call)
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and call.func.attr in {"save", "update", "bulk_update", "update_or_create"}
    ]

    assert row_writes == []
    assert any(isinstance(call.func, ast.Name) and call.func.id == "write_interface_row" for call in calls)
