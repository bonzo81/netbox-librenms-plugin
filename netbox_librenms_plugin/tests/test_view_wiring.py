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


class TestGatedViewsResolveThroughRestrictedQuerysets:
    """A gated view must not resolve an object by raw pk.

    NetBoxObjectPermissionMixin asks ``has_perm`` WITHOUT an instance, so a CONSTRAINED grant (a
    site-scoped change_device, say) clears the gate; a plain ``get_object_or_404`` behind it then
    reads or writes an object outside that grant. This is a RECURRING defect class — the same
    finding has landed on the cable remote picker, the LAG/parent relationship sync, the
    move-to-winner endpoints and the LibreNMS location push — so it is enforced mechanically here
    instead of case by case.

    "Gated" means the class declares ``required_object_permissions`` (by assignment or annotation,
    statically or per-request) OR calls one of the ``require_*_permission(s)`` gates: a view gated
    only by the plugin write permission reaches objects by raw pk just as easily.

    Scope and limits: a raw lookup inherited from an ungated base class is NOT seen, and neither is
    ``Model.objects.get(pk=...)`` / ``.filter(pk=...)`` — the secondary-lookup form. Both remain a
    review matter.
    """

    GATE_CALLS = frozenset(
        {
            "require_write_permission",
            "require_write_permission_json",
            "require_object_permissions",
            "require_object_permissions_json",
            "require_all_permissions",
            "require_all_permissions_json",
        }
    )

    @classmethod
    def _scan_tree(cls, tree, label):
        """Return {(label, class, line)} for gated classes in *tree* that resolve by raw pk."""
        import ast

        def _is_scoped(node):
            """Whether the first get_object_or_404 argument came from restricted_queryset(...)."""
            # Accepts the chained form too: self.restricted_queryset(Module).select_related(...).
            return any(
                isinstance(inner, ast.Call) and getattr(inner.func, "attr", "") == "restricted_queryset"
                for inner in ast.walk(node)
            )

        def _declares_gate(class_node):
            for sub in ast.walk(class_node):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if getattr(target, "id", getattr(target, "attr", "")) == "required_object_permissions":
                            return True
                elif isinstance(sub, ast.AnnAssign):
                    target = sub.target
                    if getattr(target, "id", getattr(target, "attr", "")) == "required_object_permissions":
                        return True
                elif isinstance(sub, ast.Call) and getattr(sub.func, "attr", "") in cls.GATE_CALLS:
                    return True
            return False

        offenders = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _declares_gate(node):
                continue
            # The mixin that DEFINES restrict_object_or_404 is where the raw call belongs.
            if any(isinstance(sub, ast.FunctionDef) and sub.name == "restrict_object_or_404" for sub in node.body):
                continue
            for sub in ast.walk(node):
                if not (isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "get_object_or_404"):
                    continue
                if not (sub.args and _is_scoped(sub.args[0])):
                    offenders.add((label, node.name, sub.lineno))
        return offenders

    @classmethod
    def _scan(cls):
        """Run :meth:`_scan_tree` over every view module."""
        import ast
        import pathlib

        import netbox_librenms_plugin

        views_root = pathlib.Path(netbox_librenms_plugin.__file__).parent / "views"
        offenders = set()
        for path in sorted(views_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            offenders |= cls._scan_tree(tree, str(path.relative_to(views_root)))
        return offenders

    def test_no_gated_view_resolves_an_object_by_raw_pk(self):
        """Every gated view resolves through restrict_object_or_404 / restricted_queryset, never a raw pk lookup."""
        offenders = sorted(self._scan())
        assert not offenders, (
            "gated view(s) resolving an object by raw pk — a constrained grant clears the gate and "
            f"then reaches objects outside it; use restrict_object_or_404: {offenders}"
        )

    def test_the_scan_flags_a_raw_lookup(self):
        """Guard the guard: the REAL scan must still flag the pattern it exists to catch."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            "        return get_object_or_404(Device, pk=pk)\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>"), "the scan no longer flags a raw pk lookup"

    def test_the_scan_flags_a_view_gated_only_by_the_write_permission(self):
        """A view gated by require_write_permission alone reaches objects by raw pk just as easily."""
        import ast

        source = (
            "class V:\n"
            "    def post(self, request, pk):\n"
            "        if error := self.require_write_permission():\n"
            "            return error\n"
            "        return get_object_or_404(Device, pk=pk)\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>"), "a write-gated raw lookup must be flagged"

    def test_the_scan_accepts_a_scoped_lookup(self):
        """The positive control: a lookup already routed through restricted_queryset is not flagged."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            "        return get_object_or_404(self.restricted_queryset(Device), pk=pk)\n"
        )
        assert not self._scan_tree(ast.parse(source), "<fixture>")


@pytest.mark.django_db
class TestGatedViewsRefuseOutOfScopeObjects:
    """The behavioural half of the guard above: a CONSTRAINED grant must not reach another object.

    One representative view per family (device-field write, owner-scoped sync, module install) is
    driven through the REAL gate and the REAL restrict(), so the structural scan cannot pass while
    the runtime behaviour is broken.
    """

    @staticmethod
    def _user(username, model_grants):
        """A real non-superuser with plugin write plus ``model_grants`` = [(model, action, constraints)]."""
        from core.models import ObjectType
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        LibreNMSSettings = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")

        user = get_user_model().objects.create_user(username=username, password="x")
        plugin = ObjectPermission.objects.create(name=f"{username}-plugin", actions=["view", "change"])
        plugin.object_types.set([ObjectType.objects.get_for_model(LibreNMSSettings)])
        plugin.users.set([user])
        for i, (model, action, constraints) in enumerate(model_grants):
            perm = ObjectPermission.objects.create(
                name=f"{username}-{action}-{i}", actions=[action], constraints=constraints
            )
            perm.object_types.set([ObjectType.objects.get_for_model(model)])
            perm.users.set([user])
        return get_user_model().objects.get(pk=user.pk)  # reload to clear the perm cache

    @staticmethod
    def _request(user, data, method="post"):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        factory = RequestFactory()
        request = getattr(factory, method)("/x/", data)
        request.user = user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def test_device_field_write_404s_a_device_outside_the_grant(self):
        """UpdateDeviceNameView writes the device itself, so a pk-constrained change_device must not reach another one."""
        from dcim.models import Device
        from django.http import Http404

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        in_scope = make_device("scope-devfield-in")
        out_of_scope = make_device("scope-devfield-out")
        original_name = out_of_scope.name
        user = self._user("scope-devfield", [(Device, "change", {"pk": in_scope.pk})])

        view = UpdateDeviceNameView()
        request = self._request(user, {"server_key": "default"})
        view.setup(request)
        with pytest.raises(Http404):
            view.post(request, pk=out_of_scope.pk)

        out_of_scope.refresh_from_db()
        assert out_of_scope.name == original_name  # untouched

    def test_ip_sync_404s_an_owner_outside_the_grant(self):
        """SyncIPAddressesView resolves the owner device by URL pk; a scoped view_device must not reach another."""
        from dcim.models import Device
        from django.http import Http404
        from ipam.models import IPAddress

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        in_scope = make_device("scope-ipsync-in")
        out_of_scope = make_device("scope-ipsync-out")
        user = self._user(
            "scope-ipsync",
            [
                (Device, "view", {"pk": in_scope.pk}),
                (IPAddress, "add", None),
                (IPAddress, "change", None),
            ],
        )

        view = SyncIPAddressesView()
        request = self._request(user, {"server_key": "default", "select": ["10.0.0.1"]})
        view.setup(request)
        with pytest.raises(Http404):
            view.post(request, object_type="device", pk=out_of_scope.pk)

    def test_module_install_404s_a_page_device_outside_the_grant(self):
        """InstallModuleView resolves the page device by URL pk before touching modules."""
        from dcim.models import Device, Interface, Module
        from django.http import Http404

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        in_scope = make_device("scope-modinstall-in")
        out_of_scope = make_device("scope-modinstall-out")
        user = self._user(
            "scope-modinstall",
            [
                (Device, "view", {"pk": in_scope.pk}),
                (Module, "add", None),
                (Interface, "add", None),
                (Interface, "change", None),
                (Interface, "delete", None),
            ],
        )

        view = InstallModuleView()
        request = self._request(user, {"server_key": "default"})
        view.setup(request)
        with pytest.raises(Http404):
            view.post(request, pk=out_of_scope.pk)

    def test_location_push_404s_a_device_outside_the_grant(self):
        """UpdateDeviceLocationView is gated by the plugin write permission and reads the device by URL pk, so it must scope that read too."""
        from dcim.models import Device
        from django.http import Http404

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        in_scope = make_device("scope-loc-in")
        out_of_scope = make_device("scope-loc-out")
        user = self._user("scope-loc", [(Device, "view", {"pk": in_scope.pk})])

        view = UpdateDeviceLocationView()
        request = self._request(user, {"server_key": "default"})
        view.setup(request)
        with pytest.raises(Http404):
            view.post(request, pk=out_of_scope.pk)

    def test_module_move_refuses_a_conflict_module_outside_the_grant(self):
        """MoveModuleView reassigns the conflict module's bay/device, and its pk comes from the POST — a secondary lookup the primary scoping does not cover."""
        from dcim.models import Device, Module, ModuleBay, ModuleType
        from dcim.models import Manufacturer

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.modules import MoveModuleView

        page_device = make_device("scope-move-page")
        other_device = make_device("scope-move-other")
        mfr, _ = Manufacturer.objects.get_or_create(name="Mfr-scope-move", slug="mfr-scope-move")
        mtype = ModuleType.objects.create(manufacturer=mfr, model="MT-scope-move")
        target_bay = ModuleBay.objects.create(device=page_device, name="Slot 1")
        foreign_bay = ModuleBay.objects.create(device=other_device, name="Slot 9")
        # The module the caller names lives on a device the grant does NOT cover.
        foreign_module = Module.objects.create(device=other_device, module_bay=foreign_bay, module_type=mtype)

        user = self._user(
            "scope-move",
            [
                (Device, "view", {"pk": page_device.pk}),
                (ModuleBay, "view", None),  # so the run reaches the conflict-module lookup under test
                (Module, "change", {"device__name": "scope-move-page"}),
                (Module, "delete", {"device__name": "scope-move-page"}),
            ],
        )
        view = MoveModuleView()
        request = self._request(
            user,
            {
                "server_key": "default",
                "conflict_module_id": str(foreign_module.pk),
                "target_bay_id": str(target_bay.pk),
            },
        )
        view.setup(request)
        view.post(request, pk=page_device.pk)

        foreign_module.refresh_from_db()
        assert foreign_module.device_id == other_device.pk  # not moved
        assert foreign_module.module_bay_id == foreign_bay.pk

    def test_vc_serial_assign_refuses_a_member_outside_the_grant(self):
        """AssignVCSerialView overwrites the member's serial and takes its pk from the POST, guarded only by same-VC membership."""
        from dcim.models import Device

        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.device_fields import AssignVCSerialView

        vc = VirtualChassis.objects.create(name="VC-scope-vcser")
        page_member = make_device("scope-vcser-1")
        sibling = make_device("scope-vcser-2")
        for position, member in ((1, page_member), (2, sibling)):
            member.virtual_chassis = vc
            member.vc_position = position
            member.save()
        vc.master = page_member
        vc.save()
        sibling.serial = "ORIGINAL"
        sibling.save()

        user = self._user("scope-vcser", [(Device, "change", {"pk": page_member.pk})])
        view = AssignVCSerialView()
        request = self._request(
            user,
            {"server_key": "default", "member_id_0": str(sibling.pk), "serial_0": "HIJACKED"},
        )
        view.setup(request)
        view.post(request, pk=page_member.pk)

        sibling.refresh_from_db()
        assert sibling.serial == "ORIGINAL"  # a sibling outside the grant keeps its serial

    def test_in_scope_object_still_resolves(self):
        """The device the grant DOES cover passes the lookup — the scoping must not over-block."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        device = make_device("scope-devfield-ok")
        user = self._user("scope-devfield-ok", [(Device, "change", {"pk": device.pk})])

        view = UpdateDeviceNameView()
        request = self._request(user, {"server_key": "default"})
        view.setup(request)
        # No LibreNMS mapping on this device, so the view refuses on its MERITS (a flash message +
        # redirect), not with a 404 at the lookup — proof the restricted queryset resolved it.
        response = view.post(request, pk=device.pk)
        assert response.status_code in (200, 302)


class TestCacheKeysAreServerScoped:
    """Every production cache key is namespaced by the LibreNMS server it belongs to.

    Multi-server scoping is the most repeated finding class in this stack's review history: a
    reader or writer that drops ``server_key`` silently addresses the DEFAULT server's namespace,
    so a refresh on server B renders an empty table, or one server's snapshot lands where another
    server's readers look. Every site was fixed one at a time; this keeps the class from returning.

    The helpers take ``server_key`` last, so a call is scoped when it passes the keyword or enough
    positional arguments to reach it.

    Scope and limits: only direct attribute calls are matched, so a helper passed as a callable and
    invoked under a local name (``modules.py`` hands ``self.get_cache_key`` to
    ``_resolve_single_install_binding_item``) escapes the scan, and a call forwarding ``**kwargs``
    is taken on trust — its contents are not inspected. Both remain a review matter.
    """

    # helper name -> number of positional args needed to reach server_key
    HELPERS = {"get_cache_key": 3, "get_last_fetched_key": 3, "get_vlan_overrides_key": 2}

    @classmethod
    def _scan(cls):
        """Return ["<file>:<line> <helper>"] for production cache-key calls with no server_key."""
        import ast
        import pathlib

        import netbox_librenms_plugin

        package_root = pathlib.Path(netbox_librenms_plugin.__file__).parent
        unscoped = []
        for path in sorted(package_root.rglob("*.py")):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                helper = getattr(node.func, "attr", "")
                if helper not in cls.HELPERS:
                    continue
                # kw.arg is None for **kwargs forwarding, which may carry server_key: not a miss.
                keyed = (
                    any(kw.arg in ("server_key", None) for kw in node.keywords) or len(node.args) >= cls.HELPERS[helper]
                )
                if not keyed:
                    unscoped.append(f"{path.relative_to(package_root)}:{node.lineno} {helper}")
        return unscoped

    def test_every_cache_key_call_passes_a_server_key(self):
        """A cache key built without server_key addresses the default server's namespace."""
        unscoped = self._scan()
        assert not unscoped, (
            "cache key(s) built without a server_key — they address the default server's namespace, "
            f"so another server's readers never see the entry: {unscoped}"
        )

    def test_the_helpers_still_take_server_key_last(self):
        """Guard the guard: the positional-arity assumption above must match the real signatures."""
        import inspect

        from netbox_librenms_plugin.views.mixins import CacheMixin

        for helper, position in self.HELPERS.items():
            params = list(inspect.signature(getattr(CacheMixin, helper)).parameters)
            assert params[-1] == "server_key", f"{helper} no longer takes server_key last: {params}"
            # params includes self, so the positional count to reach server_key is len(params) - 1.
            assert len(params) - 1 == position, f"{helper} arity changed — update HELPERS: {params}"
