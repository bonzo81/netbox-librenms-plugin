"""Coverage tests for views/object_sync/devices.py."""

from unittest.mock import MagicMock, patch

import pytest


from netbox_librenms_plugin.tests.view_test_helpers import post as _post


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
        "port_id": 4001,
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

    def test_denied_without_view_device_permission(self):
        """The verify endpoint must reject a user lacking dcim.view_device (no probing arbitrary device IDs for cached interface data)."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        # Simulate the real object-permission gate denying access.
        view.require_object_permissions_json = MagicMock(
            return_value=JsonResponse({"error": "Missing permissions: dcim.view_device"}, status=403)
        )
        request = MagicMock()
        # port_id is supplied so the 404 can only come from the malformed-payload guard, not
        # from the missing-port_id guard that also returns 404.
        request.body = json.dumps({"device_id": 1, "interface_name": "eth0", "port_id": 10}).encode()

        mock_device = MagicMock()
        mock_device.virtual_chassis = None
        with patch.object(view, "restrict_object_or_404", return_value=mock_device) as mock_get_obj:
            with patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache:
                response = view.post(request)

        assert response.status_code == 403
        # Must deny before resolving the device (no ID probing) and before the cache.
        mock_get_obj.assert_not_called()
        mock_cache.get.assert_not_called()

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
        view.require_object_permissions_json.assert_called_once_with("POST")

    def test_checks_permission_before_resolving_device(self):
        """The object-view gate must run BEFORE restrict_object_or_404 so an unauthorized caller can't probe arbitrary device IDs (existence via 404). Exercises the REAL require_object_permissions_json (only request.user.has_perm is mocked) — mocking the gate itself would mask a missing NetBoxObjectPermissionMixin base (AttributeError/500 in production)."""
        import json
        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        view = object.__new__(SingleInterfaceVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        request = MagicMock()
        request.body = json.dumps({"device_id": 999, "interface_name": "eth0"}).encode()
        request.user.has_perm.return_value = False  # unauthorized → real gate returns 403
        view.request = request  # check_object_permissions reads self.request.user
        view.restrict_object_or_404 = MagicMock()

        response = view.post(request)

        assert response.status_code == 403
        view.restrict_object_or_404.assert_not_called()  # device never resolved → no arbitrary-ID probing

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

    def test_returns_404_when_cached_data_malformed(self):
        """A truthy but malformed cache value (non-dict) is treated as a miss (404), not a 500 — reading .get() on it would raise AttributeError."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1, "interface_name": "eth0"}).encode()

        mock_device = MagicMock()
        mock_device.virtual_chassis = None

        with patch.object(view, "restrict_object_or_404", return_value=mock_device):
            with patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device", return_value=mock_device
            ):
                with patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache:
                    # A truthy non-dict snapshot: the old `if cached_data:` guard would enter and
                    # raise on `cached_data.get("ports", [])`.
                    mock_cache.get.return_value = ["not", "a", "dict"]
                    with patch.object(view, "get_cache_key", return_value="test_key"):
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
                "port_id": 4001,
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
                "port_id": 4001,
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

    @pytest.mark.django_db
    def test_resolves_netbox_interface_by_stable_port_id_not_name(self):
        """The NetBox interface is matched by its stored LibreNMS port_id, not the display name — a naming-mode mismatch (synced under ifName, viewed under ifDescr) must not mis-resolve onto a same-display-named decoy. Driven through the real view/DB/render so the assertion is on rendered behaviour, not mock call patterns."""
        import json

        from django.core.cache import cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("verify-portid-host")
        # Decoy: its NetBox name equals the ifDescr display the client posts, but it carries the
        # WRONG LibreNMS port_id and is DISABLED — a name-based lookup would wrongly land here.
        decoy = make_interface(device, "GigabitEthernet0/1")
        decoy.enabled = False
        set_librenms_device_id(decoy, 7, "default")  # mutates custom_field_data in memory only
        decoy.save()
        # Correct: stored LibreNMS port_id 42 identifies it, though its NetBox name ("Gi0/1")
        # differs from the current ifDescr display. ENABLED, matching the port's up state.
        correct = make_interface(device, "Gi0/1")
        correct.enabled = True
        set_librenms_device_id(correct, 42, "default")
        correct.save()

        port_data = {
            "ifDescr": "GigabitEthernet0/1",
            "ifName": "Gi0/1",
            "port_id": 42,
            "ifSpeed": 1000,
            "ifType": "ethernetCsmacd",
            "ifAlias": "",
            "ifPhysAddress": "",
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "_source": "host",
        }

        view = self._make_view()
        # cache.set() writes the real cache backend (NOT rolled back with the test DB); clean
        # the key up even on assertion failure so a reused device PK cannot read stale ports.
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, {"ports": [port_data]})
        try:
            request = RequestFactory().post(
                "/plugins/librenms_plugin/verify-interface/",
                data=json.dumps(
                    {
                        "device_id": device.pk,
                        "interface_name": "GigabitEthernet0/1",
                        "interface_name_field": "ifDescr",
                        "port_id": 42,
                        "server_key": "default",
                    }
                ),
                content_type="application/json",
            )
            request.user = _make_verify_superuser("verify-portid")
            response = view.post(request)

            assert response.status_code == 200
            formatted = json.loads(response.content)["formatted_row"]
            # The 'enabled' badge compares the RESOLVED interface's enabled flag against the port's
            # up state. Resolving by stable port_id picks the enabled id-42 interface → green match;
            # a display-name fallback would pick the disabled decoy → an orange mismatch. So a green
            # badge proves port_id precedence over the name lookup — asserted on real rendered output.
            assert "text-success" in formatted["enabled"]
            assert "text-warning" not in formatted["enabled"]
        finally:
            cache.delete(cache_key)

    @pytest.mark.django_db
    def test_verify_repaint_payload_includes_member_specific_librenms_id_cell(self):
        """The verify response's formatted_row must carry a librenms_id cell so a VC member switch can repaint the (member-specific) badge — its colour compares this port_id against the resolved member's stored librenms_id, so omitting it strands the previous member's match/mismatch state on screen."""
        import json

        from django.core.cache import cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("verify-badge-host")
        iface = make_interface(device, "Gi0/1")
        set_librenms_device_id(iface, 42, "default")  # mutates custom_field_data in memory only
        iface.save()

        port_data = {
            "ifDescr": "Gi0/1",
            "ifName": "Gi0/1",
            "port_id": 42,
            "ifSpeed": 1000,
            "ifType": "ethernetCsmacd",
            "ifAlias": "",
            "ifPhysAddress": "",
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "_source": "host",
        }

        view = self._make_view()
        # cache.set() writes the real cache backend (NOT rolled back with the test DB); clean
        # the key up even on assertion failure so a reused device PK cannot read stale ports.
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, {"ports": [port_data]})
        try:
            request = RequestFactory().post(
                "/plugins/librenms_plugin/verify-interface/",
                data=json.dumps(
                    {
                        "device_id": device.pk,
                        "interface_name": "Gi0/1",
                        "interface_name_field": "ifName",
                        "port_id": 42,
                        "server_key": "default",
                    }
                ),
                content_type="application/json",
            )
            request.user = _make_verify_superuser("verify-badge")
            response = view.post(request)

            assert response.status_code == 200
            formatted = json.loads(response.content)["formatted_row"]
            # The cell must be present (it was previously dropped from the repaint payload) and
            # reflect THIS member: port_id 42 matches the resolved interface's stored librenms_id 42,
            # so the badge is the green "match".
            assert "librenms_id" in formatted, "verify repaint payload dropped the librenms_id cell"
            assert "text-success" in formatted["librenms_id"]
        finally:
            cache.delete(cache_key)

    @pytest.mark.django_db
    def test_verify_repaint_resolves_the_selected_vc_members_librenms_id(self):
        """Through a REAL virtual chassis: the repaint badge must reflect the SELECTED member's interface (owner-pinned resolution), while the cache key normalizes to the VC sync device. A decoy same-named interface on the sync master carries a DIFFERENT stored librenms_id, so any regression that resolves against the sync device instead of the member renders the orange mismatch, not the green match asserted here."""
        import json

        from django.core.cache import cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_virtual_chassis
        from netbox_librenms_plugin.utils import set_librenms_device_id

        master = make_device("verify-vc-master")
        member = make_device("verify-vc-member")
        make_virtual_chassis("verify-vc", master, member)
        # Device-level librenms_id makes the MASTER the VC sync device (priority 1 in
        # get_librenms_sync_device), so the ports cache lives under the master's key.
        set_librenms_device_id(master, 4242, "default")  # in-memory CF mutation...
        master.save()  # ...must be persisted for the sync-device lookup to see it

        # Decoy on the sync master: same display name, DIFFERENT stored port id. Resolving
        # against the master would match this row and paint the mismatch badge.
        decoy = make_interface(master, "Gi0/1")
        set_librenms_device_id(decoy, 99, "default")
        decoy.save()

        iface = make_interface(member, "Gi0/1")
        set_librenms_device_id(iface, 42, "default")
        iface.save()

        port_data = {
            "ifDescr": "Gi0/1",
            "ifName": "Gi0/1",
            "port_id": 42,
            "ifSpeed": 1000,
            "ifType": "ethernetCsmacd",
            "ifAlias": "",
            "ifPhysAddress": "",
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "_source": "host",
        }

        view = self._make_view()
        # Cache under the SYNC device (master) — the view normalizes the selected member to it.
        cache_key = view.get_cache_key(master, "ports", "default")
        cache.set(cache_key, {"ports": [port_data]})
        try:
            request = RequestFactory().post(
                "/plugins/librenms_plugin/verify-interface/",
                data=json.dumps(
                    {
                        "device_id": member.pk,
                        "interface_name": "Gi0/1",
                        "interface_name_field": "ifName",
                        "port_id": 42,
                        "server_key": "default",
                    }
                ),
                content_type="application/json",
            )
            request.user = _make_verify_superuser("verify-vc")
            response = view.post(request)

            assert response.status_code == 200
            formatted = json.loads(response.content)["formatted_row"]
            assert "librenms_id" in formatted, "verify repaint payload dropped the librenms_id cell"
            # Green = matched the MEMBER's stored id (42). The master's decoy stores 99, so a
            # sync-device-resolved badge would be the orange mismatch instead.
            assert "text-success" in formatted["librenms_id"]
            assert "text-warning" not in formatted["librenms_id"]
        finally:
            cache.delete(cache_key)


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
        """The object-view gate must run BEFORE restrict_object_or_404 so an unauthorized caller can't probe arbitrary device IDs (existence via 404). Exercises the real require_object_permissions_json (only request.user.has_perm is mocked)."""
        import json
        from netbox_librenms_plugin.views.object_sync.devices import SingleModuleVerifyView

        view = object.__new__(SingleModuleVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        request = MagicMock()
        request.body = json.dumps({"device_id": 999, "ent_physical_index": 10}).encode()
        request.user.has_perm.return_value = False  # unauthorized → real gate returns 403
        view.request = request  # check_object_permissions reads self.request.user
        view.restrict_object_or_404 = MagicMock()

        response = view.post(request)

        assert response.status_code == 403
        view.restrict_object_or_404.assert_not_called()  # device never resolved → no arbitrary-ID probing

    @pytest.mark.django_db
    def test_success_propagates_can_change_interface_to_verify_table(self):
        """Verify-row table keeps Update Interface available for a user with real change perms.

        Device is resolved through the real restrict() lookup and every table flag is driven by the
        view's REAL ``has_perm`` against a precise NetBox permission grant — only the module-inventory
        pipeline / table render (a separate rendering boundary) stays stubbed.
        """
        import json

        from dcim.models import Interface, Module, ModuleBayTemplate, ModuleType
        from django.http import JsonResponse
        from django.test import RequestFactory

        from netbox_librenms_plugin.models import (
            CarrierAutoInstallRule,
            LibreNMSSettings,
            ModuleBayMapping,
            ModuleTypeMapping,
        )
        from netbox_librenms_plugin.views.object_sync.devices import SingleModuleVerifyView

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
            patch.object(view, "restrict_object_or_404", return_value=selected_device),
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers", return_value={"prod": "Prod"}
            ),
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

        with patch.object(view, "restrict_object_or_404") as mock_get_obj:
            response = view.post(request)

        assert response.status_code == 403
        mock_get_obj.assert_not_called()  # device never resolved → no arbitrary-ID probing

    def test_declares_vlan_group_read_permission(self):
        from ipam.models import VLANGroup

        from netbox_librenms_plugin.views.object_sync.devices import SingleVlanGroupVerifyView

        assert ("view", VLANGroup) in SingleVlanGroupVerifyView.required_object_permissions["POST"]

    @pytest.mark.django_db
    def test_denies_a_user_without_vlan_read_permission(self):
        """The verify endpoint must gate its VLAN membership reads."""
        from dcim.models import Device
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.views.object_sync.devices import SingleVlanGroupVerifyView

        device = _make_real_device("vg-no-vlan-perm")
        user = _user_with_perms(
            "vg-no-vlan-perm",
            [("view", Device), ("view", VLANGroup)],
        )
        request = _real_verify_request(
            {"device_id": device.pk, "vid": "100", "server_key": "default"},
            "vg-no-vlan-request",
        )
        request.user = user
        view = SingleVlanGroupVerifyView()
        view.setup(request)

        assert user.has_perm("dcim.view_device") is True
        assert user.has_perm("ipam.view_vlangroup") is True
        assert user.has_perm("ipam.view_vlan") is False
        assert ("view", VLAN) in view.required_object_permissions["POST"]
        assert view.check_object_permissions("POST")[0] is False

        response = view.post(request)

        assert response.status_code == 403

    @pytest.mark.django_db
    def test_a_hidden_vlan_is_not_reported_as_available(self):
        """A constrained VLAN grant must not disclose another VLAN through its VID."""
        import json

        from dcim.models import Device
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.tests.view_test_helpers import grant
        from netbox_librenms_plugin.views.object_sync.devices import SingleVlanGroupVerifyView

        device = _make_real_device("vg-hidden")
        group = VLANGroup.objects.create(name="Grp-vg-hidden", slug="grp-vg-hidden")
        visible = VLAN.objects.create(vid=10, name="visible", group=group)
        hidden = VLAN.objects.create(vid=20, name="hidden", group=group)
        user = _user_with_perms(
            "vg-hidden",
            [("view", Device), ("view", VLANGroup)],
        )
        user = grant(user, "view", VLAN, constraints={"pk": visible.pk})
        request = _real_verify_request(
            {
                "device_id": device.pk,
                "vid": str(hidden.vid),
                "vlan_group_id": group.pk,
                "vlan_type": "U",
            },
            "vg-hidden-request",
        )
        request.user = user
        view = SingleVlanGroupVerifyView()
        view.setup(request)

        response = view.post(request)

        assert response.status_code == 200
        assert json.loads(response.content)["is_missing"] is True

        visible_request = _real_verify_request(
            {
                "device_id": device.pk,
                "vid": str(visible.vid),
                "vlan_group_id": group.pk,
                "vlan_type": "U",
            },
            "vg-visible-request",
        )
        visible_request.user = user
        visible_view = SingleVlanGroupVerifyView()
        visible_view.setup(visible_request)

        visible_response = visible_view.post(visible_request)

        assert json.loads(visible_response.content)["is_missing"] is False

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
    def test_returns_400_when_vlan_group_id_is_not_an_integer(self):
        """Reject a malformed group ID before it reaches the restricted ORM lookup."""
        import json

        from netbox_librenms_plugin.views.object_sync.devices import SingleVlanGroupVerifyView

        device = _make_real_device("vg-invalid-group")
        request = _real_verify_request(
            {"device_id": device.pk, "vid": "100", "vlan_group_id": "not-an-id"},
            "vg-invalid-group",
        )
        view = SingleVlanGroupVerifyView()
        view.setup(request)

        response = view.post(request)

        assert response.status_code == 400
        assert json.loads(response.content)["message"] == "Invalid VLAN group ID"

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
        response = _post(view, request)

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

    def test_declares_vlan_group_read_permission(self):
        from ipam.models import VLANGroup

        from netbox_librenms_plugin.views.object_sync.devices import VerifyVlanSyncGroupView

        assert ("view", VLANGroup) in VerifyVlanSyncGroupView.required_object_permissions["POST"]

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

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404"
        ) as mock_get_obj:
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
    def test_returns_400_when_vlan_group_id_is_not_an_integer(self):
        """Reject a malformed group ID before it reaches the restricted ORM lookup."""
        import json

        from netbox_librenms_plugin.views.object_sync.devices import VerifyVlanSyncGroupView

        request = _real_verify_request(
            {"vid": "100", "vlan_group_id": "not-an-id"},
            "sync-invalid-group",
        )
        view = VerifyVlanSyncGroupView()
        view.setup(request)

        response = view.post(request)

        assert response.status_code == 400
        assert json.loads(response.content)["message"] == "Invalid VLAN group ID"

    @pytest.mark.django_db
    def test_returns_success_with_vlan_group(self):
        """A VLAN that exists in the selected group with a matching name reports exists_in_netbox — real DB, no mocks."""
        import json

        from django.http import JsonResponse
        from ipam.models import VLAN, VLANGroup

        group = VLANGroup.objects.create(name="Grp-sync-with", slug="grp-sync-with")
        VLAN.objects.create(vid=10, name="vlan10", group=group)

        view = self._make_view()
        request = _real_verify_request({"vid": "10", "vlan_group_id": group.pk, "name": "vlan10"}, "sync-group-with")
        response = _post(view, request)

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
        request = _real_verify_request({"vid": "20", "name": "vlan20"}, "sync-group-without")
        response = _post(view, request)

        assert isinstance(response, JsonResponse)
        data = json.loads(response.content)
        assert response.status_code == 200
        assert data["status"] == "success"
        assert data["exists_in_netbox"] is False
        assert data["css_class"]  # a real CSS class was computed

    @pytest.mark.django_db
    def test_a_hidden_vlan_is_not_returned_from_the_group_lookup(self):
        """The standalone VLAN verifier must apply the constrained VLAN grant too."""
        import json

        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.tests.view_test_helpers import grant
        from netbox_librenms_plugin.views.object_sync.devices import VerifyVlanSyncGroupView

        group = VLANGroup.objects.create(name="Grp-sync-hidden", slug="grp-sync-hidden")
        visible = VLAN.objects.create(vid=10, name="visible", group=group)
        hidden = VLAN.objects.create(vid=20, name="hidden", group=group)
        user = _user_with_perms("sync-hidden", [("view", VLANGroup)])
        user = grant(user, "view", VLAN, constraints={"pk": visible.pk})
        request = _real_verify_request(
            {"vid": str(hidden.vid), "vlan_group_id": group.pk, "name": hidden.name},
            "sync-hidden-request",
        )
        request.user = user
        view = VerifyVlanSyncGroupView()
        view.setup(request)

        response = view.post(request)

        data = json.loads(response.content)
        assert response.status_code == 200
        assert data["exists_in_netbox"] is False
        assert data["name_matches"] is False

        visible_request = _real_verify_request(
            {"vid": str(visible.vid), "vlan_group_id": group.pk, "name": visible.name},
            "sync-visible-request",
        )
        visible_request.user = user
        visible_view = VerifyVlanSyncGroupView()
        visible_view.setup(visible_request)

        visible_response = visible_view.post(visible_request)

        assert json.loads(visible_response.content)["exists_in_netbox"] is True


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


@pytest.mark.django_db
class TestSaveVlanGroupOverridesRealCacheBackend:
    """Drive SaveVlanGroupOverridesView against a REAL cache backend, not a MagicMock.

    The MagicMock-cache tests above synthesise a ``.ttl()`` on the cache; ``cache.ttl()``
    is a django-redis extension that every other Django backend (e.g. the LocMemCache
    NetBox falls back to) lacks, so those tests stay green even though the raw ``cache.ttl()``
    call raised ``AttributeError`` mid-request. These exercise the real view against a real
    LocMemCache so the backend-agnostic ``cache_remaining_ttl`` guard is actually tested.
    """

    def _post(self, device, cache_backend, *, vid_group_map=None):
        import json

        from django.test import RequestFactory

        from netbox_librenms_plugin.views.object_sync.devices import SaveVlanGroupOverridesView

        request = RequestFactory().post(
            "/save-vlan-group-overrides/",
            data=json.dumps(
                {
                    "device_id": device.pk,
                    "vid_group_map": vid_group_map or {"10": "5"},
                    "server_key": "default",
                }
            ),
            content_type="application/json",
        )
        view = object.__new__(SaveVlanGroupOverridesView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        request.user = _make_verify_superuser(device.name)
        view.request = request
        with patch.object(view, "require_write_permission_json", return_value=None):
            with patch("netbox_librenms_plugin.views.object_sync.devices.cache", cache_backend):
                return view.post(request)

    def test_backend_without_ttl_returns_graceful_400_not_attributeerror(self):
        """A ttl-less backend (LocMemCache) yields the graceful 400 — the old cache.ttl() raised."""
        from django.core.cache.backends.locmem import LocMemCache
        from django.http import JsonResponse

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("vlan-override-locmem")
        real_locmem = LocMemCache("vlan-override-real", {})
        # Precondition that made the old code crash: a real non-redis backend has no ttl().
        assert not hasattr(real_locmem, "ttl")

        response = self._post(device, real_locmem)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_backend_with_ttl_saves_overrides(self):
        """A backend that exposes ttl() (redis-like) still saves through the shared guard."""
        import json

        from django.core.cache.backends.locmem import LocMemCache
        from django.http import JsonResponse

        from netbox_librenms_plugin.tests.conftest import make_device

        class _TtlLocMemCache(LocMemCache):
            """Real LocMemCache with a redis-like ttl() so the success path stays real, not mocked."""

            def ttl(self, key):
                return 300

        device = make_device("vlan-override-ttl")
        redis_like = _TtlLocMemCache("vlan-override-ttl-real", {})

        response = self._post(device, redis_like, vid_group_map={"10": "5", "20": "5"})

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        assert json.loads(response.content)["status"] == "success"


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


# ---------------------------------------------------------------------------
# DeviceIPAddressTableView cached-snapshot handling (real DB + real cache)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestIpCachedSnapshotMgmtIpBackfill:
    """A pre-upgrade IP snapshot lacking the mgmt_ip key must resolve it on read, not silently skip auto-select."""

    def _view(self, mgmt_ip_resolves_to="10.0.0.9"):
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        view = DeviceIPAddressTableView()
        # The LibreNMS client is the external boundary — mock it; everything else is real.
        api = MagicMock(server_key="default", cache_timeout=300)
        api.get_stored_librenms_id.return_value = 7
        api.get_device_info.return_value = (True, {"ip": mgmt_ip_resolves_to})
        view._librenms_api = api
        request = RequestFactory().get("/")
        request.user = make_superuser()
        view.request = request
        return view, api, request

    def test_missing_mgmt_ip_key_resolves_on_cached_render(self):
        from django.core.cache import cache as real_cache

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("ip-preupgrade")
        view, api, request = self._view()
        key = view.get_cache_key(device, "ip_addresses", "default")
        # Pre-upgrade snapshot: NO "mgmt_ip" key.
        real_cache.set(key, {"ip_addresses": [], "ports_by_id": {"7": {}}}, timeout=300)
        try:
            # This test exercises the django-redis-style positive-TTL path explicitly. Other
            # backends intentionally skip the backfill because Django's core cache API has no TTL
            # introspection.
            with patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.cache_remaining_ttl",
                return_value=300,
            ):
                view._prepare_context(request, device, "ifName", fetch_fresh=False, server_key="default")
                # The missing key triggered a one-time live resolve of the management IP...
                api.get_device_info.assert_called_once_with(7)
                # ...and the resolved VALUE was backfilled into the re-cached snapshot...
                assert real_cache.get(key)["mgmt_ip"] == "10.0.0.9"
                # ...so the next cached render reads it WITHOUT a second LibreNMS round-trip. Proving
                # the backfill is consumed is the point of storing it; asserting only the stored value
                # would still pass if every render re-resolved.
                api.get_device_info.reset_mock()
                view._prepare_context(request, device, "ifName", fetch_fresh=False, server_key="default")
                api.get_device_info.assert_not_called()
        finally:
            real_cache.delete(key)

    def test_present_mgmt_ip_key_does_not_resolve(self):
        from django.core.cache import cache as real_cache

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("ip-postupgrade")
        view, api, request = self._view()
        key = view.get_cache_key(device, "ip_addresses", "default")
        # Complete snapshot: mgmt_ip already stored (even empty "" must be honoured, not re-resolved).
        real_cache.set(key, {"ip_addresses": [], "mgmt_ip": "", "ports_by_id": {"7": {}}}, timeout=300)
        try:
            view._prepare_context(request, device, "ifName", fetch_fresh=False, server_key="default")
            api.get_device_info.assert_not_called()
        finally:
            real_cache.delete(key)


@pytest.mark.django_db
class TestIpCachedSnapshotFailsClosedOnMalformedCache:
    """A stale/corrupt truthy cache value (list/str/wrong-shaped dict) must fail closed, not 500 the render."""

    def _view(self):
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        view = DeviceIPAddressTableView()
        # cache_timeout/server_key only; the cached path must NOT reach LibreNMS for a corrupt entry.
        view._librenms_api = MagicMock(server_key="default", cache_timeout=300)
        request = RequestFactory().get("/")
        request.user = make_superuser()
        view.request = request
        return view, request

    @pytest.mark.parametrize(
        "bad_value",
        [
            ["junk"],  # a list — .get(...) would AttributeError on the old code
            "corrupt-string",  # a str — same crash class
            {"ports_by_id": {"7": {}}},  # dict missing the "ip_addresses" list
            {"ip_addresses": "not-a-list"},  # dict whose ip_addresses isn't a list
            # Container is valid but a nested field is corrupt — these reach enrichment on the
            # old (container-only) check and 500 the tab; the per-row/ports_by_id/mgmt_ip
            # validation must now fail them closed too.
            {"ip_addresses": [{"port_id": 7}]},  # row has port_id but no addr pair → KeyError in _create_base_ip_entry
            # unhashable port_id → TypeError in `port_id not in port_data_cache`
            {"ip_addresses": [{"port_id": [], "ip_address": "1.1.1.1", "prefix_length": 24}]},
            {"ip_addresses": [], "ports_by_id": ["bad"]},  # non-mapping ports_by_id → dict(["bad"]) ValueError
            {"ip_addresses": [], "mgmt_ip": 123},  # non-str mgmt_ip → bad deref/auto-select
        ],
    )
    def test_malformed_cache_returns_none_and_purges_key(self, bad_value):
        from django.core.cache import cache as real_cache

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("ip-corruptcache")
        view, request = self._view()
        key = view.get_cache_key(device, "ip_addresses", "default")
        real_cache.set(key, bad_value, timeout=300)
        try:
            result = view._prepare_context(request, device, "ifName", fetch_fresh=False, server_key="default")
            # Fail closed: treated as a cache miss (None), never crashing the tab render.
            assert result is None
            # The corrupt entry is purged so the next GET doesn't keep serving garbage.
            assert real_cache.get(key) is None
        finally:
            real_cache.delete(key)


# ---------------------------------------------------------------------------
# conftest.make_superuser — idempotent superuser builder
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestMakeSuperuserHelperIsIdempotent:
    """conftest.make_superuser() must reuse/correct a pre-existing inactive 'review-su' row, not trip the unique constraint."""

    def test_reactivates_existing_inactive_review_user(self):
        from django.contrib.auth import get_user_model

        from netbox_librenms_plugin.tests.conftest import make_superuser

        User = get_user_model()
        # An earlier test left an inactive review-su; and there is no other active superuser, so the
        # filter short-circuit misses and the helper reaches the get-or-create path.
        User.objects.filter(is_superuser=True, is_active=True).delete()
        User.objects.create(username="review-su", is_superuser=False, is_active=False)

        user = make_superuser()  # bare create() would raise IntegrityError on the duplicate username

        assert user.username == "review-su"
        assert user.is_superuser and user.is_active
        # No duplicate row was created.
        assert User.objects.filter(username="review-su").count() == 1
