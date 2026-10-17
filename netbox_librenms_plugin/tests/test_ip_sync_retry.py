"""
The IP sync POST runs as one retried transaction: a lock conflict never commits a part of the batch.

Every conflict here is a real PostgreSQL conflict with a second connection that holds a row lock,
under a short ``lock_timeout`` on the test's connection. The one patch counts the attempts and
calls through; it releases the second connection's lock when the second attempt starts.
"""

from types import SimpleNamespace

import pytest
from dcim.models import Device, Interface
from django.db import OperationalError
from ipam.models import IPAddress

from netbox_librenms_plugin.tests.conftest import (
    bind_librenms_server,
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_superuser,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.interface_sync_post_helpers import SERVER_KEY
from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row, lock_timeout, second_connection
from netbox_librenms_plugin.tests.test_interface_late_write import post_ip_sync, seed_ip_rows
from netbox_librenms_plugin.tests.view_test_helpers import make_view, messages_on
from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE, database_error_sqlstate
from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

# Long enough for a blocked statement to be a real lock wait, short enough for two attempts.
LOCK_TIMEOUT_MS = 200
ROWS = [("198.18.30.10", 7031, "Ethernet1"), ("198.18.31.10", 7032, "Ethernet2")]


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


@pytest.fixture
def attempts(monkeypatch):
    """Count IP sync attempts; ``before_retry`` runs when the second attempt starts."""
    real_attempt = SyncIPAddressesView._process_ip_sync
    state = SimpleNamespace(count=0, before_retry=None)

    def counting_attempt(self, *args, **kwargs):
        state.count += 1
        if state.count == 2 and state.before_retry is not None:
            state.before_retry()
        return real_attempt(self, *args, **kwargs)

    monkeypatch.setattr(SyncIPAddressesView, "_process_ip_sync", counting_attempt)
    return state


def _two_rows(name):
    """A device with two interfaces and one cached IP row for each; the second row's interface is returned too."""
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": 44}})
    first = make_interface(device, "Ethernet1", iface_type="1000base-t")
    second = make_interface(device, "Ethernet2", iface_type="1000base-t")
    seed_ip_rows(device, ROWS)
    return device, first, second


@transactional_db_with_all_apps()
def test_a_lock_conflict_in_one_row_rolls_back_the_batch_and_the_retry_syncs_every_row(client, attempts):
    device, first, second = _two_rows("ip-retry-once")
    client.force_login(make_superuser("ip-retry-once-user"))

    with second_connection() as other:
        lock_row(other, Interface, second.pk)
        attempts.before_retry = other.rollback
        with lock_timeout(LOCK_TIMEOUT_MS):
            response = post_ip_sync(client, device, [address for address, _, _ in ROWS])

    assert attempts.count == 2
    assert response.status_code == 302
    assert messages_on(response.wsgi_request) == [("success", "Created IP addresses: 198.18.30.10/24, 198.18.31.10/24")]
    assert IPAddress.objects.get(address="198.18.30.10/24").assigned_object == first
    assert IPAddress.objects.get(address="198.18.31.10/24").assigned_object == second


@transactional_db_with_all_apps()
def test_lock_conflicts_on_both_attempts_give_the_try_again_answer_and_commit_no_row(client, attempts):
    device, _first, second = _two_rows("ip-retry-exhausted")
    client.force_login(make_superuser("ip-retry-exhausted-user"))

    with second_connection() as other:
        lock_row(other, Interface, second.pk)
        with lock_timeout(LOCK_TIMEOUT_MS):
            response = post_ip_sync(client, device, [address for address, _, _ in ROWS])

    assert attempts.count == 2
    assert response.status_code == 302
    assert messages_on(response.wsgi_request) == [("error", TRY_AGAIN_MESSAGE)]
    assert not IPAddress.objects.filter(address__in=[f"{address}/24" for address, _, _ in ROWS]).exists()


@transactional_db_with_all_apps()
def test_the_retry_writes_again_the_librenms_id_that_the_first_attempt_discovered(
    client, attempts, settings, librenms_server
):
    """
    The management-IP lookup discovers the device's LibreNMS ID and saves it; the conflict rolls that back.

    No selected row is the management address, so nothing else reads the device again: the retry
    must still write the ID, or the sync reports success while the ID is missing.
    """
    from netbox_librenms_plugin.server_mappings import read_mapping

    bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
    device = make_device("ip-retry-discovery.example.net")
    make_interface(device, "Ethernet1", iface_type="1000base-t")
    second = make_interface(device, "Ethernet2", iface_type="1000base-t")
    seed_ip_rows(device, ROWS)
    librenms_server.register(
        f"/api/v0/devices/{device.name}", {"status": "ok", "devices": [{"device_id": 4501, "hostname": device.name}]}
    )
    librenms_server.register(
        "/api/v0/devices/4501", {"status": "ok", "devices": [{"device_id": 4501, "ip": "198.18.99.1"}]}
    )
    client.force_login(make_superuser("ip-retry-discovery-user"))

    with second_connection() as other:
        lock_row(other, Interface, second.pk)
        attempts.before_retry = other.rollback
        with lock_timeout(LOCK_TIMEOUT_MS):
            response = post_ip_sync(
                client, device, [address for address, _, _ in ROWS], extra={"set-primary-ip-toggle": "on"}
            )

    assert attempts.count == 2
    assert [level for level, _text in messages_on(response.wsgi_request)] == ["success"]
    device.refresh_from_db()
    assert read_mapping(device).own_id(SERVER_KEY) == 4501


@transactional_db_with_all_apps()
def test_a_lock_conflict_in_the_management_ip_lookup_is_raised_not_read_as_no_ip(settings, librenms_server):
    """The lookup's LibreNMS ID discovery writes the device; a lock conflict there must reach the runner."""
    api = bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
    device = make_device("ip-mgmt-busy.example.net")
    librenms_server.register(
        f"/api/v0/devices/{device.name}", {"status": "ok", "devices": [{"device_id": 4301, "hostname": device.name}]}
    )
    view = make_view(SyncIPAddressesView, librenms_api=api)

    with second_connection() as other:
        lock_row(other, Device, device.pk)
        with lock_timeout(LOCK_TIMEOUT_MS), pytest.raises(OperationalError) as caught:
            view.get_management_ip(device)

    assert database_error_sqlstate(caught.value) == "55P03"
