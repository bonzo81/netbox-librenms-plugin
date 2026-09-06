"""Real-ORM tests for module interface binding, adoption, and VC name normalization scopes."""

import pytest

from netbox_librenms_plugin.tests.cache_test_helpers import seed_inventory
from netbox_librenms_plugin.tests.conftest import (
    install_module,
    make_device,
    make_device_with_module_bays,
    make_interface,
    make_module_bay,
    make_module_type,
    make_superuser,
    make_virtual_chassis,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post as view_post

pytestmark = pytest.mark.django_db


def _module_with_interfaces(device, bay_name, model, names):
    """Install a module and give it interfaces named *names*."""
    from dcim.models import Interface

    module = install_module(device, bay_name, model)
    for name in names:
        Interface.objects.create(device=device, module=module, name=name, type="other")
    return module


# The device custom field and the seeded snapshot must carry the same id, or the cache reads stale.
ADOPTION_LIBRENMS_ID = 71
ADOPTION_ENT_INDEX = 710


def _adoption_inventory_row(model, name):
    """A cached row with no port identity, so only the template adoption can change NetBox."""
    return {
        "entPhysicalIndex": ADOPTION_ENT_INDEX,
        "entPhysicalModelName": model,
        "entPhysicalName": name,
        "entPhysicalDescr": name,
        "entPhysicalClass": "module",
        "entPhysicalContainedIn": 0,
        "entPhysicalSerialNum": "",
    }


class TestInterfacePortBinding:
    """Binding a LibreNMS port_id falls back through name, coordinates, and the lone interface."""

    def _bind(self, device, item, module_pk):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        return _bind_interface_librenms_id(device, item, module_pk, "default", Interface.objects.all())

    def test_coordinates_pick_the_module_interface_when_no_name_matches(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        device = make_device_with_module_bays("bind-coordinates", ["Slot 1"])
        module = _module_with_interfaces(device, "Slot 1", "BIND-COORD-CARD", ["Ethernet1/17", "Ethernet1/18"])
        item = {"_librenms_port_id": 8801, "_librenms_ifname": "port 1/17"}

        result = self._bind(device, item, module.pk)

        assert result["status"] == "bound"
        assert result["interface"] == "Ethernet1/17"
        bound = module.interfaces.get(name="Ethernet1/17")
        assert get_librenms_device_id(bound, "default", auto_save=False) == 8801

    def test_a_lone_module_interface_is_used_when_nothing_else_narrows_it(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        device = make_device_with_module_bays("bind-lone", ["Slot 1"])
        module = _module_with_interfaces(device, "Slot 1", "BIND-LONE-CARD", ["Uplink"])
        item = {"_librenms_port_id": 8802, "_librenms_ifdescr": "Unmatched Label"}

        result = self._bind(device, item, module.pk)

        assert result["status"] == "bound"
        assert result["interface"] == "Uplink"
        # The identity has to reach the database, not just the returned dict.
        assert get_librenms_device_id(module.interfaces.get(name="Uplink"), "default", auto_save=False) == 8802

    def test_no_module_context_and_no_name_match_reports_a_skip(self):
        device = make_device("bind-nothing")
        make_interface(device, "Ethernet1")
        item = {"_librenms_port_id": 8803, "_librenms_ifname": "Unmatched Label"}

        result = self._bind(device, item, None)

        assert result == {
            "status": "skipped",
            "reason": "no matching interface found for port_id 8803",
        }


class TestRecordBindOutcome:
    """A failed bind is reported in the install summary without claiming a change."""

    def test_a_non_bound_outcome_is_recorded_as_skipped(self):
        from netbox_librenms_plugin.views.sync.modules import _record_bind_outcome

        skipped = []
        changed = _record_bind_outcome(
            {"status": "conflict", "reason": "port_id 5 already assigned"},
            {"name": "CARD → Slot 1"},
            skipped,
        )

        assert changed is False
        assert skipped == ["CARD → Slot 1: port_id 5 already assigned"]

    def test_a_bound_outcome_reports_whether_netbox_changed(self):
        from netbox_librenms_plugin.views.sync.modules import _record_bind_outcome

        skipped = []
        assert _record_bind_outcome({"status": "bound", "changed": True}, {"name": "row"}, skipped) is True
        assert _record_bind_outcome({"status": "bound", "changed": False}, {"name": "row"}, skipped) is False
        assert skipped == []


class TestVCNameNormalizationScopes:
    """Interfaces outside the caller's change or delete grant are skipped, never rewritten."""

    def _member_with_module(self, tag, interface_name="Te1/1/1"):
        from dcim.models import Interface

        first = make_device(f"{tag}-first")
        device = make_device(f"{tag}-second")
        make_virtual_chassis(f"{tag}-vc", first, device)
        bay = make_module_bay(device, f"{tag} Bay")
        module = install_module(device, bay.name, f"{tag.upper()}-CARD")
        interface = Interface.objects.create(device=device, module=module, name=interface_name, type="other")
        return device, module, interface

    def _normalize(self, device, module, changeable, deletable):
        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        return _normalize_module_interface_names_for_vc_member(device, module, changeable, deletable)

    def test_an_interface_outside_the_change_grant_is_skipped(self):
        from dcim.models import Interface

        device, module, interface = self._member_with_module("vcscope-nochange")

        result = self._normalize(device, module, Interface.objects.exclude(pk=interface.pk), Interface.objects.all())

        interface.refresh_from_db()
        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 1}
        assert interface.name == "Te1/1/1"

    def test_a_name_that_does_not_carry_a_member_position_is_left_alone(self):
        from dcim.models import Interface

        device, module, interface = self._member_with_module("vcscope-norewrite", interface_name="xe-0/0/0")

        result = self._normalize(device, module, Interface.objects.all(), Interface.objects.all())

        interface.refresh_from_db()
        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 0}
        assert interface.name == "xe-0/0/0"

    def test_a_conflict_outside_the_change_grant_is_skipped(self):
        from dcim.models import Interface

        device, module, interface = self._member_with_module("vcscope-conflict-nochange")
        conflict = make_interface(device, "Te2/1/1")

        result = self._normalize(device, module, Interface.objects.exclude(pk=conflict.pk), Interface.objects.all())

        conflict.refresh_from_db()
        interface.refresh_from_db()
        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 1}
        assert conflict.module_id is None
        assert interface.name == "Te1/1/1"

    def test_an_undeletable_generated_interface_blocks_the_adoption(self):
        from dcim.models import Interface

        device, module, interface = self._member_with_module("vcscope-nodelete")
        conflict = make_interface(device, "Te2/1/1")

        result = self._normalize(device, module, Interface.objects.all(), Interface.objects.exclude(pk=interface.pk))

        conflict.refresh_from_db()
        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 1}
        assert conflict.module_id is None
        assert Interface.objects.filter(pk=interface.pk).exists()


class TestUpdateModuleInterfaceAdoption:
    """The update action adopts standalone template interfaces when the cached row binds nothing."""

    def _module_with_templates(self, tag, template_names):
        from dcim.models import InterfaceTemplate, Module

        device = make_device(f"{tag}-device", librenms_cf={"default": ADOPTION_LIBRENMS_ID})
        bay = make_module_bay(device, f"{tag} Bay")
        module_type = make_module_type(f"{tag.upper()}-CARD")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, status="active")
        # Templates are added after the install so the interfaces stay standalone.
        for name in template_names:
            InterfaceTemplate.objects.create(module_type=module_type, name=name, type="other")
        return device, module

    def _seed(self, view, device, module):
        seed_inventory(
            view,
            device,
            [_adoption_inventory_row(module.module_type.model, module.module_bay.name)],
            librenms_id=ADOPTION_LIBRENMS_ID,
        )

    def _post(self, view_class, device, data, live_librenms, module=None):
        request = make_request("post", data, user=make_superuser(), path="/modules/")
        view = view_class()
        view._librenms_api = live_librenms.api
        if module is not None:
            self._seed(view, device, module)
        return view, request, view_post(view, request, pk=device.pk)

    def test_a_missing_module_id_reports_an_error(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, _module = self._module_with_templates("adopt-bad-id", ["Ethernet1/1"])

        _view, request, response = self._post(
            UpdateModuleInterfaceView, device, {"server_key": "default"}, live_librenms
        )

        assert response.status_code == 302
        assert "Missing or invalid module ID." in message_texts(request, "error")

    def test_standalone_template_interfaces_are_adopted(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module = self._module_with_templates("adopt-plain", ["Ethernet1/1", "Ethernet1/2"])
        first = make_interface(device, "Ethernet1/1")
        second = make_interface(device, "Ethernet1/2")
        untouched = make_interface(device, "Management1")

        _view, request, response = self._post(
            UpdateModuleInterfaceView,
            device,
            {"module_id": str(module.pk), "ent_index": str(ADOPTION_ENT_INDEX), "server_key": "default"},
            live_librenms,
            module=module,
        )

        first.refresh_from_db()
        second.refresh_from_db()
        untouched.refresh_from_db()
        assert response.status_code == 302
        assert first.module_id == module.pk
        assert second.module_id == module.pk
        assert untouched.module_id is None
        assert any("adopted 2 existing standalone interface(s)" in text for text in message_texts(request, "success"))

    def test_a_module_type_with_no_matching_standalone_interface_reports_the_reason(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module = self._module_with_templates("adopt-nothing", ["Ethernet1/1"])

        _view, request, response = self._post(
            UpdateModuleInterfaceView,
            device,
            {"module_id": str(module.pk), "ent_index": str(ADOPTION_ENT_INDEX), "server_key": "default"},
            live_librenms,
            module=module,
        )

        assert response.status_code == 302
        assert any("no matching standalone interfaces found" in text for text in message_texts(request, "warning"))

    def test_an_interface_outside_the_change_grant_is_not_adopted(self, live_librenms):
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module = self._module_with_templates("adopt-scoped", ["Ethernet1/1"])
        blocked = make_interface(device, "Ethernet1/1")
        user = make_user_with_perms("adopt-scoped-user", [("view", Device), ("view", Module)])
        user = grant(user, "change", Interface, constraints={"name": "Management1"})
        request = make_request(
            "post",
            {"module_id": str(module.pk), "ent_index": str(ADOPTION_ENT_INDEX), "server_key": "default"},
            user=user,
            path="/modules/",
        )
        view = UpdateModuleInterfaceView()
        view._librenms_api = live_librenms.api
        self._seed(view, device, module)

        response = view_post(view, request, pk=device.pk)

        blocked.refresh_from_db()
        assert response.status_code == 302
        assert blocked.module_id is None
        assert any("no matching standalone interfaces found" in text for text in message_texts(request, "warning"))
