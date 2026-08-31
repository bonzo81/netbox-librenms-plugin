"""Behavioral tests for the LibreNMS HTTP test helpers."""

import pytest

from netbox_librenms_plugin.tests.mock_librenms_server import _LibreNMSHandler


@pytest.mark.parametrize("payload", [[], False, 0, ""])
def test_response_factory_preserves_supplied_falsy_json(mock_response_factory, payload):
    """The response factory must replace only a missing JSON payload."""
    assert mock_response_factory(json_data=payload).json() == payload


@pytest.mark.parametrize("disconnect_error", [BrokenPipeError(), ConnectionResetError()])
def test_mock_server_ignores_disconnect_during_header_write(disconnect_error):
    """A client disconnect during header flushing must not escape the server thread."""

    class HeaderDisconnectHandler(_LibreNMSHandler):
        def __init__(self):
            self.disconnect_error = disconnect_error

        def send_response(self, status):
            pass

        def send_header(self, name, value):
            pass

        def end_headers(self):
            raise self.disconnect_error

    HeaderDisconnectHandler()._send_json(200, {"status": "ok"})
