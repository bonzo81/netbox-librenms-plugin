"""
The row-level sync diff, and the per-row Sync button that reads it (issue #179 item 6).

"Out of sync" is answered in one place, so the coloured columns and the button cannot disagree,
and neither can disagree with the writer. The first class is the test that keeps that promise: it
runs the real writer against every row shape and asserts the row state predicted what it did.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface

SERVER_KEY = "default"


def _port(**overrides):
    """Return a LibreNMS port row that a fully synced interface matches field for field."""
    port = {
        "port_id": 42,
        "ifName": "Ethernet1",
        "ifDescr": "Ethernet1",
        "ifAlias": "Uplink",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "ifPhysAddress": "AA:BB:CC:DD:EE:FF",
    }
    port.update(overrides)
    return port


def _mapping():
    """Create the ifType mapping the synced pair's type is written from."""
    from netbox_librenms_plugin.models import InterfaceTypeMapping

    return InterfaceTypeMapping.objects.get_or_create(
        librenms_type="ethernetCsmacd",
        librenms_speed=1_000_000,
        defaults={"netbox_type": "1000base-t"},
    )[0]


def _synced_interface(tag, **overrides):
    """Return a device and an interface that a sync of :func:`_port` would leave untouched."""
    from dcim.models import MACAddress

    from netbox_librenms_plugin.utils import set_librenms_device_id

    _mapping()
    device = make_device(tag)
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    interface.speed = 1_000_000
    interface.mtu = 1500
    interface.enabled = True
    interface.description = "Uplink"
    for field, value in overrides.items():
        setattr(interface, field, value)
    set_librenms_device_id(interface, 42, SERVER_KEY)
    interface.save()
    mac = MACAddress.objects.create(mac_address="AA:BB:CC:DD:EE:FF")
    interface.mac_addresses.add(mac)
    interface.primary_mac_address = mac
    interface.save()
    return device, interface


def _row(interface=None, **overrides):
    """Return a table row bound to *interface* (or to nothing, for a NetBox-absent row)."""
    row = _port(**overrides)
    row["netbox_interface"] = interface
    row["exists_in_netbox"] = interface is not None
    return row


def _state(row, *, vlan_context=None):
    """Compute one row's sync state the way the table does."""
    from netbox_librenms_plugin.interface_diff import compute_row_sync_state
    from netbox_librenms_plugin.interface_sync import get_netbox_interface_type

    return compute_row_sync_state(
        row,
        interface_name_field="ifName",
        server_key=SERVER_KEY,
        netbox_type=get_netbox_interface_type(row),
        vlan_context=vlan_context,
    )


# The shapes a row can take, and whether a sync of that row writes anything. Each case names the
# rule it pins; the parity test below proves the diff and the writer answer it the same way.
_ROW_SHAPES = [
    ("already in sync", {}, {}, False),
    ("the name differs", {}, {"name": "OtherName"}, True),
    ("the description differs", {}, {"description": "Something else"}, True),
    ("the mtu differs", {}, {"mtu": 9000}, True),
    ("the enabled state differs", {}, {"enabled": False}, True),
    ("the speed differs", {}, {"speed": 10_000_000}, True),
    ("the mapped type differs", {}, {"type": "virtual"}, True),
    # An ifType with no mapping is no opinion: it only fills a type-less interface, so a typed
    # one is not a difference. The table paints the column red anyway, to name the missing
    # mapping, but that must not put the row itself into "differs".
    ("an unmapped ifType leaves a typed interface alone", {"ifType": "someUnmappedType"}, {}, False),
    # LibreNMS omits ifAdminStatus for a port it cannot poll administratively. The writer treats
    # that as enabled; the table read it as disabled, so such a row differed forever.
    ("an absent ifAdminStatus reads as enabled", {"ifAdminStatus": None}, {}, False),
    (
        "an absent ifAdminStatus still differs from a disabled interface",
        {"ifAdminStatus": None},
        {"enabled": False},
        True,
    ),
    # The writer coerces ifMtu before storing it, so the comparison has to coerce it too.
    ("a string ifMtu is coerced before comparing", {"ifMtu": "1500"}, {}, False),
    ("an out-of-range ifMtu is not written", {"ifMtu": 10**9}, {"mtu": None}, False),
    # An alias echoing either canonical name is not a description.
    ("an ifAlias echoing ifName is not a description", {"ifAlias": "Ethernet1"}, {"description": ""}, False),
    ("a non-string ifAlias clears the description", {"ifAlias": 7}, {"description": ""}, False),
    # A MAC the macaddr column refuses is skipped by the writer, so it is not a difference.
    ("an unusable MAC is skipped", {"ifPhysAddress": "unknown"}, {}, False),
    ("a blank MAC is skipped", {"ifPhysAddress": ""}, {}, False),
]


@pytest.mark.django_db
class TestTheDiffMatchesTheWriter:
    """The row state must predict exactly what a sync of that row writes."""

    @pytest.mark.parametrize(
        ("label", "port_overrides", "interface_overrides", "writes"),
        [pytest.param(*case, id=case[0]) for case in _ROW_SHAPES],
    )
    def test_the_row_state_predicts_whether_a_sync_writes(self, label, port_overrides, interface_overrides, writes):
        from netbox_librenms_plugin.interface_diff import ROW_DIFFERS
        from netbox_librenms_plugin.interface_sync import get_netbox_interface_type, update_interface_from_port

        slug = label.replace(" ", "-")[:40]
        _device, interface = _synced_interface(f"diff-{abs(hash(slug)) % 10000}", **interface_overrides)
        row = _row(interface, **port_overrides)

        # vlan_context is left out so the row state covers exactly the fields this writer owns.
        predicted = _state(row).state == ROW_DIFFERS

        changed = update_interface_from_port(
            interface,
            row,
            server_key=SERVER_KEY,
            interface_name_field="ifName",
            netbox_type=get_netbox_interface_type(row),
        )

        assert predicted == changed == writes, f"{label}: predicted={predicted} actual={changed}"

    def test_a_row_missing_from_netbox_is_absent_not_differing(self):
        from netbox_librenms_plugin.interface_diff import ABSENT, ROW_ABSENT, SYNC_FIELDS

        state = _state(_row(None))

        assert state.state == ROW_ABSENT
        assert state.differing_fields == ()
        assert {state.verdict(field) for field in SYNC_FIELDS} == {ABSENT}

    def test_the_differing_fields_name_only_what_changed(self):
        _device, interface = _synced_interface("diff-fields", mtu=9000, description="Stale")

        assert _state(_row(interface)).differing_fields == ("description", "mtu")

    def test_a_mac_attached_but_not_primary_still_differs(self):
        """assign_interface_mac also makes the MAC primary, so attaching it alone is not in sync."""
        _device, interface = _synced_interface("diff-mac-primary")
        interface.primary_mac_address = None
        interface.save()

        assert _state(_row(interface)).differing_fields == ("mac_address",)

    def test_a_vm_row_ignores_the_fields_a_vminterface_has_no_column_for(self):
        """VMInterface carries neither type nor speed, so a sync cannot change them."""
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.interface_diff import MATCHES
        from netbox_librenms_plugin.tests.conftest import make_cluster, make_vm

        _mapping()
        vm = make_vm("row-diff-vm", make_cluster("row-diff-cluster"))
        interface = VMInterface.objects.create(virtual_machine=vm, name="Ethernet1", mtu=1500, enabled=True)
        row = _row(interface, ifPhysAddress="", port_id=None)

        state = _state(row)

        assert state.verdict("type") == MATCHES
        assert state.verdict("speed") == MATCHES
        assert "type" not in state.differing_fields
        assert "speed" not in state.differing_fields

    def test_an_interface_with_no_stored_id_differs_on_librenms_id(self):
        _device, interface = _synced_interface("diff-no-id")
        interface.custom_field_data["librenms_id"] = None
        interface.save()

        assert _state(_row(interface)).differing_fields == ("librenms_id",)


@pytest.mark.django_db
class TestTheVlanVerdict:
    """The VLAN part of the row state follows the assignment a sync would write."""

    def _vlan_context(self, row):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        return LibreNMSInterfaceTable._vlan_row_context(row)

    def _verdict(self, row):
        return _state(row, vlan_context=self._vlan_context(row)).verdict("vlans")

    def test_a_row_reporting_no_vlans_matches_a_modeless_interface(self):
        from netbox_librenms_plugin.interface_diff import MATCHES

        _device, interface = _synced_interface("vlan-none")

        assert self._verdict(_row(interface)) == MATCHES

    def test_a_reported_vlan_against_no_assignment_differs(self):
        from netbox_librenms_plugin.interface_diff import DIFFERS

        _device, interface = _synced_interface("vlan-untagged")

        assert self._verdict(_row(interface, untagged_vlan=100, tagged_vlans=[])) == DIFFERS

    def test_a_matching_untagged_assignment_matches(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.interface_diff import MATCHES

        _device, interface = _synced_interface("vlan-match")
        group = VLANGroup.objects.create(name="Row Diff Group", slug="row-diff-group")
        vlan = VLAN.objects.create(vid=100, name="Row Diff 100", group=group, status="active")
        interface.mode = "access"
        interface.untagged_vlan = vlan
        interface.save()

        row = _row(interface, untagged_vlan=100, tagged_vlans=[])
        row["vlan_group_map"] = {100: {"group_id": str(group.pk)}}

        assert self._verdict(row) == MATCHES

    def test_a_tagged_vlan_netbox_holds_but_librenms_does_not_report_differs(self):
        """A sync clears it, so the row is not in sync even though every reported VLAN matches.

        The mode and the reported VLAN both match here on purpose: the extra VLAN is the only
        difference left, so the assertion cannot pass on some other rule.
        """
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.interface_diff import DIFFERS

        _device, interface = _synced_interface("vlan-extra")
        group = VLANGroup.objects.create(name="Row Diff Extra", slug="row-diff-extra")
        reported = VLAN.objects.create(vid=100, name="Row Diff 100", group=group, status="active")
        extra = VLAN.objects.create(vid=999, name="Row Diff 999", group=group, status="active")
        interface.mode = "tagged"
        interface.save()
        interface.tagged_vlans.set([reported, extra])

        row = _row(interface, untagged_vlan=None, tagged_vlans=[100])
        row["vlan_group_map"] = {100: {"group_id": str(group.pk)}}

        assert self._verdict(row) == DIFFERS

    def test_a_different_untagged_vlan_alone_differs(self):
        """The mode already matches here, so only the untagged VID can carry the verdict."""
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.interface_diff import DIFFERS

        _device, interface = _synced_interface("vlan-untagged-swap")
        group = VLANGroup.objects.create(name="Row Diff Swap", slug="row-diff-swap")
        VLAN.objects.create(vid=100, name="Row Diff Swap 100", group=group, status="active")
        assigned = VLAN.objects.create(vid=200, name="Row Diff Swap 200", group=group, status="active")
        interface.mode = "access"
        interface.untagged_vlan = assigned
        interface.save()

        row = _row(interface, untagged_vlan=100, tagged_vlans=[])
        row["vlan_group_map"] = {100: {"group_id": str(group.pk)}}

        assert self._verdict(row) == DIFFERS

    def test_the_same_vid_in_a_different_group_differs(self):
        """Same VID, other group: the sync moves the assignment, so the row is not in sync."""
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.interface_diff import DIFFERS

        _device, interface = _synced_interface("vlan-group-swap")
        assigned_group = VLANGroup.objects.create(name="Row Diff Group A", slug="row-diff-group-a")
        other_group = VLANGroup.objects.create(name="Row Diff Group B", slug="row-diff-group-b")
        assigned = VLAN.objects.create(vid=100, name="Row Diff A 100", group=assigned_group, status="active")
        VLAN.objects.create(vid=100, name="Row Diff B 100", group=other_group, status="active")
        interface.mode = "access"
        interface.untagged_vlan = assigned
        interface.save()

        row = _row(interface, untagged_vlan=100, tagged_vlans=[])
        row["vlan_group_map"] = {100: {"group_id": str(other_group.pk)}}

        assert self._verdict(row) == DIFFERS

    def test_a_tagged_vid_in_a_different_group_differs(self):
        """The mode and the tagged set both match, so only the group can carry the verdict."""
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.interface_diff import DIFFERS

        _device, interface = _synced_interface("vlan-tagged-group")
        assigned_group = VLANGroup.objects.create(name="Row Diff Tag A", slug="row-diff-tag-a")
        other_group = VLANGroup.objects.create(name="Row Diff Tag B", slug="row-diff-tag-b")
        assigned = VLAN.objects.create(vid=100, name="Row Diff Tag A 100", group=assigned_group, status="active")
        VLAN.objects.create(vid=100, name="Row Diff Tag B 100", group=other_group, status="active")
        interface.mode = "tagged"
        interface.save()
        interface.tagged_vlans.set([assigned])

        row = _row(interface, untagged_vlan=None, tagged_vlans=[100])
        row["vlan_group_map"] = {100: {"group_id": str(other_group.pk)}}

        assert self._verdict(row) == DIFFERS

    def test_a_mode_change_alone_differs(self):
        """LibreNMS states the mode in ifTrunk, and a sync writes it even with no VLAN to move."""
        from netbox_librenms_plugin.interface_diff import DIFFERS

        _device, interface = _synced_interface("vlan-mode")
        interface.mode = "tagged"
        interface.save()

        assert self._verdict(_row(interface, mode=None, untagged_vlan=None, tagged_vlans=[])) == DIFFERS


def _render_tab(device, rows, *, migrated=False):
    """Render the real interface sync tab around a table built from *rows*."""
    from django.template.loader import render_to_string
    from django.test import RequestFactory
    from django_tables2 import RequestConfig

    from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable
    from netbox_librenms_plugin.tests.view_test_helpers import make_superuser

    request = RequestFactory().get("/")
    request.user = make_superuser("row-diff-render")
    table = LibreNMSInterfaceTable(rows, device=device, server_key=SERVER_KEY)
    table.migrated_to_marker = migrated
    RequestConfig(request).configure(table)
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
        "migrated_to_marker": migrated,
        "has_write_permission": True,
    }
    return render_to_string("netbox_librenms_plugin/_interface_sync_content.html", context, request=request)


@pytest.mark.django_db
class TestTheButtonFollowsTheRowState:
    """The rendered tab shows a Sync button on exactly the rows a sync would change."""

    def test_only_the_rows_a_sync_would_change_carry_a_button(self):
        _device, in_sync = _synced_interface("btn-in-sync")
        differs_device, differs = _synced_interface("btn-differs", mtu=9000)
        rows = [
            _row(in_sync),
            _row(differs, port_id=43, ifName="Ethernet2"),
            _row(None, port_id=44, ifName="Ethernet3"),
        ]
        # One table, one device: the rows only need a device for the VC member column.
        html = _render_tab(differs_device, rows)

        assert 'name="sync_one" value="43"' in html, "the row a sync would change offers the button"
        assert 'name="sync_one" value="44"' in html, "a row missing from NetBox offers it too"
        assert 'name="sync_one" value="42"' not in html, "an in-sync row must not offer a no-op sync"

    def test_the_button_sits_inside_the_sync_form(self):
        device, differs = _synced_interface("btn-in-form", mtu=9000)

        html = _render_tab(device, [_row(differs)])

        form_start = html.index('<form method="post"')
        assert form_start < html.index('name="sync_one"') < html.index("</form>")

    def test_a_migrated_donor_renders_no_button(self):
        """Migrated mode drops the form, so a submit button there would do nothing."""
        device, differs = _synced_interface("btn-migrated", mtu=9000)

        html = _render_tab(device, [_row(differs)], migrated=True)

        assert 'name="sync_one"' not in html

    def test_a_row_the_user_cannot_write_renders_no_button(self):
        device, differs = _synced_interface("btn-unresolvable", mtu=9000)
        row = _row(differs)
        row["sync_target_resolvable"] = False

        html = _render_tab(device, [row])

        assert 'name="sync_one"' not in html

    def test_the_column_colour_and_the_button_read_the_same_verdict(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        device, differs = _synced_interface("btn-column-agreement", mtu=9000)
        row = _row(differs)
        table = LibreNMSInterfaceTable([row], device=device, server_key=SERVER_KEY)

        assert "text-warning" in str(table.render_mtu(row["ifMtu"], row)), "the differing column is amber"
        assert "text-success" in str(table.render_name(row["ifName"], row)), "a matching column stays green"
        assert 'name="sync_one"' in str(table.render_actions(None, row))


@pytest.mark.django_db
class TestThePerRowButtonSyncsOneRow:
    """A posted sync_one goes through the bulk view, and writes only that row."""

    def _setup(self, tag, settings):
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        server_key = configure_default_librenms_server(settings)
        device = make_device(tag)
        set_librenms_device_id(device, 1, server_key)
        device.save()
        ports = [
            _port(port_id=10, ifName="Ethernet10", ifDescr="Ethernet10", ifAlias="Ten"),
            _port(port_id=11, ifName="Ethernet11", ifDescr="Ethernet11", ifAlias="Eleven"),
            _port(port_id=12, ifName="Ethernet12", ifDescr="Ethernet12", ifAlias="Twelve"),
        ]
        cache_key = SyncInterfacesView().get_cache_key(device, "ports", server_key)
        cache.set(cache_key, {"ports": ports, "port_stack_relationships": {}})
        return device, server_key, cache_key

    def _post(self, device, server_key, data):
        from types import SimpleNamespace

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, post
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        request = make_request(
            "post",
            {"server_key": server_key, **data},
            user=make_superuser(f"row-sync-{device.pk}"),
        )
        request.GET = request.GET.copy()
        request.GET["interface_name_field"] = "ifName"
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key=server_key)
        return post(view, request, object_type="device", object_id=device.pk)

    def test_sync_one_writes_only_its_own_row(self, settings):
        from django.core.cache import cache

        device, server_key, cache_key = self._setup("row-sync-one", settings)
        try:
            response = self._post(device, server_key, {"sync_one": "11"})
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert sorted(device.interfaces.values_list("name", flat=True)) == ["Ethernet11"]

    def test_the_pressed_row_wins_over_the_tick_boxes(self, settings):
        """The browser submits both, and the user pressed one row, not the selection."""
        from django.core.cache import cache

        device, server_key, cache_key = self._setup("row-sync-wins", settings)
        try:
            self._post(device, server_key, {"sync_one": "11", "select": ["10", "12"]})
        finally:
            cache.delete(cache_key)

        assert sorted(device.interfaces.values_list("name", flat=True)) == ["Ethernet11"]

    def test_an_unusable_sync_one_falls_back_to_the_selection(self, settings):
        from django.core.cache import cache

        device, server_key, cache_key = self._setup("row-sync-fallback", settings)
        try:
            self._post(device, server_key, {"sync_one": "", "select": ["10", "12"]})
        finally:
            cache.delete(cache_key)

        assert sorted(device.interfaces.values_list("name", flat=True)) == ["Ethernet10", "Ethernet12"]
