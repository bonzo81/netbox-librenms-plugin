"""Coverage tests for views/object_sync/devices.py."""

from unittest.mock import MagicMock, patch

import pytest


def _make_real_device(tag):
    """Create and return a real NetBox Device (with its required FKs) for DB-backed tests."""
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

    mfr, _ = Manufacturer.objects.get_or_create(name=f"Mfr-{tag}", slug=f"mfr-{tag}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=f"DT-{tag}", slug=f"dt-{tag}")
    role, _ = DeviceRole.objects.get_or_create(name=f"Role-{tag}", slug=f"role-{tag}")
    site, _ = Site.objects.get_or_create(name=f"Site-{tag}", slug=f"site-{tag}")
    return Device.objects.create(name=f"host-{tag}", device_type=dt, role=role, site=site, status="active")


def _make_verify_superuser(tag):
    """A real superuser so restrict(user, "view") returns the full queryset in DB-backed verify tests."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_superuser(username=f"verify-su-{tag}", email="", password="x")


def _real_verify_request(body, tag):
    """A real POST request wired with a real superuser (so the object-perm gate + restrict() resolve for real)."""
    import json as _json

    from django.test import RequestFactory

    request = RequestFactory().post("/verify/", data=_json.dumps(body), content_type="application/json")
    request.user = _make_verify_superuser(tag)
    return request


def _configured_default():
    """Patch context declaring 'default' as a configured LibreNMS server so a POSTed server_key resolves."""
    from unittest.mock import patch

    return patch(
        "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
        return_value={"default": "Default"},
    )


def _real_port(**overrides):
    """A LibreNMS-shaped port dict with every field LibreNMSInterfaceTable.format_interface_data reads."""
    port = {
        "ifName": "eth0",
        "ifDescr": "eth0",
        "ifAlias": "",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1000000000,
        "ifPhysAddress": "00:11:22:33:44:55",
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "ifOperStatus": "up",
    }
    port.update(overrides)
    return port


def _user_with_perms(tag, perm_specs):
    """Real user granted exactly ``perm_specs`` = [(action, Model), ...] via NetBox ObjectPermissions.

    Lets a test drive the view's real ``has_perm`` from a precise, real permission set (instead of a
    mocked ``has_perm`` side-effect), so the perm→table-flag mapping is exercised end to end.
    """
    from core.models import ObjectType
    from django.contrib.auth import get_user_model
    from users.models import ObjectPermission

    user = get_user_model().objects.create_user(username=f"perms-{tag}", password="x")
    for i, (action, model) in enumerate(perm_specs):
        op = ObjectPermission.objects.create(name=f"{tag}-{action}-{i}", actions=[action])
        op.object_types.set([ObjectType.objects.get_for_model(model)])
        op.users.set([user])
    return get_user_model().objects.get(pk=user.pk)  # clear the per-request perm cache


def _make_device_view():
    """Create a DeviceLibreNMSSyncView instance bypassing __init__."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

    view = object.__new__(DeviceLibreNMSSyncView)
    view.request = MagicMock()
    view.request.path = "/dcim/devices/1/librenms-sync/"
    view.kwargs = {}
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    view._librenms_api.cache_timeout = 300
    return view


def _make_interface_view():
    """Create a DeviceInterfaceTableView instance bypassing __init__."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

    view = object.__new__(DeviceInterfaceTableView)
    view.request = MagicMock()
    view.request.path = "/dcim/devices/1/librenms-sync/"
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    return view


class TestDeviceLibreNMSSyncViewContextMethods:
    """Tests for DeviceLibreNMSSyncView context delegation."""

    def test_get_interface_context_delegates_to_interface_view(self):
        """get_interface_context() creates DeviceInterfaceTableView, copies request, and calls get_context_data."""

        view = _make_device_view()
        request = MagicMock()
        obj = MagicMock()

        mock_ctx = {"interfaces": []}
        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceInterfaceTableView.get_context_data",
            autospec=True,
            return_value=mock_ctx,
        ) as mock_get_context:
            with patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_interface_name_field", return_value="ifName"
            ):
                result = view.get_interface_context(request, obj)
        assert result == mock_ctx
        assert mock_get_context.called
        child_instance = mock_get_context.call_args[0][0]
        # Identity check: the child view must store a *copy* of the request,
        # not the original — equality (==) would silently pass even if the
        # original were reused (MagicMock instances compare equal).
        assert child_instance.request is not request
        assert mock_get_context.call_args[0][1] is request
        assert mock_get_context.call_args[0][2] is obj

    def test_get_cable_context_delegates_to_cable_view(self):
        """get_cable_context() creates DeviceCableTableView, copies request, and calls get_context_data."""

        view = _make_device_view()
        request = MagicMock()
        obj = MagicMock()

        mock_ctx = {"cables": []}
        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceCableTableView.get_context_data",
            autospec=True,
            return_value=mock_ctx,
        ) as mock_get_context:
            result = view.get_cable_context(request, obj)
        assert result == mock_ctx
        assert mock_get_context.called
        child_instance = mock_get_context.call_args[0][0]
        assert child_instance.request is not request
        assert mock_get_context.call_args[0][1] is request
        assert mock_get_context.call_args[0][2] is obj

    def test_get_ip_context_delegates_to_ip_view(self):
        """get_ip_context() creates DeviceIPAddressTableView and calls get_context_data with request and obj."""

        view = _make_device_view()
        request = MagicMock()
        obj = MagicMock()

        mock_ctx = {"ips": []}
        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceIPAddressTableView.get_context_data",
            autospec=True,
            return_value=mock_ctx,
        ) as mock_get_context:
            result = view.get_ip_context(request, obj)
        assert result == mock_ctx
        assert mock_get_context.called
        assert mock_get_context.call_args[0][1] is request
        assert mock_get_context.call_args[0][2] is obj
        child_instance = mock_get_context.call_args[0][0]
        assert child_instance.request is not request

    def test_get_vlan_context_delegates_to_vlan_view(self):
        """get_vlan_context() creates DeviceVLANTableView, copies request, and calls get_vlan_context."""

        view = _make_device_view()
        request = MagicMock()
        obj = MagicMock()

        mock_ctx = {"vlans": []}
        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceVLANTableView.get_vlan_context",
            autospec=True,
            return_value=mock_ctx,
        ) as mock_get_context:
            result = view.get_vlan_context(request, obj)
        assert result == mock_ctx
        assert mock_get_context.called
        child_instance = mock_get_context.call_args[0][0]
        assert child_instance.request is not request
        assert mock_get_context.call_args[0][1] is request
        assert mock_get_context.call_args[0][2] is obj

    def test_get_module_context_delegates_to_module_view(self):
        """get_module_context() creates DeviceModuleTableView, copies request, and calls get_context_data."""

        view = _make_device_view()
        request = MagicMock()
        obj = MagicMock()

        mock_ctx = {"modules": []}
        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView.get_context_data",
            autospec=True,
            return_value=mock_ctx,
        ) as mock_get_context:
            result = view.get_module_context(request, obj)
        assert result == mock_ctx
        assert mock_get_context.called
        child_instance = mock_get_context.call_args[0][0]
        assert child_instance.request is not request
        assert mock_get_context.call_args[0][1] is request
        assert mock_get_context.call_args[0][2] is obj


class TestDeviceInterfaceTableView:
    """Tests for DeviceInterfaceTableView."""

    def test_get_interfaces_returns_all_interfaces(self):
        """get_interfaces() returns obj.interfaces.all()."""
        view = _make_interface_view()
        obj = MagicMock()
        mock_qs = MagicMock()
        obj.interfaces.all.return_value = mock_qs

        result = view.get_interfaces(obj)
        assert result is mock_qs
        obj.interfaces.all.assert_called_once()

    def test_get_redirect_url_returns_device_url(self):
        """get_redirect_url() returns the device interface sync URL."""
        view = _make_interface_view()
        obj = MagicMock()
        obj.pk = 42

        with patch("netbox_librenms_plugin.views.object_sync.devices.reverse") as mock_reverse:
            mock_reverse.return_value = "/dcim/devices/42/interface-sync/"
            result = view.get_redirect_url(obj)
        mock_reverse.assert_called_once_with("plugins:netbox_librenms_plugin:device_interface_sync", kwargs={"pk": 42})
        assert result == "/dcim/devices/42/interface-sync/"

    def test_get_table_returns_vc_table_for_vc_device(self):
        """get_table() returns VCInterfaceTable when device has virtual_chassis."""

        view = _make_interface_view()
        obj = MagicMock()
        obj.virtual_chassis = MagicMock()  # Has VC
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        with patch("netbox_librenms_plugin.views.object_sync.devices.VCInterfaceTable") as mock_vc_table:
            mock_table = MagicMock()
            mock_vc_table.return_value = mock_table
            result = view.get_table([], obj, "ifName", vlan_groups=[])

        mock_vc_table.assert_called_once_with(
            [], device=obj, interface_name_field="ifName", vlan_groups=[], server_key="default"
        )
        assert result is mock_table

    def test_get_table_returns_librenms_table_for_non_vc_device(self):
        """get_table() returns LibreNMSInterfaceTable when no virtual_chassis."""
        view = _make_interface_view()
        obj = MagicMock()
        obj.virtual_chassis = None  # No VC

        with patch("netbox_librenms_plugin.views.object_sync.devices.LibreNMSInterfaceTable") as mock_table_cls:
            mock_table = MagicMock()
            mock_table_cls.return_value = mock_table
            result = view.get_table([], obj, "ifName")

        mock_table_cls.assert_called_once_with(
            [], device=obj, interface_name_field="ifName", vlan_groups=None, server_key="default"
        )
        assert result is mock_table

    def test_get_table_sets_htmx_url(self):
        """get_table() sets htmx_url on the returned table."""
        view = _make_interface_view()
        view.request.path = "/dcim/devices/1/librenms-sync/"
        obj = MagicMock()
        obj.virtual_chassis = None

        with patch("netbox_librenms_plugin.views.object_sync.devices.LibreNMSInterfaceTable") as mock_table_cls:
            mock_table = MagicMock()
            mock_table_cls.return_value = mock_table
            view.get_table([], obj, "ifName")

        assert mock_table.htmx_url == "/dcim/devices/1/librenms-sync/?tab=interfaces&server_key=default"


class TestSingleInterfaceVerifyView:
    """Tests for SingleInterfaceVerifyView."""

    def _make_view(self):
        from unittest.mock import MagicMock

        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        view = object.__new__(SingleInterfaceVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        # Direct post() calls bypass dispatch() (which sets self.request), so null the object-perm
        # gate here; the dedicated perm test overrides it. Mirrors the other verify-view harnesses.
        view.require_object_permissions_json = MagicMock(return_value=None)
        return view

    def test_returns_400_when_no_device_id(self):
        """Returns 400 JSON error when no device_id in request body."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"interface_name": "eth0"}).encode()

        response = view.post(request)
        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_checks_permission_before_resolving_device(self):
        """The object-view gate must run BEFORE get_object_or_404 so an unauthorized caller can't probe arbitrary device IDs (existence via 404). Exercises the REAL require_object_permissions_json (only request.user.has_perm is mocked) — mocking the gate itself would mask a missing NetBoxObjectPermissionMixin base (AttributeError/500 in production)."""
        import json
        from unittest.mock import patch

        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        view = object.__new__(SingleInterfaceVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        request = MagicMock()
        request.body = json.dumps({"device_id": 999, "interface_name": "eth0"}).encode()
        request.user.has_perm.return_value = False  # unauthorized → real gate returns 403
        view.request = request  # check_object_permissions reads self.request.user

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404") as mock_get_obj:
            response = view.post(request)

        assert response.status_code == 403
        mock_get_obj.assert_not_called()  # device never resolved → no arbitrary-ID probing

    @pytest.mark.django_db
    def test_returns_404_when_no_cached_data(self):
        """Returns 404 when no cached ports exist for the (real) device."""
        from django.core.cache import cache as real_cache
        from django.http import JsonResponse

        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        device = _make_real_device("iface-nocache")
        view = SingleInterfaceVerifyView()
        request = _real_verify_request(
            {"device_id": device.pk, "interface_name": "eth0", "server_key": "default"}, "iface-nocache"
        )
        view.request = request

        real_cache.delete(view.get_cache_key(device, "ports", "default"))
        with _configured_default():
            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 404

    @pytest.mark.django_db
    def test_returns_404_when_interface_not_in_cache(self):
        """Returns 404 when the requested interface isn't among the real cached ports."""
        from django.core.cache import cache as real_cache
        from django.http import JsonResponse

        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        device = _make_real_device("iface-miss")
        view = SingleInterfaceVerifyView()
        request = _real_verify_request(
            {
                "device_id": device.pk,
                "interface_name": "eth99",
                "interface_name_field": "ifName",
                "server_key": "default",
            },
            "iface-miss",
        )
        view.request = request

        key = view.get_cache_key(device, "ports", "default")
        real_cache.set(key, {"ports": [{"ifName": "eth0", "speed": 1000}]})
        try:
            with _configured_default():
                response = view.post(request)
        finally:
            real_cache.delete(key)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 404

    @pytest.mark.django_db
    def test_returns_success_when_interface_found(self):
        """Returns success + formatted_row when the interface is in cache (real device, real cache, real table)."""
        import json

        from django.core.cache import cache as real_cache
        from django.http import JsonResponse

        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        device = _make_real_device("iface-hit")
        view = SingleInterfaceVerifyView()
        request = _real_verify_request(
            {
                "device_id": device.pk,
                "interface_name": "eth0",
                "interface_name_field": "ifName",
                "server_key": "default",
            },
            "iface-hit",
        )
        view.request = request

        key = view.get_cache_key(device, "ports", "default")
        real_cache.set(key, {"ports": [_real_port()]})
        try:
            with _configured_default():
                response = view.post(request)
        finally:
            real_cache.delete(key)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "success"
        assert "formatted_row" in data

    @pytest.mark.django_db
    def test_non_vc_device_uses_own_cache_key_no_sync_lookup(self):
        """A non-VC device is used directly: its own cache key hits and get_librenms_sync_device never runs."""
        from unittest.mock import patch

        from django.core.cache import cache as real_cache
        from django.http import JsonResponse

        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        device = _make_real_device("iface-nonvc")  # no virtual_chassis
        view = SingleInterfaceVerifyView()
        request = _real_verify_request(
            {
                "device_id": device.pk,
                "interface_name": "eth0",
                "interface_name_field": "ifName",
                "server_key": "default",
            },
            "iface-nonvc",
        )
        view.request = request

        key = view.get_cache_key(device, "ports", "default")  # keyed on the device itself
        real_cache.set(key, {"ports": [_real_port()]})
        try:
            with (
                _configured_default(),
                patch(
                    "netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device",
                    side_effect=AssertionError("get_librenms_sync_device must not run for a non-VC device"),
                ),
            ):
                response = view.post(request)
        finally:
            real_cache.delete(key)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200  # cache hit under the device's OWN key proves it was used directly


class TestSingleModuleVerifyView:
    """Tests for SingleModuleVerifyView."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import SingleModuleVerifyView

        view = object.__new__(SingleModuleVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.has_write_permission = MagicMock(return_value=True)
        view.require_object_permissions_json = MagicMock(return_value=None)
        view.get_cache_key = MagicMock(return_value="test_key")
        return view

    def test_checks_permission_before_resolving_device(self):
        """The object-view gate must run BEFORE get_object_or_404 so an unauthorized caller can't probe arbitrary device IDs (existence via 404). Exercises the real require_object_permissions_json (only request.user.has_perm is mocked)."""
        import json
        from unittest.mock import patch

        from netbox_librenms_plugin.views.object_sync.devices import SingleModuleVerifyView

        view = object.__new__(SingleModuleVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        request = MagicMock()
        request.body = json.dumps({"device_id": 999, "ent_physical_index": 10}).encode()
        request.user.has_perm.return_value = False  # unauthorized → real gate returns 403
        view.request = request  # check_object_permissions reads self.request.user

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404") as mock_get_obj:
            response = view.post(request)

        assert response.status_code == 403
        mock_get_obj.assert_not_called()  # device never resolved → no arbitrary-ID probing

    @pytest.mark.django_db
    def test_success_propagates_can_change_interface_to_verify_table(self):
        """Verify-row table keeps Update Interface available for a user with real change perms.

        Device is resolved through the real restrict() lookup and every table flag is driven by the
        view's REAL ``has_perm`` against a precise NetBox permission grant — only the module-inventory
        pipeline / table render (a separate rendering boundary) stays stubbed.
        """
        import json

        from dcim.models import Interface, Module, ModuleBayTemplate, ModuleType
        from django.apps import apps
        from django.http import JsonResponse
        from django.test import RequestFactory

        from netbox_librenms_plugin.models import (
            CarrierAutoInstallRule,
            ModuleBayMapping,
            ModuleTypeMapping,
        )
        from netbox_librenms_plugin.views.object_sync.devices import SingleModuleVerifyView

        # Resolve via the app registry, not the module attribute: the autouse mock_librenms_config
        # fixture patches netbox_librenms_plugin.models.LibreNMSSettings to a MagicMock in the full
        # suite, which would otherwise break ObjectType.get_for_model() in _user_with_perms.
        LibreNMSSettings = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")

        device = _make_real_device("mod-canchange")  # non-VC real device (with real device_type/manufacturer)
        user = _user_with_perms(
            "mod-canchange",
            [
                ("view", type(device)),  # dcim.Device — object-perm gate + restrict
                ("change", LibreNMSSettings),  # plugin write (has_write_permission)
                ("add", Module),
                ("change", Module),
                ("delete", Module),
                ("change", Interface),
                ("add", ModuleBayTemplate),
                ("add", ModuleType),
                ("add", CarrierAutoInstallRule),
                ("add", ModuleBayMapping),
                ("add", ModuleTypeMapping),
            ],
        )

        view = SingleModuleVerifyView()
        request = RequestFactory().post(
            "/verify-module/",
            data=json.dumps({"device_id": device.pk, "ent_physical_index": 10, "server_key": "default"}),
            content_type="application/json",
        )
        request.user = user
        view.request = request
        view.kwargs = {}
        view.args = ()

        inventory_data = [{"entPhysicalIndex": 10, "entPhysicalContainedIn": 0, "entPhysicalName": "Module 1"}]
        row = {"ent_physical_index": 10, "depth": 0, "status": "Installed"}
        mock_table = MagicMock()
        mock_table.format_module_data.return_value = "<tr>row</tr>"

        with (
            _configured_default(),
            patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache,
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[]),
            patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}),
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.LibreNMSModuleTable", return_value=mock_table
            ) as mock_table_cls,
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView._get_module_types",
                return_value={},
            ),
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView._find_transparent_indices",
                return_value=set(),
            ),
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView._collect_top_items",
                return_value=inventory_data,
            ),
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView._build_table_rows_for_member",
                return_value=[row],
            ),
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView._detect_serial_conflicts",
                return_value=None,
            ),
        ):
            mock_cache.get.return_value = {"inventory": inventory_data}
            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        mock_table_cls.assert_called_once_with(
            [],
            device=device,
            server_key="default",
            has_write_permission=True,
            can_add_module=True,
            can_change_module=True,
            can_change_interface=True,
            can_delete_module=True,
            can_add_module_bay_template=True,
            can_add_module_type=True,
            can_add_carrier_rule=True,
            can_add_module_bay_mapping=True,
            can_add_module_type_mapping=True,
        )

    def test_post_threads_active_server_key_into_row_builder(self):
        """On a non-default LibreNMS server, the POST server_key must reach the row builder."""
        import json

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1, "ent_physical_index": 10, "server_key": "prod"}).encode()
        request.user.has_perm = MagicMock(return_value=False)

        selected_device = MagicMock()
        selected_device.virtual_chassis = None
        selected_device.device_type = MagicMock()
        selected_device.device_type.manufacturer = MagicMock()
        inventory_data = [{"entPhysicalIndex": 10, "entPhysicalContainedIn": 0, "entPhysicalName": "Module 1"}]
        row = {"ent_physical_index": 10, "depth": 0, "status": "Installed"}

        captured = {}

        def _capture_server_key(child_view, *args, **kwargs):
            # autospec=True passes the bound DeviceModuleTableView instance as the first arg.
            captured["server_key"] = child_view._active_server_key
            return [row]

        mock_table = MagicMock()
        mock_table.format_module_data.return_value = "<tr>row</tr>"

        with (
            patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404", return_value=selected_device),
            patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache,
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[]),
            patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}),
            patch("netbox_librenms_plugin.views.object_sync.devices.LibreNMSModuleTable", return_value=mock_table),
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView._get_module_types",
                return_value={},
            ),
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView._find_transparent_indices",
                return_value=set(),
            ),
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView._collect_top_items",
                return_value=inventory_data,
            ),
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView._build_table_rows_for_member",
                autospec=True,
                side_effect=_capture_server_key,
            ),
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView._detect_serial_conflicts",
                return_value=None,
            ),
        ):
            mock_cache.get.return_value = {"inventory": inventory_data}
            view.post(request)

        # The POST-resolved "prod" server_key (not the default) reached the row builder.
        assert captured["server_key"] == "prod"


class TestSingleVlanGroupVerifyView:
    """Tests for SingleVlanGroupVerifyView."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import SingleVlanGroupVerifyView

        view = object.__new__(SingleVlanGroupVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        # Direct post() calls bypass dispatch() (which sets self.request), so null the object-perm
        # gate here; the dedicated perm test exercises the real gate. Mirrors the other harnesses.
        view.require_object_permissions_json = MagicMock(return_value=None)
        return view

    def test_checks_permission_before_resolving_device(self):
        """The object-view gate must run BEFORE get_object_or_404 so an unauthorized caller can't probe arbitrary device IDs (existence via 404). Exercises the real require_object_permissions_json (only request.user.has_perm is mocked)."""
        import json
        from unittest.mock import patch

        from netbox_librenms_plugin.views.object_sync.devices import SingleVlanGroupVerifyView

        view = object.__new__(SingleVlanGroupVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        request = MagicMock()
        request.body = json.dumps({"device_id": 999, "vid": "100"}).encode()
        request.user.has_perm.return_value = False  # unauthorized → real gate returns 403
        view.request = request

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404") as mock_get_obj:
            response = view.post(request)

        assert response.status_code == 403
        mock_get_obj.assert_not_called()  # device never resolved → no arbitrary-ID probing

    def test_returns_400_when_no_device_id(self):
        """Returns 400 when no device_id provided."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vid": "10"}).encode()

        response = view.post(request)
        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_returns_400_when_no_vid(self):
        """Returns 400 when no vid provided."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1}).encode()

        response = view.post(request)
        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_returns_400_when_invalid_vid(self):
        """Returns 400 when vid is not a valid integer (real device resolved through restrict())."""
        from django.http import JsonResponse

        from netbox_librenms_plugin.views.object_sync.devices import SingleVlanGroupVerifyView

        device = _make_real_device("vg-invalid-vid")
        view = SingleVlanGroupVerifyView()
        request = _real_verify_request(
            {"device_id": device.pk, "vid": "notanumber", "server_key": "default"}, "vg-invalid-vid"
        )
        view.request = request

        with _configured_default():
            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_returns_success_with_vlan_group(self):
        """A VID present in the selected group is reported not-missing — real Device/Interface/VLAN/VLANGroup, no mocks."""
        import json

        from dcim.models import Interface
        from django.http import JsonResponse
        from ipam.models import VLAN, VLANGroup

        device = _make_real_device("vg-with")
        Interface.objects.create(device=device, name="eth0", type="1000base-t")
        group = VLANGroup.objects.create(name="Grp-vg-with", slug="grp-vg-with")
        VLAN.objects.create(vid=10, name="ten", group=group)

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps(
            {"device_id": device.pk, "interface_name": "eth0", "vid": "10", "vlan_group_id": group.pk, "vlan_type": "U"}
        ).encode()
        request.user = _make_verify_superuser("vg-with")
        view.request = request
        response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "success"
        assert data["is_missing"] is False  # vid 10 exists in the selected group
        assert data["css_class"]  # a real CSS class was computed

    @pytest.mark.django_db
    def test_returns_success_without_vlan_group(self):
        """With no group selected and no global VLANs, the VID is reported missing — real DB, no mocks."""
        import json

        from django.http import JsonResponse

        device = _make_real_device("vg-without")

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": device.pk, "vid": "100", "vlan_type": "T"}).encode()
        request.user = _make_verify_superuser("vg-without")
        view.request = request
        response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "success"
        assert data["is_missing"] is True  # vid 100 not present in any global VLAN

    @pytest.mark.django_db
    def test_existing_interface_with_untagged_and_tagged_vlans(self):
        """The NetBox untagged + tagged VLAN assignments are read off a real interface (no mocks)."""
        import json

        from dcim.models import Interface
        from django.http import JsonResponse
        from ipam.models import VLAN, VLANGroup

        device = _make_real_device("vg-iface")
        group = VLANGroup.objects.create(name="Grp-vg-iface", slug="grp-vg-iface")
        untagged = VLAN.objects.create(vid=1, name="native", group=group)
        tagged = VLAN.objects.create(vid=10, name="ten", group=group)
        iface = Interface.objects.create(device=device, name="eth0", type="1000base-t", mode="tagged")
        iface.untagged_vlan = untagged
        iface.save()
        iface.tagged_vlans.add(tagged)

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps(
            {"device_id": device.pk, "vid": "10", "vlan_group_id": group.pk, "vlan_type": "T", "interface_name": "eth0"}
        ).encode()
        request.user = _make_verify_superuser("vg-iface")
        view.request = request
        response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "success"
        assert data["is_missing"] is False  # tagged vid 10 exists in the selected group

    def test_render_vlans_cell_returns_dash_for_empty_values(self):
        """Empty VLAN inputs render em dash placeholder."""
        view = self._make_view()
        assert view._render_vlans_cell(None, [], [], False, None, set()) == "—"


class TestVerifyVlanSyncGroupView:
    """Tests for VerifyVlanSyncGroupView."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import VerifyVlanSyncGroupView

        view = object.__new__(VerifyVlanSyncGroupView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        # Direct post() calls bypass dispatch() (which sets self.request), so null the object-perm
        # gate here; the dedicated perm test exercises the real gate. Mirrors the other harnesses.
        view.require_object_permissions_json = MagicMock(return_value=None)
        return view

    def test_checks_permission_before_resolving_group(self):
        """The object-view gate (on VLAN — no device here) must run BEFORE get_object_or_404 so an unauthorized caller can't enumerate VLANs/groups (existence via 404). Exercises the real require_object_permissions_json (only request.user.has_perm is mocked)."""
        import json
        from unittest.mock import patch

        from netbox_librenms_plugin.views.object_sync.devices import VerifyVlanSyncGroupView

        view = object.__new__(VerifyVlanSyncGroupView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        request = MagicMock()
        request.body = json.dumps({"vlan_group_id": 5, "vid": "100"}).encode()
        request.user.has_perm.return_value = False  # unauthorized → real gate returns 403
        view.request = request

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404") as mock_get_obj:
            response = view.post(request)

        assert response.status_code == 403
        mock_get_obj.assert_not_called()  # group never resolved → no enumeration

    def test_returns_400_when_no_vid(self):
        """Returns 400 when no vid provided."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vlan_group_id": "1"}).encode()

        response = view.post(request)
        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_returns_400_when_invalid_vid(self):
        """Returns 400 when vid is not a valid integer."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vid": "badvalue"}).encode()

        response = view.post(request)
        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_returns_success_with_vlan_group(self):
        """A VLAN that exists in the selected group with a matching name reports exists_in_netbox — real DB, no mocks."""
        import json

        from django.http import JsonResponse
        from ipam.models import VLAN, VLANGroup

        group = VLANGroup.objects.create(name="Grp-sync-with", slug="grp-sync-with")
        VLAN.objects.create(vid=10, name="vlan10", group=group)

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vid": "10", "vlan_group_id": group.pk, "name": "vlan10"}).encode()
        response = view.post(request)

        assert isinstance(response, JsonResponse)
        data = json.loads(response.content)
        assert response.status_code == 200
        assert data["status"] == "success"
        assert data["exists_in_netbox"] is True
        assert data["name_matches"] is True
        assert data["css_class"]  # a real CSS class was computed

    @pytest.mark.django_db
    def test_returns_success_without_vlan_group(self):
        """With no group and no matching global VLAN, exists_in_netbox is False — real DB, no mocks."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vid": "20", "name": "vlan20"}).encode()
        response = view.post(request)

        assert isinstance(response, JsonResponse)
        data = json.loads(response.content)
        assert response.status_code == 200
        assert data["status"] == "success"
        assert data["exists_in_netbox"] is False
        assert data["css_class"]  # a real CSS class was computed


class TestSaveVlanGroupOverridesView:
    """Tests for SaveVlanGroupOverridesView."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import SaveVlanGroupOverridesView

        view = object.__new__(SaveVlanGroupOverridesView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_returns_error_when_no_device_id(self):
        """Returns 400 error when no device_id in request."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vid_group_map": {}}).encode()

        with patch.object(view, "require_write_permission_json", return_value=None):
            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_requires_write_permission(self):
        """Returns error response when user lacks write permission."""
        import json

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1}).encode()

        error_response = MagicMock()
        with patch.object(view, "require_write_permission_json", return_value=error_response):
            result = view.post(request)

        assert result is error_response

    @pytest.mark.django_db
    def test_returns_error_when_no_cached_ports(self):
        """Returns 400 when ports cache TTL is zero (real device + real write-gate/restrict)."""
        from django.http import JsonResponse

        device = _make_real_device("ovr-nocache")
        view = self._make_view()
        request = _real_verify_request(
            {"device_id": device.pk, "vid_group_map": {"10": "5"}, "server_key": "default"}, "ovr-nocache"
        )
        view.request = request

        with (
            _configured_default(),
            patch("netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device", return_value=device),
            patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="ports_key"),
        ):
            mock_cache.ttl.return_value = 0
            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_post_does_not_500_on_cache_backend_without_ttl(self):
        """A cache backend without .ttl() (a REAL LocMemCache) degrades gracefully, not AttributeError-500."""
        from django.core.cache.backends.locmem import LocMemCache
        from django.http import JsonResponse

        device = _make_real_device("ovr-nottl")
        view = self._make_view()
        request = _real_verify_request(
            {"device_id": device.pk, "vid_group_map": {"10": "5"}, "server_key": "default"}, "ovr-nottl"
        )
        view.request = request
        # A REAL LocMemCache genuinely has no .ttl() — unlike a MagicMock cache, which fabricates one
        # and so masks this exact AttributeError.
        real_locmem = LocMemCache("test-no-ttl", {})

        with (
            _configured_default(),
            patch("netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device", return_value=device),
            patch("netbox_librenms_plugin.views.object_sync.devices.cache", real_locmem),
            patch.object(view, "get_cache_key", return_value="ports_key"),
        ):
            response = view.post(request)  # must not raise AttributeError

        # No ttl available -> treated as "no cached ports" -> graceful 400, never a 500.
        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_saves_overrides_to_cache(self):
        """Successfully saves VLAN group overrides to cache (real device + real write-gate/restrict)."""
        import json

        from django.http import JsonResponse

        device = _make_real_device("ovr-save")
        view = self._make_view()
        request = _real_verify_request(
            {"device_id": device.pk, "vid_group_map": {"10": "5", "20": "5"}, "server_key": "default"}, "ovr-save"
        )
        view.request = request

        with (
            _configured_default(),
            patch("netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device", return_value=device),
            patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="ports_key"),
            patch.object(view, "get_vlan_overrides_key", return_value="vlan_overrides_key"),
        ):
            mock_cache.ttl.return_value = 300
            mock_cache.get.return_value = {}
            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "success"
        mock_cache.set.assert_called_once()

    @pytest.mark.django_db
    def test_save_overrides_uses_device_when_sync_device_none(self):
        """If VC sync-device resolution fails, fallback uses the (real) selected device."""
        from django.http import JsonResponse

        device = _make_real_device("ovr-syncnone")
        view = self._make_view()
        request = _real_verify_request(
            {"device_id": device.pk, "vid_group_map": {"10": "5"}, "server_key": "default"}, "ovr-syncnone"
        )
        view.request = request

        with (
            _configured_default(),
            patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device", return_value=None
            ) as mock_get_sync_device,
            patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="ports_key"),
            patch.object(view, "get_vlan_overrides_key", return_value="vlan_overrides_key"),
        ):
            mock_cache.ttl.return_value = 300
            mock_cache.get.return_value = {}
            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        # Fallback: get_librenms_sync_device was called with the selected (real) device.
        mock_get_sync_device.assert_called_once()
        assert mock_get_sync_device.call_args[0][0].pk == device.pk


class TestDeviceCableTableView:
    """Tests for DeviceCableTableView."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        view = object.__new__(DeviceCableTableView)
        view._librenms_api = MagicMock()
        return view

    def test_get_table_returns_vc_cable_table_for_vc_device(self):
        """get_table() returns VCCableTable when device has virtual_chassis."""
        view = self._make_view()
        obj = MagicMock()
        obj.virtual_chassis = MagicMock()

        with patch("netbox_librenms_plugin.views.object_sync.devices.VCCableTable") as mock_vc_table:
            mock_table = MagicMock()
            mock_vc_table.return_value = mock_table
            result = view.get_table([], obj)

        assert result is mock_table
        mock_vc_table.assert_called_once_with([], device=obj)

    def test_get_table_returns_librenms_cable_table_for_non_vc_device(self):
        """get_table() returns LibreNMSCableTable when no virtual_chassis."""
        view = self._make_view()
        obj = MagicMock()
        obj.virtual_chassis = None

        with patch("netbox_librenms_plugin.views.object_sync.devices.LibreNMSCableTable") as mock_cable_table:
            mock_table = MagicMock()
            mock_cable_table.return_value = mock_table
            result = view.get_table([], obj)

        assert result is mock_table
        mock_cable_table.assert_called_once_with([], device=obj)


class TestDeviceModuleTableView:
    """Tests for DeviceModuleTableView."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        view = object.__new__(DeviceModuleTableView)
        view.request = MagicMock()
        view.request.path = "/dcim/devices/1/librenms-sync/"
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "prod-server"
        view.has_write_permission = MagicMock(return_value=True)
        view.request.user.has_perm = MagicMock(
            side_effect=lambda p: (
                p
                in {
                    "dcim.add_module",
                    "dcim.change_module",
                    "dcim.change_interface",
                    "dcim.delete_module",
                    "dcim.add_modulebaytemplate",
                    "dcim.add_moduletype",
                    "netbox_librenms_plugin.add_carrierautoinstallrule",
                    "netbox_librenms_plugin.add_modulebaymapping",
                    "netbox_librenms_plugin.add_moduletypemapping",
                }
            )
        )
        return view

    def test_get_table_returns_librenms_module_table(self):
        """get_table() returns LibreNMSModuleTable with device and server_key."""
        view = self._make_view()
        obj = MagicMock()
        obj.virtual_chassis = None

        with patch("netbox_librenms_plugin.views.object_sync.devices.LibreNMSModuleTable") as mock_table_cls:
            mock_table = MagicMock()
            mock_table_cls.return_value = mock_table
            result = view.get_table([], obj)

        mock_table_cls.assert_called_once_with(
            [],
            device=obj,
            server_key="prod-server",
            has_write_permission=True,
            can_add_module=True,
            can_change_module=True,
            can_change_interface=True,
            can_delete_module=True,
            can_add_module_bay_template=True,
            can_add_module_type=True,
            can_add_carrier_rule=True,
            can_add_module_bay_mapping=True,
            can_add_module_type_mapping=True,
        )
        assert result is mock_table

    def test_get_table_sets_htmx_url(self):
        """get_table() sets htmx_url with modules tab."""
        view = self._make_view()
        obj = MagicMock()
        obj.virtual_chassis = None

        with patch("netbox_librenms_plugin.views.object_sync.devices.LibreNMSModuleTable") as mock_table_cls:
            mock_table = MagicMock()
            mock_table_cls.return_value = mock_table
            view.get_table([], obj)

        assert mock_table.htmx_url == "/dcim/devices/1/librenms-sync/?tab=modules&server_key=prod-server"
