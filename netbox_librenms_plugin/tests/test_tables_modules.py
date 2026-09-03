"""
Tests for netbox_librenms_plugin.tables.modules module.

Covers LibreNMSModuleTable render_* methods by calling them directly,
bypassing __init__ with object.__new__.  No DB access required.
"""

from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.tests._html_helpers import open_tags


class TestLibreNMSModuleTable:
    """Direct unit tests for every render_* method on LibreNMSModuleTable."""

    def _make_table(
        self,
        device=None,
        can_add_module=True,
        can_change_module=True,
        can_change_interface=True,
        can_delete_module=True,
        can_add_module_bay_template=True,
        can_add_module_type=True,
        can_add_carrier_rule=True,
        can_add_module_bay_mapping=True,
        can_add_module_type_mapping=True,
    ):
        """Create a bare table instance without calling __init__."""
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = object.__new__(LibreNMSModuleTable)
        table.device = device
        table.csrf_token = "test-csrf-token"
        table.server_key = ""
        table.has_write_permission = True
        table.can_add_module = can_add_module
        table.can_change_module = can_change_module
        table.can_change_interface = can_change_interface
        table.can_delete_module = can_delete_module
        table.can_add_module_bay_template = can_add_module_bay_template
        table.can_add_module_type = can_add_module_type
        table.can_add_carrier_rule = can_add_carrier_rule
        table.can_add_module_bay_mapping = can_add_module_bay_mapping
        table.can_add_module_type_mapping = can_add_module_type_mapping
        return table

    # ------------------------------------------------------------------
    # render_name
    # ------------------------------------------------------------------

    def test_render_name_depth_zero_returns_value(self):
        """At depth 0 the raw value is returned unchanged."""
        table = self._make_table()
        result = table.render_name("Router", {"depth": 0})
        assert result == "Router"

    def test_render_name_depth_zero_none_value_returns_dash(self):
        """At depth 0, None value is replaced with '-'."""
        table = self._make_table()
        result = table.render_name(None, {"depth": 0})
        assert result == "-"

    def test_render_name_depth_zero_missing_depth_defaults_to_zero(self):
        """When 'depth' key is absent the record defaults to 0."""
        table = self._make_table()
        result = table.render_name("Switch", {})
        assert result == "Switch"

    def test_render_name_depth_nonzero_contains_indent_and_prefix(self):
        """Non-zero depth produces a padded span with tree prefix."""
        table = self._make_table()
        result = table.render_name("Card", {"depth": 2})
        result_str = str(result)
        assert "padding-left:40px" in result_str  # 2 * 20 = 40
        assert "white-space: nowrap;" in result_str
        assert "└─" in result_str
        assert "Card" in result_str

    def test_render_name_depth_zero_oob_badge_renders(self):
        """Depth-0 OOB row must render the badge without raising (the badge was built with a no-arg format_html that raised TypeError)."""
        table = self._make_table()
        result_str = str(table.render_name("Card", {"depth": 0, "_source": "oob"}))
        assert "Card" in result_str
        assert "OOB" in result_str

    def test_render_name_nested_oob_badge_inside_padded_container(self):
        """OOB badge must sit inside the padded <span> so it stays indented on nested rows (was rendering at column 0, outside the container)."""
        table = self._make_table()
        result_str = str(table.render_name("Card", {"depth": 2, "_source": "oob"}))
        assert "OOB" in result_str
        # Badge span closes, then the padded outer span closes — i.e. badge is inside.
        assert "OOB</span></span>" in result_str

    def test_render_name_depth_one_correct_padding(self):
        """Depth 1 produces 20 px of padding."""
        table = self._make_table()
        result = table.render_name("Sub", {"depth": 1})
        result_str = str(result)
        assert "padding-left:20px" in result_str

    def test_render_name_depth_nonzero_none_value_shows_dash(self):
        """None value at non-zero depth falls back to '-'."""
        table = self._make_table()
        result = table.render_name(None, {"depth": 3})
        result_str = str(result)
        assert "-" in result_str

    # ------------------------------------------------------------------
    # render_model
    # ------------------------------------------------------------------

    def test_render_model_empty_string_returns_dash(self):
        """Empty string value returns '-'."""
        table = self._make_table()
        assert table.render_model("", {}) == "-"

    def test_render_model_dash_value_returns_dash(self):
        """Literal '-' value returns '-'."""
        table = self._make_table()
        assert table.render_model("-", {}) == "-"

    def test_render_model_none_returns_dash(self):
        """None value returns '-'."""
        table = self._make_table()
        assert table.render_model(None, {}) == "-"

    def test_render_model_with_url_returns_link(self):
        """When module_type_url is present, a hyperlink is rendered."""
        table = self._make_table()
        result = str(table.render_model("C9300", {"module_type_url": "/dcim/module-types/1/"}))
        assert 'href="/dcim/module-types/1/"' in result
        assert "C9300" in result

    def test_render_model_without_url_returns_plain_value(self):
        """Without a URL the plain value string is returned."""
        table = self._make_table()
        result = table.render_model("C9300", {})
        assert result == "C9300"

    # ------------------------------------------------------------------
    # render_serial
    # ------------------------------------------------------------------

    def test_render_serial_empty_returns_dash(self):
        """Empty serial returns '-'."""
        table = self._make_table()
        assert table.render_serial("", {}) == "-"

    def test_render_serial_none_returns_dash(self):
        """None serial returns '-'."""
        table = self._make_table()
        assert table.render_serial(None, {}) == "-"

    def test_render_serial_present_returns_value(self):
        """Non-empty serial is returned as-is."""
        table = self._make_table()
        assert table.render_serial("SN12345", {}) == "SN12345"

    # ------------------------------------------------------------------
    # render_description
    # ------------------------------------------------------------------

    def test_render_description_none_returns_dash(self):
        """None description returns '-'."""
        table = self._make_table()
        assert table.render_description(None, {}) == "-"

    def test_render_description_empty_string_returns_dash(self):
        """Empty string description returns '-'."""
        table = self._make_table()
        assert table.render_description("", {}) == "-"

    def test_render_description_short_returns_value(self):
        """Short description (≤60 chars) is returned unchanged."""
        table = self._make_table()
        short = "Short description"
        assert table.render_description(short, {}) == short

    def test_render_description_exactly_60_chars_not_truncated(self):
        """Description of exactly 60 chars is returned unchanged."""
        table = self._make_table()
        exact = "A" * 60
        assert table.render_description(exact, {}) == exact

    def test_render_description_long_truncated_with_ellipsis(self):
        """Description longer than 60 chars is truncated and has hellip."""
        table = self._make_table()
        long_desc = "B" * 65
        result = str(table.render_description(long_desc, {}))
        assert "B" * 57 in result
        assert "&hellip;" in result

    def test_render_description_long_title_contains_full_value(self):
        """The title attribute of the truncated span contains the full value."""
        table = self._make_table()
        long_desc = "C" * 70
        result = str(table.render_description(long_desc, {}))
        assert long_desc in result  # full value in title attribute

    # ------------------------------------------------------------------
    # render_item_class
    # ------------------------------------------------------------------

    def test_render_item_class_module_uses_expansion_card_icon(self):
        """'module' class uses the mdi-expansion-card icon."""
        table = self._make_table()
        result = str(table.render_item_class("module", {}))
        assert "mdi-expansion-card" in result
        assert "module" in result

    def test_render_item_class_fan_uses_fan_icon(self):
        """'fan' class uses the mdi-fan icon."""
        table = self._make_table()
        result = str(table.render_item_class("fan", {}))
        assert "mdi-fan" in result

    def test_render_item_class_power_supply_uses_plug_icon(self):
        """'powerSupply' class uses the mdi-power-plug icon."""
        table = self._make_table()
        result = str(table.render_item_class("powerSupply", {}))
        assert "mdi-power-plug" in result

    def test_render_item_class_port_uses_ethernet_icon(self):
        """'port' class uses the mdi-ethernet icon."""
        table = self._make_table()
        result = str(table.render_item_class("port", {}))
        assert "mdi-ethernet" in result

    def test_render_item_class_unknown_uses_default_icon(self):
        """Unknown class falls back to mdi-card-outline icon."""
        table = self._make_table()
        result = str(table.render_item_class("unknown_class", {}))
        assert "mdi-card-outline" in result

    def test_render_item_class_io_module_variant(self):
        """'ioModule' is also mapped to expansion-card."""
        table = self._make_table()
        result = str(table.render_item_class("ioModule", {}))
        assert "mdi-expansion-card" in result

    # ------------------------------------------------------------------
    # render_module_bay
    # ------------------------------------------------------------------

    def test_render_module_bay_none_shows_no_matching_bay(self):
        """None value shows the 'No matching bay' danger span."""
        table = self._make_table()
        result = str(table.render_module_bay(None, {}))
        assert "text-danger" in result
        assert "No matching bay" in result

    def test_render_module_bay_dash_shows_no_matching_bay(self):
        """Literal '-' shows the 'No matching bay' danger span."""
        table = self._make_table()
        result = str(table.render_module_bay("-", {}))
        assert "text-danger" in result

    def test_render_module_bay_empty_shows_no_matching_bay(self):
        """Empty string shows the 'No matching bay' danger span."""
        table = self._make_table()
        result = str(table.render_module_bay("", {}))
        assert "text-danger" in result

    def test_render_module_bay_integrated_row_shows_plain_dash(self):
        """Integrated rows have no bay of their own; show '-' not a red warning."""
        table = self._make_table()
        result = str(table.render_module_bay("-", {"status": "Integrated"}))
        assert "text-danger" not in result
        assert "No matching bay" not in result
        assert result == "-"

    def test_render_module_bay_with_url_renders_link(self):
        """When module_bay_url is present, a hyperlink is rendered."""
        table = self._make_table()
        result = str(table.render_module_bay("Bay 1", {"module_bay_url": "/dcim/module-bays/5/"}))
        assert 'href="/dcim/module-bays/5/"' in result
        assert "Bay 1" in result

    def test_render_module_bay_without_url_returns_plain_value(self):
        """Without URL the plain bay name is returned."""
        table = self._make_table()
        result = table.render_module_bay("Bay 1", {})
        assert result == "Bay 1"

    # ------------------------------------------------------------------
    # render_module_type
    # ------------------------------------------------------------------

    def test_render_module_type_none_shows_no_matching_type(self):
        """None value shows the 'No matching type' warning span."""
        table = self._make_table()
        result = str(table.render_module_type(None, {}))
        assert "text-warning" in result
        assert "No matching type" in result

    def test_render_module_type_dash_shows_no_matching_type(self):
        """Literal '-' shows the 'No matching type' warning span."""
        table = self._make_table()
        result = str(table.render_module_type("-", {}))
        assert "text-warning" in result

    def test_render_module_type_with_url_renders_link(self):
        """When module_type_url is present, a hyperlink is rendered."""
        table = self._make_table()
        result = str(table.render_module_type("C9300-NM-8X", {"module_type_url": "/dcim/module-types/10/"}))
        assert 'href="/dcim/module-types/10/"' in result
        assert "C9300-NM-8X" in result

    def test_render_module_type_without_url_returns_plain_value(self):
        """Without URL the plain type name is returned."""
        table = self._make_table()
        result = table.render_module_type("C9300-NM-8X", {})
        assert result == "C9300-NM-8X"

    # ------------------------------------------------------------------
    # render_status
    # ------------------------------------------------------------------

    def test_render_status_installed_uses_success_badge(self):
        """'Installed' status renders a bg-success badge."""
        table = self._make_table()
        result = str(table.render_status("Installed", {}))
        assert "bg-success" in result
        assert "Installed" in result

    def test_render_status_matched_uses_info_badge(self):
        """'Matched' status renders a bg-info badge."""
        table = self._make_table()
        result = str(table.render_status("Matched", {}))
        assert "bg-info" in result

    def test_render_status_wraps_live_badge_with_install_indicator(self):
        """The live badge is wrapped and a hidden 'Installing…' spinner badge is appended."""
        table = self._make_table()
        result = str(table.render_status("Matched", {}))
        # Live badge wrapper so CSS can hide it while an install POST is in flight.
        assert "lnms-status-live" in result
        # Hidden spinner badge revealed by tr.htmx-request (see _module_sync.html).
        assert "lnms-installing" in result
        assert "spinner-border" in result
        assert "Installing" in result
        # The real status badge is still present inside the wrapper.
        assert "bg-info" in result

    def test_render_status_install_indicator_present_for_every_status(self):
        """The install spinner badge rides along on non-installable statuses too (hidden by CSS)."""
        table = self._make_table()
        for status in ("Installed", "No Bay", "Serial Mismatch"):
            result = str(table.render_status(status, {}))
            assert "lnms-installing" in result, status

    def test_render_status_update_serial_row_spinner_says_updating(self):
        """An installed-module row offering Update Serial labels the spinner 'Updating…', not 'Installing…'."""
        table = self._make_table()
        record = {"installed_module_id": 42, "can_update_serial": True}
        result = str(table.render_status("Serial Mismatch", record))
        assert "Updating…" in result
        assert "Installing…" not in result

    def test_render_status_update_interface_row_spinner_says_updating(self):
        """An installed-module row offering Update Interface labels the spinner 'Updating…'."""
        table = self._make_table()
        record = {"installed_module_id": 42, "can_update_interface_binding": True}
        result = str(table.render_status("Installed", record))
        assert "Updating…" in result
        assert "Installing…" not in result

    def test_render_status_install_row_spinner_still_says_installing(self):
        """An installable (not-yet-installed) row keeps the 'Installing…' spinner label."""
        table = self._make_table()
        result = str(table.render_status("Matched", {"can_install": True}))
        assert "Installing…" in result
        assert "Updating…" not in result

    def test_render_status_carrier_row_spinner_says_installing(self):
        """A No Bay carrier-install row labels the spinner 'Installing…' (it installs a module)."""
        table = self._make_table()
        record = {"carrier_install_options": [{"bay_id": 1, "module_type_id": 2}]}
        result = str(table.render_status("No Bay", record))
        assert "Installing…" in result
        assert "Updating…" not in result

    def test_render_status_no_bay_uses_warning_badge(self):
        """'No Bay' status renders a bg-warning badge."""
        table = self._make_table()
        result = str(table.render_status("No Bay", {}))
        assert "bg-warning" in result

    def test_render_status_serial_mismatch_uses_danger_badge(self):
        """'Serial Mismatch' status renders a bg-danger badge."""
        table = self._make_table()
        result = str(table.render_status("Serial Mismatch", {}))
        assert "bg-danger" in result
        assert "Serial Mismatch" in result

    def test_render_status_name_conflict_uses_warning_badge(self):
        """'Name Conflict' status renders a bg-warning text-dark badge."""
        table = self._make_table()
        result = str(table.render_status("Name Conflict", {}))
        assert "bg-warning" in result
        assert "text-dark" in result
        assert "Name Conflict" in result

    def test_render_status_name_conflict_shows_reason_tooltip(self):
        """'Name Conflict' with a name_conflict_reason shows the reason on a warning icon."""
        table = self._make_table()
        reason = "Duplicate interface names detected due to sibling bays."
        result = str(table.render_status("Name Conflict", {"name_conflict_reason": reason}))
        assert "bg-warning" in result
        assert "mdi-alert-outline" in result
        assert "cursor-help" not in result
        assert reason in result

    def test_render_status_unknown_value_uses_secondary_badge(self):
        """Unknown status value falls back to bg-secondary."""
        table = self._make_table()
        result = str(table.render_status("Weird Status", {}))
        assert "bg-secondary" in result

    def test_render_status_no_bay_with_model_warning_adds_alert_icon(self):
        """model_warning on a No Bay row surfaces the NetBox-model hint as a tooltip."""
        table = self._make_table()
        result = str(
            table.render_status(
                "No Bay",
                {"model_warning": "No bay defined for class=fan; add Fan Tray N or Fan N bay templates"},
            )
        )
        assert "mdi-alert-outline" in result
        assert "Fan Tray N or Fan N" in result
        # The status cell must be returned as already-marked-safe HTML (not an
        # auto-escaped plain string). Regression guard against `SafeString + ""`
        # collapsing to a regular `str` and getting escaped on render.
        assert "<span" in result
        assert "&lt;span" not in result

    def test_render_status_no_bay_with_holder_hint_adds_possible_carrier_badge(self):
        """holder_hint_present (and no concrete options) appends the Possible Carrier? hint."""
        table = self._make_table()
        result = str(
            table.render_status(
                "No Bay",
                {"holder_hint_present": True},
            )
        )
        assert "Possible Carrier?" in result
        # Must NOT be HTML-escaped.
        assert "&lt;span" not in result

    def test_render_status_no_type_with_model_warning_adds_alert_icon(self):
        """model_warning on a No Type row surfaces the missing-ModuleType hint."""
        table = self._make_table()
        result = str(
            table.render_status(
                "No Type",
                {"model_warning": "No NetBox ModuleType matches 'ASR-9904-FAN'."},
            )
        )
        assert "mdi-alert-outline" in result
        assert "ASR-9904-FAN" in result

    # ------------------------------------------------------------------
    # render_actions
    # ------------------------------------------------------------------

    def test_render_actions_no_device_returns_empty_string(self):
        """Returns empty string when no device is set on the table."""
        table = self._make_table(device=None)
        result = table.render_actions(None, {"can_install": True})
        assert result == ""

    def test_render_actions_no_permissions_returns_empty_string(self):
        """Returns empty string when the user lacks both add and change permissions."""
        device = MagicMock()
        device.pk = 1
        table = self._make_table(device=device, can_add_module=False, can_change_module=False)
        result = table.render_actions(None, {"can_install": True})
        assert result == ""

    def test_render_actions_no_write_permission_returns_empty_string(self):
        """Returns empty string when plugin write permission is absent."""
        device = MagicMock()
        device.pk = 1
        table = self._make_table(device=device, can_add_module=True, can_change_module=True)
        table.has_write_permission = False
        result = table.render_actions(None, {"can_install": True})
        assert result == ""

    def test_render_actions_no_buttons_returns_empty_string(self):
        """Returns empty string when record has no actionable flags."""
        device = MagicMock()
        device.pk = 1
        table = self._make_table(device=device)
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/fake/"):
            result = table.render_actions(None, {})
        assert result == ""

    def test_render_actions_can_install_renders_install_button(self):
        """can_install=True renders an Install form button."""
        device = MagicMock()
        device.pk = 1
        table = self._make_table(device=device)
        record = {
            "can_install": True,
            "module_bay_id": 5,
            "module_type_id": 10,
            "serial": "SN123",
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/install-url/"):
            result = str(table.render_actions(None, record))

        assert "Install" in result
        assert "/install-url/" in result
        assert "SN123" in result
        assert "mdi-download" in result
        # The install form posts via HTMX so a single install swaps just the module table
        # in place instead of full-page reloading the whole sync view.
        assert 'hx-post="/install-url/"' in result
        assert 'hx-target="#module-sync-content"' in result

    def test_render_actions_has_installable_children_renders_branch_button(self):
        """has_installable_children + ent_physical_index renders Install Branch button."""
        device = MagicMock()
        device.pk = 2
        table = self._make_table(device=device)
        record = {
            "has_installable_children": True,
            "ent_physical_index": 42,
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/branch-url/"):
            result = str(table.render_actions(None, record))

        assert "Install Branch" in result
        assert "/branch-url/" in result
        assert "mdi-file-tree" in result

    def test_render_actions_both_buttons_rendered(self):
        """Both Install and Install Branch buttons render when both flags are set."""
        device = MagicMock()
        device.pk = 3
        table = self._make_table(device=device)
        record = {
            "can_install": True,
            "module_bay_id": 1,
            "module_type_id": 2,
            "serial": "SN-BOTH",
            "has_installable_children": True,
            "ent_physical_index": 99,
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = str(table.render_actions(None, record))

        assert "Install" in result
        assert "Install Branch" in result

    def test_render_actions_installable_children_without_index_skips_branch(self):
        """has_installable_children without ent_physical_index skips branch button."""
        device = MagicMock()
        device.pk = 4
        table = self._make_table(device=device)
        record = {
            "has_installable_children": True,
            # ent_physical_index intentionally absent
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = table.render_actions(None, record)

        assert result == ""

    def test_render_actions_csrf_token_included_in_form(self):
        """The CSRF token stored on the table is embedded in install form."""
        device = MagicMock()
        device.pk = 5
        table = self._make_table(device=device)
        table.csrf_token = "my-csrf-value"
        record = {"can_install": True, "module_bay_id": 1, "module_type_id": 1, "serial": ""}
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = str(table.render_actions(None, record))

        assert "my-csrf-value" in result

    def test_render_actions_serial_mismatch_renders_update_serial_button(self):
        """can_update_serial=True renders an Update Serial form button."""
        device = MagicMock()
        device.pk = 6
        table = self._make_table(device=device)
        record = {
            "can_update_serial": True,
            "installed_module_id": 42,
            "serial": "NS225161205",
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = str(table.render_actions(None, record))

        assert "Update Serial" in result
        assert "update-module-serial" in result or "/url/" in result
        assert "NS225161205" in result
        assert "mdi-sync" in result
        # Posts via HTMX so the update swaps just the module table in place (with the
        # closest-row spinner) instead of full-page reloading the whole sync view.
        assert 'hx-post="/url/"' in result
        assert 'hx-target="#module-sync-content"' in result
        assert 'hx-indicator="closest tr"' in result
        assert 'hx-disabled-elt="find button"' in result

    def test_render_actions_no_update_serial_without_flag(self):
        """Update Serial button not rendered when can_update_serial is not set."""
        device = MagicMock()
        device.pk = 7
        table = self._make_table(device=device)
        record = {
            "installed_module_id": 42,
            "serial": "NS225161205",
            # can_update_serial intentionally absent
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = table.render_actions(None, record)

        assert result == ""

    def test_render_actions_no_update_serial_without_module_id(self):
        """Update Serial button not rendered when installed_module_id is missing."""
        device = MagicMock()
        device.pk = 8
        table = self._make_table(device=device)
        record = {
            "can_update_serial": True,
            # installed_module_id intentionally absent
            "serial": "NS225161205",
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = table.render_actions(None, record)

        assert result == ""

    def test_render_actions_can_update_interface_renders_button(self):
        """Installed row with a safe interface candidate renders Update Interface."""
        device = MagicMock()
        device.pk = 81
        table = self._make_table(device=device, can_change_interface=True)
        record = {
            "can_update_interface_binding": True,
            "installed_module_id": 42,
            "ent_physical_index": 77,
            "librenms_port_id": 56284,
            "librenms_ifname": "TenGigabitEthernet1/1/1",
            "librenms_ifdescr": "Te1/1/1",
            "name": "Te1/1/1",
            "description": "10G transceiver",
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = str(table.render_actions(None, record))

        assert "Update Interface" in result
        assert "mdi-link-variant" in result
        assert 'name="module_id" value="42"' in result
        # Posts via HTMX so the update swaps just the module table in place (with the
        # closest-row spinner) instead of full-page reloading the whole sync view.
        assert 'hx-post="/url/"' in result
        assert 'hx-target="#module-sync-content"' in result
        assert 'hx-indicator="closest tr"' in result
        assert 'hx-disabled-elt="find button"' in result

    def test_render_actions_update_interface_requires_permission(self):
        """Update Interface button is hidden without change-interface permission."""
        device = MagicMock()
        device.pk = 82
        table = self._make_table(device=device, can_change_interface=False)
        record = {
            "can_update_interface_binding": True,
            "installed_module_id": 42,
            "ent_physical_index": 77,
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = str(table.render_actions(None, record))

        assert "Update Interface" not in result

    def test_render_actions_carrier_install_option_renders_htmx_form(self):
        """A No Bay row with a carrier_install_options entry renders an HTMX install form."""
        device = MagicMock()
        device.pk = 83
        table = self._make_table(device=device)
        record = {
            "status": "No Bay",
            "carrier_install_options": [
                {"bay_id": 12, "module_type_id": 34, "module_type_name": "CARRIER-A", "bay_name": "Slot 0"},
            ],
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/install-url/"):
            result = str(table.render_actions(None, record))

        assert "Install CARRIER-A into" in result
        assert 'name="module_bay_id" value="12"' in result
        assert 'name="module_type_id" value="34"' in result
        # Carrier-install posts to install_module, which returns the table partial for HTMX,
        # so it swaps in place with the closest-row spinner like the other install actions.
        assert 'hx-post="/install-url/"' in result
        assert 'hx-target="#module-sync-content"' in result
        assert 'hx-indicator="closest tr"' in result
        assert 'hx-disabled-elt="find button"' in result

    def test_render_actions_can_replace_renders_replace_button(self):
        """can_replace=True with installed_module_id renders a Replace button."""
        device = MagicMock()
        device.pk = 9
        table = self._make_table(device=device)
        record = {
            "can_replace": True,
            "installed_module_id": 55,
            "ent_physical_index": 200,
            "serial": "S1",
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/preview-url/"):
            result = str(table.render_actions(None, record))

        assert "Replace" in result
        assert "mdi-swap-horizontal" in result
        # The preview fragment carries hx- forms, so it must arrive through an HTMX swap: a
        # fetch()+innerHTML load leaves them unbound and they fall back to a full page reload.
        (button,) = [tag for tag in open_tags(result, "button") if tag.get("hx-get")]
        assert button["hx-get"] == "/preview-url/?module_id=55&ent_index=200&server_key=&selected_device_id=9"
        assert button["hx-target"] == "#htmx-modal-content"
        assert button["hx-swap"] == "innerHTML"
        # A newer click supersedes an older in-flight preview, as the AbortController did.
        assert button["hx-sync"] == "#htmx-modal-content:replace"
        assert button["hx-disabled-elt"] == "this"

    # One record per row action that posts through HTMX.
    _HTMX_ROW_ACTIONS = {
        "install": {"can_install": True, "module_bay_id": 1, "module_type_id": 2, "serial": "S1"},
        "install_branch": {"has_installable_children": True, "ent_physical_index": 5},
        "update_serial": {"can_update_serial": True, "installed_module_id": 42, "serial": "S2"},
        "update_interface": {
            "can_update_interface_binding": True,
            "installed_module_id": 42,
            "ent_physical_index": 77,
            "librenms_port_id": 56284,
        },
        "carrier_install": {
            "status": "No Bay",
            "carrier_install_options": [
                {"bay_id": 12, "module_type_id": 34, "module_type_name": "CARRIER-A", "bay_name": "Slot 0"}
            ],
        },
    }

    @pytest.mark.parametrize("record", _HTMX_ROW_ACTIONS.values(), ids=list(_HTMX_ROW_ACTIONS))
    def test_render_actions_row_form_drops_a_concurrent_module_action(self, record):
        """Two module actions in flight raced into one container: every row form takes the same lock."""
        device = MagicMock()
        device.pk = 12
        table = self._make_table(device=device)

        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = str(table.render_actions(None, record))

        forms = [tag for tag in open_tags(result, "form") if tag.get("hx-post")]
        assert forms
        for form in forms:
            assert form["hx-sync"] == "#module-sync-content:drop"
            # The row spinner and the button lock stay: hx-sync only gates a SECOND action.
            assert form["hx-disabled-elt"] == "find button"

    def test_render_actions_serial_mismatch_shows_both_update_and_replace(self):
        """Serial Mismatch row (can_update_serial + can_replace) shows both buttons."""
        device = MagicMock()
        device.pk = 10
        table = self._make_table(device=device)
        record = {
            "can_update_serial": True,
            "can_replace": True,
            "installed_module_id": 66,
            "ent_physical_index": 300,
            "serial": "NEWSERIAL",
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = str(table.render_actions(None, record))

        assert "Update Serial" in result
        assert "Replace" in result

    def test_render_actions_no_replace_without_module_id(self):
        """Replace button not rendered when installed_module_id is absent."""
        device = MagicMock()
        device.pk = 11
        table = self._make_table(device=device)
        record = {
            "can_replace": True,
            # installed_module_id intentionally absent
            "ent_physical_index": 400,
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = table.render_actions(None, record)

        assert "Replace" not in str(result)

    # ------------------------------------------------------------------
    # Permission-gating: add-only vs change-only
    # ------------------------------------------------------------------

    def test_render_actions_add_only_shows_install_hides_update_serial(self):
        """User with add but not change sees Install but not Update Serial."""
        device = MagicMock()
        device.pk = 20
        table = self._make_table(device=device, can_add_module=True, can_change_module=False)
        record = {
            "can_install": True,
            "module_bay_id": 1,
            "module_type_id": 2,
            "serial": "SN",
            "can_update_serial": True,
            "installed_module_id": 99,
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = str(table.render_actions(None, record))

        assert "Install" in result
        assert "Update Serial" not in result

    def test_render_actions_change_only_shows_update_serial_hides_install(self):
        """User with change but not add sees Update Serial but not Install."""
        device = MagicMock()
        device.pk = 21
        table = self._make_table(device=device, can_add_module=False, can_change_module=True)
        record = {
            "can_install": True,
            "module_bay_id": 1,
            "module_type_id": 2,
            "serial": "SN",
            "can_update_serial": True,
            "installed_module_id": 99,
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            result = str(table.render_actions(None, record))

        assert "Install" not in result
        assert "Update Serial" in result

    def test_render_actions_replace_requires_both_add_and_change(self):
        """Replace button only shown when user has both add and change."""
        device = MagicMock()
        device.pk = 22
        record = {
            "can_replace": True,
            "installed_module_id": 55,
            "ent_physical_index": 200,
        }
        # add-only: no Replace
        table = self._make_table(device=device, can_add_module=True, can_change_module=False)
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            assert "Replace" not in str(table.render_actions(None, record))

        # change-only: no Replace
        table = self._make_table(device=device, can_add_module=False, can_change_module=True)
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            assert "Replace" not in str(table.render_actions(None, record))

        # add+change but no delete: no Replace
        table = self._make_table(device=device, can_add_module=True, can_change_module=True, can_delete_module=False)
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            assert "Replace" not in str(table.render_actions(None, record))

        # all three: Replace shown
        table = self._make_table(device=device, can_add_module=True, can_change_module=True, can_delete_module=True)
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/url/"):
            assert "Replace" in str(table.render_actions(None, record))

    """Tests for __init__ and configure methods, bypassing django-tables2 super().__init__."""

    def test_init_sets_device_and_defaults(self):
        """__init__ sets device, tab, prefix, htmx_url, csrf_token attributes."""
        import django_tables2 as dt2
        from unittest.mock import MagicMock, patch

        device = MagicMock()
        with patch.object(dt2.Table, "__init__", return_value=None):
            from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

            table = LibreNMSModuleTable(device=device)

        assert table.device is device
        assert table.tab == "modules"
        assert table.prefix == "modules_"
        assert table.htmx_url is None
        assert table.csrf_token == ""

    def test_init_without_device_defaults_to_none(self):
        """__init__ with no device argument sets device=None."""
        import django_tables2 as dt2
        from unittest.mock import patch

        with patch.object(dt2.Table, "__init__", return_value=None):
            from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

            table = LibreNMSModuleTable()

        assert table.device is None

    def test_configure_sets_csrf_token(self):
        """configure() sets csrf_token from get_token and calls RequestConfig."""
        import django_tables2 as dt2
        from unittest.mock import MagicMock, patch

        with patch.object(dt2.Table, "__init__", return_value=None):
            from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

            table = LibreNMSModuleTable()

        request = MagicMock()
        request.headers.get.return_value = ""  # no HX-Current-URL header
        mock_rc_instance = MagicMock()

        with (
            patch("netbox_librenms_plugin.tables.modules.get_table_paginate_count", return_value=25),
            patch("django.middleware.csrf.get_token", return_value="csrf-abc"),
            patch("django_tables2.RequestConfig", return_value=mock_rc_instance) as mock_rc_cls,
        ):
            table.configure(request)

        assert table.csrf_token == "csrf-abc"
        mock_rc_cls.assert_called_once()
        mock_rc_instance.configure.assert_called_once_with(table)

    def test_render_actions_no_bay_with_suggestion_renders_add_mapping_button(self):
        """A No Bay row with a model_suggestion gets an "Add Mapping" link."""
        device = MagicMock()
        device.pk = 54
        table = self._make_table(device=device)
        table.server_key = "production"
        table.return_url = "/dcim/devices/54/librenms-sync/?tab=modules"
        record = {
            "status": "No Bay",
            "model_suggestion": {
                "librenms_name": r"^0/(\d+)$",
                "librenms_class": "module",
                "netbox_bay_name": r"Slot \1",
                "is_regex": True,
                "description": "Suggested mapping",
                "example_item": "0/0",
                "example_bay": "Slot 0",
            },
        }
        with patch(
            "netbox_librenms_plugin.tables.modules.reverse", return_value="/plugins/librenms/module-bay-mappings/add/"
        ):
            result = str(table.render_actions("", record))
        assert "Add Mapping" in result
        # The link must include the suggestion as querystring
        assert "librenms_name=" in result
        assert "is_regex=true" in result
        assert "netbox_bay_name=" in result
        # return_url must round-trip back to the modules tab
        assert "return_url=" in result

    def test_render_actions_no_bay_without_suggestion_omits_button(self):
        """No suggestion -> no "Add Mapping" button."""
        device = MagicMock()
        device.pk = 54
        table = self._make_table(device=device)
        table.server_key = "production"
        record = {"status": "No Bay"}
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/x/"):
            result = str(table.render_actions("", record))
        assert "Add Mapping" not in result

    def test_render_actions_matched_row_does_not_render_add_mapping(self):
        """`Matched` rows must not render the Add Mapping button even with a suggestion key."""
        device = MagicMock()
        device.pk = 54
        table = self._make_table(device=device)
        table.server_key = "production"
        record = {
            "status": "Matched",
            "model_suggestion": {
                "librenms_name": r"^0/(\d+)$",
                "librenms_class": "module",
                "netbox_bay_name": r"Slot \1",
                "is_regex": True,
                "example_item": "0/0",
                "example_bay": "Slot 0",
            },
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/x/"):
            result = str(table.render_actions("", record))
        assert "Add Mapping" not in result

    def test_render_actions_no_type_with_suggestion_renders_add_mapping_button(self):
        """A No Type row with a type_suggestion gets an "Add Mapping" link to moduletypemapping_add."""
        device = MagicMock()
        device.pk = 55
        table = self._make_table(device=device)
        table.server_key = "production"
        table.return_url = "/dcim/devices/55/librenms-sync/?tab=modules"
        record = {
            "status": "No Type",
            "type_suggestion": {
                "librenms_model": "SFP-10G-SR",
                "description": "Auto-suggested: maps LibreNMS model 'SFP-10G-SR' to a NetBox ModuleType.",
            },
        }
        with patch(
            "netbox_librenms_plugin.tables.modules.reverse",
            return_value="/plugins/librenms/module-type-mappings/add/",
        ):
            result = str(table.render_actions("", record))
        assert "Add Mapping" in result
        assert "librenms_model=" in result
        assert "return_url=" in result

    def test_render_actions_no_type_without_suggestion_omits_add_mapping_button(self):
        """No type_suggestion on a No Type row → no "Add Mapping" button."""
        device = MagicMock()
        device.pk = 56
        table = self._make_table(device=device)
        record = {"status": "No Type"}
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/x/"):
            result = str(table.render_actions("", record))
        assert "Add Mapping" not in result

    def test_render_actions_no_type_add_mapping_not_shown_for_other_statuses(self):
        """type_suggestion on a non-No-Type row must not render the Add Mapping button."""
        device = MagicMock()
        device.pk = 57
        table = self._make_table(device=device)
        record = {
            "status": "Matched",
            "type_suggestion": {
                "librenms_model": "SFP-10G-SR",
                "description": "should be ignored",
            },
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/x/"):
            result = str(table.render_actions("", record))
        assert "Add Mapping" not in result

    def test_render_actions_no_type_with_module_type_create_renders_add_module_type_button(self):
        """A No Type row with a `module_type_create` prefill gets an "Add Module Type" link to dcim:moduletype_add."""
        device = MagicMock()
        device.pk = 58
        table = self._make_table(device=device)
        table.return_url = "/dcim/devices/58/librenms-sync/?tab=modules"
        record = {
            "status": "No Type",
            "module_type_create": {
                "manufacturer": 7,
                "model": "X2-10GB-LR",
                "part_number": "X2-10GB-LR",
                "description": "10Gbase-LR",
            },
        }

        def _reverse(name):
            return {
                "plugins:netbox_librenms_plugin:moduletypemapping_add": "/plugins/librenms/module-type-mappings/add/",
                "dcim:moduletype_add": "/dcim/module-types/add/",
            }[name]

        with patch("netbox_librenms_plugin.tables.modules.reverse", side_effect=_reverse):
            result = str(table.render_actions("", record))
        assert "Add Module Type" in result
        assert "/dcim/module-types/add/" in result
        assert "manufacturer=7" in result
        assert "model=X2-10GB-LR" in result
        assert "part_number=X2-10GB-LR" in result
        assert "description=10Gbase-LR" in result
        assert "return_url=" in result

    def test_render_actions_no_type_omits_module_type_button_when_no_prefill(self):
        """Without a `module_type_create` prefill, the Add Module Type button must not render."""
        device = MagicMock()
        device.pk = 59
        table = self._make_table(device=device)
        record = {"status": "No Type"}
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/x/"):
            result = str(table.render_actions("", record))
        assert "Add Module Type" not in result

    def test_render_actions_module_type_button_skipped_for_other_statuses(self):
        """`module_type_create` on a non-No-Type row must not produce the button."""
        device = MagicMock()
        device.pk = 60
        table = self._make_table(device=device)
        record = {
            "status": "Matched",
            "module_type_create": {"model": "X", "part_number": "X"},
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/x/"):
            result = str(table.render_actions("", record))
        assert "Add Module Type" not in result

    def test_render_actions_module_type_button_skips_blank_prefill_values(self):
        """Empty prefill values must be dropped from the querystring (no `=&` artefacts)."""
        device = MagicMock()
        device.pk = 61
        table = self._make_table(device=device)
        record = {
            "status": "No Type",
            "module_type_create": {
                "model": "GLC-T",
                "part_number": "GLC-T",
                "description": "",
                "manufacturer": None,
            },
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/dcim/module-types/add/"):
            result = str(table.render_actions("", record))
        assert "Add Module Type" in result
        assert "description=" not in result
        assert "manufacturer=" not in result
        assert "model=GLC-T" in result

    def test_render_actions_module_type_button_skipped_when_user_lacks_add_moduletype_perm(self):
        """Without `dcim.add_moduletype`, the Add Module Type CTA must be hidden — clicking
        it would surface only a permission error from NetBox's native form."""
        device = MagicMock()
        device.pk = 62
        table = self._make_table(device=device, can_add_module_type=False)
        record = {
            "status": "No Type",
            "module_type_create": {"model": "X", "part_number": "X"},
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/dcim/module-types/add/"):
            result = str(table.render_actions("", record))
        assert "Add Module Type" not in result


# ---------------------------------------------------------------------------
# Integrated badge + action gating
# ---------------------------------------------------------------------------


class TestRenderIntegrated(TestLibreNMSModuleTable):
    def test_render_status_integrated_uses_muted_badge_with_parent_name(self):
        table = self._make_table()
        record = {
            "status": "Integrated",
            "integrated_in_name": "XIOM 2/x1",
        }
        html = str(table.render_status("Integrated", record))
        assert "Integrated in XIOM 2/x1" in html
        # muted styling
        assert "text-muted" in html or "bg-light" in html
        # tooltip explains the dedupe
        assert "Duplicate SNMP entry" in html

    def test_render_actions_integrated_returns_empty(self):
        device = MagicMock()
        device.pk = 99
        table = self._make_table(device=device)
        record = {
            "status": "Integrated",
            "integrated_in_name": "XIOM 2/x1",
            # Even if a stale type_suggestion / module_type_create slipped in,
            # actions must still be empty for Integrated rows.
            "type_suggestion": {"librenms_model": "X", "description": "y"},
            "module_type_create": {"model": "X", "part_number": "X"},
        }
        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/x/"):
            assert table.render_actions("", record) == ""
