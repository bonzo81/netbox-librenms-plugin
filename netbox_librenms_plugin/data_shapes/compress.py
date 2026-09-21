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
  novelty signature reads (OS-agnostic: ifType, sub-interface naming, LAG type, VLAN presence, VRF).
* **Referential integrity** — the IP and neighbour-link rows are trimmed to the ports that survive,
  so the recording never points at a port it no longer holds.

The fingerprint must track whatever axes :func:`compute_shape_signature` reads from ports; widen it in
lockstep if that signature grows.
"""

import re

from netbox_librenms_plugin.constants import INTERFACE_NAME_FIELDS
from netbox_librenms_plugin.data_shapes.envelope import unwrap_response, wrap_response
from netbox_librenms_plugin.data_shapes.ports import (
    compile_lag_patterns,
    port_has_vlan,
    port_has_vrf,
    port_is_lag,
    port_names,
)

_SUB_RE = re.compile(r"\.\d+$")


def _route_key(recording, suffix):
    """Return the first response key whose path (query stripped) ends with *suffix*, or None."""
    for key in recording.get("responses", {}):
        if key.split("?", 1)[0].endswith(suffix):
            return key
    return None


def _unwrap(value):
    """Return the body from a recording response value."""
    _status, body = unwrap_response(value)
    return body


def _rewrap(original, new_body):
    """Re-apply the original response's status framing (if any) around *new_body*."""
    status, _body = unwrap_response(original)
    return wrap_response(status, new_body)


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

    Args:
        recording (dict): A recording that can contain transceiver responses.

    Returns:
        set[str]: Port ids referenced by captured transceivers, excluding 0 and ``"0"``.

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


def _build_name_indexes(dict_ports):
    """
    Index ports by each interface-name field without merging their ambiguity namespaces.

    Keep every candidate for a name. Compression must retain all candidates when a sub-interface
    base is ambiguous, or removing one candidate makes the compressed resolver invent an edge.

    Args:
        dict_ports (list[dict]): Ports to index.

    Returns:
        dict[str, dict[str, list[dict]]]: Candidate ports keyed first by field, then name.

    """
    indexes = {field: {} for field in INTERFACE_NAME_FIELDS}
    for field, by_name in indexes.items():
        for port in dict_ports:
            name = port.get(field)
            if not isinstance(name, str) or not name:
                continue
            candidates = by_name.setdefault(name, [])
            if all(str(candidate.get("port_id")) != str(port.get("port_id")) for candidate in candidates):
                candidates.append(port)
    return indexes


def _add_name_relationship_ports(dict_ports, name_indexes, keep_ids):
    """
    Keep every child and base candidate that can affect a name-derived relationship.

    The resolver indexes ``ifName`` and ``ifDescr`` separately, drops ambiguous names, and derives
    an edge for every ``base.N`` child. Retaining all children and every same-field base candidate
    preserves both real edges and deliberate ambiguity.

    Args:
        dict_ports (list[dict]): Ports to scan for kept sub-unit names.
        name_indexes (dict): Per-field candidate indexes from :func:`_build_name_indexes`.
        keep_ids (set[str]): Port ids to extend.

    """
    for port in dict_ports:
        for field, by_name in name_indexes.items():
            name = port.get(field)
            if not isinstance(name, str):
                continue
            base, separator, suffix = name.rpartition(".")
            if not separator or not suffix.isdigit():
                continue
            candidates = by_name.get(base, [])
            if not candidates:
                continue
            keep_ids.add(str(port.get("port_id")))
            keep_ids.update(str(candidate.get("port_id")) for candidate in candidates)


def _fingerprint(port, compiled_lag_patterns=()):
    """Reduce a port to the shape axes the outcome tests/signature distinguish (for dedup)."""
    return (
        port.get("ifType"),
        # Sub-interface naming style (drives signature sub_interfaces). Scan BOTH name fields via
        # port_names, not just ifName: an ifDescr-mode capture's ".N" sub-unit name can live in
        # ifDescr, and the retention path already keys on port_names — a fingerprint that read only
        # ifName could fold such a sub-interface into a non-subinterface representative.
        any(_SUB_RE.search(name) for name in port_names(port)),
        # VLAN axis — use the signature's own value-based predicate (not key presence) so a
        # no-VLAN port (ifVlan None/0) and a real-VLAN port get distinct fingerprints and the
        # representative kept can't flip compute_shape_signature's vlans axis.
        port_has_vlan(port),
        # LAG axis — the signature detects an aggregate by NAME pattern too, not only by the
        # ieee8023adLag ifType the first axis already carries. Without this a pattern-matched LAG
        # (Cisco "Po1", carried as propVirtual) shares a fingerprint with any other propVirtual
        # port, so compression can drop the only LAG port and flip the signature's lag axis.
        port_is_lag(port, compiled_lag_patterns),
        # VRF axis — without it compression can drop every VRF-tagged port and leave a VRF list
        # nothing references. Boolean, not the id: a router carries hundreds of VRFs, and one
        # representative per id would keep hundreds of ports and defeat compression entirely. The
        # VRF list is trimmed to what the surviving ports reference instead (see _prune_vrf_rows).
        port_has_vrf(port),
    )


# Routes whose rows name one of the device's OWN ports, and the field that names it. Compression
# drops ports, so a row left behind would point at a port the recording no longer holds — a
# dangling reference the IP and cables tabs would render as an unresolvable row. (A link's
# ``remote_port_id`` belongs to the far device and is deliberately not filtered.)
_PORT_REFERENCING_ROUTES = (("/ip", "addresses", "port_id"), ("/links", "links", "local_port_id"))


def _prune_vrf_rows(recording, responses, kept_ports):
    """Trim the VRF list to the VRFs the surviving ports still reference."""
    key = _route_key(recording, "/routing/vrf")
    if key is None:
        return
    body = _unwrap(responses[key])
    rows = body.get("vrfs") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return
    referenced = {str(port.get("ifVrf")) for port in kept_ports if isinstance(port, dict) and port_has_vrf(port)}
    kept_rows = [row for row in rows if not isinstance(row, dict) or str(row.get("vrf_id")) in referenced]
    # Compression must never change the novelty signature, and the vrf facet is "the list is
    # non-empty". A device whose ports name a vrf_id the table does not list would otherwise flip
    # it, so an empty result means leave the list alone.
    if not kept_rows or len(kept_rows) == len(rows):
        return
    new_body = dict(body)
    new_body["vrfs"] = kept_rows
    if "count" in new_body:
        new_body["count"] = len(kept_rows)
    responses[key] = _rewrap(responses[key], new_body)


def _prune_port_references(recording, responses, keep_ids):
    """Drop /ip and /links rows whose local port was compressed away."""
    for suffix, row_field, id_field in _PORT_REFERENCING_ROUTES:
        key = _route_key(recording, suffix)
        if key is None:
            continue
        body = _unwrap(responses[key])
        rows = body.get(row_field) if isinstance(body, dict) else None
        if not isinstance(rows, list):
            continue
        kept_rows = [row for row in rows if not isinstance(row, dict) or str(row.get(id_field)) in keep_ids]
        if len(kept_rows) == len(rows):
            continue
        new_body = dict(body)
        new_body[row_field] = kept_rows
        if "count" in new_body:
            new_body["count"] = len(kept_rows)
        responses[key] = _rewrap(responses[key], new_body)


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
    name_indexes = _build_name_indexes(dict_ports)

    # Always keep the ports whose relationships we must preserve: those named by port_stack, plus the
    # base-level ports their ``.N`` names resolve to.
    referenced = _port_stack_referenced(recording) | _transceiver_referenced(recording)
    keep_ids = {str(p.get("port_id")) for p in dict_ports if str(p.get("port_id")) in referenced}
    _add_name_relationship_ports(dict_ports, name_indexes, keep_ids)

    # One representative per distinct fingerprint preserves every shape the signature reads while
    # collapsing redundant cardinality. Iterate in original order so the first ieee8023adLag port
    # (the signature's LAG name_prefix source) is the same one the full recording would pick.
    compiled_lag_patterns = compile_lag_patterns(recording)
    seen_fingerprints = set()
    for p in dict_ports:
        fp = _fingerprint(p, compiled_lag_patterns)
        if fp not in seen_fingerprints:
            seen_fingerprints.add(fp)
            keep_ids.add(str(p.get("port_id")))
    kept = [p for p in ports if isinstance(p, dict) and str(p.get("port_id")) in keep_ids]
    if len(kept) == len(dict_ports):
        return recording  # nothing dropped — leave the recording (and its meta) untouched

    new_body = dict(ports_body)
    new_body["ports"] = kept
    # LibreNMS sends "count" alongside "ports"; leaving the original would describe a port set
    # the recording no longer holds, and a reader that trusts it would look for missing rows.
    if "count" in new_body:
        new_body["count"] = len(kept)
    new_responses = dict(recording["responses"])
    new_responses[ports_key] = _rewrap(recording["responses"][ports_key], new_body)
    _prune_port_references(recording, new_responses, {str(p.get("port_id")) for p in kept})
    _prune_vrf_rows(recording, new_responses, kept)

    out = dict(recording)
    out["responses"] = new_responses
    out["meta"] = {**(recording.get("meta") or {}), "compressed_ports": {"from": len(dict_ports), "to": len(kept)}}
    return out
