"""
Field-aware, deterministic anonymization of LibreNMS data-shape recordings.

The goal is to make a captured recording safe to share publicly while keeping every field the
sync logic actually reads intact, so the anonymized recording still drives the same outcome
tests. Three strategies, by field:

* **Preserve verbatim** the logic-bearing fields (ifName/ifType/port ids, ENTITY-MIB
  class/index/position, VLANs, transceiver optics, os). Anonymizing these would destroy the
  LAG/sub-interface/VC detection the recordings exist to test.
* **Pseudonymize deterministically** identifiers (serials, hostnames, model SKUs): the same
  input always maps to the same fake, so cross-references (a device serial that equals a stack
  member serial) still match after anonymization.
* **Scrub** PII to safe placeholders (IPs → RFC 5737/3849 documentation ranges, MACs → a
  synthetic ``02:00:00`` block, lat/lng → null, location → ``"Lab"``, free-text → "").

:func:`find_pii` is a regex safety-net: it sweeps the anonymized result for any IP/MAC/email the
field rules missed, so a human (or the mgmt command) can catch leaks before publishing.
"""

import hashlib
import re

# Logic-bearing fields the sync/detection/relationship code reads — never altered.
PRESERVE_KEYS = frozenset(
    {
        "ifName",
        "ifDescr",
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
        "os",
        "device_id",
        "status",
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
    }
)
SERIAL_KEYS = frozenset({"serial", "entPhysicalSerialNum"})
HOSTNAME_KEYS = frozenset({"hostname", "sysName", "remote_hostname"})
MODEL_KEYS = frozenset({"hardware", "entPhysicalModelName"})
IP_KEYS = frozenset({"ip", "ipv4", "ipv6", "inet", "ip_address", "overwrite_ip"})
MAC_KEYS = frozenset({"ifPhysAddress", "mac", "mac_address"})
GEO_KEYS = frozenset({"lat", "lng", "latitude", "longitude"})
LOCATION_KEYS = frozenset({"location", "sysLocation"})
FREETEXT_KEYS = frozenset({"ifAlias", "sysContact", "sysDescr", "purpose", "notes"})

# Documentation/synthetic ranges this module emits — find_pii() allows them.
_DOC_IP_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.", "2001:db8")
_SYNTH_MAC_PREFIX = "02:00:00"

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){3,}[0-9a-fA-F]{1,4}\b")
_MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _hash(value, salt, length=6):
    """Return a stable short hex digest of *value* (salted), for deterministic pseudonyms."""
    return hashlib.sha256(f"{salt}::{value}".encode()).hexdigest()[:length]


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


def _anon_value(key, value, salt):
    """Apply the field rule for a single scalar (non-container) value keyed by *key*."""
    if key in PRESERVE_KEYS:
        return value
    if key in GEO_KEYS:
        return None
    if not isinstance(value, str) or not value or value == "-":
        # Pseudonym/scrub rules below operate on real string values; leave empties/sentinels
        # and non-strings (ints, bools, null) untouched so logic-bearing numerics survive.
        return value
    if key in SERIAL_KEYS:
        return f"SN-{_hash(value, salt)}"
    if key in HOSTNAME_KEYS:
        return f"device-{_hash(value, salt)}"
    if key in MODEL_KEYS:
        return f"MODEL-{_hash(value, salt)}"
    if key in IP_KEYS:
        return _doc_ip(value, salt)
    if key in MAC_KEYS:
        return _synthetic_mac(value, salt)
    if key in LOCATION_KEYS:
        return "Lab"
    if key in FREETEXT_KEYS:
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

    Top-level ``name``/``description``/``meta`` (human-authored, not device PII) are left as-is;
    only the captured ``responses`` are transformed. Pass a stable *salt* to make the pseudonyms
    reproducible across runs (and unique per contributor if desired).

    Args:
        recording (dict): A recording as produced by :func:`.capture.capture_device_recording`.
        salt (str): Optional salt mixed into the deterministic pseudonym hashes.

    Returns:
        dict: A new recording with anonymized ``responses``.
    """
    out = dict(recording)
    out["responses"] = {key: _walk(body, salt) for key, body in recording.get("responses", {}).items()}
    return out


def find_pii(recording):
    """
    Sweep a recording's responses for residual IP/MAC/email strings the field rules missed.

    The documentation IP ranges and the synthetic ``02:00:00`` MAC block this module emits are
    treated as safe and not reported.

    Args:
        recording (dict): The recording to scan (typically the anonymized output).

    Returns:
        list[dict]: One ``{"path", "kind", "value"}`` entry per residual match.
    """
    findings = []

    def scan(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                scan(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                scan(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            for kind, regex in (("ipv4", _IPV4_RE), ("ipv6", _IPV6_RE), ("mac", _MAC_RE), ("email", _EMAIL_RE)):
                for match in regex.findall(obj):
                    if kind in ("ipv4", "ipv6") and match.startswith(_DOC_IP_PREFIXES):
                        continue
                    # A 6-octet MAC also satisfies the loose IPv6 pattern; let the dedicated mac
                    # kind handle it so a synthetic MAC isn't double-reported as a bogus IPv6.
                    if kind == "ipv6" and _MAC_RE.fullmatch(match):
                        continue
                    if kind == "mac" and match.lower().startswith(_SYNTH_MAC_PREFIX):
                        continue
                    findings.append({"path": path, "kind": kind, "value": match})

    scan(recording.get("responses", {}), "responses")
    return findings
