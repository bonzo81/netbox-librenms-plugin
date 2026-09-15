"""Integration coverage for bridge and parent relationships from LibreNMS port stacks."""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface
from netbox_librenms_plugin.tests.view_test_helpers import make_request, post
from netbox_librenms_plugin.utils import normalize_relationship_maps

pytestmark = pytest.mark.django_db


LINUX_PORTS = [
    {"port_id": 100, "ifName": "vmbr0", "ifDescr": "vmbr0", "ifType": "ethernetCsmacd"},
    {"port_id": 101, "ifName": "bond0", "ifDescr": "bond0", "ifType": "ieee8023adLag"},
    {"port_id": 102, "ifName": "bond0.110", "ifDescr": "bond0.110", "ifType": "ethernetCsmacd"},
    {"port_id": 103, "ifName": "nic0", "ifDescr": "nic0", "ifType": "ethernetCsmacd"},
]


def test_bridge_members_are_independent_from_parent_relationships(mock_librenms_api):
    """Keep a sub-interface parent when the same interface also joins a bridge."""
    relationships = mock_librenms_api.resolve_port_relationships(
        LINUX_PORTS,
        [
            {"high_port_id": 102, "low_port_id": 101},
            {"high_port_id": 102, "low_port_id": 100},
            {"high_port_id": 100, "low_port_id": 103},
        ],
        lag_patterns={},
        bridge_patterns={"linux": r"^(vmbr|br|bridge)\d+$"},
        compiled_sap_patterns=[],
    )

    assert relationships == {
        "lag_members": {},
        "sub_interfaces": {102: 101},
        "bridge_members": {102: 100, 103: 100},
    }


def test_bridge_pair_is_not_reclassified_as_lag_by_name_field_fallback(mock_librenms_api):
    """Keep one relationship kind when bridge names differ between canonical fields."""
    relationships = mock_librenms_api.resolve_port_relationships(
        [
            {
                "port_id": 100,
                "ifName": "vmbr0",
                "ifDescr": "LAN bridge",
                "ifType": "ethernetCsmacd",
            },
            {
                "port_id": 101,
                "ifName": "bond0",
                "ifDescr": "uplink",
                "ifType": "ieee8023adLag",
            },
        ],
        [{"high_port_id": 100, "low_port_id": 101}],
        lag_patterns={},
        bridge_patterns={"linux": r"^(vmbr|br|bridge)\d+$"},
        compiled_sap_patterns=[],
    )

    assert relationships == {
        "lag_members": {},
        "sub_interfaces": {},
        "bridge_members": {101: 100},
    }


def test_name_field_fallback_does_not_reclassify_a_sub_interface_pair_as_bridge(mock_librenms_api):
    """Assign one relationship type to a port-stack pair across name-field fallback."""
    relationships = mock_librenms_api.resolve_port_relationships(
        [
            {
                "port_id": 100,
                "ifName": "vmbr0.100",
                "ifDescr": "VLAN 100",
                "ifType": "ethernetCsmacd",
            },
            {
                "port_id": 101,
                "ifName": "vmbr0",
                "ifDescr": "LAN bridge",
                "ifType": "ethernetCsmacd",
            },
        ],
        [{"high_port_id": 100, "low_port_id": 101}],
        interface_name_field="ifName",
        lag_patterns={},
        bridge_patterns={"linux": r"^(vmbr|br|bridge)\d+$"},
        compiled_sap_patterns=[],
    )

    assert relationships == {
        "lag_members": {},
        "sub_interfaces": {100: 101},
        "bridge_members": {},
    }


def test_bridge_pattern_is_stored_with_the_existing_port_stack_mapping():
    """Store all name-based port-stack rules in the existing per-OS mapping row."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    mapping = PortStackLagPattern.objects.create(
        librenms_os="bridge-test-os",
        lag_name_pattern=r"^bond\d+$",
        bridge_name_pattern=r"^vmbr\d+$",
    )

    assert [pattern.pattern for pattern in PortStackLagPattern.compiled_bridge_patterns_for_os("bridge-test-os")] == [
        r"^vmbr\d+$"
    ]
    assert "bridge_name_pattern: ^vmbr\\d+$" in mapping.to_yaml()


def test_normalize_relationship_maps_includes_bridge_edges():
    """Normalize bridge edges through the same corruption guard as other relationships."""
    relationships = {
        "lag_members": {"10": "20"},
        "sub_interfaces": {"30": "40"},
        "bridge_members": {"50": "60", "invalid": 70},
    }

    assert normalize_relationship_maps(relationships) == ({10: 20}, {30: 40}, {50: 60})


def test_inline_parent_sync_promotes_a_physical_child_to_virtual():
    """Prepare the source type before NetBox validates a parent relationship."""
    from types import SimpleNamespace

    from django.core.cache import cache

    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

    device = make_device("parent-type-promotion")
    child = make_interface(device, "bond0.110", iface_type="1000base-t")
    parent = make_interface(device, "bond0")
    for interface, port_id in ((child, 102), (parent, 101)):
        set_librenms_device_id(interface, port_id, "default")
        interface.save()
    request = make_request(
        "post",
        {"port_id": "102", "parent_port_id": "101", "interface_name_field": "ifName"},
    )
    view = SyncInterfaceParentView()
    view._librenms_api = SimpleNamespace(server_key="default")
    cache.set(
        view.get_cache_key(device, "ports", "default"),
        {
            "ports": [
                {"port_id": 102, "ifName": child.name},
                {"port_id": 101, "ifName": parent.name},
            ],
            "port_stack_relationships": {
                "lag_members": {},
                "sub_interfaces": {102: 101},
                "bridge_members": {},
            },
        },
    )

    response = post(view, request, object_type="device", object_id=device.pk)

    assert response.status_code == 200, response.content
    child.refresh_from_db()
    assert child.type == "virtual"
    assert child.parent_id == parent.pk


@pytest.mark.parametrize("role", ["lag", "bridge"])
def test_inline_parent_sync_preserves_a_virtual_role_and_its_members(role):
    """Reject a parent edge instead of silently replacing a LAG or bridge role."""
    from types import SimpleNamespace

    from django.core.cache import cache

    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

    device = make_device(f"parent-{role}-preservation")
    child = make_interface(device, f"target-{role}", iface_type=role)
    role_member = make_interface(device, "nic0")
    setattr(role_member, role, child)
    role_member.save(update_fields=[role])
    parent = make_interface(device, "Ethernet1")
    for interface, port_id in ((child, 100), (parent, 101)):
        set_librenms_device_id(interface, port_id, "default")
        interface.save()
    request = make_request(
        "post",
        {"port_id": "100", "parent_port_id": "101", "interface_name_field": "ifName"},
    )
    view = SyncInterfaceParentView()
    view._librenms_api = SimpleNamespace(server_key="default")
    cache.set(
        view.get_cache_key(device, "ports", "default"),
        {
            "ports": [
                {"port_id": 100, "ifName": child.name},
                {"port_id": 101, "ifName": parent.name},
            ],
            "port_stack_relationships": {
                "lag_members": {},
                "sub_interfaces": {100: 101},
                "bridge_members": {},
            },
        },
    )

    response = post(view, request, object_type="device", object_id=device.pk)

    assert response.status_code == 409, response.content
    child.refresh_from_db()
    role_member.refresh_from_db()
    assert child.type == role
    assert child.parent_id is None
    assert getattr(role_member, f"{role}_id") == child.pk


def test_inline_lag_sync_does_not_replace_a_parent_child_role():
    """Reject LAG promotion when the target is already a virtual child."""
    from types import SimpleNamespace

    from django.core.cache import cache

    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import (
        SyncInterfaceLagView,
        SyncInterfaceParentView,
    )

    device = make_device("lag-target-parent-preservation")
    target = make_interface(device, "bond0.110", iface_type="1000base-t")
    parent = make_interface(device, "bond0")
    member = make_interface(device, "nic0")
    for interface, port_id in ((target, 100), (parent, 101), (member, 102)):
        set_librenms_device_id(interface, port_id, "default")
        interface.save()

    api = SimpleNamespace(server_key="default")
    parent_view = SyncInterfaceParentView()
    parent_view._librenms_api = api
    lag_view = SyncInterfaceLagView()
    lag_view._librenms_api = api
    cache.set(
        parent_view.get_cache_key(device, "ports", "default"),
        {
            "ports": [
                {"port_id": 100, "ifName": target.name},
                {"port_id": 101, "ifName": parent.name},
                {"port_id": 102, "ifName": member.name},
            ],
            "port_stack_relationships": {
                "lag_members": {102: 100},
                "sub_interfaces": {100: 101},
                "bridge_members": {},
            },
        },
    )

    parent_response = post(
        parent_view,
        make_request(
            "post",
            {"port_id": "100", "parent_port_id": "101", "interface_name_field": "ifName"},
        ),
        object_type="device",
        object_id=device.pk,
    )
    lag_response = post(
        lag_view,
        make_request(
            "post",
            {"port_id": "102", "lag_port_id": "100", "interface_name_field": "ifName"},
        ),
        object_type="device",
        object_id=device.pk,
    )

    assert parent_response.status_code == 200, parent_response.content
    assert lag_response.status_code == 409, lag_response.content
    target.refresh_from_db()
    member.refresh_from_db()
    assert target.type == "virtual"
    assert target.parent_id == parent.pk
    assert member.lag_id is None


def test_inline_lag_sync_rejects_cross_member_parent_on_netbox_44(monkeypatch):
    """Reject an invalid LAG target before NetBox 4.4 reaches its VC validation bug."""
    from types import SimpleNamespace

    from dcim.models import Interface
    from django.core.cache import cache

    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

    _virtual_chassis, (parent_device, target_device) = make_virtual_chassis_members("lag-target-cross-member-parent")
    parent = make_interface(parent_device, "bond0")
    target = make_interface(target_device, "bond0.110", iface_type="virtual")
    target.parent = parent
    target.save(update_fields=["parent"])
    member = make_interface(target_device, "nic0")
    for interface, port_id in ((target, 100), (member, 102)):
        set_librenms_device_id(interface, port_id, "default")
        interface.save()

    view = SyncInterfaceLagView()
    view._librenms_api = SimpleNamespace(server_key="default")
    cache.set(
        view.get_cache_key(parent_device, "ports", "default"),
        {
            "ports": [
                {"port_id": 100, "ifName": target.name},
                {"port_id": 102, "ifName": member.name},
            ],
            "port_stack_relationships": {
                "lag_members": {102: 100},
                "sub_interfaces": {},
                "bridge_members": {},
            },
        },
    )

    original_clean = Interface.clean

    def netbox_44_clean(interface):
        if interface.pk == target.pk:
            raise AttributeError(
                "'Interface' object has no attribute 'virtual_chassis'",
                name="virtual_chassis",
            )
        return original_clean(interface)

    monkeypatch.setattr(Interface, "clean", netbox_44_clean)

    response = post(
        view,
        make_request(
            "post",
            {"port_id": "102", "lag_port_id": "100", "interface_name_field": "ifName"},
        ),
        object_type="device",
        object_id=target_device.pk,
    )

    assert response.status_code == 409, response.content
    assert b"NetBox rejected the LAG relationship" in response.content
    target.refresh_from_db()
    member.refresh_from_db()
    assert target.type == "virtual"
    assert target.parent_id == parent.pk
    assert member.lag_id is None


def test_parent_promotion_preserves_a_channel_sub_interface():
    """Keep channel subinterfaces unchanged across supported NetBox versions."""
    from netbox_librenms_plugin.views.sync.interfaces import _parent_child_needs_promotion

    device = make_device("parent-channel-preservation")
    child = make_interface(device, "Ethernet1:1")
    child.channel_id = 1

    assert not _parent_child_needs_promotion(child)


def test_inline_bridge_sync_sets_the_bridge_relationship():
    """Apply a bridge edge through the same inline flow as LAG and parent edges."""
    from types import SimpleNamespace

    from django.core.cache import cache

    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceBridgeView

    device = make_device("inline-bridge-sync")
    member = make_interface(device, "nic0")
    bridge = make_interface(device, "vmbr0", iface_type="virtual")
    for interface, port_id in ((member, 103), (bridge, 100)):
        set_librenms_device_id(interface, port_id, "default")
        interface.save()
    request = make_request(
        "post",
        {"port_id": "103", "bridge_port_id": "100", "interface_name_field": "ifName"},
    )
    view = SyncInterfaceBridgeView()
    view._librenms_api = SimpleNamespace(server_key="default")
    cache.set(
        view.get_cache_key(device, "ports", "default"),
        {
            "ports": [
                {"port_id": 103, "ifName": member.name},
                {"port_id": 100, "ifName": bridge.name},
            ],
            "port_stack_relationships": {
                "lag_members": {},
                "sub_interfaces": {},
                "bridge_members": {103: 100},
            },
        },
    )

    response = post(view, request, object_type="device", object_id=device.pk)

    assert response.status_code == 200, response.content
    member.refresh_from_db()
    assert member.bridge_id == bridge.pk


def test_bulk_sync_applies_parent_and_bridge_to_the_same_interface():
    """Keep bridge membership independent from the child interface's parent edge."""
    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    device = make_device("bulk-parent-bridge-sync")
    child = make_interface(device, "bond0.110", iface_type="1000base-t")
    parent = make_interface(device, "bond0", iface_type="lag")
    bridge = make_interface(device, "vmbr0", iface_type="virtual")
    for interface, port_id in ((child, 102), (parent, 101), (bridge, 100)):
        set_librenms_device_id(interface, port_id, "default")
        interface.save()
    ports = [
        {"port_id": 102, "ifName": child.name},
        {"port_id": 101, "ifName": parent.name},
        {"port_id": 100, "ifName": bridge.name},
    ]
    view = object.__new__(SyncInterfacesView)
    view.interface_name_field = "ifName"
    view.request = make_request("post")
    view._selected_port_ids = {102}

    view._sync_interface_relationships(
        device,
        ports,
        {
            "lag_members": {},
            "sub_interfaces": {102: 101},
            "bridge_members": {102: 100},
        },
        "default",
    )

    child.refresh_from_db()
    assert child.type == "virtual"
    assert child.parent_id == parent.pk
    assert child.bridge_id == bridge.pk
    parent.refresh_from_db()
    assert parent.type == "lag"


def test_failed_parent_sync_restores_the_source_type_and_relationship():
    """Restore both source mutations when NetBox rejects the prepared parent edge."""
    from django.core.exceptions import ValidationError

    from netbox_librenms_plugin.views.sync.interfaces import (
        _apply_interface_relationship,
        _promote_parent_child,
    )

    device = make_device("parent-source-restore")
    child = make_interface(device, "nic0", iface_type="1000base-t")

    with pytest.raises(ValidationError):
        _apply_interface_relationship(
            child,
            "parent",
            child,
            prepare_source=lambda source: _promote_parent_child(source, with_restore=True),
        )

    assert child.type == "1000base-t"
    assert child.parent_id is None


def test_relationship_column_renders_bridge_in_the_existing_column():
    """Render bridge state and its action without adding another interface-table column."""
    from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

    device = make_device("bridge-relationship-column")
    member = make_interface(device, "nic0")
    table = LibreNMSInterfaceTable([], device=device, server_key="default")
    html = str(
        table.render_parent(
            None,
            {
                "port_id": 103,
                "netbox_interface": member,
                "bridge_sync_status": "missing_nb",
                "librenms_bridge_name": "vmbr0",
                "librenms_bridge_port_id": 100,
            },
        )
    )

    assert "Bridge vmbr0" in html
    assert "bridge-sync-btn" in html
    assert 'data-bridge-port-id="100"' in html
    assert "sync-interface-bridge" in html


def test_inline_bridge_sync_supports_virtual_machine_interfaces():
    """Use NetBox's VMInterface.bridge field through the shared bridge endpoint."""
    from types import SimpleNamespace

    from django.core.cache import cache
    from virtualization.models import VMInterface

    from netbox_librenms_plugin.tests.conftest import make_vm
    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceBridgeView

    vm = make_vm("vm-bridge-sync")
    member = VMInterface.objects.create(virtual_machine=vm, name="nic0")
    bridge = VMInterface.objects.create(virtual_machine=vm, name="vmbr0")
    for interface, port_id in ((member, 103), (bridge, 100)):
        set_librenms_device_id(interface, port_id, "default")
        interface.save()
    request = make_request("post", {"port_id": "103", "bridge_port_id": "100"})
    view = SyncInterfaceBridgeView()
    view._librenms_api = SimpleNamespace(server_key="default")
    cache.set(
        view.get_cache_key(vm, "ports", "default"),
        {
            "ports": [
                {"port_id": 103, "ifName": member.name},
                {"port_id": 100, "ifName": bridge.name},
            ],
            "port_stack_relationships": {
                "lag_members": {},
                "sub_interfaces": {},
                "bridge_members": {103: 100},
            },
        },
    )

    response = post(view, request, object_type="virtualmachine", object_id=vm.pk)

    assert response.status_code == 200, response.content
    member.refresh_from_db()
    assert member.bridge_id == bridge.pk


def test_default_linux_bridge_pattern_is_seeded():
    """Ship the issue's Linux bridge convention as editable mapping data."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    mapping = PortStackLagPattern.objects.get(librenms_os="linux")

    assert mapping.lag_name_pattern == r"^bond\d+$"
    assert mapping.bridge_name_pattern == r"^(vmbr|br|bridge)\d+$"


def test_interface_selection_and_inline_action_include_bridge_dependencies():
    """Keep bridge selection and inline POST wiring in the shared relationship JavaScript."""
    from pathlib import Path

    import netbox_librenms_plugin

    source = (
        Path(netbox_librenms_plugin.__file__).parent / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"
    ).read_text(encoding="utf-8")

    assert "row.dataset.bridgePortId" in source
    assert ".lag-sync-btn, .parent-sync-btn, .bridge-sync-btn" in source
    assert "btn.dataset.bridgePortId" in source
    assert "`${relation}_port_id`" in source


def test_device_interface_lookup_prefetches_bridge(db, django_assert_num_queries):
    """Render device bridge state without a query for each matched interface."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

    device = make_device("device-bridge-prefetch")
    bridge = make_interface(device, "vmbr0", iface_type="virtual")
    member = make_interface(device, "nic0")
    member.bridge = bridge
    member.save(update_fields=["bridge"])
    view = DeviceInterfaceTableView()
    view.setup(make_request("get"))
    api = object.__new__(LibreNMSAPI)
    api.server_key = "default"
    view._librenms_api = api

    maps = view._build_interface_lookup_maps(device)

    with django_assert_num_queries(0):
        assert maps["by_name"]["nic0"].bridge.pk == bridge.pk


def test_vm_interface_lookup_prefetches_bridge(db, django_assert_num_queries):
    """Render VM bridge state without a query for each matched interface."""
    from virtualization.models import VMInterface

    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.tests.conftest import make_vm
    from netbox_librenms_plugin.views.object_sync.vms import VMInterfaceTableView

    vm = make_vm("vm-bridge-prefetch")
    bridge = VMInterface.objects.create(virtual_machine=vm, name="vmbr0")
    VMInterface.objects.create(virtual_machine=vm, name="nic0", bridge=bridge)
    view = VMInterfaceTableView()
    view.setup(make_request("get"))
    api = object.__new__(LibreNMSAPI)
    api.server_key = "default"
    view._librenms_api = api

    maps = view._build_interface_lookup_maps(vm)

    with django_assert_num_queries(0):
        assert maps["by_name"]["nic0"].bridge.pk == bridge.pk
