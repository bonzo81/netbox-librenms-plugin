"""
IP mappings on the IP Addresses sync tab: the table previews the refusal, the writer enforces it.

The tests drive real requests against real NetBox objects and real IP mappings. The snapshot is
seeded in the cache the way a refresh leaves it; the server is a loopback LibreNMS.
"""

import json
import re
from html import unescape

import pytest
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse
from ipam.models import VRF, IPAddress

from netbox_librenms_plugin.models import IPAddressMapping
from netbox_librenms_plugin.sync_cache import TAB_SPECS, SyncTab, sync_snapshot_key
from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_superuser
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.test_interface_rule_writes import _port

pytestmark = pytest.mark.django_db

SERVER_KEY = "default"
PORT_ID = 7501
LINK_LOCAL = "fe80::1/64"
GLOBAL_V4 = "10.0.0.1/24"


def _row(ip_with_mask):
    address, _, length = ip_with_mask.partition("/")
    return {
        "ip_address": address,
        "prefix_length": int(length),
        "ip_with_mask": ip_with_mask,
        "port_id": PORT_ID,
        "interface_name": "eth0",
    }


def _seed(device, rows, mgmt_ip=""):
    cache.set(
        sync_snapshot_key(device, TAB_SPECS[SyncTab.IP_ADDRESSES].data_type, SERVER_KEY),
        {
            "ip_addresses": rows,
            "mgmt_ip": mgmt_ip,
            "ports_by_id": {PORT_ID: _port(PORT_ID, "eth0")},
            "bound_ports_by_id": {},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )


def _sync(client, device, vrfs):
    """Select every address in *vrfs* (address to posted VRF pk, or "" for Global) and sync it."""
    data = {"server_key": SERVER_KEY, "select": list(vrfs)}
    data.update({f"vrf_{address}": vrf for address, vrf in vrfs.items()})
    return client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses", kwargs={"object_type": "device", "pk": device.pk}
        ),
        data,
        HTTP_HX_REQUEST="true",
    )


def _messages(response):
    return " ".join(str(message) for message in get_messages(response.wsgi_request))


def _row_html(client, device, address):
    """Render the IP tab and return the row of *address*, as the browser gets it."""
    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"tab": "ipaddresses", "server_key": SERVER_KEY},
    )
    assert response.status_code == 200
    html = unescape(response.context["ip_sync"]["table"].as_html(response.wsgi_request))
    row = re.search(rf'<tr[^>]*data-name="{re.escape(address)}"[^>]*>.*?</tr>', html, flags=re.S)
    assert row is not None
    return row.group(0)


def _cell(row_html, column):
    return re.search(rf'<td[^>]*data-col="{column}"[^>]*>(.*?)</td>', row_html, flags=re.S).group(1)


def _checkbox(row_html):
    return re.search(r'<input[^>]*name="select"[^>]*>', row_html).group(0)


def _verify(client, device, address, vrf):
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_ipaddress"),
        json.dumps(
            {
                "device_id": device.pk,
                "object_type": "device",
                "ip_address": address,
                "row_id": address,
                "vrf_id": vrf.pk if vrf is not None else "",
                "server_key": SERVER_KEY,
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()["formatted_row"]


@pytest.fixture
def device(client, live_librenms):
    """A device whose eth0 is bound to the LibreNMS port that carries both addresses."""
    device = make_device("ip-rule-sync", librenms_cf={SERVER_KEY: {"id": 4510}})
    eth0 = make_interface(device, "eth0", iface_type="virtual")
    seed_mapping(eth0, SERVER_KEY, own=PORT_ID)
    client.force_login(make_superuser("ip-rule-sync-user"))
    return device


@pytest.fixture
def blue():
    return VRF.objects.create(name="ip-rule-blue")


@pytest.fixture
def red():
    return VRF.objects.create(name="ip-rule-red")


class TestTheTableShowsAnIgnoredAddress:
    def test_a_link_local_row_is_ignored_and_an_ipv4_row_still_offers_create(self, client, device):
        IPAddressMapping.objects.create(prefix="fe80::/10")
        _seed(device, [_row(LINK_LOCAL), _row(GLOBAL_V4)])

        ignored = _row_html(client, device, LINK_LOCAL)
        allowed = _row_html(client, device, GLOBAL_V4)

        assert "Ignored" in _cell(ignored, "status")
        assert "Not synced: IP mapping fe80::/10 ignores this address" in _cell(ignored, "status")
        assert "Create</button>" not in _cell(ignored, "status")
        assert "disabled" in _checkbox(ignored)
        assert "Create</button>" in _cell(allowed, "status")
        assert "disabled" not in _checkbox(allowed)

    def test_an_address_netbox_holds_as_synced_stays_synced(self, client, device):
        IPAddressMapping.objects.create(prefix="fe80::/10")
        IPAddress.objects.create(address=LINK_LOCAL, assigned_object=device.interfaces.get(name="eth0"))
        _seed(device, [_row(LINK_LOCAL)])

        status = _cell(_row_html(client, device, LINK_LOCAL), "status")

        assert "Synced" in status
        assert "Ignored" not in status

    def test_the_row_is_decided_in_the_vrf_it_shows(self, client, device, blue, red):
        IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue)
        IPAddress.objects.create(address=LINK_LOCAL, vrf=blue)
        IPAddress.objects.create(address="fe80::2/64", vrf=red)
        _seed(device, [_row(LINK_LOCAL), _row("fe80::2/64")])

        assert "Ignored" in _cell(_row_html(client, device, LINK_LOCAL), "status")
        assert "Update</button>" in _cell(_row_html(client, device, "fe80::2/64"), "status")

    def test_an_ignored_management_row_is_no_set_primary_ip_candidate(self, client, device):
        IPAddressMapping.objects.create(prefix="fe80::/10")
        _seed(device, [_row(LINK_LOCAL), _row(GLOBAL_V4)], mgmt_ip="fe80::1")

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
            {"tab": "ipaddresses", "server_key": SERVER_KEY},
        )

        table = response.context["ip_sync"]["table"]
        assert [row.get("is_mgmt_ip") for row in table.data if row["ip_with_mask"] == LINK_LOCAL] == [True]
        assert json.loads(table.attrs["data-mgmt-rows"]) == []


class TestTheWriterRefusesAnIgnoredAddress:
    def test_only_the_ipv4_address_is_created(self, client, device):
        IPAddressMapping.objects.create(prefix="fe80::/10")
        _seed(device, [_row(LINK_LOCAL), _row(GLOBAL_V4)])

        response = _sync(client, device, {LINK_LOCAL: "", GLOBAL_V4: ""})

        assert IPAddress.objects.filter(address=GLOBAL_V4).exists()
        assert not IPAddress.objects.filter(address__net_host_contained="fe80::/10").exists()
        assert f"{LINK_LOCAL} (IP mapping fe80::/10 ignores this address)" in _messages(response)

    def test_an_address_netbox_holds_is_neither_changed_nor_deleted(self, client, device):
        IPAddressMapping.objects.create(prefix="fe80::/10")
        eth0 = device.interfaces.get(name="eth0")
        existing = IPAddress.objects.create(address=LINK_LOCAL, assigned_object=eth0, description="kept")
        _seed(device, [_row(LINK_LOCAL)])

        response = _sync(client, device, {LINK_LOCAL: ""})

        existing.refresh_from_db()
        assert existing.assigned_object == eth0
        assert existing.description == "kept"
        assert f"{LINK_LOCAL} (IP mapping fe80::/10 ignores this address)" in _messages(response)

    @pytest.mark.parametrize("ignored", [True, False], ids=["ignored", "control"])
    def test_a_confirmed_reassignment_is_refused(self, client, device, ignored):
        existing = IPAddress.objects.create(address=LINK_LOCAL)
        _seed(device, [_row(LINK_LOCAL)])
        # The confirmation is signed while no mapping applies; the mapping then exists when it is used.
        conflict = _sync(client, device, {LINK_LOCAL: ""}).context["conflicts"][0]
        if ignored:
            IPAddressMapping.objects.create(prefix="fe80::/10")

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
                kwargs={"object_type": "device", "pk": device.pk},
            ),
            {"server_key": SERVER_KEY, "force_all": "1", "conflict_intent": conflict["intent"]},
            HTTP_HX_REQUEST="true",
        )

        existing.refresh_from_db()
        if ignored:
            assert existing.assigned_object is None
            assert f"{LINK_LOCAL} (IP mapping fe80::/10 ignores this address)" in _messages(response)
        else:
            # The control proves the confirmation reassigns the address when no mapping ignores it.
            assert existing.assigned_object == device.interfaces.get(name="eth0")

    @pytest.mark.parametrize("posted", ["blue", "red", "global"])
    def test_a_vrf_mapping_refuses_only_an_address_posted_into_its_vrf(self, client, device, blue, red, posted):
        IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue)
        _seed(device, [_row(LINK_LOCAL)])
        vrf = {"blue": blue, "red": red, "global": None}[posted]

        response = _sync(client, device, {LINK_LOCAL: vrf.pk if vrf is not None else ""})

        created = IPAddress.objects.filter(address=LINK_LOCAL)
        if posted == "blue":
            assert not created.exists()
            assert f"{LINK_LOCAL} (IP mapping fe80::/10 ignores this address)" in _messages(response)
        else:
            assert list(created.values_list("vrf", flat=True)) == [vrf.pk if vrf is not None else None]
            assert "Skipped" not in _messages(response)

    @pytest.mark.parametrize("ignored", [True, False], ids=["ignored", "control"])
    def test_an_ignored_address_creates_no_missing_interface(self, client, settings, live_librenms, ignored):
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.test_ip_address_sync_safety import (
            _configure_test_server,
            _serve_librenms_ip_rows,
        )

        _configure_test_server(settings)
        device = make_device("ip-rule-create-missing", librenms_cf={SERVER_KEY: {"id": 42}})
        client.force_login(make_superuser("ip-rule-create-missing-user"))
        rows = [{"address": "fe80::1", "prefix_length": 64, "port_id": 7502, "interface": "Ethernet7"}]
        _serve_librenms_ip_rows(live_librenms.server, rows, device_name=device.name)
        refreshed = client.post(
            reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk]),
            {"server_key": SERVER_KEY, "interface_name_field": "ifName"},
            HTTP_HX_REQUEST="true",
        )
        assert refreshed.status_code == 200
        if ignored:
            IPAddressMapping.objects.create(prefix="fe80::/10")

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
                kwargs={"object_type": "device", "pk": device.pk},
            ),
            {
                "server_key": SERVER_KEY,
                "create-missing-interfaces-toggle": "on",
                "select": [LINK_LOCAL],
                f"vrf_{LINK_LOCAL}": "",
            },
        )

        assert Interface.objects.filter(device=device, name="Ethernet7").exists() is (not ignored), _messages(response)
        assert IPAddress.objects.filter(address=LINK_LOCAL).exists() is (not ignored)


class TestAVrfChangeRedecidesTheRow:
    def test_the_ignored_state_follows_the_chosen_vrf(self, client, device, blue, red):
        IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue)
        # A refresh caches the enriched row, so the snapshot carries the pill of the VRF shown first.
        row = _row(LINK_LOCAL) | {
            "interface_url": device.interfaces.get(name="eth0").get_absolute_url(),
            "ip_rule_block": {"kind": "ignore", "reason": "stale"},
        }
        _seed(device, [row])

        into_blue = _verify(client, device, LINK_LOCAL, blue)
        into_red = _verify(client, device, LINK_LOCAL, red)
        into_global = _verify(client, device, LINK_LOCAL, None)

        assert "IP mapping fe80::/10 ignores this address" in into_blue["status"]
        assert into_blue["selectable"] is False
        for verdict in (into_red, into_global):
            assert "Ignored" not in verdict["status"]
            assert "Create</button>" in verdict["status"]
            assert verdict["selectable"] is True

    def test_an_ignored_address_netbox_holds_unassigned_in_the_chosen_vrf_shows_the_mapping(self, client, device, blue):
        IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue)
        IPAddress.objects.create(address=LINK_LOCAL, vrf=blue)
        _seed(device, [_row(LINK_LOCAL)])

        verdict = _verify(client, device, LINK_LOCAL, blue)

        assert "IP mapping fe80::/10 ignores this address" in verdict["status"]
        assert "Synced" not in verdict["status"]
        assert verdict["selectable"] is False

    def test_an_ignored_address_netbox_holds_on_the_row_interface_stays_synced(self, client, device, blue):
        IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue)
        IPAddress.objects.create(address=LINK_LOCAL, vrf=blue, assigned_object=device.interfaces.get(name="eth0"))
        _seed(device, [_row(LINK_LOCAL)])

        verdict = _verify(client, device, LINK_LOCAL, blue)

        assert "Synced" in verdict["status"]
        assert "Ignored" not in verdict["status"]
        assert verdict["selectable"] is False

    def test_an_ignored_address_that_two_interfaces_hold_stays_synced_on_the_row_interface(self, client, device, blue):
        other = make_interface(device, "eth1", iface_type="virtual")
        IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue)
        IPAddress.objects.create(address=LINK_LOCAL, vrf=blue, assigned_object=device.interfaces.get(name="eth0"))
        IPAddress.objects.create(address=LINK_LOCAL, vrf=blue, assigned_object=other)
        _seed(device, [_row(LINK_LOCAL)])

        verdict = _verify(client, device, LINK_LOCAL, blue)

        assert "Synced" in verdict["status"]
        assert verdict["selectable"] is False

    @pytest.mark.parametrize("unavailable", ["deleted", "hidden"])
    def test_an_unavailable_vrf_is_refused_and_not_verified_as_global(self, client, device, blue, unavailable):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue)
        IPAddress.objects.create(address=GLOBAL_V4, assigned_object=device.interfaces.get(name="eth0"))
        _seed(device, [_row(GLOBAL_V4)])
        vrf_pk = blue.pk
        if unavailable == "deleted":
            blue.delete()
        else:
            client.force_login(make_user_with_perms("ip-rule-no-vrf-view", [("view", Device)]))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_ipaddress"),
            json.dumps(
                {
                    "device_id": device.pk,
                    "object_type": "device",
                    "ip_address": GLOBAL_V4,
                    "row_id": GLOBAL_V4,
                    "vrf_id": vrf_pk,
                    "server_key": SERVER_KEY,
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400, response.json()
        assert response.json()["status"] == "error"
        assert "formatted_row" not in response.json()

    @pytest.mark.parametrize("mapping", ["none", "all-vrf", "blue"])
    @pytest.mark.parametrize("held", [False, True], ids=["not-held", "held-on-eth0"])
    def test_a_vrf_the_user_cannot_view_decides_as_a_vrf_that_does_not_exist(
        self, client, device, blue, red, mapping, held
    ):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        if mapping != "none":
            IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue if mapping == "blue" else None)
        if held:
            IPAddress.objects.create(address=LINK_LOCAL, vrf=blue, assigned_object=device.interfaces.get(name="eth0"))
        gone = VRF.objects.create(name="ip-rule-gone")
        gone_pk = gone.pk
        gone.delete()
        _seed(device, [_row(LINK_LOCAL)])
        user = make_user_with_perms("ip-rule-red-only", [("view", Device), ("view", Interface), ("view", IPAddress)])
        client.force_login(grant(user, "view", VRF, constraints={"name": red.name}))

        def verify(vrf_pk):
            body = {
                "device_id": device.pk,
                "object_type": "device",
                "ip_address": LINK_LOCAL,
                "row_id": LINK_LOCAL,
                "vrf_id": vrf_pk,
                "server_key": SERVER_KEY,
            }
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:verify_ipaddress"),
                json.dumps(body),
                content_type="application/json",
            )
            return response.status_code, response.json()

        hidden = verify(blue.pk)
        assert hidden[0] == 400, hidden
        assert hidden == verify(gone_pk)


def _two_holders(device, mapping_vrf, holder_vrf):
    """Map fe80::/10 in *mapping_vrf* and let eth0 and eth1 each hold LINK_LOCAL in *holder_vrf*."""
    IPAddressMapping.objects.create(prefix="fe80::/10", vrf=mapping_vrf)
    IPAddress.objects.create(address=LINK_LOCAL, vrf=holder_vrf, assigned_object=device.interfaces.get(name="eth0"))
    IPAddress.objects.create(
        address=LINK_LOCAL, vrf=holder_vrf, assigned_object=make_interface(device, "eth1", iface_type="virtual")
    )


class TestTheTableAndTheVerifyDecideARowAlike:
    """Two NetBox addresses match one source row; the table and a verify of the VRF it shows give one verdict."""

    @pytest.mark.parametrize(
        ("mapping_in_blue", "holders_in_blue", "synced"),
        [(False, False, True), (True, True, False), (False, True, False)],
        ids=["all-vrf-mapping-global-holders", "blue-mapping-blue-holders", "all-vrf-mapping-blue-holders"],
    )
    def test_the_verdicts_agree(self, client, device, blue, mapping_in_blue, holders_in_blue, synced):
        _two_holders(device, blue if mapping_in_blue else None, blue if holders_in_blue else None)
        _seed(device, [_row(LINK_LOCAL)])

        row = _row_html(client, device, LINK_LOCAL)
        shown = re.search(r'<option value="(\d*)"\s*selected', row)
        verdict = _verify(client, device, LINK_LOCAL, VRF.objects.get(pk=shown.group(1)) if shown else None)

        def verdict_of(status, disabled):
            return {"synced": "Synced" in status, "ignored": "Ignored" in status, "disabled": disabled}

        table = verdict_of(_cell(row, "status"), "disabled" in _checkbox(row))
        assert table == verdict_of(verdict["status"], not verdict["selectable"])
        assert table["synced"] is synced

    def test_an_address_held_in_a_vrf_the_user_cannot_view_is_decided_in_global(self, client, device, blue):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        IPAddressMapping.objects.create(prefix="fe80::/10")
        IPAddress.objects.create(address=LINK_LOCAL, vrf=blue, assigned_object=device.interfaces.get(name="eth0"))
        _seed(device, [_row(LINK_LOCAL)])
        perms = [("view", Device), ("view", Interface), ("view", IPAddress)]
        client.force_login(make_user_with_perms("ip-rule-hidden-holder", perms))

        row = _row_html(client, device, LINK_LOCAL)
        verdict = _verify(client, device, LINK_LOCAL, None)

        assert "Ignored" in _cell(row, "status")
        assert "Synced" not in _cell(row, "status")
        assert "Ignored" in verdict["status"]


class TestTheReasonNamesNoVrf:
    """A user who cannot view the mapping's VRF must not learn its name, so the reason names the prefix only."""

    def test_the_pill_the_verify_and_the_writer_name_only_the_prefix(self, client, device, blue):
        IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue)
        # The NetBox address in blue makes the row show blue; the second row has none, so its verify is not Synced.
        IPAddress.objects.create(address=LINK_LOCAL, vrf=blue)
        _seed(device, [_row(LINK_LOCAL), _row("fe80::2/64")])

        pill = _cell(_row_html(client, device, LINK_LOCAL), "status")
        verdict = _verify(client, device, "fe80::2/64", blue)["status"]
        message = _messages(_sync(client, device, {LINK_LOCAL: blue.pk}))

        for text in (pill, verdict, message):
            assert "IP mapping fe80::/10 ignores this address" in text
            assert blue.name not in text


class TestTheImportOobAttachReadsTheMappings:
    """Add as OOB sets the device's OOB IP from LibreNMS; an Ignore mapping keeps that address out too."""

    OOB_DEVICE_ID = 4343
    OOB_IP = "192.0.2.77"

    def _add_as_oob(self, client, live_librenms, target):
        live_librenms.server.device_info_response(
            device_id=self.OOB_DEVICE_ID,
            hostname=f"{target.name}-idrac",
            hardware="Integrated Remote Access Controller",
            os="idrac",
            serial=target.serial,
            ip=self.OOB_IP,
        )
        live_librenms.server.vc_inventory_callable(self.OOB_DEVICE_ID, [], {})
        return client.post(
            reverse("plugins:netbox_librenms_plugin:device_add_as_oob", args=[self.OOB_DEVICE_ID]),
            {
                "existing_device_id": str(target.pk),
                "server_key": SERVER_KEY,
                "oob_interface_id": "__new__",
                "oob_new_interface_name": "idrac",
            },
            HTTP_HX_REQUEST="true",
        )

    @pytest.mark.parametrize(("prefix", "ignored"), [("192.0.2.0/24", True), ("198.51.100.0/24", False)])
    def test_an_ignored_oob_ip_is_not_set_and_the_link_still_commits(self, client, live_librenms, prefix, ignored):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.mapping_fixtures import stored_mapping_for_test

        IPAddressMapping.objects.create(prefix=prefix)
        target = make_device(f"ip-rule-oob-{ignored}", serial=f"IP-RULE-OOB-{ignored}")
        client.force_login(make_superuser("ip-rule-oob-user"))

        response = self._add_as_oob(client, live_librenms, target)

        device = Device.objects.get(pk=target.pk)
        assert stored_mapping_for_test(device)[SERVER_KEY]["oob"]["id"] == self.OOB_DEVICE_ID
        # The other prefix is the positive control: the same request sets the OOB IP.
        assert (device.oob_ip is None) is ignored
        assert IPAddress.objects.filter(address__net_host=self.OOB_IP).exists() is (not ignored)
        assert device.interfaces.filter(name="idrac").exists() is (not ignored)
        assert (f"IP mapping {prefix} ignores this address" in _messages(response)) is ignored
