"""MAC address rendering when LibreNMS reports a non-string ifPhysAddress.

The primary home for utils.format_mac_address is test_coverage_utils.py and for the
interface table it is test_coverage_tables.py. These cases live in their own file so
they do not collide at either shared file's tail when the stack is restacked.

views/base/interfaces_view.py already treats a non-string ifPhysAddress as absent,
because a device can report an int or a list there. The table render reached the same
field through utils.format_mac_address, which assumed a string.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface


def _record(mac, *, netbox_interface=None):
    """Build one LibreNMS port row carrying *mac* as its ifPhysAddress."""
    return {
        "port_id": 7,
        "ifName": "Ethernet1",
        "ifPhysAddress": mac,
        "exists_in_netbox": netbox_interface is not None,
        "netbox_interface": netbox_interface,
    }


def _table(device, record):
    """Build the real interface table the sync tab renders."""
    from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

    return LibreNMSInterfaceTable(data=[record], device=device, server_key="default")


@pytest.mark.django_db
class TestNonStringMacAddress:
    """A malformed ifPhysAddress must render as absent, not raise."""

    @pytest.mark.parametrize("mac", [12345, ["00:11:22:33:44:55"], {"mac": "x"}, 0.0])
    def test_a_non_string_mac_is_treated_as_absent(self, mac):
        """format_mac_address only guarded falsy input, so a truthy non-string hit .strip()."""
        from netbox_librenms_plugin.utils import format_mac_address

        assert format_mac_address(mac) == ""

    def test_a_non_string_mac_does_not_break_a_row_missing_from_netbox(self):
        """The unmatched-row branch renders the value straight into the red span."""
        device = make_device("mac-render-missing")
        record = _record(12345)

        html = str(_table(device, record).render_mac_address(record["ifPhysAddress"], record))

        assert "12345" not in html
        assert 'class="text-danger"' in html

    def test_a_non_string_mac_does_not_break_a_row_matched_in_netbox(self):
        """The matched-row branch runs the MAC comparison, which reads the formatted value."""
        device = make_device("mac-render-matched")
        interface = make_interface(device, "Ethernet1")
        record = _record(12345, netbox_interface=interface)

        html = str(_table(device, record).render_mac_address(record["ifPhysAddress"], record))

        assert "12345" not in html
        assert "text-warning" in html

    def test_a_real_string_mac_still_formats(self):
        """The repair must not change the behaviour a well-formed address already had."""
        from netbox_librenms_plugin.utils import format_mac_address

        assert format_mac_address("00:11:22:33:44:55") == "00:11:22:33:44:55"
        assert format_mac_address("0011.2233.4455") == "Invalid MAC Address"
        assert format_mac_address("") == ""
