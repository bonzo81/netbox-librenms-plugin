"""
Comprehensive tests for LibreNMSAPI client.

This module provides 100% test coverage for netbox_librenms_plugin/librenms_api.py,
with particular focus on HTTP method correctness to prevent regression bugs.
"""

from unittest.mock import MagicMock, patch

import re

import pytest
import requests


from netbox_librenms_plugin.tests import test_librenms_api_helpers

# Bind the helper's autouse fixture into this module so it patches the config here only.
# `pytest_plugins` would register it session-wide and shadow PLUGINS_CONFIG for later tests.
mock_librenms_config = test_librenms_api_helpers.mock_librenms_config


# =============================================================================
# Test Class 1: Initialization (3 tests)
# =============================================================================


class TestLibreNMSAPIInit:
    """Test LibreNMSAPI initialization and configuration loading."""

    def test_init_with_multi_server_config(self, mock_librenms_config):
        """Verify initialization with multi-server configuration."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        assert api.librenms_url == "https://librenms.example.com"
        assert api.api_token == "test-token"
        assert api.cache_timeout == 300
        assert api.verify_ssl is True

    def test_init_with_legacy_config(self, mock_librenms_config):
        """Verify initialization with legacy single-server config."""
        mock_config = mock_librenms_config["mock_config"]

        # Return None for 'servers' to trigger legacy path
        def config_side_effect(plugin, key, default=None):
            if key == "servers":
                return None
            legacy = {
                "librenms_url": "https://legacy.example.com",
                "api_token": "legacy-token",
                "cache_timeout": 600,
                "verify_ssl": False,
            }
            return legacy.get(key, default)

        mock_config.side_effect = config_side_effect

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI()

        assert api.librenms_url == "https://legacy.example.com"

    def test_legacy_mode_normalizes_server_key_to_default(self, mock_librenms_config):
        """In legacy single-server mode a posted/stale server_key must not survive as api.server_key (it would become a bogus cache/redirect discriminator)."""
        mock_config = mock_librenms_config["mock_config"]

        def config_side_effect(plugin, key, default=None):
            if key == "servers":
                return None  # legacy single-server mode
            legacy = {"librenms_url": "https://legacy.example.com", "api_token": "legacy-token"}
            return legacy.get(key, default)

        mock_config.side_effect = config_side_effect

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        # A non-default (stale/tampered) key in legacy mode must normalize to "default".
        api = LibreNMSAPI(server_key="ghost")
        assert api.server_key == "default"

    def test_init_non_string_server_key_falls_back_cleanly(self, mock_librenms_config):
        """An unhashable non-string server_key (e.g. a list) is treated as unset, not raised on at the dict membership check."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        # Pre-fix: `[] not in servers_config` raised TypeError (unhashable type: 'list').
        api = LibreNMSAPI(server_key=[])

        assert api.server_key == "default"
        assert api.librenms_url == "https://librenms.example.com"

    def test_init_fallback_skips_incomplete_server_entry(self, mock_librenms_config):
        """The auto-default fallback skips a structurally incomplete entry (dict without url/token) and picks the first usable server."""
        mock_config = mock_librenms_config["mock_config"]

        def config_side_effect(plugin, key, default=None):
            if key == "servers":
                # 'bad' is a dict but carries no url/token; 'prod' is usable.
                return {"bad": {}, "prod": {"librenms_url": "https://prod.example.com", "api_token": "prod-token"}}
            return default

        mock_config.side_effect = config_side_effect

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        # 'default' isn't configured → auto-default fallback; it must skip 'bad' and pick 'prod'.
        api = LibreNMSAPI(server_key="default")

        assert api.server_key == "prod"
        assert api.librenms_url == "https://prod.example.com"

    def test_init_missing_config_raises_valueerror(self, mock_librenms_config):
        """Verify ValueError raised when configuration is missing."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = None

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        # Use the default key so this exercises the missing URL/token ValueError path in legacy
        # mode (no configured 'servers' dict, no librenms_url/api_token). Pin the message so an
        # unrelated ValueError (e.g. the misconfigured-mapping guard) can't keep this test green.
        with pytest.raises(ValueError, match=r"URL or API token is not configured for server 'default'"):
            LibreNMSAPI(server_key="default")

    def test_init_nonexistent_server_key_raises_keyerror(self, mock_librenms_config):
        """Verify KeyError raised when specific server_key doesn't exist."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with pytest.raises(KeyError, match="nonexistent"):
            LibreNMSAPI(server_key="nonexistent")

    def test_init_legacy_mode_binds_single_server_for_non_default_key(self, mock_librenms_config):
        """Legacy single-server mode has only the implicit 'default' server, bound to the single configured URL/token."""
        mock_config = mock_librenms_config["mock_config"]

        def config_side_effect(plugin, key, default=None):
            if key == "servers":
                return None  # legacy single-server mode (no multi-server dict)
            legacy = {"librenms_url": "https://legacy.example.com", "api_token": "legacy-token"}
            return legacy.get(key, default)

        mock_config.side_effect = config_side_effect

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="stale")  # must not raise in legacy mode
        assert api.librenms_url == "https://legacy.example.com"

        # The default key must still work in legacy mode.
        assert LibreNMSAPI(server_key="default").librenms_url == "https://legacy.example.com"

    def test_init_default_falls_back_to_first_server(self, mock_librenms_config):
        """Verify 'default' key falls back to first configured server."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = {
            "primary": {
                "librenms_url": "https://primary.example.com",
                "api_token": "primary-token",
            }
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        assert api.server_key == "primary"
        assert api.librenms_url == "https://primary.example.com"

    def test_init_stale_auto_selected_server_falls_back(self, mock_librenms_config):
        """Issue #110: a stale LibreNMSSettings.selected_server (no longer configured) must fall back to the first server rather than hard-fail — it was auto-resolved, not explicitly requested, so the KeyError guard must not fire."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = {
            "primary": {"librenms_url": "https://primary.example.com", "api_token": "t"},
        }
        mock_settings = mock_librenms_config["mock_settings"]
        mock_settings.objects.first.return_value.selected_server = "gone-server"

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI()  # server_key=None -> auto-resolved from (stale) settings
        assert api.server_key == "primary"

    def test_init_blank_server_key_treated_as_non_explicit_falls_back(self, mock_librenms_config):
        """A blank/whitespace server_key is 'no key': a stale selected_server must still fall back, not hard-fail. Treating '' as explicit would mark the auto-resolved key explicit and defeat the issue #110 fallback (KeyError)."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = {
            "primary": {"librenms_url": "https://primary.example.com", "api_token": "t"},
        }
        mock_settings = mock_librenms_config["mock_settings"]
        mock_settings.objects.first.return_value.selected_server = "gone-server"

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        for blank in ("", "   "):
            api = LibreNMSAPI(server_key=blank)  # blank → auto-resolved from the (stale) settings
            assert api.server_key == "primary"

    def test_init_skips_leading_malformed_server_entry(self, mock_librenms_config):
        """Issue #110: the first-entry fallback must pick the first *valid* mapping, skipping a malformed (non-dict) leading entry that would otherwise raise at the config read."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = {
            "broken": "not-a-dict",
            "good": {"librenms_url": "https://good.example.com", "api_token": "t"},
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        assert api.server_key == "good"
        assert api.librenms_url == "https://good.example.com"

    def test_init_all_malformed_server_entries_raise_valueerror(self, mock_librenms_config):
        """Issue #110: when no configured entry is a valid mapping, raise a clear ValueError instead of crashing on the malformed entry's config read."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = {"broken": "not-a-dict", "also-bad": 123}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with pytest.raises(ValueError):
            LibreNMSAPI(server_key="default")

    def test_init_selected_key_malformed_raises_valueerror_not_typeerror(self, mock_librenms_config):
        """Issue #110: an explicitly requested key that *exists* but maps to a non-dict must raise a clear ValueError, not an opaque TypeError from indexing the string at the config read."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = {"broken": "not-a-dict"}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with pytest.raises(ValueError, match="expected a mapping"):
            LibreNMSAPI(server_key="broken")

    def test_get_available_servers_skips_malformed_entry(self, mock_librenms_config):
        """get_available_servers() powers the sync-POST server-key membership check."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = {
            "good": {"librenms_url": "https://good.example.com", "api_token": "t", "display_name": "Good"},
            "broken": "not-a-dict",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        servers = LibreNMSAPI.get_available_servers()

        assert servers == {"good": "Good"}
        assert "broken" not in servers

    def test_init_incomplete_dict_entry_raises_valueerror_not_keyerror(self, mock_librenms_config):
        """A dict-shaped but incomplete entry ({"bad": {}}) passes the isinstance check, so the config read must fail with ValueError, not an opaque KeyError on config['librenms_url']."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = {"bad": {}}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with pytest.raises(ValueError):
            LibreNMSAPI(server_key="bad")

    def test_get_available_servers_skips_incomplete_dict_entry(self, mock_librenms_config):
        """An incomplete dict entry (no librenms_url/api_token) must not be selectable — it would pass the membership check and then crash LibreNMSAPI construction."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = {
            "good": {"librenms_url": "https://good.example.com", "api_token": "t", "display_name": "Good"},
            "bad": {},  # dict-shaped but unusable
            "tokenless": {"librenms_url": "https://x.example.com"},  # url but no token
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        servers = LibreNMSAPI.get_available_servers()

        assert servers == {"good": "Good"}
        assert "bad" not in servers
        assert "tokenless" not in servers

    def test_init_non_mapping_server_config_raises_valueerror(self, mock_librenms_config):
        """A structurally invalid (non-mapping) server entry must raise ValueError, not leak a TypeError from the dict access — so build_librenms_api falls back to None."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = {"badserver": None}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI, build_librenms_api

        with pytest.raises(ValueError, match="misconfigured"):
            LibreNMSAPI(server_key="badserver")
        # build_librenms_api must convert that into a clean None, not a 500.
        assert build_librenms_api("badserver") is None


# =============================================================================
# Test Class 2: Connection Testing (4 tests)
# =============================================================================


class TestLibreNMSAPIConnection:
    """Test connection testing functionality."""

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_connection_success(self, mock_get, mock_librenms_config):
        """Verify successful connection test."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "system": [{"local_ver": "24.1.0"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.test_connection()

        assert result is not None
        assert "error" not in result
        mock_get.assert_called_once()
        assert "/api/v0/system" in mock_get.call_args[0][0]

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_connection_auth_failure_401(self, mock_get, mock_librenms_config):
        """Verify 401 unauthorized handling."""
        mock_get.return_value.status_code = 401

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.test_connection()

        assert result.get("error") is True

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_connection_auth_failure_403(self, mock_get, mock_librenms_config):
        """Verify 403 forbidden handling."""
        mock_get.return_value.status_code = 403

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.test_connection()

        assert result.get("error") is True

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_connection_timeout(self, mock_get, mock_librenms_config):
        """Verify timeout exception handling."""
        mock_get.side_effect = requests.exceptions.Timeout("Connection timed out")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.test_connection()

        assert result.get("error") is True
        assert "timeout" in result.get("message", "").lower()


# =============================================================================
# Test Class 3: HTTP Methods - CRITICAL (18 tests)
# =============================================================================


class TestLibreNMSAPIHttpMethods:
    """
    CRITICAL: Verify each API method uses the correct HTTP verb.

    These tests prevent regression bugs where HTTP methods are accidentally
    changed during refactoring (e.g., GET changed to DELETE).
    """

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_info_uses_get(self, mock_get, mock_post, mock_delete, mock_librenms_config):
        """Verify get_device_info uses GET, never DELETE."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 1}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_info(device_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()
        mock_post.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_device_uses_post(self, mock_post, mock_get, mock_delete, mock_librenms_config):
        """Verify add_device uses POST."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"status": "ok", "device_id": 42}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.add_device(
            data={
                "hostname": "test.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        mock_post.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.patch")
    def test_update_device_field_uses_patch(self, mock_patch, mock_delete, mock_librenms_config):
        """Verify update_device_field uses PATCH."""
        mock_patch.return_value.status_code = 200
        mock_patch.return_value.json.return_value = {"status": "ok"}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.update_device_field(device_id=1, field_data={"field": ["location"], "data": ["DC1"]})

        mock_patch.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_id_by_ip_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_device_id_by_ip uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 10}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_id_by_ip("192.168.1.1")

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_id_by_hostname_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_device_id_by_hostname uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 20}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_id_by_hostname("test-host")

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_ports_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_ports uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "ports": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_ports(device_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_locations_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_locations uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "locations": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_locations()

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_location_uses_post(self, mock_post, mock_delete, mock_librenms_config):
        """Verify add_location uses POST."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "status": "ok",
            "message": "Location created #1",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.add_location(location_data={"location": "DC1"})

        mock_post.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.patch")
    def test_update_location_uses_patch(self, mock_patch, mock_delete, mock_librenms_config):
        """Verify update_location uses PATCH."""
        mock_patch.return_value.status_code = 200
        mock_patch.return_value.json.return_value = {
            "status": "ok",
            "message": "Location updated",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.update_location(location_name="DC1", location_data={"location": "DC1-Updated"})

        mock_patch.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_links_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_device_links uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "links": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_links(device_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_ips_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_device_ips uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "addresses": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_ips(device_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_port_by_id_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_port_by_id uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "port": [{}]}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_port_by_id(port_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_inventory_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_device_inventory uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "inventory": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_inventory(device_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_poller_groups_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_poller_groups uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "groups": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_poller_groups()

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_inventory_filtered_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_inventory_filtered uses GET."""
        mock_get.return_value.status_code = 200
        # Return non-empty inventory so it doesn't fall back to get_device_inventory
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "inventory": [{"entPhysicalClass": "chassis"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_inventory_filtered(device_id=1, ent_physical_class="chassis")

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_list_devices_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify list_devices uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "devices": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.list_devices()

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_test_connection_uses_get(self, mock_get, mock_librenms_config):
        """Verify test_connection uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok"}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.test_connection()

        mock_get.assert_called_once()


# ====================================================================================
# Test Class 4: Device Lookup (6 tests)
# ====================================================================================


class TestLibreNMSAPIDeviceLookup:
    """Test device lookup functionality."""

    def test_get_librenms_id_from_custom_field(self, mock_librenms_config):
        """Returns ID when already stored in cf['librenms_id']."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        device = MagicMock()
        device.name = "test-device"
        device.cf = {"librenms_id": 42}

        result = api.get_librenms_id(device)
        assert result == 42

    def test_get_librenms_id_normalizes_string_to_int(self, mock_librenms_config):
        """Returns int for string-stored librenms_id; read path uses auto_save=False so no write-back."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        device = MagicMock()
        device.name = "test-device"
        device.cf = {"librenms_id": "42"}
        device.custom_field_data = {"librenms_id": "42"}

        result = api.get_librenms_id(device)
        assert result == 42
        # auto_save=False in this read path — normalization is NOT written back
        device.save.assert_not_called()

    def test_get_librenms_id_empty_string_falls_through_to_discovery(self, mock_librenms_config):
        """An empty-string librenms_id is treated as not set (falls through to API discovery)."""
        from unittest.mock import patch

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        device = MagicMock()
        device.name = "test-device"
        device.cf = {"librenms_id": ""}
        device.primary_ip = None

        with patch("netbox_librenms_plugin.librenms_api.cache") as mock_cache:
            mock_cache.get.return_value = None
            with patch.object(api, "get_device_id_by_hostname", return_value=None):
                result = api.get_librenms_id(device)
        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.cache")
    def test_get_librenms_id_from_cache(self, mock_cache, mock_librenms_config):
        """Returns ID from Django cache when not in custom field."""
        mock_cache.get.return_value = 99

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        device = MagicMock()
        device.name = "test-device"
        device.cf = {}

        result = api.get_librenms_id(device)
        assert result == 99

    @patch("netbox_librenms_plugin.librenms_api.cache")
    def test_get_librenms_id_handles_objects_without_device_identity_attrs(self, mock_cache, mock_librenms_config):
        """Objects like interfaces should return None cleanly when they have no stored or cached ID."""
        from types import SimpleNamespace

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        interface = SimpleNamespace(
            cf={},
            _meta=SimpleNamespace(model_name="interface"),
            pk=123,
        )

        mock_cache.get.return_value = None

        result = api.get_librenms_id(interface)

        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.cache")
    def test_get_stored_librenms_id_skips_hostname_lookup(self, mock_cache, mock_librenms_config):
        """Stored-only helper must not trigger discovery lookups for interface-like objects."""
        from types import SimpleNamespace

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        interface = SimpleNamespace(
            cf={},
            name="GigabitEthernet1/0/1",
            _meta=SimpleNamespace(model_name="interface"),
            pk=123,
        )

        mock_cache.get.return_value = None

        with patch.object(api, "get_device_id_by_hostname") as mock_hostname_lookup:
            result = api.get_stored_librenms_id(interface)

        assert result is None
        mock_hostname_lookup.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.cache")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_librenms_id_by_ip_lookup(self, mock_get, mock_cache, mock_librenms_config):
        """Performs IP lookup and caches result."""
        mock_cache.get.return_value = None
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 55}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        device = MagicMock()
        device.name = "test-device"
        device.cf = {}
        device.primary_ip4 = MagicMock()
        device.primary_ip4.address = MagicMock()
        device.primary_ip4.address.ip = "10.0.0.1"

        result = api.get_librenms_id(device)
        assert result == 55
        mock_cache.set.assert_called_once()

    @patch("netbox_librenms_plugin.librenms_api.cache")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_librenms_id_by_hostname_lookup(self, mock_get, mock_cache, mock_librenms_config):
        """Falls back to hostname lookup."""
        mock_cache.get.return_value = None

        # First call (IP lookup) returns empty, second call (hostname lookup) returns device
        call_count = [0]

        def side_effect(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            call_count[0] += 1
            if call_count[0] == 2:  # Second call is hostname lookup
                resp.json.return_value = {
                    "status": "ok",
                    "devices": [{"device_id": 77}],
                }
            else:
                resp.json.return_value = {"status": "ok", "devices": []}
            return resp

        mock_get.side_effect = side_effect

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        device = MagicMock()
        device.name = "test-device"
        device.cf = {}
        device.primary_ip4 = MagicMock()
        device.primary_ip4.address = MagicMock()
        device.primary_ip4.address.ip = "10.0.0.1"

        result = api.get_librenms_id(device)
        assert result == 77

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_id_by_ip_not_found(self, mock_get, mock_librenms_config):
        """Returns None when IP not found in LibreNMS."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "devices": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.get_device_id_by_ip("192.168.99.99")

        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_id_by_hostname_not_found(self, mock_get, mock_librenms_config):
        """Returns None when hostname not found."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "devices": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.get_device_id_by_hostname("nonexistent-host")

        assert result is None


# =============================================================================
# Test Class 5: Device Operations (6 tests)
# =============================================================================


class TestLibreNMSAPIDeviceOperations:
    """Test device CRUD operations."""

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_device_success(self, mock_post, mock_librenms_config):
        """Verify successful device addition."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "status": "ok",
            "message": "Device added successfully",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.add_device(
            data={
                "hostname": "test-device.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        assert result[0] is True
        assert result[1] == "Device added successfully."

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_device_snmpv1_success(self, mock_post, mock_librenms_config):
        """Verify successful device addition using SNMPv1."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "status": "ok",
            "message": "Device added successfully",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.add_device(
            data={
                "hostname": "legacy-device.example.com",
                "snmp_version": "v1",
                "community": "public",
            }
        )

        assert result[0] is True
        assert result[1] == "Device added successfully."
        # Verify the payload includes correct snmpver and community
        call_args = mock_post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["snmpver"] == "v1"
        assert payload["community"] == "public"

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_device_duplicate_error(self, mock_post, mock_librenms_config):
        """Verify duplicate device handling."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "status": "error",
            "message": "Device already exists",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.add_device(
            data={
                "hostname": "duplicate-device.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        assert result[0] is False
        assert "Device already exists" in result[1]

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_device_snmpv3_success(self, mock_post, mock_librenms_config):
        """Verify successful device addition using SNMPv3 with all required fields."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "status": "ok",
            "message": "Device added successfully",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.add_device(
            data={
                "hostname": "secure-device.example.com",
                "snmp_version": "v3",
                "authlevel": "authPriv",
                "authname": "snmpuser",
                "authpass": "authpassword123",
                "authalgo": "SHA",
                "cryptopass": "cryptopassword456",
                "cryptoalgo": "AES",
            }
        )

        assert result[0] is True
        assert result[1] == "Device added successfully."
        # Verify the payload includes correct snmpver and all v3 fields
        call_args = mock_post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["snmpver"] == "v3"
        assert payload["authlevel"] == "authPriv"
        assert payload["authname"] == "snmpuser"
        assert payload["authpass"] == "authpassword123"
        assert payload["authalgo"] == "SHA"
        assert payload["cryptopass"] == "cryptopassword456"
        assert payload["cryptoalgo"] == "AES"
        # Ensure community is NOT included for v3
        assert "community" not in payload

    @patch("netbox_librenms_plugin.librenms_api.requests.patch")
    def test_update_device_field_success(self, mock_patch, mock_librenms_config):
        """Verify successful device field update."""
        mock_patch.return_value.status_code = 200
        mock_patch.return_value.json.return_value = {
            "status": "ok",
            "message": "Device field updated",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, message = api.update_device_field(device_id=123, field_data={"field": "notes", "data": "Updated note"})

        assert success is True
        assert "updated" in message.lower()

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_info_success(self, mock_get, mock_librenms_config):
        """Verify retrieving device info."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 123, "hostname": "test-device"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, device_data = api.get_device_info(device_id=123)

        assert success is True
        assert device_data is not None
        assert device_data["device_id"] == 123

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_info_not_found(self, mock_get, mock_librenms_config):
        """Empty devices list returns (False, None) without raising."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "devices": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        success, result = api.get_device_info(1)
        assert success is False
        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_info_caches_success(self, mock_get, mock_librenms_config):
        """A successful lookup is cached: a second call within the TTL skips the HTTP request."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 7777, "hostname": "cached-device"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        first = api.get_device_info(device_id=7777)
        second = api.get_device_info(device_id=7777)

        assert first == second == (True, {"device_id": 7777, "hostname": "cached-device"})
        assert mock_get.call_count == 1  # second call served from cache, not re-fetched

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_info_does_not_cache_failure(self, mock_get, mock_librenms_config):
        """Failures are never cached, so a transient error doesn't persist for the cache window."""
        mock_get.side_effect = requests.exceptions.Timeout("boom")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        assert api.get_device_info(device_id=7778) == (False, None)
        assert api.get_device_info(device_id=7778) == (False, None)

        assert mock_get.call_count == 2  # not cached → re-attempted on the next call

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_info_use_cache_false_bypasses_stale_cache(self, mock_get, mock_librenms_config):
        """use_cache=False refetches live data instead of returning a stale cached payload, and refreshes the cache."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 424242, "hostname": "stale"}],
        }

        api = LibreNMSAPI(server_key="default")
        # First call populates the 60s cache with the (soon-to-be-stale) value.
        assert api.get_device_info(device_id=424242) == (True, {"device_id": 424242, "hostname": "stale"})

        # The hostname is corrected in LibreNMS within the cache window.
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 424242, "hostname": "fresh"}],
        }

        # A normal (cached) read still returns the stale value...
        assert api.get_device_info(device_id=424242) == (True, {"device_id": 424242, "hostname": "stale"})
        # ...but the import path (use_cache=False) fetches live data...
        assert api.get_device_info(device_id=424242, use_cache=False) == (
            True,
            {"device_id": 424242, "hostname": "fresh"},
        )
        # ...and that live fetch refreshes the cache, so subsequent cached reads see the correction.
        assert api.get_device_info(device_id=424242) == (True, {"device_id": 424242, "hostname": "fresh"})

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_list_devices_with_filters(self, mock_get, mock_librenms_config):
        """Verify listing devices with filter parameter."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 1}, {"device_id": 2}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, devices = api.list_devices(filters={"type": "network"})

        assert success is True
        assert len(devices) == 2


# =============================================================================
# Test Class 6: Location Operations (4 tests)
# =============================================================================


class TestLibreNMSAPILocationOperations:
    """Test location CRUD operations."""

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_locations_success(self, mock_get, mock_librenms_config):
        """Verify retrieving all locations."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "locations": [{"id": 1, "location": "DC1"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, locations = api.get_locations()

        assert success is True
        assert len(locations) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_location_success(self, mock_post, mock_librenms_config):
        """Verify successful location addition."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "status": "ok",
            "message": "Location created #5",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, result_dict = api.add_location(location_data={"location": "DC2"})

        assert success is True
        assert result_dict["id"] == "5"
        assert "message" in result_dict

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_location_error(self, mock_post, mock_librenms_config):
        """Verify location addition error handling."""
        mock_post.return_value.status_code = 500
        mock_post.return_value.json.return_value = {
            "status": "error",
            "message": "Invalid location data",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, error_msg = api.add_location(location_data={})

        assert success is False
        assert "Invalid location data" in error_msg

    @patch("netbox_librenms_plugin.librenms_api.requests.patch")
    def test_update_location_success(self, mock_patch, mock_librenms_config):
        """Verify successful location update."""
        mock_patch.return_value.status_code = 200
        mock_patch.return_value.json.return_value = {
            "status": "ok",
            "message": "Location updated",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, message = api.update_location(location_name="DC1", location_data={"location": "DC1-Updated"})

        assert success is True

    @patch("netbox_librenms_plugin.librenms_api.requests.patch")
    def test_update_location_not_found(self, mock_patch, mock_librenms_config):
        """Verify updating non-existent location."""
        mock_patch.return_value.status_code = 404
        mock_patch.return_value.json.return_value = {
            "status": "error",
            "message": "Location not found",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, message = api.update_location(location_name="NonExistent", location_data={})

        assert success is False


# =============================================================================
# Test Class 7: Ports and Inventory (9 tests)
# =============================================================================


class TestLibreNMSAPIPortsAndInventory:
    """Test ports and inventory operations."""

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_ports_all(self, mock_get, mock_librenms_config):
        """Verify retrieving all ports for a device."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "ports": [{"port_id": 1}, {"port_id": 2}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, data = api.get_ports(device_id=123)

        assert success is True
        assert "ports" in data
        assert len(data["ports"]) == 2

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_port_by_id_success(self, mock_get, mock_librenms_config):
        """Verify retrieving port by ID."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "port": [{"port_id": 1, "ifName": "GigabitEthernet0/1"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, port_data = api.get_port_by_id(port_id=1)

        assert success is True
        assert port_data is not None

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_port_by_id_error(self, mock_get, mock_librenms_config):
        """Verify handling of port retrieval error."""
        mock_get.side_effect = requests.exceptions.RequestException("Connection error")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, error_msg = api.get_port_by_id(port_id=999)

        assert success is False
        assert isinstance(error_msg, str)

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_inventory_success(self, mock_get, mock_librenms_config):
        """Verify retrieving device inventory."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "inventory": [{"entPhysicalClass": "chassis"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, inventory = api.get_device_inventory(device_id=123)

        assert success is True
        assert len(inventory) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_inventory_filtered_by_class(self, mock_get, mock_librenms_config):
        """Verify filtering inventory by physical class."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "inventory": [{"entPhysicalClass": "chassis", "entPhysicalName": "Chassis"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, inventory = api.get_inventory_filtered(device_id=123, ent_physical_class="chassis")

        assert success is True
        assert len(inventory) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_inventory_filtered_by_container(self, mock_get, mock_librenms_config):
        """Verify filtering inventory by container."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "inventory": [{"entPhysicalContainedIn": "0"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, inventory = api.get_inventory_filtered(device_id=123, ent_physical_contained_in=0)

        assert success is True

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_links_success(self, mock_get, mock_librenms_config):
        """Verify retrieving device links."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "links": [{"id": 1, "local_port_id": 10, "remote_port_id": 20}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, links_dict = api.get_device_links(device_id=123)

        assert success is True
        assert "links" in links_dict
        assert len(links_dict["links"]) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_ips_success(self, mock_get, mock_librenms_config):
        """Verify retrieving device IP addresses."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "addresses": [{"ipv4_address": "10.0.0.1"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, ips = api.get_device_ips(device_id=123)

        assert success is True
        assert len(ips) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_ips_empty(self, mock_get, mock_librenms_config):
        """Verify handling device with no IPs."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "addresses": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, ips = api.get_device_ips(device_id=123)

        assert success is True
        assert len(ips) == 0

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_ips_404_is_empty_not_failure(self, mock_get, mock_librenms_config):
        """LibreNMS 404s /devices/{id}/ip for a device with no IPs — a successful empty result, not a fetch failure."""
        import requests

        response = mock_get.return_value
        response.status_code = 404
        error = requests.exceptions.HTTPError("404 Client Error: Not Found")
        error.response = response
        response.raise_for_status.side_effect = error
        response.json.return_value = {
            "status": "error",
            "message": "The device does not have any IP addresses",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, ips = api.get_device_ips(device_id=123)

        # A device with no IPs must surface as an empty success so the IP tab shows an empty
        # table, not a red "Failed to fetch IP addresses" error (mirrors get_device_links).
        assert success is True
        assert ips == []

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_ips_404_empty_message_is_case_insensitive(self, mock_get, mock_librenms_config):
        """LibreNMS capitalization changes do not turn its stable no-address response into a failure."""
        response = mock_get.return_value
        response.status_code = 404
        error = requests.exceptions.HTTPError("404 Client Error: Not Found")
        error.response = response
        response.raise_for_status.side_effect = error
        response.json.return_value = {
            "status": "error",
            "message": "Device 123 Does Not Have Any IP Addresses",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        success, ips = LibreNMSAPI(server_key="default").get_device_ips(device_id=123)

        assert success is True
        assert ips == []

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_ips_404_for_missing_device_remains_a_failure(self, mock_get, mock_librenms_config):
        """Only LibreNMS's explicit empty-IP response is an empty success; a stale device id stays visible."""
        import requests

        response = mock_get.return_value
        response.status_code = 404
        response.json.return_value = {"status": "error", "message": "Device 123 does not exist"}
        error = requests.exceptions.HTTPError("404 Client Error: Not Found")
        error.response = response
        response.raise_for_status.side_effect = error

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, message = api.get_device_ips(device_id=123)

        assert success is False
        assert message == "Device 123 does not exist"


# =============================================================================
# Test Class 8: Poller and Devices (4 tests)
# =============================================================================


class TestLibreNMSAPIPollerAndDevices:
    """Test poller groups and device listing operations."""

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_poller_groups_success(self, mock_get, mock_librenms_config):
        """Verify retrieving poller groups."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "get_poller_group": [{"id": 1, "group_name": "primary"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, groups = api.get_poller_groups()

        assert success is True
        assert len(groups) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_poller_groups_empty(self, mock_get, mock_librenms_config):
        """Verify handling empty poller groups."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "get_poller_group": [],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, groups = api.get_poller_groups()

        assert success is True
        assert len(groups) == 0

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_list_devices_all(self, mock_get, mock_librenms_config):
        """Verify listing all devices."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 1}, {"device_id": 2}, {"device_id": 3}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, devices = api.list_devices()

        assert success is True
        assert len(devices) == 3

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_list_devices_empty(self, mock_get, mock_librenms_config):
        """Verify handling empty device list."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "devices": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, devices = api.list_devices()

        assert success is True
        assert len(devices) == 0


# =============================================================================
# Test Class 9: Error Handling (6 tests)
# =============================================================================


class TestLibreNMSAPIErrorHandling:
    """Test error handling and edge cases."""

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_network_error_handling(self, mock_get, mock_librenms_config):
        """Verify handling of network errors."""
        mock_get.side_effect = requests.exceptions.ConnectionError("Network unreachable")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, result = api.get_device_info(device_id=123)

        assert success is False
        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_timeout_error_handling(self, mock_get, mock_librenms_config):
        """Verify handling of timeout errors."""
        mock_get.side_effect = requests.exceptions.Timeout("Request timed out")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, result = api.get_device_info(device_id=123)

        assert success is False
        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_invalid_json_response(self, mock_get, mock_librenms_config):
        """Verify handling of invalid JSON responses — ValueError is now caught gracefully."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.side_effect = ValueError("Invalid JSON")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        # ValueError is now caught, returning (False, None) instead of propagating
        success, result = api.get_device_info(device_id=123)
        assert success is False
        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_http_500_error_handling(self, mock_get, mock_librenms_config):
        """Verify handling of HTTP 500 errors."""
        mock_get.return_value.status_code = 500
        mock_get.return_value.json.return_value = {
            "status": "error",
            "message": "Internal server error",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, result = api.get_device_info(device_id=123)

        assert success is False
        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_malformed_api_response(self, mock_post, mock_librenms_config):
        """Verify handling of malformed API responses."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {}  # Missing expected fields

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.add_device(
            data={
                "hostname": "test.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        # Should handle missing fields gracefully
        assert result[0] is False

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_ssl_verification_error(self, mock_get, mock_librenms_config):
        """Verify handling of SSL verification errors."""
        mock_get.side_effect = requests.exceptions.SSLError("SSL certificate verification failed")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.test_connection()

        assert result.get("error") is True


# ====================================================================================
# Test Class 5-9 continuing in next part due to length...
# Run `make unittest` to execute all tests
# ====================================================================================


# ====================================================================================
# Guard tests: int conversion guard and VLAN dict guard
# ====================================================================================


class TestGetLibreNMSIdIntGuard:
    """Tests for the int conversion guard in get_librenms_id."""

    def test_non_integer_string_from_ip_returns_none(self, mock_librenms_config):
        """When get_device_id_by_ip returns a non-int string, _store_librenms_id must not be called."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        obj = MagicMock()
        obj.primary_ip.address.ip = "192.168.1.1"
        obj.primary_ip.dns_name = ""
        obj.name = "test-device"
        obj._meta.model_name = "device"
        obj.pk = 1

        with (
            patch.object(api, "_get_cache_key", return_value="test_cache_key"),
            patch("netbox_librenms_plugin.librenms_api.cache") as mock_cache,
            patch.object(api, "get_device_id_by_ip", return_value="not-an-int"),
            patch.object(api, "get_device_id_by_hostname", return_value=None),
            patch.object(api, "_store_librenms_id") as mock_store,
        ):
            mock_cache.get.return_value = None
            result = api.get_librenms_id(obj)

        assert result is None
        mock_store.assert_not_called()

    def test_valid_integer_string_stores_and_returns(self, mock_librenms_config):
        """When get_device_id_by_ip returns a valid int string, it should be stored and returned."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        obj = MagicMock()
        obj.primary_ip.address.ip = "192.168.1.1"
        obj.primary_ip.dns_name = ""
        obj.name = "test-device"
        obj._meta.model_name = "device"
        obj.pk = 1

        with (
            patch.object(api, "_get_cache_key", return_value="test_cache_key"),
            patch("netbox_librenms_plugin.librenms_api.cache") as mock_cache,
            patch.object(api, "get_device_id_by_ip", return_value="42"),
            patch.object(api, "_store_librenms_id") as mock_store,
        ):
            mock_cache.get.return_value = None
            result = api.get_librenms_id(obj)

        assert result == 42
        mock_store.assert_called_once_with(obj, 42)


class TestVlanEntryDictGuard:
    """Tests for the isinstance(vlan_entry, dict) guard in _parse_port_vlan_info."""

    def test_non_dict_entry_is_skipped(self, mock_librenms_config):
        """Non-dict entries in vlans_data should be skipped without error."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        port_data = {
            "ifName": "eth0",
            "ifDescr": "eth0",
            "port_id": 1,
            "ifTrunk": "dot1Q",
            "ifVlan": None,
            "vlans": [{"vlan": 10, "untagged": 1}, "bad_entry", {"vlan": 20}],
        }
        result = api.parse_port_vlan_data(port_data)

        # Only VIDs 10 and 20 should be parsed; string entry is skipped
        assert result["untagged_vlan"] == 10
        assert 20 in result["tagged_vlans"]
        assert len(result["tagged_vlans"]) == 1

    def test_mixed_bad_entries_no_exception(self, mock_librenms_config):
        """Multiple non-dict entries mixed with valid dicts should not raise."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        port_data = {
            "ifName": "eth0",
            "ifDescr": "eth0",
            "port_id": 2,
            "ifTrunk": "dot1Q",
            "ifVlan": None,
            "vlans": [None, 123, "unexpected", {"vlan": 30}],
        }
        result = api.parse_port_vlan_data(port_data)

        assert result["tagged_vlans"] == [30]
        assert result["untagged_vlan"] is None


# ====================================================================================
# Response-shape regression tests for get_device_transceivers / get_device_info /
# get_device_vlans / get_port_vlan_details. These guard the malformed-payload
# branches that were tightened up alongside the inventory/transceiver work.
# ====================================================================================


class TestGetDeviceInfoResponseShape:
    """Cover response-shape branches in get_device_info()."""

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_non_dict_device_entry_returns_failure(self, mock_get, mock_librenms_config):
        """A non-dict entry in the devices list must not propagate as truthy data."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "devices": ["not-a-dict"]}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, data = api.get_device_info(device_id=1)

        assert success is False
        assert data is None

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_missing_devices_key_returns_failure(self, mock_get, mock_librenms_config):
        """KeyError on missing 'devices' must be caught and return (False, None)."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok"}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, data = api.get_device_info(device_id=1)

        assert success is False
        assert data is None


class TestGetDeviceTransceiversResponseShape:
    """Cover response-shape branches in get_device_transceivers()."""

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_success_returns_transceiver_list(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "transceivers": [
                {"port_id": 1, "type": "QSFP28", "serial": "SN1"},
                {"port_id": 2, "type": "SFP+", "serial": "SN2"},
            ],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, data = api.get_device_transceivers(device_id=123)

        assert success is True
        assert len(data) == 2
        assert data[0]["serial"] == "SN1"

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_invalid_json_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.side_effect = ValueError("bad json")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_transceivers(device_id=123)

        assert success is False
        assert "Invalid JSON" in msg

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_non_dict_response_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = ["unexpected", "list"]

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_transceivers(device_id=123)

        assert success is False
        assert "Unexpected" in msg

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_missing_transceivers_key_uses_server_message(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "message": "no transceivers MIB"}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_transceivers(device_id=123)

        assert success is False
        assert msg == "no transceivers MIB"

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_status_not_ok_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "error",
            "transceivers": [],
            "message": "device offline",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_transceivers(device_id=123)

        assert success is False
        assert msg == "device offline"

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_transceivers_not_list_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "transceivers": {"port_id": 1},
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_transceivers(device_id=123)

        assert success is False
        assert "Unexpected" in msg

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_malformed_transceiver_entry_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "transceivers": [{"port_id": 1}, None],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_transceivers(device_id=123)

        assert success is False
        assert "Malformed" in msg

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_request_exception_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.side_effect = requests.exceptions.ConnectionError("boom")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_transceivers(device_id=123)

        assert success is False
        assert "boom" in msg


class TestGetDeviceVlansResponseShape:
    """Cover response-shape branches in get_device_vlans()."""

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_success_filters_by_device_id(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "vlans": [
                {"vlan_id": 1, "device_id": 7, "vlan_vlan": 10, "vlan_name": "DATA"},
                {"vlan_id": 2, "device_id": 8, "vlan_vlan": 20, "vlan_name": "VOICE"},
            ],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, vlans = api.get_device_vlans(device_id=7)

        assert success is True
        assert len(vlans) == 1
        assert vlans[0]["vlan_vlan"] == 10

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_vlans_not_list_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "vlans": "oops",
            "message": "bad payload",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_vlans(device_id=7)

        assert success is False
        assert msg == "bad payload"

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_non_dict_item_in_vlans_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "vlans": [{"vlan_id": 1, "device_id": 7}, "not-a-dict"],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_vlans(device_id=7)

        assert success is False
        assert "invalid item shape" in msg

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_status_not_ok_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "error", "message": "nope"}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_vlans(device_id=7)

        assert success is False
        assert msg == "nope"

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_non_dict_response_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = ["unexpected"]

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_vlans(device_id=7)

        assert success is False
        assert msg == "Unexpected response format"

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_http_404_returns_dedicated_message(self, mock_get, mock_librenms_config):
        response = MagicMock(status_code=404)
        mock_get.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_vlans(device_id=7)

        assert success is False
        assert msg == "VLANs resource not found"

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_value_error_returns_connection_message(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.side_effect = ValueError("bad json")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_device_vlans(device_id=7)

        assert success is False
        assert "Error connecting to LibreNMS" in msg


class TestGetPortVlanDetailsResponseShape:
    """Cover response-shape branches in get_port_vlan_details()."""

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_success_returns_port_dict(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "port": [{"port_id": 11, "ifName": "Te1/1/1", "vlans": [{"vlan": 10, "untagged": 1}]}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, port = api.get_port_vlan_details(port_id=11)

        assert success is True
        assert port["port_id"] == 11

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_non_dict_response_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = ["unexpected"]

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_port_vlan_details(port_id=11)

        assert success is False
        assert msg == "Unexpected response format"

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_status_not_ok_uses_server_message(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "error", "message": "no port"}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_port_vlan_details(port_id=11)

        assert success is False
        assert msg == "no port"

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_missing_port_list_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "port": {"port_id": 11}}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_port_vlan_details(port_id=11)

        assert success is False
        assert "missing 'port' list" in msg

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_empty_port_list_returns_not_found(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "port": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_port_vlan_details(port_id=11)

        assert success is False
        assert msg == "Port not found"

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_non_dict_port_entry_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "port": ["bad-entry"]}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_port_vlan_details(port_id=11)

        assert success is False
        assert "invalid 'port' entry" in msg

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_request_exception_returns_failure(self, mock_get, mock_librenms_config):
        mock_get.side_effect = requests.exceptions.ConnectionError("net down")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, msg = api.get_port_vlan_details(port_id=11)

        assert success is False
        assert "net down" in msg


# =============================================================================
# Test Class: get_port_stack()
# =============================================================================


class TestGetPortStack:
    """Tests for LibreNMSAPI.get_port_stack()."""

    def test_returns_mappings_list_on_success(self, mock_librenms_api):
        """get_port_stack returns (True, list) on HTTP 200."""
        from unittest.mock import MagicMock, patch

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "status": "ok",
            "mappings": [
                {"high_port_id": 1, "low_port_id": 2, "high_ifIndex": 1, "low_ifIndex": 2},
            ],
        }
        fake_response.raise_for_status = MagicMock()
        with patch("netbox_librenms_plugin.librenms_api.requests.get", return_value=fake_response) as mock_get:
            success, data = mock_librenms_api.get_port_stack(42)

        assert success is True
        assert data == [{"high_port_id": 1, "low_port_id": 2, "high_ifIndex": 1, "low_ifIndex": 2}]
        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0]
        assert "/api/v0/devices/42/port_stack" in call_url

    def test_returns_false_on_404(self, mock_librenms_api):
        """get_port_stack returns (False, error_str) when device not found."""
        import requests as _requests
        from unittest.mock import MagicMock, patch

        fake_resp = MagicMock()
        fake_resp.status_code = 404
        http_error = _requests.exceptions.HTTPError(response=fake_resp)
        fake_resp.raise_for_status.side_effect = http_error
        with patch("netbox_librenms_plugin.librenms_api.requests.get", return_value=fake_resp):
            success, data = mock_librenms_api.get_port_stack(99)

        assert success is False
        assert "not found" in data.lower()

    @pytest.mark.parametrize("payload", [{"status": "ok"}, {"status": "ok", "mappings": None}])
    def test_missing_or_null_mappings_fails_closed(self, mock_librenms_api, payload):
        """A successful response must contain the documented list-valued mappings field."""
        from unittest.mock import MagicMock, patch

        fake_response = MagicMock()
        fake_response.json.return_value = payload
        fake_response.raise_for_status = MagicMock()
        with patch("netbox_librenms_plugin.librenms_api.requests.get", return_value=fake_response):
            success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert "mappings" in data

    def test_error_status_without_mappings_fails_not_empty(self, mock_librenms_api):
        """An error payload that omits 'mappings' must fail, not normalise to (True, []), so a real API failure isn't masked as 'no LAG/parent relationships'."""
        from unittest.mock import MagicMock, patch

        fake_response = MagicMock()
        fake_response.json.return_value = {"status": "error", "message": "device polling disabled"}
        fake_response.raise_for_status = MagicMock()
        with patch("netbox_librenms_plugin.librenms_api.requests.get", return_value=fake_response):
            success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert data == "device polling disabled"

    def test_error_status_with_mappings_present_still_fails(self, mock_librenms_api):
        """An error payload that still carries mappings must honor the explicit error status before consuming them, so it fails rather than silently skipping valid sync."""
        from unittest.mock import MagicMock, patch

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "status": "error",
            "message": "stale poll",
            "mappings": [],
        }
        fake_response.raise_for_status = MagicMock()
        with patch("netbox_librenms_plugin.librenms_api.requests.get", return_value=fake_response):
            success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert data == "stale poll"

    def test_non_string_status_fails_not_empty(self, mock_librenms_api):
        """A non-string status like `false` is malformed and must fail, not be accepted as (True, [])."""
        from unittest.mock import MagicMock, patch

        fake_response = MagicMock()
        # {"status": false, "mappings": []} — only an absent status or "ok" is a genuine answer;
        # accepting this would silently suppress LAG/sub-interface relationship updates.
        fake_response.json.return_value = {"status": False, "mappings": []}
        fake_response.raise_for_status = MagicMock()
        with patch("netbox_librenms_plugin.librenms_api.requests.get", return_value=fake_response):
            success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert "LibreNMS reported an error fetching port stack" in data

    def test_returns_error_on_invalid_json(self, mock_librenms_api):
        """A non-JSON body (response.json() raises ValueError) must surface as (False, error) instead of letting the exception escape."""
        from unittest.mock import MagicMock, patch

        fake_response = MagicMock()
        fake_response.raise_for_status = MagicMock()
        fake_response.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
        with patch("netbox_librenms_plugin.librenms_api.requests.get", return_value=fake_response):
            success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert "Invalid JSON" in data

    def test_json_decode_error_reports_invalid_json_not_connection_error(self, mock_librenms_api):
        """requests.exceptions.JSONDecodeError subclasses BOTH ValueError and RequestException, so the ValueError handler must precede the RequestException handler — otherwise a JSON decode failure is swallowed by the broad handler and mislabeled 'Error connecting to LibreNMS'."""
        from unittest.mock import MagicMock, patch

        import requests as _requests

        fake_response = MagicMock()
        fake_response.raise_for_status = MagicMock()
        fake_response.json.side_effect = _requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        with patch("netbox_librenms_plugin.librenms_api.requests.get", return_value=fake_response):
            success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert "Invalid JSON" in data
        assert "Error connecting" not in data

    def test_malformed_mappings_fails_not_empty(self, mock_librenms_api):
        """A non-list (or list-of-non-dicts) `mappings` is malformed, not 'no relationships'."""
        from unittest.mock import MagicMock, patch

        for bad in ({"mappings": {"oops": 1}}, {"mappings": ["not-a-dict", 2]}):
            fake_response = MagicMock()
            fake_response.raise_for_status = MagicMock()
            fake_response.json.return_value = bad
            with patch("netbox_librenms_plugin.librenms_api.requests.get", return_value=fake_response):
                success, data = mock_librenms_api.get_port_stack(5)
            assert success is False, f"{bad!r} should fail"
            assert "mappings" in data

    def test_non_object_payload_fails_not_empty(self, mock_librenms_api):
        """A non-object top-level payload (list/string/null) is malformed, not 'no relationships'."""
        from unittest.mock import MagicMock, patch

        for bad in ([{"high_port_id": 1, "low_port_id": 2}], "oops", None):
            fake_response = MagicMock()
            fake_response.raise_for_status = MagicMock()
            fake_response.json.return_value = bad
            with patch("netbox_librenms_plugin.librenms_api.requests.get", return_value=fake_response):
                success, data = mock_librenms_api.get_port_stack(5)
            assert success is False, f"{bad!r} should fail"
            assert "non-object" in data


# =============================================================================
# Fixture port data for resolve_port_relationships tests
# =============================================================================

# Fixture port data for resolve_port_relationships tests
NOKIA_PORTS = [
    {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
    {"port_id": 102, "ifName": "lag-1", "ifType": "ieee8023adLag"},
]
NOKIA_PORT_STACK = [
    {"high_port_id": 101, "low_port_id": 102},  # valid LAG membership
    {"high_port_id": 102, "low_port_id": 200},  # low_id 200 not in ports (missing = skip)
]
NOKIA_SAP_PORTS = [
    {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
    {"port_id": 102, "ifName": "lag-1", "ifType": "ieee8023adLag"},
    {"port_id": 200, "ifName": "lag1:0", "ifType": "ipForward"},  # SAP entry with colon
]
NOKIA_SAP_PORT_STACK = [
    {"high_port_id": 101, "low_port_id": 102},  # valid LAG
    {"high_port_id": 102, "low_port_id": 200},  # SAP: should be excluded
]
JUNOS_PORTS = [
    {"port_id": 201, "ifName": "xe-0/0/0", "ifType": "ethernetCsmacd"},
    {"port_id": 202, "ifName": "xe-0/0/0.0", "ifType": "propVirtual"},
    {"port_id": 203, "ifName": "ae1", "ifType": "ieee8023adLag"},
    {"port_id": 204, "ifName": "ae1.0", "ifType": "ieee8023adLag"},
    {"port_id": 205, "ifName": "ae10", "ifType": "ieee8023adLag"},
    {"port_id": 206, "ifName": "ae10.2221", "ifType": "l2vlan"},
]
JUNOS_PORT_STACK = [
    {"high_port_id": 202, "low_port_id": 204},  # xe-0/0/0.0 -> ae1.0 resolves to xe-0/0/0 in ae1
    {"high_port_id": 205, "low_port_id": 206},  # ae10 -> ae10.2221 (sub-interface)
]
CISCO_IOS_PORTS = [
    {"port_id": 301, "ifName": "Te1/1", "ifType": "ethernetCsmacd"},
    {"port_id": 302, "ifName": "Po10", "ifType": "propVirtual"},  # IOS port-channel
    {"port_id": 303, "ifName": "Po10.100", "ifType": "l2vlan"},
]
CISCO_IOS_PORT_STACK = [
    {"high_port_id": 301, "low_port_id": 302},  # LAG membership
    {"high_port_id": 302, "low_port_id": 303},  # sub-interface
]
ARCOS_PORTS = [
    {"port_id": 401, "ifName": "swp4", "ifType": "ethernetCsmacd"},
    {"port_id": 402, "ifName": "bond1", "ifType": "ieee8023adLag"},
    {"port_id": 403, "ifName": "swp15", "ifType": "ethernetCsmacd"},
    {"port_id": 404, "ifName": "swp15.3", "ifType": "ethernetCsmacd"},  # sub-if, not propVirtual
]
ARCOS_PORT_STACK = [
    {"high_port_id": 401, "low_port_id": 402},  # LAG membership
    {"high_port_id": 403, "low_port_id": 404},  # sub-interface
]


# =============================================================================
# Test Class: resolve_port_relationships()
# =============================================================================


@pytest.fixture
def ios_lag_patterns():
    """LAG patterns dict for Cisco IOS (propVirtual LAGs identified by name)."""
    return {"ios": r"^Po\d+$"}


class TestResolvePortRelationships:
    """Tests for LibreNMSAPI.resolve_port_relationships()."""

    def test_resolves_a_verbatim_live_port_stack_entry(self, mock_librenms_api):
        """A port_stack entry exactly as a live LibreNMS returns it must resolve.

        Pinned to a real response, not a hand-written one: LibreNMS serves the ports_stack rows
        verbatim, so an entry carries id/device_id/high_ifIndex/high_port_id/low_ifIndex/
        low_port_id/ifStackStatus. The API docs advertise port_id_high/port_id_low instead, and
        every synthetic fixture in this file once used that spelling, so production and its tests
        agreed on a shape no server sends and every relationship resolved to nothing.
        """
        ports = [
            {"port_id": 6477, "ifName": "em0", "ifType": "ethernetCsmacd"},
            {"port_id": 6482, "ifName": "em0.0", "ifType": "propVirtual"},
        ]
        live_entry = {
            "id": 2795,
            "device_id": 15,
            "high_ifIndex": 17,
            "high_port_id": 6477,
            "low_ifIndex": 18,
            "low_port_id": 6482,
            "ifStackStatus": "active",
        }

        result = mock_librenms_api.resolve_port_relationships(ports, [live_entry], lag_patterns={})

        assert result["sub_interfaces"] == {6482: 6477}

    def test_unrecognized_port_stack_shape_is_logged(self, mock_librenms_api, caplog):
        """An entry carrying neither id key must warn, not silently drop every relationship."""
        ports = [{"port_id": 1, "ifName": "eth0", "ifType": "ethernetCsmacd"}]
        with caplog.at_level("WARNING"):
            result = mock_librenms_api.resolve_port_relationships(
                ports, [{"port_id_high": 1, "port_id_low": 2}], lag_patterns={}
            )
        assert result == {"lag_members": {}, "sub_interfaces": {}}
        assert any("Unrecognized port_stack entry shape" in r.getMessage() for r in caplog.records)

    def test_nokia_lag_membership(self, mock_librenms_api):
        """Nokia: high=physical, low=lag-1 (ieee8023adLag) -> member in lag_members."""
        result = mock_librenms_api.resolve_port_relationships(NOKIA_PORTS, NOKIA_PORT_STACK[:1], lag_patterns={})
        assert result["lag_members"] == {101: 102}
        assert result["sub_interfaces"] == {}

    def test_nameless_lag_aggregate_resolved_by_iftype(self, mock_librenms_api):
        """A nameless aggregate (ifType=ieee8023adLag, empty ifName/ifDescr) still resolves via ifType.

        ifType alone authoritatively classifies a LAG aggregate, so a valid port_id with no name must
        stay in the by_id lookup — otherwise the port_stack pair referencing it is skipped and the
        member's LAG membership is silently dropped.
        """
        ports = [
            {"port_id": 201, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 202, "ifName": "", "ifDescr": "", "ifType": "ieee8023adLag"},  # nameless aggregate
        ]
        port_stack = [{"high_port_id": 201, "low_port_id": 202}]
        result = mock_librenms_api.resolve_port_relationships(ports, port_stack, lag_patterns={})
        assert result["lag_members"] == {201: 202}

    @pytest.mark.parametrize(
        "port_stack",
        [
            [
                {"high_port_id": 10, "low_port_id": 100},
                {"high_port_id": 10, "low_port_id": 200},
            ],
            [
                {"high_port_id": 10, "low_port_id": 200},
                {"high_port_id": 10, "low_port_id": 100},
            ],
        ],
    )
    def test_conflicting_lag_targets_are_dropped_regardless_of_input_order(self, mock_librenms_api, port_stack):
        ports = [
            {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 100, "ifName": "Port-Channel1", "ifType": "ieee8023adLag"},
            {"port_id": 200, "ifName": "Port-Channel2", "ifType": "ieee8023adLag"},
        ]

        result = mock_librenms_api.resolve_port_relationships(ports, port_stack, lag_patterns={})

        assert result["lag_members"] == {}

    def test_subinterface_resolved_from_ifdescr_mode(self, mock_librenms_api):
        """On an ifDescr-mode device the sub-unit name lives in ifDescr (ifName empty); the resolver must still pair child -> parent instead of dropping both ports (which only matched ifName before)."""
        ports = [
            {"port_id": 1, "ifName": "", "ifDescr": "ge-0/0/0", "ifType": "ethernetCsmacd"},
            {"port_id": 2, "ifName": "", "ifDescr": "ge-0/0/0.100", "ifType": "l3ipvlan"},
        ]
        port_stack = [{"high_port_id": 2, "low_port_id": 1}]
        result = mock_librenms_api.resolve_port_relationships(
            ports, port_stack, lag_patterns={}, interface_name_field="ifDescr"
        )
        assert result["sub_interfaces"] == {2: 1}

    def test_lag_aggregate_matched_from_ifdescr_name_pattern(self, mock_librenms_api):
        """A name-pattern LAG aggregate is detected from the configured ifDescr field."""
        ports = [
            {"port_id": 11, "ifName": "", "ifDescr": "Gi0/1", "ifType": "ethernetCsmacd"},
            {"port_id": 12, "ifName": "", "ifDescr": "Po1", "ifType": "propVirtual"},
        ]
        port_stack = [{"high_port_id": 11, "low_port_id": 12}]
        # low (Po1) is the aggregate via the configured pattern; member -> aggregate.
        result = mock_librenms_api.resolve_port_relationships(
            ports, port_stack, lag_patterns={"ios": r"^Po\d+$"}, interface_name_field="ifDescr"
        )
        assert result["lag_members"] == {11: 12}

    def test_string_port_stack_ids_match_int_port_ids(self, mock_librenms_api):
        """String high_port_id/low_port_id values must match integer port records and vice versa."""
        # NOKIA_PORTS carries int port_ids 101/102; reference them as strings in the stack.
        str_stack = [{"high_port_id": "101", "low_port_id": "102"}]
        result = mock_librenms_api.resolve_port_relationships(NOKIA_PORTS, str_stack, lag_patterns={})
        assert result["lag_members"] == {101: 102}
        # And the inverse: int stack ids against str port_ids. The map is normalized at the
        # source (normalize_librenms_port_id), so the result is canonical ints regardless of
        # whether the port records carried str or int port_ids — consumers never juggle types.
        str_ports = [
            {"port_id": "101", "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": "102", "ifName": "lag-1", "ifType": "ieee8023adLag"},
        ]
        result = mock_librenms_api.resolve_port_relationships(
            str_ports, [{"high_port_id": 101, "low_port_id": 102}], lag_patterns={}
        )
        assert result["lag_members"] == {101: 102}

    def test_padded_port_ids_match_canonical_stack_ids(self, mock_librenms_api):
        ports = [
            {"port_id": "0101", "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": "00102", "ifName": "lag-1", "ifType": "ieee8023adLag"},
        ]

        result = mock_librenms_api.resolve_port_relationships(
            ports,
            [{"high_port_id": 101, "low_port_id": 102}],
            lag_patterns={},
        )

        assert result["lag_members"] == {101: 102}

    def test_duplicate_canonical_port_ids_make_that_endpoint_ambiguous(self, mock_librenms_api):
        ports = [
            {"port_id": 101, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": "00101", "ifName": "Ethernet2", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "lag-1", "ifType": "ieee8023adLag"},
        ]

        result = mock_librenms_api.resolve_port_relationships(
            ports,
            [{"high_port_id": 101, "low_port_id": 102}],
            lag_patterns={},
        )

        assert result["lag_members"] == {}

    def test_sub_interface_ids_use_port_record_types_not_stack(self, mock_librenms_api):
        """sub_interfaces must store canonical normalized port_ids (like the LAG branch), not the raw port_stack ids."""
        # Ports carry STRING port_ids; the stack references them as INTs.
        str_ports = [
            {"port_id": "205", "ifName": "ae10", "ifType": "ieee8023adLag"},
            {"port_id": "206", "ifName": "ae10.2221", "ifType": "l2vlan"},
        ]
        int_stack = [{"high_port_id": 205, "low_port_id": 206}]
        result = mock_librenms_api.resolve_port_relationships(str_ports, int_stack, lag_patterns={})
        # Canonical normalized ids (port-record-derived, not the stack ids): str port_ids are
        # normalized to ints at the source so the map is self-consistent for every consumer.
        assert result["sub_interfaces"] == {206: 205}

    def test_an_empty_name_field_is_not_a_sap_match(self, mock_librenms_api):
        """A SAP pattern that matches the empty string must not drop a row over a blank name field."""
        ports = [
            {"port_id": 201, "ifName": "1/1/c1/1", "ifDescr": "", "ifType": "ethernetCsmacd"},
            {"port_id": 202, "ifName": "", "ifDescr": "", "ifType": "ieee8023adLag"},
        ]
        port_stack = [{"high_port_id": 201, "low_port_id": 202}]
        result = mock_librenms_api.resolve_port_relationships(
            ports, port_stack, lag_patterns={}, compiled_sap_patterns=[re.compile("^$")]
        )
        assert result["lag_members"] == {201: 202}

    def test_nokia_sap_excluded_when_colon_in_name(self, mock_librenms_api):
        """Nokia SAP entries (colon in name) must be excluded from output."""
        result = mock_librenms_api.resolve_port_relationships(
            NOKIA_SAP_PORTS, NOKIA_SAP_PORT_STACK, lag_patterns={}, compiled_sap_patterns=[re.compile(":")]
        )
        assert result["lag_members"] == {101: 102}
        assert 200 not in result["lag_members"].values()

    def test_nokia_sap_excluded_when_colon_only_in_ifname_ifdescr_mode(self, mock_librenms_api):
        """On an ifDescr-mode device a SAP port can carry a clean ifDescr (the primary name) but the real lag1:0 marker in ifName; the colon check must scan ALL names, not just the primary, or the SAP row is misclassified as a LAG member."""
        ports = [
            {"port_id": 101, "ifName": "1/1/c1/1", "ifDescr": "phys-1", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "lag-1", "ifDescr": "agg-1", "ifType": "ieee8023adLag"},
            # SAP entry: clean ifDescr (the primary in this mode), colon only in ifName.
            {"port_id": 200, "ifName": "lag1:0", "ifDescr": "sap-clean", "ifType": "ipForward"},
        ]
        port_stack = [
            {"high_port_id": 101, "low_port_id": 102},  # genuine LAG membership
            {"high_port_id": 200, "low_port_id": 102},  # SAP row: must be skipped
        ]
        result = mock_librenms_api.resolve_port_relationships(
            ports,
            port_stack,
            lag_patterns={},
            interface_name_field="ifDescr",
            compiled_sap_patterns=[re.compile(":")],
        )
        assert result["lag_members"] == {101: 102}
        assert 200 not in result["lag_members"]

    def test_junos_sub_unit_stripping(self, mock_librenms_api):
        """Junos: xe-0/0/0.0 -> ae1.0 pair strips to xe-0/0/0 member of ae1."""
        result = mock_librenms_api.resolve_port_relationships(JUNOS_PORTS, JUNOS_PORT_STACK[:1], lag_patterns={})
        assert result["lag_members"].get(201) == 203

    def test_junos_ae_sub_interface(self, mock_librenms_api):
        """Junos: ae10 -> ae10.2221 from the ifStack, and every other unit from its own name."""
        result = mock_librenms_api.resolve_port_relationships(JUNOS_PORTS, JUNOS_PORT_STACK[1:], lag_patterns={})
        # 206 is the pair the ifStack states. The other two are units LibreNMS never parents:
        # xe-0/0/0.0 is only stacked onto ae1.0, and ae1.0 appears in no row of its own.
        assert result["sub_interfaces"] == {202: 201, 204: 203, 206: 205}

    def test_sub_interface_detected_via_ifname_when_namefield_is_ifdescr(self, mock_librenms_api):
        """The resolver falls back to ifName when ifDescr yields no sub-interface edge."""
        ports = [
            # The ifDescr labels have no sub-unit structure.
            {"port_id": 401, "ifName": "xe-0/0/0", "ifDescr": "uplink-core", "ifType": "ethernetCsmacd"},
            {"port_id": 402, "ifName": "xe-0/0/0.100", "ifDescr": "vlan-100-svc", "ifType": "l2vlan"},
        ]
        port_stack = [{"high_port_id": 401, "low_port_id": 402}]
        result = mock_librenms_api.resolve_port_relationships(
            ports, port_stack, lag_patterns={}, interface_name_field="ifDescr"
        )
        # The fallback resolves child 402 from ifName alone.
        assert result["sub_interfaces"] == {402: 401}

    def test_lag_physical_resolution_uses_ifname_when_namefield_is_ifdescr(self, mock_librenms_api):
        """The resolver falls back to ifName alone when ifDescr yields no LAG edge."""
        # The ifDescr labels yield no relationship, while ifName has the Junos structure.
        ports = [
            # Physical member and logical unit.
            {"port_id": 201, "ifName": "xe-0/0/0", "ifDescr": "member-phys", "ifType": "ethernetCsmacd"},
            {"port_id": 202, "ifName": "xe-0/0/0.0", "ifDescr": "member-unit", "ifType": "l2vlan"},
            # Aggregate and logical unit.
            {"port_id": 203, "ifName": "ae1", "ifDescr": "bundle-core", "ifType": "ieee8023adLag"},
            {"port_id": 204, "ifName": "ae1.0", "ifDescr": "bundle-unit", "ifType": "l2vlan"},
        ]
        # ifStack relates the logical units.
        port_stack = [{"high_port_id": 204, "low_port_id": 202}]
        result = mock_librenms_api.resolve_port_relationships(
            ports, port_stack, lag_patterns={"junos": r"^ae\d"}, interface_name_field="ifDescr"
        )
        # The ifName fallback collapses both units to their physical ports.
        assert result["lag_members"] == {201: 203}
        # The logical member is not bound directly.
        assert 202 not in result["lag_members"]

    def test_other_field_evidence_is_not_added_when_configured_field_yields_relationships(self, mock_librenms_api):
        """The resolver keeps only configured-field edges when that field yields a relationship."""
        ports = [
            {"port_id": 1, "ifName": "et-0/0/0", "ifDescr": "primary", "ifType": "ethernetCsmacd"},
            {"port_id": 2, "ifName": "et-0/0/0.1", "ifDescr": "unit", "ifType": "propVirtual"},
            {"port_id": 3, "ifName": "service-parent", "ifDescr": "service", "ifType": "ethernetCsmacd"},
            {"port_id": 4, "ifName": "service-child", "ifDescr": "service.1", "ifType": "propVirtual"},
        ]

        result = mock_librenms_api.resolve_port_relationships(
            ports, [], lag_patterns={}, interface_name_field="ifName", compiled_sap_patterns=[]
        )

        assert result == {"lag_members": {}, "sub_interfaces": {2: 1}}

    def test_fallback_uses_other_field_alone_when_configured_field_yields_nothing(self, mock_librenms_api):
        """The resolver uses only ifDescr when configured ifName yields no relationship."""
        ports = [
            {"port_id": 1, "ifName": "uplink", "ifDescr": "xe-0/0/0", "ifType": "ethernetCsmacd"},
            {"port_id": 2, "ifName": "customer", "ifDescr": "xe-0/0/0.1", "ifType": "propVirtual"},
        ]

        result = mock_librenms_api.resolve_port_relationships(
            ports, [], lag_patterns={}, interface_name_field="ifName", compiled_sap_patterns=[]
        )

        assert result == {"lag_members": {}, "sub_interfaces": {2: 1}}

    def test_each_empty_relationship_map_falls_back_independently(self, mock_librenms_api):
        """Each empty configured-field map falls back without replacing a populated map."""
        ports = [
            {"port_id": 1, "ifName": "ethernet-1", "ifDescr": "member", "ifType": "ethernetCsmacd"},
            {"port_id": 2, "ifName": "lag-1", "ifDescr": "aggregate", "ifType": "ieee8023adLag"},
            {"port_id": 3, "ifName": "xe-0/0/0", "ifDescr": "physical", "ifType": "ethernetCsmacd"},
            {"port_id": 4, "ifName": "xe-0/0/0.1", "ifDescr": "logical", "ifType": "propVirtual"},
        ]
        port_stack = [{"high_port_id": 1, "low_port_id": 2}]

        result = mock_librenms_api.resolve_port_relationships(
            ports, port_stack, lag_patterns={}, interface_name_field="ifDescr", compiled_sap_patterns=[]
        )

        assert result == {"lag_members": {1: 2}, "sub_interfaces": {4: 3}}

    def test_ifname_lag_pattern_resolves_in_ifdescr_mode(self, mock_librenms_api):
        """An ifName LAG pattern still resolves LAG members in ifDescr mode."""
        ports = [
            {
                "port_id": 17343,
                "ifName": "Te0/1/0",
                "ifDescr": "TenGigabitEthernet0/1/0",
                "ifType": "ethernetCsmacd",
            },
            {
                "port_id": 23722,
                "ifName": "Po1",
                "ifDescr": "Port-channel1",
                "ifType": "propVirtual",
            },
            {
                "port_id": 23723,
                "ifName": "Po1.100",
                "ifDescr": "Port-channel1.100",
                "ifType": "l2vlan",
            },
        ]
        port_stack = [
            {"high_port_id": 17343, "low_port_id": 23722},
            {"high_port_id": 23722, "low_port_id": 23723},
        ]

        result = mock_librenms_api.resolve_port_relationships(
            ports,
            port_stack,
            lag_patterns={"iosxe": r"^Po\d+$"},
            interface_name_field="ifDescr",
            compiled_sap_patterns=[],
        )

        assert result == {
            "lag_members": {17343: 23722},
            "sub_interfaces": {23723: 23722},
        }

    def test_empty_configured_lag_map_falls_back_without_replacing_sub_interfaces(self, mock_librenms_api):
        """An empty LAG map falls back without replacing configured sub-interfaces."""
        ports = [
            {"port_id": 1, "ifName": "member", "ifDescr": "ethernet-1", "ifType": "ethernetCsmacd"},
            {"port_id": 2, "ifName": "aggregate", "ifDescr": "bundle-1", "ifType": "propVirtual"},
            {"port_id": 3, "ifName": "xe-0/0/0", "ifDescr": "physical", "ifType": "ethernetCsmacd"},
            {"port_id": 4, "ifName": "xe-0/0/0.1", "ifDescr": "logical", "ifType": "l2vlan"},
        ]
        port_stack = [{"high_port_id": 1, "low_port_id": 2}]

        result = mock_librenms_api.resolve_port_relationships(
            ports,
            port_stack,
            lag_patterns={"x": r"^bundle-\d+$"},
            interface_name_field="ifName",
            compiled_sap_patterns=[],
        )

        assert result == {"lag_members": {1: 2}, "sub_interfaces": {4: 3}}

    def test_cisco_ios_lag_via_name_pattern(self, mock_librenms_api, ios_lag_patterns):
        """Cisco IOS: Po10 has propVirtual type but is a LAG via name pattern."""
        result = mock_librenms_api.resolve_port_relationships(
            CISCO_IOS_PORTS, CISCO_IOS_PORT_STACK[:1], lag_patterns=ios_lag_patterns
        )
        assert result["lag_members"] == {301: 302}

    def test_non_numeric_dotted_suffix_not_stripped_to_base(self, mock_librenms_api):
        """A dotted interface name with a NON-numeric suffix is a legitimate physical name, not a Junos sub-unit (.N), and must NOT be remapped to its base port during LAG physical-resolution."""
        ports = [
            {"port_id": 301, "ifName": "xe-0/0/0", "ifType": "ethernetCsmacd"},  # the base port
            {"port_id": 302, "ifName": "xe-0/0/0.foo", "ifType": "ethernetCsmacd"},  # non-numeric suffix
            {"port_id": 303, "ifName": "ae1", "ifType": "ieee8023adLag"},
        ]
        stack = [{"high_port_id": 303, "low_port_id": 302}]  # ae1 <- xe-0/0/0.foo
        result = mock_librenms_api.resolve_port_relationships(ports, stack, lag_patterns={})
        # The member is the actual port 302, NOT the base 301 (which the old .N-agnostic strip
        # would have wrongly resolved "xe-0/0/0.foo" to).
        assert result["lag_members"] == {302: 303}
        assert 301 not in result["lag_members"]

    def test_cisco_ios_sub_interface(self, mock_librenms_api, ios_lag_patterns):
        """Cisco IOS: Po10 -> Po10.100 detected as sub-interface."""
        result = mock_librenms_api.resolve_port_relationships(
            CISCO_IOS_PORTS, CISCO_IOS_PORT_STACK[1:], lag_patterns=ios_lag_patterns
        )
        assert result["sub_interfaces"] == {303: 302}

    def test_arcos_lag_membership(self, mock_librenms_api):
        """ArcOS: swp4 member of bond1 (ieee8023adLag)."""
        result = mock_librenms_api.resolve_port_relationships(ARCOS_PORTS, ARCOS_PORT_STACK[:1], lag_patterns={})
        assert result["lag_members"] == {401: 402}

    def test_arcos_sub_interface_ethernetcsmacd(self, mock_librenms_api):
        """ArcOS: swp15.3 sub-interface of swp15 (both ethernetCsmacd -- not propVirtual)."""
        result = mock_librenms_api.resolve_port_relationships(ARCOS_PORTS, ARCOS_PORT_STACK[1:], lag_patterns={})
        assert result["sub_interfaces"] == {404: 403}

    def test_junos_aggregate_unit_gets_its_aggregate_as_parent(self, mock_librenms_api):
        """Junos never stacks ae2.0 on ae2, so the aggregate unit's parent must come from its name."""
        # Verbatim shape from a live MX304 (LibreNMS device 20): ifStack carries
        # et-0/0/6 <-> et-0/0/6.0 and et-0/0/6.0 <-> ae2.0, and no row at all for ae2.0 <-> ae2.
        ports = [
            {"port_id": 4301, "ifName": "et-0/0/6", "ifType": "ethernetCsmacd"},
            {"port_id": 4302, "ifName": "et-0/0/6.0", "ifType": "propVirtual"},
            {"port_id": 4303, "ifName": "ae2", "ifType": "ieee8023adLag"},
            {"port_id": 4304, "ifName": "ae2.0", "ifType": "ieee8023adLag"},
        ]
        port_stack = [
            {"high_port_id": 4301, "low_port_id": 4302},
            {"high_port_id": 4302, "low_port_id": 4304},
        ]

        result = mock_librenms_api.resolve_port_relationships(ports, port_stack, lag_patterns={}, device_os="junos")

        assert result["lag_members"] == {4301: 4303}
        assert result["sub_interfaces"] == {4302: 4301, 4304: 4303}

    def test_name_derived_parent_uses_configured_field_only(self, mock_librenms_api):
        """A name-derived parent comes from ifDescr without adding the ifName parent."""
        ports = [
            {"port_id": 11, "ifName": "primary", "ifDescr": "xe-0/0/1", "ifType": "ethernetCsmacd"},
            {"port_id": 12, "ifName": "alternate", "ifDescr": "secondary", "ifType": "ethernetCsmacd"},
            {"port_id": 13, "ifName": "alternate.1", "ifDescr": "xe-0/0/1.1", "ifType": "propVirtual"},
        ]

        result = mock_librenms_api.resolve_port_relationships(
            ports, [], lag_patterns={}, interface_name_field="ifDescr", compiled_sap_patterns=[]
        )

        assert result == {"lag_members": {}, "sub_interfaces": {13: 11}}

    def test_mutual_cross_field_names_resolve_from_ifname_only(self, mock_librenms_api):
        """A cross-field mutual pair resolves from ifName only."""
        ports = [
            {"port_id": 1, "ifName": "Eth1.1", "ifDescr": "service", "ifType": "l2vlan"},
            {"port_id": 2, "ifName": "Eth1", "ifDescr": "service.1", "ifType": "ethernetCsmacd"},
        ]

        result = mock_librenms_api.resolve_port_relationships(ports, [], lag_patterns={})

        assert result["sub_interfaces"] == {1: 2}

    def test_disagreeing_name_fields_resolve_from_ifname_only(self, mock_librenms_api):
        """A child whose fields name different parents resolves from ifName only."""
        ports = [
            {"port_id": 11, "ifName": "Gi0/1", "ifDescr": "core-a", "ifType": "ethernetCsmacd"},
            {"port_id": 12, "ifName": "Gi0/2", "ifDescr": "core-b", "ifType": "ethernetCsmacd"},
            {"port_id": 13, "ifName": "Gi0/1.100", "ifDescr": "core-b.100", "ifType": "l2vlan"},
        ]

        result = mock_librenms_api.resolve_port_relationships(ports, [], lag_patterns={})

        assert result["sub_interfaces"] == {13: 11}

    def test_name_derived_parents_use_ifname_only(self, mock_librenms_api):
        """Name-derived parents form the hierarchy described by ifName only."""
        ports = [
            {"port_id": 1, "ifName": "a.1", "ifDescr": "x", "ifType": "propVirtual"},
            {"port_id": 2, "ifName": "a", "ifDescr": "b.1", "ifType": "ethernetCsmacd"},
            {"port_id": 3, "ifName": "a.1.9", "ifDescr": "b", "ifType": "propVirtual"},
        ]

        result = mock_librenms_api.resolve_port_relationships(ports, [], lag_patterns={}, compiled_sap_patterns=[])

        assert result["sub_interfaces"] == {1: 2, 3: 1}

    def test_unrelated_stated_row_does_not_mix_name_fields(self, mock_librenms_api):
        """An ifDescr-only stated row does not enter the ifName relationship graph."""
        ports = [
            {"port_id": 31, "ifName": "ae0", "ifDescr": "q.7", "ifType": "ethernetCsmacd"},
            {"port_id": 32, "ifName": "ae0.1", "ifDescr": "a-desc", "ifType": "propVirtual"},
            {"port_id": 34, "ifName": "ae0.2.3", "ifDescr": "q", "ifType": "propVirtual"},
            {"port_id": 33, "ifName": "ae0.2", "ifDescr": "c-desc", "ifType": "propVirtual"},
        ]
        port_stack = [{"high_port_id": 34, "low_port_id": 31}]

        result = mock_librenms_api.resolve_port_relationships(
            ports, port_stack, lag_patterns={}, compiled_sap_patterns=[]
        )

        assert result["sub_interfaces"] == {32: 31, 33: 31, 34: 33}

    def test_stated_relationships_use_ifname_only(self, mock_librenms_api):
        """Stated relationships form the hierarchy described by ifName only."""
        ports = [
            {"port_id": 41, "ifName": "x.1", "ifDescr": "a1", "ifType": "propVirtual"},
            {"port_id": 42, "ifName": "x", "ifDescr": "y.1", "ifType": "ethernetCsmacd"},
            {"port_id": 43, "ifName": "x.1.2", "ifDescr": "y", "ifType": "propVirtual"},
            {"port_id": 44, "ifName": "x.1.2.3", "ifDescr": "d1", "ifType": "propVirtual"},
        ]
        port_stack = [
            {"high_port_id": 42, "low_port_id": 41},
            {"high_port_id": 43, "low_port_id": 42},
            {"high_port_id": 41, "low_port_id": 43},
        ]

        result = mock_librenms_api.resolve_port_relationships(
            ports, port_stack, lag_patterns={}, compiled_sap_patterns=[]
        )

        assert result["sub_interfaces"] == {41: 42, 43: 41, 44: 43}

    def test_name_derived_parents_still_resolve_a_deep_chain(self, mock_librenms_api):
        """Single-field name derivation resolves a legitimate grandparent chain."""
        ports = [
            {"port_id": 21, "ifName": "et-0/0/1", "ifType": "ethernetCsmacd"},
            {"port_id": 22, "ifName": "et-0/0/1.1", "ifType": "propVirtual"},
            {"port_id": 23, "ifName": "et-0/0/1.1.2", "ifType": "propVirtual"},
        ]

        result = mock_librenms_api.resolve_port_relationships(ports, [], lag_patterns={}, compiled_sap_patterns=[])

        assert result["sub_interfaces"] == {22: 21, 23: 22}

    def test_name_derived_parent_needs_a_real_base_port(self, mock_librenms_api):
        """A dotted name whose base is not a port on the device gets no invented parent."""
        ports = [
            {"port_id": 4501, "ifName": "ae7.0", "ifType": "ieee8023adLag"},
            {"port_id": 4502, "ifName": "lo0.16385", "ifType": "softwareLoopback"},
        ]

        result = mock_librenms_api.resolve_port_relationships(ports, [], lag_patterns={})

        assert result["sub_interfaces"] == {}

    @pytest.mark.django_db
    def test_junos_channelized_colon_ports_keep_their_relationships(self, mock_librenms_api):
        """A Junos breakout port (xe-1/1/3:1) must not be caught by another vendor's SAP rule."""
        from netbox_librenms_plugin.models import PortStackLagPattern

        # The colon SAP rule must actually be stored, or OS scoping is not what keeps these edges.
        assert PortStackLagPattern.objects.filter(librenms_os="timos", sap_name_pattern=":").exists()
        # Verbatim shape from a live MX480 (LibreNMS device 9), where the colon guard dropped
        # 77 of 156 usable ifStack rows and left ten aggregates with no members at all.
        ports = [
            {"port_id": 4601, "ifName": "xe-1/1/3:1", "ifType": "ethernetCsmacd"},
            {"port_id": 4602, "ifName": "xe-1/1/3:1.0", "ifType": "propVirtual"},
            {"port_id": 4603, "ifName": "ae0", "ifType": "ieee8023adLag"},
            {"port_id": 4604, "ifName": "ae0.0", "ifType": "ieee8023adLag"},
        ]
        port_stack = [
            {"high_port_id": 4601, "low_port_id": 4602},
            {"high_port_id": 4602, "low_port_id": 4604},
        ]

        # lag_patterns stays unset: supplying it skips the OS-scoped SAP read, which is the
        # exact decision this test guards.
        result = mock_librenms_api.resolve_port_relationships(ports, port_stack, device_os="junos")

        assert result["lag_members"] == {4601: 4603}
        assert result["sub_interfaces"][4602] == 4601

    def test_sub_interface_detected_when_parent_is_low(self, mock_librenms_api):
        """Sub-interface parenting is position-independent: a pair emitted child=high/parent=low must resolve, not fall through to the LAG branch and get dropped by the self-reference guard."""
        ports = [
            {"port_id": 601, "ifName": "Gi0/1", "ifType": "ethernetCsmacd"},  # parent
            {"port_id": 602, "ifName": "Gi0/1.100", "ifType": "l2vlan"},  # sub-interface child
        ]
        # Reverse ordering: the child (Gi0/1.100) is the HIGH side, the parent (Gi0/1) the LOW.
        # The forward-only check (l_name.startswith(h_name + '.')) misses this; the reverse
        # check must catch it. Map is child -> parent.
        reverse_stack = [{"high_port_id": 602, "low_port_id": 601}]
        result = mock_librenms_api.resolve_port_relationships(ports, reverse_stack, lag_patterns={})
        assert result["sub_interfaces"] == {602: 601}
        # And the forward ordering still resolves identically (child stays the map key).
        forward_stack = [{"high_port_id": 601, "low_port_id": 602}]
        result = mock_librenms_api.resolve_port_relationships(ports, forward_stack, lag_patterns={})
        assert result["sub_interfaces"] == {602: 601}

    @pytest.mark.parametrize(
        ("high_id", "low_id"),
        [
            (1, 2),
            (2, 1),
        ],
    )
    def test_cross_field_mutual_pair_resolves_from_ifname_in_either_stack_order(
        self, mock_librenms_api, high_id, low_id
    ):
        """A stated cross-field mutual pair resolves from ifName in either row order."""
        ports = [
            {
                "port_id": 1,
                "ifName": "Eth1.1",
                "ifDescr": "service",
                "ifType": "l2vlan",
            },
            {
                "port_id": 2,
                "ifName": "Eth1",
                "ifDescr": "service.1",
                "ifType": "ethernetCsmacd",
            },
        ]
        port_stack = [{"high_port_id": high_id, "low_port_id": low_id}]

        result = mock_librenms_api.resolve_port_relationships(ports, port_stack, lag_patterns={})

        assert result["sub_interfaces"] == {1: 2}

    def test_both_aggregate_disambiguated_by_structural_iftype(self, mock_librenms_api):
        """A too-broad name pattern that matches the member too marks both sides as aggregates; the structural ieee8023adLag signal must break the tie instead of dropping the membership."""
        ports = [
            {"port_id": 701, "ifName": "bond0", "ifType": "ieee8023adLag"},  # the real aggregate
            {"port_id": 702, "ifName": "bond0-slave", "ifType": "ethernetCsmacd"},  # member
        ]
        stack = [{"high_port_id": 702, "low_port_id": 701}]
        # 'bond' (unanchored) matches BOTH bond0 and bond0-slave, so name-matching alone makes
        # both look like aggregates. The aggregate is the one whose ifType is ieee8023adLag.
        broad = {"linux": r"bond"}
        result = mock_librenms_api.resolve_port_relationships(ports, stack, lag_patterns=broad)
        assert result["lag_members"] == {702: 701}

    def test_both_aggregate_ambiguous_is_skipped(self, mock_librenms_api):
        """When both sides name-match AND neither is structurally ieee8023adLag, the aggregate is genuinely ambiguous, so no (possibly wrong) membership is guessed."""
        ports = [
            {"port_id": 711, "ifName": "bond0", "ifType": "ethernetCsmacd"},
            {"port_id": 712, "ifName": "bond0-slave", "ifType": "ethernetCsmacd"},
        ]
        stack = [{"high_port_id": 712, "low_port_id": 711}]
        broad = {"linux": r"bond"}
        result = mock_librenms_api.resolve_port_relationships(ports, stack, lag_patterns=broad)
        assert result["lag_members"] == {}

    def test_empty_port_stack_returns_empty_maps(self, mock_librenms_api):
        """Empty port_stack returns empty dicts."""
        result = mock_librenms_api.resolve_port_relationships(NOKIA_PORTS, [], lag_patterns={})
        assert result == {"lag_members": {}, "sub_interfaces": {}}

    def test_missing_port_ids_are_skipped(self, mock_librenms_api):
        """Entries where high_port_id or low_port_id is absent from ports list are skipped."""
        stack = [{"high_port_id": 9999, "low_port_id": 101}]
        result = mock_librenms_api.resolve_port_relationships(NOKIA_PORTS, stack, lag_patterns={})
        assert result["lag_members"] == {}

    def test_non_dict_port_entries_are_skipped(self, mock_librenms_api):
        """A malformed (non-dict) entry in `ports` must not crash resolution; valid pairs still resolve."""
        ports = [None, "oops", 42, *NOKIA_PORTS]
        result = mock_librenms_api.resolve_port_relationships(ports, NOKIA_PORT_STACK[:1], lag_patterns={})
        assert result["lag_members"] == {101: 102}

    def test_non_string_ifname_does_not_crash_and_resolves_by_iftype(self, mock_librenms_api):
        """A non-string ifName must not crash the downstream string ops; membership still resolves by ifType.

        The member's malformed name is irrelevant to LAG classification — the aggregate (502) is
        authoritatively a LAG via ifType, and the port_stack pairs 501 to it — so the membership
        resolves. The no-crash guarantee holds because every name op iterates _port_names (which
        skips the non-string), never the raw ifName.
        """
        ports = [
            {"port_id": 501, "ifName": 12345, "ifType": "ethernetCsmacd"},  # malformed: non-string
            {"port_id": 502, "ifName": "lag9", "ifType": "ieee8023adLag"},
        ]
        stack = [{"high_port_id": 501, "low_port_id": 502}]
        result = mock_librenms_api.resolve_port_relationships(ports, stack, lag_patterns={})
        assert result == {"lag_members": {501: 502}, "sub_interfaces": {}}

    @pytest.mark.django_db
    def test_db_patterns_scoped_to_device_os(self, mock_librenms_api):
        """With device_os set, only that OS's stored pattern is loaded — a pattern from a different platform must not classify this device's interfaces (the round-12 finding)."""
        from netbox_librenms_plugin.models import PortStackLagPattern

        PortStackLagPattern.objects.create(librenms_os="ztest_pochannel", lag_name_pattern=r"^Po\d+$")
        PortStackLagPattern.objects.create(librenms_os="ztest_bond", lag_name_pattern=r"^bond\d+$")

        # device_os matches the Po-channel pattern → Po10 (propVirtual) classified as a LAG.
        scoped = mock_librenms_api.resolve_port_relationships(
            CISCO_IOS_PORTS, CISCO_IOS_PORT_STACK[:1], device_os="ztest_pochannel"
        )
        assert scoped["lag_members"] == {301: 302}

        # device_os scopes to the bond pattern only; Po10 is not matched → no LAG.
        other = mock_librenms_api.resolve_port_relationships(
            CISCO_IOS_PORTS, CISCO_IOS_PORT_STACK[:1], device_os="ztest_bond"
        )
        assert other["lag_members"] == {}

    @pytest.mark.django_db
    def test_db_patterns_unscoped_when_no_device_os(self, mock_librenms_api):
        """Without device_os, every stored pattern is loaded (legacy behaviour)."""
        from netbox_librenms_plugin.models import PortStackLagPattern

        # Migration 0013 seeds ios/iosxe with ^Po\d+$, which already classifies Po10. Use a name
        # no seeded row can match, so only the unscoped load of every stored pattern explains it.
        PortStackLagPattern.objects.create(librenms_os="ztest_zagg", lag_name_pattern=r"^Zagg\d+$")
        ports = [
            {"port_id": 801, "ifName": "Te1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 802, "ifName": "Zagg7", "ifType": "propVirtual"},
        ]

        result = mock_librenms_api.resolve_port_relationships(ports, [{"high_port_id": 801, "low_port_id": 802}])
        assert result["lag_members"] == {801: 802}

    @pytest.mark.django_db
    def test_db_patterns_non_string_device_os_disables_name_patterns(self, mock_librenms_api):
        """A present-but-unusable device_os (LibreNMS can return ``os`` as a number) must not crash on ``.strip()`` AND must DISABLE name-pattern matching — falling back to every stored vendor regex would re-globalize them. Only an OMITTED os (None) is unscoped."""
        from netbox_librenms_plugin.models import PortStackLagPattern

        PortStackLagPattern.objects.create(librenms_os="ztest_pochannel", lag_name_pattern=r"^Po\d+$")

        # device_os=123 (int) would raise AttributeError on device_os.strip() before the original fix;
        # now it must fail closed (no name-pattern LAG match) rather than load every pattern.
        result = mock_librenms_api.resolve_port_relationships(CISCO_IOS_PORTS, CISCO_IOS_PORT_STACK[:1], device_os=123)
        assert result["lag_members"] == {}  # name-pattern matching disabled for a malformed os

    def test_invalid_lag_pattern_is_skipped_and_logged(self, mock_librenms_api, caplog):
        """A configured LAG pattern that isn't valid regex is skipped (it must not crash relationship resolution) AND logged at WARNING — otherwise a user with a typo in their PortStackLagPattern has no way to tell why LAG detection silently isn't working for that OS."""
        import logging

        bad = "([unterminated"  # invalid regex → re.error on compile
        with caplog.at_level(logging.WARNING, logger="netbox_librenms_plugin.librenms_api"):
            result = mock_librenms_api.resolve_port_relationships(
                NOKIA_PORTS, NOKIA_PORT_STACK[:1], lag_patterns={"junos": bad}
            )
        # Resolution still completes: the LAG is matched structurally (ieee8023adLag ifType),
        # independent of the broken name pattern.
        assert result["lag_members"] == {101: 102}
        # The invalid pattern was reported at WARNING, naming the offending pattern string.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(bad in r.getMessage() for r in warnings)

    def test_non_string_lag_pattern_is_skipped_not_crashed(self, mock_librenms_api, caplog):
        """A non-string value in the explicit lag_patterns dict is skipped, not crashed: re.compile raises TypeError (not re.error), so unlike the DB-backed path (strings guaranteed) it must be caught too."""
        import logging

        # None (or any non-string) → re.compile raises TypeError, not re.error. Before the fix this
        # propagated out of resolve_port_relationships and crashed the whole refresh.
        with caplog.at_level(logging.WARNING, logger="netbox_librenms_plugin.librenms_api"):
            result = mock_librenms_api.resolve_port_relationships(
                NOKIA_PORTS, NOKIA_PORT_STACK[:1], lag_patterns={"junos": None}
            )
        # Resolution still completes via structural (ieee8023adLag) detection; the bad value skipped.
        assert result["lag_members"] == {101: 102}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("None" in r.getMessage() for r in warnings)

    def test_ambiguous_shared_name_is_not_indexed_to_either_port(self, mock_librenms_api):
        """A name carried by two different ports indexes neither, so a duplicate label can't hijack the base-name lookup and bind a sub-unit's LAG membership onto the wrong aggregate."""
        ports = [
            {"port_id": 201, "ifName": "xe-0/0/0", "ifType": "ethernetCsmacd"},  # physical member
            {"port_id": 203, "ifName": "ae1", "ifType": "ieee8023adLag"},  # the REAL ae1 aggregate
            {"port_id": 204, "ifName": "ae1.0", "ifType": "ieee8023adLag"},  # sub-unit of ae1
            # A DIFFERENT aggregate whose ifDescr coincidentally equals "ae1" (ifName mode still
            # indexes ifDescr). Processed last, so last-write-wins would point by_name["ae1"] HERE.
            {"port_id": 999, "ifName": "ae99", "ifDescr": "ae1", "ifType": "ieee8023adLag"},
        ]
        port_stack = [{"high_port_id": 201, "low_port_id": 204}]  # xe-0/0/0 <-> ae1.0
        result = mock_librenms_api.resolve_port_relationships(ports, port_stack, lag_patterns={})
        # The ifName base resolves to the real aggregate 203. The unrelated ifDescr on 999 does
        # not make the ifName lookup ambiguous and cannot hijack the edge.
        assert 999 not in result["lag_members"].values()
        assert result["lag_members"] == {201: 203}

    def test_physical_resolution_does_not_cross_interface_name_fields(self, mock_librenms_api):
        """A base name in ifName must not resolve through another port's ifDescr."""
        ports = [
            {"port_id": 201, "ifName": "xe-0/0/0", "ifType": "ethernetCsmacd"},
            {"port_id": 204, "ifName": "ae1.0", "ifType": "l2vlan"},
            {
                "port_id": 999,
                "ifName": "ae99",
                "ifDescr": "ae1",
                "ifType": "ieee8023adLag",
            },
        ]
        port_stack = [{"high_port_id": 201, "low_port_id": 204}]

        result = mock_librenms_api.resolve_port_relationships(
            ports,
            port_stack,
            lag_patterns={"junos": r"^ae\d"},
            interface_name_field="ifDescr",
        )

        assert 999 not in result["lag_members"].values()
        assert result["lag_members"] == {201: 204}
