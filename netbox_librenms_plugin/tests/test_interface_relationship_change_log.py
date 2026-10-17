"""
The relationship pass of the interface sync records each row it writes in the change log, as NetBox does.

Each write has one change record with the row's state before and after the write, and it moves the
row's ``last_updated``. The tests post the interface sync end to end; each posts one selected row
whose attributes are already in sync, so only the relationship pass writes.
"""

import pytest
from core.models import ObjectChange
from dcim.models import Interface
from django.contrib.contenttypes.models import ContentType

from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device, make_superuser
from netbox_librenms_plugin.tests.interface_sync_post_helpers import (
    SERVER_KEY,
    SYNCED,
    bound_interface,
    post_interface_sync,
    seed_ports,
    sync_port,
)
from netbox_librenms_plugin.tests.view_test_helpers import messages_on


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


def _synced_interface(device, name, port_id, *, iface_type="other"):
    """Return an interface that a sync of ``sync_port(port_id, name)`` leaves unchanged."""
    interface = bound_interface(device, name, port_id, iface_type=iface_type)
    Interface.objects.filter(pk=interface.pk).update(speed=1_000_000, mtu=1500, enabled=True)
    return interface


def _lag_link_and_aggregate_type(device):
    """The member gets its LAG; the aggregate is promoted to type ``lag``."""
    member = _synced_interface(device, "eth1", 1)
    aggregate = _synced_interface(device, "Po1", 2)
    ports = [sync_port(1, "eth1"), sync_port(2, "Po1")]
    changes = {member.pk: {"lag": (None, aggregate.pk)}, aggregate.pk: {"type": ("other", "lag")}}
    return ports, {"lag_members": {1: 2}}, changes


def _parent_link_and_child_type(device):
    """The child gets its parent and is promoted to type ``virtual``: one row, one write."""
    child = _synced_interface(device, "eth1.100", 11)
    parent = _synced_interface(device, "eth1", 10)
    ports = [sync_port(11, "eth1.100"), sync_port(10, "eth1")]
    changes = {child.pk: {"parent": (None, parent.pk), "type": ("other", "virtual")}}
    return ports, {"sub_interfaces": {11: 10}}, changes


def _bridge_link(device):
    """The member gets its bridge."""
    member = _synced_interface(device, "eth2", 20)
    bridge = _synced_interface(device, "br0", 21, iface_type="bridge")
    ports = [sync_port(20, "eth2"), sync_port(21, "br0", if_type="bridge")]
    return ports, {"bridge_members": {20: 21}}, {member.pk: {"bridge": (None, bridge.pk)}}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "edge",
    [_lag_link_and_aggregate_type, _parent_link_and_child_type, _bridge_link],
    ids=["lag", "parent", "bridge"],
)
def test_a_relationship_write_records_the_row_before_and_after_and_moves_last_updated(client, edge):
    device = make_device(f"relationship-change-log{edge.__name__}", librenms_cf={SERVER_KEY: {"id": 30}})
    ports, relationships, changes = edge(device)
    seed_ports(device, ports, **relationships)
    last_updated = dict(Interface.objects.filter(pk__in=changes).values_list("pk", "last_updated"))
    client.force_login(make_superuser("relationship-change-log-user"))

    response = post_interface_sync(client, device, [ports[0]["port_id"]], htmx=False)

    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]
    records = ObjectChange.objects.filter(changed_object_type=ContentType.objects.get_for_model(Interface))
    assert sorted(records.values_list("changed_object_id", flat=True)) == sorted(changes)
    for pk, fields in changes.items():
        record = records.get(changed_object_id=pk)
        assert {field: record.prechange_data[field] for field in fields} == {
            field: before for field, (before, _) in fields.items()
        }
        assert {field: record.postchange_data[field] for field in fields} == {
            field: after for field, (_, after) in fields.items()
        }
        assert Interface.objects.get(pk=pk).last_updated > last_updated[pk]
