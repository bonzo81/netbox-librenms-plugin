"""
Characterization of ``BaseCableTableView``'s row-resolution internals.

Home for the cases that pin cables_view.py behaviour before each complexity refactor.
Branches above #104 do not touch this file, so cases can be appended at its tail
without fighting a restack.

Every object here is a real NetBox record and every permission is a real
ObjectPermission, so a resolution that only works against a mock cannot pass.

Covered so far:
- ``_build_normal_link_context``: which link resolves to which device, owner and
  interface, what each lookup index holds, which pks land in the visibility sets,
  and when a trace is loaded.
- ``enrich_serial_remote``: the guards, the manual pick, and the sibling-row claims.
- ``_prepare_context``: the unresolved-identity, serial carry-forward and migrated
  donor branches.
- ``SyncCablesView.display_sync_results``: every result category's text and order.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis,
)
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_view

SERVER_KEY = "default"


def _view(user=None):
    """The real cable view bound to a real request, so ``_viewable_queryset`` restricts for real."""
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    view = make_view(BaseCableTableView, make_request("get", user=user or make_superuser("cable-ctx-su")))
    view._active_server_key = SERVER_KEY
    return view


def _set_librenms_id(obj, librenms_id):
    """Seed the multi-server librenms_id custom field the resolution indexes read."""
    obj.custom_field_data["librenms_id"] = {SERVER_KEY: librenms_id}
    obj.save()
    return obj


def _link(**overrides):
    """A normal (non-serial) cable row as the table builds it."""
    row = {"_source": "lldp"}
    row.update(overrides)
    return row


class _Scenario:
    """One realistic page of normal rows, covering each resolution path the method owns."""

    def __init__(self, tag):
        self.local_a = make_device(f"{tag}-local-m1")
        self.local_b = make_device(f"{tag}-local-m2")
        make_virtual_chassis(f"vc-{tag}", self.local_a, self.local_b)

        # Remote resolved by librenms_id, remote resolved by a hostname needing domain stripping.
        self.remote_by_id = _set_librenms_id(make_device(f"{tag}-remote-by-id"), 9001)
        self.remote_by_name = make_device(f"{tag}-remote-by-name")
        self.spare = make_device(f"{tag}-spare")

        self.local_iface_a = _set_librenms_id(make_interface(self.local_a, "Gi0/1"), 7001)
        self.local_iface_b = make_interface(self.local_b, "Gi0/2")
        self.remote_iface_id = make_interface(self.remote_by_id, "Gi1/1")
        self.remote_iface_name = make_interface(self.remote_by_name, "Gi2/1")
        self.manual_iface = make_interface(self.spare, "Gi9/9")

        # Each end is cabled to a DIFFERENT third party, so the row is a real mismatch and the
        # trace branch (two visible, differing cable ids) is the one that runs.
        self.local_cable = cable_together(self.local_iface_a, make_interface(self.spare, "Gi0/1"))
        self.remote_cable = cable_together(self.remote_iface_id, make_interface(self.spare, "Gi0/2"))

        self.link_by_id = _link(
            local_port="Gi0/1",
            local_port_id=7001,
            remote_device_id=9001,
            remote_port="Gi1/1",
        )
        self.link_by_name = _link(
            local_port="Gi0/2",
            remote_device=f"{tag}-remote-by-name.example.net",
            remote_port="Gi2/1",
        )
        self.link_manual = _link(
            local_port="Gi0/1",
            manual_remote_id=self.manual_iface.pk,
        )
        self.link_unknown = _link(local_port="Gi0/1", remote_device=f"{tag}-not-in-netbox")
        self.serial_row = {"_source": "serial", "local_port": "Console 1"}

    @property
    def links(self):
        return [self.link_by_id, self.link_by_name, self.link_manual, self.link_unknown, self.serial_row]


@pytest.mark.django_db
class TestNormalLinkContextResolution:
    """What each link resolves to, and what the lookup indexes hold."""

    def test_a_page_with_only_serial_rows_builds_no_context(self):
        """The method owns normal rows only, so a serial-only page short-circuits."""
        obj = make_device("ctx-serial-only")

        assert _view()._build_normal_link_context([{"_source": "serial"}], obj, SERVER_KEY) is None

    def test_an_empty_page_builds_no_context(self):
        obj = make_device("ctx-empty")

        assert _view()._build_normal_link_context([], obj, SERVER_KEY) is None

    def test_the_context_exposes_exactly_the_documented_keys(self):
        scenario = _Scenario("ctx-keys")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert set(context) == {
            "local_owner_by_link",
            "remote_device_by_link",
            "remote_owner_by_link",
            "interfaces_by_pk",
            "interfaces_by_name",
            "interface_ids_by_device",
            "visible_interface_ids",
            "visible_owner_ids",
            "visible_cable_ids",
            "trace_paths",
            "trace_visibility",
        }

    def test_a_remote_resolves_by_librenms_id(self):
        scenario = _Scenario("ctx-by-id")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert context["remote_device_by_link"][id(scenario.link_by_id)] == scenario.remote_by_id

    def test_a_remote_resolves_by_hostname_after_the_domain_is_dropped(self):
        scenario = _Scenario("ctx-by-name")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert context["remote_device_by_link"][id(scenario.link_by_name)] == scenario.remote_by_name

    def test_an_unknown_hostname_resolves_to_no_remote(self):
        scenario = _Scenario("ctx-unknown")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert context["remote_device_by_link"][id(scenario.link_unknown)] is None
        assert context["remote_owner_by_link"][id(scenario.link_unknown)] is None

    def test_an_exact_hostname_match_wins_over_the_stripped_name(self):
        """Exact evidence blocks the domain-stripped fallback, which would match a different device."""
        stripped_twin = make_device("ctx-dup-remote")
        obj = make_device("ctx-dup-local")
        exact = make_device("ctx-dup-remote.example.net")
        link = _link(local_port="Gi0/1", remote_device=exact.name)

        context = _view()._build_normal_link_context([link], obj, SERVER_KEY)

        assert context["remote_device_by_link"][id(link)] == exact
        assert context["remote_device_by_link"][id(link)] != stripped_twin

    def test_the_serial_row_is_excluded_from_every_index(self):
        scenario = _Scenario("ctx-skip-serial")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert id(scenario.serial_row) not in context["local_owner_by_link"]
        assert id(scenario.serial_row) not in context["remote_device_by_link"]


@pytest.mark.django_db
class TestNormalLinkContextOwners:
    """Local and remote owners resolve through the virtual chassis, not the page object."""

    def test_the_local_owner_is_the_chassis_member_owning_the_port(self):
        scenario = _Scenario("ctx-owner-local")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert context["local_owner_by_link"][id(scenario.link_by_id)] == scenario.local_a

    def test_a_standalone_remote_owns_its_own_port(self):
        scenario = _Scenario("ctx-owner-remote")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert context["remote_owner_by_link"][id(scenario.link_by_id)] == scenario.remote_by_id

    def test_a_page_object_outside_a_chassis_owns_every_local_port(self):
        obj = make_device("ctx-standalone-local")
        make_interface(obj, "Gi0/1")
        link = _link(local_port="Gi0/1")

        context = _view()._build_normal_link_context([link], obj, SERVER_KEY)

        assert context["local_owner_by_link"][id(link)] == obj


@pytest.mark.django_db
class TestNormalLinkContextIndexes:
    """The prefetched interface catalog and its three lookup indexes."""

    def test_named_and_id_evidence_both_load_their_interface(self):
        scenario = _Scenario("ctx-index")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        loaded = set(context["interfaces_by_pk"])
        assert scenario.local_iface_a.pk in loaded, "the local port named by the row must load"
        assert scenario.remote_iface_id.pk in loaded, "the remote port named by the row must load"
        assert scenario.manual_iface.pk in loaded, "a manual pick must load by pk"

    def test_the_name_index_is_keyed_by_device_and_name(self):
        scenario = _Scenario("ctx-name-index")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert context["interfaces_by_name"][(scenario.local_a.pk, "Gi0/1")] == [scenario.local_iface_a]

    def test_the_id_index_is_keyed_by_device_and_librenms_id(self):
        scenario = _Scenario("ctx-id-index")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert context["interface_ids_by_device"][(scenario.local_a.pk, 7001)] == [scenario.local_iface_a]

    def test_an_interface_without_a_librenms_id_is_absent_from_the_id_index(self):
        scenario = _Scenario("ctx-id-absent")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert not [key for key in context["interface_ids_by_device"] if key[0] == scenario.local_b.pk]


@pytest.mark.django_db
class TestNormalLinkContextVisibility:
    """The visibility sets come from real restricted querysets, not from the catalog."""

    def test_a_superuser_sees_every_loaded_object(self):
        scenario = _Scenario("ctx-vis-all")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert scenario.local_iface_a.pk in context["visible_interface_ids"]
        assert scenario.local_a.pk in context["visible_owner_ids"]
        assert scenario.local_cable.pk in context["visible_cable_ids"]

    def test_a_remote_outside_the_view_scope_resolves_to_nothing(self):
        """A hidden device is real evidence, so the row must resolve to no remote at all."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        scenario = _Scenario("ctx-vis-hidden")
        user = make_user_with_perms("ctx-hidden-viewer", [])
        # Everything EXCEPT the remote resolved by librenms_id.
        user = grant(user, "view", Device, constraints={"pk__in": [scenario.local_a.pk, scenario.local_b.pk]})

        context = _view(user)._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert context["remote_device_by_link"][id(scenario.link_by_id)] is None
        assert scenario.remote_by_id.pk not in context["visible_owner_ids"]

    def test_an_invisible_cable_is_absent_from_the_visible_cable_set(self):
        from dcim.models import Cable, Device, Interface

        scenario = _Scenario("ctx-vis-cable")
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        user = make_user_with_perms("ctx-cable-viewer", [])
        user = grant(user, "view", Device)
        user = grant(user, "view", Interface)
        user = grant(user, "view", Cable, constraints={"pk__in": [scenario.remote_cable.pk]})

        context = _view(user)._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert scenario.local_cable.pk not in context["visible_cable_ids"]
        assert scenario.remote_cable.pk in context["visible_cable_ids"]


@pytest.mark.django_db
class TestNormalLinkContextTraces:
    """A trace is loaded only for a row whose two ends sit on different visible cables."""

    def test_a_mismatched_row_loads_the_local_trace(self):
        scenario = _Scenario("ctx-trace")

        context = _view()._build_normal_link_context(scenario.links, scenario.local_a, SERVER_KEY)

        assert scenario.local_iface_a.pk in context["trace_paths"]
        assert context["trace_visibility"], "a loaded trace must carry its permission map"

    def test_an_uncabled_row_loads_no_trace(self):
        obj = make_device("ctx-trace-none")
        remote = make_device("ctx-trace-none-remote")
        make_interface(obj, "Gi0/1")
        make_interface(remote, "Gi1/1")
        link = _link(local_port="Gi0/1", remote_device=remote.name, remote_port="Gi1/1")

        context = _view()._build_normal_link_context([link], obj, SERVER_KEY)

        assert context["trace_paths"] == {}


# ---------------------------------------------------------------------------
# enrich_serial_remote
#
# test_serial_cables_view.py already pins the plain label path (uncabled pick,
# deterministic ordering, dead label, every port cabled). These cases pin the
# branches it does NOT reach, which are the ones a split of this method could
# move: the manual pick, the unconfigured sensor, the two early guards, the
# sibling-row claim set, and the preloaded remote_context variants.
# ---------------------------------------------------------------------------


def _serial_view():
    """The cable view bound to a real request, matching the serial suite's own driver."""
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    view = make_view(BaseCableTableView, make_request("get", user=make_superuser("serial-ctx-su")))
    view._active_server_key = SERVER_KEY
    view.librenms_id = 12
    return view


def _serial_link(csp, **overrides):
    """A serial row whose local end already resolved to *csp*."""
    row = {
        "_source": "serial",
        "local_port": csp.name,
        "netbox_local_interface_id": csp.pk,
        "is_configured": True,
        "cable_status": "No Cable",
    }
    row.update(overrides)
    return row


@pytest.mark.django_db
class TestEnrichSerialRemoteGuards:
    """The early exits that must leave the row untouched."""

    def test_a_row_without_a_resolved_local_port_is_left_alone(self):
        from netbox_librenms_plugin.tests.conftest import make_serial_device

        device, _, _ = make_serial_device("serial-guard-remote", cp_names=["con0"])
        link = {"_source": "serial", "remote_device": device.name}

        _serial_view().enrich_serial_remote(link)

        assert link == {"_source": "serial", "remote_device": device.name}

    def test_a_hidden_serial_cable_stops_resolution(self):
        from netbox_librenms_plugin.tests.conftest import make_serial_device

        device, _, _ = make_serial_device("serial-hidden-remote", cp_names=["con0"])
        _local, (csp,), _ = make_serial_device("serial-hidden-local", csp_names=["ttyS0"])
        link = _serial_link(csp, remote_device=device.name, _serial_cable_hidden=True)

        _serial_view().enrich_serial_remote(link)

        assert "netbox_remote_interface_id" not in link
        assert "can_create_cable" not in link

    def test_an_unsupported_multi_termination_stops_resolution(self):
        from netbox_librenms_plugin.tests.conftest import make_serial_device

        device, _, _ = make_serial_device("serial-multi-remote", cp_names=["con0"])
        _local, (csp,), _ = make_serial_device("serial-multi-local", csp_names=["ttyS0"])
        link = _serial_link(csp, remote_device=device.name, _multi_termination_unsupported=True)

        _serial_view().enrich_serial_remote(link)

        assert "netbox_remote_interface_id" not in link

    def test_a_deleted_local_port_stops_resolution(self):
        """The row carries a stale CSP pk, so the re-fetch must fail closed."""
        from netbox_librenms_plugin.tests.conftest import make_serial_device

        device, _, _ = make_serial_device("serial-stale-remote", cp_names=["con0"])
        _local, (csp,), _ = make_serial_device("serial-stale-local", csp_names=["ttyS0"])
        stale_pk = csp.pk
        csp.delete()
        link = {"_source": "serial", "netbox_local_interface_id": stale_pk, "remote_device": device.name}

        _serial_view().enrich_serial_remote(link)

        assert "netbox_remote_interface_id" not in link

    def test_an_unconfigured_sensor_never_auto_selects_a_remote(self):
        """The default label names the local appliance port, so it is not evidence for a device."""
        from netbox_librenms_plugin.tests.conftest import make_serial_device

        device, _, _ = make_serial_device("serial-unconfigured-remote", cp_names=["con0"])
        _local, (csp,), _ = make_serial_device("serial-unconfigured-local", csp_names=["ttyS0"])
        link = _serial_link(csp, remote_device=device.name, is_configured=False)

        _serial_view().enrich_serial_remote(link)

        assert "netbox_remote_interface_id" not in link
        assert "netbox_remote_device_id" not in link


@pytest.mark.django_db
class TestEnrichSerialRemoteManualPick:
    """A hand-picked remote wins over the label and resolves at port level."""

    def test_a_manual_pick_resolves_to_the_picked_port(self):
        from netbox_librenms_plugin.tests.conftest import make_serial_device

        picked_device, _, (picked,) = make_serial_device("serial-manual-picked", cp_names=["con9"])
        # A different device the label names, to prove the pick wins over the label.
        label_device, _, _ = make_serial_device("serial-manual-label", cp_names=["con0"])
        _local, (csp,), _ = make_serial_device("serial-manual-local", csp_names=["ttyS0"])
        link = _serial_link(csp, remote_device=label_device.name, manual_remote_id=picked.pk)

        _serial_view().enrich_serial_remote(link)

        assert link["netbox_remote_interface_id"] == picked.pk
        assert link["netbox_remote_device_id"] == picked_device.pk
        assert link["manual_remote"] is True

    def test_a_manual_pick_is_reserved_against_sibling_rows(self):
        from netbox_librenms_plugin.tests.conftest import make_serial_device

        _picked_device, _, (picked,) = make_serial_device("serial-claim-picked", cp_names=["con9"])
        _local, (csp,), _ = make_serial_device("serial-claim-local", csp_names=["ttyS0"])
        link = _serial_link(csp, manual_remote_id=picked.pk)
        claimed = set()

        _serial_view().enrich_serial_remote(link, claimed_cp_ids=claimed)

        assert picked.pk in claimed

    def test_a_vanished_manual_pick_fails_closed_instead_of_falling_back_to_the_label(self):
        """Falling back would silently sync a different endpoint than the user chose."""
        from netbox_librenms_plugin.tests.conftest import make_serial_device

        label_device, _, (label_port,) = make_serial_device("serial-vanished-label", cp_names=["con0"])
        _picked_device, _, (picked,) = make_serial_device("serial-vanished-picked", cp_names=["con9"])
        _local, (csp,), _ = make_serial_device("serial-vanished-local", csp_names=["ttyS0"])
        vanished_pk = picked.pk
        picked.delete()
        link = _serial_link(csp, remote_device=label_device.name, manual_remote_id=vanished_pk)

        _serial_view().enrich_serial_remote(link)

        assert link["cable_status"] == "Selected remote port is no longer available"
        assert link["can_create_cable"] is False
        assert link["manual_remote"] is True
        assert link.get("netbox_remote_interface_id") != label_port.pk


@pytest.mark.django_db
class TestEnrichSerialRemoteClaims:
    """Two rows resolving to one device must not target the same free port."""

    def test_a_claimed_port_is_skipped_for_the_next_row(self):
        from netbox_librenms_plugin.tests.conftest import make_serial_device

        device, _, (first, second) = make_serial_device("serial-dedup-remote", cp_names=["con-a", "con-b"])
        _local, (csp,), _ = make_serial_device("serial-dedup-local", csp_names=["ttyS0"])
        link = _serial_link(csp, remote_device=device.name)
        claimed = {first.pk}

        _serial_view().enrich_serial_remote(link, claimed_cp_ids=claimed)

        assert link["netbox_remote_interface_id"] == second.pk
        assert claimed == {first.pk, second.pk}

    def test_an_auto_pick_claims_its_port(self):
        from netbox_librenms_plugin.tests.conftest import make_serial_device

        device, _, (only,) = make_serial_device("serial-autoclaim-remote", cp_names=["con0"])
        _local, (csp,), _ = make_serial_device("serial-autoclaim-local", csp_names=["ttyS0"])
        link = _serial_link(csp, remote_device=device.name)
        claimed = set()

        _serial_view().enrich_serial_remote(link, claimed_cp_ids=claimed)

        assert claimed == {only.pk}


# ---------------------------------------------------------------------------
# _prepare_context
#
# The cached and plain-refresh paths are well covered in test_coverage_base_views.py.
# These pin three branches that carried NO test at all, which are exactly the ones a
# split of this method could drop silently: the unresolved-id short circuit, the
# carry-forward of serial rows when the serial source is skipped, and the read-only
# donor pass that strips every sync affordance.
# ---------------------------------------------------------------------------


def _api_view(user, server_key=SERVER_KEY):
    """The device Cables tab bound to a REAL LibreNMS client, so a refresh performs real HTTP."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

    view = make_view(
        DeviceCableTableView,
        make_request("get", user=user),
        librenms_api=LibreNMSAPI(server_key=server_key),
    )
    view._active_server_key = server_key
    return view


def _point_at(settings, url, server_key=SERVER_KEY):
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

    configure_librenms_servers(
        settings, {server_key: {"librenms_url": url, "api_token": "test-token", "verify_ssl": False}}
    )
    return server_key


@pytest.mark.django_db
class TestPrepareContextIncompleteRefresh:
    """A refresh that cannot establish the device's identity must not render a table."""

    def test_an_ambiguous_librenms_id_returns_the_refresh_incomplete_context(self, librenms_server, settings):
        """Two owners of one id is a conflict, so the tab reports it instead of rendering rows.

        The short circuit sits after the fetch-failure guard, so it is only reached when
        get_links_data still returns a list. A device whose console ports the viewer cannot
        read does exactly that, so the row carries both conditions.
        """
        from dcim.models import Device
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_serial_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        server_key = _point_at(settings, librenms_server.url)
        make_device("ctx-ambiguous-owner-a", librenms_cf={server_key: 8801})
        make_device("ctx-ambiguous-owner-b", librenms_cf={server_key: 8801})
        target, _csps, _ = make_serial_device("ctx-ambiguous-target", csp_names=["ttyS0"])
        target.custom_field_data["librenms_id"] = {server_key: None}
        target.save()
        librenms_server.register(
            f"/api/v0/devices/{target.name}",
            {"status": "ok", "devices": [{"device_id": 8801}]},
            method="GET",
        )
        user = make_user_with_perms("ctx-ambiguous-user", [])
        user = grant(user, "view", Device)
        view = _api_view(user, server_key)
        cache_key = view.get_cache_key(target, "links", server_key)
        cache.set(cache_key, {"links": [], "snapshot_token": "stale"}, timeout=300)

        context = view._prepare_context(view.request, target, fetch_fresh=True, server_key=server_key)

        # Precondition: the conflict really fired, rather than the fetch failing for another reason.
        assert view._librenms_id_unresolved is True
        assert context == {
            "table": None,
            "object": target,
            "cache_expiry": None,
            "server_key": server_key,
            "refresh_incomplete": True,
        }
        assert cache.get(cache_key) is None, "the stale snapshot must not survive an unresolved refresh"

    def test_a_skipped_serial_source_carries_the_previous_serial_rows_forward(self, librenms_server, settings):
        """The user cannot read the console ports, so the refresh must not silently drop those rows."""
        from dcim.models import Device
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_serial_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        server_key = _point_at(settings, librenms_server.url)
        device, (csp,), _ = make_serial_device("ctx-serialskip-device", csp_names=["ttyS0"])
        device.custom_field_data["librenms_id"] = {server_key: 8802}
        device.save()
        librenms_server.register("/api/v0/devices/8802/links", {"status": "ok", "links": []})
        # Device is viewable; its ConsoleServerPorts are NOT, which is what skips the serial source.
        user = make_user_with_perms("ctx-serialskip-user", [])
        user = grant(user, "view", Device)
        view = _api_view(user, server_key)
        cache_key = view.get_cache_key(device, "links", server_key)
        prior_serial_row = {
            "_source": "serial",
            "device_id": device.pk,
            "local_port": csp.name,
            "local_port_id": f"serial:{csp.pk}",
            "sensor_id": csp.pk,
            "sensor_index_int": 1,
            "is_configured": True,
            "remote_device": "some-router",
        }
        cache.set(cache_key, {"links": [prior_serial_row], "snapshot_token": "prior"}, timeout=300)

        context = view._prepare_context(view.request, device, fetch_fresh=True, server_key=server_key)

        # Precondition: the skip really fired, so the assertions below describe that state.
        assert view._serial_source_skipped is True
        assert "serial" in context["incomplete_sources"]
        cached_after = cache.get(cache_key)
        assert [row["_source"] for row in cached_after["links"]] == ["serial"], (
            "the previous serial row must survive a refresh that could not read the serial source"
        )


@pytest.mark.django_db
class TestPrepareContextReadOnlyDonor:
    """A device merged into another must offer no cable actions on its own tab."""

    def test_a_migrated_donor_strips_every_row_affordance(self, settings):
        from django.core.cache import cache

        server_key = _point_at(settings, "https://librenms.example.invalid")
        donor = make_device("ctx-donor-device")
        donor.custom_field_data["librenms_id"] = {
            server_key: {"_migrated_to": {"device_id": 4242, "server_key": server_key, "at": "2026-09-16T00:00:00Z"}}
        }
        donor.save()
        peer = make_device("ctx-donor-peer")
        make_interface(peer, "Gi1/1")
        make_interface(donor, "Gi0/1")
        view = _api_view(make_superuser("ctx-donor-user"), server_key)
        cache_key = view.get_cache_key(donor, "links", server_key)
        cache.set(
            cache_key,
            {
                "links": [
                    {
                        "_source": "lldp",
                        "device_id": donor.pk,
                        "local_port": "Gi0/1",
                        "remote_device": peer.name,
                        "remote_port": "Gi1/1",
                    }
                ],
                "snapshot_token": "donor",
            },
            timeout=300,
        )

        context = view._prepare_context(view.request, donor, fetch_fresh=False, server_key=server_key)

        rows = [row.record for row in context["table"].rows]
        # Precondition: the row really rendered, so "no affordance" is not an empty-table artefact.
        assert rows, "the cached snapshot must render a row"
        assert all(row.get("can_create_cable") is False for row in rows)
        assert all("picker_url" not in row for row in rows)


# ---------------------------------------------------------------------------
# SyncCablesView.display_sync_results
#
# Fifteen near-identical blocks, one per result category. The existing coverage
# patches the messages module and passes four keys, so it pins neither the text
# nor the order. This drives real message storage with every category populated.
# ---------------------------------------------------------------------------

# Every key display_sync_results reports, in the order the producer initialises them.
SYNC_RESULT_KEYS = (
    "valid",
    "invalid",
    "failed",
    "duplicate",
    "missing_remote",
    "rejected_selection",
    "skipped",
    "overwritten",
    "tagged",
    "conflict",
    "denied",
    "stale",
    "unverified",
    "unsupported",
    "patch_path",
)


@pytest.mark.django_db
class TestDisplaySyncResults:
    """Each populated category raises exactly one flash message, at its own level."""

    def _run_with_every_category(self):
        from netbox_librenms_plugin.tests.view_test_helpers import message_texts
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        request = make_request("post", user=make_superuser("cable-results-user"))
        view = object.__new__(SyncCablesView)
        # One distinct interface name per category, so a message cannot be attributed
        # to the wrong key and still pass.
        results = {key: [f"if-{key}"] for key in SYNC_RESULT_KEYS}

        view.display_sync_results(request, results)

        return {level: message_texts(request, level) for level in ("error", "warning", "info", "success")}

    def test_every_category_reports_at_its_documented_level(self):
        by_level = self._run_with_every_category()

        assert [text.split(": ")[-1] for text in by_level["error"]] == [
            "if-missing_remote",
            "if-invalid",
            "if-failed",
            "if-rejected_selection",
            "if-denied.",
            "if-stale",
            "if-unverified.",
            "if-unsupported.",
        ]
        assert [text.split(": ")[-1] for text in by_level["warning"]] == ["if-duplicate", "if-conflict"]
        assert [text.split(": ")[-1] for text in by_level["info"]] == ["if-skipped", "if-patch_path", "if-tagged"]
        assert [text.split(": ")[-1] for text in by_level["success"]] == ["if-overwritten", "if-valid"]

    def test_the_wording_of_each_category_is_unchanged(self):
        by_level = self._run_with_every_category()
        everything = [text for level in ("error", "warning", "info", "success") for text in by_level[level]]

        assert "Remote device or interface not found in NetBox for: if-missing_remote" in everything
        assert "No LibreNMS link data found for interfaces: if-invalid" in everything
        assert "Failed to sync cables for interfaces: if-failed" in everything
        assert "Selected device is not part of this cable-sync page for interfaces: if-rejected_selection" in everything
        assert "You do not have permission to change the selected cable connection(s) for: if-denied." in everything
        assert (
            "The cable state or target changed after confirmation. Refresh and review the current cable for: if-stale"
            in everything
        )
        assert (
            "These links come from a LibreNMS source the last refresh could not read, so they may be out of "
            "date. Refresh Cables before syncing: if-unverified." in everything
        )
        assert (
            "Multi-termination cables cannot be changed by cable sync. Update them in NetBox for: if-unsupported."
            in everything
        )
        assert "Cable already exists for interfaces: if-duplicate" in everything
        assert "Skipped OOB-controller links (context only, not syncable to the host): if-skipped" in everything
        assert "Skipped links already modeled through a patch path: if-patch_path" in everything
        assert "Tagged existing cable(s) as LibreNMS-managed for: if-tagged" in everything
        assert "Overwrite protection: confirm the current cable(s) in the dialog for: if-conflict" in everything
        assert "Overwrote existing cable for interfaces: if-overwritten" in everything
        assert "Successfully created cable for interfaces: if-valid" in everything

    def test_an_empty_result_set_says_nothing(self):
        from netbox_librenms_plugin.tests.view_test_helpers import message_texts
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        request = make_request("post", user=make_superuser("cable-results-empty-user"))
        view = object.__new__(SyncCablesView)

        view.display_sync_results(request, {key: [] for key in SYNC_RESULT_KEYS})

        assert message_texts(request) == []

    def test_a_partial_result_dict_still_reports_what_it_carries(self):
        """Existing callers pass only the keys they populate, so absent keys must not raise."""
        from netbox_librenms_plugin.tests.view_test_helpers import message_texts
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        request = make_request("post", user=make_superuser("cable-results-partial-user"))
        view = object.__new__(SyncCablesView)

        view.display_sync_results(request, {"valid": [], "invalid": [], "duplicate": [], "missing_remote": ["if-only"]})

        assert message_texts(request, "error") == ["Remote device or interface not found in NetBox for: if-only"]
