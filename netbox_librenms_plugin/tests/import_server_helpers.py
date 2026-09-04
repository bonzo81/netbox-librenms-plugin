"""Shared LibreNMS stubs for the multi-server request tests.

The import and object-page cohorts drive the same two-server configuration, the same device
payloads and the same rendered selector, so the stub shape lives here rather than in each module.
"""

import json

from requests import Response

from netbox_librenms_plugin.tests.conftest import configure_librenms_servers


def configure_servers(settings):
    """Configure two usable LibreNMS servers, primary and secondary."""
    configure_librenms_servers(
        settings,
        {
            "primary": {
                "display_name": "Primary LibreNMS",
                "librenms_url": "https://primary.example.com",
                "api_token": "test-token",
            },
            "secondary": {
                "display_name": "Secondary LibreNMS",
                "librenms_url": "https://secondary.example.com",
                "api_token": "test-token",
            },
        },
    )


def json_response(url, payload, status=200):
    """Build one JSON LibreNMS API response."""
    response = Response()
    response.status_code = status
    response.url = url
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    return response


def librenms_device(device_id, hostname):
    """Build one minimal LibreNMS device payload."""
    return {
        "device_id": device_id,
        "hostname": hostname,
        "sysName": hostname,
        "location": "",
        "hardware": "",
        "os": "linux",
        "type": "server",
        "serial": "",
        "ip": None,
        "disabled": 0,
        "status": 1,
    }


def device_info_response(url, device_id, name):
    """Build one LibreNMS device-detail response."""
    return json_response(
        url,
        {
            "status": "ok",
            "devices": [
                {
                    "device_id": device_id,
                    "sysName": name,
                    "hostname": name,
                    "hardware": "Test appliance",
                }
            ],
        },
    )


def selector_html(html):
    """Return the rendered server selector region of a page."""
    start = html.index('id="librenms-server-selector"')
    return html[start : html.index("</ul>", start)]
