"""Render fallbacks in tables/interfaces.py and tables/cables.py.

The primary home for both modules is test_coverage_tables.py. These cases live in their
own file so they do not collide at that shared file's tail when the stack is restacked.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_virtual_chassis


def _port(port_id=42, **overrides):
    """Return one complete LibreNMS interface record."""
    record = {
        "port_id": port_id,
        "ifName": "Ethernet1",
        "ifDescr": "Ethernet1",
        "ifAlias": "Uplink",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifPhysAddress": "AA:BB:CC:DD:EE:FF",
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "untagged_vlan": None,
        "tagged_vlans": [],
        "missing_vlans": [],
        "vlan_group_map": {},
    }
    record.update(overrides)
    return record


def _interface_table(device=None, *, data=None, server_key="default"):
    from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

    return LibreNMSInterfaceTable(
        data=data or [],
        device=device,
        interface_name_field="ifName",
        server_key=server_key,
    )


@pytest.mark.django_db
class TestUntaggedVlanComparison:
    def test_a_different_stored_untagged_vlan_is_a_mismatch(self):
        """NetBox clears untagged_vlan on save unless the interface has a mode, so set one."""
        from ipam.models import VLAN, VLANGroup

        device = make_device("interface-untagged-vlan-mismatch")
        interface = make_interface(device, "Ethernet1")
        group = VLANGroup.objects.create(name="Untagged mismatch group", slug="untagged-mismatch-group")
        stored = VLAN.objects.create(vid=300, name="Untagged 300", group=group, status="active")
        interface.mode = "tagged"
        interface.untagged_vlan = stored
        interface.save()
        record = _port(
            exists_in_netbox=True,
            netbox_interface=interface,
            untagged_vlan=100,
            vlan_group_map={100: {"group_id": str(group.pk), "group_name": group.name}},
        )

        html = str(_interface_table(device).render_vlans(None, record))

        assert '<span class="text-warning">100(U)' in html


@pytest.mark.django_db
class TestRelationshipPillWithoutALibreNMSName:
    def test_a_netbox_only_lag_renders_the_type_label_as_the_whole_pill(self):
        """missing_lnms carries no LibreNMS name, so the pill falls back to the type label."""
        from netbox_librenms_plugin.interface_relationships import (
            build_relationship_maps,
            enrich_port_relationships,
        )

        device = make_device("relationship-netbox-only-lag")
        lag = make_interface(device, "Port-Channel1", iface_type="lag")
        member = make_interface(device, "Ethernet1")
        member.lag = lag
        member.save()
        record = _port(netbox_interface=member, exists_in_netbox=True)
        maps = build_relationship_maps({"ports": [record], "port_stack_relationships": {}})

        enrich_port_relationships(record, maps, interface_name_field="ifName", server_key="default")
        html = str(_interface_table(device).render_parent(None, record))

        assert record["lag_sync_status"] == "missing_lnms"
        assert record["librenms_lag_name"] is None
        # The badge body is the type label alone, not an empty pill next to the tooltip.
        assert "></i>LAG</span>" in html
        assert 'title="LAG: Not in LibreNMS"' in html
        assert "lag-sync-btn" not in html


@pytest.mark.django_db
class TestInterfaceTypeComparison:
    def test_a_mapped_type_that_differs_from_netbox_renders_a_warning(self):
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        device = make_device("interface-type-mismatch")
        interface = make_interface(device, "Ethernet1", iface_type="10gbase-t")
        InterfaceTypeMapping.objects.create(
            librenms_type="ethernetCsmacd",
            librenms_speed=1_000_000,
            netbox_type="1000base-t",
        )
        record = _port(exists_in_netbox=True, netbox_interface=interface)

        html = str(_interface_table(device).render_type(record["ifType"], record))

        assert "text-warning" in html
        assert "1000base-t" in html


@pytest.mark.django_db
class TestVcCableMemberSelection:
    def test_a_port_name_without_a_matching_member_falls_back_to_the_viewed_device(self):
        from netbox_librenms_plugin.tables.cables import VCCableTable

        first = make_device("vc-cable-fallback-1")
        second = make_device("vc-cable-fallback-2")
        make_virtual_chassis("vc-cable-fallback", first, second)
        table = VCCableTable([], device=first)
        # row_id is carried alongside local_port_id: branches above this one key the rendered
        # select on row_id, so a row without it renders here but breaks there.
        matched = {"local_port": "Ethernet2", "local_port_id": 11, "row_id": 11}
        # Position 7 has no member, so the row keeps the device whose page is being viewed.
        unmatched = {"local_port": "Ethernet7", "local_port_id": 12, "row_id": 12}

        matched_html = str(table.render_device_selection(None, matched))
        unmatched_html = str(table.render_device_selection(None, unmatched))

        assert f'value="{second.pk}" selected' in matched_html
        assert f'value="{first.pk}" selected' in unmatched_html
