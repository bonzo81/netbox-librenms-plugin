"""The IP tab's Create VRF action: one rule offers it, and the endpoint re-derives that rule."""

import re

import pytest
from django.contrib.auth import get_user
from django.contrib.messages import get_messages
from django.urls import reverse
from ipam.models import VRF, IPAddress
from tenancy.models import Tenant

from netbox_librenms_plugin.data_shapes.recordings_store import load_recording
from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_device, make_superuser, make_vm
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms, make_view

SERVER_KEY = "test"
RD_VRF = ("vrf-df874a", "203.0.113.82:61434")
NO_RD_VRF = "vrf-0ed884"
# The three iosxe-subinterfaces addresses on vrf-df874a, then the one on vrf-0ed884, then an untagged one.
RD_ROWS = ("192.0.2.27/29", "192.0.2.234/29", "203.0.113.46/30")
NO_RD_ROW = "192.0.2.159/24"
UNTAGGED_ROW = "192.0.2.5/32"

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded(recording_server, settings):
    """Cache a fresh iosxe-subinterfaces IP snapshot for a new Device or VM, the way a refresh does."""

    def _seed(name, *, object_type="device"):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView
        from netbox_librenms_plugin.views.object_sync.vms import VMIPAddressTableView

        recording = load_recording("iosxe-subinterfaces")
        server, api = recording_server(recording, server_key=SERVER_KEY)
        configure_librenms_servers(settings, {SERVER_KEY: {"librenms_url": server.url, "api_token": "test-token"}})
        librenms_cf = {SERVER_KEY: {"id": recording["device_id"]}}
        if object_type == "device":
            owner, view_class = make_device(name, librenms_cf=librenms_cf), DeviceIPAddressTableView
        else:
            owner, view_class = make_vm(name), VMIPAddressTableView
            owner.custom_field_data["librenms_id"] = librenms_cf
            owner.save()
        view = make_view(view_class, None, librenms_api=api)
        view.librenms_api.cache_timeout = 300
        assert view._prepare_context(make_request("get"), owner, "ifName", fetch_fresh=True, server_key=SERVER_KEY)
        return owner

    return _seed


def _rows(client, owner, object_type="device"):
    """Render the real IP tab from its cache and return each row's HTML by address."""
    response = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": object_type, "pk": owner.pk, "tab": "ipaddresses"},
        ),
        {"server_key": SERVER_KEY},
    )
    assert response.status_code == 200
    rows = re.findall(r"<tr[^>]*data-interface=\"([^\"]+)\"[^>]*>(.*?)</tr>", response.content.decode(), flags=re.S)
    return dict(rows)


def _offers_create(row_html):
    return 'name="create_vrf"' in row_html


def _selected_vrf(row_html, vrf):
    return re.search(rf'<option value="{vrf.pk}"\s+selected>', row_html) is not None


def _create(client, owner, row_id, object_type="device"):
    return client.post(
        reverse(
            "plugins:netbox_librenms_plugin:create_ip_row_vrf",
            kwargs={"object_type": object_type, "pk": owner.pk},
        ),
        {"server_key": SERVER_KEY, "create_vrf": row_id},
    )


def _messages(response, level_tag):
    return [str(message) for message in get_messages(response.wsgi_request) if message.level_tag == level_tag]


def _superuser_client(client):
    client.force_login(make_superuser("ip-vrf-create-user"))
    return client


class TestTheCreateAffordance:
    """The row offers Create VRF only when no NetBox VRF has the LibreNMS RD or name."""

    def test_every_row_on_a_missing_vrf_offers_it_and_the_confirmation_names_it(self, client, seeded):
        owner = seeded("vrf-offer")

        rows = _rows(_superuser_client(client), owner)

        assert all(_offers_create(rows[row_id]) for row_id in (*RD_ROWS, NO_RD_ROW))
        assert not _offers_create(rows[UNTAGGED_ROW])
        assert f"route distinguisher {RD_VRF[1]}?" in rows[RD_ROWS[0]]
        assert "no route distinguisher?" in rows[NO_RD_ROW]
        create_url = reverse(
            "plugins:netbox_librenms_plugin:create_ip_row_vrf", kwargs={"object_type": "device", "pk": owner.pk}
        )
        assert f'formaction="{create_url}"' in rows[RD_ROWS[0]]

    @pytest.mark.parametrize(
        "existing",
        [
            pytest.param({"name": "NetBox RD holder", "rd": RD_VRF[1]}, id="rd-match"),
            pytest.param({"name": RD_VRF[0]}, id="name-match"),
            pytest.param({"name": RD_VRF[0], "rd": "65000:1"}, id="name-match-other-rd"),
        ],
    )
    def test_a_matching_vrf_is_suggested_instead(self, client, seeded, existing):
        vrf = VRF.objects.create(**existing)
        owner = seeded(f"vrf-match-{existing['name']}")

        rows = _rows(_superuser_client(client), owner)

        assert not any(_offers_create(rows[row_id]) for row_id in RD_ROWS)
        assert all(_selected_vrf(rows[row_id], vrf) for row_id in RD_ROWS)

    def test_a_name_two_vrfs_share_offers_nothing(self, client, seeded):
        for index in (1, 2):
            tenant = Tenant.objects.create(name=f"VRF create tenant {index}", slug=f"vrf-create-tenant-{index}")
            VRF.objects.create(name=NO_RD_VRF, tenant=tenant)
        owner = seeded("vrf-ambiguous-name")

        rows = _rows(_superuser_client(client), owner)

        assert not _offers_create(rows[NO_RD_ROW])
        assert "mdi-lightbulb-on-outline" not in rows[NO_RD_ROW]

    def test_an_address_netbox_holds_offers_nothing(self, client, seeded):
        IPAddress.objects.create(address=RD_ROWS[0], status="active")
        owner = seeded("vrf-held-address")

        rows = _rows(_superuser_client(client), owner)

        assert not _offers_create(rows[RD_ROWS[0]])
        assert _offers_create(rows[RD_ROWS[1]])


class TestVRFVisibility:
    """The dropdown and the suggestion show only viewable VRFs; every VRF still blocks a create."""

    def _constrained_client(self, client, name):
        from dcim.models import Device

        user = make_user_with_perms(f"{name}-user", [("view", Device)])
        client.force_login(grant(user, "view", VRF, constraints={"name": "Visible VRF"}))
        return client

    @pytest.mark.parametrize(
        "hidden", [pytest.param({"rd": RD_VRF[1]}, id="rd"), pytest.param({"rd": None}, id="name")]
    )
    def test_a_hidden_matching_vrf_is_not_listed_suggested_or_named_and_blocks_create(self, client, seeded, hidden):
        visible = VRF.objects.create(name="Visible VRF")
        secret = VRF.objects.create(name=RD_VRF[0] if hidden["rd"] is None else "Secret VRF", rd=hidden["rd"])
        owner = seeded(f"vrf-hidden-{hidden['rd']}")

        rows = _rows(self._constrained_client(client, f"vrf-hidden-{hidden['rd']}"), owner)

        row = rows[RD_ROWS[0]]
        assert f'<option value="{secret.pk}"' not in row
        assert secret.name not in row
        assert f'<option value="{visible.pk}"' in row
        assert "mdi-lightbulb-on-outline" not in row
        assert not _offers_create(row)

    def test_a_create_blocked_by_a_hidden_vrf_is_refused_without_naming_it(self, client, seeded):
        VRF.objects.create(name="Secret VRF", rd=RD_VRF[1])
        owner = seeded("vrf-hidden-post")
        client = self._constrained_client(client, "vrf-hidden-post")
        grant(get_user(client), "add", VRF)

        response = _create(client, owner, RD_ROWS[0])

        assert list(VRF.objects.values_list("name", flat=True)) == ["Secret VRF"]
        refusals = _messages(response, "danger")
        assert len(refusals) == 1 and "Secret VRF" not in refusals[0]

    def test_a_viewable_matching_vrf_is_still_suggested(self, client, seeded):
        visible = VRF.objects.create(name="Visible VRF", rd=RD_VRF[1])
        owner = seeded("vrf-visible-match")

        rows = _rows(self._constrained_client(client, "vrf-visible-match"), owner)

        assert _selected_vrf(rows[RD_ROWS[0]], visible)


class TestCreate:
    """The POST creates the VRF only; the row's normal suggestion then preselects it."""

    def test_the_vrf_is_created_with_its_rd_and_every_row_on_it_preselects_it(self, client, seeded):
        from core.models import ObjectChange

        owner = seeded("vrf-create-rd")
        client = _superuser_client(client)

        response = _create(client, owner, RD_ROWS[1])

        assert response.status_code == 302
        vrf = VRF.objects.get(name=RD_VRF[0])
        assert vrf.rd == RD_VRF[1]
        successes = _messages(response, "success")
        assert len(successes) == 1 and RD_VRF[0] in successes[0] and RD_VRF[1] in successes[0]
        assert ObjectChange.objects.filter(changed_object_id=vrf.pk, action="create").exists()
        assert not IPAddress.objects.filter(address__in=RD_ROWS).exists()
        rows = _rows(client, owner)
        assert all(_selected_vrf(rows[row_id], vrf) and not _offers_create(rows[row_id]) for row_id in RD_ROWS)
        assert _offers_create(rows[NO_RD_ROW])

    def test_the_create_holds_the_vrf_name_lock_until_the_transaction_ends(self, client, seeded):
        """Read from pg_locks: the test transaction stays open, so the create's advisory lock is still held."""
        from django.db import connection

        from netbox_librenms_plugin.utils import advisory_lock_key
        from netbox_librenms_plugin.views.sync.ip_addresses import vrf_create_lock_identity

        owner = seeded("vrf-create-lock")

        _create(_superuser_client(client), owner, RD_ROWS[0])

        assert VRF.objects.filter(name=RD_VRF[0]).exists()
        key = advisory_lock_key(vrf_create_lock_identity(RD_VRF[0]))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted"
                " AND classid = %s AND objid = %s AND objsubid = 1",
                [(key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF],
            )
            assert cursor.fetchone()[0] == 1

    def test_a_vrf_without_an_rd_is_created_with_none_and_matches_by_name(self, client, seeded):
        owner = seeded("vrf-create-no-rd")
        client = _superuser_client(client)

        _create(client, owner, NO_RD_ROW)

        vrf = VRF.objects.get(name=NO_RD_VRF)
        assert vrf.rd is None
        assert _selected_vrf(_rows(client, owner)[NO_RD_ROW], vrf)

    def test_a_virtual_machine_page_creates_the_vrf(self, client, seeded):
        owner = seeded("vrf-create-vm", object_type="virtualmachine")
        client = _superuser_client(client)

        response = _create(client, owner, RD_ROWS[0], object_type="virtualmachine")

        assert response.status_code == 302
        assert VRF.objects.filter(name=RD_VRF[0], rd=RD_VRF[1]).exists()


class TestRefusals:
    """Every refusal leaves NetBox without the new VRF and says why."""

    @pytest.mark.parametrize(
        "rival",
        [pytest.param({"name": RD_VRF[0]}, id="name"), pytest.param({"name": "Rival", "rd": RD_VRF[1]}, id="rd")],
    )
    def test_a_vrf_created_after_the_row_was_derived_is_refused_not_adopted(self, client, seeded, monkeypatch, rival):
        """The existence check inside the transaction; the lock that serializes it is pinned separately."""
        from netbox_librenms_plugin.views.sync.ip_addresses import CreateVRFFromIPRowView

        owner = seeded(f"vrf-late-{rival['name']}")
        original = CreateVRFFromIPRowView._creatable_vrf_identity

        def rival_created_after_derivation(view, *args, **kwargs):
            identity = original(view, *args, **kwargs)
            VRF.objects.create(**rival)
            return identity

        monkeypatch.setattr(CreateVRFFromIPRowView, "_creatable_vrf_identity", rival_created_after_derivation)

        response = _create(_superuser_client(client), owner, RD_ROWS[0])

        assert list(VRF.objects.values_list("name", "rd")) == [(rival["name"], rival.get("rd"))]
        assert len(_messages(response, "danger")) == 1
        assert _messages(response, "success") == []

    @pytest.mark.parametrize(
        "row_id",
        [UNTAGGED_ROW, "198.51.100.1/24", "not-an-address"],
        ids=["untagged-row", "row-not-in-snapshot", "invalid"],
    )
    def test_a_row_without_the_action_is_refused(self, client, seeded, row_id):
        owner = seeded(f"vrf-no-action-{row_id}")

        response = _create(_superuser_client(client), owner, row_id)

        assert not VRF.objects.exists()
        assert len(_messages(response, "danger")) == 1

    def test_a_row_whose_vrf_now_exists_is_refused_by_the_rederived_rule(self, client, seeded):
        owner = seeded("vrf-exists-now")
        VRF.objects.create(name=RD_VRF[0])

        response = _create(_superuser_client(client), owner, RD_ROWS[0])

        assert VRF.objects.count() == 1
        assert len(_messages(response, "danger")) == 1

    def test_missing_add_vrf_permission_is_refused(self, client, seeded):
        from dcim.models import Device

        owner = seeded("vrf-no-add")
        client.force_login(make_user_with_perms("vrf-no-add-user", [("view", Device)]))

        response = _create(client, owner, RD_ROWS[0])

        assert not VRF.objects.exists()
        assert _messages(response, "danger") == ["Missing permissions: ipam.add_vrf"]

    def test_a_constrained_add_grant_rolls_the_create_back(self, client, seeded):
        from dcim.models import Device

        owner = seeded("vrf-constrained")
        user = make_user_with_perms("vrf-constrained-user", [("view", Device)])
        user = grant(user, "add", VRF, constraints={"name": "some-other-vrf"})
        client.force_login(user)

        response = _create(client, owner, RD_ROWS[0])

        assert not VRF.objects.exists()
        assert len(_messages(response, "danger")) == 1

    def test_a_migrated_donor_is_refused(self, client, seeded):
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        owner = seeded("vrf-migrated")
        mark_librenms_migrated(owner, make_device("vrf-migrated-winner").pk, SERVER_KEY)
        owner.save()

        response = _create(_superuser_client(client), owner, RD_ROWS[0])

        assert not VRF.objects.exists()
        assert any("migrated" in text for text in _messages(response, "danger"))
