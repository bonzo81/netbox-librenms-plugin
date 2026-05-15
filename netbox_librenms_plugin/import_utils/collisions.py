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


def _candidate_pks_for_row(validation: dict) -> list[tuple[int, str, str]]:
    """Return [(nb_device_pk, nb_device_name, role)] candidates for a single row.

    ``role`` is a short human-readable label describing how this LibreNMS
    row would touch the NetBox device:

    * ``"host"`` — the device is the existing NetBox device this row would
      update / link to (``existing_device``).
    * ``"oob"`` — this LibreNMS row would be installed as the OOB
      controller of the NetBox device (``oob_candidate.device``).
    * ``"merge_host_named"`` / ``"merge_oob_named"`` — the row would feed
      a Stage-2 merge of two NetBox devices.
    * ``"promote_target"`` — the row should be promoted to host of an
      existing NetBox device that currently only has an OOB link
      (``promote_to_host``).

    Duplicate ``(pk, role)`` tuples are de-duplicated; a single row may
    legitimately surface the same pk under different roles.
    """
    candidates: list[tuple[int, str, str]] = []
    seen: set[tuple[int, str]] = set()

    def _add(pk, name, role):
        try:
            pk_int = int(pk)
        except (TypeError, ValueError):
            return
        key = (pk_int, role)
        if key in seen:
            return
        seen.add(key)
        candidates.append((pk_int, str(name or f"device-{pk_int}"), role))

    existing = validation.get("existing_device")
    if existing is not None and getattr(existing, "pk", None) is not None:
        _add(existing.pk, getattr(existing, "name", None), "host")

    oob_candidate = validation.get("oob_candidate") or {}
    oob_device = oob_candidate.get("device") if isinstance(oob_candidate, dict) else None
    if oob_device is not None and getattr(oob_device, "pk", None) is not None:
        _add(oob_device.pk, getattr(oob_device, "name", None), "oob")

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
            _add(target.pk, getattr(target, "name", None), "promote_target")
        target_pk = promote.get("existing_device_pk")
        if target_pk is not None:
            _add(target_pk, promote.get("existing_device_name"), "promote_target")

    return candidates


def detect_bulk_collisions(devices: list[dict]) -> list[dict]:
    """Find groups of LibreNMS rows in *devices* that resolve to the same NetBox device.

    *devices* matches the list assembled by ``BulkImportConfirmView`` —
    each item is a dict with at least ``device_id``, ``device_name`` and
    ``validation`` keys.

    Returns a list of collision groups (one per offending NetBox pk),
    sorted by ``nb_device_pk`` for stable rendering. Each group:

    .. code-block:: python

        {
            "nb_device_pk": int,
            "nb_device_name": str,
            "librenms_rows": [
                {"device_id": int, "hostname": str, "role": str},
                ...
            ],
        }

    Rows are de-duplicated by ``device_id`` within a group (same LibreNMS
    row touching the same NetBox device under multiple roles only appears
    once, with all matching role labels joined by ``", "``).

    A group is only emitted when at least two distinct LibreNMS
    ``device_id`` values target the same NetBox pk.
    """
    by_nb_pk: dict[int, dict] = {}

    for entry in devices or []:
        validation = entry.get("validation") or {}
        try:
            libre_id = int(entry.get("device_id"))
        except (TypeError, ValueError):
            continue
        hostname = entry.get("device_name") or f"device-{libre_id}"

        for nb_pk, nb_name, role in _candidate_pks_for_row(validation):
            bucket = by_nb_pk.setdefault(
                nb_pk,
                {"nb_device_pk": nb_pk, "nb_device_name": nb_name, "_rows": {}},
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
    for nb_pk in sorted(by_nb_pk.keys()):
        bucket = by_nb_pk[nb_pk]
        rows = list(bucket["_rows"].values())
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r["device_id"])
        collisions.append(
            {
                "nb_device_pk": bucket["nb_device_pk"],
                "nb_device_name": bucket["nb_device_name"],
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
