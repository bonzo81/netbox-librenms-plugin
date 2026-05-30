"""Tests for serial_utils.py — pure mapper, no Django DB required."""

import json
import os
import pytest

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "acs6048_sensors_fixture.json")


def _load_fixture() -> list[dict]:
    with open(FIXTURE_PATH) as f:
        return json.load(f)


class TestParsePortNumber:
    def test_standard_index(self):
        from netbox_librenms_plugin.serial_utils import parse_port_number

        assert parse_port_number("acsSerialPortTableStatus.7") == 7

    def test_two_digit_index(self):
        from netbox_librenms_plugin.serial_utils import parse_port_number

        assert parse_port_number("acsSerialPortTableStatus.49") == 49

    def test_no_suffix_returns_none(self):
        from netbox_librenms_plugin.serial_utils import parse_port_number

        assert parse_port_number("acsSerialPortTableStatus") is None

    def test_empty_string_returns_none(self):
        from netbox_librenms_plugin.serial_utils import parse_port_number

        assert parse_port_number("") is None

    def test_none_returns_none(self):
        from netbox_librenms_plugin.serial_utils import parse_port_number

        assert parse_port_number(None) is None


class TestStripStatusSuffix:
    def test_strips_status(self):
        from netbox_librenms_plugin.serial_utils import strip_status_suffix

        assert strip_status_suffix("PROD-LAB03A-RA1 Status") == "PROD-LAB03A-RA1"

    def test_strips_default_port_name(self):
        from netbox_librenms_plugin.serial_utils import strip_status_suffix

        assert strip_status_suffix("ttyS49 Status") == "ttyS49"

    def test_no_suffix_unchanged(self):
        from netbox_librenms_plugin.serial_utils import strip_status_suffix

        assert strip_status_suffix("bare label") == "bare label"

    def test_empty_string(self):
        from netbox_librenms_plugin.serial_utils import strip_status_suffix

        assert strip_status_suffix("") == ""


class TestMapSensorsToSerialLinks:
    def test_basic_row_shape(self):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        sensors = [
            {
                "sensor_id": 1975,
                "device_id": 12,
                "sensor_type": "acsSerialPortTable",
                "sensor_index": "acsSerialPortTableStatus.11",
                "sensor_descr": "PROD-LAB03A-RA1 Status",
                "sensor_current": 2,
                "group": "Serial Ports",
            }
        ]
        links = map_sensors_to_serial_links(sensors)
        assert len(links) == 1
        row = links[0]
        assert row["local_port"] == "ttyS11"
        assert row["local_port_id"] == "serial:1975"
        assert row["remote_device"] == "PROD-LAB03A-RA1"
        assert row["remote_port"] is None
        assert row["remote_device_id"] is None
        assert row["is_configured"] is True
        assert row["_source"] == "serial"
        assert row["sensor_id"] == 1975
        assert row["sensor_index_int"] == 11

    def test_default_named_port_is_not_configured(self):
        """A port whose label still equals the default name (ttyS49) is unconfigured."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        sensors = [
            {
                "sensor_id": 2016,
                "device_id": 12,
                "sensor_type": "acsSerialPortTable",
                "sensor_index": "acsSerialPortTableStatus.49",
                "sensor_descr": "ttyS49 Status",
                "sensor_current": 1,
                "group": "Serial Ports",
            }
        ]
        links = map_sensors_to_serial_links(sensors)
        assert len(links) == 1
        assert links[0]["is_configured"] is False
        assert links[0]["remote_device"] == "ttyS49"

    def test_unknown_sensor_type_skipped(self):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        sensors = [
            {
                "sensor_id": 9999,
                "device_id": 12,
                "sensor_type": "someOtherTable",
                "sensor_index": "someOtherTable.1",
                "sensor_descr": "Foo Status",
                "sensor_current": 2,
                "group": "Other",
            }
        ]
        assert map_sensors_to_serial_links(sensors) == []

    def test_unparseable_index_skipped(self):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        sensors = [
            {
                "sensor_id": 9998,
                "device_id": 12,
                "sensor_type": "acsSerialPortTable",
                "sensor_index": "acsSerialPortTableStatus",  # no trailing .N
                "sensor_descr": "PROD-LAB03A-RA1 Status",
                "sensor_current": 2,
                "group": "Serial Ports",
            }
        ]
        assert map_sensors_to_serial_links(sensors) == []

    def test_sorted_by_port_number(self):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        sensors = [
            {
                "sensor_id": 100 + n,
                "device_id": 12,
                "sensor_type": "acsSerialPortTable",
                "sensor_index": f"acsSerialPortTableStatus.{n}",
                "sensor_descr": f"device-{n} Status",
                "sensor_current": 2,
                "group": "Serial Ports",
            }
            for n in [33, 7, 49, 11]
        ]
        links = map_sensors_to_serial_links(sensors)
        assert [r["sensor_index_int"] for r in links] == [7, 11, 33, 49]

    def test_custom_port_name_pattern(self):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        sensors = [
            {
                "sensor_id": 200,
                "device_id": 12,
                "sensor_type": "acsSerialPortTable",
                "sensor_index": "acsSerialPortTableStatus.3",
                "sensor_descr": "MyDevice Status",
                "sensor_current": 2,
                "group": "Serial Ports",
            }
        ]
        links = map_sensors_to_serial_links(sensors, port_name_pattern="serial{N}")
        assert links[0]["local_port"] == "serial3"

    def test_empty_input(self):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        assert map_sensors_to_serial_links([]) == []


class TestMapSensorsWithFixture:
    """Integration-style tests using the captured device-12 (ACS6048) fixture."""

    @pytest.fixture(scope="class")
    def fixture_sensors(self):
        return _load_fixture()

    def test_fixture_loads_49_sensors(self, fixture_sensors):
        assert len(fixture_sensors) == 49

    def test_all_are_acs_type(self, fixture_sensors):
        for s in fixture_sensors:
            assert s["sensor_type"] == "acsSerialPortTable"

    def test_mapper_returns_49_rows(self, fixture_sensors):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(fixture_sensors)
        assert len(links) == 49

    def test_all_source_serial(self, fixture_sensors):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(fixture_sensors)
        assert all(r["_source"] == "serial" for r in links)

    def test_ports_numbered_1_to_49(self, fixture_sensors):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(fixture_sensors)
        assert [r["sensor_index_int"] for r in links] == list(range(1, 50))

    def test_port_11_is_prod_lab03a_ra1(self, fixture_sensors):
        """Index 11 = PROD-LAB03A-RA1 — the clearest resolvable label in the fixture."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(fixture_sensors)
        row = links[10]  # 0-indexed, port 11
        assert row["sensor_index_int"] == 11
        assert row["local_port"] == "ttyS11"
        assert row["remote_device"] == "PROD-LAB03A-RA1"
        assert row["is_configured"] is True

    def test_port_49_is_default_unconfigured(self, fixture_sensors):
        """Index 49 = ttyS49 Status — the unused appliance port, not configured."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(fixture_sensors)
        row = links[48]  # last row
        assert row["sensor_index_int"] == 49
        assert row["local_port"] == "ttyS49"
        assert row["remote_device"] == "ttyS49"
        assert row["is_configured"] is False

    def test_port_44_vendor_alias_is_configured(self, fixture_sensors):
        """Index 44 = '7750SR-7s-con1' — vendor alias, configured but won't resolve."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(fixture_sensors)
        row = links[43]
        assert row["sensor_index_int"] == 44
        assert row["remote_device"] == "7750SR-7s-con1"
        assert row["is_configured"] is True

    def test_configured_count(self, fixture_sensors):
        """48 out of 49 ports have custom labels (all except ttyS49)."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(fixture_sensors)
        configured = [r for r in links if r["is_configured"]]
        unconfigured = [r for r in links if not r["is_configured"]]
        assert len(unconfigured) == 1
        assert unconfigured[0]["local_port"] == "ttyS49"
        assert len(configured) == 48

    def test_local_port_id_format(self, fixture_sensors):
        """All local_port_ids follow the 'serial:<sensor_id>' pattern."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(fixture_sensors)
        for row in links:
            assert row["local_port_id"].startswith("serial:")
            assert row["local_port_id"].split(":", 1)[1].isdigit()
