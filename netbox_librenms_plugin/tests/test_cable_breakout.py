"""Cable sync on a breakout channel writes one profiled cable on the channelized parent interface."""

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

    def test_channel_two_creates_the_cable_with_the_known_far_end_of_channel_one(self):
        setup = _Breakout("lower-known")
        setup.seed(setup.row(1), setup.row(2))

        setup.sync(2)

        assert setup.b_side(setup.parent_cable()) == [(1, setup.far_ends[0]), (2, setup.far_ends[1])]
        records = setup.records()
        assert [records[row_id]["can_create_cable"] for row_id in ("101", "102")] == [False, False]

    @pytest.mark.parametrize(
        ("sync_first", "reason"),
        [
            (True, "channel ID 2 has no far end on the cable on et-0/0/1 yet, so sync it first"),
            (False, "channel ID 2 has no known far end, and NetBox numbers the far ends in order"),
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
            ("", True, "has no breakout-1c4p-4c1p profile"),
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
    setup.sync(2)
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
