"""
The member-count badge on an aggregate row (issue #179 item 9).

The relationship column has only ever pointed upward: a row says what it is attached to. An
aggregate row said nothing about what is attached to *it*, so a paginated table could not answer
"are ae0's four members here". The downward view is one inversion of the three edge maps the
snapshot already carries, so the count is complete even when no member is on the page, and the
table render and the single-row verify read the same definition.
"""

import json

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface
from netbox_librenms_plugin.utils import set_librenms_device_id

SERVER_KEY = "default"


def _port(port_id, name, if_type="ethernetCsmacd"):
    """Return the LibreNMS port row shape every render path reads."""
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifAlias": "",
        "ifType": if_type,
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "ifPhysAddress": "",
        "_source": "host",
    }


# One aggregate, two members, one unrelated port. ae0.0 is a sub-interface of the aggregate, so the
# same device exercises the LAG and the parent inversion at once.
PORTS = [
    _port(10, "ae0", "ieee8023adLag"),
    _port(11, "ge-0/0/0"),
    _port(12, "ge-0/0/1"),
    _port(13, "ae0.0", "l2vlan"),
    _port(14, "ge-0/0/2"),
]

RELATIONSHIPS = {
    "lag_members": {11: 10, 12: 10},
    "sub_interfaces": {13: 10},
    "bridge_members": {},
}


def _cached_data(relationships=None, ports=None):
    """Return the cached LibreNMS snapshot shape the table and the verify view both read."""
    return {
        "ports": [dict(port) for port in (ports if ports is not None else PORTS)],
        "port_stack_relationships": relationships if relationships is not None else RELATIONSHIPS,
    }


def _enriched_rows(cached_data=None, interface_name_field="ifName"):
    """Enrich every row through the production relationship path, with no NetBox interfaces bound."""
    from netbox_librenms_plugin.interface_relationships import (
        build_relationship_maps,
        enrich_port_relationships,
    )

    cached_data = cached_data or _cached_data()
    maps = build_relationship_maps(cached_data)
    rows = cached_data["ports"]
    for row in rows:
        row["netbox_interface"] = None
        row["exists_in_netbox"] = False
        enrich_port_relationships(row, maps, interface_name_field, SERVER_KEY)
    return rows


def _render_tab(device, rows, *, per_page=None, table_class=None):
    """Render the real interface sync tab around a table built from *rows*."""
    from django.template.loader import render_to_string
    from django.test import RequestFactory
    from django_tables2 import RequestConfig

    from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable
    from netbox_librenms_plugin.tests.view_test_helpers import make_superuser

    request = RequestFactory().get("/")
    request.user = make_superuser(f"member-badge-{device.name}")
    table = (table_class or LibreNMSInterfaceTable)(rows, device=device, server_key=SERVER_KEY)
    table.migrated_to_marker = False
    RequestConfig(request, paginate={"per_page": per_page} if per_page else False).configure(table)
    context = {
        "interface_sync": {
            "object": device,
            "table": table,
            "server_key": SERVER_KEY,
            "netbox_only_interfaces": [],
            "virtual_chassis_members": [],
            "cache_expiry": None,
            "oob_incomplete": False,
            "relationship_data_incomplete": False,
        },
        "interface_name_field": "ifName",
        "migrated_to_marker": False,
        "has_write_permission": True,
    }
    return render_to_string("netbox_librenms_plugin/_interface_sync_content.html", context, request=request)


class TestTheInversion:
    """One inversion serves LAG, parent and bridge, and it is the only place the direction flips."""

    def test_it_groups_members_under_their_aggregate(self):
        from netbox_librenms_plugin.utils import invert_relationship_edges

        assert invert_relationship_edges({11: 10, 12: 10, 13: 20}) == {10: [11, 12], 20: [13]}

    def test_it_orders_members_deterministically(self):
        """The badge tooltip lists members in this order, so two renders cannot disagree."""
        from netbox_librenms_plugin.utils import invert_relationship_edges

        assert invert_relationship_edges({30: 10, 11: 10, 21: 10}) == {10: [11, 21, 30]}

    def test_an_empty_map_inverts_to_an_empty_map(self):
        from netbox_librenms_plugin.utils import invert_relationship_edges

        assert invert_relationship_edges({}) == {}


@pytest.mark.django_db
class TestASelfEdgeCountsForNothing:
    """
    A port is not its own member.

    The resolver cannot emit a self-edge: it skips a port-stack pair whose ends resolve to one
    physical port. A cached map can still hold one, which is why the inversion drops it. The drop
    stays in the inversion rather than in ``normalize_relationship_maps``: the relationship writer
    reads that same map and refuses a self-LAG through NetBox's own ``clean()``, and filtering the
    map upstream would leave that refusal untested.
    """

    def test_the_inversion_drops_it(self):
        from netbox_librenms_plugin.utils import invert_relationship_edges

        assert invert_relationship_edges({10: 10, 11: 10}) == {10: [11]}

    def test_an_aggregate_does_not_count_itself(self):
        cached = _cached_data({"lag_members": {10: 10, 11: 10}, "sub_interfaces": {}, "bridge_members": {}})
        rows = {row["port_id"]: row for row in _enriched_rows(cached)}

        assert rows[10]["librenms_lag_member_names"] == ["ge-0/0/0"]

    def test_the_writer_still_sees_the_edge(self):
        """The upward map is untouched, so the self-LAG refusal keeps running where it is tested."""
        from netbox_librenms_plugin.utils import normalize_relationship_maps

        lag_members, _sub_interfaces, _bridge_members = normalize_relationship_maps(
            {"lag_members": {10: 10, 11: 10}, "sub_interfaces": {}, "bridge_members": {}}
        )

        assert lag_members == {10: 10, 11: 10}


@pytest.mark.django_db
class TestTheRowCarriesItsMembers:
    """Enrichment writes the downward view onto the row, beside the upward one it already wrote."""

    def test_an_aggregate_row_lists_its_lag_members(self):
        rows = {row["port_id"]: row for row in _enriched_rows()}

        assert rows[10]["librenms_lag_member_names"] == ["ge-0/0/0", "ge-0/0/1"]

    def test_an_aggregate_row_lists_its_sub_interfaces(self):
        rows = {row["port_id"]: row for row in _enriched_rows()}

        assert rows[10]["librenms_sub_interface_names"] == ["ae0.0"]

    def test_a_member_row_lists_no_members(self):
        rows = {row["port_id"]: row for row in _enriched_rows()}

        assert rows[11]["librenms_lag_member_names"] == []
        assert rows[11]["librenms_sub_interface_names"] == []

    def test_a_bridge_row_lists_its_bridged_ports(self):
        cached = _cached_data({"lag_members": {}, "sub_interfaces": {}, "bridge_members": {11: 10, 12: 10}})
        rows = {row["port_id"]: row for row in _enriched_rows(cached)}

        assert rows[10]["librenms_bridge_member_names"] == ["ge-0/0/0", "ge-0/0/1"]

    def test_a_member_missing_from_the_snapshot_is_still_counted(self):
        """A trimmed snapshot must not shrink the count: the tooltip names the port id instead."""
        cached = _cached_data({"lag_members": {11: 10, 99: 10}, "sub_interfaces": {}, "bridge_members": {}})
        rows = {row["port_id"]: row for row in _enriched_rows(cached)}

        assert rows[10]["librenms_lag_member_names"] == ["ge-0/0/0", "port 99"]

    def test_a_non_string_member_name_falls_back_to_the_port_id(self):
        """LibreNMS copies ifName unvalidated, and joining a non-string would 500 the whole table."""
        cached = _cached_data({"lag_members": {11: 10}, "sub_interfaces": {}, "bridge_members": {}})
        cached["ports"][1]["ifName"] = 12345
        rows = {row["port_id"]: row for row in _enriched_rows(cached)}

        assert rows[10]["librenms_lag_member_names"] == ["port 11"]

    def test_the_names_follow_the_active_name_field(self):
        cached = _cached_data()
        cached["ports"][1]["ifDescr"] = "xe-0/0/0"
        rows = {row["port_id"]: row for row in _enriched_rows(cached, interface_name_field="ifDescr")}

        assert rows[10]["librenms_lag_member_names"] == ["xe-0/0/0", "ge-0/0/1"]


@pytest.mark.django_db
class TestTheBadgeRenders:
    """The rendered tab badges the aggregate rows and nothing else."""

    def test_an_aggregate_row_shows_its_member_count(self):
        device = make_device("member-badge-count")

        html = _render_tab(device, _enriched_rows())

        assert "</i>2 members</span>" in html
        assert "</i>1 sub-interface</span>" in html

    def test_the_tooltip_names_the_members_and_escapes_them(self):
        """The names come from LibreNMS, so assert the title attribute itself, not the page text."""
        device = make_device("member-badge-tooltip")
        cached = _cached_data({"lag_members": {11: 10, 12: 10}, "sub_interfaces": {}, "bridge_members": {}})
        cached["ports"][1]["ifName"] = 'a"<b>x</b>'

        html = _render_tab(device, _enriched_rows(cached))

        assert 'title="In LibreNMS: a&quot;&lt;b&gt;x&lt;/b&gt;, ge-0/0/1"' in html
        assert "<b>x</b>" not in html

    def test_a_row_with_no_members_shows_no_badge(self):
        device = make_device("member-badge-none")
        rows = [row for row in _enriched_rows() if row["port_id"] == 14]

        html = _render_tab(device, rows)

        assert "mdi-vector-combine" not in html
        assert "mdi-file-tree" not in html

    def test_the_count_survives_pagination(self):
        """The members are on another page; the count comes from the snapshot, not from the page."""
        device = make_device("member-badge-paginated")

        html = _render_tab(device, _enriched_rows(), per_page=1)

        assert "ae0" in html, "the aggregate is the only row on page 1"
        assert "</i>2 members</span>" in html

    def test_one_member_reads_as_singular(self):
        device = make_device("member-badge-singular")
        cached = _cached_data({"lag_members": {11: 10}, "sub_interfaces": {}, "bridge_members": {}})

        html = _render_tab(device, _enriched_rows(cached))

        assert "</i>1 member</span>" in html

    def test_a_long_member_list_is_truncated_in_the_tooltip(self):
        """A Linux bridge can hold dozens of ports; an unbounded title attribute is unreadable."""
        device = make_device("member-badge-truncated")
        ports = [_port(10, "br0")] + [_port(pid, f"eth{pid}") for pid in range(20, 50)]
        cached = _cached_data(
            {"lag_members": {}, "sub_interfaces": {}, "bridge_members": {pid: 10 for pid in range(20, 50)}},
            ports=ports,
        )

        html = _render_tab(device, _enriched_rows(cached))

        assert "</i>30 bridged ports</span>" in html
        title = html.split('title="In LibreNMS: ', 1)[1].split('"', 1)[0]
        assert title.endswith(", +15 more")
        assert title.count(", ") == 15, "fifteen names, then the summary"
        assert "eth34," in title and "eth35" not in title, "the sixteenth name is not listed"

    def test_a_vm_table_renders_no_lag_member_badge(self):
        """VMInterface has no lag field, so the VM table renders no LAG line in either direction."""
        from netbox_librenms_plugin.tables.interfaces import LibreNMSVMInterfaceTable

        device = make_device("member-badge-vm")
        cached = _cached_data({"lag_members": {11: 10, 12: 10}, "sub_interfaces": {13: 10}, "bridge_members": {}})

        html = _render_tab(device, _enriched_rows(cached), table_class=LibreNMSVMInterfaceTable)

        assert "mdi-vector-combine" not in html
        assert "</i>1 sub-interface</span>" in html, "parent relationships are still supported on a VM"


@pytest.mark.django_db
class TestTheRealTabRendersIt:
    """
    The whole interfaces tab, through the real view.

    Every other render case in this file enriches the rows itself, so it would stay green if the
    view stopped enriching at all. This one seeds only the cache and asks the device's sync page
    for the interfaces tab, so it fails if any link in the production chain drops the badge.
    """

    def test_the_interfaces_tab_badges_the_aggregate(self):
        from django.core.cache import cache
        from django.test import Client

        from netbox_librenms_plugin.tests.view_test_helpers import make_superuser
        from netbox_librenms_plugin.views.mixins import CacheMixin

        device = make_device("member-badge-e2e")
        interface = make_interface(device, "ae0", iface_type="lag")
        set_librenms_device_id(interface, 10, SERVER_KEY)
        interface.save()
        set_librenms_device_id(device, 4242, SERVER_KEY)
        device.save()
        cache.set(CacheMixin().get_cache_key(device, "ports", SERVER_KEY), _cached_data(), 300)

        client = Client()
        client.force_login(make_superuser("member-badge-e2e-user"))
        response = client.get(f"/dcim/devices/{device.pk}/librenms-sync/", {"tab": "interfaces"})

        assert response.status_code == 200
        html = response.content.decode()
        assert "</i>2 members</span>" in html
        assert "</i>1 sub-interface</span>" in html


@pytest.mark.django_db
class TestTheVerifyPathAgrees:
    """A single-row verify re-renders the same cell, so it must re-derive the same badge."""

    def test_an_inline_verify_keeps_the_member_badge(self):
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser
        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        device = make_device("member-badge-verify")
        interface = make_interface(device, "ae0")
        set_librenms_device_id(interface, 10, SERVER_KEY)
        interface.save()

        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = SERVER_KEY
        view._librenms_api = api
        cache.set(view.get_cache_key(device, "ports", SERVER_KEY), _cached_data())

        request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": device.pk,
                    "interface_name": "ae0",
                    "interface_name_field": "ifName",
                    "port_id": 10,
                }
            ),
            user=make_superuser("member-badge-verify-user"),
            path="/verify/",
            content_type="application/json",
        )
        view.setup(request)
        response = view.post(request)

        assert response.status_code == 200
        parent_cell = json.loads(response.content)["formatted_row"]["parent"]
        assert "</i>2 members</span>" in parent_cell
        assert "</i>1 sub-interface</span>" in parent_cell
