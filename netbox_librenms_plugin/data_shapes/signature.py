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


def _ports(recording):
    """Return the ports list from the ports response body, or []."""
    body = _body(recording, lambda k: k.endswith("/ports"))
    if isinstance(body, dict) and isinstance(body.get("ports"), list):
        return [p for p in body["ports"] if isinstance(p, dict)]
    return []


def compute_shape_signature(recording):
    """
    Compute the testing-relevant fingerprint of a recording.

    Args:
        recording (dict): A recording (raw or anonymized — the signature only reads structural
            fields that anonymization preserves).

    Returns:
        dict: ``{os, virtual_chassis:{present,root_class,member_count,position_base},
            lag:{present,ieee8023ad,name_prefix}, sub_interfaces:{present,styles}, port_stack,
            vlans, transceivers}``.
    """
    device_id = recording.get("device_id")
    dev_body = _body(recording, lambda k: k == f"GET /api/v0/devices/{device_id}")
    os_name = recording.get("meta", {}).get("os")
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

    # LAG + sub-interface styles from ports.
    ports = _ports(recording)
    lag_ports = [p for p in ports if p.get("ifType") == "ieee8023adLag" and isinstance(p.get("ifName"), str)]
    name_prefix = re.sub(r"\d+$", "", lag_ports[0]["ifName"]) if lag_ports else None
    sub_styles = set()
    for p in ports:
        name = p.get("ifName")
        if isinstance(name, str) and re.search(r"\.\d+$", name):
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

    return {
        "os": os_name,
        "virtual_chassis": vc,
        "lag": {"present": bool(lag_ports), "ieee8023ad": bool(lag_ports), "name_prefix": name_prefix},
        "sub_interfaces": {"present": bool(sub_styles), "styles": sorted(sub_styles)},
        "port_stack": port_stack,
        "vlans": any(p.get("ifVlan") not in (None, "") or bool(p.get("vlans")) for p in ports),
        "transceivers": has_transceivers,
    }


def _structural_axes(signature):
    """Reduce a signature to its OS-independent shape axes (VC + LAG + sub-interface presence)."""
    vc = signature.get("virtual_chassis", {})
    return (
        vc.get("present", False),
        vc.get("root_class"),
        signature.get("lag", {}).get("present", False),
        signature.get("sub_interfaces", {}).get("present", False),
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
        sig = entry.get("signature", {})
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
