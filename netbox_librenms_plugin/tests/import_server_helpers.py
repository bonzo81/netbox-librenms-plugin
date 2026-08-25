"""Shared LibreNMS stubs for the multi-server import request tests.

Two modules drive the same two-server import configuration and the same device payload, so the
stub shape lives here rather than in either test module.
"""

import json
from copy import deepcopy

from requests import Response


def configure_servers(settings):
    """Configure two usable LibreNMS servers, primary and secondary."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
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
    }
    settings.PLUGINS_CONFIG = plugin_config


def json_response(url, payload):
    """Build one JSON LibreNMS API response."""
    response = Response()
    response.status_code = 200
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
