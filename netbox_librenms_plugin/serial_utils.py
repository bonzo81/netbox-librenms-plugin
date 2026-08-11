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
    device_id         int|None - NetBox device id for the host these ports belong to
    remote_device     str   - Avocent port label (hint only, may be wrong)
    remote_port       None  - unresolved at map time; filled later by name-match enrichment
    remote_device_id  None  - always None (no LibreNMS device ref for labels)
    is_configured     bool  - True when label was customised from the default
    _source           str   - always "serial"
    sensor_id         int   - LibreNMS sensor_id for later reference
    sensor_index_int  int   - port number extracted from sensor_index

Sorting: rows are returned ordered by sensor_index_int (port number).
"""

import logging
import re

from netbox_librenms_plugin.utils import coerce_librenms_id

logger = logging.getLogger(__name__)

# Default local ConsoleServerPort name pattern (``{N}`` -> port number) for a serial sensor type
# with no explicit pattern configured (a caller passing ``sensor_types`` as a bare set).
DEFAULT_SERIAL_PORT_NAME_PATTERN = "ttyS{N}"

_INDEX_SUFFIX_RE = re.compile(r"\.(\d+)$")


def get_serial_sensor_type_patterns() -> dict:
    """
    Return the recognized ``{sensor_type: local port-name pattern}`` map for serial sensors.

    Reads the ``SerialSensorTypePattern`` table (migration-seeded with Avocent ACS and Cisco
    IOS async lines) so an operator can surface a new vendor's serial lines — or disable a
    shipped one by deleting its row — without a code change. An empty table therefore means
    "recognize nothing": there is deliberately no code-level fallback map that would resurrect
    a deleted vendor.

    Returns:
        dict: sensor_type -> local ConsoleServerPort name pattern.
    """
    from netbox_librenms_plugin.models import SerialSensorTypePattern

    patterns = {}
    for sensor_type, port_name_pattern in SerialSensorTypePattern.objects.values_list(
        "sensor_type",
        "port_name_pattern",
    ):
        normalized_type = (sensor_type or "").strip()
        normalized_pattern = (port_name_pattern or "").strip()
        if not normalized_type or "{N}" not in normalized_pattern:
            logger.warning(
                "Skipping invalid serial sensor pattern %r -> %r",
                sensor_type,
                port_name_pattern,
            )
            continue
        patterns[normalized_type] = normalized_pattern
    return patterns


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
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


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
            ``SerialSensorTypePattern`` table's map (:func:`get_serial_sensor_type_patterns`).

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
    # Materialize a non-mapping iterable ONCE: the per-row membership check below would
    # consume a generator on the first row and silently drop every later same-type row.
    if isinstance(sensor_types, dict):
        type_patterns = sensor_types
        recognized_types = sensor_types
    else:
        type_patterns = {}
        recognized_types = frozenset(sensor_types)

    links = []

    for sensor in sensors:
        # Harden against malformed LibreNMS rows so one bad record can't crash mapping and
        # drop ALL serial rows: skip non-dicts, rows missing sensor_id, unparseable indices,
        # and coerce a non-string sensor_descr before stripping its suffix.
        if not isinstance(sensor, dict):
            continue
        sensor_type = sensor.get("sensor_type")
        # An unhashable value (LibreNMS can return []) raises TypeError on the membership test and
        # would abort mapping for every serial row in the response.
        if not isinstance(sensor_type, str) or sensor_type not in recognized_types:
            continue

        sensor_id = coerce_librenms_id(sensor.get("sensor_id"))
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

    local_port_counts = {}
    for link in links:
        local_port = link["local_port"]
        local_port_counts[local_port] = local_port_counts.get(local_port, 0) + 1
    links = [link for link in links if local_port_counts[link["local_port"]] == 1]
    links.sort(key=lambda r: r["sensor_index_int"])
    return links
