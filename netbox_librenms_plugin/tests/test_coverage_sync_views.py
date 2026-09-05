"""Comprehensive coverage tests for views/sync/ modules."""

from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip, make_vm
from netbox_librenms_plugin.tests.view_test_helpers import grant
from netbox_librenms_plugin.tests.view_test_helpers import make_request as make_real_request
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms, make_view, message_texts, missing_pk
from netbox_librenms_plugin.tests.view_test_helpers import post as _post


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
        from dcim.models import Cable, Device, Interface

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        perms = SyncCablesView.required_object_permissions["POST"]
        assert ("view", Device) in perms
        assert ("add", Cable) in perms
        assert ("change", Cable) in perms
        assert ("change", Interface) in perms


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

    @pytest.mark.django_db
    def test_interface_not_found_returns_missing_remote(self):
        """A remote interface id that doesn't exist → real Interface.DoesNotExist → missing_remote."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        dev = make_device("cable-sync-nf")
        local = make_interface(dev, "eth0")
        # remote id points at no row → the second Interface.objects.get raises DoesNotExist.
        link_data = {
            "local_port": "eth0",
            "netbox_local_interface_id": local.pk,
            "netbox_remote_interface_id": local.pk + 9999,
            "netbox_remote_device_id": dev.pk,
        }
        interface = {"local_port_id": "42"}
        result = view.handle_cable_creation(link_data, interface)
        assert result["status"] == "missing_remote"

    @pytest.mark.django_db
    def test_existing_cable_returns_duplicate(self):
        """Two already-cabled real interfaces → check_existing_cable True → duplicate."""
        from netbox_librenms_plugin.tests.conftest import cable_together, make_device, make_interface
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        dev = make_device("cable-sync-dup")
        local = make_interface(dev, "eth0")
        remote = make_interface(dev, "eth1")
        cable_together(local, remote)  # real existing cable

        link_data = {
            "local_port": "eth0",
            "netbox_local_interface_id": local.pk,
            "netbox_remote_interface_id": remote.pk,
            "netbox_remote_device_id": dev.pk,
        }
        interface = {"local_port_id": "42"}
        result = view.handle_cable_creation(link_data, interface)
        assert result["status"] == "duplicate"

    @pytest.mark.django_db
    def test_creates_cable_returns_valid(self):
        """Two uncabled real interfaces → a real Cable is created and persisted → valid."""
        from dcim.models import Cable

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = _make_view(SyncCablesView)
        dev = make_device("cable-sync-ok")
        local = make_interface(dev, "eth0")
        remote = make_interface(dev, "eth1")

        link_data = {
            "local_port": "eth0",
            "netbox_local_interface_id": local.pk,
            "netbox_remote_interface_id": remote.pk,
            "netbox_remote_device_id": dev.pk,
        }
        interface = {"local_port_id": "42"}
        result = view.handle_cable_creation(link_data, interface)

        assert result["status"] == "valid"
        # The cable was actually written to the DB connecting the two interfaces.
        local.refresh_from_db()
        remote.refresh_from_db()
        assert local.cable_id is not None
        assert local.cable_id == remote.cable_id
        assert Cable.objects.filter(pk=local.cable_id).exists()


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
            with patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ):
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
            with patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ):
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
            with patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ):
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
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_vm,
        ):
            result = view.get_object(5, "virtualmachine")
        assert result is mock_vm

    def test_device_type_fetches_device(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView, Device

        view = _make_view(AddDeviceToLibreNMSView)
        mock_device = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_device,
        ) as mock_get:
            result = view.get_object(1, "device")
        assert result is mock_device
        mock_get.assert_called_once_with(Device, "change", pk=1)


class TestAddDeviceToLibreNMSViewPost:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        view.request = _make_request({"object_type": "device"})
        mock_obj = MagicMock()
        mock_error = MagicMock()
        # Perm check now runs after the object is resolved so the mixin can
        # build the right object-permission set (Device vs VirtualMachine).
        with patch.object(view, "get_object", return_value=mock_obj):
            with patch.object(view, "require_all_permissions", return_value=mock_error) as mock_perm:
                result = view.post(view.request, object_id=1)
        assert result is mock_error
        mock_perm.assert_called_once_with("POST")
        # Exactly one model in the dynamically-set required_object_permissions.
        from dcim.models import Device

        assert view.required_object_permissions == {"POST": [("change", Device)]}

    def test_invalid_object_type_returns_400_without_perm_check(self):
        """Bad object_type returns 400 immediately, before any permission check (no model to check yet)."""
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        view.request = _make_request({"object_type": "bogus"})
        with patch.object(view, "get_object", return_value=None):
            with patch.object(view, "require_all_permissions") as mock_perm:
                response = view.post(view.request, object_id=1)
        assert response.status_code == 400
        mock_perm.assert_not_called()

    def test_form_invalid_shows_error_and_redirects(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = _make_view(AddDeviceToLibreNMSView)
        view.request = _make_request({"v1v2-snmp_version": "v2c", "snmp_version": "v2c", "object_type": "device"})
        mock_obj = MagicMock()
        mock_obj.get_absolute_url.return_value = "/dcim/devices/1/"
        mock_form = MagicMock()
        mock_form.is_valid.return_value = False
        mock_form.errors.items.return_value = [("hostname", ["This field is required."])]
        with patch.object(view, "require_all_permissions", return_value=None):
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
        view.request = _make_request({"v1v2-snmp_version": "v2c", "object_type": "device"})
        mock_obj = MagicMock()
        mock_obj.get_absolute_url.return_value = "/dcim/devices/1/"
        mock_form = MagicMock()
        mock_form.is_valid.return_value = True
        mock_form.cleaned_data = {}
        with patch.object(view, "require_all_permissions", return_value=None):
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
            with patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ):
                with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
                    with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                        _post(view, view.request, pk=1)
        view._librenms_api.update_device_field.assert_called_once()
        mock_msg.success.assert_called_once()

    def test_without_site_shows_warning(self):
        view = self._make_view()
        view._librenms_api.get_librenms_id.return_value = 42
        device = MagicMock()
        device.site = None
        device.pk = 1
        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ):
                with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
                    with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                        _post(view, view.request, pk=1)
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
            with patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ):
                with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
                    with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                        _post(view, view.request, pk=1)
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

    @pytest.mark.django_db
    def test_vc_member_with_own_legacy_id_reads_sync_device_key(self):
        """The reader must resolve the VC sync device UNCONDITIONALLY like the writers: a viewed member's own legacy bare-int id must not shadow a sibling's explicit per-server mapping, else the sync POST reads a cache key the refresh never wrote and always fails with 'No cached data found'."""
        from dcim.models import VirtualChassis
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        # Viewed member holds a legacy bare-int id (a universal per-server fallback);
        # its sibling holds the explicit dict mapping that get_librenms_sync_device
        # prefers (priority 1 beats the legacy fallback 1b).
        viewed = make_device("vc-cache-viewed", librenms_cf=42)
        sibling = make_device("vc-cache-sibling", librenms_cf={"secondary": {"id": 99}})
        vc = VirtualChassis.objects.create(name="vc-cache-read")
        for pos, member in enumerate((viewed, sibling), start=1):
            member.virtual_chassis = vc
            member.vc_position = pos
            member.save()

        view = _make_view(SyncInterfacesView)
        ports = [{"ifName": "eth0"}]
        # BaseInterfaceTableView.post/get_context_data cache under the RESOLVED sync
        # device (the sibling) — seed exactly what the refresh writes.
        writer_key = view.get_cache_key(sibling, "ports", "secondary")
        cache.set(writer_key, {"ports": ports}, 60)
        try:
            with patch("netbox_librenms_plugin.views.sync.interfaces.messages"):
                result = view.get_cached_ports_data(view.request, viewed, "secondary")
        finally:
            cache.delete(writer_key)
        assert result == ports


class TestSyncInterfacesViewServerRebind:
    """SyncInterfacesView.post must rebind to the POSTed server_key, failing closed."""

    def test_stale_server_key_fails_closed_without_sync(self):
        """A POSTed key that no longer resolves redirects with an error — no lazy client rebuild, no sync."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._librenms_api = None  # no cached client; the posted key must resolve on its own
        req = _make_request({"server_key": "ghost", "select": ["eth0"]})
        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch.object(view, "get_object", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None),
            patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msg,
            patch.object(view, "sync_selected_interfaces") as mock_sync,
        ):
            view.request = req
            resp = view.post(req, "device", 1)
        mock_sync.assert_not_called()
        mock_msg.error.assert_called_once()
        assert resp.status_code == 302
        assert "server_key=" not in resp["Location"]

    def test_posted_server_key_is_bound_for_the_sync(self):
        """The POSTed key rebinds the client (not just a string pass-through), so cache reads and per-server id writes use the tab's server."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._librenms_api = None
        api = MagicMock()
        api.server_key = "secondary"
        req = _make_request({"server_key": "secondary", "select": ["eth0"]})
        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch.object(view, "get_object", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=api) as mock_build,
            patch.object(view, "get_cached_ports_data", return_value=None),
            patch("netbox_librenms_plugin.views.sync.interfaces.messages"),
        ):
            view.request = req
            resp = view.post(req, "device", 1)
        mock_build.assert_called_once_with("secondary")
        assert view._librenms_api is api
        assert view._post_server_key == "secondary"
        assert resp.status_code == 302
        assert "server_key=secondary" in resp["Location"]


@pytest.mark.django_db
class TestSyncInterfacesViewSyncInterface:
    """Real-DB tests for SyncInterfacesView.sync_interface target resolution."""

    def _patches(self, view):
        return (
            patch.object(view, "get_netbox_interface_type", return_value="other"),
            patch.object(view, "update_interface_attributes"),
            patch.object(view, "_sync_interface_vlans"),
        )

    def test_device_creates_interface_via_get_or_create(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._post_server_key = "default"
        view._lookup_maps = {}
        device = make_device("sync-iface-dev")
        view.request = _make_request()
        librenms_if = {"ifName": "eth0", "ifType": "ether", "ifSpeed": 1000000000}

        p_type, p_attrs, p_vlans = self._patches(view)
        with p_type, p_attrs, p_vlans:
            view.sync_interface(device, librenms_if, [], "ifName")

        assert device.interfaces.filter(name="eth0").exists()

    def test_vm_creates_vminterface_via_get_or_create(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._post_server_key = "default"
        view._lookup_maps = {}
        vm = make_vm("sync-iface-vm")
        view.request = _make_request()
        librenms_if = {"ifName": "eth0"}

        with patch.object(view, "update_interface_attributes"), patch.object(view, "_sync_interface_vlans"):
            view.sync_interface(vm, librenms_if, [], "ifName")

        assert vm.interfaces.filter(name="eth0").exists()

    def test_device_with_vc_member_selection_valid(self):
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._post_server_key = "default"
        view._lookup_maps = {}
        vc = VirtualChassis.objects.create(name="vc-sync")
        master = make_device("vc-sync-master")
        master.virtual_chassis = vc
        master.vc_position = 1
        master.save()
        member2 = make_device("vc-sync-member2")
        member2.virtual_chassis = vc
        member2.vc_position = 2
        member2.save()
        # Select member2 (a valid VC member) → the interface lands on member2, not the master.
        view.request = _make_request({"device_selection_10": str(member2.id)})
        librenms_if = {"ifName": "eth0", "port_id": 10}

        p_type, p_attrs, p_vlans = self._patches(view)
        with p_type, p_attrs, p_vlans:
            view.sync_interface(master, librenms_if, [], "ifName")

        assert member2.interfaces.filter(name="eth0").exists()
        assert not master.interfaces.filter(name="eth0").exists()

    def test_device_with_invalid_vc_member_is_skipped(self):
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._post_server_key = "default"
        view._lookup_maps = {}
        vc = VirtualChassis.objects.create(name="vc-sync-inv")
        master = make_device("vc-sync-inv-master")
        master.virtual_chassis = vc
        master.vc_position = 1
        master.save()
        # An existing device that is not a member of this VC is an invalid explicit target.
        outsider = make_device("vc-sync-outsider")
        view.request = _make_request({"device_selection_10": str(outsider.id)})
        librenms_if = {"ifName": "eth0", "port_id": 10}

        p_type, p_attrs, p_vlans = self._patches(view)
        with p_type, p_attrs, p_vlans:
            view.sync_interface(master, librenms_if, [], "ifName")

        assert not master.interfaces.filter(name="eth0").exists()
        assert not outsider.interfaces.filter(name="eth0").exists()

    def test_device_with_device_selection_wrong_device_is_skipped(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = _make_view(SyncInterfacesView)
        view._post_server_key = "default"
        view._lookup_maps = {}
        device = make_device("sync-iface-noVC")  # no virtual_chassis
        other = make_device("sync-iface-other")
        # A selection for another device is invalid when the page device has no chassis.
        view.request = _make_request({"device_selection_10": str(other.id)})
        librenms_if = {"ifName": "eth0", "port_id": 10}

        p_type, p_attrs, p_vlans = self._patches(view)
        with p_type, p_attrs, p_vlans:
            view.sync_interface(device, librenms_if, [], "ifName")

        assert not device.interfaces.filter(name="eth0").exists()
        assert not other.interfaces.filter(name="eth0").exists()

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


@pytest.mark.django_db
class TestDeleteNetBoxInterfacesViewPost:
    def _make_view(self, request=None):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        return make_view(DeleteNetBoxInterfacesView, request or make_real_request("post"))

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

    @pytest.mark.django_db
    def test_no_interface_ids_returns_400(self):
        obj = make_device("del-noids-dev")
        req = make_real_request("post", {"interface_ids": []})
        view = self._make_view(req)
        result = _post(view, req, object_type="device", object_id=obj.pk)
        assert result.status_code == 400

    @pytest.mark.django_db
    def test_device_interface_wrong_device_skipped(self):
        import json

        from dcim.models import Device, Interface

        obj = make_device("del-wrongdev-target")
        other = make_device("del-wrongdev-other")
        iface = make_interface(other, "eth0")  # belongs to a different device
        user = make_user_with_perms("del-wrongdev-user", [])
        user = grant(user, "view", Device, constraints={"pk": obj.pk})
        user = grant(user, "delete", Interface, constraints={"pk": iface.pk})
        req = make_real_request("post", {"interface_ids": [str(iface.pk)]}, user=user)
        view = self._make_view(req)
        result = _post(view, req, object_type="device", object_id=obj.pk)
        data = json.loads(result.content)
        assert data["deleted_count"] == 0
        assert data["errors"] == [f"Interface {iface.name} does not belong to this device"]
        assert other.interfaces.filter(pk=iface.pk).exists()  # not deleted

    @pytest.mark.django_db
    def test_vm_interface_wrong_vm_skipped(self):
        import json

        from virtualization.models import VirtualMachine, VMInterface

        vm = make_vm("del-wrongvm-target")
        other_vm = make_vm("del-wrongvm-other")
        vmiface = VMInterface.objects.create(virtual_machine=other_vm, name="eth0")
        user = make_user_with_perms("del-wrongvm-user", [])
        user = grant(user, "view", VirtualMachine, constraints={"pk": vm.pk})
        user = grant(user, "delete", VMInterface, constraints={"pk": vmiface.pk})
        req = make_real_request("post", {"interface_ids": [str(vmiface.pk)]}, user=user)
        view = self._make_view(req)
        result = _post(view, req, object_type="virtualmachine", object_id=vm.pk)
        data = json.loads(result.content)
        assert data["deleted_count"] == 0
        assert data["errors"] == [f"Interface {vmiface.name} does not belong to this virtual machine"]
        assert VMInterface.objects.filter(pk=vmiface.pk).exists()

    @pytest.mark.django_db
    def test_deletes_device_interface_successfully(self):
        import json

        from dcim.models import Interface

        obj = make_device("del-ok-dev")
        iface = make_interface(obj, "eth0")
        req = make_real_request("post", {"interface_ids": [str(iface.pk)]})
        view = self._make_view(req)
        result = _post(view, req, object_type="device", object_id=obj.pk)
        data = json.loads(result.content)
        assert data["deleted_count"] == 1
        assert not Interface.objects.filter(pk=iface.pk).exists()  # actually deleted

    @pytest.mark.django_db
    def test_deletes_vm_interface_successfully(self):
        import json

        from virtualization.models import VMInterface

        vm = make_vm("del-ok-vm")
        vmiface = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        req = make_real_request("post", {"interface_ids": [str(vmiface.pk)]})
        view = self._make_view(req)
        result = _post(view, req, object_type="virtualmachine", object_id=vm.pk)
        data = json.loads(result.content)
        assert data["deleted_count"] == 1
        assert not VMInterface.objects.filter(pk=vmiface.pk).exists()

    @pytest.mark.django_db
    def test_interface_not_found_adds_error(self):
        import json

        from dcim.models import Interface

        obj = make_device("del-notfound-dev")
        absent_pk = missing_pk(Interface)
        req = make_real_request("post", {"interface_ids": [str(absent_pk)]})
        view = self._make_view(req)
        result = _post(view, req, object_type="device", object_id=obj.pk)
        data = json.loads(result.content)
        assert "errors" in data
        assert data["deleted_count"] == 0

    @pytest.mark.django_db
    def test_device_with_vc_validates_members(self):
        import json

        from dcim.models import Interface, VirtualChassis

        vc = VirtualChassis.objects.create(name="vc-del")
        member = make_device("del-vc-member")
        member.virtual_chassis = vc
        member.vc_position = 1
        member.save()
        outsider = make_device("del-vc-outsider")  # not part of the VC
        iface = make_interface(outsider, "eth0")
        req = make_real_request("post", {"interface_ids": [str(iface.pk)]})
        view = self._make_view(req)
        result = _post(view, req, object_type="device", object_id=member.pk)
        data = json.loads(result.content)
        assert data["deleted_count"] == 0
        assert data["errors"] == [f"Interface {iface.name} does not belong to this device or its virtual chassis"]
        assert Interface.objects.filter(pk=iface.pk).exists()  # not deleted (not a VC member's)


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


class TestSyncIPAddressesViewGetManagementIp:
    def test_non_string_ip_returns_none(self):
        """A non-string ip in the device_info payload yields None via an explicit type guard (like _resolve_management_ip), not by raising AttributeError into the broad except."""
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        view._librenms_api = MagicMock()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {"ip": 167772163})  # int, not str

        assert view.get_management_ip(MagicMock()) is None

    def test_string_ip_is_returned_stripped(self):
        """A normal string ip is returned (whitespace-stripped)."""
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        view._librenms_api = MagicMock()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {"ip": "  10.0.0.5  "})

        assert view.get_management_ip(MagicMock()) == "10.0.0.5"


class TestSyncIPAddressesViewGetObject:
    def test_device_type_returns_device(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        mock_dev = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_dev,
        ):
            result = view.get_object("device", 1)
        assert result is mock_dev

    def test_vm_type_returns_vm(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        mock_vm = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
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
        # Without a POSTed key the redirect still carries the bound client's server, which the
        # librenms_api property returns without rebuilding it.
        assert "server_key=default" in url

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

    def test_unbound_api_misconfigured_default_degrades_without_500(self):
        """On the failed-rebind redirect path _librenms_api is unbound, so get_ip_tab_url resolves the active/default server via the librenms_api property (the redirect must carry the resolved server_key — see test_unknown_server_key_errors_without_500)."""
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        view._librenms_api = None  # failed rebind left it unbound
        # _post_server_key intentionally unset — the redirect happens before it's assigned.
        mock_obj = MagicMock()
        mock_obj.__class__ = Device
        mock_obj.pk = 7
        with (
            # The property constructs the default client → a misconfigured default raises.
            patch("netbox_librenms_plugin.views.mixins.LibreNMSAPI", side_effect=KeyError("ghost")),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/fake/"),
        ):
            url = view.get_ip_tab_url(mock_obj)  # must not raise
        assert "tab=ipaddresses" in url
        assert "server_key" not in url


@pytest.mark.django_db
class TestSyncIPAddressesViewProcessIpSync:
    """Real-DB tests for SyncIPAddressesView.process_ip_sync."""

    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        view._post_server_key = "default"
        return view

    @staticmethod
    def _seed_lib_id(iface, value):
        iface.custom_field_data["librenms_id"] = {"default": value}
        iface.save()

    def _run(self, view, selected, cached, obj, object_type):
        """Drive process_ip_sync with the primary-IP toggle off and no VRF selection."""
        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.resolve_set_primary_ip", return_value=False),
            patch.object(view, "get_vrf_selection", return_value=None),
        ):
            return view.process_ip_sync(view.request, selected, cached, obj, object_type)

    def test_creates_new_ip_address(self):
        from ipam.models import IPAddress

        view = self._setup_view()
        obj = make_device("ipsync-create-dev")
        iface = make_interface(obj, "eth0")
        self._seed_lib_id(iface, 5)
        # No interface_name → match can ONLY succeed via port_id (a name-fallback regression
        # can't mask a port-id break).
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 5}]

        results = self._run(view, ["10.0.0.1/24"], cached, obj, "device")

        assert "10.0.0.1/24" in results["created"]
        created = IPAddress.objects.get(address="10.0.0.1/24")
        assert created.assigned_object == iface

    def test_existing_ip_address_on_another_interface_requires_confirmation(self):
        view = self._setup_view()
        obj = make_device("ipsync-update-dev")
        iface = make_interface(obj, "eth0")
        self._seed_lib_id(iface, 5)
        other = make_interface(obj, "eth1")
        existing = make_ip("10.0.0.1/24", assigned_object=other)  # currently on a different interface
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 5}]

        results = self._run(view, ["10.0.0.1/24"], cached, obj, "device")

        assert results["updated"] == []
        assert results["conflicts"][0]["row_id"] == "10.0.0.1/24"
        existing.refresh_from_db()
        assert existing.assigned_object == other

    def test_unchanged_ip_address_skipped(self):
        view = self._setup_view()
        obj = make_device("ipsync-unchanged-dev")
        iface = make_interface(obj, "eth0")
        self._seed_lib_id(iface, 5)
        make_ip("10.0.0.1/24", assigned_object=iface)  # already on the matched interface
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 5}]

        results = self._run(view, ["10.0.0.1/24"], cached, obj, "device")

        assert "10.0.0.1/24" in results["unchanged"]

    def test_no_matching_interface_skips_without_writing(self):
        """No NetBox interface matches the row → skip (no create/update)."""
        from ipam.models import IPAddress

        view = self._setup_view()
        obj = make_device("ipsync-nomatch-dev")  # no interfaces
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "interface_url": None}]

        results = self._run(view, ["10.0.0.1/24"], cached, obj, "device")

        assert "10.0.0.1/24" in results["skipped_no_interface"]
        assert "10.0.0.1/24" not in results["created"]
        assert not IPAddress.objects.filter(address="10.0.0.1/24").exists()

    def test_renamed_interface_resolved_via_cached_url_pk(self):
        """A renamed interface with no port_id and a stale cached name still resolves via the cached interface_url PK."""
        from ipam.models import IPAddress

        view = self._setup_view()
        obj = make_device("ipsync-renamed-dev")
        # Current NetBox name differs from the LibreNMS-cached name ("eth0"); no librenms_id
        # seeded, so the row has no usable port_id either — only interface_url can resolve it.
        iface = make_interface(obj, "eth0-renamed")
        cached = [
            {
                "ip_address": "10.0.0.1",
                "ip_with_mask": "10.0.0.1/24",
                "port_id": None,
                "interface_name": "eth0",  # stale LibreNMS name — no longer matches
                "interface_url": iface.get_absolute_url(),  # PK survives the rename
            }
        ]

        results = self._run(view, ["10.0.0.1/24"], cached, obj, "device")

        assert "10.0.0.1/24" in results["created"], results
        created = IPAddress.objects.get(address="10.0.0.1/24")
        assert created.assigned_object == iface

    def test_existing_bound_ip_not_unbound_when_no_interface(self):
        """Regression for the data-loss case: when no NetBox interface resolves, an IP that is ALREADY bound must keep its binding."""
        view = self._setup_view()
        obj = make_device("ipsync-bound-nomatch-dev")
        bound_owner = make_device("ipsync-bound-owner-dev")
        bound_iface = make_interface(bound_owner, "eth9")
        existing_ip = make_ip("10.0.0.1/24", assigned_object=bound_iface)
        # No port_id/interface_name → _match_interface returns None → row skipped before update.
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "interface_url": None}]

        results = self._run(view, ["10.0.0.1/24"], cached, obj, "device")

        assert "10.0.0.1/24" in results["skipped_no_interface"]
        existing_ip.refresh_from_db()
        # The existing binding must be untouched — not nulled.
        assert existing_ip.assigned_object == bound_iface

    def test_ip_assigned_to_interface_matched_by_port_id(self):
        from ipam.models import IPAddress

        view = self._setup_view()
        obj = make_device("ipsync-byport-dev")
        iface = make_interface(obj, "eth0")
        self._seed_lib_id(iface, 5)
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 5, "interface_name": "eth0"}]

        self._run(view, ["10.0.0.1/24"], cached, obj, "device")

        assert IPAddress.objects.get(address="10.0.0.1/24").assigned_object == iface

    def test_ip_assigned_to_interface_matched_by_name(self):
        from ipam.models import IPAddress
        from virtualization.models import VMInterface

        view = self._setup_view()
        vm = make_vm("ipsync-byname-vm")
        vmiface = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        # port_id 7 matches nothing (no CF on the VM interface) → falls back to the name "eth0".
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 7, "interface_name": "eth0"}]

        self._run(view, ["10.0.0.1/24"], cached, vm, "virtualmachine")

        assert IPAddress.objects.get(address="10.0.0.1/24").assigned_object == vmiface

    def test_ambiguous_port_id_binds_by_name(self):
        """Two interfaces share a port id (id ambiguous), but the row names one uniquely → it binds by name.

        This is what the rendered IP table shows (its render drops the ambiguous id and links by name),
        so the sync must agree and NOT drop the row into skipped_no_interface. by_name is fail-closed
        (obj's own interface wins), so the fall-through binds only to the uniquely-named interface.
        """
        from ipam.models import IPAddress

        view = self._setup_view()
        obj = make_device("ipsync-ambport-dev")
        a = make_interface(obj, "eth0")
        b = make_interface(obj, "eth1")
        self._seed_lib_id(a, 5)
        self._seed_lib_id(b, 5)  # both share port id 5 → ambiguous
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 5, "interface_name": "eth0"}]

        results = self._run(view, ["10.0.0.1/24"], cached, obj, "device")

        assert IPAddress.objects.get(address="10.0.0.1/24").assigned_object == a
        assert "10.0.0.1/24" not in results["skipped_no_interface"]

    def test_ambiguous_port_id_and_unresolvable_name_still_skips(self):
        """When the port id is ambiguous AND the row's name matches no interface, it still fails closed (skip, no bind)."""
        from ipam.models import IPAddress

        view = self._setup_view()
        obj = make_device("ipsync-ambport-noname")
        a = make_interface(obj, "eth0")
        b = make_interface(obj, "eth1")
        self._seed_lib_id(a, 5)
        self._seed_lib_id(b, 5)  # both share port id 5 → ambiguous
        # interface_name "eth9" resolves nowhere, and interface_url is absent → nothing to fall through to.
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 5, "interface_name": "eth9"}]

        results = self._run(view, ["10.0.0.1/24"], cached, obj, "device")

        assert "10.0.0.1/24" in results["skipped_no_interface"]
        assert not IPAddress.objects.filter(address="10.0.0.1/24").exists()

    def test_shared_vc_interface_name_binds_to_viewed_member(self):
        """A name shared across VC members (no stored port id) binds to the VIEWED member's own interface, matching the rendered table."""
        from dcim.models import VirtualChassis
        from ipam.models import IPAddress

        view = self._setup_view()
        vc = VirtualChassis.objects.create(name="vc-ipsync-shared")
        m1 = make_device("ipsync-shared-m1")
        m1.virtual_chassis = vc
        m1.vc_position = 1
        m1.save()
        m2 = make_device("ipsync-shared-m2")
        m2.virtual_chassis = vc
        m2.vc_position = 2
        m2.save()
        m1_eth0 = make_interface(m1, "eth0")
        make_interface(m2, "eth0")  # sibling reuses the name — must NOT block binding to m1's own
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "interface_name": "eth0"}]

        results = self._run(view, ["10.0.0.1/24"], cached, m1, "device")

        # The render (obj-only) shows this IP linked to m1's eth0; the sync must agree, not skip.
        assert "10.0.0.1/24" in results["created"]
        assert IPAddress.objects.get(address="10.0.0.1/24").assigned_object == m1_eth0

    def test_sibling_only_name_collision_stays_ambiguous(self):
        """A name the viewed member doesn't own, shared by two siblings, is genuinely ambiguous → skip."""
        from dcim.models import VirtualChassis
        from ipam.models import IPAddress

        view = self._setup_view()
        vc = VirtualChassis.objects.create(name="vc-ipsync-sibamb")
        m1 = make_device("ipsync-sibamb-m1")
        m1.virtual_chassis = vc
        m1.vc_position = 1
        m1.save()
        m2 = make_device("ipsync-sibamb-m2")
        m2.virtual_chassis = vc
        m2.vc_position = 2
        m2.save()
        m3 = make_device("ipsync-sibamb-m3")
        m3.virtual_chassis = vc
        m3.vc_position = 3
        m3.save()
        # m1 (the viewed member) has NO eth9; two siblings share it → can't pick one → skip.
        make_interface(m2, "eth9")
        make_interface(m3, "eth9")
        cached = [{"ip_address": "10.0.0.9", "ip_with_mask": "10.0.0.9/24", "interface_name": "eth9"}]

        results = self._run(view, ["10.0.0.9/24"], cached, m1, "device")

        assert "10.0.0.9/24" in results["skipped_no_interface"]
        assert not IPAddress.objects.filter(address="10.0.0.9/24").exists()


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


@pytest.mark.django_db
class TestSyncSiteLocationViewGetSiteByPk:
    def _make_view(self, request=None):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        return make_view(SyncSiteLocationView, request)

    def test_found_returns_site(self):
        from dcim.models import Site

        site = Site.objects.create(name="Bypk Site", slug="bypk-site")

        assert self._make_view().get_site_by_pk(site.pk) == site

    def test_not_found_returns_none(self):
        from dcim.models import Site

        absent_pk = missing_pk(Site)

        assert self._make_view().get_site_by_pk(absent_pk) is None


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
                    view.request = req
                    view.post(req)
        mock_msg.error.assert_called_once()

    def test_site_not_found_shows_error(self):
        view = self._make_view()
        req = _make_request({"pk": "5", "action": "create"})
        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(view, "get_site_by_pk", return_value=None):
                with patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msg:
                    with patch("netbox_librenms_plugin.views.sync.locations.redirect"):
                        view.request = req
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
                        view.request = req
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
                    view.request = req
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
                    view.request = req
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
        from dcim.models import Device
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        perms = SyncVLANsView.required_object_permissions["POST"]
        assert ("view", Device) in perms
        assert ("add", VLAN) in perms
        assert ("change", VLAN) in perms
        assert ("view", VLANGroup) not in perms


class TestSyncVLANsViewGetObject:
    def test_device_type_returns_device(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = _make_view(SyncVLANsView)
        mock_dev = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
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
                        view.request = req
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
                    view.request = req
                    result = view.post(req, object_type="device", object_id=1)
        mock_handle.assert_called_once()
        assert result is mock_response

    @pytest.mark.django_db
    def test_global_vlan_sync_does_not_require_vlan_group_permission(self):
        """A request that selects no group must not require access to VLANGroup."""
        from dcim.models import Device
        from ipam.models import VLAN

        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        device = make_device("vlan-global-perm")
        user = make_user_with_perms(
            "vlan-global-perm",
            [("view", Device), ("add", VLAN), ("change", VLAN)],
        )
        request = make_real_request(
            "post",
            {"action": "create_vlans", "server_key": "default", "select": ["100"], "vlan_group_100": ""},
            user=user,
        )
        view = make_view(SyncVLANsView, request)

        response = _post(view, request, object_type="device", object_id=device.pk)

        assert response.status_code == 302
        assert message_texts(request, "error") == ["No cached VLAN data. Please refresh VLANs first."]

    @pytest.mark.django_db
    def test_grouped_vlan_sync_still_requires_vlan_group_permission(self):
        """A non-empty per-row group selection must keep the VLANGroup permission gate."""
        from dcim.models import Device
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        device = make_device("vlan-grouped-perm")
        group = VLANGroup.objects.create(name="Group permission", slug="group-permission")
        user = make_user_with_perms(
            "vlan-grouped-perm",
            [("view", Device), ("add", VLAN), ("change", VLAN)],
        )
        request = make_real_request(
            "post",
            {
                "action": "create_vlans",
                "server_key": "default",
                "select": ["100"],
                "vlan_group_100": str(group.pk),
            },
            user=user,
        )
        view = make_view(SyncVLANsView, request)

        response = _post(view, request, object_type="device", object_id=device.pk)

        assert response.status_code == 302
        assert any("ipam.view_vlangroup" in text for text in message_texts(request, "error"))

    @pytest.mark.django_db
    def test_grouped_vlan_permission_uses_canonical_vid_field_name(self):
        """A leading-zero selection must not hide its canonical group field from the gate."""
        from dcim.models import Device
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        device = make_device("vlan-grouped-canonical-perm")
        group = VLANGroup.objects.create(name="Canonical group permission", slug="canonical-group-permission")
        user = make_user_with_perms(
            "vlan-grouped-canonical-perm",
            [("view", Device), ("add", VLAN), ("change", VLAN)],
        )
        request = make_real_request(
            "post",
            {
                "action": "create_vlans",
                "server_key": "default",
                "select": ["0100"],
                "vlan_group_100": str(group.pk),
            },
            user=user,
        )
        view = make_view(SyncVLANsView, request)

        response = _post(view, request, object_type="device", object_id=device.pk)

        assert response.status_code == 302
        assert any("ipam.view_vlangroup" in text for text in message_texts(request, "error"))


@pytest.mark.django_db
class TestSyncVLANsViewHandleCreateVlans:
    """Real-DB tests for SyncVLANsView._handle_create_vlans."""

    def _make_view(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._post_server_key = "default"
        view.request = _make_request()
        return view

    def _run(self, view, req, obj, cached_vlans):
        """Drive _handle_create_vlans with the LibreNMS VLAN snapshot served from cache."""
        with (
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="key"),
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msg,
            patch.object(view, "_redirect", return_value=MagicMock()),
        ):
            mock_cache.get.return_value = cached_vlans
            view._handle_create_vlans(req, obj, "device", 1)
        return mock_msg

    def test_no_selected_vlans_shows_error(self):
        from ipam.models import VLAN

        view = self._make_view()
        obj = make_device("vlan-dev-none")
        with (
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msg,
            patch.object(view, "_redirect", return_value=MagicMock()),
        ):
            view._handle_create_vlans(_make_request({"select": []}), obj, "device", 1)
        mock_msg.error.assert_called_once()
        assert VLAN.objects.count() == 0

    def test_cache_miss_shows_error(self):
        from ipam.models import VLAN

        view = self._make_view()
        obj = make_device("vlan-dev-cachemiss")
        mock_msg = self._run(view, _make_request({"select": ["10"]}), obj, None)
        mock_msg.error.assert_called_once()
        assert VLAN.objects.count() == 0

    def test_invalid_vid_string_skipped(self):
        from ipam.models import VLAN

        view = self._make_view()
        obj = make_device("vlan-dev-badvid")
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        mock_msg = self._run(view, _make_request({"select": ["not-a-number"]}), obj, cached_vlans)
        assert VLAN.objects.count() == 0  # non-numeric vid never reaches get_or_create
        mock_msg.warning.assert_called_once()

    def test_vid_not_in_librenms_data_skipped(self):
        from ipam.models import VLAN

        view = self._make_view()
        obj = make_device("vlan-dev-novid")
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        mock_msg = self._run(view, _make_request({"select": ["99"]}), obj, cached_vlans)
        assert VLAN.objects.count() == 0  # vid 99 not present in the LibreNMS snapshot
        mock_msg.warning.assert_called_once()

    def test_creates_vlan_with_group(self):
        from ipam.models import VLAN, VLANGroup

        view = self._make_view()
        obj = make_device("vlan-dev-group")
        group = VLANGroup.objects.create(name="Corp", slug="corp")
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        req = _make_request({"select": ["10"], "vlan_group_10": str(group.pk)})

        self._run(view, req, obj, cached_vlans)

        vlan = VLAN.objects.get(vid=10, group=group)
        assert vlan.name == "Management"
        assert vlan.status == "active"

    def test_creates_vlan_global_no_group(self):
        from ipam.models import VLAN

        view = self._make_view()
        obj = make_device("vlan-dev-global")
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]

        self._run(view, _make_request({"select": ["10"]}), obj, cached_vlans)

        vlan = VLAN.objects.get(vid=10, group__isnull=True)
        assert vlan.name == "Management"

    def test_updates_vlan_name_when_changed(self):
        from ipam.models import VLAN

        view = self._make_view()
        obj = make_device("vlan-dev-update")
        VLAN.objects.create(vid=10, name="OldName", status="active")
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "NewName"}]

        mock_msg = self._run(view, _make_request({"select": ["10"]}), obj, cached_vlans)

        assert VLAN.objects.get(vid=10, group__isnull=True).name == "NewName"
        assert "updated" in mock_msg.success.call_args[0][1]

    def test_skips_unchanged_vlan(self):
        from ipam.models import VLAN

        view = self._make_view()
        obj = make_device("vlan-dev-skip")
        VLAN.objects.create(vid=10, name="Management", status="active")
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]

        mock_msg = self._run(view, _make_request({"select": ["10"]}), obj, cached_vlans)

        # No duplicate row, name preserved, summary reports "unchanged".
        assert VLAN.objects.filter(vid=10, group__isnull=True).count() == 1
        assert VLAN.objects.get(vid=10, group__isnull=True).name == "Management"
        assert "unchanged" in mock_msg.success.call_args[0][1]

    @pytest.mark.django_db
    def test_invalid_group_id_is_rejected(self):
        """A stale/tampered vlan_group_{vid} pointing at a missing group fails closed: the VID is skipped with an error, not persisted as a global VLAN in the wrong scope."""
        from ipam.models import VLAN

        view = self._make_view()
        # Group 999999 does not exist → must NOT silently fall back to a global VLAN.
        req = _make_request({"select": ["10"], "vlan_group_10": "999999"})
        mock_obj = MagicMock()
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache:
            mock_cache.get.return_value = cached_vlans
            with patch.object(view, "get_cache_key", return_value="key"):
                with patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msg:
                    with patch.object(view, "_redirect", return_value=MagicMock()):
                        view._handle_create_vlans(req, mock_obj, "device", 1)
        # Real VLAN model + real VLANGroup lookup: nothing created in any scope, error surfaced.
        assert not VLAN.objects.filter(vid=10).exists()
        mock_msg.error.assert_called()

    def test_summary_message_shows_counts(self):
        view = self._make_view()
        obj = make_device("vlan-dev-summary")
        cached_vlans = [{"vlan_vlan": 10, "vlan_name": "Management"}]

        mock_msg = self._run(view, _make_request({"select": ["10"]}), obj, cached_vlans)

        mock_msg.success.assert_called_once()
        assert "created" in mock_msg.success.call_args[0][1]

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


@pytest.mark.django_db
class TestSyncIPAddressesViewSetPrimaryIp:
    """Phase 1: auto-match the LibreNMS management IP and set it as Primary IP."""

    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = _make_view(SyncIPAddressesView)
        view._post_server_key = "default"
        return view

    def test_same_host(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView as V

        assert V._same_host("10.0.0.1", "10.0.0.1") is True
        assert V._same_host("10.0.0.1", "10.0.0.2") is False
        assert V._same_host("not-an-ip", "10.0.0.1") is False
        # IPv6 equality across differing textual forms
        assert V._same_host("2001:db8::1", "2001:0db8:0000:0000:0000:0000:0000:0001") is True

    @pytest.mark.django_db
    def test_set_primary_ip_sets_ipv4_and_is_idempotent(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView as V

        obj = make_device("setprimary-v4")
        iface = make_interface(obj, "eth0")
        ip_obj = make_ip("10.0.0.1/24", assigned_object=iface)

        assert V._set_primary_ip(obj, ip_obj) is True
        obj.refresh_from_db()
        assert obj.primary_ip4_id == ip_obj.pk

        # Already pointing at this IP -> no change, no DB write.
        assert V._set_primary_ip(obj, ip_obj) is False

    @pytest.mark.django_db
    def test_set_primary_ip_uses_v6_field(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView as V

        obj = make_device("setprimary-v6")
        iface = make_interface(obj, "eth0")
        ip_obj = make_ip("2001:db8::1/64", assigned_object=iface)

        assert V._set_primary_ip(obj, ip_obj) is True
        obj.refresh_from_db()
        assert obj.primary_ip6_id == ip_obj.pk

    @pytest.mark.django_db
    def test_set_primary_ip_survives_netbox44_family_property(self):
        """_set_primary_ip must not read IPAddress.family: on NetBox 4.4 the property raises AttributeError on the in-memory str address of a freshly created IPAddress (forced)."""
        from ipam.models import IPAddress

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView as V

        obj = make_device("setprimary-nb44")
        iface = make_interface(obj, "eth0")
        ip_obj = make_ip("2001:db8::44/64", assigned_object=iface)
        ip_obj.address = "2001:db8::44/64"  # in-memory str, as after IPAddress.objects.create()
        # NetBox 4.4's family property verbatim — 4.5+ added a str-tolerant branch.
        netbox44_family = property(lambda self: self.address.version if self.address else None)
        with patch.object(IPAddress, "family", netbox44_family):
            assert V._set_primary_ip(obj, ip_obj) is True
        obj.refresh_from_db()
        assert obj.primary_ip6_id == ip_obj.pk

    def _run_process(self, view, cached, *, mgmt_ip, set_primary=True):
        """Drive process_ip_sync against a REAL Device + interface so _build_interface_maps() takes the production Device branch."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        selected = ["10.0.0.1/24"]
        obj = make_device("ipsync-setprimary-dev")
        make_interface(obj, "eth0")  # matched by LibreNMS port id (5, patched) or name ("eth0")
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.resolve_set_primary_ip", return_value=set_primary):
            with patch.object(view, "get_management_ip", return_value=mgmt_ip) as mock_mgmt:
                with patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction", _atomic_txn()):
                    with patch(
                        "netbox_librenms_plugin.views.sync.ip_addresses.get_librenms_device_id",
                        return_value=5,
                    ):
                        with patch.object(view, "get_vrf_selection", return_value=None):
                            results = view.process_ip_sync(view.request, selected, cached, obj, "device")
        obj.refresh_from_db()
        return results, obj, mock_mgmt

    def test_primary_set_when_matched_and_interface_assigned(self):
        view = self._setup_view()
        # No interface_name → match can ONLY succeed via port_id, so a port-id regression
        # can't be masked by the name fallback.
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 5}]
        results, obj, _ = self._run_process(view, cached, mgmt_ip="10.0.0.1")
        assert results["primary_set"] == ["10.0.0.1/24"]
        # The real device now points its primary_ip4 at the (real) created address.
        assert obj.primary_ip4_id is not None
        assert str(obj.primary_ip4.address).startswith("10.0.0.1")

    def test_primary_skipped_when_no_interface(self):
        view = self._setup_view()
        # No port_id / interface_name match -> interface cannot be resolved.
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "interface_name": None}]
        results, obj, _ = self._run_process(view, cached, mgmt_ip="10.0.0.1")
        assert results["primary_set"] == []
        assert results["primary_no_interface"] == ["10.0.0.1/24"]
        assert obj.primary_ip4_id is None  # nothing persisted as primary

    def test_primary_skipped_when_ip_does_not_match_mgmt(self):
        view = self._setup_view()
        # No interface_name → match can ONLY succeed via port_id, so a port-id regression
        # can't be masked by the name fallback.
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 5}]
        results, obj, _ = self._run_process(view, cached, mgmt_ip="10.9.9.9")
        assert results["primary_set"] == []
        assert obj.primary_ip4_id is None

    def test_toggle_off_skips_mgmt_lookup_and_primary(self):
        view = self._setup_view()
        # No interface_name → match can ONLY succeed via port_id, so a port-id regression
        # can't be masked by the name fallback.
        cached = [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 5}]
        results, obj, mock_mgmt = self._run_process(view, cached, mgmt_ip="10.0.0.1", set_primary=False)
        assert results["primary_set"] == []
        mock_mgmt.assert_not_called()
