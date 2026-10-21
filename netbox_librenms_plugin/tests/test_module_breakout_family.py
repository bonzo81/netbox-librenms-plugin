"""A module install leaves the channel rows of a predicted breakout family to the naming plugin."""

from contextlib import contextmanager

import pytest

from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.tests.cache_test_helpers import seed_inventory
from netbox_librenms_plugin.tests.conftest import (
    make_device_with_module_bays,
    make_interface,
    make_module_type,
    make_superuser,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.test_module_install_views import _item, _post_branch, _table_rows
from netbox_librenms_plugin.tests.test_module_install_views import _with_installable_child
from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, render_module_tab
from netbox_librenms_plugin.tests.view_test_helpers import post as view_post
from netbox_librenms_plugin.tests.view_test_helpers import rendered_module_form

pytestmark = pytest.mark.django_db

PARENT = "xe-1/1/1"
CHANNELS = [f"{PARENT}:{lane}" for lane in range(4)]


@contextmanager
def _predicts(mapping):
    """Connect a real prediction receiver that answers *mapping* for the names it knows."""
    from netbox_librenms_plugin.signals import predict_module_interface_names

    def receiver(sender, device, module, names, **kwargs):
        return [predicted for name in names for predicted in mapping.get(name, [name])]

    predict_module_interface_names.connect(receiver, dispatch_uid="test-breakout-family", weak=False)
    try:
        yield
    finally:
        predict_module_interface_names.disconnect(dispatch_uid="test-breakout-family")


def _optic_type(model, template_name):
    from dcim.models import InterfaceTemplate

    module_type = make_module_type(model)
    InterfaceTemplate.objects.create(module_type=module_type, name=template_name, type="10gbase-x-sfpp")
    return module_type


class TestInstallBind:
    """The install binds the optic's port, and moves the row into the module only when the plugin owns it."""

    def test_a_breakout_channel_keeps_no_module_and_gets_its_port(self, live_librenms):
        from dcim.models import Interface, Module

        device = make_device_with_module_bays("breakout-install", ["Slot 1"])
        seed_mapping(device, "default", own=81)
        module_type = _optic_type("BREAKOUT-OPTIC", PARENT)
        channels = [make_interface(device, name) for name in CHANNELS]
        rows = [_item(10, module_type.model, "Slot 1", _librenms_port_id=6701, _librenms_ifname=CHANNELS[0])]

        with _predicts({PARENT: [PARENT, *CHANNELS]}):
            request = _post_branch(device, _with_installable_child(rows, 10, "breakout-install"), 10, 81, live_librenms)
            table_row = next(row for row in _table_rows(device) if row.get("ent_physical_index") == 10)

        module = Module.objects.get(device=device, module_type=module_type)
        bound = Interface.objects.get(pk=channels[0].pk)
        assert any("Installed" in text for text in message_texts(request, "success"))
        assert read_mapping(bound).own_id("default") == 6701
        assert [Interface.objects.get(pk=row.pk).module_id for row in channels] == [None] * 4
        assert Interface.objects.get(device=device, name=PARENT).module_id == module.pk
        # Nothing is left to update, so the table offers no Update Interface action.
        assert not table_row.get("can_update_interface_binding")

    def test_a_one_to_one_rename_still_moves_the_bound_row(self, live_librenms):
        from dcim.models import Interface, Module

        device = make_device_with_module_bays("rename-install", ["Slot 1"])
        seed_mapping(device, "default", own=82)
        module_type = _optic_type("RENAME-OPTIC", "Ethernet1/1")
        renamed = make_interface(device, "Eth1/1")
        rows = [_item(10, module_type.model, "Slot 1", _librenms_port_id=6702, _librenms_ifname="Eth1/1")]

        with _predicts({"Ethernet1/1": ["Eth1/1"]}):
            _post_branch(device, _with_installable_child(rows, 10, "rename-install"), 10, 82, live_librenms)

        module = Module.objects.get(device=device, module_type=module_type)
        renamed = Interface.objects.get(pk=renamed.pk)
        assert renamed.module_id == module.pk
        assert read_mapping(renamed).own_id("default") == 6702


class TestUpdateInterfaceAdoption:
    """The occupied-bay action adopts the raw template rows of a family and leaves its channels alone."""

    def test_only_the_template_row_of_a_breakout_family_is_adopted(self, live_librenms):
        from dcim.models import InterfaceTemplate, Module

        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device_with_module_bays("breakout-adopt", ["Slot 1"])
        seed_mapping(device, "default", own=83)
        module_type = make_module_type("BREAKOUT-ADOPT-OPTIC")
        module = Module.objects.create(
            device=device, module_bay=device.modulebays.get(name="Slot 1"), module_type=module_type, status="active"
        )
        # Added after the install, so the rows stay standalone.
        InterfaceTemplate.objects.create(module_type=module_type, name=PARENT, type="10gbase-x-sfpp")
        parent = make_interface(device, PARENT)
        channels = [make_interface(device, name) for name in CHANNELS]
        row = _item(20, module_type.model, "Slot 1")
        user = make_superuser()

        with _predicts({PARENT: [PARENT, *CHANNELS]}):
            seed_inventory(DeviceModuleTableView(), device, [row], librenms_id=83)
            content = render_module_tab(device, user=user)
            table_row = next(row for row in _table_rows(device) if row.get("ent_physical_index") == 20)
            _action, data = rendered_module_form(content, "update-module-interface", 20)
            view = UpdateModuleInterfaceView()
            view._librenms_api = live_librenms.api
            request = make_request("post", data, user=user, path="/modules/")
            response = view_post(view, request, pk=device.pk)

        assert table_row["adoptable_interface_count"] == 1
        assert response.status_code == 302
        parent.refresh_from_db()
        assert parent.module_id == module.pk
        assert [type(row).objects.get(pk=row.pk).module_id for row in channels] == [None] * 4
        assert any("adopted 1 existing standalone interface(s)" in text for text in message_texts(request, "success"))


class TestOneRender:
    """The Modules table predicts the names of an installed module once per render."""

    def test_the_bind_check_and_the_adoptable_count_share_one_prediction(self):
        from unittest.mock import patch

        from dcim.models import InterfaceTemplate, Module

        from netbox_librenms_plugin.views.base import modules_view
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = make_device_with_module_bays("adoptable-once", ["Slot 1"])
        seed_mapping(device, "default", own=84)
        module_type = make_module_type("ADOPTABLE-ONCE-OPTIC")
        module = Module.objects.create(
            device=device, module_bay=device.modulebays.get(name="Slot 1"), module_type=module_type, status="active"
        )
        # Added after the install, so the rows stay standalone.
        InterfaceTemplate.objects.create(module_type=module_type, name=PARENT, type="10gbase-x-sfpp")
        bound = make_interface(device, PARENT)
        seed_mapping(bound, "default", own=6704)
        row = _item(30, module_type.model, "Slot 1", _librenms_port_id=6704, _librenms_ifname=PARENT)
        seed_inventory(DeviceModuleTableView(), device, [row], librenms_id=84)
        real = modules_view.get_module_adoptable_interface_names

        with patch.object(modules_view, "get_module_adoptable_interface_names", wraps=real) as predicted:
            table_row = next(row for row in _table_rows(device) if row.get("ent_physical_index") == 30)

        assert table_row["can_update_interface_binding"]
        assert table_row["adoptable_interface_count"] == 1
        assert [call.args[1] for call in predicted.call_args_list] == [module]
