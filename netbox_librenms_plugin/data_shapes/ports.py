"""
Port-level primitives shared by the data-shape signature, compressor and anonymizer.

All three must agree on what a port is NAMED, whether it carries VLAN data, and whether it is a LAG
aggregate — a second copy of any of those rules drifts, and the compressor then drops a port whose
shape the signature reads (or the anonymizer hashes a name the signature matches). This module owns
them, and depends on nothing else in the package so every consumer can import it.
"""

import re
from itertools import islice

import re2


ANON_INTERFACE_NAME_PREFIX = "iface-"
ANON_INTERFACE_NAME_RE = re.compile(rf"^{re.escape(ANON_INTERFACE_NAME_PREFIX)}[0-9a-f]{{6}}(?:\.\d+)*$")


def port_has_vlan(port):
    """
    Return whether a port row carries real VLAN data (the signature's ``vlans`` axis predicate).

    Keyed on the VALUE, not mere key presence: ``ifVlan`` ``None``/``""``/``0``/``"0"`` are LibreNMS's
    no-/default-VLAN sentinels and do NOT count. The ifVlan value is string-valued JSON elsewhere in
    the client, so the string ``"0"`` is the same sentinel as the int ``0``. Only a real id or a
    non-empty ``vlans`` list counts. The compressor's port fingerprint imports this function, so its
    VLAN axis stays in lockstep with the signature. Otherwise, two same-shape ports (one with
    ``ifVlan: 0`` or ``None``) could collapse to a representative whose value flips the signature's
    vlans axis.

    Args:
        port (dict): A LibreNMS port row.

    Returns:
        bool: Whether the port carries a real VLAN id or a non-empty VLAN list.

    """
    return port.get("ifVlan") not in (None, "", 0, "0") or bool(port.get("vlans"))


def port_has_vrf(port):
    """
    Return whether a port row names a VRF.

    LibreNMS reports ``ifVrf`` 0 (and null) for a port with no VRF, so the value decides, not key
    presence. The capture uses this to decide whether to record the VRF route at all, and the
    compressor's port fingerprint uses it as its VRF axis, so the two cannot disagree about which
    ports are VRF-tagged.

    Args:
        port (dict): A LibreNMS port row.

    Returns:
        bool: Whether the port carries a VRF id.

    """
    return bool(port.get("ifVrf"))


# Recording patterns use RE2, including when replay calls the compiled objects directly.
# These limits bound each recording's compilation work and each program's backend memory.
_MAX_LAG_NAME_LEN = 256
_MAX_LAG_PATTERNS = 100
_MAX_LAG_PATTERN_LEN = 200
_MAX_RECORDING_PATTERN_MEMORY = 1 << 20


def _compile_recording_patterns(recording, key):
    """
    Compile one of a recording's per-OS pattern maps into usable regexes.

    The single place a recording's (untrusted, community-submitted) patterns are turned into
    regexes: the signature, the port compressor, the anonymizer and the replay all read them, and
    a second copy of the compile step could bypass the bounded engine.

    Args:
        recording (dict): A recording; a missing or non-dict map yields no patterns.
        key (str): The recording key holding the map.

    Returns:
        list: RE2 patterns; invalid, unsupported and oversized patterns are excluded.

    """
    compiled = []
    # A truthy non-dict map (e.g. a list) has no .values() and must degrade to "no patterns",
    # not crash --validate.
    patterns = recording.get(key)
    patterns = patterns if isinstance(patterns, dict) else {}
    options = re2.Options()
    options.max_mem = _MAX_RECORDING_PATTERN_MEMORY
    options.log_errors = False
    for pattern_str in islice(patterns.values(), _MAX_LAG_PATTERNS):
        if not isinstance(pattern_str, str) or len(pattern_str) > _MAX_LAG_PATTERN_LEN:
            continue
        try:
            compiled.append(re2.compile(pattern_str, options=options))
        except (re2.error, UnicodeEncodeError):
            continue
    return compiled


def compile_lag_patterns(recording):
    """
    Compile a recording's ``lag_patterns`` into usable regexes, skipping the unsafe ones.

    Args:
        recording (dict): A recording; a missing or non-dict ``lag_patterns`` yields no patterns.

    Returns:
        list: The compiled LAG name patterns.

    """
    return _compile_recording_patterns(recording, "lag_patterns")


def compile_sap_patterns(recording):
    """
    Compile a recording's ``sap_patterns`` into usable regexes, skipping the unsafe ones.

    A replay that drops these resolves a service access point as a LAG member, so the captured
    rule has to travel with the recording exactly as the LAG rule does.

    Args:
        recording (dict): A recording; a missing or non-dict ``sap_patterns`` yields no patterns.

    Returns:
        list: The compiled SAP name patterns.

    """
    return _compile_recording_patterns(recording, "sap_patterns")


def port_names(port):
    """
    Return the non-empty names a port is known by (``ifName`` + ``ifDescr``).

    Every name-based detector reads BOTH fields: on an ifDescr-mode device the structured name
    (a ``.N`` sub-unit, a LAG name) lives in ifDescr while ifName carries an arbitrary label, so an
    ifName-only scan misses the shape entirely.

    Args:
        port (dict): A LibreNMS port row.

    Returns:
        list[str]: Non-empty string values from ``ifName`` and ``ifDescr``.

    """
    return [n for n in (port.get("ifName"), port.get("ifDescr")) if isinstance(n, str) and n]


def name_matches_lag_pattern(name, compiled_lag_patterns):
    """Return whether *name* matches any compiled LAG pattern (length-bounded — see _MAX_LAG_NAME_LEN)."""
    if not isinstance(name, str) or len(name) > _MAX_LAG_NAME_LEN:
        return False
    return any(pat.search(name) for pat in compiled_lag_patterns)


def port_is_lag(port, compiled_lag_patterns):
    """
    Return whether *port* is a LAG aggregate, mirroring ``resolve_port_relationships._is_lag_aggregate``.

    An ``ieee8023adLag`` ifType OR a name matching a configured per-OS LAG pattern. Reading ifType
    alone would classify a pattern-based LAG (e.g. Cisco "Po1", carried as propVirtual) as not-a-LAG.

    Args:
        port (dict): A LibreNMS port row.
        compiled_lag_patterns (Iterable): Compiled per-OS LAG name patterns.

    Returns:
        bool: Whether the port's type or a known name identifies it as a LAG aggregate.

    """
    if port.get("ifType") == "ieee8023adLag":
        return True
    return any(name_matches_lag_pattern(name, compiled_lag_patterns) for name in port_names(port))
