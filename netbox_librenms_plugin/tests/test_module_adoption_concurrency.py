"""Concurrency coverage for automatic module interface adoption."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from django.apps import apps

from netbox_librenms_plugin.tests.conftest import make_device, make_interface

pytestmark = pytest.mark.django_db(
    transaction=True,
    available_apps=[app.name for app in apps.get_app_configs()],
)


def test_concurrent_hidden_interface_is_not_adopted(monkeypatch):
    """Authorize the interface that Module.save adopts after its internal lookup."""
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
    original_full_clean = Module.full_clean

    def pause_before_save(module, *args, **kwargs):
        result = original_full_clean(module, *args, **kwargs)
        if module.device_id == device.pk and module.module_bay_id == bay.pk and module.pk is None:
            install_ready.set()
            assert winner_committed.wait(5), "test did not commit the competing interface"
        return result

    monkeypatch.setattr(Module, "full_clean", pause_before_save)

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
            return view.post(request, pk=thread_device.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(install_module)
        assert install_ready.wait(5), "module install did not reach the adoption race window"
        # The worker holds an open transaction while it waits. Bound the competing write too, so a
        # conflicting row lock fails with a readable error instead of hanging the run. transaction=True
        # leaves the connection in autocommit, where postgres ignores a SET LOCAL, so the timeout and
        # the insert it guards must share one explicit transaction.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '5s'")
            winner = Interface.objects.create(device=device, name="Te1/1/1", type="10gbase-x-sfpp")
        winner_committed.set()
        future.result(timeout=10)

    winner.refresh_from_db()
    assert winner.module_id is None
    assert not Module.objects.filter(device=device, module_bay=bay).exists()
