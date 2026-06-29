"""
Field-aware, deterministic anonymization of LibreNMS data-shape recordings.

The goal is to make a captured recording safe to share publicly while keeping every field the
sync logic actually reads intact, so the anonymized recording still drives the same outcome
tests. Three strategies, by field:

* **Preserve verbatim** the logic-bearing fields (ifName/ifType/port ids, ENTITY-MIB
  class/index/position, VLANs, transceiver optics) and the public module-matching SKUs
  (entPhysicalModelName, transceiver model — keyed to NetBox ModuleType for module install).
  Anonymizing these would destroy the LAG/sub-interface/VC detection and module-matching the
  recordings exist to test.
* **Pseudonymize deterministically** identifiers (serials, hostnames, the device chassis SKU,
  firmware/software versions): the same input always maps to the same fake, so cross-references (a
  device serial that equals a stack member serial) still match after anonymization. The OS string
  is pseudonymized too, but *unsalted* (see :func:`pseudonymize_os`) so the same OS yields one
  stable token across all recordings — the novelty matcher needs that to compare/relate platforms.
  Vendor-naming SNMP OIDs (sysObjectID, sensor_oid, entPhysicalVendorType) are remapped under the
  example-enterprise arc so they keep their shape without naming the vendor the OS hash hides.
* **Scrub** PII to safe placeholders (IPs → RFC 5737/3849 documentation ranges, MACs → a
  synthetic ``02:00:00`` block, lat/lng → null, location → ``"Lab"``, free-text → "").

:func:`find_pii` is a regex safety-net: it sweeps the anonymized result for any IP/MAC/email the
field rules missed, so a human (or the mgmt command) can catch leaks before publishing.
"""

import hashlib
import re

# Logic-bearing fields the sync/detection/relationship code reads — never altered.
# NOTE: ifName/ifDescr are NOT here — they carry real infra (custom names, "** host **"
# annotations) so they get pattern-aware anonymization via INTERFACE_NAME_KEYS instead.
PRESERVE_KEYS = frozenset(
    {
        "ifType",
        "ifIndex",
        "ifSpeed",
        "ifMtu",
        "ifAdminStatus",
        "ifOperStatus",
        "ifVlan",
        "ifTrunk",
        "port_id",
        "local_port_id",
        "remote_port_id",
        "high_port_id",
        "low_port_id",
        "entPhysicalClass",
        "entPhysicalIndex",
        "entPhysicalContainedIn",
        "entPhysicalParentRelPos",
        # The module-matching key: ModuleTypeMapping resolves entPhysicalModelName (and the
        # transceiver `model`, merged into it) to a NetBox ModuleType to drive module install.
        # It's a public vendor catalog SKU (not PII), so it's preserved verbatim — pseudonymizing
        # it would foreclose recording-driven module-install outcome tests. (Device `hardware`,
        # the chassis SKU, is NOT a match key and stays pseudonymized via MODEL_KEYS.)
        "entPhysicalModelName",
        # entPhysicalName/entPhysicalDescr are logic-bearing and preserved verbatim like the model
        # SKU above: module-type matching reads entPhysicalName first (device_operations), VC member
        # name/description come from both (virtual_chassis), and slot/MDA + transceiver matching
        # fullmatch entPhysicalName / read entPhysicalDescr (modules_view). Scrubbing them would
        # destroy the module-install / VC-detection outcomes the recordings exist to test; residual
        # free-text PII in them is the find_pii safety net's responsibility, not a field scrub.
        "entPhysicalName",
        "entPhysicalDescr",
        # NOTE: `os` is NOT preserved — it's pseudonymized to a stable os-<hash> (see
        # pseudonymize_os) so a recording never advertises the exact platform.
        "device_id",
        "status",
        # serial-port sensor logic-bearing fields (Avocent console servers). parse_port_number
        # reads the trailing int from sensor_index; sensor_type gates the AVOCENT filter; sensor_id
        # forms the synthetic local_port_id. None carry PII (sensor_descr does — see below).
        "sensor_id",
        "sensor_index",
        "sensor_type",
        "vlan",
        "vlan_id",
        "vlan_vlan",
        "untagged",
        "tagged",
        # transceiver optics shape
        "type",
        "channels",
        "connector",
        "wavelength",
        # transceiver SKU — merged into entPhysicalModelName for ModuleType matching (see above),
        # a public part number, preserved so module-install outcomes can be tested from recordings.
        "model",
    }
)
# Interface-name fields: pattern-aware (see _anon_interface_name). The logic-bearing port-name
# token is preserved; custom names and free-text annotations are dropped/pseudonymized.
INTERFACE_NAME_KEYS = frozenset({"ifName", "ifDescr"})
# Serial-port sensor label (Avocent). A default/uncustomised label is a generic port name
# (no PII); a customised one is the attached device's hostname. Pattern-aware (see
# _anon_serial_label) so the is_configured outcome (label vs port name) is preserved.
SERIAL_LABEL_KEYS = frozenset({"sensor_descr"})
# BGP ASN — identifying, not read by the sync logic. Mapped to a deterministic private ASN.
BGP_KEYS = frozenset({"bgpLocalAs", "bgpLocalas", "bgp_local_as"})
SERIAL_KEYS = frozenset({"serial", "entPhysicalSerialNum"})
# `display` is the LibreNMS device display name — operators often set it to a real FQDN.
HOSTNAME_KEYS = frozenset({"hostname", "sysName", "remote_hostname", "display"})
# Device chassis SKU (e.g. "WS-C3560X-24T-S"). Pseudonymized: it's not a module-matching key, so
# blanking it loses no testable outcome. (entPhysicalModelName / transceiver `model` ARE matching
# keys and are preserved via PRESERVE_KEYS instead.)
MODEL_KEYS = frozenset({"hardware"})
# entPhysicalMfgName is the ENTITY-MIB manufacturer name (e.g. "Cisco Systems Inc."). It names the
# vendor the os-hash deliberately masks and is read by no sync logic, so it's pseudonymized to a
# deterministic MFG-<hash> — the field shape (present, non-empty) survives without the vendor.
MFG_KEYS = frozenset({"entPhysicalMfgName"})
# Firmware / software version strings. Identifying (pin an exact build → deployment fingerprint /
# CVE surface) and read by no sync logic, so pseudonymized to a deterministic fw-<hash>. (Device
# chassis HARDWARE revision is left alone — it's not a firmware version.)
VERSION_KEYS = frozenset({"version", "features", "entPhysicalFirmwareRev", "entPhysicalSoftwareRev"})
IP_KEYS = frozenset({"ip", "ipv4", "ipv6", "inet", "ip_address", "overwrite_ip"})
MAC_KEYS = frozenset({"ifPhysAddress", "mac", "mac_address"})
GEO_KEYS = frozenset({"lat", "lng", "latitude", "longitude"})
LOCATION_KEYS = frozenset({"location", "sysLocation"})
# Operator-configurable free text → scrubbed to "". entPhysicalAssetID (RFC 2737 asset-tracking id)
# and entPhysicalAlias (manager-assigned alias) can carry internal asset tags / rack codes; neither
# is read by the sync logic.
FREETEXT_KEYS = frozenset(
    {
        "ifAlias",
        "sysContact",
        "sysDescr",
        "purpose",
        "notes",
        "entPhysicalAssetID",
        "entPhysicalAlias",
        # ENTITY-MIB manufacture date (e.g. "2021-03-15,12:00:00.0") — an identifying build date read
        # by no sync logic, scrubbed to empty.
        "entPhysicalMfgDate",
    }
)
# OID-valued fields whose enterprise arc (1.3.6.1.4.1.<N>) names the vendor — e.g. sysObjectID
# 1.3.6.1.4.1.6527… (Nokia), sensor_oid …10418… (Avocent). They re-reveal the platform that
# pseudonymize_os hides, and no sync logic reads them, so map each to a well-formed OID under the
# IANA example-enterprise number (32473, RFC 5612) so the shape is kept but the vendor is not.
OID_KEYS = frozenset({"sysObjectID", "sensor_oid", "entPhysicalVendorType"})
# SNMP credentials/config from the device row — secrets, scrubbed to empty. A LibreNMS
# /api/v0/devices/{id} response is a full DB row that carries these in plaintext; none are read
# by the sync logic, so blanking them all is safe and conservative.
SNMP_CREDENTIAL_KEYS = frozenset(
    {
        "community",
        "authname",
        "authpass",
        "authalgo",
        "cryptopass",
        "cryptoalgo",
        "authlevel",
        "snmpver",
        "snmp_community",
    }
)
# Safety-net (find_pii) key denylist: any non-empty string under a key whose name suggests a
# secret is flagged for review, even if it's a field the rules above don't explicitly cover.
_SECRET_KEY_HINTS = ("pass", "community", "secret", "token", "cryptopass", "privkey", "authkey", "apikey")

# Documentation/synthetic ranges this module emits — find_pii() allows them.
_DOC_IP_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.", "2001:db8")
_SYNTH_MAC_PREFIX = "02:00:00"

# Octet-validated (0-255) and bounded so dotted-decimal SNMP OIDs (1.3.6.1.4.1.9.1…) and
# letter-suffixed version strings (24.4.1.41I-ULH) don't read as IPs. A genuine IP — preceded
# and followed by a non-word, non-dot boundary — still matches.
_OCTET = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"
_IPV4_RE = re.compile(rf"(?<![\w.]){_OCTET}(?:\.{_OCTET}){{3}}(?![\w.])")
# Match full AND compressed (::) IPv6 — the old "3+ groups, all present" form missed compressed
# literals like 2001:4860::1, which then slipped past the residual-PII safety net. This is the
# standard comprehensive grammar: it requires either 8 groups or a "::" compression marker, so a
# plain colon-separated decimal sequence (e.g. a "12:34:56" timestamp) is NOT matched.
_H16 = r"[0-9A-Fa-f]{1,4}"
_IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:])(?:"
    rf"(?:{_H16}:){{7}}{_H16}"  # 1:2:3:4:5:6:7:8
    rf"|(?:{_H16}:){{1,7}}:"  # 1::            1:2:3:4:5:6:7::
    rf"|(?:{_H16}:){{1,6}}:{_H16}"  # 1::8          1:2:3:4:5:6::8
    rf"|(?:{_H16}:){{1,5}}(?::{_H16}){{1,2}}"  # 1::7:8        1:2:3:4:5::7:8
    rf"|(?:{_H16}:){{1,4}}(?::{_H16}){{1,3}}"
    rf"|(?:{_H16}:){{1,3}}(?::{_H16}){{1,4}}"
    rf"|(?:{_H16}:){{1,2}}(?::{_H16}){{1,5}}"
    rf"|{_H16}:(?::{_H16}){{1,6}}"  # 1::3:4:5:6:7:8
    rf"|:(?:(?::{_H16}){{1,7}}|:)"  # ::2:3:4:5:6:7:8  ::
    r")(?![0-9A-Fa-f:])"
)
_MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# Free-text FQDN safety-net (dotted labels + an alphabetic TLD). Anonymized hostnames are
# single-label ("device-xxxx") and versions/OIDs lack an alpha TLD, so they don't match.
_FQDN_RE = re.compile(r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}\b")
# find_pii: keys whose values are identifiers (firmware/model/descr) that can look like a
# dotted IP (e.g. version "3.9.0.4") but are NOT addresses — exempt them from IP/FQDN flagging.
_IP_EXEMPT_KEYS = frozenset(
    {
        "version",
        "hardware",
        "features",
        "sysDescr",
        "model",
        "entPhysicalModelName",
        # entPhysicalDescr is NOT exempt: it's broad free-text preserved verbatim (logic-bearing),
        # so the residual-PII scan is its only safety net — exempting it from the IP/FQDN checks
        # would let a chassis/module description carrying a hostname or address pass unnoticed. The
        # version/model-like fields below stay exempt to suppress dotted-version false positives.
        "entPhysicalFirmwareRev",
        "entPhysicalHardwareRev",
        "entPhysicalSoftwareRev",
        "entPhysicalVendorType",
        "entPhysicalMfgName",
        "sysObjectID",
        "icon",  # static asset path, e.g. "images/os/nokia.svg" (looks like an FQDN, isn't)
    }
)

# Known interface-name prefixes (junos / cisco ios-xe-xr / nokia / cumulus-arcos / linux). A name
# is a "real port" iff it's slot notation (1/1/c31/3, A/1) or one of these prefixes immediately
# followed by an optional '-' and a digit. Anything else (custom names like "AORTA-SSP-CUSTOMER-1",
# "to_core-rtr", "IXIA") is pseudonymized. Alternation backtracks, so prefix order doesn't matter.
_IF_PREFIXES = (
    "GigabitEthernet|TenGigabitEthernet|TenGigE|FortyGigabitEthernet|FortyGigE|HundredGigE|"
    "TwentyFiveGigE|FastEthernet|Ethernet|Port-channel|Bundle-Ether|MgmtEth|Management|Loopback|"
    "Tunnel|Serial|Vlan|"
    "ge|xe|et|fe|ae|irb|lo|em|fxp|gr|ip|lt|mt|pp|st|vcp|vme|fab|gre|ipip|pime|pimd|cbp|pip|esi|"
    "fti|sp|dsc|lsi|mtun|vt|esa|lag|"
    "swp|bond|eth|ens|eno|enp|veth|mgmt|vni|br|"
    "Gi|Te|Fa|Hu|Fo|Twe|Eth|Po|BE|Lo|Tu|Se|nve|BVI|Null"
)
# Junos pseudo-interfaces whose base name carries no slot/digit (irb, jsrv, dsc, ...). Without a
# dedicated rule the general "prefix + digit" shape below never matches them, so a base/sub-unit
# pair like jsrv <-> jsrv.1 anonymizes to two unrelated iface-<hash> values and the resolver's
# name-based sub-unit pairing (low_name startswith high_name + '.') is lost. Match the bare base
# (optionally + '.unit') as a COMPLETE token, word-bounded, so the relationship survives while free
# text like "jsrv-customer" is still pseudonymized rather than preserved verbatim (which would leak
# it — unlike CR's "make the trailing digit optional", which would also preserve any value starting
# with a short prefix such as "ip"/"lo").
_DIGITLESS_IF_NAMES = "jsrv|irb|dsc|lsi|mtun|pimd|pime|tap"
# systemd "predictable" Linux names (eno1np0, ens3f1, enp2s0f1np0): the n<phys_port> / f<function>
# segments are letters+digits that the general "stop at the first letter" run below would truncate,
# collapsing siblings like …np0 / …np1 onto one preserved token and breaking name-based correlation
# in captured recordings. Match the full systemd grammar as a leading token so the distinguishing
# suffix survives. enx<MAC> is intentionally NOT matched here — it's MAC-derived, so it falls through
# to the iface-<hash> pseudonym rather than leaking a MAC.
_LINUX_PREDICTABLE_IF_RE = r"(?:eno\d+|ens\d+(?:f\d+)?|enp\d+s\d+(?:f\d+)?)(?:np\d+)?(?:d\d+)?"
# A logic-bearing port-name token at the START of the string. Three shapes: a digit-less Junos
# special (above), slot notation (optional leading letter, then digit groups separated by '/'), or a
# known prefix + '-?' + digit. The trailing run is restricted to slot-path characters — digits and
# the separators '/', '.', ':', '-' — so it captures the rest of the slot/sub-unit but STOPS at the
# first letter or '_' that begins a free-text annotation (e.g. "eth0_customerA" -> "eth0"). Any
# letters that are legitimately part of a port name live in the prefix or in a '/'-delimited slot
# component (matched above), never in the bare trailing run.
_PORT_TOKEN_RE = re.compile(
    rf"^(?:(?:{_DIGITLESS_IF_NAMES})(?:\.\d+)?(?![\w/.:-])"
    rf"|(?:{_LINUX_PREDICTABLE_IF_RE})[\d/.:-]*"
    rf"|(?:[A-Za-z]?\d+(?:/[A-Za-z]*\d+)+|[A-Za-z]/\d+|(?:{_IF_PREFIXES})-?\d)[\d/.:-]*)"
)


def _hash(value, salt, length=6):
    """Return a stable short hex digest of *value* (salted), for deterministic pseudonyms."""
    return hashlib.sha256(f"{salt}::{value}".encode()).hexdigest()[:length]


# A pseudonymized OS token, e.g. "os-1a2b3c". Used to recognize an already-anonymized value.
_OS_TOKEN_RE = re.compile(r"^os-[0-9a-f]{6}$")


def pseudonymize_os(os_name):
    """
    Map a LibreNMS OS string to a stable, salt-independent ``os-<hash>`` token (idempotent).

    Every OS is hashed — even common ones — so a recording never advertises the exact platform a
    contributor runs. The hash is deliberately *unsalted* so the same OS always yields the same
    token across recordings and contributors, which lets the novelty matcher still compare and
    relate them (see :mod:`~netbox_librenms_plugin.data_shapes.signature`). Case-insensitive, and a
    value that is already an ``os-<hash>`` token is returned unchanged so re-anonymizing is a no-op.

    Args:
        os_name: The raw LibreNMS OS string (or an already-pseudonymized token).

    Returns:
        The ``os-<hash>`` token, or the input unchanged when it is empty/non-string/already hashed.
    """
    if not isinstance(os_name, str) or not os_name:
        return os_name
    if _OS_TOKEN_RE.match(os_name):
        return os_name
    return f"os-{_hash(os_name.lower(), '')}"


def _doc_ip(value, salt):
    """Map an IP (optionally with a /prefix) to a deterministic documentation-range address."""
    addr, sep, prefix = value.partition("/")
    if ":" in addr:
        anon = f"2001:db8::{_hash(addr, salt, 4)}"
    else:
        octet = int(_hash(addr, salt, 4), 16) % 254 + 1
        anon = f"192.0.2.{octet}"
    return f"{anon}{sep}{prefix}" if sep else anon


def _synthetic_mac(value, salt):
    """Map a MAC to a deterministic address in the locally-administered 02:00:00 block."""
    digest = _hash(value, salt, 6)
    return f"{_SYNTH_MAC_PREFIX}:{digest[0:2]}:{digest[2:4]}:{digest[4:6]}"


def _anon_interface_name(value, salt):
    """
    Anonymize an ifName/ifDescr while preserving the logic-bearing port-name token.

    The relationship resolver keys on ``ifName`` (sub-interface ``parent.N`` split, LAG name
    patterns); ``ifDescr`` is only a whole-string name fallback. So we keep the leading port-name
    token verbatim and drop everything after it (the free-text annotation that leaks hostnames,
    customer/engineer names). Names with no recognizable port token become a deterministic
    ``iface-<hash>`` pseudonym.

    Args:
        value (str): The raw ifName/ifDescr value.
        salt (str): Salt mixed into the pseudonym hash.

    Returns:
        str: The preserved token, or a stable pseudonym.
    """
    match = _PORT_TOKEN_RE.match(value)
    if match:
        token = match.group(0)
        # Whole value is a port name → keep it; otherwise it had a free-text tail → keep only
        # the token (e.g. "lag2, IP interface, ** core-rtr ae42 **" → "lag2").
        return token
    return f"iface-{_hash(value, salt)}"


# A default Avocent serial-port label is a generic, device-agnostic port name: a known serial/console
# prefix + a port number, optionally with LibreNMS's trailing " Status" (e.g. "ttyS49 Status",
# "Port 7"). Anything else — including a SHORT hostname like "core1" that's structurally just
# letters+digits — is treated as a customised label and pseudonymized, since a bare letters+digits
# rule would preserve (leak) such hostnames. The prefix allowlist is what keeps the two apart.
_SERIAL_DEFAULT_LABEL_RE = re.compile(
    r"^(?:ttyUSB|ttyS|ttyD|tty|port|serial|console|com)[\s-]?\d+(?: Status)?$",
    re.IGNORECASE,
)


def _anon_serial_label(value, salt):
    """
    Anonymize a serial-port sensor description while preserving the is_configured outcome.

    ``map_sensors_to_serial_links`` derives ``is_configured`` from whether the label (after its
    " Status" suffix is stripped) differs from the default port name. A generic/default label
    carries no PII and is kept verbatim so that outcome is preserved; a customised label is the
    remote device's hostname, so it becomes a deterministic ``device-<hash>`` pseudonym (still a
    non-empty value distinct from the port name, so is_configured stays True).

    Args:
        value (str): The raw sensor_descr value.
        salt (str): Salt mixed into the pseudonym hash.

    Returns:
        str: The preserved default label, or a stable hostname pseudonym.
    """
    if _SERIAL_DEFAULT_LABEL_RE.match(value):
        return value
    return f"device-{_hash(value, salt)}"


def _anon_oid(value, salt):
    """Map an SNMP OID to a deterministic OID under the example-enterprise arc (hides the vendor)."""
    prefix = "." if value.startswith(".") else ""
    return f"{prefix}1.3.6.1.4.1.32473.{int(_hash(value, salt, 6), 16)}"


def _anon_asn(value, salt):
    """Map a BGP ASN to a deterministic 16-bit private ASN (64512-65534), preserving int type."""
    if value in (None, "", 0):
        return value
    return 64512 + int(_hash(str(value), salt, 4), 16) % 1023


def _anon_value(key, value, salt):
    """Apply the field rule for a single scalar (non-container) value keyed by *key*."""
    if key in PRESERVE_KEYS:
        return value
    if key in GEO_KEYS:
        return None
    if key in BGP_KEYS:
        # Handled before the str-guard below since ASNs arrive as ints.
        return _anon_asn(value, salt)
    if not isinstance(value, str) or not value or value == "-":
        # Pseudonym/scrub rules below operate on real string values; leave empties/sentinels
        # and non-strings (ints, bools, null) untouched so logic-bearing numerics survive.
        return value
    if key in INTERFACE_NAME_KEYS:
        return _anon_interface_name(value, salt)
    if key in SERIAL_LABEL_KEYS:
        return _anon_serial_label(value, salt)
    if key in SERIAL_KEYS:
        return f"SN-{_hash(value, salt)}"
    if key in HOSTNAME_KEYS:
        return f"device-{_hash(value, salt)}"
    if key in MODEL_KEYS:
        return f"MODEL-{_hash(value, salt)}"
    if key in MFG_KEYS:
        return f"MFG-{_hash(value, salt)}"
    if key in VERSION_KEYS:
        return f"fw-{_hash(value, salt)}"
    if key == "os":
        return pseudonymize_os(value)
    if key == "icon":
        # The icon path spells out the OS/vendor (e.g. "images/os/nokia.svg"), which would re-reveal
        # exactly what pseudonymize_os hides — genericize it to stay consistent with the hashed os.
        return "images/os/generic.svg"
    if key in OID_KEYS:
        return _anon_oid(value, salt)
    if key in IP_KEYS:
        return _doc_ip(value, salt)
    if key in MAC_KEYS:
        return _synthetic_mac(value, salt)
    if key in LOCATION_KEYS:
        return "Lab"
    if key in FREETEXT_KEYS or key in SNMP_CREDENTIAL_KEYS:
        return ""
    return value


def _walk(obj, salt):
    """Recursively anonymize a parsed JSON body (dict/list/scalar)."""
    if isinstance(obj, dict):
        return {k: (_walk(v, salt) if isinstance(v, (dict, list)) else _anon_value(k, v, salt)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(item, salt) for item in obj]
    return obj


def anonymize_recording(recording, *, salt=""):
    """
    Return a copy of *recording* with every response body anonymized field-by-field.

    The top-level ``name``/``description`` are auto-populated by the capture view from the NetBox
    device name (e.g. ``"core-rtr01-shape"``), so they are regenerated to a neutral,
    hostname-free token here — this is the single boundary every caller (UI submission, mgmt
    command) passes through. ``meta`` is kept (os/topology, not PII). Pass a stable *salt* to make
    pseudonyms reproducible across runs (and unique per contributor if desired).

    Args:
        recording (dict): A recording as produced by :func:`.capture.capture_device_recording`.
        salt (str): Optional salt mixed into the deterministic pseudonym hashes.

    Returns:
        dict: A new recording with anonymized ``responses`` and a neutral name/description.
    """
    out = dict(recording)
    out["responses"] = {key: _walk(body, salt) for key, body in recording.get("responses", {}).items()}
    # Pseudonymize meta.os too (it's outside `responses`, so _walk doesn't reach it) and key the
    # neutral name off the pseudonymized token, so neither the metadata nor the name leaks the OS.
    meta = dict(recording.get("meta") or {})
    if meta.get("os"):
        meta["os"] = pseudonymize_os(meta["os"])
    out["meta"] = meta
    out["name"] = f"{meta.get('os') or 'device'}-shape-{_hash(recording.get('name', ''), salt)}"
    out["description"] = "Anonymized LibreNMS data-shape capture."
    return out


def find_pii(recording):
    """
    Sweep a recording's responses for residual secrets the field rules missed.

    Catches IP/MAC/email string patterns, plus any non-empty value under a key whose name
    suggests a credential (defense-in-depth for unexpected secret fields the rules don't
    explicitly scrub). The documentation IP ranges and the synthetic ``02:00:00`` MAC block this
    module emits are treated as safe and not reported.

    Args:
        recording (dict): The recording to scan (typically the anonymized output).

    Returns:
        list[dict]: One ``{"path", "kind", "value"}`` entry per residual match. Credential-key
            findings report ``value`` as ``"<redacted>"`` so the secret itself isn't echoed.
    """
    findings = []

    def scan(obj, path, key=None):
        if isinstance(obj, dict):
            for k, v in obj.items():
                is_secret_key = any(hint in k.lower() for hint in _SECRET_KEY_HINTS)
                if is_secret_key and v:
                    findings.append({"path": f"{path}.{k}", "kind": "credential", "value": "<redacted>"})
                    # Don't recurse into a flagged secret's value: re-scanning it would re-report the
                    # raw contents as ipv4/mac/email/fqdn, echoing the very secret we just redacted.
                    # This also covers a secret stored as a dict/list (not just a bare string), whose
                    # children would otherwise be walked and surfaced individually.
                    continue
                scan(v, f"{path}.{k}", k)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                scan(v, f"{path}[{i}]", key)
        elif isinstance(obj, str):
            # version/model/descr values can look like a dotted IP (e.g. "3.9.0.4") or end in a
            # word — they're identifiers, not addresses/hostnames, so skip address/FQDN flagging.
            ip_exempt = key in _IP_EXEMPT_KEYS
            for kind, regex in (
                ("ipv4", _IPV4_RE),
                ("ipv6", _IPV6_RE),
                ("mac", _MAC_RE),
                ("email", _EMAIL_RE),
                ("fqdn", _FQDN_RE),
            ):
                if ip_exempt and kind in ("ipv4", "ipv6", "fqdn"):
                    continue
                for match in regex.findall(obj):
                    # IPv6 hex is case-insensitive, so lower-case before the documentation-range
                    # check (e.g. 2001:DB8::1 must still be exempted like 2001:db8::1).
                    if kind in ("ipv4", "ipv6") and match.lower().startswith(_DOC_IP_PREFIXES):
                        continue
                    # A 6-octet MAC also satisfies the loose IPv6 pattern; let the dedicated mac
                    # kind handle it so a synthetic MAC isn't double-reported as a bogus IPv6.
                    if kind == "ipv6" and _MAC_RE.fullmatch(match):
                        continue
                    if kind == "mac" and match.lower().startswith(_SYNTH_MAC_PREFIX):
                        continue
                    findings.append({"path": path, "kind": kind, "value": match})

    # Scan the whole recording, not just `responses`: residual PII can also land in top-level
    # fields (name/description/meta), which feed the same validation + fixture safety net.
    scan(recording, "recording")
    return findings
