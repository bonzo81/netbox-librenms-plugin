"""Step 1 smoke tests — verify view class wiring (mixins, MRO, key attributes).

These tests never touch the database or network; they only inspect class
hierarchies and attribute presence.
"""

import os
from pathlib import Path

import pytest


class TestLibreNMSAPIMixinWiring:
    """Views that need LibreNMSAPIMixin must have it in their MRO."""

    def _assert_has_api_mixin(self, view_class):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        assert LibreNMSAPIMixin in view_class.__mro__, f"{view_class.__name__} is missing LibreNMSAPIMixin in its MRO"

    def test_sync_site_location_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        self._assert_has_api_mixin(SyncSiteLocationView)

    def test_add_device_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        self._assert_has_api_mixin(AddDeviceToLibreNMSView)

    def test_update_location_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        self._assert_has_api_mixin(UpdateDeviceLocationView)

    def test_update_device_name_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        self._assert_has_api_mixin(UpdateDeviceNameView)

    def test_update_device_serial_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView

        self._assert_has_api_mixin(UpdateDeviceSerialView)

    def test_update_device_type_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceTypeView

        self._assert_has_api_mixin(UpdateDeviceTypeView)

    def test_update_device_platform_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDevicePlatformView

        self._assert_has_api_mixin(UpdateDevicePlatformView)

    def test_create_assign_platform_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        self._assert_has_api_mixin(CreateAndAssignPlatformView)

    def test_assign_vc_serial_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import AssignVCSerialView

        self._assert_has_api_mixin(AssignVCSerialView)


class TestCacheMixinWiring:
    """Views that cache LibreNMS data must have CacheMixin and expose get_cache_key."""

    def _assert_has_cache_mixin(self, view_class):
        from netbox_librenms_plugin.views.mixins import CacheMixin

        assert CacheMixin in view_class.__mro__, f"{view_class.__name__} is missing CacheMixin"
        assert hasattr(view_class, "get_cache_key"), f"{view_class.__name__} missing get_cache_key method"

    def test_sync_interfaces_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        self._assert_has_cache_mixin(SyncInterfacesView)

    def test_sync_cables_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        self._assert_has_cache_mixin(SyncCablesView)

    def test_sync_ip_addresses_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        self._assert_has_cache_mixin(SyncIPAddressesView)

    def test_sync_vlans_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        self._assert_has_cache_mixin(SyncVLANsView)

    def test_delete_interfaces_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        self._assert_has_cache_mixin(DeleteNetBoxInterfacesView)


class TestPermissionMixinWiring:
    """All action views must have LibreNMSPermissionMixin."""

    def _assert_has_permission_mixin(self, view_class):
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        assert LibreNMSPermissionMixin in view_class.__mro__, (
            f"{view_class.__name__} is missing LibreNMSPermissionMixin"
        )

    def test_sync_interfaces_has_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        self._assert_has_permission_mixin(SyncInterfacesView)

    def test_sync_cables_has_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        self._assert_has_permission_mixin(SyncCablesView)

    def test_add_device_has_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        self._assert_has_permission_mixin(AddDeviceToLibreNMSView)


class TestRequiredObjectPermissionsWiring:
    """POST-only sync views that modify NetBox objects must declare required_object_permissions
    and include the NetBoxObjectPermissionMixin (and LibreNMSPermissionMixin) in their MRO."""

    def _assert_has_mixins(self, view_class):
        """Assert that *view_class* includes both permission mixins in its MRO.

        Checking the MRO (not just runtime behaviour) guarantees that the permission
        enforcement is wired at the class level — a missing mixin would silently skip
        all permission checks even if the tests otherwise pass.
        """
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin, NetBoxObjectPermissionMixin

        assert NetBoxObjectPermissionMixin in view_class.__mro__, (
            f"{view_class.__name__} is missing NetBoxObjectPermissionMixin"
        )
        assert LibreNMSPermissionMixin in view_class.__mro__, (
            f"{view_class.__name__} is missing LibreNMSPermissionMixin"
        )

    def test_sync_interfaces_has_required_object_permissions(self):
        from dcim.models import Interface
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        self._assert_has_mixins(SyncInterfacesView)
        view = object.__new__(SyncInterfacesView)
        # Dynamic views compute permissions per-request; verify the resolver works
        perms_device = view.get_required_permissions_for_object_type("device")
        perms_vm = view.get_required_permissions_for_object_type("virtualmachine")

        assert ("add", Interface) in perms_device
        assert ("change", Interface) in perms_device
        assert ("add", VMInterface) in perms_vm
        assert ("change", VMInterface) in perms_vm

    def test_sync_cables_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        self._assert_has_mixins(SyncCablesView)
        assert "POST" in SyncCablesView.required_object_permissions

    def test_sync_vlans_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        self._assert_has_mixins(SyncVLANsView)
        assert "POST" in SyncVLANsView.required_object_permissions

    def test_sync_ip_addresses_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        self._assert_has_mixins(SyncIPAddressesView)
        assert "POST" in SyncIPAddressesView.required_object_permissions

    def test_update_device_name_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        self._assert_has_mixins(UpdateDeviceNameView)
        assert "POST" in UpdateDeviceNameView.required_object_permissions

    def test_update_device_serial_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView

        self._assert_has_mixins(UpdateDeviceSerialView)
        assert "POST" in UpdateDeviceSerialView.required_object_permissions

    def test_delete_interfaces_has_required_object_permissions(self):
        from dcim.models import Interface
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        self._assert_has_mixins(DeleteNetBoxInterfacesView)
        view = object.__new__(DeleteNetBoxInterfacesView)
        # Dynamic views compute permissions per-request; verify the resolver works
        perms_device = view.get_required_permissions_for_object_type("device")
        perms_vm = view.get_required_permissions_for_object_type("virtualmachine")

        assert ("delete", Interface) in perms_device
        assert ("delete", VMInterface) in perms_vm


class TestViewPropertyLazyInit:
    """Verify that _librenms_api starts as None (lazy, not eager-init) and that
    the librenms_api property descriptor exists on the class."""

    def test_librenms_api_mixin_property_is_defined_on_class(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        assert isinstance(LibreNMSAPIMixin.__dict__.get("librenms_api"), property), (
            "librenms_api must be a property descriptor on LibreNMSAPIMixin"
        )

    def test_librenms_api_starts_as_none_after_mixin_init(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        class DummyView(LibreNMSAPIMixin):
            pass

        dummy = DummyView()
        # After init, the backing attribute must be None (lazy, not eager)
        assert dummy._librenms_api is None

    def test_sync_interfaces_has_librenms_api_property_via_class(self):
        """BaseLibreNMSSyncView must expose librenms_api through its MRO.

        SyncInterfacesView gains LibreNMSAPIMixin in the view-fixes PR; on the
        current upstream/develop baseline we verify the property via
        BaseLibreNMSSyncView, which inherits the mixin unconditionally.
        """
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        assert any("librenms_api" in vars(cls) for cls in BaseLibreNMSSyncView.__mro__)


# ── Template syntax smoke tests ──────────────────────────────────────────────

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "netbox_librenms_plugin"
_TEMPLATE_FILES = sorted(_TEMPLATE_DIR.rglob("*.html"))


class TestTemplateSyntax:
    """Compile every plugin template to catch syntax errors early."""

    @pytest.fixture(autouse=True, scope="class")
    def _django_engine(self):
        """Ensure Django is set up once and expose the template engine."""
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "netbox.settings")
        import django

        django.setup()
        from django.template import engines

        self.__class__._engine = engines["django"]

    @pytest.mark.parametrize(
        "template_path",
        _TEMPLATE_FILES,
        ids=[str(p.relative_to(_TEMPLATE_DIR)) for p in _TEMPLATE_FILES],
    )
    def test_template_compiles(self, template_path):
        """Each template must parse without TemplateSyntaxError."""
        source = template_path.read_text()
        # Compile the template — raises TemplateSyntaxError on bad tags
        self._engine.from_string(source)


class TestRenderDeviceSelectionEscape:
    """VCCableTable.render_device_selection must HTML-escape member.name."""

    def test_member_name_is_escaped(self):
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.tables.cables import VCCableTable

        device = MagicMock()
        device.id = 1
        vc = MagicMock()
        member = MagicMock()
        member.id = 1
        member.name = '<script>alert("xss")</script>'
        vc.members.all.return_value = [member]
        device.virtual_chassis = vc

        table = VCCableTable([], device=device)
        record = {"local_port": "eth0", "local_port_id": "42"}

        with patch(
            "netbox_librenms_plugin.tables.cables.get_virtual_chassis_member",
            return_value=member,
        ):
            html = str(table.render_device_selection(None, record))

        assert "<script>" not in html
        assert "&lt;script&gt;" in html
