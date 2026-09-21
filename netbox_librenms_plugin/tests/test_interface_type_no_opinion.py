"""An unmapped LibreNMS ifType must leave the NetBox interface type alone.

``get_netbox_interface_type`` used to return the literal ``"other"`` when no
``InterfaceTypeMapping`` matched, and the writer wrote it unconditionally, so a correct type
(``lag`` above all) was flattened by a sync that simply had no mapping for the port.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface


def _port(**overrides):
    port = {
        "port_id": 9100,
        "ifName": "Ethernet1",
        "ifDescr": "Ethernet1",
        "ifAlias": "",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifAdminStatus": "up",
    }
    port.update(overrides)
    return port


def _sync(device, port):
    """Resolve or create one interface through the real writer and read the row back."""
    from dcim.models import Interface

    from netbox_librenms_plugin.interface_sync import resolve_or_create_interface_from_port

    interface = resolve_or_create_interface_from_port(
        device,
        port,
        server_key="default",
        interface_name_field="ifName",
        changeable_queryset=Interface.objects.all(),
        viewable_queryset=Interface.objects.all(),
    )
    interface.refresh_from_db()
    return interface


def _mapping(librenms_type, netbox_type, speed=None):
    from netbox_librenms_plugin.models import InterfaceTypeMapping

    return InterfaceTypeMapping.objects.create(
        librenms_type=librenms_type,
        netbox_type=netbox_type,
        librenms_speed=speed,
    )


@pytest.mark.django_db
class TestUnmappedTypeIsNoOpinion:
    """No mapping means no opinion, not ``other``."""

    def test_an_unmapped_iftype_returns_no_opinion(self):
        from netbox_librenms_plugin.interface_sync import get_netbox_interface_type

        _mapping("ethernetCsmacd", "1000base-t")

        assert get_netbox_interface_type({"ifType": "someVendorType", "ifSpeed": None}) is None

    def test_an_unmapped_iftype_keeps_the_existing_netbox_type(self):
        """The defect: a correct ``lag`` type was overwritten with ``other``."""
        device = make_device("no-opinion-keep")
        interface = make_interface(device, "ae0", iface_type="lag")

        synced = _sync(device, _port(ifName="ae0", ifType="someVendorType", port_id=9101))

        assert synced.pk == interface.pk
        assert synced.type == "lag"

    def test_an_unmapped_iftype_still_writes_the_other_fields(self):
        """Skipping the type must not skip the rest of the row."""
        device = make_device("no-opinion-other-fields")
        make_interface(device, "ae0", iface_type="lag")

        synced = _sync(device, _port(ifName="ae0", ifType="someVendorType", ifMtu=9000, port_id=9102))

        assert synced.mtu == 9000

    def test_a_new_interface_with_an_unmapped_iftype_falls_back_to_other(self):
        """A created interface has no type to preserve, so the default is still written."""
        device = make_device("no-opinion-create")

        synced = _sync(device, _port(ifName="ge-0/0/9", ifType="someVendorType", port_id=9103))

        assert synced.type == "other"

    def test_a_mapped_iftype_still_overwrites_the_existing_type(self):
        """Positive control: the no-opinion rule must not disable a real mapping."""
        device = make_device("no-opinion-mapped")
        make_interface(device, "Ethernet1", iface_type="virtual")
        _mapping("ethernetCsmacd", "1000base-t", speed=1_000_000)

        synced = _sync(device, _port(port_id=9104))

        assert synced.type == "1000base-t"


@pytest.mark.django_db
class TestSeededLagMapping:
    """Migration 0020 seeds the mapping every LAG aggregate needs."""

    def test_the_lag_mapping_is_seeded(self):
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        seeded = InterfaceTypeMapping.objects.filter(librenms_type="ieee8023adLag", librenms_speed__isnull=True)

        assert seeded.count() == 1
        assert seeded.first().netbox_type == "lag"

    def test_an_aggregate_syncs_as_a_lag_without_a_manual_mapping(self):
        """NetBox refuses LAG member assignment until the aggregate is typed ``lag``."""
        device = make_device("seeded-lag")

        synced = _sync(device, _port(ifName="ae0", ifType="ieee8023adLag", ifSpeed=None, port_id=9105))

        assert synced.type == "lag"

    def test_an_aggregate_type_survives_a_second_sync(self):
        """The seeded mapping must agree with itself, not flip the type back."""
        device = make_device("seeded-lag-twice")
        port = _port(ifName="ae0", ifType="ieee8023adLag", ifSpeed=None, port_id=9106)

        _sync(device, port)

        assert _sync(device, port).type == "lag"


@pytest.mark.django_db
class TestTableAgreesWithTheWriter:
    """The row tells the user what the sync will do, so both must resolve the same mapping."""

    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        return LibreNMSInterfaceTable(
            data=[],
            device=None,
            interface_name_field="ifName",
            server_key="default",
        )

    def test_the_table_applies_the_writers_speed_rule(self):
        """A speed row at or below the port speed applies; the table used to need an exact hit."""
        from netbox_librenms_plugin.interface_sync import get_netbox_interface_type

        _mapping("ethernetCsmacd", "1000base-t", speed=1_000_000)

        written = get_netbox_interface_type({"ifType": "ethernetCsmacd", "ifSpeed": 10_000_000_000})
        shown = self._table().get_interface_mapping("ethernetCsmacd", 10_000_000)

        assert written == "1000base-t"
        assert shown is not None and shown.netbox_type == written

    def test_a_speed_row_above_the_port_speed_does_not_apply(self):
        """The other side of the same rule, so the test above cannot pass for the wrong reason."""
        _mapping("ethernetCsmacd", "10gbase-x-sfpp", speed=10_000_000)

        assert self._table().get_interface_mapping("ethernetCsmacd", 1_000_000) is None

    def test_the_missing_mapping_tooltip_names_the_iftype(self):
        """The gap is only fixable if the row says which ifType has no mapping."""
        display, icon = self._table().render_mapping_tooltip("someVendorType", 1_000_000, None)

        assert display == "someVendorType"
        assert "someVendorType" in str(icon)

    def test_the_mapping_snapshot_is_still_taken_once(self, django_assert_num_queries):
        """The shared resolver must not reintroduce a query per row."""
        _mapping("ethernetCsmacd", "1000base-t", speed=1_000_000)
        table = self._table()

        with django_assert_num_queries(1):
            for speed in range(5):
                table.get_interface_mapping("ethernetCsmacd", speed)


@pytest.mark.django_db
class TestSeededLagMappingReverse:
    """Rolling migration 0020 back must not take an operator's own row with it."""

    @staticmethod
    def _run_reverse():
        import importlib

        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        mod = importlib.import_module("netbox_librenms_plugin.migrations.0020_seed_lag_interface_type_mapping")
        historical_apps = (
            MigrationExecutor(connection)
            .loader.project_state([("netbox_librenms_plugin", "0020_seed_lag_interface_type_mapping")])
            .apps
        )
        with connection.schema_editor() as editor:
            mod.remove_lag_mapping(historical_apps, editor)

    def test_the_untouched_seed_row_is_removed(self):
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        self._run_reverse()

        assert not InterfaceTypeMapping.objects.filter(librenms_type="ieee8023adLag").exists()

    def test_a_repointed_row_survives(self):
        """The operator changed what the mapping means, so it is their row now."""
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        InterfaceTypeMapping.objects.filter(librenms_type="ieee8023adLag").update(netbox_type="virtual")

        self._run_reverse()

        assert InterfaceTypeMapping.objects.filter(librenms_type="ieee8023adLag", netbox_type="virtual").exists()
