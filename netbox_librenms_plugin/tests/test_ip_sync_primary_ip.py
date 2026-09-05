"""
Primary-IP, interface-matching and redirect paths of ``views/sync/ip_addresses.py``.

The primary home for this module is ``test_coverage_sync_views2.py``. These cases live in their
own file because that file sits on a lower branch of the PR stack, where appending to its tail
conflicts on every restack.
"""

import pytest
from django.contrib import messages as django_messages
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse
from ipam.models import VRF, IPAddress
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.tests.conftest import (
    configure_no_librenms_servers,
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_view, missing_pk
from netbox_librenms_plugin.utils import set_librenms_device_id
from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView


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


def _ip_url(obj):
    """Return the IP sync endpoint for a Device or a VirtualMachine."""
    object_type = "virtualmachine" if isinstance(obj, VirtualMachine) else "device"
    return reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": object_type, "pk": obj.pk},
    )


def _row(address, port_id, interface_name, *, prefix_length=24, **extra):
    """Build one cached LibreNMS IP row."""
    row = {
        "ip_address": address,
        "prefix_length": prefix_length,
        "ip_with_mask": f"{address}/{prefix_length}",
        "port_id": port_id,
        "interface_name": interface_name,
    }
    row.update(extra)
    return row


def _seed(obj, rows):
    """Write the IP snapshot the sync view reads, keyed the way production keys it."""
    ports = {row["port_id"]: {"port_id": row["port_id"], "ifName": row["interface_name"]} for row in rows}
    cache.set(
        SyncIPAddressesView().get_cache_key(obj, "ip_addresses", SERVER_KEY),
        {
            "ip_addresses": rows,
            "mgmt_ip": "",
            "ports_by_id": ports,
            "interface_name_field": "ifName",
        },
        timeout=300,
    )


def _set_librenms_id(obj, value):
    """Store a LibreNMS id on a real object through the production writer."""
    set_librenms_device_id(obj, value, SERVER_KEY)
    obj.save(update_fields=["custom_field_data"])


def _serve_device_info(live_librenms, device_id, payload):
    """Register the LibreNMS device record the management-IP lookup reads."""
    live_librenms.server.register(
        f"/api/v0/devices/{device_id}",
        {"status": "ok", "devices": [{"device_id": device_id, **payload}]},
    )


class _PrimaryIPStealer:
    """Point a device at *address_pk* once its row is locked, as a concurrent request would."""

    def __init__(self, device_pk, address_pk):
        self.device_pk = device_pk
        self.address_pk = address_pk
        self.fired = False

    def __call__(self, execute, sql, params, many, context):
        """Run one out-of-band primary-IP write after the first device row lock."""
        from django.db import connection

        result = execute(sql, params, many, context)
        lowered = sql.lower()
        if not self.fired and "dcim_device" in lowered and "for update" in lowered:
            self.fired = True
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE dcim_device SET primary_ip4_id = %s WHERE id = %s",
                    [self.address_pk, self.device_pk],
                )
        return result


def _sync_payload(rows, *, set_primary=True):
    """Build the POST body the IP sync tab submits for *rows*."""
    row_ids = [row["ip_with_mask"] for row in rows]
    data = {"server_key": SERVER_KEY, "select": row_ids}
    data.update({f"vrf_{row_id}": "" for row_id in row_ids})
    if set_primary:
        data["set-primary-ip-toggle"] = "true"
    return data


@pytest.mark.django_db
class TestPrimaryIPFromManagementAddress:
    """The management address decides which synced row becomes the primary IP."""

    def test_management_row_becomes_the_primary_ip(self, client, live_librenms):
        """A synced row matching the LibreNMS management IP is written to primary_ip4."""
        device = make_device("ip-primary-set", librenms_cf={SERVER_KEY: {"id": 4201}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9201)
        _serve_device_info(live_librenms, 4201, {"ip": "198.18.40.10"})
        row = _row("198.18.40.10", 9201, interface.name)
        _seed(device, [row])
        _login(client, "ip-primary-set-user")

        response = client.post(_ip_url(device), _sync_payload([row]))

        assert response.status_code == 302
        address = IPAddress.objects.get(address=row["ip_with_mask"])
        assert address.assigned_object == interface
        device.refresh_from_db()
        assert device.primary_ip4_id == address.pk
        assert any(text.startswith("Set as Primary IP: 198.18.40.10/24") for text in _messages(response, "success"))

    def test_primary_ip_already_pointing_at_the_row_is_left_alone(self, client, live_librenms):
        """A row whose address is already the primary IP reports no primary change."""
        device = make_device("ip-primary-idempotent", librenms_cf={SERVER_KEY: {"id": 4202}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9202)
        _serve_device_info(live_librenms, 4202, {"ip": "198.18.41.10"})
        address = IPAddress.objects.create(address="198.18.41.10/24", assigned_object=interface, status="active")
        device.primary_ip4 = address
        device.save()
        row = _row("198.18.41.10", 9202, interface.name)
        _seed(device, [row])
        _login(client, "ip-primary-idempotent-user")

        response = client.post(_ip_url(device), _sync_payload([row]))

        assert response.status_code == 302
        assert any("IP addresses already exist" in text for text in _messages(response, "warning"))
        assert not any("Set as Primary IP" in text for text in _messages(response))
        device.refresh_from_db()
        assert device.primary_ip4_id == address.pk

    def test_primary_ip_written_after_the_object_was_read_is_respected(self, client, live_librenms):
        """The locked re-read sees a primary IP written after the sync loaded the object."""
        from django.db import connection

        device = make_device("ip-primary-stale", librenms_cf={SERVER_KEY: {"id": 4208}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9209)
        _serve_device_info(live_librenms, 4208, {"ip": "198.18.41.20"})
        address = IPAddress.objects.create(address="198.18.41.20/24", assigned_object=interface, status="active")
        row = _row("198.18.41.20", 9209, interface.name)
        _seed(device, [row])
        _login(client, "ip-primary-stale-user")
        stealer = _PrimaryIPStealer(device.pk, address.pk)

        with connection.execute_wrapper(stealer):
            response = client.post(_ip_url(device), _sync_payload([row]))

        assert stealer.fired is True
        assert response.status_code == 302
        assert not any("Set as Primary IP" in text for text in _messages(response))
        device.refresh_from_db()
        assert device.primary_ip4_id == address.pk

    def test_management_row_without_an_interface_reports_primary_not_set(self, client, live_librenms):
        """An unmatched management row is reported apart from an ordinary unmatched row."""
        device = make_device("ip-primary-no-interface", librenms_cf={SERVER_KEY: {"id": 4203}})
        _serve_device_info(live_librenms, 4203, {"ip": "198.18.42.10"})
        management_row = _row("198.18.42.10", 9203, "Ethernet1")
        other_row = _row("198.18.42.11", 9204, "Ethernet2")
        _seed(device, [management_row, other_row])
        _login(client, "ip-primary-no-interface-user")

        response = client.post(_ip_url(device), _sync_payload([management_row, other_row]))

        assert response.status_code == 302
        assert IPAddress.objects.filter(address__in=["198.18.42.10/24", "198.18.42.11/24"]).count() == 0
        warnings = _messages(response, "warning")
        assert any(text.startswith("Primary IP not set for 198.18.42.10/24") for text in warnings)
        assert any("Skipped (no matching NetBox interface): 198.18.42.11/24" in text for text in warnings)

    def test_primary_ip_refuses_a_management_only_sibling_interface(self, client, live_librenms):
        """A management-only interface on a chassis sibling cannot carry the primary IP."""
        from dcim.models import Interface

        _chassis, members = make_virtual_chassis_members("ip-primary-vc")
        viewed, sibling = members
        _set_librenms_id(viewed, 4204)
        management_interface = Interface.objects.create(device=sibling, name="mgmt0", type="1000base-t", mgmt_only=True)
        _set_librenms_id(management_interface, 9205)
        _serve_device_info(live_librenms, 4204, {"ip": "198.18.43.10"})
        row = _row("198.18.43.10", 9205, management_interface.name)
        _seed(viewed, [row])
        _login(client, "ip-primary-vc-user")

        response = client.post(_ip_url(viewed), _sync_payload([row]))

        assert response.status_code == 302
        address = IPAddress.objects.get(address="198.18.43.10/24")
        assert address.assigned_object == management_interface
        viewed.refresh_from_db()
        assert viewed.primary_ip4_id is None
        assert any(
            text.startswith("Primary IP not set for 198.18.43.10/24") and "not eligible" in text
            for text in _messages(response, "warning")
        )


@pytest.mark.django_db
class TestIPRowResolution:
    """Per-row VRF and interface resolution against current NetBox state."""

    def test_unknown_vrf_selection_fails_the_row(self, client, live_librenms):
        """A posted VRF the caller cannot resolve fails its row instead of writing a global address."""
        device = make_device("ip-vrf-missing", librenms_cf={SERVER_KEY: {"id": 4205}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9206)
        row = _row("198.18.44.10", 9206, interface.name)
        _seed(device, [row])
        _login(client, "ip-vrf-missing-user")

        response = client.post(
            _ip_url(device),
            {
                "server_key": SERVER_KEY,
                "select": row["ip_with_mask"],
                f"vrf_{row['ip_with_mask']}": missing_pk(VRF),
            },
        )

        assert response.status_code == 302
        assert not IPAddress.objects.filter(address="198.18.44.10/24").exists()
        assert any(
            "Selected VRF is no longer available or you do not have permission to view it." in text
            for text in _messages(response, "error")
        )

    def test_duplicate_stored_port_ids_skip_the_row(self, client, live_librenms):
        """An ambiguous port id skips the row and refuses the cached interface URL fallback."""
        device = make_device("ip-ambiguous-port", librenms_cf={SERVER_KEY: {"id": 4206}})
        first = make_interface(device, "Ethernet1", iface_type="1000base-t")
        second = make_interface(device, "Ethernet2", iface_type="1000base-t")
        _set_librenms_id(first, 9207)
        _set_librenms_id(second, 9207)
        row = _row("198.18.45.10", 9207, "Ethernet9", interface_url=first.get_absolute_url())
        _seed(device, [row])
        _login(client, "ip-ambiguous-port-user")

        response = client.post(_ip_url(device), _sync_payload([row], set_primary=False))

        assert response.status_code == 302
        assert not IPAddress.objects.filter(address="198.18.45.10/24").exists()
        assert any(
            "Skipped (no matching NetBox interface): 198.18.45.10/24" in text for text in _messages(response, "warning")
        )

    def test_renamed_interface_is_matched_through_the_cached_url(self, client, live_librenms):
        """A renamed interface with no stored port id is still resolved by the cached interface URL."""
        vm = make_vm("ip-renamed-interface")
        _set_librenms_id(vm, 4207)
        interface = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        cached_url = interface.get_absolute_url()
        interface.name = "eth1"
        interface.save()
        row = _row("198.18.46.10", 9208, "eth0", interface_url=cached_url)
        _seed(vm, [row])
        _login(client, "ip-renamed-interface-user")

        response = client.post(_ip_url(vm), _sync_payload([row], set_primary=False))

        assert response.status_code == 302
        assert IPAddress.objects.get(address="198.18.46.10/24").assigned_object == interface


@pytest.mark.django_db
class TestManagementIPLookup:
    """``get_management_ip`` reads the live LibreNMS device record over real HTTP."""

    def _view(self, live_librenms):
        """Bind a real sync view to the loopback LibreNMS client."""
        return make_view(SyncIPAddressesView, librenms_api=live_librenms.api)

    def test_object_without_a_librenms_id_is_never_looked_up(self, live_librenms):
        """An object with no resolvable LibreNMS id must not request a device record."""
        device = make_device("ip-mgmt-no-id")
        view = self._view(live_librenms)

        assert view.get_management_ip(device) is None
        assert "/api/v0/devices/None" not in [request["path"] for request in live_librenms.server.requests]

    def test_failed_device_read_yields_no_management_ip(self, live_librenms):
        """A LibreNMS fault while reading the device record yields no management IP."""
        device = make_device("ip-mgmt-read-fails", librenms_cf={SERVER_KEY: {"id": 4210}})
        live_librenms.server.register("/api/v0/devices/4210", {"status": "error"}, status=500)
        view = self._view(live_librenms)

        assert view.get_management_ip(device) is None
        assert "/api/v0/devices/4210" in [request["path"] for request in live_librenms.server.requests]

    def test_non_string_management_ip_is_ignored(self, live_librenms):
        """A device record whose ip is not a string reports no management IP."""
        device = make_device("ip-mgmt-not-a-string", librenms_cf={SERVER_KEY: {"id": 4211}})
        _serve_device_info(live_librenms, 4211, {"ip": 12345})
        view = self._view(live_librenms)

        assert view.get_management_ip(device) is None

    def test_management_ip_is_stripped(self, live_librenms):
        """Surrounding whitespace is removed from the reported management IP."""
        device = make_device("ip-mgmt-padded", librenms_cf={SERVER_KEY: {"id": 4212}})
        _serve_device_info(live_librenms, 4212, {"ip": "  198.18.47.10  "})
        view = self._view(live_librenms)

        assert view.get_management_ip(device) == "198.18.47.10"

    def test_blank_management_ip_is_reported_as_none(self, live_librenms):
        """A whitespace-only management IP is reported as no management IP."""
        device = make_device("ip-mgmt-blank", librenms_cf={SERVER_KEY: {"id": 4213}})
        _serve_device_info(live_librenms, 4213, {"ip": "   "})
        view = self._view(live_librenms)

        assert view.get_management_ip(device) is None


@pytest.mark.django_db
class TestIPTabRedirect:
    """The IP tab redirect carries whichever server key the request could resolve."""

    def test_bound_client_key_is_used_when_no_post_key_was_resolved(self, live_librenms):
        """With no POST-resolved key the redirect falls back to the bound client's server key."""
        device = make_device("ip-tab-bound-key")
        view = make_view(SyncIPAddressesView, librenms_api=live_librenms.api)

        url = view.get_ip_tab_url(device)

        assert url.endswith(f"?tab=ipaddresses&server_key={live_librenms.api.server_key}")

    def test_redirect_degrades_when_no_server_is_configured(self, client, settings):
        """An unresolvable server and an unusable default still redirect instead of raising."""
        configure_no_librenms_servers(settings)
        device = make_device("ip-tab-no-server")
        _login(client, "ip-tab-no-server-user")

        response = client.post(
            _ip_url(device),
            {"server_key": "removed", "select": "198.18.48.10/24"},
        )

        assert response.status_code == 302
        assert response["Location"].endswith("?tab=ipaddresses")
        assert _messages(response, "error") == ["Selected LibreNMS server is no longer configured."]

    def test_get_object_rejects_an_unknown_object_type(self):
        """``get_object`` fails closed on an object type it cannot map to a model."""
        from django.http import Http404

        device = make_device("ip-object-type")
        view = make_view(SyncIPAddressesView, librenms_api=False)

        with pytest.raises(Http404):
            view.get_object("switch", device.pk)
