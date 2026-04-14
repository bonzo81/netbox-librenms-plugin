"""Comprehensive coverage tests for views/sync/ modules."""

from unittest.mock import MagicMock, patch


def _make_post(data):
    """Return a mock POST object backed by a real dict."""
    mock = MagicMock()
    mock.get = lambda key, default=None: data.get(key, default)
    mock.getlist = lambda key: data[key] if isinstance(data.get(key), list) else ([data[key]] if key in data else [])
    return mock


def _make_request(post_data=None, headers=None):
    """Build a minimal mock HTTP request."""
    req = MagicMock()
    req.method = "POST"
    req.headers = headers or {}
    req.META = {"HTTP_REFERER": "/dcim/devices/1/"}
    req.get_host.return_value = "testserver"
    req.is_secure.return_value = False
    req.POST = _make_post(post_data or {})
    req.user = MagicMock()
    return req


def _make_view(cls):
    """Instantiate a view bypassing __init__, injecting a mock LibreNMS API."""
    view = object.__new__(cls)
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    view.request = _make_request()
    return view


def _atomic_txn():
    """Return a mock transaction object whose atomic() acts as a no-op context manager."""
    mock_txn = MagicMock()
    mock_txn.atomic.return_value.__enter__ = MagicMock(return_value=None)
    mock_txn.atomic.return_value.__exit__ = MagicMock(return_value=False)
    return mock_txn


# ===========================================================================
# cables.py — SyncCablesView
# ===========================================================================


class TestSyncCablesViewStructure:
    def test_has_required_mixins(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView
        from netbox_librenms_plugin.views.mixins import CacheMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin

        mro = SyncCablesView.__mro__
        assert LibreNMSPermissionMixin in mro
        assert NetBoxObjectPermissionMixin in mro
        assert CacheMixin in mro

    def test_required_object_permissions(self):
        from dcim.models import Cable

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        perms = SyncCablesView.required_object_permissions["POST"]
        assert ("add", Cable) in perms
        assert ("change", Cable) in perms


class TestSyncCablesViewGetSelectedInterfaces:
    def test_empty_select_returns_none(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        req = _make_request({"select": []})
        initial_device = MagicMock()
        result = view.get_selected_interfaces(req, initial_device)
        assert result is None

    def test_only_empty_strings_returns_none(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        req = _make_request({"select": ["", ""]})
        initial_device = MagicMock()
        result = view.get_selected_interfaces(req, initial_device)
        assert result is None

    def test_single_port_uses_initial_device_id(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        req = _make_request({"select": ["42"]})
        initial_device = MagicMock()
        initial_device.id = 5
        result = view.get_selected_interfaces(req, initial_device)
        assert result is not None
        assert len(result) == 1
        assert result[0]["device_id"] == 5
        assert result[0]["local_port_id"] == "42"

    def test_port_with_device_override(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        req = _make_request({"select": ["42"], "device_selection_42": "7"})
        initial_device = MagicMock()
        initial_device.id = 5
        result = view.get_selected_interfaces(req, initial_device)
        assert result is not None
        assert result[0]["device_id"] == "7"

    def test_multiple_ports(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        req = _make_request({"select": ["1", "2"]})
        initial_device = MagicMock()
        initial_device.id = 10
        result = view.get_selected_interfaces(req, initial_device)
        assert result is not None
        assert len(result) == 2


class TestSyncCablesViewGetCachedLinksData:
    def test_cache_miss_returns_none(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        obj = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache:
            mock_cache.get.return_value = None
            with patch.object(view, "get_cache_key", return_value="key1"):
                result = view.get_cached_links_data(view.request, obj)
        assert result is None

    def test_cache_hit_returns_links_list(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        obj = MagicMock()
        links = [{"local_port_id": "1"}]
        with patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache:
            mock_cache.get.return_value = {"links": links}
            with patch.object(view, "get_cache_key", return_value="key1"):
                result = view.get_cached_links_data(view.request, obj)
        assert result == links


class TestSyncCablesViewValidatePrerequisites:
    def test_no_cached_links_returns_false(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_messages:
            view = _make_view(SyncCablesView)
            result = view.validate_prerequisites([], [{"local_port_id": "1"}])
        assert result is False
        mock_messages.error.assert_called_once()

    def test_none_cached_links_returns_false(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_messages:
            view = _make_view(SyncCablesView)
            result = view.validate_prerequisites(None, [{"local_port_id": "1"}])
        assert result is False
        mock_messages.error.assert_called_once()

    def test_no_selected_interfaces_returns_false(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_messages:
            view = _make_view(SyncCablesView)
            result = view.validate_prerequisites([{"local_port_id": "1"}], None)
        assert result is False
        mock_messages.error.assert_called_once()

    def test_both_present_returns_true(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        result = view.validate_prerequisites([{"local_port_id": "1"}], [{"device_id": 1}])
        assert result is True


class TestSyncCablesViewVerifyCableCreationRequirements:
    def test_missing_local_interface_id(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        result = view.verify_cable_creation_requirements({"netbox_remote_interface_id": 2})
        assert result is False

    def test_missing_remote_interface_id(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        result = view.verify_cable_creation_requirements({"netbox_local_interface_id": 1})
        assert result is False

    def test_none_local_id(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        result = view.verify_cable_creation_requirements(
            {"netbox_local_interface_id": None, "netbox_remote_interface_id": 2}
        )
        assert result is False

    def test_all_fields_present(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        result = view.verify_cable_creation_requirements(
            {"netbox_local_interface_id": 1, "netbox_remote_interface_id": 2}
        )
        assert result is True


class TestSyncCablesViewHandleCableCreation:
    def test_missing_requirements_returns_invalid(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        link_data = {
            "local_port": "eth0",
            "netbox_local_interface_id": None,
            "netbox_remote_interface_id": 2,
            "netbox_remote_device_id": 5,
        }
        interface = {"local_port_id": "42"}
        result = view.handle_cable_creation(link_data, interface)
        assert result["status"] == "invalid"
        assert result["interface"] == "eth0"

    def test_display_name_falls_back_to_port_id(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        link_data = {"netbox_local_interface_id": None, "netbox_remote_interface_id": 2, "netbox_remote_device_id": 5}
        interface = {"local_port_id": "99"}
        result = view.handle_cable_creation(link_data, interface)
        assert result["status"] == "invalid"
        assert result["interface"] == "99"

    def test_interface_not_found_returns_missing_remote(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        link_data = {"local_port": "eth0", "netbox_local_interface_id": 1, "netbox_remote_interface_id": 2}
        interface = {"local_port_id": "42"}
        mock_iface = MagicMock()
        mock_iface.DoesNotExist = Interface.DoesNotExist
        mock_iface.objects.get.side_effect = Interface.DoesNotExist
        with patch("netbox_librenms_plugin.views.sync.cables.Interface", mock_iface):
            result = view.handle_cable_creation(link_data, interface)
        assert result["status"] == "missing_remote"

    def test_existing_cable_returns_duplicate(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        link_data = {"local_port": "eth0", "netbox_local_interface_id": 1, "netbox_remote_interface_id": 2}
        interface = {"local_port_id": "42"}
        mock_iface = MagicMock()
        mock_iface.DoesNotExist = Interface.DoesNotExist
        mock_iface.objects.get.return_value = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.cables.Interface", mock_iface):
            with patch.object(view, "check_existing_cable", return_value=True):
                result = view.handle_cable_creation(link_data, interface)
        assert result["status"] == "duplicate"

    def test_creates_cable_returns_valid(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        link_data = {"local_port": "eth0", "netbox_local_interface_id": 1, "netbox_remote_interface_id": 2}
        interface = {"local_port_id": "42"}
        mock_iface = MagicMock()
        mock_iface.DoesNotExist = Interface.DoesNotExist
        mock_iface.objects.get.return_value = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.cables.Interface", mock_iface):
            with patch.object(view, "check_existing_cable", return_value=False):
                with patch.object(view, "create_cable", return_value=True):
                    result = view.handle_cable_creation(link_data, interface)
        assert result["status"] == "valid"


class TestSyncCablesViewProcessSingleInterface:
    def test_port_found_delegates_to_handle_cable_creation(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        cached_links = [{"local_port_id": "5", "netbox_local_interface_id": 1, "netbox_remote_interface_id": 2}]
        interface = {"local_port_id": "5", "device_id": 1}
        expected = {"status": "valid", "interface": "eth0"}
        with patch.object(view, "handle_cable_creation", return_value=expected) as mock_handle:
            result = view.process_single_interface(interface, cached_links)
        assert result == expected
        mock_handle.assert_called_once()

    def test_port_not_in_cache_returns_invalid(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        cached_links = [{"local_port_id": "99"}]
        interface = {"local_port_id": "42"}
        result = view.process_single_interface(interface, cached_links)
        assert result["status"] == "invalid"
        assert result["interface"] == "42"


class TestSyncCablesViewProcessInterfaceSync:
    def test_valid_result_collected(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        interfaces = [{"local_port_id": "1"}]
        expected = {"status": "valid", "interface": "eth0"}
        with patch("netbox_librenms_plugin.views.sync.cables.transaction", _atomic_txn()):
            with patch.object(view, "process_single_interface", return_value=expected):
                results = view.process_interface_sync(interfaces, [])
        assert "eth0" in results["valid"]

    def test_duplicate_result_collected(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        interfaces = [{"local_port_id": "1"}]
        expected = {"status": "duplicate", "interface": "eth0"}
        with patch("netbox_librenms_plugin.views.sync.cables.transaction", _atomic_txn()):
            with patch.object(view, "process_single_interface", return_value=expected):
                results = view.process_interface_sync(interfaces, [])
        assert "eth0" in results["duplicate"]

    def test_missing_remote_result_collected(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        interfaces = [{"local_port_id": "1"}]
        expected = {"status": "missing_remote", "interface": "eth1"}
        with patch("netbox_librenms_plugin.views.sync.cables.transaction", _atomic_txn()):
            with patch.object(view, "process_single_interface", return_value=expected):
                results = view.process_interface_sync(interfaces, [])
        assert "eth1" in results["missing_remote"]

    def test_exception_adds_to_invalid(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        interfaces = [{"local_port_id": "55"}]
        with patch("netbox_librenms_plugin.views.sync.cables.transaction", _atomic_txn()):
            with patch.object(view, "process_single_interface", side_effect=Exception("boom")):
                results = view.process_interface_sync(interfaces, [])
        assert "55" in results["invalid"]


class TestSyncCablesViewDisplaySyncResults:
    def test_missing_remote_calls_error(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msg:
            view = _make_view(SyncCablesView)
            view.display_sync_results(
                view.request,
                {"valid": [], "invalid": [], "duplicate": [], "missing_remote": ["eth0"]},
            )
        mock_msg.error.assert_called_once()

    def test_invalid_calls_error(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msg:
            view = _make_view(SyncCablesView)
            view.display_sync_results(
                view.request,
                {"valid": [], "invalid": ["eth1"], "duplicate": [], "missing_remote": []},
            )
        mock_msg.error.assert_called_once()

    def test_duplicate_calls_warning(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msg:
            view = _make_view(SyncCablesView)
            view.display_sync_results(
                view.request,
                {"valid": [], "invalid": [], "duplicate": ["eth2"], "missing_remote": []},
            )
        mock_msg.warning.assert_called_once()

    def test_valid_calls_success(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msg:
            view = _make_view(SyncCablesView)
            view.display_sync_results(
                view.request,
                {"valid": ["eth3"], "invalid": [], "duplicate": [], "missing_remote": []},
            )
        mock_msg.success.assert_called_once()

    def test_empty_results_no_messages_called(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msg:
            view = _make_view(SyncCablesView)
            view.display_sync_results(
                view.request,
                {"valid": [], "invalid": [], "duplicate": [], "missing_remote": []},
            )
        mock_msg.error.assert_not_called()
        mock_msg.warning.assert_not_called()
        mock_msg.success.assert_not_called()


class TestSyncCablesViewPost:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        mock_error = MagicMock()
        with patch.object(view, "require_all_permissions", return_value=mock_error):
            result = view.post(view.request, pk=1)
        assert result is mock_error

    def test_validate_prerequisites_failure_redirects(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        mock_device = MagicMock()
        mock_device.pk = 1
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("netbox_librenms_plugin.views.sync.cables.get_object_or_404", return_value=mock_device):
                with patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/fake/"):
                    with patch.object(view, "get_selected_interfaces", return_value=None):
                        with patch.object(view, "get_cached_links_data", return_value=None):
                            with patch.object(view, "validate_prerequisites", return_value=False):
                                with patch("netbox_librenms_plugin.views.sync.cables.redirect") as mock_redirect:
                                    view.post(view.request, pk=1)
        mock_redirect.assert_called_once()

    def test_successful_sync_redirects(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        mock_device = MagicMock()
        mock_device.pk = 1
        results = {"valid": ["eth0"], "invalid": [], "duplicate": [], "missing_remote": []}
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("netbox_librenms_plugin.views.sync.cables.get_object_or_404", return_value=mock_device):
                with patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/fake/"):
                    with patch.object(view, "get_selected_interfaces", return_value=[{"local_port_id": "1"}]):
                        with patch.object(view, "get_cached_links_data", return_value=[{"local_port_id": "1"}]):
                            with patch.object(view, "validate_prerequisites", return_value=True):
                                with patch.object(view, "process_interface_sync", return_value=results):
                                    with patch.object(view, "display_sync_results"):
                                        with patch("netbox_librenms_plugin.views.sync.cables.redirect") as mock_r:
                                            view.post(view.request, pk=1)
        mock_r.assert_called_once()

    def test_server_key_stored_from_post(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        view.request = _make_request({"server_key": "secondary"})
        mock_device = MagicMock()
        mock_device.pk = 1
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("netbox_librenms_plugin.views.sync.cables.get_object_or_404", return_value=mock_device):
                with patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/fake/"):
                    with patch.object(view, "get_selected_interfaces", return_value=None):
                        with patch.object(view, "get_cached_links_data", return_value=None):
                            with patch.object(view, "validate_prerequisites", return_value=False):
                                with patch("netbox_librenms_plugin.views.sync.cables.redirect"):
                                    view.post(view.request, pk=1)
        assert view._post_server_key == "secondary"


# ===========================================================================
# devices.py — AddDeviceToLibreNMSView
# ===========================================================================


class TestAddDeviceToLibreNMSViewStructure:
    def test_has_required_mixins(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSPermissionMixin
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        mro = AddDeviceToLibreNMSView.__mro__
        assert LibreNMSPermissionMixin in mro
        assert LibreNMSAPIMixin in mro


class TestAddDeviceToLibreNMSViewGetFormClass:
    def test_snmp_v2c_returns_v1v2_form(self):
        from netbox_librenms_plugin.forms import AddToLIbreSNMPV1V2
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        view.request = _make_request({"snmp_version": "v2c"})
        assert view.get_form_class() is AddToLIbreSNMPV1V2

    def test_snmp_v1_returns_v1v2_form(self):
        from netbox_librenms_plugin.forms import AddToLIbreSNMPV1V2
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        view.request = _make_request({"snmp_version": "v1"})
        assert view.get_form_class() is AddToLIbreSNMPV1V2

    def test_snmp_v3_returns_v3_form(self):
        from netbox_librenms_plugin.forms import AddToLIbreSNMPV3
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        view.request = _make_request({"snmp_version": "v3"})
        assert view.get_form_class() is AddToLIbreSNMPV3

    def test_no_snmp_version_falls_back_to_prefixed(self):
        from netbox_librenms_plugin.forms import AddToLIbreSNMPV3
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        view.request = _make_request({"v3-snmp_version": "v3"})
        assert view.get_form_class() is AddToLIbreSNMPV3

    def test_v1v2_prefixed_returns_v1v2_form(self):
        from netbox_librenms_plugin.forms import AddToLIbreSNMPV1V2
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        view.request = _make_request({"v1v2-snmp_version": "v2c"})
        assert view.get_form_class() is AddToLIbreSNMPV1V2


class TestAddDeviceToLibreNMSViewGetObject:
    def test_vm_type_fetches_vm(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        mock_vm = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=mock_vm):
            result = view.get_object(5, "virtualmachine")
        assert result is mock_vm

    def test_no_type_tries_device_first(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.devices.Device") as mock_dev_cls:
            mock_dev_cls.objects.get.return_value = mock_device
            mock_dev_cls.DoesNotExist = Exception
            result = view.get_object(1)
        assert result is mock_device

    def test_device_not_found_falls_back_to_vm(self):
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        mock_vm = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.devices.Device") as mock_dev_cls:
            mock_dev_cls.DoesNotExist = Device.DoesNotExist
            mock_dev_cls.objects.get.side_effect = Device.DoesNotExist
            with patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=mock_vm):
                result = view.get_object(1)
        assert result is mock_vm


class TestAddDeviceToLibreNMSViewPost:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        mock_error = MagicMock()
        with patch.object(view, "require_write_permission", return_value=mock_error):
            result = view.post(view.request, object_id=1)
        assert result is mock_error

    def test_form_invalid_shows_error_and_redirects(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        view.request = _make_request({"v1v2-snmp_version": "v2c", "snmp_version": "v2c"})
        mock_obj = MagicMock()
        mock_obj.get_absolute_url.return_value = "/dcim/devices/1/"
        mock_form = MagicMock()
        mock_form.is_valid.return_value = False
        mock_form.errors.items.return_value = [("hostname", ["This field is required."])]
        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(view, "get_object", return_value=mock_obj):
                with patch.object(view, "get_form_class", return_value=MagicMock(return_value=mock_form)):
                    with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                        with patch("netbox_librenms_plugin.views.sync.devices.redirect") as mock_redirect:
                            view.post(view.request, object_id=1)
        mock_msg.error.assert_called()
        mock_redirect.assert_called_once()

    def test_form_valid_injects_snmp_version_for_v2c(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        view.request = _make_request({"v1v2-snmp_version": "v2c"})
        mock_obj = MagicMock()
        mock_obj.get_absolute_url.return_value = "/dcim/devices/1/"
        mock_form = MagicMock()
        mock_form.is_valid.return_value = True
        mock_form.cleaned_data = {}
        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(view, "get_object", return_value=mock_obj):
                with patch.object(view, "get_form_class", return_value=MagicMock(return_value=mock_form)):
                    with patch.object(view, "form_valid", return_value=MagicMock()):
                        view.post(view.request, object_id=1)
        assert mock_form.cleaned_data.get("snmp_version") == "v2c"


class TestAddDeviceToLibreNMSViewFormValid:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.request = _make_request()
        view.object = MagicMock()
        view.object.get_absolute_url.return_value = "/dcim/devices/1/"
        return view

    def _make_form(self, data):
        form = MagicMock()
        form.cleaned_data = data
        return form

    def test_v2c_includes_community_in_device_data(self):
        view = self._make_view()
        view._librenms_api.add_device.return_value = (True, "ok")
        form = self._make_form({"hostname": "h1", "community": "public", "force_add": False})
        with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
            with patch("netbox_librenms_plugin.views.sync.devices.messages"):
                view.form_valid(form, snmp_version="v2c")
        call_args = view._librenms_api.add_device.call_args[0][0]
        assert call_args["community"] == "public"
        assert call_args["snmp_version"] == "v2c"

    def test_v3_includes_auth_fields(self):
        view = self._make_view()
        view._librenms_api.add_device.return_value = (True, "ok")
        form = self._make_form(
            {
                "hostname": "h1",
                "force_add": False,
                "authlevel": "authPriv",
                "authname": "admin",
                "authpass": "secret",
                "authalgo": "SHA",
                "cryptopass": "crypt",
                "cryptoalgo": "AES",
            }
        )
        with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
            with patch("netbox_librenms_plugin.views.sync.devices.messages"):
                view.form_valid(form, snmp_version="v3")
        call_args = view._librenms_api.add_device.call_args[0][0]
        assert call_args["snmp_version"] == "v3"
        assert call_args["authlevel"] == "authPriv"
        assert "community" not in call_args

    def test_unknown_snmp_version_shows_error(self):
        view = self._make_view()
        form = self._make_form({"hostname": "h1", "force_add": False})
        with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
            with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                view.form_valid(form, snmp_version="v99")
        mock_msg.error.assert_called_once()
        view._librenms_api.add_device.assert_not_called()

    def test_optional_port_included_when_set(self):
        view = self._make_view()
        view._librenms_api.add_device.return_value = (True, "ok")
        form = self._make_form({"hostname": "h1", "community": "pub", "force_add": False, "port": 161})
        with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
            with patch("netbox_librenms_plugin.views.sync.devices.messages"):
                view.form_valid(form, snmp_version="v2c")
        call_args = view._librenms_api.add_device.call_args[0][0]
        assert call_args["port"] == 161

    def test_optional_port_skipped_when_none(self):
        view = self._make_view()
        view._librenms_api.add_device.return_value = (True, "ok")
        form = self._make_form({"hostname": "h1", "community": "pub", "force_add": False, "port": None})
        with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
            with patch("netbox_librenms_plugin.views.sync.devices.messages"):
                view.form_valid(form, snmp_version="v2c")
        call_args = view._librenms_api.add_device.call_args[0][0]
        assert "port" not in call_args

    def test_api_success_shows_success_message(self):
        view = self._make_view()
        view._librenms_api.add_device.return_value = (True, "Device added")
        form = self._make_form({"hostname": "h1", "community": "pub", "force_add": False})
        with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
            with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                view.form_valid(form, snmp_version="v2c")
        mock_msg.success.assert_called_once()

    def test_api_failure_shows_error_message(self):
        view = self._make_view()
        view._librenms_api.add_device.return_value = (False, "Connection failed")
        form = self._make_form({"hostname": "h1", "community": "pub", "force_add": False})
        with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
            with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                view.form_valid(form, snmp_version="v2c")
        mock_msg.error.assert_called_once()


# ===========================================================================
# devices.py — UpdateDeviceLocationView
# ===========================================================================


class TestUpdateDeviceLocationViewPost:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        view = object.__new__(UpdateDeviceLocationView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.request = _make_request()
        return view

    def test_permission_denied_returns_early(self):
        view = self._make_view()
        mock_error = MagicMock()
        with patch.object(view, "require_write_permission", return_value=mock_error):
            result = view.post(view.request, pk=1)
        assert result is mock_error

    def test_with_site_calls_update_device_field(self):
        view = self._make_view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.update_device_field.return_value = (True, "ok")
        device = MagicMock()
        device.site = MagicMock()
        device.site.name = "London"
        device.get_absolute_url.return_value = "/dcim/devices/1/"
        with patch.object(view, "require_write_permission", return_value=None):
            with patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=device):
                with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
                    with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                        view.post(view.request, pk=1)
        view._librenms_api.update_device_field.assert_called_once()
        mock_msg.success.assert_called_once()

    def test_without_site_shows_warning(self):
        view = self._make_view()
        view._librenms_api.get_librenms_id.return_value = 42
        device = MagicMock()
        device.site = None
        device.pk = 1
        with patch.object(view, "require_write_permission", return_value=None):
            with patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=device):
                with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
                    with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                        view.post(view.request, pk=1)
        view._librenms_api.update_device_field.assert_not_called()
        mock_msg.warning.assert_called_once()

    def test_api_failure_shows_error(self):
        view = self._make_view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.update_device_field.return_value = (False, "API error")
        device = MagicMock()
        device.site = MagicMock()
        device.site.name = "Paris"
        with patch.object(view, "require_write_permission", return_value=None):
            with patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=device):
                with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
                    with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                        view.post(view.request, pk=1)
        mock_msg.error.assert_called_once()


# ===========================================================================
# interfaces.py — SyncInterfacesView
# ===========================================================================


class TestSyncInterfacesViewStructure:
    def test_has_required_mixins(self):
        from netbox_librenms_plugin.views.mixins import (
            CacheMixin,
            LibreNMSPermissionMixin,
            NetBoxObjectPermissionMixin,
            VlanAssignmentMixin,
        )
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        mro = SyncInterfacesView.__mro__
        assert LibreNMSPermissionMixin in mro
        assert NetBoxObjectPermissionMixin in mro
        assert VlanAssignmentMixin in mro
        assert CacheMixin in mro


class TestSyncInterfacesViewGetRequiredPermissions:
    def test_device_returns_interface_permissions(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        perms = view.get_required_permissions_for_object_type("device")
        assert ("add", Interface) in perms
        assert ("change", Interface) in perms

    def test_virtualmachine_returns_vminterface_permissions(self):
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        perms = view.get_required_permissions_for_object_type("virtualmachine")
        assert ("add", VMInterface) in perms
        assert ("change", VMInterface) in perms

    def test_invalid_type_raises_http404(self):
        from django.http import Http404

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        raised = False
        try:
            view.get_required_permissions_for_object_type("bogus")
        except Http404:
            raised = True
        assert raised


class TestSyncInterfacesViewGetSelectedInterfaces:
    def test_empty_list_returns_none_and_shows_error(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        req = _make_request({"select": []})
        with patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msg:
            result = view.get_selected_interfaces(req, "ifName")
        assert result is None
        mock_msg.error.assert_called_once()

    def test_non_empty_returns_list(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        req = _make_request({"select": ["eth0", "eth1"]})
        result = view.get_selected_interfaces(req, "ifName")
        assert result == ["eth0", "eth1"]


class TestSyncInterfacesViewGetCachedPortsData:
    def test_cache_miss_returns_none_and_warns(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        obj = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache:
            mock_cache.get.return_value = None
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msg:
                    result = view.get_cached_ports_data(view.request, obj, "default")
        assert result is None
        mock_msg.warning.assert_called_once()

    def test_cache_hit_returns_ports_list(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        obj = MagicMock()
        ports = [{"ifName": "eth0"}]
        with patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache:
            mock_cache.get.return_value = {"ports": ports}
            with patch.object(view, "get_cache_key", return_value="key"):
                result = view.get_cached_ports_data(view.request, obj, "default")
        assert result == ports


class TestSyncInterfacesViewPost:
    def test_permission_denied_device_returns_early(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        mock_error = MagicMock()
        with patch.object(view, "require_all_permissions", return_value=mock_error):
            result = view.post(view.request, object_type="device", object_id=1)
        assert result is mock_error

    def test_permission_denied_vm_returns_early(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        mock_error = MagicMock()
        with patch.object(view, "require_all_permissions", return_value=mock_error):
            result = view.post(view.request, object_type="virtualmachine", object_id=1)
        assert result is mock_error

    def test_invalid_object_type_raises_404(self):
        from django.http import Http404

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        raised = False
        try:
            view.post(view.request, object_type="invalid", object_id=1)
        except Http404:
            raised = True
        assert raised

    def test_no_selection_redirects(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        mock_obj = MagicMock()
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch.object(view, "get_object", return_value=mock_obj):
                with patch(
                    "netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field",
                    return_value="ifName",
                ):
                    with patch.object(view, "get_selected_interfaces", return_value=None):
                        with patch(
                            "netbox_librenms_plugin.views.sync.interfaces.reverse",
                            return_value="/fake/",
                        ):
                            with patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mock_redirect:
                                view.post(view.request, object_type="device", object_id=1)
        mock_redirect.assert_called_once()

    def test_cache_miss_redirects(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        mock_obj = MagicMock()
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch.object(view, "get_object", return_value=mock_obj):
                with patch(
                    "netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field",
                    return_value="ifName",
                ):
                    with patch.object(view, "get_selected_interfaces", return_value=["eth0"]):
                        with patch.object(view, "get_cached_ports_data", return_value=None):
                            with patch(
                                "netbox_librenms_plugin.views.sync.interfaces.reverse",
                                return_value="/fake/",
                            ):
                                with patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mock_redirect:
                                    view.post(view.request, object_type="device", object_id=1)
        mock_redirect.assert_called_once()

    def test_device_full_sync_success(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        mock_obj = MagicMock()
        ports = [{"ifName": "eth0"}]
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch.object(view, "get_object", return_value=mock_obj):
                with patch(
                    "netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field",
                    return_value="ifName",
                ):
                    with patch.object(view, "get_selected_interfaces", return_value=["eth0"]):
                        with patch.object(view, "get_cached_ports_data", return_value=ports):
                            with patch.object(view, "get_vlan_groups_for_device", return_value=[]):
                                with patch.object(view, "_build_vlan_lookup_maps", return_value={}):
                                    with patch.object(view, "sync_selected_interfaces"):
                                        with patch(
                                            "netbox_librenms_plugin.views.sync.interfaces.reverse",
                                            return_value="/fake/",
                                        ):
                                            with patch("netbox_librenms_plugin.views.sync.interfaces.messages"):
                                                with patch(
                                                    "netbox_librenms_plugin.views.sync.interfaces.redirect"
                                                ) as mock_redirect:
                                                    view.post(
                                                        view.request,
                                                        object_type="device",
                                                        object_id=1,
                                                    )
        mock_redirect.assert_called_once()

    def test_vm_full_sync_success(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        mock_obj = MagicMock()
        ports = [{"ifName": "eth0"}]
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch.object(view, "get_object", return_value=mock_obj):
                with patch(
                    "netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field",
                    return_value="ifName",
                ):
                    with patch.object(view, "get_selected_interfaces", return_value=["eth0"]):
                        with patch.object(view, "get_cached_ports_data", return_value=ports):
                            with patch.object(view, "get_vlan_groups_for_device", return_value=[]):
                                with patch.object(view, "_build_vlan_lookup_maps", return_value={}):
                                    with patch.object(view, "sync_selected_interfaces"):
                                        with patch(
                                            "netbox_librenms_plugin.views.sync.interfaces.reverse",
                                            return_value="/fake/",
                                        ):
                                            with patch("netbox_librenms_plugin.views.sync.interfaces.messages"):
                                                with patch(
                                                    "netbox_librenms_plugin.views.sync.interfaces.redirect"
                                                ) as mock_redirect:
                                                    view.post(
                                                        view.request,
                                                        object_type="virtualmachine",
                                                        object_id=1,
                                                    )
        mock_redirect.assert_called_once()


class TestSyncInterfacesViewSyncInterface:
    def test_device_creates_interface_via_get_or_create(self):
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._post_server_key = "default"
        view._lookup_maps = {}
        mock_device = MagicMock()
        mock_device.__class__ = Device
        mock_device.id = 1
        mock_device.virtual_chassis = None
        view.request = _make_request()
        mock_iface = MagicMock()
        mock_iface_cls = MagicMock()
        mock_iface_cls.objects.get_or_create.return_value = (mock_iface, True)
        librenms_if = {"ifName": "eth0", "ifType": "ether", "ifSpeed": 1000000000}
        with patch("netbox_librenms_plugin.views.sync.interfaces.Interface", mock_iface_cls):
            with patch.object(view, "get_netbox_interface_type", return_value="1000base-t"):
                with patch.object(view, "update_interface_attributes"):
                    with patch.object(view, "_sync_interface_vlans"):
                        view.sync_interface(mock_device, librenms_if, [], "ifName")
        mock_iface_cls.objects.get_or_create.assert_called_once_with(device=mock_device, name="eth0")

    def test_vm_creates_vminterface_via_get_or_create(self):
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._post_server_key = "default"
        view._lookup_maps = {}
        mock_vm = MagicMock()
        mock_vm.__class__ = VirtualMachine
        view.request = _make_request()
        mock_iface = MagicMock()
        mock_vmiface_cls = MagicMock()
        mock_vmiface_cls.objects.get_or_create.return_value = (mock_iface, True)
        librenms_if = {"ifName": "eth0"}
        with patch("netbox_librenms_plugin.views.sync.interfaces.VMInterface", mock_vmiface_cls):
            with patch.object(view, "update_interface_attributes"):
                with patch.object(view, "_sync_interface_vlans"):
                    view.sync_interface(mock_vm, librenms_if, [], "ifName")
        mock_vmiface_cls.objects.get_or_create.assert_called_once_with(virtual_machine=mock_vm, name="eth0")

    def test_device_with_vc_member_selection_valid(self):
        # Patch Device.objects on the real class to avoid replacing the class
        # itself (which would break isinstance() in the view code).
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._post_server_key = "default"
        view._lookup_maps = {}
        mock_device = MagicMock()
        mock_device.__class__ = Device
        mock_device.id = 1
        mock_device.virtual_chassis = MagicMock()
        mock_device.virtual_chassis.members.values_list.return_value = [1, 7]
        view.request = _make_request({"device_selection_eth0": "7"})
        mock_target = MagicMock()
        mock_target.id = 7
        mock_iface = MagicMock()
        mock_iface_cls = MagicMock()
        mock_iface_cls.objects.get_or_create.return_value = (mock_iface, True)
        librenms_if = {"ifName": "eth0"}
        with patch("netbox_librenms_plugin.views.sync.interfaces.Interface", mock_iface_cls):
            with patch.object(Device, "objects") as mock_dev_mgr:
                mock_dev_mgr.get.return_value = mock_target
                with patch.object(view, "get_netbox_interface_type", return_value="other"):
                    with patch.object(view, "update_interface_attributes"):
                        with patch.object(view, "_sync_interface_vlans"):
                            view.sync_interface(mock_device, librenms_if, [], "ifName")
        mock_iface_cls.objects.get_or_create.assert_called_once_with(device=mock_target, name="eth0")

    def test_device_with_invalid_vc_member_falls_back_to_obj(self):
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._post_server_key = "default"
        view._lookup_maps = {}
        mock_device = MagicMock()
        mock_device.__class__ = Device
        mock_device.id = 1
        mock_device.virtual_chassis = MagicMock()
        mock_device.virtual_chassis.members.values_list.return_value = [1, 2]
        view.request = _make_request({"device_selection_eth0": "99"})
        mock_target = MagicMock()
        mock_target.id = 99
        mock_iface = MagicMock()
        mock_iface_cls = MagicMock()
        mock_iface_cls.objects.get_or_create.return_value = (mock_iface, True)
        librenms_if = {"ifName": "eth0"}
        with patch("netbox_librenms_plugin.views.sync.interfaces.Interface", mock_iface_cls):
            with patch.object(Device, "objects") as mock_dev_mgr:
                mock_dev_mgr.get.return_value = mock_target
                with patch.object(view, "get_netbox_interface_type", return_value="other"):
                    with patch.object(view, "update_interface_attributes"):
                        with patch.object(view, "_sync_interface_vlans"):
                            view.sync_interface(mock_device, librenms_if, [], "ifName")
        # Falls back to mock_device (not mock_target) because 99 not in [1, 2]
        mock_iface_cls.objects.get_or_create.assert_called_once_with(device=mock_device, name="eth0")

    def test_device_with_device_selection_wrong_device_falls_back(self):
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._post_server_key = "default"
        view._lookup_maps = {}
        mock_device = MagicMock()
        mock_device.__class__ = Device
        mock_device.id = 1
        mock_device.virtual_chassis = None
        view.request = _make_request({"device_selection_eth0": "99"})
        mock_target = MagicMock()
        mock_target.id = 99  # != obj.id (1), no VC
        mock_iface = MagicMock()
        mock_iface_cls = MagicMock()
        mock_iface_cls.objects.get_or_create.return_value = (mock_iface, True)
        librenms_if = {"ifName": "eth0"}
        with patch("netbox_librenms_plugin.views.sync.interfaces.Interface", mock_iface_cls):
            with patch.object(Device, "objects") as mock_dev_mgr:
                mock_dev_mgr.get.return_value = mock_target
                with patch.object(view, "get_netbox_interface_type", return_value="other"):
                    with patch.object(view, "update_interface_attributes"):
                        with patch.object(view, "_sync_interface_vlans"):
                            view.sync_interface(mock_device, librenms_if, [], "ifName")
        # Falls back to mock_device because target.id != obj.id with no VC
        mock_iface_cls.objects.get_or_create.assert_called_once_with(device=mock_device, name="eth0")

    def test_invalid_object_type_raises_value_error(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._post_server_key = "default"
        view._lookup_maps = {}
        mock_other = MagicMock()
        mock_other.__class__ = object  # Not Device or VirtualMachine
        view.request = _make_request()
        librenms_if = {"ifName": "eth0"}
        raised = False
        try:
            view.sync_interface(mock_other, librenms_if, [], "ifName")
        except ValueError:
            raised = True
        assert raised


class TestSyncInterfacesViewGetNetboxInterfaceType:
    def test_speed_match_returns_mapped_type(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        librenms_if = {"ifType": "ethernetCsmacd", "ifSpeed": 1000000000}
        mock_mapping = MagicMock()
        mock_mapping.netbox_type = "1000base-t"
        mock_qs = MagicMock()
        mock_qs.filter.return_value.order_by.return_value.first.return_value = mock_mapping
        with patch("netbox_librenms_plugin.views.sync.interfaces.InterfaceTypeMapping") as mock_itm:
            with patch(
                "netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps",
                return_value=1000000,
            ):
                mock_itm.objects.filter.return_value = mock_qs
                result = view.get_netbox_interface_type(librenms_if)
        assert result is not None

    def test_fallback_no_speed_mapping(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        librenms_if = {"ifType": "ethernetCsmacd", "ifSpeed": None}
        mock_mapping = MagicMock()
        mock_mapping.netbox_type = "other"
        mock_qs = MagicMock()
        mock_qs.filter.return_value.first.return_value = mock_mapping
        with patch("netbox_librenms_plugin.views.sync.interfaces.InterfaceTypeMapping") as mock_itm:
            with patch(
                "netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps",
                return_value=None,
            ):
                mock_itm.objects.filter.return_value = mock_qs
                result = view.get_netbox_interface_type(librenms_if)
        assert result == "other"

    def test_no_mappings_returns_other(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        librenms_if = {"ifType": "unknown_type", "ifSpeed": None}
        mock_qs = MagicMock()
        mock_qs.filter.return_value.first.return_value = None
        with patch("netbox_librenms_plugin.views.sync.interfaces.InterfaceTypeMapping") as mock_itm:
            with patch(
                "netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps",
                return_value=None,
            ):
                mock_itm.objects.filter.return_value = mock_qs
                result = view.get_netbox_interface_type(librenms_if)
        assert result == "other"


class TestSyncInterfacesViewHandleMacAddress:
    def test_no_mac_address_no_op(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        mock_iface = MagicMock()
        view.handle_mac_address(mock_iface, None)
        mock_iface.mac_addresses.filter.assert_not_called()

    def test_existing_mac_is_reused(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        mock_iface = MagicMock()
        existing_mac = MagicMock()
        mock_iface.mac_addresses.filter.return_value.first.return_value = existing_mac
        view.handle_mac_address(mock_iface, "aa:bb:cc:dd:ee:ff")
        mock_iface.mac_addresses.add.assert_called_once_with(existing_mac)

    def test_new_mac_is_created(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        mock_iface = MagicMock()
        mock_iface.mac_addresses.filter.return_value.first.return_value = None
        new_mac = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.interfaces.MACAddress") as mock_mac_cls:
            mock_mac_cls.objects.create.return_value = new_mac
            view.handle_mac_address(mock_iface, "aa:bb:cc:dd:ee:ff")
        mock_mac_cls.objects.create.assert_called_once_with(mac_address="aa:bb:cc:dd:ee:ff")
        mock_iface.mac_addresses.add.assert_called_once_with(new_mac)


# ===========================================================================
# interfaces.py — DeleteNetBoxInterfacesView
# ===========================================================================


class TestDeleteNetBoxInterfacesViewPost:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        view = object.__new__(DeleteNetBoxInterfacesView)
        view._librenms_api = MagicMock()
        view.request = _make_request()
        return view

    def test_permission_denied_returns_json_403(self):
        view = self._make_view()
        mock_error = MagicMock()
        mock_error.status_code = 403
        with patch.object(view, "require_all_permissions_json", return_value=mock_error):
            result = view.post(view.request, object_type="device", object_id=1)
        assert result is mock_error

    def test_invalid_object_type_returns_400(self):
        from django.http import Http404

        view = self._make_view()
        # get_required_permissions_for_object_type raises Http404 for invalid types
        raised = False
        try:
            view.post(view.request, object_type="bogus", object_id=1)
        except Http404:
            raised = True
        assert raised

    def test_no_interface_ids_returns_400(self):
        view = self._make_view()
        req = _make_request({"interface_ids": []})
        mock_obj = MagicMock()
        with patch.object(view, "require_all_permissions_json", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.sync.interfaces.get_object_or_404",
                return_value=mock_obj,
            ):
                result = view.post(req, object_type="device", object_id=1)
        assert result.status_code == 400

    def test_device_interface_wrong_device_skipped(self):
        import json

        view = self._make_view()
        req = _make_request({"interface_ids": ["5"]})
        mock_obj = MagicMock()
        mock_obj.id = 1
        mock_obj.virtual_chassis = None
        mock_iface = MagicMock()
        mock_iface.name = "eth0"
        mock_iface.device_id = 99  # Wrong device
        with patch.object(view, "require_all_permissions_json", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.sync.interfaces.get_object_or_404",
                return_value=mock_obj,
            ):
                with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_iface_cls:
                    mock_iface_cls.objects.get.return_value = mock_iface
                    mock_iface_cls.DoesNotExist = Exception
                    with patch(
                        "netbox_librenms_plugin.views.sync.interfaces.transaction",
                        _atomic_txn(),
                    ):
                        result = view.post(req, object_type="device", object_id=1)
        data = json.loads(result.content)
        assert data["deleted_count"] == 0

    def test_vm_interface_wrong_vm_skipped(self):
        import json

        view = self._make_view()
        req = _make_request({"interface_ids": ["5"]})
        mock_obj = MagicMock()
        mock_obj.id = 1
        mock_vmiface = MagicMock()
        mock_vmiface.name = "eth0"
        mock_vmiface.virtual_machine_id = 99  # Wrong VM
        with patch.object(view, "require_all_permissions_json", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.sync.interfaces.get_object_or_404",
                return_value=mock_obj,
            ):
                with patch("netbox_librenms_plugin.views.sync.interfaces.VMInterface") as mock_vmiface_cls:
                    mock_vmiface_cls.objects.get.return_value = mock_vmiface
                    mock_vmiface_cls.DoesNotExist = Exception
                    with patch(
                        "netbox_librenms_plugin.views.sync.interfaces.transaction",
                        _atomic_txn(),
                    ):
                        result = view.post(req, object_type="virtualmachine", object_id=1)
        data = json.loads(result.content)
        assert data["deleted_count"] == 0

    def test_deletes_device_interface_successfully(self):
        import json

        view = self._make_view()
        req = _make_request({"interface_ids": ["5"]})
        mock_obj = MagicMock()
        mock_obj.id = 1
        mock_obj.virtual_chassis = None
        mock_iface = MagicMock()
        mock_iface.name = "eth0"
        mock_iface.device_id = 1  # Correct device
        with patch.object(view, "require_all_permissions_json", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.sync.interfaces.get_object_or_404",
                return_value=mock_obj,
            ):
                with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_iface_cls:
                    mock_iface_cls.objects.get.return_value = mock_iface
                    mock_iface_cls.DoesNotExist = Exception
                    with patch(
                        "netbox_librenms_plugin.views.sync.interfaces.transaction",
                        _atomic_txn(),
                    ):
                        result = view.post(req, object_type="device", object_id=1)
        data = json.loads(result.content)
        assert data["deleted_count"] == 1
        mock_iface.delete.assert_called_once()

    def test_deletes_vm_interface_successfully(self):
        import json

        view = self._make_view()
        req = _make_request({"interface_ids": ["5"]})
        mock_obj = MagicMock()
        mock_obj.id = 1
        mock_vmiface = MagicMock()
        mock_vmiface.name = "eth0"
        mock_vmiface.virtual_machine_id = 1  # Correct VM
        with patch.object(view, "require_all_permissions_json", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.sync.interfaces.get_object_or_404",
                return_value=mock_obj,
            ):
                with patch("netbox_librenms_plugin.views.sync.interfaces.VMInterface") as mock_vmiface_cls:
                    mock_vmiface_cls.objects.get.return_value = mock_vmiface
                    mock_vmiface_cls.DoesNotExist = Exception
                    with patch(
                        "netbox_librenms_plugin.views.sync.interfaces.transaction",
                        _atomic_txn(),
                    ):
                        result = view.post(req, object_type="virtualmachine", object_id=1)
        data = json.loads(result.content)
        assert data["deleted_count"] == 1

    def test_interface_not_found_adds_error(self):
        import json

        from dcim.models import Interface

        view = self._make_view()
        req = _make_request({"interface_ids": ["999"]})
        mock_obj = MagicMock()
        mock_obj.id = 1
        mock_obj.virtual_chassis = None
        with patch.object(view, "require_all_permissions_json", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.sync.interfaces.get_object_or_404",
                return_value=mock_obj,
            ):
                with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_iface_cls:
                    mock_iface_cls.DoesNotExist = Interface.DoesNotExist
                    mock_iface_cls.objects.get.side_effect = Interface.DoesNotExist
                    with patch(
                        "netbox_librenms_plugin.views.sync.interfaces.transaction",
                        _atomic_txn(),
                    ):
                        result = view.post(req, object_type="device", object_id=1)
        data = json.loads(result.content)
        assert "errors" in data
        assert data["deleted_count"] == 0

    def test_device_with_vc_validates_members(self):
        import json

        view = self._make_view()
        req = _make_request({"interface_ids": ["5"]})
        mock_obj = MagicMock()
        mock_obj.id = 1
        mock_obj.virtual_chassis = MagicMock()
        member = MagicMock()
        member.id = 1
        mock_obj.virtual_chassis.members.all.return_value = [member]
        mock_iface = MagicMock()
        mock_iface.name = "eth0"
        mock_iface.device_id = 99  # Not in VC members
        with patch.object(view, "require_all_permissions_json", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.sync.interfaces.get_object_or_404",
                return_value=mock_obj,
            ):
                with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_iface_cls:
                    mock_iface_cls.objects.get.return_value = mock_iface
                    mock_iface_cls.DoesNotExist = Exception
                    with patch(
                        "netbox_librenms_plugin.views.sync.interfaces.transaction",
                        _atomic_txn(),
                    ):
                        result = view.post(req, object_type="device", object_id=1)
        data = json.loads(result.content)
        assert data["deleted_count"] == 0


# ===========================================================================
# ip_addresses.py — SyncIPAddressesView
# ===========================================================================


class TestSyncIPAddressesViewStructure:
    def test_has_required_mixins(self):
        from netbox_librenms_plugin.views.mixins import (
            CacheMixin,
            LibreNMSPermissionMixin,
            NetBoxObjectPermissionMixin,
        )
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        mro = SyncIPAddressesView.__mro__
        assert LibreNMSPermissionMixin in mro
        assert NetBoxObjectPermissionMixin in mro
        assert CacheMixin in mro

    def test_required_object_permissions(self):
        from ipam.models import IPAddress

        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        perms = SyncIPAddressesView.required_object_permissions["POST"]
        assert ("add", IPAddress) in perms
        assert ("change", IPAddress) in perms


class TestSyncIPAddressesViewGetVrfSelection:
    def test_no_vrf_id_returns_none(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        req = _make_request({})
        result = view.get_vrf_selection(req, "192.168.1.1")
        assert result is None

    def test_valid_vrf_id_returns_vrf(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        req = _make_request({"vrf_192.168.1.1": "3"})
        mock_vrf = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.VRF") as mock_vrf_cls:
            mock_vrf_cls.objects.get.return_value = mock_vrf
            result = view.get_vrf_selection(req, "192.168.1.1")
        assert result is mock_vrf

    def test_vrf_does_not_exist_returns_none(self):
        from ipam.models import VRF

        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        req = _make_request({"vrf_192.168.1.1": "999"})
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.VRF") as mock_vrf_cls:
            mock_vrf_cls.DoesNotExist = VRF.DoesNotExist
            mock_vrf_cls.objects.get.side_effect = VRF.DoesNotExist
            result = view.get_vrf_selection(req, "192.168.1.1")
        assert result is None


class TestSyncIPAddressesViewGetCachedIpData:
    def test_cache_miss_returns_none(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        obj = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.cache") as mock_cache:
            mock_cache.get.return_value = None
            with patch.object(view, "get_cache_key", return_value="key"):
                result = view.get_cached_ip_data(view.request, obj)
        assert result is None

    def test_cache_hit_returns_ip_list(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        obj = MagicMock()
        ips = [{"ip_address": "192.168.1.1"}]
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.cache") as mock_cache:
            mock_cache.get.return_value = {"ip_addresses": ips}
            with patch.object(view, "get_cache_key", return_value="key"):
                result = view.get_cached_ip_data(view.request, obj)
        assert result == ips


class TestSyncIPAddressesViewGetObject:
    def test_device_type_returns_device(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        mock_dev = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.sync.ip_addresses.get_object_or_404",
            return_value=mock_dev,
        ):
            result = view.get_object("device", 1)
        assert result is mock_dev

    def test_vm_type_returns_vm(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        mock_vm = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.sync.ip_addresses.get_object_or_404",
            return_value=mock_vm,
        ):
            result = view.get_object("virtualmachine", 1)
        assert result is mock_vm

    def test_invalid_type_raises_404(self):
        from django.http import Http404

        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        raised = False
        try:
            view.get_object("invalid", 1)
        except Http404:
            raised = True
        assert raised


class TestSyncIPAddressesViewGetIpTabUrl:
    def test_device_url_includes_tab(self):
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        view._post_server_key = None
        mock_obj = MagicMock()
        mock_obj.__class__ = Device
        mock_obj.pk = 1
        with patch(
            "netbox_librenms_plugin.views.sync.ip_addresses.reverse",
            return_value="/fake/",
        ):
            url = view.get_ip_tab_url(mock_obj)
        assert "ipaddresses" in url

    def test_vm_url_includes_tab(self):
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        view._post_server_key = None
        mock_obj = MagicMock()
        mock_obj.__class__ = VirtualMachine
        mock_obj.pk = 2
        with patch(
            "netbox_librenms_plugin.views.sync.ip_addresses.reverse",
            return_value="/fake/",
        ):
            url = view.get_ip_tab_url(mock_obj)
        assert "ipaddresses" in url

    def test_server_key_appended(self):
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        view._post_server_key = "myserver"
        mock_obj = MagicMock()
        mock_obj.__class__ = Device
        mock_obj.pk = 1
        with patch(
            "netbox_librenms_plugin.views.sync.ip_addresses.reverse",
            return_value="/fake/",
        ):
            url = view.get_ip_tab_url(mock_obj)
        assert "myserver" in url


class TestSyncIPAddressesViewPost:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        mock_error = MagicMock()
        with patch.object(view, "require_all_permissions", return_value=mock_error):
            result = view.post(view.request, object_type="device", pk=1)
        assert result is mock_error

    def test_cache_miss_shows_error_and_redirects(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        mock_obj = MagicMock()
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch.object(view, "get_object", return_value=mock_obj):
                with patch.object(view, "get_cached_ip_data", return_value=None):
                    with patch.object(view, "get_ip_tab_url", return_value="/fake/?tab=ipaddresses"):
                        with patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msg:
                            with patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect") as mock_redirect:
                                view.post(view.request, object_type="device", pk=1)
        mock_msg.error.assert_called()
        mock_redirect.assert_called_once()

    def test_no_selected_ips_shows_error_and_redirects(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        mock_obj = MagicMock()
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch.object(view, "get_object", return_value=mock_obj):
                with patch.object(view, "get_cached_ip_data", return_value=[{"ip_address": "10.0.0.1"}]):
                    with patch.object(view, "get_selected_ips", return_value=[]):
                        with patch.object(view, "get_ip_tab_url", return_value="/fake/?tab=ipaddresses"):
                            with patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msg:
                                with patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect") as mock_redirect:
                                    view.post(view.request, object_type="device", pk=1)
        mock_msg.error.assert_called()
        mock_redirect.assert_called_once()

    def test_successful_sync_redirects(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        mock_obj = MagicMock()
        results = {"created": [], "updated": [], "unchanged": [], "failed": []}
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch.object(view, "get_object", return_value=mock_obj):
                with patch.object(view, "get_cached_ip_data", return_value=[{"ip_address": "10.0.0.1"}]):
                    with patch.object(view, "get_selected_ips", return_value=["10.0.0.1"]):
                        with patch.object(view, "process_ip_sync", return_value=results):
                            with patch.object(view, "display_sync_results"):
                                with patch.object(
                                    view,
                                    "get_ip_tab_url",
                                    return_value="/fake/?tab=ipaddresses",
                                ):
                                    with patch(
                                        "netbox_librenms_plugin.views.sync.ip_addresses.redirect"
                                    ) as mock_redirect:
                                        view.post(view.request, object_type="device", pk=1)
        mock_redirect.assert_called_once()


class TestSyncIPAddressesViewProcessIpSync:
    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        view._post_server_key = "default"
        return view

    def test_creates_new_ip_address(self):
        view = self._setup_view()
        selected = ["10.0.0.1"]
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "interface_url": None}]
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction", _atomic_txn()):
            with patch("netbox_librenms_plugin.views.sync.ip_addresses.IPAddress") as mock_ip_cls:
                mock_ip_cls.objects.filter.return_value.first.return_value = None
                with patch.object(view, "get_vrf_selection", return_value=None):
                    results = view.process_ip_sync(view.request, selected, cached, MagicMock(), "device")
        assert "10.0.0.1" in results["created"]

    def test_updates_existing_ip_address_different_interface(self):
        view = self._setup_view()
        selected = ["10.0.0.1"]
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "interface_url": None}]
        existing_ip = MagicMock()
        existing_ip.assigned_object = MagicMock()  # Different from None interface
        existing_ip.vrf = None
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction", _atomic_txn()):
            with patch("netbox_librenms_plugin.views.sync.ip_addresses.IPAddress") as mock_ip_cls:
                mock_ip_cls.objects.filter.return_value.first.return_value = existing_ip
                with patch.object(view, "get_vrf_selection", return_value=None):
                    results = view.process_ip_sync(view.request, selected, cached, MagicMock(), "device")
        assert "10.0.0.1" in results["updated"]
        existing_ip.save.assert_called_once()

    def test_unchanged_ip_address_skipped(self):
        view = self._setup_view()
        selected = ["10.0.0.1"]
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "interface_url": None}]
        existing_ip = MagicMock()
        existing_ip.assigned_object = None  # Same as interface (None)
        existing_ip.vrf = None
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction", _atomic_txn()):
            with patch("netbox_librenms_plugin.views.sync.ip_addresses.IPAddress") as mock_ip_cls:
                mock_ip_cls.objects.filter.return_value.first.return_value = existing_ip
                with patch.object(view, "get_vrf_selection", return_value=None):
                    results = view.process_ip_sync(view.request, selected, cached, MagicMock(), "device")
        assert "10.0.0.1" in results["unchanged"]

    def test_ip_with_interface_url_device(self):
        view = self._setup_view()
        selected = ["10.0.0.1"]
        cached = [
            {
                "ip_address": "10.0.0.1",
                "ip_with_mask": "10.0.0.1/24",
                "interface_url": "/api/dcim/interfaces/5/",
            }
        ]
        mock_iface = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction", _atomic_txn()):
            with patch("netbox_librenms_plugin.views.sync.ip_addresses.IPAddress") as mock_ip_cls:
                mock_ip_cls.objects.filter.return_value.first.return_value = None
                with patch("netbox_librenms_plugin.views.sync.ip_addresses.Interface") as mock_iface_cls:
                    mock_iface_cls.objects.get.return_value = mock_iface
                    with patch.object(view, "get_vrf_selection", return_value=None):
                        view.process_ip_sync(view.request, selected, cached, MagicMock(), "device")
        mock_iface_cls.objects.get.assert_called_once_with(id="5")

    def test_ip_with_interface_url_vm(self):
        view = self._setup_view()
        selected = ["10.0.0.1"]
        cached = [
            {
                "ip_address": "10.0.0.1",
                "ip_with_mask": "10.0.0.1/24",
                "interface_url": "/api/virtualization/interfaces/7/",
            }
        ]
        mock_vmiface = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction", _atomic_txn()):
            with patch("netbox_librenms_plugin.views.sync.ip_addresses.IPAddress") as mock_ip_cls:
                mock_ip_cls.objects.filter.return_value.first.return_value = None
                with patch("netbox_librenms_plugin.views.sync.ip_addresses.VMInterface") as mock_vmiface_cls:
                    mock_vmiface_cls.objects.get.return_value = mock_vmiface
                    with patch.object(view, "get_vrf_selection", return_value=None):
                        view.process_ip_sync(view.request, selected, cached, MagicMock(), "virtualmachine")
        mock_vmiface_cls.objects.get.assert_called_once_with(id="7")


class TestSyncIPAddressesViewDisplaySyncResults:
    def test_created_calls_success(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msg:
            view.display_sync_results(
                view.request,
                {"created": ["10.0.0.1"], "updated": [], "unchanged": [], "failed": []},
            )
        mock_msg.success.assert_called()

    def test_updated_calls_success(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msg:
            view.display_sync_results(
                view.request,
                {"created": [], "updated": ["10.0.0.2"], "unchanged": [], "failed": []},
            )
        mock_msg.success.assert_called()

    def test_unchanged_calls_warning(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msg:
            view.display_sync_results(
                view.request,
                {"created": [], "updated": [], "unchanged": ["10.0.0.3"], "failed": []},
            )
        mock_msg.warning.assert_called_once()

    def test_failed_calls_error(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msg:
            view.display_sync_results(
                view.request,
                {"created": [], "updated": [], "unchanged": [], "failed": ["10.0.0.4"]},
            )
        mock_msg.error.assert_called_once()


# ===========================================================================
# locations.py — SyncSiteLocationView
# ===========================================================================


class TestSyncSiteLocationViewStructure:
    def test_has_required_mixins(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSPermissionMixin
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        mro = SyncSiteLocationView.__mro__
        assert LibreNMSPermissionMixin in mro
        assert LibreNMSAPIMixin in mro


class TestSyncSiteLocationViewCheckCoordinatesMatch:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view._librenms_api = MagicMock()
        view.request = _make_request()
        return view

    def test_matching_within_tolerance(self):
        view = self._make_view()
        result = view.check_coordinates_match(51.5074, -0.1278, 51.5074, -0.1278)
        assert result is True

    def test_outside_tolerance(self):
        view = self._make_view()
        result = view.check_coordinates_match(51.5074, -0.1278, 52.0000, -0.1278)
        assert result is False

    def test_any_none_returns_false(self):
        view = self._make_view()
        assert view.check_coordinates_match(None, -0.1278, 51.5074, -0.1278) is False
        assert view.check_coordinates_match(51.5074, None, 51.5074, -0.1278) is False
        assert view.check_coordinates_match(51.5074, -0.1278, None, -0.1278) is False
        assert view.check_coordinates_match(51.5074, -0.1278, 51.5074, None) is False


class TestSyncSiteLocationViewMatchSiteWithLocation:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view._librenms_api = MagicMock()
        view.request = _make_request()
        return view

    def test_matches_by_name_case_insensitive(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.slug = "london"
        locations = [{"location": "london"}, {"location": "Paris"}]
        result = view.match_site_with_location(site, locations)
        assert result == {"location": "london"}

    def test_matches_by_slug(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "New York"
        site.slug = "new-york"
        locations = [{"location": "paris"}, {"location": "new-york"}]
        result = view.match_site_with_location(site, locations)
        assert result == {"location": "new-york"}

    def test_no_match_returns_none(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "Tokyo"
        site.slug = "tokyo"
        locations = [{"location": "london"}, {"location": "paris"}]
        result = view.match_site_with_location(site, locations)
        assert result is None


class TestSyncSiteLocationViewCreateSyncData:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view._librenms_api = MagicMock()
        view.request = _make_request()
        return view

    def test_with_matching_location_synced_coords(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.slug = "london"
        site.latitude = 51.5074
        site.longitude = -0.1278
        locations = [{"location": "london", "lat": "51.5074", "lng": "-0.1278"}]
        result = view.create_sync_data(site, locations)
        assert result.netbox_site is site
        assert result.librenms_location == locations[0]
        assert result.is_synced is True

    def test_with_matching_location_unsynced_coords(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.slug = "london"
        site.latitude = 51.5074
        site.longitude = -0.1278
        locations = [{"location": "london", "lat": "52.0000", "lng": "-0.1278"}]
        result = view.create_sync_data(site, locations)
        assert result.is_synced is False

    def test_no_matching_location(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "Tokyo"
        site.slug = "tokyo"
        site.latitude = 35.6762
        site.longitude = 139.6503
        locations = [{"location": "london", "lat": "51.5074", "lng": "-0.1278"}]
        result = view.create_sync_data(site, locations)
        assert result.librenms_location is None
        assert result.is_synced is False


class TestSyncSiteLocationViewGetSiteByPk:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view._librenms_api = MagicMock()
        view.request = _make_request()
        return view

    def test_found_returns_site(self):
        view = self._make_view()
        mock_site = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls:
            mock_site_cls.objects.get.return_value = mock_site
            result = view.get_site_by_pk(1)
        assert result is mock_site

    def test_not_found_returns_none(self):
        from django.core.exceptions import ObjectDoesNotExist

        view = self._make_view()
        with patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls:
            mock_site_cls.objects.get.side_effect = ObjectDoesNotExist
            result = view.get_site_by_pk(999)
        assert result is None


class TestSyncSiteLocationViewPost:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view._librenms_api = MagicMock()
        view.request = _make_request()
        return view

    def test_permission_denied_returns_early(self):
        view = self._make_view()
        mock_error = MagicMock()
        with patch.object(view, "require_write_permission", return_value=mock_error):
            result = view.post(view.request)
        assert result is mock_error

    def test_no_pk_shows_error(self):
        view = self._make_view()
        req = _make_request({})
        with patch.object(view, "require_write_permission", return_value=None):
            with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
                with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                    view.post(req)
        mock_msg.error.assert_called_once()

    def test_site_not_found_shows_error(self):
        view = self._make_view()
        req = _make_request({"pk": "5", "action": "create"})
        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(view, "get_site_by_pk", return_value=None):
                with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
                    with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                        view.post(req)
        mock_msg.error.assert_called_once()

    def test_unknown_action_shows_error(self):
        view = self._make_view()
        req = _make_request({"pk": "5", "action": "delete"})
        mock_site = MagicMock()
        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(view, "get_site_by_pk", return_value=mock_site):
                with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
                    with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                        view.post(req)
        mock_msg.error.assert_called_once()

    def test_create_action_delegates(self):
        view = self._make_view()
        req = _make_request({"pk": "5", "action": "create"})
        mock_site = MagicMock()
        mock_response = MagicMock()
        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(view, "get_site_by_pk", return_value=mock_site):
                with patch.object(view, "create_librenms_location", return_value=mock_response) as mock_create:
                    result = view.post(req)
        mock_create.assert_called_once_with(req, mock_site)
        assert result is mock_response

    def test_update_action_delegates(self):
        view = self._make_view()
        req = _make_request({"pk": "5", "action": "update"})
        mock_site = MagicMock()
        mock_response = MagicMock()
        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(view, "get_site_by_pk", return_value=mock_site):
                with patch.object(view, "update_librenms_location", return_value=mock_response) as mock_update:
                    result = view.post(req)
        mock_update.assert_called_once_with(req, mock_site)
        assert result is mock_response


class TestSyncSiteLocationViewCreateLibrenmsLocation:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view._librenms_api = MagicMock()
        view.request = _make_request()
        return view

    def test_missing_lat_or_lng_warns(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.latitude = None
        site.longitude = -0.1278
        with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
            with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                view.create_librenms_location(view.request, site)
        mock_msg.warning.assert_called_once()
        view._librenms_api.add_location.assert_not_called()

    def test_api_success_shows_success(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.latitude = 51.5074
        site.longitude = -0.1278
        view._librenms_api.add_location.return_value = (True, "Created")
        with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
            with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                view.create_librenms_location(view.request, site)
        mock_msg.success.assert_called_once()

    def test_api_failure_shows_error(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.latitude = 51.5074
        site.longitude = -0.1278
        view._librenms_api.add_location.return_value = (False, "Failed")
        with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
            with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                view.create_librenms_location(view.request, site)
        mock_msg.error.assert_called_once()


class TestSyncSiteLocationViewUpdateLibrenmsLocation:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view._librenms_api = MagicMock()
        view.request = _make_request()
        return view

    def test_missing_lat_or_lng_warns(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.latitude = 51.5074
        site.longitude = None
        with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
            with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                view.update_librenms_location(view.request, site)
        mock_msg.warning.assert_called_once()
        view._librenms_api.get_locations.assert_not_called()

    def test_get_locations_fails(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.latitude = 51.5074
        site.longitude = -0.1278
        view._librenms_api.get_locations.return_value = (False, "Error")
        with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
            with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                view.update_librenms_location(view.request, site)
        mock_msg.error.assert_called_once()
        view._librenms_api.update_location.assert_not_called()

    def test_no_matching_location_shows_error(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "Tokyo"
        site.slug = "tokyo"
        site.latitude = 35.6762
        site.longitude = 139.6503
        view._librenms_api.get_locations.return_value = (True, [{"location": "london"}])
        with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
            with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                view.update_librenms_location(view.request, site)
        mock_msg.error.assert_called_once()
        view._librenms_api.update_location.assert_not_called()

    def test_api_success_shows_success(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.slug = "london"
        site.latitude = 51.5074
        site.longitude = -0.1278
        view._librenms_api.get_locations.return_value = (True, [{"location": "london"}])
        view._librenms_api.update_location.return_value = (True, "Updated")
        with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
            with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                view.update_librenms_location(view.request, site)
        mock_msg.success.assert_called_once()

    def test_api_failure_shows_error(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.slug = "london"
        site.latitude = 51.5074
        site.longitude = -0.1278
        view._librenms_api.get_locations.return_value = (True, [{"location": "london"}])
        view._librenms_api.update_location.return_value = (False, "Failure")
        with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
            with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                view.update_librenms_location(view.request, site)
        mock_msg.error.assert_called_once()


class TestSyncSiteLocationViewBuildLocationData:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view._librenms_api = MagicMock()
        view.request = _make_request()
        return view

    def test_includes_name_by_default(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.latitude = 51.5074
        site.longitude = -0.1278
        data = view.build_location_data(site)
        assert "location" in data
        assert data["location"] == "London"
        assert data["lat"] == str(site.latitude)
        assert data["lng"] == str(site.longitude)

    def test_excludes_name_when_false(self):
        view = self._make_view()
        site = MagicMock()
        site.name = "London"
        site.latitude = 51.5074
        site.longitude = -0.1278
        data = view.build_location_data(site, include_name=False)
        assert "location" not in data
        assert "lat" in data
        assert "lng" in data


# ===========================================================================
# vlans.py — SyncVLANsView
# ===========================================================================


class TestSyncVLANsViewStructure:
    def test_has_required_mixins(self):
        from netbox_librenms_plugin.views.mixins import (
            CacheMixin,
            LibreNMSPermissionMixin,
            NetBoxObjectPermissionMixin,
        )
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        mro = SyncVLANsView.__mro__
        assert LibreNMSPermissionMixin in mro
        assert NetBoxObjectPermissionMixin in mro
        assert CacheMixin in mro

    def test_required_object_permissions(self):
        from ipam.models import VLAN

        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        perms = SyncVLANsView.required_object_permissions["POST"]
        assert ("add", VLAN) in perms
        assert ("change", VLAN) in perms


class TestSyncVLANsViewGetObject:
    def test_device_type_returns_device(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = _make_view(SyncVLANsView)
        mock_dev = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.sync.vlans.get_object_or_404",
            return_value=mock_dev,
        ):
            result = view.get_object("device", 1)
        assert result is mock_dev

    def test_invalid_type_raises_404(self):
        from django.http import Http404

        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = _make_view(SyncVLANsView)
        raised = False
        try:
            view.get_object("invalid", 1)
        except Http404:
            raised = True
        assert raised


class TestSyncVLANsViewRedirect:
    def test_device_redirect_url(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = _make_view(SyncVLANsView)
        view._post_server_key = None
        with patch(
            "netbox_librenms_plugin.views.sync.vlans.reverse",
            return_value="/fake/",
        ):
            with patch("netbox_librenms_plugin.views.sync.vlans.redirect") as mock_redirect:
                view._redirect("device", 1)
        mock_redirect.assert_called_once()
        call_arg = mock_redirect.call_args[0][0]
        assert "vlans" in call_arg

    def test_server_key_in_redirect(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = _make_view(SyncVLANsView)
        view._post_server_key = "myserver"
        with patch(
            "netbox_librenms_plugin.views.sync.vlans.reverse",
            return_value="/fake/",
        ):
            with patch("netbox_librenms_plugin.views.sync.vlans.redirect") as mock_redirect:
                view._redirect("device", 1)
        call_arg = mock_redirect.call_args[0][0]
        assert "myserver" in call_arg


class TestSyncVLANsViewPost:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = _make_view(SyncVLANsView)
        mock_error = MagicMock()
        with patch.object(view, "require_all_permissions", return_value=mock_error):
            result = view.post(view.request, object_type="device", object_id=1)
        assert result is mock_error

    def test_invalid_action_shows_error(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = _make_view(SyncVLANsView)
        req = _make_request({"action": "delete_vlans", "server_key": ""})
        mock_obj = MagicMock()
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch.object(view, "get_object", return_value=mock_obj):
                with patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msg:
                    with patch.object(view, "_redirect", return_value=MagicMock()):
                        view.post(req, object_type="device", object_id=1)
        mock_msg.error.assert_called_once()

    def test_create_vlans_action_delegates(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = _make_view(SyncVLANsView)
        req = _make_request({"action": "create_vlans", "server_key": ""})
        mock_obj = MagicMock()
        mock_response = MagicMock()
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch.object(view, "get_object", return_value=mock_obj):
                with patch.object(view, "_handle_create_vlans", return_value=mock_response) as mock_handle:
                    result = view.post(req, object_type="device", object_id=1)
        mock_handle.assert_called_once()
        assert result is mock_response


class TestSyncVLANsViewHandleCreateVlans:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._post_server_key = "default"
        view.request = _make_request()
        return view

    def test_no_selected_vlans_shows_error(self):
        view = self._make_view()
        req = _make_request({"select": []})
        mock_obj = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msg:
            with patch.object(view, "_redirect", return_value=MagicMock()):
                view._handle_create_vlans(req, mock_obj, "device", 1)
        mock_msg.error.assert_called_once()

    def test_cache_miss_shows_error(self):
        view = self._make_view()
        req = _make_request({"select": ["10"]})
        mock_obj = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache:
            mock_cache.get.return_value = None
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msg:
                    with patch.object(view, "_redirect", return_value=MagicMock()):
                        view._handle_create_vlans(req, mock_obj, "device", 1)
        mock_msg.error.assert_called_once()

    def test_invalid_vid_string_skipped(self):
        view = self._make_view()
        req = _make_request({"select": ["not-a-number"]})
        mock_obj = MagicMock()
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache:
            mock_cache.get.return_value = cached_vlans
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.vlans.transaction", _atomic_txn()):
                    with patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls:
                        with patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msg:
                            with patch.object(view, "_redirect", return_value=MagicMock()):
                                view._handle_create_vlans(req, mock_obj, "device", 1)
        mock_vlan_cls.objects.get_or_create.assert_not_called()
        mock_msg.warning.assert_called_once()

    def test_vid_not_in_librenms_data_skipped(self):
        view = self._make_view()
        req = _make_request({"select": ["99"]})
        mock_obj = MagicMock()
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache:
            mock_cache.get.return_value = cached_vlans
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.vlans.transaction", _atomic_txn()):
                    with patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls:
                        with patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msg:
                            with patch.object(view, "_redirect", return_value=MagicMock()):
                                view._handle_create_vlans(req, mock_obj, "device", 1)
        mock_vlan_cls.objects.get_or_create.assert_not_called()
        mock_msg.warning.assert_called_once()

    def test_creates_vlan_with_group(self):
        from ipam.models import VLANGroup

        view = self._make_view()
        req = _make_request({"select": ["10"], "vlan_group_10": "3"})
        mock_obj = MagicMock()
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        mock_group = MagicMock()
        mock_vlan = MagicMock()
        mock_vlan.name = "Management"
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache:
            mock_cache.get.return_value = cached_vlans
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.vlans.transaction", _atomic_txn()):
                    with patch("netbox_librenms_plugin.views.sync.vlans.VLANGroup") as mock_vg_cls:
                        mock_vg_cls.objects.get.return_value = mock_group
                        mock_vg_cls.DoesNotExist = VLANGroup.DoesNotExist
                        with patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls:
                            mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, True)
                            with patch("netbox_librenms_plugin.views.sync.vlans.messages"):
                                with patch.object(view, "_redirect", return_value=MagicMock()):
                                    view._handle_create_vlans(req, mock_obj, "device", 1)
        mock_vlan_cls.objects.get_or_create.assert_called_once_with(
            vid=10,
            group=mock_group,
            defaults={"name": "Management", "status": "active"},
        )

    def test_creates_vlan_global_no_group(self):
        view = self._make_view()
        req = _make_request({"select": ["10"]})
        mock_obj = MagicMock()
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        mock_vlan = MagicMock()
        mock_vlan.name = "Management"
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache:
            mock_cache.get.return_value = cached_vlans
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.vlans.transaction", _atomic_txn()):
                    with patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls:
                        mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, True)
                        with patch("netbox_librenms_plugin.views.sync.vlans.messages"):
                            with patch.object(view, "_redirect", return_value=MagicMock()):
                                view._handle_create_vlans(req, mock_obj, "device", 1)
        mock_vlan_cls.objects.get_or_create.assert_called_once_with(
            vid=10,
            group=None,
            defaults={"name": "Management", "status": "active"},
        )

    def test_updates_vlan_name_when_changed(self):
        view = self._make_view()
        req = _make_request({"select": ["10"]})
        mock_obj = MagicMock()
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "NewName"}]
        mock_vlan = MagicMock()
        mock_vlan.name = "OldName"  # Different from librenms_name
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache:
            mock_cache.get.return_value = cached_vlans
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.vlans.transaction", _atomic_txn()):
                    with patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls:
                        mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, False)
                        with patch("netbox_librenms_plugin.views.sync.vlans.messages"):
                            with patch.object(view, "_redirect", return_value=MagicMock()):
                                view._handle_create_vlans(req, mock_obj, "device", 1)
        mock_vlan.save.assert_called_once()

    def test_skips_unchanged_vlan(self):
        view = self._make_view()
        req = _make_request({"select": ["10"]})
        mock_obj = MagicMock()
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        mock_vlan = MagicMock()
        mock_vlan.name = "Management"  # Same as librenms_name
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache:
            mock_cache.get.return_value = cached_vlans
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.vlans.transaction", _atomic_txn()):
                    with patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls:
                        mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, False)
                        with patch("netbox_librenms_plugin.views.sync.vlans.messages"):
                            with patch.object(view, "_redirect", return_value=MagicMock()):
                                view._handle_create_vlans(req, mock_obj, "device", 1)
        mock_vlan.save.assert_not_called()

    def test_invalid_group_id_falls_back_to_global(self):
        from ipam.models import VLANGroup

        view = self._make_view()
        req = _make_request({"select": ["10"], "vlan_group_10": "999"})
        mock_obj = MagicMock()
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        mock_vlan = MagicMock()
        mock_vlan.name = "Management"
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache:
            mock_cache.get.return_value = cached_vlans
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.vlans.transaction", _atomic_txn()):
                    with patch("netbox_librenms_plugin.views.sync.vlans.VLANGroup") as mock_vg_cls:
                        mock_vg_cls.DoesNotExist = VLANGroup.DoesNotExist
                        mock_vg_cls.objects.get.side_effect = VLANGroup.DoesNotExist
                        with patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls:
                            mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, True)
                            with patch("netbox_librenms_plugin.views.sync.vlans.messages"):
                                with patch.object(view, "_redirect", return_value=MagicMock()):
                                    view._handle_create_vlans(req, mock_obj, "device", 1)
        # Falls back to group=None when VLANGroup.DoesNotExist
        call_kwargs = mock_vlan_cls.objects.get_or_create.call_args[1]
        assert call_kwargs["group"] is None

    def test_summary_message_shows_counts(self):
        view = self._make_view()
        req = _make_request({"select": ["10"]})
        mock_obj = MagicMock()
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        mock_vlan = MagicMock()
        mock_vlan.name = "Management"
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache:
            mock_cache.get.return_value = cached_vlans
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.vlans.transaction", _atomic_txn()):
                    with patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls:
                        mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, True)
                        with patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msg:
                            with patch.object(view, "_redirect", return_value=MagicMock()):
                                view._handle_create_vlans(req, mock_obj, "device", 1)
        mock_msg.success.assert_called_once()
        success_msg = mock_msg.success.call_args[0][1]
        assert "created" in success_msg

    def test_no_vlans_created_shows_warning(self):
        view = self._make_view()
        # Select VID that is not in cached data to get skipped_count=0 too
        req = _make_request({"select": ["99"]})
        mock_obj = MagicMock()
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache:
            mock_cache.get.return_value = cached_vlans
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.vlans.transaction", _atomic_txn()):
                    with patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msg:
                        with patch.object(view, "_redirect", return_value=MagicMock()):
                            view._handle_create_vlans(req, mock_obj, "device", 1)
        mock_msg.warning.assert_called_once()
