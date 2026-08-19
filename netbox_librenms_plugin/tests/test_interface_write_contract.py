"""Field-level contract for the shared LibreNMS to NetBox interface writer.

``update_interface_from_port`` ends in ``save()``, which does not run field validators, so any
value it accepts reaches the column as-is.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface


def _port(**overrides):
    port = {
        "port_id": 8100,
        "ifName": "Ethernet1",
        "ifDescr": "Ethernet1",
        "ifAlias": "Uplink to core",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifAdminStatus": "up",
    }
    port.update(overrides)
    return port


@pytest.mark.django_db
class TestInterfaceMtuContract:
    """An out-of-range or non-numeric ifMtu must not reach the column."""

    def _sync(self, name, **overrides):
        from netbox_librenms_plugin.interface_sync import update_interface_from_port

        device = make_device(f"mtu-{name}")
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        update_interface_from_port(
            interface,
            _port(**overrides),
            server_key="default",
            interface_name_field="ifName",
            netbox_type="1000base-t",
        )
        # The contract is about what reaches the column, so read the row back rather than
        # asserting on the attribute the writer just assigned in memory.
        interface.refresh_from_db()
        return interface

    def test_a_valid_mtu_is_written(self):
        """Positive control, so the rejections below cannot pass for the wrong reason."""
        assert self._sync("valid", ifMtu=9000).mtu == 9000

    @pytest.mark.parametrize("bad", [0, -1, 65537, 10**12, "not-a-number", "", None, True])
    def test_an_unusable_mtu_becomes_none(self, bad):
        """NetBox accepts 1..65536; anything else is dropped rather than written raw."""
        assert self._sync(f"bad-{str(bad)[:8].strip() or 'blank'}", ifMtu=bad).mtu is None


@pytest.mark.django_db
class TestInterfaceAliasContract:
    """The written description must follow the rule the interface table displays."""

    def _sync(self, name, **overrides):
        from netbox_librenms_plugin.interface_sync import update_interface_from_port

        device = make_device(f"alias-{name}")
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        interface.description = "previous description"
        update_interface_from_port(
            interface,
            _port(**overrides),
            server_key="default",
            interface_name_field="ifName",
            netbox_type="1000base-t",
        )
        # The contract is about what reaches the column, so read the row back rather than
        # asserting on the attribute the writer just assigned in memory.
        interface.refresh_from_db()
        return interface

    def test_a_real_alias_is_written(self):
        assert self._sync("real").description == "Uplink to core"

    def test_an_alias_echoing_the_selected_name_is_blanked(self):
        assert self._sync("echo-name", ifAlias="Ethernet1").description == ""

    def test_an_alias_echoing_the_other_canonical_name_is_blanked(self):
        """The table blanks an alias matching ifDescr too, so the writer must agree."""
        interface = self._sync("echo-descr", ifName="Gi0/1", ifDescr="Ethernet1", ifAlias="Ethernet1")
        assert interface.description == ""
