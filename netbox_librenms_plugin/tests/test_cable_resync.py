"""
Tests for the re-sync affordance on already-cabled cable rows.

Cabled rows used to be dead ends: ``check_cable_status`` / ``check_serial_cable_status`` set
``can_create_cable=False`` whenever either end had a cable, so the UI never offered a Sync
action and the overwrite gate (``classify_cable_action``) was only reachable through the
stale-cache race. These tests pin the re-sync semantics, against REAL Device / Interface /
ConsoleServerPort / ConsolePort / FrontPort / RearPort / Cable / Tag rows:

  - desired connection already cabled directly:
      untagged -> "Cable Found", Sync offered -> adopting it is a tag-only change
      tagged   -> "Cable Found", no action (nothing to do)
  - cabled endpoints whose traced path REACHES the LibreNMS target (a patch-panel remodel):
      -> "Connected via Patch Path", no action (a remodel is a better model of the same link)
  - cabled somewhere that does NOT reach the target:
      -> "Cable Mismatch", Sync offered -> the classify gate applies (silent overwrite of a
         plugin-owned cable, force modal otherwise)
"""

import pytest

from netbox_librenms_plugin.tests.conftest import cable_together, make_device, make_interface, make_serial_device
from netbox_librenms_plugin.tests.test_serial_cables_view import _make_view


def _panel(name):
    """Build a 1-position patch panel; front/rear pass-through via PortMapping."""
    from dcim.models import FrontPort, PortMapping, RearPort

    panel = make_device(name)
    rp = RearPort.objects.create(device=panel, name="R1", type="8p8c", positions=1)
    fp = FrontPort.objects.create(device=panel, name="F1", type="8p8c", positions=1)
    PortMapping.objects.create(device=panel, front_port=fp, rear_port=rp, front_port_position=1, rear_port_position=1)
    return panel, fp, rp


def _serial_row(csp, label, obj):
    """A serial cable row as it comes out of the cache strip (raw keys only)."""
    return {
        "_source": "serial",
        "device_id": obj.id,
        "local_port": csp.name,
        "local_port_id": f"serial:{csp.pk}",
        "sensor_id": csp.pk,
        "sensor_index_int": 1,
        "is_configured": True,
        "remote_device": label,
    }


def _librenms_tag():
    from netbox_librenms_plugin.utils import get_librenms_cable_tag

    return get_librenms_cable_tag()


# ---------------------------------------------------------------------------
# cable_path_reaches
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestCablePathReaches:
    """Path-reach detection: direct cable, through a panel, wrong device, and un-cabled."""

    def test_direct_cable_reaches_remote_termination(self):
        acs, (csp,), _ = make_serial_device("reach-direct", csp_names=["ttyS1"])
        _r, _, (cp,) = make_serial_device("reach-direct-r", cp_names=["console"])
        cable_together(csp, cp)
        csp.refresh_from_db()

        from netbox_librenms_plugin.utils import cable_path_reaches

        assert cable_path_reaches(csp, remote_termination=cp)
        assert cable_path_reaches(csp, remote_device=cp.device)

    def test_panel_path_reaches_end_device(self):
        acs, (csp,), _ = make_serial_device("reach-panel", csp_names=["ttyS1"])
        _panel_dev, fp, rp = _panel("reach-panel-pp")
        end, _, (cp,) = make_serial_device("reach-panel-end", cp_names=["console"])
        cable_together(csp, fp)
        cable_together(rp, cp)
        csp.refresh_from_db()

        from netbox_librenms_plugin.utils import cable_path_reaches

        assert cable_path_reaches(csp, remote_termination=cp)
        assert cable_path_reaches(csp, remote_device=end)

    def test_path_to_other_device_does_not_reach(self):
        acs, (csp,), _ = make_serial_device("reach-wrong", csp_names=["ttyS1"])
        _r, _, (cp,) = make_serial_device("reach-wrong-r", cp_names=["console"])
        other = make_device("reach-wrong-other")
        cable_together(csp, cp)
        csp.refresh_from_db()

        from netbox_librenms_plugin.utils import cable_path_reaches

        assert not cable_path_reaches(csp, remote_device=other)

    def test_uncabled_termination_does_not_reach(self):
        acs, (csp,), _ = make_serial_device("reach-free", csp_names=["ttyS1"])
        _r, _, (cp,) = make_serial_device("reach-free-r", cp_names=["console"])

        from netbox_librenms_plugin.utils import cable_path_reaches

        assert not cable_path_reaches(csp, remote_termination=cp)


# ---------------------------------------------------------------------------
# Interface rows (check_cable_status)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestInterfaceRowResyncStatus:
    """check_cable_status offers the right action for every cabled-interface state."""

    def _link(self, local, remote):
        return {"netbox_local_interface_id": local.pk, "netbox_remote_interface_id": remote.pk}

    def test_matched_untagged_cable_offers_adopt(self):
        local = make_interface(make_device("if-adopt-l"), "Gi0/1")
        remote = make_interface(make_device("if-adopt-r"), "Gi0/2")
        cable_together(local, remote)

        link = _make_view().check_cable_status(self._link(local, remote))
        assert link["cable_status"] == "Cable Found"
        assert link["can_create_cable"] is True  # sync adopts (tag only)

    def test_matched_tagged_cable_offers_no_action(self):
        local = make_interface(make_device("if-noop-l"), "Gi0/1")
        remote = make_interface(make_device("if-noop-r"), "Gi0/2")
        cable = cable_together(local, remote)
        cable.tags.add(_librenms_tag())

        link = _make_view().check_cable_status(self._link(local, remote))
        assert link["cable_status"] == "Cable Found"
        assert link["can_create_cable"] is False  # nothing to do

    def test_patch_path_to_target_offers_no_action(self):
        local = make_interface(make_device("if-path-l"), "Gi0/1")
        remote = make_interface(make_device("if-path-r"), "Gi0/2")
        _panel_dev, fp, rp = _panel("if-path-pp")
        cable_together(local, fp)
        cable_together(rp, remote)

        link = _make_view().check_cable_status(self._link(local, remote))
        assert link["cable_status"] == "Connected via Patch Path"
        assert link["can_create_cable"] is False  # the remodel is a better model of the same link

    def test_local_cabled_elsewhere_is_mismatch(self):
        local = make_interface(make_device("if-mis-l"), "Gi0/1")
        remote = make_interface(make_device("if-mis-r"), "Gi0/2")
        third = make_interface(make_device("if-mis-3"), "Gi0/3")
        cable_together(local, third)

        link = _make_view().check_cable_status(self._link(local, remote))
        assert link["cable_status"] == "Cable Mismatch"
        assert link["can_create_cable"] is True  # re-sync offered, gated by classify

    def test_remote_cabled_elsewhere_is_mismatch(self):
        local = make_interface(make_device("if-mis2-l"), "Gi0/1")
        remote = make_interface(make_device("if-mis2-r"), "Gi0/2")
        third = make_interface(make_device("if-mis2-3"), "Gi0/3")
        cable_together(remote, third)

        link = _make_view().check_cable_status(self._link(local, remote))
        assert link["cable_status"] == "Cable Mismatch"
        assert link["can_create_cable"] is True

    def test_oob_row_never_offers_resync(self):
        local = make_interface(make_device("if-oob-l"), "Gi0/1")
        remote = make_interface(make_device("if-oob-r"), "Gi0/2")
        third = make_interface(make_device("if-oob-3"), "Gi0/3")
        cable_together(local, third)

        link = self._link(local, remote)
        link["_source"] = "oob"
        link = _make_view().check_cable_status(link)
        assert link["can_create_cable"] is False  # OOB rows are context-only


# ---------------------------------------------------------------------------
# Serial rows (enrich_links_data end-to-end over real DB state)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSerialRowResyncStatus:
    """The serial enrich resolves cabled CSPs to adopt / connected / mismatch states."""

    def _enrich(self, obj, row):
        view = _make_view()
        return view.enrich_links_data([row], obj, server_key="default")[0]

    def test_matched_untagged_serial_cable_offers_adopt(self):
        acs, (csp,), _ = make_serial_device("ser-adopt", csp_names=["ttyS1"])
        remote_dev, _, (cp,) = make_serial_device("ser-adopt-r", cp_names=["console"])
        cable_together(csp, cp)

        link = self._enrich(acs, _serial_row(csp, "ser-adopt-r", acs))
        assert link["cable_status"] == "Cable Found"
        assert link["can_create_cable"] is True  # sync adopts (tag only)
        # The resolved remote is the CP the cable ALREADY lands on, not a fresh free port.
        assert link["netbox_remote_interface_id"] == cp.pk

    def test_matched_tagged_serial_cable_offers_no_action(self):
        acs, (csp,), _ = make_serial_device("ser-noop", csp_names=["ttyS1"])
        _r, _, (cp,) = make_serial_device("ser-noop-r", cp_names=["console"])
        cable = cable_together(csp, cp)
        cable.tags.add(_librenms_tag())

        link = self._enrich(acs, _serial_row(csp, "ser-noop-r", acs))
        assert link["cable_status"] == "Cable Found"
        assert link["can_create_cable"] is False

    def test_patch_path_to_label_device_offers_no_action(self):
        acs, (csp,), _ = make_serial_device("ser-path", csp_names=["ttyS1"])
        _panel_dev, fp, rp = _panel("ser-path-pp")
        _end, _, (cp,) = make_serial_device("ser-path-end", cp_names=["console"])
        cable_together(csp, fp)
        cable_together(rp, cp)

        link = self._enrich(acs, _serial_row(csp, "ser-path-end", acs))
        assert link["cable_status"] == "Connected via Patch Path"
        assert link["can_create_cable"] is False

    def test_cabled_to_wrong_device_is_mismatch_with_repoint_target(self):
        acs, (csp,), _ = make_serial_device("ser-mis", csp_names=["ttyS1"])
        _wrong, _, (wrong_cp,) = make_serial_device("ser-mis-wrong", cp_names=["console"])
        _label_dev, _, (free_cp,) = make_serial_device("ser-mis-label", cp_names=["console"])
        cable_together(csp, wrong_cp)

        link = self._enrich(acs, _serial_row(csp, "ser-mis-label", acs))
        assert link["cable_status"] == "Cable Mismatch"
        assert link["can_create_cable"] is True
        # Re-sync re-points at the label-matched device's free ConsolePort.
        assert link["netbox_remote_interface_id"] == free_cp.pk

    def test_cabled_row_with_unresolvable_label_stays_inactionable(self):
        acs, (csp,), _ = make_serial_device("ser-nolabel", csp_names=["ttyS1"])
        _r, _, (cp,) = make_serial_device("ser-nolabel-r", cp_names=["console"])
        cable_together(csp, cp)

        link = self._enrich(acs, _serial_row(csp, "no-such-device-anywhere", acs))
        assert link["cable_status"] == "Cable Found"  # cabled, but no LibreNMS target to compare
        assert link["can_create_cable"] is False


# ---------------------------------------------------------------------------
# Adopt flow end-to-end through the real request stack
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestCableAdoptHtmx:
    """Syncing a matched-but-untagged row through the real HTTP stack adopts the cable:
    the librenms tag is added, the cable is NOT deleted/recreated, and no modal is raised.
    """

    def test_sync_adopts_matched_untagged_cable(self):
        from dcim.models import Cable
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, (csp,), _ = make_serial_device("acs-adopt-e2e", csp_names=["ttyS9"])
        _r, _, (cp,) = make_serial_device("router-adopt-e2e", cp_names=["console"])
        cable = cable_together(csp, cp)  # untagged: the pre-enrichment plugin cable case

        link = {
            "local_port": "ttyS9",
            "local_port_id": f"serial:{csp.pk}-s",
            "_source": "serial",
            "device_id": acs.id,
            "remote_device": "router-adopt-e2e",
            "netbox_local_interface_id": csp.pk,
            "netbox_remote_interface_id": cp.pk,
            "can_create_cable": True,
            "is_configured": True,
            "sensor_id": 9,
            "sensor_index_int": 9,
        }
        key_view = object.__new__(SyncCablesView)
        cache.set(key_view.get_cache_key(acs, "links", "default"), {"links": [link]}, timeout=300)

        user = get_user_model().objects.create_superuser("adopt-admin", "adopt@example.com", "pw")
        client = Client()
        client.force_login(user)
        url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk])
        resp = client.post(url, data={"select": link["local_port_id"], "server_key": "default"}, HTTP_HX_REQUEST="true")

        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'id="cable-force-submit"' not in content  # no modal — adopting is non-destructive
        csp.refresh_from_db()
        assert csp.cable_id == cable.pk  # SAME cable, not recreated
        assert Cable.objects.filter(pk=cable.pk).exists()
        assert "librenms" in set(csp.cable.tags.values_list("slug", flat=True))  # adopted
        # The re-rendered row now shows the tagged state: Cable Found with no Sync button.
        assert "Cable Found" in content


# ---------------------------------------------------------------------------
# Cabled rows display the cable's ACTUAL far end
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestCabledSerialRowFarEndDisplay:
    """A cabled serial row must show where the cable REALLY goes — the far-end device and
    port, linked — even when the LibreNMS label resolves to nothing (or to something else).
    The cable itself knows its far end; an unresolvable name hint must not leave the Remote
    Device / Remote Port columns dead.
    """

    def _enrich(self, obj, row):
        return _make_view().enrich_links_data([row], obj, server_key="default")[0]

    def _row(self, csp, label, obj):
        return {
            "_source": "serial",
            "device_id": obj.id,
            "local_port": csp.name,
            "local_port_id": f"serial:{csp.pk}",
            "sensor_id": csp.pk,
            "sensor_index_int": 1,
            "is_configured": True,
            "remote_device": label,
        }

    def test_unresolvable_label_shows_actual_far_end(self):
        acs, (csp,), _ = make_serial_device("farend-ser", csp_names=["ttyS1"])
        actual, _, (cp,) = make_serial_device("farend-ser-actual", cp_names=["console"])
        cable_together(csp, cp)

        link = self._enrich(acs, self._row(csp, "name-that-matches-nothing", acs))

        assert link["cable_status"] == "Cable Found"
        assert link["remote_device_display"] == "farend-ser-actual"  # reality, not the dead label
        assert link["remote_device"] == "name-that-matches-nothing"  # raw label preserved for re-enrich
        assert link["remote_device_url"]
        assert link["remote_port_name"] == "console"
        assert link["remote_port_url"]
        assert link["can_create_cable"] is False  # display only — no sync target from a dead label

    def test_tagged_trusted_cable_shows_actual_far_end_not_label_device(self):
        """The trust rule keeps the row inactionable, but the display must show where the
        tagged cable REALLY goes — not the wrong-name label device."""
        acs, (csp,), _ = make_serial_device("farend-trust", csp_names=["ttyS1"])
        actual, _, (cp,) = make_serial_device("farend-trust-actual", cp_names=["console"])
        _label_dev, _, _ = make_serial_device("farend-trust-label", cp_names=["console"])
        cable = cable_together(csp, cp)
        cable.tags.add(_librenms_tag())

        link = self._enrich(acs, self._row(csp, "farend-trust-label", acs))

        assert link["cable_status"] == "Cable Found"
        assert link["remote_device_display"] == "farend-trust-actual"
        assert link["remote_port_name"] == "console"
        assert link["can_create_cable"] is False

    def test_far_end_display_does_not_leak_into_the_cached_label(self):
        """Enrichment must be idempotent across the cache round-trip: the far-end DISPLAY name
        must not overwrite the raw LibreNMS label, or a fresh refresh and a cached re-render
        disagree forever (label dead -> "Cable Found"; leaked far-end name resolves -> the same
        row flips to "Connected via Patch Path" on the next render)."""
        from netbox_librenms_plugin.views.base.cables_view import _RAW_LINK_KEYS

        acs, (csp,), _ = make_serial_device("leak-ser", csp_names=["ttyS1"])
        _panel_dev, fp, rp = _panel("leak-ser-pp")
        _end, _, (cp,) = make_serial_device("leak-ser-end", cp_names=["console"])
        cable_together(csp, fp)
        cable_together(rp, cp)

        fresh = self._enrich(acs, self._row(csp, "name-that-matches-nothing", acs))
        assert fresh["cable_status"] == "Cable Found"  # dead label: no sync target
        assert fresh["remote_device"] == "name-that-matches-nothing"  # RAW label untouched
        assert fresh["remote_device_display"] == "leak-ser-end"  # display shows reality

        # Cache round-trip (what a browser F5 does): strip to raw keys, re-enrich.
        stripped = {k: v for k, v in fresh.items() if k in _RAW_LINK_KEYS}
        again = self._enrich(acs, stripped)
        assert again["cable_status"] == "Cable Found"  # same status as the fresh render
        assert again["remote_device"] == "name-that-matches-nothing"

    def test_patch_path_row_shows_path_end_not_panel(self):
        """A Connected-via-Patch-Path row shows the END of the traced path (the real console),
        not the panel port the first cable segment lands on."""
        acs, (csp,), _ = make_serial_device("farend-path", csp_names=["ttyS1"])
        _panel_dev, fp, rp = _panel("farend-path-pp")
        end, _, (cp,) = make_serial_device("farend-path-end", cp_names=["console"])
        cable_together(csp, fp)
        cable_together(rp, cp)

        link = self._enrich(acs, self._row(csp, "farend-path-end", acs))

        assert link["cable_status"] == "Connected via Patch Path"
        assert link["remote_port_name"] == "console"  # the path end, not "F1"
        assert link["remote_port_url"]
