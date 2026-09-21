"""
How the IP tab resolves each row's LibreNMS interface name.

Its own file rather than an append to ``test_ip_address_render_boundaries.py``: that file sits on a
lower branch of the PR stack, so adding to it conflicts on every restack.
"""

from copy import deepcopy

import pytest
from django.core.cache import cache
from ipam.models import VRF, IPAddress
from tenancy.models import Tenant

from netbox_librenms_plugin.data_shapes.recordings_store import load_recording
from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable
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


def _recorded_ip_view(recording_server, recording_name):
    """
    Return a real view, device, IP rows, and server backed by one recording.

    Args:
        recording_server: Loader for the recording-backed HTTP server.
        recording_name: Bundled data-shape recording name.

    Returns:
        tuple: The view, NetBox device, LibreNMS IP rows, and recording server.

    """
    from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

    recording = load_recording(recording_name)
    server, api = recording_server(recording)
    device = make_device(
        f"ip-vrf-{recording_name}",
        librenms_cf={api.server_key: {"id": recording["device_id"]}},
    )
    view = make_view(DeviceIPAddressTableView, None, librenms_api=api)
    success, rows = view.get_ip_addresses(device)
    assert success is True
    return view, device, rows, server


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
class TestIpRowVrfSuggestions:
    """LibreNMS VRF evidence can preselect an unset NetBox VRF choice."""

    def test_warm_render_keeps_suggestion_without_reading_librenms_vrfs(self, recording_server):
        """A cached source identity supplies the suggestion without another LibreNMS read."""
        target = VRF.objects.create(name="NetBox RD target", rd="203.0.113.82:61434")
        view, device, _rows, server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        view.librenms_api.cache_timeout = 300
        fresh = view._prepare_context(_request(), device, "ifName", fetch_fresh=True, server_key="test")
        assert fresh is not None
        server.requests.clear()

        warm = view._prepare_context(_request(), device, "ifName", fetch_fresh=False, server_key="test")

        assert all(request["path"] != "/api/v0/routing/vrf" for request in server.requests)
        tagged = next(row for row in warm["table"].data if row["port_id"] == 23724)
        assert tagged["vrf_id"] is None
        assert tagged["suggested_vrf_id"] == target.pk
        assert tagged["vrf_suggested_from"] == {
            "name": "vrf-df874a",
            "matched_by": "route distinguisher",
        }

    def test_warm_render_removes_suggestion_after_netbox_vrf_is_deleted(self, recording_server):
        """A warm render drops a cached suggestion whose current NetBox VRF is gone."""
        target = VRF.objects.create(name="NetBox RD target", rd="203.0.113.82:61434")
        view, device, _rows, server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        view.librenms_api.cache_timeout = 300
        fresh = view._prepare_context(_request(), device, "ifName", fetch_fresh=True, server_key="test")
        assert fresh is not None
        target.delete()
        server.requests.clear()

        warm = view._prepare_context(_request(), device, "ifName", fetch_fresh=False, server_key="test")

        assert all(request["path"] != "/api/v0/routing/vrf" for request in server.requests)
        tagged = next(row for row in warm["table"].data if row["port_id"] == 23724)
        assert tagged.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in tagged

    def test_warm_render_adds_suggestion_after_netbox_vrf_is_created(self, recording_server):
        """A warm render binds cached source identity to a newly created NetBox VRF."""
        view, device, _rows, server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        view.librenms_api.cache_timeout = 300
        fresh = view._prepare_context(_request(), device, "ifName", fetch_fresh=True, server_key="test")
        assert fresh is not None
        target = VRF.objects.create(name="NetBox RD target", rd="203.0.113.82:61434")
        server.requests.clear()

        warm = view._prepare_context(_request(), device, "ifName", fetch_fresh=False, server_key="test")

        assert all(request["path"] != "/api/v0/routing/vrf" for request in server.requests)
        tagged = next(row for row in warm["table"].data if row["port_id"] == 23724)
        assert tagged["vrf_id"] is None
        assert tagged["suggested_vrf_id"] == target.pk
        assert tagged["vrf_suggested_from"] == {
            "name": "vrf-df874a",
            "matched_by": "route distinguisher",
        }

    def test_poisoned_cached_vrf_identity_does_not_suggest(self, recording_server):
        """A malformed cached source identity is ignored without a LibreNMS read."""
        VRF.objects.create(name="vrf-0ed884")
        view, device, _rows, server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        view.librenms_api.cache_timeout = 300
        fresh = view._prepare_context(_request(), device, "ifName", fetch_fresh=True, server_key="test")
        assert fresh is not None
        cache_key = view.get_cache_key(device, "ip_addresses", "test")
        snapshot = cache.get(cache_key)
        tagged = next(row for row in snapshot["ip_addresses"] if row["port_id"] == 17347)
        tagged["librenms_vrf"] = {"name": [], "rd": None}
        cache.set(cache_key, snapshot)
        server.requests.clear()

        warm = view._prepare_context(_request(), device, "ifName", fetch_fresh=False, server_key="test")

        assert all(request["path"] != "/api/v0/routing/vrf" for request in server.requests)
        tagged = next(row for row in warm["table"].data if row["port_id"] == 17347)
        assert tagged.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in tagged

    def test_a_suggestion_never_reaches_the_verify_path_as_a_netbox_vrf(self, recording_server):
        """A suggestion preselects a dropdown. Read as the row's VRF it would report "Synced"."""
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        target = VRF.objects.create(name="Suggested only", rd="203.0.113.82:61434")
        view, device, rows, _server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")

        enriched = view.enrich_ip_data(rows, device, "ifName", server_key="test")

        tagged = next(row for row in enriched if row["port_id"] == 23724)
        assert tagged["suggested_vrf_id"] == target.pk
        # NetBox holds no IPAddress for this row, so the verify path must still offer to create it.
        verify = SingleIPAddressVerifyView()
        _row, original_vrf_id, _port_id = verify._find_in_cache({"ip_addresses": [tagged]}, "192.0.2.27", 29)
        assert original_vrf_id is None
        assert verify._determine_status(False, False, original_vrf_id, target.pk) == "sync"

    def test_cache_only_render_does_not_read_librenms_vrfs(self, recording_server):
        """The cache-status view can render a tagged snapshot without touching LibreNMS."""
        target = VRF.objects.create(name="NetBox RD target", rd="203.0.113.82:61434")
        view, device, _rows, server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        view.librenms_api.cache_timeout = 300
        fresh = view._prepare_context(_request(), device, "ifName", fetch_fresh=True, server_key="test")
        assert fresh is not None
        view.cache_only = True
        server.requests.clear()

        cached = view._prepare_context(_request(), device, "ifName", fetch_fresh=False, server_key="test")

        assert all(request["path"] != "/api/v0/routing/vrf" for request in server.requests)
        tagged = next(row for row in cached["table"].data if row["port_id"] == 23724)
        assert tagged["suggested_vrf_id"] == target.pk

    def test_unique_route_distinguisher_suggests_a_vrf(self, recording_server):
        """A unique RD match takes precedence over the different VRF names."""
        target = VRF.objects.create(name="NetBox RD target", rd="203.0.113.82:61434")
        view, device, rows, server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")

        enriched = view.enrich_ip_data(rows, device, "ifName", server_key="test")

        tagged = next(row for row in enriched if row["port_id"] == 23724)
        assert tagged["vrf_id"] is None
        assert tagged["suggested_vrf_id"] == target.pk
        assert tagged["vrf_suggested_from"] == {
            "name": "vrf-df874a",
            "matched_by": "route distinguisher",
        }
        vrf_requests = [request for request in server.requests if request["path"] == "/api/v0/routing/vrf"]
        assert len(vrf_requests) == 1

    def test_suggested_vrf_renders_an_accessible_source_mark(self, recording_server):
        """The preselected dropdown identifies why LibreNMS suggested the VRF."""
        VRF.objects.create(name="NetBox RD target", rd="203.0.113.82:61434")
        view, device, rows, _server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        enriched = view.enrich_ip_data(rows, device, "ifName", server_key="test")
        tagged = next(row for row in enriched if row["port_id"] == 23724)

        html = IPAddressTable([tagged]).as_html(_request())

        label = "Suggested from LibreNMS VRF vrf-df874a by route distinguisher match"
        assert f'title="{label}"' in html
        assert f'aria-label="{label}"' in html
        assert "mdi-lightbulb-on-outline text-muted" in html

    def test_unique_name_suggests_a_vrf_when_no_rd_matches(self, recording_server):
        """A unique name supplies the suggestion when the source has no RD."""
        target = VRF.objects.create(name="vrf-0ed884")
        view, device, rows, _server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")

        enriched = view.enrich_ip_data(rows, device, "ifName", server_key="test")

        tagged = next(row for row in enriched if row["port_id"] == 17347)
        assert tagged["vrf_id"] is None
        assert tagged["suggested_vrf_id"] == target.pk
        assert tagged["vrf_suggested_from"] == {
            "name": "vrf-0ed884",
            "matched_by": "name",
        }

    def test_untagged_port_neither_suggests_nor_fetches_vrfs(self, recording_server):
        """The common ifVrf=0 path has no VRF request or suggestion."""
        VRF.objects.create(name="Unrelated VRF", rd="203.0.113.82:61434")
        view, device, rows, server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        untagged_rows = [row for row in rows if row["port_id"] == 17351]
        server.requests.clear()

        enriched = view.enrich_ip_data(untagged_rows, device, "ifName", server_key="test")

        assert enriched[0].get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in enriched[0]
        assert all(request["path"] != "/api/v0/routing/vrf" for request in server.requests)

    def test_nokia_base_instance_is_not_a_vrf_suggestion(self, recording_server):
        """Nokia Base identifies the global table, not a VRF named Base."""
        VRF.objects.create(name="Base")
        view, device, rows, _server = _recorded_ip_view(recording_server, "nokia-timos-transceivers")

        enriched = view.enrich_ip_data(rows, device, "ifName", server_key="test")

        base_row = next(row for row in enriched if row["port_id"] == 442)
        assert base_row.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in base_row

    def test_ambiguous_route_distinguisher_does_not_suggest_a_vrf(self, recording_server):
        """An RD claimed by two NetBox VRFs binds neither one."""
        first = VRF.objects.create(name="First RD claimant", rd="203.0.113.82:61434")
        # NetBox enforces RD uniqueness, so keep the second candidate unsaved.
        second = VRF(name="Second RD claimant", rd="203.0.113.82:61434")
        view, _device, rows, _server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        port_data = {}
        view._load_port_names(port_data, rows)
        vrf_identities = view._load_vrf_identities(port_data, rows)

        suggestions = view._load_vrf_suggestions(vrf_identities, [first, second])

        assert "23724" not in suggestions

    def test_ambiguous_name_does_not_suggest_a_vrf(self, recording_server):
        """A name claimed by two NetBox VRFs binds neither one."""
        first_tenant = Tenant.objects.create(name="First VRF tenant", slug="first-vrf-tenant")
        second_tenant = Tenant.objects.create(name="Second VRF tenant", slug="second-vrf-tenant")
        VRF.objects.create(name="vrf-0ed884", tenant=first_tenant)
        VRF.objects.create(name="vrf-0ed884", tenant=second_tenant)
        view, device, rows, _server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")

        enriched = view.enrich_ip_data(rows, device, "ifName", server_key="test")

        tagged = next(row for row in enriched if row["port_id"] == 17347)
        assert tagged.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in tagged

    def test_stale_port_id_does_not_suggest_a_vrf(self, recording_server):
        """An IP row cannot inherit VRF evidence from an unrelated port."""
        VRF.objects.create(name="NetBox RD target", rd="203.0.113.82:61434")
        view, device, rows, _server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        stale_row = {**next(row for row in rows if row["port_id"] == 23724), "port_id": 999999}

        enriched = view.enrich_ip_data([stale_row], device, "ifName", server_key="test")

        assert enriched[0].get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in enriched[0]

    @pytest.mark.parametrize("if_vrf", [None, 0, "0", "not-a-number", True, "missing"])
    def test_unusable_if_vrf_does_not_suggest_or_fetch(self, recording_server, if_vrf):
        """Missing, null, zero, boolean and non-numeric port values are not VRF tags."""
        VRF.objects.create(name="NetBox RD target", rd="203.0.113.82:61434")
        view, device, rows, server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        recording = load_recording("iosxe-subinterfaces")
        ports_response = deepcopy(recording["responses"]["GET /api/v0/devices/45/ports"])
        port = next(port for port in ports_response["ports"] if port["port_id"] == 23724)
        if if_vrf == "missing":
            port.pop("ifVrf")
        else:
            port["ifVrf"] = if_vrf
        server.register("/api/v0/devices/45/ports", ports_response, method="GET")
        tagged_rows = [row for row in rows if row["port_id"] == 23724]
        server.requests.clear()

        enriched = view.enrich_ip_data(tagged_rows, device, "ifName", server_key="test")

        assert all(row.get("suggested_vrf_id") is None for row in enriched)
        assert all("vrf_suggested_from" not in row for row in enriched)
        assert all(request["path"] != "/api/v0/routing/vrf" for request in server.requests)

    def test_existing_ip_vrf_is_kept_without_a_suggestion_mark(self, recording_server):
        """Source evidence never replaces an existing NetBox IP VRF."""
        VRF.objects.create(name="Suggested target", rd="203.0.113.82:61434")
        existing_vrf = VRF.objects.create(name="Existing assignment")
        IPAddress.objects.create(address="192.0.2.27/29", vrf=existing_vrf, status="active")
        view, device, rows, _server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")

        enriched = view.enrich_ip_data(rows, device, "ifName", server_key="test")

        existing = next(row for row in enriched if row["ip_address"] == "192.0.2.27")
        assert existing["vrf_id"] == existing_vrf.pk
        assert "vrf_suggested_from" not in existing

    def test_an_existing_global_address_gets_no_suggestion(self, recording_server):
        """An address NetBox already holds globally is not a candidate: the row reads "Synced"."""
        VRF.objects.create(name="Suggested target", rd="203.0.113.82:61434")
        # No VRF: the row's vrf_id stays None, which must not be read as "free to suggest into".
        IPAddress.objects.create(address="192.0.2.27/29", status="active")
        view, device, rows, _server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")

        enriched = view.enrich_ip_data(rows, device, "ifName", server_key="test")

        existing = next(row for row in enriched if row["ip_address"] == "192.0.2.27")
        assert existing["exists"] is True
        assert existing.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in existing

    def test_string_typed_librenms_ids_still_suggest(self, recording_server):
        """LibreNMS documents string ids for /routing/vrf; the join must not silently stop."""
        target = VRF.objects.create(name="NetBox RD target", rd="203.0.113.82:61434")
        view, device, rows, server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        recording = load_recording("iosxe-subinterfaces")
        vrf_response = deepcopy(recording["responses"]["GET /api/v0/routing/vrf"])
        for vrf in vrf_response["vrfs"]:
            vrf["vrf_id"] = str(vrf["vrf_id"])
            vrf["device_id"] = str(vrf["device_id"])
        server.register("/api/v0/routing/vrf", vrf_response, method="GET")

        enriched = view.enrich_ip_data(rows, device, "ifName", server_key="test")

        tagged = next(row for row in enriched if row["port_id"] == 23724)
        assert tagged["suggested_vrf_id"] == target.pk

    def test_failed_vrf_read_leaves_rows_working_without_suggestions(self, recording_server, caplog):
        """A failed optional VRF read degrades to the ordinary manual dropdown."""
        VRF.objects.create(name="NetBox RD target", rd="203.0.113.82:61434")
        view, device, rows, server = _recorded_ip_view(recording_server, "iosxe-subinterfaces")
        server.register(
            "/api/v0/routing/vrf",
            {"status": "error", "message": "VRF service unavailable"},
            status=500,
            method="GET",
        )

        with caplog.at_level("DEBUG", logger="netbox_librenms_plugin.views.base.ip_addresses_view"):
            enriched = view.enrich_ip_data(rows, device, "ifName", server_key="test")

        assert len(enriched) == len(rows)
        assert all("vrf_suggested_from" not in row for row in enriched)
        assert "Could not load LibreNMS VRFs" in caplog.text


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
