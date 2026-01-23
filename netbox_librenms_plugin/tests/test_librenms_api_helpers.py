"""Helper fixtures for LibreNMS API tests."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def mock_librenms_config():
    """Auto-mock LibreNMS configuration for all API tests."""
    with (
        patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_config,
        patch("netbox_librenms_plugin.models.LibreNMSSettings") as mock_settings,
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
        mock_settings.objects.filter.return_value.first.return_value = None

        yield {"mock_config": mock_config, "mock_settings": mock_settings}
