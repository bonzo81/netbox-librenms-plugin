"""Coverage tests for netbox_librenms_plugin.import_utils.filters module."""

from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.librenms_api import LibreNMSUnreachable


class _CacheFailingOn:
    """The real Django cache, except that the named operations raise.

    Redis is a true external boundary: a local test cannot take it down for one caller
    only, so the outage is injected here and every other key still round-trips.
    """

    def __init__(self, failing_operations, error):
        from django.core.cache import cache

        self._cache = cache
        self._failing_operations = frozenset(failing_operations)
        self._error = error

    def _run(self, operation, *args, **kwargs):
        if operation in self._failing_operations:
            raise self._error
        return getattr(self._cache, operation)(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self._run("get", *args, **kwargs)

    def set(self, *args, **kwargs):
        return self._run("set", *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._run("delete", *args, **kwargs)


class TestCacheOutageIsNotAnEmptyResult:
    """A Redis outage must not read as "no devices match your filter"."""

    SEARCH = {"hostname": "edge"}
    DEVICE = {"device_id": 4242, "hostname": "edge-01"}

    def _api(self, settings, server):
        """Bind a real LibreNMSAPI to the loopback server, which serves one device."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

        server.register("/api/v0/devices", {"status": "ok", "devices": [self.DEVICE]})
        configure_librenms_servers(
            settings,
            {"default": {"librenms_url": server.url, "api_token": "token", "cache_timeout": 300}},
        )
        return LibreNMSAPI(server_key="default")

    def _break_cache(self, monkeypatch, *operations):
        """Take the cache down for the named operations only."""
        from redis.exceptions import ConnectionError as RedisConnectionError

        from netbox_librenms_plugin.import_utils import filters as filters_module

        monkeypatch.setattr(
            filters_module,
            "cache",
            _CacheFailingOn(operations, RedisConnectionError("redis is down")),
        )

    def _fetch(self, api, **kwargs):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        devices = get_librenms_devices_for_import(api, filters=self.SEARCH, **kwargs)
        return [device["device_id"] for device in devices]

    def test_a_cache_read_outage_still_returns_the_fetched_devices(self, settings, librenms_server, monkeypatch):
        """A dead cache is a miss, so the fetch still runs and reports what LibreNMS holds."""
        api = self._api(settings, librenms_server)
        self._break_cache(monkeypatch, "get")

        assert self._fetch(api) == [4242]

    def test_a_cache_write_outage_still_returns_the_fetched_devices(self, settings, librenms_server, monkeypatch):
        """Failing to store the result must not discard the result."""
        api = self._api(settings, librenms_server)
        self._break_cache(monkeypatch, "set")

        assert self._fetch(api) == [4242]

    def test_a_cache_delete_outage_still_returns_the_fetched_devices(self, settings, librenms_server, monkeypatch):
        """force_refresh drops the key first, and that drop failing changes nothing."""
        api = self._api(settings, librenms_server)
        self._break_cache(monkeypatch, "delete")

        assert self._fetch(api, force_refresh=True) == [4242]

    def test_a_full_cache_outage_still_returns_the_fetched_devices(self, settings, librenms_server, monkeypatch):
        """Redis being down entirely is the real case: every operation fails, the import still works."""
        api = self._api(settings, librenms_server)
        self._break_cache(monkeypatch, "get", "set", "delete")

        assert self._fetch(api) == [4242]
        assert self._fetch(api, force_refresh=True) == [4242]

    def test_an_unexpected_error_propagates_instead_of_reporting_no_devices(
        self, settings, librenms_server, monkeypatch
    ):
        """Only the cache degrades quietly: anything else must surface, not return []."""
        from netbox_librenms_plugin.import_utils import filters as filters_module

        def explode(*_args, **_kwargs):
            raise RuntimeError("boom")

        api = self._api(settings, librenms_server)
        monkeypatch.setattr(filters_module, "get_import_search_cache_key", explode)

        with pytest.raises(RuntimeError, match="boom"):
            self._fetch(api)


class TestGetDeviceCountForFilters:
    """Tests for get_device_count_for_filters (line 101)."""

    @patch("netbox_librenms_plugin.import_utils.filters.get_librenms_devices_for_import")
    def test_returns_device_count(self, mock_get):
        from netbox_librenms_plugin.import_utils.filters import get_device_count_for_filters

        mock_get.return_value = [{"device_id": 1}, {"device_id": 2}]
        api = MagicMock()
        result = get_device_count_for_filters(api, {})
        assert result == 2

    @patch("netbox_librenms_plugin.import_utils.filters.get_librenms_devices_for_import")
    def test_excludes_disabled_when_show_disabled_false(self, mock_get):
        from netbox_librenms_plugin.import_utils.filters import get_device_count_for_filters

        mock_get.return_value = [
            {"device_id": 1, "disabled": 0},
            {"device_id": 2, "disabled": 1},
        ]
        api = MagicMock()
        result = get_device_count_for_filters(api, {}, show_disabled=False)
        assert result == 1

    @patch("netbox_librenms_plugin.import_utils.filters.get_librenms_devices_for_import")
    def test_includes_disabled_when_show_disabled_true(self, mock_get):
        from netbox_librenms_plugin.import_utils.filters import get_device_count_for_filters

        mock_get.return_value = [
            {"device_id": 1, "disabled": 0},
            {"device_id": 2, "disabled": 1},
        ]
        api = MagicMock()
        result = get_device_count_for_filters(api, {}, show_disabled=True)
        assert result == 2

    @patch("netbox_librenms_plugin.import_utils.filters.get_librenms_devices_for_import")
    def test_passes_force_refresh_as_force_refresh(self, mock_get):
        from netbox_librenms_plugin.import_utils.filters import get_device_count_for_filters

        mock_get.return_value = []
        api = MagicMock()
        get_device_count_for_filters(api, {}, clear_cache=True)
        mock_get.assert_called_once_with(api, filters={}, force_refresh=True)


class TestGetLibreNMSDevicesForImport:
    """Tests for get_librenms_devices_for_import (lines 112-244)."""

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_status_filter_up(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (True, [{"device_id": 1}])

        get_librenms_devices_for_import(api, filters={"status": "1"})
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "up"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_status_filter_down(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (True, [{"device_id": 1}])

        get_librenms_devices_for_import(api, filters={"status": "0"})
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "down"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_location_filter_goes_to_api(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (True, [])

        get_librenms_devices_for_import(api, filters={"location": "10"})
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "location_id"
        assert call_args["query"] == "10"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_type_filter_goes_to_api(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (True, [])

        get_librenms_devices_for_import(api, filters={"type": "network"})
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "type"
        assert call_args["query"] == "network"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_os_filter_goes_to_api(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (True, [])

        get_librenms_devices_for_import(api, filters={"os": "ios"})
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "os"
        assert call_args["query"] == "ios"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_hostname_filter_goes_to_api(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (True, [])

        get_librenms_devices_for_import(api, filters={"hostname": "router1"})
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "hostname"
        assert call_args["query"] == "router1"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_sysname_filter_goes_to_api(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (True, [])

        get_librenms_devices_for_import(api, filters={"sysname": "core-sw"})
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "sysName"
        assert call_args["query"] == "core-sw"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_hardware_filter_goes_to_client_side(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (
            True,
            [
                {"hardware": "Cisco C9300", "device_id": 1},
                {"hardware": "Other Device", "device_id": 2},
            ],
        )

        result = get_librenms_devices_for_import(api, filters={"hardware": "C9300"})
        # API gets no filters
        api.list_devices.assert_called_once_with(None)
        # Only the matching device survives client-side filtering
        assert len(result) == 1
        assert result[0]["device_id"] == 1
        assert 2 not in [d["device_id"] for d in result]

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_location_plus_type_location_to_api_type_to_client(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        devices = [
            {"device_id": 1, "type": "network", "location_id": 10},
            {"device_id": 2, "type": "server", "location_id": 10},
        ]
        api.list_devices.return_value = (True, devices)

        result = get_librenms_devices_for_import(api, filters={"location": "10", "type": "network"})
        call_args = api.list_devices.call_args[0][0]
        # location goes to API
        assert call_args["type"] == "location_id"
        # only the matching device survives client-side type filter
        assert len(result) == 1
        assert result[0]["device_id"] == 1
        assert 2 not in [d["device_id"] for d in result]

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_force_refresh_deletes_cache(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (True, [])

        get_librenms_devices_for_import(api, filters={}, force_refresh=True)
        mock_cache.delete.assert_called_once()

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_cache_hit_returns_early_with_from_cache_true(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        cached_devices = [{"device_id": 1}]
        mock_cache.get.return_value = cached_devices

        api = MagicMock()
        api.server_key = "default"

        result, from_cache = get_librenms_devices_for_import(api, filters={}, return_cache_status=True)
        assert from_cache is True
        assert result == cached_devices
        api.list_devices.assert_not_called()

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_api_failure_raises_and_caches_nothing(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (False, "Connection error")

        with pytest.raises(LibreNMSUnreachable, match="Connection error"):
            get_librenms_devices_for_import(api, filters={})

        mock_cache.set.assert_not_called()

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_api_failure_raises_with_return_cache_status(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (False, "Connection error")

        with pytest.raises(LibreNMSUnreachable, match="Connection error"):
            get_librenms_devices_for_import(api, filters={}, return_cache_status=True)

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_an_unexpected_error_propagates(self, mock_cache):
        """Returning [] here made a broken client look like an empty LibreNMS."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.list_devices.side_effect = RuntimeError("Unexpected error")

        with pytest.raises(RuntimeError, match="Unexpected error"):
            get_librenms_devices_for_import(api, filters={})

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_an_unexpected_error_propagates_with_return_cache_status(self, mock_cache):
        """The tuple contract never softens an error into ([], False)."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.list_devices.side_effect = RuntimeError("Unexpected error")

        with pytest.raises(RuntimeError, match="Unexpected error"):
            get_librenms_devices_for_import(api, filters={}, return_cache_status=True)

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_success_caches_result(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        devices = [{"device_id": 1}]
        api.list_devices.return_value = (True, devices)

        get_librenms_devices_for_import(api, filters={})
        mock_cache.set.assert_called_once()

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_creates_api_when_none_provided(self, mock_cache):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None

        mock_api_instance = MagicMock()
        mock_api_instance.server_key = "default"
        mock_api_instance.cache_timeout = 300
        mock_api_instance.list_devices.return_value = (True, [])

        with patch("netbox_librenms_plugin.import_utils.filters.LibreNMSAPI") as MockAPI:
            MockAPI.return_value = mock_api_instance
            get_librenms_devices_for_import(server_key="default")
            MockAPI.assert_called_once_with(server_key="default")

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_status_with_other_filters_go_to_client(self, mock_cache):
        """When status is set, all other filters go client-side."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        devices = [{"device_id": 1, "type": "server", "location_id": 5}]
        api.list_devices.return_value = (True, devices)

        get_librenms_devices_for_import(api, filters={"status": "1", "location": "5", "type": "server"})
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "up"


class TestApplyClientFilters:
    """Tests for _apply_client_filters (lines 258-284)."""

    def test_filter_by_location(self):
        from netbox_librenms_plugin.import_utils.filters import _apply_client_filters

        devices = [
            {"device_id": 1, "location_id": 10},
            {"device_id": 2, "location_id": 20},
        ]
        result = _apply_client_filters(devices, {"location": "10"})
        assert len(result) == 1
        assert result[0]["device_id"] == 1

    def test_filter_by_type(self):
        from netbox_librenms_plugin.import_utils.filters import _apply_client_filters

        devices = [
            {"device_id": 1, "type": "network"},
            {"device_id": 2, "type": "server"},
        ]
        result = _apply_client_filters(devices, {"type": "network"})
        assert len(result) == 1
        assert result[0]["device_id"] == 1

    def test_filter_by_os(self):
        from netbox_librenms_plugin.import_utils.filters import _apply_client_filters

        devices = [
            {"device_id": 1, "os": "ios"},
            {"device_id": 2, "os": "linux"},
        ]
        result = _apply_client_filters(devices, {"os": "ios"})
        assert len(result) == 1

    def test_filter_by_hostname(self):
        from netbox_librenms_plugin.import_utils.filters import _apply_client_filters

        devices = [
            {"device_id": 1, "hostname": "router01.example.com"},
            {"device_id": 2, "hostname": "switch01.example.com"},
        ]
        result = _apply_client_filters(devices, {"hostname": "router"})
        assert len(result) == 1
        assert result[0]["device_id"] == 1

    def test_filter_by_sysname(self):
        from netbox_librenms_plugin.import_utils.filters import _apply_client_filters

        devices = [
            {"device_id": 1, "sysName": "core-router"},
            {"device_id": 2, "sysName": "access-switch"},
        ]
        result = _apply_client_filters(devices, {"sysname": "core"})
        assert len(result) == 1

    def test_filter_by_hardware(self):
        from netbox_librenms_plugin.import_utils.filters import _apply_client_filters

        devices = [
            {"device_id": 1, "hardware": "Cisco C9300-48P"},
            {"device_id": 2, "hardware": "Juniper MX480"},
        ]
        result = _apply_client_filters(devices, {"hardware": "C9300"})
        assert len(result) == 1

    def test_hardware_none_value_handled(self):
        from netbox_librenms_plugin.import_utils.filters import _apply_client_filters

        devices = [
            {"device_id": 1, "hardware": None},
            {"device_id": 2, "hardware": "Cisco C9300"},
        ]
        result = _apply_client_filters(devices, {"hardware": "C9300"})
        assert len(result) == 1
        assert result[0]["device_id"] == 2

    def test_no_filters_returns_all(self):
        from netbox_librenms_plugin.import_utils.filters import _apply_client_filters

        devices = [{"device_id": 1}, {"device_id": 2}]
        result = _apply_client_filters(devices, {})
        assert len(result) == 2


class TestGetLibreNMSDevicesMoreCoverage:
    """More tests for missing filter branches."""

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_status_invalid_string_sets_none(self, mock_cache):
        """Lines 116-117: ValueError/TypeError when status is not a valid int."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (True, [{"device_id": 1}])

        result = get_librenms_devices_for_import(api, filters={"status": "invalid_value"})
        assert isinstance(result, list)
        # Invalid status means api.list_devices is called with None (no API type filter)
        api.list_devices.assert_called_once_with(None)
        # The single device returned from the API is passed through unchanged
        assert len(result) == 1
        assert result[0]["device_id"] == 1

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_status_with_all_other_filters_go_to_client(self, mock_cache):
        """Lines 130-136: When status set, all filters (loc/type/os/hostname/sysname/hw) go client-side."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (
            True,
            [
                {
                    "device_id": 1,
                    "type": "server",
                    "location_id": 5,
                    "os": "linux",
                    "hostname": "srv01",
                    "sysName": "srv01",
                    "hardware": "Dell",
                },
                {
                    "device_id": 2,
                    "type": "other",
                    "location_id": 99,
                    "os": "windows",
                    "hostname": "othersrv",
                    "sysName": "othersrv",
                    "hardware": "HP",
                },
            ],
        )

        result = get_librenms_devices_for_import(
            api,
            filters={
                "status": "1",
                "location": "5",
                "type": "server",
                "os": "linux",
                "hostname": "srv01",
                "sysname": "srv01",
                "hardware": "Dell",
            },
        )
        assert isinstance(result, list)
        # The matching device should be present, but the non-matching device should not
        device_ids = [d["device_id"] for d in result]
        assert 1 in device_ids
        assert len(result) == 1
        assert 2 not in device_ids

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_location_with_remaining_client_filters(self, mock_cache):
        """Lines 150-156: location API filter with type/os/hostname/sysname/hardware as client filters."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (
            True,
            [
                {
                    "device_id": 1,
                    "type": "network",
                    "os": "ios",
                    "hostname": "router01",
                    "sysName": "router01",
                    "hardware": "Cisco",
                    "location_id": "5",
                },
                {
                    "device_id": 2,
                    "type": "network",
                    "os": "ios",
                    "hostname": "switch99",
                    "sysName": "switch99",
                    "hardware": "Cisco",
                    "location_id": "5",
                },
            ],
        )

        result = get_librenms_devices_for_import(
            api,
            filters={
                "location": "5",
                "type": "network",
                "os": "ios",
                "hostname": "router01",
                "sysname": "router01",
                "hardware": "Cisco",
            },
        )
        assert len(result) == 1
        assert result[0]["device_id"] == 1
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "location_id"
        assert call_args["query"] == "5"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_type_filter_with_remaining_client_filters(self, mock_cache):
        """Lines 162-168: type API filter with os/hostname/sysname/hardware as client filters."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (
            True,
            [
                {
                    "device_id": 1,
                    "type": "network",
                    "os": "ios",
                    "hostname": "router01",
                    "sysName": "router01",
                    "hardware": "Cisco",
                },
            ],
        )

        get_librenms_devices_for_import(
            api,
            filters={
                "type": "network",
                "os": "ios",
                "hostname": "router01",
                "sysname": "router01",
                "hardware": "Cisco",
            },
        )
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "type"
        assert call_args["query"] == "network"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_os_filter_with_remaining_client_filters(self, mock_cache):
        """Lines 174-178: os API filter with hostname/sysname/hardware as client filters."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (
            True,
            [
                {"device_id": 1, "os": "ios", "hostname": "router01", "sysName": "router01", "hardware": "Cisco"},
            ],
        )

        get_librenms_devices_for_import(
            api,
            filters={
                "os": "ios",
                "hostname": "router01",
                "sysname": "router01",
                "hardware": "Cisco",
            },
        )
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "os"
        assert call_args["query"] == "ios"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_hostname_filter_with_sysname_and_hardware(self, mock_cache):
        """Lines 184-186: hostname API filter with sysname/hardware as client filters."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (
            True,
            [
                {"device_id": 1, "hostname": "router01", "sysName": "router01", "hardware": "Cisco"},
            ],
        )

        get_librenms_devices_for_import(
            api,
            filters={
                "hostname": "router01",
                "sysname": "router01",
                "hardware": "Cisco",
            },
        )
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "hostname"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_sysname_filter_with_hardware(self, mock_cache):
        """Line 194: sysname API filter with hardware as client filter."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (
            True,
            [
                {"device_id": 1, "sysName": "router01", "hardware": "Cisco"},
            ],
        )

        get_librenms_devices_for_import(
            api,
            filters={
                "sysname": "router01",
                "hardware": "Cisco",
            },
        )
        call_args = api.list_devices.call_args[0][0]
        assert call_args["type"] == "sysName"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_client_filters_applied_to_results(self, mock_cache):
        """Line 237: _apply_client_filters is called when client_filters is set."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        # Two devices, one matches hardware filter, one doesn't
        api.list_devices.return_value = (
            True,
            [
                {"device_id": 1, "hardware": "Cisco C9300", "location_id": 5},
                {"device_id": 2, "hardware": "Juniper MX480", "location_id": 5},
            ],
        )

        result = get_librenms_devices_for_import(
            api,
            filters={
                "location": "5",
                "hardware": "C9300",  # Goes to client_filters
            },
        )
        # Should only return the Cisco device after client filtering
        assert len(result) == 1
        assert result[0]["device_id"] == 1


class TestGetLibreNMSReturnCacheStatus:
    """Tests for return_cache_status path (line 237)."""

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_return_cache_status_with_fresh_data(self, mock_cache):
        """Line 237: return devices, from_cache when return_cache_status=True."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (True, [{"device_id": 1}])

        result = get_librenms_devices_for_import(api, return_cache_status=True)
        assert isinstance(result, tuple)
        devices, from_cache = result
        assert from_cache is False
        assert len(devices) == 1

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_return_cache_status_with_cached_data(self, mock_cache):
        """Line 218: return devices, from_cache when cache hit + return_cache_status=True."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        cached_devices = [{"device_id": 1}]
        mock_cache.get.return_value = cached_devices
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300

        result = get_librenms_devices_for_import(api, return_cache_status=True)
        assert isinstance(result, tuple)
        devices, from_cache = result
        assert from_cache is True

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_api_failure_raises_from_the_return_cache_status_path(self, mock_cache):
        """A failed fetch raises, so return_cache_status never yields a misleading ([], False)."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.list_devices.return_value = (False, "Error")

        with pytest.raises(LibreNMSUnreachable, match="Error"):
            get_librenms_devices_for_import(api, return_cache_status=True)


class TestCacheKeyServerKeyIsolation:
    """Test that cache keys are isolated per server key (Thread 38)."""

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_cache_key_uses_api_server_key(self, mock_cache):
        """Different server_keys produce different cache keys."""
        from unittest.mock import MagicMock

        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        mock_cache.get.return_value = None
        api1 = MagicMock()
        api1.server_key = "server1"
        api1.cache_timeout = 300
        api2 = MagicMock()
        api2.server_key = "server2"
        api2.cache_timeout = 300
        api1.list_devices.return_value = (True, [])
        api2.list_devices.return_value = (True, [])

        get_librenms_devices_for_import(api1, filters={})
        get_librenms_devices_for_import(api2, filters={})

        assert mock_cache.set.call_count == 2
        keys = [call.args[0] for call in mock_cache.set.call_args_list]
        assert keys[0] != keys[1]
        assert any("server1" in k for k in keys)
        assert any("server2" in k for k in keys)
