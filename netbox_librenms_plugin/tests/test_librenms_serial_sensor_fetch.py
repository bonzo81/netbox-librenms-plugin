"""
Error and skip paths of ``LibreNMSAPI._fetch_serial_port_sensors``.

``test_librenms_api.py::TestGetSerialPortSensors`` covers the success and malformed-payload
branches, the empty-table 404, and the non-JSON 200. These are the paths it does not reach: a
404 that means the endpoint is missing rather than the table empty, a 404 whose body is not
JSON, and the warning a row with an unreadable type has to raise. They are pinned here because
that file is edited by every branch above this one, so appending to it would fight each restack.

Every case drives the real HTTP client against the loopback LibreNMS.
"""

import pytest

pytestmark = pytest.mark.django_db  # the fetch reads the SerialSensorTypePattern rows

SENSORS_PATH = "/api/v0/resources/sensors"


def _sensor(device_id, *, sensor_type="acsSerialPortTable", port_num=7):
    """One recognized serial sensor row as LibreNMS returns it."""
    return {
        "sensor_id": 1000 + port_num,
        "device_id": device_id,
        "sensor_type": sensor_type,
        "sensor_index": f"acsSerialPortTableStatus.{port_num}",
        "sensor_descr": f"device-{port_num} Status",
        "sensor_current": 2,
        "group": "Serial Ports",
    }


class TestSensorsEndpointNotFound:
    """A 404 carries two meanings and must not collapse into one.

    The other meaning, an empty sensor table reading as success with zero rows, is pinned by
    ``test_librenms_api.py::TestGetSerialPortSensors::test_librenms_empty_inventory_404_returns_empty``.
    """

    def test_a_missing_endpoint_is_reported_as_a_failure(self, mock_librenms_api, librenms_server):
        """A 404 that does NOT say the table is empty is a real fetch failure."""
        librenms_server.register(SENSORS_PATH, {"status": "error", "message": "No such route"}, status=404)
        mock_librenms_api.librenms_url = librenms_server.url

        success, message = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is False
        assert message == "Sensors resource endpoint not found"

    def test_a_404_without_a_json_body_is_reported_as_a_failure(self, mock_librenms_api, librenms_server):
        """The 404 branch parses the body, so a non-JSON one must not raise."""
        librenms_server.register_raw(SENSORS_PATH, "<html>not found</html>", status=404, content_type="text/html")
        mock_librenms_api.librenms_url = librenms_server.url

        success, message = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is False
        assert message == "Sensors resource endpoint not found"


class TestUnreadableSensorType:
    """A row whose type is not a string names no serial type, so it is skipped, not fatal."""

    def test_a_non_string_sensor_type_is_skipped_and_the_others_still_resolve(
        self, mock_librenms_api, librenms_server, caplog
    ):
        wanted = _sensor(12, port_num=7)
        unreadable = {**_sensor(12, port_num=9), "sensor_type": 1234}
        librenms_server.register(SENSORS_PATH, {"status": "ok", "sensors": [unreadable, wanted]})
        mock_librenms_api.librenms_url = librenms_server.url

        with caplog.at_level("WARNING", logger="netbox_librenms_plugin.librenms_api"):
            success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert [row["sensor_id"] for row in data] == [wanted["sensor_id"]]
        assert any("non-string sensor_type" in record.getMessage() for record in caplog.records), (
            "a skipped row must be reported, or a broken feed looks like a device with no serial ports"
        )

    def test_the_skip_count_names_every_unreadable_row(self, mock_librenms_api, librenms_server, caplog):
        sensors = [{**_sensor(12, port_num=port), "sensor_type": None} for port in (1, 2, 3)]
        librenms_server.register(SENSORS_PATH, {"status": "ok", "sensors": sensors})
        mock_librenms_api.librenms_url = librenms_server.url

        with caplog.at_level("WARNING", logger="netbox_librenms_plugin.librenms_api"):
            success, data = mock_librenms_api.get_serial_port_sensors(device_id=12)

        assert success is True
        assert data == []
        assert any("Skipped 3 sensor(s)" in record.getMessage() for record in caplog.records)
