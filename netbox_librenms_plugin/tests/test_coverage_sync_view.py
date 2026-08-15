"""Coverage tests for views/base/librenms_sync_view.py missing lines."""

from unittest.mock import MagicMock, patch

import pytest
from dcim.models import Device

from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
from django.test import RequestFactory


def _make_view():
    """Create a BaseLibreNMSSyncView instance bypassing __init__."""
    from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

    view = object.__new__(BaseLibreNMSSyncView)
    view.request = MagicMock()
    view.tab = "librenms_sync"
    view.model = MagicMock()
    view.queryset = MagicMock()
    view.kwargs = {}
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    view._librenms_api.librenms_url = "https://x.example.com"
    view._librenms_api.cache_timeout = 300
    return view


class TestBaseLibreNMSSyncViewGet:
    """Tests for get() method (lines 29-53)."""

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.render")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView.get_object")
    def test_get_non_vc_device(self, mock_get_obj, mock_render):
        """Non-VC device: librenms_lookup_device stays as obj."""
        view = _make_view()

        obj = MagicMock()
        obj.virtual_chassis = None
        mock_get_obj.return_value = obj

        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.get_librenms_id.return_value = 42

        view.get_context_data = MagicMock(return_value={"test": "ctx"})
        mock_render.return_value = MagicMock()

        request = RequestFactory().get("/")
        view.get(request, pk=1)

        # lookup device should be obj
        assert view._librenms_lookup_device is obj

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.render")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView.get_object")
    def test_get_rebinds_header_to_request_server_key(self, mock_get_obj, mock_render, mock_multi_server_config):
        """The page header must rebind to ?server_key so it matches the server the tabs render for."""
        view = _make_view()
        view._librenms_api.server_key = "default"  # bound to the default server
        obj = MagicMock()
        obj.virtual_chassis = None
        obj.custom_field_data = {}
        mock_get_obj.return_value = obj
        view.get_context_data = MagicMock(return_value={})
        mock_render.return_value = MagicMock()

        request = RequestFactory().get("/x/?server_key=secondary")
        with patch(
            "netbox_librenms_plugin.librenms_api.get_plugin_config",
            side_effect=lambda _plugin, key: mock_multi_server_config if key == "servers" else None,
        ):
            view.get(request, pk=1)

        assert view._librenms_api.server_key == "secondary"

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.render")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView.get_object")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_sync_device")
    def test_get_unresolved_server_key_fails_closed(self, mock_get_sync, mock_get_obj, mock_render):
        """Stale ?server_key fails closed: no default-server librenms_id, VC delegation skipped."""
        view = _make_view()
        view._librenms_api.server_key = "default"  # stays bound to the default server
        view._librenms_api.get_librenms_id.return_value = 99  # default server's mapping; must NOT surface

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()  # VC member: delegation would normally run
        mock_get_obj.return_value = obj
        view.get_context_data = MagicMock(return_value={})
        mock_render.return_value = MagicMock()

        request = RequestFactory().get("/x/?server_key=ghost")
        # Non-blank ?server_key=ghost the factory can't resolve -> rebind None -> unresolved=True.
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
            view.get(request, pk=1)

        mock_get_sync.assert_not_called()  # VC delegation skipped on unresolved
        # The default client must never be queried: get_librenms_id can auto-discover/store a
        # mapping, which is exactly the fail-open behaviour this branch blocks. Asserting the id is
        # None alone would still pass if the lookup ran and its result were merely discarded.
        view._librenms_api.get_librenms_id.assert_not_called()
        assert view._librenms_lookup_device is obj
        assert view.librenms_id is None  # no default-server mapping attributed to the gone server

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.render")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView.get_object")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_sync_device")
    def test_get_vc_member_always_delegates_to_sync_device(self, mock_get_sync, mock_get_obj, mock_render):
        """VC member: no own librenms_id - get_librenms_sync_device returns VC primary."""
        view = _make_view()

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()
        mock_get_obj.return_value = obj

        vc_primary = MagicMock()  # Represents the VC primary device
        mock_get_sync.return_value = vc_primary

        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        # Member has no own librenms_id; get_librenms_id returns None after delegation
        view._librenms_api.get_librenms_id.return_value = None

        view.get_context_data = MagicMock(return_value={})
        mock_render.return_value = MagicMock()

        request = RequestFactory().get("/")
        view.get(request, pk=1)

        mock_get_sync.assert_called_once_with(obj, server_key="default")
        # When member has no own ID, lookup uses the VC primary returned by get_librenms_sync_device
        assert view._librenms_lookup_device is vc_primary

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.render")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView.get_object")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_sync_device")
    def test_get_vc_member_with_own_librenms_id_uses_itself(self, mock_get_sync, mock_get_obj, mock_render):
        """VC member: has own librenms_id - get_librenms_sync_device still called, returns member itself."""
        view = _make_view()

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()
        mock_get_obj.return_value = obj

        # get_librenms_sync_device returns obj itself (member has own librenms_id, priority 1)
        mock_get_sync.return_value = obj

        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.get_librenms_id.return_value = 55

        view.get_context_data = MagicMock(return_value={})
        mock_render.return_value = MagicMock()

        request = RequestFactory().get("/")
        view.get(request, pk=1)

        mock_get_sync.assert_called_once_with(obj, server_key="default")
        # When member has its own ID, get_librenms_sync_device returns the member itself
        assert view._librenms_lookup_device is obj

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.render")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView.get_object")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_sync_device")
    def test_get_vc_member_no_sync_device_falls_back_to_obj(self, mock_get_sync, mock_get_obj, mock_render):
        """VC member: when get_librenms_sync_device returns None, keeps obj."""
        view = _make_view()

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()
        mock_get_obj.return_value = obj

        mock_get_sync.return_value = None

        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.get_librenms_id.return_value = 55

        view.get_context_data = MagicMock(return_value={})
        mock_render.return_value = MagicMock()

        request = RequestFactory().get("/")
        view.get(request, pk=1)

        mock_get_sync.assert_called_once_with(obj, server_key="default")
        assert view._librenms_lookup_device is obj


class TestGetContextDataVC:
    """Tests for get_context_data() VC context (lines 69-91)."""

    def test_vc_context_sync_device_has_id_and_ip(self):
        """VC device: sync_device_has_librenms_id and sync_device_has_primary_ip set."""
        view = _make_view()
        view.librenms_id = 42
        view._librenms_lookup_device = MagicMock()

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()
        obj._meta = MagicMock()
        obj._meta.model_name = "device"

        sync_device = MagicMock()
        sync_device.primary_ip = MagicMock()
        sync_device._meta.model_name = "device"
        sync_device.pk = 10

        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.librenms_url = "https://x.example.com"
        # Explicitly set get_librenms_id return value so sync_device_has_librenms_id
        # is determined by the patched get_librenms_device_id, not a bare MagicMock.
        view._librenms_api.get_librenms_id.return_value = 42
        # Note: production code calls get_librenms_device_id() (module-level function),
        # not self.librenms_api.get_librenms_id(). The patch below is the correct target.

        view.get_librenms_device_info = MagicMock(
            return_value={
                "found_in_librenms": True,
                "librenms_device_details": {
                    "librenms_device_serial": "SN001",
                    "librenms_device_hardware": "Cisco",
                    "librenms_device_os": "ios",
                    "librenms_device_version": "16.9",
                    "librenms_device_features": "-",
                    "librenms_device_location": "NYC",
                    "librenms_device_hardware_match": None,
                    "vc_inventory_serials": [],
                },
                "mismatched_device": False,
            }
        )
        view.get_interface_context = MagicMock(return_value=None)
        view.get_cable_context = MagicMock(return_value=None)
        view.get_ip_context = MagicMock(return_value=None)
        view.get_vlan_context = MagicMock(return_value=None)

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_sync_device") as mock_sync:
            mock_sync.return_value = sync_device
            with patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_device_id") as mock_id:
                mock_id.return_value = 42
                with patch(
                    "netbox_librenms_plugin.views.base.librenms_sync_view.get_interface_name_field",
                    return_value="ifName",
                ):
                    with patch(
                        "netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView._build_all_server_mappings",
                        return_value=None,
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView._get_platform_info",
                            return_value={},
                        ):
                            with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV1V2"):
                                with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV3"):
                                    with patch("dcim.models.Manufacturer") as MockMfr:
                                        MockMfr.objects.all.return_value.order_by.return_value = []
                                        with patch.object(view, "get_context_data", wraps=view.get_context_data):
                                            # Call parent get_context_data via a mock of super()
                                            with patch(
                                                "netbox_librenms_plugin.views.base.librenms_sync_view.LibreNMSAPIMixin.get_context_data",
                                                return_value={},
                                            ):
                                                ctx = view.get_context_data(MagicMock(), obj)

        assert ctx.get("is_vc_member") is True
        assert ctx.get("sync_device_has_librenms_id") is True
        assert ctx.get("sync_device_has_primary_ip") is True

    def test_vc_context_sync_device_has_no_id(self):
        """VC device where get_librenms_device_id returns None → sync_device_has_librenms_id is False."""
        view = _make_view()
        view.librenms_id = 42
        view._librenms_lookup_device = MagicMock()

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()
        obj._meta = MagicMock()
        obj._meta.model_name = "device"

        sync_device = MagicMock()
        sync_device.primary_ip = None  # also no IP
        sync_device._meta.model_name = "device"
        sync_device.pk = 10

        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.librenms_url = "https://x.example.com"
        # Explicitly set to None so sync_device_has_librenms_id computes as False
        # (determined by the patched get_librenms_device_id returning None below).
        view._librenms_api.get_librenms_id.return_value = None

        view.get_librenms_device_info = MagicMock(
            return_value={
                "found_in_librenms": False,
                "librenms_device_details": {
                    "librenms_device_serial": "",
                    "librenms_device_hardware": "-",
                    "librenms_device_os": "-",
                    "librenms_device_version": "-",
                    "librenms_device_features": "-",
                    "librenms_device_location": "-",
                    "librenms_device_hardware_match": None,
                    "vc_inventory_serials": [],
                },
                "mismatched_device": False,
            }
        )
        view.get_interface_context = MagicMock(return_value=None)
        view.get_cable_context = MagicMock(return_value=None)
        view.get_ip_context = MagicMock(return_value=None)
        view.get_vlan_context = MagicMock(return_value=None)

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_sync_device") as mock_sync:
            mock_sync.return_value = sync_device
            with patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_device_id") as mock_id:
                mock_id.return_value = None  # No ID → flag should be False
                with patch(
                    "netbox_librenms_plugin.views.base.librenms_sync_view.get_interface_name_field",
                    return_value="ifName",
                ):
                    with patch(
                        "netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView._build_all_server_mappings",
                        return_value=None,
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView._get_platform_info",
                            return_value={},
                        ):
                            with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV1V2"):
                                with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV3"):
                                    with patch("dcim.models.Manufacturer") as MockMfr:
                                        MockMfr.objects.all.return_value.order_by.return_value = []
                                        with patch.object(view, "get_context_data", wraps=view.get_context_data):
                                            with patch(
                                                "netbox_librenms_plugin.views.base.librenms_sync_view.LibreNMSAPIMixin.get_context_data",
                                                return_value={},
                                            ):
                                                ctx = view.get_context_data(MagicMock(), obj)

        assert ctx.get("is_vc_member") is True
        assert ctx.get("sync_device_has_librenms_id") is False
        assert ctx.get("sync_device_has_primary_ip") is False


class TestContextAllTabsPresent:
    """Regression coverage for sync-tab context keys."""

    def test_get_context_data_contains_all_sync_tabs(self):
        """Context always exposes all tab keys, including module_sync."""
        view = _make_view()
        view.librenms_id = 42

        obj = MagicMock()
        obj.virtual_chassis = None
        obj._meta = MagicMock()
        obj._meta.model_name = "device"
        obj.pk = 1
        obj.cf = {"librenms_id": {"default": 42}}

        interface_ctx = MagicMock()
        cable_ctx = MagicMock()
        ip_ctx = MagicMock()
        vlan_ctx = MagicMock()
        module_ctx = MagicMock()

        view.get_librenms_device_info = MagicMock(
            return_value={
                "found_in_librenms": True,
                "librenms_device_details": {
                    "librenms_device_serial": "SN001",
                    "librenms_device_hardware": "Cisco",
                    "librenms_device_os": "ios",
                    "librenms_device_version": "16.9",
                    "librenms_device_features": "-",
                    "librenms_device_location": "NYC",
                    "librenms_device_hardware_match": None,
                    "vc_inventory_serials": [],
                },
                "mismatched_device": False,
            }
        )
        view.get_interface_context = MagicMock(return_value=interface_ctx)
        view.get_cable_context = MagicMock(return_value=cable_ctx)
        view.get_ip_context = MagicMock(return_value=ip_ctx)
        view.get_vlan_context = MagicMock(return_value=vlan_ctx)
        view.get_module_context = MagicMock(return_value=module_ctx)

        with patch(
            "netbox_librenms_plugin.views.base.librenms_sync_view.LibreNMSAPIMixin.get_context_data",
            return_value={},
        ):
            with patch(
                "netbox_librenms_plugin.views.base.librenms_sync_view.get_interface_name_field", return_value="ifName"
            ):
                with patch(
                    "netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView._get_platform_info",
                    return_value={},
                ):
                    with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV1V2"):
                        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV3"):
                            with patch("dcim.models.Manufacturer") as MockMfr:
                                MockMfr.objects.all.return_value.order_by.return_value = []
                                ctx = view.get_context_data(MagicMock(), obj)

        assert "interface_sync" in ctx
        assert "cable_sync" in ctx
        assert "ip_sync" in ctx
        assert "vlan_sync" in ctx
        assert "module_sync" in ctx
        assert ctx["interface_sync"] is interface_ctx
        assert ctx["module_sync"] is module_ctx

    @pytest.mark.django_db
    def test_get_context_data_exposes_active_server_key(self):
        """The active server_key is in context so the create-platform modal forwards it (preserving the server tab on redirect)."""
        from netbox_librenms_plugin.tests.conftest import make_device

        view = _make_view()
        view.librenms_id = 42
        view._librenms_api.server_key = "production"  # non-default active server

        obj = make_device("ctx-serverkey-dev", librenms_cf={"production": 42})

        view.get_librenms_device_info = MagicMock(
            return_value={
                "found_in_librenms": True,
                "librenms_device_details": {
                    "librenms_device_serial": "SN001",
                    "librenms_device_hardware": "Cisco",
                    "librenms_device_os": "ios",
                    "librenms_device_version": "16.9",
                    "librenms_device_features": "-",
                    "librenms_device_location": "NYC",
                    "librenms_device_hardware_match": None,
                    "vc_inventory_serials": [],
                },
                "mismatched_device": False,
            }
        )
        view.get_interface_context = MagicMock(return_value=None)
        view.get_cable_context = MagicMock(return_value=None)
        view.get_ip_context = MagicMock(return_value=None)
        view.get_vlan_context = MagicMock(return_value=None)
        view.get_module_context = MagicMock(return_value=None)

        with patch(
            "netbox_librenms_plugin.views.base.librenms_sync_view.LibreNMSAPIMixin.get_context_data",
            return_value={},
        ):
            with patch(
                "netbox_librenms_plugin.views.base.librenms_sync_view.get_interface_name_field", return_value="ifName"
            ):
                with patch(
                    "netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView._get_platform_info",
                    return_value={},
                ):
                    with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV1V2"):
                        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV3"):
                            ctx = view.get_context_data(MagicMock(), obj)

        # The create-platform modal include (no `only`) inherits this; without it the modal's
        # {% if server_key %} hidden field never renders and the redirect drops the server tab.
        assert ctx["server_key"] == "production"


class TestModuleContextDefaults:
    """Tests for module-context defaults and concrete overrides."""

    def test_module_sync_is_none_when_not_overridden(self):
        from netbox_librenms_plugin.views.object_sync.vms import VMLibreNMSSyncView

        base_view = _make_view()
        assert base_view.get_module_context(MagicMock(), MagicMock()) is None

        vm_view = object.__new__(VMLibreNMSSyncView)
        assert vm_view.get_module_context(MagicMock(), MagicMock()) is None

    def test_device_view_module_context_is_non_none(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

        request = MagicMock()
        obj = MagicMock()
        view = object.__new__(DeviceLibreNMSSyncView)

        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView.get_context_data",
            return_value={"modules": []},
        ) as mock_get_context:
            result = view.get_module_context(request, obj)

        assert result == {"modules": []}
        mock_get_context.assert_called_once()


class TestBuildAllServerMappings:
    """Tests for _build_all_server_mappings (lines 181, 193, 200, 207-208)."""

    def test_returns_none_for_non_dict_cf(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 42}  # legacy bare int
        result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")
        assert result is None

    def test_returns_none_for_empty_dict_cf(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {}}
        result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")
        assert result is None

    def test_valid_dict_cf_returns_list(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": 42, "secondary": 99}}

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {
                    "servers": {
                        "default": {"librenms_url": "https://x.example.com", "display_name": "Default"},
                        "secondary": {"librenms_url": "https://y.example.com", "display_name": "Secondary"},
                    }
                }
            }
            result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")

        assert result is not None
        assert len(result) == 2
        # Active server should be first
        assert result[0]["is_active"] is True
        assert result[0]["server_key"] == "default"

    def test_bool_value_skipped(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": True, "other": 42}}

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {
                    "servers": {"other": {"librenms_url": "https://x.example.com", "display_name": "Other"}}
                }
            }
            result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")

        assert result is not None
        assert len(result) == 1
        assert result[0]["server_key"] == "other"

    def test_oob_only_entry_with_invalid_host_id_is_surfaced(self):
        """An entry whose host "id" is a corrupt non-None value (0) but whose "oob.id" is valid must still surface as an OOB-only mapping so the user can see/remove it, not be dropped by the <=0 guard."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": {"id": 0, "oob": {"id": 42}}}}

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {
                    "servers": {"default": {"librenms_url": "https://x.example.com", "display_name": "Default"}}
                }
            }
            result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")

        assert result is not None
        entry = next((m for m in result if m["server_key"] == "default"), None)
        assert entry is not None  # not dropped despite the invalid host id
        assert entry["is_oob_only"] is True

    def test_string_device_id_converted_to_int(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": "77"}}

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {
                    "servers": {"default": {"librenms_url": "https://x.example.com", "display_name": "Default"}}
                }
            }
            result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")

        assert result[0]["device_id"] == 77

    def test_non_digit_string_skipped(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": "not-a-number"}}

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
            result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")

        assert result is None

    def test_legacy_default_key_falls_back_to_root_librenms_url(self):
        """'default' key with no matching servers entry uses root librenms_url."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": 42}}

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {
                    "librenms_url": "https://legacy.example.com",
                    "display_name": "Legacy Server",
                    "servers": {},
                }
            }
            result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")

        assert result is not None
        assert result[0]["librenms_url"] == "https://legacy.example.com"

    def test_malformed_server_config_treated_as_unconfigured(self):
        """Non-dict server config entry → is_configured=False."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": 42}}

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {"default": "this-is-not-a-dict"}}}
            result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")

        assert result is not None
        assert result[0]["is_configured"] is False

    def test_dict_entry_uses_host_id(self):
        """New dict form {server_key: {"id": N, "oob": {...}}} renders the host id."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": {"id": 42, "oob": {"id": 17, "type": "idrac"}}}}

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {
                    "servers": {"default": {"librenms_url": "https://x.example.com", "display_name": "Default"}}
                }
            }
            result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")

        assert result is not None
        assert result[0]["device_id"] == 42
        assert result[0]["is_oob_only"] is False

    def test_oob_only_entry_surfaced_with_oob_id(self):
        """An OOB-only entry ({"oob": {...}} with no host id) must still surface so the user can see/remove it; it falls back to the OOB controller's id and is flagged."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": {"oob": {"id": 17, "type": "idrac"}}}}

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {
                    "servers": {"default": {"librenms_url": "https://x.example.com", "display_name": "Default"}}
                }
            }
            result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")

        assert result is not None
        assert len(result) == 1
        assert result[0]["device_id"] == 17
        assert result[0]["is_oob_only"] is True
        assert result[0]["device_url"] == "https://x.example.com/device/device=17/"

    def test_migrated_only_dict_entry_skipped(self):
        """A migrated-only entry ({"_migrated_to": ...}) has neither id nor oob → skipped."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": {"_migrated_to": {"device_id": 5}}}}

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
            result = BaseLibreNMSSyncView._build_all_server_mappings(obj, "default")

        assert result is None


class TestAllServerMappingsDidValidation:
    """all_server_mappings must skip invalid device IDs in the cf_value dict."""

    def _call(self, obj, active_server_key="default"):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        return BaseLibreNMSSyncView._build_all_server_mappings(obj, active_server_key)

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_skips_boolean_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": True, "prod": 42}}
        result = self._call(obj)
        # Only prod=42 should survive
        assert len(result) == 1
        assert result[0]["device_id"] == 42

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_skips_none_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": None}}
        result = self._call(obj)
        assert result is None  # empty list → returns None

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_coerces_digit_string_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"prod": "99"}}
        result = self._call(obj)
        assert len(result) == 1
        assert result[0]["device_id"] == 99

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_skips_non_digit_string_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": "bogus"}}
        result = self._call(obj)
        assert result is None

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_valid_int_passes_through(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": 5, "secondary": 10}}
        result = self._call(obj)
        assert len(result) == 2
        ids = {e["device_id"] for e in result}
        assert ids == {5, 10}


class TestGetLibreNMSDeviceInfo:
    """Tests for get_librenms_device_info (lines 228+)."""

    def test_no_librenms_id_returns_defaults(self):
        view = _make_view()
        view.librenms_id = None
        view._librenms_api = MagicMock()

        obj = MagicMock()
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is False
        assert result["mismatched_device"] is False

    def test_librenms_id_success_sets_found(self):
        view = _make_view()
        view.librenms_id = 42
        view._librenms_api = MagicMock()
        view._librenms_api.librenms_url = "https://x.example.com"

        obj = MagicMock()
        obj.primary_ip = None
        obj.name = "mydevice"
        obj.virtual_chassis = None
        obj.serial = "SN001"
        obj.platform = None

        device_info = {
            "hardware": "Cisco C9300",
            "serial": "SN001",
            "os": "ios",
            "version": "16.9",
            "features": "-",
            "sysName": "mydevice",
            "hostname": "mydevice.example.com",
            "ip": "10.0.0.1",
            "location": "NYC",
        }
        view._librenms_api.get_device_info.return_value = (True, device_info)

        with patch(
            "netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type"
        ) as mock_match:
            mock_match.return_value = {"matched": False, "device_type": None, "match_type": None}
            result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True

    def test_mismatched_device_when_names_differ(self):
        view = _make_view()
        view.librenms_id = 42
        view._librenms_api = MagicMock()
        view._librenms_api.librenms_url = "https://x.example.com"

        obj = MagicMock()
        obj.primary_ip = None
        obj.name = "device-netbox"
        obj.virtual_chassis = None
        obj.serial = ""
        obj.platform = None

        device_info = {
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "version": "-",
            "features": "-",
            "sysName": "completely-different",
            "hostname": "also-different.example.com",
            "ip": "192.168.0.1",
            "location": "-",
        }
        view._librenms_api.get_device_info.return_value = (True, device_info)

        with patch(
            "netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type"
        ) as mock_match:
            mock_match.return_value = {"matched": False, "device_type": None, "match_type": None}
            with patch(
                "netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView._strip_vc_pattern",
                return_value=None,
            ):
                result = view.get_librenms_device_info(obj)

        assert result["mismatched_device"] is True


class TestStripVcPattern:
    """Tests for _strip_vc_pattern (lines 378+)."""

    def test_strips_default_pattern(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        mock_settings_cls = MagicMock()
        settings_obj = MagicMock()
        settings_obj.vc_member_name_pattern = "-M{position}"
        mock_settings_cls.objects.first.return_value = settings_obj

        with patch("netbox_librenms_plugin.models.LibreNMSSettings", mock_settings_cls, create=True):
            result = BaseLibreNMSSyncView._strip_vc_pattern("switch01-m2")
            # The suffix -m2 should be stripped, returning "switch01"
            assert result == "switch01"  # suffix -m2 must be stripped

    def test_returns_none_on_exception(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        mock_settings_cls = MagicMock()
        mock_settings_cls.objects.first.side_effect = Exception("DB error")

        with patch("netbox_librenms_plugin.models.LibreNMSSettings", mock_settings_cls, create=True):
            result = BaseLibreNMSSyncView._strip_vc_pattern("some-device")
            assert result is None


class TestLibreNMSIdLegacyDetection:
    """Tests for librenms_id_is_legacy detection (lines 113-115)."""

    def test_bare_int_cf_detected_as_legacy(self):
        """bare int CF → librenms_id_is_legacy = True."""
        view = _make_view()
        view.librenms_id = 42
        view._librenms_lookup_device = MagicMock()
        view._librenms_lookup_device.cf = {"librenms_id": 42}

        obj = MagicMock()
        obj.virtual_chassis = None
        obj._meta = MagicMock()
        obj._meta.model_name = "device"
        obj.pk = 1
        obj.serial = "SN"
        obj.platform = None

        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.librenms_url = "https://x.example.com"

        view.get_librenms_device_info = MagicMock(
            return_value={
                "found_in_librenms": True,
                "librenms_device_details": {
                    "librenms_device_serial": "SN",
                    "librenms_device_hardware": "-",
                    "librenms_device_os": "-",
                    "librenms_device_version": "-",
                    "librenms_device_features": "-",
                    "librenms_device_location": "-",
                    "librenms_device_hardware_match": None,
                    "vc_inventory_serials": [],
                },
                "mismatched_device": False,
            }
        )
        view.get_interface_context = MagicMock(return_value=None)
        view.get_cable_context = MagicMock(return_value=None)
        view.get_ip_context = MagicMock(return_value=None)
        view.get_vlan_context = MagicMock(return_value=None)

        with patch(
            "netbox_librenms_plugin.views.base.librenms_sync_view.get_interface_name_field", return_value="ifName"
        ):
            with patch(
                "netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView._build_all_server_mappings",
                return_value=None,
            ):
                with patch(
                    "netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView._get_platform_info",
                    return_value={},
                ):
                    with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV1V2"):
                        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV3"):
                            with patch("dcim.models.Manufacturer") as MockMfr:
                                MockMfr.objects.all.return_value.order_by.return_value = []
                                with patch(
                                    "netbox_librenms_plugin.views.base.librenms_sync_view.LibreNMSAPIMixin.get_context_data",
                                    return_value={},
                                ):
                                    ctx = view.get_context_data(MagicMock(), obj)

        assert ctx.get("librenms_id_is_legacy") is True


class TestAbstractMethods:
    """Tests for abstract get_*_context methods (lines 349-376)."""

    def test_get_interface_context_returns_none(self):
        view = _make_view()
        result = view.get_interface_context(MagicMock(), MagicMock())
        assert result is None

    def test_get_cable_context_returns_none(self):
        view = _make_view()
        result = view.get_cable_context(MagicMock(), MagicMock())
        assert result is None

    def test_get_ip_context_returns_none(self):
        view = _make_view()
        result = view.get_ip_context(MagicMock(), MagicMock())
        assert result is None

    def test_get_vlan_context_returns_none(self):
        view = _make_view()
        result = view.get_vlan_context(MagicMock(), MagicMock())
        assert result is None


class TestGetVCInventorySerials:
    """Tests for _get_vc_inventory_serials (lines 412-452)."""

    def test_no_inventory_returns_empty(self):
        view = _make_view()
        view.librenms_id = 42
        view._librenms_api.get_device_inventory.return_value = (False, [])

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()
        obj.virtual_chassis.members.all.return_value = []

        result = view._get_vc_inventory_serials(obj)
        assert result == []

    def test_chassis_components_matched(self):
        view = _make_view()
        view.librenms_id = 42

        inventory = [
            {
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "SN001",
                "entPhysicalDescr": "Chassis",
                "entPhysicalModelName": "C9300",
            },
            {
                "entPhysicalClass": "module",
                "entPhysicalSerialNum": "SN002",
                "entPhysicalDescr": "Module",
                "entPhysicalModelName": "",
            },
        ]
        view._librenms_api.get_device_inventory.return_value = (True, inventory)

        member = MagicMock()
        member.serial = "SN001"

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()
        obj.virtual_chassis.members.all.return_value = [member]

        result = view._get_vc_inventory_serials(obj)
        assert len(result) == 1
        assert result[0]["serial"] == "SN001"
        assert result[0]["assigned_member"] is member

    def test_unassigned_serial_returns_none_member(self):
        view = _make_view()
        view.librenms_id = 42

        inventory = [
            {
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "UNKNOWN_SN",
                "entPhysicalDescr": "Chassis",
                "entPhysicalModelName": "MX480",
            },
        ]
        view._librenms_api.get_device_inventory.return_value = (True, inventory)

        member = MagicMock()
        member.serial = "SN001"  # Different serial

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()
        obj.virtual_chassis.members.all.return_value = [member]

        result = view._get_vc_inventory_serials(obj)
        assert len(result) == 1
        assert result[0]["assigned_member"] is None

    def test_empty_serial_skipped(self):
        view = _make_view()
        view.librenms_id = 42

        inventory = [
            {
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "-",
                "entPhysicalDescr": "Chassis",
                "entPhysicalModelName": "",
            },
        ]
        view._librenms_api.get_device_inventory.return_value = (True, inventory)

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()
        obj.virtual_chassis.members.all.return_value = []

        result = view._get_vc_inventory_serials(obj)
        assert result == []


class TestGetPlatformInfo:
    """Tests for _get_platform_info (lines 463-502)."""

    def test_no_os_returns_no_platform(self):
        view = _make_view()
        obj = MagicMock()
        obj.platform = None

        librenms_info = {
            "librenms_device_details": {
                "librenms_device_os": "-",
                "librenms_device_version": "-",
            }
        }

        with patch("dcim.models.Platform") as MockPlatform:
            MockPlatform.DoesNotExist = type("DoesNotExist", (Exception,), {})
            MockPlatform.objects.get.side_effect = MockPlatform.DoesNotExist()
            result = view._get_platform_info(librenms_info, obj)

        assert result["platform_exists"] is False
        assert result["platform_name"] is None

    def test_matching_platform_found(self):
        view = _make_view()
        obj = MagicMock()
        mock_platform = MagicMock()

        librenms_info = {
            "librenms_device_details": {
                "librenms_device_os": "ios",
                "librenms_device_version": "16.9",
            }
        }

        with patch(
            "netbox_librenms_plugin.views.base.librenms_sync_view.find_matching_platform",
            return_value={"found": True, "platform": mock_platform, "match_type": "exact"},
        ):
            result = view._get_platform_info(librenms_info, obj)

        assert result["platform_exists"] is True
        assert result["matching_platform"] is mock_platform

    def test_platform_does_not_exist(self):
        view = _make_view()
        obj = MagicMock()
        obj.platform = None

        librenms_info = {
            "librenms_device_details": {
                "librenms_device_os": "eos",
                "librenms_device_version": "4.28",
            }
        }

        with patch(
            "netbox_librenms_plugin.views.base.librenms_sync_view.find_matching_platform",
            return_value={"found": False, "platform": None, "match_type": None},
        ):
            result = view._get_platform_info(librenms_info, obj)

        assert result["platform_exists"] is False
        assert result["matching_platform"] is None


@pytest.mark.django_db
class TestInterfaceSyncRefreshButtonServerKey:
    """The 'Refresh Interfaces' button must carry the active server_key — via the hidden input the enclosing form emits (htmx includes form values on non-GET) plus the button's own hx-vals context fallback — so a non-default server tab refreshes from the right LibreNMS server/cache, not the fallback."""

    def _render(self, *, server_key="prod"):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        mfr, _ = Manufacturer.objects.get_or_create(name="ACME-ifsync", slug="acme-ifsync")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-ifsync", slug="dt-ifsync")
        role, _ = DeviceRole.objects.get_or_create(name="Role-ifsync", slug="role-ifsync")
        site, _ = Site.objects.get_or_create(name="Site-ifsync", slug="site-ifsync")
        device = Device.objects.create(name="ifsync-refresh-dev", device_type=dt, role=role, site=site, status="active")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        table = LibreNMSInterfaceTable([], device=device, server_key=server_key)
        RequestConfig(request).configure(table)
        ctx = {
            "object": device,
            "has_librenms_id": True,
            "server_key": server_key,
            "librenms_server_info": {"server_key": server_key},
            "interface_name_field": "ifName",
            "migrated_to_marker": None,
            "migrated_to_winner": None,
            "has_write_permission": False,
            "interface_sync": {
                "object": device,
                "table": table,
                "server_key": server_key,
                "netbox_only_interfaces": [],
                "virtual_chassis_members": [],
                "cache_expiry": None,
                "oob_incomplete": False,
            },
        }
        return render_to_string("netbox_librenms_plugin/_interface_sync.html", ctx, request=request)

    def test_refresh_button_includes_server_key(self):
        html = self._render(server_key="prod")
        # The refresh POST carries the active server key two ways: the hidden input the enclosing
        # form emits (htmx includes form values on non-GET requests) and the button's own hx-vals
        # context fallback. parent-child moved server_key OUT of the button's hx-include into hx-vals,
        # but the hidden input the form relies on stays.
        assert '<input type="hidden" name="server_key" value="prod">' in html
        assert "get('server_key') || 'prod'" in html


@pytest.mark.django_db
class TestFullPageMigratedContextServerScope:
    """The full-page migrated banner must use the resolved render key, not the global default."""

    def test_stale_server_key_builds_migrated_banner_under_requested_key(self, client, settings):
        """A marker under a removed server still renders through the real request path."""
        from copy import deepcopy

        from django.test import override_settings
        from django.urls import reverse

        from netbox_librenms_plugin.tests.conftest import make_device

        winner = make_device("f4-winner")
        donor = make_device("f4-donor")
        # The migration marker lives under a NON-default server that is now stale/deleted.
        donor.custom_field_data["librenms_id"] = {
            "edgelondon": {"_migrated_to": {"device_id": winner.pk, "server_key": "edgelondon", "at": "x"}}
        }
        donor.save()

        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {
            "default": {
                "librenms_url": "https://default.example.com",
                "api_token": "test-token",
            }
        }
        user = make_user_with_perms("stale-key-viewer", [("view", Device)])
        client.force_login(user)
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[donor.pk])

        with override_settings(PLUGINS_CONFIG=plugin_config):
            response = client.get(url, {"server_key": "edgelondon"})

        assert response.status_code == 200
        assert response.context["migrated_to_marker"]
        assert response.context["migrated_to_winner"].pk == winner.pk
