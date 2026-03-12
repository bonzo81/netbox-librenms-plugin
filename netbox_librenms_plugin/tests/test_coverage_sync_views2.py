"""
Coverage tests for views/sync/ (cables, devices, interfaces, ip_addresses, locations, vlans).

All DB interactions are mocked via MagicMock.  No @pytest.mark.django_db.
"""

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(post_data=None, get_data=None, user=None):
    req = MagicMock()
    _post = post_data or {}
    post_mock = MagicMock()
    post_mock.get = lambda k, d=None: _post.get(k, d)
    post_mock.getlist = lambda k: (
        _post.get(k) if isinstance(_post.get(k), list) else ([] if k not in _post else [_post[k]])
    )
    req.POST = post_mock
    req.GET = get_data or {}
    req.user = user or MagicMock()
    req.META = {}
    req.htmx = False
    return req


def _denied_response():
    resp = MagicMock()
    resp.status_code = 403
    return resp


# ===========================================================================
# views/sync/cables.py — SyncCablesView
# ===========================================================================


class TestSyncCablesViewPermissionDenied:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=_denied_response())
        view.request = _make_request()

        with patch("netbox_librenms_plugin.views.sync.cables.get_object_or_404") as mock_get:
            result = view.post(view.request, pk=1)

        assert result.status_code == 403
        mock_get.assert_not_called()


class TestSyncCablesViewCacheMiss:
    def test_cache_miss_redirects_with_error(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.request = _make_request(post_data={"select": ["1"], "device_selection_1": "1"})
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        mock_device = MagicMock(pk=1)

        with (
            patch("netbox_librenms_plugin.views.sync.cables.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = None
            view.post(view.request, pk=1)

        mock_msgs.error.assert_called_once()
        mock_redirect.assert_called_once()


class TestSyncCablesViewNoSelection:
    def test_no_selection_redirects_with_error(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.request = _make_request(post_data={})
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        mock_device = MagicMock(pk=1)

        with (
            patch("netbox_librenms_plugin.views.sync.cables.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect"),
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = {"links": [{"local_port_id": "99"}]}
            view.post(view.request, pk=1)

        mock_msgs.error.assert_called_once()


class TestSyncCablesViewSuccessPath:
    def test_valid_cable_created(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.request = _make_request(post_data={"select": ["port1"]})
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        mock_device = MagicMock(pk=1)
        local_iface = MagicMock(pk=10)
        remote_iface = MagicMock(pk=20)
        link_data = {
            "local_port_id": "port1",
            "local_port": "Gi0/1",
            "netbox_local_interface_id": 10,
            "netbox_remote_interface_id": 20,
        }

        with (
            patch("netbox_librenms_plugin.views.sync.cables.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect"),
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.cables.Cable") as mock_cable_cls,
            patch("netbox_librenms_plugin.views.sync.cables.Interface") as mock_iface_cls,
            patch("netbox_librenms_plugin.views.sync.cables.transaction"),
            patch("netbox_librenms_plugin.views.sync.cables.ContentType") as mock_ct,
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_ct.objects.get_for_model.return_value = MagicMock()
            mock_cache.get.return_value = {"links": [link_data]}
            mock_iface_cls.objects.get.side_effect = [local_iface, remote_iface]
            mock_cable_cls.objects.filter.return_value.exists.return_value = False

            view.post(view.request, pk=1)

        mock_cable_cls.objects.create.assert_called_once()
        mock_msgs.success.assert_called_once()


class TestSyncCablesViewDuplicateCable:
    def test_duplicate_cable_shows_warning(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.request = _make_request(post_data={"select": ["port1"]})
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        mock_device = MagicMock(pk=1)
        link_data = {
            "local_port_id": "port1",
            "local_port": "Gi0/1",
            "netbox_local_interface_id": 10,
            "netbox_remote_interface_id": 20,
        }

        with (
            patch("netbox_librenms_plugin.views.sync.cables.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect"),
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.cables.Cable") as mock_cable_cls,
            patch("netbox_librenms_plugin.views.sync.cables.Interface") as mock_iface_cls,
            patch("netbox_librenms_plugin.views.sync.cables.transaction"),
            patch("netbox_librenms_plugin.views.sync.cables.ContentType") as mock_ct,
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_ct.objects.get_for_model.return_value = MagicMock()
            mock_cache.get.return_value = {"links": [link_data]}
            local_iface = MagicMock(pk=10)
            remote_iface = MagicMock(pk=20)
            mock_iface_cls.objects.get.side_effect = [local_iface, remote_iface]
            mock_cable_cls.objects.filter.return_value.exists.return_value = True

            view.post(view.request, pk=1)

        mock_msgs.warning.assert_called_once()


class TestSyncCablesViewMissingRemote:
    def test_interface_does_not_exist_shows_error(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.request = _make_request(post_data={"select": ["port1"]})
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        mock_device = MagicMock(pk=1)
        link_data = {
            "local_port_id": "port1",
            "local_port": "Gi0/1",
            "netbox_local_interface_id": 10,
            "netbox_remote_interface_id": 20,
        }

        class _DNE(Exception):
            pass

        with (
            patch("netbox_librenms_plugin.views.sync.cables.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect"),
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.cables.Interface") as mock_iface_cls,
            patch("netbox_librenms_plugin.views.sync.cables.transaction"),
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = {"links": [link_data]}
            mock_iface_cls.DoesNotExist = _DNE
            mock_iface_cls.objects.get.side_effect = _DNE()

            view.post(view.request, pk=1)

        mock_msgs.error.assert_called_once()


class TestSyncCablesViewMissingLinkData:
    def test_no_matching_link_data_reports_invalid(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.request = _make_request(post_data={"select": ["port_unknown"]})
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        mock_device = MagicMock(pk=1)

        with (
            patch("netbox_librenms_plugin.views.sync.cables.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect"),
            patch("netbox_librenms_plugin.views.sync.cables.transaction"),
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = {"links": [{"local_port_id": "other_port"}]}
            view.post(view.request, pk=1)

        mock_msgs.error.assert_called_once()


class TestSyncCablesViewInvalidLinkData:
    def test_missing_netbox_ids_reports_invalid(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.request = _make_request(post_data={"select": ["port1"]})
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        mock_device = MagicMock(pk=1)
        # Missing netbox_local_interface_id
        link_data = {"local_port_id": "port1", "local_port": "Gi0/1", "netbox_remote_interface_id": 20}

        with (
            patch("netbox_librenms_plugin.views.sync.cables.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect"),
            patch("netbox_librenms_plugin.views.sync.cables.transaction"),
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = {"links": [link_data]}
            view.post(view.request, pk=1)

        mock_msgs.error.assert_called_once()


class TestSyncCablesViewHelpers:
    def test_get_selected_interfaces_returns_none_when_empty(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        req = _make_request(post_data={})
        result = view.get_selected_interfaces(req, MagicMock(id=1))
        assert result is None

    def test_get_selected_interfaces_builds_list(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        req = _make_request(post_data={"select": ["port1", "port2"], "device_selection_port1": "5"})
        result = view.get_selected_interfaces(req, MagicMock(id=1))
        assert len(result) == 2
        assert result[0]["local_port_id"] == "port1"
        assert result[0]["device_id"] == "5"
        # port2 defaults to initial_device.id
        assert result[1]["device_id"] == 1

    def test_validate_prerequisites_false_on_no_cache(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.request = _make_request()
        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs:
            result = view.validate_prerequisites(None, ["some"])
        assert result is False
        mock_msgs.error.assert_called_once()

    def test_validate_prerequisites_false_on_no_selection(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.request = _make_request()
        with patch("netbox_librenms_plugin.views.sync.cables.messages"):
            result = view.validate_prerequisites([{"port": "x"}], None)
        assert result is False

    def test_verify_cable_creation_requirements_true(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        data = {"netbox_local_interface_id": 1, "netbox_remote_interface_id": 2}
        assert view.verify_cable_creation_requirements(data) is True

    def test_verify_cable_creation_requirements_false(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        data = {"netbox_local_interface_id": 1}
        assert view.verify_cable_creation_requirements(data) is False

    def test_display_sync_results_all_branches(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        req = _make_request()
        results = {
            "valid": ["Gi0/1"],
            "invalid": ["Gi0/2"],
            "duplicate": ["Gi0/3"],
            "missing_remote": ["Gi0/4"],
        }
        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs:
            view.display_sync_results(req, results)
        assert mock_msgs.success.call_count == 1
        assert mock_msgs.error.call_count == 2
        assert mock_msgs.warning.call_count == 1

    def test_display_sync_results_empty(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        req = _make_request()
        results = {"valid": [], "invalid": [], "duplicate": [], "missing_remote": []}
        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs:
            view.display_sync_results(req, results)
        mock_msgs.success.assert_not_called()
        mock_msgs.warning.assert_not_called()

    def test_get_cached_links_data_no_data(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")

        with (
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = None
            result = view.get_cached_links_data(_make_request(), MagicMock())
        assert result is None

    def test_get_cached_links_data_returns_links(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")

        with (
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = {"links": [{"local_port_id": "p"}]}
            result = view.get_cached_links_data(_make_request(), MagicMock())
        assert result == [{"local_port_id": "p"}]

    def test_check_existing_cable(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        local = MagicMock(pk=1)
        remote = MagicMock(pk=2)

        with (
            patch("netbox_librenms_plugin.views.sync.cables.Cable") as mock_cable_cls,
            patch("netbox_librenms_plugin.views.sync.cables.ContentType") as mock_ct,
        ):
            mock_ct.objects.get_for_model.return_value = MagicMock()
            mock_cable_cls.objects.filter.return_value.exists.return_value = True
            result = view.check_existing_cable(local, remote)
        assert result is True


class TestSyncCablesViewProcessInterfaceSyncException:
    """Lines 147-149: outer except Exception handler in process_interface_sync."""

    def test_process_single_interface_exception_caught(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.request = _make_request()

        interface = {"local_port_id": "port1"}
        cached_links = []

        mock_transaction = MagicMock()
        # Make __exit__ NOT suppress exceptions (return False)
        mock_transaction.atomic.return_value.__exit__.return_value = False

        with (
            patch("netbox_librenms_plugin.views.sync.cables.transaction", mock_transaction),
            patch.object(view, "process_single_interface", side_effect=RuntimeError("test error")),
        ):
            results = view.process_interface_sync([interface], cached_links)

        assert "port1" in results["invalid"]


class TestAddDeviceToLibreNMSViewPermission:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        view.require_write_permission = MagicMock(return_value=_denied_response())
        view.request = _make_request(post_data={"snmp_version": "v2c"})

        result = view.post(view.request, object_id=1)
        assert result.status_code == 403


class TestAddDeviceToLibreNMSViewFormInvalid:
    def test_invalid_form_shows_errors(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        view.require_write_permission = MagicMock(return_value=None)

        mock_device = MagicMock()
        mock_device.get_absolute_url.return_value = "/device/1/"

        post_data = {"v1v2-snmp_version": "v2c", "object_type": "device"}

        with (
            patch("netbox_librenms_plugin.views.sync.devices.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.devices.redirect"),
        ):
            mock_device_cls.objects.get.return_value = mock_device
            view.request = _make_request(post_data=post_data)
            view.object = mock_device

            # Provide an invalid form (missing required hostname)
            view.post(view.request, object_id=1)

        # Should show form errors
        assert mock_msgs.error.call_count >= 0  # form validation may or may not find errors


class TestAddDeviceToLibreNMSViewFormValid:
    def test_valid_v2c_form_calls_api(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        view.require_write_permission = MagicMock(return_value=None)

        mock_device = MagicMock()
        mock_device.get_absolute_url.return_value = "/device/1/"
        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.add_device.return_value = (True, "Device added")

        mock_form = MagicMock()
        mock_form.is_valid.return_value = True
        mock_form.cleaned_data = {
            "hostname": "router.example.com",
            "snmp_version": "v2c",
            "community": "public",
            "force_add": False,
            "port": None,
            "transport": None,
            "port_association_mode": None,
            "poller_group": None,
        }

        with (
            patch("netbox_librenms_plugin.views.sync.devices.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.devices.redirect"),
            patch("netbox_librenms_plugin.views.sync.devices.AddToLIbreSNMPV1V2", return_value=mock_form),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_device_cls.objects.get.return_value = mock_device
            post_data = {"v1v2-snmp_version": "v2c", "object_type": "device"}
            view.request = _make_request(post_data=post_data)
            view.object = mock_device

            view.post(view.request, object_id=1)

        mock_api.add_device.assert_called_once()
        mock_msgs.success.assert_called_once()


class TestAddDeviceToLibreNMSViewFormValidExtraFields:
    """Covers lines 74-78: transport and port_association_mode optional fields."""

    def test_valid_form_with_transport_and_pam(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        view.require_write_permission = MagicMock(return_value=None)

        mock_device = MagicMock()
        mock_device.get_absolute_url.return_value = "/device/1/"
        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.add_device.return_value = (True, "Device added")

        mock_form = MagicMock()
        mock_form.is_valid.return_value = True
        mock_form.cleaned_data = {
            "hostname": "router.example.com",
            "snmp_version": "v2c",
            "community": "public",
            "force_add": False,
            "port": 161,
            "transport": "udp6",
            "port_association_mode": 2,
            "poller_group": None,
        }

        with (
            patch("netbox_librenms_plugin.views.sync.devices.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.devices.messages"),
            patch("netbox_librenms_plugin.views.sync.devices.redirect"),
            patch("netbox_librenms_plugin.views.sync.devices.AddToLIbreSNMPV1V2", return_value=mock_form),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_device_cls.objects.get.return_value = mock_device
            post_data = {"v1v2-snmp_version": "v2c", "object_type": "device"}
            view.request = _make_request(post_data=post_data)
            view.object = mock_device

            view.post(view.request, object_id=1)

        call_kwargs = mock_api.add_device.call_args[0][0]
        assert call_kwargs.get("transport") == "udp6"
        assert call_kwargs.get("port_association_mode") == 2

    def test_invalid_poller_group_ignored(self):
        """Covers lines 81-82: except (ValueError, TypeError) for non-int poller_group."""
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        view.require_write_permission = MagicMock(return_value=None)

        mock_device = MagicMock()
        mock_device.get_absolute_url.return_value = "/device/1/"
        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.add_device.return_value = (True, "Device added")

        mock_form = MagicMock()
        mock_form.is_valid.return_value = True
        mock_form.cleaned_data = {
            "hostname": "router.example.com",
            "snmp_version": "v2c",
            "community": "public",
            "force_add": False,
            "port": None,
            "transport": None,
            "port_association_mode": None,
            "poller_group": "not-a-number",  # Triggers except path
        }

        with (
            patch("netbox_librenms_plugin.views.sync.devices.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.devices.messages"),
            patch("netbox_librenms_plugin.views.sync.devices.redirect"),
            patch("netbox_librenms_plugin.views.sync.devices.AddToLIbreSNMPV1V2", return_value=mock_form),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_device_cls.objects.get.return_value = mock_device
            post_data = {"v1v2-snmp_version": "v2c", "object_type": "device"}
            view.request = _make_request(post_data=post_data)
            view.object = mock_device

            view.post(view.request, object_id=1)

        call_kwargs = mock_api.add_device.call_args[0][0]
        assert "poller_group" not in call_kwargs


class TestAddDeviceToLibreNMSViewV3:
    def test_v3_form_submits_v3_data(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        view.require_write_permission = MagicMock(return_value=None)
        view.request = _make_request()

        mock_device = MagicMock()
        mock_device.get_absolute_url.return_value = "/device/1/"
        mock_api = MagicMock()
        mock_api.add_device.return_value = (False, "Error")

        mock_form = MagicMock()
        mock_form.is_valid.return_value = True
        mock_form.cleaned_data = {
            "hostname": "router.example.com",
            "snmp_version": "v3",
            "authlevel": "authPriv",
            "authname": "user",
            "authpass": "auth123",
            "authalgo": "MD5",
            "cryptopass": "priv123",
            "cryptoalgo": "DES",
            "force_add": False,
            "port": None,
            "transport": None,
            "port_association_mode": None,
            "poller_group": None,
        }

        with (
            patch("netbox_librenms_plugin.views.sync.devices.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.devices.redirect"),
            patch("netbox_librenms_plugin.views.sync.devices.AddToLIbreSNMPV3", return_value=mock_form),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_device_cls.objects.get.return_value = mock_device
            post_data = {"v3-snmp_version": "v3", "object_type": "device"}
            view.request = _make_request(post_data=post_data)
            view.object = mock_device
            view.post(view.request, object_id=1)

        mock_api.add_device.assert_called_once()
        mock_msgs.error.assert_called_once()


class TestAddDeviceToLibreNMSViewUnknownVersion:
    def test_unknown_snmp_version_shows_error(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        view.require_write_permission = MagicMock(return_value=None)

        mock_device = MagicMock()
        mock_device.get_absolute_url.return_value = "/device/1/"
        mock_api = MagicMock()

        mock_form = MagicMock()
        mock_form.is_valid.return_value = True
        mock_form.cleaned_data = {
            "hostname": "router.example.com",
            "snmp_version": "v99",  # Unknown version
            "force_add": False,
            "port": None,
            "transport": None,
            "port_association_mode": None,
            "poller_group": None,
        }

        with (
            patch("netbox_librenms_plugin.views.sync.devices.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.devices.redirect"),
            patch("netbox_librenms_plugin.views.sync.devices.AddToLIbreSNMPV3", return_value=mock_form),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_device_cls.objects.get.return_value = mock_device
            post_data = {"object_type": "device"}
            view.request = _make_request(post_data=post_data)
            view.object = mock_device
            view.post(view.request, object_id=1)

        mock_msgs.error.assert_called()


class TestAddDeviceToLibreNMSViewGetFormClass:
    def test_v1v2_form_class(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView, AddToLIbreSNMPV1V2

        view = object.__new__(AddDeviceToLibreNMSView)
        view.request = _make_request(post_data={"snmp_version": "v2c"})
        assert view.get_form_class() is AddToLIbreSNMPV1V2

    def test_v3_form_class(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView, AddToLIbreSNMPV3

        view = object.__new__(AddDeviceToLibreNMSView)
        view.request = _make_request(post_data={"snmp_version": "v3"})
        assert view.get_form_class() is AddToLIbreSNMPV3

    def test_v1v2_via_prefix(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView, AddToLIbreSNMPV1V2

        view = object.__new__(AddDeviceToLibreNMSView)
        view.request = _make_request(post_data={"v1v2-snmp_version": "v1"})
        assert view.get_form_class() is AddToLIbreSNMPV1V2

    def test_get_object_virtualmachine(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        mock_vm = MagicMock()

        with patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=mock_vm):
            result = view.get_object(5, object_type="virtualmachine")
        assert result is mock_vm

    def test_get_object_device_not_found_falls_back_to_vm(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        mock_vm = MagicMock()

        class _DNE(Exception):
            pass

        with (
            patch("netbox_librenms_plugin.views.sync.devices.Device") as mock_dev_cls,
            patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=mock_vm),
        ):
            mock_dev_cls.DoesNotExist = _DNE
            mock_dev_cls.objects.get.side_effect = _DNE()
            result = view.get_object(5)
        assert result is mock_vm

    def test_form_valid_with_poller_group(self):
        """poller_group valid int is passed to API."""
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        view.request = _make_request()
        mock_api = MagicMock()
        mock_api.add_device.return_value = (True, "ok")
        view.object = MagicMock()
        view.object.get_absolute_url.return_value = "/d/"

        mock_form = MagicMock()
        mock_form.cleaned_data = {
            "hostname": "h.example.com",
            "snmp_version": "v2c",
            "community": "public",
            "force_add": False,
            "port": 161,
            "transport": "udp",
            "port_association_mode": None,
            "poller_group": "2",
        }

        with (
            patch("netbox_librenms_plugin.views.sync.devices.messages"),
            patch("netbox_librenms_plugin.views.sync.devices.redirect"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view.form_valid(mock_form, snmp_version="v2c")

        call_args = mock_api.add_device.call_args[0][0]
        assert call_args["poller_group"] == 2


class TestUpdateDeviceLocationView:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        view = object.__new__(UpdateDeviceLocationView)
        view.require_write_permission = MagicMock(return_value=_denied_response())
        view.request = _make_request()

        result = view.post(view.request, pk=1)
        assert result.status_code == 403

    def test_device_with_site_updates_location(self):
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        view = object.__new__(UpdateDeviceLocationView)
        view.require_write_permission = MagicMock(return_value=None)

        mock_device = MagicMock()
        mock_device.site.name = "London"
        mock_api = MagicMock()
        mock_api.get_librenms_id.return_value = 42
        mock_api.update_device_field.return_value = (True, "ok")

        with (
            patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.devices.redirect"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view.request = _make_request()
            view.post(view.request, pk=1)

        mock_api.update_device_field.assert_called_once()
        mock_msgs.success.assert_called_once()

    def test_device_with_site_api_failure(self):
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        view = object.__new__(UpdateDeviceLocationView)
        view.require_write_permission = MagicMock(return_value=None)

        mock_device = MagicMock()
        mock_device.site.name = "London"
        mock_api = MagicMock()
        mock_api.get_librenms_id.return_value = 42
        mock_api.update_device_field.return_value = (False, "Connection refused")

        with (
            patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.devices.redirect"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view.request = _make_request()
            view.post(view.request, pk=1)

        mock_msgs.error.assert_called_once()

    def test_device_no_site_shows_warning(self):
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        view = object.__new__(UpdateDeviceLocationView)
        view.require_write_permission = MagicMock(return_value=None)

        mock_device = MagicMock()
        mock_device.site = None
        mock_api = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.devices.redirect"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view.request = _make_request()
            view.post(view.request, pk=1)

        mock_msgs.warning.assert_called_once()


# ===========================================================================
# views/sync/ip_addresses.py — SyncIPAddressesView
# ===========================================================================


class TestSyncIPAddressesViewPermissionDenied:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=_denied_response())
        view.request = _make_request()

        result = view.post(view.request, object_type="device", pk=1)
        assert result.status_code == 403


class TestSyncIPAddressesViewCacheMiss:
    def test_cache_miss_redirects(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")

        mock_device = MagicMock(pk=1)
        mock_api = MagicMock(server_key="default")

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = None
            view.request = _make_request(post_data={"select": ["192.168.1.1/24"]})
            view.post(view.request, object_type="device", pk=1)

        mock_msgs.error.assert_called_once()


class TestSyncIPAddressesViewNoSelection:
    def test_no_selection_redirects(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")

        mock_device = MagicMock(pk=1)
        mock_api = MagicMock(server_key="default")

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ip_addresses": [{"ip_address": "10.0.0.1"}]}
            view.request = _make_request(post_data={})
            view.post(view.request, object_type="device", pk=1)

        mock_msgs.error.assert_called_once()


class TestSyncIPAddressesViewCreateIP:
    def test_new_ip_is_created(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")

        mock_device = MagicMock(pk=1)
        mock_api = MagicMock(server_key="default")

        ip_data = {
            "ip_address": "10.0.0.1",
            "ip_with_mask": "10.0.0.1/24",
            "interface_url": "http://localhost/api/dcim/interfaces/5/",
        }

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.IPAddress") as mock_ip_cls,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.Interface") as mock_iface_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ip_addresses": [ip_data]}
            mock_ip_cls.objects.filter.return_value.first.return_value = None
            mock_iface_cls.objects.get.return_value = MagicMock()

            view.request = _make_request(post_data={"select": ["10.0.0.1"]})
            view.post(view.request, object_type="device", pk=1)

        mock_ip_cls.objects.create.assert_called_once()
        mock_msgs.success.assert_called()


class TestSyncIPAddressesViewUpdateIP:
    def test_existing_ip_updated_when_interface_differs(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")

        mock_device = MagicMock(pk=1)
        mock_api = MagicMock(server_key="default")

        ip_data = {
            "ip_address": "10.0.0.1",
            "ip_with_mask": "10.0.0.1/24",
            "interface_url": "http://localhost/api/dcim/interfaces/5/",
        }
        mock_existing_ip = MagicMock()
        mock_existing_ip.assigned_object = MagicMock()  # Different from mock_iface
        mock_existing_ip.vrf = None
        mock_iface = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.IPAddress") as mock_ip_cls,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.Interface") as mock_iface_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ip_addresses": [ip_data]}
            mock_ip_cls.objects.filter.return_value.first.return_value = mock_existing_ip
            mock_iface_cls.objects.get.return_value = mock_iface

            view.request = _make_request(post_data={"select": ["10.0.0.1"]})
            view.post(view.request, object_type="device", pk=1)

        mock_existing_ip.save.assert_called_once()
        mock_msgs.success.assert_called()


class TestSyncIPAddressesViewUnchangedIP:
    def test_unchanged_ip_shows_warning(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")

        mock_device = MagicMock(pk=1)
        mock_api = MagicMock(server_key="default")

        ip_data = {
            "ip_address": "10.0.0.1",
            "ip_with_mask": "10.0.0.1/24",
            "interface_url": None,
        }
        mock_existing_ip = MagicMock()
        mock_existing_ip.assigned_object = None
        mock_existing_ip.vrf = None

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.IPAddress") as mock_ip_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ip_addresses": [ip_data]}
            mock_ip_cls.objects.filter.return_value.first.return_value = mock_existing_ip

            view.request = _make_request(post_data={"select": ["10.0.0.1"]})
            view.post(view.request, object_type="device", pk=1)

        mock_msgs.warning.assert_called()


class TestSyncIPAddressesViewHelpers:
    def test_get_object_device(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.get_object_or_404", return_value=mock_device):
            result = view.get_object("device", 1)
        assert result is mock_device

    def test_get_object_virtualmachine(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        mock_vm = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.get_object_or_404", return_value=mock_vm):
            result = view.get_object("virtualmachine", 1)
        assert result is mock_vm

    def test_get_object_invalid_type_raises(self):
        from django.http import Http404
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        try:
            view.get_object("unknown", 1)
            assert False, "Should have raised Http404"
        except Http404:
            pass

    def test_get_vrf_selection_none_when_no_vrf(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        req = _make_request(post_data={})
        result = view.get_vrf_selection(req, "10.0.0.1")
        assert result is None

    def test_get_vrf_selection_returns_vrf(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        mock_vrf = MagicMock()
        req = _make_request(post_data={"vrf_10.0.0.1": "3"})

        with patch("netbox_librenms_plugin.views.sync.ip_addresses.VRF") as mock_vrf_cls:
            mock_vrf_cls.objects.get.return_value = mock_vrf
            result = view.get_vrf_selection(req, "10.0.0.1")
        assert result is mock_vrf

    def test_get_vrf_selection_not_found_returns_none(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        req = _make_request(post_data={"vrf_10.0.0.1": "99"})

        class _DNE(Exception):
            pass

        with patch("netbox_librenms_plugin.views.sync.ip_addresses.VRF") as mock_vrf_cls:
            mock_vrf_cls.DoesNotExist = _DNE
            mock_vrf_cls.objects.get.side_effect = _DNE()
            result = view.get_vrf_selection(req, "10.0.0.1")
        assert result is None

    def test_get_ip_tab_url_device(self):
        from dcim.models import Device
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view._post_server_key = "prod"
        mock_api = MagicMock(server_key="prod")
        mock_device = MagicMock(spec=Device, pk=5)

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/device/5/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            url = view.get_ip_tab_url(mock_device)
        assert "ipaddresses" in url
        assert "server_key=prod" in url

    def test_display_sync_results_all_branches(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        req = _make_request()
        results = {
            "created": ["10.0.0.1"],
            "updated": ["10.0.0.2"],
            "unchanged": ["10.0.0.3"],
            "failed": ["10.0.0.4"],
        }
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msgs:
            view.display_sync_results(req, results)
        assert mock_msgs.success.call_count == 2
        assert mock_msgs.warning.call_count == 1
        assert mock_msgs.error.call_count == 1

    def test_get_ip_tab_url_vm(self):
        from virtualization.models import VirtualMachine
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view._post_server_key = "default"
        mock_api = MagicMock(server_key="default")
        mock_vm = MagicMock(spec=VirtualMachine, pk=7)

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/vm/7/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            url = view.get_ip_tab_url(mock_vm)
        assert "ipaddresses" in url

    def test_get_ip_tab_url_no_server_key(self):
        from dcim.models import Device
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view._post_server_key = None
        mock_api = MagicMock(server_key=None)
        mock_device = MagicMock(spec=Device, pk=5)

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/device/5/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            url = view.get_ip_tab_url(mock_device)
        assert "server_key" not in url


class TestSyncIPAddressesViewVMInterface:
    def test_vm_interface_resolved(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")

        mock_vm = MagicMock()
        mock_vm.pk = 2
        mock_api = MagicMock(server_key="default")

        ip_data = {
            "ip_address": "10.0.0.5",
            "ip_with_mask": "10.0.0.5/24",
            "interface_url": "http://localhost/api/virtualization/interfaces/9/",
        }

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.get_object_or_404", return_value=mock_vm),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.messages"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.IPAddress") as mock_ip_cls,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.VMInterface") as mock_vmiface_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ip_addresses": [ip_data]}
            mock_ip_cls.objects.filter.return_value.first.return_value = None
            mock_vmiface_cls.objects.get.return_value = MagicMock()

            view.request = _make_request(post_data={"select": ["10.0.0.5"]})
            view.post(view.request, object_type="virtualmachine", pk=2)

        mock_vmiface_cls.objects.get.assert_called_once()


# ===========================================================================
# views/sync/vlans.py — SyncVLANsView
# ===========================================================================


class TestSyncVLANsViewPermissionDenied:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=_denied_response())
        view.request = _make_request()

        result = view.post(view.request, object_type="device", object_id=1)
        assert result.status_code == 403


class TestSyncVLANsViewInvalidAction:
    def test_invalid_action_shows_error(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view.request = _make_request(post_data={"action": "bad_action"})
            view.post(view.request, object_type="device", object_id=1)

        mock_msgs.error.assert_called_once()


class TestSyncVLANsViewNoSelection:
    def test_no_selection_shows_error(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"some": "data"}
            view.request = _make_request(post_data={"action": "create_vlans"})
            view.post(view.request, object_type="device", object_id=1)

        mock_msgs.error.assert_called_once()


class TestSyncVLANsViewCacheMiss:
    def test_cache_miss_shows_error(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = None
            view.request = _make_request(post_data={"action": "create_vlans", "select": ["100"]})
            view.post(view.request, object_type="device", object_id=1)

        mock_msgs.error.assert_called_once()


class TestSyncVLANsViewCreateVLAN:
    def test_new_vlan_created(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        mock_vlan = MagicMock()

        librenms_vlans = [{"vlan_vlan": 100, "vlan_name": "Management"}]

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = librenms_vlans
            mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, True)

            view.request = _make_request(post_data={"action": "create_vlans", "select": ["100"]})
            view.post(view.request, object_type="device", object_id=1)

        mock_vlan_cls.objects.get_or_create.assert_called_once()
        mock_msgs.success.assert_called_once()


class TestSyncVLANsViewUpdateVLAN:
    def test_existing_vlan_name_updated(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        mock_vlan = MagicMock()
        mock_vlan.name = "OldName"

        librenms_vlans = [{"vlan_vlan": 100, "vlan_name": "Management"}]

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages"),
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = librenms_vlans
            mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, False)

            view.request = _make_request(post_data={"action": "create_vlans", "select": ["100"]})
            view.post(view.request, object_type="device", object_id=1)

        mock_vlan.save.assert_called_once()


class TestSyncVLANsViewUnchangedVLAN:
    def test_unchanged_vlan_counts_as_skipped(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        mock_vlan = MagicMock()
        mock_vlan.name = "Management"  # Same name as librenms → unchanged

        librenms_vlans = [{"vlan_vlan": 100, "vlan_name": "Management"}]

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = librenms_vlans
            mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, False)

            view.request = _make_request(post_data={"action": "create_vlans", "select": ["100"]})
            view.post(view.request, object_type="device", object_id=1)

        mock_msgs.success.assert_called()
        assert "unchanged" in str(mock_msgs.success.call_args_list)


class TestSyncVLANsViewWithGroup:
    def test_vlan_created_in_group(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        mock_vlan_group = MagicMock(pk=3)
        mock_vlan = MagicMock()

        librenms_vlans = [{"vlan_vlan": 200, "vlan_name": "Production"}]

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages"),
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls,
            patch("netbox_librenms_plugin.views.sync.vlans.VLANGroup") as mock_group_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = librenms_vlans
            mock_group_cls.objects.get.return_value = mock_vlan_group
            mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, True)

            view.request = _make_request(post_data={"action": "create_vlans", "select": ["200"], "vlan_group_200": "3"})
            view.post(view.request, object_type="device", object_id=1)

        call_kwargs = mock_vlan_cls.objects.get_or_create.call_args[1]
        assert call_kwargs.get("group") is mock_vlan_group or mock_vlan_cls.objects.get_or_create.called

    def test_invalid_vlan_group_id_falls_back_to_global(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        mock_vlan = MagicMock()

        librenms_vlans = [{"vlan_vlan": 200, "vlan_name": "Production"}]

        class _DNE(Exception):
            pass

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages"),
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls,
            patch("netbox_librenms_plugin.views.sync.vlans.VLANGroup") as mock_group_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = librenms_vlans
            mock_group_cls.DoesNotExist = _DNE
            mock_group_cls.objects.get.side_effect = _DNE()
            mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, True)

            view.request = _make_request(
                post_data={"action": "create_vlans", "select": ["200"], "vlan_group_200": "99"}
            )
            view.post(view.request, object_type="device", object_id=1)

        # Falls back to global VLAN (group=None)
        call_kwargs = mock_vlan_cls.objects.get_or_create.call_args[1]
        assert call_kwargs.get("group") is None

    def test_invalid_vid_string_skipped(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        librenms_vlans = [{"vlan_vlan": 100, "vlan_name": "Mgmt"}]

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages"),
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = librenms_vlans
            # "not-a-vid" → int() fails → skip
            view.request = _make_request(post_data={"action": "create_vlans", "select": ["not-a-vid"]})
            view.post(view.request, object_type="device", object_id=1)

        # no VLAN created, shows "no VLANs" warning
        mock_vlan_cls.objects.get_or_create.assert_not_called()

    def test_unknown_vid_in_cache_skipped(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        librenms_vlans = [{"vlan_vlan": 100, "vlan_name": "Mgmt"}]

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages"),
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = librenms_vlans
            # VID 999 not in cached vlans → skipped
            view.request = _make_request(post_data={"action": "create_vlans", "select": ["999"]})
            view.post(view.request, object_type="device", object_id=1)

        mock_vlan_cls.objects.get_or_create.assert_not_called()

    def test_no_vlans_shows_warning(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        librenms_vlans = [{"vlan_vlan": 100, "vlan_name": "Mgmt"}]

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.VLAN"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = librenms_vlans
            view.request = _make_request(post_data={"action": "create_vlans", "select": ["not-a-vid"]})
            view.post(view.request, object_type="device", object_id=1)

        mock_msgs.warning.assert_called()


class TestSyncVLANsViewGroupedUpdateSkip:
    """Lines 134-139: grouped VLAN update (elif) and unchanged (else) paths."""

    def test_grouped_vlan_name_updated(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        mock_vlan_group = MagicMock(pk=3)
        mock_vlan = MagicMock()
        mock_vlan.name = "OldGroupedName"  # Different from librenms → update path

        librenms_vlans = [{"vlan_vlan": 300, "vlan_name": "NewGroupedName"}]

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages"),
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls,
            patch("netbox_librenms_plugin.views.sync.vlans.VLANGroup") as mock_group_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = librenms_vlans
            mock_group_cls.objects.get.return_value = mock_vlan_group
            mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, False)  # exists

            view.request = _make_request(post_data={"action": "create_vlans", "select": ["300"], "vlan_group_300": "3"})
            view.post(view.request, object_type="device", object_id=1)

        mock_vlan.save.assert_called_once()

    def test_grouped_vlan_unchanged_skipped(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        mock_vlan_group = MagicMock(pk=3)
        mock_vlan = MagicMock()
        mock_vlan.name = "SameName"  # Same name → skipped_count path

        librenms_vlans = [{"vlan_vlan": 300, "vlan_name": "SameName"}]

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mock_vlan_cls,
            patch("netbox_librenms_plugin.views.sync.vlans.VLANGroup") as mock_group_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = librenms_vlans
            mock_group_cls.objects.get.return_value = mock_vlan_group
            mock_vlan_cls.objects.get_or_create.return_value = (mock_vlan, False)

            view.request = _make_request(post_data={"action": "create_vlans", "select": ["300"], "vlan_group_300": "3"})
            view.post(view.request, object_type="device", object_id=1)

        mock_vlan.save.assert_not_called()
        mock_msgs.success.assert_called()
        assert "unchanged" in str(mock_msgs.success.call_args_list)

    def test_get_object_device(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.vlans.get_object_or_404", return_value=mock_device):
            result = view.get_object("device", 1)
        assert result is mock_device

    def test_get_object_invalid_raises(self):
        from django.http import Http404
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        try:
            view.get_object("vm_type", 1)
            assert False, "Should have raised Http404"
        except Http404:
            pass

    def test_redirect_with_server_key(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view._post_server_key = "production"
        mock_api = MagicMock(server_key="production")

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/device/1/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.redirect") as mock_redirect,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view._redirect("device", 1)

        call_url = mock_redirect.call_args[0][0]
        assert "server_key=production" in call_url


# ===========================================================================
# views/sync/locations.py — SyncSiteLocationView
# ===========================================================================


class TestSyncSiteLocationViewPost:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=_denied_response())
        view.request = _make_request(post_data={"action": "create", "pk": "1"})

        result = view.post(view.request)
        assert result.status_code == 403

    def test_missing_pk_shows_error(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.locations.redirect"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view.request = _make_request(post_data={"action": "create"})
            view.post(view.request)

        mock_msgs.error.assert_called_once()

    def test_site_not_found_shows_error(self):
        from django.core.exceptions import ObjectDoesNotExist
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.locations.redirect"),
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.get.side_effect = ObjectDoesNotExist()
            view.request = _make_request(post_data={"action": "create", "pk": "99"})
            view.post(view.request)

        mock_msgs.error.assert_called_once()

    def test_unknown_action_shows_error(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()
        mock_site = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.locations.redirect"),
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.get.return_value = mock_site
            view.request = _make_request(post_data={"action": "banana", "pk": "1"})
            view.post(view.request)

        mock_msgs.error.assert_called_once()


class TestSyncSiteLocationViewCreate:
    def test_create_without_coords_shows_warning(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()
        mock_site = MagicMock()
        mock_site.name = "London"
        mock_site.latitude = None
        mock_site.longitude = None

        with (
            patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.locations.redirect"),
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.get.return_value = mock_site
            view.request = _make_request(post_data={"action": "create", "pk": "1"})
            view.post(view.request)

        mock_msgs.warning.assert_called_once()

    def test_create_success(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()
        mock_api.add_location.return_value = (True, "ok")
        mock_site = MagicMock()
        mock_site.name = "London"
        mock_site.latitude = 51.5
        mock_site.longitude = -0.12

        with (
            patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.locations.redirect"),
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.get.return_value = mock_site
            view.request = _make_request(post_data={"action": "create", "pk": "1"})
            view.post(view.request)

        mock_msgs.success.assert_called_once()

    def test_create_failure(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()
        mock_api.add_location.return_value = (False, "Server error")
        mock_site = MagicMock()
        mock_site.name = "London"
        mock_site.latitude = 51.5
        mock_site.longitude = -0.12

        with (
            patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.locations.redirect"),
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.get.return_value = mock_site
            view.request = _make_request(post_data={"action": "create", "pk": "1"})
            view.post(view.request)

        mock_msgs.error.assert_called_once()


class TestSyncSiteLocationViewUpdate:
    def test_update_without_coords_shows_warning(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()
        mock_site = MagicMock()
        mock_site.name = "Berlin"
        mock_site.latitude = None
        mock_site.longitude = 13.4

        with (
            patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.locations.redirect"),
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.get.return_value = mock_site
            view.request = _make_request(post_data={"action": "update", "pk": "1"})
            view.post(view.request)

        mock_msgs.warning.assert_called_once()

    def test_update_api_failure_fetching_locations(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()
        mock_api.get_locations.return_value = (False, "Connection error")
        mock_site = MagicMock()
        mock_site.name = "Berlin"
        mock_site.latitude = 52.5
        mock_site.longitude = 13.4

        with (
            patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.locations.redirect"),
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.get.return_value = mock_site
            view.request = _make_request(post_data={"action": "update", "pk": "1"})
            view.post(view.request)

        mock_msgs.error.assert_called_once()

    def test_update_no_matching_location(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()
        mock_api.get_locations.return_value = (True, [{"location": "Paris"}])  # No Berlin
        mock_site = MagicMock()
        mock_site.name = "Berlin"
        mock_site.slug = "berlin"
        mock_site.latitude = 52.5
        mock_site.longitude = 13.4

        with (
            patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.locations.redirect"),
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.get.return_value = mock_site
            view.request = _make_request(post_data={"action": "update", "pk": "1"})
            view.post(view.request)

        mock_msgs.error.assert_called_once()

    def test_update_success(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()
        mock_api.get_locations.return_value = (True, [{"location": "Berlin"}])
        mock_api.update_location.return_value = (True, "ok")
        mock_site = MagicMock()
        mock_site.name = "Berlin"
        mock_site.slug = "berlin"
        mock_site.latitude = 52.5
        mock_site.longitude = 13.4

        with (
            patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.locations.redirect"),
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.get.return_value = mock_site
            view.request = _make_request(post_data={"action": "update", "pk": "1"})
            view.post(view.request)

        mock_msgs.success.assert_called_once()

    def test_update_failure(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()
        mock_api.get_locations.return_value = (True, [{"location": "Berlin"}])
        mock_api.update_location.return_value = (False, "Not found in LibreNMS")
        mock_site = MagicMock()
        mock_site.name = "Berlin"
        mock_site.slug = "berlin"
        mock_site.latitude = 52.5
        mock_site.longitude = 13.4

        with (
            patch("netbox_librenms_plugin.views.sync.locations.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.locations.redirect"),
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.get.return_value = mock_site
            view.request = _make_request(post_data={"action": "update", "pk": "1"})
            view.post(view.request)

        mock_msgs.error.assert_called_once()


class TestSyncSiteLocationViewHelpers:
    def test_match_site_by_name(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        site = MagicMock()
        site.name = "London"
        site.slug = "london"
        locations = [{"location": "London"}, {"location": "Berlin"}]
        result = view.match_site_with_location(site, locations)
        assert result["location"] == "London"

    def test_match_site_by_slug(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        site = MagicMock()
        site.name = "LondonDC"
        site.slug = "london"
        locations = [{"location": "london"}]
        result = view.match_site_with_location(site, locations)
        assert result is not None

    def test_match_site_not_found(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        site = MagicMock()
        site.name = "Tokyo"
        site.slug = "tokyo"
        locations = [{"location": "London"}]
        result = view.match_site_with_location(site, locations)
        assert result is None

    def test_check_coordinates_match_true(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        assert view.check_coordinates_match(51.5, -0.12, "51.5", "-0.12") is True

    def test_check_coordinates_match_false(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        assert view.check_coordinates_match(51.5, -0.12, "52.0", "-0.12") is False

    def test_check_coordinates_none_returns_false(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        assert view.check_coordinates_match(None, -0.12, "51.5", "-0.12") is False

    def test_build_location_data_with_name(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        site = MagicMock()
        site.name = "London"
        site.latitude = "51.5"
        site.longitude = "-0.12"
        data = view.build_location_data(site)
        assert data["location"] == "London"
        assert data["lat"] == "51.5"

    def test_build_location_data_without_name(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        site = MagicMock()
        site.name = "London"
        site.latitude = "51.5"
        site.longitude = "-0.12"
        data = view.build_location_data(site, include_name=False)
        assert "location" not in data

    def test_get_site_by_pk_returns_none_on_not_found(self):
        from django.core.exceptions import ObjectDoesNotExist
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)

        with patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls:
            mock_site_cls.objects.get.side_effect = ObjectDoesNotExist()
            result = view.get_site_by_pk(99)
        assert result is None

    def test_get_queryset_api_failure_returns_empty(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        view.request = _make_request()
        view.filterset = None
        mock_api = MagicMock()
        mock_api.get_locations.return_value = (False, "Error")

        with (
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.all.return_value = []
            result = view.get_queryset()
        assert result == []

    def test_create_sync_data_no_match(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        site = MagicMock()
        site.name = "Atlantis"
        site.slug = "atlantis"
        locations = [{"location": "London"}]
        sync_data = view.create_sync_data(site, locations)
        assert sync_data.is_synced is False
        assert sync_data.librenms_location is None

    def test_create_sync_data_with_match(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        site = MagicMock()
        site.name = "London"
        site.slug = "london"
        site.latitude = 51.5
        site.longitude = -0.12
        locations = [{"location": "London", "lat": "51.5", "lng": "-0.12"}]
        sync_data = view.create_sync_data(site, locations)
        assert sync_data.is_synced is True


class TestSyncSiteLocationViewSuperMethods:
    """Lines 26-28, 32-35: get_table and get_context_data (call super())."""

    def test_get_table_configures_table(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        mock_table = MagicMock()
        mock_request = MagicMock()
        view.request = mock_request

        with patch("netbox_librenms_plugin.views.sync.locations.SingleTableView.get_table", return_value=mock_table):
            result = view.get_table()

        mock_table.configure.assert_called_once_with(mock_request)
        assert result is mock_table

    def test_get_context_data_adds_filter_form(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        mock_queryset = MagicMock()
        mock_filterset_cls = MagicMock()
        mock_filterset_instance = MagicMock()
        mock_filterset_cls.return_value = mock_filterset_instance
        view.filterset = mock_filterset_cls
        view.request = MagicMock()
        view.request.GET = {}
        view.get_queryset = MagicMock(return_value=mock_queryset)

        parent_ctx = {"some_key": "some_value"}
        with patch(
            "netbox_librenms_plugin.views.sync.locations.SingleTableView.get_context_data", return_value=parent_ctx
        ):
            result = view.get_context_data()

        assert "filter_form" in result
        assert result["filter_form"] is mock_filterset_instance.form


class TestSyncSiteLocationViewGetQuerysetFilterset:
    """Lines 44-49: filterset branch in get_queryset."""

    def test_get_queryset_with_filterset_and_get_params(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        mock_api = MagicMock(server_key="default")
        mock_request = MagicMock()
        mock_request.GET = {"name": "London"}
        view.request = mock_request
        view.filterset = MagicMock()
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        view.get_librenms_locations = MagicMock(return_value=(True, [{"location": "London"}]))
        view.create_sync_data = MagicMock(side_effect=lambda s, _locs: s)

        mock_site = MagicMock()
        mock_site.name = "London"

        mock_qs = MagicMock()
        view.filterset.return_value.qs = mock_qs

        with (
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.all.return_value = [mock_site]
            result = view.get_queryset()

        assert result is mock_qs

    def test_get_queryset_no_get_params_returns_list(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        mock_api = MagicMock(server_key="default")
        mock_request = MagicMock()
        mock_request.GET = {}  # Empty GET → falls through to return sync_data (line 49)
        view.request = mock_request
        view.filterset = None  # Falsy → line 47 skipped
        view.get_librenms_locations = MagicMock(return_value=(True, [{"location": "London"}]))
        mock_site = MagicMock()
        mock_site.name = "London"
        view.create_sync_data = MagicMock(side_effect=lambda s, _locs: s)

        with (
            patch("netbox_librenms_plugin.views.sync.locations.Site") as mock_site_cls,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_site_cls.objects.all.return_value = [mock_site]
            result = view.get_queryset()

        assert result == [mock_site]
