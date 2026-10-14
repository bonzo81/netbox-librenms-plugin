"""Repair inventory containment that LibreNMS reports with a missing parent row."""

from netbox_librenms_plugin.constants import JUNOS_FPC_LOCATOR_RE, JUNOS_PIC_LOCATOR_RE, JUNOS_PORT_LOCATOR_RE

# Row keys the repair sets; the modules table reads them.
DANGLING_PARENT_KEY = "_dangling_parent"
REPORTED_PARENT_KEY = "_reported_parent"


def _locator(pattern, row):
    descr = row.get("entPhysicalDescr")
    return pattern.fullmatch(descr.strip()) if isinstance(descr, str) else None


def _pics_by_slot(rows, by_index):
    """Return ``{(fpc, pic): [PIC rows]}`` for the PIC rows held by an FPC container of the same slot."""
    pics = {}
    for row in rows:
        if not row.get("entPhysicalIndex") or row.get("entPhysicalClass") != "module":
            continue
        if (pic := _locator(JUNOS_PIC_LOCATOR_RE, row)) is None:
            continue
        holder = by_index.get(row.get("entPhysicalContainedIn"))
        if holder is None or holder.get("entPhysicalClass") != "container":
            continue
        fpc = _locator(JUNOS_FPC_LOCATOR_RE, holder)
        if fpc is not None and int(fpc["fpc"]) == int(pic["fpc"]):
            pics.setdefault((int(pic["fpc"]), int(pic["pic"])), []).append(row)
    return pics


def _reaches(start, target_index, by_index):
    """Return whether the ancestry of *start*, *start* included, reaches the row *target_index*."""
    visited = set()
    index = start.get("entPhysicalIndex")
    while index in by_index and index not in visited:
        if index == target_index:
            return True
        visited.add(index)
        index = by_index[index].get("entPhysicalContainedIn")
    return False


def repair_dangling_parents(inventory):
    """
    Give a Juniper MX optic whose reported parent row is missing the PIC that its description names.

    LibreNMS numbers some MX optic parents from a base that no PIC row uses. A ``port`` row whose
    non-zero ``entPhysicalContainedIn`` names no row of the same list, and whose description ends in
    ``@ F/P/N``, moves below the one ``module`` row ``PIC: ... @ F/P/*`` held by a container
    ``FPC: ... @ F/*/*``. The row keeps the reported value in ``_reported_parent``. A row with no
    single candidate, or whose repair would close a cycle, keeps its parent and gets
    ``_dangling_parent``.

    Args:
        inventory (list[dict]): The rows of one source inventory list, changed in place.

    """
    # Index 0 is the parent value of a root row, so a row with no index or index 0 is no parent.
    by_index = {row["entPhysicalIndex"]: row for row in inventory if row.get("entPhysicalIndex")}
    pics = _pics_by_slot(inventory, by_index)
    for row in inventory:
        reported = row.get("entPhysicalContainedIn")
        if row.get("entPhysicalClass") != "port" or not reported or reported in by_index:
            continue
        port = _locator(JUNOS_PORT_LOCATOR_RE, row)
        candidates = pics.get((int(port["fpc"]), int(port["pic"])), []) if port else []
        if len(candidates) == 1 and not _reaches(candidates[0], row.get("entPhysicalIndex"), by_index):
            row["entPhysicalContainedIn"] = candidates[0]["entPhysicalIndex"]
            row[REPORTED_PARENT_KEY] = reported
        else:
            row[DANGLING_PARENT_KEY] = reported
