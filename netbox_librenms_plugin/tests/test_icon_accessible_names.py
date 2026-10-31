"""An icon that gives its note only in a title also gives that note as its accessible name."""

import pytest
from django.template import Context, Template

from netbox_librenms_plugin.interface_rules import InterfaceRuleMatcher
from netbox_librenms_plugin.tests.conftest import stamp_rule_decision
from netbox_librenms_plugin.tests.view_test_helpers import assert_titled_icons_are_named

MARKUP = "<b>x</b>"


def test_note_icon_escapes_the_note_once_for_both_attributes():
    from netbox_librenms_plugin.utils import note_icon

    html = str(note_icon("mdi-alert", MARKUP))

    assert MARKUP not in html
    assert [icon["aria-label"] for icon in assert_titled_icons_are_named(html)] == [MARKUP]


def test_the_remote_port_badges_are_named():
    from netbox_librenms_plugin.utils import remote_port_html

    html = remote_port_html("Gi0/1", {"manual_remote": True, "also_reported_by": ["lldp", MARKUP]})

    icons = assert_titled_icons_are_named(html)
    assert len(icons) == 2
    assert MARKUP.upper() in icons[1]["title"]


def test_the_missing_vlan_warning_is_named():
    from netbox_librenms_plugin.utils import get_missing_vlan_warning

    assert len(assert_titled_icons_are_named(get_missing_vlan_warning(10, [10]))) == 1


def test_the_ambiguous_vlan_group_warning_is_named():
    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable

    table = LibreNMSVLANTable(data=[], vlan_groups=[])
    record = {"vlan_id": 40, "name": "AMBIG", "exists_in_netbox": False, "is_ambiguous": True}

    assert len(assert_titled_icons_are_named(table.render_vlan_group_selection(None, record))) == 1


def test_the_inherited_vlan_badge_is_named():
    from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

    icons = assert_titled_icons_are_named(
        LibreNMSInterfaceTable._render_vlan_inherited_badge({"vlan_inherited_from": MARKUP})
    )

    assert MARKUP in icons[0]["title"]


@pytest.mark.django_db
def test_the_interface_type_note_is_named():
    from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

    table = LibreNMSInterfaceTable(data=[], device=None, interface_name_field="ifName", server_key="default")
    record = stamp_rule_decision(
        {
            "port_id": 42,
            "ifName": "Ethernet1",
            "ifDescr": "Ethernet1",
            "synced_name": "Ethernet1",
            "ifType": MARKUP,
            "ifSpeed": 1_000_000_000,
            "exists_in_netbox": False,
        },
        rules=InterfaceRuleMatcher(()),
    )

    html = str(table.render_type(record["ifType"], record))

    assert MARKUP not in html
    assert MARKUP in assert_titled_icons_are_named(html)[0]["title"]


def test_the_vrf_suggestion_is_named():
    from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable

    template = Template(IPAddressTable.base_columns["vrf"].template_code)
    record = {"row_id": "r1", "vrfs": [], "vrf_suggested_from": {"name": MARKUP, "matched_by": "name"}}

    html = template.render(Context({"record": record}))

    assert MARKUP not in html
    assert MARKUP in assert_titled_icons_are_named(html)[0]["title"]
