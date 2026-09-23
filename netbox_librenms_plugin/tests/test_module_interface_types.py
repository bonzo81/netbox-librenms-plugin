"""Report and apply module interface types from interface templates."""

import pytest
from django.urls import reverse

from netbox_librenms_plugin.tests.cache_test_helpers import seed_inventory
from netbox_librenms_plugin.tests.conftest import install_module, make_device_with_module_bays
from netbox_librenms_plugin.tests.view_test_helpers import get as view_get
from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts
from netbox_librenms_plugin.tests.view_test_helpers import post as view_post

pytestmark = pytest.mark.django_db


def _module_with_templates(tag, templates):
    """Install a real module and add the requested interface templates."""
    from dcim.models import InterfaceTemplate

    device = make_device_with_module_bays(f"{tag}-device", ["Slot 1"])
    module = install_module(device, "Slot 1", f"{tag.upper()}-CARD")
    for name, interface_type in templates:
        InterfaceTemplate.objects.create(module_type=module.module_type, name=name, type=interface_type)
    return device, module


def _inventory_row(module, *, index=179):
    """Return a cached LibreNMS row that matches the installed module."""
    return {
        "entPhysicalIndex": index,
        "entPhysicalName": module.module_bay.name,
        "entPhysicalModelName": module.module_type.model,
        "entPhysicalClass": "module",
        "entPhysicalContainedIn": 0,
        "entPhysicalSerialNum": "",
    }


def _seed_module_tab(device, module, *, inventory_row=None, librenms_id=179):
    """Map the device and seed one real module-tab inventory snapshot."""
    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    set_librenms_device_id(device, librenms_id, "default")
    device.save(update_fields=["custom_field_data"])
    device.__dict__.pop("cf", None)
    return seed_inventory(
        DeviceModuleTableView(),
        device,
        [inventory_row or _inventory_row(module)],
        librenms_id=librenms_id,
    )


def _render_module_tab(device, user):
    """Render the real module table from its seeded cache."""
    from netbox_librenms_plugin.tests.view_test_helpers import make_request
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    request = make_request(
        "get",
        {"tab": "modules", "server_key": "default"},
        user=user,
        path=reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": device.pk}),
    )
    view = DeviceModuleTableView()
    view.setup(request, pk=device.pk)
    view.cache_only = True
    context = view.get_context_data(request, device)
    return context["table"].as_html(request)


class TestModuleTemplateInterfaceSpecs:
    """Template name prediction keeps types only while positions remain attributable."""

    def test_prediction_rewrites_names_and_keeps_types_by_position(self):
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_module_template_interface_specs

        device, module = _module_with_templates(
            "type-spec-rewrite",
            [("Ethernet1", "1000base-t"), ("Ethernet2", "10gbase-x-sfpp")],
        )

        @receiver(predict_module_interface_names, dispatch_uid="test-type-spec-rewrite")
        def rewrite(sender, device, module, names, **kwargs):
            return [f"{name}/child" for name in names]

        try:
            assert get_module_template_interface_specs(device, module) == [
                ("Ethernet1/child", "1000base-t"),
                ("Ethernet2/child", "10gbase-x-sfpp"),
            ]
        finally:
            predict_module_interface_names.disconnect(dispatch_uid="test-type-spec-rewrite")

    def test_prediction_length_change_keeps_the_instantiated_names_and_types(self):
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_module_template_interface_specs

        device, module = _module_with_templates(
            "type-spec-filter",
            [("Ethernet1", "1000base-t"), ("Ethernet2", "10gbase-x-sfpp")],
        )

        @receiver(predict_module_interface_names, dispatch_uid="test-type-spec-filter")
        def filter_names(sender, device, module, names, **kwargs):
            return names[:1]

        try:
            assert get_module_template_interface_specs(device, module) == [
                ("Ethernet1", "1000base-t"),
                ("Ethernet2", "10gbase-x-sfpp"),
            ]
        finally:
            predict_module_interface_names.disconnect(dispatch_uid="test-type-spec-filter")

    def test_reordered_prediction_does_not_swap_template_types(self):
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_module_template_interface_specs

        device, module = _module_with_templates(
            "type-spec-reorder",
            [("Ethernet1", "1000base-t"), ("Ethernet2", "10gbase-x-sfpp")],
        )

        @receiver(predict_module_interface_names, dispatch_uid="test-type-spec-reorder")
        def reorder_names(sender, device, module, names, **kwargs):
            return list(reversed(names))

        try:
            assert get_module_template_interface_specs(device, module) == [
                ("Ethernet1", "1000base-t"),
                ("Ethernet2", "10gbase-x-sfpp"),
            ]
        finally:
            predict_module_interface_names.disconnect(dispatch_uid="test-type-spec-reorder")

    def test_duplicate_prediction_keeps_the_instantiated_names_and_types(self):
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_module_template_interface_specs

        device, module = _module_with_templates(
            "type-spec-duplicate",
            [("Ethernet1", "1000base-t"), ("Ethernet2", "10gbase-x-sfpp")],
        )

        @receiver(predict_module_interface_names, dispatch_uid="test-type-spec-duplicate")
        def duplicate_name(sender, device, module, names, **kwargs):
            return ["Ethernet2", "Ethernet2", "Ethernet1"]

        try:
            assert get_module_template_interface_specs(device, module) == [
                ("Ethernet1", "1000base-t"),
                ("Ethernet2", "10gbase-x-sfpp"),
            ]
        finally:
            predict_module_interface_names.disconnect(dispatch_uid="test-type-spec-duplicate")

    def test_a_receiver_that_renames_nothing_is_asked_once_for_the_whole_module(self):
        """Only an actual rename costs one send per template, so the common case stays at one."""
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_module_template_interface_specs

        device, module = _module_with_templates(
            "type-spec-send-count",
            [("Ethernet1", "1000base-t"), ("Ethernet2", "10gbase-x-sfpp"), ("Ethernet3", "1000base-t")],
        )
        calls = []

        @receiver(predict_module_interface_names, dispatch_uid="test-type-spec-send-count")
        def count_calls(sender, device, module, names, **kwargs):
            calls.append(list(names))
            return None

        try:
            specs = get_module_template_interface_specs(device, module)
        finally:
            predict_module_interface_names.disconnect(dispatch_uid="test-type-spec-send-count")

        assert specs == [
            ("Ethernet1", "1000base-t"),
            ("Ethernet2", "10gbase-x-sfpp"),
            ("Ethernet3", "1000base-t"),
        ]
        assert calls == [["Ethernet1", "Ethernet2", "Ethernet3"]]

    def test_vc_rewrite_collision_keeps_the_name_without_a_type(self):
        from dcim.models import InterfaceTemplate

        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
        from netbox_librenms_plugin.utils import get_module_template_interface_specs

        first = make_device_with_module_bays("type-spec-collision-first", [])
        second = make_device_with_module_bays("type-spec-collision-second", ["Slot 1"])
        make_virtual_chassis("type-spec-collision-vc", first, second)
        module = install_module(second, "Slot 1", "TYPE-SPEC-COLLISION-CARD")
        InterfaceTemplate.objects.create(
            module_type=module.module_type,
            name="Ethernet1/1",
            type="1000base-t",
        )
        InterfaceTemplate.objects.create(
            module_type=module.module_type,
            name="Ethernet2/1",
            type="10gbase-x-sfpp",
        )

        assert get_module_template_interface_specs(second, module) == [("Ethernet2/1", "")]


class TestModuleInterfaceTypeTab:
    """The real module tab reports actionable interface type differences."""

    def test_real_tab_renders_mismatch_badge_and_preview_action(self, settings):
        from dcim.models import Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_superuser

        configure_default_librenms_server(settings)
        device, module = _module_with_templates(
            "type-tab-mismatch",
            [("Ethernet1", "10gbase-x-sfpp")],
        )
        Interface.objects.create(device=device, module=module, name="Ethernet1", type="1000base-t")
        cache_key = _seed_module_tab(device, module)

        try:
            content = _render_module_tab(device, make_superuser("type-tab-mismatch-user"))
        finally:
            cache.delete(cache_key)

        assert "1 interface type differs from its module template: Ethernet1" in content
        preview_url = reverse(
            "plugins:netbox_librenms_plugin:module_interface_type_preview",
            kwargs={"pk": device.pk},
        )
        assert preview_url in content
        assert 'data-action="review-interface-types"' in content
        assert 'hx-target="#htmx-modal-content"' in content
        assert 'hx-swap="innerHTML"' in content
        assert 'hx-sync="#htmx-modal-content:replace"' in content
        assert 'hx-disabled-elt="this"' in content

    @pytest.mark.parametrize("template_type", ["", "1000base-t"], ids=["untyped-template", "matching-type"])
    def test_real_tab_omits_action_when_no_template_type_differs(self, settings, template_type):
        from dcim.models import Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_superuser

        configure_default_librenms_server(settings)
        device, module = _module_with_templates(
            f"type-tab-clean-{template_type or 'empty'}",
            [("Ethernet1", template_type)],
        )
        Interface.objects.create(device=device, module=module, name="Ethernet1", type="1000base-t")
        cache_key = _seed_module_tab(device, module)

        try:
            content = _render_module_tab(
                device,
                make_superuser(f"type-tab-clean-{template_type or 'empty'}-user"),
            )
        finally:
            cache.delete(cache_key)

        assert "interface type differs from" not in content
        assert 'aria-label="Review interface type differences"' not in content

    def test_module_type_mismatch_suppresses_interface_type_action(self, settings):
        from dcim.models import Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import (
            configure_default_librenms_server,
            make_module_type,
            make_superuser,
        )

        configure_default_librenms_server(settings)
        device, module = _module_with_templates(
            "type-tab-module-mismatch",
            [("Ethernet1", "10gbase-x-sfpp")],
        )
        Interface.objects.create(device=device, module=module, name="Ethernet1", type="1000base-t")
        other_type = make_module_type("TYPE-TAB-OTHER-CARD")
        inventory_row = _inventory_row(module)
        inventory_row["entPhysicalModelName"] = other_type.model
        cache_key = _seed_module_tab(device, module, inventory_row=inventory_row)

        try:
            content = _render_module_tab(device, make_superuser("type-tab-module-mismatch-user"))
        finally:
            cache.delete(cache_key)

        assert "Type Mismatch" in content
        assert "interface type differs from" not in content
        assert 'aria-label="Review interface type differences"' not in content


class TestModuleInterfaceTypeTable:
    """Table hints expose bounded detail and require interface change access."""

    def test_tooltip_caps_real_interface_names_and_reports_the_remainder(self, settings):
        from dcim.models import Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_superuser

        configure_default_librenms_server(settings)
        templates = [(f"Ethernet{number:02}", "10gbase-x-sfpp") for number in range(1, 13)]
        device, module = _module_with_templates("type-table-tooltip", templates)
        Interface.objects.bulk_create(
            [
                Interface(device=device, module=module, name=name, type="1000base-t")
                for name, _template_type in templates
            ]
        )
        cache_key = _seed_module_tab(device, module)

        try:
            content = _render_module_tab(device, make_superuser("type-table-tooltip-user"))
        finally:
            cache.delete(cache_key)

        assert "12 interface types differ from their module template" in content
        assert "Ethernet10, +2 more" in content
        assert "Ethernet11" not in content

    @pytest.mark.parametrize(
        "table_kwargs,record",
        [
            ({"can_change_interface": False}, {"installed_module_id": 1}),
            ({"can_change_interface": True}, {}),
        ],
        ids=["no-change-permission", "no-installed-module"],
    )
    def test_preview_action_requires_change_permission_and_installed_module(self, table_kwargs, record):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        device = make_device_with_module_bays("type-table-action-device", [])
        table = LibreNMSModuleTable(
            [],
            device=device,
            has_write_permission=True,
            **table_kwargs,
        )

        content = str(
            table.render_actions(
                "",
                {"interface_type_mismatch_count": 1, **record},
            )
        )

        assert "module-interface-type-preview" not in content


class TestModuleInterfaceTypeMismatchDiscovery:
    """Mismatch discovery respects view grants and loads interfaces once per device."""

    @staticmethod
    def _view(user):
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        request = make_request("get", user=user)
        view = DeviceModuleTableView()
        view.setup(request)
        return view

    def test_interfaces_outside_the_view_grant_are_not_reported(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        device, module = _module_with_templates(
            "type-discovery-permission",
            [("Ethernet1", "10gbase-x-sfpp")],
        )
        Interface.objects.create(device=device, module=module, name="Ethernet1", type="1000base-t")
        user = make_user_with_perms(
            "type-discovery-permission-user",
            [("view", Interface)],
            constraints={"name": "Management1"},
            plugin_write=False,
        )

        assert self._view(user)._find_module_interface_type_mismatches(module) == []

    def test_two_modules_on_one_device_use_one_interface_query(self):
        from dcim.models import Interface, InterfaceTemplate
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import make_superuser

        device = make_device_with_module_bays("type-discovery-query-device", ["Slot 1", "Slot 2"])
        first = install_module(device, "Slot 1", "TYPE-DISCOVERY-QUERY-FIRST")
        second = install_module(device, "Slot 2", "TYPE-DISCOVERY-QUERY-SECOND")
        for module, name in ((first, "Ethernet1"), (second, "Ethernet2")):
            InterfaceTemplate.objects.create(module_type=module.module_type, name=name, type="10gbase-x-sfpp")
            Interface.objects.create(device=device, module=module, name=name, type="1000base-t")
        view = self._view(make_superuser("type-discovery-query-user"))

        with CaptureQueriesContext(connection) as captured:
            assert [interface.name for interface in view._find_module_interface_type_mismatches(first)] == ["Ethernet1"]
            assert [interface.name for interface in view._find_module_interface_type_mismatches(second)] == [
                "Ethernet2"
            ]

        interface_selects = [
            query["sql"]
            for query in captured.captured_queries
            if 'FROM "dcim_interface"' in query["sql"] and query["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert len(interface_selects) == 1, interface_selects

    def test_a_row_built_without_a_request_reports_nothing(self):
        """The direct row-builder seam has no requester, so nothing can be permission-scoped."""
        from dcim.models import Interface

        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        device, module = _module_with_templates(
            "type-discovery-requestless",
            [("Ethernet1", "10gbase-x-sfpp")],
        )
        Interface.objects.create(device=device, module=module, name="Ethernet1", type="1000base-t")

        assert object.__new__(BaseModuleTableView)._find_module_interface_type_mismatches(module) == []

    def test_module_without_bound_interfaces_does_not_send_prediction_signal(self):
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.tests.conftest import make_superuser

        _device, module = _module_with_templates(
            "type-discovery-unbound",
            [("Ethernet1", "10gbase-x-sfpp")],
        )
        calls = []

        @receiver(predict_module_interface_names, dispatch_uid="test-type-discovery-unbound")
        def record_call(sender, device, module, names, **kwargs):
            calls.append(list(names))

        try:
            assert (
                self._view(make_superuser("type-discovery-unbound-user"))._find_module_interface_type_mismatches(module)
                == []
            )
            assert calls == []
        finally:
            predict_module_interface_names.disconnect(dispatch_uid="test-type-discovery-unbound")

    def test_vc_template_name_collision_reports_no_type_mismatch(self):
        from dcim.models import Interface, InterfaceTemplate

        from netbox_librenms_plugin.tests.conftest import make_superuser, make_virtual_chassis

        first = make_device_with_module_bays("type-discovery-collision-first", [])
        second = make_device_with_module_bays("type-discovery-collision-second", ["Slot 1"])
        make_virtual_chassis("type-discovery-collision-vc", first, second)
        module = install_module(second, "Slot 1", "TYPE-DISCOVERY-COLLISION-CARD")
        InterfaceTemplate.objects.create(
            module_type=module.module_type,
            name="Ethernet1/1",
            type="1000base-t",
        )
        InterfaceTemplate.objects.create(
            module_type=module.module_type,
            name="Ethernet2/1",
            type="10gbase-x-sfpp",
        )
        Interface.objects.create(
            device=second,
            module=module,
            name="Ethernet2/1",
            type="10gbase-x-sfpp",
        )

        assert (
            self._view(make_superuser("type-discovery-collision-user"))._find_module_interface_type_mismatches(module)
            == []
        )


class TestModuleInterfaceTypePreview:
    """The preview derives current differences from NetBox without inventory cache data."""

    def test_preview_lists_real_interface_and_template_types_without_inventory_cache(self, settings):
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_superuser
        from netbox_librenms_plugin.views.sync.modules import ModuleInterfaceTypePreviewView

        configure_default_librenms_server(settings)
        device, module = _module_with_templates(
            "type-preview",
            [("Ethernet1", "10gbase-x-sfpp")],
        )
        interface = Interface.objects.create(
            device=device,
            module=module,
            name="Ethernet1",
            type="1000base-t",
        )
        request = make_request(
            "get",
            {
                "server_key": "default",
                "selected_device_id": str(device.pk),
                "module_id": str(module.pk),
            },
            user=make_superuser("type-preview-user"),
        )

        response = view_get(ModuleInterfaceTypePreviewView(), request, pk=device.pk)

        assert response.status_code == 200
        content = response.content.decode()
        assert "Ethernet1" in content
        assert interface.get_type_display() in content
        assert "SFP+ (10GE)" in content
        assert f'name="interface_id" value="{interface.pk}" checked' in content
        assert f'name="current_type_{interface.pk}"' in content
        assert 'value="1000base-t"' in content
        assert f'name="template_type_{interface.pk}"' in content
        assert 'value="10gbase-x-sfpp"' in content

    def test_preview_requires_interface_view_permission(self, settings):
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import ModuleInterfaceTypePreviewView

        configure_default_librenms_server(settings)
        device, module = _module_with_templates(
            "type-preview-denied",
            [("Ethernet1", "10gbase-x-sfpp")],
        )
        Interface.objects.create(device=device, module=module, name="Ethernet1", type="1000base-t")
        user = make_user_with_perms(
            "type-preview-denied-user",
            [("view", Device), ("view", Module)],
        )
        request = make_request(
            "get",
            {
                "server_key": "default",
                "selected_device_id": str(device.pk),
                "module_id": str(module.pk),
            },
            user=user,
        )

        response = view_get(ModuleInterfaceTypePreviewView(), request, pk=device.pk)

        assert response.status_code == 302
        assert response.url == "/"
        assert any("Missing permissions" in text for text in message_texts(request))


def _vc_member_type_mismatch(tag):
    """Create a two-member VC with a rewritten template name on member two."""
    from dcim.models import Interface, InterfaceTemplate

    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis

    page_device = make_device_with_module_bays(f"{tag}-page", [])
    member = make_device_with_module_bays(f"{tag}-member", ["Slot 1"])
    make_virtual_chassis(f"{tag}-vc", page_device, member)
    module = install_module(member, "Slot 1", f"{tag.upper()}-CARD")
    InterfaceTemplate.objects.create(
        module_type=module.module_type,
        name="TenGigabitEthernet1/1/1",
        type="10gbase-x-sfpp",
    )
    interface = Interface.objects.create(
        device=member,
        module=module,
        name="TenGigabitEthernet2/1/1",
        type="1000base-t",
    )
    return page_device, member, module, interface


class TestApplyModuleInterfaceTypes:
    """The apply endpoint updates selected, unchanged interfaces with exact grants."""

    @staticmethod
    def _post(
        settings,
        user,
        page_device,
        member,
        module,
        interface,
        *,
        current_type="1000base-t",
        template_type="10gbase-x-sfpp",
        htmx=False,
    ):
        from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server
        from netbox_librenms_plugin.views.sync.modules import ApplyModuleInterfaceTypesView

        configure_default_librenms_server(settings)
        request = make_request(
            "post",
            {
                "server_key": "default",
                "selected_device_id": str(member.pk),
                "module_id": str(module.pk),
                "interface_id": [str(interface.pk)],
                f"current_type_{interface.pk}": current_type,
                f"template_type_{interface.pk}": template_type,
            },
            user=user,
            **({"HTTP_HX_REQUEST": "true"} if htmx else {}),
        )
        response = ApplyModuleInterfaceTypesView.as_view()(request, pk=page_device.pk)
        return request, response

    def test_real_post_updates_a_vc_member_interface_with_exact_permissions(self, settings):
        from dcim.models import Device, Interface, Module
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.view_test_helpers import assert_locked_before_update
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        page_device, member, module, interface = _vc_member_type_mismatch("type-apply-vc")
        user = make_user_with_perms(
            "type-apply-vc-user",
            [("view", Device), ("view", Module), ("change", Interface)],
        )

        with CaptureQueriesContext(connection) as captured:
            request, response = self._post(settings, user, page_device, member, module, interface)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.type == "10gbase-x-sfpp"
        assert message_texts(request, "success") == ["Updated the type of 1 interface from its module template."]
        assert_locked_before_update(captured, "dcim_interface")

    @pytest.mark.django_db(transaction=True)
    def test_type_update_invalidates_loaded_interface_snapshot(self, settings):
        from dcim.models import Device, Interface, Module
        from django.core.cache import cache

        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab
        from netbox_librenms_plugin.tests.cache_test_helpers import clear_snapshots, seed_every_tab
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.utils import set_librenms_device_id

        page_device, member, module, interface = _vc_member_type_mismatch("type-apply-cache")
        set_librenms_device_id(page_device, 179, "default")
        page_device.save(update_fields=["custom_field_data"])
        user = make_user_with_perms(
            "type-apply-cache-user",
            [("view", Device), ("view", Module), ("change", Interface)],
        )
        keys = seed_every_tab(page_device)
        interface_key = SyncCacheConsistency(page_device).snapshot_key(SyncTab.INTERFACES, "default")
        module_key = SyncCacheConsistency(page_device).snapshot_key(SyncTab.MODULES, "default")

        try:
            _request, response = self._post(settings, user, page_device, member, module, interface)

            interface.refresh_from_db()
            assert response.status_code == 302
            assert interface.type == "10gbase-x-sfpp"
            assert cache.get(interface_key) is None
            assert cache.get(module_key) is not None, "the source module snapshot was cleared before its transition"
            assert "X-LibreNMS-Cache-Transition" in response
        finally:
            clear_snapshots(keys)

    def test_post_without_change_interface_permission_changes_nothing(self, settings):
        from dcim.models import Device, Module

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        page_device, member, module, interface = _vc_member_type_mismatch("type-apply-denied")
        user = make_user_with_perms(
            "type-apply-denied-user",
            [("view", Device), ("view", Module)],
        )

        _request, response = self._post(settings, user, page_device, member, module, interface)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert response.url == "/"
        assert interface.type == "1000base-t"
        assert any("Missing permissions" in text for text in message_texts(_request))

    def test_type_update_cannot_leave_a_constrained_change_grant(self, settings):
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        page_device, member, module, interface = _vc_member_type_mismatch("type-apply-scope")
        user = make_user_with_perms(
            "type-apply-scope-user",
            [("view", Device), ("view", Module)],
        )
        user = grant(user, "change", Interface, constraints={"type": "1000base-t"})

        _request, response = self._post(settings, user, page_device, member, module, interface, htmx=True)

        interface.refresh_from_db()
        assert response.status_code == 200
        assert response["HX-Retarget"] == "#module-sync-content"
        assert interface.type == "1000base-t"
        assert (
            f"No interface types were changed. The updated interface is outside your change permission scope: "
            f"{interface.name}."
        ) in response.content.decode()
        assert "X-LibreNMS-Cache-Transition" not in response

    def test_changed_current_type_is_skipped(self, settings):
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        page_device, member, module, interface = _vc_member_type_mismatch("type-apply-race")
        Interface.objects.filter(pk=interface.pk).update(type="other")
        user = make_user_with_perms(
            "type-apply-race-user",
            [("view", Device), ("view", Module), ("change", Interface)],
        )

        request, response = self._post(settings, user, page_device, member, module, interface)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.type == "other"
        assert message_texts(request, "warning") == [
            "Skipped 1 interface because it changed after the preview: TenGigabitEthernet2/1/1."
        ]

    def test_unrelated_invalid_field_does_not_block_type_update(self, settings):
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        page_device, member, module, interface = _vc_member_type_mismatch("type-apply-narrow")
        Interface.objects.filter(pk=interface.pk).update(mtu=0)
        user = make_user_with_perms(
            "type-apply-narrow-user",
            [("view", Device), ("view", Module), ("change", Interface)],
        )

        _request, response = self._post(settings, user, page_device, member, module, interface)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.type == "10gbase-x-sfpp"
        assert interface.mtu == 0

    def test_template_type_changed_after_preview_is_skipped(self, settings):
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        page_device, member, module, interface = _vc_member_type_mismatch("type-apply-template-changed")
        module.module_type.interfacetemplates.update(type="100gbase-x-qsfp28")
        user = make_user_with_perms(
            "type-apply-template-changed-user",
            [("view", Device), ("view", Module), ("change", Interface)],
        )

        request, response = self._post(settings, user, page_device, member, module, interface)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.type == "1000base-t"
        assert message_texts(request, "warning") == [
            "Skipped 1 interface because its template type changed after the preview: TenGigabitEthernet2/1/1."
        ]

    def test_virtual_template_type_is_rejected_for_a_lag_member(self, settings):
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.conftest import make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        page_device, member, module, interface = _vc_member_type_mismatch("type-apply-invalid-virtual")
        module.module_type.interfacetemplates.update(type="virtual")
        aggregate = make_interface(member, "Port-Channel1", iface_type="lag")
        interface.lag = aggregate
        interface.save(update_fields=["lag"])
        user = make_user_with_perms(
            "type-apply-invalid-virtual-user",
            [("view", Device), ("view", Module), ("change", Interface)],
        )

        request, response = self._post(
            settings,
            user,
            page_device,
            member,
            module,
            interface,
            template_type="virtual",
        )

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.type == "1000base-t"
        assert message_texts(request, "warning") == [
            "Skipped TenGigabitEthernet2/1/1 because Virtual interfaces cannot have a parent LAG interface."
        ]

    def test_validation_refusal_does_not_roll_back_a_valid_interface(self, settings):
        from dcim.models import Device, Interface, InterfaceTemplate, Module

        from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import ApplyModuleInterfaceTypesView

        page_device, member, module, valid_interface = _vc_member_type_mismatch("type-apply-partial")
        InterfaceTemplate.objects.create(
            module_type=module.module_type,
            name="TenGigabitEthernet1/1/2",
            type="virtual",
        )
        aggregate = make_interface(member, "Port-Channel1", iface_type="lag")
        invalid_interface = Interface.objects.create(
            device=member,
            module=module,
            name="TenGigabitEthernet2/1/2",
            type="1000base-t",
            lag=aggregate,
        )
        user = make_user_with_perms(
            "type-apply-partial-user",
            [("view", Device), ("view", Module), ("change", Interface)],
        )
        configure_default_librenms_server(settings)
        request = make_request(
            "post",
            {
                "server_key": "default",
                "selected_device_id": str(member.pk),
                "module_id": str(module.pk),
                "interface_id": [str(valid_interface.pk), str(invalid_interface.pk)],
                f"current_type_{valid_interface.pk}": "1000base-t",
                f"template_type_{valid_interface.pk}": "10gbase-x-sfpp",
                f"current_type_{invalid_interface.pk}": "1000base-t",
                f"template_type_{invalid_interface.pk}": "virtual",
            },
            user=user,
        )

        response = view_post(ApplyModuleInterfaceTypesView(), request, pk=page_device.pk)

        valid_interface.refresh_from_db()
        invalid_interface.refresh_from_db()
        assert response.status_code == 302
        assert valid_interface.type == "10gbase-x-sfpp"
        assert invalid_interface.type == "1000base-t"
        assert message_texts(request, "success") == ["Updated the type of 1 interface from its module template."]
        assert message_texts(request, "warning") == [
            "Skipped TenGigabitEthernet2/1/2 because Virtual interfaces cannot have a parent LAG interface."
        ]

    def test_interface_bound_to_another_module_is_skipped(self, settings):
        from dcim.models import Device, Interface, InterfaceTemplate, Module

        from netbox_librenms_plugin.tests.conftest import make_module_bay
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        page_device, member, first_module, interface = _vc_member_type_mismatch("type-apply-other-module")
        make_module_bay(member, "Slot 2")
        second_module = install_module(member, "Slot 2", "TYPE-APPLY-OTHER-SECOND-CARD")
        InterfaceTemplate.objects.create(
            module_type=second_module.module_type,
            name="TenGigabitEthernet1/1/1",
            type="10gbase-x-sfpp",
        )
        user = make_user_with_perms(
            "type-apply-other-module-user",
            [("view", Device), ("view", Module), ("change", Interface)],
        )

        request, response = self._post(settings, user, page_device, member, second_module, interface)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.module_id == first_module.pk
        assert interface.type == "1000base-t"
        assert message_texts(request, "warning") == [
            "Skipped 1 selected interface because it is unavailable for this module."
        ]

    def test_interface_outside_the_change_grant_is_skipped(self, settings):
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        page_device, member, module, interface = _vc_member_type_mismatch("type-apply-change-scope")
        user = make_user_with_perms(
            "type-apply-change-scope-user",
            [("view", Device), ("view", Module)],
        )
        user = grant(user, "change", Interface, constraints={"name": "Management1"})

        request, response = self._post(settings, user, page_device, member, module, interface)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.type == "1000base-t"
        assert message_texts(request, "warning") == [
            "Skipped 1 selected interface because it is unavailable for this module."
        ]
