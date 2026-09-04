"""End-to-end tests for the module install actions and their inventory ignore rules."""

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
)
from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post as view_post

pytestmark = pytest.mark.django_db


def _item(index, model, name, *, parent=0, phys_class="module", **extra):
    row = {
        "entPhysicalIndex": index,
        "entPhysicalModelName": model,
        "entPhysicalName": name,
        "entPhysicalDescr": name,
        "entPhysicalClass": phys_class,
        "entPhysicalContainedIn": parent,
        "entPhysicalSerialNum": "",
    }
    row.update(extra)
    return row


def _drive(view_class, device, data, live_librenms):
    """POST into a bound view with a real request and the loopback LibreNMS client."""
    request = make_request("post", data, user=make_superuser(), path="/modules/")
    view = view_class()
    view._librenms_api = live_librenms.api
    return view, request, view_post(view, request, pk=device.pk)


class TestInstallModuleView:
    """The single-row install reports its identity fallback and the binding it made."""

    def test_a_non_numeric_bay_or_type_is_rejected(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("install-bad-ids", librenms_cf={"default": 61})

        _view, request, response = _drive(
            InstallModuleView,
            device,
            {"module_bay_id": "not-a-pk", "module_type_id": "1", "server_key": "default"},
            live_librenms,
        )

        assert response.status_code == 302
        assert "Missing or invalid module bay/module type ID." in message_texts(request, "error")
        assert not Module.objects.filter(device=device).exists()

    def test_posted_row_metadata_binds_the_interface_and_is_flagged_as_a_fallback(self, live_librenms):
        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("install-post-fallback", librenms_cf={"default": 62})
        bay = make_module_bay(device, "Fallback Bay")
        module_type = make_module_type("FALLBACK-CARD")
        interface = make_interface(device, "Te1/1/1")

        _view, request, response = _drive(
            InstallModuleView,
            device,
            {
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "serial": "-",
                "server_key": "default",
                "librenms_port_id": "6201",
                "librenms_ifname": interface.name,
            },
            live_librenms,
        )

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.module.module_type == module_type
        assert get_librenms_device_id(interface, "default", auto_save=False) == 6201
        assert any("serial: N/A" in text for text in message_texts(request, "success"))
        assert any("fallback used posted row metadata" in text for text in message_texts(request, "warning"))
        assert any("Bound Te1/1/1 to LibreNMS port_id 6201" in text for text in message_texts(request, "info"))


class TestInstallBranchView:
    """The branch install validates its parent index, needs a snapshot, and reports each row."""

    def test_a_non_numeric_parent_index_is_rejected(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("branch-bad-index", librenms_cf={"default": 63})

        _view, request, response = _drive(
            InstallBranchView,
            device,
            {"parent_index": "not-a-number", "server_key": "default"},
            live_librenms,
        )

        assert response.status_code == 302
        assert "Invalid parent inventory index." in message_texts(request, "error")

    def test_without_a_snapshot_the_user_is_told_to_refresh(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("branch-no-cache", librenms_cf={"default": 64})

        _view, request, response = _drive(
            InstallBranchView,
            device,
            {"parent_index": "10", "server_key": "default"},
            live_librenms,
        )

        assert response.status_code == 302
        assert "No cached inventory data. Please refresh modules first." in message_texts(request, "error")

    def test_a_branch_install_creates_the_module_and_binds_its_port(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device_with_module_bays("branch-install", ["Slot 1"])
        device.custom_field_data["librenms_id"] = {"default": 65}
        device.save(update_fields=["custom_field_data"])
        module_type = make_module_type("BRANCH-CARD")
        interface = make_interface(device, "Te1/1/1")
        rows = [
            _item(
                10,
                module_type.model,
                "Slot 1",
                _librenms_port_id=6501,
                _librenms_ifname=interface.name,
            )
        ]

        request = make_request(
            "post",
            {"parent_index": "10", "server_key": "default"},
            user=make_superuser(),
            path="/modules/",
        )
        view = InstallBranchView()
        view._librenms_api = live_librenms.api
        seed_inventory(view, device, rows, librenms_id=65)
        response = view_post(view, request, pk=device.pk)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert Module.objects.filter(device=device, module_type=module_type).exists()
        assert get_librenms_device_id(interface, "default", auto_save=False) == 6501
        assert any("Installed" in text for text in message_texts(request, "success"))

    def test_a_bind_failure_is_reported_next_to_the_install(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device_with_module_bays("branch-bind-miss", ["Slot 1"])
        device.custom_field_data["librenms_id"] = {"default": 66}
        device.save(update_fields=["custom_field_data"])
        module_type = make_module_type("BRANCH-MISS-CARD")
        rows = [_item(10, module_type.model, "Slot 1", _librenms_port_id=6601, _librenms_ifname="No Such Interface")]

        request = make_request(
            "post",
            {"parent_index": "10", "server_key": "default"},
            user=make_superuser(),
            path="/modules/",
        )
        view = InstallBranchView()
        view._librenms_api = live_librenms.api
        seed_inventory(view, device, rows, librenms_id=66)
        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        assert Module.objects.filter(device=device, module_type=module_type).exists()
        assert any("no matching interface found for port_id 6601" in text for text in message_texts(request))


class TestInstallSelectedView:
    """A tampered selection list is refused rather than partially installed."""

    def test_a_non_numeric_selection_is_rejected(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        device = make_device("selected-bad-index", librenms_cf={"default": 67})
        request = make_request(
            "post",
            {"select": ["not-a-number"], "server_key": "default"},
            user=make_superuser(),
            path="/modules/",
        )
        view = InstallSelectedView()
        view._librenms_api = live_librenms.api
        seed_inventory(view, device, [_item(10, "ANY-CARD", "Slot 1")], librenms_id=67)

        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        assert "Invalid selection." in message_texts(request, "error")
        assert not Module.objects.filter(device=device).exists()


class TestUpdateModuleSerialView:
    """The serial update validates its inputs before touching the module."""

    def test_an_invalid_selected_device_and_module_id_are_both_reported(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device("serial-bad-inputs", librenms_cf={"default": 68})

        _view, request, response = _drive(
            UpdateModuleSerialView,
            device,
            {"module_id": "", "selected_device_id": "not-a-pk", "server_key": "default"},
            live_librenms,
        )

        assert response.status_code == 302
        assert any("falling back to the page device" in text for text in message_texts(request, "warning"))
        assert "Missing or invalid module ID." in message_texts(request, "error")

    def test_a_placeholder_serial_is_stored_as_blank(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device_with_module_bays("serial-placeholder", ["Slot 1"])
        module = install_module(device, "Slot 1", "SERIAL-PLACEHOLDER-CARD", serial="OLD")

        _view, request, response = _drive(
            UpdateModuleSerialView,
            device,
            {"module_id": str(module.pk), "serial": "N/A", "server_key": "default"},
            live_librenms,
        )

        module.refresh_from_db()
        assert response.status_code == 302
        assert module.serial == ""


class TestInventoryIgnoreRulesDuringCollection:
    """Branch collection honours the same skip/transparent rules the table renders with."""

    @staticmethod
    def _rule(name, pattern, action):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        return InventoryIgnoreRule.objects.create(
            name=name,
            match_type=InventoryIgnoreRule.MATCH_CONTAINS,
            pattern=pattern,
            action=action,
            require_serial_match_parent=False,
        )

    def _collect(self, parent_index, rows, rules):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        index_map = {row["entPhysicalIndex"]: row for row in rows}
        return InstallBranchView()._collect_branch(parent_index, rows, rules, "", index_map)

    def test_a_skip_rule_drops_the_item_and_its_subtree(self):
        rows = [
            _item(1, "PARENT", "Slot 1"),
            _item(2, "SKIP-ME", "FT-IDPROM", parent=1),
            _item(3, "DEEP-SKIP", "FT-IDPROM-child", parent=2),
            _item(4, "KEEP-ME", "NormalChild", parent=1),
        ]
        rules = [self._rule("idprom-skip", "IDPROM", "skip")]

        collected = self._collect(1, rows, rules)

        assert [row["entPhysicalModelName"] for row in collected] == ["PARENT", "KEEP-ME"]

    def test_a_transparent_rule_promotes_the_children(self):
        rows = [
            _item(1, "PARENT", "Slot 1"),
            _item(2, "TRANSPARENT", "T-IDPROM", parent=1),
            _item(3, "GRANDCHILD", "Optic 1", parent=2),
        ]
        rules = [self._rule("idprom-transparent", "IDPROM", "transparent")]

        collected = self._collect(1, rows, rules)

        assert [row["entPhysicalModelName"] for row in collected] == ["PARENT", "GRANDCHILD"]

    def test_a_transparent_root_is_replaced_by_its_children(self):
        rows = [
            _item(1, "ROOT", "RP-IDPROM"),
            _item(2, "CHILD", "Optic 1", parent=1),
        ]
        rules = [self._rule("root-transparent", "IDPROM", "transparent")]

        collected = self._collect(1, rows, rules)

        assert [row["entPhysicalModelName"] for row in collected] == ["CHILD"]

    def test_a_skipped_root_collects_nothing(self):
        rows = [
            _item(1, "ROOT", "RP-IDPROM"),
            _item(2, "CHILD", "Optic 1", parent=1),
        ]
        rules = [self._rule("root-skip", "IDPROM", "skip")]

        assert self._collect(1, rows, rules) == []
