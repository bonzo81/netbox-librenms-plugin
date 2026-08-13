"""
Tests for the cable remote-end picker.

Remote-end matching is name-based (the serial label / LLDP port name), which is a matter of
luck — names often don't match. The picker lets the user choose the remote endpoint by hand:

- The pick is stored in a user- and row-scoped cache entry, separate from the shared LibreNMS
  snapshot. Enrichment resolves it in preference to label/name matching.
- The picker endpoint serves the modal (device search -> port list) and the POST that writes
  the pick into the cached row.
- Trust rule: a serial cable that carries the librenms tag is trusted over the label — the
  label is only a hint, so a tagged cable must NOT flip to "Cable Mismatch" against a
  wrong-name label (that would offer a silent re-point of a deliberately-placed cable).

All tests run against real Device / ConsoleServerPort / ConsolePort / Interface / Cable rows.
"""

import pytest
from django.core.cache.backends.locmem import LocMemCache

from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    configured_server_key,
    librenms_cable_tag as _librenms_tag,
    make_device,
    make_interface,
    make_patch_panel,
    make_serial_device,
    make_serial_row as _serial_row,
    make_virtual_chassis,
)
from netbox_librenms_plugin.tests.test_serial_cables_view import _make_view

SERVER_KEY = configured_server_key()


class CountingLocMemCache(LocMemCache):
    """Real local-memory cache backend with visible read volume for request tests."""

    get_count = 0

    def get(self, key, default=None, version=None):
        type(self).get_count += 1
        return super().get(key, default=default, version=version)


def _serial_refetch_get(link):
    """Return a real-shape LibreNMS HTTP fake that rebuilds one serial row."""
    import json

    import requests

    def external_get(url, *args, **kwargs):
        response = requests.models.Response()
        response.url = url
        if url.endswith("/links"):
            response.status_code = 404
            response._content = b'{"status":"error","message":"Device does not have any links"}'
        elif url.endswith("/ports"):
            response.status_code = 200
            response._content = b'{"status":"ok","ports":[]}'
        elif url.endswith("/resources/sensors"):
            response.status_code = 200
            response._content = json.dumps(
                {
                    "status": "ok",
                    "sensors": [
                        {
                            "sensor_id": link["sensor_id"],
                            "device_id": 13,
                            "sensor_type": "acsSerialPortTable",
                            "sensor_index": "acsSerialPortTableStatus.8",
                            "sensor_descr": f"{link['remote_device']} Status",
                        }
                    ],
                }
            ).encode()
        else:
            response.status_code = 200
            response._content = b'{"status":"ok"}'
        return response

    return external_get


def _rendered_sync_data(client, device, row_ids, server_key=SERVER_KEY, submit_name="select"):
    """Return the endpoint-bound fields emitted by the real cable table."""
    from django.urls import reverse

    rendered = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"tab": "cables", "server_key": server_key},
    )
    assert rendered.status_code == 200
    requested_ids = [row_ids] if isinstance(row_ids, (str, int)) else list(row_ids)
    records = {str(row.record["row_id"]): row.record for row in rendered.context["cable_sync"]["table"].rows}
    data = {
        "server_key": server_key,
        submit_name: requested_ids[0] if len(requested_ids) == 1 else requested_ids,
    }
    for row_id in requested_ids:
        record = records[str(row_id)]
        data.update(
            {
                f"expected_local_id_{row_id}": record["netbox_local_interface_id"],
                f"expected_local_device_id_{row_id}": record["netbox_local_device_id"],
                f"expected_remote_id_{row_id}": record["netbox_remote_interface_id"],
                f"expected_remote_device_id_{row_id}": record["netbox_remote_device_id"],
            }
        )
    return data


# ---------------------------------------------------------------------------
# manual_remote_id enrichment
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestManualRemoteEnrichment:
    """enrich honors a manually picked remote over label/name matching."""

    def _enrich(self, obj, row):
        return _make_view().enrich_links_data([row], obj, server_key=SERVER_KEY)[0]

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

        row = {
            "local_port": "Gi0/1",
            "local_port_id": "1001",
            "remote_device": "name-that-matches-nothing",
            "remote_port": "also-no-match",
            "manual_remote_id": remote.pk,
        }
        link = _make_view().enrich_links_data([row], local_dev, server_key=SERVER_KEY)[0]

        assert link["netbox_local_interface_id"] == local.pk
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

    def test_manual_pick_on_a_multi_termination_cable_is_not_actionable(self):
        """A picked breakout endpoint must not advertise an action the writer rejects."""
        from dcim.models import Cable

        acs, (csp,), _ = make_serial_device("pick-multi-local", csp_names=["ttyS1"])
        _target, _, target_ports = make_serial_device(
            "pick-multi-target",
            cp_names=["console1", "console2"],
        )
        _other, _, (other_port,) = make_serial_device("pick-multi-other", cp_names=["console"])
        breakout = Cable(
            a_terminations=[target_ports[0], target_ports[1]],
            b_terminations=[other_port],
            status="connected",
        )
        breakout.save()

        link = self._enrich(
            acs,
            _serial_row(csp, "no-label-match", acs, manual_remote_id=target_ports[0].pk),
        )

        assert link["cable_status"] == "Multi-termination Cable Not Supported"
        assert link["can_create_cable"] is False
        assert link.get("picker_url") is None
        assert str(breakout.pk) in link["cable_url"]


def test_manual_pick_overlay_reads_the_real_cache_in_one_batch():
    """A large cable snapshot must not issue one cache read for each row."""
    from django.core.cache import cache

    from netbox_librenms_plugin.utils import apply_cable_manual_picks, cable_manual_pick_cache_key

    rows = [{"local_port_id": str(port_id), "row_id": str(port_id)} for port_id in range(1, 501)]
    snapshot_key = "librenms_plugin:test:manual-pick-batch"
    snapshot_token = "manual-pick-batch-token"
    user_id = 123
    picked_row = rows[-1]
    cache.set(
        cable_manual_pick_cache_key(snapshot_key, snapshot_token, user_id, picked_row["row_id"]),
        {"manual_remote_id": 456},
        timeout=300,
    )

    class CacheReadCounter:
        def __init__(self, backend):
            self.backend = backend
            self.get_calls = 0
            self.get_many_calls = 0

        def get(self, key):
            self.get_calls += 1
            return self.backend.get(key)

        def get_many(self, keys):
            self.get_many_calls += 1
            return self.backend.get_many(keys)

    counted_cache = CacheReadCounter(cache)
    overlaid, applied = apply_cable_manual_picks(
        counted_cache,
        snapshot_key,
        {"links": rows, "snapshot_token": snapshot_token},
        user_id,
        rows,
    )

    assert applied is True
    assert overlaid[-1]["manual_remote_id"] == 456
    assert counted_cache.get_calls == 0
    assert counted_cache.get_many_calls == 1


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
        link = _make_view().enrich_links_data([row], acs, server_key=SERVER_KEY)[0]

        assert link["cable_status"] == "Cable Found"
        assert link["can_create_cable"] is False

    def test_untagged_cable_to_other_device_still_mismatches(self):
        """The trust rule is tag-gated: an untagged cable keeps the mismatch/re-point offer."""
        acs, (csp,), _ = make_serial_device("trust-ser2", csp_names=["ttyS1"])
        _actual, _, (actual_cp,) = make_serial_device("trust-ser2-actual", cp_names=["console"])
        _label_dev, _, (_label_cp,) = make_serial_device("trust-ser2-label", cp_names=["console"])
        cable_together(csp, actual_cp)  # untagged

        row = _serial_row(csp, "trust-ser2-label", acs)
        link = _make_view().enrich_links_data([row], acs, server_key=SERVER_KEY)[0]

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
        cache.set(key_view.get_cache_key(acs, "links", SERVER_KEY), {"links": [link]}, timeout=300)
        url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[acs.pk])
        return acs, csp, link, url

    def test_get_returns_picker_modal(self):
        client = self._client("modal")
        _acs, _csp, link, url = self._seed_serial("modal")

        resp = client.get(url, {"row_id": link["local_port_id"], "server_key": SERVER_KEY})

        assert resp.status_code == 200
        content = resp.content.decode()
        assert "ttyS5" in content  # row context shown
        assert 'name="q"' in content  # device search input
        assert "remote-picker-ports" in content  # port area placeholder

    def test_virtual_interfaces_are_not_listed_or_accepted(self):
        """The normal picker must expose only Interfaces that can terminate a Cable."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("physical-interfaces")
        local_device = make_device("picker-physical-local")
        local = make_interface(local_device, "Ethernet1")
        remote_device = make_device("picker-physical-remote")
        lag = make_interface(remote_device, "Port-Channel1", iface_type="lag")
        physical = make_interface(remote_device, "Ethernet9")
        row = {
            "_source": "main",
            "local_port": local.name,
            "local_port_id": 10,
            "remote_device": "unknown",
            "remote_port": "unknown",
        }
        cache_key = object.__new__(SyncCablesView).get_cache_key(local_device, "links", SERVER_KEY)
        cache.set(cache_key, {"links": [row], "snapshot_token": "physical-interfaces"}, timeout=300)
        url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local_device.pk])

        ports = client.get(
            url,
            {
                "row_id": "10",
                "server_key": SERVER_KEY,
                "source": "main",
                "action": "ports",
                "device_id": remote_device.pk,
            },
        )

        assert ports.status_code == 200
        assert physical.name in ports.content.decode()
        assert lag.name not in ports.content.decode()

        rejected = client.post(
            url,
            {
                "row_id": "10",
                "server_key": SERVER_KEY,
                "remote_interface_id": lag.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        accepted = client.post(
            url,
            {
                "row_id": "10",
                "server_key": SERVER_KEY,
                "remote_interface_id": physical.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        assert rejected.status_code == 400
        assert accepted.status_code == 200

    def test_unknown_row_in_valid_snapshot_does_not_refetch_librenms(self):
        """A forged row ID must not turn a valid cached snapshot into a live API fetch."""
        from unittest.mock import patch

        client = self._client("unknown-row")
        _acs, _csp, _link, url = self._seed_serial("unknown-row")

        with patch("netbox_librenms_plugin.librenms_api.requests.get") as external_get:
            response = client.get(url, {"row_id": "missing", "server_key": SERVER_KEY})

        assert response.status_code == 404
        external_get.assert_not_called()

    def test_unconfigured_serial_label_does_not_auto_create_a_cable(self):
        """A default sensor label is not evidence for a remote Device match."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("unconfigured-label")
        local, (csp,), _ = make_serial_device("picker-unconfigured-local", csp_names=["ttyS49"])
        remote, _, (cp,) = make_serial_device("ttyS49", cp_names=["console"])
        row = _serial_row(csp, csp.name, local, is_configured=False)
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(local, "links", server_key),
            {"links": [row]},
            timeout=300,
        )

        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert rendered.status_code == 200
        record = next(iter(rendered.context["cable_sync"]["table"].rows)).record
        assert record.get("netbox_remote_interface_id") is None
        assert record.get("can_create_cable") is False

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            {
                "sync_one": row["local_port_id"],
                f"expected_local_id_{row['local_port_id']}": csp.pk,
                f"expected_local_device_id_{row['local_port_id']}": local.pk,
                f"expected_remote_id_{row['local_port_id']}": cp.pk,
                f"expected_remote_device_id_{row['local_port_id']}": remote.pk,
                "server_key": server_key,
            },
        )

        assert synced.status_code == 302
        csp.refresh_from_db()
        cp.refresh_from_db()
        assert csp.cable_id is None
        assert cp.cable_id is None

    def test_removed_server_cache_cannot_drive_picker_or_cable_sync(self):
        """Action endpoints must reject a cached snapshot for an unconfigured server key."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("removed-server")
        local, (csp,), _ = make_serial_device("picker-removed-server-local", csp_names=["ttyS1"])
        remote, _, (cp,) = make_serial_device("picker-removed-server-remote", cp_names=["console"])
        row = _serial_row(csp, remote.name, local)
        row["local_port_id"] = "serial:removed-server"
        stale_key = "removed-server"
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", stale_key)
        cache.set(cache_key, {"links": [row], "snapshot_token": "removed-server-snapshot"}, timeout=300)

        picker_response = client.post(
            reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local.pk]),
            {
                "row_id": row["local_port_id"],
                "server_key": stale_key,
                "remote_interface_id": cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        sync_response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            {"sync_one": row["local_port_id"], "server_key": stale_key},
        )

        assert picker_response.status_code == 400
        assert sync_response.status_code == 302
        csp.refresh_from_db()
        cp.refresh_from_db()
        assert csp.cable_id is None
        assert cp.cable_id is None

    def test_malformed_origin_redirect_stays_on_the_cable_tab(self):
        """A rejected POST must preserve only the validated server key in a local redirect."""
        from django.urls import reverse

        client = self._client("malformed-origin")
        local = make_device("picker-malformed-origin-local")

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            {
                "origin_device_id": "not-a-device-id",
                "server_key": SERVER_KEY,
            },
        )

        expected_path = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk])
        assert response.status_code == 302
        assert response["Location"] == f"{expected_path}?tab=cables&server_key={SERVER_KEY}"

    def test_migrated_selected_member_is_read_only_in_render_and_verify(self):
        """The exact local termination owner must suppress actions before the writer rejects it."""
        import json

        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import mark_librenms_migrated, set_librenms_device_id
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("migrated-selected-member")
        page = make_device("picker-migrated-selected-page")
        selected = make_device("picker-migrated-selected-local")
        make_virtual_chassis("picker-migrated-selected-vc", page, selected)
        local_interface = make_interface(selected, "Ethernet2/1")
        remote = make_device("picker-migrated-selected-remote")
        remote_interface = make_interface(remote, "Ethernet9")
        winner = make_device("picker-migrated-selected-winner")
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(page, 42, server_key)
        page.save()
        row = {
            "local_port": local_interface.name,
            "local_port_id": 10,
            "remote_device": remote.name,
            "remote_port": remote_interface.name,
            "remote_port_id": 20,
            "_source": "main",
        }
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(page, "links", server_key),
            {"links": [row]},
            timeout=300,
        )
        mark_librenms_migrated(selected, winner.pk, server_key)
        selected.save()

        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[page.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        assert rendered.status_code == 200
        record = next(iter(rendered.context["cable_sync"]["table"].rows)).record
        table_html = rendered.context["cable_sync"]["table"].as_html(rendered.wsgi_request)
        assert record["netbox_local_device_id"] == selected.pk
        assert record["can_create_cable"] is False
        assert "Pick remote end" not in table_html
        assert 'name="sync_one"' not in table_html

        verified = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            data=json.dumps(
                {
                    "device_id": selected.pk,
                    "origin_device_id": page.pk,
                    "row_id": "10",
                    "server_key": server_key,
                }
            ),
            content_type="application/json",
        )

        assert verified.status_code == 200
        formatted = verified.json()["formatted_row"]
        assert formatted["can_create_cable"] is False
        assert "Pick remote end" not in formatted["actions"]
        assert "sync_one" not in formatted["actions"]

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[page.pk]),
            {
                "sync_one": "10",
                "server_key": server_key,
                "expected_local_id_10": local_interface.pk,
                "expected_local_device_id_10": selected.pk,
                "expected_remote_id_10": remote_interface.pk,
                "expected_remote_device_id_10": remote.pk,
            },
        )
        assert synced.status_code == 302
        local_interface.refresh_from_db()
        remote_interface.refresh_from_db()
        assert local_interface.cable_id is None
        assert remote_interface.cable_id is None

    def test_migrated_donor_cache_cannot_drive_cable_sync(self):
        """A direct POST must not mutate a Device after it entered migrated mode."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import mark_librenms_migrated
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("migrated-donor")
        donor, (csp,), _ = make_serial_device("picker-migrated-donor", csp_names=["ttyS1"])
        winner = make_device("picker-migrated-winner")
        remote, _, (cp,) = make_serial_device("picker-migrated-remote", cp_names=["console"])
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        row = _serial_row(csp, remote.name, donor)
        row["local_port_id"] = "serial:migrated-donor"
        cache_key = object.__new__(SyncCablesView).get_cache_key(donor, "links", server_key)
        cache.set(cache_key, {"links": [row]}, timeout=300)
        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[donor.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        record = next(iter(rendered.context["cable_sync"]["table"].rows)).record
        assert record["can_create_cable"] is True
        mark_librenms_migrated(donor, winner.pk, server_key)
        donor.save()

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[donor.pk]),
            {
                "sync_one": record["row_id"],
                "server_key": server_key,
                f"expected_local_id_{record['row_id']}": csp.pk,
                f"expected_local_device_id_{record['row_id']}": donor.pk,
                f"expected_remote_id_{record['row_id']}": cp.pk,
                f"expected_remote_device_id_{record['row_id']}": remote.pk,
            },
        )

        assert response.status_code == 302
        csp.refresh_from_db()
        cp.refresh_from_db()
        assert csp.cable_id is None
        assert cp.cable_id is None

    def test_migrated_vc_origin_cannot_repaint_or_sync_through_a_live_member(self):
        """A member repaint must retain the migrated page's read-only contract."""
        import json

        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import mark_librenms_migrated, set_librenms_device_id
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("migrated-vc-origin")
        donor = make_device("picker-migrated-vc-donor")
        member = make_device("picker-migrated-vc-member")
        make_virtual_chassis("picker-migrated-vc", donor, member)
        member_interface = make_interface(member, "Ethernet2/1")
        remote = make_device("picker-migrated-vc-remote")
        remote_interface = make_interface(remote, "Ethernet9")
        winner = make_device("picker-migrated-vc-winner")
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(donor, 42, server_key)
        donor.save()
        row = {
            "local_port": member_interface.name,
            "local_port_id": 10,
            "remote_device": remote.name,
            "remote_port": remote_interface.name,
            "remote_port_id": 20,
            "_source": "main",
        }
        cache_key = object.__new__(SyncCablesView).get_cache_key(donor, "links", server_key)
        cache.set(cache_key, {"links": [row]}, timeout=300)
        mark_librenms_migrated(donor, winner.pk, server_key)
        donor.save()

        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[donor.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        assert rendered.status_code == 200
        rendered_record = next(iter(rendered.context["cable_sync"]["table"].rows)).record
        assert rendered_record["can_create_cable"] is False
        assert rendered_record.get("picker_url") is None

        verified = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            data=json.dumps(
                {
                    "device_id": member.pk,
                    "origin_device_id": donor.pk,
                    "row_id": "10",
                    "server_key": server_key,
                }
            ),
            content_type="application/json",
        )

        assert verified.status_code == 200
        formatted = verified.json()["formatted_row"]
        assert formatted["can_create_cable"] is False
        assert "<form" not in formatted["actions"]

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[member.pk]),
            {
                "sync_one": "10",
                "origin_device_id": donor.pk,
                "expected_local_id_10": member_interface.pk,
                "expected_local_device_id_10": member.pk,
                "expected_remote_id_10": remote_interface.pk,
                "expected_remote_device_id_10": remote.pk,
                "server_key": server_key,
            },
        )

        assert synced.status_code == 302
        member_interface.refresh_from_db()
        remote_interface.refresh_from_db()
        assert member_interface.cable_id is None
        assert remote_interface.cable_id is None

    def test_force_confirmation_retains_the_origin_migration_guard(self):
        """The force modal must retain the page owner that authorized the first POST."""
        import re

        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import mark_librenms_migrated, set_librenms_device_id
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("force-origin")
        origin = make_device("picker-force-origin")
        member = make_device("picker-force-member")
        make_virtual_chassis("picker-force-vc", origin, member)
        make_interface(origin, "Ethernet1")
        local_interface = make_interface(member, "Ethernet1")
        desired_device = make_device("picker-force-desired")
        desired_interface = make_interface(desired_device, "Ethernet9")
        occupied_device = make_device("picker-force-occupied")
        occupied_interface = make_interface(occupied_device, "Ethernet8")
        existing_cable = cable_together(local_interface, occupied_interface)
        winner = make_device("picker-force-winner")
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(origin, 42, server_key)
        origin.save()
        row = {
            "local_port": local_interface.name,
            "local_port_id": 10,
            "remote_device": desired_device.name,
            "remote_port": desired_interface.name,
            "remote_port_id": 20,
            "_source": "main",
        }
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(origin, "links", server_key),
            {"links": [row]},
            timeout=300,
        )
        sync_url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[member.pk])

        conflict = client.post(
            sync_url,
            {
                "sync_one": "10",
                "origin_device_id": origin.pk,
                "expected_local_id_10": local_interface.pk,
                "expected_local_device_id_10": member.pk,
                "expected_remote_id_10": desired_interface.pk,
                "expected_remote_device_id_10": desired_device.pk,
                "server_key": server_key,
            },
            HTTP_HX_REQUEST="true",
        )

        assert conflict.status_code == 200
        html = conflict.content.decode()
        assert f'name="origin_device_id" value="{origin.pk}"' in html
        intent_match = re.search(r'name="expected_cable_intent_10" value="([^"]+)"', html)
        assert intent_match is not None
        mark_librenms_migrated(origin, winner.pk, server_key)
        origin.save()

        forced = client.post(
            sync_url,
            {
                "force": "on",
                "select": "10",
                "origin_device_id": origin.pk,
                "expected_local_id_10": local_interface.pk,
                "expected_local_device_id_10": member.pk,
                "expected_remote_id_10": desired_interface.pk,
                "expected_remote_device_id_10": desired_device.pk,
                "expected_cable_intent_10": intent_match.group(1),
                "server_key": server_key,
            },
        )

        assert forced.status_code == 302
        local_interface.refresh_from_db()
        desired_interface.refresh_from_db()
        assert local_interface.cable_id == existing_cable.pk
        assert desired_interface.cable_id is None

    def test_migrated_cache_owner_cannot_drive_an_unmarked_member_write(self):
        """The chassis cache owner is part of the migrated read-only boundary."""
        import json

        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_ip
        from netbox_librenms_plugin.utils import mark_librenms_migrated, set_librenms_device_id
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("migrated-cache-owner")
        origin = make_device("picker-cache-origin")
        member = make_device("picker-cache-member")
        cache_owner = make_device("picker-cache-owner")
        chassis = make_virtual_chassis("picker-cache-vc", origin, member, cache_owner)
        chassis.master = cache_owner
        chassis.save(update_fields=["master"])
        make_interface(origin, "Ethernet1")
        member_interface = make_interface(member, "Ethernet1")
        management = make_interface(cache_owner, "Management1")
        primary_ip = make_ip("198.18.0.10/32", assigned_object=management)
        cache_owner.primary_ip4 = primary_ip
        cache_owner.save(update_fields=["primary_ip4"])
        remote = make_device("picker-cache-remote")
        remote_interface = make_interface(remote, "Ethernet9")
        winner = make_device("picker-cache-winner")
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(cache_owner, 42, server_key)
        cache_owner.save()
        row = {
            "local_port": "Ethernet1",
            "local_port_id": 10,
            "remote_device": remote.name,
            "remote_port": remote_interface.name,
            "remote_port_id": 20,
            "_source": "main",
        }
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(cache_owner, "links", server_key),
            {"links": [row]},
            timeout=300,
        )
        mark_librenms_migrated(cache_owner, winner.pk, server_key)
        cache_owner.save()

        verified = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            data=json.dumps(
                {
                    "device_id": member.pk,
                    "origin_device_id": origin.pk,
                    "row_id": "10",
                    "server_key": server_key,
                }
            ),
            content_type="application/json",
        )

        assert verified.status_code == 200
        formatted = verified.json()["formatted_row"]
        assert formatted["can_create_cable"] is False
        assert "<form" not in formatted["actions"]

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[member.pk]),
            {
                "sync_one": "10",
                "origin_device_id": origin.pk,
                "expected_local_id_10": member_interface.pk,
                "expected_local_device_id_10": member.pk,
                "expected_remote_id_10": remote_interface.pk,
                "expected_remote_device_id_10": remote.pk,
                "server_key": server_key,
            },
        )

        assert synced.status_code == 302
        member_interface.refresh_from_db()
        remote_interface.refresh_from_db()
        assert member_interface.cable_id is None
        assert remote_interface.cable_id is None

    def test_search_action_filters_devices(self):
        client = self._client("search")
        _acs, _csp, link, url = self._seed_serial("search")
        target = make_device("picker-search-target")
        make_device("picker-search-other")

        resp = client.get(
            url,
            {"row_id": link["local_port_id"], "server_key": SERVER_KEY, "action": "search", "q": "search-target"},
        )

        assert resp.status_code == 200
        content = resp.content.decode()
        assert "picker-search-target" in content
        assert "picker-search-other" not in content
        assert str(target.pk) in content

    def test_search_action_loads_sites_with_the_device_query(self):
        """Rendering 20 site labels must not issue one lazy Site query per result."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        client = self._client("search-sites")
        _acs, _csp, link, url = self._seed_serial("search-sites")
        for index in range(20):
            make_device(f"picker-search-site-{index:02d}")

        with CaptureQueriesContext(connection) as captured:
            response = client.get(
                url,
                {
                    "row_id": link["local_port_id"],
                    "server_key": SERVER_KEY,
                    "action": "search",
                    "q": "picker-search-site-",
                },
            )

        assert response.status_code == 200
        standalone_site_queries = [
            query["sql"] for query in captured.captured_queries if 'FROM "dcim_site"' in query["sql"]
        ]
        assert standalone_site_queries == []

    def test_search_fragment_does_not_read_the_cable_snapshot(self):
        from django.core.cache import caches
        from django.test import override_settings
        from django.urls import reverse

        client = self._client("search-cache-volume")
        device = make_device("picker-search-cache-volume")
        url = reverse(
            "plugins:netbox_librenms_plugin:cable_remote_picker",
            args=[device.pk],
        )
        cache_settings = {
            "default": {
                "BACKEND": "netbox_librenms_plugin.tests.test_cable_remote_picker.CountingLocMemCache",
                "LOCATION": "picker-fragment-cache-volume",
            }
        }

        with override_settings(CACHES=cache_settings):
            CountingLocMemCache.get_count = 0
            caches["default"].clear()
            response = client.get(
                url,
                {
                    "row_id": "not-needed-for-search",
                    "server_key": SERVER_KEY,
                    "source": "serial",
                    "action": "search",
                    "q": "nothing",
                },
            )

        assert response.status_code == 200
        assert CountingLocMemCache.get_count == 0

    def test_ports_action_lists_console_ports_for_serial_rows(self):
        client = self._client("ports")
        _acs, _csp, link, url = self._seed_serial("ports")
        remote, _, (cp,) = make_serial_device("picker-ports-target", cp_names=["console-X"])
        make_interface(remote, "Gi0/1")  # an Interface that must NOT be offered for a serial row

        resp = client.get(
            url,
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "source": "serial",
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

        from netbox_librenms_plugin.tests.conftest import clear_test_cache

        client = self._client("expiry")
        _acs, _csp, link, url = self._seed_serial("expiry")
        remote, _, (cp,) = make_serial_device("picker-expiry-target", cp_names=["console-Y"])
        make_interface(remote, "Gi0/9")

        clear_test_cache(cache)  # snapshot gone between modal-open and device-click

        resp = client.get(
            url,
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
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

        resp = client.get(url, {"row_id": link["local_port_id"], "server_key": SERVER_KEY})

        assert resp.status_code == 200
        assert "source=serial" in resp.content.decode()

    def test_ports_action_rejects_malformed_device_id(self):
        """A hand-crafted non-numeric device_id gets a 400, not a 500."""
        client = self._client("badid")
        _acs, _csp, link, url = self._seed_serial("badid")

        resp = client.get(
            url,
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "source": "serial",
                "action": "ports",
                "device_id": "abc",
            },
        )

        assert resp.status_code == 400

    def test_ports_action_rejects_out_of_range_device_id(self):
        """A numeric value outside the NetBox PK range must return a controlled response."""
        client = self._client("large-device-id")
        _acs, _csp, link, url = self._seed_serial("large-device-id")

        response = client.get(
            url,
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "source": "serial",
                "action": "ports",
                "device_id": "9" * 100,
            },
        )

        assert response.status_code == 404

    def test_picker_post_rejects_out_of_range_remote_id(self):
        """An out-of-range remote PK must fail at the request boundary."""
        client = self._client("large-remote-id")
        _acs, _csp, link, url = self._seed_serial("large-remote-id")

        response = client.post(
            url,
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": "9" * 100,
            },
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 400

    def test_verify_rejects_out_of_range_device_id(self):
        """Cable verify must reject a numeric PK that cannot fit the database column."""
        import json

        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        client = self._client("large-verify-device-id")
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            data=json.dumps(
                {
                    "device_id": "9" * 100,
                    "row_id": "10",
                    "server_key": next(iter(LibreNMSAPI.get_available_servers())),
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 404

    def test_ports_action_splits_free_and_cabled_ports(self):
        """The port list separates free ports from cabled ones (which stay pickable but marked)."""
        client = self._client("split")
        _acs, _csp, link, url = self._seed_serial("split")
        remote, _, (free_cp, cabled_cp) = make_serial_device("picker-split-target", cp_names=["con-free", "con-used"])
        _peer, (peer_csp,), _ = make_serial_device("picker-split-peer", csp_names=["s0"])
        cable_together(cabled_cp, peer_csp)

        resp = client.get(
            url,
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "source": "serial",
                "action": "ports",
                "device_id": remote.pk,
            },
        )

        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'label="Available"' in content
        assert "con-free" in content
        assert 'label="Already cabled (overwrite-protected)"' in content
        assert "con-used — cabled" in content

    def test_post_stores_manual_pick_outside_the_shared_snapshot(self):
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import cable_manual_pick_cache_key, cable_snapshot_token
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("post")
        acs, _csp, link, url = self._seed_serial("post")
        _r, _, (cp,) = make_serial_device("picker-post-target", cp_names=["console"])

        resp = client.post(
            url,
            data={
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 200
        key_view = object.__new__(SyncCablesView)
        snapshot_key = key_view.get_cache_key(acs, "links", SERVER_KEY)
        cached = cache.get(snapshot_key)
        row = next(r for r in cached["links"] if r["local_port_id"] == link["local_port_id"])
        assert "manual_remote_id" not in row
        user_id = client.session["_auth_user_id"]
        pick = cache.get(
            cable_manual_pick_cache_key(
                snapshot_key,
                cable_snapshot_token(cached),
                user_id,
                link["local_port_id"],
            )
        )
        assert pick["manual_remote_id"] == cp.pk
        content = resp.content.decode()
        # The response re-renders the cable partial with the resolved remote shown...
        assert "console" in content
        # ...and closes the picker modal via the OOB block.
        assert "closeHtmxModal" in content

    def test_duplicate_local_port_rows_keep_independent_picker_and_sync_state(self):
        """Two LibreNMS links on one local port must remain separate actionable rows."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("duplicate-link-rows")
        local_device = make_device("picker-duplicate-link-local")
        local_device.custom_field_data["librenms_id"] = 10
        local_device.save()
        local = make_interface(local_device, "Ethernet1")
        first_device = make_device("picker-duplicate-link-first")
        make_interface(first_device, "Ethernet1")
        second_device = make_device("picker-duplicate-link-second")
        make_interface(second_device, "Ethernet1")
        picked_device = make_device("picker-duplicate-link-picked")
        picked = make_interface(picked_device, "Ethernet9")
        rows = [
            {
                "link_id": 101,
                "local_port": local.name,
                "local_port_id": 10,
                "remote_device": first_device.name,
                "remote_port": "Ethernet1",
                "remote_port_id": 20,
                "_source": "main",
            },
            {
                "link_id": 102,
                "local_port": local.name,
                "local_port_id": 10,
                "remote_device": second_device.name,
                "remote_port": "Ethernet1",
                "remote_port_id": 30,
                "_source": "main",
            },
        ]
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(local_device, "links", server_key)
        cache.set(cache_key, {"links": rows, "snapshot_token": "duplicate-link-snapshot"}, timeout=300)

        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local_device.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert rendered.status_code == 200
        table = rendered.context["cable_sync"]["table"]
        assert table is not None, rendered.context["cable_sync"]["server_key"]
        content = table.as_html(rendered.wsgi_request)
        assert "main:link:101" in content
        assert "main:link:102" in content

        picker_url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local_device.pk])
        picked_response = client.post(
            picker_url,
            {
                "row_id": "main:link:102",
                "server_key": server_key,
                "remote_interface_id": picked.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        assert picked_response.status_code == 200

        post_data = _rendered_sync_data(client, local_device, "main:link:102", server_key)
        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local_device.pk]),
            post_data,
            HTTP_HX_REQUEST="true",
        )

        assert synced.status_code == 200
        local.refresh_from_db()
        picked.refresh_from_db()
        assert local.cable_id == picked.cable_id

    def test_manual_pick_reserves_remote_before_an_earlier_auto_row(self):
        """Auto matching must not consume an endpoint reserved by a later manual row."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("manual-reservation-order")
        local, (auto_csp, manual_csp), _ = make_serial_device(
            "picker-manual-reservation-local",
            csp_names=["ttyS1", "ttyS2"],
        )
        remote, _, (reserved_cp, available_cp) = make_serial_device(
            "picker-manual-reservation-remote",
            cp_names=["console-a", "console-b"],
        )
        auto_row = _serial_row(auto_csp, remote.name, local, sensor_index_int=1)
        manual_row = _serial_row(manual_csp, "no-label-match", local, sensor_index_int=2)
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", SERVER_KEY)
        cache.set(
            cache_key,
            {"links": [auto_row, manual_row], "snapshot_token": "manual-reservation-snapshot"},
            timeout=300,
        )
        picker_url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local.pk])
        picked = client.post(
            picker_url,
            {
                "row_id": manual_row["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": reserved_cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        assert picked.status_code == 200

        post_data = _rendered_sync_data(
            client,
            local,
            auto_row["local_port_id"],
        )
        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            post_data,
            HTTP_HX_REQUEST="true",
        )

        assert synced.status_code == 200
        auto_csp.refresh_from_db()
        reserved_cp.refresh_from_db()
        available_cp.refresh_from_db()
        assert auto_csp.cable_id == available_cp.cable_id
        assert reserved_cp.cable_id is None

    def test_sync_rejects_an_auto_target_that_changed_after_render(self):
        """A row identity must not authorize a different auto-selected ConsolePort."""
        import re

        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("stale-auto-target")
        local, (csp,), _ = make_serial_device("picker-stale-auto-local", csp_names=["ttyS1"])
        remote, _, (first_cp, second_cp) = make_serial_device(
            "picker-stale-auto-remote",
            cp_names=["console0", "console1"],
        )
        _blocker, (blocker_csp,), _ = make_serial_device(
            "picker-stale-auto-blocker",
            csp_names=["ttyS1"],
        )
        row = _serial_row(csp, remote.name, local)
        row["local_port_id"] = "serial:stale-auto-target"
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cache.set(cache_key, {"links": [row], "snapshot_token": "stale-auto-target"}, timeout=300)

        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        assert rendered.status_code == 200
        record = next(iter(rendered.context["cable_sync"]["table"].rows)).record
        table_html = rendered.context["cable_sync"]["table"].as_html(rendered.wsgi_request)
        expected_name = "expected_remote_id_serial:stale-auto-target"
        assert re.search(rf'name="{re.escape(expected_name)}"\s+value="{first_cp.pk}"', table_html)

        cable_together(blocker_csp, first_cp)
        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            {
                "sync_one": "serial:stale-auto-target",
                "server_key": server_key,
                "expected_local_id_serial:stale-auto-target": record["netbox_local_interface_id"],
                "expected_local_device_id_serial:stale-auto-target": record["netbox_local_device_id"],
                expected_name: record["netbox_remote_interface_id"],
                "expected_remote_device_id_serial:stale-auto-target": record["netbox_remote_device_id"],
            },
        )

        assert synced.status_code == 302
        csp.refresh_from_db()
        first_cp.refresh_from_db()
        second_cp.refresh_from_db()
        assert csp.cable_id is None
        assert first_cp.cable_id is not None
        assert second_cp.cable_id is None

    def test_pick_on_a_deleted_local_port_does_not_reserve_the_remote(self):
        """A dead picked row must not block a valid auto row from using its free port."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("deleted-manual-source")
        local, (auto_csp, deleted_csp), _ = make_serial_device(
            "picker-deleted-source-local",
            csp_names=["ttyS1", "ttyS2"],
        )
        remote, _, (console_port,) = make_serial_device(
            "picker-deleted-source-remote",
            cp_names=["console"],
        )
        auto_row = _serial_row(auto_csp, remote.name, local, sensor_index_int=1)
        deleted_row = _serial_row(deleted_csp, "no-label-match", local, sensor_index_int=2)
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cache.set(
            cache_key,
            {"links": [auto_row, deleted_row], "snapshot_token": "deleted-source-snapshot"},
            timeout=300,
        )
        picker_url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local.pk])
        picked = client.post(
            picker_url,
            {
                "row_id": deleted_row["local_port_id"],
                "server_key": server_key,
                "remote_interface_id": console_port.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        assert picked.status_code == 200
        deleted_csp.delete()

        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        records = [row.record for row in rendered.context["cable_sync"]["table"].rows]
        rendered_auto = next(row for row in records if row["row_id"] == auto_row["local_port_id"])
        assert rendered_auto["netbox_remote_interface_id"] == console_port.pk

        row_id = auto_row["local_port_id"]
        post_data = {
            "sync_one": row_id,
            "server_key": server_key,
            f"expected_local_id_{row_id}": rendered_auto["netbox_local_interface_id"],
            f"expected_local_device_id_{row_id}": rendered_auto["netbox_local_device_id"],
            f"expected_remote_id_{row_id}": rendered_auto["netbox_remote_interface_id"],
            f"expected_remote_device_id_{row_id}": rendered_auto["netbox_remote_device_id"],
        }
        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            post_data,
            HTTP_HX_REQUEST="true",
        )

        assert synced.status_code == 200
        auto_csp.refresh_from_db()
        console_port.refresh_from_db()
        assert auto_csp.cable_id == console_port.cable_id

    def test_deleted_manual_serial_target_does_not_fall_back_to_the_label(self):
        """A stale explicit pick must not cable the row to an automatic target."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("deleted-manual-serial-target")
        local, (csp,), _ = make_serial_device(
            "picker-deleted-manual-serial-local",
            csp_names=["ttyS1"],
        )
        automatic, _, (automatic_cp,) = make_serial_device(
            "picker-deleted-manual-serial-auto",
            cp_names=["console"],
        )
        picked_device, _, (picked_cp,) = make_serial_device(
            "picker-deleted-manual-serial-picked",
            cp_names=["console"],
        )
        row = _serial_row(csp, automatic.name, local)
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cache.set(cache_key, {"links": [row], "snapshot_token": "deleted-manual-serial"}, timeout=300)

        picked = client.post(
            reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local.pk]),
            {
                "row_id": row["local_port_id"],
                "server_key": server_key,
                "remote_interface_id": picked_cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        assert picked.status_code == 200
        post_data = _rendered_sync_data(client, local, row["local_port_id"], server_key)
        picked_cp.delete()

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            post_data,
            HTTP_HX_REQUEST="true",
        )

        assert synced.status_code == 200
        csp.refresh_from_db()
        automatic_cp.refresh_from_db()
        assert csp.cable_id is None
        assert automatic_cp.cable_id is None
        assert picked_device.consoleports.count() == 0

    def test_deleted_manual_interface_target_does_not_fall_back_to_the_label(self):
        """A stale explicit Interface pick must not cable the row to an automatic target."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("deleted-manual-interface-target")
        local_device = make_device("picker-deleted-manual-interface-local")
        local = make_interface(local_device, "Ethernet1")
        automatic_device = make_device("picker-deleted-manual-interface-auto")
        automatic = make_interface(automatic_device, "Ethernet1")
        picked_device = make_device("picker-deleted-manual-interface-picked")
        picked = make_interface(picked_device, "Ethernet9")
        row = {
            "local_port": local.name,
            "local_port_id": 10,
            "remote_device": automatic_device.name,
            "remote_port": automatic.name,
            "remote_port_id": 20,
            "_source": "main",
        }
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(local_device, "links", server_key)
        cache.set(cache_key, {"links": [row], "snapshot_token": "deleted-manual-interface"}, timeout=300)

        picked_response = client.post(
            reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local_device.pk]),
            {
                "row_id": "10",
                "server_key": server_key,
                "remote_interface_id": picked.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        assert picked_response.status_code == 200
        post_data = _rendered_sync_data(client, local_device, "10", server_key)
        picked.delete()

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local_device.pk]),
            post_data,
            HTTP_HX_REQUEST="true",
        )

        assert synced.status_code == 200
        local.refresh_from_db()
        automatic.refresh_from_db()
        assert local.cable_id is None
        assert automatic.cable_id is None
        assert picked_device.interfaces.count() == 0

    def test_picking_an_occupied_remote_reports_a_conflict_before_sync(self):
        """The picker partial must classify both terminations, not only the local CSP."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("occupied-remote")
        local, (local_csp,), _ = make_serial_device("picker-occupied-local", csp_names=["ttyS1"])
        remote, _, (occupied_cp,) = make_serial_device("picker-occupied-remote", cp_names=["console"])
        _other, (other_csp,), _ = make_serial_device("picker-occupied-other", csp_names=["ttyS1"])
        existing_cable = cable_together(other_csp, occupied_cp)
        row = _serial_row(local_csp, "no-label-match", local)
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cache.set(cache_key, {"links": [row], "snapshot_token": "occupied-remote"}, timeout=300)

        picked = client.post(
            reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local.pk]),
            {
                "row_id": row["local_port_id"],
                "server_key": server_key,
                "remote_interface_id": occupied_cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        assert picked.status_code == 200
        assert "Cable Mismatch" in picked.content.decode()
        local_csp.refresh_from_db()
        occupied_cp.refresh_from_db()
        assert local_csp.cable_id is None
        assert occupied_cp.cable_id == existing_cable.pk

    def test_verify_and_sync_keep_the_current_manual_interface_pick(self):
        """A verify repaint must use the same user-scoped pick as the later write."""
        import json

        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("verify-manual-pick")
        local_device = make_device("picker-verify-manual-local")
        local = make_interface(local_device, "Ethernet1")
        automatic_device = make_device("picker-verify-manual-auto")
        automatic = make_interface(automatic_device, "Ethernet1")
        picked_device = make_device("picker-verify-manual-picked")
        picked = make_interface(picked_device, "Ethernet9")
        row = {
            "local_port": local.name,
            "local_port_id": 10,
            "remote_device": automatic_device.name,
            "remote_port": automatic.name,
            "remote_port_id": 20,
            "_source": "main",
        }
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(local_device, "links", server_key)
        cache.set(cache_key, {"links": [row], "snapshot_token": "verify-manual-pick"}, timeout=300)

        picked_response = client.post(
            reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local_device.pk]),
            {
                "row_id": "10",
                "server_key": server_key,
                "remote_interface_id": picked.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        assert picked_response.status_code == 200

        verified = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            data=json.dumps(
                {
                    "device_id": local_device.pk,
                    "row_id": "10",
                    "server_key": server_key,
                }
            ),
            content_type="application/json",
        )

        assert verified.status_code == 200
        formatted_row = verified.json()["formatted_row"]
        formatted = json.dumps(formatted_row)
        assert reverse("dcim:interface", args=[picked.pk]) in formatted
        assert reverse("dcim:interface", args=[automatic.pk]) not in formatted
        assert picked_device.name in formatted_row["remote_device"]
        assert reverse("dcim:device", args=[picked_device.pk]) in formatted_row["remote_device"]
        assert automatic_device.name not in formatted_row["remote_device"]
        assert (
            reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local_device.pk])
            in formatted_row["actions"]
        )
        assert 'hx-get="' in formatted_row["actions"]

        post_data = {
            "sync_one": "10",
            "server_key": server_key,
            "expected_local_id_10": formatted_row["expected_local_id"],
            "expected_local_device_id_10": formatted_row["expected_local_device_id"],
            "expected_remote_id_10": formatted_row["expected_remote_id"],
            "expected_remote_device_id_10": formatted_row["expected_remote_device_id"],
        }
        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local_device.pk]),
            post_data,
            HTTP_HX_REQUEST="true",
        )
        assert synced.status_code == 200
        local.refresh_from_db()
        picked.refresh_from_db()
        assert local.cable_id == picked.cable_id

    def test_remote_virtual_chassis_member_renders_and_syncs_by_exact_owner(self):
        """A remote physical port must bind to its actual VC member through the full flow."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("remote-vc-owner")
        local_device = make_device("picker-remote-vc-local")
        local = make_interface(local_device, "Ethernet1")
        remote_first = make_device("picker-remote-vc-first")
        remote_second = make_device("picker-remote-vc-second")
        make_virtual_chassis("picker-remote-vc", remote_first, remote_second)
        remote = make_interface(remote_second, "ge-2/0/0")
        row = {
            "_source": "main",
            "local_port": local.name,
            "local_port_id": 10,
            "remote_device": remote_first.name,
            "remote_port": remote.name,
            "remote_port_id": 20,
        }
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(local_device, "links", server_key),
            {"links": [row], "snapshot_token": "remote-vc-owner"},
            timeout=300,
        )

        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local_device.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert rendered.status_code == 200
        record = next(iter(rendered.context["cable_sync"]["table"].rows)).record
        assert record["netbox_local_interface_id"] == local.pk
        assert record["netbox_remote_interface_id"] == remote.pk
        assert record["netbox_remote_device_id"] == remote_second.pk

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local_device.pk]),
            {
                "sync_one": record["row_id"],
                "server_key": server_key,
                f"expected_local_id_{record['row_id']}": local.pk,
                f"expected_local_device_id_{record['row_id']}": local_device.pk,
                f"expected_remote_id_{record['row_id']}": remote.pk,
                f"expected_remote_device_id_{record['row_id']}": remote_second.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        assert synced.status_code == 200
        local.refresh_from_db()
        remote.refresh_from_db()
        assert local.cable_id == remote.cable_id

    def test_vc_sync_reclassifies_the_selected_member_instead_of_the_default_member(self):
        """A selected VC member must not inherit the default member's patch-path state."""
        import json

        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("vc-selected-topology")
        first = make_device("picker-vc-topology-first")
        second = make_device("picker-vc-topology-second")
        make_virtual_chassis("picker-vc-topology", first, second)
        first.custom_field_data["librenms_id"] = {SERVER_KEY: 600}
        first.save()
        first_local = make_interface(first, "Ethernet1")
        second_local = make_interface(second, "Ethernet1")
        remote_device = make_device("picker-vc-topology-remote")
        remote = make_interface(remote_device, "Ethernet9")
        _panel, front, rear = make_patch_panel("picker-vc-topology-panel")
        cable_together(first_local, front)
        cable_together(rear, remote)
        row = {
            "local_port": "Ethernet1",
            "local_port_id": 10,
            "remote_device": remote_device.name,
            "remote_port": remote.name,
            "remote_port_id": 20,
            "_source": "main",
        }
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(first, "links", server_key)
        cache.set(cache_key, {"links": [row], "snapshot_token": "vc-selected-topology"}, timeout=300)

        verified = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            data=json.dumps({"device_id": second.pk, "row_id": "10", "server_key": server_key}),
            content_type="application/json",
        )
        assert verified.status_code == 200
        formatted_row = verified.json()["formatted_row"]
        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[first.pk]),
            {
                "sync_one": "10",
                "device_selection_10": second.pk,
                "server_key": server_key,
                "expected_local_id_10": formatted_row["expected_local_id"],
                "expected_local_device_id_10": formatted_row["expected_local_device_id"],
                "expected_remote_id_10": formatted_row["expected_remote_id"],
                "expected_remote_device_id_10": formatted_row["expected_remote_device_id"],
            },
            HTTP_HX_REQUEST="true",
        )

        assert "Cable Mismatch" in formatted_row["cable_status"]
        assert formatted_row["can_create_cable"] is True
        assert "Sync Cable" in formatted_row["actions"]
        assert synced.status_code == 200
        assert "expected_cable_intent_10" in synced.content.decode()
        second_local.refresh_from_db()
        remote.refresh_from_db()
        assert second_local.cable_id is None
        assert remote.cable_id is not None

    def test_vc_sync_uses_the_local_endpoint_returned_by_member_verify(self):
        """A verified VC member change must create the cable on that exact member."""
        import json
        import re

        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("vc-verified-local")
        first = make_device("picker-vc-verified-first")
        second = make_device("picker-vc-verified-second")
        make_virtual_chassis("picker-vc-verified", first, second)
        first_local = make_interface(first, "Ethernet1")
        second_local = make_interface(second, "Ethernet1")
        remote_device = make_device("picker-vc-verified-remote")
        remote = make_interface(remote_device, "Ethernet9")
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(first, 600, server_key)
        first.save()
        row = {
            "_source": "main",
            "local_port": "Ethernet1",
            "local_port_id": 10,
            "remote_device": remote_device.name,
            "remote_port": remote.name,
            "remote_port_id": 20,
        }
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(first, "links", server_key),
            {"links": [row], "snapshot_token": "vc-verified-local"},
            timeout=300,
        )
        initial_render = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[first.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        initial_record = next(iter(initial_render.context["cable_sync"]["table"].rows)).record
        assert initial_record["netbox_local_interface_id"] == first_local.pk
        assert initial_record["netbox_local_device_id"] == first.pk

        verified = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            data=json.dumps(
                {
                    "device_id": second.pk,
                    "origin_device_id": first.pk,
                    "row_id": "10",
                    "server_key": server_key,
                }
            ),
            content_type="application/json",
        )

        assert verified.status_code == 200
        formatted = verified.json()["formatted_row"]
        assert formatted["expected_local_id"] == second_local.pk
        assert formatted["expected_local_device_id"] == second.pk
        assert formatted["expected_remote_id"] == remote.pk
        assert formatted["expected_remote_device_id"] == remote_device.pk
        assert "<form" not in formatted["actions"]
        assert 'name="select"' not in formatted["actions"]
        assert 'name="sync_one" value="10"' in re.sub(r"\s+", " ", formatted["actions"])

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[first.pk]),
            {
                "sync_one": "10",
                "origin_device_id": first.pk,
                "device_selection_10": second.pk,
                "expected_local_id_10": formatted["expected_local_id"],
                "expected_local_device_id_10": formatted["expected_local_device_id"],
                "expected_remote_id_10": formatted["expected_remote_id"],
                "expected_remote_device_id_10": formatted["expected_remote_device_id"],
                "server_key": server_key,
            },
            HTTP_HX_REQUEST="true",
        )

        assert synced.status_code == 200
        first_local.refresh_from_db()
        second_local.refresh_from_db()
        remote.refresh_from_db()
        assert first_local.cable_id is None
        assert second_local.cable_id is not None
        assert second_local.cable_id == remote.cable_id

    def test_manual_pick_is_visible_only_to_the_user_who_made_it(self):
        from django.contrib.auth import get_user_model
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        picker = self._client("private-a")
        other_user = self._client("private-b")
        acs, _csp, link, url = self._seed_serial("private")
        _remote, _, (cp,) = make_serial_device("picker-private-target", cp_names=["private-console"])

        picked = picker.post(
            url,
            data={
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        request_factory = RequestFactory()

        def links_for(client):
            """Read the cached rows the way the sync POST does, as that client's user."""
            request = request_factory.post("/")
            request.user = get_user_model().objects.get(pk=client.session["_auth_user_id"])
            sync_view = object.__new__(SyncCablesView)
            sync_view.request = request
            sync_view._post_server_key = SERVER_KEY
            return sync_view.get_cached_links_data(request, acs)

        picker_links = links_for(picker)
        other_links = links_for(other_user)

        assert picked.status_code == 200
        assert "private-console" in picked.content.decode()
        assert picker_links[0]["manual_remote_id"] == cp.pk
        assert "manual_remote_id" not in other_links[0]

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
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": iface.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 400
        key_view = object.__new__(SyncCablesView)
        cached = cache.get(key_view.get_cache_key(acs, "links", SERVER_KEY))
        row = next(r for r in cached["links"] if r["local_port_id"] == link["local_port_id"])
        assert "manual_remote_id" not in row

    def test_pick_then_sync_creates_cable_to_picked_port(self):
        """Full flow: pick a remote by hand, then sync — a real Cable lands on the picked port."""
        from dcim.models import Cable
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.utils import (
            apply_cable_manual_picks,
            cable_snapshot_token,
        )
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("flow")
        acs, csp, link, url = self._seed_serial("flow")
        _r, _, (cp,) = make_serial_device("picker-flow-target", cp_names=["console"])

        pick = client.post(
            url,
            data={
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        assert pick.status_code == 200

        key_view = object.__new__(SyncCablesView)
        snapshot_key = key_view.get_cache_key(acs, "links", SERVER_KEY)
        cached = cache.get(snapshot_key)
        overlaid, applied = apply_cable_manual_picks(
            cache,
            snapshot_key,
            {"links": cached["links"], "snapshot_token": cable_snapshot_token(cached)},
            client.session["_auth_user_id"],
            cached["links"],
        )
        assert applied is True
        # The overlay only carries the pick; enrichment resolves it to the NetBox termination.
        assert overlaid[0]["manual_remote_id"] == cp.pk

        sync_url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk])
        post_data = _rendered_sync_data(client, acs, link["local_port_id"])
        resp = client.post(
            sync_url,
            data=post_data,
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 200, resp.content.decode()
        csp.refresh_from_db()
        cp.refresh_from_db()
        assert csp.cable_id is not None, resp.content.decode()
        assert csp.cable_id == cp.cable_id  # cabled to the PICKED port
        assert "librenms" in set(Cable.objects.get(pk=csp.cable_id).tags.values_list("slug", flat=True))

    def test_batch_rejects_two_manual_rows_targeting_one_remote_port(self):
        from dcim.models import Cable
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("duplicate-remote")
        acs, (csp_a, csp_b), _unused = make_serial_device(
            "acs-duplicate-remote",
            csp_names=["ttyS1", "ttyS2"],
        )
        links = [
            _serial_row(csp_a, "no-label-a", acs, sensor_index_int=1),
            _serial_row(csp_b, "no-label-b", acs, sensor_index_int=2),
        ]
        key_view = object.__new__(SyncCablesView)
        cache.set(key_view.get_cache_key(acs, "links", SERVER_KEY), {"links": links}, timeout=300)
        picker_url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[acs.pk])
        _remote, _, (remote_port,) = make_serial_device(
            "picker-duplicate-remote-target",
            cp_names=["console"],
        )
        for link in links:
            response = client.post(
                picker_url,
                data={
                    "row_id": link["local_port_id"],
                    "server_key": SERVER_KEY,
                    "remote_interface_id": remote_port.pk,
                },
                HTTP_HX_REQUEST="true",
            )
            assert response.status_code == 200

        sync_url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk])
        post_data = _rendered_sync_data(
            client,
            acs,
            [link["local_port_id"] for link in links],
        )
        response = client.post(
            sync_url,
            data=post_data,
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert Cable.objects.count() == 0
        assert "same cable endpoint" in response.content.decode()

    def test_batch_rejects_an_endpoint_reused_across_local_and_remote_roles(self):
        from dcim.models import Cable
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        device = make_device("cable-cross-role")
        first = make_interface(device, "Ethernet1")
        shared = make_interface(device, "Ethernet2")
        last = make_interface(device, "Ethernet3")
        rows = [
            {
                "local_port": first.name,
                "local_port_id": "cross-role-1",
                "device_id": device.pk,
                "remote_device": device.name,
                "remote_port": shared.name,
                "netbox_local_interface_id": first.pk,
                "netbox_remote_interface_id": shared.pk,
                "netbox_remote_device_id": device.pk,
            },
            {
                "local_port": shared.name,
                "local_port_id": "cross-role-2",
                "device_id": device.pk,
                "remote_device": device.name,
                "remote_port": last.name,
                "netbox_local_interface_id": shared.pk,
                "netbox_remote_interface_id": last.pk,
                "netbox_remote_device_id": device.pk,
            },
        ]
        cache_key = object.__new__(SyncCablesView).get_cache_key(device, "links", SERVER_KEY)
        cache.set(cache_key, {"links": rows}, timeout=300)
        client = self._client("cross-role")
        post_data = _rendered_sync_data(
            client,
            device,
            [row["local_port_id"] for row in rows],
        )

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[device.pk]),
            data=post_data,
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert Cable.objects.count() == 0
        assert "same cable endpoint" in response.content.decode()

    def test_batch_rejects_two_rows_overridden_onto_one_vc_member_interface(self):
        """The uniqueness gate must resolve the local end exactly as the sync does.

        A member override re-resolves the local port by LibreNMS port id and by both name
        candidates. Two rows whose alternate name is the same port on the chosen member collapse
        onto one interface, so the batch must be refused whole — not applied row by row.
        """
        from dcim.models import Cable
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client("vc-collapse")
        page = make_device("cable-vc-collapse-page")
        member = make_device("cable-vc-collapse-member")
        make_virtual_chassis("cable-vc-collapse", page, member)
        make_interface(page, "Ethernet1")
        make_interface(page, "Gi1/0/1")
        # The member carries only ONE of the two names, so both rows land on it.
        member_local = make_interface(member, "Ethernet1")
        remote_device = make_device("cable-vc-collapse-remote")
        first_remote = make_interface(remote_device, "R1")
        second_remote = make_interface(remote_device, "R2")
        set_librenms_device_id(page, 700, SERVER_KEY)
        page.save()
        rows = [
            {
                "_source": "main",
                "local_port": "Ethernet1",
                "local_port_id": 11,
                "remote_device": remote_device.name,
                "remote_port": first_remote.name,
            },
            {
                "_source": "main",
                "local_port": "Gi1/0/1",
                "local_port_alt": "Ethernet1",
                "local_port_id": 12,
                "remote_device": remote_device.name,
                "remote_port": second_remote.name,
            },
        ]
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(page, "links", SERVER_KEY),
            {"links": rows, "snapshot_token": "vc-collapse"},
            timeout=300,
        )

        post_data = {
            "server_key": SERVER_KEY,
            "origin_device_id": page.pk,
            "select": ["11", "12"],
        }
        # What the member-verify repaint would post back: both rows now name the member's port.
        for row_id, remote in (("11", first_remote), ("12", second_remote)):
            post_data.update(
                {
                    f"device_selection_{row_id}": member.pk,
                    f"expected_local_id_{row_id}": member_local.pk,
                    f"expected_local_device_id_{row_id}": member.pk,
                    f"expected_remote_id_{row_id}": remote.pk,
                    f"expected_remote_device_id_{row_id}": remote_device.pk,
                }
            )

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[page.pk]),
            data=post_data,
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert "same cable endpoint" in response.content.decode()
        assert Cable.objects.count() == 0

    @pytest.mark.parametrize("malformed", [[None], "not-a-dict"])
    def test_picker_search_survives_a_malformed_cached_snapshot(self, malformed):
        """The search fragment never reads the snapshot, so a corrupt one cannot break it.

        Purging the corrupt entry is the job of the readers that consume it (see the modal
        render and the verify path); see also
        ``test_search_fragment_does_not_read_the_cable_snapshot``.
        """
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        device, (csp,), _ = make_serial_device("picker-malformed-cache", csp_names=["ttyS1"])
        target = make_device("picker-malformed-cache-target")
        cache_key = object.__new__(SyncCablesView).get_cache_key(device, "links", SERVER_KEY)
        cached = {"links": malformed} if isinstance(malformed, list) else malformed
        cache.set(cache_key, cached, timeout=300)
        client = self._client("malformed-cache")

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[device.pk]),
            {
                "row_id": f"serial:{csp.pk}",
                "server_key": SERVER_KEY,
                "action": "search",
                "q": target.name,
            },
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert target.name in response.content.decode()


# ---------------------------------------------------------------------------
# Re-pointing an EXISTING cable via the picker (always modal-confirmed)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestManualRepointOfExistingCable:
    """Cabled rows offer the picker too, and a manual re-point ALWAYS confirms through the
    warning modal, including a plugin-tagged cable. Every destructive replacement confirms the
    exact current cable and gets the visible trace plus the force checkbox.
    """

    def test_tagged_cabled_row_offers_the_picker(self):
        """A satisfied (tagged Cable Found) row still offers the pick-remote action so the cable can be changed."""
        acs, (csp,), _ = make_serial_device("repoint-aff", csp_names=["ttyS1"])
        _r, _, (cp,) = make_serial_device("repoint-aff-r", cp_names=["console"])
        cable = cable_together(csp, cp)
        cable.tags.add(_librenms_tag())

        link = _make_view().enrich_links_data([_serial_row(csp, "repoint-aff-r", acs)], acs, server_key=SERVER_KEY)[0]

        assert link["can_create_cable"] is False  # nothing to sync as-is
        assert link.get("picker_url")  # ...but the cable can be re-pointed

    def test_patch_path_row_offers_the_picker(self):
        """A Connected-via-Patch-Path row offers the picker (re-pointing replaces the whole path, modal-confirmed)."""
        acs, (csp,), _ = make_serial_device("repoint-path", csp_names=["ttyS1"])
        _panel_dev, fp, rp = make_patch_panel("repoint-path-pp")
        end, _, (cp,) = make_serial_device("repoint-path-end", cp_names=["console"])
        cable_together(csp, fp)
        cable_together(rp, cp)

        link = _make_view().enrich_links_data([_serial_row(csp, "repoint-path-end", acs)], acs, server_key=SERVER_KEY)[
            0
        ]

        assert link["cable_status"] == "Connected via Patch Path"
        assert link.get("picker_url")

    def test_patch_path_to_an_interface_does_not_satisfy_a_serial_target(self):
        """A serial label requires a ConsolePort, not any port on the named device."""
        acs, (csp,), _ = make_serial_device("repoint-path-type", csp_names=["ttyS1"])
        _panel_device, front_port, rear_port = make_patch_panel("repoint-path-type-panel")
        target, _, (console_port,) = make_serial_device(
            "repoint-path-type-target",
            cp_names=["console"],
        )
        target_interface = make_interface(target, "Ethernet1")
        cable_together(csp, front_port)
        cable_together(rear_port, target_interface)

        link = _make_view().enrich_links_data(
            [_serial_row(csp, target.name, acs)],
            acs,
            server_key=SERVER_KEY,
        )[0]

        assert link["cable_status"] == "Cable Mismatch"
        assert link["netbox_remote_interface_id"] == console_port.pk
        assert link["can_create_cable"] is True

    def test_pick_then_sync_pops_modal_then_force_replaces(self):
        """Full e2e: pick a new remote on a tagged-cabled row -> sync warns via the modal -> force replaces."""
        from dcim.models import Cable
        from django.urls import reverse

        client = self._client_e2e("repoint-e2e")
        acs, csp, old, link, picker_url = self._seed_cabled("repoint-e2e")
        _t, _, (new_cp,) = make_serial_device("repoint-e2e-target", cp_names=["console"])

        pick = client.post(
            picker_url,
            data={"row_id": link["local_port_id"], "server_key": SERVER_KEY, "remote_interface_id": new_cp.pk},
            HTTP_HX_REQUEST="true",
        )
        assert pick.status_code == 200

        sync_url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk])
        post_data = _rendered_sync_data(client, acs, link["local_port_id"])
        resp = client.post(
            sync_url,
            data=post_data,
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'id="cable-force-submit"' in content  # the warning modal
        # ...including the doomed cable's trace (the plan's core requirement for the modal).
        assert f"#{old.pk}" in content  # the cable segment label
        assert "console" in content  # the old far-end port appears in the hops
        assert Cable.objects.filter(pk=old.pk).exists()  # nothing destroyed yet

        from netbox_librenms_plugin.tests.test_cable_overwrite import _intent_from_modal

        expected_intent = _intent_from_modal(resp, link["local_port_id"])

        forced = client.post(
            sync_url,
            data={
                **post_data,
                "force": "on",
                f"expected_cable_intent_{link['local_port_id']}": expected_intent,
            },
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

        from netbox_librenms_plugin.tests.conftest import clear_test_cache
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client_e2e("expiry-post")
        acs, csp, _old, link, picker_url = self._seed_cabled("expiry-post")
        _t, _, (new_cp,) = make_serial_device("expiry-post-target", cp_names=["console"])
        acs.custom_field_data["librenms_id"] = {SERVER_KEY: 13}
        acs.save(update_fields=["custom_field_data"])

        clear_test_cache(cache)  # the snapshot expired between render and pick

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_serial_refetch_get(link),
        ) as external_get:
            resp = client.post(
                picker_url,
                data={"row_id": link["local_port_id"], "server_key": SERVER_KEY, "remote_interface_id": new_cp.pk},
                HTTP_HX_REQUEST="true",
            )

        refetched = cache.get(object.__new__(SyncCablesView).get_cache_key(acs, "links", SERVER_KEY))
        assert refetched is not None, [call.args[0] for call in external_get.call_args_list]
        assert resp.status_code == 200, resp.content.decode()
        assert new_cp.name in resp.content.decode()
        key_view = object.__new__(SyncCablesView)
        for configured_key in SyncCablesView().librenms_api.get_available_servers():
            cached = cache.get(key_view.get_cache_key(acs, "links", configured_key))
            if cached:
                assert all("manual_remote_id" not in row for row in cached["links"])

    def test_pick_with_unconfigured_server_key_is_rejected(self):
        """A stale server key must not reuse or refresh a removed server snapshot."""
        client = self._client_e2e("ghost-key")
        _acs, _csp, _old, link, picker_url = self._seed_cabled("ghost-key")
        _t, _, (new_cp,) = make_serial_device("ghost-key-target", cp_names=["console"])

        resp = client.post(
            picker_url,
            data={"row_id": link["local_port_id"], "server_key": "ghost", "remote_interface_id": new_cp.pk},
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 400

    def test_pick_modal_get_refetches_when_cache_expired(self):
        """Opening the picker after the snapshot expired re-fetches instead of a dead-end warning."""
        from unittest.mock import patch

        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import clear_test_cache
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client = self._client_e2e("expiry-get")
        acs, _csp, _old, link, picker_url = self._seed_cabled("expiry-get")
        acs.custom_field_data["librenms_id"] = {SERVER_KEY: 13}
        acs.save(update_fields=["custom_field_data"])

        clear_test_cache(cache)

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_serial_refetch_get(link),
        ) as external_get:
            resp = client.get(picker_url, {"row_id": link["local_port_id"], "server_key": SERVER_KEY})

        refetched = cache.get(object.__new__(SyncCablesView).get_cache_key(acs, "links", SERVER_KEY))
        assert refetched is not None, [call.args[0] for call in external_get.call_args_list]
        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'name="q"' in content, content  # the real picker, not the cache-expired warning
        assert "Cache has expired" not in content

    def test_pick_modal_get_rejects_an_unconfigured_server_key(self):
        """The picker modal must fail closed when its server was removed."""
        client = self._client_e2e("forged-get")
        _acs, _csp, _old, link, picker_url = self._seed_cabled("forged-get")

        resp = client.get(picker_url, {"row_id": link["local_port_id"], "server_key": "ghost"})

        assert resp.status_code == 400

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

        link = _serial_row(csp, f"router-{name}", acs, sensor_index_int=8)
        key_view = object.__new__(SyncCablesView)
        cache.set(key_view.get_cache_key(acs, "links", SERVER_KEY), {"links": [link]}, timeout=300)
        picker_url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[acs.pk])
        return acs, csp, old, link, picker_url


@pytest.mark.django_db
class TestRemotePickerObjectScope:
    """The picker enumerates devices and their ports, so every lookup must run through a restricted
    queryset.

    Its gate asks ``has_perm("dcim.view_device")`` without an instance, which a CONSTRAINED grant
    clears — a raw manager would then let that grant enumerate and bind terminations it cannot see.
    """

    @staticmethod
    def _scoped_client(
        name,
        device_names,
        *,
        write=False,
        view_console_ports=False,
        view_console_server_ports=False,
    ):
        """A real non-superuser with plugin access plus a view_device grant limited to *device_names*."""
        from core.models import ObjectType
        from dcim.models import ConsolePort, ConsoleServerPort, Device
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.test import Client
        from users.models import ObjectPermission

        LibreNMSSettings = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")

        user = get_user_model().objects.create_user(username=f"picker-scoped-{name}", password="pw")
        plugin = ObjectPermission.objects.create(
            name=f"{name}-plugin", actions=["view", "change"] if write else ["view"]
        )
        plugin.object_types.set([ObjectType.objects.get_for_model(LibreNMSSettings)])
        plugin.users.set([user])
        scope = ObjectPermission.objects.create(
            name=f"{name}-devices", actions=["view"], constraints={"name__in": list(device_names)}
        )
        scope.object_types.set([ObjectType.objects.get_for_model(Device)])
        scope.users.set([user])
        if view_console_ports or view_console_server_ports:
            port_scope = ObjectPermission.objects.create(name=f"{name}-console-ports", actions=["view"])
            port_models = [ConsoleServerPort]
            if view_console_ports:
                port_models.append(ConsolePort)
            port_scope.object_types.set([ObjectType.objects.get_for_model(model) for model in port_models])
            port_scope.users.set([user])

        client = Client()
        client.force_login(user)
        return client

    @staticmethod
    def _seed(name, label="name-that-matches-nothing"):
        """Cache a serial row for a real ACS device and return it with the picker URL."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, (csp,), _ = make_serial_device(f"acs-{name}", csp_names=["ttyS5"])
        link = _serial_row(csp, label, acs)
        link["local_port_id"] = f"serial:{csp.pk}-s"
        key_view = object.__new__(SyncCablesView)
        cache.set(key_view.get_cache_key(acs, "links", SERVER_KEY), {"links": [link]}, timeout=300)
        url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[acs.pk])
        return acs, link, url

    def test_modal_404s_for_a_device_outside_the_grant(self):
        _acs, link, url = self._seed("scope-modal")
        client = self._scoped_client("modal", ["some-other-device"])

        resp = client.get(url, {"row_id": link["local_port_id"], "server_key": SERVER_KEY})

        assert resp.status_code == 404

    def test_picker_rejects_a_serial_row_when_the_local_port_is_hidden(self):
        """A raw cached sensor row must not bypass current ConsoleServerPort view scope."""
        from dcim.models import ConsolePort, Device
        from django.test import Client

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        acs, link, url = self._seed("hidden-local-port", label="restricted-console-label")
        _remote, _, (remote_port,) = make_serial_device(
            "picker-hidden-local-target",
            cp_names=["console"],
        )
        user = make_user_with_perms(
            "picker-hidden-local-user",
            [("view", Device), ("view", ConsolePort)],
        )
        client = Client()
        client.force_login(user)

        modal = client.get(url, {"row_id": link["local_port_id"], "server_key": SERVER_KEY})
        picked = client.post(
            url,
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": remote_port.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        assert modal.status_code == 404
        assert picked.status_code == 404
        assert "restricted-console-label" not in modal.content.decode()

    def test_picker_rejects_a_normal_row_when_the_local_interface_is_hidden(self):
        """A raw cached LLDP row must not bypass current Interface view scope."""
        from dcim.models import Device, Interface
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local_device = make_device("picker-hidden-interface-local")
        local_interface = make_interface(local_device, "Ethernet1")
        remote_device = make_device("picker-hidden-interface-remote")
        remote_interface = make_interface(remote_device, "Ethernet9")
        row = {
            "link_id": 10,
            "_source": "main",
            "local_port_id": 100,
            "local_port": local_interface.name,
            "remote_device": "restricted-neighbor-label",
            "remote_port": remote_interface.name,
        }
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(local_device, "links", SERVER_KEY),
            {"links": [row], "snapshot_token": "hidden-interface-picker"},
            timeout=300,
        )
        user = make_user_with_perms(
            "picker-hidden-interface-user",
            [("view", Device)],
        )
        user = grant(user, "view", Interface, constraints={"pk": remote_interface.pk})
        client = Client()
        client.force_login(user)
        url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local_device.pk])

        modal = client.get(url, {"row_id": "100", "server_key": SERVER_KEY})
        picked = client.post(
            url,
            {
                "row_id": "100",
                "server_key": SERVER_KEY,
                "remote_interface_id": remote_interface.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        assert modal.status_code == 404
        assert picked.status_code == 404
        assert "restricted-neighbor-label" not in modal.content.decode()

    def test_plugin_view_only_user_does_not_receive_a_picker_action(self):
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        remote, _, _ = make_serial_device("picker-view-only-target", cp_names=["console"])
        acs, link, _url = self._seed("view-only-picker", label=remote.name)
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(acs, "links", server_key),
            {"links": [link]},
            timeout=300,
        )
        client = self._scoped_client(
            "view-only-picker",
            [acs.name, remote.name],
            view_console_ports=True,
        )

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[acs.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert response.status_code == 200
        table_html = response.context["cable_sync"]["table"].as_html(response.wsgi_request)
        assert "Pick remote end" not in table_html
        assert "cable-remote-picker" not in table_html

    def test_visible_but_unchangeable_serial_row_is_not_actionable(self):
        """Display access alone must not render a cable action that the writer will deny."""
        from core.models import ObjectType
        from dcim.models import Cable, ConsolePort, ConsoleServerPort, Device
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse
        from users.models import ObjectPermission

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local, (csp,), _ = make_serial_device("picker-view-only-local", csp_names=["ttyS1"])
        remote, _, (cp,) = make_serial_device("picker-view-only-remote", cp_names=["console"])
        row = _serial_row(csp, remote.name, local)
        row["local_port_id"] = "serial:view-only"
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cache.set(cache_key, {"links": [row]}, timeout=300)

        user = get_user_model().objects.create_user("picker-view-only-user")
        grants = [
            (apps.get_model("netbox_librenms_plugin", "LibreNMSSettings"), ["view", "change"]),
            (Device, ["view"]),
            (ConsoleServerPort, ["view"]),
            (ConsolePort, ["view"]),
            (Cable, ["view", "add", "change"]),
        ]
        for index, (model, actions) in enumerate(grants):
            permission = ObjectPermission.objects.create(
                name=f"picker-view-only-{index}",
                actions=actions,
            )
            permission.object_types.set([ObjectType.objects.get_for_model(model)])
            permission.users.set([user])

        client = Client()
        client.force_login(user)
        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        record = next(iter(rendered.context["cable_sync"]["table"].rows)).record
        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            {
                "sync_one": row["local_port_id"],
                "server_key": server_key,
                f"expected_local_id_{row['local_port_id']}": record["netbox_local_interface_id"],
                f"expected_local_device_id_{row['local_port_id']}": record["netbox_local_device_id"],
                f"expected_remote_id_{row['local_port_id']}": record["netbox_remote_interface_id"],
                f"expected_remote_device_id_{row['local_port_id']}": record["netbox_remote_device_id"],
            },
        )

        assert rendered.status_code == 200
        table_html = rendered.context["cable_sync"]["table"].as_html(rendered.wsgi_request)
        assert csp.name in table_html
        assert 'name="sync_one"' not in table_html
        assert synced.status_code == 302
        csp.refresh_from_db()
        cp.refresh_from_db()
        assert csp.cable_id is None
        assert cp.cable_id is None

    def test_serial_auto_pick_skips_a_view_only_console_port(self):
        """Allocation must prefer a later actionable port over an earlier view-only port."""
        from dcim.models import Cable, ConsolePort, ConsoleServerPort, Device
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local, (csp,), _ = make_serial_device("picker-actionable-local", csp_names=["ttyS1"])
        remote, _, (view_only, actionable) = make_serial_device(
            "picker-actionable-remote",
            cp_names=["console-a", "console-b"],
        )
        row = _serial_row(csp, remote.name, local)
        row["local_port_id"] = "serial:actionable-port"
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(local, "links", server_key),
            {"links": [row]},
            timeout=300,
        )
        user = make_user_with_perms(
            "picker-actionable-user",
            [
                ("view", Device),
                ("view", ConsoleServerPort),
                ("change", ConsoleServerPort),
                ("view", ConsolePort),
                ("view", Cable),
                ("add", Cable),
                ("change", Cable),
            ],
        )
        user = grant(user, "change", ConsolePort, constraints={"pk": actionable.pk})
        client = Client()
        client.force_login(user)

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert response.status_code == 200
        record = next(iter(response.context["cable_sync"]["table"].rows)).record
        assert record["netbox_remote_interface_id"] == actionable.pk
        assert record["netbox_remote_interface_id"] != view_only.pk
        assert record["can_create_cable"] is True

    def test_manual_pick_on_an_unchangeable_source_does_not_reserve_the_port(self):
        """A dead manual row must not starve an actionable auto-matched sibling row."""
        from dcim.models import Cable, ConsolePort, ConsoleServerPort, Device
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local, (actionable_csp, view_only_csp), _ = make_serial_device(
            "picker-dead-reservation-local",
            csp_names=["ttyS1", "ttyS2"],
        )
        remote, _, (cp,) = make_serial_device("picker-dead-reservation-remote", cp_names=["console"])
        auto_row = _serial_row(actionable_csp, remote.name, local, sensor_index_int=1)
        auto_row["local_port_id"] = "serial:auto-actionable"
        manual_row = _serial_row(view_only_csp, remote.name, local, sensor_index_int=2)
        manual_row["local_port_id"] = "serial:manual-view-only"
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cache.set(
            cache_key,
            {"links": [auto_row, manual_row], "snapshot_token": "dead-reservation"},
            timeout=300,
        )
        user = make_user_with_perms(
            "picker-dead-reservation-user",
            [
                ("view", Device),
                ("view", ConsoleServerPort),
                ("view", ConsolePort),
                ("change", ConsolePort),
                ("view", Cable),
                ("add", Cable),
                ("change", Cable),
            ],
        )
        user = grant(user, "change", ConsoleServerPort, constraints={"pk": actionable_csp.pk})
        client = Client()
        client.force_login(user)
        picker_url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[local.pk])
        picked = client.post(
            picker_url,
            {
                "row_id": manual_row["local_port_id"],
                "server_key": server_key,
                "remote_interface_id": cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )
        assert picked.status_code == 200

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert response.status_code == 200
        records = {row.record["row_id"]: row.record for row in response.context["cable_sync"]["table"].rows}
        assert records[auto_row["local_port_id"]]["netbox_remote_interface_id"] == cp.pk
        assert records[auto_row["local_port_id"]]["can_create_cable"] is True
        assert records[manual_row["local_port_id"]]["can_create_cable"] is False

    def test_first_cable_sync_bootstraps_the_provenance_tag(self):
        """Cable permissions alone carry the first sync: the tag is plugin infrastructure.

        Its name and color come from the plugin settings row, so the plugin creates it on first
        use the same way it creates the ``librenms_id`` custom field. Charging the first syncing
        user for ``extras.add_tag`` would make cable sync fail for exactly one operator and
        contradicts the documented permission set (docs/usage_tips/permissions.md).
        """
        from core.models import ObjectType
        from dcim.models import Cable, ConsolePort, ConsoleServerPort, Device
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse
        from extras.models import Tag
        from users.models import ObjectPermission

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local, (csp,), _ = make_serial_device("picker-no-tag-add-local", csp_names=["ttyS1"])
        remote, _, (cp,) = make_serial_device("picker-no-tag-add-remote", cp_names=["console"])
        row = _serial_row(csp, remote.name, local)
        row["local_port_id"] = "serial:no-tag-add"
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(local, "links", server_key),
            {"links": [row]},
            timeout=300,
        )
        Tag.objects.filter(name="librenms").delete()
        user = get_user_model().objects.create_user("picker-no-tag-add-user")
        grants = [
            (apps.get_model("netbox_librenms_plugin", "LibreNMSSettings"), ["view", "change"]),
            (Device, ["view"]),
            (ConsoleServerPort, ["view", "change"]),
            (ConsolePort, ["view", "change"]),
            (Cable, ["view", "add", "change"]),
        ]
        for index, (model, actions) in enumerate(grants):
            permission = ObjectPermission.objects.create(name=f"picker-no-tag-add-{index}", actions=actions)
            permission.object_types.set([ObjectType.objects.get_for_model(model)])
            permission.users.set([user])
        client = Client()
        client.force_login(user)
        post_data = _rendered_sync_data(client, local, row["local_port_id"], server_key)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            post_data,
        )

        assert response.status_code == 302
        csp.refresh_from_db()
        cp.refresh_from_db()
        assert csp.cable_id is not None
        assert csp.cable_id == cp.cable_id
        tag = Tag.objects.get(name="librenms")
        assert list(Cable.objects.get(pk=csp.cable_id).tags.all()) == [tag]

    def test_verify_uses_the_selected_member_and_hides_an_unauthorized_action(self):
        """The VC repaint and writer must use the same permission-scoped termination."""
        import json

        from core.models import ObjectType
        from dcim.models import Cable, Device, Interface
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse
        from users.models import ObjectPermission

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        first = make_device("picker-verify-scope-first")
        second = make_device("picker-verify-scope-second")
        make_virtual_chassis("picker-verify-scope-vc", first, second)
        first.custom_field_data["librenms_id"] = {SERVER_KEY: 500}
        first.save()
        first_local = make_interface(first, "Ethernet1")
        second_local = make_interface(second, "Ethernet1")
        remote_device = make_device("picker-verify-scope-remote")
        remote = make_interface(remote_device, "Ethernet9")
        row = {
            "local_port": "Ethernet1",
            "local_port_id": 10,
            "remote_device": remote_device.name,
            "remote_port": remote.name,
            "remote_port_id": 20,
            "_source": "main",
        }
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(first, "links", server_key)
        cache.set(cache_key, {"links": [row], "snapshot_token": "verify-member-scope"}, timeout=300)

        user = get_user_model().objects.create_user("picker-verify-scope-user")
        grants = [
            (apps.get_model("netbox_librenms_plugin", "LibreNMSSettings"), ["view", "change"], None),
            (Device, ["view"], None),
            (Interface, ["view"], None),
            (Interface, ["change"], {"id__in": [first_local.pk, remote.pk]}),
            (Cable, ["view", "add", "change"], None),
        ]
        for index, (model, actions, constraints) in enumerate(grants):
            permission = ObjectPermission.objects.create(
                name=f"picker-verify-scope-{index}",
                actions=actions,
                constraints=constraints,
            )
            permission.object_types.set([ObjectType.objects.get_for_model(model)])
            permission.users.set([user])

        client = Client()
        client.force_login(user)
        verified = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            data=json.dumps({"device_id": second.pk, "row_id": "10", "server_key": server_key}),
            content_type="application/json",
        )
        formatted_row = verified.json()["formatted_row"]
        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[first.pk]),
            {
                "sync_one": "10",
                "device_selection_10": second.pk,
                "server_key": server_key,
                "expected_local_id_10": formatted_row["expected_local_id"],
                "expected_local_device_id_10": formatted_row["expected_local_device_id"],
                "expected_remote_id_10": formatted_row["expected_remote_id"],
                "expected_remote_device_id_10": formatted_row["expected_remote_device_id"],
            },
        )

        assert verified.status_code == 200
        assert reverse("dcim:interface", args=[second_local.pk]) in formatted_row["local_port"]
        assert reverse("dcim:interface", args=[first_local.pk]) not in formatted_row["local_port"]
        assert formatted_row["can_create_cable"] is False
        assert "Sync Cable" not in formatted_row["actions"]
        assert synced.status_code == 302
        first_local.refresh_from_db()
        second_local.refresh_from_db()
        remote.refresh_from_db()
        assert first_local.cable_id is None
        assert second_local.cable_id is None
        assert remote.cable_id is None

    def test_search_lists_only_devices_inside_the_grant(self):
        acs, link, url = self._seed("scope-search")
        make_device("picker-scope-visible")
        make_device("picker-scope-hidden")
        client = self._scoped_client("search", [acs.name, "picker-scope-visible"])

        resp = client.get(
            url,
            {"row_id": link["local_port_id"], "server_key": SERVER_KEY, "action": "search", "q": "picker-scope"},
        )

        assert resp.status_code == 200
        content = resp.content.decode()
        assert "picker-scope-visible" in content
        assert "picker-scope-hidden" not in content

    def test_ports_fragment_404s_for_a_device_outside_the_grant(self):
        acs, link, url = self._seed("scope-ports")
        hidden, _, (cp,) = make_serial_device("picker-scope-ports-hidden", cp_names=["console-H"])
        client = self._scoped_client("ports", [acs.name])

        resp = client.get(
            url,
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "source": "serial",
                "action": "ports",
                "device_id": hidden.pk,
            },
        )

        assert resp.status_code == 404
        assert cp.name not in resp.content.decode()

    def test_post_refuses_a_port_on_a_device_outside_the_grant(self):
        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, link, url = self._seed("scope-post")
        _hidden, _, (cp,) = make_serial_device("picker-scope-post-hidden", cp_names=["console"])
        client = self._scoped_client(
            "post",
            [acs.name],
            write=True,
            view_console_ports=True,
        )

        resp = client.post(
            url,
            data={
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 400
        key_view = object.__new__(SyncCablesView)
        cached = cache.get(key_view.get_cache_key(acs, "links", SERVER_KEY))
        row = next(r for r in cached["links"] if r["local_port_id"] == link["local_port_id"])
        assert "manual_remote_id" not in row  # nothing bound

    def test_ports_fragment_requires_console_port_view_permission(self):
        acs, link, url = self._seed("scope-port-model")
        remote, _, (cp,) = make_serial_device("picker-scope-port-target", cp_names=["hidden-console"])
        client = self._scoped_client("port-model", [acs.name, remote.name])

        resp = client.get(
            url,
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "source": "serial",
                "action": "ports",
                "device_id": remote.pk,
            },
        )

        assert resp.status_code == 403
        assert cp.name not in resp.content.decode()

    def test_ports_fragment_hides_occupancy_without_cable_view_permission(self):
        acs, link, url = self._seed("scope-cable-state")
        remote, _, (free_cp, cabled_cp) = make_serial_device(
            "picker-scope-cable-state-target",
            cp_names=["console-free", "console-hidden-cable"],
        )
        _other, (other_csp,), _ = make_serial_device(
            "picker-scope-cable-state-other",
            csp_names=["ttyS1"],
        )
        cable_together(other_csp, cabled_cp)
        client = self._scoped_client(
            "cable-state",
            [acs.name, remote.name],
            view_console_ports=True,
        )

        response = client.get(
            url,
            {
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "source": "serial",
                "action": "ports",
                "device_id": remote.pk,
            },
        )

        assert response.status_code == 200
        content = response.content.decode()
        assert free_cp.name in content
        assert cabled_cp.name not in content

    def test_post_requires_console_port_view_permission(self):
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import cable_manual_pick_cache_key, cable_snapshot_token
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, link, url = self._seed("scope-port-post")
        remote, _, (cp,) = make_serial_device("picker-scope-port-post-target", cp_names=["hidden-console"])
        client = self._scoped_client(
            "port-post",
            [acs.name, remote.name],
            write=True,
            view_console_server_ports=True,
        )

        resp = client.post(
            url,
            data={
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        key_view = object.__new__(SyncCablesView)
        snapshot_key = key_view.get_cache_key(acs, "links", SERVER_KEY)
        cached = cache.get(snapshot_key)
        pick_key = cable_manual_pick_cache_key(
            snapshot_key,
            cable_snapshot_token(cached),
            client.session["_auth_user_id"],
            link["local_port_id"],
        )
        assert resp.status_code == 400
        assert cp.name not in resp.content.decode()
        assert cache.get(pick_key) is None

    def test_in_scope_pick_still_works(self):
        """The device the grant DOES cover still picks — the scoping must not over-block."""
        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, link, url = self._seed("scope-ok")
        remote, _, (cp,) = make_serial_device("picker-scope-ok-target", cp_names=["console"])
        client = self._scoped_client(
            "ok",
            [acs.name, remote.name],
            write=True,
            view_console_ports=True,
        )

        resp = client.post(
            url,
            data={
                "row_id": link["local_port_id"],
                "server_key": SERVER_KEY,
                "remote_interface_id": cp.pk,
            },
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 200
        key_view = object.__new__(SyncCablesView)
        cached = cache.get(key_view.get_cache_key(acs, "links", SERVER_KEY))
        row = next(r for r in cached["links"] if r["local_port_id"] == link["local_port_id"])
        assert "manual_remote_id" not in row

    def test_picker_declares_the_device_view_gate(self):
        """A missing view_device grant must be refused by the declared gate, not by a 404 at the lookup."""
        from dcim.models import Device

        from netbox_librenms_plugin.views.base.cables_view import CableRemotePickerView
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        assert NetBoxObjectPermissionMixin in CableRemotePickerView.__mro__
        for method in ("GET", "POST"):
            assert ("view", Device) in CableRemotePickerView.required_object_permissions[method]


@pytest.mark.django_db
class TestNormalCableLinkObjectScope:
    """Normal VC cable rows must not expose Interfaces on hidden Device owners."""

    def test_hidden_vc_member_is_absent_from_the_row_and_member_selector(self):
        from core.models import ObjectType
        from dcim.models import Device, Interface
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse
        from users.models import ObjectPermission

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        visible = make_device("normal-scope-visible")
        hidden = make_device("normal-scope-hidden")
        make_virtual_chassis("normal-scope-vc", visible, hidden)
        hidden_local = make_interface(hidden, "Ethernet2")
        remote_device = make_device("normal-scope-remote")
        remote = make_interface(remote_device, "Ethernet9")
        row = {
            "_source": "main",
            "local_port": hidden_local.name,
            "local_port_id": 10,
            "remote_device": remote_device.name,
            "remote_port": remote.name,
            "remote_port_id": 20,
        }
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(visible, "links", server_key),
            {"links": [row], "snapshot_token": "normal-hidden-owner"},
            timeout=300,
        )

        user = get_user_model().objects.create_user("normal-scope-user")
        plugin_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        plugin_permission = ObjectPermission.objects.create(name="normal-scope-plugin", actions=["view"])
        plugin_permission.object_types.add(ObjectType.objects.get_for_model(plugin_model))
        plugin_permission.users.add(user)
        device_permission = ObjectPermission.objects.create(
            name="normal-scope-devices",
            actions=["view"],
            constraints={"pk__in": [visible.pk, remote_device.pk]},
        )
        device_permission.object_types.add(ObjectType.objects.get_for_model(Device))
        device_permission.users.add(user)
        interface_permission = ObjectPermission.objects.create(
            name="normal-scope-interfaces",
            actions=["view"],
        )
        interface_permission.object_types.add(ObjectType.objects.get_for_model(Interface))
        interface_permission.users.add(user)
        client = Client()
        client.force_login(user)

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[visible.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert response.status_code == 200
        table = response.context["cable_sync"]["table"]
        record = next(iter(table.rows)).record
        table_html = table.as_html(response.wsgi_request)
        assert record.get("netbox_local_interface_id") is None
        assert reverse("dcim:interface", args=[hidden_local.pk]) not in table_html
        assert hidden.name not in table_html
        assert f'value="{hidden.pk}"' not in table_html

    def test_verify_does_not_repaint_a_remote_interface_on_a_hidden_vc_member(self):
        import json

        from core.models import ObjectType
        from dcim.models import Device, Interface
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse
        from users.models import ObjectPermission

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local_device = make_device("normal-verify-scope-local")
        local = make_interface(local_device, "Ethernet1")
        remote_visible = make_device("normal-verify-scope-remote-visible")
        remote_hidden = make_device("normal-verify-scope-remote-hidden")
        make_virtual_chassis("normal-verify-scope-vc", remote_visible, remote_hidden)
        remote = make_interface(remote_hidden, "ge-2/0/0")
        row = {
            "_source": "main",
            "local_port": local.name,
            "local_port_id": 10,
            "remote_device": remote_visible.name,
            "remote_port": remote.name,
            "remote_port_id": 20,
        }
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(local_device, "links", server_key),
            {"links": [row], "snapshot_token": "normal-verify-hidden-owner"},
            timeout=300,
        )

        user = get_user_model().objects.create_user("normal-verify-scope-user")
        plugin_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        plugin_permission = ObjectPermission.objects.create(name="normal-verify-scope-plugin", actions=["view"])
        plugin_permission.object_types.add(ObjectType.objects.get_for_model(plugin_model))
        plugin_permission.users.add(user)
        device_permission = ObjectPermission.objects.create(
            name="normal-verify-scope-devices",
            actions=["view"],
            constraints={"pk__in": [local_device.pk, remote_visible.pk]},
        )
        device_permission.object_types.add(ObjectType.objects.get_for_model(Device))
        device_permission.users.add(user)
        interface_permission = ObjectPermission.objects.create(
            name="normal-verify-scope-interfaces",
            actions=["view"],
        )
        interface_permission.object_types.add(ObjectType.objects.get_for_model(Interface))
        interface_permission.users.add(user)
        client = Client()
        client.force_login(user)

        initial = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local_device.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        initial_record = next(iter(initial.context["cable_sync"]["table"].rows)).record
        verified = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            data=json.dumps(
                {
                    "device_id": local_device.pk,
                    "origin_device_id": local_device.pk,
                    "row_id": initial_record["row_id"],
                    "server_key": server_key,
                }
            ),
            content_type="application/json",
        )

        assert initial_record.get("netbox_remote_interface_id") is None
        assert verified.status_code == 200
        formatted = json.dumps(verified.json()["formatted_row"])
        assert reverse("dcim:interface", args=[remote.pk]) not in formatted
        assert reverse("dcim:device", args=[remote_hidden.pk]) not in formatted
        assert remote_hidden.name not in formatted

    def test_writer_does_not_fall_back_past_an_unchangeable_stable_id_match(self):
        from core.models import ObjectType
        from dcim.models import Cable, Device, Interface
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse
        from users.models import ObjectPermission

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import get_librenms_cable_tag, set_librenms_device_id
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        first = make_device("normal-writer-scope-first")
        selected = make_device("normal-writer-scope-selected")
        make_virtual_chassis("normal-writer-scope-vc", first, selected)
        first_local = make_interface(first, "Ethernet1")
        hidden_stable = make_interface(selected, "stable-owner")
        changeable_fallback = make_interface(selected, "Ethernet1")
        remote_device = make_device("normal-writer-scope-remote")
        remote = make_interface(remote_device, "Ethernet9")
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(first_local, 10, server_key)
        first_local.save()
        set_librenms_device_id(hidden_stable, 10, server_key)
        hidden_stable.save()
        row = {
            "_source": "main",
            "local_port": "Ethernet1",
            "local_port_id": 10,
            "remote_device": remote_device.name,
            "remote_port": remote.name,
            "remote_port_id": 20,
        }
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(first, "links", server_key),
            {"links": [row], "snapshot_token": "normal-writer-scope"},
            timeout=300,
        )
        get_librenms_cable_tag()

        user = get_user_model().objects.create_user("normal-writer-scope-user")
        plugin_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        plugin_permission = ObjectPermission.objects.create(
            name="normal-writer-scope-plugin",
            actions=["view", "change"],
        )
        plugin_permission.object_types.add(ObjectType.objects.get_for_model(plugin_model))
        plugin_permission.users.add(user)
        device_permission = ObjectPermission.objects.create(
            name="normal-writer-scope-devices",
            actions=["view"],
        )
        device_permission.object_types.add(ObjectType.objects.get_for_model(Device))
        device_permission.users.add(user)
        interface_view = ObjectPermission.objects.create(
            name="normal-writer-scope-interface-view",
            actions=["view"],
        )
        interface_view.object_types.add(ObjectType.objects.get_for_model(Interface))
        interface_view.users.add(user)
        interface_change = ObjectPermission.objects.create(
            name="normal-writer-scope-interface-change",
            actions=["change"],
            constraints={"pk__in": [first_local.pk, changeable_fallback.pk, remote.pk]},
        )
        interface_change.object_types.add(ObjectType.objects.get_for_model(Interface))
        interface_change.users.add(user)
        cable_permission = ObjectPermission.objects.create(
            name="normal-writer-scope-cables",
            actions=["view", "add", "change"],
        )
        cable_permission.object_types.add(ObjectType.objects.get_for_model(Cable))
        cable_permission.users.add(user)
        client = Client()
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[first.pk]),
            {
                "sync_one": "10",
                "origin_device_id": first.pk,
                "device_selection_10": selected.pk,
                "expected_local_id_10": changeable_fallback.pk,
                "expected_local_device_id_10": selected.pk,
                "expected_remote_id_10": remote.pk,
                "expected_remote_device_id_10": remote_device.pk,
                "server_key": server_key,
            },
        )

        assert response.status_code == 302
        changeable_fallback.refresh_from_db()
        remote.refresh_from_db()
        assert changeable_fallback.cable_id is None
        assert remote.cable_id is None

    def test_duplicate_local_stable_ids_are_not_rendered_or_synced(self):
        """A corrupt duplicate binding must not choose one local Interface by query order."""
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse
        from dcim.models import Cable

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local_device = make_device("normal-duplicate-id-local")
        first = make_interface(local_device, "Ethernet1")
        second = make_interface(local_device, "Ethernet2")
        remote_device = make_device("normal-duplicate-id-remote")
        remote = make_interface(remote_device, "Ethernet9")
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        for interface in (first, second):
            set_librenms_device_id(interface, 10, server_key)
            interface.save()
        set_librenms_device_id(remote, 20, server_key)
        remote.save()
        row = {
            "_source": "main",
            "local_port": first.name,
            "local_port_id": 10,
            "remote_device": remote_device.name,
            "remote_port": remote.name,
            "remote_port_id": 20,
        }
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(local_device, "links", server_key),
            {"links": [row], "snapshot_token": "normal-duplicate-id"},
            timeout=300,
        )
        user = get_user_model().objects.create_superuser("normal-duplicate-id-user", "", "pw")
        client = Client()
        client.force_login(user)

        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local_device.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert rendered.status_code == 200
        record = next(iter(rendered.context["cable_sync"]["table"].rows)).record
        assert record.get("netbox_local_interface_id") is None
        assert record.get("can_create_cable") is False

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local_device.pk]),
            {
                "sync_one": record["row_id"],
                f"expected_local_id_{record['row_id']}": first.pk,
                f"expected_local_device_id_{record['row_id']}": local_device.pk,
                f"expected_remote_id_{record['row_id']}": remote.pk,
                f"expected_remote_device_id_{record['row_id']}": remote_device.pk,
                "server_key": server_key,
            },
        )

        assert synced.status_code == 302
        assert Cable.objects.count() == 0


@pytest.mark.django_db
class TestCablePathReadScope:
    """Cable status must not reveal a hidden segment in a traced patch path."""

    def test_hidden_middle_cable_makes_the_path_state_unavailable(self, client):
        from core.models import ObjectType
        from dcim.models import Cable, Device, FrontPort, Interface, RearPort
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.urls import reverse
        from users.models import ObjectPermission

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local_device = make_device("path-scope-local")
        local = make_interface(local_device, "Ethernet1")
        _panel_a, front_a, rear_a = make_patch_panel("path-scope-panel-a")
        _panel_b, front_b, rear_b = make_patch_panel("path-scope-panel-b")
        remote_device = make_device("path-scope-remote")
        remote = make_interface(remote_device, "Ethernet9")
        first = cable_together(local, front_a)
        middle = cable_together(rear_a, front_b)
        last = cable_together(rear_b, remote)

        user = get_user_model().objects.create_user("path-scope-user")

        def grant(name, model, pks):
            permission = ObjectPermission.objects.create(
                name=name,
                actions=["view"],
                constraints={"pk__in": list(pks)},
            )
            permission.object_types.add(ObjectType.objects.get_for_model(model))
            permission.users.add(user)

        plugin_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        plugin_permission = ObjectPermission.objects.create(name="path-scope-plugin", actions=["view"])
        plugin_permission.object_types.add(ObjectType.objects.get_for_model(plugin_model))
        plugin_permission.users.add(user)
        grant("path-scope-devices", Device, Device.objects.values_list("pk", flat=True))
        grant("path-scope-interfaces", Interface, [local.pk, remote.pk])
        grant("path-scope-front", FrontPort, [front_a.pk, front_b.pk])
        grant("path-scope-rear", RearPort, [rear_a.pk, rear_b.pk])
        grant("path-scope-cables", Cable, [first.pk, last.pk])
        assert middle.pk not in [first.pk, last.pk]

        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(local_device, "links", server_key)
        cache.set(
            cache_key,
            {
                "links": [
                    {
                        "local_port": local.name,
                        "local_port_id": 10,
                        "remote_device": remote_device.name,
                        "remote_port": remote.name,
                        "remote_port_id": 20,
                        "_source": "main",
                    }
                ]
            },
            timeout=300,
        )
        client.force_login(user)

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local_device.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert response.status_code == 200
        table = response.context["cable_sync"]["table"]
        content = table.as_html(response.wsgi_request)
        assert "Cable State Not Available" in content
        assert "Connected via Patch Path" not in content
