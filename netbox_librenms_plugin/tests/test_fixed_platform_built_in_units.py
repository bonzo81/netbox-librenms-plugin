"""
Built-in units of a fixed platform that takes transceivers but no modules (Nokia 7250 IXR-X).

The system board reports itself as "Slot 1" (ioModule), "MDA 1/1" (mdaModule, below Slot 1) and "Slot A"
(cpmModule), all with the chassis part number and serial. The transceiver in port 1/1/c1 sits below MDA 1/1.
"""

import pytest

pytestmark = pytest.mark.django_db

SERVER_KEY = "default"
PART_NUMBER = "3HE15789AARF01"
OPTIC_MODEL = "QSFP-DD-400G-DR4"
CHASSIS, SLOT_1, MDA, OPTIC, SLOT_A = 1, 50, 60, 70, 80
BUILT_IN_UNITS = (SLOT_1, MDA, SLOT_A)


def ixr_inventory():
    """Return the ENTITY-MIB rows LibreNMS reports for the system board and one transceiver."""
    rows = [
        (CHASSIS, 0, "chassis", "Chassis", PART_NUMBER, "NS230961125"),
        (SLOT_1, CHASSIS, "ioModule", "Slot 1", PART_NUMBER, "NS230961125"),
        (MDA, SLOT_1, "mdaModule", "MDA 1/1", PART_NUMBER, "NS230961125"),
        (OPTIC, MDA, "port", "1/1/c1", OPTIC_MODEL, "OPTIC-SN-1"),
        (SLOT_A, CHASSIS, "cpmModule", "Slot A", PART_NUMBER, "NS230961125"),
    ]
    return [
        {
            "entPhysicalIndex": index,
            "entPhysicalContainedIn": parent,
            "entPhysicalClass": phys_class,
            "entPhysicalName": name,
            "entPhysicalDescr": name,
            "entPhysicalModelName": model,
            "entPhysicalSerialNum": serial,
            "entPhysicalParentRelPos": 1,
        }
        for index, parent, phys_class, name, model, serial in rows
    ]


def ixr_device(name, *, part_number=PART_NUMBER):
    """Create a 7250 IXR-X with a device-level bay for port 1/1/c1 and cache its inventory snapshot."""
    from django.core.cache import cache

    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type_with_bays
    from netbox_librenms_plugin.tests.view_test_helpers import trusted_module_inventory_payload
    from netbox_librenms_plugin.views.mixins import CacheMixin

    device = make_device_with_module_bays(name, ["1/1/c1"])
    device.device_type.part_number = part_number
    device.device_type.save()
    make_module_type_with_bays(OPTIC_MODEL, manufacturer=device.device_type.manufacturer)
    payload = trusted_module_inventory_payload(device, ixr_inventory(), server_key=SERVER_KEY)
    cache.set(CacheMixin().get_cache_key(device, "inventory", server_key=SERVER_KEY), payload)
    return device


def model_rule(**fields):
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return InventoryIgnoreRule.objects.create(
        name="Fixed-platform built-in units",
        match_type="model_matches_device_type",
        pattern="",
        action="transparent",
        require_serial_match_parent=False,
        **fields,
    )


def tab_rows(device, *, user=None):
    """Build the Modules tab from its cached snapshot through the real view and return its rows by index."""
    from django.urls import reverse

    from netbox_librenms_plugin.tests.view_test_helpers import make_request
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    request = make_request(
        "get",
        {"tab": "modules", "server_key": SERVER_KEY},
        user=user,
        path=reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": device.pk}),
    )
    view = DeviceModuleTableView()
    view.setup(request, pk=device.pk)
    view.cache_only = True
    context = view.get_context_data(request, device, server_key=SERVER_KEY)
    return {row["ent_physical_index"]: row for row in context["table"].data}


class TestNestedBuiltInUnits:
    """The model rule looks through ancestors that are built-in units themselves."""

    def test_the_model_rule_hides_every_built_in_unit_and_lifts_the_transceiver(self):
        device = ixr_device("ixr-nested")
        model_rule()

        rows = tab_rows(device)

        assert set(rows) == {OPTIC}
        assert (rows[OPTIC]["depth"], rows[OPTIC]["module_bay"]) == (0, "1/1/c1")

    def test_without_the_part_number_the_built_in_units_stay_rows(self):
        device = ixr_device("ixr-no-part-number", part_number="")
        model_rule()

        rows = tab_rows(device)

        assert {SLOT_1, SLOT_A, OPTIC} <= set(rows)
        assert rows[OPTIC]["depth"] > 0

    def test_a_unit_below_a_real_module_is_not_matched(self):
        from netbox_librenms_plugin.views.base.modules_view import DeviceIdentity, _check_ignore_rules

        rule = model_rule()
        inventory = {item["entPhysicalIndex"]: item for item in ixr_inventory()}
        # A line card of its own model holds the unit, so the unit is a real FRU on that card.
        inventory[SLOT_1] = {**inventory[SLOT_1], "entPhysicalModelName": "3HE16474AA"}
        identity = DeviceIdentity(type_models=frozenset({PART_NUMBER.casefold()}))

        assert _check_ignore_rules(inventory[MDA], inventory[SLOT_1], [rule], inventory, identity) is None
        assert _check_ignore_rules(inventory[SLOT_1], inventory[CHASSIS], [rule], inventory, identity) is None

    def test_a_built_in_chain_must_end_at_the_chassis(self):
        from netbox_librenms_plugin.views.base.modules_view import DeviceIdentity, _check_ignore_rules

        rule = model_rule()
        inventory = {item["entPhysicalIndex"]: item for item in ixr_inventory()}
        inventory[SLOT_1] = {**inventory[SLOT_1], "entPhysicalContainedIn": 999}
        identity = DeviceIdentity(type_models=frozenset({PART_NUMBER.casefold()}))

        assert _check_ignore_rules(inventory[MDA], inventory[SLOT_1], [rule], inventory, identity) is None

    def test_the_serial_rule_still_takes_direct_chassis_children_only(self):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        device = ixr_device("ixr-serial-rule", part_number="")
        device.serial = "NS230961125"
        device.save()
        InventoryIgnoreRule.objects.create(
            name="Embedded board", match_type="serial_matches_device", pattern="", action="transparent"
        )

        rows = tab_rows(device)

        assert SLOT_1 not in rows
        assert SLOT_A not in rows
        assert MDA in rows

    def test_the_lifted_transceiver_installs_into_the_device_bay(self):
        from dcim.models import Module

        from netbox_librenms_plugin.tests.view_test_helpers import (
            make_request,
            post,
            render_module_tab,
            rendered_module_form,
        )
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = ixr_device("ixr-install")
        model_rule()
        _action, fields = rendered_module_form(render_module_tab(device), "install-module", OPTIC)

        post(InstallModuleView(), make_request(data=fields), pk=device.pk)

        module = Module.objects.get(device=device, serial="OPTIC-SN-1")
        assert (module.module_bay.name, module.module_bay.module_id) == ("1/1/c1", None)
