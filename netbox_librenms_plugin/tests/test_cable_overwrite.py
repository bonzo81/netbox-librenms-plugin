"""
Tests for LibreNMS cable-sync overwrite protection.

Covers, against REAL Cable / Tag / ConsoleServerPort / ConsolePort / FrontPort / RearPort rows:
  - classify_cable_action(): the decision (create / noop / tag_only / needs_force) that gates
    whether a sync may touch an existing cable.
  - render_cable_trace(): the full end-to-end path (through patch panels) of a cable that would be
    overwritten, for display in the force-confirm modal.
  - handle_cable_creation(): the view-level behavior for a conflict and an exact confirmed
    overwrite.

Every destructive replacement requires confirmation of the exact current cable topology. A
purely additive change (adding the librenms tag without changing terminations) does not require
force.
"""

import pytest
from django.core.exceptions import PermissionDenied

from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    configured_server_key,
    make_device,
    make_interface,
    make_patch_panel,
    make_serial_device,
    make_serial_row,
    make_superuser,
)

SERVER_KEY = configured_server_key()


def _sync_view(server_key=SERVER_KEY):
    from uuid import uuid4

    from django.contrib.auth import get_user_model
    from django.test import RequestFactory

    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    sync = object.__new__(SyncCablesView)
    sync.request = RequestFactory().post("/")
    sync.request.session = {}
    from django.contrib.messages.storage.fallback import FallbackStorage

    sync.request._messages = FallbackStorage(sync.request)
    sync.request.user = get_user_model().objects.create_superuser(
        f"cable-overwrite-{uuid4().hex}",
        password="pw",
    )
    sync._post_server_key = server_key
    return sync


def _serial_link(csp, cp):
    row_id = f"serial:{csp.pk}"
    return {
        "local_port": csp.name,
        "local_port_id": row_id,
        "row_id": row_id,
        "_source": "serial",
        "netbox_local_interface_id": csp.pk,
        "netbox_remote_interface_id": cp.pk,
    }


def _confirmed_intent(local_term, remote_term, *cables):
    """Return the production confirmation token for current cables and endpoints."""
    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    state = SyncCablesView._cable_state_token({cable.pk: cable for cable in cables})
    return SyncCablesView._cable_intent_token(state, local_term, remote_term)


def _intent_from_modal(response, row_id):
    """Return the opaque endpoint-bound intent emitted by the real modal."""
    import re

    match = re.search(
        rf'name="expected_cable_intent_{re.escape(str(row_id))}" value="([^"]+)"',
        response.content.decode(),
    )
    assert match is not None
    return match.group(1)


def _rendered_sync_data(client, device, row_id, server_key=SERVER_KEY):
    """Return the endpoint-bound fields emitted by the real cable table."""
    from django.urls import reverse

    rendered = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"tab": "cables", "server_key": server_key},
    )
    assert rendered.status_code == 200
    record = next(
        row.record for row in rendered.context["cable_sync"]["table"].rows if str(row.record["row_id"]) == str(row_id)
    )
    return {
        "select": row_id,
        "server_key": server_key,
        f"expected_local_id_{row_id}": record["netbox_local_interface_id"],
        f"expected_local_device_id_{row_id}": record["netbox_local_device_id"],
        f"expected_remote_id_{row_id}": record["netbox_remote_interface_id"],
        f"expected_remote_device_id_{row_id}": record["netbox_remote_device_id"],
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
class TestCableStateTokenColumns:
    """The topology fingerprint may only name columns this NetBox's CableTermination actually has."""

    # CableTermination on NetBox 4.4: no `connector`, no `positions`. Naming either raises
    # FieldError and takes down every cable sync, and Django reports only the first one.
    NETBOX_44_COLUMNS = (
        ("cable_id", "cable_end", "pk"),
        ("cable_id", "cable_end", "termination_type_id", "termination_id"),
    )
    ADDED_AFTER_44 = ("connector", "positions")

    @staticmethod
    def _cabled_pair(name):
        _acs, csps, _ = make_serial_device(f"tok-acs-{name}", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device(f"tok-r-{name}", cp_names=["console-A"])
        return cable_together(csps[0], cps[0])

    def _as_netbox_44(self):
        from unittest.mock import patch

        return patch(
            "netbox_librenms_plugin.views.sync.cables._termination_topology_columns",
            return_value=self.NETBOX_44_COLUMNS,
        )

    def test_columns_added_after_44_are_dropped_when_the_model_lacks_them(self):
        """The fingerprint query must name nothing the running NetBox's model does not have."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        cable = self._cabled_pair("netbox-44")

        with self._as_netbox_44(), CaptureQueriesContext(connection) as queries:
            token = SyncCablesView._cable_state_token({cable.pk: cable})

        assert token
        for column in self.ADDED_AFTER_44:
            assert not any(column in query["sql"] for query in queries), (
                f"the fingerprint named {column}, which NetBox 4.4 has no column for"
            )

    def test_a_cable_still_syncs_in_the_netbox_44_shape(self):
        """The whole sync, not just the fingerprint: it is one step of a longer flow."""
        from dcim.models import Cable

        acs, csps, _ = make_serial_device("tok-e2e-acs", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device("tok-e2e-r", cp_names=["console-A"])

        with self._as_netbox_44():
            result = _sync_view().handle_cable_creation(
                _serial_link(csps[0], cps[0]),
                {"device_id": acs.id},
            )

        assert result["status"] == "valid", result
        csps[0].refresh_from_db()
        assert csps[0].cable_id is not None
        assert Cable.objects.filter(pk=csps[0].cable_id).exists()

    def test_every_column_this_netbox_has_is_fingerprinted(self):
        """The other direction: a column the model does have must not be dropped everywhere."""
        from dcim.models import CableTermination
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        present = {field.name for field in CableTermination._meta.get_fields()}
        expected = [column for column in self.ADDED_AFTER_44 if column in present]
        if not expected:
            pytest.skip("this NetBox has none of the post-4.4 termination columns")

        cable = self._cabled_pair("current-netbox")

        with CaptureQueriesContext(connection) as queries:
            token = SyncCablesView._cable_state_token({cable.pk: cable})

        assert token
        for column in expected:
            assert any(column in query["sql"] for query in queries), f"{column} is missing from the fingerprint"


@pytest.mark.django_db
class TestClassifyCableAction:
    """The decision that controls how sync may touch an existing cable."""

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

    def test_repoint_over_librenms_only_cable_still_requires_confirmation(self):
        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        _acs, csps, _ = make_serial_device("acs-c4", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device("r-c4", cp_names=["console-A", "console-B"])
        old = cable_together(csps[0], cps[0])  # csp -> console-A
        old.tags.add(get_librenms_cable_tag())
        decision = _classify(csps[0], cps[1])  # sync now wants csp -> console-B
        assert decision["action"] == "needs_force"
        assert old in decision["to_remove"]

    def test_existing_provenance_tag_with_custom_slug_remains_owned(self):
        from extras.models import Tag

        from netbox_librenms_plugin.models import LibreNMSSettings

        settings, _created = LibreNMSSettings.objects.get_or_create(pk=1)
        settings.cable_sync_tag = "Managed by LibreNMS"
        settings.save()
        provenance_tag = Tag.objects.create(
            name="Managed by LibreNMS",
            slug="librenms-provenance",
            color="00ff00",
        )
        _acs, csps, _ = make_serial_device("acs-custom-slug", csp_names=["ttyS1"])
        _remote, _, cps = make_serial_device("remote-custom-slug", cp_names=["console-A", "console-B"])
        old = cable_together(csps[0], cps[0])
        old.tags.add(provenance_tag)

        decision = _classify(csps[0], cps[0])

        assert decision["action"] == "noop"

    def test_provenance_tag_uses_an_available_slug_when_default_is_taken(self):
        from extras.models import Tag

        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        settings, _created = LibreNMSSettings.objects.get_or_create(pk=1)
        settings.cable_sync_tag = "Managed by LibreNMS"
        settings.save()
        existing = Tag.objects.create(
            name="Different owner",
            slug="managed-by-librenms",
            color="ff0000",
        )

        provenance_tag = get_librenms_cable_tag()

        assert provenance_tag.name == "Managed by LibreNMS"
        assert provenance_tag.slug != existing.slug

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
        assert decision["action"] == "needs_force"
        assert remote_cable in decision["to_remove"]

    def test_same_side_terminations_on_one_breakout_are_not_a_direct_match(self):
        """Sharing one Cable is not proof that two terminations are connected."""
        from dcim.models import Cable

        _local, csps, _ = make_serial_device("acs-breakout-classify", csp_names=["ttyS1", "ttyS2"])
        _remote, _, cps = make_serial_device("remote-breakout-classify", cp_names=["c1", "c2"])
        cable = Cable(a_terminations=csps, b_terminations=cps, status="connected")
        cable.save()

        decision = _classify(csps[0], csps[1])

        assert decision["action"] == "unsupported"
        assert decision["to_remove"] == []


# ---------------------------------------------------------------------------
# render_cable_trace
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestRenderCableTrace:
    """The full end-to-end path of a cable, spanning patch-panel front/rear pass-throughs."""

    def test_trace_through_patch_panel_reaches_end_device(self):
        from dcim.models import Cable

        from netbox_librenms_plugin.utils import render_cable_trace

        _acs, csps, _ = make_serial_device("acs-trace", csp_names=["ttyS1"])
        _panel_dev, fp, rp = make_patch_panel("panel-trace")
        _end, _, cps = make_serial_device("end-trace", cp_names=["console"])

        first = cable_together(csps[0], fp)  # ttyS1 -- F1
        cable_together(rp, cps[0])  # R1 -- console@end-trace

        # Re-fetch fresh so a_terminations + CablePath resolve (production passes freshly-loaded
        # cables from the termination FKs); the in-memory cable_together return is stale.
        first = Cable.objects.get(pk=first.pk)
        # Production always passes the request's user; without one the renderer fails closed.
        hops = render_cable_trace(first, user=make_superuser("cable-trace-user"))
        flat = " ".join(str(hop) for hop in hops)
        # The trace must run all the way through the panel to the end device's console port.
        assert "end-trace" in flat
        assert "console" in flat
        assert "ttyS1" in flat
        # More than one segment (through the panel), i.e. not a single point-to-point hop.
        assert len(hops) >= 2

    def test_trace_without_a_user_labels_every_hop_restricted(self):
        """Fail closed: no identity means no labels, not unrestricted ones."""
        from dcim.models import Cable

        from netbox_librenms_plugin.utils import render_cable_trace

        _acs, csps, _ = make_serial_device("acs-nouser", csp_names=["ttyS1"])
        _panel_dev, fp, rp = make_patch_panel("panel-nouser")
        _end, _, cps = make_serial_device("end-nouser", cp_names=["console"])
        first = cable_together(csps[0], fp)
        cable_together(rp, cps[0])
        first = Cable.objects.get(pk=first.pk)

        hops = render_cable_trace(first)

        assert hops, "the trace itself must still resolve"
        flat = " ".join(str(hop) for hop in hops)
        assert "Restricted" in flat
        assert "end-nouser" not in flat
        assert "ttyS1" not in flat


# ---------------------------------------------------------------------------
# view-level overwrite behaviour (force gate)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSerialCableOverwriteBehaviour:
    """handle_cable_creation honours the overwrite gate and the force flag end-to-end."""

    def _setup(self, name):
        acs, csps, _ = make_serial_device(f"acs-{name}", csp_names=["ttyS1"])
        _r, _, cps = make_serial_device(f"r-{name}", cp_names=["console-A", "console-B"])
        return acs, csps[0], cps[0], cps[1]

    def test_repoint_over_librenms_cable_requires_confirmation(self):
        from dcim.models import Cable
        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        acs, csp, cp_a, cp_b = self._setup("ovr")
        old = cable_together(csp, cp_a)
        old.tags.add(get_librenms_cable_tag())

        sync = _sync_view()
        result = sync.handle_cable_creation(_serial_link(csp, cp_b), {"device_id": acs.id})

        assert result["status"] == "conflict"
        assert Cable.objects.filter(pk=old.pk).exists()
        csp.refresh_from_db()
        assert csp.cable_id == old.pk

    def test_max_length_setting_still_creates_a_keyed_cable_description(self):
        from dcim.models import Cable

        from netbox_librenms_plugin.models import LibreNMSSettings

        acs, csp, cp_a, _cp_b = self._setup("long-description")
        settings, _created = LibreNMSSettings.objects.get_or_create(pk=1)
        settings.cable_sync_description = "x" * 200
        settings.save()

        result = _sync_view("secondary").handle_cable_creation(
            _serial_link(csp, cp_a),
            {"device_id": acs.id},
        )

        assert result["status"] == "valid"
        cable = Cable.objects.get()
        assert len(cable.description) == Cable._meta.get_field("description").max_length
        assert cable.description.endswith(" (secondary)")

    def test_long_server_key_is_bounded_to_the_cable_description_field(self):
        from dcim.models import Cable

        acs, csp, cp_a, _cp_b = self._setup("long-server-key")
        server_key = "placeholder-server-" * 20

        result = _sync_view(server_key).handle_cable_creation(
            _serial_link(csp, cp_a),
            {"device_id": acs.id},
        )

        assert result["status"] == "valid"
        cable = Cable.objects.get()
        assert len(cable.description) == Cable._meta.get_field("description").max_length

    def test_each_cable_action_reuses_its_locked_settings_and_request_tag(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        get_librenms_cable_tag()
        sync = _sync_view()
        rows = []
        for index in range(3):
            acs, csp, cp, _unused = self._setup(f"query-{index}")
            rows.append((acs, csp, cp))

        with CaptureQueriesContext(connection) as captured:
            results = [
                sync.handle_cable_creation(_serial_link(csp, cp), {"device_id": acs.id}) for acs, csp, cp in rows
            ]

        assert [result["status"] for result in results] == ["valid", "valid", "valid"]
        settings_selects = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "netbox_librenms_plugin_librenmssettings" in query["sql"]
        ]
        provenance_tag_selects = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and 'FROM "extras_tag"' in query["sql"]
            and '"extras_tag"."name" =' in query["sql"]
        ]
        assert len(settings_selects) == len(rows)
        assert len(provenance_tag_selects) == 1

    def test_tagging_failure_rolls_back_the_cable(self):
        """A provenance-tagging failure must not commit an untagged cable while reporting failure.

        An untagged cable isn't recognized as plugin-owned by classify_cable_action, so a
        half-persisted create both contradicts the error toast and turns the user's retry
        into a force-confirm conflict against their own cable.
        """
        from dcim.models import Cable
        from django.db import connection

        acs, csp, cp_a, _cp_b = self._setup("tagfail")
        sync = _sync_view()

        def fail_tag_insert(execute, sql, params, many, context):
            if sql.lstrip().upper().startswith("INSERT") and '"extras_taggeditem"' in sql:
                raise RuntimeError("tag insert failed")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(fail_tag_insert):
            result = sync.handle_cable_creation(_serial_link(csp, cp_a), {"device_id": acs.id})

        assert result["status"] == "failed"  # an operational failure, not missing link data
        assert Cable.objects.count() == 0  # the cable and its failed tag stamp roll back together

    @pytest.mark.parametrize(
        ("error_type", "expected_message"),
        [
            (RuntimeError, "Failed to sync cables for interfaces"),
            (PermissionDenied, "You do not have permission"),
        ],
        ids=("operational-failure", "permission-denial"),
    )
    def test_existing_cable_tag_signal_failure_is_reported_without_mutation(self, error_type, expected_message):
        """The real request classifies a model-signal failure and leaves the cable unchanged."""
        from django.contrib.auth import get_user_model
        from django.contrib.messages import get_messages
        from django.core.cache import cache
        from django.db.models.signals import m2m_changed
        from django.test import Client
        from django.urls import reverse
        from extras.models import TaggedItem

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local_device = make_device(f"signal-local-{error_type.__name__}")
        local = make_interface(local_device, "Ethernet1")
        remote_device = make_device(f"signal-remote-{error_type.__name__}")
        remote = make_interface(remote_device, "Ethernet2")
        cable = cable_together(local, remote)
        row = {
            "_source": "main",
            "local_port_id": 101,
            "local_port": local.name,
            "remote_device": remote_device.name,
            "remote_port": remote.name,
            "remote_port_id": 202,
        }
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(local_device, "links", SERVER_KEY),
            {"links": [row], "snapshot_token": f"signal-{error_type.__name__}"},
            timeout=300,
        )
        user = get_user_model().objects.create_superuser(
            f"signal-user-{error_type.__name__}",
            password="pw",
        )
        client = Client()
        client.force_login(user)
        post_data = _rendered_sync_data(client, local_device, row["local_port_id"])

        def reject_tag_update(sender, instance, action, **kwargs):
            if action == "pre_add" and instance.pk == cable.pk:
                raise error_type("tag update rejected")

        dispatch_uid = f"cable-sync-tag-failure-{error_type.__name__}"
        m2m_changed.connect(reject_tag_update, sender=TaggedItem, dispatch_uid=dispatch_uid)
        try:
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local_device.pk]),
                post_data,
            )
        finally:
            m2m_changed.disconnect(sender=TaggedItem, dispatch_uid=dispatch_uid)

        assert response.status_code == 302
        assert any(expected_message in str(message) for message in get_messages(response.wsgi_request))
        cable.refresh_from_db()
        assert cable.tags.count() == 0

    def test_conflict_on_foreign_cable_without_force_leaves_db_untouched(self):
        from dcim.models import Cable

        acs, csp, cp_a, cp_b = self._setup("conf")
        old = cable_together(csp, cp_a)
        old.tags.add(_make_tag("dcim-modeled"))

        sync = _sync_view()
        result = sync.handle_cable_creation(_serial_link(csp, cp_b), {"device_id": acs.id})

        assert result["status"] == "conflict"
        assert Cable.objects.filter(pk=old.pk).exists()  # foreign cable survives
        csp.refresh_from_db()
        assert csp.cable_id == old.pk  # still cabled to console-A
        assert result.get("row_id") == _serial_link(csp, cp_b)["row_id"]
        assert result.get("trace")  # trace carried for the modal

    def test_force_overwrites_foreign_cable(self):
        from dcim.models import Cable

        acs, csp, cp_a, cp_b = self._setup("force")
        old = cable_together(csp, cp_a)
        old.tags.add(_make_tag("dcim-modeled"))

        sync = _sync_view()
        link = _serial_link(csp, cp_b)
        link["expected_cable_intent"] = _confirmed_intent(csp, cp_b, old)
        result = sync.handle_cable_creation(link, {"device_id": acs.id}, force=True)

        assert result["status"] == "overwritten"
        assert not Cable.objects.filter(pk=old.pk).exists()
        csp.refresh_from_db()
        cp_b.refresh_from_db()
        assert csp.cable_id == cp_b.cable_id

    def test_force_rejects_a_multi_termination_cable(self):
        """Force must not disconnect unrelated terminations on one cable."""
        from dcim.models import Cable, CableTermination

        acs, csps, _ = make_serial_device("acs-force-breakout", csp_names=["ttyS1", "ttyS2"])
        _old_remote, _, old_ports = make_serial_device(
            "remote-force-breakout-old",
            cp_names=["console1", "console2"],
        )
        _target_device, _, (target_port,) = make_serial_device(
            "remote-force-breakout-target",
            cp_names=["console"],
        )
        old = Cable(a_terminations=csps, b_terminations=old_ports, status="connected")
        old.save()
        link = _serial_link(csps[0], target_port)
        link["expected_cable_intent"] = _confirmed_intent(csps[0], target_port, old)

        result = _sync_view().handle_cable_creation(link, {"device_id": acs.pk}, force=True)

        assert result["status"] == "unsupported"
        assert Cable.objects.filter(pk=old.pk).exists()
        assert CableTermination.objects.filter(cable=old).count() == 4
        target_port.refresh_from_db()
        assert target_port.cable_id is None

    def test_same_connection_untagged_gets_tagged_not_recreated(self):
        acs, csp, cp_a, _cp_b = self._setup("tag")
        cable = cable_together(csp, cp_a)  # untagged, already the desired connection

        sync = _sync_view()
        result = sync.handle_cable_creation(_serial_link(csp, cp_a), {"device_id": acs.id})

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
        _old_router, _, (cp_a,) = make_serial_device(f"router-old-{name}", cp_names=["console-A"])
        _target_router, _, (cp_b,) = make_serial_device(f"router-{name}", cp_names=["console-B"])
        csp = csps[0]

        # A foreign-tagged cable occupies the CSP -> the sync to console-B must conflict.
        old = cable_together(csp, cp_a)
        old.tags.add(_make_tag(f"dcim-modeled-{name}"))

        # Seed the raw LibreNMS snapshot targeting console-B on another device.
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
        cache.set(key_view.get_cache_key(acs, "links", SERVER_KEY), {"links": [link]}, timeout=300)

        user = get_user_model().objects.create_superuser(f"modal-admin-{name}", f"{name}@example.com", "pw")
        client = Client()
        client.force_login(user)
        url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk])
        return client, url, link, old, csp, cp_b

    def test_conflict_returns_oob_modal_and_leaves_db_untouched(self):
        from dcim.models import Cable

        client, url, link, old, csp, cp_b = self._seed("conf")
        post_data = _rendered_sync_data(client, csp.device, link["local_port_id"])
        resp = client.post(url, data=post_data, HTTP_HX_REQUEST="true")

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
        expected_intent = _confirmed_intent(csp, cp_b, old)
        assert f'name="expected_cable_intent_{link["local_port_id"]}" value="{expected_intent}"' in content
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
        post_data = _rendered_sync_data(client, csp.device, link["local_port_id"])
        initial = client.post(
            url,
            data=post_data,
            HTTP_HX_REQUEST="true",
        )
        expected_intent = _intent_from_modal(initial, link["local_port_id"])
        resp = client.post(
            url,
            data={
                **post_data,
                "force": "on",
                f"expected_cable_intent_{link['local_port_id']}": expected_intent,
            },
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

    def test_force_cannot_delete_a_multi_termination_cable(self):
        """One row must not disconnect unrelated lanes on a breakout cable."""
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse
        from dcim.models import Cable, CableTermination

        from netbox_librenms_plugin.utils import cable_manual_pick_cache_key
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, csps, _ = make_serial_device(
            "acs-multi-termination",
            csp_names=["ttyS1", "ttyS2"],
        )
        _old_remote, _, old_ports = make_serial_device(
            "remote-multi-termination-old",
            cp_names=["console1", "console2"],
        )
        target_device, _, (target_port,) = make_serial_device(
            "remote-multi-termination-target",
            cp_names=["console"],
        )
        old = Cable(a_terminations=csps, b_terminations=old_ports, status="connected")
        old.save()
        acs.custom_field_data["librenms_id"] = {SERVER_KEY: 13}
        acs.save(update_fields=["custom_field_data"])
        row = make_serial_row(csps[0], target_device.name, acs)
        snapshot = {"links": [row], "snapshot_token": "multi-termination"}
        cache_key = object.__new__(SyncCablesView).get_cache_key(acs, "links", SERVER_KEY)
        cache.set(
            cache_key,
            snapshot,
            timeout=300,
        )
        user = get_user_model().objects.create_superuser("multi-termination-admin", "", "pw")
        cache.set(
            cable_manual_pick_cache_key(cache_key, snapshot["snapshot_token"], user.pk, row["local_port_id"]),
            {"manual_remote_id": target_port.pk},
            timeout=300,
        )
        client = Client()
        client.force_login(user)
        row_id = row["local_port_id"]
        cable_state = SyncCablesView._cable_state_token({old.pk: old})
        intent = SyncCablesView._cable_intent_token(cable_state, csps[0], target_port)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk]),
            {
                "sync_one": row_id,
                "server_key": SERVER_KEY,
                "force": "on",
                f"expected_local_id_{row_id}": csps[0].pk,
                f"expected_local_device_id_{row_id}": acs.pk,
                f"expected_remote_id_{row_id}": target_port.pk,
                f"expected_remote_device_id_{row_id}": target_device.pk,
                f"expected_cable_intent_{row_id}": intent,
            },
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert "Multi-termination cables cannot be changed by cable sync" in response.content.decode()
        assert Cable.objects.filter(pk=old.pk).exists()
        assert Cable.objects.count() == 1
        assert CableTermination.objects.filter(cable=old).count() == 4
        for termination in [*csps, *old_ports]:
            termination.refresh_from_db()
            assert termination.cable_id == old.pk
        target_port.refresh_from_db()
        assert target_port.cable_id is None

    def test_force_confirmation_rejects_a_replacement_cable(self):
        from dcim.models import Cable

        client, url, link, old, csp, _target_cp = self._seed("stale")
        post_data = _rendered_sync_data(client, csp.device, link["local_port_id"])
        initial = client.post(
            url,
            data=post_data,
            HTTP_HX_REQUEST="true",
        )
        expected_intent = _intent_from_modal(initial, link["local_port_id"])

        old.delete()
        _replacement_device, _, (replacement_remote,) = make_serial_device(
            "router-stale-replacement",
            cp_names=["console"],
        )
        replacement = cable_together(csp, replacement_remote)
        replacement.tags.add(_make_tag("replacement-owner"))

        forced = client.post(
            url,
            data={
                **post_data,
                "force": "on",
                f"expected_cable_intent_{link['local_port_id']}": expected_intent,
            },
            HTTP_HX_REQUEST="true",
        )

        assert forced.status_code == 200
        assert "cable state or target changed after confirmation" in forced.content.decode()
        assert Cable.objects.filter(pk=replacement.pk).exists()
        csp.refresh_from_db()
        assert csp.cable_id == replacement.pk

    def test_force_confirmation_rejects_changed_terminations_on_same_cable(self):
        """The same Cable PK must not hide a different termination topology."""
        from dcim.models import Cable, CableTermination

        client, url, link, old, csp, target_cp = self._seed("stale-topology")
        post_data = _rendered_sync_data(client, csp.device, link["local_port_id"])
        initial = client.post(
            url,
            data=post_data,
            HTTP_HX_REQUEST="true",
        )
        expected_intent = _intent_from_modal(initial, link["local_port_id"])

        changed_device, _, (changed_cp,) = make_serial_device(
            "router-stale-topology-changed",
            cp_names=["console"],
        )
        CableTermination.objects.filter(cable=old, cable_end="B").update(
            termination_id=changed_cp.pk,
            _device_id=changed_device.pk,
            _site_id=changed_device.site_id,
            _location_id=changed_device.location_id,
            _rack_id=changed_device.rack_id,
        )

        forced = client.post(
            url,
            data={
                **post_data,
                "force": "on",
                f"expected_cable_intent_{link['local_port_id']}": expected_intent,
            },
            HTTP_HX_REQUEST="true",
        )

        assert forced.status_code == 200
        assert "cable state or target changed after confirmation" in forced.content.decode()
        assert Cable.objects.filter(pk=old.pk).exists()
        csp.refresh_from_db()
        target_cp.refresh_from_db()
        assert csp.cable_id == old.pk
        assert CableTermination.objects.filter(
            cable=old,
            cable_end="B",
            termination_id=changed_cp.pk,
        ).exists()
        assert target_cp.cable_id is None

    def test_force_confirmation_rejects_a_changed_manual_target(self):
        """Confirmation covers the desired endpoint as well as the occupying cable."""
        from dcim.models import Cable
        from django.urls import reverse

        client, url, link, old, csp, _initial_target = self._seed("stale-target")
        post_data = _rendered_sync_data(client, csp.device, link["local_port_id"])
        first = client.post(
            url,
            data=post_data,
            HTTP_HX_REQUEST="true",
        )
        expected_intent = _intent_from_modal(first, link["local_port_id"])

        _other_device, _, (other_target,) = make_serial_device(
            "router-stale-target-other",
            cp_names=["console"],
        )
        repicked = client.post(
            reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[csp.device_id]),
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": other_target.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        assert repicked.status_code == 200

        forced = client.post(
            url,
            data={
                **post_data,
                "force": "on",
                f"expected_cable_intent_{link['local_port_id']}": expected_intent,
            },
            HTTP_HX_REQUEST="true",
        )

        assert forced.status_code == 200
        assert "cable state or target changed after confirmation" in forced.content.decode()
        assert Cable.objects.filter(pk=old.pk).exists()
        csp.refresh_from_db()
        other_target.refresh_from_db()
        assert csp.cable_id == old.pk
        assert other_target.cable_id is None

    def test_force_confirmation_is_stale_when_the_conflict_disappears(self):
        """A force form must not become an unconfirmed create after its cable is removed."""
        from django.urls import reverse

        client, url, link, old, csp, _initial_target = self._seed("stale-create")
        post_data = _rendered_sync_data(client, csp.device, link["local_port_id"])
        first = client.post(
            url,
            data=post_data,
            HTTP_HX_REQUEST="true",
        )
        expected_intent = _intent_from_modal(first, link["local_port_id"])

        _other_device, _, (other_target,) = make_serial_device(
            "router-stale-create-other",
            cp_names=["console"],
        )
        repicked = client.post(
            reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[csp.device_id]),
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": other_target.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        assert repicked.status_code == 200
        old.delete()

        forced = client.post(
            url,
            data={
                **post_data,
                "force": "on",
                f"expected_cable_intent_{link['local_port_id']}": expected_intent,
            },
            HTTP_HX_REQUEST="true",
        )

        assert forced.status_code == 200
        assert "cable state or target changed after confirmation" in forced.content.decode()
        csp.refresh_from_db()
        other_target.refresh_from_db()
        assert csp.cable_id is None
        assert other_target.cable_id is None


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
        acs, (csp,), _ = make_serial_device(f"acs-{name}", csp_names=["ttyS1"])
        _panel_dev, fp, rp = make_patch_panel(f"panel-{name}")
        end, _, (cp,) = make_serial_device(f"end-{name}", cp_names=["console"])
        c1 = cable_together(csp, fp)
        c2 = cable_together(rp, cp)
        return acs, csp, c1, c2

    def test_force_repoint_deletes_only_the_endpoint_segment(self):
        from dcim.models import Cable

        acs, csp, c1, c2 = self._panel_path("midkeep")
        _t, _, (target_cp,) = make_serial_device("midkeep-target", cp_names=["console"])

        link = _serial_link(csp, target_cp)
        link["expected_cable_intent"] = _confirmed_intent(csp, target_cp, c1)
        result = _sync_view().handle_cable_creation(link, {"device_id": acs.id}, force=True)

        assert result["status"] == "overwritten"
        assert not Cable.objects.filter(pk=c1.pk).exists()  # endpoint segment gone
        assert Cable.objects.filter(pk=c2.pk).exists()  # the trunk-side segment SURVIVES
        csp.refresh_from_db()
        target_cp.refresh_from_db()
        assert csp.cable_id == target_cp.cable_id

    def test_conflict_names_exactly_the_segments_to_remove(self):
        acs, csp, c1, c2 = self._panel_path("midname")
        _t, _, (target_cp,) = make_serial_device("midname-target", cp_names=["console"])

        result = _sync_view().handle_cable_creation(_serial_link(csp, target_cp), {"device_id": acs.id})

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
        link["remote_device"] = target_cp.device.name
        link["is_configured"] = True
        key_view = object.__new__(SyncCablesView)
        cache.set(key_view.get_cache_key(acs, "links", SERVER_KEY), {"links": [link]}, timeout=300)

        user = get_user_model().objects.create_superuser("midmodal-admin", "midmodal@example.com", "pw")
        client = Client()
        client.force_login(user)
        post_data = _rendered_sync_data(client, acs, link["local_port_id"])
        resp = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk]),
            data=post_data,
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

    Acting on an EXISTING cable also needs view scope for it: the cable table reports current
    cable state through view-scoped queries, so a row whose cable the user cannot see must not
    be adopted, replaced or described. Each test below narrows one grant and leaves the rest
    open, so the denial it asserts can only come from the gate it names.
    """

    def _sync_view_with_real_user(self, *actions, constraints=None, constrained_actions=None, device_ids=None):
        """Build a sync view whose request user holds a REAL NetBox ObjectPermission for Cable
        with the given actions — NetBox's ObjectPermissionBackend ignores Django's
        user_permissions m2m, so has_perm() only honors ObjectPermission assignments.

        *constraints* makes a grant a CONSTRAINED one: ``has_perm`` (asked without an instance)
        still passes, while ``restrict()`` narrows to the matching cables. It applies only to
        *constrained_actions* (default: every action), so a test can narrow the one gate it is
        about and leave the others wide open.
        """
        from core.models import ObjectType
        from dcim.models import Cable
        from django.contrib.auth import get_user_model

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView
        from users.models import ObjectPermission

        suffix = "-scoped" if constraints else ""
        from dcim.models import ConsolePort, ConsoleServerPort

        user = get_user_model().objects.create_user(f"perm-user-{'-'.join(actions) or 'none'}{suffix}")
        scoped = tuple(constrained_actions if constrained_actions is not None else (actions if constraints else ()))
        unscoped = tuple(action for action in actions if action not in scoped)
        for label, grant_actions, grant_constraints in (("open", unscoped, None), ("scoped", scoped, constraints)):
            if not grant_actions:
                continue
            op = ObjectPermission.objects.create(
                name=f"cable-{label}-{'-'.join(grant_actions)}{suffix}",
                actions=list(grant_actions),
                constraints=grant_constraints,
            )
            op.object_types.add(ObjectType.objects.get_for_model(Cable))
            op.users.add(user)
        # The terminations are resolved through a change-restricted queryset. These tests vary
        # the cable grant, so keep the port grant unconstrained.
        ports = ObjectPermission.objects.create(
            name=f"ports-change{suffix}-{'-'.join(actions) or 'none'}", actions=["view", "change"]
        )
        ports.object_types.add(ObjectType.objects.get_for_model(ConsoleServerPort))
        ports.object_types.add(ObjectType.objects.get_for_model(ConsolePort))
        ports.users.add(user)
        devices = ObjectPermission.objects.create(
            name=f"devices-view{suffix}-{'-'.join(actions) or 'none'}",
            actions=["view"],
            constraints={"pk__in": device_ids} if device_ids is not None else None,
        )
        from dcim.models import Device

        devices.object_types.add(ObjectType.objects.get_for_model(Device))
        devices.users.add(user)
        user = get_user_model().objects.get(pk=user.pk)  # reload to reset the perm cache

        sync = object.__new__(SyncCablesView)
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage

        sync.request = RequestFactory().post("/")
        sync.request.session = {}
        sync.request._messages = FallbackStorage(sync.request)
        sync.request.user = user
        sync._post_server_key = SERVER_KEY
        return sync

    def test_overwrite_without_delete_perm_is_denied_and_cable_survives(self):
        from dcim.models import Cable

        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        acs, (csp,), _ = make_serial_device("permdel", csp_names=["ttyS1"])
        _r, _, (cp_a, cp_b) = make_serial_device("permdel-r", cp_names=["console-A", "console-B"])
        old = cable_together(csp, cp_a)
        old.tags.add(get_librenms_cable_tag())

        sync = self._sync_view_with_real_user("view", "add", "change")  # no delete
        link = _serial_link(csp, cp_b)
        link["expected_cable_intent"] = _confirmed_intent(csp, cp_b, old)
        result = sync.handle_cable_creation(link, {"device_id": acs.id}, force=True)

        assert result["status"] == "denied"
        assert Cable.objects.filter(pk=old.pk).exists()  # nothing deleted
        csp.refresh_from_db()
        assert csp.cable_id == old.pk

    def test_tag_only_adoption_respects_the_concrete_cable_change_scope(self):
        acs, (csp,), _ = make_serial_device("permtag", csp_names=["ttyS1"])
        _remote, _, (cp,) = make_serial_device("permtag-r", cp_names=["console"])
        cable = cable_together(csp, cp)
        _other_acs, (other_csp,), _ = make_serial_device("permtag-other", csp_names=["ttyS2"])
        _other_remote, _, (other_cp,) = make_serial_device("permtag-other-r", cp_names=["console"])
        in_scope = cable_together(other_csp, other_cp)
        # View scope covers every cable, so only the narrowed CHANGE grant can deny this.
        sync = self._sync_view_with_real_user(
            "view",
            "add",
            "change",
            constraints={"pk": in_scope.pk},
            constrained_actions=("change",),
        )

        result = sync.handle_cable_creation(_serial_link(csp, cp), {"device_id": acs.pk})

        assert result["status"] == "denied"
        assert not cable.tags.exists()

    def test_tag_only_adoption_requires_view_scope_for_the_existing_cable(self):
        acs, (csp,), _ = make_serial_device("permtag-hidden", csp_names=["ttyS1"])
        _remote, _, (cp,) = make_serial_device("permtag-hidden-r", cp_names=["console"])
        cable = cable_together(csp, cp)
        _other_acs, (other_csp,), _ = make_serial_device("permtag-hidden-other", csp_names=["ttyS2"])
        _other_remote, _, (other_cp,) = make_serial_device("permtag-hidden-other-r", cp_names=["console"])
        visible = cable_together(other_csp, other_cp)
        # Change scope covers the cable; only the narrowed VIEW grant can deny this. Cable sync
        # never acts on a cable the user cannot see, however writable it is.
        sync = self._sync_view_with_real_user(
            "view",
            "add",
            "change",
            constraints={"pk": visible.pk},
            constrained_actions=("view",),
        )

        result = sync.handle_cable_creation(_serial_link(csp, cp), {"device_id": acs.pk})

        assert result["status"] == "denied"
        assert not cable.tags.exists()

    def test_conflict_without_view_cable_permission_is_denied(self):
        acs, (csp,), _ = make_serial_device("trace-no-view-local", csp_names=["ttyS1"])
        _current, _, (current_cp,) = make_serial_device(
            "trace-no-view-current",
            cp_names=["console"],
        )
        _target, _, (target_cp,) = make_serial_device("trace-no-view-target", cp_names=["console"])
        cable_together(csp, current_cp)
        sync = self._sync_view_with_real_user("add", "change")

        result = sync.handle_cable_creation(_serial_link(csp, target_cp), {"device_id": acs.pk})

        assert result["status"] == "denied"
        assert "trace" not in result

    def test_conflict_trace_redacts_topology_outside_view_scope(self):
        acs, (csp,), _ = make_serial_device("trace-scope-local", csp_names=["ttyS1"])
        _panel, front, rear = make_patch_panel("trace-scope-hidden-panel")
        _hidden_end, _, (hidden_cp,) = make_serial_device(
            "trace-scope-hidden-end",
            cp_names=["hidden-console"],
        )
        cable_together(csp, front)
        cable_together(rear, hidden_cp)
        _target, _, (target_cp,) = make_serial_device("trace-scope-target", cp_names=["console"])
        sync = self._sync_view_with_real_user(
            "view",
            "add",
            "change",
            device_ids=[acs.pk, target_cp.device_id],
        )

        result = sync.handle_cable_creation(_serial_link(csp, target_cp), {"device_id": acs.pk})

        rendered_trace = str(result["trace"])
        assert result["status"] == "conflict"
        assert "Restricted" in rendered_trace
        assert "trace-scope-hidden-panel" not in rendered_trace
        assert "trace-scope-hidden-end" not in rendered_trace
        assert "hidden-console" not in rendered_trace

    def test_overwrite_with_delete_perm_proceeds(self):
        from dcim.models import Cable

        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        acs, (csp,), _ = make_serial_device("permdel2", csp_names=["ttyS1"])
        _r, _, (cp_a, cp_b) = make_serial_device("permdel2-r", cp_names=["console-A", "console-B"])
        old = cable_together(csp, cp_a)
        old.tags.add(get_librenms_cable_tag())

        sync = self._sync_view_with_real_user("view", "add", "change", "delete")
        link = _serial_link(csp, cp_b)
        link["expected_cable_intent"] = _confirmed_intent(csp, cp_b, old)
        result = sync.handle_cable_creation(link, {"device_id": acs.id}, force=True)

        assert result["status"] == "overwritten"
        assert not Cable.objects.filter(pk=old.pk).exists()

    def test_overwrite_denied_when_the_delete_grant_excludes_the_doomed_cable(self):
        """A CONSTRAINED delete_cable grant clears has_perm (no instance is asked), so the doomed
        cable must be checked against the user's actual delete scope — otherwise the overwrite
        destroys a cable the user cannot see."""
        from dcim.models import Cable

        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        acs, (csp,), _ = make_serial_device("permdel3", csp_names=["ttyS1"])
        _r, _, (cp_a, cp_b) = make_serial_device("permdel3-r", cp_names=["console-A", "console-B"])
        old = cable_together(csp, cp_a)
        old.tags.add(get_librenms_cable_tag())
        # An unrelated cable is the ONLY one the grant covers.
        _other_acs, (other_csp,), _ = make_serial_device("permdel3-other", csp_names=["ttyS2"])
        _o, _, (other_cp,) = make_serial_device("permdel3-other-r", cp_names=["console-C"])
        in_scope = cable_together(other_csp, other_cp)

        # View/add/change cover every cable, so only the narrowed DELETE grant can deny this.
        sync = self._sync_view_with_real_user(
            "view",
            "add",
            "change",
            "delete",
            constraints={"pk": in_scope.pk},
            constrained_actions=("delete",),
        )
        link = _serial_link(csp, cp_b)
        link["expected_cable_intent"] = _confirmed_intent(csp, cp_b, old)
        result = sync.handle_cable_creation(link, {"device_id": acs.id}, force=True)

        assert result["status"] == "denied"
        assert Cable.objects.filter(pk=old.pk).exists()  # the out-of-scope cable survives
        csp.refresh_from_db()
        assert csp.cable_id == old.pk


@pytest.mark.django_db
class TestCableSyncLeastPrivilegeByRowType:
    def _client(self, name, termination_models):
        from core.models import ObjectType
        from dcim.models import Cable, Device
        from django.contrib.auth import get_user_model
        from django.test import Client
        from users.models import ObjectPermission

        from netbox_librenms_plugin.models import LibreNMSSettings

        user = get_user_model().objects.create_user(f"cable-row-perms-{name}")

        def grant(label, model, actions):
            permission = ObjectPermission.objects.create(name=f"{name}-{label}", actions=actions)
            permission.object_types.add(ObjectType.objects.get_for_model(model))
            permission.users.add(user)

        grant("plugin", LibreNMSSettings, ["view", "change"])
        grant("devices", Device, ["view"])
        grant("cables", Cable, ["add", "change"])
        for model in termination_models:
            grant(f"{model._meta.model_name}s", model, ["view", "change"])

        client = Client()
        client.force_login(user)
        return client

    @staticmethod
    def _cache_and_url(device, link):
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        cache.set(view.get_cache_key(device, "links", SERVER_KEY), {"links": [link]}, timeout=300)
        return reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[device.pk])

    def test_interface_only_user_does_not_need_console_port_permissions(self):
        from dcim.models import Cable, Interface

        local_device = make_device("least-interface-local")
        local = make_interface(local_device, "Ethernet1")
        remote_device = make_device("least-interface-remote")
        remote = make_interface(remote_device, "Ethernet2")
        link = {
            "local_port": local.name,
            "local_port_id": "101",
            "device_id": local_device.pk,
            "remote_device": remote_device.name,
            "remote_port": remote.name,
            "netbox_local_interface_id": local.pk,
            "netbox_remote_interface_id": remote.pk,
            "netbox_remote_device_id": remote_device.pk,
        }
        client = self._client("interface", [Interface])
        url = self._cache_and_url(local_device, link)
        post_data = _rendered_sync_data(client, local_device, link["local_port_id"])

        response = client.post(
            url,
            post_data,
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert Cable.objects.count() == 1

    def test_serial_only_user_does_not_need_interface_permissions(self):
        from dcim.models import Cable, ConsolePort, ConsoleServerPort

        local_device, (local,), _unused = make_serial_device("least-serial-local", csp_names=["ttyS1"])
        remote_device, _, (remote,) = make_serial_device("least-serial-remote", cp_names=["console"])
        link = {
            "_source": "serial",
            "local_port": local.name,
            "local_port_id": "serial:201",
            "device_id": local_device.pk,
            "remote_device": remote_device.name,
            "is_configured": True,
            "sensor_id": 201,
            "sensor_index_int": 1,
            "netbox_local_interface_id": local.pk,
            "netbox_remote_interface_id": remote.pk,
            "netbox_remote_device_id": remote_device.pk,
        }
        client = self._client("serial", [ConsoleServerPort, ConsolePort])
        url = self._cache_and_url(local_device, link)
        post_data = _rendered_sync_data(client, local_device, link["local_port_id"])

        response = client.post(
            url,
            post_data,
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert Cable.objects.count() == 1
