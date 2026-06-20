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


def _candidate_pks_for_row(validation: dict) -> list[tuple[int, str, str, str]]:
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

    ``model_name`` is the Python class name of the NetBox object (e.g. ``"Device"``,
    ``"VirtualMachine"``) so that two objects of different types that happen to share
    the same pk are not grouped as collisions.

    Args:
        validation (dict): The per-row validation result to inspect.

    Returns:
        list[tuple[int, str, str, str]]: ``(nb_device_pk, nb_device_name, role,
            model_name)`` candidates, de-duplicated on ``(pk, role, model_name)`` (a
            single row may legitimately surface the same pk under different roles).
    """
    candidates: list[tuple[int, str, str, str]] = []
    seen: set[tuple[int, str, str]] = set()

    def _add(pk, name, role, model_name="Device"):
        try:
            pk_int = int(pk)
        except (TypeError, ValueError):
            return
        key = (pk_int, role, model_name)
        if key in seen:
            return
        seen.add(key)
        candidates.append((pk_int, str(name or f"device-{pk_int}"), role, model_name))

    existing = validation.get("existing_device")
    if existing is not None and getattr(existing, "pk", None) is not None:
        _add(existing.pk, getattr(existing, "name", None), "host", type(existing).__name__)

    oob_candidate = validation.get("oob_candidate") or {}
    oob_device = oob_candidate.get("device") if isinstance(oob_candidate, dict) else None
    if oob_device is not None and getattr(oob_device, "pk", None) is not None:
        _add(oob_device.pk, getattr(oob_device, "name", None), "oob", type(oob_device).__name__)

    merge = validation.get("merge_candidates") or {}
    if isinstance(merge, dict):
        for slot, role in (("host_named", "merge_host_named"), ("oob_named", "merge_oob_named")):
            entry = merge.get(slot) or {}
            pk = entry.get("pk") if isinstance(entry, dict) else None
            name = entry.get("name") if isinstance(entry, dict) else None
            if pk is not None:
                _add(pk, name, role)

    promote = validation.get("promote_to_host") or {}
    if isinstance(promote, dict):
        target = promote.get("existing_device")
        if target is not None and getattr(target, "pk", None) is not None:
            _add(target.pk, getattr(target, "name", None), "promote_target", type(target).__name__)

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
                    "librenms_rows": [
                        {"device_id": int, "hostname": str, "role": str},
                        ...
                    ],
                }
    """
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
        try:
            libre_id = int(entry.get("device_id"))
        except (TypeError, ValueError):
            continue
        hostname = entry.get("device_name") or f"device-{libre_id}"

        for nb_pk, nb_name, role, model_name in _candidate_pks_for_row(validation):
            bucket_key = (model_name, nb_pk)
            bucket = by_nb_pk.setdefault(
                bucket_key,
                {"nb_device_pk": nb_pk, "nb_device_name": nb_name, "nb_model_name": model_name, "_rows": {}},
            )
            # Keep the first non-default name we see — rows often disagree
            # on the cached display string, but the underlying pk is the
            # source of truth.
            if bucket["nb_device_name"].startswith("device-") and not nb_name.startswith("device-"):
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
                "nb_device_name": bucket["nb_device_name"],
                # Class name ("Device"/"VirtualMachine") so the template links to the right
                # object type — a VM collision must not render a dcim:device URL.
                "nb_model_name": bucket["nb_model_name"],
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
