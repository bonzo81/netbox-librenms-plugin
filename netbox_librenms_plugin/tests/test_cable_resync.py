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
      -> "Cable Mismatch", Sync offered -> the force-confirmation gate applies to every cable
"""

import pytest

from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    configured_server_key,
    librenms_cable_tag as _librenms_tag,
    make_device,
    make_interface,
    make_patch_panel as _panel,  # 1-position patch panel; version-gated front/rear wiring
    make_serial_device,
    make_serial_row as _serial_row,
    persist_test_server_mapping,
)
from netbox_librenms_plugin.tests.test_serial_cables_view import _make_view

SERVER_KEY = configured_server_key()


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
        return view.enrich_links_data([row], obj, server_key=SERVER_KEY)[0]

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
        cache.set(key_view.get_cache_key(acs, "links", SERVER_KEY), {"links": [link]}, timeout=300)

        user = get_user_model().objects.create_superuser("adopt-admin", "adopt@example.com", "pw")
        client = Client()
        client.force_login(user)
        url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk])
        persist_test_server_mapping(acs, SERVER_KEY)
        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[acs.pk]),
            {"tab": "cables", "server_key": SERVER_KEY},
        )
        record = next(iter(rendered.context["cable_sync"]["table"].rows)).record
        row_id = record["row_id"]
        resp = client.post(
            url,
            data={
                "sync_one": row_id,
                "server_key": SERVER_KEY,
                f"expected_local_id_{row_id}": record["netbox_local_interface_id"],
                f"expected_local_device_id_{row_id}": record["netbox_local_device_id"],
                f"expected_remote_id_{row_id}": record["netbox_remote_interface_id"],
                f"expected_remote_device_id_{row_id}": record["netbox_remote_device_id"],
            },
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'id="cable-force-submit"' not in content  # no modal — adopting is non-destructive
        csp.refresh_from_db()
        assert csp.cable_id == cable.pk  # SAME cable, not recreated
        assert Cable.objects.filter(pk=cable.pk).exists()
        assert "librenms" in set(csp.cable.tags.values_list("slug", flat=True))  # adopted
        # The re-rendered row now shows the tagged state: Cable Found with no Sync button.
        assert "Cable Found" in content


@pytest.mark.django_db
class TestPatchPathSyncGuard:
    """A correct patch path is display-only, including for crafted sync requests."""

    def test_patch_path_row_cannot_be_replaced_by_a_direct_cable(self):
        from dcim.models import Cable
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local_device = make_device("patch-guard-local")
        local = make_interface(local_device, "Ethernet1")
        remote_device = make_device("patch-guard-remote")
        remote = make_interface(remote_device, "Ethernet1")
        _panel_device, front_port, rear_port = _panel("patch-guard-panel")
        local_segment = cable_together(local, front_port)
        remote_segment = cable_together(rear_port, remote)
        row = {
            "local_port": local.name,
            "local_port_id": 10,
            "remote_device": remote_device.name,
            "remote_port": remote.name,
            "remote_port_id": 20,
            "_source": "main",
        }
        server_key = SERVER_KEY
        sync_view = object.__new__(SyncCablesView)
        cache.set(
            sync_view.get_cache_key(local_device, "links", server_key),
            {"links": [row], "snapshot_token": "patch-path-guard"},
            timeout=300,
        )
        persist_test_server_mapping(local_device, server_key)
        user = get_user_model().objects.create_superuser(
            "patch-guard-admin",
            "patch-guard@example.com",
            "pw",
        )
        client = Client()
        client.force_login(user)

        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local_device.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert rendered.status_code == 200
        table = rendered.context["cable_sync"]["table"]
        record = next(iter(table.rows)).record
        content = table.as_html(rendered.wsgi_request)
        assert "Connected via Patch Path" in content
        selection = next(part.split(">", 1)[0] for part in content.split("<input") if 'name="select"' in part)
        assert 'value="10"' in selection
        assert "disabled" in selection

        cable_state = SyncCablesView._cable_state_token(
            {local_segment.pk: local_segment, remote_segment.pk: remote_segment}
        )
        expected_intent = SyncCablesView._cable_intent_token(cable_state, local, remote)
        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local_device.pk]),
            {
                "select": "10",
                "server_key": server_key,
                "force": "on",
                "expected_cable_intent_10": expected_intent,
                "expected_local_id_10": record["netbox_local_interface_id"],
                "expected_local_device_id_10": record["netbox_local_device_id"],
                "expected_remote_id_10": record["netbox_remote_interface_id"],
                "expected_remote_device_id_10": record["netbox_remote_device_id"],
            },
            HTTP_HX_REQUEST="true",
        )

        assert synced.status_code == 200
        content = synced.content.decode()
        assert "Skipped OOB-controller links" not in content
        assert "already modeled through a patch path" in content
        local.refresh_from_db()
        remote.refresh_from_db()
        assert local.cable_id == local_segment.pk
        assert remote.cable_id == remote_segment.pk
        assert Cable.objects.filter(pk__in=[local_segment.pk, remote_segment.pk]).count() == 2


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
        return _make_view().enrich_links_data([row], obj, server_key=SERVER_KEY)[0]

    def _row(self, csp, label, obj):
        return _serial_row(csp, label, obj)

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


@pytest.mark.django_db
class TestMakeSerialRowSensorIndex:
    """make_serial_row's sensor_index_int is an explicit param (default 1), so multi-row tests can give each row a distinct, production-realistic index (serial_utils sorts rows by it)."""

    def test_defaults_to_one(self):
        acs, (csp,), _ = make_serial_device("idx-default", csp_names=["ttyS1"])
        assert _serial_row(csp, "x", acs)["sensor_index_int"] == 1

    def test_honours_explicit_index(self):
        acs, (csp,), _ = make_serial_device("idx-explicit", csp_names=["ttyS2"])
        assert _serial_row(csp, "x", acs, sensor_index_int=7)["sensor_index_int"] == 7
