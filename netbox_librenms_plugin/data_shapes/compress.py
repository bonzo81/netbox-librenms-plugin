"""
Shrink a data-shape recording's port list to the minimum that preserves its testable outcome.

A real capture (a Nokia timos router, a big chassis switch) can carry thousands of ports, but most
of that is high-cardinality, low-novelty repetition — 48 near-identical access ports add scale, not
a new shape, and nothing the outcome tests read distinguishes them. This collapses such repetition
to one representative per distinct port *fingerprint*, while keeping every port that the LAG /
sub-interface relationship resolution actually needs. The result drives
:func:`~netbox_librenms_plugin.librenms_api.LibreNMSAPI.resolve_port_relationships` and
:func:`~netbox_librenms_plugin.data_shapes.signature.compute_shape_signature` to identical output —
only smaller and reviewable.

Two invariants, verified by the tests:

* **Relationship integrity** — every port referenced by ``port_stack`` (and every base-level port its
  name resolution depends on) is kept, so ``resolve_port_relationships`` yields the same LAG/sub maps.
* **Signature preservation** — one representative per distinct fingerprint keeps every shape the
  novelty signature reads (OS-agnostic: ifType, sub-interface naming, LAG type, VLAN presence).

The fingerprint must track whatever axes :func:`compute_shape_signature` reads from ports; widen it in
lockstep if that signature grows.
"""

import re

from netbox_librenms_plugin.data_shapes.signature import port_has_vlan

_SUB_RE = re.compile(r"\.\d+$")


def _route_key(recording, suffix):
    """Return the first response key whose path (query stripped) ends with *suffix*, or None."""
    for key in recording.get("responses", {}):
        if key.split("?", 1)[0].endswith(suffix):
            return key
    return None


def _unwrap(value):
    """Return the body from a recording response value, unwrapping a ``[status, body]`` pair."""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        return value[1]
    return value


def _rewrap(original, new_body):
    """Re-apply the original ``[status, body]`` framing (if any) around *new_body*."""
    if isinstance(original, list) and len(original) == 2 and isinstance(original[0], int):
        return [original[0], new_body]
    return new_body


def _port_stack_referenced(recording):
    """Return the set of str port ids named by any ``port_stack`` mapping (0/"0" sentinel excluded)."""
    ps_key = _route_key(recording, "/port_stack")
    referenced = set()
    if ps_key is None:
        return referenced
    ps_body = _unwrap(recording["responses"][ps_key])
    mappings = ps_body.get("mappings") if isinstance(ps_body, dict) else None
    for entry in mappings or []:
        if not isinstance(entry, dict):
            continue
        for side in ("high_port_id", "low_port_id"):
            val = entry.get(side)
            if val not in (None, 0, "0"):
                referenced.add(str(val))
    return referenced


def _transceiver_referenced(recording):
    """
    Return the set of str port ids referenced by any captured transceiver (0/"0" excluded).

    Transceivers point at ports by ``port_id``; if compression drops those ports the replay is no
    longer a self-consistent LibreNMS dataset (the transceiver-merge can't map them to a port name),
    so they must be kept alongside the port_stack-referenced ones.
    """
    tx_key = _route_key(recording, "/transceivers")
    referenced = set()
    if tx_key is None:
        return referenced
    tx_body = _unwrap(recording["responses"][tx_key])
    entries = tx_body.get("transceivers") if isinstance(tx_body, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        port_id = entry.get("port_id")
        if port_id not in (None, 0, "0"):
            referenced.add(str(port_id))
    return referenced


def _port_names(port):
    """
    Return the non-empty string names a port is known by (ifName + ifDescr).

    Mirrors ``resolve_port_relationships._port_names``: that resolver keys its lookup maps on every
    known name field, not just ifName, because an ifDescr-mode device's structured ``.N`` sub-unit
    name can live in ifDescr while ifName carries an arbitrary label. Compress must scan the same
    fields or it can drop a base port the resolver needs, breaking the relationship-integrity
    invariant this module promises.
    """
    return [name for field in ("ifName", "ifDescr") if isinstance(name := port.get(field), str) and name]


def _build_name_index(dict_ports):
    """
    Index ports by every name they're known by (ifName + ifDescr), dropping AMBIGUOUS names.

    A name carried by two DIFFERENT ports (distinct port_ids) can't disambiguate which port a
    base/sub-unit lookup means, so it's dropped entirely — mirroring the ambiguous-name drop in
    ``resolve_port_relationships`` (whose old last-write-wins ``by_name`` would bind the wrong
    aggregate). Keying the same way here keeps compress's base-port retention consistent with what
    the resolver actually resolves.
    """
    by_name: dict = {}
    ambiguous_names: set = set()
    for p in dict_ports:
        for name in _port_names(p):
            if name in ambiguous_names:
                continue
            existing = by_name.get(name)
            if existing is not None and str(existing.get("port_id")) != str(p.get("port_id")):
                del by_name[name]
                ambiguous_names.add(name)
                continue
            by_name[name] = p
    return by_name


def _add_base_name_ports(dict_ports, by_name, keep_ids):
    """
    Grow *keep_ids* to include base-level ports that kept ``parent.N`` names resolve to (fixpoint).

    resolve_port_relationships' _resolve_physical strips a ``.N`` suffix and looks the base up by
    name; dropping that base would change the resolved LAG/sub maps, so keep it too. Scan every
    known name (ifName + ifDescr), not just ifName, so an ifDescr-mode device whose ``.N`` marker
    lives in ifDescr still has its base port preserved.
    """
    changed = True
    while changed:
        changed = False
        for p in dict_ports:
            if str(p.get("port_id")) not in keep_ids:
                continue
            for name in _port_names(p):
                if "." not in name:
                    continue
                base = by_name.get(name.rsplit(".", 1)[0])
                if base is not None and str(base.get("port_id")) not in keep_ids:
                    keep_ids.add(str(base.get("port_id")))
                    changed = True


def _fingerprint(port):
    """Reduce a port to the shape axes the outcome tests/signature distinguish (for dedup)."""
    name = port.get("ifName")
    name = name if isinstance(name, str) else ""
    return (
        port.get("ifType"),
        bool(_SUB_RE.search(name)),  # sub-interface naming style (drives signature sub_interfaces)
        # VLAN axis — use the signature's own value-based predicate (not key presence) so a
        # no-VLAN port (ifVlan None/0) and a real-VLAN port get distinct fingerprints and the
        # representative kept can't flip compute_shape_signature's vlans axis.
        port_has_vlan(port),
    )


def compress_recording(recording):
    """
    Return a copy of *recording* with its ports list trimmed to a minimal representative set.

    Ports referenced by ``port_stack`` (and the base-level ports their ``.N`` names resolve to) are
    always kept, so the LAG/sub-interface relationship maps are unchanged. From the remaining ports,
    one representative per distinct :func:`_fingerprint` is kept, so the novelty signature is
    unchanged while redundant high-cardinality repetition is dropped. Recordings without a ports
    response are returned unchanged.

    Args:
        recording (dict): A recording (raw or anonymized — only structural fields are read).

    Returns:
        dict: A new recording. When ports were trimmed, ``meta["compressed_ports"]`` records
            ``{"from": <original count>, "to": <kept count>}``; the input is never mutated.
    """
    # Target the MAIN device's ports route. A recording may carry a second /ports route for a linked
    # OOB controller (devices/<oob_id>/ports); compress only the host's ports (the OOB controller's
    # are a separate device, left intact for the merge), falling back to any /ports route otherwise.
    ports_key = _route_key(recording, f"devices/{recording.get('device_id')}/ports") or _route_key(recording, "/ports")
    if ports_key is None:
        return recording
    ports_body = _unwrap(recording["responses"][ports_key])
    if not (isinstance(ports_body, dict) and isinstance(ports_body.get("ports"), list)):
        return recording

    ports = ports_body["ports"]
    dict_ports = [p for p in ports if isinstance(p, dict)]
    by_name = _build_name_index(dict_ports)

    # Always keep the ports whose relationships we must preserve: those named by port_stack, plus the
    # base-level ports their ``.N`` names resolve to.
    referenced = _port_stack_referenced(recording) | _transceiver_referenced(recording)
    keep_ids = {str(p.get("port_id")) for p in dict_ports if str(p.get("port_id")) in referenced}
    _add_base_name_ports(dict_ports, by_name, keep_ids)

    # One representative per distinct fingerprint preserves every shape the signature reads while
    # collapsing redundant cardinality. Iterate in original order so the first ieee8023adLag port
    # (the signature's LAG name_prefix source) is the same one the full recording would pick.
    seen_fingerprints = set()
    for p in dict_ports:
        fp = _fingerprint(p)
        if fp not in seen_fingerprints:
            seen_fingerprints.add(fp)
            keep_ids.add(str(p.get("port_id")))

    kept = [p for p in ports if isinstance(p, dict) and str(p.get("port_id")) in keep_ids]
    if len(kept) == len(dict_ports):
        return recording  # nothing dropped — leave the recording (and its meta) untouched

    new_body = dict(ports_body)
    new_body["ports"] = kept
    new_responses = dict(recording["responses"])
    new_responses[ports_key] = _rewrap(recording["responses"][ports_key], new_body)

    out = dict(recording)
    out["responses"] = new_responses
    out["meta"] = {**(recording.get("meta") or {}), "compressed_ports": {"from": len(dict_ports), "to": len(kept)}}
    return out
