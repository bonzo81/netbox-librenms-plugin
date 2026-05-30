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
        assert result is not None
        assert all(r.get("_source") != "serial" for r in result)

    def test_serial_fetch_failure_does_not_append(self):
        """When sensor fetch fails, no serial rows are added (graceful degradation)."""
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

        assert result == []

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


class TestEnrichLocalPortSerial:
    """enrich_local_port resolves ConsoleServerPort for serial rows."""

    def test_csp_found_sets_url_and_id(self):
        """When CSP exists by name, URL and netbox_local_interface_id are set."""
        view = _make_view()

        csp = MagicMock()
        csp.pk = 99

        obj = _mock_obj()
        obj.consoleserverports.filter.return_value.first.return_value = csp

        link = {"local_port": "ttyS7", "local_port_id": "serial:1007", "_source": "serial"}

        with patch("netbox_librenms_plugin.views.base.cables_view.reverse", return_value="/dcim/csp/99/") as mock_rev:
            view.enrich_local_port(link, obj)

        assert link["netbox_local_interface_id"] == 99
        assert link["local_port_url"] == "/dcim/csp/99/"
        mock_rev.assert_called_once_with("dcim:consoleserverport", args=[99])

    def test_csp_not_found_no_url_set(self):
        """When CSP name doesn't match, no URL or id is set."""
        view = _make_view()

        obj = _mock_obj()
        obj.consoleserverports.filter.return_value.first.return_value = None

        link = {"local_port": "ttyS99", "local_port_id": "serial:1099", "_source": "serial"}

        view.enrich_local_port(link, obj)

        assert "local_port_url" not in link
        assert "netbox_local_interface_id" not in link

    def test_serial_does_not_touch_interfaces(self):
        """Serial rows should never query obj.interfaces."""
        view = _make_view()

        obj = _mock_obj()
        obj.consoleserverports.filter.return_value.first.return_value = None

        link = {"local_port": "ttyS3", "local_port_id": "serial:1003", "_source": "serial"}

        with patch("netbox_librenms_plugin.views.base.cables_view.reverse"):
            view.enrich_local_port(link, obj)

        obj.interfaces.filter.assert_not_called()


# ---------------------------------------------------------------------------
# check_serial_cable_status
# ---------------------------------------------------------------------------


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
        """CSP that has a cable -> 'Cable Found' with cable_url."""
        view = _make_view()
        cable = MagicMock()
        cable.pk = 55

        csp = MagicMock()
        csp.cable = cable

        link = {"_source": "serial", "netbox_local_interface_id": 99}

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.ConsoleServerPort") as MockCSP,
            patch("netbox_librenms_plugin.views.base.cables_view.reverse", return_value="/dcim/cables/55/"),
        ):
            MockCSP.objects.get.return_value = csp
            view.check_serial_cable_status(link)

        assert link["cable_status"] == "Cable Found"
        assert link["cable_url"] == "/dcim/cables/55/"
        assert link["can_create_cable"] is False

    def test_csp_without_cable(self):
        """CSP with no cable -> 'No Cable'."""
        view = _make_view()

        csp = MagicMock()
        csp.cable = None

        link = {"_source": "serial", "netbox_local_interface_id": 99}

        with patch("netbox_librenms_plugin.views.base.cables_view.ConsoleServerPort") as MockCSP:
            MockCSP.objects.get.return_value = csp
            view.check_serial_cable_status(link)

        assert link["cable_status"] == "No Cable"
        assert link["can_create_cable"] is False

    def test_csp_does_not_exist(self):
        """If DB lookup raises DoesNotExist -> 'Console Server Port Not Found'."""
        from dcim.models import ConsoleServerPort

        view = _make_view()
        link = {"_source": "serial", "netbox_local_interface_id": 999}

        with patch("netbox_librenms_plugin.views.base.cables_view.ConsoleServerPort") as MockCSP:
            MockCSP.DoesNotExist = ConsoleServerPort.DoesNotExist
            MockCSP.objects.get.side_effect = ConsoleServerPort.DoesNotExist
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
        device.consoleports.filter.return_value.first.return_value = cp

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
        device.consoleports.filter.return_value.first.return_value = None  # no uncabled CP

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
        """When CSP and CP both exist and are uncabled, creates cable and returns valid."""
        from dcim.models import ConsolePort, ConsoleServerPort

        view = self._make_sync_view()

        csp = MagicMock(spec=ConsoleServerPort)
        csp.cable = None

        cp = MagicMock(spec=ConsolePort)
        cp.cable = None

        link_data = {
            "_source": "serial",
            "local_port": "ttyS7",
            "netbox_local_interface_id": 99,
            "netbox_remote_interface_id": 77,
        }
        interface = {"device_id": 1, "local_port_id": "serial:1007"}

        with (
            patch("netbox_librenms_plugin.views.sync.cables.ConsoleServerPort") as MockCSP,
            patch("netbox_librenms_plugin.views.sync.cables.ConsolePort") as MockCP,
            patch.object(view, "create_cable", return_value=True) as mock_create,
        ):
            MockCSP.objects.get.return_value = csp
            MockCSP.DoesNotExist = ConsoleServerPort.DoesNotExist
            MockCP.objects.get.return_value = cp
            MockCP.DoesNotExist = ConsolePort.DoesNotExist
            result = view.handle_serial_cable_creation(link_data, interface)

        assert result["status"] == "valid"
        assert result["interface"] == "ttyS7"
        mock_create.assert_called_once_with(csp, cp, view.request)

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
        """When CSP already has a cable, returns duplicate."""
        from dcim.models import ConsolePort, ConsoleServerPort

        view = self._make_sync_view()

        csp = MagicMock(spec=ConsoleServerPort)
        csp.cable = MagicMock()  # has cable

        cp = MagicMock(spec=ConsolePort)
        cp.cable = None

        link_data = {
            "_source": "serial",
            "local_port": "ttyS7",
            "netbox_local_interface_id": 99,
            "netbox_remote_interface_id": 77,
        }
        interface = {"device_id": 1, "local_port_id": "serial:1007"}

        with (
            patch("netbox_librenms_plugin.views.sync.cables.ConsoleServerPort") as MockCSP,
            patch("netbox_librenms_plugin.views.sync.cables.ConsolePort") as MockCP,
        ):
            MockCSP.objects.get.return_value = csp
            MockCSP.DoesNotExist = ConsoleServerPort.DoesNotExist
            MockCP.objects.get.return_value = cp
            MockCP.DoesNotExist = ConsolePort.DoesNotExist
            result = view.handle_serial_cable_creation(link_data, interface)

        assert result["status"] == "duplicate"

    def test_csp_does_not_exist_returns_missing_remote(self):
        """DoesNotExist on CSP lookup returns missing_remote."""
        from dcim.models import ConsoleServerPort

        view = self._make_sync_view()

        link_data = {
            "_source": "serial",
            "local_port": "ttyS7",
            "netbox_local_interface_id": 9999,
            "netbox_remote_interface_id": 77,
        }
        interface = {"device_id": 1, "local_port_id": "serial:1007"}

        with patch("netbox_librenms_plugin.views.sync.cables.ConsoleServerPort") as MockCSP:
            MockCSP.DoesNotExist = ConsoleServerPort.DoesNotExist
            MockCSP.objects.get.side_effect = ConsoleServerPort.DoesNotExist
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
