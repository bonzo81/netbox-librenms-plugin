"""
How the IP tab resolves each row's LibreNMS interface name.

Its own file rather than an append to ``test_ip_address_render_boundaries.py``: that file sits on a
lower branch of the PR stack, so adding to it conflicts on every restack.
"""

import pytest
from django.core.cache import cache

from netbox_librenms_plugin.tests.conftest import make_device, make_interface
from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_view
from netbox_librenms_plugin.utils import set_librenms_device_id

SERVER_KEY = "default"
DEVICE_ID = 6100


def _set_librenms_id(obj, value):
    set_librenms_device_id(obj, value, SERVER_KEY)
    obj.save(update_fields=["custom_field_data"])


def _request():
    return make_request("get")


def _ip_view(live_librenms):
    from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

    return make_view(DeviceIPAddressTableView, None, librenms_api=live_librenms.api)


def _seed(live_librenms, port_count):
    """Serve a device whose ports each carry one IP address."""
    ports = [
        {"port_id": 9000 + index, "ifName": f"Ethernet{index}", "ifDescr": f"Ethernet{index}"}
        for index in range(port_count)
    ]
    live_librenms.server.register(
        f"/api/v0/devices/{DEVICE_ID}/ports",
        {"status": "ok", "ports": ports},
    )
    live_librenms.server.register(
        f"/api/v0/devices/{DEVICE_ID}/ip",
        {
            "status": "ok",
            "addresses": [
                {"ipv4_address": f"198.18.{index}.10", "ipv4_prefixlen": 24, "port_id": port["port_id"]}
                for index, port in enumerate(ports)
            ],
        },
    )
    # The per-port route still answers, so a regression re-introducing the fan-out passes its
    # assertions and is caught only by the request count below.
    for port in ports:
        live_librenms.server.register(f"/api/v0/ports/{port['port_id']}", {"status": "ok", "port": [port]})
    return ports


@pytest.mark.django_db
class TestIpRowInterfaceNames:
    """The device ports payload already carries every name the IP rows need."""

    def test_enrichment_does_not_fetch_each_port_individually(self, live_librenms):
        """One /devices/{id}/ports read replaces one /ports/{port_id} call per row."""
        device = make_device("ip-fanout", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        ports = _seed(live_librenms, 6)
        view = _ip_view(live_librenms)
        success, raw = view.get_ip_addresses(device)
        assert success is True
        live_librenms.server.requests.clear()

        view.enrich_ip_data(raw, device, "ifName", server_key=SERVER_KEY)

        per_port = [r for r in live_librenms.server.requests if r["path"].startswith("/api/v0/ports/")]
        assert per_port == [], f"{len(per_port)} per-port fetches for {len(ports)} rows"

    def test_rows_still_carry_the_librenms_interface_name(self, live_librenms):
        """Dropping the fan-out must not drop the name it was fetched for."""
        device = make_device("ip-fanout-names", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        interface = make_interface(device, "Ethernet0", iface_type="1000base-t")
        _set_librenms_id(interface, 9000)
        _seed(live_librenms, 3)
        view = _ip_view(live_librenms)
        _success, raw = view.get_ip_addresses(device)

        enriched = view.enrich_ip_data(raw, device, "ifName", server_key=SERVER_KEY)

        assert [row["interface_name"] for row in enriched] == ["Ethernet0", "Ethernet1", "Ethernet2"]

    def test_the_alternate_name_field_is_honoured(self, live_librenms):
        """ifDescr-mode devices read their name from the same one ports payload."""
        device = make_device("ip-fanout-ifdescr", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        _seed(live_librenms, 2)
        view = _ip_view(live_librenms)
        _success, raw = view.get_ip_addresses(device)

        enriched = view.enrich_ip_data(raw, device, "ifDescr", server_key=SERVER_KEY)

        assert [row["interface_name"] for row in enriched] == ["Ethernet0", "Ethernet1"]

    def test_a_row_naming_an_unknown_port_keeps_its_address(self, live_librenms):
        """An IP row pointing at a port the device does not report still renders, without a name."""
        device = make_device("ip-fanout-unknown", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        _seed(live_librenms, 1)
        live_librenms.server.register(
            f"/api/v0/devices/{DEVICE_ID}/ip",
            {
                "status": "ok",
                "addresses": [{"ipv4_address": "198.18.9.9", "ipv4_prefixlen": 24, "port_id": 4242}],
            },
        )
        view = _ip_view(live_librenms)
        _success, raw = view.get_ip_addresses(device)

        enriched = view.enrich_ip_data(raw, device, "ifName", server_key=SERVER_KEY)

        assert len(enriched) == 1
        assert enriched[0]["ip_address"] == "198.18.9.9"
        assert enriched[0].get("interface_name") is None


@pytest.mark.django_db
class TestWarmRenderWithoutACachedPortMap:
    """A snapshot cached before ports_by_id existed still renders."""

    def test_a_pre_upgrade_cache_entry_renders_and_names_its_rows(self, live_librenms):
        """The names are rebuilt from LibreNMS; the view must not need an id a fresh fetch would have set."""

        device = make_device("ip-warm-preupgrade", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        _seed(live_librenms, 2)
        live_librenms.server.register(
            f"/api/v0/devices/{DEVICE_ID}",
            {"status": "ok", "devices": [{"device_id": DEVICE_ID, "ip": "198.18.0.1"}]},
        )
        view = _ip_view(live_librenms)
        # A snapshot from before the port map was cached: it carries mgmt_ip, so nothing else on
        # the warm path resolves the LibreNMS id.
        cache.set(
            view.get_cache_key(device, "ip_addresses", SERVER_KEY),
            {
                "ip_addresses": [
                    {"ipv4_address": "198.18.0.10", "ipv4_prefixlen": 24, "port_id": 9000},
                    {"ipv4_address": "198.18.1.10", "ipv4_prefixlen": 24, "port_id": 9001},
                ],
                "mgmt_ip": "198.18.0.1",
                "interface_name_field": "ifName",
            },
            timeout=300,
        )

        context = view._prepare_context(_request(), device, "ifName", fetch_fresh=False, server_key=SERVER_KEY)

        assert context is not None
        rows = cache.get(view.get_cache_key(device, "ip_addresses", SERVER_KEY))["ports_by_id"]
        assert sorted(rows) == ["9000", "9001"]
