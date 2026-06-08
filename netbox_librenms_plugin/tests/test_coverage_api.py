"""Coverage tests for librenms_api.py missing lines."""

from unittest.mock import MagicMock, patch

import requests


def _make_api(url="https://librenms.example.com", token="test-token"):
    """Create a LibreNMSAPI instance without database calls."""
    with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_cfg:
        mock_cfg.side_effect = lambda plugin, key, default=None: {
            "servers": None,
            "librenms_url": url,
            "api_token": token,
            "cache_timeout": 300,
            "verify_ssl": True,
        }.get(key, default)

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        return LibreNMSAPI(server_key="default")


class TestLibreNMSAPIInitFallback:
    """Tests for __init__ fallback when no server_key (lines 35-36)."""

    def test_init_reads_selected_server_from_settings(self):
        """When no server_key, tries to get selected_server from LibreNMSSettings."""
        servers_config = {
            "primary": {
                "librenms_url": "https://primary.example.com",
                "api_token": "tok",
                "cache_timeout": 300,
                "verify_ssl": True,
            }
        }

        with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_cfg:
            mock_cfg.side_effect = lambda plugin, key, default=None: servers_config if key == "servers" else default

            mock_settings_obj = MagicMock()
            mock_settings_obj.selected_server = "primary"
            mock_settings_class = MagicMock()
            mock_settings_class.objects.first.return_value = mock_settings_obj

            # LibreNMSSettings is imported inline in __init__, patch via models
            with patch.dict(
                "sys.modules", {"netbox_librenms_plugin.models": MagicMock(LibreNMSSettings=mock_settings_class)}
            ):
                from netbox_librenms_plugin.librenms_api import LibreNMSAPI

                api = LibreNMSAPI()
                assert api.server_key == "primary"
                assert api.librenms_url == "https://primary.example.com"
                assert api.api_token == "tok"

    def test_init_settings_import_error_defaults_to_default(self):
        """When LibreNMSSettings can't be imported, defaults to 'default'."""
        with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_cfg:
            mock_cfg.side_effect = lambda plugin, key, default=None: {
                "servers": None,
                "librenms_url": "https://x.example.com",
                "api_token": "tok",
                "cache_timeout": 300,
                "verify_ssl": True,
            }.get(key, default)

            # Simulate AttributeError when accessing LibreNMSSettings (covers except branch)
            mock_models = MagicMock()
            mock_models.LibreNMSSettings.objects.first.side_effect = AttributeError("no attr")

            with patch.dict("sys.modules", {"netbox_librenms_plugin.models": mock_models}):
                from netbox_librenms_plugin.librenms_api import LibreNMSAPI

                api = LibreNMSAPI()
                assert api.server_key == "default"
                assert api.librenms_url == "https://x.example.com"
                assert api.api_token == "tok"


class TestTestConnectionErrors:
    """Tests for test_connection error paths (lines 116, 121, 137, 146-147, 157-171)."""

    def test_http_403_returns_error(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        with patch("requests.get", return_value=mock_resp):
            result = api.test_connection()
        assert result["error"] is True
        assert "forbidden" in result["message"].lower()

    def test_http_404_returns_error(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("requests.get", return_value=mock_resp):
            result = api.test_connection()
        assert result["error"] is True
        assert "not found" in result["message"].lower()

    def test_http_500_returns_error(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("requests.get", return_value=mock_resp):
            result = api.test_connection()
        assert result["error"] is True
        assert "server error" in result["message"].lower()

    def test_http_unexpected_code_returns_error(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        with patch("requests.get", return_value=mock_resp):
            result = api.test_connection()
        assert result["error"] is True
        assert "302" in result["message"]

    def test_ssl_error_returns_error(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.SSLError("cert failed")):
            result = api.test_connection()
        assert result["error"] is True
        assert "SSL" in result["message"] or "ssl" in result["message"].lower()

    def test_connection_error_returns_error(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("unreachable")):
            result = api.test_connection()
        assert result["error"] is True
        assert "Connection failed" in result["message"]

    def test_timeout_returns_error(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            result = api.test_connection()
        assert result["error"] is True
        assert "timeout" in result["message"].lower()

    def test_generic_exception_returns_error(self):
        api = _make_api()
        with patch("requests.get", side_effect=ValueError("something weird")):
            result = api.test_connection()
        assert result["error"] is True
        assert "Unexpected error" in result["message"]


class TestGetAvailableServersLegacy:
    """Tests for get_available_servers legacy path (line 231)."""

    def test_legacy_config_no_servers(self):
        """When no servers_config, returns default server with legacy URL."""
        with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_cfg:

            def side_effect(plugin, key, default=None):
                if key == "servers":
                    return None
                if key == "librenms_url":
                    return "https://legacy.example.com"
                return default

            mock_cfg.side_effect = side_effect
            from netbox_librenms_plugin.librenms_api import LibreNMSAPI

            result = LibreNMSAPI.get_available_servers()
            assert "default" in result
            assert "legacy.example.com" in result["default"]

    def test_no_legacy_url_returns_default_label(self):
        """When no servers_config and no legacy URL, returns default label."""
        with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_cfg:

            def side_effect(plugin, key, default=None):
                return None

            mock_cfg.side_effect = side_effect
            from netbox_librenms_plugin.librenms_api import LibreNMSAPI

            result = LibreNMSAPI.get_available_servers()
            assert "default" in result
            assert result["default"] == "Default Server"


class TestGetLibreNMSIdDictServerKey:
    """Tests for get_librenms_id → _store_librenms_id with dict CF (lines 259-262)."""

    def test_dict_cf_routes_to_get_librenms_device_id(self):
        """When CF has a dict 'librenms_id', get_librenms_id uses get_librenms_device_id(obj, server_key, auto_save=False)."""
        api = _make_api()

        obj = MagicMock()
        obj.cf = {"librenms_id": {"default": None}}
        obj.custom_field_data = {"librenms_id": {"default": None}}
        obj._meta.model_name = "device"
        obj.pk = 42
        obj.primary_ip = None
        obj.name = None

        with patch("netbox_librenms_plugin.utils.get_librenms_device_id", return_value=None) as mock_get_id:
            with patch("netbox_librenms_plugin.librenms_api.cache") as mock_cache:
                mock_cache.get.return_value = None
                result = api.get_librenms_id(obj)
                assert result is None
                mock_get_id.assert_called_once_with(obj, "default", auto_save=False)

    def test_get_librenms_id_zero_is_valid(self):
        """Regression: librenms_id == 0 must be returned directly without falling through to API lookup.
        The code uses 'is not None' guards, so 0 is a valid cached/stored ID."""
        api = _make_api()

        obj = MagicMock()
        obj.cf = {"librenms_id": {"default": 0}}
        obj.custom_field_data = {"librenms_id": {"default": 0}}
        obj._meta.model_name = "device"
        obj.pk = 1
        obj.primary_ip = None
        obj.name = "zero-device"

        # get_librenms_device_id returns 0 (valid ID, must not be treated as falsy)
        with patch("netbox_librenms_plugin.utils.get_librenms_device_id", return_value=0) as mock_get_id:
            with patch("netbox_librenms_plugin.librenms_api.cache") as mock_cache:
                mock_cache.get.return_value = None
                result = api.get_librenms_id(obj)
        # 0 is a valid ID — method should return 0, not fall through to hostname lookup
        assert result == 0
        mock_get_id.assert_called_once_with(obj, "default", auto_save=False)

    def test_store_librenms_id_via_hostname_lookup(self):
        """get_librenms_id reaches _store_librenms_id when CF/cache miss but hostname API hit."""
        api = _make_api()

        obj = MagicMock()
        obj.cf = {"librenms_id": None}  # CF key present so _store_librenms_id uses CF path
        obj.custom_field_data = {}
        obj._meta.model_name = "device"
        obj.pk = 99
        obj.primary_ip = None
        obj.name = "hostname"

        with patch("netbox_librenms_plugin.utils.get_librenms_device_id", return_value=None):
            with patch("netbox_librenms_plugin.librenms_api.cache") as mock_cache:
                mock_cache.get.return_value = None
                with patch.object(api, "get_device_id_by_hostname", return_value=42) as mock_by_hostname:
                    with patch("netbox_librenms_plugin.utils.set_librenms_device_id") as mock_set_id:
                        result = api.get_librenms_id(obj)

        assert result == 42
        mock_by_hostname.assert_called_once_with("hostname")
        mock_set_id.assert_called_once_with(obj, 42, "default")


class TestGetPortsErrors:
    """Tests for get_ports error paths (lines 373, 375-376)."""

    def test_http_error_404_returns_false(self):
        api = _make_api()
        http_err = requests.exceptions.HTTPError(response=MagicMock(status_code=404))
        with patch("requests.get", side_effect=http_err):
            ok, msg = api.get_ports(1)
        assert ok is False
        assert "not found" in msg.lower()

    def test_http_error_other_returns_false(self):
        api = _make_api()
        http_err = requests.exceptions.HTTPError(response=MagicMock(status_code=500))
        http_err.response = MagicMock(status_code=500)
        with patch("requests.get", side_effect=http_err):
            ok, msg = api.get_ports(1)
        assert ok is False

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("conn error")):
            ok, msg = api.get_ports(1)
        assert ok is False


class TestGetInventoryFilteredErrors:
    """Tests for get_inventory_filtered error paths (lines 405, 407, 409, 411)."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.RequestException("error")):
            ok, result = api.get_inventory_filtered(1)
        assert ok is False
        assert isinstance(result, str)  # error message string

    def test_empty_results_with_no_filters_returns_true_empty_list(self):
        """Empty inventory with status:ok is a valid successful empty response."""
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "ok", "inventory": []}

        with patch("requests.get", return_value=mock_resp):
            ok, result = api.get_inventory_filtered(1)
        assert ok is True
        assert result == []

    def test_fallback_to_all_endpoint_when_filtered_empty(self):
        """If filtered endpoint returns empty and params present, falls back to /all."""
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "ok", "inventory": []}

        all_inventory = [{"entPhysicalClass": "chassis", "entPhysicalContainedIn": 0}]

        with patch("requests.get", return_value=mock_resp):
            with patch.object(api, "get_device_inventory", return_value=(True, all_inventory)):
                ok, result = api.get_inventory_filtered(1, ent_physical_class="chassis")

        assert ok is True
        assert len(result) == 1

    def test_fallback_fails_when_all_endpoint_fails(self):
        """If /all fallback also fails, returns False, []."""
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "ok", "inventory": []}

        with patch("requests.get", return_value=mock_resp):
            with patch.object(api, "get_device_inventory", return_value=(False, [])):
                ok, result = api.get_inventory_filtered(1, ent_physical_class="chassis")

        assert ok is False
        assert result == []


class TestGetDeviceVlansErrors:
    """Tests for get_device_vlans error paths (lines 474-480)."""

    def test_http_error_404_returns_false(self):
        api = _make_api()
        http_err = requests.exceptions.HTTPError(response=MagicMock(status_code=404))
        http_err.response = MagicMock(status_code=404)
        with patch("requests.get", side_effect=http_err):
            ok, msg = api.get_device_vlans(1)
        assert ok is False

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("error")):
            ok, msg = api.get_device_vlans(1)
        assert ok is False

    def test_non_200_returns_http_status(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        http_err = requests.exceptions.HTTPError("503 Service Unavailable")
        http_err.response = mock_resp
        mock_resp.raise_for_status.side_effect = http_err
        with patch("requests.get", return_value=mock_resp):
            ok, msg = api.get_device_vlans(1)
        assert ok is False
        assert "503" in msg


class TestGetDeviceLinksErrors:
    """Tests for get_device_links error paths (lines 505-508)."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("error")):
            ok, msg = api.get_device_links(1)
        assert ok is False

    def test_request_exception_base_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.RequestException("error")):
            ok, msg = api.get_device_links(1)
        assert ok is False


class TestListDevicesErrors:
    """Tests for list_devices error paths (lines 542-547)."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("error")):
            ok, msg = api.list_devices()
        assert ok is False

    def test_non_200_returns_empty(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "error"}
        with patch("requests.get", return_value=mock_resp):
            ok, result = api.list_devices()
        assert ok is False


class TestGetDeviceIpsErrors:
    """Tests for get_device_ips error paths (lines 580-585)."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("error")):
            ok, msg = api.get_device_ips(1)
        assert ok is False


class TestGetDeviceInfoErrors:
    """Tests for get_device_info error paths (lines 606-607)."""

    def test_non_200_returns_false(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status.return_value = None
        with patch("requests.get", return_value=mock_resp):
            ok, data = api.get_device_info(1)
        assert ok is False
        assert data is None

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.RequestException("error")):
            ok, data = api.get_device_info(1)
        assert ok is False
        assert data is None


class TestGetPortVlanDetailsErrors:
    """Tests for get_port_vlan_details error paths."""

    def test_http_error_404_returns_false(self):
        api = _make_api()
        http_err = requests.exceptions.HTTPError(response=MagicMock(status_code=404))
        http_err.response = MagicMock(status_code=404)
        with patch("requests.get", side_effect=http_err):
            ok, msg = api.get_port_vlan_details(1)
        assert ok is False
        assert "not found" in msg.lower()

    def test_non_404_http_error_returns_false(self):
        api = _make_api()
        http_err = requests.exceptions.HTTPError(response=MagicMock(status_code=500))
        http_err.response = MagicMock(status_code=500)
        with patch("requests.get", side_effect=http_err):
            ok, msg = api.get_port_vlan_details(1)
        assert ok is False

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("error")):
            ok, msg = api.get_port_vlan_details(1)
        assert ok is False

    def test_non_200_returns_false(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.raise_for_status.return_value = None
        with patch("requests.get", return_value=mock_resp):
            ok, msg = api.get_port_vlan_details(1)
        assert ok is False

    def test_request_exception_base_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.RequestException("error")):
            ok, msg = api.get_port_vlan_details(1)
        assert ok is False

    def test_http_404_via_raise_for_status_returns_false(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        http_error = requests.exceptions.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_error
        with patch("requests.get", return_value=mock_resp):
            ok, msg = api.get_port_vlan_details(1)
        assert ok is False


class TestListDevicesSuccess:
    """Tests for list_devices success (lines 805+)."""

    def test_list_devices_with_filters(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "ok", "devices": [{"device_id": 1}]}

        with patch("requests.get", return_value=mock_resp) as mock_get:
            ok, result = api.list_devices({"type": "network"})

        assert ok is True
        assert len(result) == 1
        # Verify the filter was forwarded to the outgoing request
        assert mock_get.call_args[1]["params"] == {"type": "network"}

    def test_list_devices_no_filters(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "ok", "devices": []}

        with patch("requests.get", return_value=mock_resp):
            ok, result = api.list_devices()

        assert ok is True
        assert result == []


class TestGetPoller:
    """Tests for get_poller_groups error path."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.RequestException("error")):
            ok, result = api.get_poller_groups()
        assert ok is False

    def test_non_ok_status_returns_false(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "error"}
        with patch("requests.get", return_value=mock_resp):
            ok, result = api.get_poller_groups()
        assert ok is False


class TestAddDeviceErrors:
    """Tests for add_device errors."""

    def _make_device_data(self):
        return {"hostname": "router01", "snmp_version": "v2c", "community": "public", "force_add": False}

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.post", side_effect=requests.exceptions.RequestException("error")):
            ok, msg = api.add_device(self._make_device_data())
        assert ok is False

    def test_non_ok_response_returns_false(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "error", "message": "Already exists"}
        with patch("requests.post", return_value=mock_resp):
            ok, msg = api.add_device(self._make_device_data())
        assert ok is False


class TestGetLocationsErrors:
    """Tests for get_locations errors."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.RequestException("error")):
            ok, msg = api.get_locations()
        assert ok is False


class TestUpdateDeviceFieldErrors:
    """Tests for update_device_field errors."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.patch", side_effect=requests.exceptions.RequestException("error")):
            ok, msg = api.update_device_field(1, {"field": "value"})
        assert ok is False


class TestGetDeviceIdByIPErrors:
    """Tests for get_device_id_by_ip errors."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.RequestException("error")):
            result = api.get_device_id_by_ip("192.168.1.1")
        assert result is None

    def test_non_200_returns_none(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        http_error = requests.exceptions.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_error
        with patch("requests.get", return_value=mock_resp):
            result = api.get_device_id_by_ip("192.168.1.1")
        assert result is None

    def test_null_devices_field_returns_none(self):
        """API returns {"devices": null} — TypeError must be caught, not propagate."""
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"devices": None}
        with patch("requests.get", return_value=mock_resp):
            result = api.get_device_id_by_ip("192.168.1.1")
        assert result is None

    def test_empty_devices_list_returns_none(self):
        """API returns {"devices": []} — no match, returns None."""
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"devices": []}
        with patch("requests.get", return_value=mock_resp):
            result = api.get_device_id_by_ip("192.168.1.1")
        assert result is None


class TestGetDeviceIdByHostnameErrors:
    """Tests for get_device_id_by_hostname errors."""

    def test_request_exception_returns_none(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.RequestException("error")):
            result = api.get_device_id_by_hostname("router01")
        assert result is None

    def test_null_devices_field_returns_none(self):
        """API returns {"devices": null} — TypeError must be caught, not propagate."""
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"devices": None}
        with patch("requests.get", return_value=mock_resp):
            result = api.get_device_id_by_hostname("router01")
        assert result is None

    def test_empty_devices_list_returns_none(self):
        """API returns {"devices": []} — no match, returns None."""
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"devices": []}
        with patch("requests.get", return_value=mock_resp):
            result = api.get_device_id_by_hostname("router01")
        assert result is None


class TestStorelibrenmsId:
    """Tests for _store_librenms_id (lines 259-262)."""

    def test_stores_via_set_librenms_device_id_when_cf_has_key(self):
        api = _make_api()
        obj = MagicMock()
        obj.cf = {"librenms_id": {"default": None}}
        obj.custom_field_data = {"librenms_id": {"default": None}}

        with patch("netbox_librenms_plugin.utils.set_librenms_device_id") as mock_set:
            api._store_librenms_id(obj, 42)
        mock_set.assert_called_once_with(obj, 42, "default")
        obj.save.assert_called_once()

    def test_stores_in_cache_when_no_cf_key(self):
        api = _make_api()
        obj = MagicMock()
        obj.cf = {}  # No 'librenms_id' key

        with patch("netbox_librenms_plugin.librenms_api.cache") as mock_cache:
            api._store_librenms_id(obj, 42)
        mock_cache.set.assert_called_once()
        cache_key_used = mock_cache.set.call_args[0][0]
        assert api.server_key in cache_key_used


class TestParsePortVlanData:
    """Tests for parse_port_vlan_data (lines 978+)."""

    def test_no_if_vlan_returns_mode_none(self):
        api = _make_api()
        port_data = {"port_id": 1, "ifName": "Gi0/1", "ifDescr": "GigabitEthernet", "ifVlan": ""}
        result = api.parse_port_vlan_data(port_data)
        assert result["mode"] is None

    def test_trunk_mode_set_correctly(self):
        api = _make_api()
        port_data = {
            "port_id": 1,
            "ifName": "Gi0/1",
            "ifDescr": "GE",
            "ifVlan": "100",
            "ifTrunk": "dot1Q",
            "vlans": [{"vlan": 100, "untagged": 0}, {"vlan": 200, "untagged": 0}],
        }
        result = api.parse_port_vlan_data(port_data)
        assert result["mode"] == "tagged"
        assert 100 in result["tagged_vlans"]
        assert 200 in result["tagged_vlans"]

    def test_access_mode_from_vlan_array(self):
        api = _make_api()
        port_data = {
            "port_id": 1,
            "ifName": "Gi0/2",
            "ifDescr": "GE",
            "ifVlan": "100",
            "vlans": [{"vlan": 100, "untagged": 1}],
        }
        result = api.parse_port_vlan_data(port_data)
        assert result["mode"] == "access"
        assert result["untagged_vlan"] == 100

    def test_fallback_to_if_vlan_when_no_vlans_array(self):
        api = _make_api()
        port_data = {"port_id": 1, "ifName": "Gi0/3", "ifDescr": "GE", "ifVlan": "50", "ifTrunk": None}
        result = api.parse_port_vlan_data(port_data)
        assert result["mode"] == "access"
        assert result["untagged_vlan"] == 50

    def test_invalid_if_vlan_fallback_returns_none(self):
        """Lines 1028-1029: ValueError when ifVlan is not an integer."""
        api = _make_api()
        port_data = {"port_id": 1, "ifName": "Gi0/4", "ifDescr": "GE", "ifVlan": "not-a-number"}
        result = api.parse_port_vlan_data(port_data)
        assert result["untagged_vlan"] is None

    def test_if_descr_used_as_interface_name(self):
        api = _make_api()
        port_data = {"port_id": 1, "ifName": "Gi0/5", "ifDescr": "GigabitEthernet0/5", "ifVlan": ""}
        result = api.parse_port_vlan_data(port_data, interface_name_field="ifDescr")
        assert result["interface_name"] == "GigabitEthernet0/5"

    def test_string_vlan_id_normalized_to_int(self):
        """VLAN ID as string '50' must be coerced to int 50 to avoid string sort."""
        api = _make_api()
        port_data = {
            "port_id": 1,
            "ifName": "Gi0/6",
            "ifDescr": "GE",
            "ifVlan": "50",
            "vlans": [{"vlan": "50", "untagged": 1}, {"vlan": "100", "untagged": 0}],
        }
        result = api.parse_port_vlan_data(port_data)
        assert result["untagged_vlan"] == 50
        assert result["tagged_vlans"] == [100]

    def test_none_vlan_id_skipped(self):
        """Entry with missing 'vlan' key (None) must be skipped."""
        api = _make_api()
        port_data = {
            "port_id": 1,
            "ifName": "Gi0/7",
            "ifDescr": "GE",
            "ifVlan": "200",
            "vlans": [{"untagged": 0}, {"vlan": 200, "untagged": 1}],
        }
        result = api.parse_port_vlan_data(port_data)
        assert result["untagged_vlan"] == 200
        assert result["tagged_vlans"] == []

    def test_malformed_vlan_id_skipped(self):
        """Non-numeric string vlan ID is skipped gracefully."""
        api = _make_api()
        port_data = {
            "port_id": 1,
            "ifName": "Gi0/8",
            "ifDescr": "GE",
            "ifVlan": "300",
            "vlans": [{"vlan": "N/A", "untagged": 0}, {"vlan": 300, "untagged": 1}],
        }
        result = api.parse_port_vlan_data(port_data)
        assert result["untagged_vlan"] == 300
        assert result["tagged_vlans"] == []

    def test_empty_vlans_array_falls_back_to_if_vlan(self):
        """Empty vlans array should use ifVlan fallback."""
        api = _make_api()
        port_data = {"port_id": 1, "ifName": "Gi0/9", "ifDescr": "GE", "ifVlan": "10", "vlans": []}
        result = api.parse_port_vlan_data(port_data)
        assert result["untagged_vlan"] == 10


class TestGetDeviceInfoResponseFormats:
    """Tests for get_device_info with unusual API response shapes."""

    def test_null_devices_returns_false(self):
        """{"devices": null} must return (False, None), not raise TypeError."""
        from unittest.mock import MagicMock, patch

        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"devices": None}
        with patch("requests.get", return_value=mock_resp):
            success, result = api.get_device_info(1)
        assert success is False
        assert result is None

    def test_empty_devices_list_returns_false(self):
        """{"devices": []} must return (False, None)."""
        from unittest.mock import MagicMock, patch

        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"devices": []}
        with patch("requests.get", return_value=mock_resp):
            success, result = api.get_device_info(1)
        assert success is False
        assert result is None

    def test_non_200_returns_false(self):
        """Non-200 status code returns (False, None)."""
        from unittest.mock import MagicMock, patch

        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("requests.get", return_value=mock_resp):
            success, result = api.get_device_info(1)
        assert success is False
        assert result is None

    def test_valid_device_returns_device_dict(self):
        """Normal response returns (True, device_dict)."""
        from unittest.mock import MagicMock, patch

        api = _make_api()
        device = {"device_id": 42, "hostname": "router01"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"devices": [device]}
        with patch("requests.get", return_value=mock_resp):
            success, result = api.get_device_info(42)
        assert success is True
        assert result["device_id"] == 42


class TestGetPortByIdErrors:
    """Tests for get_port_by_id errors."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.RequestException("error")):
            ok, msg = api.get_port_by_id(1)
        assert ok is False


class TestGetDeviceInventoryErrors:
    """Tests for get_device_inventory errors."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.get", side_effect=requests.exceptions.RequestException("error")):
            ok, msg = api.get_device_inventory(1)
        assert ok is False


class TestGetAvailableServersMultiConfig:
    """Tests for get_available_servers with multi-server config (lines 161-165)."""

    def test_multi_server_config_returns_dict(self):
        api = _make_api()
        servers_config = {
            "primary": {"display_name": "Primary Server"},
            "secondary": {"display_name": "Secondary Server"},
        }

        with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_config:
            mock_config.side_effect = lambda plugin, key, default=None: servers_config if key == "servers" else None
            result = api.get_available_servers()
        assert result == {"primary": "Primary Server", "secondary": "Secondary Server"}

    def test_multi_server_config_uses_key_when_no_display_name(self):
        api = _make_api()
        servers_config = {
            "main": {},  # No display_name key
        }

        with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_config:
            mock_config.side_effect = lambda plugin, key, default=None: servers_config if key == "servers" else None
            result = api.get_available_servers()
        assert result == {"main": "main"}


class TestAddDeviceWithOptionalFields:
    """Tests for add_device with optional fields (lines 405, 407, 409, 411)."""

    def _make_base_data(self):
        return {"hostname": "router01", "snmp_version": "v2c", "community": "public", "force_add": False}

    def test_add_device_with_port(self):
        api = _make_api()
        data = {**self._make_base_data(), "port": 161}
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "ok", "message": "Device added"}
        with patch("requests.post", return_value=mock_resp) as mock_post:
            ok, msg = api.add_device(data)
        assert ok is True
        assert msg == "Device added successfully."
        assert "port" in mock_post.call_args[1]["json"]

    def test_add_device_with_transport(self):
        api = _make_api()
        data = {**self._make_base_data(), "transport": "udp6"}
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "ok", "message": "ok"}
        with patch("requests.post", return_value=mock_resp) as mock_post:
            ok, msg = api.add_device(data)
        assert ok is True
        assert msg == "Device added successfully."
        assert "transport" in mock_post.call_args[1]["json"]

    def test_add_device_with_port_association_mode(self):
        api = _make_api()
        data = {**self._make_base_data(), "port_association_mode": "ifName"}
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "ok", "message": "ok"}
        with patch("requests.post", return_value=mock_resp) as mock_post:
            ok, msg = api.add_device(data)
        assert ok is True
        assert msg == "Device added successfully."
        assert "port_association_mode" in mock_post.call_args[1]["json"]

    def test_add_device_with_poller_group(self):
        api = _make_api()
        data = {**self._make_base_data(), "poller_group": 2}
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "ok", "message": "ok"}
        with patch("requests.post", return_value=mock_resp) as mock_post:
            ok, msg = api.add_device(data)
        assert ok is True
        assert msg == "Device added successfully."
        assert "poller_group" in mock_post.call_args[1]["json"]


class TestUpdateDeviceFieldUnexpected:
    """Tests for update_device_field non-ok status (line 474)."""

    def test_non_ok_status_returns_false(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "error", "message": "Failed"}
        with patch("requests.patch", return_value=mock_resp):
            ok, msg = api.update_device_field(1, {"field": "value"})
        assert ok is False

    def test_request_exception_with_json_response(self):
        """Lines 477-479: extract message from JSON error response."""
        api = _make_api()
        mock_response = MagicMock()
        mock_response.json.return_value = {"message": "Detailed error"}
        exc = requests.exceptions.RequestException("error")
        exc.response = mock_response
        with patch("requests.patch", side_effect=exc):
            ok, msg = api.update_device_field(1, {"field": "value"})
        assert ok is False
        assert "Detailed error" in msg


class TestGetLocationsNoLocations:
    """Tests for get_locations when no locations found (line 505)."""

    def test_no_locations_in_response(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "ok"}  # No 'locations' key
        with patch("requests.get", return_value=mock_resp):
            ok, msg = api.get_locations()
        assert ok is False


class TestAddLocationErrors:
    """Tests for add_location error paths (lines 542-547)."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.post", side_effect=requests.exceptions.RequestException("error")):
            ok, msg = api.add_location({"location": "TestSite", "lat": 0, "lng": 0})
        assert ok is False

    def test_request_exception_with_json_response(self):
        api = _make_api()
        mock_response = MagicMock()
        mock_response.json.return_value = {"message": "Detailed error"}
        exc = requests.exceptions.RequestException("error")
        exc.response = mock_response
        with patch("requests.post", side_effect=exc):
            ok, msg = api.add_location({"location": "TestSite", "lat": 0, "lng": 0})
        assert ok is False
        assert "Detailed error" in msg


class TestUpdateLocationErrors:
    """Tests for update_location error paths (lines 580-585)."""

    def test_request_exception_returns_false(self):
        api = _make_api()
        with patch("requests.patch", side_effect=requests.exceptions.RequestException("error")):
            ok, msg = api.update_location("TestSite", {"lat": 0, "lng": 0})
        assert ok is False

    def test_request_exception_with_json_response(self):
        api = _make_api()
        mock_response = MagicMock()
        mock_response.json.return_value = {"message": "Update failed"}
        exc = requests.exceptions.RequestException("error")
        exc.response = mock_response
        with patch("requests.patch", side_effect=exc):
            ok, msg = api.update_location("TestSite", {"lat": 0, "lng": 0})
        assert ok is False
        assert "Update failed" in msg


class TestGetInventoryFilteredNonOk:
    """Tests for get_inventory_filtered non-200 response (line 689 + 791, 799)."""

    def test_non_200_response_returns_false(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status.return_value = None
        with patch("requests.get", return_value=mock_resp):
            ok, data = api.get_inventory_filtered(1)
        assert ok is False
        assert isinstance(data, str)  # error message, not empty list

    def test_ent_physical_contained_in_filter(self):
        """Line 791: ent_physical_contained_in filter exercised — API returns already-filtered list."""
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        # The real LibreNMS API filters server-side; mock returns only the matching item.
        inventory = [
            {"entPhysicalContainedIn": "1", "entPhysicalName": "slot1"},
        ]
        mock_resp.json.return_value = {"status": "ok", "inventory": inventory}
        with patch("requests.get", return_value=mock_resp) as mock_get:
            ok, data = api.get_inventory_filtered(1, ent_physical_contained_in="1")
        assert ok is True
        assert len(data) == 1
        mock_get.assert_called_once()
        _, call_kwargs = mock_get.call_args
        assert call_kwargs.get("params", {}).get("entPhysicalContainedIn") == "1"

    def test_empty_inventory_returns_empty(self):
        """Line 799: when response lacks status:ok (even with an empty inventory list), returns False."""
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"inventory": []}  # Empty inventory
        with patch("requests.get", return_value=mock_resp):
            ok, data = api.get_inventory_filtered(1)
        # No "status":"ok" in response → falls through to return False, message
        assert ok is False
        assert isinstance(data, str)  # error message, not empty list


class TestGetDeviceVlansHttpError:
    """Tests for get_device_vlans HTTP error paths (lines 918, 924)."""

    def test_http_404_returns_not_found(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        exc = requests.exceptions.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = exc
        exc.response = mock_resp
        with patch("requests.get", side_effect=exc):
            ok, msg = api.get_device_vlans(1)
        assert ok is False

    def test_http_5xx_returns_error(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        exc = requests.exceptions.HTTPError(response=mock_resp)
        exc.response = mock_resp
        with patch("requests.get", side_effect=exc):
            ok, msg = api.get_device_vlans(1)
        assert ok is False


class TestGetPortVlanDetailsHttpError:
    """Tests for get_port_vlan_details HTTP error paths (line 974)."""

    def test_http_non_404_returns_http_error(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        exc = requests.exceptions.HTTPError(response=mock_resp)
        exc.response = mock_resp
        with patch("requests.get", side_effect=exc):
            ok, msg = api.get_port_vlan_details(1)
        assert ok is False
        assert "HTTP error" in msg


class TestGetInventoryFilteredNonOkStatus:
    """Line 689: non-200 returns False with error message."""

    def test_non_200_status(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.return_value = None
        with patch("requests.get", return_value=mock_resp):
            ok, data = api.get_inventory_filtered(1)
        assert ok is False
        assert isinstance(data, str)  # error message, not empty list


class TestGetDeviceVlansNonOkResponse:
    """Line 918: get_device_vlans when status != ok."""

    def test_vlans_response_status_not_ok(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"status": "error", "message": "Failed to retrieve VLANs"}
        with patch("requests.get", return_value=mock_resp):
            ok, msg = api.get_device_vlans(1)
        assert ok is False
        assert "Failed" in msg


class TestGetDeviceInventoryNonOkStatus:
    """Line 689: get_device_inventory non-200 returns False, []."""

    def test_non_200_status(self):
        api = _make_api()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500 Server Error")
        with patch("requests.get", return_value=mock_resp):
            ok, data = api.get_device_inventory(1)
        assert ok is False


class TestMalformedPayloads:
    """Tests for malformed-payload guards in API methods (inventory, devices, vlans)."""

    def _ok_resp(self, body: dict):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = body
        return mock_resp

    def test_get_device_inventory_none_inventory(self):
        """get_device_inventory: inventory=None returns (False, ...)."""
        api = _make_api()
        with patch("requests.get", return_value=self._ok_resp({"status": "ok", "inventory": None})):
            ok, msg = api.get_device_inventory(1)
        assert ok is False
        assert msg is not None

    def test_get_device_inventory_non_list_inventory(self):
        """get_device_inventory: inventory as dict returns (False, ...)."""
        api = _make_api()
        with patch("requests.get", return_value=self._ok_resp({"status": "ok", "inventory": {}})):
            ok, msg = api.get_device_inventory(1)
        assert ok is False

    def test_get_device_inventory_non_dict_inventory_item(self):
        """get_device_inventory: list containing non-dict items returns (False, ...)."""
        api = _make_api()
        body = {"status": "ok", "inventory": [None, {"entPhysicalName": "slot1"}]}
        with patch("requests.get", return_value=self._ok_resp(body)):
            ok, msg = api.get_device_inventory(1)
        assert ok is False
        assert msg is not None

    def test_get_inventory_filtered_none_inventory(self):
        """get_inventory_filtered: inventory=None in filtered path returns (False, ...) without calling get_device_inventory."""
        api = _make_api()
        with patch("requests.get", return_value=self._ok_resp({"status": "ok", "inventory": None})):
            with patch.object(api, "get_device_inventory") as mock_get_inv:
                ok, msg = api.get_inventory_filtered(1, ent_physical_class="chassis")
        assert ok is False
        assert msg is not None
        mock_get_inv.assert_not_called()

    def test_get_inventory_filtered_non_dict_inventory_item(self):
        """get_inventory_filtered: list containing non-dict items returns (False, ...) without fallback."""
        api = _make_api()
        body = {"status": "ok", "inventory": ["bad"]}
        with patch("requests.get", return_value=self._ok_resp(body)):
            with patch.object(api, "get_device_inventory") as mock_get_inv:
                ok, msg = api.get_inventory_filtered(1, ent_physical_class="chassis")
        assert ok is False
        assert msg is not None
        mock_get_inv.assert_not_called()

    def test_list_devices_none_devices(self):
        """list_devices: devices=None returns (False, ...)."""
        api = _make_api()
        with patch("requests.get", return_value=self._ok_resp({"status": "ok", "devices": None})):
            ok, msg = api.list_devices()
        assert ok is False
        assert msg is not None

    def test_list_devices_non_list_devices(self):
        """list_devices: devices as string returns (False, ...)."""
        api = _make_api()
        with patch("requests.get", return_value=self._ok_resp({"status": "ok", "devices": "bad"})):
            ok, msg = api.list_devices()
        assert ok is False

    def test_get_device_vlans_none_vlans(self):
        """get_device_vlans: vlans=None returns (False, ...)."""
        api = _make_api()
        with patch("requests.get", return_value=self._ok_resp({"status": "ok", "vlans": None})):
            ok, msg = api.get_device_vlans(1)
        assert ok is False
        assert msg is not None

    def test_get_device_vlans_fails_closed_on_non_dict_items(self):
        """get_device_vlans: non-dict items in vlans list cause fail-closed (False, error message)."""
        api = _make_api()
        vlans = [None, "bad", {"device_id": 1, "vlan_id": 10}]
        with patch("requests.get", return_value=self._ok_resp({"status": "ok", "vlans": vlans})):
            ok, msg = api.get_device_vlans(1)
        assert ok is False
        assert "invalid item shape" in msg

    def test_get_device_ips_none_addresses(self):
        """get_device_ips: addresses=None returns (False, ...)."""
        api = _make_api()
        with patch("requests.get", return_value=self._ok_resp({"addresses": None})):
            ok, _ = api.get_device_ips(1)
        assert ok is False
