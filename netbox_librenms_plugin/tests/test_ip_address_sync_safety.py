"""End-to-end safety tests for LibreNMS IP address synchronization."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, BrokenBarrierError
from unittest.mock import patch

import pytest
from django.apps import apps
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from ipam.models import IPAddress, VRF
from requests import Response

from netbox_librenms_plugin.constants import INTERFACE_NAME_FIELDS
from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
)
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms, make_view


def _json_response(url, payload, status=200):
    """Return a real requests response carrying a JSON payload."""
    response = Response()
    response.status_code = status
    response.url = url
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    return response


def _librenms_ip_response(address, prefix_length, *, device_name="ip-prefix-device", port_id=7001):
    """Return an HTTP dispatcher for one LibreNMS device IP row."""
    return _librenms_ip_rows_response(
        [{"address": address, "prefix_length": prefix_length, "port_id": port_id, "interface": "Ethernet1"}],
        device_name=device_name,
    )


def _librenms_ip_rows_response(rows, *, device_name):
    """Return an HTTP dispatcher for a complete LibreNMS device IP snapshot."""
    rows_by_port = {}
    for row in rows:
        rows_by_port.setdefault(str(row["port_id"]), row)

    def _get(url, **_kwargs):
        if url.endswith(f"/api/v0/devices/{device_name}"):
            return _json_response(
                url,
                {"status": "ok", "devices": [{"device_id": 42, "hostname": device_name}]},
            )
        if url.endswith("/api/v0/devices/42/ip"):
            return _json_response(
                url,
                {
                    "status": "ok",
                    "addresses": [
                        {
                            "port_id": row["port_id"],
                            "ip_address": row["address"],
                            "prefix_length": row["prefix_length"],
                        }
                        for row in rows
                    ],
                },
            )
        if "/api/v0/ports/" in url:
            row = rows_by_port.get(url.rsplit("/", 1)[-1])
            if row is None:
                raise AssertionError(f"Unexpected LibreNMS port request: {url}")
            port = {
                "port_id": row["port_id"],
                "ifName": row["interface"],
                "ifDescr": row["interface"],
            }
            port.update(row.get("port_fields", {}))
            return _json_response(
                url,
                {
                    "status": "ok",
                    "port": [port],
                },
            )
        if url.endswith("/api/v0/devices/42"):
            return _json_response(
                url,
                {"status": "ok", "devices": [{"device_id": 42, "ip": "198.18.0.254"}]},
            )
        raise AssertionError(f"Unexpected LibreNMS request: {url}")

    return _get


def _hidden_form_inputs(html):
    """Return a rendered form's hidden inputs by name, as a browser would submit them.

    Repeated names collapse, so use this only where one value per name is expected.
    """
    import re

    inputs = {}
    for tag in re.findall(r"<input\b[^>]*>", html):
        attributes = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
        if attributes.get("type") == "hidden" and "name" in attributes:
            inputs[attributes["name"]] = attributes.get("value", "")
    return inputs


def _configure_test_server(settings):
    """Configure one deterministic LibreNMS server without discarding other plugin settings."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "default": {"librenms_url": "https://librenms.example.com", "api_token": "test-token"}
    }
    settings.PLUGINS_CONFIG = plugin_config


def _message_texts(response):
    """Return messages emitted by one real client request."""
    return [str(message) for message in get_messages(response.wsgi_request)]


def _refresh_ip_snapshot(client, device, address, prefix_length):
    """Refresh one IP row through the real view and cache pipeline."""
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=_librenms_ip_response(address, prefix_length, device_name=device.name),
    ):
        return client.post(
            refresh_url,
            {"server_key": "default", "interface_name_field": "ifName"},
            HTTP_HX_REQUEST="true",
        )


class TestManagementIpLiveLookup:
    """get_management_ip over a real HTTP LibreNMS, both outcomes its producer can return."""

    @pytest.mark.django_db
    def test_management_ip_reads_the_live_device_endpoint_and_degrades_on_a_fault(self, settings, librenms_server):
        """A 200 yields the management IP; a faulting endpoint yields None so the sync is never blocked."""
        from netbox_librenms_plugin.tests.conftest import configure_librenms_servers
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        configure_librenms_servers(
            settings,
            {"default": {"librenms_url": librenms_server.url, "api_token": "token", "verify_ssl": False}},
        )
        device = make_device("mgmt-ip-live-lookup")
        set_librenms_device_id(device, 9903, "default")
        device.save()
        # A real instance: __init__ binds _librenms_api, and the lazy property builds the
        # configured client from it.
        view = SyncIPAddressesView()

        librenms_server.register(
            "/api/v0/devices/9903",
            {"status": "ok", "devices": [{"device_id": 9903, "ip": "  198.18.7.9  "}]},
        )
        try:
            assert view.get_management_ip(device) == "198.18.7.9"

            # A faulting endpoint is a real (False, None) lookup: no management IP, no raise.
            librenms_server.register("/api/v0/devices/9903", {"status": "error"}, status=500)
            assert view.get_management_ip(device) is None
        finally:
            cache.delete("librenms_device_info_default_9903")


class _IPHostLookupBarrier:
    """Pause concurrent requests after their first destination-host lookup."""

    def __init__(self, barrier):
        self.barrier = barrier
        self.lookup_seen = False

    def __call__(self, execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if (
            not self.lookup_seen
            and sql.lstrip().upper().startswith("SELECT")
            and 'FROM "ipam_ipaddress"' in sql
            and "HOST(" in sql.upper()
        ):
            self.lookup_seen = True
            try:
                self.barrier.wait(timeout=0.25)
            except BrokenBarrierError:
                # The other request waits for the advisory lock. This short pause
                # keeps both requests active without consuming the lock budget.
                pass
        return result


class _FirstHostAdvisoryBarrier:
    """Rendezvous concurrent bulk syncs before each request's first host lock."""

    def __init__(self, barrier):
        self.barrier = barrier
        self.lock_seen = False

    def __call__(self, execute, sql, params, many, context):
        if not self.lock_seen and "pg_advisory_xact_lock" in sql:
            self.lock_seen = True
            try:
                self.barrier.wait(timeout=1)
            except BrokenBarrierError:
                pass
        return execute(sql, params, many, context)


def _sync_cached_ip(device_pk, user_pk, row_id, lookup_wrapper):
    """Submit one cached IP row through the public sync endpoint."""
    from django.contrib.auth import get_user_model

    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '30s'")
            cursor.execute("SET statement_timeout = '45s'")
        thread_client = Client()
        thread_client.force_login(get_user_model().objects.get(pk=user_pk))
        url = reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": "device", "pk": device_pk},
        )
        with connection.execute_wrapper(lookup_wrapper):
            response = thread_client.post(
                url,
                {
                    "server_key": "default",
                    "select": row_id,
                    f"vrf_{row_id}": "",
                },
            )
        return response.status_code
    finally:
        connection.close()


def _sync_cached_ips(device_pk, user_pk, row_ids, lock_wrapper):
    """Submit one bulk cached-IP request through the public sync endpoint."""
    from django.contrib.auth import get_user_model

    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '30s'")
            cursor.execute("SET statement_timeout = '45s'")
        thread_client = Client()
        thread_client.force_login(get_user_model().objects.get(pk=user_pk))
        url = reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": "device", "pk": device_pk},
        )
        data = {"server_key": "default", "select": row_ids}
        data.update({f"vrf_{row_id}": "" for row_id in row_ids})
        with connection.execute_wrapper(lock_wrapper):
            response = thread_client.post(url, data)
        return response.status_code, _message_texts(response)
    finally:
        connection.close()


@pytest.mark.django_db(
    transaction=True,
    available_apps=[app.name for app in apps.get_app_configs()],
)
def test_concurrent_global_ip_sync_creates_one_address(settings):
    """Two requests for one global host must not create duplicate IP rows."""
    from netbox_librenms_plugin.utils import set_librenms_device_id

    _configure_test_server(settings)
    user = make_superuser("global-ip-concurrency-user")
    row_id = "198.18.20.10/24"
    devices = [
        make_device("global-ip-device-a", librenms_cf={"default": {"id": 42}}),
        make_device("global-ip-device-b", librenms_cf={"default": {"id": 43}}),
    ]
    for port_id, device in enumerate(devices, start=7040):
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        set_librenms_device_id(interface, port_id, "default")
        interface.save(update_fields=["custom_field_data"])
        cache.set(
            f"librenms_ip_addresses_device_{device.pk}_default",
            {
                "ip_addresses": [
                    {
                        "ip_address": "198.18.20.10",
                        "prefix_length": 24,
                        "ip_with_mask": row_id,
                        "port_id": port_id,
                        "interface_name": "Ethernet1",
                    }
                ],
                "mgmt_ip": "",
                "ports_by_id": {},
                "interface_name_field": "ifName",
            },
            timeout=300,
        )

    lookup_barrier = Barrier(2)
    lookup_wrappers = [_IPHostLookupBarrier(lookup_barrier) for _device in devices]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_sync_cached_ip, device.pk, user.pk, row_id, wrapper)
            for device, wrapper in zip(devices, lookup_wrappers, strict=True)
        ]
        statuses = [future.result(timeout=60) for future in futures]

    assert sorted(statuses) == [200, 302]
    assert all(wrapper.lookup_seen for wrapper in lookup_wrappers)
    assert IPAddress.objects.filter(address__net_host="198.18.20.10", vrf=None).count() == 1


@pytest.mark.django_db(
    transaction=True,
    available_apps=[app.name for app in apps.get_app_configs()],
)
def test_concurrent_bulk_ip_sync_orders_host_locks_before_interface_scope(settings):
    """Opposite bulk orders must not deadlock on host and Device locks."""
    from netbox_librenms_plugin.utils import set_librenms_device_id

    _configure_test_server(settings)
    user = make_superuser("bulk-ip-lock-order-user")
    device = make_device("bulk-ip-lock-order-device", librenms_cf={"default": {"id": 42}})
    interfaces = [
        make_interface(device, "Ethernet1", iface_type="1000base-t"),
        make_interface(device, "Ethernet2", iface_type="1000base-t"),
    ]
    for port_id, interface in zip((7041, 7042), interfaces, strict=True):
        set_librenms_device_id(interface, port_id, "default")
        interface.save(update_fields=["custom_field_data"])

    rows = [
        {
            "ip_address": "198.18.22.10",
            "prefix_length": 24,
            "ip_with_mask": "198.18.22.10/24",
            "port_id": 7041,
            "interface_name": "Ethernet1",
        },
        {
            "ip_address": "198.18.22.11",
            "prefix_length": 24,
            "ip_with_mask": "198.18.22.11/24",
            "port_id": 7042,
            "interface_name": "Ethernet2",
        },
    ]
    cache.set(
        f"librenms_ip_addresses_device_{device.pk}_default",
        {
            "ip_addresses": rows,
            "mgmt_ip": "",
            "ports_by_id": {},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )

    barrier = Barrier(2)
    wrappers = [_FirstHostAdvisoryBarrier(barrier) for _ in range(2)]
    row_ids = [row["ip_with_mask"] for row in rows]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_sync_cached_ips, device.pk, user.pk, selected, wrapper)
            for selected, wrapper in zip((row_ids, list(reversed(row_ids))), wrappers, strict=True)
        ]
        outcomes = [future.result(timeout=60) for future in futures]

    assert [status for status, _messages in outcomes] == [302, 302]
    assert all(not any("Failed to sync" in message for message in messages) for _, messages in outcomes)
    assert all(wrapper.lock_seen for wrapper in wrappers)
    assert IPAddress.objects.filter(address__in=row_ids, vrf=None).count() == 2


@pytest.mark.django_db
@pytest.mark.parametrize(
    "address",
    ["198.18.8.10/24", "2001:db8:8::10/64"],
)
def test_device_import_host_match_accepts_prefixed_management_addresses(address):
    """Device import must compare the canonical host while retaining VRF-independent matching."""
    from netbox_librenms_plugin.import_utils.device_operations import resolve_device_by_host_ip

    family = "ipv6" if ":" in address else "ipv4"
    device = make_device(f"prefixed-import-{family}")
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    IPAddress.objects.create(address=address, assigned_object=interface)

    matched, ambiguous, _rows = resolve_device_by_host_ip(address)

    assert matched == device
    assert ambiguous is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    "address",
    ["198.18.9.10/24", "2001:db8:9::10/64"],
)
def test_oob_permission_preflight_matches_prefixed_addresses(address):
    """The OOB preflight and writer must resolve the same canonical host in the global table."""
    from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

    family = "ipv6" if ":" in address else "ipv4"
    device = make_device(f"prefixed-oob-{family}")
    interface = make_interface(device, "Management1", iface_type="1000base-t")
    IPAddress.objects.create(address=address, assigned_object=interface)
    user = make_user_with_perms(f"prefixed-oob-user-{family}", [])
    request = make_request("post", {"oob_interface_id": str(interface.pk)}, user=user)

    assert AddAsOOBView._missing_oob_ip_permissions(request, address, device=device) is None


@pytest.mark.parametrize(
    "address",
    ["198.18.10.10/24", "2001:db8:10::10/64"],
)
def test_device_name_preserves_prefixed_ip_literals(address):
    """Domain stripping must not truncate a prefixed IP literal as if it were a hostname."""
    from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

    assert _determine_device_name({"hostname": address}, strip_domain=True) == address


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("device_name", "librenms_address", "prefix_length", "expected_address"),
    [
        ("ip-prefix-v4", "198.18.0.10/24", 24, "198.18.0.10/24"),
        ("ip-prefix-v6", "2001:db8:1::10/64", 64, "2001:db8:1::10/64"),
    ],
)
def test_refresh_and_sync_accepts_an_already_prefixed_address(
    client, settings, device_name, librenms_address, prefix_length, expected_address
):
    """The refresh must not append a second prefix to an already-prefixed address."""
    _configure_test_server(settings)

    device = make_device(device_name, librenms_cf={"default": {"id": 42}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    interface.custom_field_data["librenms_id"] = {"default": 7001}
    interface.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("ip-prefix-user"))

    refresh_response = _refresh_ip_snapshot(client, device, librenms_address, prefix_length)

    assert refresh_response.status_code == 200
    cached = cache.get(f"librenms_ip_addresses_device_{device.pk}_default")
    assert cached is not None, refresh_response.content.decode()
    assert cached["ip_addresses"][0]["ip_with_mask"] == expected_address

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    sync_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": expected_address,
            f"vrf_{expected_address}": "",
        },
    )

    assert sync_response.status_code == 302
    synced = IPAddress.objects.get(address=expected_address, vrf=None)
    assert synced.assigned_object == interface


@pytest.mark.django_db
def test_refresh_rejects_conflicting_embedded_and_separate_prefixes(client, settings):
    """Contradictory LibreNMS prefix evidence must not produce a syncable cache entry."""
    _configure_test_server(settings)
    device = make_device("ip-prefix-conflict")
    make_interface(device, "Ethernet1", iface_type="1000base-t")
    client.force_login(make_superuser("ip-prefix-conflict-user"))

    refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=_librenms_ip_response(
            "198.18.1.10/25",
            24,
            device_name=device.name,
        ),
    ):
        response = client.post(
            refresh_url,
            {"server_key": "default", "interface_name_field": "ifName"},
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 200
    assert b"Failed to fetch IP addresses from LibreNMS" in response.content
    assert cache.get(f"librenms_ip_addresses_device_{device.pk}_default") is None
    assert not IPAddress.objects.filter(address="198.18.1.10/25").exists()


@pytest.mark.django_db
def test_sync_requires_confirmation_before_reassigning_an_ip_in_the_same_vrf(client, settings):
    """A selected row must not silently take an IP from another interface."""
    _configure_test_server(settings)

    device = make_device("ip-reassignment-conflict", librenms_cf={"default": {"id": 42}})
    target_interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    target_interface.custom_field_data["librenms_id"] = {"default": 7001}
    target_interface.save(update_fields=["custom_field_data"])
    current_interface = make_interface(device, "Ethernet2", iface_type="1000base-t")
    existing = IPAddress.objects.create(
        address="198.18.2.10/24",
        assigned_object=current_interface,
        status="active",
    )
    client.force_login(make_superuser("ip-reassignment-conflict-user"))

    refresh_response = _refresh_ip_snapshot(client, device, "198.18.2.10", 24)

    assert refresh_response.status_code == 200

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    sync_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": "198.18.2.10/24",
            "vrf_198.18.2.10/24": "",
        },
        HTTP_HX_REQUEST="true",
    )

    assert sync_response.status_code == 200
    assert b"Confirm IP address changes" in sync_response.content
    existing.refresh_from_db()
    assert existing.assigned_object == current_interface
    assert existing.assigned_object != target_interface


@pytest.mark.django_db
def test_native_ip_conflict_response_renders_a_complete_page(client, settings):
    """A native form submission must not replace the browser document with an HTMX fragment."""
    _configure_test_server(settings)
    device = make_device("native-ip-conflict", librenms_cf={"default": {"id": 42}})
    target_interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    target_interface.custom_field_data["librenms_id"] = {"default": 7001}
    target_interface.save(update_fields=["custom_field_data"])
    existing_interface = make_interface(device, "Ethernet2", iface_type="1000base-t")
    existing = IPAddress.objects.create(address="198.18.2.20/24", assigned_object=existing_interface)
    client.force_login(make_superuser("native-ip-conflict-user"))
    assert _refresh_ip_snapshot(client, device, "198.18.2.20", 24).status_code == 200

    response = client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": "device", "pk": device.pk},
        ),
        {
            "server_key": "default",
            "select": "198.18.2.20/24",
            "vrf_198.18.2.20/24": "",
        },
    )

    assert response.status_code == 200
    assert any(
        template.name == "netbox_librenms_plugin/ip_address_conflicts_page.html" for template in response.templates
    )
    assert b"Confirm IP address changes" in response.content
    existing.refresh_from_db()
    assert existing.assigned_object == existing_interface


@pytest.mark.django_db
def test_bulk_sync_applies_safe_rows_and_forces_only_selected_conflicts(client, settings):
    """A bulk request must apply safe rows and retain unselected conflicts unchanged."""
    _configure_test_server(settings)
    device = make_device("ip-bulk-conflicts", librenms_cf={"default": {"id": 42}})
    target_one = make_interface(device, "Ethernet1", iface_type="1000base-t")
    target_one.custom_field_data["librenms_id"] = {"default": 7001}
    target_one.save(update_fields=["custom_field_data"])
    target_two = make_interface(device, "Ethernet2", iface_type="1000base-t")
    target_two.custom_field_data["librenms_id"] = {"default": 7002}
    target_two.save(update_fields=["custom_field_data"])
    target_three = make_interface(device, "Ethernet3", iface_type="1000base-t")
    target_three.custom_field_data["librenms_id"] = {"default": 7003}
    target_three.save(update_fields=["custom_field_data"])
    current_one = make_interface(device, "Ethernet4", iface_type="1000base-t")
    current_two = make_interface(device, "Ethernet5", iface_type="1000base-t")
    existing_one = IPAddress.objects.create(address="198.18.5.10/24", assigned_object=current_one)
    existing_two = IPAddress.objects.create(address="198.18.5.11/24", assigned_object=current_two)
    rows = [
        {"address": "198.18.5.10", "prefix_length": 24, "port_id": 7001, "interface": "Ethernet1"},
        {"address": "198.18.5.11", "prefix_length": 24, "port_id": 7002, "interface": "Ethernet2"},
        {"address": "198.18.5.12", "prefix_length": 24, "port_id": 7003, "interface": "Ethernet3"},
    ]
    client.force_login(make_superuser("ip-bulk-conflicts-user"))
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=_librenms_ip_rows_response(rows, device_name=device.name),
    ):
        refresh_response = client.post(
            refresh_url,
            {"server_key": "default", "interface_name_field": "ifName"},
            HTTP_HX_REQUEST="true",
        )
    assert refresh_response.status_code == 200

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    conflict_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": ["198.18.5.10/24", "198.18.5.11/24", "198.18.5.12/24"],
            "vrf_198.18.5.10/24": "",
            "vrf_198.18.5.11/24": "",
            "vrf_198.18.5.12/24": "",
        },
        HTTP_HX_REQUEST="true",
    )

    assert conflict_response.status_code == 200
    conflicts = conflict_response.context["conflicts"]
    assert [conflict["row_id"] for conflict in conflicts] == ["198.18.5.10/24", "198.18.5.11/24"]
    safe = IPAddress.objects.get(address="198.18.5.12/24", vrf=None)
    assert safe.assigned_object == target_three
    existing_one.refresh_from_db()
    existing_two.refresh_from_db()
    assert existing_one.assigned_object == current_one
    assert existing_two.assigned_object == current_two

    force_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "force_conflict": conflicts[0]["row_id"],
            "conflict_intent": [conflict["intent"] for conflict in conflicts],
        },
        HTTP_HX_REQUEST="true",
    )

    assert force_response.status_code == 200
    existing_one.refresh_from_db()
    existing_two.refresh_from_db()
    assert existing_one.assigned_object == target_one
    assert existing_two.assigned_object == current_two


@pytest.mark.django_db
def test_confirmation_replays_create_missing_when_the_target_name_turns_ambiguous(client, settings):
    """The confirmation form must replay the create-missing choice, or a newly ambiguous name skips the row."""
    from dcim.models import Interface

    _configure_test_server(settings)
    _chassis, members = make_virtual_chassis_members("ip-conflict-vc", count=3)
    page_device, target_device, sibling = members
    page_device.custom_field_data["librenms_id"] = {"default": {"id": 42}}
    page_device.save(update_fields=["custom_field_data"])
    # The signed target: an unbound sibling interface, unambiguous by name while the intent is built.
    target = make_interface(target_device, "Ethernet2/1", iface_type="1000base-t")
    holder = make_interface(page_device, "Ethernet1/9", iface_type="1000base-t")
    existing = IPAddress.objects.create(address="198.18.31.10/24", assigned_object=holder)
    rows = [
        {
            "address": "198.18.31.10",
            "prefix_length": 24,
            "port_id": 7101,
            "interface": "Ethernet2/1",
            "port_fields": {"ifType": "ethernetCsmacd"},
        }
    ]
    client.force_login(make_superuser("ip-conflict-vc-user"))
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[page_device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=_librenms_ip_rows_response(rows, device_name=page_device.name),
    ):
        assert (
            client.post(
                refresh_url,
                {"server_key": "default", "interface_name_field": "ifName"},
                HTTP_HX_REQUEST="true",
            ).status_code
            == 200
        )

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": page_device.pk},
    )
    conflict_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "create-missing-interfaces-toggle": "on",
            "select": "198.18.31.10/24",
            "vrf_198.18.31.10/24": "",
        },
        HTTP_HX_REQUEST="true",
    )
    assert conflict_response.status_code == 200
    conflicts = conflict_response.context["conflicts"]
    assert [conflict["row_id"] for conflict in conflicts] == ["198.18.31.10/24"]

    # A second member gains the same interface name, so the name map turns ambiguous and
    # _match_interface alone can no longer resolve the interface the intent already signed.
    make_interface(sibling, "Ethernet2/1", iface_type="1000base-t")

    # Submit exactly what the rendered form carries, the way a browser would.
    hidden = _hidden_form_inputs(conflict_response.content.decode())
    force_response = client.post(
        sync_url,
        {**hidden, "force_conflict": conflicts[0]["row_id"]},
        HTTP_HX_REQUEST="true",
    )

    assert force_response.status_code == 200
    # resolve_or_create_interface_from_port recovers the signed interface itself, so its PK still
    # satisfies the intent and the confirmed move lands on it.
    existing.refresh_from_db()
    assert existing.assigned_object == target
    assert Interface.objects.filter(device=target_device, name="Ethernet2/1").count() == 1


@pytest.mark.django_db
def test_row_action_syncs_only_its_ip_when_another_row_is_checked(client, settings):
    """A row action must not submit unrelated bulk selections from the outer form."""
    _configure_test_server(settings)
    device = make_device("ip-row-action", librenms_cf={"default": {"id": 42}})
    first_interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    second_interface = make_interface(device, "Ethernet2", iface_type="1000base-t")
    first_interface.custom_field_data["librenms_id"] = {"default": 7030}
    second_interface.custom_field_data["librenms_id"] = {"default": 7031}
    first_interface.save(update_fields=["custom_field_data"])
    second_interface.save(update_fields=["custom_field_data"])
    rows = [
        {
            "address": "198.18.30.10",
            "prefix_length": 24,
            "port_id": 7030,
            "interface": "Ethernet1",
        },
        {
            "address": "198.18.31.10",
            "prefix_length": 24,
            "port_id": 7031,
            "interface": "Ethernet2",
        },
    ]
    client.force_login(make_superuser("ip-row-action-user"))
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=_librenms_ip_rows_response(rows, device_name=device.name),
    ):
        refresh_response = client.post(
            refresh_url,
            {"server_key": "default", "interface_name_field": "ifName"},
            HTTP_HX_REQUEST="true",
        )

    assert refresh_response.status_code == 200
    rendered = refresh_response.content.decode()
    assert 'name="sync_one" value="198.18.31.10/24"' in rendered
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": "198.18.30.10/24",
            "sync_one": "198.18.31.10/24",
            "vrf_198.18.30.10/24": "",
            "vrf_198.18.31.10/24": "",
        },
    )

    assert response.status_code == 302
    assert not IPAddress.objects.filter(address="198.18.30.10/24").exists()
    assert IPAddress.objects.get(address="198.18.31.10/24").assigned_object == second_interface


@pytest.mark.django_db
def test_create_missing_interfaces_materializes_one_interface_for_bulk_ip_rows(client, settings):
    """Bulk IP sync must create one shared termination for rows on the same missing port."""
    from dcim.models import Interface

    from netbox_librenms_plugin.utils import get_librenms_device_id

    _configure_test_server(settings)
    device = make_device("ip-create-missing-interface", librenms_cf={"default": {"id": 42}})
    blue = VRF.objects.create(name="Create Missing Blue")
    rows = [
        {
            "address": "198.18.12.10",
            "prefix_length": 24,
            "port_id": 7001,
            "interface": "Ethernet1",
            "port_fields": {
                "ifType": "ethernetCsmacd",
                "ifSpeed": 1_000_000_000,
                "ifAlias": "Server uplink",
                "ifMtu": 1500,
                "ifAdminStatus": "up",
            },
        },
        {
            "address": "2001:db8:12::10",
            "prefix_length": 64,
            "port_id": 7001,
            "interface": "Ethernet1",
        },
    ]
    client.force_login(make_superuser("ip-create-missing-interface-user"))
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=_librenms_ip_rows_response(rows, device_name=device.name),
    ):
        refresh_response = client.post(
            refresh_url,
            {"server_key": "default", "interface_name_field": "ifName"},
            HTTP_HX_REQUEST="true",
        )
    assert refresh_response.status_code == 200
    assert 'name="create-missing-interfaces-toggle"' in refresh_response.content.decode()
    assert not Interface.objects.filter(device=device, name="Ethernet1").exists()

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    response = client.post(
        sync_url,
        {
            "server_key": "default",
            "create-missing-interfaces-toggle": "on",
            "select": ["198.18.12.10/24", "2001:db8:12::10/64"],
            "vrf_198.18.12.10/24": str(blue.pk),
            "vrf_2001:db8:12::10/64": "",
        },
    )

    assert response.status_code == 302
    interface = Interface.objects.get(device=device, name="Ethernet1")
    assert Interface.objects.filter(device=device, name="Ethernet1").count() == 1
    assert get_librenms_device_id(interface, "default", auto_save=False) == 7001
    assert interface.description == "Server uplink"
    assert interface.mtu == 1500
    assert interface.enabled is True
    assert {
        (str(ip.address), ip.assigned_object_id) for ip in IPAddress.objects.filter(assigned_object_id=interface.pk)
    } >= {
        ("198.18.12.10/24", interface.pk),
        ("2001:db8:12::10/64", interface.pk),
    }
    assert IPAddress.objects.get(address="198.18.12.10/24", vrf=blue).assigned_object == interface


@pytest.mark.django_db
def test_create_missing_interfaces_rejects_legacy_snapshot_before_processing_rows(client, settings):
    """Create-missing must reject a legacy snapshot once, before processing its rows."""
    from dcim.models import Interface

    _configure_test_server(settings)
    device = make_device("ip-create-missing-legacy", librenms_cf={"default": {"id": 42}})
    row_id = "198.18.12.20/24"
    cache.set(
        f"librenms_ip_addresses_device_{device.pk}_default",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.12.20",
                    "prefix_length": 24,
                    "ip_with_mask": row_id,
                    "port_id": 7020,
                    "interface_name": "Ethernet20",
                }
            ]
        },
        timeout=300,
    )
    client.force_login(make_superuser("ip-create-missing-legacy-user"))

    response = client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": "device", "pk": device.pk},
        ),
        {
            "server_key": "default",
            "create-missing-interfaces-toggle": "on",
            "select": row_id,
            f"vrf_{row_id}": "",
        },
    )

    assert response.status_code == 302
    assert _message_texts(response) == ["Cache has expired. Please refresh the IP data."]
    assert not Interface.objects.filter(device=device).exists()
    assert not IPAddress.objects.filter(address=row_id).exists()


@pytest.mark.django_db
def test_create_missing_interfaces_reuses_interface_catalog_for_bulk_rows(client, settings):
    """Bulk create-missing should materialize different ports with one catalog scan."""
    from dcim.models import Interface

    _configure_test_server(settings)
    device = make_device("ip-create-missing-catalog", librenms_cf={"default": {"id": 42}})
    rows = [
        {
            "address": "198.18.12.21",
            "prefix_length": 24,
            "port_id": 7021,
            "interface": "Ethernet21",
            "port_fields": {"ifType": "ethernetCsmacd"},
        },
        {
            "address": "198.18.12.22",
            "prefix_length": 24,
            "port_id": 7022,
            "interface": "Ethernet22",
            "port_fields": {"ifType": "ethernetCsmacd"},
        },
    ]
    client.force_login(make_superuser("ip-create-missing-catalog-user"))
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=_librenms_ip_rows_response(rows, device_name=device.name),
    ):
        assert (
            client.post(
                refresh_url,
                {"server_key": "default", "interface_name_field": "ifName"},
                HTTP_HX_REQUEST="true",
            ).status_code
            == 200
        )

    catalog_reads = 0

    class CatalogReadCounter:
        """Count the full device-interface catalog reads used by create-missing."""

        def __call__(self, execute, sql, params, many, context):
            nonlocal catalog_reads
            normalized = " ".join(sql.split())
            if 'FROM "dcim_interface"' in normalized and '"dcim_interface"."device_id" IN' in normalized:
                catalog_reads += 1
            return execute(sql, params, many, context)

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    with connection.execute_wrapper(CatalogReadCounter()):
        response = client.post(
            sync_url,
            {
                "server_key": "default",
                "create-missing-interfaces-toggle": "on",
                "select": ["198.18.12.21/24", "198.18.12.22/24"],
                "vrf_198.18.12.21/24": "",
                "vrf_198.18.12.22/24": "",
            },
        )

    assert response.status_code == 302
    # Upper bound, not an exact count: the counter matches generated SQL text, and a NetBox
    # release that adds a select_related or prefetch would change it without changing behaviour.
    # The point of the assertion is that the catalog is reused, not rebuilt per row.
    assert catalog_reads <= 2  # initial matching map plus one reusable creation catalog
    assert {interface.name for interface in Interface.objects.filter(device=device)} == {"Ethernet21", "Ethernet22"}
    assert IPAddress.objects.get(address="198.18.12.21/24").assigned_object.name == "Ethernet21"
    assert IPAddress.objects.get(address="198.18.12.22/24").assigned_object.name == "Ethernet22"


@pytest.mark.django_db
def test_failed_create_missing_row_does_not_leak_interface_catalog(settings):
    """A failed row must not leave a rolled-back interface in the request catalog."""
    from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

    _configure_test_server(settings)
    device = make_device("ip-create-missing-savepoint", librenms_cf={"default": {"id": 42}})
    first_row = "198.18.12.23/24"
    second_row = "198.18.12.24/24"
    cached_ips = [
        {
            "ip_address": "198.18.12.23",
            "prefix_length": 24,
            "ip_with_mask": first_row,
            "port_id": 7023,
            "interface_name": "Ethernet23",
        },
        {
            "ip_address": "198.18.12.24",
            "prefix_length": 24,
            "ip_with_mask": second_row,
            "port_id": 7023,
            "interface_name": "Ethernet23",
        },
    ]
    cached_ports = {
        "7023": {
            "port_id": 7023,
            "ifName": "Ethernet23",
            "ifDescr": "Ethernet23",
            "ifType": "ethernetCsmacd",
        },
    }
    request = make_request(
        "post",
        {
            "create-missing-interfaces-toggle": "on",
            "select": [first_row, second_row],
            f"vrf_{first_row}": "",
            f"vrf_{second_row}": "",
        },
    )
    view = make_view(SyncIPAddressesView, request)
    view._post_server_key = "default"

    results = view.process_ip_sync(
        request,
        [first_row, second_row],
        cached_ips,
        device,
        "device",
        force_intents={
            first_row: {
                "target_interface": {"model": "dcim.interface", "pk": "999999"},
                "target_vrf_id": None,
                "ip_pk": "999999",
                "ip_state": {},
                "kind": "reassign",
            }
        },
        cached_ports_by_id=cached_ports,
        interface_name_field="ifName",
    )

    from dcim.models import Interface

    assert results["failed"] == [first_row]
    assert results["created"] == [second_row]
    assert Interface.objects.filter(device=device, name="Ethernet23").count() == 1
    second_interface = Interface.objects.get(device=device, name="Ethernet23")
    assert not IPAddress.objects.filter(address=first_row).exists()
    assert IPAddress.objects.get(address=second_row).assigned_object == second_interface


@pytest.mark.django_db
def test_create_missing_interface_resolves_the_virtual_chassis_member(client, settings):
    """IP sync must create a physical port on its unambiguous current chassis member."""
    from dcim.models import Interface

    _configure_test_server(settings)
    _chassis, members = make_virtual_chassis_members("ip-create-vc")
    page_device, target_device = members
    page_device.custom_field_data["librenms_id"] = {"default": {"id": 42}}
    page_device.save(update_fields=["custom_field_data"])
    rows = [
        {
            "address": "198.18.13.10",
            "prefix_length": 24,
            "port_id": 7002,
            "interface": "Ethernet2/1",
            "port_fields": {"ifType": "ethernetCsmacd"},
        }
    ]
    client.force_login(make_superuser("ip-create-vc-user"))
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[page_device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=_librenms_ip_rows_response(rows, device_name=page_device.name),
    ):
        assert (
            client.post(
                refresh_url,
                {"server_key": "default", "interface_name_field": "ifName"},
                HTTP_HX_REQUEST="true",
            ).status_code
            == 200
        )

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": page_device.pk},
    )
    response = client.post(
        sync_url,
        {
            "server_key": "default",
            "create-missing-interfaces-toggle": "on",
            "select": "198.18.13.10/24",
            "vrf_198.18.13.10/24": "",
        },
    )

    assert response.status_code == 302
    interface = Interface.objects.get(device=target_device, name="Ethernet2/1")
    assert not Interface.objects.filter(device=page_device, name="Ethernet2/1").exists()
    assert IPAddress.objects.get(address="198.18.13.10/24", vrf=None).assigned_object == interface


@pytest.mark.django_db
def test_create_missing_interface_supports_virtual_machine_ip_sync(client, settings):
    """The opt-in interface materializer must use VMInterface for a VM IP row."""
    from virtualization.models import VMInterface

    _configure_test_server(settings)
    virtual_machine = make_vm("ip-create-vm")
    virtual_machine.custom_field_data["librenms_id"] = {"default": {"id": 42}}
    virtual_machine.save(update_fields=["custom_field_data"])
    rows = [
        {
            "address": "2001:db8:14::10",
            "prefix_length": 64,
            "port_id": 7014,
            "interface": "eth0",
            "port_fields": {"ifType": "ethernetCsmacd", "ifMtu": 9000},
        }
    ]
    client.force_login(make_superuser("ip-create-vm-user"))
    refresh_url = reverse("plugins:netbox_librenms_plugin:vm_ipaddress_sync", args=[virtual_machine.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=_librenms_ip_rows_response(rows, device_name=virtual_machine.name),
    ):
        assert (
            client.post(
                refresh_url,
                {"server_key": "default", "interface_name_field": "ifName"},
                HTTP_HX_REQUEST="true",
            ).status_code
            == 200
        )

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "virtualmachine", "pk": virtual_machine.pk},
    )
    response = client.post(
        sync_url,
        {
            "server_key": "default",
            "create-missing-interfaces-toggle": "on",
            "select": "2001:db8:14::10/64",
            "vrf_2001:db8:14::10/64": "",
        },
    )

    assert response.status_code == 302
    interface = VMInterface.objects.get(virtual_machine=virtual_machine, name="eth0")
    assert interface.mtu == 9000
    assert IPAddress.objects.get(address="2001:db8:14::10/64", vrf=None).assigned_object == interface


@pytest.mark.django_db
def test_create_missing_interfaces_rejects_ambiguous_cached_port_names(client, settings):
    """Bulk IP sync must not merge distinct LibreNMS ports that share the selected name."""
    from dcim.models import Interface

    _configure_test_server(settings)
    device = make_device("ip-create-ambiguous", librenms_cf={"default": {"id": 42}})
    rows = [
        {
            "address": "198.18.15.10",
            "prefix_length": 24,
            "port_id": 7015,
            "interface": "duplicate-name",
            "port_fields": {"ifType": "ethernetCsmacd"},
        },
        {
            "address": "198.18.15.11",
            "prefix_length": 24,
            "port_id": 7016,
            "interface": "duplicate-name",
            "port_fields": {"ifType": "ethernetCsmacd"},
        },
    ]
    client.force_login(make_superuser("ip-create-ambiguous-user"))
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=_librenms_ip_rows_response(rows, device_name=device.name),
    ):
        assert (
            client.post(
                refresh_url,
                {"server_key": "default", "interface_name_field": "ifName"},
                HTTP_HX_REQUEST="true",
            ).status_code
            == 200
        )

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    response = client.post(
        sync_url,
        {
            "server_key": "default",
            "create-missing-interfaces-toggle": "on",
            "select": ["198.18.15.10/24", "198.18.15.11/24"],
            "vrf_198.18.15.10/24": "",
            "vrf_198.18.15.11/24": "",
        },
    )

    assert response.status_code == 302
    assert any("ambiguous" in message for message in _message_texts(response))
    assert not Interface.objects.filter(device=device).exists()
    assert not IPAddress.objects.filter(address="198.18.15.10/24").exists()
    assert not IPAddress.objects.filter(address="198.18.15.11/24").exists()


@pytest.mark.django_db
def test_interface_refresh_and_sync_preserve_the_ip_snapshot(client, settings):
    """Independent interface cache work must not expire a still-live IP snapshot."""
    _configure_test_server(settings)
    device = make_device("ip-cache-survives-interface-sync", librenms_cf={"default": {"id": 42}})
    rows = [
        {
            "address": "198.18.16.10",
            "prefix_length": 24,
            "port_id": 7016,
            "interface": "Ethernet1",
            "port_fields": {
                "ifType": "ethernetCsmacd",
                "ifAlias": "",
                "ifAdminStatus": "up",
            },
        }
    ]
    ip_dispatcher = _librenms_ip_rows_response(rows, device_name=device.name)

    def librenms_response(url, **kwargs):
        if url.endswith("/api/v0/devices/42/ports"):
            port = {
                "port_id": 7016,
                "ifName": "Ethernet1",
                "ifDescr": "Ethernet1",
                "ifType": "ethernetCsmacd",
                "ifAlias": "",
                "ifAdminStatus": "up",
            }
            return _json_response(url, {"status": "ok", "ports": [port]})
        return ip_dispatcher(url, **kwargs)

    client.force_login(make_superuser("ip-cache-survival-user"))
    ip_refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk])
    interface_refresh_url = reverse("plugins:netbox_librenms_plugin:device_interface_sync", args=[device.pk])
    ip_cache_key = f"librenms_ip_addresses_device_{device.pk}_default"
    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        assert (
            client.post(
                ip_refresh_url,
                {"server_key": "default", "interface_name_field": "ifName"},
                HTTP_HX_REQUEST="true",
            ).status_code
            == 200
        )
        assert cache.get(ip_cache_key) is not None
        assert (
            client.post(
                interface_refresh_url,
                {"server_key": "default", "interface_name_field": "ifName"},
                HTTP_HX_REQUEST="true",
            ).status_code
            == 200
        )
    assert cache.get(ip_cache_key) is not None

    interface_sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )
    assert (
        client.post(
            interface_sync_url,
            {
                "server_key": "default",
                "interface_name_field": "ifName",
                "select": "7016",
                "exclude_columns": "vlans",
            },
        ).status_code
        == 302
    )
    assert cache.get(ip_cache_key) is not None

    ip_sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    response = client.post(
        ip_sync_url,
        {
            "server_key": "default",
            "select": "198.18.16.10/24",
            "vrf_198.18.16.10/24": "",
        },
    )

    assert response.status_code == 302
    assert IPAddress.objects.get(address="198.18.16.10/24", vrf=None).assigned_object.name == "Ethernet1"


@pytest.mark.django_db
def test_expired_ip_snapshot_redirects_the_whole_htmx_page(client, settings):
    """An expired snapshot must replace the stale tab and countdown, not load a page in the modal."""
    _configure_test_server(settings)
    device = make_device("ip-expired-htmx", librenms_cf={"default": {"id": 42}})
    client.force_login(make_superuser("ip-expired-htmx-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )

    response = client.post(
        sync_url,
        {"server_key": "default", "select": "198.18.17.10/24"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    expected_url = (
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])
        + "?tab=ipaddresses&server_key=default"
    )
    assert response.headers["HX-Redirect"] == expected_url


@pytest.mark.django_db
def test_create_missing_interfaces_does_not_adopt_a_hidden_existing_interface(client, settings):
    """A hidden stable-ID match must block the opt-in writer without exposing or mutating it."""
    from dcim.models import Device, Interface

    _configure_test_server(settings)
    device = make_device("ip-hidden-interface", librenms_cf={"default": {"id": 42}})
    hidden_interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    hidden_interface.custom_field_data["librenms_id"] = {"default": 7018}
    hidden_interface.save(update_fields=["custom_field_data"])
    user = make_user_with_perms("ip-hidden-interface-user", [])
    user = grant(user, "view", Device, constraints={"pk": device.pk})
    user = grant(user, "view", Interface, constraints={"name": "another-interface"})
    user = grant(user, "add", Interface)
    user = grant(user, "change", Interface)
    user = grant(user, "add", IPAddress)
    user = grant(user, "change", IPAddress)
    client.force_login(user)
    cache.set(
        f"librenms_ip_addresses_device_{device.pk}_default",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.18.10",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.18.10/24",
                    "port_id": 7018,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {
                7018: {
                    "port_id": 7018,
                    "ifName": "Ethernet1",
                    "ifDescr": "Ethernet1",
                    "ifType": "ethernetCsmacd",
                }
            },
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )

    response = client.post(
        sync_url,
        {
            "server_key": "default",
            "create-missing-interfaces-toggle": "on",
            "select": "198.18.18.10/24",
            "vrf_198.18.18.10/24": "",
        },
    )

    assert response.status_code == 302
    assert any("outside your view scope" in message for message in _message_texts(response))
    hidden_interface.refresh_from_db()
    assert hidden_interface.name == "Ethernet1"
    assert not IPAddress.objects.filter(address="198.18.18.10/24").exists()


@pytest.mark.django_db
def test_direct_ip_sync_post_does_not_mutate_a_migrated_donor(client, settings):
    """The writer must enforce the migrated read-only state behind the hidden form."""
    from netbox_librenms_plugin.utils import mark_librenms_migrated

    _configure_test_server(settings)
    donor = make_device("ip-migrated-donor", librenms_cf={"default": {"id": 42}})
    winner = make_device("ip-migration-winner")
    interface = make_interface(donor, "Ethernet1", iface_type="1000base-t")
    interface.custom_field_data["librenms_id"] = {"default": 7019}
    interface.save(update_fields=["custom_field_data"])
    cache.set(
        f"librenms_ip_addresses_device_{donor.pk}_default",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.19.10",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.19.10/24",
                    "port_id": 7019,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {7019: {"port_id": 7019, "ifName": "Ethernet1", "ifDescr": "Ethernet1"}},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    mark_librenms_migrated(donor, winner.pk, "default")
    donor.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("ip-migrated-donor-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": donor.pk},
    )

    response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": "198.18.19.10/24",
            "vrf_198.18.19.10/24": "",
        },
    )

    assert response.status_code == 302
    assert any("read-only because it was migrated" in message for message in _message_texts(response))
    assert not IPAddress.objects.filter(address="198.18.19.10/24").exists()


@pytest.mark.django_db
def test_invalid_force_all_confirmation_reports_the_confirmation_error(client, settings):
    """An invalid force token must not be reported as an empty row selection."""
    _configure_test_server(settings)
    device = make_device("invalid-force-confirmation", librenms_cf={"default": {"id": 42}})
    cache.set(
        f"librenms_ip_addresses_device_{device.pk}_default",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.19.20",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.19.20/24",
                    "port_id": 7020,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    client.force_login(make_superuser("invalid-force-confirmation-user"))

    response = client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": "device", "pk": device.pk},
        ),
        {
            "server_key": "default",
            "force_all": "1",
            "conflict_intent": "invalid-token",
        },
    )

    assert response.status_code == 302
    message_texts = _message_texts(response)
    assert message_texts == ["IP address confirmation is invalid or has expired. Refresh the IP data and try again."]


@pytest.mark.django_db
def test_invalid_confirmation_is_not_reported_as_an_ip_address(client, settings):
    """A bad confirmation token must remain separate from per-address results."""
    from netbox_librenms_plugin.utils import set_librenms_device_id

    _configure_test_server(settings)
    device = make_device("mixed-invalid-confirmation", librenms_cf={"default": {"id": 42}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    set_librenms_device_id(interface, 7021, "default")
    interface.save(update_fields=["custom_field_data"])
    row_id = "198.18.19.21/24"
    cache.set(
        f"librenms_ip_addresses_device_{device.pk}_default",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.19.21",
                    "prefix_length": 24,
                    "ip_with_mask": row_id,
                    "port_id": 7021,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    client.force_login(make_superuser("mixed-invalid-confirmation-user"))

    response = client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": "device", "pk": device.pk},
        ),
        {
            "server_key": "default",
            "select": row_id,
            f"vrf_{row_id}": "",
            "conflict_intent": "invalid-token",
        },
    )

    assert response.status_code == 302
    assert _message_texts(response) == [
        f"Created IP addresses: {row_id}",
        "IP address confirmation is invalid or has expired. Refresh the IP data and try again.",
    ]


@pytest.mark.django_db
def test_interface_scope_change_during_lock_is_reported_as_a_failure(client, settings):
    """A matched interface that leaves view scope must not be reported as missing."""
    from dcim.models import Device, Interface
    from django.db import connection

    from netbox_librenms_plugin.utils import set_librenms_device_id

    _configure_test_server(settings)
    device = make_device("interface-lock-view-scope", librenms_cf={"default": {"id": 42}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    interface.description = "managed"
    set_librenms_device_id(interface, 7030, "default")
    interface.save()
    user = make_user_with_perms("interface-lock-view-scope-user", [])
    user = grant(user, "view", Device, constraints={"pk": device.pk})
    user = grant(user, "view", Interface, constraints={"description": "managed"})
    user = grant(user, "add", IPAddress)
    user = grant(user, "change", IPAddress)
    client.force_login(user)
    cache.set(
        f"librenms_ip_addresses_device_{device.pk}_default",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.19.30",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.19.30/24",
                    "port_id": 7030,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {
                7030: {
                    "port_id": 7030,
                    "ifName": "Ethernet1",
                    "ifDescr": "Ethernet1",
                    "ifType": "ethernetCsmacd",
                }
            },
            "interface_name_field": "ifName",
        },
        timeout=300,
    )

    class RestrictInterfaceBeforeLock:
        """Move the matched interface outside the grant before its locking read."""

        def __init__(self):
            self.fired = False

        def __call__(self, execute, sql, params, many, context):
            if not self.fired and 'FROM "dcim_interface"' in sql and "FOR UPDATE" in sql.upper():
                self.fired = True
                Interface.objects.filter(pk=interface.pk).update(description="restricted")
            return execute(sql, params, many, context)

    scope_change = RestrictInterfaceBeforeLock()
    with connection.execute_wrapper(scope_change):
        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
                kwargs={"object_type": "device", "pk": device.pk},
            ),
            {
                "server_key": "default",
                "select": "198.18.19.30/24",
                "vrf_198.18.19.30/24": "",
            },
        )

    assert response.status_code == 302
    assert scope_change.fired
    assert any("no longer available in your view scope" in message for message in _message_texts(response))
    assert not IPAddress.objects.filter(address="198.18.19.30/24").exists()


@pytest.mark.django_db
def test_existing_ip_outside_change_scope_is_reported_without_mutation(client, settings):
    """A natural-key match outside the caller's change scope must stay unchanged."""
    from dcim.models import Device, Interface

    from netbox_librenms_plugin.utils import set_librenms_device_id

    _configure_test_server(settings)
    device = make_device("ip-change-scope")
    target = make_interface(device, "Ethernet1", iface_type="1000base-t")
    original = make_interface(device, "Ethernet2", iface_type="1000base-t")
    set_librenms_device_id(target, 7020, "default")
    target.save()
    protected_ip = IPAddress.objects.create(
        address="198.18.20.10/24",
        assigned_object=original,
        status="active",
    )
    changeable_ip = IPAddress.objects.create(address="198.18.20.11/24", status="active")
    user = make_user_with_perms(
        "ip-change-scope-user",
        [("view", Device), ("view", Interface), ("add", IPAddress)],
    )
    user = grant(user, "change", IPAddress, constraints={"pk": changeable_ip.pk})
    client.force_login(user)
    cache.set(
        f"librenms_ip_addresses_device_{device.pk}_default",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.20.10",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.20.10/24",
                    "port_id": 7020,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )

    response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": "198.18.20.10/24",
            "vrf_198.18.20.10/24": "",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert b"outside your change scope" in response.content
    # The row carries no grant to change this address, so it must not be offered as forceable
    # and must not hand the client an intent token it could replay.
    conflict = next(c for c in response.context["conflicts"] if c["row_id"] == "198.18.20.10/24")
    assert conflict["forceable"] is False
    assert conflict["intent"] == ""
    protected_ip.refresh_from_db()
    assert protected_ip.assigned_object_id == original.pk


@pytest.mark.django_db
def test_ip_sync_does_not_write_after_interface_owner_disappears(client, settings):
    """A row whose Device disappears before its lock must not create an IP address."""
    from django.db import connection

    from netbox_librenms_plugin.utils import set_librenms_device_id

    _configure_test_server(settings)
    device = make_device("ip-owner-disappears")
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    set_librenms_device_id(interface, 7021, "default")
    interface.save()
    cache.set(
        f"librenms_ip_addresses_device_{device.pk}_default",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.21.10",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.21.10/24",
                    "port_id": 7021,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    client.force_login(make_superuser("ip-owner-disappears-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )

    class DeleteOwnerBeforeLock:
        """Delete the Device as the writer is about to lock it."""

        def __init__(self, device_pk):
            self.device_pk = device_pk
            self.fired = False

        def __call__(self, execute, sql, params, many, context):
            if not self.fired and 'FROM "dcim_device"' in sql and "FOR UPDATE" in sql.upper():
                self.fired = True
                type(device).objects.filter(pk=self.device_pk).delete()
            return execute(sql, params, many, context)

    delete_owner = DeleteOwnerBeforeLock(device.pk)
    with connection.execute_wrapper(delete_owner):
        response = client.post(
            sync_url,
            {
                "server_key": "default",
                "select": "198.18.21.10/24",
                "vrf_198.18.21.10/24": "",
            },
        )

    assert response.status_code == 302
    assert delete_owner.fired
    assert not IPAddress.objects.filter(address="198.18.21.10/24").exists()


@pytest.mark.django_db
def test_force_reassigns_only_the_matching_vrf_row(client, settings):
    """Confirmation for one VRF must not mutate the same address in another VRF."""
    _configure_test_server(settings)
    blue = VRF.objects.create(name="Blue")
    red = VRF.objects.create(name="Red")
    device = make_device("ip-vrf-reassign", librenms_cf={"default": {"id": 42}})
    target = make_interface(device, "Ethernet1", iface_type="1000base-t")
    target.custom_field_data["librenms_id"] = {"default": 7001}
    target.save(update_fields=["custom_field_data"])
    blue_current = make_interface(device, "Ethernet2", iface_type="1000base-t")
    red_current = make_interface(device, "Ethernet3", iface_type="1000base-t")
    blue_ip = IPAddress.objects.create(address="198.18.3.10/24", vrf=blue, assigned_object=blue_current)
    red_ip = IPAddress.objects.create(address="198.18.3.10/24", vrf=red, assigned_object=red_current)
    client.force_login(make_superuser("ip-vrf-reassign-user"))
    assert _refresh_ip_snapshot(client, device, "198.18.3.10", 24).status_code == 200

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    conflict_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": "198.18.3.10/24",
            "vrf_198.18.3.10/24": str(blue.pk),
        },
        HTTP_HX_REQUEST="true",
    )

    assert conflict_response.status_code == 200
    conflict = conflict_response.context["conflicts"][0]
    assert conflict["target_vrf"] == "Blue"
    assert conflict["forceable"] is True

    force_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "force_conflict": "198.18.3.10/24",
            "conflict_intent": conflict["intent"],
        },
        HTTP_HX_REQUEST="true",
    )

    assert force_response.status_code == 200
    assert force_response.headers["HX-Redirect"]
    blue_ip.refresh_from_db()
    red_ip.refresh_from_db()
    assert blue_ip.assigned_object == target
    assert blue_ip.vrf == blue
    assert red_ip.assigned_object == red_current
    assert red_ip.vrf == red


@pytest.mark.django_db
def test_a_force_checkbox_without_a_valid_intent_syncs_nothing(client, settings):
    """An expired confirmation must drop its row, not sync it against the Global VRF."""
    # The checkbox and its signed intent post together, so a token that has aged past max_age
    # leaves the checkbox posting alone. The confirmation form carries no vrf_<row_id> field,
    # so the row would be classified against Global and a third address row created.
    _configure_test_server(settings)
    blue = VRF.objects.create(name="Stale Intent Blue")
    red = VRF.objects.create(name="Stale Intent Red")
    device = make_device("ip-stale-intent", librenms_cf={"default": {"id": 42}})
    target = make_interface(device, "Ethernet1", iface_type="1000base-t")
    target.custom_field_data["librenms_id"] = {"default": 7001}
    target.save(update_fields=["custom_field_data"])
    blue_current = make_interface(device, "Ethernet2", iface_type="1000base-t")
    red_current = make_interface(device, "Ethernet3", iface_type="1000base-t")
    blue_ip = IPAddress.objects.create(address="198.18.4.10/24", vrf=blue, assigned_object=blue_current)
    red_ip = IPAddress.objects.create(address="198.18.4.10/24", vrf=red, assigned_object=red_current)
    client.force_login(make_superuser("ip-stale-intent-user"))
    assert _refresh_ip_snapshot(client, device, "198.18.4.10", 24).status_code == 200

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    conflict_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": "198.18.4.10/24",
            "vrf_198.18.4.10/24": str(blue.pk),
        },
        HTTP_HX_REQUEST="true",
    )

    assert conflict_response.status_code == 200
    assert conflict_response.context["conflicts"][0]["forceable"] is True

    force_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "force_conflict": "198.18.4.10/24",
            "conflict_intent": "expired-token",
        },
        HTTP_HX_REQUEST="true",
    )

    assert force_response.status_code == 200
    assert _message_texts(force_response) == [
        "IP address confirmation is invalid or has expired. Refresh the IP data and try again."
    ]
    assert not IPAddress.objects.filter(address="198.18.4.10/24", vrf__isnull=True).exists()
    blue_ip.refresh_from_db()
    red_ip.refresh_from_db()
    assert blue_ip.assigned_object == blue_current
    assert red_ip.assigned_object == red_current
    assert target.ip_addresses.count() == 0


@pytest.mark.django_db
def test_sync_creates_an_independent_global_row_when_other_vrfs_are_ambiguous(client, settings):
    """Rows in other VRFs must not block creation in the explicitly selected Global VRF."""
    _configure_test_server(settings)
    blue = VRF.objects.create(name="Independent Blue")
    red = VRF.objects.create(name="Independent Red")
    device = make_device("ip-vrf-independent-create", librenms_cf={"default": {"id": 42}})
    target = make_interface(device, "Ethernet1", iface_type="1000base-t")
    target.custom_field_data["librenms_id"] = {"default": 7001}
    target.save(update_fields=["custom_field_data"])
    blue_interface = make_interface(device, "Ethernet2", iface_type="1000base-t")
    red_interface = make_interface(device, "Ethernet3", iface_type="1000base-t")
    blue_ip = IPAddress.objects.create(address="198.18.11.10/24", vrf=blue, assigned_object=blue_interface)
    red_ip = IPAddress.objects.create(address="198.18.11.10/24", vrf=red, assigned_object=red_interface)
    client.force_login(make_superuser("ip-vrf-independent-create-user"))
    assert _refresh_ip_snapshot(client, device, "198.18.11.10", 24).status_code == 200

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": "198.18.11.10/24",
            "vrf_198.18.11.10/24": "",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"]
    global_ip = IPAddress.objects.get(address="198.18.11.10/24", vrf=None)
    assert global_ip.assigned_object == target
    blue_ip.refresh_from_db()
    red_ip.refresh_from_db()
    assert blue_ip.assigned_object == blue_interface
    assert red_ip.assigned_object == red_interface


@pytest.mark.django_db
def test_vrf_change_requires_confirmation_and_moves_the_identified_row(client, settings):
    """Changing the VRF dropdown must confirm and then move the exact cached IP row."""
    _configure_test_server(settings)
    source_vrf = VRF.objects.create(name="Source VRF")
    destination_vrf = VRF.objects.create(name="Destination VRF")
    device = make_device("ip-vrf-move", librenms_cf={"default": {"id": 42}})
    target = make_interface(device, "Ethernet1", iface_type="1000base-t")
    target.custom_field_data["librenms_id"] = {"default": 7001}
    target.save(update_fields=["custom_field_data"])
    source_interface = make_interface(device, "Ethernet2", iface_type="1000base-t")
    existing = IPAddress.objects.create(
        address="2001:db8:2::10/64",
        vrf=source_vrf,
        assigned_object=source_interface,
    )
    client.force_login(make_superuser("ip-vrf-move-user"))
    assert _refresh_ip_snapshot(client, device, "2001:db8:2::10", 64).status_code == 200

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    conflict_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": "2001:db8:2::10/64",
            "vrf_2001:db8:2::10/64": str(destination_vrf.pk),
        },
        HTTP_HX_REQUEST="true",
    )

    assert conflict_response.status_code == 200
    conflict = conflict_response.context["conflicts"][0]
    assert conflict["target_vrf"] == "Destination VRF"
    existing.refresh_from_db()
    assert existing.vrf == source_vrf
    assert existing.assigned_object == source_interface

    force_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "force_all": "1",
            "conflict_intent": conflict["intent"],
        },
        HTTP_HX_REQUEST="true",
    )

    assert force_response.status_code == 200
    existing.refresh_from_db()
    assert existing.vrf == destination_vrf
    assert existing.assigned_object == target


@pytest.mark.django_db
def test_confirmed_vrf_move_fails_when_the_destination_changes(client, settings):
    """A force intent must recheck same-host rows inside its signed destination VRF."""
    _configure_test_server(settings)
    source_vrf = VRF.objects.create(name="Stable Source VRF")
    destination_vrf = VRF.objects.create(name="Changing Destination VRF")
    device = make_device("ip-vrf-stale-destination", librenms_cf={"default": {"id": 42}})
    target = make_interface(device, "Ethernet1", iface_type="1000base-t")
    target.custom_field_data["librenms_id"] = {"default": 7001}
    target.save(update_fields=["custom_field_data"])
    source_interface = make_interface(device, "Ethernet2", iface_type="1000base-t")
    existing = IPAddress.objects.create(
        address="198.18.6.10/24",
        vrf=source_vrf,
        assigned_object=source_interface,
    )
    client.force_login(make_superuser("ip-vrf-stale-destination-user"))
    assert _refresh_ip_snapshot(client, device, "198.18.6.10", 24).status_code == 200

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    conflict_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": "198.18.6.10/24",
            "vrf_198.18.6.10/24": str(destination_vrf.pk),
        },
        HTTP_HX_REQUEST="true",
    )
    conflict = conflict_response.context["conflicts"][0]

    destination_blocker = IPAddress.objects.create(
        address="198.18.6.10/32",
        vrf=destination_vrf,
        status="active",
    )
    force_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "force_all": "1",
            "conflict_intent": conflict["intent"],
        },
        HTTP_HX_REQUEST="true",
    )

    assert force_response.status_code == 200
    existing.refresh_from_db()
    destination_blocker.refresh_from_db()
    assert existing.vrf == source_vrf
    assert existing.assigned_object == source_interface
    assert str(destination_blocker.address) == "198.18.6.10/32"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("address", "incoming_prefix", "existing_address", "primary_field"),
    [
        ("198.18.4.10", 24, "198.18.4.10/32", "primary_ip4"),
        ("2001:db8:4::10", 64, "2001:db8:4::10/128", "primary_ip6"),
    ],
)
def test_same_host_with_a_different_prefix_requires_confirmation_before_update(
    client, settings, address, incoming_prefix, existing_address, primary_field
):
    """Force must update one confirmed VRF-scoped row without replacing its primary-IP identity."""
    _configure_test_server(settings)
    vrf = VRF.objects.create(name=f"Prefix VRF {incoming_prefix}")
    other_vrf = VRF.objects.create(name=f"Other Prefix VRF {incoming_prefix}")
    device = make_device(f"ip-prefix-vrf-{incoming_prefix}", librenms_cf={"default": {"id": 42}})
    target = make_interface(device, "Ethernet1", iface_type="1000base-t")
    target.custom_field_data["librenms_id"] = {"default": 7001}
    target.save(update_fields=["custom_field_data"])
    existing = IPAddress.objects.create(address=existing_address, vrf=vrf, status="active")
    other_vrf_ip = IPAddress.objects.create(address=existing_address, vrf=other_vrf, status="active")
    type(device).objects.filter(pk=device.pk).update(**{f"{primary_field}_id": existing.pk})
    client.force_login(make_superuser(f"ip-prefix-vrf-user-{incoming_prefix}"))
    assert _refresh_ip_snapshot(client, device, address, incoming_prefix).status_code == 200
    row_id = f"{address}/{incoming_prefix}"

    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    response = client.post(
        sync_url,
        {
            "server_key": "default",
            "select": row_id,
            f"vrf_{row_id}": str(vrf.pk),
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    conflict = response.context["conflicts"][0]
    assert conflict["forceable"] is True
    assert "different prefix length" in conflict["reason"]
    existing.refresh_from_db()
    assert str(existing.address) == existing_address
    assert not IPAddress.objects.filter(address=row_id, vrf=vrf).exists()

    force_response = client.post(
        sync_url,
        {
            "server_key": "default",
            "force_conflict": row_id,
            "conflict_intent": conflict["intent"],
        },
        HTTP_HX_REQUEST="true",
    )

    assert force_response.status_code == 200
    assert force_response.headers["HX-Redirect"]
    existing.refresh_from_db()
    other_vrf_ip.refresh_from_db()
    device.refresh_from_db()
    assert str(existing.address) == row_id
    assert existing.vrf == vrf
    assert existing.assigned_object == target
    assert getattr(device, f"{primary_field}_id") == existing.pk
    assert str(other_vrf_ip.address) == existing_address
    assert other_vrf_ip.vrf == other_vrf
    assert other_vrf_ip.assigned_object is None


@pytest.mark.django_db
def test_ip_table_render_reads_only_the_reported_addresses(client, settings):
    """The render must not load every IPAddress row in the deployment."""
    _configure_test_server(settings)
    device = make_device("ip-scan-scope", librenms_cf={"default": {"id": 42}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    interface.custom_field_data["librenms_id"] = {"default": 7001}
    interface.save(update_fields=["custom_field_data"])
    for host in range(10, 20):
        make_ip(f"203.0.113.{host}/24")
    client.force_login(make_superuser("ip-scan-scope-user"))

    with CaptureQueriesContext(connection) as queries:
        response = _refresh_ip_snapshot(client, device, "198.18.30.10", 24)

    assert response.status_code == 200
    address_reads = [query["sql"] for query in queries.captured_queries if 'FROM "ipam_ipaddress"' in query["sql"]]
    assert address_reads
    unfiltered_reads = [sql for sql in address_reads if "WHERE" not in sql.upper()]
    assert unfiltered_reads == [], unfiltered_reads


@pytest.mark.django_db
def test_configured_interface_name_field_survives_the_cache_round_trip(client, settings):
    """An unsupported configured field must not poison the snapshot the readers validate."""
    _configure_test_server(settings)
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["interface_name_field"] = "ifAlias"
    settings.PLUGINS_CONFIG = plugin_config
    device = make_device("ip-config-field", librenms_cf={"default": {"id": 42}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    interface.custom_field_data["librenms_id"] = {"default": 7001}
    interface.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("ip-config-field-user"))

    refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=_librenms_ip_response("198.18.31.10", 24, device_name=device.name),
    ):
        refresh_response = client.post(refresh_url, {"server_key": "default"}, HTTP_HX_REQUEST="true")

    assert refresh_response.status_code == 200
    cache_key = f"librenms_ip_addresses_device_{device.pk}_default"
    assert cache.get(cache_key)["interface_name_field"] in INTERFACE_NAME_FIELDS

    render_response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"server_key": "default", "tab": "ipaddresses"},
    )

    assert render_response.status_code == 200
    # The reader accepted what the writer stored, so the snapshot survives the render.
    assert cache.get(cache_key) is not None


@pytest.mark.django_db
def test_sync_without_a_selection_reports_the_empty_selection_error(client, settings):
    """A cached snapshot with no selected row must report the selection error, not a cache miss."""
    _configure_test_server(settings)
    device = make_device("ip-empty-selection", librenms_cf={"default": {"id": 42}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    interface.custom_field_data["librenms_id"] = {"default": 7001}
    interface.save(update_fields=["custom_field_data"])
    cache.set(
        f"librenms_ip_addresses_device_{device.pk}_default",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.32.10",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.32.10/24",
                    "port_id": 7001,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    client.force_login(make_superuser("ip-empty-selection-user"))

    response = client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": "device", "pk": device.pk},
        ),
        {"server_key": "default"},
    )

    assert response.status_code == 302
    assert "No IP addresses selected for synchronization." in _message_texts(response)
    assert not IPAddress.objects.filter(address="198.18.32.10/24").exists()


@pytest.mark.django_db
def test_create_missing_interfaces_requires_change_scope_for_the_new_interface(client, settings):
    """A constrained change grant must fail the row closed instead of populating the new row."""
    from dcim.models import Device, Interface

    _configure_test_server(settings)
    device = make_device("ip-create-scope", librenms_cf={"default": {"id": 42}})
    user = make_user_with_perms("ip-create-scope-user", [])
    user = grant(user, "view", Device, constraints={"pk": device.pk})
    user = grant(user, "view", Interface)
    user = grant(user, "add", Interface)
    # The add grant is unconstrained, the change grant excludes the row this sync creates.
    user = grant(user, "change", Interface, constraints={"name": "another-interface"})
    user = grant(user, "add", IPAddress)
    user = grant(user, "change", IPAddress)
    client.force_login(user)
    cache.set(
        f"librenms_ip_addresses_device_{device.pk}_default",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.30.10",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.30.10/24",
                    "port_id": 7030,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {
                7030: {
                    "port_id": 7030,
                    "ifName": "Ethernet1",
                    "ifDescr": "Ethernet1",
                    "ifType": "ethernetCsmacd",
                    "ifAlias": "Populated by the unscoped writer",
                }
            },
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )

    response = client.post(
        sync_url,
        {
            "server_key": "default",
            "create-missing-interfaces-toggle": "on",
            "select": "198.18.30.10/24",
            "vrf_198.18.30.10/24": "",
        },
    )

    assert response.status_code == 302
    # The row runs inside transaction.atomic(), so failing closed rolls the creation back.
    assert not Interface.objects.filter(device=device, name="Ethernet1").exists()
    # No assigned_object filter: an unassigned orphan would also mean the rollback was partial.
    assert not IPAddress.objects.filter(address="198.18.30.10/24").exists()


@pytest.mark.django_db
def test_create_missing_interfaces_is_refused_without_add_and_change_grants(client, settings):
    """A view-only caller must not reach interface creation, at the gate or the writer."""
    from dcim.models import Device, Interface

    _configure_test_server(settings)
    device = make_device("ip-view-only", librenms_cf={"default": {"id": 42}})
    # Deliberately view-only on both models, plus the IP grants the row would otherwise need.
    user = make_user_with_perms("ip-view-only-user", [])
    user = grant(user, "view", Device, constraints={"pk": device.pk})
    user = grant(user, "view", Interface)
    user = grant(user, "add", IPAddress)
    user = grant(user, "change", IPAddress)
    client.force_login(user)
    cache.set(
        f"librenms_ip_addresses_device_{device.pk}_default",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.32.10",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.32.10/24",
                    "port_id": 7032,
                    "interface_name": "Ethernet1",
                }
            ],
            "mgmt_ip": "",
            "ports_by_id": {
                7032: {
                    "port_id": 7032,
                    "ifName": "Ethernet1",
                    "ifDescr": "Ethernet1",
                    "ifType": "ethernetCsmacd",
                }
            },
            "interface_name_field": "ifName",
        },
        timeout=300,
    )

    response = client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": "device", "pk": device.pk},
        ),
        {
            "server_key": "default",
            "create-missing-interfaces-toggle": "on",
            "select": "198.18.32.10/24",
            "vrf_198.18.32.10/24": "",
        },
    )

    assert response.status_code in (302, 403)
    assert not Interface.objects.filter(device=device, name="Ethernet1").exists()
    assert not IPAddress.objects.filter(address="198.18.32.10/24").exists()

    # Positive control: the identical request succeeds once the caller holds add and change, so
    # the refusal above is the permission gate and not an unrelated failure earlier in the flow.
    user = grant(user, "add", Interface)
    user = grant(user, "change", Interface)
    client.force_login(user)

    allowed = client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": "device", "pk": device.pk},
        ),
        {
            "server_key": "default",
            "create-missing-interfaces-toggle": "on",
            "select": "198.18.32.10/24",
            "vrf_198.18.32.10/24": "",
        },
    )

    assert allowed.status_code == 302
    created = Interface.objects.get(device=device, name="Ethernet1")
    # The interface alone does not prove the row completed: assert the address the sync exists for.
    assert IPAddress.objects.get(address="198.18.32.10/24").assigned_object == created


@pytest.mark.django_db
def test_create_missing_interfaces_toggle_survives_a_table_refresh(client, settings):
    """The refreshed fragment must re-check the toggle the user posted, not silently drop it."""
    _configure_test_server(settings)
    device = make_device("ip-toggle-state", librenms_cf={"default": {"id": 42}})
    client.force_login(make_superuser("ip-toggle-state-user"))
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk])
    rows = [{"address": "198.18.31.10", "prefix_length": 24, "port_id": 7031, "interface": "Ethernet1"}]

    def _refresh(payload):
        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_ip_rows_response(rows, device_name=device.name),
        ):
            response = client.post(refresh_url, payload, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        html = response.content.decode()
        assert 'id="create-missing-interfaces-toggle-cb"' in html
        return html.split('id="create-missing-interfaces-toggle-cb"', 1)[1].split(">", 1)[0]

    base = {"server_key": "default", "interface_name_field": "ifName"}
    # Positive control: without the toggle the box must stay clear, so the assertion below
    # cannot pass just because "checked" appears somewhere in the element.
    assert "checked" not in _refresh(base)
    assert "checked" in _refresh({**base, "create-missing-interfaces-toggle": "on"})
