"""
Lightweight, informational fingerprint of what a data-shape recording exercises.

The signature captures the axes that matter for the outcome tests — OS family, Virtual-Chassis
shape, LAG style, sub-interface style, port_stack/VLAN/transceiver presence — so a contributor
(or the mgmt command) can get a fuzzy "is this shape already covered?" verdict. It is a nudge,
not a gate: :func:`classify_novelty` returns ``"new"`` / ``"likely-covered"`` with the closest
match and a reason, never an authoritative decision.
"""

import re

from netbox_librenms_plugin.data_shapes.anonymize import pseudonymize_os

# Vendor kinship for the *similarity* signal only — NOT for collapsing distinct OSes into one
# "covered" verdict. ios / iosxr / nxos share a vendor but their ifName/ifDescr conventions differ,
# so an ios recording does not *cover* an iosxr shape; it's only "similar". Recordings carry a
# pseudonymized OS (see pseudonymize_os), so the map is precomputed for BOTH the raw OS strings and
# their stable os-<hash> tokens — that way novelty works whether it reads a raw or anonymized
# signature. Extend with new variants (e.g. a Junos-Evolved OS string) as they appear.
_OS_FAMILY_RAW = {
    "ios": "cisco",
    "iosxe": "cisco",
    "iosxr": "cisco",
    "nxos": "cisco",
    "asa": "cisco",
    "ftd": "cisco",
    "junos": "juniper",
    "junos-evo": "juniper",
    "junosevo": "juniper",
    "eos": "arista",
    "routeros": "mikrotik",
    "timos": "nokia",
    "sros": "nokia",
    "arcos": "arrcus",
    "vrp": "huawei",
}
_OS_FAMILY = {}
for _os, _fam in _OS_FAMILY_RAW.items():
    _OS_FAMILY[_os] = _fam
    _OS_FAMILY[pseudonymize_os(_os)] = _fam


def _os_family(os_name):
    """
    Return a coarse vendor family for an OS string (raw or pseudonymized), for similarity matching.

    A recognized OS maps to its vendor (ios/iosxr → "cisco"); an unrecognized (niche) OS falls back
    to its own pseudonymized token, so two recordings of the same niche OS still relate to each
    other while different ones don't.
    """
    if not isinstance(os_name, str) or not os_name:
        return None
    return _OS_FAMILY.get(os_name) or _OS_FAMILY.get(os_name.lower()) or pseudonymize_os(os_name)


def _body(recording, predicate):
    """Return the first response body whose route key satisfies *predicate* (unwrapping [status, body])."""
    for key, value in recording.get("responses", {}).items():
        if predicate(key):
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
                return value[1]
            return value
    return None


def _inventory_items(body):
    """Return the ``inventory`` list from an inventory response body, or []."""
    if isinstance(body, dict) and isinstance(body.get("inventory"), list):
        return [i for i in body["inventory"] if isinstance(i, dict)]
    return []


def port_has_vlan(port):
    """
    Return whether a port row carries real VLAN data (the signature's ``vlans`` axis predicate).

    Keyed on the VALUE, not mere key presence: ``ifVlan`` ``None``/``""``/``0``/``"0"`` (LibreNMS's
    no-/default-VLAN sentinels — ifVlan is string-valued JSON elsewhere in the client, so the
    string ``"0"`` is the same sentinel as the int ``0``) do NOT count, only a real id or a
    non-empty ``vlans`` list. The compressor's port fingerprint imports this so its VLAN axis stays
    in lockstep with the signature (otherwise two same-shape ports — one with ``ifVlan: 0``/``None``
    — could collapse to a representative whose value flips the signature's vlans axis).
    """
    return port.get("ifVlan") not in (None, "", 0, "0") or bool(port.get("vlans"))


def _ports(recording):
    """Return the HOST device's ports list (anchored on devices/<device_id>/ports), or []."""
    # Anchor on the host device_id exactly as compress.py does, so an OOB recording carrying a second
    # /ports route (the controller's, devices/<oob_id>/ports) can't be fingerprinted instead of the
    # host's just because it happens to come first in dict order. Fall back to any /ports route when
    # the recording has no device_id or a differently-keyed host route.
    device_id = recording.get("device_id")
    body = None
    if device_id is not None:
        body = _body(recording, lambda k: k.split("?", 1)[0].endswith(f"devices/{device_id}/ports"))
    if body is None:
        body = _body(recording, lambda k: k.split("?", 1)[0].endswith("/ports"))
    if isinstance(body, dict) and isinstance(body.get("ports"), list):
        return [p for p in body["ports"] if isinstance(p, dict)]
    return []


# Upper bound on the interface name fed to an untrusted (recording-supplied) LAG regex. Catastrophic
# backtracking cost scales with the search-input length, so capping it keeps a valid-but-pathological
# pattern in a community-submitted recording from hanging the `librenms_recordings --validate` command
# (which runs in CI). Real interface names are well under this; a longer one is anonymized garbage that
# never needs LAG name-pattern classification.
_MAX_LAG_NAME_LEN = 256


def compute_shape_signature(recording):
    """
    Compute the testing-relevant fingerprint of a recording.

    Args:
        recording (dict): A recording (raw or anonymized — the signature only reads structural
            fields that anonymization preserves).

    Returns:
        dict: ``{os, virtual_chassis:{present,root_class,member_count,position_base},
            lag:{present,ieee8023ad,name_prefix}, sub_interfaces:{present,styles}, port_stack,
            vlans, transceivers, oob}``.
    """
    device_id = recording.get("device_id")
    dev_body = _body(recording, lambda k: k == f"GET /api/v0/devices/{device_id}")
    # `recording.get("meta", {})` returns None when "meta" is present-but-null (the default only
    # applies when the key is absent), and `or {}` still passes a truthy NON-dict through — the
    # schema doesn't validate meta, so a community recording can carry "meta": null or
    # "meta": "garbage" and neither must crash --validate/--list. Normalize to a dict once.
    meta = recording.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    os_name = meta.get("os")
    if os_name is None and isinstance(dev_body, dict):
        devices = dev_body.get("devices")
        if isinstance(devices, list) and devices and isinstance(devices[0], dict):
            os_name = devices[0].get("os")

    # Virtual chassis: root container class + chassis members under it.
    root_items = _inventory_items(_body(recording, lambda k: "entPhysicalContainedIn=0" in k))
    root_class = None
    if any(i.get("entPhysicalClass") == "stack" for i in root_items):
        root_class = "stack"
    elif any(i.get("entPhysicalClass") == "chassis" for i in root_items):
        root_class = "chassis"
    chassis = [i for i in _inventory_items(_body(recording, lambda k: "entPhysicalClass=chassis" in k))]
    positions = [i.get("entPhysicalParentRelPos") for i in chassis if isinstance(i.get("entPhysicalParentRelPos"), int)]
    vc = {
        "present": len(chassis) > 1,
        "root_class": root_class,
        "member_count": len(chassis),
        "position_base": min(positions) if positions else None,
    }

    # LAG + sub-interface styles from ports. LAG detection mirrors the client's
    # resolve_port_relationships._is_lag_aggregate: an ieee8023adLag ifType OR a name matching a
    # configured per-OS LAG pattern (captured in the recording's lag_patterns). Reading ifType alone
    # would fingerprint a pattern-based LAG shape (e.g. Cisco "Po1") as lag.present=False, collapsing
    # a real LAG into a non-LAG novelty bucket.
    ports = _ports(recording)
    compiled_lag_patterns = []
    # Same normalize-before-access as meta above: a truthy non-dict lag_patterns (e.g. a list)
    # has no .values() and must degrade to "no patterns", not crash --validate.
    lag_patterns = recording.get("lag_patterns")
    lag_patterns = lag_patterns if isinstance(lag_patterns, dict) else {}
    for pattern_str in lag_patterns.values():
        try:
            compiled_lag_patterns.append(re.compile(pattern_str))
        except (re.error, TypeError):
            # recording lag_patterns are untrusted (community-submitted): a typo'd regex (re.error)
            # or a non-string value (TypeError on re.compile) is skipped, not crashed — mirroring
            # resolve_port_relationships' explicit-pattern hardening.
            continue

    def _lag_names(port):
        return [n for n in (port.get("ifName"), port.get("ifDescr")) if isinstance(n, str) and n]

    def _is_lag_port(port):
        if port.get("ifType") == "ieee8023adLag":
            return True
        # Bound the untrusted name length before applying an untrusted regex — see _MAX_LAG_NAME_LEN.
        return any(
            pat.search(name)
            for pat in compiled_lag_patterns
            for name in _lag_names(port)
            if len(name) <= _MAX_LAG_NAME_LEN
        )

    lag_ports = [p for p in ports if _is_lag_port(p)]
    lag_port_names = [name for p in lag_ports for name in _lag_names(p)]
    name_prefix = re.sub(r"\d+$", "", lag_port_names[0]) if lag_port_names else None
    sub_styles = set()
    for p in ports:
        # Scan BOTH name fields like every neighbouring detector (_lag_names above,
        # compress._fingerprint, resolve_port_relationships): on ifDescr-mode devices the
        # structured ".N" sub-unit name lives in ifDescr while ifName carries an arbitrary
        # (anonymized) label, and an ifName-only scan fingerprints them as sub-interface-free.
        for name in _lag_names(p):
            if re.search(r"\.\d+$", name):
                sub_styles.add("dot-numeric")
    port_stack_body = _body(recording, lambda k: k.endswith("/port_stack"))
    port_stack = bool(isinstance(port_stack_body, dict) and port_stack_body.get("mappings"))

    # Require an actual non-empty transceivers list, not merely a captured /transceivers body:
    # a JSON error/404 body is still "not None" and would otherwise inflate the novelty signature.
    transceiver_body = _body(recording, lambda k: k.endswith("/transceivers"))
    has_transceivers = (
        isinstance(transceiver_body, dict)
        and isinstance(transceiver_body.get("transceivers"), list)
        and bool(transceiver_body["transceivers"])
    )

    # OOB controller presence. Capture stamps meta["oob_id"] only when a separate OOB-controller
    # device's ports were merged into the host capture (and anonymization preserves meta), so it's
    # the authoritative signal. Without it an OOB and a non-OOB capture of the same host OS share an
    # identical signature and collapse into one novelty bucket — the capture modal would report an
    # OOB topology as already known when only the plain-host shape exists.
    oob_present = meta.get("oob_id") is not None  # meta normalized to a dict at the top

    return {
        "os": os_name,
        "virtual_chassis": vc,
        "lag": {
            "present": bool(lag_ports),
            # ieee8023ad reflects the ifType-based detection style specifically — a LAG found
            # only via a configured name pattern must not claim the 802.3ad-ifType style is
            # covered (present and ieee8023ad are distinct axes, else they'd always be equal).
            "ieee8023ad": any(p.get("ifType") == "ieee8023adLag" for p in lag_ports),
            "name_prefix": name_prefix,
        },
        "sub_interfaces": {"present": bool(sub_styles), "styles": sorted(sub_styles)},
        "port_stack": port_stack,
        "vlans": any(port_has_vlan(p) for p in ports),
        "transceivers": has_transceivers,
        "oob": oob_present,
    }


def _structural_axes(signature):
    """Reduce a signature to the OS-independent shape axes used for novelty matching."""
    # A partially malformed manifest entry can carry an explicit null (or non-dict) section —
    # dict.get(key, {}) returns the stored None when the key is present, so `.get()` below would
    # blow up. classify_novelty() feeds load_manifest() straight into here in the capture flow,
    # so fall back to {} per-section to degrade gracefully instead of breaking the modal.
    vc = signature.get("virtual_chassis")
    if not isinstance(vc, dict):
        vc = {}
    lag = signature.get("lag")
    if not isinstance(lag, dict):
        lag = {}
    sub = signature.get("sub_interfaces")
    if not isinstance(sub, dict):
        sub = {}
    # Coerce styles before tuple(): a malformed entry like {"styles": null} would otherwise raise
    # here, while every other malformed section is degraded gracefully above.
    styles = sub.get("styles", ())
    if not isinstance(styles, (list, tuple)):
        styles = ()
    return (
        vc.get("present", False),
        vc.get("root_class"),
        vc.get("member_count"),
        vc.get("position_base"),
        lag.get("present", False),
        lag.get("name_prefix"),
        sub.get("present", False),
        tuple(styles),
        signature.get("port_stack", False),
        signature.get("vlans", False),
        signature.get("transceivers", False),
        signature.get("oob", False),
    )


def build_manifest(recordings):
    """Build a novelty manifest (``[{name, signature}]``) from an iterable of recordings."""
    return [{"name": r.get("name"), "signature": compute_shape_signature(r)} for r in recordings]


def classify_novelty(signature, manifest):
    """
    Graded-similarity classification of a signature against a manifest of covered shapes.

    Three verdicts, because OS variants are *similar but not interchangeable* — an ios recording
    shares almost everything with an iosxr one yet they differ (notably ifName/ifDescr), so the
    former should not be reported as fully *covering* the latter:

    * ``likely-covered`` — a covered shape has the SAME OS (pseudonymized token) and the same
      VC/LAG/sub-interface shape.
    * ``similar`` — a covered shape has the same VC/LAG/sub-interface shape and a related OS (same
      vendor family, e.g. ios↔iosxr, junos↔Junos-Evolved) but not the exact OS — likely still worth
      adding, just informed by the close neighbour.
    * ``new`` — nothing shares both the shape and the OS family.

    Args:
        signature (dict): Output of :func:`compute_shape_signature` (raw or anonymized OS both work).
        manifest (list[dict]): ``[{name, signature}]`` entries (see :func:`build_manifest`).

    Returns:
        dict: ``{"verdict": "likely-covered" | "similar" | "new", "closest": <name|None>,
            "why": <str>}``.
    """
    target_shape = _structural_axes(signature)
    target_os = pseudonymize_os(signature.get("os"))
    target_family = _os_family(signature.get("os"))

    similar = None
    for entry in manifest:
        # The manifest is an on-disk artifact: a malformed item (a non-dict entry, or a
        # null/non-dict "signature") must be skipped rather than crash novelty evaluation in
        # the capture flow.
        if not isinstance(entry, dict):
            continue
        sig = entry.get("signature")
        if not isinstance(sig, dict):
            continue
        if _structural_axes(sig) != target_shape:
            continue
        name = entry.get("name")
        if pseudonymize_os(sig.get("os")) == target_os:
            return {
                "verdict": "likely-covered",
                "closest": name,
                "why": f"shares the same OS + VC/LAG/sub-interface shape with '{name}'",
            }
        if similar is None and target_family is not None and _os_family(sig.get("os")) == target_family:
            similar = name

    if similar is not None:
        return {
            "verdict": "similar",
            "closest": similar,
            "why": f"same VC/LAG/sub-interface shape as '{similar}' on a related OS variant — likely worth adding",
        }
    return {
        "verdict": "new",
        "closest": None,
        "why": "no covered shape shares its OS + VC/LAG/sub-interface shape",
    }
