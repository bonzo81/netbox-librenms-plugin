"""
Boundary paths of ``views/base/ip_addresses_view.py`` and ``ip_addressing.py``.

The primary home for these modules is ``test_coverage_base_views.py`` (render pipeline) and
``test_ip_verify.py`` (verify endpoint). These cases live in their own file because both sit on a
lower branch of the PR stack, where appending to their tails conflicts on every restack.
"""

import json

import pytest
from django.contrib import messages as django_messages
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip, make_superuser, make_vm
from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_view
from netbox_librenms_plugin.utils import set_librenms_device_id


SERVER_KEY = "default"


def _login(client, username):
    """Sign a real superuser into the test client."""
    client.force_login(make_superuser(username))


def _messages(response, level=None):
    """Return the messages the request actually queued, optionally filtered by level."""
    wanted = None if level is None else getattr(django_messages, level.upper())
    return [
        str(message) for message in get_messages(response.wsgi_request) if wanted is None or message.level == wanted
    ]


def _set_librenms_id(obj, value):
    """Store a LibreNMS id on a real object through the production writer."""
    set_librenms_device_id(obj, value, SERVER_KEY)
    obj.save(update_fields=["custom_field_data"])


def _ip_view(live_librenms, request=None):
    """Bind a real device IP table view to the loopback LibreNMS client."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

    return make_view(DeviceIPAddressTableView, request, librenms_api=live_librenms.api)


def _cache_key(obj):
    """Return the IP snapshot key production writes for *obj*."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

    return DeviceIPAddressTableView().get_cache_key(obj, "ip_addresses", SERVER_KEY)


def _refresh_url(device):
    """Return the IP tab refresh endpoint."""
    return reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk])


def _serve_ip_rows(live_librenms, device_id, rows):
    """Register the LibreNMS device IP list."""
    live_librenms.server.register(f"/api/v0/devices/{device_id}/ip", {"status": "ok", "addresses": rows})


def _serve_port(live_librenms, port_id, **fields):
    """Register one LibreNMS port record."""
    live_librenms.server.register(
        f"/api/v0/ports/{port_id}",
        {"status": "ok", "port": [{"port_id": port_id, **fields}]},
    )


def _serve_device_info(live_librenms, device_id, payload):
    """Register the LibreNMS device record the management-IP lookup reads."""
    live_librenms.server.register(
        f"/api/v0/devices/{device_id}",
        {"status": "ok", "devices": [{"device_id": device_id, **payload}]},
    )


@pytest.mark.django_db
class TestEnrichmentSkipsMalformedRows:
    """Enrichment tolerates rows LibreNMS returns that carry no usable port reference."""

    def test_rows_without_a_usable_port_reference_are_dropped(self, live_librenms):
        """A null row and a row without port_id are skipped instead of raising."""
        device = make_device("ip-enrich-malformed", librenms_cf={SERVER_KEY: {"id": 4301}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9301)
        _serve_ip_rows(
            live_librenms,
            4301,
            [
                None,
                {"ip_address": "198.18.49.5", "prefix_length": 24},
                {"ip_address": "198.18.49.10", "prefix_length": 24, "port_id": 9301},
            ],
        )
        _serve_port(live_librenms, 9301, ifName=interface.name, ifDescr=interface.name)
        view = _ip_view(live_librenms)

        success, raw = view.get_ip_addresses(device)
        enriched = view.enrich_ip_data(raw, device, "ifName", server_key=SERVER_KEY)

        assert success is True
        assert len(raw) == 3
        assert [row["ip_with_mask"] for row in enriched] == ["198.18.49.10/24"]


@pytest.mark.django_db
class TestManagementIPResolutionOnRender:
    """The render pipeline resolves the management IP without paying for a hopeless lookup."""

    def test_object_without_a_librenms_id_is_never_looked_up(self, live_librenms):
        """A cached snapshot missing mgmt_ip on an unmapped object requests no device record."""
        device = make_device("ip-render-no-id")
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        cache.set(
            _cache_key(device),
            {
                "ip_addresses": [
                    {
                        "ip_address": "198.18.51.10",
                        "prefix_length": 24,
                        "ip_with_mask": "198.18.51.10/24",
                        "port_id": 9302,
                        "interface_name": interface.name,
                    }
                ],
                "ports_by_id": {9302: {"port_id": 9302, "ifName": interface.name}},
                "interface_name_field": "ifName",
            },
            timeout=300,
        )
        request = make_request("get", path="/plugins/librenms/sync/")
        view = _ip_view(live_librenms, request)

        context = view._prepare_context(request, device, "ifName", fetch_fresh=False, server_key=SERVER_KEY)

        assert context is not None
        assert live_librenms.server.requests == []

    def test_failed_device_read_caches_an_empty_management_ip(self, client, live_librenms):
        """A LibreNMS fault while reading the device record caches a blank management IP."""
        device = make_device("ip-render-mgmt-fails", librenms_cf={SERVER_KEY: {"id": 4302}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9303)
        _serve_ip_rows(live_librenms, 4302, [{"ip_address": "198.18.51.20", "prefix_length": 24, "port_id": 9303}])
        _serve_port(live_librenms, 9303, ifName=interface.name, ifDescr=interface.name)
        live_librenms.server.register("/api/v0/devices/4302", {"status": "error"}, status=500)
        _login(client, "ip-render-mgmt-fails-user")

        response = client.post(_refresh_url(device), {"server_key": SERVER_KEY, "interface_name_field": "ifName"})

        assert response.status_code == 200
        cached = cache.get(_cache_key(device))
        assert cached["mgmt_ip"] == ""
        assert [row.get("is_mgmt_ip") for row in cached["ip_addresses"]] == [None]


@pytest.mark.django_db
class TestExistingAddressEnrichment:
    """An address that already exists in NetBox is described by its current assignment."""

    def test_unassigned_existing_address_stays_an_update_row(self, client, live_librenms):
        """An existing but unassigned address is reported as an update, not a match."""
        device = make_device("ip-render-unassigned", librenms_cf={SERVER_KEY: {"id": 4303}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9304)
        address = make_ip("198.18.51.30/24")
        _serve_ip_rows(live_librenms, 4303, [{"ip_address": "198.18.51.30", "prefix_length": 24, "port_id": 9304}])
        _serve_port(live_librenms, 9304, ifName=interface.name, ifDescr=interface.name)
        _serve_device_info(live_librenms, 4303, {"ip": "198.18.51.99"})
        _login(client, "ip-render-unassigned-user")

        response = client.post(_refresh_url(device), {"server_key": SERVER_KEY, "interface_name_field": "ifName"})

        assert response.status_code == 200
        row = cache.get(_cache_key(device))["ip_addresses"][0]
        assert row["netbox_ip_id"] == address.pk
        assert row["status"] == "update"
        assert row["interface_url"] == interface.get_absolute_url()


@pytest.mark.django_db
class TestPrepareContextDefaults:
    """``_prepare_context`` resolves its own interface name field when none is supplied."""

    def test_absent_interface_name_field_is_resolved_from_the_request(self, live_librenms):
        """With no field passed in, the request's interface_name_field decides the rendered name."""
        device = make_device("ip-render-name-field", librenms_cf={SERVER_KEY: {"id": 4304}})
        make_interface(device, "Ethernet1", iface_type="1000base-t")
        cache.set(
            _cache_key(device),
            {
                "ip_addresses": [
                    {
                        "ip_address": "198.18.51.40",
                        "prefix_length": 24,
                        "ip_with_mask": "198.18.51.40/24",
                        "port_id": 9305,
                    }
                ],
                "mgmt_ip": "",
                "ports_by_id": {9305: {"port_id": 9305, "ifName": "Gi0/1", "ifDescr": "uplink-to-core"}},
                "interface_name_field": "ifName",
            },
            timeout=300,
        )
        request = make_request("get", path="/plugins/librenms/sync/?interface_name_field=ifDescr")
        view = _ip_view(live_librenms, request)

        context = view._prepare_context(request, device, None, fetch_fresh=False, server_key=SERVER_KEY)

        assert [row["interface_name"] for row in context["table"].data] == ["uplink-to-core"]


@pytest.mark.django_db
class TestFreshFetchValidation:
    """A refresh fails closed on a payload the render pipeline cannot key by port."""

    def test_unhashable_port_id_fails_the_refresh_closed(self, client, live_librenms):
        """A row whose port_id is not a plain int or string purges the snapshot and reports failure."""
        device = make_device("ip-render-bad-port", librenms_cf={SERVER_KEY: {"id": 4305}})
        cache.set(_cache_key(device), {"ip_addresses": [], "mgmt_ip": "", "ports_by_id": {}}, timeout=300)
        _serve_ip_rows(live_librenms, 4305, [{"ip_address": "198.18.51.50", "prefix_length": 24, "port_id": {}}])
        _login(client, "ip-render-bad-port-user")

        response = client.post(_refresh_url(device), {"server_key": SERVER_KEY, "interface_name_field": "ifName"})

        assert response.status_code == 200
        assert cache.get(_cache_key(device)) is None
        assert any("Failed to fetch IP addresses" in text for text in _messages(response, "error"))


@pytest.mark.django_db
class TestRefreshWithAnUnknownServer:
    """A refresh naming an unconfigured server re-renders instead of fetching."""

    def test_unknown_server_key_re_renders_the_migrated_partial(self, client, live_librenms):
        """The error re-render keeps the migrated move card, resolved from the active server."""
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        winner = make_device("ip-refresh-winner")
        donor = make_device("ip-refresh-donor", librenms_cf={SERVER_KEY: {"id": 4306}})
        interface = make_interface(donor, "Ethernet1", iface_type="1000base-t")
        make_ip("198.18.51.60/24", assigned_object=interface)
        mark_librenms_migrated(donor, winner.pk, SERVER_KEY)
        donor.save()
        _login(client, "ip-refresh-unknown-server-user")

        response = client.post(_refresh_url(donor), {"server_key": "removed"}, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        assert live_librenms.server.requests == []
        assert _messages(response, "error") == ["Selected LibreNMS server is no longer configured."]
        assert f"Move IP addresses to {winner.name}".encode() in response.content


@pytest.mark.django_db
class TestSingleIPAddressVerify:
    """The verify endpoint validates its own JSON payload before touching the cache."""

    def _post(self, client, payload):
        """POST one verify payload as JSON."""
        return client.post(
            reverse("plugins:netbox_librenms_plugin:verify_ipaddress"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_virtual_machine_is_resolved_without_an_object_type(self, client):
        """With no object_type the endpoint falls back to a VirtualMachine lookup."""
        from dcim.models import Device

        _login(client, "ip-verify-vm-user")
        vm = make_vm("ip-verify-vm")
        while Device.objects.filter(pk=vm.pk).exists():
            vm = make_vm(f"ip-verify-vm-{vm.pk + 1}")

        response = self._post(client, {"device_id": vm.pk, "ip_address": "198.18.52.10/24"})

        assert response.status_code == 200
        assert response.json()["status"] == "success"

    def test_blank_object_id_is_rejected(self, client):
        """A blank object id is reported as missing, not as invalid."""
        _login(client, "ip-verify-blank-id-user")

        response = self._post(client, {"device_id": "", "ip_address": "198.18.52.20/24"})

        assert response.status_code == 400
        assert response.json()["message"] == "No object ID provided"

    def test_address_without_a_prefix_is_rejected(self, client):
        """A bare host address carries no prefix evidence and is rejected."""
        _login(client, "ip-verify-no-prefix-user")
        device = make_device("ip-verify-no-prefix")

        response = self._post(client, {"device_id": device.pk, "ip_address": "198.18.52.30"})

        assert response.status_code == 400
        assert response.json()["message"] == "Invalid IP address: prefix length is missing or invalid"

    def test_non_string_address_is_rejected(self, client):
        """A JSON number in the address field is rejected rather than parsed."""
        _login(client, "ip-verify-non-string-user")
        device = make_device("ip-verify-non-string")

        response = self._post(client, {"device_id": device.pk, "ip_address": 19821852})

        assert response.status_code == 400
        assert response.json()["message"] == "Invalid IP address: prefix length is missing or invalid"
