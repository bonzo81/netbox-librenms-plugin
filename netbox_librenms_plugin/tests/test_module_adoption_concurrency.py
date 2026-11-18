"""Concurrency coverage for automatic module interface adoption."""

from threading import Event
from types import SimpleNamespace

import pytest
from django.apps import apps

from netbox_librenms_plugin.tests.conftest import bounded_executor, make_device, make_interface

pytestmark = pytest.mark.django_db(
    transaction=True,
    available_apps=[app.name for app in apps.get_app_configs()],
)


def _before_the_owner_lock(sql):
    return '"dcim_device"' in sql and "FOR NO KEY UPDATE" in sql


def _after_the_adoption_scan(sql):
    return '"dcim_interface"' in sql and "FOR UPDATE" in sql


@pytest.mark.parametrize(
    "commit_point", [_before_the_owner_lock, _after_the_adoption_scan], ids=["before-owner-lock", "after-scan"]
)
def test_concurrent_hidden_interface_is_not_adopted(commit_point):
    """A hidden interface committed before Device ownership, or after the adoption scan, must not be adopted."""
    from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleBay, ModuleType
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
    from netbox_librenms_plugin.views.sync.modules import InstallModuleView

    device = make_device("module-adoption-race")
    bay = ModuleBay.objects.create(device=device, name="Adoption Race Bay")
    module_type = ModuleType.objects.create(
        manufacturer=device.device_type.manufacturer,
        model="Adoption Race Type",
    )
    InterfaceTemplate.objects.create(
        module_type=module_type,
        name="Te1/1/1",
        type="10gbase-x-sfpp",
    )
    allowed = make_interface(device, "Te1/1/2", iface_type="10gbase-x-sfpp")
    user = make_user_with_perms(
        "module-adoption-race",
        [
            ("view", Device),
            ("view", ModuleBay),
            ("view", ModuleType),
            ("add", Module),
            ("add", Interface),
            ("delete", Interface),
        ],
    )
    user = grant(user, "change", Interface, constraints={"pk": allowed.pk})
    install_ready = Event()
    winner_committed = Event()

    def pause_at_the_commit_point(execute, sql, params, many, context):
        if not install_ready.is_set() and commit_point(sql):
            if commit_point is _after_the_adoption_scan:
                # The scan finds no candidate; the interface commits before Module.save() adopts by name.
                result = execute(sql, params, many, context)
                install_ready.set()
                assert winner_committed.wait(5), "test did not commit the competing interface"
                return result
            install_ready.set()
            assert winner_committed.wait(5), "test did not commit the competing interface"
        return execute(sql, params, many, context)

    def install_module():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            thread_device = Device.objects.get(pk=device.pk)
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request(
                "post",
                {
                    "module_bay_id": str(bay.pk),
                    "module_type_id": str(module_type.pk),
                    "server_key": "default",
                },
                user=thread_user,
            )
            view = InstallModuleView()
            view._librenms_api = SimpleNamespace(server_key="default")
            view.setup(request, pk=thread_device.pk)
            with connection.execute_wrapper(pause_at_the_commit_point):
                return view.post(request, pk=thread_device.pk)
        finally:
            close_old_connections()

    with bounded_executor(max_workers=1) as executor:
        future = executor.submit(install_module)
        assert install_ready.wait(5), "module install did not reach the adoption race window"
        # The worker holds an open transaction while it waits. Bound the competing write too, so a
        # conflicting row lock fails with a readable error instead of hanging the run. transaction=True
        # leaves the connection in autocommit, where postgres ignores a SET LOCAL, so the timeout and
        # the insert it guards must share one explicit transaction.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '5s'")
            winner = Interface(device=device, name="Te1/1/1", type="10gbase-x-sfpp")
            if commit_point is _after_the_adoption_scan:
                # A save updates the Device interface counter, which waits for the owner lock; an insert does not.
                Interface.objects.bulk_create([winner])
            else:
                winner.save()
        winner_committed.set()
        future.result(timeout=10)

    winner.refresh_from_db()
    assert winner.module_id is None
    assert not Module.objects.filter(device=device, module_bay=bay).exists()
