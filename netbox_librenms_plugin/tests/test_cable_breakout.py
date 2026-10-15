"""Cable sync on a breakout channel writes one profiled cable on the channelized parent interface."""

import json

import pytest
from dcim.models import Cable, Interface
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    bind_librenms_server,
    configured_server_key,
    librenms_cable_tag,
    make_device,
    make_interface,
    make_superuser,
    persist_test_server_mapping,
)
from netbox_librenms_plugin.tests.test_cable_overwrite import _rendered_sync_data

SERVER_KEY = configured_server_key()
PROFILE = "breakout-1c4p-4c1p"

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not hasattr(Interface, "channel_id"), reason="NetBox before 4.7 has no channel interfaces"),
]


class _Breakout:
    """A channelized parent with its channels, and a peer with one plain port per channel."""

    def __init__(self, tag, channels=4):
        self.device = make_device(f"breakout-{tag}")
        persist_test_server_mapping(self.device, SERVER_KEY)
        self.parent = Interface.objects.create(
            device=self.device, name="et-0/0/1", type="100gbase-x-qsfp28", channels=channels
        )
        self.channels = [
            Interface.objects.create(
                device=self.device,
                name=f"et-0/0/1:{lane}",
                type="25gbase-x-sfp28",
                parent=self.parent,
                channel_id=lane + 1,
            )
            for lane in range(channels)
        ]
        self.peer = make_device(f"breakout-{tag}-peer")
        self.far_ends = [make_interface(self.peer, f"Ethernet{lane + 1}") for lane in range(channels)]
        self.client = Client()
        self.client.force_login(make_superuser())

    def row(self, channel_id, far_end=None):
        """One LibreNMS LLDP row from channel *channel_id* to its peer port; the row id is the local port id."""
        far_end = far_end or self.far_ends[channel_id - 1]
        return {
            "_source": "main",
            "local_port_id": 100 + channel_id,
            "local_port": self.channels[channel_id - 1].name,
            "remote_device": far_end.device.name,
            "remote_port": far_end.name,
            "remote_port_id": 200 + channel_id,
        }

    def seed(self, *rows, device=None):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        device = device or self.device
        cache_key = object.__new__(SyncCablesView).get_cache_key(device, "links", SERVER_KEY)
        cache.set(cache_key, {"links": list(rows), "snapshot_token": f"breakout-{device.pk}"}, timeout=300)

    def records(self, device=None):
        """Return the table records by row id, from a real page render."""
        device = device or self.device
        rendered = self.client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
            {"tab": "cables", "server_key": SERVER_KEY},
        )
        assert rendered.status_code == 200
        return {str(row.record["row_id"]): row.record for row in rendered.context["cable_sync"]["table"].rows}

    def sync_rows(self, channel_ids):
        """POST one Sync Cable submit that selects the rows of *channel_ids*, in that order."""
        data = {}
        for channel_id in channel_ids:
            data.update(_rendered_sync_data(self.client, self.device, str(100 + channel_id)))
        data["select"] = [str(100 + channel_id) for channel_id in channel_ids]
        response = self.client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[self.device.pk]), data
        )
        assert response.status_code == 302
        return [str(message) for message in get_messages(response.wsgi_request)]

    def verify(self, channel_id):
        """Return the row that the verify endpoint renders for the row of *channel_id*."""
        response = self.client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            data=json.dumps({"device_id": self.device.pk, "row_id": str(100 + channel_id), "server_key": SERVER_KEY}),
            content_type="application/json",
        )
        assert response.status_code == 200
        return response.json()["formatted_row"]

    def sync(self, channel_id, device=None):
        """POST the Sync Cable form of the row for *channel_id*, and return the flashed messages."""
        device = device or self.device
        row_id = str(100 + channel_id)
        data = _rendered_sync_data(self.client, device, row_id)
        response = self.client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[device.pk]), data
        )
        assert response.status_code == 302
        return [str(message) for message in get_messages(response.wsgi_request)]

    def parent_cable(self):
        return Interface.objects.get(pk=self.parent.pk).cable

    @staticmethod
    def b_side(cable):
        """Return the B terminations of *cable* by connector."""
        return [
            (termination.connector, termination.termination)
            for termination in cable.terminations.filter(cable_end="B").order_by("connector")
        ]


@pytest.fixture
def breakout_server(librenms_server, settings):
    bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)


@pytest.mark.usefixtures("breakout_server")
class TestBreakoutCableWrites:
    def test_channel_one_creates_a_profiled_cable_on_the_parent(self):
        setup = _Breakout("create")
        setup.seed(setup.row(1))

        before = setup.records()["101"]
        texts = setup.sync(1)

        cable = setup.parent_cable()
        assert (before["cable_status"], before["can_create_cable"]) == ("No Cable", True)
        assert "Successfully created cable for interfaces: et-0/0/1:0" in texts
        assert cable.profile == PROFILE
        assert list(cable.a_terminations) == [setup.parent]
        assert setup.b_side(cable) == [(1, setup.far_ends[0])]
        assert librenms_cable_tag() in cable.tags.all()
        assert cable.description.endswith(f"({SERVER_KEY})")
        channel = Interface.objects.get(pk=setup.channels[0].pk)
        assert (channel.cable_id, channel.cable_positions) == (cable.pk, [1])
        after = setup.records()["101"]
        assert (after["cable_status"], after["can_create_cable"]) == ("Cable Found", False)

    def test_channel_two_extends_the_cable_the_plugin_wrote(self):
        setup = _Breakout("extend")
        setup.seed(setup.row(1), setup.row(2))
        setup.sync(1)

        before = setup.records()["102"]
        texts = setup.sync(2)

        cable = setup.parent_cable()
        assert (before["cable_status"], before["can_create_cable"]) == ("No Cable", True)
        assert "Successfully created cable for interfaces: et-0/0/1:1" in texts
        assert Cable.objects.count() == 1
        assert setup.b_side(cable) == [(1, setup.far_ends[0]), (2, setup.far_ends[1])]
        records = setup.records()
        assert [records[row_id]["cable_status"] for row_id in ("101", "102")] == ["Cable Found", "Cable Found"]

    def test_channel_two_writes_nothing_while_channel_one_has_no_cable_also_when_channel_one_is_ignored(self):
        from netbox_librenms_plugin.models import InterfaceTypeMapping
        from netbox_librenms_plugin.tests.test_interface_rule_writes import _port
        from netbox_librenms_plugin.views.base.cables_view import port_record

        setup = _Breakout("lower-ignored")
        InterfaceTypeMapping.objects.create(action=InterfaceTypeMapping.ACTION_IGNORE, name_pattern=r"^et-0/0/1:0$")
        rows = [
            {
                **setup.row(channel_id),
                "local_port_record": port_record(_port(100 + channel_id, setup.channels[channel_id - 1].name)),
                "remote_port_record": port_record(_port(200 + channel_id, setup.far_ends[channel_id - 1].name)),
                "remote_port_key": 200 + channel_id,
            }
            for channel_id in (1, 2)
        ]
        setup.seed(*rows)

        texts = setup.sync(2)

        reason = "channel ID 1 has no far end on et-0/0/1 yet, so sync channel ID 1 first"
        assert any(reason in text for text in texts), texts
        assert setup.parent_cable() is None
        assert [Interface.objects.get(pk=far.pk).cable_id for far in setup.far_ends[:2]] == [None, None]

    def test_the_table_and_the_verify_agree_on_channel_two(self):
        setup = _Breakout("verify-agrees")
        setup.seed(setup.row(1), setup.row(2))

        record = setup.records()["102"]
        verified = setup.verify(2)

        reason = "channel ID 1 has no far end on et-0/0/1 yet, so sync channel ID 1 first"
        assert (record["cable_status"], record["can_create_cable"]) == (f"Breakout Not Synced: {reason}", False)
        assert (verified["cable_status"], verified["can_create_cable"]) == (f"Breakout Not Synced: {reason}", False)

    def test_one_submit_writes_the_channels_in_channel_order(self):
        setup = _Breakout("bulk")
        setup.seed(*(setup.row(channel_id) for channel_id in (1, 2, 3, 4)))

        setup.sync_rows([4, 2, 3, 1])

        assert Cable.objects.count() == 1
        assert setup.b_side(setup.parent_cable()) == [(lane + 1, far) for lane, far in enumerate(setup.far_ends)]

    def test_the_rendered_table_lets_one_submit_select_every_channel(self):
        import re

        setup = _Breakout("bulk-table")
        setup.seed(*(setup.row(channel_id) for channel_id in (1, 2, 3, 4)))

        rendered = setup.client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[setup.device.pk]),
            {"tab": "cables", "server_key": SERVER_KEY},
        )
        # Read the table HTML the way the other cable table tests do.
        page = rendered.context["cable_sync"]["table"].as_html(rendered.wsgi_request)
        checkboxes = re.findall(r'<input[^>]*name="select"[^>]*>', page)
        enabled = sorted(
            int(re.search(r'value="(\d+)"', box).group(1)) - 100 for box in checkboxes if "disabled" not in box
        )
        sync_buttons = re.findall(r'name="sync_one"\s+value="(\d+)"', page)

        assert enabled == [1, 2, 3, 4]
        assert sync_buttons == ["101"]
        setup.sync_rows(enabled)
        assert setup.b_side(setup.parent_cable()) == [(lane + 1, far) for lane, far in enumerate(setup.far_ends)]

    def test_a_deleted_lower_far_end_never_moves_the_other_far_ends(self):
        setup = _Breakout("deleted-lower")
        setup.seed(setup.row(1), setup.row(2))
        setup.sync_rows([1, 2])
        setup.far_ends[0].delete()
        replacement = make_interface(setup.peer, "Ethernet9")
        setup.seed(setup.row(2, far_end=replacement), setup.row(3))

        texts = setup.sync(2)
        texts_3 = setup.sync(3)

        assert any("connector 2 of the cable on et-0/0/1 ends on another interface" in text for text in texts), texts
        reason = "channel ID 1 has no far end on et-0/0/1 yet, so sync channel ID 1 first"
        assert any(reason in text for text in texts_3), texts_3
        assert setup.b_side(setup.parent_cable()) == [(2, setup.far_ends[1])]
        assert Interface.objects.get(pk=replacement.pk).cable_id is None

    @pytest.mark.parametrize(
        ("sync_first", "reason"),
        [
            (True, "channel ID 2 has no far end on et-0/0/1 yet, so sync channel ID 2 first"),
            (False, "channel ID 1 has no far end on et-0/0/1 yet, so sync channel ID 1 first"),
        ],
        ids=["extend", "create"],
    )
    def test_a_channel_after_a_gap_is_refused(self, sync_first, reason):
        setup = _Breakout(f"gap-{sync_first}")
        setup.seed(setup.row(1), setup.row(3))
        if sync_first:
            setup.sync(1)

        before = setup.records()["103"]
        texts = setup.sync(3)

        assert reason in before["cable_status"]
        assert before["can_create_cable"] is False
        assert any("et-0/0/1:2" in text and reason in text for text in texts), texts
        cable = setup.parent_cable()
        assert (setup.b_side(cable) if sync_first else cable) == ([(1, setup.far_ends[0])] if sync_first else None)
        assert Interface.objects.get(pk=setup.far_ends[2].pk).cable_id is None

    @pytest.mark.parametrize(
        ("profile", "tagged", "reason"),
        [
            (PROFILE, False, "does not have the LibreNMS cable tag"),
            ("", True, "uses profile none, which this sync does not read"),
        ],
        ids=["untagged", "unprofiled"],
    )
    def test_another_cable_on_the_parent_is_refused_and_kept(self, profile, tagged, reason):
        setup = _Breakout(f"foreign-{tagged}")
        foreign = Cable(a_terminations=[setup.parent], b_terminations=[setup.far_ends[0]], profile=profile)
        foreign.save()
        if tagged:
            foreign.tags.add(librenms_cable_tag())
        setup.seed(setup.row(2))

        before = setup.records()["102"]
        texts = setup.sync(2)

        assert reason in before["cable_status"]
        assert any(reason in text for text in texts), texts
        assert Cable.objects.get(pk=foreign.pk).profile == profile
        assert setup.b_side(foreign) == [(1 if profile else None, setup.far_ends[0])]
        assert Interface.objects.get(pk=setup.far_ends[1].pk).cable_id is None

    def test_a_cable_with_another_breakout_profile_never_reads_as_matching(self):
        from netbox_librenms_plugin.utils import cable_far_terminations

        setup = _Breakout("shuffle")
        # On this profile, channel 3 of A connector 1 maps to B connector 5, not 3.
        shuffle = Cable(
            a_terminations=[setup.parent], b_terminations=setup.far_ends[:3], profile="breakout-2c4p-8c1p-shuffle"
        )
        shuffle.save()
        shuffle.tags.add(librenms_cable_tag())
        setup.seed(setup.row(3))

        record = setup.records()["103"]
        texts = setup.sync(3)

        reason = "the cable on et-0/0/1 uses profile breakout-2c4p-8c1p-shuffle, which this sync does not read"
        assert (record["cable_status"], record["can_create_cable"]) == (f"Breakout Not Synced: {reason}", False)
        assert any(reason in text for text in texts), texts
        channel = Interface.objects.get(pk=setup.channels[2].pk)
        assert cable_far_terminations(Cable.objects.get(pk=shuffle.pk), channel) == []

    def test_a_far_end_that_is_a_channel_is_refused(self):
        setup = _Breakout("far-channel")
        peer_parent = Interface.objects.create(device=setup.peer, name="et-9/0/0", type="100gbase-x-qsfp28", channels=4)
        peer_channel = Interface.objects.create(
            device=setup.peer, name="et-9/0/0:0", type="25gbase-x-sfp28", parent=peer_parent, channel_id=1
        )
        setup.seed(setup.row(1, far_end=peer_channel))

        texts = setup.sync(1)

        assert any("the far end is a channel" in text for text in texts), texts
        assert setup.parent_cable() is None

    def test_a_channel_count_without_a_breakout_profile_is_refused(self):
        setup = _Breakout("three", channels=3)
        setup.seed(setup.row(1))

        texts = setup.sync(1)

        assert any("NetBox has no breakout cable profile for 3 channels" in text for text in texts), texts
        assert setup.parent_cable() is None


@pytest.mark.usefixtures("breakout_server")
def test_a_port_that_is_not_a_channel_still_gets_a_plain_cable():
    setup = _Breakout("plain")
    plain = make_interface(setup.device, "et-0/0/9")
    row = {**setup.row(1), "local_port_id": 109, "local_port": plain.name}
    setup.seed(row)

    setup.sync(9)

    plain.refresh_from_db()
    cable = plain.cable
    assert cable.profile == ""
    assert (list(cable.a_terminations), list(cable.b_terminations)) == ([plain], [setup.far_ends[0]])


@pytest.mark.usefixtures("breakout_server")
def test_a_plain_port_whose_far_end_is_a_channel_is_refused_until_the_channel_side_writes_the_cable():
    setup = _Breakout("peer-side")
    persist_test_server_mapping(setup.peer, SERVER_KEY)
    # The peer's own row: its Ethernet1 sees channel 1 of the breakout device.
    peer_row = {
        "_source": "main",
        "local_port_id": 101,
        "local_port": setup.far_ends[0].name,
        "remote_device": setup.device.name,
        "remote_port": setup.channels[0].name,
        "remote_port_id": 301,
    }
    setup.seed(peer_row, device=setup.peer)
    setup.seed(setup.row(1))

    before = setup.records(setup.peer)["101"]
    texts = setup.sync(1, device=setup.peer)
    setup.sync(1)
    after = setup.records(setup.peer)["101"]

    reason = "the far end is a channel, so sync the cable from the device of the channel"
    assert (reason in before["cable_status"], before["can_create_cable"]) == (True, False)
    assert any(reason in text for text in texts), texts
    assert setup.b_side(setup.parent_cable()) == [(1, setup.far_ends[0])]
    assert (after["cable_status"], after["can_create_cable"]) == ("Cable Found", False)


@pytest.mark.usefixtures("breakout_server")
def test_a_channel_reads_the_far_end_at_its_connector_and_never_its_parent():
    from netbox_librenms_plugin.utils import cable_far_terminations

    setup = _Breakout("far-read")
    setup.seed(setup.row(1), setup.row(2))
    setup.sync_rows([1, 2])
    # The same channel, now towards a neighbour that NetBox does not know.
    setup.seed({**setup.row(1), "remote_device": "breakout-far-read-unknown"})

    record = setup.records()["101"]

    cable = setup.parent_cable()
    channels = [Interface.objects.get(pk=channel.pk) for channel in setup.channels[:2]]
    assert [cable_far_terminations(cable, channel) for channel in channels] == [
        [setup.far_ends[0]],
        [setup.far_ends[1]],
    ]
    assert record["cable_status"] == f"Cabled to {setup.peer.name}"


@pytest.mark.usefixtures("breakout_server")
def test_a_port_that_becomes_a_channel_before_the_lock_gets_no_cable(monkeypatch):
    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    setup = _Breakout("becomes-channel")
    plain = make_interface(setup.device, "et-0/0/9")
    other_parent = Interface.objects.create(device=setup.device, name="et-0/0/8", type="100gbase-x-qsfp28", channels=4)
    setup.seed({**setup.row(1), "local_port_id": 109, "local_port": plain.name})
    real_lock = SyncCablesView._lock_cable_terminations

    def channelize_then_lock(view, local_term, remote_term, **kwargs):
        # A concurrent change makes the row's port a channel after the read, before the lock.
        Interface.objects.filter(pk=plain.pk).update(parent=other_parent, channel_id=1)
        return real_lock(view, local_term, remote_term, **kwargs)

    monkeypatch.setattr(SyncCablesView, "_lock_cable_terminations", channelize_then_lock)

    texts = setup.sync(9)

    assert any("The cable state or target changed" in text for text in texts), texts
    assert Interface.objects.get(pk=plain.pk).cable_id is None
    assert Interface.objects.get(pk=setup.far_ends[0].pk).cable_id is None
    assert Cable.objects.count() == 0


@pytest.mark.usefixtures("breakout_server")
def test_a_channel_with_an_empty_connector_offers_to_create_its_far_end(librenms_server):
    from urllib.parse import parse_qs, urlsplit

    from netbox_librenms_plugin.tests.conftest import map_device_to_librenms

    setup = _Breakout("create-far")
    map_device_to_librenms(setup.peer, 9001, server_key=SERVER_KEY)
    librenms_server.register(
        "/api/v0/ports/202",
        {
            "status": "ok",
            "port": [
                {
                    "port_id": 202,
                    "device_id": 9001,
                    "deleted": 0,
                    "ifName": "Ethernet20",
                    "ifDescr": "Ethernet20",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 25_000_000_000,
                }
            ],
        },
    )
    unknown_far_end = {
        **setup.row(2),
        "remote_device_id": 9001,
        "remote_port": "Ethernet20",
        "remote_port_key": 202,
    }
    setup.seed(setup.row(1), unknown_far_end)
    setup.sync(1)

    record = setup.records()["102"]
    assert record["cable_status"] == "Remote Interface Not Found in Netbox"
    assert record.get("remote_create_url"), record
    query = {key: values[0] for key, values in parse_qs(urlsplit(record["remote_create_url"]).query).items()}
    response = setup.client.post(urlsplit(record["remote_create_url"]).path, query)

    assert response.status_code == 302, response.content.decode()
    created = Interface.objects.get(device=setup.peer, name="Ethernet20")
    assert setup.b_side(setup.parent_cable()) == [(1, setup.far_ends[0]), (2, created)]


def _records_row(channel_id, setup, record):
    """Return the port records and the matched remote key that let the rule gate decide a row offline."""
    from netbox_librenms_plugin.tests.test_interface_rule_writes import _port
    from netbox_librenms_plugin.views.base.cables_view import port_record

    return {
        **record,
        "local_port_record": port_record(_port(100 + channel_id, record["local_port"])),
        "remote_port_record": port_record(_port(record["remote_port_id"], record["remote_port"])),
        "remote_port_key": record["remote_port_id"],
    }


@pytest.mark.usefixtures("breakout_server")
def test_creating_the_far_end_of_a_channel_reads_the_record_of_a_bound_parent(librenms_server):
    from urllib.parse import parse_qs, urlsplit

    from netbox_librenms_plugin.models import InterfaceTypeMapping
    from netbox_librenms_plugin.tests.conftest import map_device_to_librenms
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.test_interface_rule_writes import _port

    setup = _Breakout("bound-parent")
    seed_mapping(setup.parent, SERVER_KEY, own=300, save=False)
    setup.parent.save()
    # Any Ignore rule makes a port without a record refuse the write.
    InterfaceTypeMapping.objects.create(action=InterfaceTypeMapping.ACTION_IGNORE, name_pattern=r"^no-such-port$")
    map_device_to_librenms(setup.peer, 9002, server_key=SERVER_KEY)
    librenms_server.register("/api/v0/ports/300", {"status": "ok", "port": [_port(300, setup.parent.name)]})
    librenms_server.register(
        "/api/v0/ports/202",
        {"status": "ok", "port": [{**_port(202, "Ethernet20"), "device_id": 9002, "deleted": 0}]},
    )
    unknown_far_end = {
        **_records_row(2, setup, {**setup.row(2), "remote_port": "Ethernet20"}),
        "remote_device_id": 9002,
    }
    setup.seed(_records_row(1, setup, setup.row(1)), unknown_far_end)
    texts = setup.sync(1)

    record = setup.records()["102"]
    assert record.get("remote_create_url"), (texts, record)
    query = {key: values[0] for key, values in parse_qs(urlsplit(record["remote_create_url"]).query).items()}
    response = setup.client.post(urlsplit(record["remote_create_url"]).path, query)

    created = Interface.objects.filter(device=setup.peer, name="Ethernet20").first()
    texts = [str(message) for message in get_messages(response.wsgi_request)]
    assert response.status_code == 302
    assert setup.b_side(setup.parent_cable()) == [(1, setup.far_ends[0]), (2, created)], texts


@pytest.mark.usefixtures("breakout_server")
def test_the_verify_keeps_a_waiting_channel_selectable_like_the_table():
    setup = _Breakout("verify-select")
    setup.seed(*(setup.row(channel_id) for channel_id in (1, 2, 3, 4)))

    record = setup.records()["102"]
    verified = setup.verify(2)

    assert (record["can_create_cable"], record["can_select_cable"]) == (False, True)
    assert (verified["can_create_cable"], verified.get("can_select_cable")) == (False, True)


@pytest.mark.usefixtures("breakout_server")
def test_one_submit_orders_the_channels_of_the_submitted_chassis_member():
    from netbox_librenms_plugin.server_selection import get_librenms_sync_device
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis

    setup = _Breakout("vc-order")
    page = make_device("breakout-vc-order-page")
    persist_test_server_mapping(page, SERVER_KEY)
    # The page member has plain ports with the channel names, so the cached rows resolve there.
    for channel in setup.channels:
        make_interface(page, channel.name)
    make_virtual_chassis("breakout-vc-order", page, setup.device)
    page.refresh_from_db()
    setup.seed(setup.row(1), setup.row(2), device=get_librenms_sync_device(page, server_key=SERVER_KEY))
    records = setup.records(page)
    data = {"select": ["102", "101"], "server_key": SERVER_KEY}
    for channel_id in (2, 1):
        row_id = str(100 + channel_id)
        data.update(
            {
                f"device_selection_{row_id}": setup.device.pk,
                f"expected_local_id_{row_id}": setup.channels[channel_id - 1].pk,
                f"expected_local_device_id_{row_id}": setup.device.pk,
                f"expected_remote_id_{row_id}": records[row_id]["netbox_remote_interface_id"],
                f"expected_remote_device_id_{row_id}": records[row_id]["netbox_remote_device_id"],
            }
        )

    response = setup.client.post(reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[page.pk]), data)

    texts = [str(message) for message in get_messages(response.wsgi_request)]
    assert setup.b_side(setup.parent_cable()) == [(1, setup.far_ends[0]), (2, setup.far_ends[1])], texts


@pytest.mark.usefixtures("breakout_server")
def test_a_user_who_may_not_change_the_parent_gets_no_sync_action():
    from dcim.models import Device

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    setup = _Breakout("parent-scope")
    user = make_user_with_perms(
        "breakout-parent-scope", [("view", Device), ("view", Interface), ("view", Cable), ("add", Cable)]
    )
    user = grant(user, "change", Cable)
    # Change on every interface except a channelized parent.
    user = grant(user, "change", Interface, constraints={"channels__isnull": True})
    setup.client.force_login(user)
    setup.seed(setup.row(1), setup.row(2))

    records = setup.records()

    assert [records[row_id]["can_create_cable"] for row_id in ("101", "102")] == [False, False]
    assert not records["102"].get("can_select_cable")
