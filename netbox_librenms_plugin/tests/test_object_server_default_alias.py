"""The stale ``server_key=default`` alias on an object sync page (see test_object_server_selection.py)."""

from copy import deepcopy

import pytest
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

DEVICE_ID = 13501


@pytest.fixture
def primary_only(settings, monkeypatch):
    """Configure one usable LibreNMS server whose key is not ``default``."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_settings = plugin_config["netbox_librenms_plugin"]
        plugin_settings["servers"] = {
            "primary": {
                "display_name": "Primary LibreNMS",
                "librenms_url": server.url,
                "api_token": "default-alias-token",
                "cache_timeout": 300,
                "verify_ssl": False,
            }
        }
        plugin_settings.pop("librenms_url", None)
        plugin_settings.pop("api_token", None)
        settings.PLUGINS_CONFIG = plugin_config
        yield server


@pytest.mark.django_db
def test_a_stale_default_server_key_resolves_to_the_installation_default(client, primary_only):
    """A ?server_key=default link from a single-server install must still open the page."""
    device = make_device("default-alias-device", librenms_cf={"primary": {"id": DEVICE_ID}})
    observed = []

    def device_lookup(**request):
        observed.append(request)
        return 200, {
            "status": "ok",
            "devices": [
                {
                    "device_id": DEVICE_ID,
                    "sysName": device.name,
                    "hostname": device.name,
                    "hardware": "Test appliance",
                }
            ],
        }

    primary_only.register(f"/api/v0/devices/{DEVICE_ID}", device_lookup)
    client.force_login(make_superuser("default-alias-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    response = client.get(url, {"server_key": "default"})

    assert response.status_code == 200
    html = response.content.decode()
    assert 'data-active-server-key="primary"' in html
    assert "is not an available mapping for this object" not in html
    assert [(request["method"], request["path"]) for request in observed] == [("GET", f"/api/v0/devices/{DEVICE_ID}")]
