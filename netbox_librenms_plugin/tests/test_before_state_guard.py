"""
The before-state guard finds each write of a change-logged row that plugin code asks for without a fresh snapshot.

A probe function is compiled with a file name inside the plugin package, so its frames count as plugin code.
"""

import ast
import textwrap

import pytest
from dcim.models import Device, InterfaceTemplate, MACAddress, ModuleBay, VirtualChassis
from django.contrib.contenttypes.models import ContentType
from extras.models import Tag
from ipam.models import VLAN

from netbox_librenms_plugin.import_utils.virtual_chassis import _sync_module_bay_counter
from netbox_librenms_plugin.models import DeviceTypeMapping
from netbox_librenms_plugin.tests.before_state_guard import ALLOWED_BULK_WRITES, PACKAGE, expected_violations
from netbox_librenms_plugin.tests.conftest import (
    _shared_infra,
    make_device,
    make_device_with_module_bays,
    make_interface,
    make_module_type,
)

PROBE = "before_state_guard_probe.py"


def plugin_function(source, path=PROBE):
    """Compile *source* as plugin code at *path* in the package and return the one function that it defines."""
    namespace = {}
    exec(compile(textwrap.dedent(source), str(PACKAGE / path), "exec"), namespace)
    (function,) = (value for key, value in namespace.items() if key != "__builtins__")
    return function


def found_by(call, *args):
    """Return ``(path, function, model, kind, pk)`` for each violation that *call* causes."""
    with expected_violations() as found:
        call(*args)
    return [(v.path, v.function, v.model, v.kind, v.pk) for v in found]


def stored(obj):
    """Return a new instance of *obj* read from the database, as a plugin view reads it."""
    return type(obj).objects.get(pk=obj.pk)


def make_tag(name="bsg-tag"):
    return Tag.objects.create(name=name, slug=name)


SAVE = """
def save(obj):
    obj.description = "changed"
    obj.save()
"""


@pytest.mark.django_db
class TestSaves:
    def test_a_save_without_a_snapshot_is_a_violation(self):
        device = stored(make_device("bsg-missing"))

        assert found_by(plugin_function(SAVE), device) == [(PROBE, "save", "dcim.Device", "save missing", device.pk)]

    def test_a_second_save_that_reuses_the_consumed_snapshot_is_a_violation(self):
        device = stored(make_device("bsg-stale"))
        save_twice = plugin_function(
            """
            def save_twice(obj):
                obj.snapshot()
                obj.description = "first"
                obj.save()
                obj.description = "second"
                obj.save()
            """
        )

        assert found_by(save_twice, device) == [(PROBE, "save_twice", "dcim.Device", "save stale", device.pk)]

    def test_an_update_after_a_create_without_a_snapshot_is_a_violation(self):
        create_then_update = plugin_function(
            """
            def create_then_update():
                from dcim.models import Site

                site = Site(name="bsg-created", slug="bsg-created")
                site.save()
                site.description = "changed"
                site.save()
                return site
            """
        )

        with expected_violations() as found:
            site = create_then_update()

        assert [(v.function, v.model, v.kind, v.pk) for v in found] == [
            ("create_then_update", "dcim.Site", "save missing", site.pk)
        ]

    def test_a_fresh_snapshot_is_not_a_violation(self):
        device = stored(make_device("bsg-fresh"))
        snapshot_and_save = plugin_function(
            """
            def snapshot_and_save(obj):
                obj.snapshot()
                obj.description = "changed"
                obj.save()
            """
        )

        assert found_by(snapshot_and_save, device) == []
        assert stored(device).description == "changed"

    def test_keep_change_log_before_state_gives_a_fresh_snapshot_for_each_save(self):
        device = stored(make_device("bsg-keep"))
        keep_and_save_twice = plugin_function(
            """
            def keep_and_save_twice(obj, read):
                from netbox_librenms_plugin.interface_sync import keep_change_log_before_state

                for description in ("first", "second"):
                    keep_change_log_before_state(obj, read())
                    obj.description = description
                    obj.save()
            """
        )

        assert found_by(keep_and_save_twice, device, lambda: stored(device)) == []
        assert stored(device).description == "second"

    def test_a_save_that_a_test_asks_for_is_not_a_violation(self):
        device = stored(make_device("bsg-test-save"))

        assert found_by(device.save) == []

    def test_the_save_method_of_a_plugin_model_is_not_the_caller(self):
        """The save() override of the plugin model is skipped, so the caller is the test, or the probe."""
        _site, _manufacturer, device_type, _role = _shared_infra()
        mapping = stored(DeviceTypeMapping.objects.create(librenms_hardware="bsg-hw", netbox_device_type=device_type))

        assert found_by(mapping.save) == []
        assert found_by(plugin_function(SAVE), mapping) == [
            (PROBE, "save", "netbox_librenms_plugin.DeviceTypeMapping", "save missing", mapping.pk)
        ]

    def test_a_save_inside_netbox_that_plugin_code_triggers_is_not_a_violation(self):
        """NetBox saves the master of a new virtual chassis without a snapshot; plugin code did not ask for that save."""
        device = stored(make_device("bsg-master"))
        create_chassis = plugin_function(
            """
            def create_chassis(master):
                from dcim.models import VirtualChassis

                VirtualChassis(name="bsg-chassis", master=master).save()
            """
        )

        assert found_by(create_chassis, device) == []
        assert stored(device).virtual_chassis == VirtualChassis.objects.get(name="bsg-chassis")

    def test_a_raw_save_is_not_a_violation(self):
        device = stored(make_device("bsg-raw"))
        raw_save = plugin_function(
            """
            def raw_save(obj):
                type(obj).save_base(obj, raw=True)
            """
        )

        assert found_by(raw_save, device) == []

    def test_a_new_cable_is_not_a_violation(self):
        """Cable.save() inserts the row and then updates it; the second save is part of the one save of the caller."""
        device = make_device("bsg-cable")
        create_cable = plugin_function(
            """
            def create_cable(a, b):
                from dcim.models import Cable

                cable = Cable(a_terminations=[a], b_terminations=[b])
                cable.save()
                return cable
            """
        )

        with expected_violations() as found:
            cable = create_cable(make_interface(device, "eth-a"), make_interface(device, "eth-b"))

        assert found == []
        assert stored(cable).a_terminations

    def test_a_second_save_of_a_new_cable_without_a_snapshot_is_a_violation(self):
        device = make_device("bsg-cable-twice")
        create_then_save = plugin_function(
            """
            def create_then_save(a, b):
                from dcim.models import Cable

                cable = Cable(a_terminations=[a], b_terminations=[b])
                cable.save()
                cable.description = "changed"
                cable.save()
                return cable
            """
        )

        with expected_violations() as found:
            cable = create_then_save(make_interface(device, "eth-a"), make_interface(device, "eth-b"))

        assert [(v.function, v.model, v.kind, v.pk) for v in found] == [
            ("create_then_save", "dcim.Cable", "save missing", cable.pk)
        ]

    def test_a_save_in_a_data_migration_is_not_a_violation(self):
        """A data migration runs without a request and with historical models, which have no snapshot()."""
        migrate = plugin_function(SAVE, path="migrations/before_state_guard_probe.py")

        assert found_by(migrate, stored(make_device("bsg-migration"))) == []

    def test_a_module_that_adopts_interfaces_is_not_a_violation(self):
        """NetBox moves the adopted interfaces with bulk_update() inside Module.save(); plugin code did not ask for it."""
        device = make_device_with_module_bays("bsg-adopt", ["bay1"])
        interface = make_interface(device, "eth-adopt")
        module_type = make_module_type("bsg-adopt-type")
        InterfaceTemplate.objects.create(module_type=module_type, name="eth-adopt", type="other")
        install_adopting = plugin_function(
            """
            def install_adopting(device, bay, module_type):
                from dcim.models import Module

                module = Module(device=device, module_bay=bay, module_type=module_type, status="active")
                module._adopt_components = True
                module.save()
                return module
            """
        )

        with expected_violations() as found:
            module = install_adopting(device, ModuleBay.objects.get(device=device, name="bay1"), module_type)

        assert found == []
        assert stored(interface).module == module


@pytest.mark.django_db
class TestManyToMany:
    @pytest.mark.parametrize(
        "action, change",
        [("pre_add", "obj.tags.add(tag)"), ("pre_remove", "obj.tags.remove(tag)"), ("pre_clear", "obj.tags.clear()")],
    )
    def test_a_tag_change_without_a_snapshot_is_a_violation(self, action, change):
        tag = make_tag()
        device = make_device("bsg-tags")
        if action != "pre_add":
            device.tags.add(tag)
        change_tags = plugin_function(f"def change_tags(obj, tag):\n    {change}\n")
        device = stored(device)

        assert found_by(change_tags, device, tag) == [(PROBE, "change_tags", "dcim.Device", f"m2m {action}", device.pk)]

    def test_a_django_many_to_many_change_without_a_snapshot_is_a_violation(self):
        interface = make_interface(make_device("bsg-vlans"), "eth-vlans")
        vlan = VLAN.objects.create(vid=10, name="bsg-vlan")
        add_vlan = plugin_function("def add_vlan(obj, vlan):\n    obj.tagged_vlans.add(vlan)\n")
        interface = stored(interface)

        assert found_by(add_vlan, interface, vlan) == [
            (PROBE, "add_vlan", "dcim.Interface", "m2m pre_add", interface.pk)
        ]

    def test_a_change_that_changes_nothing_is_not_a_violation(self):
        """Django sends pre_add with an empty pk_set when each object is already related."""
        interface = make_interface(make_device("bsg-no-change"), "eth-no-change")
        vlan = VLAN.objects.create(vid=20, name="bsg-vlan-present")
        interface.tagged_vlans.add(vlan)
        add_vlan = plugin_function("def add_vlan(obj, vlan):\n    obj.tagged_vlans.add(vlan)\n")

        assert found_by(add_vlan, stored(interface), vlan) == []

    def test_a_tag_change_with_a_fresh_snapshot_is_not_a_violation(self):
        snapshot_and_add = plugin_function(
            "def snapshot_and_add(obj, tag):\n    obj.snapshot()\n    obj.tags.add(tag)\n"
        )

        assert found_by(snapshot_and_add, stored(make_device("bsg-tag-fresh")), make_tag()) == []

    def test_a_tag_change_after_a_plugin_save_is_not_a_violation(self):
        """NetBox merges the change into the change log record of the save in the same request."""
        save_then_add = plugin_function(
            """
            def save_then_add(obj, tag):
                obj.snapshot()
                obj.description = "changed"
                obj.save()
                obj.tags.add(tag)
            """
        )
        create_then_add = plugin_function(
            """
            def create_then_add(tag):
                from dcim.models import Site

                site = Site(name="bsg-tagged", slug="bsg-tagged")
                site.save()
                site.tags.add(tag)
            """
        )
        tag = make_tag()

        assert found_by(save_then_add, stored(make_device("bsg-save-add")), tag) == []
        assert found_by(create_then_add, tag) == []

    def test_a_tag_change_after_a_save_that_a_test_asks_for_is_a_violation(self):
        """The save of the test is not a save of the request that the plugin code runs in."""
        device = stored(make_device("bsg-test-then-add"))
        device.save()
        add_tag = plugin_function("def add_tag(obj, tag):\n    obj.tags.add(tag)\n")

        assert found_by(add_tag, device, make_tag()) == [(PROBE, "add_tag", "dcim.Device", "m2m pre_add", device.pk)]


@pytest.mark.django_db
class TestBulkWrites:
    def test_a_queryset_update_is_a_violation(self):
        device = make_device("bsg-update")
        update = plugin_function(
            """
            def update(pk):
                from dcim.models import Device

                Device.objects.filter(pk=pk).update(serial="changed")
            """
        )

        assert found_by(update, device.pk) == [(PROBE, "update", "dcim.Device", "update", None)]

    def test_a_bulk_update_is_one_violation(self):
        device = stored(make_device("bsg-bulk-update"))
        device.serial = "changed"
        bulk_update = plugin_function(
            """
            def bulk_update(obj):
                type(obj).objects.bulk_update([obj], ["serial"])
            """
        )

        assert found_by(bulk_update, device) == [(PROBE, "bulk_update", "dcim.Device", "bulk_update", None)]

    def test_the_add_of_a_generic_relation_is_a_violation(self):
        interface = make_interface(make_device("bsg-mac"), "eth-mac")
        mac = MACAddress.objects.create(mac_address="00:11:22:33:44:55")
        add_mac = plugin_function("def add_mac(interface, mac):\n    interface.mac_addresses.add(mac)\n")

        assert found_by(add_mac, stored(interface), mac) == [(PROBE, "add_mac", "dcim.MACAddress", "generic add", None)]
        assert stored(mac).assigned_object == interface

    def test_an_update_of_a_model_without_a_change_log_is_not_a_violation(self):
        content_type = ContentType.objects.get_for_model(Device)
        update = plugin_function("def update(model, pk):\n    model.objects.filter(pk=pk).update(model='device')\n")

        assert found_by(update, ContentType, content_type.pk) == []

    def test_an_update_that_a_test_asks_for_is_not_a_violation(self):
        device = make_device("bsg-test-update")

        assert found_by(lambda: Device.objects.filter(pk=device.pk).update(serial="changed")) == []

    def test_the_allowed_counter_update_is_not_a_violation(self):
        device = make_device_with_module_bays("bsg-counter", ["bay1"])
        Device.objects.filter(pk=device.pk).update(module_bay_count=0)

        assert found_by(_sync_module_bay_counter, stored(device)) == []
        assert stored(device).module_bay_count == 1

    def test_the_same_counter_update_in_another_function_is_a_violation(self):
        device = make_device_with_module_bays("bsg-counter-other", ["bay1"])
        sync_counter = plugin_function(
            """
            def sync_counter(device):
                type(device).objects.filter(pk=device.pk).update(module_bay_count=device.modulebays.count())
            """
        )

        assert found_by(sync_counter, device) == [(PROBE, "sync_counter", "dcim.Device", "update", None)]


def _functions(path):
    """Return the qualified name of each function in *path*, as the guard names the function of a frame."""
    names = set()

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not isinstance(child, ast.ClassDef):
                    names.add(".".join([*scope, child.name]))
                visit(child, [*scope, child.name])
            else:
                visit(child, scope)

    visit(ast.parse((PACKAGE / path).read_text()), [])
    return names


@pytest.mark.parametrize("path, function, reason", ALLOWED_BULK_WRITES)
def test_each_allowed_bulk_write_names_a_function_that_exists(path, function, reason):
    assert function in _functions(path), f"{path}:{function} no longer exists; remove the allowlist entry"
