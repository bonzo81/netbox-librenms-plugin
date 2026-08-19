"""
Bulk-import collision detection.

When a user selects multiple LibreNMS devices for bulk import, two or more
rows in the same batch may resolve to the *same* NetBox device — for
example, one row would be linked as the host and another as the OOB
controller, or two rows both want to promote to the same existing host.
Importing all of them blindly would race for the same custom-field slot
and produce inconsistent state.

`detect_bulk_collisions` walks the per-row validation results and groups
rows that target the same NetBox device pk so the bulk-confirm view can
block the import and let the user adjust their selection.
"""

from __future__ import annotations

from netbox_librenms_plugin.import_validation_helpers import MERGE_CANDIDATE_SLOTS

# Terminal match types where ``existing_device`` is an ARBITRARY duplicate the validator already
# failed closed (can_import=False, actions cleared). Such a row will never write, so it must not
# contend for a NetBox pk and block an otherwise-valid sibling import on that same pk.
_TERMINAL_AMBIGUOUS_MATCH_TYPES = frozenset({"ambiguous_hostname_or_serial", "ambiguous_librenms_id"})

# Maps each merge_candidates slot to the role label used in collision groups.
_MERGE_SLOT_ROLES = {"host_named": "merge_host_named", "oob_named": "merge_oob_named"}


def scope_bulk_collisions(collisions: list[dict], user) -> list[dict]:
    """Redact collision targets the requesting user cannot view."""
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    models = {"device": Device, "virtualmachine": VirtualMachine}
    visible_pks: dict[str, set[int]] = {}
    for model_name, model in models.items():
        candidate_pks = {
            group.get("nb_device_pk")
            for group in collisions
            if group.get("nb_model_name") == model_name and group.get("nb_device_pk") is not None
        }
        visible_pks[model_name] = set(
            model.objects.restrict(user, "view").filter(pk__in=candidate_pks).values_list("pk", flat=True)
        )

    scoped = []
    for group in collisions:
        model_name = group.get("nb_model_name")
        if group.get("nb_device_pk") in visible_pks.get(model_name, set()):
            scoped.append({**group, "target_visible": True})
            continue
        scoped.append(
            {
                **group,
                "nb_device_pk": None,
                "nb_device_name": "restricted NetBox object",
                "nb_model_name": None,
                "nb_kind": "object",
                "target_visible": False,
            }
        )
    return scoped


def _model_name_of(obj) -> str:
    """
    Return the NetBox model name (``"device"`` / ``"virtualmachine"``) for *obj*.

    Prefers Django's lowercase ``_meta.model_name`` to match the convention used across
    the codebase (so a copied ``== "virtualmachine"`` check stays correct); falls back to
    the lowercased class name for lightweight test stubs that have no ``_meta``.

    Args:
        obj: A NetBox object (Device / VirtualMachine) or a test stand-in.

    Returns:
        str: The lowercase model name.
    """
    meta = getattr(obj, "_meta", None)
    model_name = getattr(meta, "model_name", None)
    return model_name if model_name else type(obj).__name__.lower()


def _candidate_pks_for_row(validation: dict) -> list[tuple[int, str | None, str, str]]:
    """
    Return the NetBox-device candidates a single LibreNMS row would touch.

    ``role`` is a short human-readable label describing how this LibreNMS row would
    touch the NetBox device:

    * ``"host"`` — the device is the existing NetBox device this row would update /
      link to (``existing_device``).
    * ``"oob"`` — this LibreNMS row would be installed as the OOB controller of the
      NetBox device (``oob_candidate.device``).
    * ``"merge_host_named"`` / ``"merge_oob_named"`` — the row would feed a Stage-2
      merge of two NetBox devices.
    * ``"promote_target"`` — the row should be promoted to host of an existing NetBox
      device that currently only has an OOB link (``promote_to_host``).

    ``model_name`` is the NetBox model name (``"device"`` / ``"virtualmachine"``) so that
    two objects of different types that happen to share the same pk are not grouped as
    collisions. Merge-candidate slots carry their own ``model_name`` (defaulting to
    ``"device"`` — Stage-2 merge is Device-only today).

    Args:
        validation (dict): The per-row validation result to inspect.

    Returns:
        list[tuple[int, str | None, str, str]]: ``(nb_device_pk, nb_device_name, role,
            model_name)`` candidates. Each of the (at most five) sources emits a distinct
            role, so no intra-row de-duplication is needed.
    """
    from netbox_librenms_plugin.utils import coerce_positive_int

    candidates: list[tuple[int, str | None, str, str]] = []

    def _add(pk, name, role, model_name="device"):
        pk_int = coerce_positive_int(pk)
        if pk_int is None:
            return
        # model_name keys the collision bucket downstream; a non-string (e.g. a corrupt/foreign
        # merge_candidates entry) would mis-group or raise on the dict key. Normalize to a
        # lowercase string, defaulting to "device".
        if isinstance(model_name, str):
            normalized_model = model_name.strip().lower() or "device"
        else:
            normalized_model = "device"
        candidates.append((pk_int, name, role, normalized_model))

    existing = validation.get("existing_device")
    existing_match_type = validation.get("existing_match_type")
    if (
        existing is not None
        and getattr(existing, "pk", None) is not None
        and not (isinstance(existing_match_type, str) and existing_match_type in _TERMINAL_AMBIGUOUS_MATCH_TYPES)
    ):
        _add(existing.pk, getattr(existing, "name", None), "host", _model_name_of(existing))

    oob_candidate = validation.get("oob_candidate") or {}
    oob_device = oob_candidate.get("device") if isinstance(oob_candidate, dict) else None
    if oob_device is not None and getattr(oob_device, "pk", None) is not None:
        _add(oob_device.pk, getattr(oob_device, "name", None), "oob", _model_name_of(oob_device))

    merge = validation.get("merge_candidates") or {}
    if isinstance(merge, dict):
        for slot in MERGE_CANDIDATE_SLOTS:
            entry = merge.get(slot) or {}
            if not isinstance(entry, dict):
                continue
            # Derive a fallback role rather than indexing: if MERGE_CANDIDATE_SLOTS is ever
            # extended without updating _MERGE_SLOT_ROLES, a new slot degrades to a generated
            # label instead of crashing the import gate with a KeyError.
            role = _MERGE_SLOT_ROLES.get(slot, f"merge_{slot}")
            if entry.get("pk") is not None:
                _add(entry.get("pk"), entry.get("name"), role, entry.get("model_name") or "device")

    promote = validation.get("promote_to_host") or {}
    if isinstance(promote, dict):
        target = promote.get("existing_device")
        if target is not None and getattr(target, "pk", None) is not None:
            _add(target.pk, getattr(target, "name", None), "promote_target", _model_name_of(target))

    return candidates


def detect_bulk_collisions(devices: list[dict] | None) -> list[dict]:
    """
    Find groups of LibreNMS rows that resolve to the same NetBox device.

    A group is only emitted when at least two distinct LibreNMS ``device_id`` values
    target the same NetBox pk. Rows are de-duplicated by ``device_id`` within a group
    (same LibreNMS row touching the same NetBox device under multiple roles appears
    once, with all matching role labels joined by ``", "``).

    Args:
        devices (list[dict] | None): The list assembled by ``BulkImportConfirmView`` —
            each item a dict with at least ``device_id``, ``device_name`` and
            ``validation`` keys. None is accepted and treated as an empty list.

    Returns:
        list[dict]: Collision groups (one per offending NetBox device), sorted by
            model type then ``nb_device_pk`` for stable rendering. Each group has the
            shape::

                {
                    "nb_device_pk": int,
                    "nb_device_name": str,
                    "nb_model_name": str,  # "device" | "virtualmachine"
                    "librenms_rows": [
                        {"device_id": int, "hostname": str, "role": str},
                        ...
                    ],
                }
    """
    from netbox_librenms_plugin.utils import coerce_librenms_id

    # Key by (model_name, nb_pk) to avoid false collisions when a Device
    # and a VirtualMachine happen to share the same integer pk.
    by_nb_pk: dict[tuple[str, int], dict] = {}

    for entry in devices or []:
        # A single malformed row (non-dict entry, or a non-dict validation payload)
        # must be skipped, not crash the whole bulk-confirm flow on .get().
        if not isinstance(entry, dict):
            continue
        validation = entry.get("validation")
        if not isinstance(validation, dict):
            validation = {}
        # coerce_librenms_id rejects bools, floats (1.9 would otherwise truncate to a valid-looking
        # but WRONG pk), non-positive and non-numeric ids, and coerces a digit string — so a
        # malformed row is skipped rather than silently mis-keyed under a truncated device id.
        libre_id = coerce_librenms_id(entry.get("device_id"))
        if libre_id is None:
            continue
        hostname = entry.get("device_name") or f"device-{libre_id}"

        for nb_pk, nb_name, role, model_name in _candidate_pks_for_row(validation):
            bucket_key = (model_name, nb_pk)
            bucket = by_nb_pk.setdefault(
                bucket_key,
                {"nb_device_pk": nb_pk, "nb_device_name": nb_name, "nb_model_name": model_name, "_rows": {}},
            )
            # Keep the first non-empty name we see — rows often disagree on (or omit) the cached
            # display string, but the underlying pk is the source of truth. The synthetic
            # "device-<pk>" fallback is applied only at output, so a device legitimately named
            # "device-*" is never mistaken for a placeholder.
            if not bucket["nb_device_name"] and nb_name:
                bucket["nb_device_name"] = nb_name

            row = bucket["_rows"].setdefault(
                libre_id,
                {"device_id": libre_id, "hostname": hostname, "roles": []},
            )
            if role not in row["roles"]:
                row["roles"].append(role)

    collisions: list[dict] = []
    for _model_name, nb_pk in sorted(by_nb_pk.keys()):
        bucket = by_nb_pk[(_model_name, nb_pk)]
        rows = list(bucket["_rows"].values())
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r["device_id"])
        collisions.append(
            {
                "nb_device_pk": bucket["nb_device_pk"],
                # Synthesize the "device-<pk>" placeholder only here, when no row supplied a real
                # name — so a device actually named "device-*" keeps its real name above.
                "nb_device_name": bucket["nb_device_name"] or f"device-{bucket['nb_device_pk']}",
                # Model name ("device"/"virtualmachine") so the template links to the right object
                # type — a VM collision must not render a dcim:device URL.
                "nb_model_name": bucket["nb_model_name"],
                # Display label computed once so the template doesn't repeat the ternary.
                "nb_kind": "VM" if bucket["nb_model_name"] == "virtualmachine" else "device",
                "librenms_rows": [
                    {
                        "device_id": r["device_id"],
                        "hostname": r["hostname"],
                        "role": ", ".join(r["roles"]),
                    }
                    for r in rows
                ],
            }
        )
    return collisions
