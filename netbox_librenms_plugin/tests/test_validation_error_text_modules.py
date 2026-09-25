"""
The module actions show NetBox's validation message only to a superuser.

An admin ``CUSTOM_VALIDATORS`` rule (or, for the port bind that calls no ``clean()``, a real
``pre_save`` receiver of another plugin) refuses the write with a message that names a device
outside the viewer's scope. The bay actions take a real HTTP request. The inventory actions need a
seeded inventory snapshot, so they use the module tests' seam: a real request, a real user with
real grants, and the view's own permission gate. A restricted viewer gets the refused field or the
model, and a superuser gets the message.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models.signals import pre_save
from django.urls import reverse
from extras.validators import CustomValidator

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
from netbox_librenms_plugin.tests.view_test_helpers import (
    make_request,
    make_user_with_perms,
    message_texts,
    module_row_binding,
)
from netbox_librenms_plugin.tests.view_test_helpers import post as view_post
from netbox_librenms_plugin.utils import (
    module_inventory_binding_token,
    module_inventory_snapshot_digest,
    netbox_relocates_module_subtree,
)

HIDDEN = "(only a superuser sees the message)"

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.parametrize("superuser", [False, True], ids=["restricted", "superuser"]),
]


class _Refuses(CustomValidator):
    """An admin validator that refuses every clean() with *text* under *key*."""

    def __init__(self, text, key=None):
        super().__init__()
        self._text, self._key = text, key

    def validate(self, instance, request):
        self.fail(self._text, field=self._key)


def _viewer(tag, superuser, *devices):
    """Return a superuser, or a user whose device grants cover only *devices* and whose module grants are open."""
    from core.models import ObjectType
    from dcim.models import Device, DeviceType, Interface, Module, ModuleBay, ModuleBayTemplate, ModuleType
    from users.models import ObjectPermission

    from netbox_librenms_plugin.models import ModuleBayMapping

    if superuser:
        return make_superuser(f"{tag}-superuser")
    user = make_user_with_perms(
        f"{tag}-viewer", [("view", Device), ("change", Device)], constraints={"pk__in": [d.pk for d in devices]}
    )
    permission = ObjectPermission.objects.create(name=f"{tag}-modules", actions=["view", "add", "change", "delete"])
    models = (DeviceType, Interface, Module, ModuleBay, ModuleBayTemplate, ModuleType, ModuleBayMapping)
    permission.object_types.set([ObjectType.objects.get_for_model(model) for model in models])
    permission.users.set([user])
    return get_user_model().objects.get(pk=user.pk)


def _assert_shown(texts, hidden, superuser, restricted_text):
    texts = " | ".join(texts)
    if superuser:
        assert f"Conflicts with {hidden.name}." in texts, texts
    else:
        assert f"NetBox refuses {restricted_text} {HIDDEN}" in texts, texts
        assert hidden.name not in texts, texts


def _drive(view_class, device, data, user, live_librenms, inventory=None):
    """POST into a bound view with a real request, and return the message texts."""
    request = make_request("post", {"server_key": "default", **data}, user=user, path="/modules/")
    view = view_class()
    view._librenms_api = live_librenms.api
    key = None if inventory is None else seed_inventory(view, device, inventory, librenms_id=device.pk)
    try:
        view_post(view, request, pk=device.pk)
    finally:
        if key is not None:
            cache.delete(key)
    return message_texts(request)


def _row(index, model, name, **extra):
    return {
        "entPhysicalIndex": index,
        "entPhysicalModelName": model,
        "entPhysicalName": name,
        "entPhysicalDescr": name,
        "entPhysicalClass": "module",
        "entPhysicalContainedIn": 0,
        "entPhysicalSerialNum": "",
        **extra,
    }


def _linked(device):
    device.custom_field_data["librenms_id"] = {"default": device.pk}
    device.save(update_fields=["custom_field_data"])
    return device


def _refuse_the_bind_of(interface, hidden):
    from django.core.exceptions import ValidationError

    def refuse(sender, instance, **kwargs):
        if instance.pk == interface.pk:
            raise ValidationError({hidden.name: [f"Conflicts with {hidden.name}."]})

    return refuse


class TestAModuleActionShowsNetBoxsMessageOnlyToASuperuser:
    def test_a_module_install(self, settings, live_librenms, superuser):
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        tag = f"vetext-install-{int(superuser)}"
        device, hidden = _linked(make_device(tag)), make_device(f"{tag}-hidden")
        bay, module_type = make_module_bay(device, "Slot 1"), make_module_type(f"CARD-{tag}")
        settings.CUSTOM_VALIDATORS = {"dcim.module": [_Refuses(f"Conflicts with {hidden.name}.", "serial")]}

        texts = _drive(
            InstallModuleView,
            device,
            {"module_bay_id": str(bay.pk), "module_type_id": str(module_type.pk)},
            _viewer(tag, superuser, device),
            live_librenms,
        )

        _assert_shown(texts, hidden, superuser, "the serial field")

    @pytest.mark.parametrize("view_name", ["InstallBranchView", "InstallSelectedView"])
    def test_a_bulk_install_row(self, settings, live_librenms, superuser, view_name):
        from netbox_librenms_plugin.views.sync import modules

        tag = f"vetext-{view_name.lower()}-{int(superuser)}"
        device, hidden = _linked(make_device_with_module_bays(tag, ["Slot 1"])), make_device(f"{tag}-hidden")
        rows = [_row(10, make_module_type(f"CARD-{tag}").model, "Slot 1")]
        settings.CUSTOM_VALIDATORS = {"dcim.module": [_Refuses(f"Conflicts with {hidden.name}.")]}

        texts = _drive(
            getattr(modules, view_name),
            device,
            _bulk_data(device, view_name, rows),
            _viewer(tag, superuser, device),
            live_librenms,
            rows,
        )

        _assert_shown(texts, hidden, superuser, "the module")

    @pytest.mark.parametrize("view_name", ["InstallBranchView", "InstallSelectedView"])
    def test_a_bulk_install_port_bind(self, live_librenms, superuser, view_name):
        """The bind calls no clean(), so another plugin's pre_save receiver refuses it."""
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync import modules

        tag = f"vetext-bind-{view_name.lower()}-{int(superuser)}"
        device, hidden = _linked(make_device_with_module_bays(tag, ["Slot 1"])), make_device(f"{tag}-hidden")
        interface = make_interface(device, "Te1/1/1")
        model = make_module_type(f"CARD-{tag}").model
        rows = [_row(10, model, "Slot 1", _librenms_port_id=7301, _librenms_ifname=interface.name)]
        refuse = _refuse_the_bind_of(interface, hidden)

        pre_save.connect(refuse, sender=Interface, weak=False)
        try:
            texts = _drive(
                getattr(modules, view_name),
                device,
                _bulk_data(device, view_name, rows),
                _viewer(tag, superuser, device),
                live_librenms,
                rows,
            )
        finally:
            pre_save.disconnect(refuse, sender=Interface)

        _assert_shown(texts, hidden, superuser, "the module")

    def test_a_module_serial_update(self, settings, live_librenms, superuser):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        tag = f"vetext-moduleserial-{int(superuser)}"
        device, hidden = _linked(make_device_with_module_bays(tag, ["Slot 1"])), make_device(f"{tag}-hidden")
        module = install_module(device, "Slot 1", f"CARD-{tag}", serial="OLD")
        rows = [_row(11, module.module_type.model, "Slot 1", entPhysicalSerialNum="NEW")]
        settings.CUSTOM_VALIDATORS = {"dcim.module": [_Refuses(f"Conflicts with {hidden.name}.", "serial")]}
        binding = module_row_binding(device, "update_module_serial", rows[0], action_target={"module_id": module.pk})

        texts = _drive(
            UpdateModuleSerialView,
            device,
            {"module_id": str(module.pk), "ent_index": "11", "inventory_binding": binding},
            _viewer(tag, superuser, device),
            live_librenms,
            rows,
        )

        _assert_shown(texts, hidden, superuser, "the serial field")

    def test_a_module_replace(self, settings, live_librenms, superuser):
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        tag = f"vetext-replace-{int(superuser)}"
        device, hidden = _linked(make_device_with_module_bays(tag, ["Slot 1"])), make_device(f"{tag}-hidden")
        installed = install_module(device, "Slot 1", f"OLD-{tag}", serial="OLD")
        rows = [{"entPhysicalIndex": 100, "entPhysicalModelName": make_module_type(f"NEW-{tag}").model}]
        settings.CUSTOM_VALIDATORS = {"dcim.module": [_Refuses(f"Conflicts with {hidden.name}.", hidden.name)]}
        binding = module_row_binding(device, "replace_module", rows[0], action_target={"module_id": installed.pk})

        texts = _drive(
            ReplaceModuleView,
            device,
            {"module_id": str(installed.pk), "ent_index": "100", "inventory_binding": binding},
            _viewer(tag, superuser, device),
            live_librenms,
            rows,
        )

        _assert_shown(texts, hidden, superuser, "the module")

    @pytest.mark.skipif(not netbox_relocates_module_subtree(), reason="NetBox before 4.7 refuses the move first.")
    def test_a_module_move(self, settings, live_librenms, superuser):
        from netbox_librenms_plugin.views.sync.modules import MoveModuleView

        tag = f"vetext-move-{int(superuser)}"
        source, target, hidden = (
            make_device(f"{tag}-src"),
            _linked(make_device(f"{tag}-dst")),
            make_device(f"{tag}-hidden"),
        )
        make_module_bay(source, "Source Bay")
        moving = install_module(source, "Source Bay", f"CARD-{tag}", serial=f"SN-{tag}")
        target_bay = make_module_bay(target, "Target Bay")
        settings.CUSTOM_VALIDATORS = {"dcim.module": [_Refuses(f"Conflicts with {hidden.name}.", "module_bay")]}

        texts = _drive(
            MoveModuleView,
            target,
            {"conflict_module_id": str(moving.pk), "target_bay_id": str(target_bay.pk)},
            _viewer(tag, superuser, source, target),
            live_librenms,
        )

        _assert_shown(texts, hidden, superuser, "the module_bay field")

    def test_a_bay_template_add(self, client, settings, superuser):
        from dcim.models import ModuleBayTemplate

        tag = f"vetext-baytemplate-{int(superuser)}"
        device, hidden = make_device_with_module_bays(tag, []), make_device(f"{tag}-hidden")
        settings.CUSTOM_VALIDATORS = {"dcim.modulebaytemplate": [_Refuses(f"Conflicts with {hidden.name}.")]}
        client.force_login(_viewer(tag, superuser, device))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": device.pk}),
            {"target_kind": "device_type", "target_pk": str(device.device_type.pk), "name": "Slot 9"},
        )

        _assert_shown(
            message_texts(response.wsgi_request), hidden, superuser, f"the {ModuleBayTemplate._meta.verbose_name}"
        )

    def test_a_mapping_to_an_existing_bay(self, client, settings, superuser):
        tag = f"vetext-mapbay-{int(superuser)}"
        device, hidden = make_device(tag), make_device(f"{tag}-hidden")
        make_module_bay(device, "Slot 1")
        settings.CUSTOM_VALIDATORS = {
            "netbox_librenms_plugin.modulebaymapping": [_Refuses(f"Conflicts with {hidden.name}.", "netbox_bay_name")]
        }
        client.force_login(_viewer(tag, superuser, device))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": device.pk}),
            {"mode": "map_existing", "librenms_name": "Routing Engine 0", "name": "Slot 1"},
        )

        _assert_shown(message_texts(response.wsgi_request), hidden, superuser, "the netbox_bay_name field")


def _bulk_data(device, view_name, rows):
    """Return the POST of a branch install (parent index 10) or a selected install (row 10)."""
    digest = module_inventory_snapshot_digest(rows)
    if view_name == "InstallBranchView":
        binding = module_inventory_binding_token(
            device.pk, "default", "install_branch", {"parent_index": 10}, 10, digest
        )
        return {"parent_index": "10", "inventory_binding": binding}
    binding = module_inventory_binding_token(device.pk, "default", "install_selected", {}, None, digest)
    return {"select": ["10"], "inventory_binding": binding}
