"""Concurrency coverage for interface target validation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.apps import apps

from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

pytestmark = pytest.mark.django_db(
    transaction=True,
    # Include every installed app so transaction cleanup cascades through other
    # plugins whose M2M tables are outside Django's default flush list.
    available_apps=[app.name for app in apps.get_app_configs()],
)


def test_selected_vc_target_is_locked_through_interface_sync():
    """A membership update must wait until target validation and sync commit."""
    from dcim.models import Device, Interface
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection

    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, make_view
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    _vc, (page_device, target_device) = make_virtual_chassis_members("sync-target-lock")
    user = make_superuser("sync-target-lock-user")
    validation_done = Event()
    release_sync = Event()
    membership_changed = Event()

    port = {
        "ifName": "Gi0/1",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifAlias": "uplink",
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "port_id": None,
    }

    def sync_interface():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            page = Device.objects.get(pk=page_device.pk)
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request(
                "post",
                {"device_selection_Gi0/1": str(target_device.pk)},
                user=thread_user,
            )
            view = make_view(SyncInterfacesView, request)
            view._post_server_key = "default"
            real_resolve = view._resolve_device_interface

            def pause_after_validation(*args, **kwargs):
                validation_done.set()
                assert release_sync.wait(5), "test did not release the sync transaction"
                return real_resolve(*args, **kwargs)

            view._resolve_device_interface = pause_after_validation
            view.sync_selected_interfaces(page, ["Gi0/1"], [port], ["vlans"], "ifName")
        finally:
            close_old_connections()

    def move_target_out_of_chassis():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            Device.objects.filter(pk=target_device.pk).update(virtual_chassis=None, vc_position=None)
            membership_changed.set()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        sync_future = executor.submit(sync_interface)
        assert validation_done.wait(5), "interface sync did not reach target validation"
        move_future = executor.submit(move_target_out_of_chassis)
        changed_during_sync = membership_changed.wait(0.5)
        release_sync.set()
        sync_future.result(timeout=10)
        move_future.result(timeout=10)

    assert not changed_during_sync
    assert Interface.objects.filter(device=target_device, name="Gi0/1").exists()
