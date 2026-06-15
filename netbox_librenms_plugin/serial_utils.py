"""
Utilities for mapping LibreNMS serial-port sensor data to cable-sync link rows.

Serial ports on console servers (e.g. Avocent ACS) are not SNMP ifTable
entries and do not appear in LibreNMS port listings.  They surface instead as
``sensor_class=state`` sensors in the "Serial Ports" group.  This module
converts those sensor records into the same link-row shape that
``BaseCableTableView.get_links_data()`` produces, so the existing Cables tab
enrichment and sync pipeline can reuse them with minimal branching.

Row shape emitted (all keys match the cables-view link dict):
    local_port        str   - ConsoleServerPort name, e.g. "ttyS7"
    local_port_id     str   - synthetic stable key "serial:<sensor_id>"
    remote_device     str   - Avocent port label (hint only, may be wrong)
    remote_port       None  - always None at this stage (manual / Phase 3)
    remote_device_id  None  - always None (no LibreNMS device ref for labels)
    is_configured     bool  - True when label was customised from the default
    _source           str   - always "serial"
    sensor_id         int   - LibreNMS sensor_id for later reference
    sensor_index_int  int   - port number extracted from sensor_index

Sorting: rows are returned ordered by sensor_index_int (port number).
"""

import re

# Sensor types known to represent ACS-family serial ports.
# Phase 4 will replace this with a DB-backed per-OS config model.
AVOCENT_SENSOR_TYPES = frozenset({"acsSerialPortTable"})

_INDEX_SUFFIX_RE = re.compile(r"\.(\d+)$")


def parse_port_number(sensor_index: str | None) -> int | None:
    """
    Extract the trailing integer from a sensor_index string.

    Examples::

        "acsSerialPortTableStatus.7"  -> 7
        "acsSerialPortTableStatus.49" -> 49
        "invalid"                     -> None

    A non-string ``sensor_index`` (``None`` or e.g. an int from a malformed payload)
    is coerced to ``str`` so the regex search can't raise ``TypeError``.
    """
    m = _INDEX_SUFFIX_RE.search(str(sensor_index or ""))
    return int(m.group(1)) if m else None


def strip_status_suffix(descr: str) -> str:
    """
    Remove the trailing " Status" appended by LibreNMS to sensor descriptions.

    Example::

        "PROD-LAB03A-RA1 Status" -> "PROD-LAB03A-RA1"
        "ttyS49 Status"          -> "ttyS49"
        "bare label"             -> "bare label"  (no suffix, returned as-is)
    """
    if descr.endswith(" Status"):
        return descr[:-7]
    return descr


def map_sensors_to_serial_links(
    sensors: list[dict],
    port_name_pattern: str = "ttyS{N}",
) -> list[dict]:
    """
    Convert a list of LibreNMS sensor records to serial cable-sync link rows.

    Only records whose ``sensor_type`` is in :data:`AVOCENT_SENSOR_TYPES` are
    processed; all others are silently skipped.  Rows with an unparseable
    ``sensor_index`` are also skipped.

    Args:
        sensors: Raw sensor dicts as returned by
            ``LibreNMSAPI.get_serial_port_sensors()``.
        port_name_pattern: Template for the local ConsoleServerPort name.
            ``{N}`` is replaced with the port number.  Default ``"ttyS{N}"``.

    Returns:
        List of link-row dicts sorted by port number (ascending).
    """
    links = []

    for sensor in sensors:
        # Harden against malformed LibreNMS rows so one bad record can't crash mapping and
        # drop ALL serial rows: skip non-dicts, rows missing sensor_id, unparseable indices,
        # and coerce a non-string sensor_descr before stripping its suffix.
        if not isinstance(sensor, dict):
            continue
        if sensor.get("sensor_type") not in AVOCENT_SENSOR_TYPES:
            continue

        sensor_id = sensor.get("sensor_id")
        if sensor_id is None:
            continue

        port_num = parse_port_number(sensor.get("sensor_index", ""))
        if port_num is None:
            continue

        local_port = port_name_pattern.replace("{N}", str(port_num))
        raw_descr = sensor.get("sensor_descr")
        label = strip_status_suffix(raw_descr if isinstance(raw_descr, str) else "")

        links.append(
            {
                "local_port": local_port,
                "local_port_id": f"serial:{sensor_id}",
                "remote_device": label,
                "remote_port": None,
                "remote_device_id": None,
                # An empty label (missing/non-string sensor_descr, normalized to "") is NOT a
                # customised name — guard with bool(label) so malformed rows aren't marked
                # configured just because "" != the default port name.
                "is_configured": bool(label) and label != local_port,
                "_source": "serial",
                "sensor_id": sensor_id,
                "sensor_index_int": port_num,
            }
        )

    links.sort(key=lambda r: r["sensor_index_int"])
    return links
