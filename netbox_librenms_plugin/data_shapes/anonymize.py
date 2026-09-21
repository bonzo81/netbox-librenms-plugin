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
* **Pseudonymize deterministically** identifiers (serials, hostnames, ENTITY-MIB display text, the
  device chassis SKU, firmware/software versions): the same input always maps to the same fake, so
  cross-references still match after anonymization. The OS string is pseudonymized too, but
  *unsalted* (see :func:`pseudonymize_os`) so the same OS yields one stable token across all
  recordings. The novelty matcher needs that to compare platforms. Vendor-naming SNMP OIDs
  (sysObjectID, sensor_oid, entPhysicalVendorType) are remapped under the example-enterprise arc so
  they keep their shape without naming the vendor the OS hash hides.
* **Scrub** PII to safe placeholders (IPs → RFC 5737/3849 documentation ranges, MACs → a
  synthetic ``02:00:00`` block, lat/lng → null, location → ``"Lab"``, free-text → "").

:func:`find_pii` is a regex safety-net: it sweeps the anonymized result for any IP/MAC/email the
field rules missed, so a human (or the mgmt command) can catch leaks before publishing.
"""

import hashlib
import ipaddress
import re
from typing import NamedTuple

from netbox_librenms_plugin.data_shapes.ports import (
    ANON_INTERFACE_NAME_RE,
    ANON_INTERFACE_NAME_PREFIX,
    compile_lag_patterns,
    name_matches_lag_pattern,
)

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
        # The per-port VRF id and the VRF row it joins to. Ints, no PII; the join is what makes a
        # VRF fixture usable, while the VRF's NAME is customer data and is pseudonymized below.
        "ifVrf",
        "vrf_id",
        # Address prefix lengths: parse_librenms_ip_entry reads them alongside the address, so a
        # scrubbed one makes the row unparseable.
        "prefix_length",
        "ipv4_prefixlen",
        "ipv6_prefixlen",
        "port_id",
        "local_port_id",
        "remote_port_id",
        # Link-row device ids and the discovery protocol. The protocol distinguishes the same
        # neighbour reported over both CDP and LLDP, which is a shape the cables tab must handle.
        "local_device_id",
        "remote_device_id",
        "protocol",
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
# ``remote_port`` is the same kind of value on the far end of a link, matched against NetBox
# interface names by the cables tab, so it gets the same pattern-aware treatment.
INTERFACE_NAME_KEYS = frozenset({"ifName", "ifDescr", "remote_port"})
# Serial-port sensor label (Avocent). A default/uncustomised label is a generic port name
# (no PII); a customised one is the attached device's hostname. Pattern-aware (see
# _anon_serial_label) so the is_configured outcome (label vs port name) is preserved.
SERIAL_LABEL_KEYS = frozenset({"sensor_descr"})
# BGP ASN — identifying, not read by the sync logic. Mapped to a deterministic private ASN.
BGP_KEYS = frozenset({"bgpLocalAs", "bgpLocalas", "bgp_local_as"})
# The VRF name is the NetBox match key, so the instinct is to preserve it the way
# entPhysicalModelName is. It is the wrong instinct: a model name is a public vendor catalogue
# SKU, a VRF name is customer or tenant data, and these recordings are meant to be publishable.
# Deterministic pseudonymization keeps the join intact and loses no testable outcome, because a
# test builds its NetBox VRF from the fixture anyway. See _anon_vrf_name for the one exception.
VRF_NAME_KEYS = frozenset({"vrf_name"})
# The SNMP table index of the VRF row. On Nokia it is a plain ordinal ("1"), but the ENTITY form
# other platforms report is the name length followed by its ASCII codes — 8.77.103.109.116.45.118
# .114.102 spells "Mgmt-vrf" — so copying it through would publish the name the rule above hides.
# Read by nothing, so it is mapped to a stable dotted-decimal token of the same shape.
VRF_INDEX_KEYS = frozenset({"vrf_oid"})
# The route distinguisher holds an ASN or an IP, then the assigned number. Identifying, read by
# nothing, and find_pii trips on the IP form, so it is remapped with its shape kept.
ROUTE_DISTINGUISHER_KEYS = frozenset({"mplsVpnVrfRouteDistinguisher"})
SERIAL_KEYS = frozenset({"serial", "entPhysicalSerialNum"})
# `display` is the LibreNMS device display name — operators often set it to a real FQDN.
HOSTNAME_KEYS = frozenset({"hostname", "sysName", "remote_hostname", "display"})
# Device chassis SKU (e.g. "WS-C3560X-24T-S"). Pseudonymized: it's not a module-matching key, so
# blanking it loses no testable outcome. (entPhysicalModelName / transceiver `model` ARE matching
# keys and are preserved via PRESERVE_KEYS instead.)
# ``remote_platform`` is the neighbour's chassis SKU on a link row: same kind of value, and it
# names the vendor the os-hash masks.
MODEL_KEYS = frozenset({"hardware", "remote_platform"})
# entPhysicalMfgName is the ENTITY-MIB manufacturer name (e.g. "Cisco Systems Inc."). It names the
# vendor the os-hash deliberately masks and is read by no sync logic, so it's pseudonymized to a
# deterministic MFG-<hash> — the field shape (present, non-empty) survives without the vendor.
# "vendor" is the transceiver vendor LibreNMS reports on /transceivers. Same shape and same
# reasoning: it names the vendor the os-hash masks, and no sync logic reads it.
MFG_KEYS = frozenset({"entPhysicalMfgName", "vendor"})
# ENTITY-MIB names and descriptions are operator-visible display text. They can contain internal
# hostnames or labels. The structural class, index, containment, position and public model SKU live
# in separate preserved fields, so replace this free text while keeping equal values correlated.
ENTITY_TEXT_KEYS = frozenset({"entPhysicalName", "entPhysicalDescr"})
# The transceiver OUI is the IEEE-registered manufacturer prefix as an integer: 36965 is 0x009065,
# Finisar. Where "vendor" is null it is the only vendor identifier on the row, so masking the name
# alone would leave the vendor readable. Mapped to a deterministic 24-bit value, which keeps the
# integer type and the row-to-row cardinality the recording exists to preserve. Read by no sync
# logic. 0 means "no OUI" in LibreNMS and is left alone so it stays distinguishable from a mask.
OUI_KEYS = frozenset({"oui"})
# Firmware / software version strings. Identifying (pin an exact build → deployment fingerprint /
# CVE surface) and read by no sync logic, so pseudonymized to a deterministic fw-<hash>. (Device
# chassis HARDWARE revision is left alone — it's not a firmware version.)
VERSION_KEYS = frozenset({"version", "features", "entPhysicalFirmwareRev", "entPhysicalSoftwareRev", "remote_version"})
# ``ipv4_address`` / ``ipv6_address`` / ``ipv6_compressed`` are the /devices/{id}/ip row forms
# parse_librenms_ip_entry accepts. They are real routable addresses, so they map to the
# documentation ranges like every other address.
IP_KEYS = frozenset(
    {
        "ip",
        "ipv4",
        "ipv6",
        "inet",
        "ip_address",
        "overwrite_ip",
        "ipv4_address",
        "ipv6_address",
        "ipv6_compressed",
    }
)
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
        # VRF description is operator free text read by nothing; the SNMPv3 context name on an IP
        # row is often the VRF name again, which is exactly the value pseudonymized below.
        "mplsVpnVrfDescription",
        "context_name",
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
_SECRET_KEY_HINTS = (
    "pass",
    "community",
    "secret",
    "token",
    "cryptopass",
    "privkey",
    "priv_key",
    "private_key",
    "authkey",
    "auth_key",
    "apikey",
    "api_key",
)

# Documentation/synthetic ranges this module emits — find_pii() allows them.
_DOC_IP_NETWORKS = tuple(
    ipaddress.ip_network(network) for network in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)
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
# Match a full email AND the dotless internal-domain form (``user@host``): internal mail domains and
# short-hostname contacts (e.g. ``netops@corp``) carry no dot, so requiring one let a real leak slip
# through a preserved free-text field. Two ReDoS-safety details keep this linear: each domain label is
# ``[\w-]+`` (NO ``.``), so ``.`` is an unambiguous separator — a ``[\w.-]`` label would overlap the
# ``\.`` and backtrack exponentially (CodeQL py/redos); and the local part is bounded to the RFC 5321
# max of 64, so a long no-``@`` run can't backtrack quadratically across word boundaries. A local part
# is still required, so a bare ``@handle`` or a spaced ``x @ y`` does not match.
_EMAIL_RE = re.compile(r"\b[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)*\b")
# Free-text FQDN safety-net (dotted labels + an alphabetic TLD). Anonymized hostnames are
# single-label ("device-xxxx") and versions/OIDs lack an alpha TLD, so they don't match.
_FQDN_RE = re.compile(r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}\b")
# find_pii: keys whose values are identifiers (firmware/model/version) that can look like a
# dotted IP (e.g. version "3.9.0.4") but are NOT addresses — exempt them from IP/FQDN flagging.
_IP_EXEMPT_KEYS = frozenset(
    {
        "version",
        "hardware",
        "features",
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
    "fti|sp|dsc|lsi|mtun|vt|esa|lag|demux|"
    "swp|bond|eth|ens|eno|enp|veth|mgmt|vni|br|"
    "Gi|Te|Fa|Hu|Fo|Twe|Eth|Po|BE|Lo|Tu|Se|nve|BVI|Null"
)
# Junos pseudo-interfaces whose base name carries no slot/digit (irb, jsrv, dsc, ...). Without a
# dedicated rule the general "prefix + digit" shape below never matches them, so a base/sub-unit
# pair like jsrv <-> jsrv.1 anonymizes to two unrelated iface-<hash> values and the resolver's
# name-based sub-unit pairing (low_name startswith high_name + '.') is lost. Match the bare base
# (optionally + '.unit') as a COMPLETE token, word-bounded, so the relationship survives while free
# text like "jsrv-customer" is still pseudonymized rather than preserved verbatim (which would leak
# it. Making the trailing digit optional would also preserve any value that starts with a short
# prefix such as "ip" or "lo".
_DIGITLESS_IF_NAMES = "jsrv|irb|dsc|lsi|mtun|pimd|pime|tap|gre|ipip|esi|mif|rbeb|vtep"
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
# Linux tunnel devices. Some start with a prefix that is itself in _IF_PREFIXES ("ip", "gre"), so
# the generic "prefix + digit" shape below matches only the leading "ip6" and the trailing run
# stops at the first letter: ip6tnl0 and ip6tnl1 both reduced to "ip6", and _build_name_index then
# dropped the ambiguous token. The rest (sit0, tunl0, erspan0) matched no rule at all and were
# hashed, losing the pairing a tunnel name carries. Matched first so the full name survives.
_LINUX_TUNNEL_IF_RE = r"(?:ip6gretap|ip6gre|ip6tnl|gretap|erspan|tunl|sit|ipip|gre)\d+"

_PORT_TOKEN_RE = re.compile(
    rf"^(?:(?:{_DIGITLESS_IF_NAMES})(?:\.\d+)?(?![\w/.:-])"
    rf"|(?:{_LINUX_TUNNEL_IF_RE})[\d/.:-]*"
    rf"|(?:{_LINUX_PREDICTABLE_IF_RE})[\d/.:-]*"
    rf"|(?:[A-Za-z]?\d+(?:/[A-Za-z]*\d+)+|[A-Za-z]/\d+|(?:{_IF_PREFIXES})-?\d)[\d/.:-]*)"
)


def _anon_oui(value, salt):
    """Return a deterministic 24-bit stand-in for an IEEE OUI, keeping the original's type."""
    if value in (None, "", 0, "0"):
        return value  # LibreNMS reports no OUI; masking it would invent one
    masked = int(_hash(str(value), salt, length=6), 16) & 0xFFFFFF
    return str(masked) if isinstance(value, str) else masked


def _hash(value, salt, length=6):
    """Return a stable short hex digest of *value* (salted), for deterministic pseudonyms."""
    return hashlib.sha256(f"{salt}::{value}".encode()).hexdigest()[:length]


# A pseudonymized OS token, e.g. "os-1a2b3c". Used to recognize an already-anonymized value.
_OS_TOKEN_RE = re.compile(r"^os-[0-9a-f]{6}$")
_ENTITY_LOCATOR = r"\d+(?:/(?:\d+|[xc]\d+))+"
_ENTITY_LOCATOR_RE = re.compile(rf"^{_ENTITY_LOCATOR}$")
_ENTITY_LOCATOR_SUFFIX_RE = re.compile(rf"(?<![A-Za-z0-9])(?P<locator>{_ENTITY_LOCATOR})$")
_ENTITY_TOKEN_RE = re.compile(rf"^entity-[0-9a-f]{{6}}(?: {_ENTITY_LOCATOR})?$")


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


# IPv4 documentation ranges the allocator draws from. 198.51.100.0/24 is deliberately left out:
# the replay stub hands its synthesized OOB controllers addresses in that block, and a clash there
# is a duplicate device lookup alias, not just a duplicate address.
_DOC_IPV4_PREFIXES = ("192.0.2", "203.0.113")
# Probes before giving up on placing one address. Far more than a recording can need: the bound
# exists so a pathological input fails loudly instead of looping.
_DOC_IP_MAX_PROBES = 64


def _doc_ip_candidate(addr, salt, attempt):
    """Return the *attempt*-th deterministic documentation address for *addr*."""
    keyed = f"{addr}#{attempt}"
    if ":" in addr:
        return f"2001:db8::{_hash(keyed, salt, 4)}"
    digest = int(_hash(keyed, salt, 6), 16)
    prefix = _DOC_IPV4_PREFIXES[digest % len(_DOC_IPV4_PREFIXES)]
    return f"{prefix}.{digest // len(_DOC_IPV4_PREFIXES) % 254 + 1}"


def _doc_ip(value, rules):
    """
    Map an IP (optionally with a /prefix) to a documentation address, unique within the recording.

    Uniqueness is the point, not just pseudonymity. A recording used to carry a single address
    (the device row), so a hash collision was theoretical; it now carries every
    ``/devices/{id}/ip`` row, and two real addresses landing on one documentation address would
    make the fixture claim a duplicate address the device never reported — which is a shape the IP
    tab treats specially. Equal inputs still map to one output, so the join survives.

    Args:
        value (str): The address, optionally with a ``/prefix`` suffix.
        rules (_Rules): The per-recording rules carrying the allocation table.

    Returns:
        str: The documentation address, with the original prefix suffix when there was one.

    """
    addr, sep, prefix = value.partition("/")
    assigned = rules.doc_ips if rules.doc_ips is not None else {}
    if addr not in assigned:
        taken = set(assigned.values())
        for attempt in range(_DOC_IP_MAX_PROBES):
            candidate = _doc_ip_candidate(addr, rules.salt, attempt)
            if candidate not in taken:
                assigned[addr] = candidate
                break
        else:
            raise RuntimeError(
                f"Cannot place {len(assigned) + 1} distinct addresses in the documentation ranges "
                "without repeating one; compress the recording further"
            )
    anon = assigned[addr]
    return f"{anon}{sep}{prefix}" if sep else anon


def _synthetic_mac(value, salt):
    """Map a MAC to a deterministic address in the locally-administered 02:00:00 block."""
    digest = _hash(value, salt, 6)
    return f"{_SYNTH_MAC_PREFIX}:{digest[0:2]}:{digest[2:4]}:{digest[4:6]}"


# A name preserved only because the recording's own LAG patterns match it must still LOOK like a
# machine port name: those patterns are operator-authored (and, in a downloaded recording,
# untrusted), so a sloppy one like "^.+$" would otherwise preserve every free-text label verbatim.
# Aggregate-numbered, no spaces and no '_' (both mark free text like "to_core-rtr Customer A").
_LAG_NAME_SHAPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9./:-]{0,30}\d$")


def _anon_interface_name(value, rules):
    """
    Anonymize an ifName/ifDescr while preserving the logic-bearing port-name token.

    The relationship resolver keys on ``ifName`` (sub-interface ``parent.N`` split, LAG name
    patterns); ``ifDescr`` is only a whole-string name fallback. So we keep the leading port-name
    token verbatim and drop everything after it (the free-text annotation that leaks hostnames,
    customer/engineer names). Names with no recognizable port token become a deterministic
    ``iface-<hash>`` pseudonym.

    Args:
        value (str): The raw ifName/ifDescr value.
        rules (_Rules): The recording-derived rules (salt + compiled LAG patterns).

    Returns:
        str: The preserved token, or a stable pseudonym.

    """
    if ANON_INTERFACE_NAME_RE.fullmatch(value):
        return value
    match = _PORT_TOKEN_RE.match(value)
    if match:
        token = match.group(0)
        # Whole value is a port name → keep it; otherwise it had a free-text tail → keep only
        # the token (e.g. "lag2, IP interface, ** core-rtr ae42 **" → "lag2").
        return token
    # A name the recording's OWN lag_patterns recognize is logic-bearing even when the built-in
    # prefix list doesn't know it (the patterns are captured precisely because an operator can name
    # aggregates their own way). Hashing it would leave the pattern matching nothing, so replay
    # stops seeing the aggregate at all — the shape the recording exists to reproduce.
    if _LAG_NAME_SHAPE_RE.match(value) and name_matches_lag_pattern(value, rules.lag_patterns):
        return value
    base, separator, suffix = value.rpartition(".")
    if separator and suffix.isdigit() and base:
        return f"{_anon_interface_name(base, rules)}.{suffix}"
    return f"{ANON_INTERFACE_NAME_PREFIX}{_hash(value, rules.salt)}"


# A default Avocent serial-port label is a generic, device-agnostic port name: a known serial/console
# prefix + a port number, optionally with LibreNMS's trailing " Status" (e.g. "ttyS49 Status",
# "Port 7"). Anything else — including a SHORT hostname like "core1" that's structurally just
# letters+digits — is treated as a customised label and pseudonymized, since a bare letters+digits
# rule would preserve (leak) such hostnames. The prefix allowlist is what keeps the two apart.
_SERIAL_DEFAULT_LABEL_RE = re.compile(
    r"^(?:ttyUSB|ttyS|ttyD|tty|port|serial|console|com)[\s-]?\d+(?: Status)?$",
    re.IGNORECASE,
)


def _anon_serial_label(value, rules):
    """
    Anonymize a serial-port sensor description while preserving the is_configured outcome.

    ``map_sensors_to_serial_links`` derives ``is_configured`` from whether the label (after its
    " Status" suffix is stripped) differs from the default port name. A generic/default label
    carries no PII and is kept verbatim so that outcome is preserved; a customised label is the
    remote device's hostname, so it becomes a deterministic ``device-<hash>`` pseudonym (still a
    non-empty value distinct from the port name, so is_configured stays True).

    Args:
        value (str): The raw sensor_descr value.
        rules (_Rules): The recording-derived rules (salt + default serial-label matchers).

    Returns:
        str: The preserved default label, or a stable hostname pseudonym.

    """
    # The recording's own serial_type_patterns come first: THEY define what the default port name
    # is (the seeded Cisco map is "Line {N}", which the built-in prefix list below does not know).
    # Hashing such a label would flip is_configured False -> True on replay.
    if any(matcher.match(value) for matcher in rules.serial_default_labels):
        return value
    if _SERIAL_DEFAULT_LABEL_RE.match(value):
        return value
    return f"device-{_hash(value, rules.salt)}"


def _anon_entity_text(value, rules):
    """Replace private ENTITY text while keeping a bounded terminal hierarchy locator."""
    normalized = value.strip()
    if not normalized or _ENTITY_TOKEN_RE.fullmatch(normalized) or _ENTITY_LOCATOR_RE.fullmatch(normalized):
        return normalized
    locator_match = _ENTITY_LOCATOR_SUFFIX_RE.search(normalized)
    token = f"entity-{_hash(normalized, rules.salt)}"
    if locator_match:
        return f"{token} {locator_match.group('locator')}"
    return token


def _anon_oid(value, salt):
    """Map an SNMP OID to a deterministic OID under the example-enterprise arc (hides the vendor)."""
    prefix = "." if value.startswith(".") else ""
    return f"{prefix}1.3.6.1.4.1.32473.{int(_hash(value, salt, 6), 16)}"


def _anon_asn(value, salt):
    """Map a BGP ASN to a deterministic 16-bit private ASN (64512-65534), preserving int type."""
    if value in (None, "", 0):
        return value
    return 64512 + int(_hash(str(value), salt, 4), 16) % 1023


# The Nokia global routing instance. "Base is not a VRF" is a logic-bearing rule the VRF
# suggestion has to apply, so a fixture must be able to express it: the literal survives.
_GLOBAL_ROUTING_INSTANCE = "Base"


def _anon_vrf_name(value, salt):
    """Map a VRF name to a deterministic pseudonym, keeping the global-instance literal."""
    if value == _GLOBAL_ROUTING_INSTANCE:
        return value
    return f"vrf-{_hash(value, salt)}"


def _anon_vrf_index(value, salt):
    """Map a VRF SNMP index to a stable dotted-decimal token (the real one can spell the name)."""
    digest = _hash(value, salt, 6)
    return ".".join(str(int(digest[i : i + 2], 16)) for i in (0, 2, 4))


def _anon_route_distinguisher(value, rules):
    """Remap an ASN:NN or IP:NN route distinguisher, keeping which of the two forms it is."""
    salt = rules.salt
    administrator, separator, assigned = value.rpartition(":")
    if not separator:
        return f"rd-{_hash(value, salt)}"
    if _looks_like_ip(administrator):
        administrator = _doc_ip(administrator, rules)
    else:
        administrator = str(_anon_asn(administrator, salt))
    return f"{administrator}:{int(_hash(value, salt, 4), 16)}"


def _looks_like_ip(value):
    """Return whether *value* parses as an IP address."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _anon_value(key, value, rules):  # noqa: C901
    """Apply the field rule for a single scalar (non-container) value keyed by *key*."""
    salt = rules.salt
    if key in PRESERVE_KEYS:
        return value
    if key in GEO_KEYS:
        return None
    if key in BGP_KEYS:
        # Handled before the str-guard below since ASNs arrive as ints.
        return _anon_asn(value, salt)
    if key in OUI_KEYS:
        # Same reason: LibreNMS reports the OUI as an integer.
        return _anon_oui(value, salt)
    if not isinstance(value, str) or not value or value == "-":
        # Pseudonym/scrub rules below operate on real string values; leave empties/sentinels
        # and non-strings (ints, bools, null) untouched so logic-bearing numerics survive.
        return value
    if key in INTERFACE_NAME_KEYS:
        return _anon_interface_name(value, rules)
    if key in SERIAL_LABEL_KEYS:
        return _anon_serial_label(value, rules)
    if key in SERIAL_KEYS:
        return f"SN-{_hash(value, salt)}"
    if key in VRF_NAME_KEYS:
        return _anon_vrf_name(value, salt)
    if key in VRF_INDEX_KEYS:
        return _anon_vrf_index(value, salt)
    if key in ROUTE_DISTINGUISHER_KEYS:
        return _anon_route_distinguisher(value, rules)
    if key in HOSTNAME_KEYS:
        return f"device-{_hash(value, salt)}"
    if key in MODEL_KEYS:
        return f"MODEL-{_hash(value, salt)}"
    if key in MFG_KEYS:
        return f"MFG-{_hash(value, salt)}"
    if key in ENTITY_TEXT_KEYS:
        return _anon_entity_text(value, rules)
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
        return _doc_ip(value, rules)
    if key in MAC_KEYS:
        return _synthetic_mac(value, salt)
    if key in LOCATION_KEYS:
        return "Lab"
    if key in FREETEXT_KEYS or key in SNMP_CREDENTIAL_KEYS:
        return ""
    return value


class _Rules(NamedTuple):
    """The per-recording anonymization context threaded through the walk."""

    salt: str = ""
    # Compiled LAG name patterns captured WITH the recording (see _anon_interface_name).
    lag_patterns: tuple = ()
    # Compiled "this is the default port label" matchers built from the recording's
    # serial_type_patterns (see _anon_serial_label).
    serial_default_labels: tuple = ()
    # Documentation addresses already handed out for this recording, keyed by the real address
    # (see _doc_ip). A NamedTuple default is shared by every instance, so a mutable one would leak
    # across recordings: anonymize_recording always passes a fresh dict, and None means "no
    # recording context", which only a direct _Rules() in a test produces.
    doc_ips: dict | None = None


# A serial port_name_pattern is a short template like "ttyS{N}" / "Line {N}" (the model caps it at
# 100 chars), and a recording carries one per recognized vendor sensor table. Bound both, mirroring
# the LAG-pattern caps, so a downloaded recording can't hand us thousands of long templates.
_MAX_SERIAL_PATTERNS = 100
_MAX_SERIAL_PATTERN_LEN = 100


def _compile_serial_default_labels(recording):
    r"""
    Build the "default serial label" matchers from a recording's ``serial_type_patterns``.

    ``map_sensors_to_serial_links`` names the local port by ``port_name_pattern.format(N=<port>)``
    and reads ``is_configured`` from whether the sensor label differs from that name — so what
    counts as a DEFAULT label is defined by the recording's own patterns, not by a fixed prefix
    list. The literal parts are re.escape'd and only ``{N}`` becomes ``\\d+``, so the result carries
    no quantifier the (untrusted) template could have supplied.

    Args:
        recording (dict): A recording; a missing/non-dict map yields no matchers.

    Returns:
        tuple[re.Pattern, ...]: Matchers accepting the rendered name, with LibreNMS's optional
            trailing " Status" suffix.

    """
    patterns = recording.get("serial_type_patterns")
    patterns = patterns if isinstance(patterns, dict) else {}
    compiled = []
    for template in list(patterns.values())[:_MAX_SERIAL_PATTERNS]:
        if not isinstance(template, str) or "{N}" not in template or len(template) > _MAX_SERIAL_PATTERN_LEN:
            continue
        head, _, tail = template.partition("{N}")
        compiled.append(re.compile(rf"^{re.escape(head)}\d+{re.escape(tail)}(?: Status)?$", re.IGNORECASE))
    return tuple(compiled)


def _walk(obj, rules):
    """Recursively anonymize a parsed JSON body (dict/list/scalar)."""
    if isinstance(obj, dict):
        return {
            k: (_walk(v, rules) if isinstance(v, (dict, list)) else _anon_value(k, v, rules)) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_walk(item, rules) for item in obj]
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
    # The recording's own LAG / serial-label patterns decide which names are logic-bearing, so they
    # are compiled once here and threaded through the walk.
    rules = _Rules(
        salt=salt,
        lag_patterns=tuple(compile_lag_patterns(recording)),
        serial_default_labels=_compile_serial_default_labels(recording),
        doc_ips={},
    )
    out["responses"] = {key: _walk(body, rules) for key, body in recording.get("responses", {}).items()}
    # Pseudonymize meta.os too (it's outside `responses`, so _walk doesn't reach it) and key the
    # neutral name off the pseudonymized token, so neither the metadata nor the name leaks the OS.
    meta = dict(recording.get("meta") or {})
    if meta.get("os"):
        meta["os"] = pseudonymize_os(meta["os"])
    out["meta"] = meta
    # lag_patterns keys are raw LibreNMS OS names — pseudonymize them like meta.os so the
    # recording doesn't leak the platform. The pattern VALUES are what signature/replay
    # consume (values-only), so the shape behaviour is unchanged.
    lag_patterns = recording.get("lag_patterns")
    if isinstance(lag_patterns, dict):
        out["lag_patterns"] = {(pseudonymize_os(k) if k else k): v for k, v in lag_patterns.items()}
    # sap_patterns is keyed the same way and needs the same treatment, or a replay reads the SAP
    # rule under an OS name that no longer matches the pseudonymized meta.os.
    sap_patterns = recording.get("sap_patterns")
    if isinstance(sap_patterns, dict):
        out["sap_patterns"] = {(pseudonymize_os(k) if k else k): v for k, v in sap_patterns.items()}
    # serial_type_patterns passes through VERBATIM (via the dict copy above) — deliberately,
    # unlike lag_patterns: its keys are vendor sensor-table identifiers (acsSerialPortTable),
    # not OS names, and replay feeds them back through the sensor_types injection points where
    # matching is exact — pseudonymized keys would recognize nothing. Values are {N}-templates
    # for local port names, not PII.
    out["name"] = f"{meta.get('os') or 'device'}-shape-{_hash(recording.get('name', ''), salt)}"
    out["description"] = "Anonymized LibreNMS data-shape capture."
    return out


def _is_documentation_address(value):
    """Return whether an address belongs to an RFC documentation network."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in _DOC_IP_NETWORKS)


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
                    if kind in ("ipv4", "ipv6") and _is_documentation_address(match):
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
