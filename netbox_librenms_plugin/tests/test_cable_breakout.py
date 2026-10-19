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

    def test_the_table_offers_only_the_next_channel_of_the_parent(self):
        import re

        setup = _Breakout("next-channel")
        setup.seed(*(setup.row(channel_id) for channel_id in (1, 2, 3, 4)))

        for channel_id in (1, 2, 3, 4):
            rendered = setup.client.get(
                reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[setup.device.pk]),
                {"tab": "cables", "server_key": SERVER_KEY},
            )
            # Read the table HTML the way the other cable table tests do.
            page = rendered.context["cable_sync"]["table"].as_html(rendered.wsgi_request)
            checkboxes = re.findall(r'<input[^>]*name="select"[^>]*>', page)
            enabled = [re.search(r'value="(\d+)"', box).group(1) for box in checkboxes if "disabled" not in box]
            assert enabled == [str(100 + channel_id)]
            assert re.findall(r'name="sync_one"\s+value="(\d+)"', page) == [str(100 + channel_id)]
            setup.sync(channel_id)

        assert setup.b_side(setup.parent_cable()) == [(lane + 1, far) for lane, far in enumerate(setup.far_ends)]

    def test_a_deleted_lower_far_end_never_moves_the_other_far_ends(self):
        setup = _Breakout("deleted-lower")
        setup.seed(setup.row(1), setup.row(2))
        setup.sync(1)
        setup.sync(2)
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
    setup.sync(1)
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
def test_creating_the_far_end_of_a_channel_needs_no_record_of_a_bound_parent(librenms_server):
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
    # LibreNMS has no record of the parent's port 300: the rules never read it.
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


def _constrained_user(name, *, interface_view=None, cable_change=None, cable_view=None):
    from dcim.models import Device

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    user = make_user_with_perms(name, [("view", Device), ("add", Cable), ("add", Interface), ("change", Interface)])
    user = grant(user, "view", Interface, constraints=interface_view)
    user = grant(user, "view", Cable, constraints=cable_view)
    return grant(user, "change", Cable, constraints=cable_change)


@pytest.mark.usefixtures("breakout_server")
def test_a_hidden_parent_is_never_named_and_its_channels_get_no_action():
    setup = _Breakout("hidden-parent")
    setup.client.force_login(_constrained_user("breakout-hidden-parent", interface_view={"channels__isnull": True}))
    setup.seed(setup.row(1), setup.row(2))

    records = setup.records()
    verified = setup.verify(2)

    reason = "Breakout Not Synced: the parent interface of this channel is not available"
    assert [(records[row_id]["cable_status"], records[row_id]["can_create_cable"]) for row_id in ("101", "102")] == [
        (reason, False),
        (reason, False),
    ]
    assert (verified["cable_status"], verified["can_create_cable"]) == (reason, False)
    assert setup.parent.name not in verified["cable_status"]


@pytest.mark.usefixtures("breakout_server")
def test_an_extend_needs_change_on_the_existing_parent_cable():
    setup = _Breakout("cable-scope")
    setup.seed(setup.row(1), setup.row(2))
    setup.sync(1)
    setup.client.force_login(_constrained_user("breakout-cable-scope", cable_change={"label": "permitted"}))

    record = setup.records()["102"]
    verified = setup.verify(2)

    reason = "Breakout Not Synced: you may not change the cable on et-0/0/1"
    assert (record["cable_status"], record["can_create_cable"]) == (reason, False)
    # Verify links the status to the cable the user may view.
    assert (reason in verified["cable_status"], verified["can_create_cable"]) == (True, False)


def _missing_far_end(setup, librenms_server, channel_id, librenms_id):
    """Return the row of *channel_id* towards a peer port that LibreNMS has and NetBox does not."""
    from netbox_librenms_plugin.tests.conftest import map_device_to_librenms

    port_id = 200 + channel_id
    name = f"Ethernet{channel_id}0"
    map_device_to_librenms(setup.peer, librenms_id, server_key=SERVER_KEY)
    librenms_server.register(
        f"/api/v0/ports/{port_id}",
        {
            "status": "ok",
            "port": [
                {
                    "port_id": port_id,
                    "device_id": librenms_id,
                    "deleted": 0,
                    "ifName": name,
                    "ifDescr": name,
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 25_000_000_000,
                }
            ],
        },
    )
    return {**setup.row(channel_id), "remote_device_id": librenms_id, "remote_port": name, "remote_port_key": port_id}


def _post_create_remote(setup, channel_id):
    """POST the create-remote action of the row of *channel_id* the way its button would."""
    row_id = str(100 + channel_id)
    return setup.client.post(
        reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[setup.device.pk]),
        {"row_id": row_id, "expected_local_id": setup.channels[channel_id - 1].pk, "server_key": SERVER_KEY},
    )


@pytest.mark.usefixtures("breakout_server")
def test_a_direct_post_never_names_a_hidden_parent():
    setup = _Breakout("hidden-parent-post")
    setup.seed(setup.row(1), setup.row(2))
    setup.client.force_login(_constrained_user("breakout-hidden-post", interface_view={"channels__isnull": True}))

    texts = setup.sync(2)

    assert any("the parent interface of this channel is not available" in text for text in texts), texts
    assert not any("on et-0/0/1 " in text for text in texts), texts
    assert setup.parent_cable() is None


@pytest.mark.usefixtures("breakout_server")
def test_create_remote_is_offered_only_on_the_next_channel(librenms_server):
    setup = _Breakout("create-far-gap")
    setup.seed(setup.row(1), _missing_far_end(setup, librenms_server, 3, 9003))
    setup.sync(1)

    record = setup.records()["103"]
    verified = setup.verify(3)
    response = _post_create_remote(setup, 3)

    reason = "Breakout Not Synced: channel ID 2 has no far end on et-0/0/1 yet, so sync channel ID 2 first"
    assert (record["cable_status"], record.get("remote_create_url")) == (reason, None)
    assert verified["cable_status"] == reason
    assert "cable-remote-create" not in verified["actions"] and "remote-create" not in verified["actions"]
    assert response.status_code == 404
    assert not Interface.objects.filter(device=setup.peer, name="Ethernet30").exists()
    assert setup.b_side(setup.parent_cable()) == [(1, setup.far_ends[0])]


@pytest.mark.usefixtures("breakout_server")
def test_create_remote_is_not_offered_under_a_hidden_parent(librenms_server):
    setup = _Breakout("create-far-hidden")
    setup.seed(setup.row(1), _missing_far_end(setup, librenms_server, 2, 9004))
    setup.sync(1)
    setup.client.force_login(_constrained_user("breakout-create-hidden", interface_view={"channels__isnull": True}))

    record = setup.records()["102"]
    verified = setup.verify(2)
    response = _post_create_remote(setup, 2)

    reason = "Breakout Not Synced: the parent interface of this channel is not available"
    assert (record["cable_status"], record.get("remote_create_url")) == (reason, None)
    assert verified["cable_status"] == reason
    assert response.status_code == 404
    assert not Interface.objects.filter(device=setup.peer, name="Ethernet20").exists()


@pytest.mark.usefixtures("breakout_server")
def test_an_ignore_rule_on_the_parent_port_does_not_block_the_channel(librenms_server):
    from netbox_librenms_plugin.models import InterfaceTypeMapping
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.test_interface_rule_writes import _port

    setup = _Breakout("ignored-parent")
    seed_mapping(setup.parent, SERVER_KEY, own=300, save=False)
    setup.parent.save()
    # The rule matches only the parent's own port; the rules decide the channel's port.
    InterfaceTypeMapping.objects.create(action=InterfaceTypeMapping.ACTION_IGNORE, name_pattern=r"^et-0/0/1$")
    librenms_server.register("/api/v0/ports/300", {"status": "ok", "port": [_port(300, setup.parent.name)]})
    setup.seed(_records_row(1, setup, setup.row(1)))

    record = setup.records()["101"]
    verified = setup.verify(1)
    texts = setup.sync(1)

    assert (record["can_create_cable"], record.get("rule_block")) == (True, None)
    assert verified["can_create_cable"] is True
    assert "Successfully created cable for interfaces: et-0/0/1:0" in texts
    assert setup.b_side(setup.parent_cable()) == [(1, setup.far_ends[0])]


@pytest.mark.usefixtures("breakout_server")
@pytest.mark.usefixtures("breakout_server")
def test_the_channels_of_one_parent_share_its_permission_checks():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    setup = _Breakout("shared-checks")
    setup.seed(setup.row(1), setup.row(2))
    setup.sync(1)
    parent_check = f'FROM "dcim_interface" WHERE "dcim_interface"."id" = {setup.parent.pk} LIMIT 1'
    cable_check = f'FROM "dcim_cable" WHERE "dcim_cable"."id" = {setup.parent_cable().pk} LIMIT 1'

    def checks(*channel_ids):
        setup.seed(*(setup.row(channel_id) for channel_id in channel_ids))
        with CaptureQueriesContext(connection) as queries:
            setup.records()
        return [sum(check in query["sql"] for query in queries) for check in (parent_check, cable_check)]

    one_row = checks(2)
    assert one_row[0] > 0, "precondition: the gate checks the parent"
    assert checks(2, 3, 4) == one_row


def test_a_cached_render_of_a_channel_row_never_calls_librenms(librenms_server):
    from netbox_librenms_plugin.models import InterfaceTypeMapping
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    setup = _Breakout("cached-render")
    seed_mapping(setup.parent, SERVER_KEY, own=300, save=False)
    setup.parent.save()
    InterfaceTypeMapping.objects.create(action=InterfaceTypeMapping.ACTION_IGNORE, name_pattern=r"^et-0/0/1$")
    setup.seed(_records_row(1, setup, setup.row(1)))
    before = len(librenms_server.requests)

    page = setup.client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[setup.device.pk]),
        {"tab": "cables", "server_key": SERVER_KEY},
    )
    after_page = len(librenms_server.requests)
    fragment = setup.client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": setup.device.pk, "tab": "cables"},
        ),
        {"server_key": SERVER_KEY},
    )

    assert (page.status_code, fragment.status_code) == (200, 200)
    # The page shell reads the device and its poller group; the cached cable table reads nothing.
    page_paths = [request["path"] for request in librenms_server.requests[before:after_page]]
    assert [path for path in page_paths if "/ports" in path] == []
    assert [request["path"] for request in librenms_server.requests[after_page:]] == []


@pytest.mark.usefixtures("breakout_server")
def test_a_confirmed_overwrite_from_a_far_end_never_removes_the_breakout_cable():
    from netbox_librenms_plugin.tests.test_cable_overwrite import _confirmed_intent

    setup = _Breakout("far-end-overwrite")
    setup.seed(setup.row(1))
    setup.sync(1)
    cable = setup.parent_cable()
    persist_test_server_mapping(setup.peer, SERVER_KEY)
    other = make_interface(make_device("breakout-far-end-overwrite-q"), "Ethernet7")
    # The peer's Ethernet1 (far end of channel 1) now reports a plain neighbour.
    setup.seed(
        {
            "_source": "main",
            "local_port_id": 101,
            "local_port": setup.far_ends[0].name,
            "remote_device": other.device.name,
            "remote_port": other.name,
            "remote_port_id": 401,
        },
        device=setup.peer,
    )

    record = setup.records(setup.peer)["101"]
    far_end = Interface.objects.get(pk=setup.far_ends[0].pk)
    data = {
        **_rendered_sync_data(setup.client, setup.peer, "101"),
        "force": "on",
        "expected_cable_intent_101": _confirmed_intent(far_end, other, cable),
    }
    response = setup.client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[setup.peer.pk]), data
    )

    texts = [str(message) for message in get_messages(response.wsgi_request)]
    assert Cable.objects.filter(pk=cable.pk).exists(), texts
    assert (record["cable_status"], record["can_create_cable"]) == ("Multi-termination Cable Not Supported", False)
    assert setup.b_side(cable) == [(1, setup.far_ends[0])]
    assert Interface.objects.get(pk=other.pk).cable_id is None


SINGLE = "single-1c1p"


def _plain_pair(tag):
    """A page device with port eth0, a peer with port Gi0/1, and the page's cable row towards it."""
    device = make_device(f"single-{tag}")
    persist_test_server_mapping(device, SERVER_KEY)
    local = make_interface(device, "eth0")
    peer = make_device(f"single-{tag}-peer")
    remote = make_interface(peer, "Gi0/1")
    row = {"_source": "main", "local_port_id": 100, "local_port": "eth0", "remote_device": peer.name}
    return device, local, remote, {**row, "remote_port": "Gi0/1", "remote_port_id": 500}


def _single_cable(a, b, *, tagged):
    cable = Cable(a_terminations=[a], b_terminations=[b], profile=SINGLE)
    cable.save()
    if tagged:
        cable.tags.add(librenms_cable_tag())
    return cable


def _sync_plain(client, device, data):
    response = client.post(reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[device.pk]), data)
    return [str(message) for message in get_messages(response.wsgi_request)]


@pytest.mark.usefixtures("breakout_server")
class TestASingleProfileCableIsPointToPoint:
    """A single-* profile has one connector on each side, so the plain path treats it as an unprofiled cable."""

    def _client(self, device, row):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        cache.set(
            object.__new__(SyncCablesView).get_cache_key(device, "links", SERVER_KEY),
            {"links": [row], "snapshot_token": f"single-{device.pk}"},
            timeout=300,
        )
        client = Client()
        client.force_login(make_superuser())
        return client

    def _record(self, client, device):
        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
            {"tab": "cables", "server_key": SERVER_KEY},
        )
        return next(iter(rendered.context["cable_sync"]["table"].rows)).record

    def test_a_tagged_single_profile_cable_reads_as_found(self):
        device, local, remote, row = _plain_pair("tagged")
        _single_cable(local, remote, tagged=True)
        client = self._client(device, row)

        record = self._record(client, device)

        assert (record["cable_status"], record["can_create_cable"]) == ("Cable Found", False)

    def test_an_untagged_single_profile_cable_is_adopted(self):
        device, local, remote, row = _plain_pair("untagged")
        cable = _single_cable(local, remote, tagged=False)
        client = self._client(device, row)

        record = self._record(client, device)
        texts = _sync_plain(client, device, _rendered_sync_data(client, device, "100"))

        assert (record["cable_status"], record["can_create_cable"]) == ("Cable Found", True)
        assert "Tagged existing cable(s) as LibreNMS-managed for: eth0" in texts
        assert librenms_cable_tag() in Cable.objects.get(pk=cable.pk).tags.all()

    def test_a_confirmed_overwrite_replaces_a_single_profile_cable(self):
        from netbox_librenms_plugin.tests.test_cable_overwrite import _confirmed_intent

        device, local, remote, row = _plain_pair("overwrite")
        old = _single_cable(local, make_interface(make_device("single-overwrite-other"), "Gi0/9"), tagged=True)
        client = self._client(device, row)

        record = self._record(client, device)
        local.refresh_from_db()
        data = {
            **_rendered_sync_data(client, device, "100"),
            "force": "on",
            "expected_cable_intent_100": _confirmed_intent(local, remote, old),
        }
        texts = _sync_plain(client, device, data)

        local.refresh_from_db()
        remote.refresh_from_db()
        assert (record["cable_status"], record["can_create_cable"]) == ("Cable Mismatch", True)
        assert "Overwrote existing cable for interfaces: eth0" in texts
        assert not Cable.objects.filter(pk=old.pk).exists()
        assert local.cable_id is not None and local.cable_id == remote.cable_id


def test_a_single_profile_serial_cable_reads_as_found():
    from netbox_librenms_plugin.tests.conftest import make_serial_device, make_serial_row
    from netbox_librenms_plugin.tests.test_serial_cables_view import _make_view

    acs, (csp,), _ = make_serial_device("single-serial", csp_names=["ttyS1"])
    _remote, _, (console,) = make_serial_device("single-serial-r", cp_names=["console"])
    cable = Cable(a_terminations=[csp], b_terminations=[console], profile=SINGLE)
    cable.save()
    cable.tags.add(librenms_cable_tag())

    link = _make_view().enrich_links_data([make_serial_row(csp, "single-serial-r", acs)], acs, server_key=SERVER_KEY)[0]

    assert (link["cable_status"], link["can_create_cable"]) == ("Cable Found", False)


@pytest.mark.usefixtures("breakout_server")
@pytest.mark.parametrize(
    ("hidden", "reason"),
    [
        ({"interface_view": {"channels__isnull": True}}, "the parent interface of this channel is not available"),
        ({"cable_view": {"label": "permitted"}}, "the cable of the parent interface of this channel is not available"),
    ],
    ids=["hidden-parent", "hidden-cable"],
)
def test_a_one_sided_channel_row_reveals_nothing_of_a_hidden_parent_or_cable(hidden, reason):
    setup = _Breakout(f"one-sided-{next(iter(hidden))}")
    setup.seed(setup.row(1))
    setup.sync(1)
    # Both channels now name a neighbour NetBox does not know: channel 1's connector is occupied, channel 2's is empty.
    unknown = "breakout-one-sided-unknown"
    setup.seed({**setup.row(1), "remote_device": unknown}, {**setup.row(2), "remote_device": unknown})
    setup.client.force_login(_constrained_user(f"breakout-one-sided-{next(iter(hidden))}", **hidden))

    records = setup.records()

    rendered = [
        (records[row_id]["cable_status"], records[row_id].get("cable_url"), records[row_id]["can_create_cable"])
        for row_id in ("101", "102")
    ]
    assert rendered == [(f"Breakout Not Synced: {reason}", None, False)] * 2


@pytest.mark.usefixtures("breakout_server")
def test_a_cable_with_an_unknown_profile_renders_and_is_not_point_to_point():
    from netbox_librenms_plugin.utils import cable_is_point_to_point

    device, local, remote, row = _plain_pair("unknown-profile")
    cable = Cable(a_terminations=[local], b_terminations=[remote])
    cable.save()
    # A stored value that is no known profile, as an older or newer NetBox could leave it.
    Cable.objects.filter(pk=cable.pk).update(profile="no-such-profile")
    channel_setup = _Breakout("unknown-profile-channel")
    channel_cable = Cable(a_terminations=[channel_setup.parent], b_terminations=[channel_setup.far_ends[0]])
    channel_cable.save()
    Cable.objects.filter(pk=channel_cable.pk).update(profile="no-such-profile")
    channel_setup.seed(channel_setup.row(2))
    client = TestASingleProfileCableIsPointToPoint()._client(device, row)

    record = TestASingleProfileCableIsPointToPoint()._record(client, device)
    channel_record = channel_setup.records()["102"]

    assert cable_is_point_to_point(Cable.objects.get(pk=cable.pk)) is False
    assert record["cable_status"] == "Multi-termination Cable Not Supported"
    assert channel_record["cable_status"] == (
        "Breakout Not Synced: the cable on et-0/0/1 uses profile no-such-profile, which this sync does not read"
    )
