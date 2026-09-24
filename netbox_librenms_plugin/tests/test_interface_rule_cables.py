"""
A cable write goes ahead only when no LibreNMS port it touches is ignored.

The touched ports are the row's local and remote ports, the port bound to each endpoint the write
attaches (a manual pick too), and the port bound to each far end of a cable it removes. Each is
decided with the binding and the owner platform read under the lock. Only Ignore blocks a cable,
and a port with no record blocks it only when an Ignore rule could apply. The tests drive real
requests against real NetBox objects and rules; LibreNMS is a loopback server.
"""

import json
import re
from html import unescape

import pytest
from dcim.models import Cable
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.interface_rules import HIDDEN_PORT_REASON
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.sync_cache import sync_snapshot_key
from netbox_librenms_plugin.tests.conftest import (
    bind_librenms_server,
    cable_together,
    make_device,
    make_interface,
    make_superuser,
    map_device_to_librenms,
)
from netbox_librenms_plugin.tests.test_cable_overwrite import _confirmed_intent
from netbox_librenms_plugin.tests.test_interface_rule_writes import _platform, _port
from netbox_librenms_plugin.utils import assign_cable_row_ids, get_librenms_device_id, set_librenms_device_id
from netbox_librenms_plugin.views.base.cables_view import _RAW_LINK_KEYS, port_record
from netbox_librenms_plugin.views.sync.cables import SyncCablesView

SERVER_KEY = "default"
IGNORE = InterfaceTypeMapping.ACTION_IGNORE


def _bound(device, name, port_id):
    interface = make_interface(device, name, iface_type="10gbase-x-sfpp")
    set_librenms_device_id(interface, port_id, SERVER_KEY)
    interface.save()
    return interface


def _row(local, remote_device, *, records=True, **overrides):
    """One snapshot row between local Te1/1 (port 100) and remote Gi0/1 (port 500)."""
    row = {
        "local_port": local.name,
        "local_port_id": 100,
        "local_port_alt": None,
        "link_id": 1,
        "protocol": "lldp",
        "remote_port": "Gi0/1",
        "remote_device": remote_device.name,
        "remote_port_id": 500,
        "remote_device_id": None,
        "remote_port_key": 500,
        "_source": "main",
    }
    if records:
        row["local_port_record"] = port_record(_port(100, local.name))
        row["remote_port_record"] = port_record(_port(500, "Gi0/1"))
    row.update(overrides)
    return row


def _seed(device, row):
    raw = assign_cable_row_ids([{key: value for key, value in row.items() if key in _RAW_LINK_KEYS}])
    cache.set(
        sync_snapshot_key(device, "links", SERVER_KEY), {"links": raw, "snapshot_token": "rule-gate"}, timeout=300
    )
    return raw[0]["row_id"]


def _render(client, device):
    """Render the cable tab: the one row's record and its HTML."""
    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"tab": "cables", "server_key": SERVER_KEY},
    )
    assert response.status_code == 200
    table = response.context["cable_sync"]["table"]
    (row,) = list(table.rows)
    return row.record, unescape(table.as_html(response.wsgi_request))


def _sync_data(record, **extra):
    row_id = record["row_id"]
    return {
        "select": row_id,
        "server_key": SERVER_KEY,
        f"expected_local_id_{row_id}": record["netbox_local_interface_id"],
        f"expected_local_device_id_{row_id}": record["netbox_local_device_id"],
        f"expected_remote_id_{row_id}": record["netbox_remote_interface_id"],
        f"expected_remote_device_id_{row_id}": record["netbox_remote_device_id"],
        **extra,
    }


def _sync(client, device, data):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[device.pk]), data, HTTP_HX_REQUEST="true"
    )


def _messages(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def _pick(client, device, row_id, interface):
    """Pick the row's remote end by hand, through the real picker."""
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[device.pk]),
        {"row_id": row_id, "server_key": SERVER_KEY, "remote_interface_id": interface.pk},
    )
    assert response.status_code == 200


def _refused(response, port_label, reason):
    text = f"Skipped (the interface rules refuse a port): Te1/1 (LibreNMS port {port_label}: {reason}"
    return any(text in message for message in _messages(response))


class _Link:
    """Two mapped devices on their own platforms, Te1/1 bound to port 100 and Gi0/1 bound to port 500."""

    def __init__(self, tag, settings, server):
        bind_librenms_server(settings, server, server_key=SERVER_KEY)
        self.server = server
        self.local_platform = _platform(f"{tag}-local")
        self.remote_platform = _platform(f"{tag}-remote")
        self.local_device = self._device(f"{tag}-local", self.local_platform, 8)
        self.remote_device = self._device(f"{tag}-remote", self.remote_platform, 9)
        self.local = _bound(self.local_device, "Te1/1", 100)
        self.remote = _bound(self.remote_device, "Gi0/1", 500)

    @staticmethod
    def _device(name, platform, librenms_id):
        device = make_device(name)
        device.platform = platform
        device.save()
        return map_device_to_librenms(device, librenms_id, server_key=SERVER_KEY)

    def ignore(self, platform, pattern):
        return InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern=pattern)

    def cables(self):
        """Return the cables on the two endpoints, read from the database."""
        self.local.refresh_from_db()
        self.remote.refresh_from_db()
        return {interface.cable_id for interface in (self.local, self.remote)} - {None}


@pytest.fixture
def link(settings, librenms_server, client, request):
    client.force_login(make_superuser("cable-rule-user"))
    return _Link(request.node.name[:24].replace("[", "-").rstrip("]"), settings, librenms_server)


@pytest.mark.django_db
def test_the_refresh_keeps_the_rule_inputs_and_an_ignored_remote_port_gets_no_cable(client, link):
    """The whole path: refresh, table, sync. The remote platform ignores Gi0/1."""
    rule = link.ignore(link.remote_platform, "^Gi0/1$")
    link.server.register(
        "/api/v0/devices/8/links",
        {
            "status": "ok",
            "links": [
                {
                    "id": 1,
                    "protocol": "lldp",
                    "local_port_id": 100,
                    "remote_port": "Gi0/1",
                    "remote_hostname": link.remote_device.name,
                    "remote_port_id": 500,
                    "remote_device_id": 9,
                }
            ],
        },
    )
    link.server.register("/api/v0/devices/8/ports", {"status": "ok", "ports": [_port(100, "Te1/1")]})
    link.server.register("/api/v0/devices/9/ports", {"status": "ok", "ports": [_port(500, "Gi0/1")]})

    refreshed = client.post(
        reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[link.local_device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )
    assert refreshed.status_code == 200
    (raw,) = cache.get(sync_snapshot_key(link.local_device, "links", SERVER_KEY))["links"]
    assert raw["local_port_record"] == port_record(_port(100, "Te1/1"))
    assert raw["remote_port_record"] == port_record(_port(500, "Gi0/1"))

    record, html = _render(client, link.local_device)
    response = _sync(client, link.local_device, _sync_data(record))

    reason = f"ignored by interface rule {rule.pk}"
    assert f"Not synced: LibreNMS port 500 (Gi0/1): {reason}" in html
    assert "Sync Cable" not in html
    assert re.search(r'<input[^>]*name="select"[^>]*disabled', html)
    assert link.cables() == set()
    assert _refused(response, "500 (Gi0/1)", reason)


@pytest.mark.django_db
def test_an_ignored_local_port_gets_no_cable_and_the_verified_row_says_why(client, link):
    rule = link.ignore(link.local_platform, "^Te")
    row_id = _seed(link.local_device, _row(link.local, link.remote_device))

    record, html = _render(client, link.local_device)
    verified = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_cable"),
        data=json.dumps({"device_id": link.local_device.pk, "row_id": row_id, "server_key": SERVER_KEY}),
        content_type="application/json",
    ).json()["formatted_row"]
    response = _sync(client, link.local_device, _sync_data(record))

    reason = f"ignored by interface rule {rule.pk}"
    assert f"Not synced: LibreNMS port 100 (Te1/1): {reason}" in html
    assert f"Not synced: LibreNMS port 100 (Te1/1): {reason}" in unescape(verified["actions"])
    assert verified["can_create_cable"] is False
    assert link.cables() == set()
    assert _refused(response, "100 (Te1/1)", reason)


@pytest.mark.django_db
def test_a_manual_pick_bound_to_an_ignored_port_gets_no_cable(client, link):
    """The advertised port is allowed; the picked interface is bound to a port the rules ignore."""
    rule = link.ignore(link.remote_platform, "^Gi0/9$")
    picked = _bound(link.remote_device, "Gi0/9", 509)
    link.server.register("/api/v0/ports/509", {"status": "ok", "port": [_port(509, "Gi0/9")]})
    row_id = _seed(link.local_device, _row(link.local, link.remote_device))
    _pick(client, link.local_device, row_id, picked)

    record, _html = _render(client, link.local_device)
    assert record["netbox_remote_interface_id"] == picked.pk
    response = _sync(client, link.local_device, _sync_data(record))

    assert link.cables() == set()
    assert _refused(response, "509 (Gi0/9)", f"ignored by interface rule {rule.pk}")
    assert "/api/v0/ports/509" in [request["path"] for request in link.server.requests]


@pytest.mark.django_db
@pytest.mark.parametrize("ignored", [True, False], ids=["far-end-ignored", "control"])
def test_a_replacement_that_removes_a_cable_to_an_ignored_port_is_refused(client, link, ignored):
    """Te1/1 is cabled to Xe0/1 on a third device; replacing that cable detaches Xe0/1."""
    far_platform = _platform("cable-rule-far")
    far_device = make_device("cable-rule-far")
    far_device.platform = far_platform
    far_device.save()
    far = _bound(far_device, "Xe0/1", 777)
    old = cable_together(link.local, far)
    link.server.register("/api/v0/ports/777", {"status": "ok", "port": [_port(777, "Xe0/1")]})
    rule = link.ignore(far_platform, "^Xe") if ignored else link.ignore(far_platform, "^no-such-port$")
    _seed(link.local_device, _row(link.local, link.remote_device))
    record, _html = _render(client, link.local_device)
    assert record["cable_status"] == "Cable Mismatch"

    first = _sync(client, link.local_device, _sync_data(record))
    link.local.refresh_from_db()
    link.remote.refresh_from_db()
    intent = _confirmed_intent(link.local, link.remote, old)
    forced = _sync(
        client,
        link.local_device,
        _sync_data(record, force="on", **{f"expected_cable_intent_{record['row_id']}": intent}),
    )

    link.local.refresh_from_db()
    if ignored:
        assert link.local.cable_id == old.pk
        for response in (first, forced):
            assert _refused(response, "777 (Xe0/1)", f"ignored by interface rule {rule.pk}")
    else:
        assert any("Overwrite protection" in message for message in _messages(first))
        assert not Cable.objects.filter(pk=old.pk).exists()
        assert link.local.cable.b_terminations == [link.remote]


def _restricted_client(username, device_ids, interface_ids):
    """A real client whose user may view and change only these devices and interfaces."""
    from dcim.models import Device, Interface
    from django.test import Client

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    user = make_user_with_perms(username, [("view", Cable), ("add", Cable), ("change", Cable)])
    for action in ("view", "change"):
        user = grant(user, action, Device, constraints={"pk__in": device_ids})
        user = grant(user, action, Interface, constraints={"pk__in": interface_ids})
    client = Client()
    client.force_login(user)
    return client


def _actions_cell(html):
    match = re.search(r'<td[^>]*data-col="actions"[^>]*>(.*?)</td>', html, flags=re.S)
    assert match is not None
    return match.group(1)


@pytest.mark.django_db
@pytest.mark.parametrize("neighbour", ["hidden", "not-in-netbox"])
def test_the_advertised_port_of_a_neighbour_the_caller_cannot_see_is_not_named(link, neighbour):
    """
    The row's advertised port 500 (secret-if) is on neighbour B, which the caller cannot view or
    which is not in NetBox. A global rule ignores it; the caller picks a remote end on device C.
    The table and the sync still refuse, but neither names the port, its name or the rule.
    """
    rule = link.ignore(None, "^secret-if$")
    hidden_neighbour = make_device("cable-rule-secret-neighbour")
    picked_device = make_device("cable-rule-picked-c")
    picked = _bound(picked_device, "Eth9", 509)
    link.server.register("/api/v0/ports/509", {"status": "ok", "port": [_port(509, "Eth9")]})
    row = _row(link.local, link.remote_device, remote_port="secret-if")
    row["remote_port_record"] = port_record(_port(500, "secret-if"))
    row["remote_device"] = hidden_neighbour.name if neighbour == "hidden" else "not-in-netbox"
    row_id = _seed(link.local_device, row)
    client = _restricted_client(
        f"cable-rule-neighbour-{neighbour}", [link.local_device.pk, picked_device.pk], [link.local.pk, picked.pk]
    )
    _pick(client, link.local_device, row_id, picked)

    record, html = _render(client, link.local_device)
    response = _sync(client, link.local_device, _sync_data(record))

    picked.refresh_from_db()
    pill = _actions_cell(html)
    texts = " ".join(_messages(response))
    assert record["netbox_remote_interface_id"] == picked.pk
    assert "Not synced: an interface rule refuses a port on an interface or device you cannot view" in pill
    assert picked.cable_id is None
    assert "Skipped (the interface rules refuse a port): Te1/1 (an interface rule refuses a port" in texts
    for secret in ("secret-if", "port 500", str(rule)):
        assert secret not in pill
        assert secret not in texts
    for secret in (reverse("dcim:device", args=[hidden_neighbour.pk]),):
        assert secret not in html
        assert secret not in response.content.decode()


def _cable_render_queries(client, tag, count, first_port):
    """Seed *count* rows whose local port a global rule refuses, render the cable tab, and count queries."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    local_device = map_device_to_librenms(make_device(f"{tag}-local"), first_port, server_key=SERVER_KEY)
    remote_device = make_device(f"{tag}-remote")
    rows = []
    for index in range(count):
        local = _bound(local_device, f"Te1/{index}", first_port + index)
        _bound(remote_device, f"Gi0/{index}", first_port + 500 + index)
        row = _row(local, remote_device, local_port_id=first_port + index, remote_port=f"Gi0/{index}")
        # The remote port is named by its record (remote_port_key) only, so the existing interface
        # catalog, which chunks advertised ids by 32, reads both sizes in one chunk.
        row.update(remote_port_id=None, remote_port_key=first_port + 500 + index, link_id=index)
        row["local_port_record"] = port_record(_port(first_port + index, local.name))
        row["remote_port_record"] = port_record(_port(first_port + 500 + index, f"Gi0/{index}"))
        rows.append(row)
    raw = assign_cable_row_ids([{key: value for key, value in row.items() if key in _RAW_LINK_KEYS} for row in rows])
    cache.set(sync_snapshot_key(local_device, "links", SERVER_KEY), {"links": raw, "snapshot_token": tag}, timeout=300)
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": local_device.pk, "tab": "cables"},
    )
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url, {"server_key": SERVER_KEY})
    assert response.status_code == 200
    assert unescape(response.content.decode()).count("Not synced: LibreNMS port") == count
    # NetBox reloads its config revision on its own schedule; that is not part of the table.
    return sum("core_configrevision" not in query["sql"] for query in queries.captured_queries)


@pytest.mark.django_db
def test_the_cable_table_decides_blocking_rows_in_a_fixed_number_of_queries(settings, librenms_server):
    """The rule check adds the same queries for 2 blocking rows as for 20 (no query per row)."""
    from dcim.models import Device, Interface
    from django.test import Client

    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
    InterfaceTypeMapping.objects.create(action=IGNORE, name_pattern="^Te")
    user = make_user_with_perms(
        "cable-rule-queries-user",
        [
            ("view", Device),
            ("view", Interface),
            ("change", Interface),
            ("view", Cable),
            ("add", Cable),
            ("change", Cable),
        ],
    )
    client = Client()
    client.force_login(user)
    _cable_render_queries(client, "cable-rule-queries-warm", 2, 1000)

    two = _cable_render_queries(client, "cable-rule-queries-2", 2, 2000)
    twenty = _cable_render_queries(client, "cable-rule-queries-20", 20, 3000)

    print(f"Cable table queries: N=2 -> {two}, N=20 -> {twenty}")
    assert twenty == two


@pytest.mark.django_db
def test_a_refusal_does_not_name_a_port_the_caller_cannot_view(link):
    """
    Te1/1 is cabled to C, whose device and interface the caller cannot view. C is bound to port 777,
    which a rule ignores. The replacement is refused, and the refusal names nothing about C.
    """
    from dcim.models import Device, Interface
    from django.test import Client

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    hidden_device = make_device("cable-rule-hidden-device")
    hidden = _bound(hidden_device, "hidden-if", 777)
    old = cable_together(link.local, hidden)
    link.server.register("/api/v0/ports/777", {"status": "ok", "port": [_port(777, "hidden-port-name")]})
    rule = link.ignore(None, "^hidden-port")
    row_id = _seed(link.local_device, _row(link.local, link.remote_device))
    user = make_user_with_perms("cable-rule-restricted", [("view", Cable), ("add", Cable), ("change", Cable)])
    for action in ("view", "change"):
        user = grant(user, action, Device, constraints={"pk__in": [link.local_device.pk, link.remote_device.pk]})
        user = grant(user, action, Interface, constraints={"pk__in": [link.local.pk, link.remote.pk]})
    client = Client()
    client.force_login(user)
    data = {
        "select": row_id,
        "server_key": SERVER_KEY,
        f"expected_local_id_{row_id}": link.local.pk,
        f"expected_local_device_id_{row_id}": link.local_device.pk,
        f"expected_remote_id_{row_id}": link.remote.pk,
        f"expected_remote_device_id_{row_id}": link.remote_device.pk,
    }

    response = _sync(client, link.local_device, data)

    link.local.refresh_from_db()
    texts = _messages(response)
    body = response.content.decode() + " ".join(texts)
    assert link.local.cable_id == old.pk
    assert any("Skipped (the interface rules refuse a port): Te1/1 (" in text for text in texts)
    for secret in (
        "hidden-port-name",
        "port 777",
        str(rule),
        hidden_device.name,
        reverse("dcim:interface", args=[hidden.pk]),
        reverse("dcim:device", args=[hidden_device.pk]),
    ):
        assert secret not in body


class _ChangesBeforeTheLock(SyncCablesView):
    """The real sync view, with a concurrent change after the pre-lock reads and before the lock."""

    def __init__(self, *, change, **kwargs):
        super().__init__(**kwargs)
        self.change = change
        self.steps = []

    def _fetch_cable_port_records(self, link_data, terminations):
        self.steps.append("prefetch")
        return super()._fetch_cable_port_records(link_data, terminations)

    def _lock_cable_terminations(self, local_term, remote_term, **kwargs):
        # The endpoints and the far ends are already read; the lock re-reads them.
        bindings = [get_librenms_device_id(term, SERVER_KEY, auto_save=False) for term in (local_term, remote_term)]
        self.steps.append(("pre-lock bindings", *bindings))
        self.change()
        return super()._lock_cable_terminations(local_term, remote_term, **kwargs)


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["platform-to-ignored", "binding-to-unknown", "platform-to-allowed"])
def test_the_gate_decides_with_the_binding_and_platform_read_under_the_lock(client, link, change):
    """The binding and the platform change between the pre-lock read and the lock; the locked value decides."""
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, post

    ignoring, plain = _platform("cable-rule-ignoring"), _platform("cable-rule-plain")
    moved_rule = link.ignore(ignoring, "^Gi0/1$")
    # An Ignore rule can apply on the remote platform; before the change it ignores Gi0/1 only in
    # the "platform-to-allowed" case.
    link.ignore(link.remote_platform, "^Gi0/1$" if change == "platform-to-allowed" else "^no-such-port$")
    _seed(link.local_device, _row(link.local, link.remote_device))
    record, _html = _render(client, link.local_device)

    def change_state():
        if change == "binding-to-unknown":
            set_librenms_device_id(link.remote, 888, SERVER_KEY)
            link.remote.save()
        else:
            moved_to = ignoring if change == "platform-to-ignored" else plain
            type(link.remote_device).objects.filter(pk=link.remote_device.pk).update(platform=moved_to)

    view = _ChangesBeforeTheLock(change=change_state)
    request = make_request("post", _sync_data(record))
    post(view, request, pk=link.local_device.pk)

    texts = [str(message) for message in get_messages(request)]
    # The pre-lock read saw the old binding: port 500 on the remote end.
    assert view.steps == ["prefetch", ("pre-lock bindings", 100, 500)]
    if change == "platform-to-allowed":
        assert link.cables() != set()
        assert not any("refuse a port" in text for text in texts)
    elif change == "platform-to-ignored":
        assert link.cables() == set()
        assert any(f"LibreNMS port 500 (Gi0/1): ignored by interface rule {moved_rule.pk}" in text for text in texts)
    else:
        assert link.cables() == set()
        assert any(
            "LibreNMS port 888: no LibreNMS port record is cached for it; refresh the data" in text for text in texts
        )


@pytest.mark.django_db
def test_a_manual_pick_onto_another_device_keeps_the_advertised_ports_platform(client, link):
    """The advertised port 500 belongs to the neighbour on its platform; picking a device on another platform does not change that."""
    rule = link.ignore(link.remote_platform, "^Gi0/1$")
    other = make_device("cable-rule-picked-device")
    other.platform = _platform("cable-rule-picked")
    other.save()
    picked = make_interface(other, "Eth9", iface_type="10gbase-x-sfpp")
    row_id = _seed(link.local_device, _row(link.local, link.remote_device))
    _pick(client, link.local_device, row_id, picked)

    record, html = _render(client, link.local_device)
    response = _sync(client, link.local_device, _sync_data(record))

    reason = f"LibreNMS port 500 (Gi0/1): ignored by interface rule {rule.pk}"
    assert record["netbox_remote_interface_id"] == picked.pk
    assert f"Not synced: {reason}" in html
    assert "Sync Cable" not in html
    link.local.refresh_from_db()
    picked.refresh_from_db()
    assert link.local.cable_id is None and picked.cable_id is None
    assert _refused(response, "500 (Gi0/1)", f"ignored by interface rule {rule.pk}")


@pytest.mark.django_db
@pytest.mark.parametrize("rule", ["global-advertised", "picked-platform-advertised", "picked-platform-bound"])
def test_a_manual_pick_for_a_neighbour_not_in_netbox(client, link, rule):
    """
    No NetBox device owns the advertised port 500, so it is decided with no platform: only a global
    rule matches it. The picked interface's own bound port 509 is decided with B's platform.
    """
    # The neighbour is not in NetBox, so nothing is bound to its port 500 either.
    link.remote.delete()
    picked_platform = _platform("cable-rule-picked-q")
    other = make_device("cable-rule-picked-b")
    other.platform = picked_platform
    other.save()
    picked = _bound(other, "Eth9", 509)
    link.server.register("/api/v0/ports/509", {"status": "ok", "port": [_port(509, "Eth9")]})
    ignore = {
        "global-advertised": (None, "^Gi0/1$"),
        "picked-platform-advertised": (picked_platform, "^Gi0/1$"),
        "picked-platform-bound": (picked_platform, "^Eth9$"),
    }[rule]
    ignore_rule = link.ignore(*ignore)
    row = _row(link.local, link.remote_device)
    row["remote_device"] = "not-in-netbox"
    row_id = _seed(link.local_device, row)
    _pick(client, link.local_device, row_id, picked)

    record, html = _render(client, link.local_device)
    response = _sync(client, link.local_device, _sync_data(record))

    picked.refresh_from_db()
    assert record["netbox_remote_interface_id"] == picked.pk
    reason = f"ignored by interface rule {ignore_rule.pk}"
    if rule == "global-advertised":
        # The port's owner did not resolve, so the refusal names nothing about it.
        assert f"Not synced: {HIDDEN_PORT_REASON}" in html
        assert picked.cable_id is None
        assert any(f"Te1/1 ({HIDDEN_PORT_REASON})" in text for text in _messages(response))
    elif rule == "picked-platform-advertised":
        # A platform-scoped rule cannot match a port whose owner is unknown.
        assert "Sync Cable" in html
        assert picked.cable_id is not None
    else:
        # The table has no record for 509; the sync fetches it and refuses.
        assert picked.cable_id is None
        assert _refused(response, "509 (Eth9)", reason)


@pytest.mark.django_db
@pytest.mark.parametrize("may_ignore", [True, False], ids=["ignore-rule-on-platform", "ignore-rule-elsewhere"])
def test_a_missing_port_record_refuses_only_when_an_ignore_rule_could_apply(client, link, may_ignore):
    """The snapshot has no record for port 100, and LibreNMS returns none for it either."""
    link.ignore(link.local_platform if may_ignore else _platform("cable-rule-elsewhere"), "^no-such-port$")
    row = _row(link.local, link.remote_device)
    del row["local_port_record"]
    _seed(link.local_device, row)
    record, _html = _render(client, link.local_device)

    response = _sync(client, link.local_device, _sync_data(record))

    assert "/api/v0/ports/100" in [request["path"] for request in link.server.requests]
    if may_ignore:
        assert link.cables() == set()
        assert _refused(response, "100", "no LibreNMS port record is cached for it; refresh the data")
    else:
        link.local.refresh_from_db()
        assert link.local.cable is not None and link.local.cable.b_terminations == [link.remote]


@pytest.mark.django_db
def test_an_ambiguous_port_is_cabled(client, link):
    """Two equal-rank Set type rules block an interface write, but a cable writes no type."""
    for netbox_type in ("10gbase-x-sfpp", "other"):
        InterfaceTypeMapping.objects.create(
            platform=link.local_platform, name_pattern=f"^Te(?#{netbox_type})", netbox_type=netbox_type
        )
    _seed(link.local_device, _row(link.local, link.remote_device))
    record, _html = _render(client, link.local_device)

    _sync(client, link.local_device, _sync_data(record))

    link.local.refresh_from_db()
    assert link.local.cable is not None and link.local.cable.b_terminations == [link.remote]


@pytest.mark.django_db
def test_the_far_end_create_is_not_offered_when_the_local_port_is_ignored(client, link):
    rule = link.ignore(link.local_platform, "^Te")
    link.remote.delete()
    row_id = _seed(link.local_device, _row(link.local, link.remote_device))
    record, html = _render(client, link.local_device)

    offer = client.get(
        reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[link.local_device.pk]),
        {"row_id": row_id, "server_key": SERVER_KEY},
    )

    assert "remote_create_url" not in record
    assert "Create the remote interface" not in html
    assert offer.status_code == 409
    assert f"LibreNMS port 100 (Te1/1): ignored by interface rule {rule.pk}" in offer.content.decode()
