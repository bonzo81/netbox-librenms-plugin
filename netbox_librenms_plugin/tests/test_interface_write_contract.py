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
class TestInterfaceMacContract:
    """A malformed ifPhysAddress must skip only the MAC, never abort the interface update."""

    def _sync(self, name, mac):
        from netbox_librenms_plugin.interface_sync import update_interface_from_port

        device = make_device(f"mac-{name}")
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        update_interface_from_port(
            interface,
            _port(ifPhysAddress=mac, ifAlias="Updated alias"),
            server_key="default",
            interface_name_field="ifName",
            netbox_type="1000base-t",
        )
        interface.refresh_from_db()
        return interface

    @pytest.mark.parametrize("mac", ["unknown", "n/a", "zz:zz:zz:zz:zz:zz"])
    def test_a_malformed_mac_is_skipped_and_the_interface_still_updates(self, mac):
        """NetBox's macaddr column rejects these, and the rejection fires on the mac_addresses
        lookup, so an unvalidated write aborts the whole interface update."""
        from dcim.models import MACAddress

        interface = self._sync(mac.replace(":", "").replace("/", "")[:8], mac)

        assert interface.description == "Updated alias", "the interface update must still land"
        assert not interface.mac_addresses.exists(), f"{mac!r} must not be persisted as a MAC"
        # Not filter(mac_address=mac): the column parses the lookup value and would raise here too.
        assert not MACAddress.objects.exists()

    def test_the_rejection_is_logged_without_the_mac_value(self, caplog):
        """A MAC is private data, so the diagnostic must name the interface, not the value."""
        import logging

        with caplog.at_level(logging.DEBUG, logger="netbox_librenms_plugin.interface_sync"):
            interface = self._sync("logged", "unknown")

        messages = [record.getMessage() for record in caplog.records]
        assert any(str(interface.pk) in message for message in messages), messages
        assert not any("unknown" in message for message in messages), messages

    def test_a_well_formed_mac_is_still_written(self):
        """The guard must not reject the valid case it is wrapped around."""
        interface = self._sync("valid", "00:11:22:33:44:55")

        assert interface.mac_addresses.count() == 1
        assert str(interface.mac_addresses.first().mac_address) == "00:11:22:33:44:55"


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


@pytest.mark.django_db
class TestInterfaceNameGateModel:
    """The view's name gate must read the same column bound the writer enforces."""

    def test_a_vm_row_is_gated_by_the_vminterface_column(self, monkeypatch):
        """A name the VMInterface column cannot hold is skipped, not handed to the writer."""
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.tests.conftest import make_vm
        from netbox_librenms_plugin.tests.view_test_helpers import make_view
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        # NetBox gives both models a 64-character name today. The writer already reads the
        # concrete model so the two cannot drift; shrink one to prove the gate reads it too.
        monkeypatch.setattr(VMInterface._meta.get_field("name"), "max_length", 8)
        vm = make_vm("vm-name-gate")
        view = make_view(SyncInterfacesView)
        view._skipped_conflicts = []

        view.sync_interface(vm, _port(ifName="E" * 20), ["vlans"], "ifName")

        assert not VMInterface.objects.filter(virtual_machine=vm).exists()
        assert view._skipped_conflicts == [
            "EEEEEEEEEEEEEEEEEEEE (interface name is longer than the 8 characters NetBox stores)"
        ]

    def test_a_vm_name_that_fits_still_syncs(self):
        """Positive control: the stricter model must not reject an ordinary name."""
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.tests.conftest import make_vm
        from netbox_librenms_plugin.tests.view_test_helpers import make_view
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        vm = make_vm("vm-name-gate-ok")
        view = make_view(SyncInterfacesView)
        view._skipped_conflicts = []

        view.sync_interface(vm, _port(ifName="eth0"), ["vlans"], "ifName")

        assert VMInterface.objects.filter(virtual_machine=vm, name="eth0").exists()
        assert view._skipped_conflicts == []


@pytest.mark.django_db
class TestInterfaceStringLengthContract:
    """LibreNMS free text must be bounded to the column it is written to.

    ``save()`` runs no validators and Django never truncates a CharField, so an over-long
    value reaches Postgres and raises SQLSTATE 22001. Django surfaces that as ``DataError``,
    which is NOT a subclass of ``IntegrityError``, so the bulk-sync handler does not catch it
    and the whole sync 500s and rolls back.
    """

    @staticmethod
    def _max_length(field_name):
        from dcim.models import Interface

        return Interface._meta.get_field(field_name).max_length

    def _sync(self, name, **overrides):
        from netbox_librenms_plugin.interface_sync import update_interface_from_port

        device = make_device(f"len-{name}")
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        update_interface_from_port(
            interface,
            _port(**overrides),
            server_key="default",
            interface_name_field="ifName",
            netbox_type="1000base-t",
        )
        interface.refresh_from_db()
        return interface

    def test_an_over_length_alias_is_bounded_to_the_column(self):
        limit = self._max_length("description")
        alias = "A" * (limit + 50)

        interface = self._sync("alias", ifAlias=alias)

        assert len(interface.description) <= limit
        assert interface.description == alias[:limit]

    def test_an_alias_that_fits_is_written_whole(self):
        """Positive control: bounding must not truncate ordinary descriptions."""
        alias = "Uplink to core switch"

        assert self._sync("alias-fits", ifAlias=alias).description == alias

    def test_an_over_length_name_is_not_syncable(self):
        """An over-long name cannot be truncated: it would collide on (device, name)."""
        from netbox_librenms_plugin.utils import syncable_interface_name

        too_long = "E" * (self._max_length("name") + 1)

        assert syncable_interface_name({"ifName": too_long}, "ifName") is None

    def test_a_name_that_fits_is_syncable(self):
        """Positive control, including a name of exactly the column length."""
        from netbox_librenms_plugin.utils import syncable_interface_name

        for value in ("Ethernet1", "E" * self._max_length("name")):
            assert syncable_interface_name({"ifName": value}, "ifName") == value

    @pytest.mark.parametrize("value", [None, 5, ["Ethernet1"], "", "   "])
    def test_a_blank_or_non_string_name_is_not_syncable(self, value):
        from netbox_librenms_plugin.utils import syncable_interface_name

        assert syncable_interface_name({"ifName": value}, "ifName") is None

    def test_the_writer_refuses_a_name_it_cannot_store(self):
        """The writer is the last boundary before save(); it must not hand Postgres a 22001."""
        from netbox_librenms_plugin.interface_sync import update_interface_from_port

        device = make_device("len-writer-name")
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")

        with pytest.raises(ValueError, match="interface name"):
            update_interface_from_port(
                interface,
                _port(ifName="E" * (self._max_length("name") + 1)),
                server_key="default",
                interface_name_field="ifName",
                netbox_type="1000base-t",
            )
