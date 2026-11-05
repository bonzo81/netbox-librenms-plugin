"""The module views that are not grant writers refuse an invalid posted target, as the writers do."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from netbox_librenms_plugin.module_grants import GrantRefusal
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    install_module,
    make_device,
    make_device_with_module_bays,
    make_module_bay,
    make_module_type,
    make_virtual_chassis,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms, message_texts, missing_pk

pytestmark = pytest.mark.django_db

# The page device is VC position 2, where this name never rewrites: the VC report has a no-op to show.
_TEMPLATE_NAME = "2/x1/1/c9"

INVALID_TARGETS = ["not-a-pk", "missing", "other-chassis", "hidden-member"]


def _world(tag):
    """Build a page device in a three-member chassis, a device in another chassis, and a module elsewhere."""
    from dcim.models import Interface, InterfaceTemplate, Module

    sibling = make_device(f"{tag}-sibling")
    page = make_device_with_module_bays(f"{tag}-page", ["Slot 1"])
    hidden = make_device(f"{tag}-hidden")
    make_virtual_chassis(f"{tag}-vc", sibling, page, hidden)
    outsider = make_device(f"{tag}-outsider")
    make_virtual_chassis(f"{tag}-other-vc", outsider)

    module = install_module(page, "Slot 1", f"{tag.upper()}-CARD")
    InterfaceTemplate.objects.create(module_type=module.module_type, name=_TEMPLATE_NAME, type="10gbase-x-sfpp")
    interface = Interface.objects.create(device=page, module=module, name=_TEMPLATE_NAME, type="1000base-t")

    empty_bay = make_module_bay(page, "Slot 2")
    source = make_device(f"{tag}-source")
    moving = Module.objects.create(
        device=source,
        module_bay=make_module_bay(source, "Source Bay"),
        module_type=make_module_type(f"{tag.upper()}-MOVING"),
        serial=f"{tag}-moving",
        status="active",
    )
    return {
        "page": page,
        "hidden": hidden,
        "outsider": outsider,
        "module": module,
        "interface": interface,
        "empty_bay": empty_bay,
        "source": source,
        "moving": moving,
        "visible_names": [sibling.name, page.name, outsider.name, source.name],
    }


def _selection(world, kind):
    from dcim.models import Device

    return {
        "not-a-pk": "not-a-pk",
        "missing": str(missing_pk(Device)),
        "other-chassis": str(world["outsider"].pk),
        "hidden-member": str(world["hidden"].pk),
    }[kind]


def _user(world, tag, perm_specs):
    """Return a user with *perm_specs* who may view every device of *world* except the hidden member."""
    from dcim.models import Device

    from netbox_librenms_plugin.tests.view_test_helpers import grant

    user = make_user_with_perms(f"{tag}-user", perm_specs)
    return grant(user, "view", Device, constraints={"name__in": world["visible_names"]})


def _url(name, page):
    return reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs={"pk": page.pk})


class TestInterfaceTypePreview:
    @staticmethod
    def _get(client, settings, world, tag, selected_device_id):
        from dcim.models import Interface, Module

        configure_default_librenms_server(settings)
        client.force_login(_user(world, tag, [("view", Module), ("view", Interface)]))
        return client.get(
            _url("module_interface_type_preview", world["page"]),
            {"server_key": "default", "module_id": str(world["module"].pk), "selected_device_id": selected_device_id},
        )

    def test_the_page_device_target_shows_the_preview(self, client, settings):
        world = _world("tgt-preview-ok")

        response = self._get(client, settings, world, "tgt-preview-ok", "")

        assert response.status_code == 200
        assert f'name="interface_id" value="{world["interface"].pk}"' in response.content.decode()

    @pytest.mark.parametrize("kind", INVALID_TARGETS)
    def test_an_invalid_target_is_refused(self, client, settings, kind):
        world = _world(f"tgt-preview-{kind}")

        response = self._get(client, settings, world, f"tgt-preview-{kind}", _selection(world, kind))

        assert response.status_code == 400
        assert response.content.decode() == GrantRefusal.DESTINATION.message
        assert message_texts(response.wsgi_request) == []


class TestApplyInterfaceTypes:
    @staticmethod
    def _post(client, settings, world, tag, selected_device_id):
        from dcim.models import Interface, Module

        configure_default_librenms_server(settings)
        client.force_login(_user(world, tag, [("view", Module), ("change", Interface)]))
        interface = world["interface"]
        return client.post(
            _url("apply_module_interface_types", world["page"]),
            {
                "server_key": "default",
                "selected_device_id": selected_device_id,
                "module_id": str(world["module"].pk),
                "interface_id": [str(interface.pk)],
                f"current_type_{interface.pk}": "1000base-t",
                f"template_type_{interface.pk}": "10gbase-x-sfpp",
            },
        )

    def test_the_page_device_target_updates_the_type(self, client, settings):
        world = _world("tgt-apply-ok")

        response = self._post(client, settings, world, "tgt-apply-ok", "")

        world["interface"].refresh_from_db()
        assert response.status_code == 302
        assert world["interface"].type == "10gbase-x-sfpp"

    @pytest.mark.parametrize("kind", INVALID_TARGETS)
    def test_an_invalid_target_is_refused(self, client, settings, kind):
        world = _world(f"tgt-apply-{kind}")

        response = self._post(client, settings, world, f"tgt-apply-{kind}", _selection(world, kind))

        world["interface"].refresh_from_db()
        assert response.status_code == 302
        assert world["interface"].type == "1000base-t"
        assert message_texts(response.wsgi_request) == [GrantRefusal.DESTINATION.message]


class TestVCNormalizationReport:
    @staticmethod
    def _get(client, world, tag, selected_device_id):
        from dcim.models import Module

        client.force_login(_user(world, tag, [("view", Module)]))
        return client.get(
            _url("vc_normalization_report", world["page"]),
            {"module_id": str(world["module"].pk), "selected_device_id": selected_device_id},
        )

    def test_the_page_device_target_shows_the_report(self, client):
        world = _world("tgt-report-ok")

        response = self._get(client, world, "tgt-report-ok", "")

        assert response.status_code == 200
        assert _TEMPLATE_NAME in response.context["report_markdown"]

    @pytest.mark.parametrize("kind", INVALID_TARGETS)
    def test_an_invalid_target_is_refused(self, client, kind):
        world = _world(f"tgt-report-{kind}")

        response = self._get(client, world, f"tgt-report-{kind}", _selection(world, kind))

        assert response.status_code == 400
        assert response.content.decode() == GrantRefusal.DESTINATION.message
        assert message_texts(response.wsgi_request) == []


class TestMoveModule:
    @staticmethod
    def _post(client, settings, world, tag, selected_device_id):
        from dcim.models import Module, ModuleBay

        configure_default_librenms_server(settings)
        client.force_login(_user(world, tag, [("view", ModuleBay), ("change", Module), ("delete", Module)]))
        # Relocation exists from NetBox 4.7; CI also gates 4.4/4.6, where the view refuses before the move.
        with patch("netbox_librenms_plugin.views.sync.modules.netbox_relocates_module_subtree", return_value=True):
            return client.post(
                _url("move_module", world["page"]),
                {
                    "server_key": "default",
                    "selected_device_id": selected_device_id,
                    "conflict_module_id": str(world["moving"].pk),
                    "target_bay_id": str(world["empty_bay"].pk),
                },
            )

    def test_the_page_device_target_moves_the_module(self, client, settings):
        world = _world("tgt-move-ok")

        response = self._post(client, settings, world, "tgt-move-ok", "")

        world["moving"].refresh_from_db()
        assert response.status_code == 302
        assert world["moving"].module_bay_id == world["empty_bay"].pk

    @pytest.mark.parametrize("kind", INVALID_TARGETS)
    def test_an_invalid_target_is_refused(self, client, settings, kind):
        world = _world(f"tgt-move-{kind}")

        response = self._post(client, settings, world, f"tgt-move-{kind}", _selection(world, kind))

        world["moving"].refresh_from_db()
        assert response.status_code == 302
        assert world["moving"].device_id == world["source"].pk
        assert message_texts(response.wsgi_request) == [GrantRefusal.DESTINATION.message]
