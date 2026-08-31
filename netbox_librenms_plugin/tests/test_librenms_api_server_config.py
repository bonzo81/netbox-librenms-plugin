"""Usable-server-config filtering for LibreNMSAPI, driven by real plugin settings.

These focused tests drive the
``netbox.plugins.get_plugin_config`` -> ``settings.PLUGINS_CONFIG`` path via
``override_settings``. They cross-check the reusable settings fixture in
``test_librenms_api.py`` and exercise the usable-config predicate end to end.
"""

import copy

from django.conf import settings
from django.test import override_settings

from netbox_librenms_plugin.librenms_api import LibreNMSAPI


def _plugins_config_with_servers(servers):
    """Return a PLUGINS_CONFIG copy with the plugin's ``servers`` set to ``servers``."""
    config = copy.deepcopy(settings.PLUGINS_CONFIG)
    plugin_config = dict(config.get("netbox_librenms_plugin", {}))
    plugin_config["servers"] = servers
    config["netbox_librenms_plugin"] = plugin_config
    return config


class TestGetAvailableServersUsableConfig:
    """get_available_servers must only offer servers that __init__ can actually bind."""

    def test_skips_unusable_legacy_default(self):
        """Legacy mode does not offer a default server without both URL and token."""
        config = copy.deepcopy(settings.PLUGINS_CONFIG)
        plugin_config = dict(config.get("netbox_librenms_plugin", {}))
        plugin_config.update({"servers": {}, "librenms_url": "", "api_token": ""})
        config["netbox_librenms_plugin"] = plugin_config

        with override_settings(PLUGINS_CONFIG=config):
            assert LibreNMSAPI.get_available_servers() == {}

    def test_skips_incomplete_and_malformed_entries(self):
        """Non-mapping entries and dicts missing url/token are excluded from the picker."""
        servers = {
            "good": {
                "librenms_url": "https://good.example.com",
                "api_token": "good-token",
                "display_name": "Good",
            },
            "nomap": None,
            "nourl": {"display_name": "No URL", "api_token": "t"},
            "notoken": {"display_name": "No Token", "librenms_url": "https://n.example.com"},
        }
        with override_settings(PLUGINS_CONFIG=_plugins_config_with_servers(servers)):
            assert LibreNMSAPI.get_available_servers() == {"good": "Good"}

    def test_keeps_fully_configured_entries(self):
        """A server with a non-empty librenms_url and api_token remains selectable."""
        servers = {
            "primary": {
                "librenms_url": "https://primary.example.com",
                "api_token": "primary-token",
            }
        }
        with override_settings(PLUGINS_CONFIG=_plugins_config_with_servers(servers)):
            assert LibreNMSAPI.get_available_servers() == {"primary": "primary"}


class TestInitDefaultFallbackUsableConfig:
    """The 'default' auto-fallback must select the first usable configured server."""

    def test_skips_incomplete_first_server(self):
        """A partially configured first server is skipped so a later valid server is bound."""
        servers = {
            "broken": {"display_name": "Broken"},  # no librenms_url/api_token
            "primary": {
                "librenms_url": "https://primary.example.com",
                "api_token": "primary-token",
            },
        }
        with override_settings(PLUGINS_CONFIG=_plugins_config_with_servers(servers)):
            api = LibreNMSAPI(server_key="default")
        assert api.server_key == "primary"
        assert api.librenms_url == "https://primary.example.com"
