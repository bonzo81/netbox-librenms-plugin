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
