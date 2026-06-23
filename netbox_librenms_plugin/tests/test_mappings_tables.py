"""Selection-column config for the mapping tables."""


class TestPortStackLagPatternTableSelection:
    """PortStackLagPatternTable must expose a selection ToggleColumn with input name='select'."""

    def test_pk_toggle_column_uses_select_input_name(self):
        """The shared selection JS keys on input[name='select']; the table must override the default pk toggle (name='pk') so bulk selection works."""
        from netbox_librenms_plugin.tables.mappings import PortStackLagPatternTable

        col = PortStackLagPatternTable.base_columns["pk"]
        assert col.attrs.get("input", {}).get("name") == "select"
