"""
The IP tab does not assign an address to an existing interface whose LibreNMS port is ignored.

Only an Ignore rule blocks an assignment: the type is not written, so an ambiguous port is
allowed. Without the full port record the assignment is refused when an Ignore rule could apply to
the owner's platform, and goes ahead when none could. The tests drive real requests against real
NetBox objects and real rules; the management-IP lookup reads a loopback LibreNMS.
"""

import re
from html import unescape

import pytest
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse
from ipam.models import IPAddress

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.sync_cache import TAB_SPECS, SyncTab, sync_snapshot_key
from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_superuser
from netbox_librenms_plugin.tests.test_interface_rule_writes import _platform, _port
from netbox_librenms_plugin.utils import set_librenms_device_id

SERVER_KEY = "default"
IGNORE = InterfaceTypeMapping.ACTION_IGNORE
DEVICE_ID = 4310


def _device(name, platform):
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
    device.platform = platform
    device.save()
    return device


def _bound(device, name, port_id):
    interface = make_interface(device, name, iface_type="virtual")
    set_librenms_device_id(interface, port_id, SERVER_KEY)
    interface.save()
    return interface


def _row(address, port_id, name):
    return {
        "ip_address": address,
        "prefix_length": 24,
        "ip_with_mask": f"{address}/24",
        "port_id": port_id,
        "interface_name": name,
    }


def _seed(device, rows, ports_by_id, bound_ports_by_id=None):
    cache.set(
        sync_snapshot_key(device, TAB_SPECS[SyncTab.IP_ADDRESSES].data_type, SERVER_KEY),
        {
            "ip_addresses": rows,
            "mgmt_ip": "",
            "ports_by_id": ports_by_id,
            "bound_ports_by_id": bound_ports_by_id or {},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )


def _sync(client, device, rows, **extra):
    data = {"server_key": SERVER_KEY, "select": [row["ip_with_mask"] for row in rows], **extra}
    data.update({f"vrf_{row['ip_with_mask']}": "" for row in rows})
    return client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses", kwargs={"object_type": "device", "pk": device.pk}
        ),
        data,
        HTTP_HX_REQUEST="true",
    )


def _messages(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def _skipped(response, address, reason):
    text = f"Skipped (the interface rules refuse the interface): {address} ({reason}"
    return any(text in message for message in _messages(response))


def _status_cell(client, device, address):
    """Render the IP tab and return the status cell of *address*, as the browser gets it."""
    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"tab": "ipaddresses", "server_key": SERVER_KEY},
    )
    assert response.status_code == 200
    html = unescape(response.context["ip_sync"]["table"].as_html(response.wsgi_request))
    row = re.search(rf'<tr[^>]*data-name="{re.escape(address)}"[^>]*>.*?</tr>', html, flags=re.S)
    assert row is not None
    return re.search(r'<td[^>]*data-col="status"[^>]*>(.*?)</td>', row.group(0), flags=re.S).group(1)


@pytest.fixture
def ignored_vlan(client, live_librenms):
    """A device on a platform that ignores Vlan ports, a bound Vlan10 and a bound eth0."""
    platform = _platform("ip-assign")
    rule = InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
    device = _device("ip-assign", platform)
    vlan = _bound(device, "Vlan10", 7100)
    eth = _bound(device, "eth0", 7101)
    client.force_login(make_superuser("ip-assign-user"))
    return device, vlan, eth, rule


@pytest.mark.django_db
class TestAnIgnoredPortGetsNoAddress:
    def test_a_new_address_is_not_created_on_the_ignored_interface(self, client, ignored_vlan):
        device, vlan, eth, rule = ignored_vlan
        rows = [_row("198.18.60.10", 7100, "Vlan10"), _row("198.18.60.11", 7101, "eth0")]
        _seed(device, rows, {7100: _port(7100, "Vlan10", if_type="propVirtual"), 7101: _port(7101, "eth0")})

        cell = _status_cell(client, device, "198.18.60.10/24")
        response = _sync(client, device, rows)

        assert f"Not synced: LibreNMS port 7100 (Vlan10): ignored by interface rule {rule.pk}" in cell
        assert "Create</button>" not in cell
        assert not IPAddress.objects.filter(address="198.18.60.10/24").exists()
        # The skip is per row: the allowed row in the same request is written.
        assert IPAddress.objects.get(address="198.18.60.11/24").assigned_object == eth
        assert _skipped(
            response, "198.18.60.10/24", f"LibreNMS port 7100 (Vlan10): ignored by interface rule {rule.pk}"
        )
        assert not any("Failed" in message for message in _messages(response))

    def test_a_confirmed_reassignment_is_refused(self, client, ignored_vlan):
        device, vlan, eth, rule = ignored_vlan
        rows = [_row("198.18.61.10", 7100, "Vlan10")]
        _seed(device, rows, {7100: _port(7100, "Vlan10", if_type="propVirtual")})
        existing = IPAddress.objects.create(address="198.18.61.10/24", assigned_object=eth, status="active")
        # The confirmation is signed while no rule applies; the rule then exists when it is used.
        InterfaceTypeMapping.objects.filter(pk=rule.pk).update(platform=None, name_pattern="^no-such-port$")
        conflict = _sync(client, device, rows).context["conflicts"][0]
        InterfaceTypeMapping.objects.filter(pk=rule.pk).update(platform=device.platform, name_pattern="^Vlan")

        response = _sync(client, device, [], force_all="1", conflict_intent=conflict["intent"])

        existing.refresh_from_db()
        assert existing.assigned_object == eth
        assert _skipped(
            response, "198.18.61.10/24", f"LibreNMS port 7100 (Vlan10): ignored by interface rule {rule.pk}"
        )

    def test_the_primary_ip_is_not_set_from_the_ignored_interface(self, client, ignored_vlan, live_librenms):
        device, vlan, _eth, rule = ignored_vlan
        rows = [_row("198.18.62.10", 7100, "Vlan10")]
        _seed(device, rows, {7100: _port(7100, "Vlan10", if_type="propVirtual")})
        address = IPAddress.objects.create(address="198.18.62.10/24", assigned_object=vlan, status="active")
        live_librenms.server.register(
            f"/api/v0/devices/{DEVICE_ID}",
            {"status": "ok", "devices": [{"device_id": DEVICE_ID, "ip": "198.18.62.10"}]},
        )

        response = _sync(client, device, rows, **{"set-primary-ip-toggle": "true"})

        device.refresh_from_db()
        assert device.primary_ip4_id is None
        address.refresh_from_db()
        assert address.assigned_object == vlan
        assert _skipped(
            response, "198.18.62.10/24", f"LibreNMS port 7100 (Vlan10): ignored by interface rule {rule.pk}"
        )


@pytest.fixture
def renamed_source(client, live_librenms):
    """
    The source port 7101 is named eth0, but NetBox's eth0 is bound to port 7100, which is ignored.

    Nothing is bound to 7101, so the resolver falls back by name to eth0. The snapshot keeps the
    record of port 7100 because an interface in scope is bound to it.
    """
    platform = _platform("ip-assign-bound")
    rule = InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
    device = _device("ip-assign-bound", platform)
    eth = _bound(device, "eth0", 7100)
    other = make_interface(device, "eth9", iface_type="virtual")
    rows = [_row("198.18.70.10", 7101, "eth0")]
    _seed(
        device,
        rows,
        {7101: _port(7101, "eth0")},
        bound_ports_by_id={7100: _port(7100, "Vlan10", if_type="propVirtual")},
    )
    client.force_login(make_superuser("ip-assign-bound-user"))
    return device, eth, other, rows, rule


@pytest.mark.django_db
class TestTheTargetInterfacesOwnPortIsChecked:
    def test_a_new_address_is_refused_and_the_row_says_why(self, client, renamed_source):
        device, _eth, _other, rows, rule = renamed_source
        reason = f"LibreNMS port 7100 (Vlan10): ignored by interface rule {rule.pk}"

        cell = _status_cell(client, device, "198.18.70.10/24")
        response = _sync(client, device, rows)

        assert f"Not synced: {reason}" in cell
        assert not IPAddress.objects.filter(address="198.18.70.10/24").exists()
        assert _skipped(response, "198.18.70.10/24", reason)

    def test_a_confirmed_reassignment_is_refused(self, client, renamed_source):
        device, eth, other, rows, rule = renamed_source
        existing = IPAddress.objects.create(address="198.18.70.10/24", assigned_object=other, status="active")
        InterfaceTypeMapping.objects.filter(pk=rule.pk).update(platform=None, name_pattern="^no-such-port$")
        conflict = _sync(client, device, rows).context["conflicts"][0]
        InterfaceTypeMapping.objects.filter(pk=rule.pk).update(platform=device.platform, name_pattern="^Vlan")

        response = _sync(client, device, [], force_all="1", conflict_intent=conflict["intent"])

        existing.refresh_from_db()
        assert existing.assigned_object == other
        assert _skipped(
            response, "198.18.70.10/24", f"LibreNMS port 7100 (Vlan10): ignored by interface rule {rule.pk}"
        )

    def test_the_primary_ip_is_not_set(self, client, renamed_source, live_librenms):
        device, eth, _other, rows, rule = renamed_source
        IPAddress.objects.create(address="198.18.70.10/24", assigned_object=eth, status="active")
        live_librenms.server.register(
            f"/api/v0/devices/{DEVICE_ID}",
            {"status": "ok", "devices": [{"device_id": DEVICE_ID, "ip": "198.18.70.10"}]},
        )

        response = _sync(client, device, rows, **{"set-primary-ip-toggle": "true"})

        device.refresh_from_db()
        assert device.primary_ip4_id is None
        assert _skipped(
            response, "198.18.70.10/24", f"LibreNMS port 7100 (Vlan10): ignored by interface rule {rule.pk}"
        )


def _serve_device(live_librenms, device_id, ports, addresses):
    """Serve one LibreNMS device's ports and IP addresses for a real refresh."""
    server = live_librenms.server
    server.register(f"/api/v0/devices/{device_id}/ports", {"status": "ok", "ports": ports})
    server.register(f"/api/v0/devices/{device_id}/ip", {"status": "ok", "addresses": addresses})
    server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [{"device_id": device_id, "ip": ""}]})
    server.register("/api/v0/poller_group", {"status": "ok", "get_poller_group": []})


@pytest.mark.django_db
def test_a_refresh_keeps_the_record_of_the_port_bound_to_the_target_interface(client, live_librenms):
    """
    NetBox eth0 is bound to port 7100, which LibreNMS now calls eth9; port 7101 is named eth0.

    The one IP row is on 7101 and resolves by name to NetBox eth0. Neither record matches the
    global Ignore rule, so the address is assigned. The record of 7100 is evidence only: it is
    not an IP row and not a name candidate.
    """
    InterfaceTypeMapping.objects.create(action=IGNORE, name_pattern="^Vlan")
    device = make_device("ip-assign-refresh", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
    eth0 = _bound(device, "eth0", 7100)
    _serve_device(
        live_librenms,
        DEVICE_ID,
        [_port(7100, "eth9"), _port(7101, "eth0")],
        [{"port_id": 7101, "ipv4_address": "198.18.80.10", "ipv4_prefixlen": 24}],
    )
    client.force_login(make_superuser("ip-assign-refresh-user"))

    refreshed = client.post(
        reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk]),
        {"server_key": SERVER_KEY, "interface_name_field": "ifName"},
        HTTP_HX_REQUEST="true",
    )
    snapshot = cache.get(sync_snapshot_key(device, TAB_SPECS[SyncTab.IP_ADDRESSES].data_type, SERVER_KEY))
    response = _sync(client, device, [_row("198.18.80.10", 7101, "eth0")])

    assert refreshed.status_code == 200
    assert [row["port_id"] for row in snapshot["ip_addresses"]] == [7101]
    assert {str(key) for key in snapshot["ports_by_id"]} == {"7101"}
    assert snapshot["bound_ports_by_id"]["7100"]["ifName"] == "eth9"
    assert IPAddress.objects.get(address="198.18.80.10/24").assigned_object == eth0
    assert not any("Skipped" in message for message in _messages(response))


@pytest.mark.django_db
def test_the_table_does_not_resolve_an_interface_the_caller_cannot_view(live_librenms):
    """The sync resolves only interfaces in the caller's view scope; the table must not name one outside it."""
    from dcim.models import Device, Interface
    from django.test import Client
    from ipam.models import VRF

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    InterfaceTypeMapping.objects.create(action=IGNORE, name_pattern="^secret")
    device = _device("ip-assign-hidden", None)
    hidden = _bound(device, "eth0", 7100)
    rows = [_row("198.18.81.10", 7101, "eth0")]
    _seed(
        device,
        rows,
        {7101: _port(7101, "eth0")},
        bound_ports_by_id={7100: _port(7100, "secret-uplink")},
    )
    user = make_user_with_perms("ip-assign-hidden-user", [("view", Device), ("view", IPAddress), ("view", VRF)])
    user = grant(user, "view", Interface, constraints={"pk": hidden.pk + 100000})
    client = Client()
    client.force_login(user)

    cell = _status_cell(client, device, "198.18.81.10/24")
    page = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"tab": "ipaddresses", "server_key": SERVER_KEY},
    ).content.decode()

    assert "Missing NetBox Object" in cell
    for secret in ("secret-uplink", "LibreNMS port 7100", reverse("dcim:interface", args=[hidden.pk])):
        assert secret not in page


@pytest.mark.django_db
def test_a_sibling_member_the_caller_cannot_view_is_never_resolved(live_librenms):
    """
    The caller may view member A and every interface, but not sibling B. B's eth0 is bound to port
    7100 (secret-uplink), which a global rule ignores; the only row names port 7101, eth0. The
    table and the sync resolve against the same view-scoped owners, so neither reaches B.
    """
    from dcim.models import Device, Interface
    from django.test import Client
    from ipam.models import VRF

    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    rule = InterfaceTypeMapping.objects.create(action=IGNORE, name_pattern="^secret")
    member_a = _device("ip-assign-member-a", None)
    member_b = make_device("ip-assign-member-b")
    make_virtual_chassis("ip-assign-vc", member_a, member_b)
    hidden = _bound(member_b, "eth0", 7100)
    rows = [_row("198.18.82.10", 7101, "eth0")]
    _seed(member_a, rows, {7101: _port(7101, "eth0")}, bound_ports_by_id={7100: _port(7100, "secret-uplink")})
    user = make_user_with_perms(
        "ip-assign-sibling-user",
        [("view", Interface), ("view", IPAddress), ("add", IPAddress), ("change", IPAddress), ("view", VRF)],
    )
    user = grant(user, "view", Device, constraints={"pk": member_a.pk})
    client = Client()
    client.force_login(user)

    cell = _status_cell(client, member_a, "198.18.82.10/24")
    rendered = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[member_a.pk]),
        {"tab": "ipaddresses", "server_key": SERVER_KEY},
    )
    # The IP tab's own table; other tabs of the page are out of this test's scope.
    page = unescape(rendered.context["ip_sync"]["table"].as_html(rendered.wsgi_request))
    response = _sync(client, member_a, rows)
    texts = " ".join(_messages(response))

    assert "Missing NetBox Object" in cell
    assert not IPAddress.objects.filter(address="198.18.82.10/24").exists()
    assert "Skipped (no matching NetBox interface): 198.18.82.10/24" in texts
    for secret in (
        "secret-uplink",
        "port 7100",
        str(rule),
        reverse("dcim:interface", args=[hidden.pk]),
        reverse("dcim:device", args=[member_b.pk]),
    ):
        assert secret not in page
        assert secret not in texts


@pytest.mark.django_db
def test_a_source_port_bound_on_a_hidden_member_is_not_named(live_librenms):
    """
    Members A (visible) and B (hidden) each have an eth0; both interfaces are viewable. B's eth0
    holds the source port 7101, which a global rule ignores. The row falls back by name to A's
    eth0, but port 7101's owner is B, so the refusal names nothing.
    """
    from dcim.models import Device, Interface
    from django.test import Client
    from ipam.models import VRF

    from netbox_librenms_plugin.interface_rules import HIDDEN_PORT_REASON
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    rule = InterfaceTypeMapping.objects.create(action=IGNORE, name_pattern="^secret")
    member_a = _device("ip-assign-owner-a", None)
    member_b = make_device("ip-assign-owner-b")
    make_virtual_chassis("ip-assign-owner-vc", member_a, member_b)
    target = make_interface(member_a, "eth0", iface_type="virtual")
    hidden = _bound(member_b, "eth0", 7101)
    rows = [_row("198.18.83.10", 7101, "eth0")]
    _seed(member_a, rows, {7101: _port(7101, "eth0", descr="secret-src")})
    user = make_user_with_perms(
        "ip-assign-owner-user",
        [("view", Interface), ("view", IPAddress), ("add", IPAddress), ("change", IPAddress), ("view", VRF)],
    )
    user = grant(user, "view", Device, constraints={"pk": member_a.pk})
    client = Client()
    client.force_login(user)

    cell = _status_cell(client, member_a, "198.18.83.10/24")
    response = _sync(client, member_a, rows)
    texts = " ".join(_messages(response))

    assert f"Not synced: {HIDDEN_PORT_REASON}" in cell
    assert not IPAddress.objects.filter(address="198.18.83.10/24").exists()
    assert _skipped(response, "198.18.83.10/24", HIDDEN_PORT_REASON)
    for secret in (
        "port 7101",
        str(rule),
        reverse("dcim:interface", args=[hidden.pk]),
        reverse("dcim:device", args=[member_b.pk]),
    ):
        assert secret not in cell
        assert secret not in texts
    assert target.ip_addresses.count() == 0


def _ip_render_queries(client, tag, count, first_port):
    """Seed *count* rows that a global rule refuses, render the IP tab from its cache, and count queries."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    device = make_device(tag, librenms_cf={SERVER_KEY: {"id": first_port}})
    rows, ports = [], {}
    for index in range(count):
        port_id = first_port + index
        _bound(device, f"Vlan{index}", port_id)
        rows.append(_row(f"10.{first_port % 250}.{index}.10", port_id, f"Vlan{index}"))
        ports[port_id] = _port(port_id, f"Vlan{index}", if_type="propVirtual")
    _seed(device, rows, ports)
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": "ipaddresses"},
    )
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url, {"server_key": SERVER_KEY})
    assert response.status_code == 200
    assert unescape(response.content.decode()).count("Not synced: LibreNMS port") == count
    # NetBox reloads its config revision on its own schedule; that is not part of the table.
    return sum("core_configrevision" not in query["sql"] for query in queries.captured_queries)


@pytest.mark.django_db
def test_the_ip_table_decides_blocking_rows_in_a_fixed_number_of_queries(live_librenms):
    """The rule check adds the same queries for 2 blocking rows as for 20 (no query per row)."""
    from dcim.models import Device, Interface
    from django.test import Client
    from ipam.models import VRF

    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    InterfaceTypeMapping.objects.create(action=IGNORE, name_pattern="^Vlan")
    user = make_user_with_perms(
        "ip-assign-queries-user", [("view", Device), ("view", Interface), ("view", IPAddress), ("view", VRF)]
    )
    client = Client()
    client.force_login(user)
    _ip_render_queries(client, "ip-assign-queries-warm", 2, 7400)

    two = _ip_render_queries(client, "ip-assign-queries-2", 2, 7500)
    twenty = _ip_render_queries(client, "ip-assign-queries-20", 20, 7600)

    print(f"IP table queries: N=2 -> {two}, N=20 -> {twenty}")
    assert twenty == two


@pytest.mark.django_db
def test_an_ambiguous_port_still_gets_its_address(client, live_librenms):
    """Two equal-rank Set type rules block an interface write, but an assignment writes no type."""
    platform = _platform("ip-assign-tie")
    for netbox_type in ("virtual", "other"):
        InterfaceTypeMapping.objects.create(
            platform=platform, name_pattern=f"^Vlan(?#{netbox_type})", netbox_type=netbox_type
        )
    device = _device("ip-assign-tie", platform)
    vlan = _bound(device, "Vlan20", 7200)
    rows = [_row("198.18.63.10", 7200, "Vlan20")]
    _seed(device, rows, {7200: _port(7200, "Vlan20", if_type="propVirtual")})
    client.force_login(make_superuser("ip-assign-tie-user"))

    _sync(client, device, rows)

    assert IPAddress.objects.get(address="198.18.63.10/24").assigned_object == vlan


@pytest.mark.django_db
@pytest.mark.parametrize("may_ignore", [True, False], ids=["ignore-rule-on-platform", "ignore-rule-elsewhere"])
def test_a_missing_port_record_refuses_only_when_an_ignore_rule_could_apply(client, live_librenms, may_ignore):
    platform, other = _platform("ip-assign-missing"), _platform("ip-assign-missing-other")
    InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform if may_ignore else other, name_pattern="^Vlan")
    device = _device("ip-assign-missing", platform)
    eth = _bound(device, "eth5", 7300)
    rows = [_row("198.18.64.10", 7300, "eth5")]
    # The port snapshot has no record for the row's port: nothing proves the port is not ignored.
    _seed(device, rows, {7300: None})
    client.force_login(make_superuser("ip-assign-missing-user"))

    cell = _status_cell(client, device, "198.18.64.10/24")
    response = _sync(client, device, rows)

    created = IPAddress.objects.filter(address="198.18.64.10/24").first()
    if may_ignore:
        assert "Refresh needed" in cell
        assert created is None
        assert _skipped(
            response,
            "198.18.64.10/24",
            "LibreNMS port 7300: no LibreNMS port record is cached for it; refresh the data",
        )
    else:
        assert "Refresh needed" not in cell
        assert created.assigned_object == eth
