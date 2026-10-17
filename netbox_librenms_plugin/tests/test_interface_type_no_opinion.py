"""An unmapped LibreNMS ifType must leave the NetBox interface type alone.

The type selector used to return the literal ``"other"`` when no ``InterfaceTypeMapping``
matched, and the writer wrote it unconditionally, so a correct type (``lag`` above all) was
flattened by a sync that simply had no mapping for the port.
"""

import pytest
from dcim.choices import InterfaceTypeChoices

from netbox_librenms_plugin.interface_rules import InterfaceRuleMatcher, RuleDecisionKind
from netbox_librenms_plugin.tests.conftest import make_device, make_interface, stamp_rule_decision


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
        rules=InterfaceRuleMatcher.load(),
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
        _mapping("ethernetCsmacd", "1000base-t")

        decision = InterfaceRuleMatcher.load().decide(_port(ifType="someVendorType", ifSpeed=None), platform_id=None)

        assert (decision.kind, decision.netbox_type) == (RuleDecisionKind.UNMAPPED, None)

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
class TestOnlyACreatedInterfaceSkipsTheTypeCheck:
    """The interface a sync creates takes the mapped type; a row that already existed is checked, even with no type."""

    def test_a_created_interface_is_not_held_by_an_unrelated_netbox_error(self):
        from netbox_librenms_plugin.tests.conftest import make_required_interface_custom_field

        device = make_device("created-unchecked")
        make_required_interface_custom_field("created_unchecked_code")
        _mapping("ethernetCsmacd", "1000base-t")

        assert _sync(device, _port(ifName="Ethernet7", port_id=9107)).type == "1000base-t"

    @pytest.mark.skipif(
        not hasattr(InterfaceTypeChoices, "TYPE_CHANNEL"),
        reason="this NetBox has no channel interface type",
    )
    def test_a_created_interface_takes_a_type_its_empty_row_cannot_pass(self):
        """NetBox's clean() needs a channel ID for a channel interface, which a new row cannot have yet."""
        device = make_device("created-channel")
        _mapping("ethernetCsmacd", "channel")

        assert _sync(device, _port(ifName="Ethernet8", port_id=9108)).type == "channel"

    def test_an_existing_row_with_no_type_is_checked(self):
        """A type-less row that is a child of a parent keeps its type: NetBox refuses a physical child."""
        device = make_device("typeless-checked")
        parent = make_interface(device, "Ethernet9", iface_type="1000base-t")
        child = make_interface(device, "Ethernet9.10", iface_type="")
        child.parent = parent
        child.save()
        _mapping("ethernetCsmacd", "1000base-t")

        synced = _sync(device, _port(ifName="Ethernet9.10", ifDescr="Ethernet9.10", port_id=9109))

        assert synced.pk == child.pk
        assert (synced.type, synced.parent_id) == ("", parent.pk)


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

    def _type_cell(self, **port):
        record = stamp_rule_decision({**_port(**port), "exists_in_netbox": False, "netbox_interface": None})
        return str(self._table().render_type(record["ifType"], record))

    def test_the_table_applies_the_writers_speed_rule(self):
        """A speed row at or below the port speed applies; the table used to need an exact hit."""
        from dcim.models import Interface

        _mapping("ethernetCsmacd", "1000base-t", speed=1_000_000)
        device = make_device("table-agrees-speed")

        written = _sync(device, _port(ifSpeed=10_000_000_000)).type

        assert written == Interface.objects.get(device=device).type == "1000base-t"
        assert written in self._type_cell(ifSpeed=10_000_000_000)

    def test_a_speed_row_above_the_port_speed_does_not_apply(self):
        """The other side of the same rule, so the test above cannot pass for the wrong reason."""
        _mapping("ethernetCsmacd", "10gbase-x-sfpp", speed=10_000_000)

        cell = self._type_cell(ifSpeed=1_000_000_000)

        assert "10gbase-x-sfpp" not in cell
        assert "mdi-link-variant-off" in cell

    def test_the_missing_mapping_tooltip_names_the_iftype(self):
        """The gap is only fixable if the row says which ifType has no mapping."""
        cell = self._type_cell(ifType="someVendorType", ifSpeed=1_000_000_000)

        assert "No interface rule sets a type for ifType someVendorType" in cell

    def test_the_type_column_queries_no_rules(self, django_assert_num_queries):
        """The view stamps one decision per row, so the column must not reintroduce a query per row."""
        _mapping("ethernetCsmacd", "1000base-t", speed=1_000_000)
        rules = InterfaceRuleMatcher.load()
        records = [
            stamp_rule_decision({**_port(ifSpeed=speed), "exists_in_netbox": False}, rules=rules) for speed in range(5)
        ]
        table = self._table()

        with django_assert_num_queries(0):
            for record in records:
                table.render_type(record["ifType"], record)


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
            operation = mod.Migration.operations[0]
            operation.code(historical_apps, editor)
            operation.reverse_code(historical_apps, editor)

    def test_the_untouched_seed_row_survives_an_unidentifiable_reverse(self):
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        self._run_reverse()

        assert InterfaceTypeMapping.objects.filter(librenms_type="ieee8023adLag").exists()

    def test_a_repointed_row_survives(self):
        """The operator changed what the mapping means, so it is their row now."""
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        InterfaceTypeMapping.objects.filter(librenms_type="ieee8023adLag").update(netbox_type="virtual")

        self._run_reverse()

        assert InterfaceTypeMapping.objects.filter(librenms_type="ieee8023adLag", netbox_type="virtual").exists()


@pytest.mark.django_db
def test_lag_seed_roundtrip_keeps_a_preexisting_matching_mapping():
    from netbox_librenms_plugin.models import InterfaceTypeMapping

    InterfaceTypeMapping.objects.filter(librenms_type="ieee8023adLag").update(description="Operator mapping")
    TestSeededLagMappingReverse._run_reverse()
    assert InterfaceTypeMapping.objects.filter(
        librenms_type="ieee8023adLag", netbox_type="lag", description="Operator mapping"
    ).exists()
