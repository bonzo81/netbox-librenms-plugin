"""Real-ORM tests for the bulk installer's parent, bay, and mapping resolution."""

import pytest

from netbox_librenms_plugin.tests.conftest import (
    install_module,
    make_device,
    make_device_with_module_bays,
    make_module_bay,
    make_module_type,
)

pytestmark = pytest.mark.django_db


def _item(index, model, name, *, parent=0, descr=None, phys_class="module", serial="", **extra):
    row = {
        "entPhysicalIndex": index,
        "entPhysicalModelName": model,
        "entPhysicalName": name,
        "entPhysicalDescr": name if descr is None else descr,
        "entPhysicalClass": phys_class,
        "entPhysicalContainedIn": parent,
        "entPhysicalSerialNum": serial,
    }
    row.update(extra)
    return row


def _bays(device):
    from dcim.models import ModuleBay

    return list(ModuleBay.objects.filter(device=device).select_related("installed_module"))


def _find_parent(item, index_map, bays, exact=(), regex=()):
    from netbox_librenms_plugin.views.sync.modules import InstallBranchView

    return InstallBranchView._find_parent_module_id(item, index_map, bays, list(exact), list(regex))


def _mapping(**kwargs):
    from netbox_librenms_plugin.models import ModuleBayMapping

    return ModuleBayMapping.objects.create(**kwargs)


class TestFindParentModuleId:
    """The ancestor walk resolves an inventory row to the module that really holds it."""

    def test_a_parent_named_like_an_occupied_bay_resolves_to_that_module(self):
        device = make_device_with_module_bays("parent-direct", ["Slot 1"])
        module = install_module(device, "Slot 1", "PARENT-DIRECT-CARD")
        index_map = {10: _item(10, "", "Slot 1", phys_class="container")}
        child = _item(20, "SFP-X", "Transceiver 1", parent=10)

        assert _find_parent(child, index_map, _bays(device)) == module.pk

    def test_the_bay_name_may_match_the_parent_description(self):
        device = make_device_with_module_bays("parent-descr", ["Slot 2"])
        module = install_module(device, "Slot 2", "PARENT-DESCR-CARD")
        index_map = {10: _item(10, "", "", descr="Slot 2", phys_class="container")}
        child = _item(20, "SFP-X", "Transceiver 1", parent=10)

        assert _find_parent(child, index_map, _bays(device)) == module.pk

    def test_the_walk_continues_past_an_unmatched_container_to_the_grandparent(self):
        device = make_device_with_module_bays("parent-grandparent", ["Slot 3"])
        module = install_module(device, "Slot 3", "PARENT-GRANDPARENT-CARD")
        index_map = {
            10: _item(10, "", "Slot 3", phys_class="container"),
            20: _item(20, "", "Cage", parent=10, phys_class="container"),
        }
        child = _item(30, "SFP-X", "Transceiver 1", parent=20)

        assert _find_parent(child, index_map, _bays(device)) == module.pk

    def test_a_hierarchy_cycle_stops_instead_of_looping(self):
        device = make_device_with_module_bays("parent-cycle", ["Slot 4"])
        install_module(device, "Slot 4", "PARENT-CYCLE-CARD")
        index_map = {
            10: _item(10, "", "Cage A", parent=20, phys_class="container"),
            20: _item(20, "", "Cage B", parent=10, phys_class="container"),
        }
        child = _item(30, "SFP-X", "Transceiver 1", parent=10)

        assert _find_parent(child, index_map, _bays(device)) is None

    def test_an_exact_mapping_translates_the_ancestor_name_to_the_bay(self):
        device = make_device_with_module_bays("parent-exact-map", ["Slot 3"])
        module = install_module(device, "Slot 3", "PARENT-EXACT-CARD")
        # A class-scoped row must beat the vendor-agnostic one for the same name.
        wrong = _mapping(librenms_name="Rack 0-Slot 3", librenms_class="", netbox_bay_name="Nowhere")
        right = _mapping(librenms_name="Rack 0-Slot 3", librenms_class="container", netbox_bay_name="Slot 3")
        index_map = {10: _item(10, "", "Rack 0-Slot 3", phys_class="container")}
        child = _item(20, "SFP-X", "Transceiver 1", parent=10)

        assert _find_parent(child, index_map, _bays(device), exact=[right, wrong]) == module.pk

    def test_an_exact_mapping_falls_back_to_the_class_agnostic_row(self):
        device = make_device_with_module_bays("parent-exact-fallback", ["Slot 5"])
        module = install_module(device, "Slot 5", "PARENT-EXACT-FALLBACK-CARD")
        mapping = _mapping(librenms_name="Rack 0-Slot 5", librenms_class="", netbox_bay_name="Slot 5")
        index_map = {10: _item(10, "", "Rack 0-Slot 5", phys_class="container")}
        child = _item(20, "SFP-X", "Transceiver 1", parent=10)

        assert _find_parent(child, index_map, _bays(device), exact=[mapping]) == module.pk

    def test_a_regex_mapping_expands_the_ancestor_name_to_the_bay(self):
        device = make_device_with_module_bays("parent-regex-map", ["Slot 3"])
        module = install_module(device, "Slot 3", "PARENT-REGEX-CARD")
        # An exact row in the regex list has no compiled pattern and must be stepped over.
        exact_row = _mapping(librenms_name="Slot 3/0", librenms_class="", netbox_bay_name="Nowhere")
        class_scoped_miss = _mapping(
            librenms_name=r"Fan (\d+)",
            librenms_class="container",
            netbox_bay_name=r"Fan \1",
            is_regex=True,
        )
        classless_hit = _mapping(
            librenms_name=r"Slot (\d+)/0",
            librenms_class="",
            netbox_bay_name=r"Slot \1",
            is_regex=True,
        )
        index_map = {10: _item(10, "", "Slot 3/0", phys_class="container")}
        child = _item(20, "SFP-X", "Transceiver 1", parent=10)

        resolved = _find_parent(
            child,
            index_map,
            _bays(device),
            regex=[exact_row, class_scoped_miss, classless_hit],
        )

        assert resolved == module.pk

    def test_a_regex_row_whose_replacement_cannot_expand_is_stepped_over(self):
        from netbox_librenms_plugin.models import ModuleBayMapping

        device = make_device_with_module_bays("parent-regex-badref", ["Slot 6"])
        module = install_module(device, "Slot 6", "PARENT-REGEX-BADREF-CARD")
        # A row from before replacement validation existed: saved without full_clean.
        ModuleBayMapping.objects.bulk_create(
            [ModuleBayMapping(librenms_name=r"Slot (\d+)/\d+", netbox_bay_name=r"Slot \2", is_regex=True)]
        )
        broken = ModuleBayMapping.objects.get(netbox_bay_name=r"Slot \2")
        good = _mapping(librenms_name=r"Slot (\d+)/0", librenms_class="", netbox_bay_name=r"Slot \1", is_regex=True)
        index_map = {10: _item(10, "", "Slot 6/0", phys_class="container")}
        child = _item(20, "SFP-X", "Transceiver 1", parent=10)

        assert _find_parent(child, index_map, _bays(device), regex=[broken, good]) == module.pk

    def test_an_unknown_ancestor_resolves_nothing(self):
        device = make_device_with_module_bays("parent-unknown", ["Slot 7"])
        install_module(device, "Slot 7", "PARENT-UNKNOWN-CARD")
        index_map = {10: _item(10, "", "Chassis", phys_class="chassis")}
        child = _item(20, "SFP-X", "Transceiver 1", parent=10)

        assert _find_parent(child, index_map, _bays(device)) is None


class TestCandidateBaysForItem:
    """Bulk install matches against the same bay set the table renders, minus ambiguity."""

    def _device_with_two_line_cards(self, tag):
        device = make_device_with_module_bays(tag, ["Slot 1", "Slot 2"])
        first = install_module(device, "Slot 1", f"{tag}-CARD", child_bays=("Transceiver 1",))
        second = install_module(device, "Slot 2", f"{tag}-CARD", child_bays=("Transceiver 1",))
        return device, first, second

    def test_a_resolved_parent_scopes_the_names_to_that_module(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device, first, second = self._device_with_two_line_cards("cand-scoped")

        scoped = InstallBranchView._candidate_bays_for_item(_bays(device), first.pk)

        assert set(scoped) == {"Transceiver 1"}
        assert scoped["Transceiver 1"].module_id == first.pk
        assert second.pk != first.pk

    def test_without_a_parent_a_uniquely_named_module_bay_is_still_matchable(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device_with_module_bays("cand-unique", ["Slot 1"])
        module = install_module(device, "Slot 1", "CAND-UNIQUE-CARD", child_bays=("Transceiver 0/19",))

        combined = InstallBranchView._candidate_bays_for_item(_bays(device), None)

        assert combined["Transceiver 0/19"].module_id == module.pk
        assert combined["Slot 1"].module_id is None

    def test_a_name_owned_by_two_modules_is_dropped_rather_than_guessed(self, caplog):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device, _first, _second = self._device_with_two_line_cards("cand-ambiguous")

        with caplog.at_level("INFO", logger="netbox_librenms_plugin.views.sync.modules"):
            combined = InstallBranchView._candidate_bays_for_item(_bays(device), None)

        assert "Transceiver 1" not in combined
        assert set(combined) == {"Slot 1", "Slot 2"}
        assert "dropping ambiguous module-scoped bay name" in caplog.text

    def test_a_device_level_bay_of_the_same_name_wins_instead_of_being_dropped(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device, _first, _second = self._device_with_two_line_cards("cand-rescued")
        device_bay = make_module_bay(device, "Transceiver 1")

        combined = InstallBranchView._candidate_bays_for_item(_bays(device), None)

        assert combined["Transceiver 1"].pk == device_bay.pk


class TestMatchBay:
    """Bay matching falls through mappings, direct names, and finally position."""

    def test_a_regex_mapping_resolves_a_vendor_optics_label(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("match-bay-regex")
        bay = make_module_bay(device, "Optics0/0/0/5")
        mapping = _mapping(
            librenms_name=r"Optics (\d+/\d+/\d+/\d+)",
            librenms_class="",
            netbox_bay_name=r"Optics\1",
            is_regex=True,
        )
        item = _item(30, "SFP-X", "Optics 0/0/0/5", phys_class="")

        matched = InstallBranchView._match_bay(item, {30: item}, {bay.name: bay}, [], [mapping])

        assert matched == bay

    @pytest.mark.parametrize(
        ("container_index", "expected_bay"),
        [(10, "SFP 1"), (11, "SFP 2")],
    )
    def test_an_unnamed_container_slot_is_matched_by_its_position(self, container_index, expected_bay):
        """A transceiver in an unnamed cage resolves by which cage slot it sits in."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device(f"match-bay-position-{container_index}")
        bays = {name: make_module_bay(device, name) for name in ("SFP 1", "SFP 2")}
        rows = [
            _item(1, "LC-CARD", "Slot 1", phys_class="module"),
            _item(10, "", "Cage A", parent=1, phys_class="container", entPhysicalParentRelPos=1),
            _item(11, "", "Cage B", parent=1, phys_class="container", entPhysicalParentRelPos=2),
            _item(30, "SFP-X", "Transceiver", parent=container_index, phys_class="module"),
        ]
        index_map = {row["entPhysicalIndex"]: row for row in rows}

        matched = InstallBranchView._match_bay(index_map[30], index_map, dict(bays), [], [])

        assert matched == bays[expected_bay]


class TestInstallSingleResolutionPaths:
    """The shared installer's mapping load, bay miss, and adoption reporting."""

    @staticmethod
    def _install(device, item, index_map=None, **overrides):
        from dcim.models import Interface, ModuleBay

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView, _module_component_specs

        module_types = {mt.model: mt for mt in overrides.pop("module_types", [])}
        kwargs = {
            "module_bays": ModuleBay.objects.all(),
            "allowed_module_type_ids": {mt.pk for mt in module_types.values()},
            "changeable_components": {model: model.objects.all() for _, _, model in _module_component_specs()},
            "changeable_interfaces": Interface.objects.all(),
            "deletable_interfaces": Interface.objects.all(),
            "manufacturer_id": device.device_type.manufacturer_id,
        }
        kwargs.update(overrides)
        return InstallBranchView._install_single(
            device,
            item,
            index_map if index_map is not None else {item["entPhysicalIndex"]: item},
            module_types,
            **kwargs,
        )

    def test_omitted_mappings_are_loaded_from_the_database(self):
        device = make_device_with_module_bays("install-loads-mappings", ["PS1"])
        module_type = make_module_type("INSTALL-LOAD-PSU")
        _mapping(librenms_name="Power Supply 1", librenms_class="powerSupply", netbox_bay_name="PS1")
        item = _item(40, module_type.model, "Power Supply 1", phys_class="powerSupply")

        result = self._install(device, item, module_types=[module_type])

        assert result["status"] == "installed"
        assert result["name"] == f"{module_type.model} → PS1"

    def test_an_unmatched_bay_skips_without_installing(self):
        from dcim.models import Module

        device = make_device_with_module_bays("install-no-bay", ["Slot 1"])
        module_type = make_module_type("INSTALL-NO-BAY-CARD")
        item = _item(41, module_type.model, "Totally Unrelated", phys_class="module")

        result = self._install(device, item, module_types=[module_type], exact_mappings=[], regex_mappings=[])

        assert result == {"status": "skipped", "name": "Totally Unrelated", "reason": "no matching bay"}
        assert not Module.objects.filter(device=device).exists()

    def test_the_installed_name_reports_adopted_components(self):
        from dcim.models import Interface, InterfaceTemplate, ModuleBayTemplate

        device = make_device_with_module_bays("install-adopts", ["Slot 1"])
        module_type = make_module_type("INSTALL-ADOPT-CARD")
        InterfaceTemplate.objects.create(module_type=module_type, name="Te1/1/1", type="10gbase-x-sfpp")
        InterfaceTemplate.objects.create(module_type=module_type, name="Te1/1/2", type="10gbase-x-sfpp")
        ModuleBayTemplate.objects.create(module_type=module_type, name="Nested Bay")
        adopted = Interface.objects.create(device=device, name="Te1/1/1", type="10gbase-x-sfpp")
        item = _item(42, module_type.model, "Slot 1", phys_class="module")

        result = self._install(device, item, module_types=[module_type], exact_mappings=[], regex_mappings=[])

        adopted.refresh_from_db()
        assert result["status"] == "installed"
        assert result["adopted_components"] == 1
        assert result["name"].endswith("(adopted 1 existing component(s))")
        assert adopted.module_id == result["module_pk"]
        # The unmatched template still had to be created fresh, not adopted.
        assert Interface.objects.get(device=device, name="Te1/1/2").module_id == result["module_pk"]

    def test_a_component_outside_the_change_scope_blocks_the_install(self):
        from dcim.models import Interface, InterfaceTemplate, Module

        device = make_device_with_module_bays("install-adopt-denied", ["Slot 1"])
        module_type = make_module_type("INSTALL-ADOPT-DENIED-CARD")
        InterfaceTemplate.objects.create(module_type=module_type, name="Te1/1/1", type="10gbase-x-sfpp")
        Interface.objects.create(device=device, name="Te1/1/1", type="10gbase-x-sfpp")
        item = _item(43, module_type.model, "Slot 1", phys_class="module")

        result = self._install(
            device,
            item,
            module_types=[module_type],
            exact_mappings=[],
            regex_mappings=[],
            changeable_components={Interface: Interface.objects.none()},
        )

        assert result["status"] == "skipped"
        assert result["reason"] == "a matching interface is not available for module adoption"
        assert not Module.objects.filter(device=device).exists()

    def test_an_invalid_serial_fails_without_installing(self):
        from dcim.models import Module

        device = make_device_with_module_bays("install-bad-serial", ["Slot 1"])
        module_type = make_module_type("INSTALL-BAD-SERIAL-CARD")
        item = _item(44, module_type.model, "Slot 1", phys_class="module", serial="x" * 500)

        result = self._install(device, item, module_types=[module_type], exact_mappings=[], regex_mappings=[])

        assert result["status"] == "failed"
        assert "serial" in result["reason"].lower()
        assert not Module.objects.filter(device=device).exists()
