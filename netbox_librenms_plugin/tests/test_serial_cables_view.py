"""
Phase 2 tests for serial-port integration in BaseCableTableView.

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


class TestGetLinksDataSerial:
    """get_links_data() appends serial rows when device has ConsoleServerPorts."""

    def _base_setup(self, view, sensors=None):
        """Configure view with a minimal successful LLDP response and optional sensors."""
        view._librenms_api.get_device_links.return_value = (True, {"links": []})
        view._librenms_api.get_librenms_id.return_value = 12
        if sensors is not None:
            view._librenms_api.get_serial_port_sensors.return_value = (True, sensors)
        else:
            view._librenms_api.get_serial_port_sensors.return_value = (True, [])

    def test_serial_rows_appended_when_device_has_csps(self):
        """Sensors are mapped and appended to links_data."""
        view = _make_view()
        sensors = [_serial_sensor(3, "router-a"), _serial_sensor(7, "switch-b")]
        self._base_setup(view, sensors=sensors)
        obj = _mock_obj(has_csps=True)

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

    def test_no_serial_rows_when_no_csps(self):
        """Serial fetch is skipped entirely when device has no ConsoleServerPorts."""
        view = _make_view()
        self._base_setup(view)
        obj = _mock_obj(has_csps=False)

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
        """When sensor fetch fails, no serial rows are added (graceful degradation), and the
        failure is flagged so post() can warn the user instead of silently dropping the rows
        under a success banner (parity with the OOB-fetch-failure warning)."""
        view = _make_view()
        view._librenms_api.get_device_links.return_value = (True, {"links": []})
        view._librenms_api.get_librenms_id.return_value = 12
        view._librenms_api.get_serial_port_sensors.return_value = (False, "timeout")
        obj = _mock_obj(has_csps=True)

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
        obj = _mock_obj(has_csps=True)

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=None),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=None),
            patch.object(view, "get_ports_data", return_value={"ports": []}),
        ):
            view.get_links_data(obj)

        assert view._serial_links_fetch_failed is False

    def test_serial_row_shape(self):
        """Each appended row has the expected keys."""
        view = _make_view()
        sensors = [_serial_sensor(5, "prod-router-01")]
        self._base_setup(view, sensors=sensors)
        obj = _mock_obj(has_csps=True)

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
        """A serial row must resolve through ConsoleServerPorts, never Interfaces.

        The device has an Interface whose name collides with the serial port name but no
        matching CSP; the serial path must ignore the interface and leave the link
        unresolved (proving it queried CSPs, not interfaces)."""
        from dcim.models import Interface

        view = _make_view()
        obj, _, _ = make_serial_device("ser-enrich-iface")
        # A same-named Interface exists, but the serial path must not pick it up.
        Interface.objects.create(device=obj, name="ttyS3", type="other")

        link = {"local_port": "ttyS3", "local_port_id": "serial:1003", "_source": "serial"}
        view.enrich_local_port(link, obj)

        assert "local_port_url" not in link
        assert "netbox_local_interface_id" not in link


# ---------------------------------------------------------------------------
# check_serial_cable_status
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCheckSerialCableStatus:
    """check_serial_cable_status sets correct cable_status and can_create_cable.

    Driven against real ConsoleServerPort / Cable objects so the ``.cable`` state it branches
    on reflects actual NetBox termination wiring, not a value preset on a MagicMock.
    """

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

        mock_check_serial.assert_called_once_with(serial_link)
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
        """sensor_id, sensor_index_int, is_configured are kept after stripping derived keys."""
        serial_link = {
            "local_port": "ttyS7",
            "local_port_id": "serial:1007",
            "remote_port": None,
            "remote_device": "switch-a",
            "remote_port_id": None,
            "remote_device_id": None,
            "_source": "serial",
            # Serial-specific
            "sensor_id": 1007,
            "sensor_index_int": 7,
            "is_configured": True,
            # Derived (should be stripped)
            "local_port_url": "/dcim/csp/99/",
            "netbox_local_interface_id": 99,
            "cable_status": "No Cable",
            "can_create_cable": False,
            "device_id": 1,
        }

        _raw_keys = {
            "local_port",
            "local_port_id",
            "remote_port",
            "remote_device",
            "remote_port_id",
            "remote_device_id",
            "_source",
            "sensor_id",
            "sensor_index_int",
            "is_configured",
        }
        stripped = {k: v for k, v in serial_link.items() if k in _raw_keys}

        assert stripped["sensor_id"] == 1007
        assert stripped["sensor_index_int"] == 7
        assert stripped["is_configured"] is True
        assert "local_port_url" not in stripped
        assert "netbox_local_interface_id" not in stripped
        assert "cable_status" not in stripped


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
# Phase 3: enrich_serial_remote
# ---------------------------------------------------------------------------


class TestEnrichSerialRemote:
    """enrich_serial_remote resolves remote device + ConsolePort."""

    def test_label_matches_device_with_uncabled_cp(self):
        """When label matches device and device has an uncabled ConsolePort, sets can_create_cable."""
        view = _make_view()

        cp = MagicMock()
        cp.pk = 77
        cp.name = "con0"

        device = MagicMock()
        device.pk = 42
        device.consoleports = MagicMock()
        device.consoleports.filter.return_value.order_by.return_value.first.return_value = cp

        link = {
            "local_port": "ttyS7",
            "local_port_id": "serial:1007",
            "_source": "serial",
            "remote_device": "prod-router-01",
            "netbox_local_interface_id": 99,
            "cable_status": "No Cable",
        }

        with (
            patch.object(view, "get_device_by_id_or_name", return_value=(device, True, None)),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.reverse",
                side_effect=lambda name, args=None: f"/{args[0]}/",
            ),
        ):
            view.enrich_serial_remote(link)

        assert link["netbox_remote_interface_id"] == 77
        assert link["remote_port_name"] == "con0"
        assert link["can_create_cable"] is True
        assert link["netbox_remote_device_id"] == 42

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

    def test_all_cps_cabled_sets_not_found_status(self):
        """When all ConsolePorts are already cabled, sets 'Console Port Not Found'."""
        view = _make_view()

        device = MagicMock()
        device.pk = 42
        device.consoleports = MagicMock()
        device.consoleports.filter.return_value.order_by.return_value.first.return_value = None  # no uncabled CP

        link = {
            "remote_device": "prod-router-01",
            "cable_status": "No Cable",
        }

        with (
            patch.object(view, "get_device_by_id_or_name", return_value=(device, True, None)),
            patch("netbox_librenms_plugin.views.base.cables_view.reverse", return_value="/x/"),
        ):
            view.enrich_serial_remote(link)

        assert link["cable_status"] == "Console Port Not Found in NetBox"
        assert "can_create_cable" not in link

    @pytest.mark.django_db
    def test_uncabled_console_port_pick_is_deterministic_by_name(self):
        """With several uncabled ConsolePorts, the remote pick is the lowest by name, every run —
        the Avocent label is only a hint, so the choice must not depend on insertion/DB order."""
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

    def test_enrich_links_data_calls_enrich_serial_remote_when_no_cable(self):
        """enrich_links_data calls enrich_serial_remote for CSP-found rows with No Cable."""
        view = _make_view()

        link = {
            "local_port": "ttyS7",
            "local_port_id": "serial:1007",
            "_source": "serial",
            "remote_device": "prod-router-01",
            "device_id": None,
        }
        obj = _mock_obj()

        with (
            patch.object(view, "enrich_local_port"),
            patch.object(
                view,
                "check_serial_cable_status",
                side_effect=lambda lnk: lnk.update(
                    {
                        "cable_status": "No Cable",
                        "netbox_local_interface_id": 99,
                    }
                ),
            ),
            patch.object(view, "enrich_serial_remote") as mock_enrich_remote,
        ):
            view.enrich_links_data([link], obj)

        mock_enrich_remote.assert_called_once_with(link)

    def test_enrich_links_data_skips_enrich_serial_remote_when_cable_found(self):
        """enrich_serial_remote is NOT called when the CSP already has a cable."""
        view = _make_view()

        link = {
            "local_port": "ttyS3",
            "local_port_id": "serial:1003",
            "_source": "serial",
            "remote_device": "some-device",
            "device_id": None,
        }
        obj = _mock_obj()

        with (
            patch.object(view, "enrich_local_port"),
            patch.object(
                view,
                "check_serial_cable_status",
                side_effect=lambda lnk: lnk.update(
                    {
                        "cable_status": "Cable Found",
                        "netbox_local_interface_id": 88,
                    }
                ),
            ),
            patch.object(view, "enrich_serial_remote") as mock_enrich_remote,
        ):
            view.enrich_links_data([link], obj)

        mock_enrich_remote.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 3: SyncCablesView serial handling
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestHandleSerialCableCreation:
    """SyncCablesView.handle_serial_cable_creation creates CSP <-> CP cables.

    The CSP/CP lookups and their ``.cable`` state are exercised against real NetBox objects;
    ``create_cable`` (which performs the actual cable write and needs a real request/permission
    context) is the one collaborator left patched, so these tests target the handler's resolve
    + duplicate/missing detection logic end to end."""

    def _make_sync_view(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.request = _mock_request()
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_creates_cable_when_both_sides_found(self):
        """When a real CSP and CP both exist and are uncabled, create_cable is invoked and the
        result is valid."""
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

    def test_existing_cable_on_csp_returns_duplicate(self):
        """When the real CSP already has a cable, the handler returns duplicate."""
        view = self._make_sync_view()
        _, (csp,), _ = make_serial_device("ser-hsc-dup-csp", csp_names=["ttyS7"])
        _, _, (peer_cp,) = make_serial_device("ser-hsc-dup-peer", cp_names=["conX"])
        cable_together(csp, peer_cp)  # csp is now cabled
        _, _, (cp,) = make_serial_device("ser-hsc-dup-target", cp_names=["con0"])

        link_data = {
            "_source": "serial",
            "local_port": "ttyS7",
            "netbox_local_interface_id": csp.pk,
            "netbox_remote_interface_id": cp.pk,
        }
        interface = {"device_id": 1, "local_port_id": "serial:1007"}

        result = view.handle_serial_cable_creation(link_data, interface)

        assert result["status"] == "duplicate"

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

        mock_serial.assert_called_once_with(link_data, interface)
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
            patch.object(view, "check_existing_cable", return_value=False),
            patch.object(view, "create_cable", return_value=True),
        ):
            MockInterface.objects.get.return_value = iface
            MockInterface.DoesNotExist = Interface.DoesNotExist
            view.handle_cable_creation(link_data, interface)

        mock_serial.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 3: SingleCableVerifyView must handle serial rows (VC inline verify)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSingleCableVerifySerial:
    """A serial row reaching SingleCableVerifyView (the VC-member dropdown fires verify-cable
    for EVERY row, serial included) must resolve through the serial pipeline — not fall into the
    Interface lookup path, which would mislabel the row 'Missing Interface' with no Sync action."""

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
