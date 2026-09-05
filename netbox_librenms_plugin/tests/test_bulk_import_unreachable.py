"""Bulk import behaviour when the LibreNMS server cannot be reached.

The primary home for process_device_filters is test_coverage_bulk_import.py. These cases
live in their own file so they do not collide at that shared file's tail when the stack
is restacked.

The run must stop and say why, instead of returning an empty device list that looks like
"LibreNMS has no devices matching your filter".
"""

import socket

import pytest

from netbox_librenms_plugin.tests.conftest import configure_librenms_servers


def _dead_port():
    """Bind and release a loopback port so connecting to it is refused, not filtered."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _api_pointing_nowhere(settings, monkeypatch):
    """A real LibreNMSAPI bound to a port nothing listens on."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    configure_librenms_servers(
        settings,
        {
            "default": {
                "librenms_url": f"http://127.0.0.1:{_dead_port()}",
                "api_token": "token",
                "cache_timeout": 0,
                "verify_ssl": False,
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

        api = _api_pointing_nowhere(settings, monkeypatch)

        with pytest.raises(LibreNMSUnreachable) as excinfo:
            process_device_filters(
                api,
                filters={"hostname": "anything"},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=False,
            )

        assert "Connection failed" in str(excinfo.value)

    def test_the_abort_happens_before_any_device_work(self, settings, monkeypatch):
        """The pre-flight runs first, so no partial result is built and cached."""
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters
        from netbox_librenms_plugin.librenms_api import LibreNMSUnreachable

        api = _api_pointing_nowhere(settings, monkeypatch)
        calls = []
        monkeypatch.setattr(
            "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
            lambda *a, **k: calls.append(1) or ([], False),
        )

        with pytest.raises(LibreNMSUnreachable):
            process_device_filters(
                api,
                filters={"hostname": "anything"},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=False,
            )

        assert calls == []

    def test_a_reachable_server_is_not_blocked_by_the_preflight(self, settings, librenms_server):
        """The pre-flight must not turn a healthy server into a failure."""
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        librenms_server.routes["/api/v0/system"] = (200, {"status": "ok", "system": [{"version": "24.1.0"}]})
        librenms_server.routes["/api/v0/devices"] = (200, {"status": "ok", "devices": []})
        configure_librenms_servers(
            settings,
            {"default": {"librenms_url": librenms_server.url, "api_token": "token", "cache_timeout": 0}},
        )

        result = process_device_filters(
            LibreNMSAPI(server_key="default"),
            filters={"hostname": "nothing-matches"},
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=False,
        )

        assert result == []
