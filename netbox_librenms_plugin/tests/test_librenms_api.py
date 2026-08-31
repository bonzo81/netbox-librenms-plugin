"""
Comprehensive tests for LibreNMSAPI client.

This module provides 100% test coverage for netbox_librenms_plugin/librenms_api.py,
with particular focus on HTTP method correctness to prevent regression bugs.
"""

import inspect
import re
import time
from types import SimpleNamespace

import pytest
import requests


class TestApiTokenStaysOnItsHost:
    """
    A redirect off the configured LibreNMS must not carry the API token with it.

    ``requests`` drops only ``Authorization`` when a redirect crosses hosts and forwards every
    other header, so ``X-Auth-Token`` reached whatever answered the redirect.
    """

    @staticmethod
    def _redirect(from_url, to_url):
        """Return a real (redirected request, original response) pair for the auth rebuild."""
        original = requests.Request("GET", from_url, headers={"X-Auth-Token": "secret-token"}).prepare()
        response = requests.Response()
        response.request = original
        redirected = original.copy()
        redirected.url = to_url
        return redirected, response

    def test_a_cross_host_redirect_drops_the_token(self):
        from netbox_librenms_plugin.librenms_api import _TokenScopedSession

        redirected, response = self._redirect(
            "https://librenms.example/api/v0/devices", "https://elsewhere.example/api/v0/devices"
        )

        _TokenScopedSession().rebuild_auth(redirected, response)

        assert "X-Auth-Token" not in redirected.headers

    def test_a_same_host_redirect_keeps_the_token(self):
        """A reverse proxy in front of LibreNMS may redirect within the host; that must still work."""
        from netbox_librenms_plugin.librenms_api import _TokenScopedSession

        redirected, response = self._redirect(
            "https://librenms.example/api/v0/devices", "https://librenms.example/api/v0/devices/"
        )

        _TokenScopedSession().rebuild_auth(redirected, response)

        assert redirected.headers["X-Auth-Token"] == "secret-token"

    def test_a_downgrade_to_http_drops_the_token(self):
        """Same host, weaker scheme: the token would cross the network in cleartext."""
        from netbox_librenms_plugin.librenms_api import _TokenScopedSession

        redirected, response = self._redirect(
            "https://librenms.example/api/v0/devices", "http://librenms.example/api/v0/devices"
        )

        _TokenScopedSession().rebuild_auth(redirected, response)

        assert "X-Auth-Token" not in redirected.headers

    def test_an_upgrade_to_https_keeps_the_token(self):
        """The guard is about losing confidentiality, so gaining it must not break the client."""
        from netbox_librenms_plugin.librenms_api import _TokenScopedSession

        redirected, response = self._redirect(
            "http://librenms.example/api/v0/devices", "https://librenms.example/api/v0/devices"
        )

        _TokenScopedSession().rebuild_auth(redirected, response)

        assert redirected.headers["X-Auth-Token"] == "secret-token"

    def test_the_client_issues_its_requests_through_that_session(self):
        """The guard is only worth anything if the shared session is what the client calls."""
        from netbox_librenms_plugin import librenms_api

        assert isinstance(librenms_api._session, librenms_api._TokenScopedSession)
        source = inspect.getsource(librenms_api)
        # Check every verb, not only the ones in use today: the client already issues PATCH
        # through the session, and any new direct requests call bypasses the token scoping.
        for verb in ("get", "post", "patch", "put", "delete", "head", "options", "request"):
            assert f"requests.{verb}(" not in source, (
                f"a call site still bypasses the token-scoped session with requests.{verb}()"
            )


# =============================================================================
# Initialization
# =============================================================================


class TestLibreNMSAPIInit:
    """Test LibreNMSAPI initialization and configuration loading."""

    def test_init_allows_cleartext_non_loopback_server(self, configure_librenms):
        """An operator may choose HTTP for a remote LibreNMS server."""
        configure_librenms(
            {
                "default": {
                    "librenms_url": "http://librenms.example.test",
                    "api_token": "test-token",
                }
            }
        )

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        assert LibreNMSAPI(server_key="default").librenms_url == "http://librenms.example.test"

    def test_init_allows_cleartext_loopback_server(self, configure_librenms):
        """Local test and development servers may use HTTP without crossing a network."""
        configure_librenms(
            {
                "default": {
                    "librenms_url": "http://127.0.0.1:8000",
                    "api_token": "test-token",
                }
            }
        )

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        assert LibreNMSAPI(server_key="default").librenms_url == "http://127.0.0.1:8000"

    @pytest.mark.parametrize(
        "url",
        [
            "http://librenms.example.test?tenant=test",
            "http://librenms.example.test?",
            "https://librenms.example.test#fragment",
            "https://librenms.example.test#",
        ],
    )
    def test_init_rejects_query_and_fragment_in_base_url(self, configure_librenms, url):
        """A query or fragment must not absorb the API path appended to the base URL."""
        configure_librenms({"default": {"librenms_url": url, "api_token": "test-token"}})

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with pytest.raises(ValueError, match="must not include a query or fragment"):
            LibreNMSAPI(server_key="default")

    @pytest.mark.parametrize(
        "url",
        [
            "https://librenms.example.test:bad",
            "https://librenms.example.test:99999",
        ],
    )
    def test_a_server_with_an_unusable_port_is_neither_offered_nor_built(self, configure_librenms, url, caplog):
        """The picker must not offer a server the constructor then refuses to bind."""
        configure_librenms({"default": {"librenms_url": url, "api_token": "test-token"}})

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        assert LibreNMSAPI.get_available_servers() == {}
        assert "valid HTTP(S) librenms_url" in caplog.text
        assert url not in caplog.text
        assert "test-token" not in caplog.text
        with pytest.raises(ValueError, match="must use a valid port"):
            LibreNMSAPI(server_key="default")

    def test_init_with_multi_server_config(self, configure_librenms):
        """Verify initialization with multi-server configuration."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        assert api.librenms_url == "https://librenms.example.com"
        assert api.api_token == "test-token"
        assert api.cache_timeout == 300
        assert api.verify_ssl is True

    def test_init_with_legacy_config(self, configure_librenms):
        """Verify initialization with legacy single-server config."""
        configure_librenms(
            None,
            librenms_url="https://legacy.example.com",
            api_token="legacy-token",
            cache_timeout=600,
            verify_ssl=False,
        )

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        assert api.librenms_url == "https://legacy.example.com"

    def test_legacy_mode_normalizes_server_key_to_default(self, configure_librenms):
        """In legacy single-server mode a posted/stale server_key must not survive as api.server_key (it would become a bogus cache/redirect discriminator)."""
        configure_librenms(
            None,
            librenms_url="https://legacy.example.com",
            api_token="legacy-token",
        )

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        # A non-default (stale/tampered) key in legacy mode must normalize to "default".
        api = LibreNMSAPI(server_key="ghost")
        assert api.server_key == "default"

    @pytest.mark.django_db  # an unset key falls back to the stored selected_server, which is a DB read
    def test_init_non_string_server_key_falls_back_cleanly(self, configure_librenms):
        """An unhashable non-string server_key (e.g. a list) is treated as unset, not raised on at the dict membership check."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        # Pre-fix: `[] not in servers_config` raised TypeError (unhashable type: 'list').
        api = LibreNMSAPI(server_key=[])

        assert api.server_key == "default"
        assert api.librenms_url == "https://librenms.example.com"

    def test_init_fallback_skips_incomplete_server_entry(self, configure_librenms):
        """The auto-default fallback skips a structurally incomplete entry (dict without url/token) and picks the first usable server."""
        configure_librenms({"bad": {}, "prod": {"librenms_url": "https://prod.example.com", "api_token": "prod-token"}})

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        # 'default' isn't configured → auto-default fallback; it must skip 'bad' and pick 'prod'.
        api = LibreNMSAPI(server_key="default")

        assert api.server_key == "prod"
        assert api.librenms_url == "https://prod.example.com"

    def test_init_missing_config_raises_valueerror(self, configure_librenms):
        """Verify ValueError raised when configuration is missing."""
        configure_librenms(None)

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        # Use the default key so this exercises the missing URL/token ValueError path in legacy
        # mode (no configured 'servers' dict, no librenms_url/api_token). Pin the message so an
        # unrelated ValueError (e.g. the misconfigured-mapping guard) can't keep this test green.
        with pytest.raises(ValueError, match=r"URL or API token is not configured for server 'default'"):
            LibreNMSAPI(server_key="default")

    def test_init_nonexistent_server_key_raises_keyerror(self, configure_librenms):
        """Verify KeyError raised when specific server_key doesn't exist."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with pytest.raises(KeyError, match="nonexistent"):
            LibreNMSAPI(server_key="nonexistent")

    def test_init_legacy_mode_binds_single_server_for_non_default_key(self, configure_librenms):
        """Legacy single-server mode has only the implicit 'default' server, bound to the single configured URL/token."""
        configure_librenms(
            None,
            librenms_url="https://legacy.example.com",
            api_token="legacy-token",
        )

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="stale")  # must not raise in legacy mode
        assert api.librenms_url == "https://legacy.example.com"

        # The default key must still work in legacy mode.
        assert LibreNMSAPI(server_key="default").librenms_url == "https://legacy.example.com"

    def test_init_default_falls_back_to_first_server(self, configure_librenms):
        """Verify 'default' key falls back to first configured server."""
        configure_librenms(
            {
                "primary": {
                    "librenms_url": "https://primary.example.com",
                    "api_token": "primary-token",
                }
            }
        )

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        assert api.server_key == "primary"
        assert api.librenms_url == "https://primary.example.com"

    @pytest.mark.django_db
    def test_init_stale_auto_selected_server_falls_back(self, configure_librenms):
        """Issue #110: a stale LibreNMSSettings.selected_server (no longer configured) must fall back to the first server rather than hard-fail — it was auto-resolved, not explicitly requested, so the KeyError guard must not fire."""
        configure_librenms({"primary": {"librenms_url": "https://primary.example.com", "api_token": "t"}})
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "gone-server"})

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI()  # server_key=None -> auto-resolved from (stale) settings
        assert api.server_key == "primary"

    @pytest.mark.django_db
    def test_init_blank_server_key_treated_as_non_explicit_falls_back(self, configure_librenms):
        """A blank/whitespace server_key is 'no key': a stale selected_server must still fall back, not hard-fail. Treating '' as explicit would mark the auto-resolved key explicit and defeat the issue #110 fallback (KeyError)."""
        configure_librenms({"primary": {"librenms_url": "https://primary.example.com", "api_token": "t"}})
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "gone-server"})

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        for blank in ("", "   "):
            api = LibreNMSAPI(server_key=blank)  # blank → auto-resolved from the (stale) settings
            assert api.server_key == "primary"

    def test_init_skips_leading_malformed_server_entry(self, configure_librenms):
        """Issue #110: the first-entry fallback must pick the first *valid* mapping, skipping a malformed (non-dict) leading entry that would otherwise raise at the config read."""
        configure_librenms(
            {
                "broken": "not-a-dict",
                "good": {"librenms_url": "https://good.example.com", "api_token": "t"},
            }
        )

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        assert api.server_key == "good"
        assert api.librenms_url == "https://good.example.com"

    def test_init_all_malformed_server_entries_raise_valueerror(self, configure_librenms):
        """Issue #110: when no configured entry is a valid mapping, raise a clear ValueError instead of crashing on the malformed entry's config read."""
        configure_librenms({"broken": "not-a-dict", "also-bad": 123})

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with pytest.raises(ValueError):
            LibreNMSAPI(server_key="default")

    def test_init_selected_key_malformed_raises_valueerror_not_typeerror(self, configure_librenms):
        """Issue #110: an explicitly requested key that *exists* but maps to a non-dict must raise a clear ValueError, not an opaque TypeError from indexing the string at the config read."""
        configure_librenms({"broken": "not-a-dict"})

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with pytest.raises(ValueError, match="expected a mapping"):
            LibreNMSAPI(server_key="broken")

    def test_get_available_servers_skips_malformed_entry(self, configure_librenms):
        """get_available_servers() powers the sync-POST server-key membership check."""
        configure_librenms(
            {
                "good": {
                    "librenms_url": "https://good.example.com",
                    "api_token": "t",
                    "display_name": "Good",
                },
                "broken": "not-a-dict",
            }
        )

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        servers = LibreNMSAPI.get_available_servers()

        assert servers == {"good": "Good"}
        assert "broken" not in servers

    def test_init_incomplete_dict_entry_raises_valueerror_not_keyerror(self, configure_librenms):
        """A dict-shaped but incomplete entry ({"bad": {}}) passes the isinstance check, so the config read must fail with ValueError, not an opaque KeyError on config['librenms_url']."""
        configure_librenms({"bad": {}})

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with pytest.raises(ValueError):
            LibreNMSAPI(server_key="bad")

    def test_get_available_servers_skips_incomplete_dict_entry(self, configure_librenms):
        """An incomplete dict entry (no librenms_url/api_token) must not be selectable — it would pass the membership check and then crash LibreNMSAPI construction."""
        configure_librenms(
            {
                "good": {
                    "librenms_url": "https://good.example.com",
                    "api_token": "t",
                    "display_name": "Good",
                },
                "bad": {},
                "tokenless": {"librenms_url": "https://x.example.com"},
            }
        )

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        servers = LibreNMSAPI.get_available_servers()

        assert servers == {"good": "Good"}
        assert "bad" not in servers
        assert "tokenless" not in servers

    def test_init_non_mapping_server_config_raises_valueerror(self, configure_librenms):
        """A structurally invalid (non-mapping) server entry must raise ValueError, not leak a TypeError from the dict access — so build_librenms_api falls back to None."""
        configure_librenms({"badserver": None})

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI, build_librenms_api

        with pytest.raises(ValueError, match="misconfigured"):
            LibreNMSAPI(server_key="badserver")
        # build_librenms_api must convert that into a clean None, not a 500.
        assert build_librenms_api("badserver") is None


# =============================================================================
# Connection testing
# =============================================================================


class TestLibreNMSAPIConnection:
    """Test connection testing functionality."""

    PATH = "/api/v0/system"

    def test_connection_success(self, local_librenms_api, librenms_server):
        """Verify successful connection test."""
        librenms_server.register(
            self.PATH,
            {"status": "ok", "system": [{"local_ver": "24.1.0"}]},
            method="GET",
        )

        result = local_librenms_api.test_connection()

        assert result == {"local_ver": "24.1.0"}
        assert [(request["method"], request["path"]) for request in librenms_server.requests] == [("GET", self.PATH)]

    def test_connection_auth_failure_401(self, local_librenms_api, librenms_server):
        """Verify 401 unauthorized handling."""
        librenms_server.register(self.PATH, {"status": "error"}, status=401, method="GET")

        result = local_librenms_api.test_connection()

        assert result.get("error") is True

    def test_connection_auth_failure_403(self, local_librenms_api, librenms_server):
        """Verify 403 forbidden handling."""
        librenms_server.register(self.PATH, {"status": "error"}, status=403, method="GET")

        result = local_librenms_api.test_connection()

        assert result.get("error") is True

    def test_connection_timeout(self, local_librenms_api, librenms_server, monkeypatch):
        """Verify timeout exception handling."""

        def delayed_response(**request):
            time.sleep(0.05)
            return 200, {"status": "ok", "system": [{"local_ver": "late"}]}

        librenms_server.register(self.PATH, delayed_response, method="GET")
        monkeypatch.setattr("netbox_librenms_plugin.librenms_api.DEFAULT_API_TIMEOUT", 0.01)

        result = local_librenms_api.test_connection()

        assert result.get("error") is True
        assert "timeout" in result.get("message", "").lower()


# ====================================================================================
# Device lookup
# ====================================================================================


class TestLibreNMSAPIDeviceLookup:
    """Test device lookup functionality."""

    @staticmethod
    def _device(*, stored_id=None, ip_address=None, name="test-device"):
        primary_ip = None
        if ip_address is not None:
            primary_ip = SimpleNamespace(address=SimpleNamespace(ip=ip_address), dns_name="")
        custom_field_data = {} if stored_id is None else {"librenms_id": stored_id}
        return SimpleNamespace(
            name=name,
            cf=dict(custom_field_data),
            custom_field_data=custom_field_data,
            primary_ip=primary_ip,
            _meta=SimpleNamespace(model_name="device"),
            pk=123,
        )

    def test_get_librenms_id_from_custom_field(self, mock_librenms_api):
        """Returns ID when already stored in cf['librenms_id']."""
        device = self._device(stored_id=42)

        result = mock_librenms_api.get_librenms_id(device)
        assert result == 42

    def test_get_librenms_id_normalizes_string_to_int(self, mock_librenms_api):
        """Returns int for string-stored librenms_id; read path uses auto_save=False so no write-back."""
        device = self._device(stored_id="42")

        result = mock_librenms_api.get_librenms_id(device)
        assert result == 42
        assert device.custom_field_data == {"librenms_id": "42"}

    def test_get_librenms_id_empty_string_falls_through_to_discovery(
        self,
        local_librenms_api,
        librenms_server,
    ):
        """An empty-string librenms_id is treated as not set (falls through to API discovery)."""
        device = self._device(stored_id="")
        path = "/api/v0/devices/test-device"
        librenms_server.register(path, {"status": "ok", "devices": []}, method="GET")

        result = local_librenms_api.get_librenms_id(device)

        assert result is None
        assert [request["path"] for request in librenms_server.requests] == [path]

    def test_get_librenms_id_from_cache(self, mock_librenms_api):
        """Returns ID from Django cache when not in custom field."""
        from django.core.cache import cache

        device = self._device()
        cache.set(mock_librenms_api._get_cache_key(device), 99)

        result = mock_librenms_api.get_librenms_id(device)
        assert result == 99

    def test_get_librenms_id_handles_objects_without_device_identity_attrs(self, mock_librenms_api):
        """Objects like interfaces should return None cleanly when they have no stored or cached ID."""
        interface = SimpleNamespace(
            cf={},
            _meta=SimpleNamespace(model_name="interface"),
            pk=123,
        )

        result = mock_librenms_api.get_librenms_id(interface)

        assert result is None

    def test_get_stored_librenms_id_skips_hostname_lookup(self, local_librenms_api, librenms_server):
        """Stored-only helper must not trigger discovery lookups for interface-like objects."""
        interface = SimpleNamespace(
            cf={},
            name="GigabitEthernet1/0/1",
            _meta=SimpleNamespace(model_name="interface"),
            pk=123,
        )

        result = local_librenms_api.get_stored_librenms_id(interface)

        assert result is None
        assert librenms_server.requests == []

    def test_get_librenms_id_by_ip_lookup(self, local_librenms_api, librenms_server):
        """Performs IP lookup and caches result."""
        from django.core.cache import cache

        device = self._device(ip_address="192.0.2.1")
        path = "/api/v0/devices/192.0.2.1"
        librenms_server.register(
            path,
            {"status": "ok", "devices": [{"device_id": 55}]},
            method="GET",
        )

        result = local_librenms_api.get_librenms_id(device)

        assert result == 55
        assert cache.get(local_librenms_api._get_cache_key(device)) == 55
        assert [request["path"] for request in librenms_server.requests] == [path]

    def test_get_librenms_id_by_hostname_lookup(self, local_librenms_api, librenms_server):
        """Falls back to hostname lookup."""
        device = self._device(ip_address="192.0.2.1")
        ip_path = "/api/v0/devices/192.0.2.1"
        hostname_path = "/api/v0/devices/test-device"
        librenms_server.register(ip_path, {"status": "ok", "devices": []}, method="GET")
        librenms_server.register(
            hostname_path,
            {"status": "ok", "devices": [{"device_id": 77}]},
            method="GET",
        )

        result = local_librenms_api.get_librenms_id(device)

        assert result == 77
        assert [request["path"] for request in librenms_server.requests] == [ip_path, hostname_path]

    def test_get_device_id_by_ip_not_found(self, local_librenms_api, librenms_server):
        """Returns None when IP not found in LibreNMS."""
        path = "/api/v0/devices/192.0.2.99"
        librenms_server.register(path, {"status": "ok", "devices": []}, method="GET")

        result = local_librenms_api.get_device_id_by_ip("192.0.2.99")

        assert result is None
        assert librenms_server.requests[-1]["method"] == "GET"

    def test_get_device_id_by_hostname_not_found(self, local_librenms_api, librenms_server):
        """Returns None when hostname not found."""
        path = "/api/v0/devices/nonexistent.example.com"
        librenms_server.register(path, {"status": "ok", "devices": []}, method="GET")

        result = local_librenms_api.get_device_id_by_hostname("nonexistent.example.com")

        assert result is None
        assert librenms_server.requests[-1]["method"] == "GET"


# =============================================================================
# Device operations
# =============================================================================


class TestLibreNMSAPIDeviceOperations:
    """Test device CRUD operations."""

    DEVICES_PATH = "/api/v0/devices"

    def test_add_device_success(self, local_librenms_api, librenms_server):
        """Verify successful device addition."""
        librenms_server.register(
            self.DEVICES_PATH,
            {"status": "ok", "message": "Device added successfully"},
            method="POST",
        )

        result = local_librenms_api.add_device(
            data={
                "hostname": "test-device.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        assert result[0] is True
        assert result[1] == "Device added successfully."
        assert [(request["method"], request["path"]) for request in librenms_server.requests] == [
            ("POST", self.DEVICES_PATH)
        ]

    def test_add_device_snmpv1_success(self, local_librenms_api, librenms_server):
        """Verify successful device addition using SNMPv1."""
        librenms_server.register(
            self.DEVICES_PATH,
            {"status": "ok", "message": "Device added successfully"},
            method="POST",
        )

        result = local_librenms_api.add_device(
            data={
                "hostname": "legacy-device.example.com",
                "snmp_version": "v1",
                "community": "public",
            }
        )

        assert result[0] is True
        assert result[1] == "Device added successfully."
        payload = librenms_server.requests[-1]["body"]
        assert payload["snmpver"] == "v1"
        assert payload["community"] == "public"

    def test_add_device_duplicate_error(self, local_librenms_api, librenms_server):
        """Verify duplicate device handling."""
        librenms_server.register(
            self.DEVICES_PATH,
            {"status": "error", "message": "Device already exists"},
            method="POST",
        )

        result = local_librenms_api.add_device(
            data={
                "hostname": "duplicate-device.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        assert result[0] is False
        assert "Device already exists" in result[1]

    def test_add_device_snmpv3_success(self, local_librenms_api, librenms_server):
        """Verify successful device addition using SNMPv3 with all required fields."""
        librenms_server.register(
            self.DEVICES_PATH,
            {"status": "ok", "message": "Device added successfully"},
            method="POST",
        )

        result = local_librenms_api.add_device(
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
        payload = librenms_server.requests[-1]["body"]
        assert payload["snmpver"] == "v3"
        assert payload["authlevel"] == "authPriv"
        assert payload["authname"] == "snmpuser"
        assert payload["authpass"] == "authpassword123"
        assert payload["authalgo"] == "SHA"
        assert payload["cryptopass"] == "cryptopassword456"
        assert payload["cryptoalgo"] == "AES"
        # Ensure community is NOT included for v3
        assert "community" not in payload

    def test_update_device_field_success(self, local_librenms_api, librenms_server):
        """Verify successful device field update."""
        path = "/api/v0/devices/123"
        librenms_server.register(
            path,
            {"status": "ok", "message": "Device field updated"},
            method="PATCH",
        )

        success, message = local_librenms_api.update_device_field(
            device_id=123,
            field_data={"field": "notes", "data": "Updated note"},
        )

        assert success is True
        assert "updated" in message.lower()
        assert librenms_server.requests[-1]["method"] == "PATCH"
        assert librenms_server.requests[-1]["body"] == {"field": "notes", "data": "Updated note"}

    def test_get_device_info_success(self, local_librenms_api, librenms_server):
        """Verify retrieving device info."""
        librenms_server.register(
            "/api/v0/devices/123",
            {"status": "ok", "devices": [{"device_id": 123, "hostname": "test-device"}]},
            method="GET",
        )

        success, device_data = local_librenms_api.get_device_info(device_id=123)

        assert success is True
        assert device_data is not None
        assert device_data["device_id"] == 123

    def test_get_device_info_not_found(self, local_librenms_api, librenms_server):
        """Empty devices list returns (False, None) without raising."""
        librenms_server.register(
            "/api/v0/devices/1",
            {"status": "ok", "devices": []},
            method="GET",
        )

        success, result = local_librenms_api.get_device_info(1)
        assert success is False
        assert result is None

    def test_get_device_info_caches_success(self, local_librenms_api, librenms_server):
        """A successful lookup is cached: a second call within the TTL skips the HTTP request."""
        path = "/api/v0/devices/7777"
        librenms_server.register(
            path,
            {"status": "ok", "devices": [{"device_id": 7777, "hostname": "cached-device"}]},
            method="GET",
        )

        first = local_librenms_api.get_device_info(device_id=7777)
        second = local_librenms_api.get_device_info(device_id=7777)

        assert first == second == (True, {"device_id": 7777, "hostname": "cached-device"})
        assert [request["path"] for request in librenms_server.requests] == [path]

    def test_get_device_info_does_not_cache_failure(self, local_librenms_api, librenms_server):
        """Failures are never cached, so a transient error doesn't persist for the cache window."""
        from netbox_librenms_plugin.librenms_api import LibreNMSLookupError

        path = "/api/v0/devices/7778"
        librenms_server.register_disconnect(path, method="GET")

        first_success, first_failure = local_librenms_api.get_device_info(device_id=7778)
        second_success, _second_failure = local_librenms_api.get_device_info(device_id=7778)

        assert (first_success, second_success) == (False, False)
        # A timeout says nothing about whether the device exists, so it is not "not found".
        assert isinstance(first_failure, LibreNMSLookupError)
        assert first_failure.status_code is None
        assert [request["path"] for request in librenms_server.requests] == [path, path]

    def test_get_device_info_use_cache_false_bypasses_stale_cache(self, local_librenms_api, librenms_server):
        """use_cache=False refetches live data instead of returning a stale cached payload, and refreshes the cache."""
        path = "/api/v0/devices/424242"
        responses = iter(
            [
                {"status": "ok", "devices": [{"device_id": 424242, "hostname": "stale"}]},
                {"status": "ok", "devices": [{"device_id": 424242, "hostname": "fresh"}]},
            ]
        )

        def next_response(**request):
            return 200, next(responses)

        librenms_server.register(path, next_response, method="GET")

        # First call populates the 60s cache with the (soon-to-be-stale) value.
        assert local_librenms_api.get_device_info(device_id=424242) == (
            True,
            {"device_id": 424242, "hostname": "stale"},
        )

        # A normal (cached) read still returns the stale value...
        assert local_librenms_api.get_device_info(device_id=424242) == (
            True,
            {"device_id": 424242, "hostname": "stale"},
        )
        # ...but the import path (use_cache=False) fetches live data...
        assert local_librenms_api.get_device_info(device_id=424242, use_cache=False) == (
            True,
            {"device_id": 424242, "hostname": "fresh"},
        )
        # ...and that live fetch refreshes the cache, so subsequent cached reads see the correction.
        assert local_librenms_api.get_device_info(device_id=424242) == (
            True,
            {"device_id": 424242, "hostname": "fresh"},
        )
        assert [request["path"] for request in librenms_server.requests] == [path, path]

    def test_cache_only_reads_a_snapshot_even_when_live_cache_is_disabled(
        self,
        local_librenms_api,
        librenms_server,
    ):
        """A cache-only read must use an existing snapshot regardless of the live-read flag."""
        from django.core.cache import cache

        # Start from a cold fixed key, so the cache-only assertion cannot pass on another
        # test's snapshot instead of the one this test creates.
        cache.delete("librenms_device_info_default_424243")
        path = "/api/v0/devices/424243"
        librenms_server.register(
            path,
            {"status": "ok", "devices": [{"device_id": 424243, "hostname": "cached-device"}]},
            method="GET",
        )
        expected = (True, {"device_id": 424243, "hostname": "cached-device"})
        assert local_librenms_api.get_device_info(device_id=424243) == expected

        assert local_librenms_api.get_device_info(device_id=424243, use_cache=False, cache_only=True) == expected
        assert [request["path"] for request in librenms_server.requests] == [path]

    def test_cache_only_miss_does_not_contact_librenms(self, local_librenms_api, librenms_server):
        """A cache-only miss returns a miss without crossing the HTTP boundary."""
        from django.core.cache import cache

        cache.delete("librenms_device_info_default_424244")

        assert local_librenms_api.get_device_info(device_id=424244, use_cache=False, cache_only=True) == (False, None)
        assert librenms_server.requests == []

    def test_list_devices_with_filters(self, local_librenms_api, librenms_server):
        """Verify listing devices with filter parameter."""
        librenms_server.register(
            self.DEVICES_PATH,
            {"status": "ok", "devices": [{"device_id": 1}, {"device_id": 2}]},
            method="GET",
        )

        success, devices = local_librenms_api.list_devices(filters={"type": "network"})

        assert success is True
        assert len(devices) == 2
        assert librenms_server.requests[-1]["query"] == {"type": ["network"]}


# =============================================================================
# Location operations
# =============================================================================


class TestLibreNMSAPILocationOperations:
    """Test location CRUD operations."""

    RESOURCE_PATH = "/api/v0/resources/locations"
    MUTATION_PATH = "/api/v0/locations"

    def test_get_locations_success(self, local_librenms_api, librenms_server):
        """Verify retrieving all locations."""
        librenms_server.register(
            self.RESOURCE_PATH,
            {"status": "ok", "locations": [{"id": 1, "location": "DC1"}]},
            method="GET",
        )

        success, locations = local_librenms_api.get_locations()

        assert success is True
        assert len(locations) == 1
        assert librenms_server.requests[-1]["method"] == "GET"

    def test_add_location_success(self, local_librenms_api, librenms_server):
        """Verify successful location addition."""
        librenms_server.register(
            self.MUTATION_PATH,
            {"status": "ok", "message": "Location created #5"},
            method="POST",
        )

        success, result_dict = local_librenms_api.add_location(location_data={"location": "DC2"})

        assert success is True
        assert result_dict["id"] == "5"
        assert "message" in result_dict
        assert librenms_server.requests[-1]["method"] == "POST"
        assert librenms_server.requests[-1]["body"] == {"location": "DC2"}

    def test_add_location_error(self, local_librenms_api, librenms_server):
        """Verify location addition error handling."""
        librenms_server.register(
            self.MUTATION_PATH,
            {"status": "error", "message": "Invalid location data"},
            status=500,
            method="POST",
        )

        success, error_msg = local_librenms_api.add_location(location_data={})

        assert success is False
        assert "Invalid location data" in error_msg

    def test_update_location_success(self, local_librenms_api, librenms_server):
        """Verify successful location update."""
        path = f"{self.MUTATION_PATH}/DC1"
        librenms_server.register(
            path,
            {"status": "ok", "message": "Location updated"},
            method="PATCH",
        )

        success, message = local_librenms_api.update_location(
            location_name="DC1",
            location_data={"location": "DC1-Updated"},
        )

        assert success is True
        assert message == "Location updated"
        assert librenms_server.requests[-1]["method"] == "PATCH"
        assert librenms_server.requests[-1]["body"] == {"location": "DC1-Updated"}

    def test_update_location_not_found(self, local_librenms_api, librenms_server):
        """Verify updating non-existent location."""
        librenms_server.register(
            f"{self.MUTATION_PATH}/NonExistent",
            {"status": "error", "message": "Location not found"},
            status=404,
            method="PATCH",
        )

        success, message = local_librenms_api.update_location(location_name="NonExistent", location_data={})

        assert success is False
        assert message == "Location not found"


# =============================================================================
# Ports and inventory
# =============================================================================


class TestLibreNMSAPIPortsAndInventory:
    """Test ports and inventory operations."""

    def test_get_ports_requests_the_vrf_column(self, mock_librenms_api, librenms_server):
        """ifVrf is the per-port VRF join key; without the column nothing downstream can read it."""
        seen = {}

        def respond(method, path, query, headers, body):
            seen["columns"] = (query.get("columns") or [""])[0]
            return 200, {"status": "ok", "ports": []}

        librenms_server.register("/api/v0/devices/7/ports", respond, method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        success, _data = mock_librenms_api.get_ports(7)

        assert success is True
        assert "ifVrf" in seen["columns"].split(",")

    def test_get_ports_all(self, local_librenms_api, librenms_server):
        """Verify retrieving all ports for a device."""
        path = "/api/v0/devices/123/ports"
        librenms_server.register(
            path,
            {"status": "ok", "ports": [{"port_id": 1}, {"port_id": 2}]},
            method="GET",
        )

        success, data = local_librenms_api.get_ports(device_id=123)

        assert success is True
        assert "ports" in data
        assert len(data["ports"]) == 2
        assert librenms_server.requests[-1]["method"] == "GET"
        assert librenms_server.requests[-1]["path"] == path
        assert librenms_server.requests[-1]["query"]["with"] == ["vlans"]

    def test_get_port_by_id_success(self, local_librenms_api, librenms_server):
        """Verify retrieving port by ID."""
        path = "/api/v0/ports/1"
        librenms_server.register(
            path,
            {"status": "ok", "port": [{"port_id": 1, "ifName": "GigabitEthernet0/1"}]},
            method="GET",
        )

        success, port_data = local_librenms_api.get_port_by_id(port_id=1)

        assert success is True
        assert port_data is not None
        assert librenms_server.requests[-1]["method"] == "GET"

    def test_get_port_by_id_error(self, local_librenms_api, librenms_server):
        """Verify handling of port retrieval error."""
        librenms_server.register_disconnect("/api/v0/ports/999", method="GET")

        success, error_msg = local_librenms_api.get_port_by_id(port_id=999)

        assert success is False
        assert isinstance(error_msg, str)

    def test_get_device_inventory_success(self, local_librenms_api, librenms_server):
        """Verify retrieving device inventory."""
        path = "/api/v0/inventory/123/all"
        librenms_server.register(
            path,
            {"status": "ok", "inventory": [{"entPhysicalClass": "chassis"}]},
            method="GET",
        )

        success, inventory = local_librenms_api.get_device_inventory(device_id=123)

        assert success is True
        assert len(inventory) == 1
        assert librenms_server.requests[-1]["method"] == "GET"

    def test_get_inventory_filtered_by_class(self, local_librenms_api, librenms_server):
        """Verify filtering inventory by physical class."""
        path = "/api/v0/inventory/123"
        librenms_server.register(
            path,
            {"status": "ok", "inventory": [{"entPhysicalClass": "chassis", "entPhysicalName": "Chassis"}]},
            method="GET",
        )

        success, inventory = local_librenms_api.get_inventory_filtered(
            device_id=123,
            ent_physical_class="chassis",
        )

        assert success is True
        assert len(inventory) == 1
        assert librenms_server.requests[-1]["query"] == {"entPhysicalClass": ["chassis"]}

    def test_get_inventory_filtered_by_container(self, local_librenms_api, librenms_server):
        """Verify filtering inventory by container."""
        path = "/api/v0/inventory/123"
        librenms_server.register(
            path,
            {"status": "ok", "inventory": [{"entPhysicalContainedIn": "0"}]},
            method="GET",
        )

        success, inventory = local_librenms_api.get_inventory_filtered(
            device_id=123,
            ent_physical_contained_in=0,
        )

        assert success is True
        assert inventory == [{"entPhysicalContainedIn": "0"}]
        assert librenms_server.requests[-1]["query"] == {"entPhysicalContainedIn": ["0"]}

    def test_get_device_links_success(self, local_librenms_api, librenms_server):
        """Verify retrieving device links."""
        path = "/api/v0/devices/123/links"
        librenms_server.register(
            path,
            {"status": "ok", "links": [{"id": 1, "local_port_id": 10, "remote_port_id": 20}]},
            method="GET",
        )

        success, links_dict = local_librenms_api.get_device_links(device_id=123)

        assert success is True
        assert "links" in links_dict
        assert len(links_dict["links"]) == 1
        assert librenms_server.requests[-1]["method"] == "GET"

    def test_get_device_links_no_links_404_is_empty_not_failure(self, local_librenms_api, librenms_server):
        """Verify a no-links 404 returns an empty success so serial cable sync retains the cache snapshot."""
        path = "/api/v0/devices/13/links"
        librenms_server.register(
            path,
            {"status": "error", "message": "Device does not have any links"},
            status=404,
            method="GET",
        )

        success, data = local_librenms_api.get_device_links(device_id=13)

        assert success is True
        assert data == {"status": "ok", "links": []}

    def test_get_device_links_missing_device_404_is_a_failure(self, local_librenms_api, librenms_server):
        """A 404 for an unknown device must not be converted to an empty link list."""
        path = "/api/v0/devices/13/links"
        librenms_server.register(
            path,
            {"status": "error", "message": "Device not found"},
            status=404,
            method="GET",
        )

        success, data = local_librenms_api.get_device_links(device_id=13)

        assert success is False
        assert data == "Device not found"

    def test_get_device_links_librenms_no_links_message_is_empty_success(
        self,
        local_librenms_api,
        librenms_server,
    ):
        """LibreNMS also reports an empty link set as ``Links do not exist``."""
        path = "/api/v0/devices/13/links"
        librenms_server.register(
            path,
            {"status": "error", "message": "Links do not exist"},
            status=404,
            method="GET",
        )

        success, data = local_librenms_api.get_device_links(device_id=13)

        assert success is True
        assert data == {"status": "ok", "links": []}

    def test_get_device_links_non_404_http_error_still_fails(self, local_librenms_api, librenms_server):
        """A genuine server error (500) must still surface as a failure — only a 404 means 'no links'."""
        path = "/api/v0/devices/13/links"
        librenms_server.register(
            path,
            {"status": "error", "message": "boom"},
            status=500,
            method="GET",
        )

        success, data = local_librenms_api.get_device_links(device_id=13)

        assert success is False

    def test_get_device_ips_success(self, local_librenms_api, librenms_server):
        """Verify retrieving device IP addresses."""
        path = "/api/v0/devices/123/ip"
        librenms_server.register(
            path,
            {"status": "ok", "addresses": [{"ipv4_address": "192.0.2.1"}]},
            method="GET",
        )

        success, ips = local_librenms_api.get_device_ips(device_id=123)

        assert success is True
        assert len(ips) == 1

    def test_get_device_ips_empty(self, local_librenms_api, librenms_server):
        """Verify handling device with no IPs."""
        path = "/api/v0/devices/123/ip"
        librenms_server.register(path, {"status": "ok", "addresses": []}, method="GET")

        success, ips = local_librenms_api.get_device_ips(device_id=123)

        assert success is True
        assert len(ips) == 0

    def test_get_device_ips_404_is_empty_not_failure(self, local_librenms_api, librenms_server):
        """LibreNMS 404s /devices/{id}/ip for a device with no IPs — a successful empty result, not a fetch failure."""
        path = "/api/v0/devices/123/ip"
        librenms_server.register(
            path,
            {"status": "error", "message": "The device does not have any IP addresses"},
            status=404,
            method="GET",
        )

        success, ips = local_librenms_api.get_device_ips(device_id=123)

        # A device with no IPs must surface as an empty success so the IP tab shows an empty
        # table, not a red "Failed to fetch IP addresses" error (mirrors get_device_links).
        assert success is True
        assert ips == []

    def test_get_device_ips_404_empty_message_is_case_insensitive(self, local_librenms_api, librenms_server):
        """LibreNMS capitalization changes do not turn its stable no-address response into a failure."""
        path = "/api/v0/devices/123/ip"
        librenms_server.register(
            path,
            {"status": "error", "message": "Device 123 Does Not Have Any IP Addresses"},
            status=404,
            method="GET",
        )

        success, ips = local_librenms_api.get_device_ips(device_id=123)

        assert success is True
        assert ips == []

    def test_get_device_ips_404_for_missing_device_remains_a_failure(self, local_librenms_api, librenms_server):
        """Only LibreNMS's explicit empty-IP response is an empty success; a stale device id stays visible."""
        path = "/api/v0/devices/123/ip"
        librenms_server.register(
            path,
            {"status": "error", "message": "Device 123 does not exist"},
            status=404,
            method="GET",
        )

        success, message = local_librenms_api.get_device_ips(device_id=123)

        assert success is False
        assert message == "Device 123 does not exist"


# =============================================================================
# Poller groups and devices
# =============================================================================


class TestLibreNMSAPIPollerAndDevices:
    """Test poller groups and device listing operations."""

    POLLER_PATH = "/api/v0/poller_group"
    DEVICES_PATH = "/api/v0/devices"

    def test_get_poller_groups_success(self, local_librenms_api, librenms_server):
        """Verify retrieving poller groups."""
        librenms_server.register(
            self.POLLER_PATH,
            {"status": "ok", "get_poller_group": [{"id": 1, "group_name": "primary"}]},
            method="GET",
        )

        success, groups = local_librenms_api.get_poller_groups()

        assert success is True
        assert len(groups) == 1
        assert librenms_server.requests[-1]["method"] == "GET"

    def test_get_poller_groups_empty(self, local_librenms_api, librenms_server):
        """Verify handling empty poller groups."""
        librenms_server.register(
            self.POLLER_PATH,
            {"status": "ok", "get_poller_group": []},
            method="GET",
        )

        success, groups = local_librenms_api.get_poller_groups()

        assert success is True
        assert len(groups) == 0

    def test_list_devices_all(self, local_librenms_api, librenms_server):
        """Verify listing all devices."""
        librenms_server.register(
            self.DEVICES_PATH,
            {"status": "ok", "devices": [{"device_id": 1}, {"device_id": 2}, {"device_id": 3}]},
            method="GET",
        )

        success, devices = local_librenms_api.list_devices()

        assert success is True
        assert len(devices) == 3
        assert librenms_server.requests[-1]["method"] == "GET"

    def test_list_devices_empty(self, local_librenms_api, librenms_server):
        """Verify handling empty device list."""
        librenms_server.register(
            self.DEVICES_PATH,
            {"status": "ok", "devices": []},
            method="GET",
        )

        success, devices = local_librenms_api.list_devices()

        assert success is True
        assert len(devices) == 0


# =============================================================================
# Error handling
# =============================================================================


class TestLibreNMSAPIErrorHandling:
    """Test error handling and edge cases."""

    DEVICE_PATH = "/api/v0/devices/123"

    def test_network_error_handling(self, local_librenms_api, librenms_server):
        """Verify handling of network errors."""
        from netbox_librenms_plugin.librenms_api import LibreNMSLookupError

        librenms_server.register_disconnect(self.DEVICE_PATH, method="GET")

        success, result = local_librenms_api.get_device_info(device_id=123, use_cache=False)

        assert success is False
        assert isinstance(result, LibreNMSLookupError)
        assert result.status_code is None

    def test_timeout_error_handling(self, local_librenms_api, librenms_server, monkeypatch):
        """Verify handling of timeout errors."""
        from netbox_librenms_plugin.librenms_api import LibreNMSLookupError

        def delayed_response(**request):
            time.sleep(0.05)
            return 200, {"status": "ok", "devices": [{"device_id": 123}]}

        librenms_server.register(self.DEVICE_PATH, delayed_response, method="GET")
        monkeypatch.setattr("netbox_librenms_plugin.librenms_api.DEFAULT_API_TIMEOUT", 0.01)

        success, result = local_librenms_api.get_device_info(device_id=123, use_cache=False)

        assert success is False
        assert isinstance(result, LibreNMSLookupError)
        assert result.status_code is None

    def test_invalid_json_response(self, local_librenms_api, librenms_server):
        """Verify handling of invalid JSON responses — ValueError is now caught gracefully."""
        librenms_server.register_raw(self.DEVICE_PATH, "not-json", method="GET")

        # ValueError is now caught, returning (False, None) instead of propagating
        success, result = local_librenms_api.get_device_info(device_id=123, use_cache=False)
        assert success is False
        assert result is None

    def test_http_500_error_handling(self, local_librenms_api, librenms_server):
        """Verify that direct status handling classifies a 500 as server failure, not a missing device or malformed payload."""
        from netbox_librenms_plugin.librenms_api import LibreNMSLookupError

        librenms_server.register(
            self.DEVICE_PATH,
            {"status": "error", "message": "Internal server error"},
            status=500,
            method="GET",
        )

        success, result = local_librenms_api.get_device_info(device_id=123, use_cache=False)

        assert success is False
        assert isinstance(result, LibreNMSLookupError)
        assert result.status_code == 500

    def test_malformed_api_response(self, local_librenms_api, librenms_server):
        """Verify handling of malformed API responses."""
        librenms_server.register("/api/v0/devices", {}, method="POST")

        result = local_librenms_api.add_device(
            data={
                "hostname": "test.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        # Should handle missing fields gracefully
        assert result[0] is False

    def test_ssl_verification_error(self, local_librenms_api, librenms_server):
        """Verify handling of SSL verification errors."""
        local_librenms_api.librenms_url = librenms_server.url.replace("http://", "https://", 1)

        result = local_librenms_api.test_connection()

        assert result.get("error") is True
        assert "SSL certificate verification failed" in result["message"]


# ====================================================================================
# Guard tests: int conversion guard and VLAN dict guard
# ====================================================================================


class TestGetLibreNMSIdIntGuard:
    """Tests for the int conversion guard in get_librenms_id."""

    def test_non_integer_string_from_ip_returns_none(self, local_librenms_api, librenms_server):
        """When get_device_id_by_ip returns a non-int string, _store_librenms_id must not be called."""
        from django.core.cache import cache

        obj = TestLibreNMSAPIDeviceLookup._device(ip_address="192.0.2.1")
        ip_path = "/api/v0/devices/192.0.2.1"
        hostname_path = "/api/v0/devices/test-device"
        librenms_server.register(
            ip_path,
            {"status": "ok", "devices": [{"device_id": "not-an-int"}]},
            method="GET",
        )
        librenms_server.register(hostname_path, {"status": "ok", "devices": []}, method="GET")

        result = local_librenms_api.get_librenms_id(obj)

        assert result is None
        assert cache.get(local_librenms_api._get_cache_key(obj)) is None
        assert [request["path"] for request in librenms_server.requests] == [ip_path, hostname_path]

    def test_valid_integer_string_stores_and_returns(self, local_librenms_api, librenms_server):
        """When get_device_id_by_ip returns a valid int string, it should be stored and returned."""
        from django.core.cache import cache

        obj = TestLibreNMSAPIDeviceLookup._device(ip_address="192.0.2.1")
        path = "/api/v0/devices/192.0.2.1"
        librenms_server.register(
            path,
            {"status": "ok", "devices": [{"device_id": "42"}]},
            method="GET",
        )

        result = local_librenms_api.get_librenms_id(obj)

        assert result == 42
        assert cache.get(local_librenms_api._get_cache_key(obj)) == 42
        assert [request["path"] for request in librenms_server.requests] == [path]


class TestVlanEntryDictGuard:
    """Tests for the isinstance(vlan_entry, dict) guard in _parse_port_vlan_info."""

    def test_non_dict_entry_is_skipped(self, configure_librenms):
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

    def test_mixed_bad_entries_no_exception(self, configure_librenms):
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


@pytest.fixture
def local_librenms_api(mock_librenms_api, librenms_server):
    """Return the real API client pointed at the loopback LibreNMS server."""
    mock_librenms_api.librenms_url = librenms_server.url
    return mock_librenms_api


class TestGetDeviceInfoResponseShape:
    """Cover response-shape branches in get_device_info()."""

    def test_non_dict_device_entry_returns_failure(self, local_librenms_api, librenms_server):
        """A non-dict entry in the devices list must not propagate as truthy data."""
        librenms_server.register(
            "/api/v0/devices/1",
            {"status": "ok", "devices": ["not-a-dict"]},
            method="GET",
        )

        success, data = local_librenms_api.get_device_info(device_id=1)

        assert success is False
        assert data is None

    def test_missing_devices_key_returns_failure(self, local_librenms_api, librenms_server):
        """KeyError on missing 'devices' must be caught and return (False, None)."""
        librenms_server.register("/api/v0/devices/1", {"status": "ok"}, method="GET")

        success, data = local_librenms_api.get_device_info(device_id=1)

        assert success is False
        assert data is None


class TestGetDeviceTransceiversResponseShape:
    """Cover response-shape branches in get_device_transceivers()."""

    PATH = "/api/v0/devices/123/transceivers"

    def test_success_returns_transceiver_list(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {
                "status": "ok",
                "transceivers": [
                    {"port_id": 1, "type": "QSFP28", "serial": "SN1"},
                    {"port_id": 2, "type": "SFP+", "serial": "SN2"},
                ],
            },
            method="GET",
        )

        success, data = local_librenms_api.get_device_transceivers(device_id=123)

        assert success is True
        assert len(data) == 2
        assert data[0]["serial"] == "SN1"

    def test_invalid_json_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register_raw(self.PATH, "not-json", method="GET")

        success, msg = local_librenms_api.get_device_transceivers(device_id=123)

        assert success is False
        assert "Invalid JSON" in msg
        assert "Error connecting" not in msg

    def test_non_dict_response_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register(self.PATH, ["unexpected", "list"], method="GET")

        success, msg = local_librenms_api.get_device_transceivers(device_id=123)

        assert success is False
        assert "Unexpected" in msg

    def test_missing_transceivers_key_uses_server_message(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {"status": "ok", "message": "no transceivers MIB"},
            method="GET",
        )

        success, msg = local_librenms_api.get_device_transceivers(device_id=123)

        assert success is False
        assert msg == "no transceivers MIB"

    def test_status_not_ok_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {"status": "error", "transceivers": [], "message": "device offline"},
            method="GET",
        )

        success, msg = local_librenms_api.get_device_transceivers(device_id=123)

        assert success is False
        assert msg == "device offline"

    def test_transceivers_not_list_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {"status": "ok", "transceivers": {"port_id": 1}},
            method="GET",
        )

        success, msg = local_librenms_api.get_device_transceivers(device_id=123)

        assert success is False
        assert "Unexpected" in msg

    def test_malformed_transceiver_entry_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {"status": "ok", "transceivers": [{"port_id": 1}, None]},
            method="GET",
        )

        success, msg = local_librenms_api.get_device_transceivers(device_id=123)

        assert success is False
        assert "Malformed" in msg

    def test_request_exception_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register_disconnect(self.PATH, method="GET")

        success, msg = local_librenms_api.get_device_transceivers(device_id=123)

        assert success is False
        assert msg


class TestGetDeviceVrfs:
    """Cover get_device_vrfs(): one device's rows out of the instance-wide VRF table."""

    @staticmethod
    def _recorder(rows):
        """Register a /routing/vrf route that reports the query it was called with."""
        seen = {}

        def respond(method, path, query, headers, body):
            seen["hostname"] = (query.get("hostname") or [None])[0]
            return 200, {"status": "ok", "vrfs": rows, "count": len(rows)}

        return seen, respond

    def test_asks_the_server_to_filter_by_device(self, mock_librenms_api, librenms_server):
        """list_vrf documents a hostname filter, so the whole instance is never fetched."""
        seen, respond = self._recorder([{"vrf_id": 5, "vrf_name": "Base", "device_id": 5}])
        librenms_server.register("/api/v0/routing/vrf", respond, method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        success, rows = mock_librenms_api.get_device_vrfs(5)

        assert success is True
        assert seen["hostname"] == "5"
        assert [row["vrf_id"] for row in rows] == [5]

    def test_drops_rows_a_server_that_ignores_the_filter_returns(self, mock_librenms_api, librenms_server):
        """A LibreNMS that serves the whole table anyway must not leak other devices into a recording."""
        _seen, respond = self._recorder(
            [
                {"vrf_id": 5, "vrf_name": "Base", "device_id": 5},
                {"vrf_id": 9, "vrf_name": "customer-vprn", "device_id": 8},
            ]
        )
        librenms_server.register("/api/v0/routing/vrf", respond, method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        success, rows = mock_librenms_api.get_device_vrfs(5)

        assert success is True
        assert [row["vrf_id"] for row in rows] == [5]

    def test_string_device_ids_still_match(self, mock_librenms_api, librenms_server):
        """The documented payload types the ids as strings; a live 24.x server answers ints."""
        _seen, respond = self._recorder([{"vrf_id": "2", "vrf_name": "Mgmt-vrf", "device_id": "5"}])
        librenms_server.register("/api/v0/routing/vrf", respond, method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        success, rows = mock_librenms_api.get_device_vrfs(5)

        assert success is True
        assert [row["vrf_name"] for row in rows] == ["Mgmt-vrf"]

    def test_no_vrfs_on_the_instance_is_empty_not_an_error(self, mock_librenms_api, librenms_server):
        """LibreNMS answers 404 "Vrfs do not exist" for an instance with no VRF table."""
        librenms_server.register(
            "/api/v0/routing/vrf",
            {"status": "error", "message": "Vrfs do not exist"},
            status=404,
            method="GET",
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, rows = mock_librenms_api.get_device_vrfs(5)

        assert success is True
        assert rows == []

    def test_other_404s_stay_failures(self, mock_librenms_api, librenms_server):
        """A stale device id must not read as "this device has no VRFs"."""
        librenms_server.register(
            "/api/v0/routing/vrf",
            {"status": "error", "message": "Device not found"},
            status=404,
            method="GET",
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, message = mock_librenms_api.get_device_vrfs(5)

        assert success is False
        assert "Device not found" in message

    @pytest.mark.parametrize(
        "payload",
        [
            {"status": "error", "message": "boom"},
            {"status": "ok"},
            {"status": "ok", "vrfs": "not-a-list"},
            {"status": "ok", "vrfs": ["not-a-row"]},
            ["not", "an", "object"],
        ],
    )
    def test_malformed_payloads_fail_closed(self, mock_librenms_api, librenms_server, payload):
        """An empty VRF list and a broken response must never look the same."""
        librenms_server.register("/api/v0/routing/vrf", payload, method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        success, _message = mock_librenms_api.get_device_vrfs(5)

        assert success is False

    def test_transport_failure_returns_the_error(self, mock_librenms_api, librenms_server):
        """A LibreNMS that is down is a failure, never an empty VRF list."""
        mock_librenms_api.librenms_url = librenms_server.url
        librenms_server.stop()

        success, message = mock_librenms_api.get_device_vrfs(5)

        assert success is False
        assert message


class TestGetDeviceVlansResponseShape:
    """Cover response-shape branches in get_device_vlans()."""

    PATH = "/api/v0/resources/vlans"

    def test_success_filters_by_device_id(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {
                "status": "ok",
                "vlans": [
                    {"vlan_id": 1, "device_id": 7, "vlan_vlan": 10, "vlan_name": "DATA"},
                    {"vlan_id": 2, "device_id": 8, "vlan_vlan": 20, "vlan_name": "VOICE"},
                ],
            },
            method="GET",
        )

        success, vlans = local_librenms_api.get_device_vlans(device_id=7)

        assert success is True
        assert len(vlans) == 1
        assert vlans[0]["vlan_vlan"] == 10

    def test_vlans_not_list_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {"status": "ok", "vlans": "oops", "message": "bad payload"},
            method="GET",
        )

        success, msg = local_librenms_api.get_device_vlans(device_id=7)

        assert success is False
        assert msg == "bad payload"

    def test_non_dict_item_in_vlans_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {"status": "ok", "vlans": [{"vlan_id": 1, "device_id": 7}, "not-a-dict"]},
            method="GET",
        )

        success, msg = local_librenms_api.get_device_vlans(device_id=7)

        assert success is False
        assert "invalid item shape" in msg

    def test_status_not_ok_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {"status": "error", "message": "nope"},
            method="GET",
        )

        success, msg = local_librenms_api.get_device_vlans(device_id=7)

        assert success is False
        assert msg == "nope"

    def test_non_dict_response_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register(self.PATH, ["unexpected"], method="GET")

        success, msg = local_librenms_api.get_device_vlans(device_id=7)

        assert success is False
        assert msg == "Unexpected response format"

    def test_http_404_returns_dedicated_message(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {"status": "error", "message": "not found"},
            status=404,
            method="GET",
        )

        success, msg = local_librenms_api.get_device_vlans(device_id=7)

        assert success is False
        assert msg == "VLANs resource not found"

    def test_value_error_returns_connection_message(self, local_librenms_api, librenms_server):
        librenms_server.register_raw(self.PATH, "not-json", method="GET")

        success, msg = local_librenms_api.get_device_vlans(device_id=7)

        assert success is False
        assert "Error connecting to LibreNMS" in msg


class TestGetPortVlanDetailsResponseShape:
    """Cover response-shape branches in get_port_vlan_details()."""

    PATH = "/api/v0/ports/11"

    def test_success_returns_port_dict(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {
                "status": "ok",
                "port": [{"port_id": 11, "ifName": "Te1/1/1", "vlans": [{"vlan": 10, "untagged": 1}]}],
            },
            method="GET",
        )

        success, port = local_librenms_api.get_port_vlan_details(port_id=11)

        assert success is True
        assert port["port_id"] == 11

    def test_non_dict_response_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register(self.PATH, ["unexpected"], method="GET")

        success, msg = local_librenms_api.get_port_vlan_details(port_id=11)

        assert success is False
        assert msg == "Unexpected response format"

    def test_status_not_ok_uses_server_message(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {"status": "error", "message": "no port"},
            method="GET",
        )

        success, msg = local_librenms_api.get_port_vlan_details(port_id=11)

        assert success is False
        assert msg == "no port"

    def test_missing_port_list_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {"status": "ok", "port": {"port_id": 11}},
            method="GET",
        )

        success, msg = local_librenms_api.get_port_vlan_details(port_id=11)

        assert success is False
        assert "missing 'port' list" in msg

    def test_empty_port_list_returns_not_found(self, local_librenms_api, librenms_server):
        librenms_server.register(self.PATH, {"status": "ok", "port": []}, method="GET")

        success, msg = local_librenms_api.get_port_vlan_details(port_id=11)

        assert success is False
        assert msg == "Port not found"

    def test_non_dict_port_entry_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register(
            self.PATH,
            {"status": "ok", "port": ["bad-entry"]},
            method="GET",
        )

        success, msg = local_librenms_api.get_port_vlan_details(port_id=11)

        assert success is False
        assert "invalid 'port' entry" in msg

    def test_request_exception_returns_failure(self, local_librenms_api, librenms_server):
        librenms_server.register_disconnect(self.PATH, method="GET")

        success, msg = local_librenms_api.get_port_vlan_details(port_id=11)

        assert success is False
        assert "Error connecting to LibreNMS" in msg


# =============================================================================
# Test Class: get_port_stack()
# =============================================================================


def test_invalid_utf8_json_body_reaches_the_registered_route(librenms_server):
    """Malformed UTF-8 must be replacement-decoded instead of closing the connection."""
    received = []

    def respond(**request):
        received.append(request["body"])
        return 200, {"status": "ok"}

    librenms_server.register("/invalid-json", respond, method="POST")

    response = requests.post(
        f"{librenms_server.url}/invalid-json",
        data=b"\xff",
        headers={"Content-Type": "application/json"},
        timeout=2,
    )

    assert response.status_code == 200
    assert received == ["\ufffd"]


class TestGetPortStack:
    """Tests for LibreNMSAPI.get_port_stack()."""

    def test_returns_mappings_list_on_success(self, mock_librenms_api, librenms_server):
        """get_port_stack returns (True, list) on HTTP 200."""
        path = "/api/v0/devices/42/port_stack"
        requests_seen = []
        body = {
            "status": "ok",
            "mappings": [
                {"high_port_id": 1, "low_port_id": 2, "high_ifIndex": 1, "low_ifIndex": 2},
            ],
        }

        def response(**request):
            requests_seen.append(request)
            return 200, body

        librenms_server.register(path, response, method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_port_stack(42)

        assert success is True
        assert data == [{"high_port_id": 1, "low_port_id": 2, "high_ifIndex": 1, "low_ifIndex": 2}]
        assert [(request["method"], request["path"]) for request in requests_seen] == [("GET", path)]

    def test_returns_false_on_404(self, mock_librenms_api, librenms_server):
        """get_port_stack returns (False, error_str) when device not found."""
        librenms_server.register(
            "/api/v0/devices/99/port_stack",
            {"status": "error", "message": "Device does not exist"},
            status=404,
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_port_stack(99)

        assert success is False
        assert "not found" in data.lower()

    @pytest.mark.parametrize("payload", [{"status": "ok"}, {"status": "ok", "mappings": None}])
    def test_missing_or_null_mappings_fails_closed(self, mock_librenms_api, librenms_server, payload):
        """A successful response must contain the documented list-valued mappings field."""
        librenms_server.register("/api/v0/devices/5/port_stack", payload)
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert "mappings" in data

    def test_error_status_without_mappings_fails_not_empty(self, mock_librenms_api, librenms_server):
        """An error payload that omits 'mappings' must fail, not normalise to (True, []), so a real API failure isn't masked as 'no LAG/parent relationships'."""
        librenms_server.register(
            "/api/v0/devices/5/port_stack",
            {"status": "error", "message": "device polling disabled"},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert data == "device polling disabled"

    def test_error_status_with_mappings_present_still_fails(self, mock_librenms_api, librenms_server):
        """An error payload that still carries mappings must honor the explicit error status before consuming them, so it fails rather than silently skipping valid sync."""
        librenms_server.register(
            "/api/v0/devices/5/port_stack",
            {"status": "error", "message": "stale poll", "mappings": []},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert data == "stale poll"

    def test_non_string_status_fails_not_empty(self, mock_librenms_api, librenms_server):
        """A non-string status like `false` is malformed and must fail, not be accepted as (True, [])."""
        # {"status": false, "mappings": []} — only an absent status or "ok" is a genuine answer;
        # accepting this would silently suppress LAG/sub-interface relationship updates.
        librenms_server.register(
            "/api/v0/devices/5/port_stack",
            {"status": False, "mappings": []},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert "LibreNMS reported an error fetching port stack" in data

    def test_json_decode_error_reports_invalid_json_not_connection_error(self, mock_librenms_api, librenms_server):
        """requests.exceptions.JSONDecodeError subclasses BOTH ValueError and RequestException, so the ValueError handler must precede the RequestException handler — otherwise a JSON decode failure is swallowed by the broad handler and mislabeled 'Error connecting to LibreNMS'."""
        librenms_server.register_raw("/api/v0/devices/5/port_stack", "not-json", method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert "Invalid JSON" in data
        assert "Error connecting" not in data

    def test_malformed_mappings_fails_not_empty(self, mock_librenms_api, librenms_server):
        """A non-list (or list-of-non-dicts) `mappings` is malformed, not 'no relationships'."""
        path = "/api/v0/devices/5/port_stack"
        mock_librenms_api.librenms_url = librenms_server.url
        for bad in ({"mappings": {"oops": 1}}, {"mappings": ["not-a-dict", 2]}):
            librenms_server.register(path, bad)
            success, data = mock_librenms_api.get_port_stack(5)
            assert success is False, f"{bad!r} should fail"
            assert "mappings" in data

    @pytest.mark.parametrize(
        "mapping",
        [
            {"high_port_id": 1},
            {"low_port_id": 2},
        ],
    )
    def test_mapping_without_both_port_ids_fails_closed(self, mock_librenms_api, librenms_server, mapping):
        """Each mapping must identify both relationship endpoints."""
        librenms_server.register(
            "/api/v0/devices/5/port_stack",
            {"status": "ok", "mappings": [mapping]},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_port_stack(5)

        assert success is False
        assert "mappings" in data

    def test_non_object_payload_fails_not_empty(self, mock_librenms_api, librenms_server):
        """A non-object top-level payload (list/string/null) is malformed, not 'no relationships'."""
        path = "/api/v0/devices/5/port_stack"
        mock_librenms_api.librenms_url = librenms_server.url
        for bad in ([{"high_port_id": 1, "low_port_id": 2}], "oops", None):
            librenms_server.register(path, bad)
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
        """Verify that a live port_stack row with high_port_id and low_port_id resolves correctly."""
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
        assert result == {"lag_members": {}, "sub_interfaces": {}, "bridge_members": {}}
        assert any("Unrecognized port_stack entry shape" in r.getMessage() for r in caplog.records)

    def test_nokia_lag_membership(self, mock_librenms_api):
        """Nokia: high=physical, low=lag-1 (ieee8023adLag) -> member in lag_members."""
        result = mock_librenms_api.resolve_port_relationships(NOKIA_PORTS, NOKIA_PORT_STACK[:1], lag_patterns={})
        assert result["lag_members"] == {101: 102}
        assert result["sub_interfaces"] == {}

    def test_nameless_lag_aggregate_resolved_by_iftype(self, mock_librenms_api):
        """Verify that ifType identifies a nameless LAG aggregate and preserves its membership."""
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

    def test_sap_rows_stay_excluded_from_the_name_derived_sub_interface_fallback(self, mock_librenms_api):
        """
        Rule 2 skips a SAP pair, but the name-derived fallback walks every port with an id,
        not the filtered pairs, so a SAP child can still be recorded as a sub-interface."""
        ports = [
            {"port_id": 301, "ifName": "lag-1:10", "ifDescr": "lag-1:10", "ifType": "ipForward"},
            {"port_id": 302, "ifName": "lag-1:10.100", "ifDescr": "lag-1:10.100", "ifType": "ipForward"},
        ]
        # No port_stack pair: the fallback is the only path that can relate these two.
        result = mock_librenms_api.resolve_port_relationships(
            ports, [], lag_patterns={}, compiled_sap_patterns=[re.compile(":")]
        )
        assert 302 not in result["sub_interfaces"], "a SAP row must not gain a name-derived parent"
        assert result["sub_interfaces"] == {}

    def test_the_name_derived_fallback_still_relates_non_sap_sub_interfaces(self, mock_librenms_api):
        """The SAP guard must not disable the fallback for ordinary rows."""
        ports = [
            {"port_id": 401, "ifName": "ge-0/0/1", "ifDescr": "ge-0/0/1", "ifType": "ethernetCsmacd"},
            {"port_id": 402, "ifName": "ge-0/0/1.100", "ifDescr": "ge-0/0/1.100", "ifType": "l2vlan"},
        ]
        result = mock_librenms_api.resolve_port_relationships(
            ports, [], lag_patterns={}, compiled_sap_patterns=[re.compile(":")]
        )
        assert result["sub_interfaces"] == {402: 401}

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

        assert result == {"lag_members": {}, "sub_interfaces": {2: 1}, "bridge_members": {}}

    def test_fallback_uses_other_field_alone_when_configured_field_yields_nothing(self, mock_librenms_api):
        """The resolver uses only ifDescr when configured ifName yields no relationship."""
        ports = [
            {"port_id": 1, "ifName": "uplink", "ifDescr": "xe-0/0/0", "ifType": "ethernetCsmacd"},
            {"port_id": 2, "ifName": "customer", "ifDescr": "xe-0/0/0.1", "ifType": "propVirtual"},
        ]

        result = mock_librenms_api.resolve_port_relationships(
            ports, [], lag_patterns={}, interface_name_field="ifName", compiled_sap_patterns=[]
        )

        assert result == {"lag_members": {}, "sub_interfaces": {2: 1}, "bridge_members": {}}

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

        assert result == {"lag_members": {1: 2}, "sub_interfaces": {4: 3}, "bridge_members": {}}

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
            "bridge_members": {},
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

        assert result == {"lag_members": {1: 2}, "sub_interfaces": {4: 3}, "bridge_members": {}}

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

        assert result == {"lag_members": {}, "sub_interfaces": {13: 11}, "bridge_members": {}}

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
        assert result == {"lag_members": {}, "sub_interfaces": {}, "bridge_members": {}}

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
        """Verify that a non-string member ifName does not block LAG resolution through the aggregate ifType."""
        ports = [
            {"port_id": 501, "ifName": 12345, "ifType": "ethernetCsmacd"},  # malformed: non-string
            {"port_id": 502, "ifName": "lag9", "ifType": "ieee8023adLag"},
        ]
        stack = [{"high_port_id": 501, "low_port_id": 502}]
        result = mock_librenms_api.resolve_port_relationships(ports, stack, lag_patterns={})
        assert result == {"lag_members": {501: 502}, "sub_interfaces": {}, "bridge_members": {}}

    def test_none_ports_and_port_stack_return_empty_maps(self, mock_librenms_api):
        """A 0-port device can surface ports/port_stack as None (e.g. `ports_data["ports"]` is null on a 0-port iosxr device); resolution must treat that as 'nothing to resolve', not crash iterating None."""
        # ports is None (null "ports" body), port_stack a valid list — must not raise.
        result = mock_librenms_api.resolve_port_relationships(None, NOKIA_PORT_STACK[:1], lag_patterns={})
        assert result == {"lag_members": {}, "sub_interfaces": {}, "bridge_members": {}}
        # port_stack is None (null "mappings"), ports a valid list — must not raise.
        result = mock_librenms_api.resolve_port_relationships(NOKIA_PORTS, None, lag_patterns={})
        assert result == {"lag_members": {}, "sub_interfaces": {}, "bridge_members": {}}
        # Both None — the full 0-port shape.
        result = mock_librenms_api.resolve_port_relationships(None, None, lag_patterns={})
        assert result == {"lag_members": {}, "sub_interfaces": {}, "bridge_members": {}}

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


@pytest.mark.django_db  # _fetch_serial_port_sensors reads the SerialSensorTypePattern rows
class TestGetSerialPortSensors:
    """Cover response-shape branches in get_serial_port_sensors()."""

    def _make_sensor(self, device_id, sensor_type="acsSerialPortTable", port_num=7):
        return {
            "sensor_id": 1000 + port_num,
            "device_id": device_id,
            "sensor_type": sensor_type,
            "sensor_index": f"acsSerialPortTableStatus.{port_num}",
            "sensor_descr": f"device-{port_num} Status",
            "sensor_current": 2,
            "group": "Serial Ports",
        }

    def test_empty_recognition_table_skips_the_instance_sensor_request(self, mock_librenms_api, librenms_server):
        from netbox_librenms_plugin.models import SerialSensorTypePattern

        SerialSensorTypePattern.objects.all().delete()
        requests_seen = []

        def response(**request):
            requests_seen.append(request)
            return 200, {"status": "ok", "sensors": []}

        librenms_server.register("/api/v0/resources/sensors", response, method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert data == []
        assert requests_seen == []

    def test_success_filters_by_device_and_type(self, mock_librenms_api, librenms_server):
        sensors = [
            self._make_sensor(12, port_num=7),
            self._make_sensor(12, port_num=11),
            self._make_sensor(99, port_num=3),  # different device
            self._make_sensor(12, sensor_type="tempSensor", port_num=5),  # wrong type
        ]
        librenms_server.register("/api/v0/resources/sensors", {"status": "ok", "sensors": sensors})
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert len(data) == 2
        assert all(s["device_id"] == 12 for s in data)
        assert all(s["sensor_type"] == "acsSerialPortTable" for s in data)

    def test_explicit_sensor_types_map_bypasses_db(self, mock_librenms_api, librenms_server):
        """A caller-supplied recognition map filters without the live database rows."""
        from netbox_librenms_plugin.models import SerialSensorTypePattern

        SerialSensorTypePattern.objects.all().delete()  # a replay host may have no rows at all

        sensors = [self._make_sensor(12, port_num=7)]
        librenms_server.register("/api/v0/resources/sensors", {"status": "ok", "sensors": sensors})
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(
            device_id=12, sensor_types={"acsSerialPortTable": "ttyS{N}"}
        )
        assert success is True
        assert len(data) == 1

        # The DB-map path sees no rows, so it filters everything out.
        success2, data2 = mock_librenms_api.get_serial_port_sensors(device_id=12)
        assert success2 is True
        assert data2 == []

    def test_cisco_async_line_survives_type_filter(self, mock_librenms_api, librenms_server):
        """Cisco async-line sensors pass the serial type filter alongside Avocent, others are dropped."""
        cisco = {
            "sensor_id": 2002,
            "device_id": 12,
            "sensor_type": "OLD-CISCO-TS-MIB::ltsLineTable",
            "sensor_index": "tsLineActive.2",
            "sensor_descr": "peer Status",
            "sensor_current": 0,
            "group": "Serial Ports",
        }
        sensors = [
            self._make_sensor(12, port_num=7),  # acsSerialPortTable
            cisco,
            self._make_sensor(12, sensor_type="tempSensor", port_num=5),  # unrelated -> excluded
        ]
        librenms_server.register("/api/v0/resources/sensors", {"status": "ok", "sensors": sensors})
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert {s["sensor_type"] for s in data} == {
            "acsSerialPortTable",
            "OLD-CISCO-TS-MIB::ltsLineTable",
        }

    def test_non_dict_sensor_item_fails_closed(self, mock_librenms_api, librenms_server):
        """Verify a malformed non-dictionary sensor item fails closed instead of appearing as no serial sensors."""
        sensors = ["bad-string", None, self._make_sensor(12, port_num=7)]
        librenms_server.register("/api/v0/resources/sensors", {"status": "ok", "sensors": sensors})
        mock_librenms_api.librenms_url = librenms_server.url

        success, msg = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is False
        assert "invalid sensor item" in msg.lower()

    def test_non_string_sensor_type_is_skipped_without_dropping_the_serial_rows(
        self, mock_librenms_api, librenms_server
    ):
        """Verify an unhashable sensor type is skipped so one malformed row cannot stop the instance-wide refresh."""
        unreadable = self._make_sensor(12, port_num=8)
        unreadable["sensor_type"] = ["acsSerialPortTable"]  # unhashable → TypeError on `in serial_types`
        good = self._make_sensor(12, port_num=7)
        librenms_server.register(
            "/api/v0/resources/sensors",
            {"status": "ok", "sensors": [unreadable, good]},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert [sensor["sensor_id"] for sensor in data] == [good["sensor_id"]]

    def test_unrelated_non_string_sensor_type_does_not_fail_the_serial_fetch(self, mock_librenms_api, librenms_server):
        """One unrelated sensor on another device must not stop this device's serial refresh."""
        unrelated = self._make_sensor(99, port_num=5)
        unrelated["sensor_type"] = {"name": "tempSensor"}
        good = self._make_sensor(12, port_num=7)
        librenms_server.register(
            "/api/v0/resources/sensors",
            {"status": "ok", "sensors": [unrelated, good]},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert [sensor["sensor_id"] for sensor in data] == [good["sensor_id"]]

    def test_non_numeric_sensor_id_fails_closed(self, mock_librenms_api, librenms_server):
        bad = self._make_sensor(12, port_num=8)
        bad["sensor_id"] = "';alert(1);//"
        librenms_server.register("/api/v0/resources/sensors", {"status": "ok", "sensors": [bad]})
        mock_librenms_api.librenms_url = librenms_server.url

        success, msg = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is False
        assert "invalid sensor_id" in msg.lower()

    def test_unrelated_sensor_id_does_not_fail_the_serial_fetch(self, mock_librenms_api, librenms_server):
        """The endpoint returns every sensor on the instance, so only serial rows may fail it."""
        unrelated = self._make_sensor(99, sensor_type="tempSensor", port_num=5)
        unrelated["sensor_id"] = None
        good = self._make_sensor(12, port_num=7)
        librenms_server.register(
            "/api/v0/resources/sensors",
            {"status": "ok", "sensors": [unrelated, good]},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert [sensor["sensor_id"] for sensor in data] == [good["sensor_id"]]

    def test_a_malformed_serial_row_on_another_device_does_not_fail_the_requested_device(
        self, mock_librenms_api, librenms_server
    ):
        """A malformed serial row on another device must not stop the requested device refresh."""
        other = self._make_sensor(99, port_num=8)
        other["sensor_id"] = "';alert(1);//"
        good = self._make_sensor(12, port_num=7)
        librenms_server.register(
            "/api/v0/resources/sensors",
            {"status": "ok", "sensors": [other, good]},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert [sensor["sensor_id"] for sensor in data] == [good["sensor_id"]]

    def test_a_malformed_sensor_deleted_on_another_device_does_not_fail_the_requested_device(
        self, mock_librenms_api, librenms_server
    ):
        """A malformed sensor_deleted on another device must not stop the requested device refresh."""
        other = self._make_sensor(99, port_num=8)
        other["sensor_deleted"] = 2
        good = self._make_sensor(12, port_num=7)
        librenms_server.register(
            "/api/v0/resources/sensors",
            {"status": "ok", "sensors": [other, good]},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert [sensor["sensor_id"] for sensor in data] == [good["sensor_id"]]

    def test_unrelated_sensor_deleted_does_not_fail_the_serial_fetch(self, mock_librenms_api, librenms_server):
        """One unrelated sensor with an out-of-contract sensor_deleted must not drop serial rows."""
        unrelated = self._make_sensor(99, sensor_type="tempSensor", port_num=5)
        unrelated["sensor_deleted"] = 2
        good = self._make_sensor(12, port_num=7)
        librenms_server.register(
            "/api/v0/resources/sensors",
            {"status": "ok", "sensors": [unrelated, good]},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert [sensor["sensor_id"] for sensor in data] == [good["sensor_id"]]

    @pytest.mark.parametrize("deleted_value", [True, 1.0], ids=["boolean", "float"])
    def test_non_integer_sensor_deleted_on_a_serial_row_fails_closed(
        self,
        mock_librenms_api,
        librenms_server,
        deleted_value,
    ):
        """A serial row with an unreadable sensor_deleted is still a malformed response."""
        bad = self._make_sensor(12, port_num=8)
        bad["sensor_deleted"] = deleted_value
        librenms_server.register("/api/v0/resources/sensors", {"status": "ok", "sensors": [bad]})
        mock_librenms_api.librenms_url = librenms_server.url

        success, msg = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is False
        assert "invalid sensor_deleted" in msg.lower()

    def test_empty_sensor_list_returns_empty(self, mock_librenms_api, librenms_server):
        librenms_server.register("/api/v0/resources/sensors", {"status": "ok", "sensors": []})
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert data == []

    def test_librenms_empty_inventory_404_returns_empty(self, mock_librenms_api, librenms_server):
        """LibreNMS reports a valid empty sensor inventory as a specific 404 message."""
        librenms_server.register(
            "/api/v0/resources/sensors",
            {"status": "error", "message": "Sensors do not exist"},
            status=404,
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert data == []

    def test_deleted_serial_sensors_are_excluded(self, mock_librenms_api, librenms_server):
        """A discovery-deleted line must not become a live cable-sync row."""
        active = self._make_sensor(12, port_num=7)
        active["sensor_deleted"] = "0"
        deleted = self._make_sensor(12, port_num=8)
        deleted["sensor_deleted"] = 1
        librenms_server.register(
            "/api/v0/resources/sensors",
            {"status": "ok", "sensors": [active, deleted]},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert [sensor["sensor_id"] for sensor in data] == [active["sensor_id"]]

    def test_missing_sensors_key_returns_failure(self, mock_librenms_api, librenms_server):
        """status=ok but neither 'sensors' nor 'resources' present is a malformed response, not a successful zero-sensor result."""
        librenms_server.register(
            "/api/v0/resources/sensors",
            {"status": "ok", "message": "no sensors key"},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, msg = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is False
        assert "no sensors key" in msg

    def test_falsy_present_sensors_value_returns_failure(self, mock_librenms_api, librenms_server):
        """A present-but-non-list 'sensors' (e.g. "") must fail, not be coerced to an empty success."""
        librenms_server.register("/api/v0/resources/sensors", {"status": "ok", "sensors": ""})
        mock_librenms_api.librenms_url = librenms_server.url

        success, msg = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is False
        assert "missing sensor list" in msg.lower()

    def test_non_ok_status_returns_error(self, mock_librenms_api, librenms_server):
        librenms_server.register(
            "/api/v0/resources/sensors",
            {"status": "error", "message": "something went wrong"},
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, msg = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is False
        assert "wrong" in msg

    def test_404_returns_error(self, mock_librenms_api, librenms_server):
        librenms_server.register(
            "/api/v0/resources/sensors",
            {"status": "error", "message": "Resource does not exist"},
            status=404,
        )
        mock_librenms_api.librenms_url = librenms_server.url

        success, msg = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is False
        assert "not found" in msg.lower()

    def test_connection_error_returns_error(self, mock_librenms_api, librenms_server):
        librenms_server.register_disconnect("/api/v0/resources/sensors", method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        success, msg = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is False
        # This suite drives a real loopback refusal, so the message wording is the client's, not a
        # fixture's. Keep the classification pinned: a connection failure must not be reported
        # through the JSON branch (review finding on #104).
        assert "error" in msg.lower()
        assert "Invalid JSON" not in msg

    def test_recognized_type_change_applies_on_the_next_fetch(self, mock_librenms_api, librenms_server):
        """Adding a recognized sensor type applies on the next fresh fetch."""
        from netbox_librenms_plugin.models import SerialSensorTypePattern

        sensor_type = "reviewSerialTable"
        sensors = [self._make_sensor(12, sensor_type=sensor_type, port_num=7)]
        requests_seen = []

        def response(**request):
            requests_seen.append(request)
            return 200, {"status": "ok", "sensors": sensors}

        librenms_server.register("/api/v0/resources/sensors", response, method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        first_success, first_data = mock_librenms_api.get_serial_port_sensors(device_id=12)
        SerialSensorTypePattern.objects.create(sensor_type=sensor_type, port_name_pattern="console{N}")
        second_success, second_data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert first_success is True and first_data == []
        assert second_success is True and [sensor["sensor_type"] for sensor in second_data] == [sensor_type]
        assert len(requests_seen) == 2

    def test_failed_fetch_is_retried(self, mock_librenms_api, librenms_server):
        """A transient failure does not prevent the next request from fetching again."""
        good = [self._make_sensor(12, port_num=7)]
        responses = iter(
            [
                {"status": "error", "message": "boom"},
                {"status": "ok", "sensors": good},
            ]
        )
        requests_seen = []

        def response(**request):
            requests_seen.append(request)
            return 200, next(responses)

        librenms_server.register("/api/v0/resources/sensors", response, method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        ok1, msg1 = mock_librenms_api.get_serial_port_sensors(device_id=12)
        ok2, data2 = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert ok1 is False and "boom" in msg1
        assert len(requests_seen) == 2
        assert ok2 is True and [s["device_id"] for s in data2] == [12]

    def test_json_decode_error_reported_as_invalid_json(self, mock_librenms_api, librenms_server):
        """A non-JSON 200 body must surface 'Invalid JSON', not be mislabeled 'Error connecting' — requests JSONDecodeError subclasses both ValueError and RequestException, so the ValueError handler must precede the RequestException one (mirrors get_port_stack)."""
        librenms_server.register_raw("/api/v0/resources/sensors", "not-json", method="GET")
        mock_librenms_api.librenms_url = librenms_server.url

        success, msg = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is False
        assert "Invalid JSON" in msg
        assert "Error connecting" not in msg
