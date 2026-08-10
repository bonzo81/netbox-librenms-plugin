"""Concurrency coverage for OOB interface name reuse."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

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

    def pause_before_create(interface, *args, **kwargs):
        result = original_full_clean(interface, *args, **kwargs)
        if interface.device_id == device.pk and interface.name == "idrac0" and interface.pk is None:
            create_ready.set()
            assert winner_committed.wait(5), "test did not commit the competing interface"
        return result

    monkeypatch.setattr(Interface, "full_clean", pause_before_create)

    def resolve_interface():
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

    assert resolved is None and reason is None
    assert Interface.objects.filter(pk=winner.pk).exists()


def test_an_interface_outside_the_view_scope_is_never_locked():
    """Locking must happen inside the caller's view scope, not before it.

    The resolver used to lock the (device, name) row and only hide it afterwards, so a caller could
    take a row lock on an interface it cannot see and stall whoever legitimately owns it. Hold that
    row from a second connection: the scoped resolver must return without waiting on it.
    """
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
    assert reason is None
