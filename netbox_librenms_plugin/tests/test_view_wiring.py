"""
Step 1 smoke tests — verify view class wiring (mixins, MRO, key attributes).

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

    def test_convert_legacy_id_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        self._assert_has_api_mixin(ConvertLegacyLibreNMSIdView)


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
    """
    POST-only sync views that modify NetBox objects must declare required_object_permissions
    and include the NetBoxObjectPermissionMixin (and LibreNMSPermissionMixin) in their MRO."""

    def _assert_has_mixins(self, view_class):
        """
        Assert that *view_class* includes both permission mixins in its MRO.

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

    def test_remove_server_mapping_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        self._assert_has_mixins(RemoveServerMappingView)
        assert "POST" in RemoveServerMappingView.required_object_permissions

    def test_single_cable_verify_has_object_permission_gate(self):
        """The read-only verify-cable endpoint exposes device cable/topology rows, so it must gate on dcim.view_device like the interface/module verify views (object-permission mixin in MRO + declared perms)."""
        from dcim.models import Device

        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        assert NetBoxObjectPermissionMixin in SingleCableVerifyView.__mro__
        assert ("view", Device) in SingleCableVerifyView.required_object_permissions.get("POST", [])

    def test_convert_legacy_id_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        self._assert_has_mixins(ConvertLegacyLibreNMSIdView)
        assert "POST" in ConvertLegacyLibreNMSIdView.required_object_permissions

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

    def test_verify_views_have_object_permission_mixin_and_perms(self):
        """Read-only verify endpoints must wire NetBoxObjectPermissionMixin so their declared gate enforces instead of raising AttributeError (a missing mixin 500s; mock-based perm tests mask it)."""
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView
        from netbox_librenms_plugin.views.object_sync.devices import (
            SingleInterfaceVerifyView,
            SingleModuleVerifyView,
            SingleVlanGroupVerifyView,
            VerifyVlanSyncGroupView,
        )

        for view_class in (
            SingleInterfaceVerifyView,
            SingleModuleVerifyView,
            SingleVlanGroupVerifyView,
            VerifyVlanSyncGroupView,
            SingleIPAddressVerifyView,
        ):
            self._assert_has_mixins(view_class)
            assert "POST" in view_class.required_object_permissions, (
                f"{view_class.__name__} must declare a POST object-permission requirement"
            )

    def test_single_ipaddress_verify_has_object_permission_gate(self):
        """The read-only verify-ipaddress endpoint resolves an arbitrary device_id and returns its data, so it must gate on dcim.view_device like the other verify views."""
        from dcim.models import Device

        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        assert NetBoxObjectPermissionMixin in SingleIPAddressVerifyView.__mro__
        assert ("view", Device) in SingleIPAddressVerifyView.required_object_permissions.get("POST", [])


class TestViewPropertyLazyInit:
    """
    Verify that _librenms_api starts as None (lazy, not eager-init) and that
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


class TestSingleCableVerifyServerKey:
    """SingleCableVerifyView.post() must thread server_key from the POST body into VC resolution + cache key."""

    @staticmethod
    def _vc_device(tag):
        """A real Device that belongs to a VirtualChassis (so post() takes the VC sync-resolution branch)."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site, VirtualChassis

        mfr, _ = Manufacturer.objects.get_or_create(name=f"SkMfr-{tag}", slug=f"skmfr-{tag}")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=f"SkDT-{tag}", slug=f"skdt-{tag}")
        role, _ = DeviceRole.objects.get_or_create(name="SkRole", slug="skrole")
        site, _ = Site.objects.get_or_create(name="SkSite", slug="sksite")
        vc = VirtualChassis.objects.create(name=f"SkVC-{tag}")
        return Device.objects.create(
            name=f"sk-{tag}", device_type=dt, role=role, site=site, status="active", virtual_chassis=vc, vc_position=1
        )

    @staticmethod
    def _view_and_request(device, body, *, api_server_key):
        """Real view + real superuser request; _librenms_api is stubbed only to supply the active-server key."""
        import json
        from unittest.mock import MagicMock

        from django.contrib.auth import get_user_model
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = SingleCableVerifyView()
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = api_server_key  # config boundary: the active-server fallback
        request = RequestFactory().post("/verify-cable/", data=json.dumps(body), content_type="application/json")
        request.user = get_user_model().objects.create_superuser(username=f"sk-{device.pk}", email="", password="x")
        view.request = request
        view.kwargs = {}
        view.args = ()
        return view, request

    @pytest.mark.django_db
    def test_server_key_used_for_cache_lookup(self):
        """The POSTed server_key is threaded into get_librenms_sync_device and the (real) cache key."""
        from unittest.mock import patch

        device = self._vc_device("used")
        view, request = self._view_and_request(
            device,
            {"device_id": device.pk, "local_port_id": "42", "server_key": "production"},
            api_server_key="default-server",
        )

        with (
            # The posted key is honoured only when it names a configured server; post() checks the
            # LibreNMSAPI.get_available_servers() CLASSMETHOD (not the instance).
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"production": "Production"},
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=device
            ) as mock_sync_device,
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
        ):
            mock_cache.get.return_value = None  # no cached data -> early return once the key is built
            view.post(request)

            # get_librenms_sync_device gets the posted server_key (device compares by pk via Model.__eq__)
            mock_sync_device.assert_called_once_with(device, server_key="production")
            # the real cache key also carries the posted server_key (not the active default)
            cache_key_arg = mock_cache.get.call_args[0][0]
            assert "production" in cache_key_arg

    @pytest.mark.django_db
    def test_fallback_to_api_server_key(self):
        """With no server_key in the POST body, post() falls back to the active-server key."""
        from unittest.mock import patch

        device = self._vc_device("fallback")
        view, request = self._view_and_request(
            device, {"device_id": device.pk, "local_port_id": "42"}, api_server_key="fallback-server"
        )

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                side_effect=lambda dev, **kw: dev,
            ) as mock_sync_device,
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            view.post(request)

            mock_sync_device.assert_called_once()
            assert mock_sync_device.call_args[1]["server_key"] == "fallback-server"
            cache_key_arg = mock_cache.get.call_args[0][0]
            assert "fallback-server" in cache_key_arg
