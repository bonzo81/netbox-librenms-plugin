"""Real multi-server cache-scoping tests for sync-tab GET renders."""

from copy import deepcopy

import pytest
from django.core.cache import cache
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip, make_superuser

pytestmark = pytest.mark.django_db


@pytest.fixture
def configured_servers(configure_librenms):
    """Configure two real LibreNMS clients without requiring a network request."""
    configure_librenms(
        {
            "default": {
                "display_name": "Default test server",
                "librenms_url": "https://default.example.test",
                "api_token": "test-token",
                "verify_ssl": True,
            },
            "production": {
                "display_name": "Production test server",
                "librenms_url": "https://production.example.test",
                "api_token": "test-token",
                "verify_ssl": True,
            },
        }
    )


def _request(server_key):
    request = RequestFactory().get("/", {"server_key": server_key})
    request.user = make_superuser()
    return request


def _bind(view, request):
    view.setup(request)
    return view


class TestConfiguredServerCacheScoping:
    """A configured requested server reads only that server's snapshot."""

    def test_interfaces_render_the_requested_servers_snapshot(self, configured_servers):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("multiserver-interface")
        make_interface(device, "Ethernet1")
        view = DeviceInterfaceTableView()
        request = _request("production")
        key = view.get_cache_key(device, "ports", "production")
        cache.set(key, {"ports": [{"port_id": 11, "ifName": "Ethernet1", "ifAdminStatus": "up"}]})

        context = _bind(view, request).get_context_data(request, device, "ifName")

        assert context["server_key"] == "production"
        assert context["table"] is not None
        assert list(context["table"].data)[0]["port_id"] == 11

    def test_cables_render_the_requested_servers_snapshot(self, configured_servers):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        device = make_device("multiserver-cable")
        view = DeviceCableTableView()
        request = _request("production")
        key = view.get_cache_key(device, "links", "production")
        cache.set(key, {"links": [{"local_port": "Ethernet2", "local_port_id": 12}]})

        context = _bind(view, request).get_context_data(request, device)

        assert context["server_key"] == "production"
        assert context["table"] is not None
        assert list(context["table"].data)[0]["local_port_id"] == 12

    def test_vlans_render_the_requested_servers_snapshot(self, configured_servers):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

        device = make_device("multiserver-vlan")
        view = DeviceVLANTableView()
        request = _request("production")
        key = view.get_cache_key(device, "vlans", "production")
        cache.set(key, [{"vlan_vlan": 120, "vlan_name": "DATA"}])

        context = _bind(view, request).get_vlan_context(request, device)

        assert context["server_key"] == "production"
        assert context["vlan_table"] is not None
        assert list(context["vlan_table"].data)[0]["vlan_id"] == 120

    def test_ip_addresses_render_the_requested_servers_snapshot(self, configured_servers):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device = make_device("multiserver-ip")
        interface = make_interface(device, "Ethernet3")
        make_ip("198.18.30.1/24", assigned_object=interface)
        view = DeviceIPAddressTableView()
        request = _request("production")
        key = view.get_cache_key(device, "ip_addresses", "production")
        cache.set(
            key,
            {
                "ip_addresses": [
                    {
                        "ip_address": "198.18.30.1",
                        "prefix_length": 24,
                        "ip_with_mask": "198.18.30.1/24",
                        "port_id": 13,
                        "interface_name": interface.name,
                    }
                ],
                "mgmt_ip": "",
                "ports_by_id": {13: {"port_id": 13, "ifName": interface.name, "ifDescr": interface.name}},
                "interface_name_field": "ifName",
            },
        )

        context = _bind(view, request).get_context_data(request, device)

        assert context["server_key"] == "production"
        assert context["table"] is not None
        assert list(context["table"].data)[0]["ip_with_mask"] == "198.18.30.1/24"

    def test_modules_use_the_requested_server_mapping_and_snapshot(self, configured_servers):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = make_device("multiserver-module", librenms_cf={"production": 14})
        view = DeviceModuleTableView()
        request = _request("production")
        key = view.get_cache_key(device, "inventory", "production")
        cache.set(key, {"inventory": [], "librenms_id": 14, "oob_librenms_id": None})

        context = _bind(view, request).get_context_data(request, device)

        assert context["server_key"] == "production"
        assert context["object"] == device
        assert context["table"] is not None


class TestRemovedServerFailsClosed:
    """An unconfigured requested key never renders default or stale per-key data."""

    @pytest.fixture(autouse=True)
    def _default_only(self, configure_librenms):
        configure_librenms(
            {
                "default": {
                    "librenms_url": "https://default.example.test",
                    "api_token": "test-token",
                    "verify_ssl": True,
                }
            }
        )

    @pytest.mark.parametrize(
        ("view_path", "data_type", "payload", "context_method", "table_key", "extra_args"),
        [
            (
                "netbox_librenms_plugin.views.object_sync.devices.DeviceInterfaceTableView",
                "ports",
                {"ports": [{"port_id": 21, "ifName": "Ethernet4", "ifAdminStatus": "up"}]},
                "get_context_data",
                "table",
                ("ifName",),
            ),
            (
                "netbox_librenms_plugin.views.object_sync.devices.DeviceCableTableView",
                "links",
                {"links": [{"local_port": "Ethernet4", "local_port_id": 21}]},
                "get_context_data",
                "table",
                (),
            ),
            (
                "netbox_librenms_plugin.views.object_sync.devices.DeviceVLANTableView",
                "vlans",
                [{"vlan_vlan": 210, "vlan_name": "STALE"}],
                "get_vlan_context",
                "vlan_table",
                (),
            ),
            (
                "netbox_librenms_plugin.views.object_sync.devices.DeviceIPAddressTableView",
                "ip_addresses",
                {"ip_addresses": [], "mgmt_ip": "", "ports_by_id": {}},
                "get_context_data",
                "table",
                (),
            ),
        ],
    )
    def test_stale_removed_server_snapshot_is_ignored(
        self,
        view_path,
        data_type,
        payload,
        context_method,
        table_key,
        extra_args,
    ):
        from django.utils.module_loading import import_string

        view_class = import_string(view_path)
        device = make_device(f"removed-{data_type}")
        if data_type == "ports":
            make_interface(device, "Ethernet4")
        view = view_class()
        request = _request("removed")
        cache.set(view.get_cache_key(device, data_type, "removed"), payload)

        context = getattr(_bind(view, request), context_method)(request, device, *extra_args)

        assert context["server_key"] == "removed"
        assert context[table_key] is None

    def test_removed_server_ip_context_keeps_real_move_candidates(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        winner = make_device("removed-ip-winner")
        donor = make_device(
            "removed-ip-donor",
            librenms_cf={"removed": {"_migrated_to": {"device_id": winner.pk, "server_key": "removed"}}},
        )
        interface = make_interface(donor, "Ethernet5")
        address = make_ip("198.18.31.1/24", assigned_object=interface)
        view = DeviceIPAddressTableView()
        request = _request("removed")

        context = _bind(view, request).get_context_data(request, donor)

        assert context["movable_ips"] == [
            {"id": address.pk, "address": "198.18.31.1/24", "interface_name": "Ethernet5"}
        ]
        assert context["set_primary_ip"] is False


def test_cables_render_with_a_cache_backend_without_ttl(settings, configured_servers):
    """Cable rendering degrades cleanly on Django cache backends without ``ttl``."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

    cache_config = deepcopy(settings.CACHES)
    cache_config["default"] = {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "multiserver-no-ttl",
    }
    settings.CACHES = cache_config
    assert not hasattr(cache, "ttl")
    device = make_device("multiserver-no-ttl")
    view = DeviceCableTableView()
    request = _request("production")
    key = view.get_cache_key(device, "links", "production")
    cache.set(key, {"links": [{"local_port": "Ethernet6", "local_port_id": 31}]})

    context = _bind(view, request).get_context_data(request, device)

    assert context["server_key"] == "production"
    assert context["table"] is not None
    assert context["cache_expiry"] is None
