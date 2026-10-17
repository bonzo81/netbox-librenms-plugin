"""Behavioral tests for the LibreNMS HTTP test helpers."""

import pytest

from netbox_librenms_plugin.tests.mock_librenms_server import LibreNMSStubServer, _LibreNMSHandler


@pytest.mark.parametrize("payload", [[], False, 0, ""])
def test_response_factory_preserves_supplied_falsy_json(mock_response_factory, payload):
    """The response factory must replace only a missing JSON payload."""
    assert mock_response_factory(json_data=payload).json() == payload


def test_stub_reads_a_bare_list_response_as_its_body():
    """A bare [status, body] list is a 2xx list body, as unwrap_response reads it, so the stub has no device to serve."""
    device_body = {"status": "ok", "devices": [{"hostname": "device-4244.example.test"}]}
    recording = {"device_id": 4244, "responses": {"GET /api/v0/devices/4244": [200, device_body]}}

    with pytest.raises(ValueError, match="no usable"):
        LibreNMSStubServer(recordings=[recording], api_token="dev-stub-token")


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


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH"])
def test_default_port_stack_route_accepts_only_get(librenms_server, method):
    import requests

    librenms_server.ports_response(42)
    response = requests.request(method, f"{librenms_server.url}/api/v0/devices/42/port_stack", timeout=5)

    assert response.status_code == (200 if method == "GET" else 404)
    if method == "GET":
        assert response.json()["mappings"] == []
