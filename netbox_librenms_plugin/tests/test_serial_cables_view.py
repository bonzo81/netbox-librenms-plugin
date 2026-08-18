"""
Tests for serial-port integration in BaseCableTableView.

Covers:
  - get_links_data() appends serial rows when device has ConsoleServerPorts
  - enrich_local_port() resolves ConsoleServerPort for serial rows
  - check_serial_cable_status() reports correct cable state
  - enrich_links_data() routes serial rows through check_serial_cable_status
  - _raw_keys in _prepare_context preserves serial-specific fields
  - LibreNMSCableTable.render_local_port() shows Serial badge
  - LibreNMSCableTable.render_remote_device() dims unconfigured serial ports
"""

from unittest.mock import patch

import pytest

# Shared real-DB builders (see tests/conftest.py).
from netbox_librenms_plugin.tests.conftest import cable_together, make_serial_device


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(path="/plugins/librenms/device/1/cables/"):
    """Build an authenticated Django request for direct ORM integration checks."""
    from uuid import uuid4

    from django.contrib.auth import get_user_model
    from django.test import RequestFactory

    request = RequestFactory().get(path)
    request.user = get_user_model().objects.create_superuser(
        username=f"serial-view-{uuid4().hex}",
        password="pw",
    )
    return request


def _make_view():
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    view = object.__new__(BaseCableTableView)
    view.request = _make_request()
    view.librenms_id = 12
    view._librenms_api = LibreNMSAPI(server_key="default")
    return view


# ---------------------------------------------------------------------------
# enrich_local_port for serial
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEnrichLocalPortSerial:
    """enrich_local_port resolves ConsoleServerPort for serial rows against a real device."""

    def test_csp_found_sets_url_and_id(self):
        """When a CSP exists by name on the device, its id and real URL are set on the link."""
        view = _make_view()
        obj, (csp,), _ = make_serial_device("ser-enrich-found", csp_names=["ttyS7"])

        link = {"local_port": "ttyS7", "local_port_id": "serial:1007", "_source": "serial"}
        view.enrich_local_port(link, obj)

        assert link["netbox_local_interface_id"] == csp.pk
        # Real reverse("dcim:consoleserverport", args=[pk]) → URL containing the CSP pk.
        assert str(csp.pk) in link["local_port_url"]

    def test_csp_not_found_no_url_set(self):
        """When no CSP matches the serial port name, no URL or id is set."""
        view = _make_view()
        obj, _, _ = make_serial_device("ser-enrich-miss", csp_names=["ttyS1"])

        link = {"local_port": "ttyS99", "local_port_id": "serial:1099", "_source": "serial"}
        view.enrich_local_port(link, obj)

        assert "local_port_url" not in link
        assert "netbox_local_interface_id" not in link

    def test_serial_resolves_via_csp_not_interface(self):
        """A serial row must resolve through ConsoleServerPorts, never Interfaces."""
        from dcim.models import Interface

        view = _make_view()
        obj, _, _ = make_serial_device("ser-enrich-iface")
        # A same-named Interface exists, but the serial path must not pick it up.
        Interface.objects.create(device=obj, name="ttyS3", type="other")

        link = {"local_port": "ttyS3", "local_port_id": "serial:1003", "_source": "serial"}
        view.enrich_local_port(link, obj)

        assert "local_port_url" not in link
        assert "netbox_local_interface_id" not in link

    def test_csp_resolved_on_sync_device_not_viewed_obj(self):
        """On a VC-member page the CSP lives on the resolved sync device; enrich_local_port must resolve it there, not on the viewed obj (which would drop the row to 'Console Server Port Not Found')."""
        from netbox_librenms_plugin.tests.conftest import configured_server_key, make_virtual_chassis
        from netbox_librenms_plugin.utils import set_librenms_device_id

        view = _make_view()
        # The viewed member has NO CSP; the sync device (priority member) owns it.
        viewed, _, _ = make_serial_device("ser-enrich-viewed")
        sync_device, (csp,), _ = make_serial_device("ser-enrich-sync", csp_names=["ttyS5"])
        make_virtual_chassis("ser-enrich-vc", viewed, sync_device)
        set_librenms_device_id(sync_device, view.librenms_id, configured_server_key())
        sync_device.save()

        link = {"local_port": "ttyS5", "local_port_id": "serial:1005", "_source": "serial"}
        view.enrich_local_port(link, viewed)

        # Resolved against the sync device, not the viewed obj.
        assert link["netbox_local_interface_id"] == csp.pk
        assert str(csp.pk) in link["local_port_url"]


# ---------------------------------------------------------------------------
# check_serial_cable_status
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCheckSerialCableStatus:
    """check_serial_cable_status sets correct cable_status and can_create_cable."""

    def test_no_csp_id_not_found(self):
        """Missing netbox_local_interface_id -> 'Console Server Port Not Found'."""
        view = _make_view()
        link = {"_source": "serial"}
        view.check_serial_cable_status(link)
        assert link["cable_status"] == "Console Server Port Not Found in NetBox"
        assert link["can_create_cable"] is False

    def test_csp_with_cable(self):
        """A real cabled CSP -> 'Cable Found' with the real cable_url."""
        view = _make_view()
        dev, (csp,), _ = make_serial_device("ser-csp-cabled", csp_names=["ttyS7"])
        _, _, (cp,) = make_serial_device("ser-cp-peer", cp_names=["con0"])
        cable = cable_together(csp, cp)

        link = {"_source": "serial", "netbox_local_interface_id": csp.pk}
        view.check_serial_cable_status(link)

        assert link["cable_status"] == "Cable Found"
        assert str(cable.pk) in link["cable_url"]
        assert link["can_create_cable"] is False

    def test_csp_without_cable(self):
        """A real uncabled CSP -> 'No Cable'."""
        view = _make_view()
        dev, (csp,), _ = make_serial_device("ser-csp-nocable", csp_names=["ttyS7"])

        link = {"_source": "serial", "netbox_local_interface_id": csp.pk}
        view.check_serial_cable_status(link)

        assert link["cable_status"] == "No Cable"
        assert link["can_create_cable"] is False

    def test_csp_does_not_exist(self):
        """A pk with no ConsoleServerPort raises DoesNotExist -> 'Console Server Port Not Found'."""
        view = _make_view()
        link = {"_source": "serial", "netbox_local_interface_id": 999999}

        view.check_serial_cable_status(link)

        assert link["cable_status"] == "Console Server Port Not Found in NetBox"


# ---------------------------------------------------------------------------
# raw_keys preservation
# ---------------------------------------------------------------------------


class TestRawKeysPreservation:
    """Serial-specific source fields survive the _raw_keys strip in _prepare_context."""

    def test_serial_fields_survive_strip(self):
        """sensor_id, sensor_index_int, is_configured AND device_id survive; derived keys are stripped.

        Exercises the PRODUCTION _RAW_LINK_KEYS (not a hand-copied set). device_id must survive for
        serial rows: enrich_links_data's serial branch continues before re-setting it, and the
        Cables-tab render reads record["device_id"], so dropping it 500s the tab on a cached replay.
        """
        from netbox_librenms_plugin.views.base.cables_view import _RAW_LINK_KEYS

        serial_link = {
            "local_port": "ttyS7",
            "local_port_id": "serial:1007",
            "remote_port": None,
            "remote_device": "switch-a",
            "remote_port_id": None,
            "remote_device_id": None,
            "_source": "serial",
            # Serial-specific source fields (must survive)
            "sensor_id": 1007,
            "sensor_index_int": 7,
            "is_configured": True,
            "device_id": 1,
            # Derived (should be stripped)
            "local_port_url": "/dcim/csp/99/",
            "netbox_local_interface_id": 99,
            "cable_status": "No Cable",
            "can_create_cable": False,
        }

        stripped = {k: v for k, v in serial_link.items() if k in _RAW_LINK_KEYS}

        assert stripped["sensor_id"] == 1007
        assert stripped["sensor_index_int"] == 7
        assert stripped["is_configured"] is True
        assert stripped["device_id"] == 1  # serial rows keep their CSP-owning device_id
        assert "local_port_url" not in stripped
        assert "netbox_local_interface_id" not in stripped
        assert "cable_status" not in stripped


@pytest.mark.django_db
class TestSerialLinkDeviceIdSurvivesCachedRender:
    """A cached serial row must retain device_id through strip+re-enrich (the Cables render reads it)."""

    def test_cached_serial_link_keeps_device_id_through_enrich(self):
        """Strip a cached serial link to _RAW_LINK_KEYS, re-enrich, and confirm device_id survives.

        Reproduces the Cables-tab 500: on a cached render _prepare_context strips links to
        _RAW_LINK_KEYS then calls enrich_links_data, whose serial branch continues without re-setting
        device_id. The render (tables/cables.py) then does record["device_id"] → KeyError before the
        fix. Uses a real console-server device (not a MagicMock) so the CSP resolution is exercised.
        """
        from netbox_librenms_plugin.views.base.cables_view import _RAW_LINK_KEYS

        view = _make_view()
        obj, (csp,), _ = make_serial_device("ser-cached-devid", csp_names=["ttyS0"])

        # A serial link as it sits in the cache — the fresh build set device_id to the CSP-owning
        # sync device (map_sensors_to_serial_links), NOT the viewed obj.id.
        cached_serial = {
            "_source": "serial",
            "device_id": obj.id,
            "local_port": "ttyS0",
            "local_port_id": "serial:1000",
            "sensor_id": 1000,
            "sensor_index_int": 0,
            "is_configured": True,
        }

        # Exactly what _prepare_context does on a cached render: strip, then re-enrich.
        stripped = {k: v for k, v in cached_serial.items() if k in _RAW_LINK_KEYS}
        enriched = view.enrich_links_data([stripped], obj, server_key="default")

        # The Cables-tab render does record["device_id"] — must be present (no KeyError / 500).
        assert enriched[0]["device_id"] == obj.id


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


class TestCableTableSerialRendering:
    """LibreNMSCableTable renders Serial badge and dims unconfigured ports."""

    def _make_table(self):
        from netbox_librenms_plugin.tables.cables import LibreNMSCableTable

        table = object.__new__(LibreNMSCableTable)
        return table

    def test_serial_badge_shown(self):
        table = self._make_table()
        record = {"_source": "serial", "local_port_url": None, "is_configured": True}
        html = str(table.render_local_port("ttyS7", record))
        assert "Serial" in html
        assert "OOB" not in html

    def test_manual_pick_icon_shown(self):
        """A manually picked remote renders the hand icon next to the port name."""
        table = self._make_table()
        record = {"manual_remote": True, "remote_port_url": None}
        html = str(table.render_remote_port("console", record))
        assert "gesture-tap-button" in html
        assert "console" in html
        plain = str(table.render_remote_port("console", {"remote_port_url": None}))
        assert "gesture-tap-button" not in plain

    def test_oob_badge_shown(self):
        table = self._make_table()
        record = {"_source": "oob", "local_port_url": None}
        html = str(table.render_local_port("eth0", record))
        assert "OOB" in html
        assert "Serial" not in html

    def test_no_badge_for_main(self):
        table = self._make_table()
        record = {"_source": "main", "local_port_url": None}
        html = str(table.render_local_port("Gi0/1", record))
        assert "OOB" not in html
        assert "Serial" not in html

    def test_unconfigured_serial_port_is_dimmed(self):
        """Remote device label is italic/muted for unconfigured ports."""
        table = self._make_table()
        record = {"_source": "serial", "is_configured": False, "remote_device_url": None}
        html = str(table.render_remote_device("ttyS49", record))
        assert "text-muted" in html or "fst-italic" in html

    def test_configured_serial_port_not_dimmed(self):
        """Remote device label renders plainly for configured ports."""
        table = self._make_table()
        record = {"_source": "serial", "is_configured": True, "remote_device_url": None}
        html = str(table.render_remote_device("prod-router-01", record))
        assert "text-muted" not in html
        assert "fst-italic" not in html

    def test_unconfigured_serial_port_with_no_remote_name_renders_empty_not_none(self):
        """A serial row with no remote device name must not render the literal 'None'.

        render_local_port already normalizes (value or "") for the same reason; the
        dimmed serial-row branch passes the display value into format_html, which would
        stringify a None into visible "None" text in the UI.
        """
        table = self._make_table()
        record = {"_source": "serial", "is_configured": False, "remote_device_url": None}
        html = str(table.render_remote_device(None, record))
        assert "None" not in html

    def test_serial_with_url_renders_link_and_badge(self):
        """When CSP URL is set, renders linked port name with Serial badge."""
        table = self._make_table()
        record = {"_source": "serial", "local_port_url": "/dcim/csp/99/", "is_configured": True}
        html = str(table.render_local_port("ttyS7", record))
        assert 'href="/dcim/csp/99/"' in html
        assert "Serial" in html


# ---------------------------------------------------------------------------
# enrich_serial_remote
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEnrichSerialRemote:
    """enrich_serial_remote resolves remote device + ConsolePort."""

    @pytest.mark.django_db
    def test_label_matches_device_with_uncabled_cp(self):
        """When the label matches a device with an uncabled ConsolePort, sets can_create_cable + the remote port (real Device/ConsolePort)."""
        view = _make_view()
        device, _, (cp,) = make_serial_device("prod-router-uncabled", cp_names=["con0"])
        _local, (local_csp,), _ = make_serial_device("acs-uncabled-local", csp_names=["ttyS7"])

        link = {
            "local_port": "ttyS7",
            "local_port_id": "serial:1007",
            "_source": "serial",
            "remote_device": device.name,
            "netbox_local_interface_id": local_csp.pk,
            "cable_status": "No Cable",
        }

        view.enrich_serial_remote(link)

        assert link["netbox_remote_interface_id"] == cp.pk
        assert link["remote_port_name"] == "con0"
        assert link["can_create_cable"] is True
        assert link["netbox_remote_device_id"] == device.pk

    def test_device_not_found_leaves_link_unchanged(self):
        """When device lookup fails, link is not modified."""
        view = _make_view()

        link = {
            "remote_device": "unknown-device",
            "cable_status": "No Cable",
        }

        view.enrich_serial_remote(link)

        assert "netbox_remote_device_id" not in link
        assert "can_create_cable" not in link

    def test_no_label_returns_early(self):
        """When remote_device is empty/None, method returns immediately."""
        view = _make_view()
        link = {"remote_device": None, "cable_status": "No Cable"}

        view.enrich_serial_remote(link)
        assert link == {"remote_device": None, "cable_status": "No Cable"}

    @pytest.mark.django_db
    def test_all_cps_cabled_sets_not_found_status(self):
        """When every ConsolePort is already cabled, sets 'Console Port Not Found' (real Device + Cable)."""
        view = _make_view()
        device, _, (cp,) = make_serial_device("prod-router-allcabled", cp_names=["con0"])
        _, (peer_csp,), _ = make_serial_device("peer-allcabled", csp_names=["s0"])
        _local, (local_csp,), _ = make_serial_device("acs-allcabled-local", csp_names=["ttyS7"])
        cable_together(cp, peer_csp)  # the device's only ConsolePort is now cabled

        link = {
            "remote_device": device.name,
            "netbox_local_interface_id": local_csp.pk,
            "cable_status": "No Cable",
        }

        view.enrich_serial_remote(link)

        assert link["cable_status"] == "Console Port Not Found in NetBox"
        assert "can_create_cable" not in link

    @pytest.mark.django_db
    def test_uncabled_console_port_pick_is_deterministic_by_name(self):
        """With several uncabled ConsolePorts, the remote pick is the lowest by name, every run — the Avocent label is only a hint, so the choice must not depend on insertion/DB order."""
        view = _make_view()
        # Create CPs out of alphabetical order to prove ordering isn't insertion order.
        router, _, cps = make_serial_device("router-det", cp_names=["con-z", "con-a", "con-m"])
        _local, (local_csp,), _ = make_serial_device("acs-det-local", csp_names=["ttyS7"])

        link = {
            "local_port": "ttyS7",
            "_source": "serial",
            "remote_device": "router-det",
            "netbox_local_interface_id": local_csp.pk,
            "cable_status": "No Cable",
        }

        view.enrich_serial_remote(link)

        assert link["remote_port_name"] == "con-a"  # lowest name, deterministic
        assert link["can_create_cable"] is True

    @pytest.mark.django_db
    def test_enrich_links_data_resolves_remote_when_no_cable(self):
        """A serial row whose CSP exists and has no cable gets its remote ConsolePort resolved (real DB)."""
        view = _make_view()
        local, (csp,), _ = make_serial_device("acs-nocable", csp_names=["ttyS7"])
        remote, _, (cp,) = make_serial_device("router-nocable", cp_names=["con0"])
        link = {"local_port": "ttyS7", "_source": "serial", "remote_device": remote.name, "device_id": local.id}

        view.enrich_links_data([link], local)

        assert link["netbox_remote_interface_id"] == cp.pk  # enrich_serial_remote ran
        assert link["can_create_cable"] is True

    @pytest.mark.django_db
    def test_enrich_links_data_resolves_remote_for_cabled_rows(self):
        """A serial row whose CSP already has a cable STILL attempts remote resolution — cabled rows need it to offer adopt (matched untagged) / re-sync (mismatch) actions; an unresolvable label leaves the row inactionable (real DB)."""
        view = _make_view()
        local, (csp,), _ = make_serial_device("acs-cabled", csp_names=["ttyS3"])
        _, _, (peer_cp,) = make_serial_device("peer-cabled", cp_names=["con0"])
        cable_together(csp, peer_cp)  # the CSP now has a cable
        link = {"local_port": "ttyS3", "_source": "serial", "remote_device": "irrelevant", "device_id": local.id}

        view.enrich_links_data([link], local)
        # Label didn't resolve: cabled but no LibreNMS target to compare against — no action.
        assert link["cable_status"] == "Cable Found"
        assert "netbox_remote_interface_id" not in link
        assert link["can_create_cable"] is False


# ---------------------------------------------------------------------------
# Serial rows have one fixed ConsoleServerPort owner
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_verify_rejects_serial_row_with_fixed_owner():
    """The public verify endpoint must reject a forged request for a fixed-owner serial row."""
    from django.contrib.auth import get_user_model
    from django.core.cache import cache
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

    device, (csp,), _ = make_serial_device("serial-fixed-owner", csp_names=["ttyS7"])
    row = {
        "row_id": "serial:1007",
        "local_port": csp.name,
        "local_port_id": "serial:1007",
        "_source": "serial",
        "device_id": device.pk,
        "sensor_id": 1007,
    }
    server_key = next(iter(LibreNMSAPI.get_available_servers()))
    cache_key = object.__new__(SingleCableVerifyView).get_cache_key(device, "links", server_key)
    cache.set(cache_key, {"links": [row]}, timeout=300)

    user = get_user_model().objects.create_superuser(
        "serial-fixed-owner-admin",
        "serial-fixed-owner@example.com",
        "pw",
    )
    client = Client()
    client.force_login(user)
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_cable"),
        data={"device_id": device.pk, "row_id": row["row_id"], "server_key": server_key},
        content_type="application/json",
    )

    assert response.status_code == 400
    # Pin the reason: the endpoint answers 400 for a malformed payload and an unknown row too.
    assert response.json()["message"] == "Serial cable rows have a fixed device owner."


# ---------------------------------------------------------------------------
# Code-review fixes: device_id preservation (#2) + OOB-only serial guard (#4/#6)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSerialDeviceIdPreserved:
    """A serial row must keep the CSP-owning device_id set by map_sensors_to_serial_links — not get overwritten with the viewed obj.id (which would mis-default the VC member dropdown)."""

    def test_enrich_links_data_keeps_serial_device_id(self):
        viewed, _, _ = make_serial_device("ser-viewed-deviceid")
        view = _make_view()
        # device_id was stamped by map_sensors_to_serial_links to the CSP-owning sync device.
        link = {"_source": "serial", "device_id": 999999, "local_port": "ttyNope"}
        view.enrich_links_data([link], viewed)
        assert link["device_id"] == 999999  # preserved, not overwritten with viewed.id

    def test_non_serial_row_still_scoped_to_viewed_device(self):
        viewed, _, _ = make_serial_device("ser-viewed-nonserial")
        view = _make_view()
        link = {"local_port": "Gi0/0"}  # non-serial
        view.enrich_links_data([link], viewed)
        assert link["device_id"] == viewed.id  # non-serial rows are scoped to the viewed device

    def test_vc_serial_row_pins_selection_to_the_console_server_port_owner(self):
        from django.test import RequestFactory

        from netbox_librenms_plugin.tables.cables import VCCableTable
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
        from netbox_librenms_plugin.utils import assign_cable_row_ids

        viewed, _, _ = make_serial_device("serial-vc-viewed")
        owner, (csp,), _ = make_serial_device("serial-vc-owner", csp_names=["ttyS1"])
        make_virtual_chassis("serial-vc", viewed, owner)
        row = {
            "_source": "serial",
            "device_id": owner.pk,
            "local_port": csp.name,
            "local_port_id": "serial:501",
        }
        table = VCCableTable(assign_cable_row_ids([row]), device=viewed)
        request = RequestFactory().get("/")

        html = table.as_html(request)

        assert f'<option value="{owner.pk}" selected>' in html
        assert 'name="device_selection_serial:501"' in html
        assert f'value="{owner.pk}"' in html
        assert "disabled" in html


@pytest.mark.django_db
class TestSerialLocalPortQueryBound:
    def test_enrichment_query_count_does_not_grow_with_configured_rows(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        names = [f"ttyS{index}" for index in range(1, 49)]
        device, _ports, _unused = make_serial_device("serial-query-bound", csp_names=names)
        remote, _, _remote_ports = make_serial_device(
            "serial-query-bound-remote",
            cp_names=[f"console-{index:02d}" for index in range(1, 49)],
        )
        links = [
            {
                "_source": "serial",
                "device_id": device.pk,
                "local_port": name,
                "local_port_id": f"serial:{index}",
                "remote_device": remote.name,
                "is_configured": True,
            }
            for index, name in enumerate(names, start=1)
        ]
        view = _make_view()

        with CaptureQueriesContext(connection) as captured:
            view.enrich_links_data(links, device, server_key="default", sync_device=device)

        selects = [
            query["sql"] for query in captured.captured_queries if query["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert len(selects) <= 6

    @pytest.mark.parametrize("with_provenance", [False, True])
    def test_cabled_enrichment_query_count_does_not_grow_per_port(self, with_provenance):
        """Direct cabled rows must reuse cable, tag, termination, and permission state."""
        import re
        from collections import Counter

        from django.contrib.auth import get_user_model
        from django.db import connection
        from django.test import RequestFactory
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.utils import get_librenms_cable_tag
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        names = [f"ttyS{index}" for index in range(1, 49)]
        device, ports, _unused = make_serial_device("serial-cabled-query-bound", csp_names=names)
        remote, _, remote_ports = make_serial_device(
            "serial-cabled-query-bound-remote",
            cp_names=[f"console-{index:02d}" for index in range(1, 49)],
        )
        provenance_tag = get_librenms_cable_tag() if with_provenance else None
        for local_port, remote_port in zip(ports, remote_ports, strict=True):
            cable = cable_together(local_port, remote_port)
            if provenance_tag is not None:
                cable.tags.add(provenance_tag)
        links = [
            {
                "_source": "serial",
                "device_id": device.pk,
                "local_port": name,
                "local_port_id": f"serial:{index}",
                "remote_device": remote.name,
                "is_configured": True,
            }
            for index, name in enumerate(names, start=1)
        ]
        view = DeviceCableTableView()
        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser(
            "serial-cabled-query-user",
            password="pw",
        )
        view.request = request

        with CaptureQueriesContext(connection) as captured:
            view.enrich_links_data(links, device, server_key="default", sync_device=device)

        selects = [
            query["sql"] for query in captured.captured_queries if query["sql"].lstrip().upper().startswith("SELECT")
        ]
        tables = Counter(match.group(1) for sql in selects if (match := re.search(r'FROM "([^"]+)"', sql)))
        # One shared cable load serves both ends, so the provenance tag no longer changes the
        # count: the tags come back with that single prefetch either way.
        assert len(selects) <= 13, tables


@pytest.mark.django_db
class TestNormalCableLinkQueryBound:
    """Ordinary LLDP enrichment must not run permission and lookup queries per row."""

    def test_uncabled_link_enrichment_has_a_constant_query_bound(self):
        from collections import Counter
        import re

        from django.contrib.auth import get_user_model
        from django.db import connection
        from django.test import RequestFactory
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        local = make_device("normal-query-local")
        remote = make_device("normal-query-remote")
        links = []
        for index in range(48):
            local_name = f"Ethernet{index + 1}"
            remote_name = f"Ethernet{index + 101}"
            make_interface(local, local_name)
            make_interface(remote, remote_name)
            links.append(
                {
                    "_source": "main",
                    "local_port": local_name,
                    "local_port_id": 1000 + index,
                    "remote_device": remote.name,
                    "remote_port": remote_name,
                    "remote_port_id": 2000 + index,
                }
            )

        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser("normal-query-user", "", "pw")
        view = DeviceCableTableView()
        view.request = request

        with CaptureQueriesContext(connection) as captured:
            view.enrich_links_data(links, local, server_key="default", sync_device=local)

        selects = [
            query["sql"] for query in captured.captured_queries if query["sql"].lstrip().upper().startswith("SELECT")
        ]
        tables = Counter(match.group(1) for sql in selects if (match := re.search(r'FROM "([^"]+)"', sql)))
        assert all(link.get("can_create_cable") is True for link in links)
        assert len(selects) <= 20, tables

    def test_cabled_link_enrichment_reuses_prefetched_termination_state(self):
        """Point-to-point checks must not query CableTermination once per cable end."""
        from collections import Counter
        import re

        from django.contrib.auth import get_user_model
        from django.db import connection
        from django.test import RequestFactory
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import cable_together, make_device, make_interface
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        local = make_device("normal-cabled-query-local")
        remote = make_device("normal-cabled-query-remote")
        links = []
        for index in range(48):
            local_interface = make_interface(local, f"Ethernet{index + 1}")
            remote_interface = make_interface(remote, f"Ethernet{index + 101}")
            cable_together(local_interface, remote_interface)
            links.append(
                {
                    "_source": "main",
                    "local_port": local_interface.name,
                    "remote_device": remote.name,
                    "remote_port": remote_interface.name,
                }
            )

        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser("normal-cabled-query-user", "", "pw")
        view = DeviceCableTableView()
        view.request = request

        with CaptureQueriesContext(connection) as captured:
            view.enrich_links_data(links, local, server_key="default", sync_device=local)

        selects = [
            query["sql"] for query in captured.captured_queries if query["sql"].lstrip().upper().startswith("SELECT")
        ]
        tables = Counter(match.group(1) for sql in selects if (match := re.search(r'FROM "([^"]+)"', sql)))
        assert all(link.get("cable_status") == "Cable Found" for link in links)
        assert len(selects) <= 24, tables

    def test_patch_path_visibility_checks_are_batched_for_the_page(self):
        """Trace permission checks must not issue one EXISTS query per path object."""
        from django.contrib.auth import get_user_model
        from django.db import connection
        from django.test import RequestFactory
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_patch_panel
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        local = make_device("normal-trace-query-local")
        remote = make_device("normal-trace-query-remote")
        links = []
        for index in range(12):
            local_interface = make_interface(local, f"Ethernet{index + 1}")
            remote_interface = make_interface(remote, f"Ethernet{index + 101}")
            _panel, front, rear = make_patch_panel(f"normal-trace-query-panel-{index + 1}")
            cable_together(local_interface, front)
            cable_together(rear, remote_interface)
            links.append(
                {
                    "_source": "main",
                    "local_port": local_interface.name,
                    "remote_device": remote.name,
                    "remote_port": remote_interface.name,
                }
            )

        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser("normal-trace-query-user", "", "pw")
        view = DeviceCableTableView()
        view.request = request

        with CaptureQueriesContext(connection) as captured:
            view.enrich_links_data(links, local, server_key="default", sync_device=local)

        permission_exists = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith('SELECT 1 AS "A" FROM "DCIM_')
        ]
        assert all(link["cable_status"] == "Connected via Patch Path" for link in links)
        assert len(permission_exists) <= 6

    def test_duplicate_neighbor_rows_trace_each_local_interface_once(self):
        """Multiple neighbor rows for one local port must reuse its real cable trace."""
        from django.contrib.auth import get_user_model
        from django.db import connection
        from django.test import RequestFactory
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import cable_together, make_device, make_interface
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        local_device = make_device("normal-duplicate-trace-local")
        local_interface = make_interface(local_device, "Ethernet1")
        local_peer = make_interface(make_device("normal-duplicate-trace-peer"), "Ethernet1")
        cable_together(local_interface, local_peer)
        remote_device = make_device("normal-duplicate-trace-remote")
        links = []
        for index in range(24):
            remote_interface = make_interface(remote_device, f"Ethernet{index + 1}")
            remote_peer = make_interface(make_device(f"normal-duplicate-remote-peer-{index + 1}"), "Ethernet1")
            cable_together(remote_interface, remote_peer)
            links.append(
                {
                    "link_id": index + 1,
                    "_source": "main",
                    "local_port": local_interface.name,
                    "remote_device": remote_device.name,
                    "remote_port": remote_interface.name,
                }
            )

        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser("normal-duplicate-trace-user", "", "pw")

        def query_count(selected_links):
            view = DeviceCableTableView()
            view.request = request
            with CaptureQueriesContext(connection) as captured:
                view.enrich_links_data(selected_links, local_device, server_key="default", sync_device=local_device)
            return len(captured)

        one_link_queries = query_count([links[0].copy()])
        all_link_queries = query_count([link.copy() for link in links])

        assert all_link_queries <= one_link_queries + 3

    def test_multi_termination_status_links_the_offending_remote_cable(self, client):
        """A normal local cable must not hide the remote breakout cable behind the wrong URL."""
        from dcim.models import Cable
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local = make_device("normal-multi-url-local")
        local_interface = make_interface(local, "Ethernet1")
        local_other = make_interface(make_device("normal-multi-url-local-peer"), "Ethernet8")
        cable_together(local_interface, local_other)
        remote = make_device("normal-multi-url-remote")
        remote_interface = make_interface(remote, "Ethernet9")
        remote_extra = make_interface(remote, "Ethernet10")
        remote_other = make_interface(make_device("normal-multi-url-remote-peer"), "Ethernet11")
        breakout = Cable(
            a_terminations=[remote_interface, remote_extra],
            b_terminations=[remote_other],
            status="connected",
        )
        breakout.save()
        row = {
            "_source": "main",
            "local_port": local_interface.name,
            "local_port_id": 10,
            "remote_device": remote.name,
            "remote_port": remote_interface.name,
            "remote_port_id": 20,
        }
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache.set(
            object.__new__(SyncCablesView).get_cache_key(local, "links", server_key),
            {"links": [row]},
            timeout=300,
        )
        user = get_user_model().objects.create_superuser("normal-multi-url-user", "", "pw")
        client.force_login(user)

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert response.status_code == 200
        record = next(iter(response.context["cable_sync"]["table"].rows)).record
        assert record["cable_status"] == "Multi-termination Cable Not Supported"
        assert str(breakout.pk) in record["cable_url"]

    def test_large_stable_id_sets_use_bounded_query_parameters(self):
        """One large owner must not expand all accepted JSON ID shapes into one statement."""
        from django.contrib.auth import get_user_model
        from django.db import connection
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        local = make_device("normal-parameter-bound-local")
        links = [
            {
                "_source": "main",
                "local_port": f"Ethernet{port_id}",
                "local_port_id": port_id,
                "remote_device_id": 10_000 + port_id,
                "remote_port_id": 20_000 + port_id,
            }
            for port_id in range(1, 321)
        ]
        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser("normal-parameter-bound-user", "", "pw")
        view = DeviceCableTableView()
        view.request = request
        parameter_counts = []

        def capture_parameters(execute, sql, params, many, context):
            parameter_counts.append(len(params or ()))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture_parameters):
            view.enrich_links_data(links, local, server_key="default", sync_device=local)

        assert max(parameter_counts) < 2_000

    def test_candidate_loading_is_scoped_to_each_remote_device(self):
        """Common interface names must not load every name from every remote device."""
        from django.contrib.auth import get_user_model
        from django.db.models.signals import post_init
        from django.test import RequestFactory

        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        local = make_device("normal-materialization-local")
        links = []
        for index in range(16):
            local_name = f"Local{index + 1}"
            make_interface(local, local_name)
            remote = make_device(f"normal-materialization-remote-{index + 1}")
            for remote_index in range(16):
                make_interface(remote, f"Ethernet{remote_index + 1}")
            links.append(
                {
                    "_source": "main",
                    "local_port": local_name,
                    "remote_device": remote.name,
                    "remote_port": "Ethernet1",
                }
            )

        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser("normal-materialization-user", "", "pw")
        view = DeviceCableTableView()
        view.request = request
        initialized = 0

        def count_interface_instances(sender, **kwargs):
            nonlocal initialized
            initialized += 1

        post_init.connect(count_interface_instances, sender=Interface, weak=False)
        try:
            view.enrich_links_data(links, local, server_key="default", sync_device=local)
        finally:
            post_init.disconnect(count_interface_instances, sender=Interface)

        assert all(link.get("can_create_cable") is True for link in links)
        assert initialized <= 40


@pytest.mark.django_db
class TestSerialFetchSkippedWithoutHostId:
    """An unmapped device must not call LibreNMS with a missing host ID."""

    def test_serial_fetch_skipped_when_no_host_librenms_id(self):
        import requests

        obj, _csps, _ = make_serial_device("oob-only-serial", csp_names=["ttyS1"])
        view = _make_view()
        requested_urls = []

        def not_found(url, *args, **kwargs):
            requested_urls.append(url)
            response = requests.models.Response()
            response.status_code = 404
            response.url = url
            return response

        with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=not_found):
            result = view.get_links_data(obj)

        assert result is None
        assert requested_urls
        assert all("/devices/None/links" not in url for url in requested_urls)
        assert view._serial_links_fetch_failed is False


# ---------------------------------------------------------------------------
# Regression: "Cache has expired" on a terminal server (host /links 404)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSerialSyncSurvivesHostLinks404:
    """End-to-end regression for the 'Cache has expired' bug.

    A console/terminal server has no LLDP neighbours, so LibreNMS's ``/devices/<id>/links``
    endpoint returns HTTP 404 for it. That 404 used to be miscategorised as a fetch failure,
    which made ``_prepare_context`` treat the whole refresh as a partial snapshot and DELETE the
    links cache — even though the (independently fetched) serial rows rendered fine with a Sync
    button. Every serial cable sync then failed with 'Cache has expired'.

    This drives the real ``get_links_data`` → cache → ``SyncCablesView`` cable-creation flow with
    only the external LibreNMS HTTP boundary (``requests.get``) stubbed, so the real
    ``get_device_links`` 404-is-empty handling is exercised end-to-end.
    """

    def _routed_get(self):
        import json

        import requests

        def _get(url, *args, **kwargs):
            resp = requests.models.Response()
            resp.url = url
            if url.endswith("/links"):
                # LibreNMS returns 404 for a device that has no links (a terminal server).
                resp.status_code = 404
                resp._content = b'{"status":"error","message":"Device does not have any links"}'
            elif url.endswith("/resources/sensors"):
                resp.status_code = 200
                resp._content = json.dumps(
                    {
                        "status": "ok",
                        "sensors": [
                            {
                                "sensor_id": 1007,
                                "device_id": 13,
                                "sensor_type": "acsSerialPortTable",
                                "sensor_index": "acsSerialPortTableStatus.7",
                                "sensor_descr": "router-z Status",
                            }
                        ],
                    }
                ).encode()
            elif url.endswith("/ports"):
                resp.status_code = 200
                resp._content = b'{"status": "ok", "ports": []}'
            else:  # pragma: no cover - defensive default for any unexpected route
                resp.status_code = 200
                resp._content = b'{"status": "ok"}'
            return resp

        return _get

    def test_serial_rows_cached_and_syncable_when_host_links_404(self, client):
        from django.core.cache import cache
        from dcim.models import Cable
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, csps, _ = make_serial_device("acs-ts26", csp_names=["ttyS7"])
        from netbox_librenms_plugin.utils import set_librenms_device_id

        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(acs, 13, server_key)
        acs.save()
        _router, _, cps = make_serial_device("router-z", cp_names=["console"])
        csp = csps[0]
        console_port = cps[0]

        client.force_login(make_superuser())
        with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=self._routed_get()):
            refreshed = client.post(
                reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[acs.pk]),
                {"server_key": server_key},
                HTTP_HX_REQUEST="true",
            )

        assert refreshed.status_code == 200
        cache_key = object.__new__(SyncCablesView).get_cache_key(acs, "links", server_key)
        cached = cache.get(cache_key)
        assert cached is not None, "a host /links 404 must not delete the serial cache"
        serial_rows = [link for link in cached["links"] if link.get("_source") == "serial"]
        assert len(serial_rows) == 1
        row = serial_rows[0]
        assert row["local_port"] == "ttyS7"
        assert "netbox_local_interface_id" not in row
        assert "netbox_remote_interface_id" not in row
        assert "can_create_cable" not in row

        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[acs.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        records = [table_row.record for table_row in rendered.context["cable_sync"]["table"].rows]
        enriched = next(record for record in records if record["row_id"] == row["row_id"])
        assert enriched["netbox_local_interface_id"] == csp.pk
        assert enriched["netbox_remote_interface_id"] == console_port.pk
        assert enriched["can_create_cable"] is True

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk]),
            {
                "sync_one": row["row_id"],
                f"expected_local_id_{row['row_id']}": csp.pk,
                f"expected_local_device_id_{row['row_id']}": acs.pk,
                f"expected_remote_id_{row['row_id']}": console_port.pk,
                f"expected_remote_device_id_{row['row_id']}": console_port.device_id,
                "server_key": server_key,
            },
        )
        assert synced.status_code == 302

        # The cable really exists between the ConsoleServerPort and the remote ConsolePort.
        csp.refresh_from_db()
        console_port.refresh_from_db()
        assert csp.cable is not None
        assert console_port.cable_id == csp.cable_id
        assert Cable.objects.filter(pk=csp.cable_id).exists()

    def test_host_link_remains_syncable_when_sensor_fetch_fails(self, client):
        """A serial sensor outage must not invalidate a successful host-link snapshot."""
        import json

        import requests
        from dcim.models import Cable
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_superuser
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local, _csps, _ = make_serial_device("serial-partial-host-local", csp_names=["ttyS1"])
        from netbox_librenms_plugin.utils import set_librenms_device_id

        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(local, 13, server_key)
        local.save()
        local_interface = make_interface(local, "Ethernet1")
        remote = make_device("serial-partial-host-remote")
        remote_interface = make_interface(remote, "Ethernet9")

        def routed_get(url, *args, **kwargs):
            response = requests.models.Response()
            response.url = url
            if url.endswith("/links"):
                response.status_code = 200
                response._content = json.dumps(
                    {
                        "status": "ok",
                        "links": [
                            {
                                "id": 901,
                                "local_port_id": 101,
                                "remote_hostname": remote.name,
                                "remote_port": remote_interface.name,
                                "remote_port_id": 202,
                                "protocol": "lldp",
                            }
                        ],
                    }
                ).encode()
            elif url.endswith("/ports"):
                response.status_code = 200
                response._content = json.dumps(
                    {
                        "status": "ok",
                        "ports": [{"port_id": 101, "ifName": local_interface.name, "ifDescr": local_interface.name}],
                    }
                ).encode()
            elif url.endswith("/resources/sensors"):
                response.status_code = 503
                response._content = b'{"status":"error","message":"temporarily unavailable"}'
            elif url.endswith("/devices/13"):
                response.status_code = 200
                response._content = json.dumps(
                    {"status": "ok", "devices": [{"device_id": 13, "hostname": local.name}]}
                ).encode()
            else:  # pragma: no cover - unexpected external route
                response.status_code = 404
                response._content = b"{}"
            return response

        client.force_login(make_superuser())
        with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=routed_get):
            refreshed = client.post(
                reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[local.pk]),
                {"server_key": server_key},
                HTTP_HX_REQUEST="true",
            )

        assert refreshed.status_code == 200
        assert "serial port sensor fetch failed" in refreshed.content.decode()
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cached = cache.get(cache_key)
        assert cached is not None
        assert cached["incomplete_sources"] == ["serial"]
        row_id = cached["links"][0]["row_id"]

        with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=routed_get):
            cached_render = client.get(
                reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
                {"tab": "cables", "server_key": server_key},
            )
        assert cached_render.status_code == 200
        assert cached_render.context["cable_sync"]["incomplete_sources"] == ["serial"]
        assert "These LibreNMS sources failed: serial" in cached_render.content.decode()

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            {
                "sync_one": row_id,
                f"expected_local_id_{row_id}": local_interface.pk,
                f"expected_local_device_id_{row_id}": local.pk,
                f"expected_remote_id_{row_id}": remote_interface.pk,
                f"expected_remote_device_id_{row_id}": remote.pk,
                "server_key": server_key,
            },
            HTTP_HX_REQUEST="true",
        )

        assert synced.status_code == 200
        local_interface.refresh_from_db()
        remote_interface.refresh_from_db()
        assert local_interface.cable_id == remote_interface.cable_id
        assert Cable.objects.filter(pk=local_interface.cable_id).exists()

    def test_serial_row_remains_syncable_when_host_fetch_fails(self, client):
        """A host-link outage must not invalidate a successful serial snapshot."""
        import json

        import requests
        from dcim.models import Cable
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local, (csp,), _ = make_serial_device("serial-partial-sensor-local", csp_names=["ttyS7"])
        from netbox_librenms_plugin.utils import set_librenms_device_id

        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(local, 13, server_key)
        local.save()
        _remote, _, (console_port,) = make_serial_device("serial-partial-sensor-remote", cp_names=["console"])

        def routed_get(url, *args, **kwargs):
            response = requests.models.Response()
            response.url = url
            if url.endswith("/links"):
                response.status_code = 503
                response._content = b'{"status":"error","message":"temporarily unavailable"}'
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
                                "sensor_id": 1007,
                                "device_id": 13,
                                "sensor_type": "acsSerialPortTable",
                                "sensor_index": "acsSerialPortTableStatus.7",
                                "sensor_descr": "serial-partial-sensor-remote Status",
                            }
                        ],
                    }
                ).encode()
            else:  # pragma: no cover - unexpected external route
                response.status_code = 404
                response._content = b"{}"
            return response

        client.force_login(make_superuser())
        with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=routed_get):
            refreshed = client.post(
                reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[local.pk]),
                {"server_key": server_key},
                HTTP_HX_REQUEST="true",
            )

        assert refreshed.status_code == 200
        assert "host links fetch failed" in refreshed.content.decode()
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cached = cache.get(cache_key)
        assert cached is not None
        assert cached["incomplete_sources"] == ["host"]

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            {
                "sync_one": "serial:1007",
                "expected_local_id_serial:1007": csp.pk,
                "expected_local_device_id_serial:1007": local.pk,
                "expected_remote_id_serial:1007": console_port.pk,
                "expected_remote_device_id_serial:1007": console_port.device_id,
                "server_key": server_key,
            },
            HTTP_HX_REQUEST="true",
        )

        assert synced.status_code == 200
        csp.refresh_from_db()
        console_port.refresh_from_db()
        assert csp.cable_id == console_port.cable_id
        assert Cable.objects.filter(pk=csp.cable_id).exists()


# ---------------------------------------------------------------------------
# HTMX: making a cable swaps the table partial in place (no full-page reload)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestCableSyncHtmxPartial:
    """An HTMX cable-sync submit re-renders the ``#cable-sync-content`` partial (HTTP 200) instead
    of a full-page 302 redirect, so creating a cable no longer reloads the whole device page. A
    non-HTMX submit still redirects (no-JS fallback). Driven through the real Django request stack
    (Client) so middleware, permissions, and template rendering are exercised end-to-end.
    """

    def _seed_and_post(self, *, hx):
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, csps, _ = make_serial_device("acs-htmx", csp_names=["ttyS3"])
        _router, _, cps = make_serial_device("router-htmx", cp_names=["console"])
        csp, cp = csps[0], cps[0]

        # Seed the raw links cache that "Refresh Cables" writes. The request must derive the
        # current NetBox terminations and action state from the real ORM objects.
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        key_view = object.__new__(SyncCablesView)
        cache_key = key_view.get_cache_key(acs, "links", server_key)
        link = {
            "local_port": "ttyS3",
            "local_port_id": f"serial:{csp.pk}-s",
            "_source": "serial",
            "device_id": acs.id,
            "remote_device": "router-htmx",
            "is_configured": True,
            "sensor_id": 1,
            "sensor_index_int": 3,
        }
        cache.set(cache_key, {"links": [link]}, timeout=300)

        user = get_user_model().objects.create_superuser("htmx-cable-admin", "htmx@example.com", "pw")
        client = Client()
        client.force_login(user)

        url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk])
        extra = {"HTTP_HX_REQUEST": "true"} if hx else {}
        row_id = link["local_port_id"]
        resp = client.post(
            url,
            data={
                "select": row_id,
                f"expected_local_id_{row_id}": csp.pk,
                f"expected_local_device_id_{row_id}": acs.pk,
                f"expected_remote_id_{row_id}": cp.pk,
                f"expected_remote_device_id_{row_id}": cp.device_id,
                "server_key": server_key,
            },
            **extra,
        )
        return resp, csp

    def test_htmx_submit_returns_partial_and_creates_cable(self):
        from dcim.models import Cable

        resp, csp = self._seed_and_post(hx=True)
        assert resp.status_code == 200  # partial swap in place, NOT a 302 redirect
        assert b"librenms-cable-table" in resp.content  # the cable-table fragment came back
        csp.refresh_from_db()
        assert csp.cable_id is not None
        assert Cable.objects.filter(pk=csp.cable_id).exists()

    def test_non_htmx_submit_redirects_and_creates_cable(self):
        resp, csp = self._seed_and_post(hx=False)
        assert resp.status_code == 302  # full-page redirect fallback
        csp.refresh_from_db()
        assert csp.cable_id is not None

    def test_row_sync_button_ignores_other_checked_rows(self):
        """A singular row action must not submit every checked table row."""
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, (first_csp, second_csp), _ = make_serial_device(
            "acs-row-button",
            csp_names=["ttyS1", "ttyS2"],
        )
        _router, _, (first_cp, _second_cp) = make_serial_device(
            "router-row-button",
            cp_names=["console1", "console2"],
        )
        rows = [
            {
                "local_port": csp.name,
                "local_port_id": f"serial:{index}",
                "_source": "serial",
                "device_id": acs.pk,
                "remote_device": _router.name,
                "is_configured": True,
                "sensor_id": index,
                "sensor_index_int": index,
            }
            for index, csp in enumerate((first_csp, second_csp), start=1)
        ]
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(acs, "links", server_key)
        cache.set(cache_key, {"links": rows}, timeout=300)

        user = get_user_model().objects.create_superuser(
            "row-button-admin",
            "row-button@example.com",
            "pw",
        )
        client = Client()
        client.force_login(user)
        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[acs.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        assert rendered.status_code == 200
        first_row = rendered.context["cable_sync"]["table"].rows[0]
        record = first_row.record
        row_action = str(first_row.get_cell("actions"))
        assert 'name="sync_one"' in row_action
        assert f'value="{rows[0]["local_port_id"]}"' in row_action

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk]),
            data={
                "sync_one": rows[0]["local_port_id"],
                "select": [rows[0]["local_port_id"], rows[1]["local_port_id"]],
                f"expected_local_id_{rows[0]['local_port_id']}": record["netbox_local_interface_id"],
                f"expected_local_device_id_{rows[0]['local_port_id']}": record["netbox_local_device_id"],
                f"expected_remote_id_{rows[0]['local_port_id']}": record["netbox_remote_interface_id"],
                f"expected_remote_device_id_{rows[0]['local_port_id']}": record["netbox_remote_device_id"],
                "server_key": server_key,
            },
        )

        assert response.status_code == 302
        first_csp.refresh_from_db()
        second_csp.refresh_from_db()
        assert first_csp.cable_id is not None
        assert second_csp.cable_id is None

    def test_rendered_local_endpoint_rebind_is_rejected(self):
        """A raw snapshot must not switch to a replacement local termination on POST."""
        from dcim.models import ConsoleServerPort
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import Client
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local, (original_csp,), _ = make_serial_device("serial-local-rebind", csp_names=["ttyS1"])
        remote, _, (cp,) = make_serial_device("serial-local-rebind-remote", cp_names=["console"])
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        original_csp_id = original_csp.pk
        row_id = f"serial:{original_csp_id}"
        raw_row = {
            "_source": "serial",
            "device_id": local.pk,
            "local_port": original_csp.name,
            "local_port_id": row_id,
            "remote_device": remote.name,
            "sensor_id": original_csp.pk,
            "sensor_index_int": 1,
            "is_configured": True,
        }
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cache.set(cache_key, {"links": [raw_row]}, timeout=300)

        user = get_user_model().objects.create_superuser("serial-local-rebind-user", "", "pw")
        client = Client()
        client.force_login(user)
        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        assert rendered.status_code == 200
        rendered_table = rendered.context["cable_sync"]["table"].as_html(rendered.wsgi_request)

        original_csp.delete()
        replacement_csp = ConsoleServerPort.objects.create(device=local, name="ttyS1")
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            {
                "sync_one": row_id,
                f"expected_local_id_{row_id}": original_csp_id,
                f"expected_local_device_id_{row_id}": local.pk,
                f"expected_remote_id_{row_id}": cp.pk,
                f"expected_remote_device_id_{row_id}": remote.pk,
                "server_key": server_key,
            },
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        replacement_csp.refresh_from_db()
        cp.refresh_from_db()
        assert replacement_csp.cable_id is None
        assert cp.cable_id is None
        assert f'name="expected_local_id_{row_id}"' in rendered_table
        assert f'name="expected_local_device_id_{row_id}"' in rendered_table
        assert f'name="expected_remote_device_id_{row_id}"' in rendered_table
        assert f'value="{original_csp_id}"' in rendered_table


@pytest.mark.django_db
class TestSerialCableReadScope:
    """Serial enrichment must not bypass NetBox object-level view grants."""

    @staticmethod
    def _grant(user, name, model, actions, constraints=None):
        from core.models import ObjectType
        from users.models import ObjectPermission

        permission = ObjectPermission.objects.create(name=name, actions=actions, constraints=constraints)
        permission.object_types.add(ObjectType.objects.get_for_model(model))
        permission.users.add(user)

    def _user(self, name, visible_device):
        from dcim.models import Device
        from django.contrib.auth import get_user_model

        from netbox_librenms_plugin.models import LibreNMSSettings

        user = get_user_model().objects.create_user(name)
        self._grant(user, f"{name}-plugin", LibreNMSSettings, ["view", "change"])
        self._grant(user, f"{name}-device", Device, ["view"], {"pk": visible_device.pk})
        return user

    @staticmethod
    def _cache_row(device, csp, remote_device, server_key):
        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        row = {
            "local_port": csp.name,
            "local_port_id": f"serial:{csp.pk}",
            "_source": "serial",
            "device_id": device.pk,
            "remote_device": remote_device.name,
            "sensor_id": csp.pk,
            "sensor_index_int": 1,
            "is_configured": True,
        }
        key = object.__new__(SyncCablesView).get_cache_key(device, "links", server_key)
        cache.set(key, {"links": [row]}, timeout=300)
        return row

    def test_sensor_without_a_netbox_port_stays_visible_to_a_granted_user(self, client):
        """A sensor with no ConsoleServerPort is LibreNMS data, so every granted user sees it.

        Hiding it would give a granted user a shorter table than an administrator and drop the
        one row that tells the operator which console port still has to be created.
        """
        import json

        import requests
        from dcim.models import ConsoleServerPort, Device
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device, (modelled_csp,), _ = make_serial_device("serial-unmodelled-port", csp_names=["ttyS1"])
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(device, 42, server_key)
        device.save()

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
                                "sensor_id": 101,
                                "device_id": 42,
                                "sensor_type": "acsSerialPortTable",
                                "sensor_index": "acsSerialPortTableStatus.1",
                                "sensor_descr": "modelled-label Status",
                            },
                            {
                                "sensor_id": 109,
                                "device_id": 42,
                                "sensor_type": "acsSerialPortTable",
                                "sensor_index": "acsSerialPortTableStatus.9",
                                "sensor_descr": "unmodelled-label Status",
                            },
                        ],
                    }
                ).encode()
            else:
                response.status_code = 200
                response._content = b'{"status":"ok"}'
            return response

        refresh_url = reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[device.pk])

        client.force_login(make_superuser())
        with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=external_get):
            admin_html = client.post(refresh_url, {"server_key": server_key}, HTTP_HX_REQUEST="true").content.decode()

        granted = self._user("serial-unmodelled-port-user", device)
        self._grant(granted, "serial-unmodelled-port-csp", ConsoleServerPort, ["view"])
        self._grant(granted, "serial-unmodelled-port-devices", Device, ["view"])
        client.force_login(granted)
        with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=external_get):
            granted_html = client.post(refresh_url, {"server_key": server_key}, HTTP_HX_REQUEST="true").content.decode()

        # ttyS9 has no ConsoleServerPort in NetBox at all.
        assert modelled_csp.name in admin_html
        assert "ttyS9" in admin_html
        assert modelled_csp.name in granted_html
        assert "ttyS9" in granted_html

    def test_refresh_post_cannot_read_an_out_of_scope_device(self, client):
        from django.urls import reverse

        from netbox_librenms_plugin.tests.conftest import make_device

        hidden, _csps, _ = make_serial_device("serial-read-hidden", csp_names=["ttyS1"])
        visible = make_device("serial-read-visible")
        user = self._user("serial-read-hidden-user", visible)
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[hidden.pk]),
            {"server_key": "default"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 404

    def test_viewed_vc_member_does_not_expose_a_hidden_sync_owner(self, client):
        """A visible sibling must not expose serial components from a hidden sync member."""
        from dcim.models import ConsoleServerPort
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
        from netbox_librenms_plugin.utils import set_librenms_device_id

        hidden, (csp,), _ = make_serial_device("serial-hidden-sync-owner", csp_names=["ttyS1"])
        visible, _, _ = make_serial_device("serial-visible-vc-member")
        make_virtual_chassis("serial-read-scope-vc", hidden, visible)
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(hidden, 42, server_key)
        hidden.save(update_fields=["custom_field_data"])
        self._cache_row(hidden, csp, visible, server_key)
        user = self._user("serial-hidden-sync-owner-user", visible)
        self._grant(user, "serial-hidden-sync-owner-csp", ConsoleServerPort, ["view"])
        client.force_login(user)

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[visible.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert response.status_code == 200
        html = response.content.decode()
        assert csp.name not in html
        assert reverse("dcim:consoleserverport", args=[csp.pk]) not in html

        with patch("netbox_librenms_plugin.librenms_api.requests.get") as http_get:
            refreshed = client.post(
                reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[visible.pk]),
                {"server_key": server_key},
                HTTP_HX_REQUEST="true",
            )

        assert refreshed.status_code == 200
        http_get.assert_not_called()

    def test_refresh_keeps_raw_serial_inventory_but_hides_ungranted_ports(self, client):
        """A constrained CSP grant must filter the response, not the shared raw snapshot."""
        import json

        import requests
        from dcim.models import ConsoleServerPort
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        device, (visible_csp, hidden_csp), _ = make_serial_device(
            "serial-constrained-refresh",
            csp_names=["ttyS1", "ttyS2"],
        )
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(device, 42, server_key)
        device.save()
        user = self._user("serial-constrained-refresh-user", device)
        self._grant(
            user,
            "serial-constrained-refresh-csp",
            ConsoleServerPort,
            ["view"],
            {"pk": visible_csp.pk},
        )
        client.force_login(user)

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
                                "sensor_id": 101,
                                "device_id": 42,
                                "sensor_type": "acsSerialPortTable",
                                "sensor_index": "acsSerialPortTableStatus.1",
                                "sensor_descr": "visible-serial-label Status",
                            },
                            {
                                "sensor_id": 102,
                                "device_id": 42,
                                "sensor_type": "acsSerialPortTable",
                                "sensor_index": "acsSerialPortTableStatus.2",
                                "sensor_descr": "hidden-serial-label Status",
                            },
                        ],
                    }
                ).encode()
            else:
                response.status_code = 200
                response._content = b'{"status":"ok"}'
            return response

        with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=external_get):
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[device.pk]),
                {"server_key": server_key},
                HTTP_HX_REQUEST="true",
            )

        assert response.status_code == 200
        html = response.content.decode()
        assert visible_csp.name in html
        assert "visible-serial-label" in html
        assert hidden_csp.name not in html
        assert "hidden-serial-label" not in html
        cache_key = object.__new__(SyncCablesView).get_cache_key(device, "links", server_key)
        raw_links = cache.get(cache_key)["links"]
        assert {row["local_port"] for row in raw_links if row.get("_source") == "serial"} == {
            visible_csp.name,
            hidden_csp.name,
        }

    def test_refresh_without_visible_serial_ports_skips_the_sensor_request(self, client):
        """A Device grant alone must not authorize an instance-wide sensor download."""
        import requests
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device, _csps, _ = make_serial_device("serial-no-csp-grant", csp_names=["ttyS1"])
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(device, 43, server_key)
        device.save()
        user = self._user("serial-no-csp-grant-user", device)
        client.force_login(user)
        requested_urls = []

        def external_get(url, *args, **kwargs):
            requested_urls.append(url)
            response = requests.models.Response()
            response.url = url
            if url.endswith("/links"):
                response.status_code = 404
                response._content = b'{"status":"error","message":"Device does not have any links"}'
            elif url.endswith("/ports"):
                response.status_code = 200
                response._content = b'{"status":"ok","ports":[]}'
            else:
                raise AssertionError(f"unexpected LibreNMS request: {url}")
            return response

        with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=external_get):
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[device.pk]),
                {"server_key": server_key},
                HTTP_HX_REQUEST="true",
            )

        assert response.status_code == 200
        assert all(not url.endswith("/resources/sensors") for url in requested_urls)

    def test_verify_does_not_link_an_ungranted_local_interface(self, client):
        import json

        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local = make_device("cable-verify-local-scope")
        interface = make_interface(local, "Ethernet1")
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cache.set(
            cache_key,
            {
                "links": [
                    {
                        "local_port": interface.name,
                        "local_port_id": 10,
                        "remote_device": None,
                        "remote_port": None,
                        "_source": "main",
                    }
                ]
            },
            timeout=300,
        )
        user = self._user("cable-verify-local-scope-user", local)
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            data=json.dumps(
                {
                    "device_id": local.pk,
                    "row_id": "10",
                    "server_key": server_key,
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        rendered = json.dumps(response.json()["formatted_row"])
        assert interface.name in rendered
        assert reverse("dcim:interface", args=[interface.pk]) not in rendered

    def test_hidden_librenms_id_match_blocks_a_visible_hostname_fallback(self, client):
        from dcim.models import Device, Interface
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local = make_device("cable-strong-id-local")
        local_interface = make_interface(local, "Ethernet1")
        hidden = make_device("cable-strong-id-hidden")
        visible = make_device("cable-strong-id-advertised")
        visible_interface = make_interface(visible, "Ethernet9")
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(hidden, 42, server_key)
        hidden.save()
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cache.set(
            cache_key,
            {
                "links": [
                    {
                        "local_port": local_interface.name,
                        "local_port_id": 10,
                        "remote_device": visible.name,
                        "remote_device_id": 42,
                        "remote_port": visible_interface.name,
                        "remote_port_id": 20,
                        "_source": "main",
                    }
                ]
            },
            timeout=300,
        )
        user = get_user_model().objects.create_user("cable-strong-id-user")
        self._grant(user, "cable-strong-id-plugin", LibreNMSSettings, ["view"])
        self._grant(user, "cable-strong-id-devices", Device, ["view"], {"pk__in": [local.pk, visible.pk]})
        self._grant(
            user,
            "cable-strong-id-interfaces",
            Interface,
            ["view"],
            {"pk__in": [local_interface.pk, visible_interface.pk]},
        )
        client.force_login(user)

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert response.status_code == 200
        record = next(iter(response.context["cable_sync"]["table"].rows)).record
        assert record.get("netbox_remote_device_id") is None
        assert record.get("netbox_remote_interface_id") is None

    def test_hidden_exact_serial_label_blocks_a_visible_short_name_fallback(self, client):
        from dcim.models import ConsolePort, ConsoleServerPort, Device
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.tests.conftest import make_device

        local, (csp,), _ = make_serial_device("serial-strong-name-local", csp_names=["ttyS1"])
        hidden = make_device("serial-strong-name.example")
        visible, _, (console_port,) = make_serial_device("serial-strong-name", cp_names=["console"])
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        self._cache_row(local, csp, hidden, server_key)
        user = get_user_model().objects.create_user("serial-strong-name-user")
        self._grant(user, "serial-strong-name-plugin", LibreNMSSettings, ["view"])
        self._grant(
            user,
            "serial-strong-name-devices",
            Device,
            ["view"],
            {"pk__in": [local.pk, visible.pk]},
        )
        self._grant(user, "serial-strong-name-csp", ConsoleServerPort, ["view"], {"pk": csp.pk})
        self._grant(user, "serial-strong-name-cp", ConsolePort, ["view"], {"pk": console_port.pk})
        client.force_login(user)

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
            {"tab": "cables", "server_key": server_key},
        )

        assert response.status_code == 200
        record = next(iter(response.context["cable_sync"]["table"].rows)).record
        assert record.get("netbox_remote_device_id") is None
        assert record.get("netbox_remote_interface_id") is None

    def test_restricted_render_does_not_reduce_shared_snapshot_before_admin_sync(self, client):
        from dcim.models import Cable
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        local, (csp,), _ = make_serial_device("serial-cache-scope-local", csp_names=["ttyS1"])
        remote, _, (cp,) = make_serial_device("serial-cache-scope-remote", cp_names=["console"])
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        row = self._cache_row(local, csp, remote, server_key)
        restricted = self._user("serial-cache-scope-user", local)
        client.force_login(restricted)

        rendered = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[local.pk]),
            {"tab": "cables", "server_key": server_key},
        )
        assert rendered.status_code == 200

        admin = get_user_model().objects.create_superuser(
            "serial-cache-scope-admin",
            "serial-cache-scope@example.com",
            "pw",
        )
        client.force_login(admin)
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
            HTTP_HX_REQUEST="true",
        )

        assert synced.status_code == 200
        csp.refresh_from_db()
        cp.refresh_from_db()
        assert Cable.objects.filter(pk=csp.cable_id).exists()
        assert csp.cable_id == cp.cable_id

    def test_hidden_cache_owner_reports_permission_failure_instead_of_expiry(self, client):
        """A hidden chassis cache owner is not an expired LibreNMS snapshot."""
        from dcim.models import Cable
        from django.contrib.messages import get_messages
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
        from netbox_librenms_plugin.utils import set_librenms_device_id

        page, (csp,), _ = make_serial_device("serial-cache-page", csp_names=["ttyS1"])
        cache_owner = make_serial_device("serial-cache-owner")[0]
        make_virtual_chassis("serial-cache-scope-vc", page, cache_owner)
        remote = make_serial_device("serial-cache-target")[0]
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(cache_owner, 42, server_key)
        cache_owner.save()
        row = self._cache_row(cache_owner, csp, remote, server_key)
        user = self._user("serial-hidden-cache-owner-user", page)
        self._grant(user, "serial-hidden-cache-owner-cable", Cable, ["add", "change"])
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[page.pk]),
            {
                "sync_one": row["local_port_id"],
                "server_key": server_key,
            },
        )

        notices = [str(message) for message in get_messages(response.wsgi_request)]
        assert response.status_code == 302
        assert notices == ["You do not have permission to view the LibreNMS cable source device."]


# ---------------------------------------------------------------------------
# Cable enrichment: created cables carry the librenms tag, color, description, tenant
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestCableEnrichment:
    """``create_cable`` stamps provenance on every cable the sync creates: the ``librenms``
    tag (so the plugin can later recognise/own its own cables for the planned DCIM remodel),
    a configured color + description carrying the server key, and the REMOTE device's tenant.
    Driven end-to-end against real ConsoleServerPort / ConsolePort / Tenant rows through
    ``handle_cable_creation`` → ``create_cable`` (the write point shared with the
    non-serial Interface↔Interface path).
    """

    def _sync_one(self, csp, cp, server_key="production"):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        sync = object.__new__(SyncCablesView)
        sync.request = _make_request()
        sync._post_server_key = server_key
        link = {
            "local_port": csp.name,
            "_source": "serial",
            "netbox_local_interface_id": csp.pk,
            "netbox_remote_interface_id": cp.pk,
        }
        return sync.handle_cable_creation(link, {"device_id": csp.device_id})

    def test_created_cable_carries_tag_color_description_and_remote_tenant(self):
        from dcim.models import Cable
        from tenancy.models import Tenant

        from netbox_librenms_plugin.models import LibreNMSSettings

        tenant = Tenant.objects.create(name="RemoteTenant", slug="remote-tenant")
        _acs, csps, _ = make_serial_device("acs-enrich", csp_names=["ttyS1"])
        router, _, cps = make_serial_device("router-enrich", cp_names=["console"])
        router.tenant = tenant
        router.save()
        csp, cp = csps[0], cps[0]

        result = self._sync_one(csp, cp, server_key="production")
        assert result["status"] == "valid"

        csp.refresh_from_db()
        cable = Cable.objects.get(pk=csp.cable_id)

        # Provenance tag — lets the plugin recognise cables it created.
        assert "librenms" in set(cable.tags.values_list("slug", flat=True))
        # Color + description come from the settings singleton (untouched here → field
        # defaults); description carries the server key.
        settings_row, _ = LibreNMSSettings.objects.get_or_create()
        assert cable.color == settings_row.cable_sync_tag_color
        assert "production" in cable.description
        # Tenant is the REMOTE side's tenant (the target device), not the terminal server's.
        assert cable.tenant_id == tenant.pk

    def test_enrichment_settings_come_from_the_settings_row(self):
        """The provenance stamp is DB/UI-driven: custom values saved on the LibreNMSSettings singleton (what the Settings page writes) must land on the created cable and its auto-created tag — the shipped defaults must NOT win once an operator changed them."""
        from dcim.models import Cable
        from extras.models import Tag

        from netbox_librenms_plugin.models import LibreNMSSettings

        settings_row, _ = LibreNMSSettings.objects.get_or_create()
        settings_row.cable_sync_tag = "custom-prov"
        settings_row.cable_sync_tag_color = "ff5722"
        settings_row.cable_sync_description = "Stamped by custom sync"
        settings_row.save()

        _acs, csps, _ = make_serial_device("acs-enrich3", csp_names=["ttyS3"])
        _router, _, cps = make_serial_device("router-enrich3", cp_names=["console"])
        csp, cp = csps[0], cps[0]

        result = self._sync_one(csp, cp, server_key="production")
        assert result["status"] == "valid"

        csp.refresh_from_db()
        cable = Cable.objects.get(pk=csp.cable_id)
        # The custom tag was auto-created with the custom color and stamped on the cable.
        assert "custom-prov" in set(cable.tags.values_list("slug", flat=True))
        assert Tag.objects.get(slug="custom-prov").color == "ff5722"
        # Cable color + description follow the row; description still carries the server key.
        assert cable.color == "ff5722"
        assert cable.description == "Stamped by custom sync (production)"

    def test_tenant_is_remote_side_when_sides_differ(self):
        """When the two devices are in different tenants, the cable takes the REMOTE tenant."""
        from dcim.models import Cable
        from tenancy.models import Tenant

        local_tenant = Tenant.objects.create(name="LocalTenant", slug="local-tenant")
        remote_tenant = Tenant.objects.create(name="RemoteTenant2", slug="remote-tenant-2")
        acs, csps, _ = make_serial_device("acs-enrich2", csp_names=["ttyS2"])
        acs.tenant = local_tenant
        acs.save()
        router, _, cps = make_serial_device("router-enrich2", cp_names=["console"])
        router.tenant = remote_tenant
        router.save()
        csp, cp = csps[0], cps[0]

        assert self._sync_one(csp, cp)["status"] == "valid"
        csp.refresh_from_db()
        cable = Cable.objects.get(pk=csp.cable_id)
        assert cable.tenant_id == remote_tenant.pk  # remote side wins, not local
