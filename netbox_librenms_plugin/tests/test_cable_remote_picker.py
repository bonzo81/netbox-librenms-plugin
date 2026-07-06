"""
Tests for the cable remote-end picker.

Remote-end matching is name-based (the serial label / LLDP port name), which is a matter of
luck — names often don't match. The picker lets the user choose the remote endpoint by hand:

- The pick is stored on the CACHED row as ``manual_remote_id`` (a raw, strip-surviving key),
  and enrichment resolves it in preference to label/name matching. It only needs to live
  until the sync runs — after that the real cable drives the row state.
- The picker endpoint serves the modal (device search -> port list) and the POST that writes
  the pick into the cached row.
- Trust rule: a serial cable that carries the librenms tag is trusted over the label — the
  label is only a hint, so a tagged cable must NOT flip to "Cable Mismatch" against a
  wrong-name label (that would offer a silent re-point of a deliberately-placed cable).

All tests run against real Device / ConsoleServerPort / ConsolePort / Interface / Cable rows.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import cable_together, make_device, make_interface, make_serial_device
from netbox_librenms_plugin.tests.test_serial_cables_view import _make_view


def _serial_row(csp, label, obj, **extra):
    row = {
        "_source": "serial",
        "device_id": obj.id,
        "local_port": csp.name,
        "local_port_id": f"serial:{csp.pk}",
        "sensor_id": csp.pk,
        "sensor_index_int": 1,
        "is_configured": True,
        "remote_device": label,
    }
    row.update(extra)
    return row


def _librenms_tag():
    from netbox_librenms_plugin.utils import get_librenms_cable_tag

    return get_librenms_cable_tag()


# ---------------------------------------------------------------------------
# manual_remote_id enrichment
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestManualRemoteEnrichment:
    """enrich honors a manually picked remote over label/name matching."""

    def _enrich(self, obj, row):
        return _make_view().enrich_links_data([row], obj, server_key="default")[0]

    def test_manual_pick_resolves_serial_row_without_label_match(self):
        """A serial row whose label matches nothing still resolves via manual_remote_id."""
        acs, (csp,), _ = make_serial_device("pick-ser", csp_names=["ttyS1"])
        _r, _, (cp,) = make_serial_device("pick-ser-target", cp_names=["console"])

        row = _serial_row(csp, "name-that-matches-nothing", acs, manual_remote_id=cp.pk)
        link = self._enrich(acs, row)

        assert link["netbox_remote_interface_id"] == cp.pk
        assert link["remote_port_name"] == "console"
        assert link["can_create_cable"] is True
        assert link["cable_status"] == "No Cable"

    def test_manual_pick_survives_cache_strip(self):
        """manual_remote_id is a raw key: it survives the cached-render strip and re-resolves."""
        from netbox_librenms_plugin.views.base.cables_view import _RAW_LINK_KEYS

        acs, (csp,), _ = make_serial_device("pick-strip", csp_names=["ttyS1"])
        _r, _, (cp,) = make_serial_device("pick-strip-target", cp_names=["console"])

        row = _serial_row(csp, "nope", acs, manual_remote_id=cp.pk)
        stripped = {k: v for k, v in row.items() if k in _RAW_LINK_KEYS}
        assert stripped.get("manual_remote_id") == cp.pk  # survived the strip

        link = self._enrich(acs, stripped)
        assert link["netbox_remote_interface_id"] == cp.pk
        assert link["can_create_cable"] is True

    def test_manual_pick_beats_label_match(self):
        """When BOTH a label match and a manual pick exist, the manual pick wins."""
        acs, (csp,), _ = make_serial_device("pick-beats", csp_names=["ttyS1"])
        _label_dev, _, (label_cp,) = make_serial_device("pick-beats-label", cp_names=["console"])
        _picked, _, (picked_cp,) = make_serial_device("pick-beats-picked", cp_names=["console"])

        row = _serial_row(csp, "pick-beats-label", acs, manual_remote_id=picked_cp.pk)
        link = self._enrich(acs, row)

        assert link["netbox_remote_interface_id"] == picked_cp.pk
        assert link["netbox_remote_device_id"] == picked_cp.device_id

    def test_manual_pick_resolves_interface_row(self):
        """A non-serial row uses the manual pick as its remote Interface."""
        local_dev = make_device("pick-if-l")
        local = make_interface(local_dev, "Gi0/1")
        remote_dev = make_device("pick-if-r")
        remote = make_interface(remote_dev, "Gi0/48")

        view = _make_view()
        row = {
            "local_port": "Gi0/1",
            "local_port_id": "1001",
            "remote_device": "name-that-matches-nothing",
            "remote_port": "also-no-match",
            "manual_remote_id": remote.pk,
        }
        with _patch_local_port_resolution(view, local):
            link = view.enrich_links_data([row], local_dev, server_key="default")[0]

        assert link["netbox_remote_interface_id"] == remote.pk
        assert link["cable_status"] == "No Cable"
        assert link["can_create_cable"] is True

    def test_manual_pick_matched_cable_offers_adopt(self):
        """A CSP already cabled directly to the manually picked port -> Cable Found + adopt."""
        acs, (csp,), _ = make_serial_device("pick-adopt", csp_names=["ttyS1"])
        _r, _, (cp,) = make_serial_device("pick-adopt-target", cp_names=["console"])
        cable_together(csp, cp)  # untagged

        row = _serial_row(csp, "nope", acs, manual_remote_id=cp.pk)
        link = self._enrich(acs, row)

        assert link["cable_status"] == "Cable Found"
        assert link["can_create_cable"] is True  # adopt (tag only)
        assert link["netbox_remote_interface_id"] == cp.pk

    def test_manual_pick_mismatch_when_cabled_elsewhere(self):
        """A CSP cabled (untagged) somewhere else -> Cable Mismatch re-pointing at the pick."""
        acs, (csp,), _ = make_serial_device("pick-mis", csp_names=["ttyS1"])
        _w, _, (wrong_cp,) = make_serial_device("pick-mis-wrong", cp_names=["console"])
        _t, _, (picked_cp,) = make_serial_device("pick-mis-target", cp_names=["console"])
        cable_together(csp, wrong_cp)

        row = _serial_row(csp, "nope", acs, manual_remote_id=picked_cp.pk)
        link = self._enrich(acs, row)

        assert link["cable_status"] == "Cable Mismatch"
        assert link["can_create_cable"] is True
        assert link["netbox_remote_interface_id"] == picked_cp.pk


# ---------------------------------------------------------------------------
# Tagged serial cables are trusted over the label
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestTaggedSerialCableTrustedOverLabel:
    """A librenms-tagged serial cable was placed deliberately (possibly via a manual pick);
    a wrong-name label must not flip it to a mismatch offering a silent re-point.
    """

    def test_tagged_cable_to_other_device_is_not_a_mismatch(self):
        acs, (csp,), _ = make_serial_device("trust-ser", csp_names=["ttyS1"])
        _actual, _, (actual_cp,) = make_serial_device("trust-ser-actual", cp_names=["console"])
        _label_dev, _, (_label_cp,) = make_serial_device("trust-ser-label", cp_names=["console"])
        cable = cable_together(csp, actual_cp)
        cable.tags.add(_librenms_tag())

        row = _serial_row(csp, "trust-ser-label", acs)
        link = _make_view().enrich_links_data([row], acs, server_key="default")[0]

        assert link["cable_status"] == "Cable Found"
        assert link["can_create_cable"] is False

    def test_untagged_cable_to_other_device_still_mismatches(self):
        """The trust rule is tag-gated: an untagged cable keeps the mismatch/re-point offer."""
        acs, (csp,), _ = make_serial_device("trust-ser2", csp_names=["ttyS1"])
        _actual, _, (actual_cp,) = make_serial_device("trust-ser2-actual", cp_names=["console"])
        _label_dev, _, (_label_cp,) = make_serial_device("trust-ser2-label", cp_names=["console"])
        cable_together(csp, actual_cp)  # untagged

        row = _serial_row(csp, "trust-ser2-label", acs)
        link = _make_view().enrich_links_data([row], acs, server_key="default")[0]

        assert link["cable_status"] == "Cable Mismatch"
        assert link["can_create_cable"] is True


# ---------------------------------------------------------------------------
# Picker endpoint (end-to-end through the real request stack)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestRemotePickerEndpoint:
    """The picker modal, its search/ports actions, and the POST that stores the pick."""

    def _client(self, name):
        from django.contrib.auth import get_user_model
        from django.test import Client

        user = get_user_model().objects.create_superuser(f"picker-{name}", f"{name}@example.com", "pw")
        client = Client()
        client.force_login(user)
        return client

    def _seed_serial(self, name, label="name-that-matches-nothing"):
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, (csp,), _ = make_serial_device(f"acs-{name}", csp_names=["ttyS5"])
        link = _serial_row(csp, label, acs)
        link["local_port_id"] = f"serial:{csp.pk}-s"
        key_view = object.__new__(SyncCablesView)
        cache.set(key_view.get_cache_key(acs, "links", "default"), {"links": [link]}, timeout=300)
        url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[acs.pk])
        return acs, csp, link, url

    def test_get_returns_picker_modal(self):
        client = self._client("modal")
        _acs, _csp, link, url = self._seed_serial("modal")

        resp = client.get(url, {"port_id": link["local_port_id"], "server_key": "default"})

        assert resp.status_code == 200
        content = resp.content.decode()
        assert "ttyS5" in content  # row context shown
        assert 'name="q"' in content  # device search input
        assert "remote-picker-ports" in content  # port area placeholder

    def test_search_action_filters_devices(self):
        client = self._client("search")
        _acs, _csp, link, url = self._seed_serial("search")
        target = make_device("picker-search-target")
        make_device("picker-search-other")

        resp = client.get(
            url,
            {"port_id": link["local_port_id"], "server_key": "default", "action": "search", "q": "search-target"},
        )

        assert resp.status_code == 200
        content = resp.content.decode()
        assert "picker-search-target" in content
        assert "picker-search-other" not in content
        assert str(target.pk) in content

    def test_ports_action_lists_console_ports_for_serial_rows(self):
        client = self._client("ports")
        _acs, _csp, link, url = self._seed_serial("ports")
        remote, _, (cp,) = make_serial_device("picker-ports-target", cp_names=["console-X"])
        make_interface(remote, "Gi0/1")  # an Interface that must NOT be offered for a serial row

        resp = client.get(
            url,
            {
                "port_id": link["local_port_id"],
                "server_key": "default",
                "action": "ports",
                "device_id": remote.pk,
            },
        )

        assert resp.status_code == 200
        content = resp.content.decode()
        assert "console-X" in content
        assert str(cp.pk) in content
        assert "Gi0/1" not in content  # serial rows pick ConsolePorts, not Interfaces

    def test_ports_action_survives_cache_expiry_via_source_param(self):
        """The ports fragment must not silently switch port TYPE when the cache expires: the row's _source travels in the picker URL, so a serial picker still lists ConsolePorts after a flush."""
        from django.core.cache import cache

        client = self._client("expiry")
        _acs, _csp, link, url = self._seed_serial("expiry")
        remote, _, (cp,) = make_serial_device("picker-expiry-target", cp_names=["console-Y"])
        make_interface(remote, "Gi0/9")

        cache.clear()  # snapshot gone between modal-open and device-click

        resp = client.get(
            url,
            {
                "port_id": link["local_port_id"],
                "server_key": "default",
                "action": "ports",
                "device_id": remote.pk,
                "source": "serial",
            },
        )

        assert resp.status_code == 200
        content = resp.content.decode()
        assert "console-Y" in content  # still ConsolePorts, not Interfaces
        assert "Gi0/9" not in content

    def test_modal_urls_carry_the_row_source(self):
        """The rendered modal embeds source=serial in its fragment URLs so follow-up actions don't depend on the cache."""
        client = self._client("srcurl")
        _acs, _csp, link, url = self._seed_serial("srcurl")

        resp = client.get(url, {"port_id": link["local_port_id"], "server_key": "default"})

        assert resp.status_code == 200
        assert "source=serial" in resp.content.decode()

    def test_post_stores_manual_pick_in_cached_row(self):
        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("post")
        acs, _csp, link, url = self._seed_serial("post")
        _r, _, (cp,) = make_serial_device("picker-post-target", cp_names=["console"])

        resp = client.post(
            url,
            data={
                "port_id": link["local_port_id"],
                "server_key": "default",
                "remote_interface_id": cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 200
        key_view = object.__new__(SyncCablesView)
        cached = cache.get(key_view.get_cache_key(acs, "links", "default"))
        row = next(r for r in cached["links"] if r["local_port_id"] == link["local_port_id"])
        assert row["manual_remote_id"] == cp.pk
        assert row["netbox_remote_interface_id"] == cp.pk  # sync-ready without a re-enrich
        assert row["can_create_cable"] is True
        # The response re-renders the cable partial with the resolved remote shown.
        assert "console" in resp.content.decode()

    def test_post_rejects_port_of_wrong_type(self):
        """Picking an Interface for a serial row is rejected and the cache is untouched."""
        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("wrongtype")
        acs, _csp, link, url = self._seed_serial("wrongtype")
        iface = make_interface(make_device("picker-wrongtype-dev"), "Gi0/1")

        resp = client.post(
            url,
            data={
                "port_id": link["local_port_id"],
                "server_key": "default",
                "remote_interface_id": iface.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 400
        key_view = object.__new__(SyncCablesView)
        cached = cache.get(key_view.get_cache_key(acs, "links", "default"))
        row = next(r for r in cached["links"] if r["local_port_id"] == link["local_port_id"])
        assert "manual_remote_id" not in row

    def test_pick_then_sync_creates_cable_to_picked_port(self):
        """Full flow: pick a remote by hand, then sync — a real Cable lands on the picked port."""
        from dcim.models import Cable
        from django.urls import reverse

        client = self._client("flow")
        acs, csp, link, url = self._seed_serial("flow")
        _r, _, (cp,) = make_serial_device("picker-flow-target", cp_names=["console"])

        pick = client.post(
            url,
            data={
                "port_id": link["local_port_id"],
                "server_key": "default",
                "remote_interface_id": cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        assert pick.status_code == 200

        sync_url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk])
        resp = client.post(
            sync_url,
            data={"select": link["local_port_id"], "server_key": "default"},
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 200
        csp.refresh_from_db()
        cp.refresh_from_db()
        assert csp.cable_id is not None
        assert csp.cable_id == cp.cable_id  # cabled to the PICKED port
        assert "librenms" in set(Cable.objects.get(pk=csp.cable_id).tags.values_list("slug", flat=True))


def _patch_local_port_resolution(view, interface):
    """Patch enrich_local_port to resolve to *interface*.

    Non-serial LOCAL matching is name-map-based and out of scope here — the manual
    REMOTE pick is what's under test.
    """
    from unittest.mock import patch

    def fake_local(link, obj, server_key=None, sync_device=None):
        link["netbox_local_interface_id"] = interface.pk
        return None

    return patch.object(view, "enrich_local_port", side_effect=fake_local)


# ---------------------------------------------------------------------------
# Re-pointing an EXISTING cable via the picker (always modal-confirmed)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestManualRepointOfExistingCable:
    """Cabled rows offer the picker too, and a manual re-point ALWAYS confirms through the
    warning modal — even over a plugin-owned cable. The silent safe-overwrite is reserved for
    LibreNMS-driven re-points (refresh data moved); a human-initiated change of a live cable
    gets the full-trace warning and the force checkbox.
    """

    def test_tagged_cabled_row_offers_the_picker(self):
        """A satisfied (tagged Cable Found) row still offers the pick-remote action so the cable can be changed."""
        acs, (csp,), _ = make_serial_device("repoint-aff", csp_names=["ttyS1"])
        _r, _, (cp,) = make_serial_device("repoint-aff-r", cp_names=["console"])
        cable = cable_together(csp, cp)
        cable.tags.add(_librenms_tag())

        link = _make_view().enrich_links_data([_serial_row(csp, "repoint-aff-r", acs)], acs, server_key="default")[0]

        assert link["can_create_cable"] is False  # nothing to sync as-is
        assert link.get("picker_url")  # ...but the cable can be re-pointed

    def test_patch_path_row_offers_the_picker(self):
        """A Connected-via-Patch-Path row offers the picker (re-pointing replaces the whole path, modal-confirmed)."""
        from dcim.models import FrontPort, PortMapping, RearPort

        acs, (csp,), _ = make_serial_device("repoint-path", csp_names=["ttyS1"])
        panel = make_device("repoint-path-pp")
        rp = RearPort.objects.create(device=panel, name="R1", type="8p8c", positions=1)
        fp = FrontPort.objects.create(device=panel, name="F1", type="8p8c", positions=1)
        PortMapping.objects.create(
            device=panel, front_port=fp, rear_port=rp, front_port_position=1, rear_port_position=1
        )
        end, _, (cp,) = make_serial_device("repoint-path-end", cp_names=["console"])
        cable_together(csp, fp)
        cable_together(rp, cp)

        link = _make_view().enrich_links_data([_serial_row(csp, "repoint-path-end", acs)], acs, server_key="default")[0]

        assert link["cable_status"] == "Connected via Patch Path"
        assert link.get("picker_url")

    def test_manual_repoint_over_owned_cable_requires_force(self):
        """Without force, a manual re-point of a plugin-owned cable defers to the modal (DB untouched)."""
        from dcim.models import Cable

        from netbox_librenms_plugin.tests.test_cable_overwrite import _serial_link, _sync_view

        acs, (csp,), _ = make_serial_device("repoint-own", csp_names=["ttyS1"])
        _r, _, (cp_a, cp_b) = make_serial_device("repoint-own-r", cp_names=["console-A", "console-B"])
        old = cable_together(csp, cp_a)
        old.tags.add(_librenms_tag())  # plugin-owned: LibreNMS-driven re-point would be silent

        link = _serial_link(csp, cp_b)
        link["manual_remote_id"] = cp_b.pk  # ...but this re-point is a human decision

        result = _sync_view().handle_serial_cable_creation(link, {"device_id": acs.id})

        assert result["status"] == "conflict"
        assert result.get("trace")  # the modal shows what would be destroyed
        assert Cable.objects.filter(pk=old.pk).exists()
        csp.refresh_from_db()
        assert csp.cable_id == old.pk

    def test_manual_repoint_with_force_replaces_owned_cable(self):
        """With force confirmed, the manual re-point deletes the old cable and lands on the pick."""
        from dcim.models import Cable

        from netbox_librenms_plugin.tests.test_cable_overwrite import _serial_link, _sync_view

        acs, (csp,), _ = make_serial_device("repoint-force", csp_names=["ttyS1"])
        _r, _, (cp_a, cp_b) = make_serial_device("repoint-force-r", cp_names=["console-A", "console-B"])
        old = cable_together(csp, cp_a)
        old.tags.add(_librenms_tag())

        link = _serial_link(csp, cp_b)
        link["manual_remote_id"] = cp_b.pk

        result = _sync_view().handle_serial_cable_creation(link, {"device_id": acs.id}, force=True)

        assert result["status"] == "overwritten"
        assert not Cable.objects.filter(pk=old.pk).exists()
        csp.refresh_from_db()
        cp_b.refresh_from_db()
        assert csp.cable_id == cp_b.cable_id

    def test_librenms_driven_repoint_of_owned_cable_stays_silent(self):
        """No manual pick on the row -> the original safe-overwrite semantics are untouched."""
        from netbox_librenms_plugin.tests.test_cable_overwrite import _serial_link, _sync_view

        acs, (csp,), _ = make_serial_device("repoint-auto", csp_names=["ttyS1"])
        _r, _, (cp_a, cp_b) = make_serial_device("repoint-auto-r", cp_names=["console-A", "console-B"])
        old = cable_together(csp, cp_a)
        old.tags.add(_librenms_tag())

        result = _sync_view().handle_serial_cable_creation(_serial_link(csp, cp_b), {"device_id": acs.id})

        assert result["status"] == "overwritten"  # silent, as designed for data-driven re-points

    def test_pick_then_sync_pops_modal_then_force_replaces(self):
        """Full e2e: pick a new remote on a tagged-cabled row -> sync warns via the modal -> force replaces."""
        from dcim.models import Cable
        from django.urls import reverse

        client = self._client_e2e("repoint-e2e")
        acs, csp, old, link, picker_url = self._seed_cabled("repoint-e2e")
        _t, _, (new_cp,) = make_serial_device("repoint-e2e-target", cp_names=["console"])

        pick = client.post(
            picker_url,
            data={"port_id": link["local_port_id"], "server_key": "default", "remote_interface_id": new_cp.pk},
            HTTP_HX_REQUEST="true",
        )
        assert pick.status_code == 200

        sync_url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk])
        resp = client.post(
            sync_url, data={"select": link["local_port_id"], "server_key": "default"}, HTTP_HX_REQUEST="true"
        )
        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'id="cable-force-submit"' in content  # the warning modal
        # ...including the doomed cable's trace (the plan's core requirement for the modal).
        assert f"#{old.pk}" in content  # the cable segment label
        assert "console" in content  # the old far-end port appears in the hops
        assert Cable.objects.filter(pk=old.pk).exists()  # nothing destroyed yet

        forced = client.post(
            sync_url,
            data={"select": link["local_port_id"], "server_key": "default", "force": "on"},
            HTTP_HX_REQUEST="true",
        )
        assert forced.status_code == 200
        assert not Cable.objects.filter(pk=old.pk).exists()
        csp.refresh_from_db()
        new_cp.refresh_from_db()
        assert csp.cable_id == new_cp.cable_id  # re-pointed to the pick

    def test_pick_post_refetches_when_cache_expired(self):
        """A pick that lands after the snapshot expired re-fetches from LibreNMS instead of erroring."""
        from unittest.mock import patch

        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client_e2e("expiry-post")
        acs, csp, _old, link, picker_url = self._seed_cabled("expiry-post")
        _t, _, (new_cp,) = make_serial_device("expiry-post-target", cp_names=["console"])

        raw_row = dict(link)  # what a fresh LibreNMS fetch would rebuild
        cache.clear()  # the snapshot expired between render and pick

        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceCableTableView.get_links_data",
            return_value=[raw_row],
        ):
            resp = client.post(
                picker_url,
                data={"port_id": link["local_port_id"], "server_key": "default", "remote_interface_id": new_cp.pk},
                HTTP_HX_REQUEST="true",
            )

        assert resp.status_code == 200
        key_view = object.__new__(SyncCablesView)
        cached = cache.get(key_view.get_cache_key(acs, "links", "default"))
        row = next(r for r in cached["links"] if r["local_port_id"] == link["local_port_id"])
        assert row["manual_remote_id"] == new_cp.pk  # the pick survived the expiry

    def test_pick_modal_get_refetches_when_cache_expired(self):
        """Opening the picker after the snapshot expired re-fetches instead of a dead-end warning."""
        from unittest.mock import patch

        from django.core.cache import cache

        client = self._client_e2e("expiry-get")
        _acs, _csp, _old, link, picker_url = self._seed_cabled("expiry-get")

        raw_row = dict(link)
        cache.clear()

        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceCableTableView.get_links_data",
            return_value=[raw_row],
        ):
            resp = client.get(picker_url, {"port_id": link["local_port_id"], "server_key": "default"})

        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'name="q"' in content  # the real picker, not the cache-expired warning
        assert "Cache has expired" not in content

    def _client_e2e(self, name):
        from django.contrib.auth import get_user_model
        from django.test import Client

        user = get_user_model().objects.create_superuser(f"repoint-{name}", f"{name}@example.com", "pw")
        client = Client()
        client.force_login(user)
        return client

    def _seed_cabled(self, name):
        """Seed a cache row for a CSP already cabled (tagged) to its label device."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, (csp,), _ = make_serial_device(f"acs-{name}", csp_names=["ttyS8"])
        _r, _, (cp,) = make_serial_device(f"router-{name}", cp_names=["console"])
        old = cable_together(csp, cp)
        old.tags.add(_librenms_tag())

        link = _serial_row(csp, f"router-{name}", acs)
        link["local_port_id"] = f"serial:{csp.pk}-s"
        key_view = object.__new__(SyncCablesView)
        cache.set(key_view.get_cache_key(acs, "links", "default"), {"links": [link]}, timeout=300)
        picker_url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[acs.pk])
        return acs, csp, old, link, picker_url
