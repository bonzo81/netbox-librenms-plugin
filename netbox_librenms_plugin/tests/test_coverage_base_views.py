"""Integration tests for the base cable, interface, and IP address views."""

import pytest
from django.core.cache import cache

from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    make_device,
    make_interface,
    make_ip,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_request
from netbox_librenms_plugin.tests.view_test_helpers import post as view_post


pytestmark = pytest.mark.django_db


def _set_librenms_id(obj, value, server_key="default"):
    obj.custom_field_data["librenms_id"] = {server_key: value}
    obj.save(update_fields=["custom_field_data"])


def _mapped_device(name, device_id=42):
    device = make_device(name)
    _set_librenms_id(device, device_id)
    return device


def _request(method="get", data=None):
    request = make_request(method, data or {}, path="/plugins/librenms/sync/")
    request.htmx = method == "post"
    return request


def _view(view_class, live_librenms, request=None):
    view = object.__new__(view_class)
    view.request = request or _request()
    view._librenms_api = live_librenms.api
    return view


def _register_ports(live_librenms, device_id=42, ports=None):
    if ports is None:
        ports = [
            {
                "port_id": 101,
                "ifName": "Ethernet1",
                "ifDescr": "uplink-1",
                "ifType": "ethernetCsmacd",
                "ifSpeed": 1_000_000_000,
                "ifAdminStatus": "up",
                "ifOperStatus": "up",
                "ifAlias": "",
                "ifPhysAddress": "02:00:00:00:00:01",
                "ifMtu": 1500,
                "ifVlan": 1,
                "ifTrunk": 0,
            }
        ]
    live_librenms.server.ports_response(device_id, ports)
    return ports


def _register_links(live_librenms, device_id=42, links=None, *, status=200):
    if links is None:
        links = [
            {
                "id": 501,
                "local_port_id": 101,
                "local_port": "Ethernet1",
                "protocol": "lldp",
                "remote_port": "Ethernet9",
                "remote_port_id": 201,
                "remote_hostname": "remote-01.example",
                "remote_device_id": 99,
            }
        ]
    body = {"status": "ok", "links": links} if status == 200 else {"status": "error", "message": "unavailable"}
    live_librenms.server.register(
        f"/api/v0/devices/{device_id}/links",
        body,
        status=status,
        method="GET",
    )
    return links


class TestCablePayloadBoundaries:
    @pytest.mark.parametrize(
        ("payload", "expected_names", "expected_alternates"),
        [
            (
                {
                    "ports": [
                        {"port_id": 1, "ifName": "Ethernet1", "ifDescr": "uplink-1"},
                        {"port_id": 2, "ifName": "Ethernet2", "ifDescr": "Ethernet2"},
                    ]
                },
                {"1": "Ethernet1", "2": "Ethernet2"},
                {"1": "uplink-1"},
            ),
            ({"ports": [None, "bad", {"ifName": "missing-id"}, {"port_id": 3}]}, {}, {}),
            ({"ports": None}, {}, {}),
            ([], {}, {}),
        ],
    )
    def test_port_name_maps_validate_external_rows(self, payload, expected_names, expected_alternates):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        names, alternates = BaseCableTableView._build_cable_port_name_maps(payload, "ifName", "ifDescr")

        assert names == expected_names
        assert alternates == expected_alternates

    def test_link_collection_keeps_real_identity_and_skips_malformed_rows(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        rows = BaseCableTableView._collect_cable_links(
            [
                None,
                {
                    "id": 7,
                    "local_port_id": 1,
                    "local_port": "fallback",
                    "protocol": "lldp",
                    "remote_port": "Ethernet2",
                    "remote_hostname": "remote.example",
                    "remote_port_id": 2,
                    "remote_device_id": 9,
                },
            ],
            {"1": "Ethernet1"},
            {"1": "uplink-1"},
            "main",
        )

        assert rows == [
            {
                "local_port": "Ethernet1",
                "local_port_alt": "uplink-1",
                "local_port_id": 1,
                "link_id": 7,
                "protocol": "lldp",
                "remote_port": "Ethernet2",
                "remote_device": "remote.example",
                "remote_port_id": 2,
                "remote_device_id": 9,
                "_source": "main",
            }
        ]

    @pytest.mark.parametrize(
        ("success", "data", "text"),
        [
            (False, {"error": "authentication failed"}, "authentication failed"),
            (False, {"message": "server unavailable"}, "server unavailable"),
            (True, {"status": "error", "message": "bad request"}, "bad request"),
            (True, [], "expected an object"),
        ],
    )
    def test_fetch_error_classification_preserves_external_detail(self, success, data, text):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        assert text in BaseCableTableView._classify_links_fetch_error(success, data)


class TestCableHTTPAndORM:
    def test_ports_before_link_resolution_do_not_contact_librenms(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        view = _view(DeviceCableTableView, live_librenms)

        assert view.get_ports_data(_mapped_device("cable-no-resolved-id")) == {"ports": []}
        assert live_librenms.server.requests == []

    def test_links_and_names_are_loaded_over_real_http(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        device = _mapped_device("cable-live-http")
        _register_ports(live_librenms)
        _register_links(live_librenms)
        view = _view(DeviceCableTableView, live_librenms)

        links = view.get_links_data(device, server_key="default")

        assert links[0]["local_port"] == "Ethernet1"
        assert links[0]["local_port_alt"] == "uplink-1"
        assert links[0]["remote_device"] == "remote-01.example"
        assert links[0]["_source"] == "main"
        # One read per neighbour, for the other LibreNMS name its ports answer to: the far end
        # must resolve as widely as the near one (see _attach_remote_port_aliases).
        assert [request["path"] for request in live_librenms.server.requests] == [
            "/api/v0/devices/42/links",
            "/api/v0/devices/42/ports",
            "/api/v0/devices/99/ports",
        ]

    @pytest.mark.parametrize(
        "body",
        [
            {"status": "error", "message": "bad credentials"},
            {"status": "ok", "links": None},
            ["not-an-object"],
        ],
    )
    def test_malformed_or_failed_link_response_fails_closed(self, live_librenms, body):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        device = _mapped_device("cable-failed-http")
        _register_ports(live_librenms)
        live_librenms.server.register("/api/v0/devices/42/links", body)
        view = _view(DeviceCableTableView, live_librenms)

        assert view.get_links_data(device, server_key="default") is None
        assert view._links_fetch_error

    def test_host_and_oob_links_are_merged_from_two_real_devices(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        device = make_device("cable-oob")
        device.custom_field_data["librenms_id"] = {"default": {"id": 42, "oob": {"id": 99, "type": "controller"}}}
        device.save(update_fields=["custom_field_data"])
        _register_ports(live_librenms, 42)
        _register_links(live_librenms, 42)
        _register_ports(
            live_librenms,
            99,
            [
                {
                    "port_id": 301,
                    "ifName": "management",
                    "ifDescr": "management",
                    "ifType": "ethernetCsmacd",
                }
            ],
        )
        _register_links(
            live_librenms,
            99,
            [
                {
                    "id": 502,
                    "local_port_id": 301,
                    "local_port": "management",
                    "protocol": "lldp",
                    "remote_port": "Ethernet10",
                    "remote_hostname": "console.example",
                }
            ],
        )
        view = _view(DeviceCableTableView, live_librenms)

        links = view.get_links_data(device, server_key="default")

        assert [link["_source"] for link in links] == ["main", "oob"]
        assert links[1]["local_port"] == "management"

    def test_device_and_interfaces_resolve_through_real_mappings(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        local_device = _mapped_device("cable-resolve-local")
        local_interface = make_interface(local_device, "Ethernet1")
        _set_librenms_id(local_interface, 101)
        remote_device = _mapped_device("remote-01", 99)
        remote_interface = make_interface(remote_device, "Ethernet9")
        _set_librenms_id(remote_interface, 201)
        _register_ports(live_librenms)
        _register_links(live_librenms)
        view = _view(DeviceCableTableView, live_librenms)

        found, matched, error = view.get_device_by_id_or_name(99, "wrong-name.example", "default")
        rows = view.enrich_links_data(view.get_links_data(local_device, "default"), local_device, "default")

        assert found == remote_device
        assert matched is True
        assert error is None
        assert rows[0]["netbox_local_interface_id"] == local_interface.pk
        assert rows[0]["netbox_remote_interface_id"] == remote_interface.pk
        assert rows[0]["cable_status"] == "No Cable"
        assert rows[0]["can_create_cable"] is True

    def test_real_cable_is_recognized_as_the_desired_connection(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        local_device = _mapped_device("cable-existing-local")
        local_interface = make_interface(local_device, "Ethernet1")
        _set_librenms_id(local_interface, 101)
        remote_device = _mapped_device("remote-existing", 99)
        remote_interface = make_interface(remote_device, "Ethernet9")
        _set_librenms_id(remote_interface, 201)
        cable = cable_together(local_interface, remote_interface)
        _register_ports(live_librenms)
        _register_links(live_librenms)
        view = _view(DeviceCableTableView, live_librenms)

        rows = view.enrich_links_data(view.get_links_data(local_device, "default"), local_device, "default")

        assert rows[0]["cable_status"] == "Cable Found"
        assert rows[0]["cable_url"].endswith(f"/dcim/cables/{cable.pk}/")

    def test_cached_ports_are_read_without_an_http_request(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        device = _mapped_device("cable-ports-cache")
        view = _view(DeviceCableTableView, live_librenms)
        view.librenms_id = 42
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, {"status": "ok", "ports": [{"port_id": 101}]}, timeout=300)

        assert view.get_ports_data(device, "default")["ports"] == [{"port_id": 101}]
        assert live_librenms.server.requests == []

    def test_exact_name_fallback_accepts_a_fqdn(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        device = make_device("remote-fqdn")
        view = _view(DeviceCableTableView, live_librenms)

        found, matched, error = view.get_device_by_id_or_name(None, "remote-fqdn.example", "default")

        assert found == device
        assert matched is True
        assert error is None


class TestInterfaceViewWithRealObjects:
    def test_primary_ip_and_real_interface_queryset(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = _mapped_device("interface-primary")
        interface = make_interface(device, "management")
        address = make_ip("198.18.10.1/24", assigned_object=interface)
        device.primary_ip4 = address
        device.save(update_fields=["primary_ip4"])
        device.refresh_from_db()
        view = _view(DeviceInterfaceTableView, live_librenms)

        assert view.get_ip_address(device) == "198.18.10.1"
        assert list(view.get_interfaces(device)) == [interface]
        assert view.get_select_related_field(device) == "device"

    def test_lookup_maps_drop_a_duplicate_librenms_port_id(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = _mapped_device("interface-duplicate-id")
        first = make_interface(device, "Ethernet1")
        second = make_interface(device, "Ethernet2")
        _set_librenms_id(first, 101)
        _set_librenms_id(second, 101)
        view = _view(DeviceInterfaceTableView, live_librenms)

        maps = view._build_interface_lookup_maps(device)

        assert maps["by_name"] == {"Ethernet1": first, "Ethernet2": second}
        assert 101 not in maps["by_librenms_id"]
        assert maps["librenms_id_counts"][101] == 2
        assert maps["by_librenms_id_matches"][101] == [first, second]

    @pytest.mark.parametrize(
        ("ports", "expected"),
        [
            ([{"ifName": "Port-Channel1", "ifType": "ieee8023adLag"}], True),
            ([{"ifName": "Ethernet1"}, {"ifName": "Ethernet1.100"}], True),
            ([{"ifName": "Ethernet1"}, {"ifName": "Ethernet2"}], False),
        ],
    )
    def test_relationship_detection_uses_real_port_shapes(self, live_librenms, ports, expected):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        view = _view(DeviceInterfaceTableView, live_librenms)

        assert view._has_structural_relationship_signals(ports) is expected

    def test_real_post_fetches_and_caches_port_snapshot(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        live_librenms.api.cache_timeout = 300
        device = _mapped_device("interface-refresh")
        _register_ports(live_librenms)
        request = _request("post", {"server_key": "default"})
        view = _view(DeviceInterfaceTableView, live_librenms, request)

        response = view_post(view, request, pk=device.pk)

        cached = cache.get(view.get_cache_key(device, "ports", "default"))
        assert response.status_code == 200
        assert cached["ports"][0]["port_id"] == 101
        assert cached["ports"][0]["_source"] == "main"
        assert [item["path"] for item in live_librenms.server.requests] == ["/api/v0/devices/42/ports"]

    def test_cached_snapshot_builds_a_real_table_without_http(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = _mapped_device("interface-cached-table")
        interface = make_interface(device, "Ethernet1")
        _set_librenms_id(interface, 101)
        ports = _register_ports(live_librenms)
        request = _request()
        view = _view(DeviceInterfaceTableView, live_librenms, request)
        cache.set(view.get_cache_key(device, "ports", "default"), {"status": "ok", "ports": ports}, timeout=300)

        context = view.get_context_data(request, device, "ifName", server_key="default")

        assert context["table"] is not None
        assert len(context["table"].rows) == 1
        assert context["netbox_only_interfaces"] == []
        assert live_librenms.server.requests == []

    def test_hidden_interface_state_is_not_rendered_outside_view_grant(self, client, live_librenms, settings):
        from dcim.models import Device, Interface
        from django.urls import reverse

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"]["default"]["cache_timeout"] = 300
        device = _mapped_device("interface-constrained-view")
        visible = make_interface(device, "Ethernet1")
        hidden = make_interface(device, "Ethernet2")
        _set_librenms_id(visible, 101)
        _set_librenms_id(hidden, 102)
        _register_ports(
            live_librenms,
            ports=[
                {
                    "port_id": 101,
                    "ifName": visible.name,
                    "ifDescr": visible.name,
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifAdminStatus": "up",
                    "ifOperStatus": "up",
                    "ifAlias": "",
                    "ifPhysAddress": "02:00:00:00:00:01",
                    "ifMtu": 1500,
                    "ifVlan": 1,
                    "ifTrunk": 0,
                },
                {
                    "port_id": 102,
                    "ifName": hidden.name,
                    "ifDescr": hidden.name,
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifAdminStatus": "up",
                    "ifOperStatus": "up",
                    "ifAlias": "",
                    "ifPhysAddress": "02:00:00:00:00:02",
                    "ifMtu": 1500,
                    "ifVlan": 1,
                    "ifTrunk": 0,
                },
            ],
        )
        user = make_user_with_perms(
            "interface-constrained-view-user",
            [("view", Device)],
            constraints={"pk": device.pk},
        )
        user = grant(user, "view", Interface, constraints={"pk": visible.pk})
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:device_interface_sync", args=[device.pk]),
            {"server_key": "default"},
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        html = response.content.decode()

        def interface_row(interface):
            marker = f'data-interface="{interface.name}"'
            marker_index = html.index(marker)
            return html[html.rindex("<tr", 0, marker_index) : html.index("</tr>", marker_index)]

        visible_row = interface_row(visible)
        hidden_row = interface_row(hidden)
        assert [item["path"] for item in live_librenms.server.requests] == ["/api/v0/devices/42/ports"]
        assert '<span class="text-success">Enabled</span>' in visible_row
        assert '<span class="text-success">Enabled</span>' not in hidden_row
        assert '<span class="text-danger">Enabled</span>' in hidden_row


class TestIPAddressHTTPAndORM:
    def _device(self):
        device = _mapped_device("ip-address-device")
        interface = make_interface(device, "Ethernet1")
        _set_librenms_id(interface, 101)
        return device, interface

    def _register(self, live_librenms, addresses):
        live_librenms.server.register(
            "/api/v0/devices/42/ip",
            {"status": "ok", "addresses": addresses},
        )
        port = {"port_id": 101, "ifName": "Ethernet1", "ifDescr": "uplink-1"}
        live_librenms.server.register("/api/v0/ports/101", {"status": "ok", "port": [port]})
        # The IP tab reads each row's interface name from the device's port list, not per port.
        live_librenms.server.register("/api/v0/devices/42/ports", {"status": "ok", "ports": [port]})
        live_librenms.server.device_info_response(
            42,
            hostname="ip-address-device.example",
            ip="198.18.20.1",
        )

    def test_real_http_rows_match_real_interface_and_ip(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device, interface = self._device()
        address = make_ip("198.18.20.1/24", assigned_object=interface)
        rows = [{"ip_address": "198.18.20.1", "prefix_length": 24, "port_id": 101}]
        self._register(live_librenms, rows)
        view = _view(DeviceIPAddressTableView, live_librenms)

        success, raw = view.get_ip_addresses(device)
        enriched = view.enrich_ip_data(
            raw,
            device,
            "ifName",
            mgmt_ip="198.18.20.1",
            server_key="default",
        )

        assert success is True
        assert enriched[0]["netbox_ip_id"] == address.pk
        assert enriched[0]["interface_name"] == interface.name
        assert enriched[0]["status"] == "matched"
        assert enriched[0]["is_mgmt_ip"] is True

    def test_new_address_is_a_sync_candidate(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device, interface = self._device()
        rows = [{"ip_address": "198.18.21.1", "prefix_length": 24, "port_id": 101}]
        self._register(live_librenms, rows)
        view = _view(DeviceIPAddressTableView, live_librenms)

        success, raw = view.get_ip_addresses(device)
        enriched = view.enrich_ip_data(raw, device, "ifName", server_key="default")

        assert success is True
        assert enriched[0]["exists"] is False
        assert enriched[0]["status"] == "sync"
        assert enriched[0]["interface_url"] == interface.get_absolute_url()

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            ({"ip_address": "198.18.30.1", "prefix_length": 24, "port_id": 1}, "198.18.30.1/24"),
            ({"ipv4_address": "198.18.31.1", "ipv4_prefixlen": 24, "port_id": 1}, "198.18.31.1/24"),
            ({"ipv6_compressed": "2001:db8::1", "ipv6_prefixlen": 64, "port_id": 1}, "2001:db8::1/64"),
        ],
    )
    def test_supported_librenms_address_shapes_are_normalized(self, live_librenms, entry, expected):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device, _interface = self._device()
        view = _view(DeviceIPAddressTableView, live_librenms)

        assert view._create_base_ip_entry(entry, device, [])["ip_with_mask"] == expected

    def test_invalid_external_address_shape_is_rejected(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device, _interface = self._device()
        view = _view(DeviceIPAddressTableView, live_librenms)

        with pytest.raises(ValueError, match="no supported address fields"):
            view._create_base_ip_entry({"port_id": 1}, device, [])

    def test_prefetch_scans_only_reported_addresses_and_marks_duplicate_port_ids(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device, first = self._device()
        second = make_interface(device, "Ethernet2")
        _set_librenms_id(second, 101)
        reported = make_ip("198.18.40.1/24", assigned_object=first)
        unreported = make_ip("198.18.40.2/24")
        view = _view(DeviceIPAddressTableView, live_librenms)

        prefetched = view._prefetch_netbox_data(device, {"198.18.40.1/24"}, "default")

        assert prefetched["interfaces_by_librenms_id"]["101"] is None
        assert str(reported.address) in prefetched["ip_addresses_map"]
        assert str(unreported.address) not in prefetched["ip_addresses_map"]

    def test_fresh_context_uses_http_then_warm_context_uses_cache(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        live_librenms.api.cache_timeout = 300
        device, _interface = self._device()
        rows = [{"ip_address": "198.18.50.1", "prefix_length": 24, "port_id": 101}]
        self._register(live_librenms, rows)
        request = _request()
        view = _view(DeviceIPAddressTableView, live_librenms, request)

        fresh = view._prepare_context(
            request,
            device,
            "ifName",
            fetch_fresh=True,
            server_key="default",
        )
        request_count = len(live_librenms.server.requests)
        warm = view._prepare_context(
            request,
            device,
            "ifName",
            fetch_fresh=False,
            server_key="default",
        )

        assert fresh["table"] is not None
        assert warm["table"] is not None
        assert len(live_librenms.server.requests) == request_count
        cached = cache.get(view.get_cache_key(device, "ip_addresses", "default"))
        assert cached["ports_by_id"]["101"]["ifName"] == "Ethernet1"
        assert cached["mgmt_ip"] == "198.18.20.1"

    @pytest.mark.parametrize(
        "body",
        [
            {"status": "ok", "addresses": None},
            {"status": "ok", "addresses": [{"ip_address": "198.18.60.1", "prefix_length": 24}]},
        ],
    )
    def test_bad_http_snapshot_is_not_cached(self, live_librenms, body):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device, _interface = self._device()
        live_librenms.server.register("/api/v0/devices/42/ip", body)
        request = _request()
        view = _view(DeviceIPAddressTableView, live_librenms, request)
        cache_key = view.get_cache_key(device, "ip_addresses", "default")
        cache.set(cache_key, {"old": "snapshot"}, timeout=300)

        result = view._prepare_context(request, device, "ifName", fetch_fresh=True, server_key="default")

        assert result is None
        assert cache.get(cache_key) is None

    def test_corrupt_cached_snapshot_is_purged(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device, _interface = self._device()
        request = _request()
        view = _view(DeviceIPAddressTableView, live_librenms, request)
        cache_key = view.get_cache_key(device, "ip_addresses", "default")
        cache.set(cache_key, ["not", "an", "envelope"], timeout=300)

        assert view._prepare_context(request, device, "ifName", server_key="default") is None
        assert cache.get(cache_key) is None


class TestBaseClassContracts:
    def test_interface_subclass_hooks_fail_fast(self, live_librenms):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        device = make_device("base-interface-contract")
        view = _view(BaseInterfaceTableView, live_librenms)

        with pytest.raises(NotImplementedError):
            view.get_interfaces(device)
        with pytest.raises(NotImplementedError):
            view.get_redirect_url(device)
        with pytest.raises(NotImplementedError):
            view.get_table([], device, "ifName")

    def test_cable_table_hook_fails_fast(self, live_librenms):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = _view(BaseCableTableView, live_librenms)

        with pytest.raises(NotImplementedError):
            view.get_table([], make_device("base-cable-contract"))


class TestVlanGroupOverrideScope:
    """Preserve the row-scope checks while the old mock-heavy suite is replaced."""

    @staticmethod
    def _fixtures(slug):
        from dcim.models import Site
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        site = Site.objects.create(name=f"Override {slug}", slug=f"override-{slug}", status="active")
        site_type = ContentType.objects.get_for_model(Site)
        in_scope = VLANGroup.objects.create(
            name=f"In scope {slug}", slug=f"in-scope-{slug}", scope_type=site_type, scope_id=site.pk
        )
        other_site = Site.objects.create(name=f"Other {slug}", slug=f"other-{slug}", status="active")
        out_of_scope = VLANGroup.objects.create(
            name=f"Out of scope {slug}", slug=f"out-of-scope-{slug}", scope_type=site_type, scope_id=other_site.pk
        )
        return in_scope, out_of_scope

    @staticmethod
    def _apply(port, overrides):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        BaseInterfaceTableView()._add_vlan_group_selection(
            port, {"vid_to_groups": {}, "vid_group_to_vlan": {}}, make_device("vlan-override-device"), overrides
        )

    def test_override_to_an_in_scope_group_without_the_vid_is_kept(self):
        in_scope, _ = self._fixtures("keep")
        port = {"untagged_vlan": 100, "tagged_vlans": [], "vlan_groups": [in_scope]}

        self._apply(port, {"100": str(in_scope.pk)})

        assert port["vlan_group_map"][100]["group_id"] == str(in_scope.pk)

    def test_override_outside_the_row_scope_is_rejected(self):
        in_scope, out_of_scope = self._fixtures("reject")
        port = {"untagged_vlan": 100, "tagged_vlans": [], "vlan_groups": [in_scope]}

        self._apply(port, {"100": str(out_of_scope.pk)})

        assert port["vlan_group_map"][100]["group_id"] == ""

    def test_multiple_overrides_read_the_row_scope_once(self):
        in_scope, _ = self._fixtures("once")

        class CountingGroups(list):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        groups = CountingGroups([in_scope])
        port = {"untagged_vlan": 100, "tagged_vlans": [101, 102], "vlan_groups": groups}
        overrides = {str(vid): str(in_scope.pk) for vid in (100, 101, 102)}

        self._apply(port, overrides)

        assert {value["group_id"] for value in port["vlan_group_map"].values()} == {str(in_scope.pk)}
        assert groups.iterations == 1
