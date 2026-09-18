"""Helper fixtures for LibreNMS API tests."""

from unittest.mock import patch

import pytest


@pytest.fixture
def mock_librenms_config():
    """Provide the configured LibreNMS boundary only to tests that request it."""
    with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_config:
        # Default config
        mock_config.return_value = {
            "default": {
                "librenms_url": "https://librenms.example.com",
                "api_token": "test-token",
                "cache_timeout": 300,
                "verify_ssl": True,
            }
        }

        yield {"mock_config": mock_config}
