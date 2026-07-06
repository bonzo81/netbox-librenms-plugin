"""
Tests for LibreNMS cable-sync overwrite protection.

Covers, against REAL Cable / Tag / ConsoleServerPort / ConsolePort / FrontPort / RearPort rows:
  - classify_cable_action(): the tag-based decision (create / noop / tag_only / safe_overwrite /
    needs_force) that gates whether a sync may touch an existing cable.
  - render_cable_trace(): the full end-to-end path (through patch panels) of a cable that would be
    overwritten, for display in the force-confirm modal.
  - handle_serial_cable_creation(): the view-level behaviour — silent overwrite of a plugin-owned
    ({librenms}-only) cable, a blocked "conflict" on a foreign/untagged/mixed-tag cable, and a
    forced overwrite when the user confirms.

The overwrite gate: a cable may be replaced silently ONLY when its tags are exactly {librenms}.
Any other tag set (a foreign tag, librenms + another tag, or no tags at all) requires the force
modal. A purely additive change (just adding the librenms tag, terminations unchanged) is always
allowed silently.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import cable_together, make_device, make_serial_device
from netbox_librenms_plugin.tests.test_serial_cables_view import _mock_request


def _sync_view(server_key="default"):
    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    sync = object.__new__(SyncCablesView)
    sync.request = _mock_request()
    sync._post_server_key = server_key
    return sync


def _serial_link(csp, cp):
    return {
        "local_port": csp.name,
        "local_port_id": f"serial:{csp.pk}",
        "_source": "serial",
        "netbox_local_interface_id": csp.pk,
        "netbox_remote_interface_id": cp.pk,
    }


def _make_tag(name, color="ff0000"):
    from extras.models import Tag

    return Tag.objects.create(name=name, slug=name, color=color)


def _classify(local, remote):
    """Classify against freshly-loaded ``.cable`` state.

    ``cable_together`` writes the cable but does not update the passed-in termination instances'
    cached ``.cable`` FK; the production view re-fetches each termination (``.objects.get``) so it
    always sees fresh state. Mirror that here by refreshing before classifying.
    """
    from netbox_librenms_plugin.utils import classify_cable_action

    local.refresh_from_db()
    remote.refresh_from_db()
    return classify_cable_action(local, remote)


# ---------------------------------------------------------------------------
# classify_cable_action
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestClassifyCableAction:
    """The tag-based decision that decides whether a sync may touch an existing cable."""

    def test_no_existing_cable_is_create(self):
        _acs, csps, _ = make_serial_device("acs-c1", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device("r-c1", cp_names=["console"])
        decision = _classify(csps[0], cps[0])
        assert decision["action"] == "create"
        assert decision["to_remove"] == []

    def test_same_connection_untagged_is_tag_only(self):
        _acs, csps, _ = make_serial_device("acs-c2", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device("r-c2", cp_names=["console"])
        cable_together(csps[0], cps[0])  # untagged, already the desired connection
        decision = _classify(csps[0], cps[0])
        assert decision["action"] == "tag_only"
        assert decision["to_remove"] == []

    def test_same_connection_librenms_tagged_is_noop(self):
        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        _acs, csps, _ = make_serial_device("acs-c3", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device("r-c3", cp_names=["console"])
        cable = cable_together(csps[0], cps[0])
        cable.tags.add(get_librenms_cable_tag())
        decision = _classify(csps[0], cps[0])
        assert decision["action"] == "noop"

    def test_repoint_over_librenms_only_cable_is_safe_overwrite(self):
        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        _acs, csps, _ = make_serial_device("acs-c4", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device("r-c4", cp_names=["console-A", "console-B"])
        old = cable_together(csps[0], cps[0])  # csp -> console-A
        old.tags.add(get_librenms_cable_tag())
        decision = _classify(csps[0], cps[1])  # sync now wants csp -> console-B
        assert decision["action"] == "safe_overwrite"
        assert old in decision["to_remove"]

    def test_repoint_over_foreign_tagged_cable_needs_force(self):
        _acs, csps, _ = make_serial_device("acs-c5", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device("r-c5", cp_names=["console-A", "console-B"])
        old = cable_together(csps[0], cps[0])
        old.tags.add(_make_tag("dcim-modeled"))
        decision = _classify(csps[0], cps[1])
        assert decision["action"] == "needs_force"
        assert old in decision["to_remove"]

    def test_repoint_over_untagged_cable_needs_force(self):
        _acs, csps, _ = make_serial_device("acs-c6", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device("r-c6", cp_names=["console-A", "console-B"])
        cable_together(csps[0], cps[0])  # untagged — not ours
        decision = _classify(csps[0], cps[1])
        assert decision["action"] == "needs_force"

    def test_librenms_plus_other_tag_needs_force(self):
        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        _acs, csps, _ = make_serial_device("acs-c7", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device("r-c7", cp_names=["console-A", "console-B"])
        old = cable_together(csps[0], cps[0])
        old.tags.add(get_librenms_cable_tag())
        old.tags.add(_make_tag("keep", color="00ff00"))
        decision = _classify(csps[0], cps[1])
        assert decision["action"] == "needs_force"

    def test_remote_end_cabled_elsewhere_is_collected_for_removal(self):
        """A cable on the REMOTE termination (not just the local one) must also be gated/removed."""
        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        _acs, csps, _ = make_serial_device("acs-c8", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device("r-c8", cp_names=["console"])
        _o, ocsps, _ = make_serial_device("other-c8", csp_names=["ttyX"])
        remote_cable = cable_together(cps[0], ocsps[0])  # console already cabled to another CSP
        remote_cable.tags.add(get_librenms_cable_tag())
        decision = _classify(csps[0], cps[0])  # sync wants our-csp -> console
        assert decision["action"] == "safe_overwrite"
        assert remote_cable in decision["to_remove"]


# ---------------------------------------------------------------------------
# render_cable_trace
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestRenderCableTrace:
    """The full end-to-end path of a cable, spanning patch-panel front/rear pass-throughs."""

    def _panel(self, name):
        # This NetBox models front/rear pass-through via a PortMapping table (not a rear_port FK).
        from dcim.models import FrontPort, PortMapping, RearPort

        panel = make_device(name)
        rp = RearPort.objects.create(device=panel, name="R1", type="8p8c", positions=1)
        fp = FrontPort.objects.create(device=panel, name="F1", type="8p8c", positions=1)
        PortMapping.objects.create(
            device=panel, front_port=fp, rear_port=rp, front_port_position=1, rear_port_position=1
        )
        return panel, fp, rp

    def test_trace_through_patch_panel_reaches_end_device(self):
        from dcim.models import Cable

        from netbox_librenms_plugin.utils import render_cable_trace

        _acs, csps, _ = make_serial_device("acs-trace", csp_names=["ttyS1"])
        _panel, fp, rp = self._panel("panel-trace")
        _end, _, cps = make_serial_device("end-trace", cp_names=["console"])

        first = cable_together(csps[0], fp)  # ttyS1 -- F1
        cable_together(rp, cps[0])  # R1 -- console@end-trace

        # Re-fetch fresh so a_terminations + CablePath resolve (production passes freshly-loaded
        # cables from the termination FKs); the in-memory cable_together return is stale.
        first = Cable.objects.get(pk=first.pk)
        hops = render_cable_trace(first)
        flat = " ".join(str(hop) for hop in hops)
        # The trace must run all the way through the panel to the end device's console port.
        assert "end-trace" in flat
        assert "console" in flat
        assert "ttyS1" in flat
        # More than one segment (through the panel), i.e. not a single point-to-point hop.
        assert len(hops) >= 2


# ---------------------------------------------------------------------------
# view-level overwrite behaviour (force gate)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSerialCableOverwriteBehaviour:
    """handle_serial_cable_creation honours the overwrite gate and the force flag end-to-end."""

    def _setup(self, name):
        acs, csps, _ = make_serial_device(f"acs-{name}", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device(f"r-{name}", cp_names=["console-A", "console-B"])
        return acs, csps[0], cps[0], cps[1]

    def test_repoint_over_librenms_cable_overwrites_silently(self):
        from dcim.models import Cable
        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        acs, csp, cp_a, cp_b = self._setup("ovr")
        old = cable_together(csp, cp_a)
        old.tags.add(get_librenms_cable_tag())

        sync = _sync_view()
        result = sync.handle_serial_cable_creation(_serial_link(csp, cp_b), {"device_id": acs.id})

        assert result["status"] == "overwritten"
        assert not Cable.objects.filter(pk=old.pk).exists()  # old cable gone
        csp.refresh_from_db()
        cp_b.refresh_from_db()
        assert csp.cable_id is not None and csp.cable_id == cp_b.cable_id  # now csp -> console-B

    def test_conflict_on_foreign_cable_without_force_leaves_db_untouched(self):
        from dcim.models import Cable

        acs, csp, cp_a, cp_b = self._setup("conf")
        old = cable_together(csp, cp_a)
        old.tags.add(_make_tag("dcim-modeled"))

        sync = _sync_view()
        result = sync.handle_serial_cable_creation(_serial_link(csp, cp_b), {"device_id": acs.id})

        assert result["status"] == "conflict"
        assert Cable.objects.filter(pk=old.pk).exists()  # foreign cable survives
        csp.refresh_from_db()
        assert csp.cable_id == old.pk  # still cabled to console-A
        assert result.get("port_id") == _serial_link(csp, cp_b)["local_port_id"]
        assert result.get("trace")  # trace carried for the modal

    def test_force_overwrites_foreign_cable(self):
        from dcim.models import Cable

        acs, csp, cp_a, cp_b = self._setup("force")
        old = cable_together(csp, cp_a)
        old.tags.add(_make_tag("dcim-modeled"))

        sync = _sync_view()
        result = sync.handle_serial_cable_creation(_serial_link(csp, cp_b), {"device_id": acs.id}, force=True)

        assert result["status"] == "overwritten"
        assert not Cable.objects.filter(pk=old.pk).exists()
        csp.refresh_from_db()
        cp_b.refresh_from_db()
        assert csp.cable_id == cp_b.cable_id

    def test_same_connection_untagged_gets_tagged_not_recreated(self):
        acs, csp, cp_a, _cp_b = self._setup("tag")
        cable = cable_together(csp, cp_a)  # untagged, already the desired connection

        sync = _sync_view()
        result = sync.handle_serial_cable_creation(_serial_link(csp, cp_a), {"device_id": acs.id})

        assert result["status"] == "tagged"
        cable.refresh_from_db()
        assert "librenms" in set(cable.tags.values_list("slug", flat=True))
        csp.refresh_from_db()
        assert csp.cable_id == cable.pk  # same cable, not recreated
