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

        assert strip_status_suffix("rdev-d9b298-RA1 Status") == "rdev-d9b298-RA1"

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
                "sensor_descr": "rdev-d9b298-RA1 Status",
                "sensor_current": 2,
                "group": "Serial Ports",
            }
        ]
        links = map_sensors_to_serial_links(sensors)
        assert len(links) == 1
        row = links[0]
        assert row["local_port"] == "ttyS11"
        assert row["local_port_id"] == "serial:1975"
        assert row["remote_device"] == "rdev-d9b298-RA1"
        assert row["remote_port"] is None
        assert row["remote_device_id"] is None
        assert row["is_configured"] is True
        assert row["_source"] == "serial"
        assert row["sensor_id"] == 1975
        assert row["sensor_index_int"] == 11
        # device_id defaults to None when the caller doesn't supply it.
        assert row["device_id"] is None

    def test_device_id_threaded_into_rows(self):
        """The caller's NetBox device_id is carried on each row so the cable table's row_attrs (record["device_id"]) is satisfied even before enrich_links_data runs."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        sensors = [
            {
                "sensor_id": 7007,
                "device_id": 12,
                "sensor_type": "acsSerialPortTable",
                "sensor_index": "acsSerialPortTableStatus.7",
                "sensor_descr": "host-7 Status",
                "sensor_current": 2,
                "group": "Serial Ports",
            }
        ]
        links = map_sensors_to_serial_links(sensors, device_id=55)
        assert links[0]["device_id"] == 55

    @pytest.mark.django_db
    def test_serial_row_renders_in_cable_table_without_keyerror(self):
        """End-to-end: a serial row must render in LibreNMSCableTable."""
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links
        from netbox_librenms_plugin.tables.cables import LibreNMSCableTable
        from netbox_librenms_plugin.tests.conftest import make_device

        dev = make_device("serial-tbl-dev")
        rows = map_sensors_to_serial_links(
            [
                {
                    "sensor_id": 7007,
                    "device_id": 12,
                    "sensor_type": "acsSerialPortTable",
                    "sensor_index": "acsSerialPortTableStatus.7",
                    "sensor_descr": "host-7 Status",
                    "sensor_current": 2,
                    "group": "Serial Ports",
                }
            ],
            device_id=dev.id,
        )
        table = LibreNMSCableTable(rows, device=dev)
        request = RequestFactory().get("/")
        RequestConfig(request).configure(table)
        html = table.as_html(request)  # evaluates row_attrs (record["device_id"]) per row
        assert f'data-device="{dev.id}"' in html

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

    def test_non_list_sensors_fails_closed_to_empty(self):
        """A None/non-iterable top-level payload (malformed LibreNMS response) must return [], not crash.

        Without the guard, iterating ``sensors`` raises before any per-row hardening runs, so the
        serial-sync path would 500 instead of degrading to "no serial links".
        """
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        for bad in (None, {"sensor_id": 1}, 42, "not-a-list"):
            assert map_sensors_to_serial_links(bad) == []

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
                "sensor_descr": "rdev-d9b298-RA1 Status",
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
        # Per-type naming is set via the sensor_types map; it takes precedence over the fallback
        # port_name_pattern (so the configured map, not the default, wins for a known type).
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
        links = map_sensors_to_serial_links(sensors, sensor_types={"acsSerialPortTable": "serial{N}"})
        assert links[0]["local_port"] == "serial3"

    def test_empty_input(self):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        assert map_sensors_to_serial_links([]) == []


class TestMapSensorsMalformedRows:
    """A single malformed LibreNMS sensor row must not crash mapping and drop ALL serial rows."""

    def _valid(self, port=7, sid=7007):
        return {
            "sensor_id": sid,
            "device_id": 12,
            "sensor_type": "acsSerialPortTable",
            "sensor_index": f"acsSerialPortTableStatus.{port}",
            "sensor_descr": f"box-{port} Status",
            "sensor_current": 2,
            "group": "Serial Ports",
        }

    def test_non_dict_row_skipped(self):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(["not-a-dict", None, self._valid()])
        assert [r["sensor_id"] for r in links] == [7007]

    def test_missing_sensor_id_skipped(self):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        bad = self._valid()
        del bad["sensor_id"]
        links = map_sensors_to_serial_links([bad, self._valid(port=8, sid=7008)])
        assert [r["sensor_id"] for r in links] == [7008]

    def test_non_string_sensor_descr_does_not_crash(self):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        bad = self._valid(port=9, sid=7009)
        bad["sensor_descr"] = None  # malformed: not a string
        links = map_sensors_to_serial_links([bad])
        assert len(links) == 1
        # Falls back to an empty label (so it reads as the default-named, unconfigured port).
        assert links[0]["remote_device"] == ""
        # An empty label must NOT count as configured ("" != local_port is True, so the
        # is_configured flag must guard on bool(label)).
        assert links[0]["is_configured"] is False

    def test_non_string_sensor_index_does_not_crash(self):
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        bad = self._valid(port=10, sid=7010)
        bad["sensor_index"] = 5  # malformed: int, not "….N" string → unparseable, skipped
        links = map_sensors_to_serial_links([bad, self._valid(port=11, sid=7011)])
        assert [r["sensor_id"] for r in links] == [7011]


class TestConfigurableSensorTypes:
    """Recognized serial sensor_types AND their local port-name patterns are configurable.

    ``serial_sensor_types`` maps each LibreNMS ``sensor_type`` to the local ConsoleServerPort name
    pattern for that vendor, so Avocent lines become ``ttyS{N}`` and Cisco IOS async lines become
    ``Line {N}`` from one setting. Both ship recognized; a new vendor is one config entry. Cisco
    ``ciscoAsyncLine`` sensors (once shaped like Avocent: sensor_class=state, group "Serial Ports",
    ``"<peer> Status"`` descr) flow through the identical mapper — only the type and naming differ.
    """

    def _cisco_line(self, sid=13650, port=2, descr="test_location Status"):
        # Real device-52 (Catalyst 8300) shape: tsLineActive.<line> index, "<peer> Status" descr.
        return {
            "sensor_id": sid,
            "device_id": 52,
            "sensor_type": "ciscoAsyncLine",
            "sensor_index": f"tsLineActive.{port}",
            "sensor_descr": descr,
            "sensor_current": 0,
            "group": "Serial Ports",
        }

    def _acs_line(self, sid=1975, port=11, descr="host Status"):
        return {
            "sensor_id": sid,
            "device_id": 12,
            "sensor_type": "acsSerialPortTable",
            "sensor_index": f"acsSerialPortTableStatus.{port}",
            "sensor_descr": descr,
            "sensor_current": 2,
            "group": "Serial Ports",
        }

    def test_cisco_async_line_uses_its_configured_port_name_pattern(self):
        """A ciscoAsyncLine maps to a serial row named by the Cisco pattern (Line {N}), not ttyS."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links([self._cisco_line(port=2)])
        assert len(links) == 1
        row = links[0]
        assert row["local_port"] == "Line 2"
        assert row["remote_device"] == "test_location"  # "<peer> Status" -> peer label, same as Avocent
        assert row["is_configured"] is True
        assert row["sensor_index_int"] == 2
        assert row["_source"] == "serial"

    def test_each_type_gets_its_own_pattern(self):
        """Avocent and Cisco rows in one payload each use their vendor's configured name pattern."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links([self._acs_line(port=11), self._cisco_line(port=2)])
        by_id = {r["sensor_id"]: r["local_port"] for r in links}
        assert by_id[1975] == "ttyS11"  # Avocent
        assert by_id[13650] == "Line 2"  # Cisco

    def test_explicit_map_override_filters_and_names(self):
        """An explicit {type: pattern} map overrides both the recognized set and the naming."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(
            [self._acs_line(port=11), self._cisco_line(port=2)],
            sensor_types={"acsSerialPortTable": "console{N}"},
        )
        assert [(r["sensor_id"], r["local_port"]) for r in links] == [(1975, "console11")]

    def test_explicit_set_override_uses_fallback_pattern(self):
        """A bare set of types (no patterns) filters and names via the fallback port_name_pattern."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(
            [self._acs_line(port=11), self._cisco_line(port=2)],
            sensor_types=frozenset({"acsSerialPortTable"}),
        )
        assert [(r["sensor_id"], r["local_port"]) for r in links] == [(1975, "ttyS11")]

    def test_get_patterns_default_includes_avocent_and_cisco(self):
        from netbox_librenms_plugin.serial_utils import get_serial_sensor_type_patterns

        patterns = get_serial_sensor_type_patterns()
        assert patterns["acsSerialPortTable"] == "ttyS{N}"
        assert patterns["ciscoAsyncLine"] == "Line {N}"

    def test_get_patterns_respects_config_map(self, settings):
        """A configured {type: pattern} map narrows the recognized set and sets naming."""
        from netbox_librenms_plugin.serial_utils import get_serial_sensor_type_patterns

        settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"serial_sensor_types": {"acsSerialPortTable": "p{N}"}}}
        assert get_serial_sensor_type_patterns() == {"acsSerialPortTable": "p{N}"}

    def test_get_patterns_tolerates_bare_list_config(self, settings):
        """A bare list of types (no patterns) falls back to the default port-name pattern."""
        from netbox_librenms_plugin.serial_utils import get_serial_sensor_type_patterns

        settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"serial_sensor_types": ["ciscoAsyncLine"]}}
        assert get_serial_sensor_type_patterns() == {"ciscoAsyncLine": "ttyS{N}"}

    def test_get_patterns_tolerates_bare_string_config(self, settings):
        """A single type as a bare string is ONE type, not an iterable of characters.

        Without the guard, ``"ciscoAsyncLine"`` iterates letter-by-letter into a per-character
        map that matches no real sensor_type — serial sync silently returns zero rows.
        """
        from netbox_librenms_plugin.serial_utils import get_serial_sensor_type_patterns

        settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"serial_sensor_types": "ciscoAsyncLine"}}
        assert get_serial_sensor_type_patterns() == {"ciscoAsyncLine": "ttyS{N}"}


class TestMapSensorsWithFixture:
    """Integration-style tests using the captured device-12 (ACS6048) fixture."""

    # @staticmethod (not an instance method): a class-scoped fixture defined as a plain method
    # is deprecated (PytestRemovedIn10Warning) because pytest runs it on a different instance
    # than the tests. It uses neither self nor cls, so staticmethod is the correct form.
    @pytest.fixture(scope="class")
    @staticmethod
    def fixture_sensors():
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

    def test_port_11_resolves_configured_remote_device(self, fixture_sensors):
        """Index 11 = rdev-d9b298-RA1 — the clearest resolvable label in the fixture."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(fixture_sensors)
        row = links[10]  # 0-indexed, port 11
        assert row["sensor_index_int"] == 11
        assert row["local_port"] == "ttyS11"
        assert row["remote_device"] == "rdev-d9b298-RA1"
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
        """Index 44 = a console-alias label (pseudonymized), configured but won't resolve."""
        from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

        links = map_sensors_to_serial_links(fixture_sensors)
        row = links[43]
        assert row["sensor_index_int"] == 44
        assert row["remote_device"] == "rdev-9401f4-con1"
        assert row["is_configured"] is True

    def test_fixture_carries_no_raw_operator_labels(self, fixture_sensors):
        """Privacy: every bundled serial-port label must be a safe pseudonym (rdev-<hash>) or a default ttySNN.

        The fixture is committed to the repo, so operator-assigned hostnames (vendor/site/date
        identifiers) must be pseudonymized following the rdev-<hash> convention. This guards against
        the recurring leak of real infra labels into the corpus.
        """
        import re

        from netbox_librenms_plugin.serial_utils import strip_status_suffix

        safe = re.compile(r"^(rdev-[0-9a-f]{6}(-.*)?|ttyS\d+)$")
        leaked = sorted(
            label for s in fixture_sensors if not safe.match(label := strip_status_suffix(s.get("sensor_descr") or ""))
        )
        assert not leaked, f"raw operator-style labels leaked into the fixture: {leaked}"

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
