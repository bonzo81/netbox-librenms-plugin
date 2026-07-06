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

from unittest.mock import MagicMock, patch

import pytest

# Shared real-DB builders (see tests/conftest.py).
from netbox_librenms_plugin.tests.conftest import cable_together, make_serial_device


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_obj(pk=1, has_csps=True):
    obj = MagicMock()
    obj._meta = MagicMock()
    obj._meta.model_name = "device"
    obj.pk = pk
    obj.id = pk
    obj.name = "acs-console-01"
    obj.primary_ip = None
    obj.virtual_chassis = None
    csp_qs = MagicMock()
    csp_qs.exists.return_value = has_csps
    obj.consoleserverports = csp_qs
    return obj


def _mock_request(path="/plugins/librenms/device/1/cables/"):
    req = MagicMock()
    req.path = path
    req.GET = {}
    req.POST = {}
    req.headers = {}
    return req


def _make_request_json(body_dict):
    """Mock POST request carrying a JSON body (SingleCableVerifyView reads request.body)."""
    import json

    req = MagicMock()
    req.method = "POST"
    req.body = json.dumps(body_dict).encode()
    req.META = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
    return req


def _make_view():
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    view = object.__new__(BaseCableTableView)
    view.request = _mock_request()
    view.librenms_id = 12
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    return view


def _serial_sensor(port_num: int, label: str | None = None) -> dict:
    """Build a minimal sensor record matching ACS fixture shape."""
    if label is None:
        label = f"ttyS{port_num}"
    return {
        "sensor_id": 1000 + port_num,
        "device_id": 12,
        "sensor_type": "acsSerialPortTable",
        "sensor_index": f"acsSerialPortTableStatus.{port_num}",
        "sensor_descr": f"{label} Status",
        "sensor_current": 2,
        "group": "Serial Ports",
    }


# ---------------------------------------------------------------------------
# get_links_data with serial appending
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGetLinksDataSerial:
    """get_links_data() appends serial rows when the (sync) device has ConsoleServerPorts.

    Drives the serial-append path against REAL Device + ConsoleServerPort rows (make_serial_device)
    so the CSP gate (``consoleserverports.exists()``) and the ``lookup_device.id`` fed into
    map_sensors_to_serial_links run against the real ORM. Only the LibreNMS API (get_device_links /
    get_serial_port_sensors / get_librenms_id) stays mocked — a true external HTTP boundary — so a
    renamed field or a regressed CSP gate fails these tests instead of a MagicMock hiding it.
    """

    def _obj_with_csps(self, name="serial-obj", csp_names=("ttyS3", "ttyS7")):
        obj, _csps, _ = make_serial_device(name, csp_names=csp_names)
        return obj

    def _obj_no_csps(self, name="serial-obj-nocsp"):
        from netbox_librenms_plugin.tests.conftest import make_device

        return make_device(name)

    def _base_setup(self, view, sensors=None):
        """Configure the mocked LibreNMS API with a minimal successful LLDP response and sensors."""
        view._librenms_api.get_device_links.return_value = (True, {"links": []})
        view._librenms_api.get_librenms_id.return_value = 12
        view._librenms_api.get_serial_port_sensors.return_value = (True, sensors if sensors is not None else [])

    def test_serial_rows_appended_when_device_has_csps(self):
        """Sensors are mapped and appended to links_data for a real console-server device."""
        view = _make_view()
        sensors = [_serial_sensor(3, "router-a"), _serial_sensor(7, "switch-b")]
        self._base_setup(view, sensors=sensors)
        obj = self._obj_with_csps("serial-append", csp_names=("ttyS3", "ttyS7"))

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=None),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=None),
            patch.object(view, "get_ports_data", return_value={"ports": []}),
        ):
            result = view.get_links_data(obj)

        assert result is not None
        serial_rows = [r for r in result if r.get("_source") == "serial"]
        assert len(serial_rows) == 2
        port_names = {r["local_port"] for r in serial_rows}
        assert port_names == {"ttyS3", "ttyS7"}

    def test_serial_gate_uses_sync_device_csps_on_vc(self):
        """On a VC-member page the viewed obj may lack ConsoleServerPorts while the resolved sync device owns them."""
        view = _make_view()
        sensors = [_serial_sensor(3, "router-a")]
        self._base_setup(view, sensors=sensors)
        obj = self._obj_no_csps("serial-vc-member")  # viewed VC member: NO CSPs
        sync_device = self._obj_with_csps("serial-vc-sync", csp_names=("ttyS3",))  # sync device: HAS CSPs

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=None),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=sync_device,
            ),
            patch.object(view, "get_ports_data", return_value={"ports": []}),
        ):
            result = view.get_links_data(obj)

        serial_rows = [r for r in result if r.get("_source") == "serial"]
        assert len(serial_rows) == 1  # gate passed via the sync device's real CSPs
        view._librenms_api.get_serial_port_sensors.assert_called_once()

    def test_no_serial_rows_when_no_csps(self):
        """Serial fetch is skipped entirely when the device has no ConsoleServerPorts."""
        view = _make_view()
        self._base_setup(view)
        obj = self._obj_no_csps("serial-none")

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=None),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=None),
            patch.object(view, "get_ports_data", return_value={"ports": []}),
        ):
            result = view.get_links_data(obj)

        view._librenms_api.get_serial_port_sensors.assert_not_called()
        # LLDP succeeded with zero links and there are no serial rows: a *successful* empty
        # refresh returns [] (flows through to the empty table), not None ("No links found").
        assert result == []

    def test_serial_fetch_failure_does_not_append(self):
        """When sensor fetch fails, no serial rows are added (graceful degradation), and the failure is flagged so post() can warn the user instead of silently dropping the rows under a success banner (parity with the OOB-fetch-failure warning)."""
        view = _make_view()
        view._librenms_api.get_device_links.return_value = (True, {"links": []})
        view._librenms_api.get_librenms_id.return_value = 12
        view._librenms_api.get_serial_port_sensors.return_value = (False, "timeout")
        obj = self._obj_with_csps("serial-fail")

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=None),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=None),
            patch.object(view, "get_ports_data", return_value={"ports": []}),
        ):
            result = view.get_links_data(obj)

        # LLDP succeeded (empty) and the serial fetch failed without adding rows: a successful
        # refresh with zero rows returns [] (no host error recorded), not None.
        assert result == []
        assert view._serial_links_fetch_failed is True

    def test_serial_fetch_success_does_not_flag_failure(self):
        """A successful serial fetch must leave the failure flag False so no spurious warning."""
        view = _make_view()
        sensors = [_serial_sensor(3, "router-a")]
        self._base_setup(view, sensors=sensors)
        obj = self._obj_with_csps("serial-ok")

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=None),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=None),
            patch.object(view, "get_ports_data", return_value={"ports": []}),
        ):
            view.get_links_data(obj)

        assert view._serial_links_fetch_failed is False

    def test_serial_fetch_success_with_non_list_payload_is_skipped(self):
        """A malformed non-list payload on the success path is skipped by the call-site isinstance(list) guard (a non-iterable would otherwise crash the mapper), not flagged as a failure."""
        view = _make_view()
        obj = self._obj_with_csps("serial-badpayload")

        for bad in (42, "garbage", {"sensor_id": 1}):  # truthy but not a list
            view._librenms_api.get_device_links.return_value = (True, {"links": []})
            view._librenms_api.get_librenms_id.return_value = 12
            view._librenms_api.get_serial_port_sensors.return_value = (True, bad)
            with (
                patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=None),
                patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=None),
                patch.object(view, "get_ports_data", return_value={"ports": []}),
            ):
                result = view.get_links_data(obj)  # must not raise

            serial_rows = [r for r in (result or []) if r.get("_source") == "serial"]
            assert serial_rows == []  # malformed payload skipped, no rows mapped
            assert view._serial_links_fetch_failed is False  # success, just an unusable payload

    def test_serial_row_shape(self):
        """Each appended row has the expected keys."""
        view = _make_view()
        sensors = [_serial_sensor(5, "prod-router-01")]
        self._base_setup(view, sensors=sensors)
        obj = self._obj_with_csps("serial-shape", csp_names=("ttyS5",))

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=None),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=None),
            patch.object(view, "get_ports_data", return_value={"ports": []}),
        ):
            result = view.get_links_data(obj)

        row = result[0]
        assert row["_source"] == "serial"
        assert row["local_port"] == "ttyS5"
        assert row["remote_device"] == "prod-router-01"
        assert row["remote_port"] is None
        assert row["remote_device_id"] is None
        assert row["is_configured"] is True
        assert row["sensor_id"] == 1005
        assert row["sensor_index_int"] == 5


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
        view = _make_view()
        # The viewed member has NO CSP; the sync device (priority member) owns it.
        viewed, _, _ = make_serial_device("ser-enrich-viewed")
        sync_device, (csp,), _ = make_serial_device("ser-enrich-sync", csp_names=["ttyS5"])

        link = {"local_port": "ttyS5", "local_port_id": "serial:1005", "_source": "serial"}
        with patch(
            "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
            return_value=sync_device,
        ):
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
# enrich_links_data routing
# ---------------------------------------------------------------------------


class TestEnrichLinksDataSerial:
    """enrich_links_data routes serial rows to check_serial_cable_status."""

    def test_serial_row_calls_check_serial_cable_status(self):
        view = _make_view()
        serial_link = {
            "local_port": "ttyS7",
            "local_port_id": "serial:1007",
            "_source": "serial",
            "remote_device": "switch-a",
            "device_id": None,
        }

        obj = _mock_obj()

        with (
            patch.object(view, "enrich_local_port"),
            patch.object(view, "check_serial_cable_status") as mock_check_serial,
            patch.object(view, "process_remote_device") as mock_process_remote,
        ):
            view.enrich_links_data([serial_link], obj)

        mock_check_serial.assert_called_once()
        assert mock_check_serial.call_args.args[0] is serial_link  # csp= reuse arg may also be passed
        mock_process_remote.assert_not_called()

    def test_non_serial_row_goes_through_normal_path(self):
        view = _make_view()
        normal_link = {
            "local_port": "GigabitEthernet0/1",
            "local_port_id": 101,
            "_source": "main",
            "remote_device": "switch-b",
            "remote_device_id": 5,
            "device_id": None,
        }

        obj = _mock_obj()

        with (
            patch.object(view, "enrich_local_port"),
            patch.object(view, "check_serial_cable_status") as mock_check_serial,
            patch.object(view, "process_remote_device", return_value=normal_link) as mock_process_remote,
            patch.object(view, "check_cable_status"),
        ):
            view.enrich_links_data([normal_link], obj)

        mock_check_serial.assert_not_called()
        mock_process_remote.assert_called_once()


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


class TestEnrichSerialRemote:
    """enrich_serial_remote resolves remote device + ConsolePort."""

    @pytest.mark.django_db
    def test_label_matches_device_with_uncabled_cp(self):
        """When the label matches a device with an uncabled ConsolePort, sets can_create_cable + the remote port (real Device/ConsolePort)."""
        view = _make_view()
        device, _, (cp,) = make_serial_device("prod-router-uncabled", cp_names=["con0"])

        link = {
            "local_port": "ttyS7",
            "local_port_id": "serial:1007",
            "_source": "serial",
            "remote_device": device.name,
            "netbox_local_interface_id": 99,
            "cable_status": "No Cable",
        }

        with patch.object(view, "get_device_by_id_or_name", return_value=(device, True, None)):
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

        with patch.object(view, "get_device_by_id_or_name", return_value=(None, False, None)):
            view.enrich_serial_remote(link)

        assert "netbox_remote_device_id" not in link
        assert "can_create_cable" not in link

    def test_no_label_returns_early(self):
        """When remote_device is empty/None, method returns immediately."""
        view = _make_view()
        link = {"remote_device": None, "cable_status": "No Cable"}

        with patch.object(view, "get_device_by_id_or_name") as mock_lookup:
            view.enrich_serial_remote(link)

        mock_lookup.assert_not_called()

    @pytest.mark.django_db
    def test_all_cps_cabled_sets_not_found_status(self):
        """When every ConsolePort is already cabled, sets 'Console Port Not Found' (real Device + Cable)."""
        view = _make_view()
        device, _, (cp,) = make_serial_device("prod-router-allcabled", cp_names=["con0"])
        _, (peer_csp,), _ = make_serial_device("peer-allcabled", csp_names=["s0"])
        cable_together(cp, peer_csp)  # the device's only ConsolePort is now cabled

        link = {
            "remote_device": device.name,
            "cable_status": "No Cable",
        }

        with patch.object(view, "get_device_by_id_or_name", return_value=(device, True, None)):
            view.enrich_serial_remote(link)

        assert link["cable_status"] == "Console Port Not Found in NetBox"
        assert "can_create_cable" not in link

    @pytest.mark.django_db
    def test_uncabled_console_port_pick_is_deterministic_by_name(self):
        """With several uncabled ConsolePorts, the remote pick is the lowest by name, every run — the Avocent label is only a hint, so the choice must not depend on insertion/DB order."""
        view = _make_view()
        # Create CPs out of alphabetical order to prove ordering isn't insertion order.
        router, _, cps = make_serial_device("router-det", cp_names=["con-z", "con-a", "con-m"])

        link = {
            "local_port": "ttyS7",
            "_source": "serial",
            "remote_device": "router-det",
            "netbox_local_interface_id": 99,
            "cable_status": "No Cable",
        }

        with patch.object(view, "get_device_by_id_or_name", return_value=(router, True, None)):
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

        with patch.object(view, "get_device_by_id_or_name", return_value=(remote, True, None)):
            view.enrich_links_data([link], local)

        assert link["netbox_remote_interface_id"] == cp.pk  # enrich_serial_remote ran
        assert link["can_create_cable"] is True

    @pytest.mark.django_db
    def test_enrich_links_data_skips_remote_when_cable_found(self):
        """A serial row whose CSP already has a cable does NOT get a remote resolution / Sync action (real DB)."""
        view = _make_view()
        local, (csp,), _ = make_serial_device("acs-cabled", csp_names=["ttyS3"])
        _, _, (peer_cp,) = make_serial_device("peer-cabled", cp_names=["con0"])
        cable_together(csp, peer_cp)  # the CSP now has a cable
        link = {"local_port": "ttyS3", "_source": "serial", "remote_device": "irrelevant", "device_id": local.id}

        with patch.object(view, "get_device_by_id_or_name") as mock_lookup:
            view.enrich_links_data([link], local)

        assert link["cable_status"] == "Cable Found"
        assert "netbox_remote_interface_id" not in link
        mock_lookup.assert_not_called()  # enrich_serial_remote (which performs the lookup) was skipped


# ---------------------------------------------------------------------------
# SyncCablesView serial handling
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestHandleSerialCableCreation:
    """SyncCablesView.handle_serial_cable_creation creates CSP <-> CP cables."""

    def _make_sync_view(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.request = _mock_request()
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_creates_cable_when_both_sides_found(self):
        """When a real CSP and CP both exist and are uncabled, create_cable is invoked and the result is valid."""
        view = self._make_sync_view()
        _, (csp,), _ = make_serial_device("ser-hsc-csp", csp_names=["ttyS7"])
        _, _, (cp,) = make_serial_device("ser-hsc-cp", cp_names=["con0"])

        link_data = {
            "_source": "serial",
            "local_port": "ttyS7",
            "netbox_local_interface_id": csp.pk,
            "netbox_remote_interface_id": cp.pk,
        }
        interface = {"device_id": 1, "local_port_id": "serial:1007"}

        with patch.object(view, "create_cable", return_value=True) as mock_create:
            result = view.handle_serial_cable_creation(link_data, interface)

        assert result["status"] == "valid"
        assert result["interface"] == "ttyS7"
        mock_create.assert_called_once()
        passed_csp, passed_cp, passed_req = mock_create.call_args[0]
        assert passed_csp.pk == csp.pk
        assert passed_cp.pk == cp.pk
        assert passed_req is view.request

    def test_missing_csp_id_returns_missing_remote(self):
        """When netbox_local_interface_id is absent, returns missing_remote."""
        view = self._make_sync_view()

        link_data = {
            "_source": "serial",
            "local_port": "ttyS7",
            "netbox_remote_interface_id": 77,
        }
        interface = {"device_id": 1, "local_port_id": "serial:1007"}

        result = view.handle_serial_cable_creation(link_data, interface)
        assert result["status"] == "missing_remote"

    def test_missing_cp_id_returns_missing_remote(self):
        """When netbox_remote_interface_id is absent, returns missing_remote."""
        view = self._make_sync_view()

        link_data = {
            "_source": "serial",
            "local_port": "ttyS7",
            "netbox_local_interface_id": 99,
        }
        interface = {"device_id": 1, "local_port_id": "serial:1007"}

        result = view.handle_serial_cable_creation(link_data, interface)
        assert result["status"] == "missing_remote"

    def test_existing_untagged_cable_on_csp_returns_conflict(self):
        """Re-pointing the CSP over an untagged (non-managed) cable defers with a 'conflict', DB untouched."""
        from dcim.models import Cable

        view = self._make_sync_view()
        _, (csp,), _ = make_serial_device("ser-hsc-dup-csp", csp_names=["ttyS7"])
        _, _, (peer_cp,) = make_serial_device("ser-hsc-dup-peer", cp_names=["conX"])
        old = cable_together(csp, peer_cp)  # csp is now cabled (untagged → not ours)
        _, _, (cp,) = make_serial_device("ser-hsc-dup-target", cp_names=["con0"])

        link_data = {
            "_source": "serial",
            "local_port": "ttyS7",
            "local_port_id": "serial:1007",
            "netbox_local_interface_id": csp.pk,
            "netbox_remote_interface_id": cp.pk,
        }
        interface = {"device_id": 1, "local_port_id": "serial:1007"}

        result = view.handle_serial_cable_creation(link_data, interface)

        assert result["status"] == "conflict"
        assert result["port_id"] == "serial:1007"
        assert result["trace"]  # the doomed cable's end-to-end path, for the modal
        assert Cable.objects.filter(pk=old.pk).exists()  # untouched without force

    def test_csp_does_not_exist_returns_missing_remote(self):
        """A netbox_local_interface_id with no ConsoleServerPort returns missing_remote."""
        view = self._make_sync_view()
        _, _, (cp,) = make_serial_device("ser-hsc-nocsp", cp_names=["con0"])

        link_data = {
            "_source": "serial",
            "local_port": "ttyS7",
            "netbox_local_interface_id": 9999999,  # no such CSP
            "netbox_remote_interface_id": cp.pk,
        }
        interface = {"device_id": 1, "local_port_id": "serial:1007"}

        result = view.handle_serial_cable_creation(link_data, interface)

        assert result["status"] == "missing_remote"

    def test_handle_cable_creation_routes_serial_to_serial_handler(self):
        """handle_cable_creation dispatches serial _source to handle_serial_cable_creation."""
        view = self._make_sync_view()

        link_data = {"_source": "serial", "local_port": "ttyS7"}
        interface = {"device_id": 1, "local_port_id": "serial:1007"}

        with patch.object(
            view, "handle_serial_cable_creation", return_value={"status": "valid", "interface": "ttyS7"}
        ) as mock_serial:
            result = view.handle_cable_creation(link_data, interface)

        mock_serial.assert_called_once_with(link_data, interface, force=False)
        assert result["status"] == "valid"

    def test_handle_cable_creation_non_serial_does_not_route_to_serial(self):
        """handle_cable_creation does NOT call handle_serial_cable_creation for main rows."""
        from dcim.models import Interface

        view = self._make_sync_view()

        link_data = {
            "_source": "main",
            "local_port": "Gi0/1",
            "netbox_local_interface_id": 1,
            "netbox_remote_interface_id": 2,
            "netbox_remote_device_id": 3,
        }
        interface = {"device_id": 1, "local_port_id": 101}

        iface = MagicMock(spec=Interface)
        iface.device_id = 1
        iface.cable = None

        with (
            patch.object(view, "handle_serial_cable_creation") as mock_serial,
            patch("netbox_librenms_plugin.views.sync.cables.Interface") as MockInterface,
            patch.object(view, "create_cable", return_value=True),
        ):
            MockInterface.objects.get.return_value = iface
            MockInterface.DoesNotExist = Interface.DoesNotExist
            view.handle_cable_creation(link_data, interface)

        mock_serial.assert_not_called()


# ---------------------------------------------------------------------------
# SingleCableVerifyView must handle serial rows (VC inline verify)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSingleCableVerifySerial:
    """A serial row reaching SingleCableVerifyView (the VC-member dropdown fires verify-cable for EVERY row, serial included) must resolve through the serial pipeline — not fall into the Interface lookup path, which would mislabel the row 'Missing Interface' with no Sync action."""

    def _verify_view(self, server_key="default"):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = server_key
        view.request = MagicMock()
        return view

    def test_serial_row_resolves_csp_and_remote_consoleport(self):
        from django.core.cache import cache

        acs, csps, _ = make_serial_device("acs-verify", csp_names=["ttyS7"])
        router, _, cps = make_serial_device("router-z", cp_names=["console"])
        csp = csps[0]

        link = {
            "local_port": "ttyS7",
            "local_port_id": f"serial:{csp.pk}-sensor",
            "_source": "serial",
            "remote_device": "router-z",
            "remote_port": None,
            "remote_device_id": None,
            "device_id": acs.id,
            "is_configured": True,
            "sensor_id": 1007,
            "sensor_index_int": 7,
        }

        view = self._verify_view()
        cache_key = view.get_cache_key(acs, "links", "default")
        cache.set(cache_key, {"links": [link]}, timeout=300)

        request = _make_request_json(
            {"device_id": acs.id, "local_port_id": link["local_port_id"], "server_key": "default"}
        )
        resp = view.post(request)
        import json as _json

        payload = _json.loads(resp.content)
        row = payload["formatted_row"]

        # Local port resolves to the ConsoleServerPort, carrying the Serial badge.
        assert "ttyS7" in row["local_port"]
        assert "Serial" in row["local_port"]
        assert (
            f"/dcim/console-server-ports/{csp.pk}/" in row["local_port"]
            or "consoleserverport" in row["local_port"].lower()
        )
        # Remote resolves to router-z's uncabled ConsolePort, and a Sync action is offered.
        assert "router-z" in row["remote_device"]
        assert "console" in row["remote_port"]
        assert "No Cable" in row["cable_status"]
        assert "Sync Cable" in row["actions"]

    def test_serial_row_with_existing_cable_offers_no_sync(self):
        from django.core.cache import cache

        acs, csps, _ = make_serial_device("acs-verify2", csp_names=["ttyS9"])
        router, _, cps = make_serial_device("router-y", cp_names=["console"])
        csp = csps[0]
        cable_together(csp, cps[0])  # already cabled

        link = {
            "local_port": "ttyS9",
            "local_port_id": f"serial:{csp.pk}-sensor",
            "_source": "serial",
            "remote_device": "router-y",
            "remote_port": None,
            "remote_device_id": None,
            "device_id": acs.id,
            "is_configured": True,
            "sensor_id": 1009,
            "sensor_index_int": 9,
        }

        view = self._verify_view()
        cache.set(view.get_cache_key(acs, "links", "default"), {"links": [link]}, timeout=300)

        request = _make_request_json(
            {"device_id": acs.id, "local_port_id": link["local_port_id"], "server_key": "default"}
        )
        import json as _json

        row = _json.loads(view.post(request).content)["formatted_row"]
        assert "Cable Found" in row["cable_status"]
        assert "Sync Cable" not in row["actions"]


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
        with patch.object(view, "enrich_local_port"):
            view.enrich_links_data([link], viewed)
        assert link["device_id"] == viewed.id  # non-serial rows are scoped to the viewed device


@pytest.mark.django_db
class TestSerialFetchSkippedWithoutHostId:
    """An OOB-only / unmapped device (no host librenms_id) must not call get_serial_port_sensors(None) — it can only return empty and, on a transient error, would wrongly mark the snapshot partial and suppress the valid OOB rows (#4/#6)."""

    def test_serial_fetch_skipped_when_no_host_librenms_id(self):
        obj, _csps, _ = make_serial_device("oob-only-serial", csp_names=["ttyS1"])
        view = _make_view()
        view._librenms_api.get_librenms_id.return_value = None  # OOB-only / unmapped host
        view._librenms_api.get_device_links.return_value = (False, "no host mapping")
        # Must not be reached: a None device id can only filter to nothing and waste a fetch.
        view._librenms_api.get_serial_port_sensors.side_effect = AssertionError("must not fetch with no host id")
        view.get_links_data(obj)
        view._librenms_api.get_serial_port_sensors.assert_not_called()
        assert view._serial_links_fetch_failed is False


# ---------------------------------------------------------------------------
# #5: cross-row ConsolePort dedup  ·  G-B2: verify-row dimming parity
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSerialRemoteCollisionDedup:
    """Two serial rows resolving to the same remote device must target distinct uncabled ConsolePorts — otherwise the 2nd sync hits the duplicate guard and a free port stays uncabled (#5)."""

    def test_enrich_serial_remote_excludes_claimed_ports(self):
        view = _make_view()
        remote, _, (cp_a, cp_b) = make_serial_device("router-collide", cp_names=["con-a", "con-b"])
        link1 = {
            "_source": "serial",
            "remote_device": remote.name,
            "netbox_local_interface_id": 1,
            "cable_status": "No Cable",
        }
        link2 = {
            "_source": "serial",
            "remote_device": remote.name,
            "netbox_local_interface_id": 2,
            "cable_status": "No Cable",
        }
        claimed = set()
        with patch.object(view, "get_device_by_id_or_name", return_value=(remote, True, None)):
            view.enrich_serial_remote(link1, claimed_cp_ids=claimed)
            view.enrich_serial_remote(link2, claimed_cp_ids=claimed)
        assert link1["netbox_remote_interface_id"] != link2["netbox_remote_interface_id"]
        assert {link1["netbox_remote_interface_id"], link2["netbox_remote_interface_id"]} == {cp_a.pk, cp_b.pk}

    def test_enrich_links_data_dedups_remote_cp_across_serial_rows(self):
        view = _make_view()
        local, (csp1, csp2), _ = make_serial_device("acs-collide", csp_names=["ttyS1", "ttyS2"])
        remote, _, (cp_a, cp_b) = make_serial_device("router-collide2", cp_names=["con-a", "con-b"])
        links = [
            {"local_port": "ttyS1", "_source": "serial", "remote_device": remote.name, "device_id": local.id},
            {"local_port": "ttyS2", "_source": "serial", "remote_device": remote.name, "device_id": local.id},
        ]
        with patch.object(view, "get_device_by_id_or_name", return_value=(remote, True, None)):
            view.enrich_links_data(links, local)
        ids = {link.get("netbox_remote_interface_id") for link in links}
        assert ids == {cp_a.pk, cp_b.pk}  # distinct CPs, no collision


@pytest.mark.django_db
class TestSerialVerifyRowDimming:
    """The inline verify-row render dims an unconfigured serial remote label, matching the table render (G-B2)."""

    def test_unconfigured_serial_remote_is_dimmed(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = object.__new__(SingleCableVerifyView)
        view.request = _mock_request()
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        dev, (csp,), _ = make_serial_device("acs-verify-dim", csp_names=["ttyS1"])
        link = {
            "_source": "serial",
            "local_port": "ttyS1",
            "remote_device": "raw-avocent-label",
            "is_configured": False,
        }
        # Remote label doesn't resolve to a NetBox device (real lookup) -> unconfigured, no URL.
        row = view._format_serial_verify_row(view.request, dev, link, local_port_id="serial:1001", server_key="default")
        assert "text-muted fst-italic" in row["remote_device"]  # dimmed, matching LibreNMSCableTable


class TestEnrichLinksDataReusesSyncDevice:
    """enrich_links_data must reuse the sync_device _prepare_context already resolved."""

    def test_passed_sync_device_avoids_resolve_query(self):
        from unittest.mock import patch

        view = _make_view()
        sentinel_device = MagicMock()
        with patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device") as mock_resolve:
            result = view.enrich_links_data([], MagicMock(), server_key="default", sync_device=sentinel_device)

        # The caller threaded in the already-resolved device, so enrich_links_data must NOT
        # re-run get_librenms_sync_device (a second VC-members query + per-member cf scan).
        mock_resolve.assert_not_called()
        assert result == []


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

    def _librenms_config_patches(self):
        cfg = patch("netbox_librenms_plugin.librenms_api.get_plugin_config")
        settings = patch("netbox_librenms_plugin.models.LibreNMSSettings")
        get = patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=self._routed_get())
        return cfg, settings, get

    def test_serial_rows_cached_and_syncable_when_host_links_404(self):
        from django.core.cache import cache
        from dcim.models import Cable, Device

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, csps, _ = make_serial_device("acs-ts26", csp_names=["ttyS7"])
        acs.custom_field_data["librenms_id"] = 13
        acs.save()
        _router, _, cps = make_serial_device("router-z", cp_names=["console"])
        csp = csps[0]
        console_port = cps[0]

        cfg, settings, get = self._librenms_config_patches()
        with cfg as mock_config, settings as mock_settings, get:
            mock_config.return_value = {
                "default": {
                    "librenms_url": "https://librenms.example.com",
                    "api_token": "test-token",
                    "cache_timeout": 300,
                    "verify_ssl": True,
                }
            }
            mock_settings.objects.filter.return_value.first.return_value = None

            # --- Refresh: real get_links_data hits the real (mocked) 404 on /links ---
            view = object.__new__(DeviceCableTableView)
            view.model = Device
            view.request = _mock_request()
            view._librenms_api = LibreNMSAPI(server_key="default")
            # Isolate the cache-write assertion from table rendering (RequestConfig needs a real
            # request); get_table is orthogonal to the bug under test.
            with patch.object(view, "get_table"):
                view._prepare_context(view.request, acs, fetch_fresh=True, server_key="default")

            cache_key = view.get_cache_key(acs, "links", "default")
            cached = cache.get(cache_key)
            assert cached is not None, "a host /links 404 must NOT delete the serial cache"
            serial_rows = [link for link in cached["links"] if link.get("_source") == "serial"]
            assert len(serial_rows) == 1
            row = serial_rows[0]
            assert row["local_port"] == "ttyS7"
            assert row["netbox_local_interface_id"] == csp.pk
            assert row["netbox_remote_interface_id"] == console_port.pk
            assert row["can_create_cable"] is True

            # --- Sync: real SyncCablesView reads that cache and creates the cable ---
            sync = object.__new__(SyncCablesView)
            sync.request = view.request
            sync._librenms_api = LibreNMSAPI(server_key="default")
            sync._post_server_key = "default"

            cached_links = sync.get_cached_links_data(view.request, acs)
            assert cached_links, "cache present → validate_prerequisites passes, no 'Cache has expired'"
            assert sync.validate_prerequisites(cached_links, [{"local_port_id": row["local_port_id"]}]) is True

            result = sync.process_single_interface(
                {"device_id": acs.id, "local_port_id": row["local_port_id"]}, cached_links
            )
            assert result["status"] == "valid"

        # The cable really exists between the ConsoleServerPort and the remote ConsolePort.
        csp.refresh_from_db()
        console_port.refresh_from_db()
        assert csp.cable is not None
        assert console_port.cable_id == csp.cable_id
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

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        acs, csps, _ = make_serial_device("acs-htmx", csp_names=["ttyS3"])
        _router, _, cps = make_serial_device("router-htmx", cp_names=["console"])
        csp, cp = csps[0], cps[0]

        # Seed the enriched links cache that "Refresh Cables" would have written (serial rows
        # re-enrich purely against the DB, so the re-render needs no live LibreNMS call).
        key_view = object.__new__(SyncCablesView)
        cache_key = key_view.get_cache_key(acs, "links", "default")
        link = {
            "local_port": "ttyS3",
            "local_port_id": f"serial:{csp.pk}-s",
            "_source": "serial",
            "device_id": acs.id,
            "remote_device": "router-htmx",
            "netbox_local_interface_id": csp.pk,
            "netbox_remote_interface_id": cp.pk,
            "can_create_cable": True,
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
        resp = client.post(url, data={"select": link["local_port_id"], "server_key": "default"}, **extra)
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


# ---------------------------------------------------------------------------
# Cable enrichment: created cables carry the librenms tag, color, description, tenant
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestCableEnrichment:
    """``create_cable`` stamps provenance on every cable the sync creates: the ``librenms``
    tag (so the plugin can later recognise/own its own cables for the planned DCIM remodel),
    a configured color + description carrying the server key, and the REMOTE device's tenant.
    Driven end-to-end against real ConsoleServerPort / ConsolePort / Tenant rows through
    ``handle_serial_cable_creation`` → ``create_cable`` (the write point shared with the
    non-serial Interface↔Interface path).
    """

    def _sync_one(self, csp, cp, server_key="production"):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        sync = object.__new__(SyncCablesView)
        sync.request = _mock_request()
        sync._post_server_key = server_key
        link = {
            "local_port": csp.name,
            "_source": "serial",
            "netbox_local_interface_id": csp.pk,
            "netbox_remote_interface_id": cp.pk,
        }
        return sync.handle_serial_cable_creation(link, {"device_id": csp.device_id})

    def test_created_cable_carries_tag_color_description_and_remote_tenant(self):
        from dcim.models import Cable
        from netbox.plugins import get_plugin_config
        from tenancy.models import Tenant

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
        # Color + description come from plugin config; description carries the server key.
        assert cable.color == get_plugin_config("netbox_librenms_plugin", "cable_sync_tag_color")
        assert "production" in cable.description
        # Tenant is the REMOTE side's tenant (the target device), not the terminal server's.
        assert cable.tenant_id == tenant.pk

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
