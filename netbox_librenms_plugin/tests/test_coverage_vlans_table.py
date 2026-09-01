"""
Coverage tests for netbox_librenms_plugin/tables/vlans.py

Tests cover all render methods and the configure() method of LibreNMSVLANTable.
"""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device


pytestmark = pytest.mark.django_db


def _group(pk, name, scope=None):
    from ipam.models import VLANGroup

    group = VLANGroup(name=name, slug=f"{name.lower().replace(' ', '-')}-{pk}")
    group.scope = scope
    group.save()
    return group


def _make_table(data=None, vlan_groups=None):
    """Create a LibreNMSVLANTable instance with minimal data."""
    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable

    return LibreNMSVLANTable(data=data or [], vlan_groups=vlan_groups)


# ===========================================================================
# __init__ / construction
# ===========================================================================


class TestLibreNMSVLANTableInit:
    """Tests for LibreNMSVLANTable.__init__()."""

    def test_default_prefix_set(self):
        table = _make_table()
        assert table.prefix == "vlans_"

    def test_vlan_groups_default_to_empty_list(self):
        table = _make_table()
        assert table.vlan_groups == []

    def test_vlan_groups_stored_when_provided(self):
        group = _group(1, "Stored Group")
        table = _make_table(vlan_groups=[group])
        assert table.vlan_groups == [group]

    def test_none_vlan_groups_normalised_to_empty_list(self):
        table = _make_table(vlan_groups=None)
        assert table.vlan_groups == []


# ===========================================================================
# render_vlan_id
# ===========================================================================


class TestRenderVlanId:
    """Tests for LibreNMSVLANTable.render_vlan_id()."""

    def test_text_success_when_exists_and_name_matches(self):
        table = _make_table()
        record = {"exists_in_netbox": True, "name_matches": True}
        html = str(table.render_vlan_id(100, record))
        assert "text-success" in html
        assert "100" in html

    def test_text_warning_when_exists_but_name_mismatch(self):
        table = _make_table()
        record = {"exists_in_netbox": True, "name_matches": False}
        html = str(table.render_vlan_id(200, record))
        assert "text-warning" in html
        assert "200" in html

    def test_text_danger_when_not_in_netbox(self):
        table = _make_table()
        record = {"exists_in_netbox": False, "name_matches": True}
        html = str(table.render_vlan_id(300, record))
        assert "text-danger" in html
        assert "300" in html

    def test_default_name_matches_true_when_absent(self):
        """When name_matches key is absent, defaults to True → text-success if exists."""
        table = _make_table()
        record = {"exists_in_netbox": True}  # name_matches key absent
        html = str(table.render_vlan_id(10, record))
        assert "text-success" in html


# ===========================================================================
# render_name
# ===========================================================================


class TestRenderName:
    """Tests for LibreNMSVLANTable.render_name()."""

    def test_text_success_when_synced(self):
        table = _make_table()
        record = {"exists_in_netbox": True, "name_matches": True}
        html = str(table.render_name("DATA", record))
        assert "text-success" in html
        assert "DATA" in html

    def test_text_danger_when_not_in_netbox(self):
        table = _make_table()
        record = {"exists_in_netbox": False, "name_matches": True}
        html = str(table.render_name("VOICE", record))
        assert "text-danger" in html
        assert "VOICE" in html

    def test_tooltip_added_on_name_mismatch(self):
        """When exists_in_netbox=True and name_matches=False, tooltip with NetBox name is shown."""
        table = _make_table()
        record = {
            "exists_in_netbox": True,
            "name_matches": False,
            "netbox_vlan_name": "OLD_NAME",
        }
        html = str(table.render_name("NEW_NAME", record))
        assert "text-warning" in html
        assert "NEW_NAME" in html
        assert "OLD_NAME" in html
        assert "title=" in html

    def test_empty_name_rendered_as_empty_string(self):
        """render_name handles None/empty value."""
        table = _make_table()
        record = {"exists_in_netbox": False, "name_matches": True}
        html = str(table.render_name(None, record))
        assert "text-danger" in html

    def test_tooltip_contains_both_names(self):
        table = _make_table()
        record = {
            "exists_in_netbox": True,
            "name_matches": False,
            "netbox_vlan_name": "NetBox-VLANName",
        }
        html = str(table.render_name("LibreNMSName", record))
        assert "NetBox-VLANName" in html
        assert "LibreNMSName" in html

    def test_no_tooltip_when_names_match(self):
        table = _make_table()
        record = {"exists_in_netbox": True, "name_matches": True}
        html = str(table.render_name("MGMT", record))
        # Tooltip (title=) should NOT be present when names match
        assert 'title="' not in html


# ===========================================================================
# render_vlan_group_selection
# ===========================================================================


class TestRenderVlanGroupSelection:
    """Tests for LibreNMSVLANTable.render_vlan_group_selection()."""

    def _make_group(self, pk, name, scope=None):
        return _group(pk, name, scope)

    def test_select_element_rendered(self):
        table = _make_table(vlan_groups=[self._make_group(1, "Site VLANs")])
        record = {"vlan_id": 10, "name": "DATA", "exists_in_netbox": False}
        html = str(table.render_vlan_group_selection(None, record))
        assert "<select" in html
        assert 'name="vlan_group_10"' in html

    def test_no_selection_by_default(self):
        """When no auto-select criteria match, no option is pre-selected."""
        group = self._make_group(1, "Global")
        table = _make_table(vlan_groups=[group])
        record = {"vlan_id": 5, "name": "TEST", "exists_in_netbox": False}
        html = str(table.render_vlan_group_selection(None, record))
        # 'selected' should not appear for the group option
        assert "selected" not in html

    def test_existing_netbox_vlan_group_preselected(self):
        """Priority 1: existing NetBox VLAN group is pre-selected."""
        group = self._make_group(pk=7, name="Existing Group")
        table = _make_table(vlan_groups=[group])
        record = {
            "vlan_id": 20,
            "name": "EXISTING",
            "exists_in_netbox": True,
            "netbox_vlan_group_id": group.pk,
        }
        html = str(table.render_vlan_group_selection(None, record))
        assert "selected" in html

    def test_auto_selected_group_preselected(self):
        """Priority 2: auto_selected_group_id is pre-selected when exists_in_netbox is False."""
        group = self._make_group(pk=3, name="Auto Group")
        table = _make_table(vlan_groups=[group])
        record = {
            "vlan_id": 30,
            "name": "AUTO",
            "exists_in_netbox": False,
            "auto_selected_group_id": group.pk,
        }
        html = str(table.render_vlan_group_selection(None, record))
        assert "selected" in html

    def test_warning_icon_when_ambiguous_and_not_in_netbox(self):
        """is_ambiguous=True and exists_in_netbox=False shows a warning icon."""
        table = _make_table(vlan_groups=[])
        record = {
            "vlan_id": 40,
            "name": "AMBIG",
            "exists_in_netbox": False,
            "is_ambiguous": True,
        }
        html = str(table.render_vlan_group_selection(None, record))
        assert "mdi-alert" in html

    def test_no_warning_icon_when_ambiguous_but_in_netbox(self):
        """Warning icon is NOT shown when exists_in_netbox=True even if is_ambiguous."""
        table = _make_table(vlan_groups=[])
        record = {
            "vlan_id": 50,
            "name": "IN_NB",
            "exists_in_netbox": True,
            "is_ambiguous": True,
            "netbox_vlan_group_id": None,
        }
        html = str(table.render_vlan_group_selection(None, record))
        assert "mdi-alert" not in html

    def test_no_warning_icon_when_not_ambiguous(self):
        """No warning icon when is_ambiguous is False."""
        table = _make_table(vlan_groups=[])
        record = {
            "vlan_id": 60,
            "name": "CLEAR",
            "exists_in_netbox": False,
            "is_ambiguous": False,
        }
        html = str(table.render_vlan_group_selection(None, record))
        assert "mdi-alert" not in html

    def test_empty_groups_shows_no_group_option_only(self):
        table = _make_table(vlan_groups=[])
        record = {"vlan_id": 70, "name": "NOVLAN", "exists_in_netbox": False}
        html = str(table.render_vlan_group_selection(None, record))
        assert "No Group" in html

    def test_scope_info_appended_when_scope_present(self):
        """If group.scope is truthy, scope string is included in option."""
        device = make_device("vlan-table-scoped-group")
        group = self._make_group(pk=11, name="Rack VLANs", scope=device.site)
        table = _make_table(vlan_groups=[group])
        record = {"vlan_id": 80, "name": "RACK", "exists_in_netbox": False}
        html = str(table.render_vlan_group_selection(None, record))
        assert device.site.name in html

    def test_no_scope_info_when_scope_is_falsy(self):
        """If group.scope is falsy, no extra parenthetical appears."""
        group = self._make_group(pk=12, name="Global VLANs", scope=None)
        table = _make_table(vlan_groups=[group])
        record = {"vlan_id": 90, "name": "GLOBAL", "exists_in_netbox": False}
        html = str(table.render_vlan_group_selection(None, record))
        # The option text should just be the group name without extra suffix
        assert "Global VLANs" in html
        assert "(None)" not in html

    def test_vlan_id_and_name_embedded_in_select(self):
        table = _make_table(vlan_groups=[])
        record = {"vlan_id": 100, "name": "MY_VLAN", "exists_in_netbox": False}
        html = str(table.render_vlan_group_selection(None, record))
        assert 'data-vlan-id="100"' in html
        assert 'data-vlan-name="MY_VLAN"' in html


# ===========================================================================
# render_state
# ===========================================================================


class TestRenderState:
    """Tests for LibreNMSVLANTable.render_state()."""

    def test_active_integer_one_renders_active(self):
        """LIBRENMS_VLAN_STATE_ACTIVE == 1 → 'Active' with text-success."""
        from netbox_librenms_plugin.constants import LIBRENMS_VLAN_STATE_ACTIVE

        table = _make_table()
        html = str(table.render_state(LIBRENMS_VLAN_STATE_ACTIVE, {}))
        assert "text-success" in html
        assert "Active" in html

    def test_active_string_renders_active(self):
        """'active' string also renders as Active."""
        table = _make_table()
        html = str(table.render_state("active", {}))
        assert "text-success" in html
        assert "Active" in html

    def test_other_value_renders_inactive(self):
        table = _make_table()
        html = str(table.render_state(0, {}))
        assert "text-muted" in html
        assert "Inactive" in html

    def test_unknown_string_renders_inactive(self):
        table = _make_table()
        html = str(table.render_state("inactive", {}))
        assert "text-muted" in html
        assert "Inactive" in html

    def test_none_renders_inactive(self):
        table = _make_table()
        html = str(table.render_state(None, {}))
        assert "text-muted" in html


# ===========================================================================
# configure()
# ===========================================================================


class TestLibreNMSVLANTableConfigure:
    """Tests for LibreNMSVLANTable.configure()."""

    def test_configure_paginates_with_enhanced_paginator(self):
        from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
        from utilities.paginator import EnhancedPaginator

        rows = [{"vlan_id": vlan_id, "name": f"VLAN {vlan_id}", "exists_in_netbox": False} for vlan_id in range(1, 61)]
        table = LibreNMSVLANTable(data=rows)
        request = RequestFactory().get("/", {"vlans_page": "2"})
        request.user = AnonymousUser()

        table.configure(request)

        assert isinstance(table.paginator, EnhancedPaginator)
        assert table.page.number == 2
