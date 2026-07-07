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
    remote_port       None  - unresolved at map time; filled later by name-match enrichment
    remote_device_id  None  - always None (no LibreNMS device ref for labels)
    is_configured     bool  - True when label was customised from the default
    _source           str   - always "serial"
    sensor_id         int   - LibreNMS sensor_id for later reference
    sensor_index_int  int   - port number extracted from sensor_index

Sorting: rows are returned ordered by sensor_index_int (port number).
"""

import re

from netbox.plugins import get_plugin_config

# Default local ConsoleServerPort name pattern (``{N}`` -> port number) for a serial sensor type
# with no explicit pattern configured.
DEFAULT_SERIAL_PORT_NAME_PATTERN = "ttyS{N}"

# LibreNMS ``sensor_type`` -> local ConsoleServerPort name pattern, shipped as the default for the
# ``serial_sensor_types`` plugin setting. Avocent ACS (acsSerialPortTable) and Cisco IOS async
# lines (ciscoAsyncLine) both expose sensor_class=state / group "Serial Ports" / "<peer> Status"
# descriptions, so they share one mapper — only the sensor_type and the local naming differ.
# Operators add further vendor types (with their own naming) via the setting, not this module.
DEFAULT_SERIAL_SENSOR_TYPE_PATTERNS = {
    "acsSerialPortTable": "ttyS{N}",
    "ciscoAsyncLine": "Line {N}",
}

_INDEX_SUFFIX_RE = re.compile(r"\.(\d+)$")


def get_serial_sensor_type_patterns() -> dict:
    """
    Return the configured ``{sensor_type: local port-name pattern}`` map for serial sensors.

    Reads the ``serial_sensor_types`` plugin setting so an operator can surface a new vendor's
    serial lines and name them without a code change. Accepts a map (``type -> pattern``), a
    bare list of types, or a single type as a bare string (non-map forms default each type to
    :data:`DEFAULT_SERIAL_PORT_NAME_PATTERN`); falls back to
    :data:`DEFAULT_SERIAL_SENSOR_TYPE_PATTERNS` when the setting is unset or empty.

    Returns:
        dict: sensor_type -> local ConsoleServerPort name pattern.
    """
    configured = get_plugin_config("netbox_librenms_plugin", "serial_sensor_types")
    if not configured:
        return dict(DEFAULT_SERIAL_SENSOR_TYPE_PATTERNS)
    if isinstance(configured, dict):
        return dict(configured)
    if isinstance(configured, str):
        # A single type as a bare string (forgot the list wrapper) would otherwise be iterated
        # character-by-character below, yielding a per-letter map that matches no real
        # sensor_type — serial sync would silently return zero rows.
        return {configured: DEFAULT_SERIAL_PORT_NAME_PATTERN}
    # A bare list/iterable of sensor types (no per-type naming): use the fallback pattern.
    return {sensor_type: DEFAULT_SERIAL_PORT_NAME_PATTERN for sensor_type in configured}


def parse_port_number(sensor_index: str | None) -> int | None:
    """
    Extract the trailing integer from a sensor_index string.

    A non-string ``sensor_index`` (``None`` or e.g. an int from a malformed payload) is
    coerced to ``str`` so the regex search can't raise ``TypeError``.

    Examples::

        "acsSerialPortTableStatus.7"  -> 7
        "acsSerialPortTableStatus.49" -> 49
        "invalid"                     -> None

    Args:
        sensor_index (str | None): The LibreNMS sensor index to parse.

    Returns:
        int | None: The trailing integer, or None when there is no trailing number.
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

    Args:
        descr (str): The LibreNMS sensor description.

    Returns:
        str: The description with a trailing " Status" removed, else unchanged.
    """
    if descr.endswith(" Status"):
        return descr[:-7]
    return descr


def map_sensors_to_serial_links(
    sensors: list[dict],
    port_name_pattern: str = DEFAULT_SERIAL_PORT_NAME_PATTERN,
    device_id=None,
    sensor_types=None,
) -> list[dict]:
    """
    Convert a list of LibreNMS sensor records to serial cable-sync link rows.

    Only records whose ``sensor_type`` is recognized by ``sensor_types`` (default: the configured
    :func:`get_serial_sensor_type_patterns`) are processed; all others are silently skipped.  Each
    row's local port is named by that type's configured pattern (or ``port_name_pattern`` when
    ``sensor_types`` is a bare set carrying no patterns).  Rows with an unparseable ``sensor_index``
    are also skipped.

    Args:
        sensors: Raw sensor dicts as returned by
            ``LibreNMSAPI.get_serial_port_sensors()``.
        port_name_pattern: Fallback template for the local ConsoleServerPort name when a type has
            no configured pattern.  ``{N}`` is replaced with the port number.  Default ``"ttyS{N}"``.
        device_id: NetBox device id for the host these serial ports belong to.
            Included in each row so the row is self-sufficient for
            ``LibreNMSCableTable.Meta.row_attrs`` (which reads ``record["device_id"]``)
            even before ``enrich_links_data`` runs, avoiding a render-time KeyError.
        sensor_types: Recognized serial sensor types — either a ``{type: name pattern}`` map or a
            bare set/iterable of types (named via ``port_name_pattern``). Defaults to the
            plugin-configured map (:func:`get_serial_sensor_type_patterns`).

    Returns:
        List of link-row dicts sorted by port number (ascending).
    """
    # Fail closed on a non-list top-level payload (None or another non-iterable from a malformed
    # LibreNMS response) before iterating — otherwise the sync path crashes here instead of
    # degrading to "no serial links". Per-row hardening below covers malformed entries.
    if not isinstance(sensors, list):
        return []

    if sensor_types is None:
        sensor_types = get_serial_sensor_type_patterns()
    # A map carries per-type naming; a bare set/iterable carries none (fall back per row).
    type_patterns = sensor_types if isinstance(sensor_types, dict) else {}

    links = []

    for sensor in sensors:
        # Harden against malformed LibreNMS rows so one bad record can't crash mapping and
        # drop ALL serial rows: skip non-dicts, rows missing sensor_id, unparseable indices,
        # and coerce a non-string sensor_descr before stripping its suffix.
        if not isinstance(sensor, dict):
            continue
        sensor_type = sensor.get("sensor_type")
        if sensor_type not in sensor_types:
            continue

        sensor_id = sensor.get("sensor_id")
        if sensor_id is None:
            continue

        port_num = parse_port_number(sensor.get("sensor_index", ""))
        if port_num is None:
            continue

        pattern = type_patterns.get(sensor_type) or port_name_pattern
        local_port = pattern.replace("{N}", str(port_num))
        raw_descr = sensor.get("sensor_descr")
        label = strip_status_suffix(raw_descr if isinstance(raw_descr, str) else "")

        links.append(
            {
                "local_port": local_port,
                "local_port_id": f"serial:{sensor_id}",
                "device_id": device_id,
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
