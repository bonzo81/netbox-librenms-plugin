"""Behavioral tests for the loopback LibreNMS test server."""

import requests

from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


def test_invalid_utf8_json_body_reaches_the_registered_route(monkeypatch):
    """Malformed UTF-8 must be replacement-decoded instead of closing the connection."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    received = []

    def respond(**request):
        received.append(request["body"])
        return 200, {"status": "ok"}

    with librenms_mock_server() as server:
        server.register("/invalid-json", respond, method="POST")

        response = requests.post(
            f"{server.url}/invalid-json",
            data=b"\xff",
            headers={"Content-Type": "application/json"},
            timeout=2,
        )

    assert response.status_code == 200
    assert received == ["\ufffd"]
