"""Behavior tests for LibreNMS import filtering through real HTTP and cache backends."""

import pytest


def _register_devices(live_librenms, devices, *, status=200):
    payload = {"status": "ok", "devices": devices} if status == 200 else {"status": "error", "message": "failed"}
    live_librenms.server.register("/api/v0/devices", payload, status=status, method="GET")
    live_librenms.api.cache_timeout = 300


def _last_query(live_librenms):
    return live_librenms.server.requests[-1]["query"]


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
    def test_counts_real_api_rows_and_can_hide_disabled_devices(self, live_librenms):
        from netbox_librenms_plugin.import_utils.filters import get_device_count_for_filters

        _register_devices(
            live_librenms,
            [
                {"device_id": 1, "disabled": 0},
                {"device_id": 2, "disabled": "true"},
                {"device_id": 3, "disabled": None},
            ],
        )

        assert get_device_count_for_filters(live_librenms.api, {}, show_disabled=True) == 3
        assert get_device_count_for_filters(live_librenms.api, {}, show_disabled=False) == 2

    def test_clear_cache_fetches_a_changed_http_response(self, live_librenms):
        from netbox_librenms_plugin.import_utils.filters import get_device_count_for_filters

        _register_devices(live_librenms, [{"device_id": 1}])
        assert get_device_count_for_filters(live_librenms.api, {}) == 1
        _register_devices(live_librenms, [{"device_id": 1}, {"device_id": 2}])

        assert get_device_count_for_filters(live_librenms.api, {}) == 1
        assert get_device_count_for_filters(live_librenms.api, {}, clear_cache=True) == 2


class TestGetLibreNMSDevicesForImport:
    @pytest.mark.parametrize(
        ("filters", "expected_query"),
        [
            ({"status": "1"}, {"type": ["up"]}),
            ({"status": "0"}, {"type": ["down"]}),
            ({"location": "10"}, {"type": ["location_id"], "query": ["10"]}),
            ({"type": "network"}, {"type": ["type"], "query": ["network"]}),
            ({"os": "ios"}, {"type": ["os"], "query": ["ios"]}),
            ({"hostname": "router-01"}, {"type": ["hostname"], "query": ["router-01"]}),
            ({"sysname": "core-01"}, {"type": ["sysName"], "query": ["core-01"]}),
            ({"status": "invalid"}, {}),
        ],
    )
    def test_filter_priority_is_visible_on_the_real_http_query(self, live_librenms, filters, expected_query):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        _register_devices(live_librenms, [{"device_id": 1}])

        assert get_librenms_devices_for_import(live_librenms.api, filters=filters) == [{"device_id": 1}]
        assert _last_query(live_librenms) == expected_query

    def test_hardware_filter_is_applied_to_real_response_rows(self, live_librenms):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        _register_devices(
            live_librenms,
            [
                {"device_id": 1, "hardware": "Example C9300"},
                {"device_id": 2, "hardware": "Other model"},
                {"device_id": 3, "hardware": None},
            ],
        )

        result = get_librenms_devices_for_import(live_librenms.api, filters={"hardware": "c9300"})

        assert [device["device_id"] for device in result] == [1]
        assert _last_query(live_librenms) == {}

    @pytest.mark.parametrize(
        ("filters", "api_query"),
        [
            (
                {"status": "1", "location": "5", "type": "server", "os": "linux"},
                {"type": ["up"]},
            ),
            (
                {"location": "5", "type": "server", "os": "linux", "hardware": "model-a"},
                {"type": ["location_id"], "query": ["5"]},
            ),
            (
                {"type": "server", "os": "linux", "hostname": "node-01"},
                {"type": ["type"], "query": ["server"]},
            ),
            (
                {"os": "linux", "hostname": "node-01", "sysname": "node-01"},
                {"type": ["os"], "query": ["linux"]},
            ),
            (
                {"hostname": "node-01", "sysname": "node-01", "hardware": "model-a"},
                {"type": ["hostname"], "query": ["node-01"]},
            ),
            (
                {"sysname": "node-01", "hardware": "model-a"},
                {"type": ["sysName"], "query": ["node-01"]},
            ),
        ],
    )
    def test_remaining_filters_are_applied_client_side(self, live_librenms, filters, api_query):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        _register_devices(
            live_librenms,
            [
                {
                    "device_id": 1,
                    "location_id": 5,
                    "type": "server",
                    "os": "linux",
                    "hostname": "node-01.example",
                    "sysName": "node-01",
                    "hardware": "model-a",
                },
                {
                    "device_id": 2,
                    "location_id": 9,
                    "type": "network",
                    "os": "other",
                    "hostname": "node-02.example",
                    "sysName": "node-02",
                    "hardware": "model-b",
                },
            ],
        )

        result = get_librenms_devices_for_import(live_librenms.api, filters=filters)

        assert [device["device_id"] for device in result] == [1]
        assert _last_query(live_librenms) == api_query

    def test_force_refresh_replaces_a_real_cached_result(self, live_librenms):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        _register_devices(live_librenms, [{"device_id": 1}])
        first = get_librenms_devices_for_import(live_librenms.api)
        _register_devices(live_librenms, [{"device_id": 2}])

        cached, from_cache = get_librenms_devices_for_import(live_librenms.api, return_cache_status=True)
        fresh, fresh_from_cache = get_librenms_devices_for_import(
            live_librenms.api,
            force_refresh=True,
            return_cache_status=True,
        )

        assert first == cached == [{"device_id": 1}]
        assert from_cache is True
        assert fresh == [{"device_id": 2}]
        assert fresh_from_cache is False

    def test_http_failure_raises_and_is_not_cached(self, live_librenms):
        """Caching the fault as [] hid a live outage behind "no devices match your filter"."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import
        from netbox_librenms_plugin.librenms_api import LibreNMSUnreachable

        _register_devices(live_librenms, [], status=500)

        with pytest.raises(LibreNMSUnreachable):
            get_librenms_devices_for_import(live_librenms.api, return_cache_status=True)

        # The failure left nothing behind, so the next attempt asks LibreNMS again.
        live_librenms.server.requests.clear()
        _register_devices(live_librenms, [{"device_id": 11}])

        assert get_librenms_devices_for_import(live_librenms.api) == [{"device_id": 11}]
        assert live_librenms.server.requests != []

    def test_disconnected_server_fails_closed(self, live_librenms):
        """Failing closed means saying why, not returning a list that reads as no matches."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import
        from netbox_librenms_plugin.librenms_api import LibreNMSUnreachable

        live_librenms.api.cache_timeout = 300
        live_librenms.server.register_disconnect("/api/v0/devices", method="GET")

        with pytest.raises(LibreNMSUnreachable):
            get_librenms_devices_for_import(live_librenms.api)

    def test_omitted_api_is_built_from_real_plugin_settings(self, live_librenms):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        _register_devices(live_librenms, [{"device_id": 7}])

        assert get_librenms_devices_for_import(server_key="default") == [{"device_id": 7}]

    def test_cache_namespace_isolated_by_resolved_server_key(self, live_librenms):
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import
        from netbox_librenms_plugin.tests.conftest import make_recording_api
        from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

        _register_devices(live_librenms, [{"device_id": 1}])
        with librenms_mock_server() as second_server:
            second_server.register(
                "/api/v0/devices",
                {"status": "ok", "devices": [{"device_id": 2}]},
                method="GET",
            )
            second_api = make_recording_api(second_server.url, server_key="secondary")
            second_api.cache_timeout = 300

            assert get_librenms_devices_for_import(live_librenms.api) == [{"device_id": 1}]
            assert get_librenms_devices_for_import(second_api) == [{"device_id": 2}]


class TestApplyClientFilters:
    @pytest.mark.parametrize(
        ("filters", "expected_ids"),
        [
            ({"location": "10"}, [1]),
            ({"type": "network"}, [1]),
            ({"os": "ios"}, [1]),
            ({"hostname": "router"}, [1]),
            ({"sysname": "core"}, [1]),
            ({"hardware": "c9300"}, [1]),
            ({}, [1, 2, 3]),
        ],
    )
    def test_filters_defensive_real_response_shapes(self, filters, expected_ids):
        from netbox_librenms_plugin.import_utils.filters import _apply_client_filters

        devices = [
            {
                "device_id": 1,
                "location_id": 10,
                "type": "network",
                "os": "ios-xe",
                "hostname": "router-01.example",
                "sysName": "core-01",
                "hardware": "Example C9300",
            },
            {
                "device_id": 2,
                "location_id": 20,
                "type": "server",
                "os": "linux",
                "hostname": "server-01.example",
                "sysName": "server-01",
                "hardware": "Example server",
            },
            {"device_id": 3, "hardware": None},
        ]

        assert [device["device_id"] for device in _apply_client_filters(devices, filters)] == expected_ids
