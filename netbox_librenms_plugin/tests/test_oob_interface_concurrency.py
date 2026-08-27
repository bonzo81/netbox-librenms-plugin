"""Concurrency coverage for OOB interface name reuse."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident

import pytest
from django.apps import apps

from netbox_librenms_plugin.tests.conftest import make_device, make_interface

pytestmark = pytest.mark.django_db(
    transaction=True,
    available_apps=[app.name for app in apps.get_app_configs()],
)


def test_concurrent_hidden_interface_winner_is_not_reused(monkeypatch):
    """The uniqueness-race winner must stay inside the caller's Interface view grant."""
    from dcim.models import Device, Interface
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
    from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

    device = make_device("oob-interface-race")
    allowed = make_interface(device, "eth0")
    user = make_user_with_perms("oob-interface-race", [("add", Interface)])
    user = grant(user, "view", Interface, constraints={"pk": allowed.pk})
    create_ready = Event()
    winner_committed = Event()
    original_full_clean = Interface.full_clean
    resolver_thread = {}

    def pause_before_create(interface, *args, **kwargs):
        result = original_full_clean(interface, *args, **kwargs)
        # Pin the pause to the resolver thread. The patch is on the class, so it is live on the
        # main thread too, whose competing create matches device/name/pk exactly. create() does
        # not call full_clean() today; if that changes, the main thread would wait on an event
        # only it can set and the test would stall behind a misleading message.
        if (
            get_ident() == resolver_thread.get("id")
            and interface.device_id == device.pk
            and interface.name == "idrac0"
            and interface.pk is None
        ):
            create_ready.set()
            assert winner_committed.wait(5), "test did not commit the competing interface"
        return result

    monkeypatch.setattr(Interface, "full_clean", pause_before_create)

    def resolve_interface():
        resolver_thread["id"] = get_ident()
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            thread_device = Device.objects.get(pk=device.pk)
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request(
                "post",
                {"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"},
                user=thread_user,
            )
            with transaction.atomic():
                return AddAsOOBView._resolve_oob_interface(request, thread_device)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(resolve_interface)
        assert create_ready.wait(5), "OOB resolution did not reach the create race window"
        winner = Interface.objects.create(device=device, name="idrac0", type="other")
        winner_committed.set()
        resolved, reason = future.result(timeout=10)

    # The race winner sits outside the caller's view grant, so it is refused with the same
    # reason as the pre-create check rather than the "no selection made" pair.
    assert resolved is None and reason == "name_out_of_scope"
    assert Interface.objects.filter(pk=winner.pk).exists()


def test_an_interface_outside_the_view_scope_is_never_locked():
    """Verify an interface outside the caller's view scope is rejected without taking its row lock."""
    from dcim.models import Interface
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
    from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

    device = make_device("oob-lock-scope")
    hidden = make_interface(device, "idrac0")  # exists, but the user gets no view grant for it
    user = make_user_with_perms("oob-lock-scope", [("add", Interface)])

    holder_has_lock = Event()
    release_holder = Event()

    def hold_the_row():
        close_old_connections()
        try:
            with transaction.atomic():
                Interface.objects.select_for_update().filter(pk=hidden.pk).first()
                holder_has_lock.set()
                release_holder.wait(10)
        finally:
            close_old_connections()

    request = make_request("post", {"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"}, user=user)

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_the_row)
        assert holder_has_lock.wait(10), "helper thread never took the row lock"
        try:
            with transaction.atomic():
                # Fail fast rather than hang: the unfixed resolver blocks here on the held row.
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '2s'")
                resolved, reason = AddAsOOBView._resolve_oob_interface(request, device)
        finally:
            release_holder.set()
            holder.result(timeout=10)

    # The name is taken by a row this caller cannot see, so it is refused — without ever locking it.
    assert resolved is None
    assert reason == "name_out_of_scope"


def test_hidden_ip_row_is_not_locked_by_an_out_of_scope_caller():
    """Verify a caller rejects a hidden matching IP without taking its row lock."""
    from dcim.models import Device, Interface
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection, transaction
    from ipam.models import IPAddress

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
    from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

    device = make_device("oob-ip-scope")
    target = make_interface(device, "idrac0")
    hidden_ip = IPAddress.objects.create(address="10.77.0.5/32")
    visible_ip = IPAddress.objects.create(address="10.77.0.9/32")
    user = make_user_with_perms("oob-ip-scope", [("add", IPAddress)])
    # The grant deliberately excludes hidden_ip, so the caller may not change it.
    user = grant(user, "change", IPAddress, constraints={"pk": visible_ip.pk})

    row_locked = Event()
    release_row = Event()

    def hold_hidden_row():
        close_old_connections()
        try:
            with transaction.atomic():
                IPAddress.objects.select_for_update().get(pk=hidden_ip.pk)
                row_locked.set()
                assert release_row.wait(10), "test did not release the hidden row"
        finally:
            close_old_connections()

    def attach_as_caller():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '2s'")
                cursor.execute("SET statement_timeout = '10s'")
            thread_user = get_user_model().objects.get(pk=user.pk)
            thread_iface = Interface.objects.get(pk=target.pk)
            request = make_request("post", {}, user=thread_user)
            with transaction.atomic():
                return AddAsOOBView._attach_oob_ip(request, "10.77.0.5", thread_iface)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_hidden_row)
        assert row_locked.wait(10), "the hidden row was never locked"
        caller = executor.submit(attach_as_caller)
        try:
            resolved, reason = caller.result(timeout=20)
        finally:
            release_row.set()
            holder.result(timeout=10)

    # Refused on scope, not blocked on the lock, and the hidden row is untouched.
    assert resolved is None
    assert reason == "permission_change"
    hidden_ip.refresh_from_db()
    assert hidden_ip.assigned_object_id is None
    assert Device.objects.filter(pk=device.pk, oob_ip__isnull=True).exists()
