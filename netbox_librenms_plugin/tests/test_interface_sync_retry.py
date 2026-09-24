"""
The interface sync POST runs as one retried transaction and publishes only what its committed attempt reports.

Every conflict here is a real PostgreSQL conflict with a second connection. The only patch is a
counting wrapper around the owner lock, which each attempt calls once, so a test can count attempts
and release the second connection's lock between them.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from dcim.models import Device, Interface
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import OperationalError, connection, transaction
from django.urls import reverse

from netbox_librenms_plugin.middleware import FOLLOW_UP_FAILED_MESSAGE, REQUEST_FAILED_EVENT, TRY_AGAIN_MESSAGE
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_superuser,
    transactional_db_with_all_apps,
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
from netbox_librenms_plugin.utils import set_librenms_device_id
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

SERVER_KEY = "default"
# Long enough for a blocked statement to be a real lock wait, short enough for two attempts.
LOCK_TIMEOUT_MS = 200
SYNCED = "Selected interfaces synced successfully."
PLAIN_AND_HTMX = pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])


def _port(port_id, name, *, alias=""):
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifType": "ethernetCsmacd",
        "ifAdminStatus": "up",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": "",
        "ifAlias": alias,
    }


def _seed(device, ports):
    payload = {"ports": ports, "port_stack_relationships": {}}
    cache.set(SyncInterfacesView().get_cache_key(device, "ports", SERVER_KEY), payload, timeout=300)


def _sync_page(device):
    return "http://testserver" + reverse("dcim:device_librenms_sync", kwargs={"pk": device.pk}) + "?tab=interfaces"


def _post(client, device, port_ids, *, htmx, exclude_columns=("vlans", "mac_address")):
    url = (
        reverse(
            "plugins:netbox_librenms_plugin:sync_selected_interfaces",
            kwargs={"object_type": "device", "object_id": device.pk},
        )
        + "?interface_name_field=ifName"
    )
    headers = {"HTTP_REFERER": _sync_page(device)}
    if htmx:
        headers["HTTP_HX_REQUEST"] = "true"
    data = {
        "server_key": SERVER_KEY,
        "select": [str(port_id) for port_id in port_ids],
        "exclude_columns": list(exclude_columns),
    }
    return client.post(url, data, **headers)


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


@pytest.fixture
def attempts(monkeypatch):
    """Count sync attempts at the owner lock, and run ``before_retry`` when the second attempt starts."""
    real_lock = SyncInterfacesView._lock_selected_device_targets
    state = SimpleNamespace(count=0, before_retry=None)

    def counting_lock(self, obj):
        state.count += 1
        if state.count == 2 and state.before_retry is not None:
            state.before_retry()
        return real_lock(self, obj)

    monkeypatch.setattr(SyncInterfacesView, "_lock_selected_device_targets", counting_lock)
    return state


def _assert_try_again_answer(response, device, htmx, message):
    """Assert the one visible answer the middleware gives for a lock conflict."""
    if htmx:
        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert json.loads(response["HX-Trigger"]) == {REQUEST_FAILED_EVENT: None}
        assert message in response.content.decode()
        assert messages_on(response.wsgi_request) == []
    else:
        assert response.status_code == 302
        assert response["Location"] == _sync_page(device)
        assert messages_on(response.wsgi_request) == [("error", message)]


@transactional_db_with_all_apps()
@PLAIN_AND_HTMX
def test_a_lock_conflict_on_the_first_attempt_is_retried_once(client, attempts, htmx):
    device = make_device(f"sync-retry-once-{htmx}", librenms_cf={SERVER_KEY: {"id": 91}})
    _seed(device, [_port(10, "eth10")])
    client.force_login(make_superuser("sync-retry-once-user"))

    with second_connection() as other:
        lock_row(other, Device, device.pk)
        attempts.before_retry = other.rollback
        with lock_timeout(LOCK_TIMEOUT_MS):
            response = _post(client, device, [10], htmx=htmx)

    assert attempts.count == 2
    assert response.status_code == (200 if htmx else 302)
    assert Interface.objects.filter(device=device, name="eth10").exists()
    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]


@transactional_db_with_all_apps()
@PLAIN_AND_HTMX
def test_conflicts_on_both_attempts_give_one_try_again_answer_and_change_nothing(client, attempts, htmx):
    device = make_device(f"sync-retry-exhausted-{htmx}", librenms_cf={SERVER_KEY: {"id": 92}})
    _seed(device, [_port(10, "eth10")])
    client.force_login(make_superuser("sync-retry-exhausted-user"))

    with second_connection() as other:
        lock_row(other, Device, device.pk)
        with lock_timeout(LOCK_TIMEOUT_MS):
            response = _post(client, device, [10], htmx=htmx)

    assert attempts.count == 2
    _assert_try_again_answer(response, device, htmx, TRY_AGAIN_MESSAGE)
    assert not Interface.objects.filter(device=device).exists()


@transactional_db_with_all_apps()
def test_a_real_deadlock_on_the_first_attempt_is_retried_once(client, attempts):
    """The sync holds its Device and waits for an interface; the other session holds that interface and waits for the Device."""
    device = make_device("sync-deadlock", librenms_cf={SERVER_KEY: {"id": 96}})
    interface = make_interface(device, "eth10")
    set_librenms_device_id(interface, 10, SERVER_KEY)
    interface.save()
    _seed(device, [_port(10, "eth10", alias="uplink")])
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
        response = _post(client, device, [10], htmx=False)
        holder.result(timeout=10)

    assert attempts.count == 2
    assert response.status_code == 302
    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]
    interface.refresh_from_db()
    assert interface.description == "uplink"


@transactional_db_with_all_apps()
def test_a_retried_sync_reports_its_warnings_skips_and_success_once(client, attempts):
    """The attempt that conflicts has already warned, skipped a row and counted a sync; none of it is published."""
    from ipam.models import VLAN

    device = make_device("sync-retry-report", librenms_cf={SERVER_KEY: {"id": 93}})
    interface = make_interface(device, "eth10")
    set_librenms_device_id(interface, 10, SERVER_KEY)
    interface.save()
    # Port 7 is bound to another device's interface, so every attempt skips its row.
    elsewhere = make_interface(make_device("sync-retry-report-elsewhere"), "eth7")
    set_librenms_device_id(elsewhere, 7, SERVER_KEY)
    elsewhere.save()
    # A VLAN the user cannot view, so every attempt warns that the VLAN scope is incomplete.
    VLAN.objects.create(vid=812, name="sync-retry-report-hidden")
    user = make_user_with_perms("sync-retry-report-user", [("view", Device), ("add", Interface), ("change", Interface)])
    client.force_login(user)
    _seed(device, [_port(7, "eth7"), _port(10, "eth10", alias="uplink")])

    with second_connection() as other:
        # The first attempt warns and skips, then waits for this row at its write.
        lock_row(other, Interface, interface.pk)
        attempts.before_retry = other.rollback
        with lock_timeout(LOCK_TIMEOUT_MS):
            response = _post(client, device, [7, 10], htmx=False, exclude_columns=("mac_address",))

    reported = messages_on(response.wsgi_request)
    assert attempts.count == 2
    assert len([text for _level, text in reported if text.startswith("VLANs were not synced")]) == 1, reported
    assert [text for _level, text in reported if "skipped" in text] == [
        "1 interface(s) skipped: eth7 (port already mapped elsewhere or ambiguous)."
    ]
    assert [text for level, text in reported if level == "success"] == [SYNCED]
    interface.refresh_from_db()
    assert interface.description == "uplink"


@transactional_db_with_all_apps()
@PLAIN_AND_HTMX
def test_a_follow_up_conflict_after_the_commit_is_reported_and_not_retried(client, attempts, monkeypatch, htmx):
    device = make_device(f"sync-follow-up-{htmx}", librenms_cf={SERVER_KEY: {"id": 94}})
    locked = make_device(f"sync-follow-up-locked-{htmx}")
    _seed(device, [_port(10, "eth10")])
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
        response = _post(client, device, [10], htmx=htmx)

    assert attempts.count == 1
    _assert_try_again_answer(response, device, htmx, FOLLOW_UP_FAILED_MESSAGE)
    assert Interface.objects.filter(device=device, name="eth10").exists(), "the committed sync stays"


@pytest.mark.django_db
def test_an_integrity_error_after_a_swallowed_conflict_reaches_the_sync_handler(client, attempts, monkeypatch):
    """A swallowed 55P03 does not make a later 23505 a retry; the sync's own handler reports it."""
    device = make_device("sync-mixed-error", librenms_cf={SERVER_KEY: {"id": 95}})
    _seed(device, [_port(10, "eth10")])
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
        response = _post(client, device, [10], htmx=False)

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
