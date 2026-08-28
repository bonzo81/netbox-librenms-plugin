"""Coverage tests for librenms_api.py missing lines."""

from copy import deepcopy
from unittest.mock import patch

import pytest
import requests

from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


@pytest.fixture
def librenms_server(monkeypatch):
    """A real HTTP LibreNMS whose responses each test registers."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        yield server


def configure_servers(settings, servers):
    """Replace the plugin's configured servers, leaving the rest of PLUGINS_CONFIG intact."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_settings = plugin_config["netbox_librenms_plugin"]
    plugin_settings["servers"] = servers
    plugin_settings.pop("librenms_url", None)
    plugin_settings.pop("api_token", None)
    settings.PLUGINS_CONFIG = plugin_config


def configure_legacy(settings, url=None, token="legacy-token"):
    """Configure the pre-multi-server shape: no ``servers`` mapping, just a bare url/token."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_settings = plugin_config["netbox_librenms_plugin"]
    plugin_settings["servers"] = None
    if url is None:
        plugin_settings.pop("librenms_url", None)
        plugin_settings.pop("api_token", None)
    else:
        plugin_settings["librenms_url"] = url
        plugin_settings["api_token"] = token
    settings.PLUGINS_CONFIG = plugin_config


def api_for(settings, url, *, key="default", token="test-token"):
    """Point the plugin at *url* and return a real client bound to it."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    configure_servers(settings, {key: {"librenms_url": url, "api_token": token, "verify_ssl": False}})
    return LibreNMSAPI(server_key=key)


@pytest.mark.django_db
class TestLibreNMSAPIInitFallback:
    """__init__ resolves the server key from LibreNMSSettings when the caller gives none."""

    def test_init_reads_selected_server_from_settings(self, settings):
        """With no explicit key, the stored selected_server decides which server is bound."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.models import LibreNMSSettings

        configure_servers(
            settings,
            {
                "primary": {"librenms_url": "https://primary.example.com", "api_token": "tok"},
                "secondary": {"librenms_url": "https://secondary.example.com", "api_token": "tok2"},
            },
        )
        row, _ = LibreNMSSettings.objects.get_or_create()
        row.selected_server = "primary"
        row.save(update_fields=["selected_server"])

        api = LibreNMSAPI()

        assert api.server_key == "primary"
        assert api.librenms_url == "https://primary.example.com"
        assert api.api_token == "tok"

    def test_init_without_a_stored_selection_falls_back_to_default(self, settings):
        """No stored selection leaves the auto-default key, which the legacy config serves."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.models import LibreNMSSettings

        configure_legacy(settings, "https://x.example.com", token="tok")
        LibreNMSSettings.objects.all().delete()

        api = LibreNMSAPI()

        assert api.server_key == "default"
        assert api.librenms_url == "https://x.example.com"
        assert api.api_token == "tok"
        assert api.api_token == "tok"

    def test_init_survives_a_settings_lookup_that_raises(self, settings):
        """A broken settings model must not stop the client binding the default server."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        configure_legacy(settings, "https://x.example.com", token="tok")

        # Error injection at the boundary __init__ guards with (ImportError, AttributeError).
        with patch("netbox_librenms_plugin.models.LibreNMSSettings") as broken:
            broken.objects.first.side_effect = AttributeError("no attr")
            api = LibreNMSAPI()

        assert api.server_key == "default"
        assert api.librenms_url == "https://x.example.com"


@pytest.mark.django_db
class TestTestConnectionErrors:
    """test_connection turns each server answer into a user-facing outcome."""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, "authentication failed"),
            (403, "forbidden"),
            (404, "not found"),
            (500, "server error"),
            (503, "server error"),
            (302, "302"),
        ],
    )
    def test_http_status_is_reported(self, settings, librenms_server, status, expected):
        """A real response with this status reaches the caller as the matching message."""
        librenms_server.register("/api/v0/system", {"status": "error"}, status=status, method="GET")
        api = api_for(settings, librenms_server.url)

        result = api.test_connection()

        assert result["error"] is True
        assert expected in result["message"].lower()

    def test_successful_system_call_returns_the_system_row(self, settings, librenms_server):
        """A 200 with a system payload returns the first row, not an error dict."""
        librenms_server.register(
            "/api/v0/system",
            {"status": "ok", "system": [{"version": "24.9.0"}]},
            method="GET",
        )
        api = api_for(settings, librenms_server.url)

        assert api.test_connection() == {"version": "24.9.0"}

    def test_ok_status_without_a_system_payload_is_reported(self, settings, librenms_server):
        """A 200 that carries no system rows falls through to the unexpected-response branch."""
        librenms_server.register("/api/v0/system", {"status": "ok"}, method="GET")
        api = api_for(settings, librenms_server.url)

        result = api.test_connection()

        assert result["error"] is True
        assert "200" in result["message"]

    def test_connection_refused_is_reported(self, settings, librenms_server):
        """A dead server is a real connection failure, no injection needed."""
        url = librenms_server.url
        api = api_for(settings, url)
        librenms_server.stop()  # free the port so the next request is refused

        result = api.test_connection()

        assert result["error"] is True
        assert "Connection failed" in result["message"]

    @pytest.mark.parametrize(
        ("exception", "expected"),
        [
            (requests.exceptions.SSLError("cert failed"), "ssl"),
            (requests.exceptions.Timeout("timed out"), "timeout"),
            (ValueError("something weird"), "unexpected error"),
        ],
    )
    def test_transport_failures_are_reported(self, settings, librenms_server, exception, expected):
        """Injected at the requests boundary: a real TLS or timeout failure is not reproducible here."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.get", side_effect=exception):
            result = api.test_connection()

        assert result["error"] is True
        assert expected in result["message"].lower()


@pytest.mark.django_db
class TestGetAvailableServersLegacy:
    """get_available_servers on the pre-multi-server config shape."""

    def test_legacy_config_no_servers(self, settings):
        """With no servers mapping, the legacy URL is offered under the default key."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        configure_legacy(settings, "https://legacy.example.com")

        result = LibreNMSAPI.get_available_servers()

        assert "default" in result
        assert "legacy.example.com" in result["default"]

    def test_no_legacy_url_returns_default_label(self, settings):
        """With neither a servers mapping nor a legacy URL, only the bare default label is offered."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        configure_legacy(settings, None)

        result = LibreNMSAPI.get_available_servers()

        assert result == {"default": "Default Server"}


@pytest.mark.django_db
class TestGetLibreNMSIdDictServerKey:
    """get_librenms_id reads and writes real per-server device mappings."""

    def test_dict_cf_uses_the_client_server_key(self, settings, librenms_server):
        """The client returns the mapping for its bound server."""
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("mapped-device", librenms_cf={"primary": "23", "secondary": 47})
        api = api_for(settings, librenms_server.url, key="primary")

        assert api.get_librenms_id(device) == 23
        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"]["primary"] == "23"

    def test_zero_mapping_is_ignored_and_discovered(self, settings, librenms_server):
        """A zero mapping is invalid, so hostname discovery replaces it."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("stored-zero-device", librenms_cf={"default": 0})
        librenms_server.register(
            "/api/v0/devices/stored-zero-device",
            {"status": "ok", "devices": [{"device_id": 31}]},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).get_librenms_id(device)

        assert result == 31
        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"]["default"] == 31

    def test_store_librenms_id_via_hostname_lookup(self, settings, librenms_server):
        """A hostname lookup writes only the bound server mapping."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("hostname-device", librenms_cf={"default": 11, "primary": None})
        librenms_server.register(
            "/api/v0/devices/hostname-device",
            {"status": "ok", "devices": [{"device_id": 42}]},
            method="GET",
        )

        result = api_for(settings, librenms_server.url, key="primary").get_librenms_id(device)

        assert result == 42
        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"] == {
            "default": 11,
            "primary": 42,
        }


# Each read endpoint, its route, accepted body, expected value, query, and HTTP error details.
READ_ENDPOINTS = [
    pytest.param(
        "/api/v0/devices/1/ports",
        lambda api: api.get_ports(1),
        {"status": "ok", "ports": [{"port_id": 7, "ifName": "Gi0/1"}]},
        {"status": "ok", "ports": [{"port_id": 7, "ifName": "Gi0/1"}]},
        {
            "columns": [
                "port_id,ifName,ifType,ifSpeed,ifAdminStatus,ifDescr,ifAlias,ifPhysAddress,ifMtu,ifVlan,ifTrunk"
            ],
            "with": ["vlans"],
        },
        {
            404: ("Device not found in LibreNMS",),
            500: ("HTTP error:", "500"),
        },
        id="get_ports",
    ),
    pytest.param(
        "/api/v0/inventory/1",
        lambda api: api.get_inventory_filtered(1),
        {"status": "ok", "inventory": [{"entPhysicalIndex": 1, "entPhysicalClass": "chassis"}]},
        [{"entPhysicalIndex": 1, "entPhysicalClass": "chassis"}],
        {},
        {
            404: ("404", "/api/v0/inventory/1"),
            500: ("500", "/api/v0/inventory/1"),
        },
        id="get_inventory_filtered",
    ),
    pytest.param(
        "/api/v0/resources/vlans",
        lambda api: api.get_device_vlans(1),
        {
            "status": "ok",
            "vlans": [
                {"device_id": 1, "vlan_vlan": 100},
                {"device_id": 2, "vlan_vlan": 200},
            ],
        },
        [{"device_id": 1, "vlan_vlan": 100}],
        {},
        {
            404: ("VLANs resource not found",),
            500: ("HTTP error:", "500"),
        },
        id="get_device_vlans",
    ),
    pytest.param(
        "/api/v0/devices/1/links",
        lambda api: api.get_device_links(1),
        {"status": "ok", "links": [{"id": 3}]},
        {"status": "ok", "links": [{"id": 3}]},
        {},
        {
            404: ("404", "/api/v0/devices/1/links"),
            500: ("500", "/api/v0/devices/1/links"),
        },
        id="get_device_links",
    ),
    pytest.param(
        "/api/v0/devices",
        lambda api: api.list_devices(),
        {"status": "ok", "devices": [{"device_id": 1}]},
        [{"device_id": 1}],
        {},
        {
            404: ("404", "/api/v0/devices"),
            500: ("500", "/api/v0/devices"),
        },
        id="list_devices",
    ),
    pytest.param(
        "/api/v0/devices/1/ip",
        lambda api: api.get_device_ips(1),
        {"status": "ok", "addresses": [{"ipv4_address": "192.0.2.10"}]},
        [{"ipv4_address": "192.0.2.10"}],
        {},
        {
            404: ("404", "/api/v0/devices/1/ip"),
            500: ("500", "/api/v0/devices/1/ip"),
        },
        id="get_device_ips",
    ),
    pytest.param(
        "/api/v0/ports/1",
        lambda api: api.get_port_vlan_details(1),
        {"status": "ok", "port": [{"port_id": 1, "ifName": "Gi0/1"}]},
        {"port_id": 1, "ifName": "Gi0/1"},
        {"with": ["vlans"]},
        {
            404: ("Port not found in LibreNMS",),
            500: ("HTTP error:", "500"),
        },
        id="get_port_vlan_details",
    ),
]


@pytest.mark.django_db
class TestReadEndpointOutcomes:
    """Every read endpoint answers (ok, payload) over real HTTP and never raises at the caller."""

    @pytest.mark.parametrize(
        ("route", "call", "body", "expected", "expected_query", "http_errors"),
        READ_ENDPOINTS,
    )
    def test_a_served_body_is_returned(
        self,
        settings,
        librenms_server,
        route,
        call,
        body,
        expected,
        expected_query,
        http_errors,
    ):
        """Pins the route and the payload each reader pulls out, so the failure cases below are not vacuous."""
        received = []

        def read_endpoint(method, path, query, headers, body):
            received.append((method, path, query))
            return 200, expected_body

        expected_body = body
        librenms_server.register(route, read_endpoint, method="GET")

        ok, payload = call(api_for(settings, librenms_server.url))

        assert ok is True, payload
        assert payload == expected
        assert received == [("GET", route, expected_query)]

    @pytest.mark.parametrize("status", [404, 500])
    @pytest.mark.parametrize(
        ("route", "call", "body", "expected", "expected_query", "http_errors"),
        READ_ENDPOINTS,
    )
    def test_a_failing_status_is_reported(
        self,
        settings,
        librenms_server,
        route,
        call,
        body,
        expected,
        expected_query,
        http_errors,
        status,
    ):
        """A success-shaped body with a failing HTTP status is rejected."""
        librenms_server.register(route, body, status=status, method="GET")

        ok, detail = call(api_for(settings, librenms_server.url))

        assert ok is False
        for expected_detail in http_errors[status]:
            assert expected_detail in str(detail)

    @pytest.mark.parametrize(
        ("route", "call", "body", "expected", "expected_query", "http_errors"),
        READ_ENDPOINTS,
    )
    def test_an_unreachable_server_is_reported(
        self,
        settings,
        librenms_server,
        route,
        call,
        body,
        expected,
        expected_query,
        http_errors,
    ):
        """A refused connection is a real transport failure, reported rather than raised."""
        api = api_for(settings, librenms_server.url)
        librenms_server.stop()  # free the port so the request is refused

        ok, detail = call(api)

        assert ok is False
        assert detail is not None

    @pytest.mark.parametrize(
        ("route", "call", "body", "expected", "expected_query", "http_errors"),
        READ_ENDPOINTS,
    )
    def test_a_timeout_is_reported(
        self,
        settings,
        librenms_server,
        route,
        call,
        body,
        expected,
        expected_query,
        http_errors,
    ):
        """Every read endpoint catches the full RequestException hierarchy."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            ok, detail = call(api)

        assert ok is False
        assert "timed out" in str(detail).lower()


@pytest.mark.django_db
class TestGetPortsErrors:
    """get_ports names a missing device rather than reporting a bare failure."""

    def test_http_error_404_says_not_found(self, settings, librenms_server):
        librenms_server.register(
            "/api/v0/devices/1/ports",
            {"status": "error"},
            status=404,
            method="GET",
        )

        ok, msg = api_for(settings, librenms_server.url).get_ports(1)

        assert ok is False
        assert "not found" in msg.lower()


@pytest.mark.django_db
class TestGetInventoryFilteredErrors:
    """get_inventory_filtered falls back to the /all endpoint when the filtered one is empty."""

    def test_empty_results_with_no_filters_returns_true_empty_list(self, settings, librenms_server):
        """An empty inventory with status ok is a successful empty answer, not a failure."""
        librenms_server.register(
            "/api/v0/inventory/1",
            {"status": "ok", "inventory": []},
            method="GET",
        )

        ok, result = api_for(settings, librenms_server.url).get_inventory_filtered(1)

        assert ok is True
        assert result == []

    def test_fallback_to_all_endpoint_when_filtered_empty(self, settings, librenms_server):
        """A filtered query that comes back empty is retried against /all and filtered locally."""
        librenms_server.register(
            "/api/v0/inventory/1",
            {"status": "ok", "inventory": []},
            method="GET",
        )
        librenms_server.register(
            "/api/v0/inventory/1/all",
            {
                "status": "ok",
                "inventory": [
                    {
                        "entPhysicalName": "target",
                        "entPhysicalClass": "chassis",
                        "entPhysicalContainedIn": 0,
                    },
                    {
                        "entPhysicalName": "wrong-class",
                        "entPhysicalClass": "module",
                        "entPhysicalContainedIn": 0,
                    },
                    {
                        "entPhysicalName": "wrong-parent",
                        "entPhysicalClass": "chassis",
                        "entPhysicalContainedIn": 2,
                    },
                ],
            },
            method="GET",
        )

        ok, result = api_for(settings, librenms_server.url).get_inventory_filtered(
            1,
            ent_physical_class="chassis",
            ent_physical_contained_in=0,
        )

        assert ok is True
        assert result == [
            {
                "entPhysicalName": "target",
                "entPhysicalClass": "chassis",
                "entPhysicalContainedIn": 0,
            }
        ]

    def test_fallback_fails_when_all_endpoint_fails(self, settings, librenms_server):
        """When the fallback also fails there is nothing to report but the failure."""
        librenms_server.register(
            "/api/v0/inventory/1",
            {"status": "ok", "inventory": []},
            method="GET",
        )
        librenms_server.register(
            "/api/v0/inventory/1/all",
            {"status": "error"},
            status=500,
            method="GET",
        )

        ok, result = api_for(settings, librenms_server.url).get_inventory_filtered(1, ent_physical_class="chassis")

        assert ok is False
        assert isinstance(result, str)
        assert result


@pytest.mark.django_db
class TestGetDeviceVlansErrors:
    """get_device_vlans distinguishes a missing resource from a server error."""

    def test_http_error_404_names_the_resource(self, settings, librenms_server):
        librenms_server.register(
            "/api/v0/resources/vlans",
            {"status": "error"},
            status=404,
            method="GET",
        )

        ok, msg = api_for(settings, librenms_server.url).get_device_vlans(1)

        assert ok is False
        assert "not found" in msg.lower()

    def test_non_200_returns_http_status(self, settings, librenms_server):
        librenms_server.register(
            "/api/v0/resources/vlans",
            {"status": "error"},
            status=503,
            method="GET",
        )

        ok, msg = api_for(settings, librenms_server.url).get_device_vlans(1)

        assert ok is False
        assert "503" in msg


@pytest.mark.django_db
class TestGetDeviceInfoErrors:
    """get_device_info separates "the server says no such device" from "I could not ask"."""

    def test_non_200_returns_false(self, settings, librenms_server):
        librenms_server.register(
            "/api/v0/devices/1",
            {"status": "error"},
            status=404,
            method="GET",
        )

        ok, data = api_for(settings, librenms_server.url).get_device_info(1)

        assert ok is False

    def test_request_exception_reports_the_lookup_failure(self, settings, librenms_server):
        """A transport failure is not an answer about the device, so it is not "not found"."""
        from netbox_librenms_plugin.librenms_api import LibreNMSLookupError

        api = api_for(settings, librenms_server.url)
        librenms_server.stop()

        ok, data = api.get_device_info(1)

        assert ok is False
        assert isinstance(data, LibreNMSLookupError)
        assert data.status_code is None


@pytest.mark.django_db
class TestGetPortVlanDetailsErrors:
    """get_port_vlan_details names a missing port rather than reporting a bare failure."""

    def test_http_error_404_says_port_not_found(self, settings, librenms_server):
        librenms_server.register(
            "/api/v0/ports/1",
            {"status": "error"},
            status=404,
            method="GET",
        )

        ok, msg = api_for(settings, librenms_server.url).get_port_vlan_details(1)

        assert ok is False
        assert "not found" in msg.lower()

    def test_a_non_ok_payload_is_reported(self, settings, librenms_server):
        """A 200 whose body says error must not be read as a port."""
        librenms_server.register(
            "/api/v0/ports/1",
            {"status": "error", "message": "boom"},
            method="GET",
        )

        ok, msg = api_for(settings, librenms_server.url).get_port_vlan_details(1)

        assert ok is False
        assert msg == "boom"


class TestListDevicesSuccess:
    """list_devices reads real responses and forwards filters."""

    def test_list_devices_with_filters(self, settings, librenms_server):
        """A filter reaches the server and the matching devices reach the caller."""
        received = {}

        def devices(method, path, query, headers, body):
            received.update(method=method, path=path, query=query)
            return 200, {"status": "ok", "devices": [{"device_id": 1}]}

        librenms_server.register("/api/v0/devices", devices, method="GET")

        ok, result = api_for(settings, librenms_server.url).list_devices({"type": "network"})

        assert ok is True
        assert result == [{"device_id": 1}]
        assert received == {
            "method": "GET",
            "path": "/api/v0/devices",
            "query": {"type": ["network"]},
        }

    def test_list_devices_no_filters(self, settings, librenms_server):
        """An empty device list is a successful response."""
        librenms_server.register("/api/v0/devices", {"status": "ok", "devices": []}, method="GET")

        ok, result = api_for(settings, librenms_server.url).list_devices()

        assert ok is True
        assert result == []

    def test_http_200_error_envelope_returns_false(self, settings, librenms_server):
        """An HTTP 200 error envelope returns the server message."""
        librenms_server.register(
            "/api/v0/devices",
            {"status": "error", "message": "Device query rejected"},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).list_devices()

        assert result == (False, "Device query rejected")


class TestGetPoller:
    """get_poller_groups handles real success, API failure, and transport failure responses."""

    def test_success_returns_poller_groups(self, settings, librenms_server):
        """A valid poller response returns its group list."""
        groups = [{"id": 1, "group_name": "edge", "descr": "Edge pollers"}]
        librenms_server.register(
            "/api/v0/poller_group",
            {"status": "ok", "get_poller_group": groups},
            method="GET",
        )

        assert api_for(settings, librenms_server.url).get_poller_groups() == (True, groups)

    def test_request_exception_returns_false(self, settings, librenms_server):
        """A refused connection returns a failed result."""
        api = api_for(settings, librenms_server.url)
        librenms_server.stop()

        ok, result = api.get_poller_groups()

        assert ok is False
        assert result

    def test_timeout_returns_false(self, settings, librenms_server):
        """A timeout is caught through the RequestException base class."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            result = api.get_poller_groups()

        assert result == (False, "timed out")

    def test_non_ok_status_returns_false(self, settings, librenms_server):
        """An error payload returns the server message."""
        librenms_server.register(
            "/api/v0/poller_group",
            {"status": "error", "message": "Poller groups unavailable"},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).get_poller_groups()

        assert result == (False, "Poller groups unavailable")


class TestAddDeviceErrors:
    """add_device uses the real POST endpoint and reports failures."""

    def _make_device_data(self):
        return {
            "hostname": "router01",
            "snmp_version": "v2c",
            "community": "test-community",
            "force_add": False,
        }

    def test_success_posts_the_device_payload(self, settings, librenms_server):
        """A valid device is posted with the LibreNMS field names."""
        received = {}

        def add_device(method, path, query, headers, body):
            received.update(method=method, path=path, body=body)
            return 200, {"status": "ok"}

        librenms_server.register("/api/v0/devices", add_device, method="POST")

        result = api_for(settings, librenms_server.url).add_device(self._make_device_data())

        assert result == (True, "Device added successfully.")
        assert received == {
            "method": "POST",
            "path": "/api/v0/devices",
            "body": {
                "hostname": "router01",
                "snmpver": "v2c",
                "force_add": False,
                "community": "test-community",
            },
        }

    def test_request_exception_returns_false(self, settings, librenms_server):
        """A refused connection returns a failed result."""
        api = api_for(settings, librenms_server.url)
        librenms_server.stop()

        ok, msg = api.add_device(self._make_device_data())

        assert ok is False
        assert msg

    def test_timeout_returns_false(self, settings, librenms_server):
        """A timeout is caught through the RequestException base class."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.post", side_effect=requests.exceptions.Timeout("timed out")):
            result = api.add_device(self._make_device_data())

        assert result == (False, "timed out")

    def test_non_ok_response_returns_false(self, settings, librenms_server):
        """An error payload returns the server message."""
        librenms_server.register(
            "/api/v0/devices",
            {"status": "error", "message": "Already exists"},
            method="POST",
        )

        result = api_for(settings, librenms_server.url).add_device(self._make_device_data())

        assert result == (False, "Already exists")


class TestGetLocationsErrors:
    """get_locations reads a real response and handles a refused connection."""

    def test_success_returns_locations(self, settings, librenms_server):
        """A locations payload returns its location list."""
        locations = [{"id": 3, "location": "Test site"}]
        librenms_server.register(
            "/api/v0/resources/locations",
            {"status": "ok", "locations": locations},
            method="GET",
        )

        assert api_for(settings, librenms_server.url).get_locations() == (True, locations)

    def test_request_exception_returns_false(self, settings, librenms_server):
        """A refused connection returns a failed result."""
        api = api_for(settings, librenms_server.url)
        librenms_server.stop()

        ok, msg = api.get_locations()

        assert ok is False
        assert msg

    def test_timeout_returns_false(self, settings, librenms_server):
        """A timeout is caught through the RequestException base class."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            result = api.get_locations()

        assert result == (False, "timed out")


class TestUpdateDeviceFieldErrors:
    """update_device_field uses PATCH and handles a refused connection."""

    def test_success_patches_the_device(self, settings, librenms_server):
        """A successful update sends the field payload with PATCH."""
        received = {}

        def update_device(method, path, query, headers, body):
            received.update(method=method, path=path, body=body)
            return 200, {"status": "ok"}

        librenms_server.register("/api/v0/devices/1", update_device, method="PATCH")

        result = api_for(settings, librenms_server.url).update_device_field(1, {"field": "value"})

        assert result == (True, "Device fields updated successfully")
        assert received == {
            "method": "PATCH",
            "path": "/api/v0/devices/1",
            "body": {"field": "value"},
        }

    def test_request_exception_returns_false(self, settings, librenms_server):
        """A refused connection returns a failed result."""
        api = api_for(settings, librenms_server.url)
        librenms_server.stop()

        ok, msg = api.update_device_field(1, {"field": "value"})

        assert ok is False
        assert msg

    def test_timeout_returns_false(self, settings, librenms_server):
        """A timeout is caught through the RequestException base class."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.patch", side_effect=requests.exceptions.Timeout("timed out")):
            result = api.update_device_field(1, {"field": "value"})

        assert result == (False, "timed out")


class TestGetDeviceIdByIPErrors:
    """get_device_id_by_ip handles real lookup and failure responses."""

    IP = "198.18.0.10"
    ROUTE = f"/api/v0/devices/{IP}"

    def test_success_returns_device_id(self, settings, librenms_server):
        """A device response returns its ID."""
        librenms_server.register(self.ROUTE, {"devices": [{"device_id": 37}]}, method="GET")

        assert api_for(settings, librenms_server.url).get_device_id_by_ip(self.IP) == 37

    def test_request_exception_returns_none(self, settings, librenms_server):
        """A refused connection returns no device ID."""
        api = api_for(settings, librenms_server.url)
        librenms_server.stop()

        assert api.get_device_id_by_ip(self.IP) is None

    def test_timeout_returns_none(self, settings, librenms_server):
        """A timeout is caught through the RequestException base class."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            result = api.get_device_id_by_ip(self.IP)

        assert result is None

    def test_non_200_returns_none(self, settings, librenms_server):
        """A 404 response returns no device ID."""
        librenms_server.register(self.ROUTE, {"status": "error"}, status=404, method="GET")

        assert api_for(settings, librenms_server.url).get_device_id_by_ip(self.IP) is None

    def test_null_devices_field_returns_none(self, settings, librenms_server):
        """A null devices field returns no device ID."""
        librenms_server.register(self.ROUTE, {"devices": None}, method="GET")

        assert api_for(settings, librenms_server.url).get_device_id_by_ip(self.IP) is None

    def test_empty_devices_list_returns_none(self, settings, librenms_server):
        """An empty devices list returns no device ID."""
        librenms_server.register(self.ROUTE, {"devices": []}, method="GET")

        assert api_for(settings, librenms_server.url).get_device_id_by_ip(self.IP) is None


class TestGetDeviceIdByHostnameErrors:
    """get_device_id_by_hostname handles real lookup and failure responses."""

    HOSTNAME = "router01"
    ROUTE = f"/api/v0/devices/{HOSTNAME}"

    def test_success_returns_device_id(self, settings, librenms_server):
        """A device response returns its ID."""
        librenms_server.register(self.ROUTE, {"devices": [{"device_id": 41}]}, method="GET")

        assert api_for(settings, librenms_server.url).get_device_id_by_hostname(self.HOSTNAME) == 41

    def test_request_exception_returns_none(self, settings, librenms_server):
        """A refused connection returns no device ID."""
        api = api_for(settings, librenms_server.url)
        librenms_server.stop()

        assert api.get_device_id_by_hostname(self.HOSTNAME) is None

    def test_timeout_returns_none(self, settings, librenms_server):
        """A timeout is caught through the RequestException base class."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            result = api.get_device_id_by_hostname(self.HOSTNAME)

        assert result is None

    def test_null_devices_field_returns_none(self, settings, librenms_server):
        """A null devices field returns no device ID."""
        librenms_server.register(self.ROUTE, {"devices": None}, method="GET")

        assert api_for(settings, librenms_server.url).get_device_id_by_hostname(self.HOSTNAME) is None

    def test_empty_devices_list_returns_none(self, settings, librenms_server):
        """An empty devices list returns no device ID."""
        librenms_server.register(self.ROUTE, {"devices": []}, method="GET")

        assert api_for(settings, librenms_server.url).get_device_id_by_hostname(self.HOSTNAME) is None


@pytest.mark.django_db
class TestStorelibrenmsId:
    """_store_librenms_id persists to a real device mapping or the real cache."""

    def test_stores_via_custom_field_when_cf_has_key(self, settings, librenms_server):
        """An existing mapping field stores the ID in the database."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("store-mapped-device", librenms_cf={"default": None})
        api = api_for(settings, librenms_server.url)

        api._store_librenms_id(device, 42)

        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"]["default"] == 42

    def test_stores_in_cache_when_the_custom_field_is_absent(self, settings, librenms_server):
        """Only a NetBox without the plugin custom field reaches the cache fallback."""
        from django.core.cache import cache
        from extras.models import CustomField

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("store-cached-device")
        # The plugin migration gives every Device and VM this field, so the fallback below is
        # unreachable until the field is gone.
        CustomField.objects.filter(name="librenms_id").delete()
        device.refresh_from_db()
        assert "librenms_id" not in device.cf

        api = api_for(settings, librenms_server.url)
        cache_key = api._get_cache_key(device)

        api._store_librenms_id(device, 42)

        assert cache.get(cache_key) == 42
        assert api.server_key in cache_key


class TestParsePortVlanData:
    """parse_port_vlan_data normalizes VLAN data through a real client."""

    def test_no_if_vlan_returns_mode_none(self, settings, librenms_server):
        api = api_for(settings, librenms_server.url)
        port_data = {"port_id": 1, "ifName": "Gi0/1", "ifDescr": "GigabitEthernet", "ifVlan": ""}
        result = api.parse_port_vlan_data(port_data)
        assert result["mode"] is None

    def test_trunk_mode_set_correctly(self, settings, librenms_server):
        api = api_for(settings, librenms_server.url)
        port_data = {
            "port_id": 1,
            "ifName": "Gi0/1",
            "ifDescr": "GE",
            "ifVlan": "100",
            "ifTrunk": "dot1Q",
            "vlans": [{"vlan": 100, "untagged": 0}, {"vlan": 200, "untagged": 0}],
        }
        result = api.parse_port_vlan_data(port_data)
        assert result["mode"] == "tagged"
        assert 100 in result["tagged_vlans"]
        assert 200 in result["tagged_vlans"]

    def test_access_mode_from_vlan_array(self, settings, librenms_server):
        api = api_for(settings, librenms_server.url)
        port_data = {
            "port_id": 1,
            "ifName": "Gi0/2",
            "ifDescr": "GE",
            "ifVlan": "100",
            "vlans": [{"vlan": 100, "untagged": 1}],
        }
        result = api.parse_port_vlan_data(port_data)
        assert result["mode"] == "access"
        assert result["untagged_vlan"] == 100

    def test_fallback_to_if_vlan_when_no_vlans_array(self, settings, librenms_server):
        api = api_for(settings, librenms_server.url)
        port_data = {"port_id": 1, "ifName": "Gi0/3", "ifDescr": "GE", "ifVlan": "50", "ifTrunk": None}
        result = api.parse_port_vlan_data(port_data)
        assert result["mode"] == "access"
        assert result["untagged_vlan"] == 50

    def test_invalid_if_vlan_fallback_returns_none(self, settings, librenms_server):
        """A nonnumeric ifVlan leaves the untagged VLAN unset."""
        api = api_for(settings, librenms_server.url)
        port_data = {"port_id": 1, "ifName": "Gi0/4", "ifDescr": "GE", "ifVlan": "not-a-number"}
        result = api.parse_port_vlan_data(port_data)
        assert result["untagged_vlan"] is None

    def test_if_descr_used_as_interface_name(self, settings, librenms_server):
        api = api_for(settings, librenms_server.url)
        port_data = {"port_id": 1, "ifName": "Gi0/5", "ifDescr": "GigabitEthernet0/5", "ifVlan": ""}
        result = api.parse_port_vlan_data(port_data, interface_name_field="ifDescr")
        assert result["interface_name"] == "GigabitEthernet0/5"

    def test_string_vlan_id_normalized_to_int(self, settings, librenms_server):
        """String VLAN IDs are normalized to integers."""
        api = api_for(settings, librenms_server.url)
        port_data = {
            "port_id": 1,
            "ifName": "Gi0/6",
            "ifDescr": "GE",
            "ifVlan": "50",
            "vlans": [{"vlan": "50", "untagged": 1}, {"vlan": "100", "untagged": 0}],
        }
        result = api.parse_port_vlan_data(port_data)
        assert result["untagged_vlan"] == 50
        assert result["tagged_vlans"] == [100]

    def test_none_vlan_id_skipped(self, settings, librenms_server):
        """An entry without a VLAN ID is skipped."""
        api = api_for(settings, librenms_server.url)
        port_data = {
            "port_id": 1,
            "ifName": "Gi0/7",
            "ifDescr": "GE",
            "ifVlan": "200",
            "vlans": [{"untagged": 0}, {"vlan": 200, "untagged": 1}],
        }
        result = api.parse_port_vlan_data(port_data)
        assert result["untagged_vlan"] == 200
        assert result["tagged_vlans"] == []

    def test_malformed_vlan_id_skipped(self, settings, librenms_server):
        """A nonnumeric VLAN ID is skipped."""
        api = api_for(settings, librenms_server.url)
        port_data = {
            "port_id": 1,
            "ifName": "Gi0/8",
            "ifDescr": "GE",
            "ifVlan": "300",
            "vlans": [{"vlan": "N/A", "untagged": 0}, {"vlan": 300, "untagged": 1}],
        }
        result = api.parse_port_vlan_data(port_data)
        assert result["untagged_vlan"] == 300
        assert result["tagged_vlans"] == []

    def test_empty_vlans_array_falls_back_to_if_vlan(self, settings, librenms_server):
        """An empty VLAN array uses the ifVlan fallback."""
        api = api_for(settings, librenms_server.url)
        port_data = {"port_id": 1, "ifName": "Gi0/9", "ifDescr": "GE", "ifVlan": "10", "vlans": []}
        result = api.parse_port_vlan_data(port_data)
        assert result["untagged_vlan"] == 10


class TestGetDeviceInfoResponseFormats:
    """get_device_info handles unusual real response shapes."""

    def test_null_devices_returns_false(self, settings, librenms_server):
        """A null devices field returns an absent result."""
        librenms_server.register("/api/v0/devices/1", {"devices": None}, method="GET")

        success, result = api_for(settings, librenms_server.url).get_device_info(1)

        assert success is False
        assert result is None

    def test_empty_devices_list_returns_false(self, settings, librenms_server):
        """An empty devices list returns an absent result."""
        librenms_server.register("/api/v0/devices/1", {"devices": []}, method="GET")

        success, result = api_for(settings, librenms_server.url).get_device_info(1)

        assert success is False
        assert result is None

    def test_non_200_returns_false(self, settings, librenms_server):
        """A 404 response returns an absent result."""
        librenms_server.register("/api/v0/devices/1", {"status": "error"}, status=404, method="GET")

        success, result = api_for(settings, librenms_server.url).get_device_info(1)

        assert success is False
        assert result is None

    def test_valid_device_returns_device_dict(self, settings, librenms_server):
        """A valid response returns the first device object."""
        device = {"device_id": 42, "hostname": "router01"}
        librenms_server.register("/api/v0/devices/42", {"devices": [device]}, method="GET")

        success, result = api_for(settings, librenms_server.url).get_device_info(42)

        assert success is True
        assert result == device


class TestGetPortByIdErrors:
    """get_port_by_id reads a real port response and handles a refused connection."""

    def test_success_returns_port_response(self, settings, librenms_server):
        """A port response reaches the caller unchanged."""
        body = {"status": "ok", "ports": [{"port_id": 1}]}
        librenms_server.register("/api/v0/ports/1", body, method="GET")

        assert api_for(settings, librenms_server.url).get_port_by_id(1) == (True, body)

    def test_request_exception_returns_false(self, settings, librenms_server):
        """A refused connection returns a failed result."""
        api = api_for(settings, librenms_server.url)
        librenms_server.stop()

        ok, msg = api.get_port_by_id(1)

        assert ok is False
        assert msg

    def test_timeout_returns_false(self, settings, librenms_server):
        """A timeout is caught through the RequestException base class."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            result = api.get_port_by_id(1)

        assert result == (False, "timed out")


class TestGetDeviceInventoryErrors:
    """get_device_inventory reads a real response and handles a refused connection."""

    def test_success_returns_inventory(self, settings, librenms_server):
        """A valid inventory payload returns its item list."""
        inventory = [{"entPhysicalName": "Chassis", "entPhysicalClass": "chassis"}]
        librenms_server.register(
            "/api/v0/inventory/1/all",
            {"status": "ok", "inventory": inventory},
            method="GET",
        )

        assert api_for(settings, librenms_server.url).get_device_inventory(1) == (True, inventory)

    def test_request_exception_returns_false(self, settings, librenms_server):
        """A refused connection returns a failed result."""
        api = api_for(settings, librenms_server.url)
        librenms_server.stop()

        ok, msg = api.get_device_inventory(1)

        assert ok is False
        assert msg

    def test_timeout_returns_false(self, settings, librenms_server):
        """A timeout is caught through the RequestException base class."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            result = api.get_device_inventory(1)

        assert result == (False, "timed out")


@pytest.mark.django_db
class TestGetAvailableServersMultiConfig:
    """get_available_servers on a multi-server config."""

    def test_multi_server_config_returns_dict(self, settings):
        """Every fully configured server is offered under its display name."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        configure_servers(
            settings,
            {
                "primary": {
                    "librenms_url": "https://p.example.com",
                    "api_token": "pt",
                    "display_name": "Primary Server",
                },
                "secondary": {
                    "librenms_url": "https://s.example.com",
                    "api_token": "st",
                    "display_name": "Secondary Server",
                },
            },
        )

        assert LibreNMSAPI.get_available_servers() == {
            "primary": "Primary Server",
            "secondary": "Secondary Server",
        }

    def test_multi_server_config_uses_key_when_no_display_name(self, settings):
        """A server without a display name is offered under its key."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        configure_servers(settings, {"main": {"librenms_url": "https://m.example.com", "api_token": "mt"}})

        assert LibreNMSAPI.get_available_servers() == {"main": "main"}

    def test_a_partially_configured_server_is_not_offered(self, settings):
        """A server missing its token cannot be bound, so it must not appear as selectable."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        configure_servers(
            settings,
            {
                "usable": {"librenms_url": "https://u.example.com", "api_token": "ut"},
                "half": {"librenms_url": "https://h.example.com"},
            },
        )

        assert LibreNMSAPI.get_available_servers() == {"usable": "usable"}


class TestAddDeviceWithOptionalFields:
    """add_device sends each optional field over real HTTP."""

    def _make_base_data(self):
        return {
            "hostname": "router01",
            "snmp_version": "v2c",
            "community": "test-community",
            "force_add": False,
        }

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("port", 161, id="port"),
            pytest.param("transport", "udp6", id="transport"),
            pytest.param("port_association_mode", "ifName", id="port-association-mode"),
            pytest.param("poller_group", 2, id="poller-group"),
        ],
    )
    def test_add_device_with_optional_field(self, settings, librenms_server, field, value):
        """The selected optional field is present in the received JSON body."""
        received = []

        def add_device(method, path, query, headers, body):
            received.append(body)
            return 200, {"status": "ok"}

        librenms_server.register("/api/v0/devices", add_device, method="POST")
        data = {**self._make_base_data(), field: value}

        result = api_for(settings, librenms_server.url).add_device(data)

        assert result == (True, "Device added successfully.")
        assert received == [
            {
                "hostname": "router01",
                "snmpver": "v2c",
                "force_add": False,
                "community": "test-community",
                field: value,
            }
        ]


class TestUpdateDeviceFieldUnexpected:
    """update_device_field reports real API error payloads."""

    def test_non_ok_status_returns_false(self, settings, librenms_server):
        """A 200 error payload returns the server message."""
        librenms_server.register(
            "/api/v0/devices/1",
            {"status": "error", "message": "Failed"},
            method="PATCH",
        )

        result = api_for(settings, librenms_server.url).update_device_field(1, {"field": "value"})

        assert result == (False, "Failed")

    def test_http_error_uses_json_response_message(self, settings, librenms_server):
        """An HTTP error returns the JSON response message."""
        librenms_server.register(
            "/api/v0/devices/1",
            {"status": "error", "message": "Detailed error"},
            status=400,
            method="PATCH",
        )

        result = api_for(settings, librenms_server.url).update_device_field(1, {"field": "value"})

        assert result == (False, "Detailed error")


class TestGetLocationsNoLocations:
    """get_locations rejects a real payload without locations."""

    def test_no_locations_in_response(self, settings, librenms_server):
        """A payload without locations returns the format error."""
        librenms_server.register("/api/v0/resources/locations", {"status": "ok"}, method="GET")

        result = api_for(settings, librenms_server.url).get_locations()

        assert result == (False, "No locations found or unexpected response format")


class TestAddLocationErrors:
    """add_location uses the real POST endpoint and reports failures."""

    LOCATION = {"location": "TestSite", "lat": 0, "lng": 0}

    def test_success_adds_location(self, settings, librenms_server):
        """A successful response returns the new location ID."""
        received = []

        def add_location(method, path, query, headers, body):
            received.append((method, body))
            return 200, {"status": "ok", "message": "Location added #12"}

        librenms_server.register("/api/v0/locations", add_location, method="POST")

        result = api_for(settings, librenms_server.url).add_location(self.LOCATION)

        assert result == (True, {"id": "12", "message": "Location added #12"})
        assert received == [("POST", self.LOCATION)]

    def test_request_exception_returns_false(self, settings, librenms_server):
        """A refused connection returns a failed result."""
        api = api_for(settings, librenms_server.url)
        librenms_server.stop()

        ok, msg = api.add_location(self.LOCATION)

        assert ok is False
        assert msg

    def test_timeout_returns_false(self, settings, librenms_server):
        """A timeout is caught through the RequestException base class."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.post", side_effect=requests.exceptions.Timeout("timed out")):
            result = api.add_location(self.LOCATION)

        assert result == (False, "timed out")

    def test_http_error_uses_json_response_message(self, settings, librenms_server):
        """An HTTP error returns the JSON response message."""
        librenms_server.register(
            "/api/v0/locations",
            {"status": "error", "message": "Detailed error"},
            status=400,
            method="POST",
        )

        result = api_for(settings, librenms_server.url).add_location(self.LOCATION)

        assert result == (False, "Detailed error")


class TestUpdateLocationErrors:
    """update_location uses the real PATCH endpoint and reports failures."""

    LOCATION_DATA = {"lat": 0, "lng": 0}

    def test_success_updates_encoded_location(self, settings, librenms_server):
        """A successful update encodes the name and sends the field payload."""
        received = []

        def update_location(method, path, query, headers, body):
            received.append((method, path, body))
            return 200, {"status": "ok", "message": "Location updated"}

        librenms_server.register("/api/v0/locations/Test%20Site", update_location, method="PATCH")

        result = api_for(settings, librenms_server.url).update_location("Test Site", self.LOCATION_DATA)

        assert result == (True, "Location updated")
        assert received == [("PATCH", "/api/v0/locations/Test%20Site", self.LOCATION_DATA)]

    def test_request_exception_returns_false(self, settings, librenms_server):
        """A refused connection returns a failed result."""
        api = api_for(settings, librenms_server.url)
        librenms_server.stop()

        ok, msg = api.update_location("TestSite", self.LOCATION_DATA)

        assert ok is False
        assert msg

    def test_timeout_returns_false(self, settings, librenms_server):
        """A timeout is caught through the RequestException base class."""
        api = api_for(settings, librenms_server.url)

        with patch("requests.patch", side_effect=requests.exceptions.Timeout("timed out")):
            result = api.update_location("TestSite", self.LOCATION_DATA)

        assert result == (False, "timed out")

    def test_http_error_uses_json_response_message(self, settings, librenms_server):
        """An HTTP error returns the JSON response message."""
        librenms_server.register(
            "/api/v0/locations/TestSite",
            {"status": "error", "message": "Update failed"},
            status=400,
            method="PATCH",
        )

        result = api_for(settings, librenms_server.url).update_location("TestSite", self.LOCATION_DATA)

        assert result == (False, "Update failed")


class TestGetInventoryFilteredNonOk:
    """get_inventory_filtered reports real HTTP and payload failures."""

    def test_non_200_response_returns_false(self, settings, librenms_server):
        """A 404 response returns an HTTP error detail."""
        librenms_server.register("/api/v0/inventory/1", {"status": "error"}, status=404, method="GET")

        ok, data = api_for(settings, librenms_server.url).get_inventory_filtered(1)

        assert ok is False
        assert "404" in data

    def test_ent_physical_contained_in_filter(self, settings, librenms_server):
        """The contained-in filter reaches the server."""
        inventory = [{"entPhysicalContainedIn": "1", "entPhysicalName": "slot1"}]
        received = []

        def filtered_inventory(method, path, query, headers, body):
            received.append(query)
            return 200, {"status": "ok", "inventory": inventory}

        librenms_server.register("/api/v0/inventory/1", filtered_inventory, method="GET")

        ok, data = api_for(settings, librenms_server.url).get_inventory_filtered(
            1,
            ent_physical_contained_in="1",
        )

        assert ok is True
        assert data == inventory
        assert received == [{"entPhysicalContainedIn": ["1"]}]

    def test_empty_inventory_without_ok_status_returns_false(self, settings, librenms_server):
        """An empty inventory without an ok status is a format failure."""
        librenms_server.register("/api/v0/inventory/1", {"inventory": []}, method="GET")

        result = api_for(settings, librenms_server.url).get_inventory_filtered(1)

        assert result == (False, "Unexpected response format")


class TestGetDeviceVlansHttpError:
    """get_device_vlans reports real HTTP errors."""

    def test_http_404_returns_not_found(self, settings, librenms_server):
        """A 404 response names the missing VLAN resource."""
        librenms_server.register("/api/v0/resources/vlans", {"status": "error"}, status=404, method="GET")

        result = api_for(settings, librenms_server.url).get_device_vlans(1)

        assert result == (False, "VLANs resource not found")

    def test_http_5xx_returns_error(self, settings, librenms_server):
        """A 500 response returns an HTTP error detail."""
        librenms_server.register("/api/v0/resources/vlans", {"status": "error"}, status=500, method="GET")

        ok, msg = api_for(settings, librenms_server.url).get_device_vlans(1)

        assert ok is False
        assert "HTTP error" in msg
        assert "500" in msg


class TestGetPortVlanDetailsHttpError:
    """get_port_vlan_details reports a real non-404 HTTP error."""

    def test_http_non_404_returns_http_error(self, settings, librenms_server):
        """A 500 response returns an HTTP error detail."""
        librenms_server.register("/api/v0/ports/1", {"status": "error"}, status=500, method="GET")

        ok, msg = api_for(settings, librenms_server.url).get_port_vlan_details(1)

        assert ok is False
        assert "HTTP error" in msg
        assert "500" in msg


class TestGetInventoryFilteredNonOkStatus:
    """get_inventory_filtered reports a real 500 response."""

    def test_non_200_status(self, settings, librenms_server):
        """A 500 response returns an HTTP error detail."""
        librenms_server.register("/api/v0/inventory/1", {"status": "error"}, status=500, method="GET")

        ok, data = api_for(settings, librenms_server.url).get_inventory_filtered(1)

        assert ok is False
        assert "500" in data


class TestGetDeviceVlansNonOkResponse:
    """get_device_vlans reports a real non-ok payload."""

    def test_vlans_response_status_not_ok(self, settings, librenms_server):
        """An error payload returns the server message."""
        librenms_server.register(
            "/api/v0/resources/vlans",
            {"status": "error", "message": "Failed to retrieve VLANs"},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).get_device_vlans(1)

        assert result == (False, "Failed to retrieve VLANs")


class TestGetDeviceInventoryNonOkStatus:
    """get_device_inventory reports a real 500 response."""

    def test_non_200_status(self, settings, librenms_server):
        """A 500 response returns an HTTP error detail."""
        librenms_server.register("/api/v0/inventory/1/all", {"status": "error"}, status=500, method="GET")

        ok, data = api_for(settings, librenms_server.url).get_device_inventory(1)

        assert ok is False
        assert "500" in data


class TestMalformedPayloads:
    """API readers reject malformed payloads served over real HTTP."""

    def test_get_device_inventory_none_inventory(self, settings, librenms_server):
        """A null inventory is rejected."""
        librenms_server.register(
            "/api/v0/inventory/1/all",
            {"status": "ok", "inventory": None},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).get_device_inventory(1)

        assert result == (False, "Unexpected response format: invalid 'inventory' payload")

    def test_get_device_inventory_non_list_inventory(self, settings, librenms_server):
        """An object inventory is rejected."""
        librenms_server.register(
            "/api/v0/inventory/1/all",
            {"status": "ok", "inventory": {}},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).get_device_inventory(1)

        assert result == (False, "Unexpected response format: invalid 'inventory' payload")

    def test_get_device_inventory_non_dict_inventory_item(self, settings, librenms_server):
        """A non-object inventory item is rejected."""
        librenms_server.register(
            "/api/v0/inventory/1/all",
            {"status": "ok", "inventory": [None, {"entPhysicalName": "slot1"}]},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).get_device_inventory(1)

        assert result == (False, "Unexpected response format: invalid 'inventory' payload")

    def test_get_inventory_filtered_none_inventory(self, settings, librenms_server):
        """A null filtered inventory is rejected without using the fallback."""
        librenms_server.register(
            "/api/v0/inventory/1",
            {"status": "ok", "inventory": None},
            method="GET",
        )
        librenms_server.register(
            "/api/v0/inventory/1/all",
            {"status": "ok", "inventory": [{"entPhysicalClass": "chassis"}]},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).get_inventory_filtered(
            1,
            ent_physical_class="chassis",
        )

        assert result == (False, "Unexpected response format: invalid 'inventory' payload")

    def test_get_inventory_filtered_non_dict_inventory_item(self, settings, librenms_server):
        """A non-object filtered inventory item is rejected without using the fallback."""
        librenms_server.register(
            "/api/v0/inventory/1",
            {"status": "ok", "inventory": ["bad"]},
            method="GET",
        )
        librenms_server.register(
            "/api/v0/inventory/1/all",
            {"status": "ok", "inventory": [{"entPhysicalClass": "chassis"}]},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).get_inventory_filtered(
            1,
            ent_physical_class="chassis",
        )

        assert result == (False, "Unexpected response format: invalid 'inventory' payload")

    def test_list_devices_none_devices(self, settings, librenms_server):
        """A null devices field is rejected."""
        librenms_server.register(
            "/api/v0/devices",
            {"status": "ok", "devices": None},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).list_devices()

        assert result == (False, "Unexpected response format: missing 'devices' list")

    def test_list_devices_non_list_devices(self, settings, librenms_server):
        """A string devices field is rejected."""
        librenms_server.register(
            "/api/v0/devices",
            {"status": "ok", "devices": "bad"},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).list_devices()

        assert result == (False, "Unexpected response format: missing 'devices' list")

    def test_get_device_vlans_none_vlans(self, settings, librenms_server):
        """A null VLANs field is rejected."""
        librenms_server.register(
            "/api/v0/resources/vlans",
            {"status": "ok", "vlans": None},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).get_device_vlans(1)

        assert result == (False, "Unexpected response format: missing 'vlans' list")

    def test_get_device_vlans_fails_closed_on_non_dict_items(self, settings, librenms_server):
        """A non-object VLAN item is rejected."""
        vlans = [None, "bad", {"device_id": 1, "vlan_id": 10}]
        librenms_server.register(
            "/api/v0/resources/vlans",
            {"status": "ok", "vlans": vlans},
            method="GET",
        )

        result = api_for(settings, librenms_server.url).get_device_vlans(1)

        assert result == (False, "Unexpected response format: invalid item shape in 'vlans'")

    def test_get_device_ips_none_addresses(self, settings, librenms_server):
        """A null addresses field is rejected."""
        librenms_server.register("/api/v0/devices/1/ip", {"addresses": None}, method="GET")

        result = api_for(settings, librenms_server.url).get_device_ips(1)

        assert result == (False, "Unexpected response format: 'addresses' must be a list")
