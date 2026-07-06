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


# ---------------------------------------------------------------------------
# HTMX force-confirm modal delivery (end-to-end through the real request stack)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestCableOverwriteHtmxModal:
    """A conflicted HTMX sync returns the force-confirm modal out-of-band and leaves the DB
    untouched; re-submitting with ``force=on`` replaces the foreign cable. Driven through the
    real Django request stack (Client) so routing, permissions, the cache read, template
    rendering, and the OOB-swap markup are all exercised end-to-end.
    """

    def _seed(self, name):
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, csps, _ = make_serial_device(f"acs-{name}", csp_names=["ttyS5"])
        _r, _, cps = make_serial_device(f"router-{name}", cp_names=["console-A", "console-B"])
        csp, cp_a, cp_b = csps[0], cps[0], cps[1]

        # A foreign-tagged cable occupies the CSP -> the sync to console-B must conflict.
        old = cable_together(csp, cp_a)
        old.tags.add(_make_tag(f"dcim-modeled-{name}"))

        # Seed the enriched links cache that "Refresh Cables" would have written, targeting
        # console-B (a re-point over the foreign cable).
        link = {
            "local_port": "ttyS5",
            "local_port_id": f"serial:{csp.pk}-s",
            "_source": "serial",
            "device_id": acs.id,
            "remote_device": f"router-{name}",
            "netbox_local_interface_id": csp.pk,
            "netbox_remote_interface_id": cp_b.pk,
            "can_create_cable": True,
            "is_configured": True,
            "sensor_id": 1,
            "sensor_index_int": 5,
        }
        key_view = object.__new__(SyncCablesView)
        cache.set(key_view.get_cache_key(acs, "links", "default"), {"links": [link]}, timeout=300)

        user = get_user_model().objects.create_superuser(f"modal-admin-{name}", f"{name}@example.com", "pw")
        client = Client()
        client.force_login(user)
        url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk])
        return client, url, link, old, csp, cp_b

    def test_conflict_returns_oob_modal_and_leaves_db_untouched(self):
        from dcim.models import Cable

        client, url, link, old, csp, _cp_b = self._seed("conf")
        resp = client.post(url, data={"select": link["local_port_id"], "server_key": "default"}, HTTP_HX_REQUEST="true")

        assert resp.status_code == 200
        content = resp.content.decode()
        # Name the failure mode: a vanished cache row makes the sync a silent no-op (200, no
        # modal, DB untouched) — indistinguishable from a gate bug without this assert.
        assert "Cache has expired" not in content
        # The partial carries the force-confirm modal via an out-of-band swap into the shared shell.
        assert 'id="htmx-modal-content" hx-swap-oob="innerHTML"' in content
        # htmx 2.x fires no afterSettle targeting an innerHTML OOB swap's target, so the page's
        # afterSettle auto-show handler never sees the modal — the OOB block must ship its own
        # show call (the exact mirror of the close_modal block's closeHtmxModal() script).
        assert "openHtmxModal(" in content
        # The destructive warning banner announces itself to assistive tech on injection.
        assert 'class="alert alert-danger py-2 d-flex" role="alert"' in content
        assert 'name="force" value="on"' in content  # the re-submit is pre-armed with force
        assert 'id="cable-force-submit"' in content  # confirm-gated submit button present
        # The modal's re-submit must carry the row's resolved sync device, so a VC member
        # override can't silently revert to the page device on the forced re-submit.
        assert f'name="device_selection_{link["local_port_id"]}"' in content
        # And the DB is untouched: the foreign cable survives, still terminating the CSP.
        assert Cable.objects.filter(pk=old.pk).exists()
        csp.refresh_from_db()
        assert csp.cable_id == old.pk

    def test_force_resubmit_replaces_foreign_cable(self):
        from dcim.models import Cable

        client, url, link, old, csp, cp_b = self._seed("force")
        resp = client.post(
            url,
            data={"select": link["local_port_id"], "server_key": "default", "force": "on"},
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 200
        content = resp.content.decode()
        # Name the failure mode: a vanished cache row makes the sync a silent no-op (200, no
        # modal, DB untouched) — indistinguishable from a gate bug without this assert.
        assert "Cache has expired" not in content
        # No conflict left to confirm -> no force modal in the response.
        assert 'id="cable-force-submit"' not in content
        # The forced submit came FROM the force-confirm modal: the response must ship the
        # close_modal OOB block, or the modal stays open over the refreshed table.
        assert "closeHtmxModal(" in content
        assert not Cable.objects.filter(pk=old.pk).exists()  # foreign cable replaced
        csp.refresh_from_db()
        cp_b.refresh_from_db()
        assert csp.cable_id is not None
        assert csp.cable_id == cp_b.cable_id


# ---------------------------------------------------------------------------
# Overwrite scope: only endpoint segments die; trunks and mid-path stay
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestOverwritePreservesMidPathSegments:
    """A forced re-point must delete ONLY the cable segment(s) directly attached to the two
    endpoints. Patch-panel trunks (rear-to-rear inter-rack cables) and every other mid-path
    segment carry OTHER circuits and are permanent infrastructure — they must survive, and
    the warning modal must say precisely which segment dies and that the rest stays.
    """

    def _panel_path(self, name):
        """csp --c1-- FrontPort | RearPort --c2 (trunk-ish)-- ConsolePort@end."""
        from dcim.models import FrontPort, PortMapping, RearPort

        acs, (csp,), _ = make_serial_device(f"acs-{name}", csp_names=["ttyS1"])
        panel = make_device(f"panel-{name}")
        rp = RearPort.objects.create(device=panel, name="R1", type="8p8c", positions=1)
        fp = FrontPort.objects.create(device=panel, name="F1", type="8p8c", positions=1)
        PortMapping.objects.create(
            device=panel, front_port=fp, rear_port=rp, front_port_position=1, rear_port_position=1
        )
        end, _, (cp,) = make_serial_device(f"end-{name}", cp_names=["console"])
        c1 = cable_together(csp, fp)
        c2 = cable_together(rp, cp)
        return acs, csp, c1, c2

    def test_force_repoint_deletes_only_the_endpoint_segment(self):
        from dcim.models import Cable

        acs, csp, c1, c2 = self._panel_path("midkeep")
        _t, _, (target_cp,) = make_serial_device("midkeep-target", cp_names=["console"])

        link = _serial_link(csp, target_cp)
        result = _sync_view().handle_serial_cable_creation(link, {"device_id": acs.id}, force=True)

        assert result["status"] == "overwritten"
        assert not Cable.objects.filter(pk=c1.pk).exists()  # endpoint segment gone
        assert Cable.objects.filter(pk=c2.pk).exists()  # the trunk-side segment SURVIVES
        csp.refresh_from_db()
        target_cp.refresh_from_db()
        assert csp.cable_id == target_cp.cable_id

    def test_conflict_names_exactly_the_segments_to_remove(self):
        acs, csp, c1, c2 = self._panel_path("midname")
        _t, _, (target_cp,) = make_serial_device("midname-target", cp_names=["console"])

        result = _sync_view().handle_serial_cable_creation(_serial_link(csp, target_cp), {"device_id": acs.id})

        assert result["status"] == "conflict"
        assert result["removed_cables"] == [f"#{c1.pk}"]  # only the endpoint segment, never c2

    def test_modal_marks_deleted_segment_and_keeps_the_rest(self):
        """E2E: the warning modal highlights the doomed endpoint segment and labels the rest of
        the path as staying — it must NOT claim panel segments get deleted."""
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, csp, c1, c2 = self._panel_path("midmodal")
        _t, _, (target_cp,) = make_serial_device("midmodal-target", cp_names=["console"])

        link = _serial_link(csp, target_cp)
        link["local_port"] = "ttyS1"
        link["_source"] = "serial"
        link["device_id"] = acs.id
        key_view = object.__new__(SyncCablesView)
        cache.set(key_view.get_cache_key(acs, "links", "default"), {"links": [link]}, timeout=300)

        user = get_user_model().objects.create_superuser("midmodal-admin", "midmodal@example.com", "pw")
        client = Client()
        client.force_login(user)
        resp = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk]),
            data={"select": link["local_port_id"], "server_key": "default"},
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'id="cable-force-submit"' in content
        # The doomed endpoint segment is explicitly marked deleted...
        assert content.count(">deleted<") == 1
        assert f"#{c1.pk}" in content
        # ...the rest of the path is explicitly marked as staying...
        assert "stays" in content
        assert f"#{c2.pk}" in content
        # ...and the old scary claim about deleting panel segments is gone.
        assert "including any" not in content


# ---------------------------------------------------------------------------
# Overwrite requires the delete permission
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestOverwriteRequiresDeletePermission:
    """The sync view's blanket gate covers add/change Cable, but the overwrite paths DELETE
    existing cables — that must additionally require dcim.delete_cable, checked precisely on
    the destructive branch so create-only syncs keep working for add/change users.
    """

    def _sync_view_with_real_user(self, *actions):
        """Build a sync view whose request user holds a REAL NetBox ObjectPermission for Cable
        with the given actions — NetBox's ObjectPermissionBackend ignores Django's
        user_permissions m2m, so has_perm() only honors ObjectPermission assignments."""
        from core.models import ObjectType
        from dcim.models import Cable
        from django.contrib.auth import get_user_model

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView
        from users.models import ObjectPermission

        user = get_user_model().objects.create_user(f"perm-user-{'-'.join(actions) or 'none'}")
        if actions:
            op = ObjectPermission.objects.create(name=f"cable-{'-'.join(actions)}", actions=list(actions))
            op.object_types.add(ObjectType.objects.get_for_model(Cable))
            op.users.add(user)
        user = get_user_model().objects.get(pk=user.pk)  # reload to reset the perm cache

        sync = object.__new__(SyncCablesView)
        sync.request = _mock_request()
        sync.request.user = user
        sync._post_server_key = "default"
        return sync

    def test_overwrite_without_delete_perm_is_denied_and_cable_survives(self):
        from dcim.models import Cable

        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        acs, (csp,), _ = make_serial_device("permdel", csp_names=["ttyS1"])
        _r, _, (cp_a, cp_b) = make_serial_device("permdel-r", cp_names=["console-A", "console-B"])
        old = cable_together(csp, cp_a)
        old.tags.add(get_librenms_cable_tag())  # plugin-owned: would be silently overwritten

        sync = self._sync_view_with_real_user("add", "change")  # no delete
        result = sync.handle_serial_cable_creation(_serial_link(csp, cp_b), {"device_id": acs.id})

        assert result["status"] == "denied"
        assert Cable.objects.filter(pk=old.pk).exists()  # nothing deleted
        csp.refresh_from_db()
        assert csp.cable_id == old.pk

    def test_overwrite_with_delete_perm_proceeds(self):
        from dcim.models import Cable

        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        acs, (csp,), _ = make_serial_device("permdel2", csp_names=["ttyS1"])
        _r, _, (cp_a, cp_b) = make_serial_device("permdel2-r", cp_names=["console-A", "console-B"])
        old = cable_together(csp, cp_a)
        old.tags.add(get_librenms_cable_tag())

        sync = self._sync_view_with_real_user("add", "change", "delete")
        result = sync.handle_serial_cable_creation(_serial_link(csp, cp_b), {"device_id": acs.id})

        assert result["status"] == "overwritten"
        assert not Cable.objects.filter(pk=old.pk).exists()
