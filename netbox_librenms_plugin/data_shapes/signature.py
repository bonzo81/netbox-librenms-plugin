"""
Lightweight, informational fingerprint of what a data-shape recording exercises.

The signature captures the axes that matter for the outcome tests — OS family, Virtual-Chassis
shape, LAG style, sub-interface style, port_stack/VLAN/transceiver presence — so a contributor
(or the mgmt command) can get a fuzzy "is this shape already covered?" verdict. It is a nudge,
not a gate: :func:`classify_novelty` returns ``"new"`` / ``"likely-covered"`` with the closest
match and a reason, never an authoritative decision.
"""

import re

# Coarse OS-family grouping so e.g. ios/iosxe/nxos all read as "cisco" for novelty matching.
_OS_FAMILY = {
    "ios": "cisco",
    "iosxe": "cisco",
    "iosxr": "cisco",
    "nxos": "cisco",
    "junos": "juniper",
    "eos": "arista",
    "routeros": "mikrotik",
    "timos": "nokia",
    "sros": "nokia",
}


def _os_family(os_name):
    """Map a LibreNMS os string to a coarse vendor family (falls back to the raw os)."""
    if not isinstance(os_name, str) or not os_name:
        return None
    return _OS_FAMILY.get(os_name.lower(), os_name.lower())


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

    return {
        "os": os_name,
        "virtual_chassis": vc,
        "lag": {"present": bool(lag_ports), "ieee8023ad": bool(lag_ports), "name_prefix": name_prefix},
        "sub_interfaces": {"present": bool(sub_styles), "styles": sorted(sub_styles)},
        "port_stack": port_stack,
        "vlans": any("ifVlan" in p or "vlans" in p for p in ports),
        "transceivers": _body(recording, lambda k: k.endswith("/transceivers")) is not None,
    }


def _novelty_axes(signature):
    """Reduce a signature to the coarse axes novelty matching compares."""
    vc = signature.get("virtual_chassis", {})
    return (
        _os_family(signature.get("os")),
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
    Fuzzily classify a signature against a manifest of already-covered shapes.

    Args:
        signature (dict): Output of :func:`compute_shape_signature`.
        manifest (list[dict]): ``[{name, signature}]`` entries (see :func:`build_manifest`).

    Returns:
        dict: ``{"verdict": "new" | "likely-covered", "closest": <name|None>, "why": <str>}``.
    """
    target = _novelty_axes(signature)
    for entry in manifest:
        if _novelty_axes(entry.get("signature", {})) == target:
            name = entry.get("name")
            return {
                "verdict": "likely-covered",
                "closest": name,
                "why": f"shares OS-family + VC/LAG/sub-interface axes with '{name}'",
            }
    return {
        "verdict": "new",
        "closest": None,
        "why": "no covered shape shares its OS-family + VC/LAG/sub-interface axes",
    }
