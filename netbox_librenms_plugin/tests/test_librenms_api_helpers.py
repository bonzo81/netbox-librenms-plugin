"""Helper fixtures for LibreNMS API tests."""

from unittest.mock import patch

import pytest


@pytest.fixture
def mock_librenms_config():
    """Mock the LibreNMS configuration boundaries for tests that request this fixture."""
    with (
        patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_config,
        patch("netbox_librenms_plugin.librenms_api._get_selected_server_key", return_value=None) as mock_settings,
    ):
        # Default config
        mock_config.return_value = {
            "default": {
                "librenms_url": "https://librenms.example.com",
                "api_token": "test-token",
                "cache_timeout": 300,
                "verify_ssl": True,
            }
        }

        yield {"mock_config": mock_config, "mock_settings": mock_settings}
