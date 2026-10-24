"""Repair inventory containment that LibreNMS reports with a wrong or missing parent row."""

from collections import Counter

from netbox_librenms_plugin.constants import JUNOS_FPC_LOCATOR_RE, JUNOS_PIC_LOCATOR_RE, JUNOS_PORT_LOCATOR_RE

# Row keys the repair sets; the modules table reads them.
DANGLING_PARENT_KEY = "_dangling_parent"
REPORTED_PARENT_KEY = "_reported_parent"


def _locator(pattern, row):
    descr = row.get("entPhysicalDescr")
    return pattern.fullmatch(descr.strip()) if isinstance(descr, str) else None


def _pics_by_slot(rows, by_index, unique):
    """Return ``{(fpc, pic): [PIC rows]}`` for the PIC rows held by an FPC container of the same slot."""
    pics = {}
    for row in rows:
        if not unique(row.get("entPhysicalIndex")) or row.get("entPhysicalClass") != "module":
            continue
        if (pic := _locator(JUNOS_PIC_LOCATOR_RE, row)) is None:
            continue
        holder_index = row.get("entPhysicalContainedIn")
        holder = by_index.get(holder_index) if unique(holder_index) else None
        if holder is not None and _is_fpc_of_slot(holder, int(pic["fpc"])):
            pics.setdefault((int(pic["fpc"]), int(pic["pic"])), []).append(row)
    return pics


def _is_fpc_of_slot(row, slot):
    fpc = _locator(JUNOS_FPC_LOCATOR_RE, row) if row.get("entPhysicalClass") == "container" else None
    return fpc is not None and int(fpc["fpc"]) == slot


def _ancestry(start, by_index):
    """Yield *start* and then each row above it; stop at a missing row or a repeat."""
    visited = set()
    index = start.get("entPhysicalIndex")
    while index in by_index and index not in visited:
        visited.add(index)
        yield by_index[index]
        index = by_index[index].get("entPhysicalContainedIn")


def _reaches(start, target_index, by_index):
    """Return whether the ancestry of *start*, *start* included, reaches the row *target_index*."""
    return any(row.get("entPhysicalIndex") == target_index for row in _ancestry(start, by_index))


def _repair_pic_parents(rows, by_index, unique):
    """Move each PIC row whose ancestry has no FPC row of its slot below the one FPC row of that slot."""
    for row in rows:
        if not unique(row.get("entPhysicalIndex")) or row.get("entPhysicalClass") != "module":
            continue
        if (pic := _locator(JUNOS_PIC_LOCATOR_RE, row)) is None:
            continue
        slot = int(pic["fpc"])
        if any(_is_fpc_of_slot(ancestor, slot) for ancestor in _ancestry(row, by_index)):
            continue
        candidates = [other for other in rows if _is_fpc_of_slot(other, slot)]
        if len(candidates) != 1 or not unique(candidates[0].get("entPhysicalIndex")):
            continue
        if _reaches(candidates[0], row["entPhysicalIndex"], by_index):
            continue
        row[REPORTED_PARENT_KEY] = row.get("entPhysicalContainedIn")
        row["entPhysicalContainedIn"] = candidates[0]["entPhysicalIndex"]


def repair_dangling_parents(inventory):
    """
    Give a Junos PIC row its FPC row, and a Juniper MX optic whose parent row is missing its PIC.

    Junos EVO reports a PIC ``module`` row ``PIC: ... @ F/P/*`` with the slot number as parent. A PIC
    row whose ancestry reaches no container ``FPC: ... @ F/*/*`` of the same slot F moves below the
    one such FPC row of the same list, unless that FPC row is below the PIC. The PIC keeps the
    reported value in ``_reported_parent`` and gets no ``_dangling_parent``. Then the optic repair runs.

    LibreNMS numbers some MX optic parents from a base that no PIC row uses. A ``port`` row whose
    non-zero ``entPhysicalContainedIn`` names no row of the same list, and whose description ends in
    ``@ F/P/N``, moves below the one ``module`` row ``PIC: ... @ F/P/*`` held by a container
    ``FPC: ... @ F/*/*``. The row keeps the reported value in ``_reported_parent``. A row with no
    single candidate, whose repair would close a cycle, or whose optic, PIC or FPC index is not a
    unique positive index, keeps its parent and gets ``_dangling_parent``.

    Args:
        inventory (list[dict]): The rows of one source inventory list, changed in place.

    """
    # Index 0 is the parent value of a root row, so a row with no index or index 0 is no parent.
    by_index = {row["entPhysicalIndex"]: row for row in inventory if row.get("entPhysicalIndex")}
    counts = Counter(row.get("entPhysicalIndex") for row in inventory)

    def unique(index):
        # The readers key rows by index, so a shared index can resolve to another row.
        return bool(index) and counts[index] == 1

    _repair_pic_parents(inventory, by_index, unique)
    pics = _pics_by_slot(inventory, by_index, unique)
    for row in inventory:
        reported = row.get("entPhysicalContainedIn")
        if row.get("entPhysicalClass") != "port" or not reported or reported in by_index:
            continue
        if not unique(row.get("entPhysicalIndex")):
            row[DANGLING_PARENT_KEY] = reported
            continue
        port = _locator(JUNOS_PORT_LOCATOR_RE, row)
        candidates = pics.get((int(port["fpc"]), int(port["pic"])), []) if port else []
        if len(candidates) == 1 and not _reaches(candidates[0], row.get("entPhysicalIndex"), by_index):
            row["entPhysicalContainedIn"] = candidates[0]["entPhysicalIndex"]
            row[REPORTED_PARENT_KEY] = reported
        else:
            row[DANGLING_PARENT_KEY] = reported
