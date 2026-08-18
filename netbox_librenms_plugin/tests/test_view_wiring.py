"""
Step 1 smoke tests — verify view class wiring (mixins, MRO, key attributes).

These tests never touch the database or network; they only inspect class
hierarchies and attribute presence.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

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

    Two forms are flagged: the PRIMARY lookup (``get_object_or_404(Model, pk=...)``) and the
    SECONDARY one (``Model.objects.get(pk=...)`` / ``.filter(pk=...)``), which is where the same
    defect kept reappearing after the primary lookups were scoped — on the module move/serial
    endpoints, the interface delete targets and the OOB interface reuse.

    What counts is where the id came from, and a deliberate re-lock must SAY so: call
    ``relock_scoped_row(Model, pk=donor.oob_ip_id)``. That is not a ``<Model>.objects`` chain, so it
    never reaches this rule, and every call is greppable. Scoping happened where the source object
    was resolved, and restricting the re-read would instead demand a permission the view's gate
    never required: ``restrict()`` returns ``none()`` for a user who lacks the model-level grant, so
    a change-only caller would silently lose rows out of a lock set and be told the object "no
    longer exists".

    Until 2026-08 the rule instead exempted any ``pk=<expr>.<name>_id``. That read a SPELLING as
    provenance: it silenced a legitimate re-lock keyed by a local, while waving through
    ``Device.objects.get(pk=payload.device_id)`` on a request-derived object. Both directions were
    wrong, so the heuristic is gone.

    Scope and limits, stated because this rule is a lint and not a proof: a class gated only
    through an INHERITED base is not seen (that is how the routed sync pages resolved any device by
    pk; :class:`TestRoutedSyncPagesScopeTheirObject` now covers them behaviourally); module-level
    helpers are not seen at all; a manager reached through an alias or ``_default_manager``, a
    ``**kwargs``/``Q()`` lookup, or a natural-key lookup all pass; bulk ``pk__in=<collection>``
    locks are not covered; and a ``.filter(pk=...).exists()`` probe is exempt because it reads no
    object data and is how ``_required_perms_for_object`` decides WHICH permission to demand.
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
    PK_LOOKUP_KEYS = frozenset({"pk", "id"})
    RAW_TERMINALS = frozenset({"get", "filter", "get_or_create"})

    @classmethod
    def _scan_tree(cls, tree, label):
        """Return {(label, class, line)} for gated classes in *tree* that resolve by raw pk."""
        import ast

        def _call_chain(node):
            """Yield calls in a queryset receiver chain, excluding arguments and filters."""
            cur = node
            while True:
                if isinstance(cur, ast.Call):
                    yield cur
                    if not isinstance(cur.func, ast.Attribute):
                        return
                    cur = cur.func.value
                elif isinstance(cur, ast.Attribute):
                    cur = cur.value
                else:
                    return

        def _is_scoped(node):
            """Whether the queryset came from restricted_queryset(...) / .restrict(...)."""
            # Accepts the chained form too: self.restricted_queryset(Module).select_related(...).
            return any(
                getattr(call.func, "attr", "") in ("restricted_queryset", "restrict") for call in _call_chain(node)
            )

        def _has_scoped_relationship_filter(call):
            """Whether a positive lookup constrains ``__in`` to a restricted queryset."""
            return any(
                kw.arg and kw.arg.endswith("__in") and _is_scoped(kw.value)
                for chain_call in _call_chain(call)
                if getattr(chain_call.func, "attr", "") in cls.RAW_TERMINALS
                for kw in chain_call.keywords
            )

        def _through_manager(node):
            """Whether *node* is an unscoped ``<Model>.objects`` chain."""
            saw_objects = False
            cur = node
            while True:
                if isinstance(cur, ast.Call):
                    if getattr(cur.func, "attr", "") in ("restricted_queryset", "restrict"):
                        return False
                    cur = cur.func
                elif isinstance(cur, ast.Attribute):
                    saw_objects = saw_objects or cur.attr == "objects"
                    cur = cur.value
                else:
                    return saw_objects

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
            # `<qs>.exists()` reads no object data — see the class docstring.
            exempt = {
                sub.func.value
                for sub in ast.walk(node)
                if isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "exists"
                and isinstance(sub.func.value, ast.Call)
            }
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                if getattr(sub.func, "id", None) == "get_object_or_404":
                    if not (sub.args and (_is_scoped(sub.args[0]) or _has_scoped_relationship_filter(sub.args[0]))):
                        offenders.add((label, node.name, sub.lineno))
                    continue
                if not isinstance(sub.func, ast.Attribute) or sub.func.attr not in cls.RAW_TERMINALS:
                    continue
                if sub in exempt:
                    continue
                pk_kwargs = [kw for kw in sub.keywords if kw.arg in cls.PK_LOOKUP_KEYS]
                # A deliberate re-lock declares itself by calling relock_scoped_row, which is not a
                # `<Model>.objects` chain and so never reaches here. The old exemption instead
                # accepted any `pk=<expr>.<name>_id`, which a request-derived attribute satisfied
                # by accident (`pk=payload.device_id`) — see test_the_scan_flags_a_tainted_attribute.
                if not pk_kwargs:
                    continue
                # A scoped queryset can also arrive as an ARGUMENT rather than the receiver:
                # `Port.objects.filter(pk=..., device__in=self.restricted_queryset(Device))`
                # constrains by owner visibility, which is a deliberate and sufficient scoping.
                if (
                    _through_manager(sub.func.value)
                    and not _is_scoped(sub.func.value)
                    and not _has_scoped_relationship_filter(sub)
                ):
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

    def test_no_lexically_gated_view_resolves_an_object_by_raw_pk(self):
        """No class declaring a gate in its own body resolves an object by raw pk.

        This is a lint over one spelling, NOT proof that every view is scoped: a class gated only
        through an inherited base is invisible here (see TestRoutedSyncPagesScopeTheirObject, which
        covers the routed pages behaviourally), and so are module-level helpers.
        """
        offenders = sorted(self._scan())
        assert not offenders, (
            "view(s) resolving an object by raw pk — a constrained grant clears the gate and then "
            "reaches objects outside it; use restrict_object_or_404, or relock_scoped_row when the "
            f"id came from an already-resolved object: {offenders}"
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

    def test_the_scan_flags_a_tainted_attribute(self):
        """Guard the guard: an `*_id` ATTRIBUTE is not proof of provenance.

        The retired exemption accepted any ``pk=<expr>.<name>_id``, so a request-derived attribute
        passed silently. Only relock_scoped_row marks a lookup as a deliberate re-lock now.
        """
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request):\n"
            "        payload = json.loads(request.body)\n"
            "        return Device.objects.get(pk=payload.device_id)\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>"), (
            "the scan exempts a client-derived `*_id` attribute again — provenance must come from "
            "relock_scoped_row, not from how the expression is spelled"
        )

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

    def test_the_scan_rejects_an_unrelated_scope_in_a_primary_queryset(self):
        """A nested scope call must not bless get_object_or_404's unscoped queryset."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            "        return get_object_or_404(\n"
            "            Interface.objects.filter(audit=self.restricted_queryset(Device).count()), pk=remote_pk\n"
            "        )\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>"), "an unrelated scope call must not hide a raw lookup"

    def test_the_scan_accepts_a_primary_relationship_scope(self):
        """An owner ``__in`` filter from a restricted queryset scopes the primary lookup."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            "        return get_object_or_404(\n"
            "            Interface.objects.filter(device__in=self.restricted_queryset(Device)), pk=remote_pk\n"
            "        )\n"
        )
        assert not self._scan_tree(ast.parse(source), "<fixture>")

    def test_the_scan_rejects_a_negated_relationship_scope(self):
        """An exclusion of permitted owners selects precisely the objects outside the grant."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            "        return get_object_or_404(\n"
            "            Interface.objects.exclude(device__in=self.restricted_queryset(Device)), pk=remote_pk\n"
            "        )\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>"), "a negated permission scope must be flagged"

    @pytest.mark.parametrize(
        "lookup",
        [
            "Module.objects.get(pk=module_id)",
            "Module.objects.filter(pk=module_id, device=d).first()",
            "Module.objects.select_for_update().filter(pk=module_id).first()",
            "Interface.objects.get(id=iface_id)",
        ],
    )
    def test_the_scan_flags_a_secondary_raw_lookup(self, lookup):
        """The secondary form is the one that kept slipping through — every shape of it must be flagged."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            f"        return {lookup}\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>"), f"not flagged: {lookup}"

    def test_the_scan_accepts_a_scoped_secondary_lookup(self):
        """Both scoping spellings clear it: the mixin helper and the bare manager restrict()."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            "        a = self.restricted_queryset(Module, 'change').select_for_update().filter(pk=module_id)\n"
            "        return Interface.objects.restrict(request.user, 'view').get(pk=iface_id)\n"
        )
        assert not self._scan_tree(ast.parse(source), "<fixture>")

    def test_the_scan_accepts_scoping_passed_as_an_argument(self):
        """Constraining by owner visibility scopes the row just as well as scoping its own manager."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            "        return Interface.objects.filter(\n"
            "            pk=remote_pk, device__in=self.restricted_queryset(Device)\n"
            "        ).first()\n"
        )
        assert not self._scan_tree(ast.parse(source), "<fixture>")

    def test_the_scan_rejects_an_unrelated_nested_scope_call(self):
        """A scope call in an unrelated filter value must not bless a raw primary-key lookup."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            "        return Interface.objects.filter(\n"
            "            pk=remote_pk, audit=self.restricted_queryset(Device).count()\n"
            "        ).first()\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>"), "an unrelated scope call must not hide a raw lookup"

    def test_the_scan_exempts_a_relock_that_declares_itself(self):
        """A re-lock routed through relock_scoped_row must not demand a new permission."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            "        a = self.relock_scoped_row(Device, pk=existing_device.pk)\n"
            "        return relock_scoped_row(IPAddress, pk=donor.oob_ip_id)\n"
        )
        assert not self._scan_tree(ast.parse(source), "<fixture>")

    def test_the_scan_still_flags_a_relock_that_does_not(self):
        """The same re-lock spelled as a raw manager chain is reported: provenance must be declared."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            "        return IPAddress.objects.select_for_update().filter(pk=donor.oob_ip_id).first()\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>"), "use relock_scoped_row to declare a re-lock"

    def test_the_scan_exempts_an_exists_probe(self):
        """A .exists() probe reads no object data and picks WHICH permission to demand."""
        import ast

        source = (
            "class V:\n"
            "    required_object_permissions = {'POST': []}\n"
            "    def post(self, request, pk):\n"
            "        return Device.objects.filter(pk=object_id).exists()\n"
        )
        assert not self._scan_tree(ast.parse(source), "<fixture>")


class TestPostedSelectionsFailClosed:
    """Prevent an explicit object selection from degrading to an absent selection.

    The scanner recognizes assignments from ``request.POST.get()`` and attribute-based local
    helper calls. It does not model subscription reads, ``request.data``, or module-level callers.
    """

    LOOKUP_ERRORS = frozenset({"DoesNotExist", "ObjectDoesNotExist", "TypeError", "ValueError"})

    @staticmethod
    def _exception_names(node):
        """Return the exception names handled by an except clause."""
        import ast

        if isinstance(node, ast.Tuple):
            return {name for element in node.elts for name in TestPostedSelectionsFailClosed._exception_names(element)}
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, ast.Attribute):
            return {node.attr}
        return set()

    @staticmethod
    def _is_post_get(node):
        """Whether *node* reads a value from request.POST."""
        import ast

        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "POST"
        )

    @staticmethod
    def _is_restricted_get(node):
        """Whether *node* gets one object from a permission-restricted queryset."""
        import ast

        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "restricted_queryset"
                for call in ast.walk(node.func.value)
            )
        )

    @staticmethod
    def _returns_none(statement):
        """Whether *statement* is ``return None``."""
        import ast

        return isinstance(statement, ast.Return) and (
            statement.value is None or isinstance(statement.value, ast.Constant) and statement.value.value is None
        )

    @staticmethod
    def _reports_rejected_selection(statements):
        """Whether the handler records the invalid selection before it returns."""
        import ast

        return any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "_record_skipped_conflict"
            for statement in statements
            for call in ast.walk(statement)
        )

    @staticmethod
    def _is_none_guard(node, name):
        """Whether *node* checks that *name* is None."""
        import ast

        return (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == name
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Is)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value is None
        )

    @staticmethod
    def _stops_current_path(statements):
        """Whether *statements* stop the current path before the selected object can be used."""
        import ast

        return bool(statements) and isinstance(statements[-1], (ast.Break, ast.Continue, ast.Raise, ast.Return))

    @classmethod
    def _statement_lists(cls, node):
        """Yield each ordered statement list nested below *node*."""
        import ast

        for _field, value in ast.iter_fields(node):
            if isinstance(value, list) and value and all(isinstance(item, ast.stmt) for item in value):
                yield value
                for statement in value:
                    yield from cls._statement_lists(statement)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        yield from cls._statement_lists(item)
            elif isinstance(value, ast.AST):
                yield from cls._statement_lists(value)

    @classmethod
    def _calls_reject_none(cls, tree, helper_name):
        """Whether every local call to *helper_name* stops when its result is None."""
        import ast

        found_call = False
        for statements in cls._statement_lists(tree):
            for index, statement in enumerate(statements):
                if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Call):
                    continue
                function = statement.value.func
                if not isinstance(function, ast.Attribute) or function.attr != helper_name:
                    continue
                found_call = True
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    return False
                if index + 1 >= len(statements):
                    return False
                guard = statements[index + 1]
                if not isinstance(guard, ast.If) or not cls._is_none_guard(guard.test, statement.targets[0].id):
                    return False
                if not cls._stops_current_path(guard.body):
                    return False
        return found_call

    @classmethod
    def _scan_tree(cls, tree, label):
        """Return posted restricted lookups whose failure becomes no selection."""
        import ast

        offenders = set()
        for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            posted_names = {
                target.id
                for assignment in ast.walk(function)
                if isinstance(assignment, ast.Assign) and cls._is_post_get(assignment.value)
                for target in assignment.targets
                if isinstance(target, ast.Name)
            }
            if not posted_names:
                continue

            terminal_none = bool(function.body and cls._returns_none(function.body[-1]))
            for try_node in (node for node in ast.walk(function) if isinstance(node, ast.Try)):
                restricted_gets = [
                    call
                    for statement in try_node.body
                    for call in ast.walk(statement)
                    if cls._is_restricted_get(call)
                    and any(isinstance(node, ast.Name) and node.id in posted_names for node in ast.walk(call))
                ]
                if not restricted_gets:
                    continue

                for handler in try_node.handlers:
                    handled = cls._exception_names(handler.type)
                    catches_lookup_error = (
                        handler.type is None
                        or bool(handled & cls.LOOKUP_ERRORS)
                        or bool(handled & {"Exception", "BaseException"})
                    )
                    last_statement = handler.body[-1] if handler.body else None
                    passes_to_terminal_none = terminal_none and isinstance(last_statement, ast.Pass)
                    returns_none = cls._returns_none(last_statement)
                    rejects_selection = passes_to_terminal_none or returns_none
                    reports_rejection = cls._reports_rejected_selection(handler.body[:-1])
                    if catches_lookup_error and rejects_selection and not reports_rejection:
                        if not cls._calls_reject_none(tree, function.name):
                            offenders.add((label, function.name, restricted_gets[0].lineno))

        return offenders

    @classmethod
    def _scan(cls):
        """Run the fail-open selection scan over every view module."""
        import ast
        import pathlib

        import netbox_librenms_plugin

        views_root = pathlib.Path(netbox_librenms_plugin.__file__).parent / "views"
        offenders = set()
        for path in sorted(views_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            offenders |= cls._scan_tree(tree, str(path.relative_to(views_root)))
        return offenders

    def test_no_posted_restricted_selection_fails_open(self):
        offenders = sorted(self._scan())
        assert not offenders, (
            "posted object selection(s) degrade to no selection after a restricted lookup fails; "
            f"raise or report the invalid selection instead: {offenders}"
        )

    def test_the_scan_flags_the_old_vrf_pattern(self):
        """Guard the guard with the fail-open structure that caused the VRF defect."""
        import ast

        source = (
            "class V:\n"
            "    def get_selection(self, request):\n"
            "        selected_id = request.POST.get('selection')\n"
            "        if selected_id:\n"
            "            try:\n"
            "                return self.restricted_queryset(VRF).get(pk=selected_id)\n"
            "            except (VRF.DoesNotExist, TypeError, ValueError):\n"
            "                pass\n"
            "        return None\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>")

    @pytest.mark.parametrize("handler", ["except:", "except Exception:", "except BaseException:"])
    def test_the_scan_flags_broad_exception_handlers(self, handler):
        """Broad handlers must not hide a restricted lookup that fails open."""
        import ast

        source = (
            "class V:\n"
            "    def get_selection(self, request):\n"
            "        selected_id = request.POST.get('selection')\n"
            "        try:\n"
            "            return self.restricted_queryset(Device).get(pk=selected_id)\n"
            f"        {handler}\n"
            "            return None\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>")

    def test_the_scan_checks_async_view_helpers(self):
        """An async helper must not escape the posted-selection scan."""
        import ast

        source = (
            "class V:\n"
            "    async def get_selection(self, request):\n"
            "        selected_id = request.POST.get('selection')\n"
            "        try:\n"
            "            return self.restricted_queryset(Device).get(pk=selected_id)\n"
            "        except Device.DoesNotExist:\n"
            "            return None\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>")

    def test_the_scan_flags_a_logging_handler_that_falls_through(self):
        """Logging before ``pass`` must not hide a lookup that still degrades to None."""
        import ast

        source = (
            "class V:\n"
            "    def get_selection(self, request):\n"
            "        selected_id = request.POST.get('selection')\n"
            "        try:\n"
            "            return self.restricted_queryset(Device).get(pk=selected_id)\n"
            "        except Device.DoesNotExist:\n"
            "            logger.debug('missing')\n"
            "            pass\n"
            "        return None\n"
        )
        assert self._scan_tree(ast.parse(source), "<fixture>")

    def test_the_scan_finds_a_guarded_helper_call_inside_an_except_block(self):
        """A helper call inside an except handler must count when its None result stops the path."""
        import ast

        source = (
            "class V:\n"
            "    def get_selection(self, request):\n"
            "        selected_id = request.POST.get('selection')\n"
            "        try:\n"
            "            return self.restricted_queryset(Device).get(pk=selected_id)\n"
            "        except Device.DoesNotExist:\n"
            "            return None\n"
            "    def post(self, request):\n"
            "        try:\n"
            "            work()\n"
            "        except ValueError:\n"
            "            selected = self.get_selection(request)\n"
            "            if selected is None:\n"
            "                return None\n"
            "        return selected\n"
        )
        assert not self._scan_tree(ast.parse(source), "<fixture>")

    def test_the_scan_accepts_an_explicit_invalid_selection_sentinel(self):
        """A None sentinel is safe when every caller stops before it can reach a write."""
        import ast

        source = (
            "class V:\n"
            "    def get_selection(self, request):\n"
            "        selected_id = request.POST.get('selection')\n"
            "        try:\n"
            "            return self.restricted_queryset(Device).get(pk=selected_id)\n"
            "        except Device.DoesNotExist:\n"
            "            return None\n"
            "    def sync(self, request):\n"
            "        selected = self.get_selection(request)\n"
            "        if selected is None:\n"
            "            self.record_invalid_selection()\n"
            "            return\n"
            "        self.write(selected)\n"
        )
        assert not self._scan_tree(ast.parse(source), "<fixture>")


class TestViewTestHelpers:
    def test_bind_and_call_populates_view_kwargs(self):
        """The direct-call helper must bind URL kwargs exactly as Django dispatch does."""
        from django.http import HttpResponse
        from django.test import RequestFactory
        from django.views import View

        from netbox_librenms_plugin.tests.view_test_helpers import bind_and_call

        class KwargView(View):
            def post(self, request, **kwargs):
                assert self.kwargs == kwargs
                return HttpResponse()

        response = bind_and_call(KwargView(), RequestFactory().post("/"), "post", pk=17)

        assert isinstance(response, HttpResponse)

    def test_message_level_rejects_non_level_message_attributes(self):
        from netbox_librenms_plugin.tests.view_test_helpers import _message_level

        with pytest.raises(ValueError, match="unknown message level"):
            _message_level("add_message")


class TestModuleWriteViewPermissionDeclarations:
    @pytest.mark.parametrize(
        ("view_name", "expected"),
        [
            ("InstallModuleView", ("view", "Device")),
            ("InstallModuleView", ("view", "ModuleBay")),
            ("InstallModuleView", ("view", "ModuleType")),
            ("InstallBranchView", ("view", "Device")),
            ("InstallSelectedView", ("view", "Device")),
            ("UpdateModuleSerialView", ("view", "Device")),
            ("UpdateModuleInterfaceView", ("view", "Device")),
            ("UpdateModuleInterfaceView", ("view", "Module")),
            ("ReplaceModuleView", ("view", "Device")),
            ("MoveModuleView", ("view", "Device")),
            ("MoveModuleView", ("view", "ModuleBay")),
        ],
    )
    def test_write_gate_declares_each_restricted_read(self, view_name, expected):
        """Each dynamic gate must declare every model read before the first lookup."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from netbox_librenms_plugin.views.sync import modules

        view = getattr(modules, view_name)()
        denied = object()
        view.require_all_permissions = MagicMock(return_value=denied)
        request = SimpleNamespace(POST={})

        assert view.post(request, pk=1) is denied
        assert any(
            action == expected[0] and model.__name__ == expected[1]
            for action, model in view.required_object_permissions["POST"]
        )

    @pytest.mark.parametrize(
        ("method", "target_kind", "target_model"),
        [("get", "device_type", "DeviceType"), ("post", "module_type", "ModuleType")],
    )
    def test_add_bay_template_gate_declares_device_and_dynamic_target_reads(self, method, target_kind, target_model):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        view = AddBayTemplateView()
        denied = object()
        view.require_all_permissions = MagicMock(return_value=denied)
        request = SimpleNamespace(GET={"target_kind": target_kind}, POST={"target_kind": target_kind})

        assert getattr(view, method)(request, pk=1) is denied
        declared = {(action, model.__name__) for action, model in view.required_object_permissions[method.upper()]}
        assert ("view", "Device") in declared
        assert ("view", target_model) in declared


class TestImportMappingPermissionOrder:
    @pytest.mark.parametrize(
        ("view_name", "target_model"),
        [("AddDeviceTypeMappingView", "DeviceType"), ("AddPlatformMappingView", "Platform")],
    )
    def test_target_read_is_declared_and_gated_before_restricted_lookup(self, view_name, target_model):
        """Posted mapping targets must not be resolved before their view-permission gate."""
        import ast
        import inspect
        import textwrap

        from netbox_librenms_plugin.views.imports import actions

        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(actions, view_name).post)))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        gate_line = min(node.lineno for node in calls if getattr(node.func, "attr", "") == "require_object_permissions")
        target_lookup_line = min(
            node.lineno
            for node in calls
            if getattr(node.func, "attr", "") == "get"
            and isinstance(node.func.value, ast.Call)
            and getattr(node.func.value.func, "attr", "") == "restricted_queryset"
            and node.func.value.args
            and getattr(node.func.value.args[0], "id", "") == target_model
        )
        declaration_line = min(
            assignment.lineno
            for assignment in ast.walk(tree)
            if isinstance(assignment, ast.Assign)
            and any(
                getattr(target, "id", getattr(target, "attr", "")) == "required_object_permissions"
                for target in assignment.targets
            )
            and any(
                isinstance(node, ast.Tuple)
                and len(node.elts) == 2
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value == "view"
                and getattr(node.elts[1], "id", "") == target_model
                for node in ast.walk(assignment.value)
            )
        )

        assert declaration_line < gate_line < target_lookup_line


class TestScopedRowLocks:
    def test_restricted_queryset_locks_only_the_target_table(self):
        """Permission joins must not become PostgreSQL row-lock targets."""
        import ast
        import pathlib

        import netbox_librenms_plugin

        views_root = pathlib.Path(netbox_librenms_plugin.__file__).parent / "views"
        offenders = []
        for path in sorted(views_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or getattr(node.func, "attr", "") != "select_for_update":
                    continue
                receiver = node.func.value
                is_restricted = any(
                    isinstance(inner, ast.Call)
                    and getattr(inner.func, "attr", "") in {"restricted_queryset", "restrict"}
                    for inner in ast.walk(receiver)
                )
                if not is_restricted:
                    continue
                of_keyword = next((kw for kw in node.keywords if kw.arg == "of"), None)
                if not (
                    of_keyword
                    and isinstance(of_keyword.value, (ast.Tuple, ast.List))
                    and [getattr(item, "value", None) for item in of_keyword.value.elts] == ["self"]
                ):
                    offenders.append((str(path.relative_to(views_root)), node.lineno))

        assert not offenders, f"restricted row locks must use select_for_update(of=('self',)): {offenders}"


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
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        user = make_user_with_perms(username, [])
        for i, (model, action, constraints) in enumerate(model_grants):
            user = grant(
                user,
                action,
                model,
                constraints=constraints,
                name=f"{username}-{action}-{i}",
            )
        return user

    @staticmethod
    def _request(user, data, method="post"):
        from netbox_librenms_plugin.tests.view_test_helpers import make_request

        return make_request(method, data, user=user, path="/x/")

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
        from dcim.models import Device, Interface
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
                (Interface, "view", None),
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
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType
        from django.http import Http404

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        in_scope = make_device("scope-modinstall-in")
        out_of_scope = make_device("scope-modinstall-out")
        user = self._user(
            "scope-modinstall",
            [
                (Device, "view", {"pk": in_scope.pk}),
                (ModuleBay, "view", None),
                (ModuleType, "view", None),
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

    def test_module_serial_update_refuses_a_module_outside_the_grant(self):
        """UpdateModuleSerialView writes the serial of a module whose pk comes from the POST, filtered only by device."""
        from dcim.models import Device, Module

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        page_device = make_device("scope-modserial-page")
        mtype = make_module_type("MT-scope-modserial")
        bay = make_module_bay(page_device, "Slot 1")
        # On the page device, so the view's device filter passes; only the grant excludes it.
        module = Module.objects.create(device=page_device, module_bay=bay, module_type=mtype, serial="ORIGINAL")
        decoy = Module.objects.create(
            device=page_device, module_bay=make_module_bay(page_device, "Slot 2"), module_type=mtype
        )

        user = self._user(
            "scope-modserial",
            [
                (Device, "view", {"pk": page_device.pk}),
                (Module, "change", {"pk": decoy.pk}),
            ],
        )
        view = UpdateModuleSerialView()
        request = self._request(
            user,
            {"server_key": "default", "module_id": str(module.pk), "serial": "HIJACKED"},
        )
        view.setup(request)
        view.post(request, pk=page_device.pk)

        module.refresh_from_db()
        assert module.serial == "ORIGINAL"

    def test_module_replace_refuses_to_delete_the_target_outside_the_delete_grant(self):
        """Replace deletes its target, so change access alone must not authorize the operation."""
        from dcim.models import Device, Interface, Module, ModuleType
        from django.core.cache import cache
        from django.http import Http404

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("scope-replace-target")
        module_type = make_module_type("MT-scope-replace-target")
        target = Module.objects.create(
            device=device,
            module_bay=make_module_bay(device, "Slot 1"),
            module_type=module_type,
            serial="OLD-TARGET",
        )
        decoy = Module.objects.create(
            device=device,
            module_bay=make_module_bay(device, "Slot 2"),
            module_type=module_type,
        )
        user = self._user(
            "scope-replace-target",
            [
                (Device, "view", {"pk": device.pk}),
                (ModuleType, "view", {"pk": module_type.pk}),
                (Module, "add", None),
                (Module, "change", {"pk": target.pk}),
                (Module, "delete", {"pk": decoy.pk}),
                (Interface, "add", None),
                (Interface, "change", None),
                (Interface, "delete", None),
            ],
        )
        view = ReplaceModuleView()
        view._librenms_api = MagicMock(server_key="default")
        request = self._request(
            user,
            {"module_id": str(target.pk), "ent_index": "100"},
        )
        view.setup(request)
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        cache.set(
            cache_key,
            {
                "inventory": [
                    {
                        "entPhysicalIndex": 100,
                        "entPhysicalModelName": module_type.model,
                        "entPhysicalSerialNum": "NEW-TARGET",
                    }
                ],
                "librenms_id": 1,
            },
        )
        try:
            with pytest.raises(Http404):
                view.post(request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        target.refresh_from_db()
        assert target.serial == "OLD-TARGET"
        assert Module.objects.filter(module_bay=target.module_bay).count() == 1

    def test_module_replace_fails_closed_on_a_hidden_serial_conflict(self):
        """An existing serial outside delete scope must block replacement, not become a duplicate."""
        from dcim.models import Device, Interface, Module, ModuleType
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.tests.view_test_helpers import message_texts
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("scope-replace-conflict")
        other_device = make_device("scope-replace-hidden")
        module_type = make_module_type("MT-scope-replace-conflict")
        target = Module.objects.create(
            device=device,
            module_bay=make_module_bay(device, "Slot 1"),
            module_type=module_type,
            serial="OLD-CONFLICT",
        )
        hidden = Module.objects.create(
            device=other_device,
            module_bay=make_module_bay(other_device, "Secret Slot"),
            module_type=module_type,
            serial="INCOMING-CONFLICT",
        )
        user = self._user(
            "scope-replace-conflict",
            [
                (Device, "view", {"pk": device.pk}),
                (ModuleType, "view", {"pk": module_type.pk}),
                (Module, "add", None),
                (Module, "change", {"pk": target.pk}),
                (Module, "delete", {"pk": target.pk}),
                (Interface, "add", None),
                (Interface, "change", None),
                (Interface, "delete", None),
            ],
        )
        view = ReplaceModuleView()
        view._librenms_api = MagicMock(server_key="default")
        request = self._request(user, {"module_id": str(target.pk), "ent_index": "100"})
        view.setup(request)
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        cache.set(
            cache_key,
            {
                "inventory": [
                    {
                        "entPhysicalIndex": 100,
                        "entPhysicalModelName": module_type.model,
                        "entPhysicalSerialNum": hidden.serial,
                    }
                ],
                "librenms_id": 1,
            },
        )
        try:
            view.post(request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        target.refresh_from_db()
        hidden.refresh_from_db()
        assert target.serial == "OLD-CONFLICT"
        assert hidden.serial == "INCOMING-CONFLICT"
        assert Module.objects.filter(serial="INCOMING-CONFLICT").count() == 1
        assert any("cannot remove" in text for text in message_texts(request, "error"))

    def test_module_preview_does_not_expose_a_hidden_serial_conflict(self):
        """The preview must not render the location or id of a conflict outside view scope."""
        from dcim.models import Device, Module
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.views.sync.modules import ModuleMismatchPreviewView

        device = make_device("scope-preview-target")
        hidden_device = make_device("scope-preview-hidden")
        module_type = make_module_type("MT-scope-preview")
        target = Module.objects.create(
            device=device,
            module_bay=make_module_bay(device, "Visible Slot"),
            module_type=module_type,
            serial="OLD-PREVIEW",
        )
        hidden = Module.objects.create(
            device=hidden_device,
            module_bay=make_module_bay(hidden_device, "Hidden Slot"),
            module_type=module_type,
            serial="INCOMING-PREVIEW",
        )
        user = self._user(
            "scope-preview-conflict",
            [
                (Device, "view", {"pk": device.pk}),
                (Module, "view", {"pk": target.pk}),
            ],
        )
        view = ModuleMismatchPreviewView()
        view._librenms_api = MagicMock(server_key="default")
        request = self._request(
            user,
            {"module_id": str(target.pk), "ent_index": "100"},
            method="get",
        )
        view.setup(request)
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        cache.set(
            cache_key,
            {
                "inventory": [
                    {
                        "entPhysicalIndex": 100,
                        "entPhysicalModelName": module_type.model,
                        "entPhysicalSerialNum": hidden.serial,
                    }
                ],
                "librenms_id": 1,
            },
        )
        try:
            response = view.get(request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        html = response.content.decode()
        assert hidden_device.name not in html
        assert hidden.module_bay.name not in html
        assert f'name="conflict_module_id" value="{hidden.pk}"' not in html
        assert "cannot view" in html
        assert "Update Serial Only" not in html

    def test_site_location_push_refuses_a_site_outside_the_grant(self):
        """SyncSiteLocationView is gated by the plugin write permission alone and takes the site pk from the POST body."""
        from dcim.models import Site

        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        in_scope = Site.objects.create(name="scope-site-in", slug="scope-site-in")
        out_of_scope = Site.objects.create(name="scope-site-out", slug="scope-site-out")
        user = self._user("scope-site", [(Site, "view", {"pk": in_scope.pk})])

        view = SyncSiteLocationView()
        request = self._request(user, {"action": "update", "pk": str(out_of_scope.pk)})
        view.setup(request)

        # Bounces on "Site not found" before any LibreNMS call, so no API stub is needed.
        assert view.get_site_by_pk(out_of_scope.pk) is None
        assert view.get_site_by_pk(in_scope.pk) == in_scope  # the grant DOES still resolve

    def test_oob_interface_reuse_refuses_an_interface_outside_the_grant(self):
        """AddAsOOBView reuses an interface whose pk comes from the OOB form, filtered only by device."""
        from dcim.models import Device, Interface
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        device = make_device("scope-oobiface")
        granted = make_interface(device, "eth0")
        # Same device, so the view's device filter passes; only the grant excludes it.
        outside = make_interface(device, "idrac0")

        user = self._user(
            "scope-oobiface",
            [
                (Device, "view", {"pk": device.pk}),
                (Interface, "view", {"pk": granted.pk}),
            ],
        )
        request = self._request(user, {"oob_interface_id": str(outside.pk)})

        with transaction.atomic():
            resolved, _reason = AddAsOOBView._resolve_oob_interface(request, device)
        assert resolved is None  # outside the grant, so it is not handed back for the OOB attach

    def test_module_move_refuses_to_delete_a_bay_occupant_outside_the_grant(self):
        """MoveModuleView deletes whatever occupies the target bay; the bay filter proves where that row sits, not that the delete grant covers it."""
        from dcim.models import Device, Module, ModuleBay

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.views.sync.modules import MoveModuleView

        page_device = make_device("scope-moveocc-page")
        mtype = make_module_type("MT-scope-moveocc")
        source_bay = make_module_bay(page_device, "Slot 1")
        target_bay = make_module_bay(page_device, "Slot 2")
        conflict_module = Module.objects.create(device=page_device, module_bay=source_bay, module_type=mtype)
        # The bay's current occupant sits on the page device, so only the delete grant excludes it.
        occupant = Module.objects.create(device=page_device, module_bay=target_bay, module_type=mtype)

        user = self._user(
            "scope-moveocc",
            [
                (Device, "view", {"pk": page_device.pk}),
                (ModuleBay, "view", None),
                (Module, "change", None),  # the module being moved IS covered
                (Module, "delete", {"pk": conflict_module.pk}),  # the occupant is NOT
            ],
        )
        view = MoveModuleView()
        request = self._request(
            user,
            {
                "server_key": "default",
                "conflict_module_id": str(conflict_module.pk),
                "target_bay_id": str(target_bay.pk),
                "module_id": str(occupant.pk),
            },
        )
        view.setup(request)
        view.post(request, pk=page_device.pk)

        assert Module.objects.filter(pk=occupant.pk).exists()  # outside the delete grant, so kept

    def test_interface_delete_refuses_an_interface_outside_the_grant(self):
        """DeleteNetBoxInterfacesView deletes ids straight from the POST; the device check proves ownership, not the grant."""
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        device = make_device("scope-ifdel")
        granted = make_interface(device, "eth0")
        # Same device, so every ownership check the view makes passes; only the grant excludes it.
        outside = make_interface(device, "eth1")

        user = self._user(
            "scope-ifdel",
            [
                (Device, "view", {"pk": device.pk}),
                (Interface, "delete", {"pk": granted.pk}),
            ],
        )
        view = DeleteNetBoxInterfacesView()
        request = self._request(user, {"server_key": "default", "interface_ids": [str(outside.pk)]})
        view.setup(request)
        response = view.post(request, object_type="device", object_id=device.pk)

        assert Interface.objects.filter(pk=outside.pk).exists()
        assert json.loads(response.content)["deleted_count"] == 0

    def test_vm_interface_delete_refuses_an_interface_outside_the_grant(self):
        """The virtual-machine branch of the same view resolves VMInterface ids from the POST too."""
        from virtualization.models import VirtualMachine, VMInterface

        from netbox_librenms_plugin.tests.conftest import make_vm
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        vm = make_vm("scope-vmifdel")
        granted = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        outside = VMInterface.objects.create(virtual_machine=vm, name="eth1")

        user = self._user(
            "scope-vmifdel",
            [
                (VirtualMachine, "view", {"pk": vm.pk}),
                (VMInterface, "delete", {"pk": granted.pk}),
            ],
        )
        view = DeleteNetBoxInterfacesView()
        request = self._request(user, {"server_key": "default", "interface_ids": [str(outside.pk)]})
        view.setup(request)
        response = view.post(request, object_type="virtualmachine", object_id=vm.pk)

        assert VMInterface.objects.filter(pk=outside.pk).exists()
        assert json.loads(response.content)["deleted_count"] == 0

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
            {"server_key": "default", "member_id_1": str(sibling.pk), "serial_1": "HIJACKED"},
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


class TestRoutedSyncPagesScopeTheirObject:
    """A routed sync page must not resolve an object the caller's grant excludes.

    ``LibreNMSPermissionMixin`` extends Django's ``PermissionRequiredMixin``, which only checks the
    model-level plugin permission and never evaluates NetBox object-permission constraints. The base
    table views then resolve the URL pk with a raw ``get_object_or_404``, so a CONSTRAINED
    ``dcim.view_device`` grant never narrows the lookup and the page renders any device by pk.

    The static scan in :class:`TestGatedViewsResolveThroughRestrictedQuerysets` cannot see this: it
    only considers classes that declare a gate lexically, and these classes declare none.
    """

    ROUTED_DEVICE_PAGES = (
        ("object_sync.devices", "DeviceInterfaceTableView"),
        ("object_sync.devices", "DeviceCableTableView"),
        ("object_sync.devices", "DeviceIPAddressTableView"),
        ("object_sync.devices", "DeviceVLANTableView"),
        ("object_sync.devices", "DeviceModuleTableView"),
    )

    @pytest.mark.django_db
    @pytest.mark.parametrize("module_path,class_name", ROUTED_DEVICE_PAGES)
    def test_a_constrained_grant_cannot_reach_a_hidden_device(self, module_path, class_name):
        """A user granted view_device only for device A must not resolve device B."""
        import importlib

        from dcim.models import Device
        from django.http import Http404

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import (
            make_request,
            make_user_with_perms,
            make_view,
        )

        allowed = make_device(f"scoped-allowed-{class_name.lower()}")
        hidden = make_device(f"scoped-hidden-{class_name.lower()}")
        user = make_user_with_perms(
            f"scoped-viewer-{class_name.lower()}",
            [("view", Device)],
            constraints={"name": allowed.name},
        )
        request = make_request("get", user=user, path=f"/plugins/librenms/devices/{hidden.pk}/sync/")
        view_class = getattr(importlib.import_module(f"netbox_librenms_plugin.views.{module_path}"), class_name)
        view = make_view(view_class, request)

        # Sanity: the grant really is constrained — the allowed device stays reachable.
        assert view.get_object(allowed.pk).pk == allowed.pk

        with pytest.raises(Http404):
            view.get_object(hidden.pk)
