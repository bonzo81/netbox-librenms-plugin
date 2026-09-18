"""Bulk import behaviour when the LibreNMS server cannot be reached.

The primary home for process_device_filters is test_coverage_bulk_import.py. These cases
live in their own file so they do not collide at that shared file's tail when the stack
is restacked.

The run must stop and say why, instead of returning an empty device list that looks like
"LibreNMS has no devices matching your filter". LibreNMS answers a search that matches
nothing with 200 and an empty list, so a failed fetch is never an empty result.
"""

import socket

import pytest

from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

SEARCH_FILTERS = {"hostname": "edge"}


def _dead_port():
    """Bind and release a loopback port so connecting to it is refused, not filtered."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _api_pointing_nowhere(settings, monkeypatch):
    """Return a real LibreNMSAPI bound to a port nothing listens on, and that port."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    port = _dead_port()
    configure_librenms_servers(
        settings,
        {
            "default": {
                "librenms_url": f"http://127.0.0.1:{port}",
                "api_token": "token",
                "cache_timeout": 300,
                "verify_ssl": False,
            }
        },
    )
    return LibreNMSAPI(server_key="default"), port


def _api_for(settings, server, cache_timeout=300):
    """Bind a real LibreNMSAPI to the loopback server."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    configure_librenms_servers(
        settings,
        {
            "default": {
                "librenms_url": server.url,
                "api_token": "token",
                "cache_timeout": cache_timeout,
            }
        },
    )
    return LibreNMSAPI(server_key="default")


@pytest.mark.django_db
class TestUnreachableLibreNMS:
    """An unreachable server must abort the run with the real reason."""

    def test_an_unreachable_server_aborts_the_run(self, settings, monkeypatch):
        """A refused connection must raise, not return an empty device list."""
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters
        from netbox_librenms_plugin.librenms_api import LibreNMSUnreachable

        api, port = _api_pointing_nowhere(settings, monkeypatch)

        with pytest.raises(LibreNMSUnreachable) as excinfo:
            process_device_filters(
                api,
                filters=SEARCH_FILTERS,
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=False,
            )

        # The message carries the endpoint that did not answer, not a generic string.
        assert str(port) in str(excinfo.value)

    def test_a_healthy_server_with_no_matches_returns_an_empty_list(self, settings, librenms_server):
        """A search that matches nothing is a result, not a failure."""
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

        librenms_server.register("/api/v0/devices", {"status": "ok", "devices": []})

        result = process_device_filters(
            _api_for(settings, librenms_server),
            filters=SEARCH_FILTERS,
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=False,
        )

        assert result == []

    def test_a_search_never_asks_the_system_endpoint(self, settings, librenms_server):
        """Reachability is decided by the device fetch itself, so no extra round trip is made."""
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

        librenms_server.register("/api/v0/devices", {"status": "ok", "devices": []})

        process_device_filters(
            _api_for(settings, librenms_server),
            filters=SEARCH_FILTERS,
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=False,
        )

        assert [request["path"] for request in librenms_server.requests] == ["/api/v0/devices"]


@pytest.mark.django_db
class TestFailedFetchIsNotCached:
    """A fetch failure is an error, never a cached empty result."""

    def _register_flapping_devices(self, server, state):
        """Serve a fault until *state* says healthy, then serve one device."""

        def devices(**_request):
            if state["healthy"]:
                return 200, {"status": "ok", "devices": [{"device_id": 4242, "hostname": "edge-01"}]}
            return 500, {"status": "error", "message": "boom"}

        server.register("/api/v0/devices", devices)

    def test_a_failed_fetch_raises_instead_of_returning_an_empty_list(self, settings, librenms_server):
        """The fetch itself reports the fault, so the caller cannot mistake it for no matches."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import
        from netbox_librenms_plugin.librenms_api import LibreNMSUnreachable

        self._register_flapping_devices(librenms_server, {"healthy": False})

        with pytest.raises(LibreNMSUnreachable):
            get_librenms_devices_for_import(_api_for(settings, librenms_server), filters=SEARCH_FILTERS)

    def test_a_failed_fetch_is_not_cached_so_the_next_attempt_sees_the_recovery(self, settings, librenms_server):
        """Caching the failure as [] hid a live server behind 'no devices match your filter'."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import
        from netbox_librenms_plugin.librenms_api import LibreNMSUnreachable

        state = {"healthy": False}
        self._register_flapping_devices(librenms_server, state)
        api = _api_for(settings, librenms_server)

        with pytest.raises(LibreNMSUnreachable):
            get_librenms_devices_for_import(api, filters=SEARCH_FILTERS)

        state["healthy"] = True
        recovered = get_librenms_devices_for_import(api, filters=SEARCH_FILTERS)

        assert [device["device_id"] for device in recovered] == [4242]

    def test_an_empty_result_is_still_cached(self, settings, librenms_server):
        """Only failures stop being cached: a real empty answer must not re-query LibreNMS."""
        from netbox_librenms_plugin.import_utils.filters import get_librenms_devices_for_import

        librenms_server.register("/api/v0/devices", {"status": "ok", "devices": []})
        api = _api_for(settings, librenms_server)

        assert get_librenms_devices_for_import(api, filters=SEARCH_FILTERS) == []
        assert get_librenms_devices_for_import(api, filters=SEARCH_FILTERS) == []

        assert [request["path"] for request in librenms_server.requests] == ["/api/v0/devices"]
