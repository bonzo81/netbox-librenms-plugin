"""Virtual chassis detection, creation, and management."""

import logging
from contextlib import nullcontext
from string import Formatter
from typing import List, NamedTuple

from dcim.models import Device, DeviceType, VirtualChassis
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from redis.exceptions import RedisError

from ..constants import (
    JUNOS_FPC_DESCR_PREFIX,
    JUNOS_FPC_MEMBER_DESCR_RE,
    JUNOS_VC_ROOT_DESCR_MARKER,
)
from ..librenms_api import LibreNMSAPI
from ..models import RuleConfigurationError
from ..serial_identity import (
    UNAVAILABLE_MASTER_LINK,
    Comparison,
    MemberSerials,
    ResolutionKind,
    SerialGroups,
    SerialRules,
    assignment_lock,
    compare,
    display,
    group,
    naming_token,
    plain_context,
    resolve_devices_write,
    stored_form,
)
from ..transactions import atomic_with_events
from ..utils import exception_text_for, is_vc_position, librenms_text

logger = logging.getLogger(__name__)


def empty_virtual_chassis_data() -> dict:
    """Public helper for callers that need a blank VC payload."""
    return {
        "is_stack": False,
        "member_count": 0,
        "members": [],
        "detection_failed": False,
        "detection_error": None,
    }


def _clone_virtual_chassis_data(data: dict | None) -> dict:
    """Return a defensive copy of cached VC data to avoid shared references."""
    if not data:
        return empty_virtual_chassis_data()

    members = [member.copy() for member in data.get("members", [])]
    member_count = data.get("member_count") or len(members)

    return {
        "is_stack": bool(data.get("is_stack")),
        "member_count": member_count,
        "members": members,
        "detection_failed": bool(data.get("detection_failed")),
        "detection_error": data.get("detection_error"),
    }


def _failed_virtual_chassis_data(error: str) -> dict:
    """Return a VC payload that distinguishes a failed read from a non-stack device."""
    data = empty_virtual_chassis_data()
    data["detection_failed"] = True
    data["detection_error"] = error
    return data


# v6: each member carries its reported ``inventory_position``.
_VC_CACHE_VERSION = "v6"


def _vc_cache_key(api: LibreNMSAPI, device_id: int | str, serial_rules: SerialRules | None = None) -> str:
    """Return the cache key of one detection; the serial rules decide the result, so a rule edit changes the key."""
    server_key = getattr(api, "server_key", "default")
    digest = (serial_rules or SerialRules.load()).digest[:16]
    return f"librenms_vc_detection_{_VC_CACHE_VERSION}_{server_key}_{device_id}_{digest}"


def get_virtual_chassis_data(
    api: LibreNMSAPI, device_id: int | str, *, force_refresh: bool = False, serial_rules=None
) -> dict:
    """
    Fetch (and cache) virtual chassis data for a LibreNMS device.

    *serial_rules* are the batch's :class:`SerialRules`; None reads them.
    """
    if not api or device_id is None:
        return empty_virtual_chassis_data()
    try:
        return _cached_virtual_chassis_data(api, device_id, force_refresh, serial_rules)
    except RedisError as e:
        # A Redis outage fails only this device, as one does inside detection.
        logger.exception("Virtual chassis cache unavailable for device %s: %s", device_id, e)
        return _failed_virtual_chassis_data(exception_text_for(e, VirtualChassis, None) or type(e).__name__)


def _cached_virtual_chassis_data(api: LibreNMSAPI, device_id, force_refresh: bool, serial_rules) -> dict:
    """Return the cached detection data of *device_id*, and detect and cache it on a miss."""
    serial_rules = serial_rules or SerialRules.load()
    try:
        with transaction.atomic() if connection.in_atomic_block else nullcontext():
            cache_key = _vc_cache_key(api, device_id, serial_rules)
    except DatabaseError as e:
        logger.exception("Could not read serial rules for virtual chassis detection on device %s", device_id)
        return _failed_virtual_chassis_data(exception_text_for(e, VirtualChassis, None))
    _cache_timeout = getattr(api, "cache_timeout", None)
    cache_timeout = 300 if _cache_timeout is None else _cache_timeout
    if not force_refresh and cache_timeout != 0:
        cached = cache.get(cache_key)
        if cached is not None:
            return _clone_virtual_chassis_data(cached)

    detection_data = detect_virtual_chassis_from_inventory(api, device_id, serial_rules=serial_rules)
    if detection_data is None:
        # Cache a confirmed non-stack result so subsequent renders skip repeated reads.
        empty = empty_virtual_chassis_data()
        if cache_timeout != 0:
            cache.set(cache_key, empty, timeout=cache_timeout)
        return _clone_virtual_chassis_data(empty)

    if detection_data.get("detection_failed"):
        return _clone_virtual_chassis_data(detection_data)

    if "detection_error" not in detection_data:
        detection_data["detection_error"] = None

    cache_value = _clone_virtual_chassis_data(detection_data)
    if cache_timeout != 0:
        cache.set(cache_key, cache_value, timeout=cache_timeout)
    return _clone_virtual_chassis_data(cache_value)


def prefetch_vc_data_for_devices(api: LibreNMSAPI, device_ids: List[int], *, force_refresh: bool = False) -> None:
    """
    Pre-warm the virtual chassis cache for multiple devices.

    This eliminates the 0.5-1s delay when rendering the import table
    by proactively fetching VC data before validation.

    Args:
        api: LibreNMSAPI instance
        device_ids: List of LibreNMS device IDs to prefetch VC data for
        force_refresh: When True, bypass cache and fetch fresh data

    Example:
        >>> # Before rendering import table
        >>> prefetch_vc_data_for_devices(api, [123, 124, 125])
        >>> # Now all validate_device_for_import() calls hit cache instantly

    """
    if not api or not device_ids:
        return

    logger.debug(f"Pre-warming VC cache for {len(device_ids)} devices")
    # Batch-local: a rule change shows in the next batch.
    serial_rules = SerialRules.load()

    for idx, device_id in enumerate(device_ids):
        # This populates the cache if empty, or skips if already cached
        try:
            get_virtual_chassis_data(api, device_id, force_refresh=force_refresh, serial_rules=serial_rules)
        except (BrokenPipeError, ConnectionError, IOError, OSError) as e:
            logger.warning("Connection error during VC prefetch at device %s: %s", idx, e)
            # Stop processing if connection is broken
            return
        except Exception as e:
            # Log but continue for other errors
            logger.warning("Error prefetching VC data for device %s: %s", device_id, e)

    logger.debug(f"VC cache warming complete for {len(device_ids)} devices")


def select_vc_parent_index(root_rows: list) -> int | None:
    """Return the index of the root that holds the stack members: a stack root first, else a chassis root."""
    for wanted in ("stack", "chassis"):
        for row in root_rows:
            if isinstance(row, dict) and row.get("entPhysicalClass") == wanted:
                index = _as_int(row.get("entPhysicalIndex"))
                if index is not None:
                    return index
    return None


def _is_junos_vc_root(row: dict) -> bool:
    """Return whether *row* is a chassis root whose description names a Junos Virtual Chassis."""
    descr = row.get("entPhysicalDescr")
    return (
        row.get("entPhysicalClass") == "chassis"
        and _as_int(row.get("entPhysicalContainedIn")) == 0
        and isinstance(descr, str)
        and JUNOS_VC_ROOT_DESCR_MARKER.casefold() in descr.casefold()
    )


def junos_fpc_slot_match(text: str):
    """Return the match of "FPC <n>" at the start of trimmed *text*, then a space or the end, or None."""
    # The appended space lets the shared pattern match "FPC <n>" at the end of the text.
    return JUNOS_FPC_MEMBER_DESCR_RE.match(f"{text} ")


def _is_junos_fpc(row: dict) -> bool:
    """Return whether *row* is a Junos FPC container row; an "FPC <n> ..." row names FPC n instead."""
    # The anonymizer reads the trimmed description, so live inventory and replay agree.
    descr = librenms_text(row.get("entPhysicalDescr"))
    return (
        row.get("entPhysicalClass") == "container"
        and descr.startswith(JUNOS_FPC_DESCR_PREFIX)
        # "FPC <n>" alone is the FPC itself; more text after the slot names FPC n.
        and not ((named := junos_fpc_slot_match(descr)) and descr[named.end() :].strip())
    )


def _junos_fpc_serials_hold(root_serial, fpc_serials: list, context) -> bool:
    """Return whether two or more FPCs each have their own real serial and the root serial matches exactly one."""
    groups = group(fpc_serials, context)
    return (
        len(fpc_serials) >= 2
        and not groups.absent
        and all(len(fpcs) == 1 for fpcs in groups)
        and len(groups.find(root_serial)) == 1
    )


def _junos_fpc_rows(root: dict, children: list, contexts) -> list:
    """Return the FPC member rows of a Junos Virtual Chassis root, or [] when no serial context meets every condition."""
    if not _is_junos_vc_root(root):
        return []
    fpcs = [row for row in children if _is_junos_fpc(row)]
    # Fewer than two FPCs is no stack, so no serial context decides it.
    if len(fpcs) < 2:
        return []
    fpc_serials = [row.get("entPhysicalSerialNum") for row in fpcs]
    # A false stack creates bogus member devices on import, so one serial context must meet every condition.
    if any(_junos_fpc_serials_hold(root.get("entPhysicalSerialNum"), fpc_serials, context) for context in contexts):
        return fpcs
    return []


def identify_vc_master(members: list, device_serial, context) -> dict | None:
    """
    Return the stack member that is the imported device, or None when no single member is.

    This is the one master decision, for import and for the pages that preview it. The master is
    returned only when exactly one member matches: none or several mean that the stack is
    imported as a single device, because a guess splits it into more devices than switches.
    Junos FPC members must also meet the FPC serial checks under *context*, because detection
    accepts a shape that the serial rules of any manufacturer explain.

    Args:
        members: Detected members, as :func:`extract_vc_members` returns them.
        device_serial: The serial of the device that becomes the master.
        context: The serial context of the DeviceType the device imports as.

    """
    root_serials = [member["root_serial"] for member in members if "root_serial" in member]
    if root_serials and (
        len(root_serials) != len(members)
        or len(set(root_serials)) != 1
        or not _junos_fpc_serials_hold(root_serials[0], [member.get("serial") for member in members], context)
    ):
        return None
    matches = [member for member in members if compare(device_serial, member.get("serial"), context) is Comparison.SAME]
    return matches[0] if len(matches) == 1 else None


# Fixed texts: a numbering refusal reaches any import or module viewer, so it names no member, serial or rule.
VC_NUMBERING_INVALID = "The virtual chassis member numbers that LibreNMS reports cannot be numbered in NetBox, so the stack is not imported."
VC_NUMBERING_RULE_INVALID = (
    "A virtual chassis member numbering rule does not work, so the stack is not imported. Fix the rule, then try again."
)
VC_NUMBERING_MISMATCH = (
    "The virtual chassis member numbers in NetBox do not agree with the LibreNMS inventory under the current "
    "member numbering rules. Restore a matching rule or correct the members, then refresh Modules."
)
VC_NUMBERING_UNRECOGNIZED = (
    "The LibreNMS inventory of this virtual chassis does not describe its stack members, so no row can be given "
    "to a member. Refresh Modules when LibreNMS reports the stack."
)
VC_NUMBERING_NO_MASTER = (
    "This virtual chassis has no master among its members, so its member numbering rule is unknown. "
    "Set the master, then refresh Modules."
)


class VCNumberingError(Exception):
    """The member numbers of a stack cannot be decided; the message is one of the fixed ``VC_NUMBERING_*`` texts."""


def numbering_refusal_text(exc) -> str:
    """Return the fixed text of a numbering refusal; a broken rule shows its details only through ``shown_to``."""
    return str(exc) if isinstance(exc, VCNumberingError) else VC_NUMBERING_RULE_INVALID


def load_numbering_rules() -> tuple:
    """Return every VCMemberNumberingRule in id order, read once for a request or a batch."""
    from ..models import VCMemberNumberingRule

    return tuple(VCMemberNumberingRule.objects.order_by("pk"))


def select_numbering_rule(rules, device_type):
    """
    Return the rule that numbers the members of a stack of *device_type*, or None.

    Every rule of the type's manufacturer and every unscoped rule compiles first, so a broken
    pattern never loses to a broader rule. A lower tier wins, then the lowest id.

    Raises:
        RuleConfigurationError: A candidate rule stores a pattern that does not compile.

    """
    candidates = [rule for rule in rules if rule.manufacturer_id in (None, device_type.manufacturer_id)]
    for rule in candidates:
        _ = rule._compiled_device_type_pattern
    matching = [rule for rule in candidates if rule.matches(device_type)]
    return min(matching, key=lambda rule: (rule.tier, rule.pk), default=None)


def numbering_mode_for(device_type, rules) -> str:
    """Return the numbering mode of *device_type*: its rule's mode, or ``reported`` without a rule."""
    from ..models import VCMemberNumberingRule

    rule = select_numbering_rule(rules, device_type)
    return VCMemberNumberingRule.MODE_REPORTED if rule is None else rule.numbering_mode


def project_vc_members(members: list, mode: str) -> list[dict]:
    """
    Return copies of *members* whose ``position`` is the NetBox ``vc_position`` under *mode*.

    This is the one numbering arithmetic. It reads only ``inventory_position``, so a second
    projection gives the same result. ``one_based`` adds 1 when the stack is numbered from 0.

    Raises:
        VCNumberingError: The reported numbers are no unique positions, *mode* is no mode, or a
            number is outside the range of ``Device.vc_position``.

    """
    from ..models import VCMemberNumberingRule

    sources = [member.get("inventory_position") for member in members]
    if not sources or not all(is_vc_position(source) for source in sources) or len(set(sources)) != len(sources):
        raise VCNumberingError(VC_NUMBERING_INVALID)
    if mode == VCMemberNumberingRule.MODE_ONE_BASED:
        shift = 1 if min(sources) == 0 and max(sources) > 0 else 0
    elif mode == VCMemberNumberingRule.MODE_REPORTED:
        shift = 0
    else:
        raise VCNumberingError(VC_NUMBERING_RULE_INVALID)
    projected = [{**member, "position": member["inventory_position"] + shift} for member in members]
    field = Device._meta.get_field("vc_position")
    for member in projected:
        try:
            field.run_validators(member["position"])
        except ValidationError as exc:
            raise VCNumberingError(VC_NUMBERING_INVALID) from exc
    return projected


def unnumbered_virtual_chassis(vc_data: dict, error: str | None = None) -> dict:
    """Return *vc_data* without NetBox numbers, names or a master: no DeviceType, or the numbering *error*."""
    members = [
        {**member, "position": None, "suggested_name": None, "is_master": False} for member in vc_data["members"]
    ]
    return {
        **vc_data,
        "members": members,
        "master_identified": False,
        "numbering_available": False,
        "numbering_error": error,
        "device_type_id": None,
    }


def prepare_virtual_chassis(
    vc_data: dict, *, device_type, master_name, device_serial, serial_rules, numbering_rules=None
) -> dict:
    """
    Return the import view of detected stack data for *device_type*; the one preparation that previews and import read.

    It selects the numbering rule, projects the reported numbers, names the members and marks the
    master from the projected list. Without a DeviceType the numbers are unavailable.

    Args:
        vc_data: Detection data, or data that this function prepared before.
        device_type: The DeviceType the device imports as, or None.
        master_name: The name the device imports with.
        device_serial: The LibreNMS serial of the device that becomes the master.
        serial_rules: The batch's :class:`SerialRules`.
        numbering_rules: The batch's numbering rules; None reads them.

    Raises:
        VCNumberingError: The members cannot be numbered.
        RuleConfigurationError: A candidate numbering rule stores a pattern that does not compile.

    """
    if not vc_data or not vc_data.get("is_stack"):
        return vc_data
    if device_type is None:
        return unnumbered_virtual_chassis(vc_data)
    rules = load_numbering_rules() if numbering_rules is None else numbering_rules
    members = project_vc_members(vc_data["members"], numbering_mode_for(device_type, rules))
    master = None
    if display(device_serial):
        master = identify_vc_master(members, device_serial, serial_rules.for_device_type(device_type))
    for member in members:
        member["is_master"] = member is master
    prepared = {
        **vc_data,
        "members": members,
        "master_identified": master is not None,
        "numbering_available": True,
        "numbering_error": None,
        "device_type_id": device_type.pk,
    }
    if master_name:
        update_vc_member_suggested_names(prepared, master_name, serial_rules=serial_rules, device_type=device_type)
    else:
        for member in members:
            member["suggested_name"] = f"Member-{member['position']}"
    return prepared


def prepared_master(prepared: dict) -> dict | None:
    """Return the member entry of *prepared* that import makes the master, or None."""
    return next((member for member in prepared.get("members", ()) if member.get("is_master")), None)


def _member_entries(rows: list, *, model_field: str) -> list[dict]:
    """Return one member entry per inventory row, in position order."""
    positions = _member_positions([row.get("entPhysicalParentRelPos") for row in rows])
    members = [
        {
            "serial": row.get("entPhysicalSerialNum", ""),
            # The reported number; only project_vc_members derives a NetBox position from it.
            "inventory_position": position,
            "position": position,
            "model": row.get(model_field, ""),
            "name": row.get("entPhysicalName", ""),
            "index": row.get("entPhysicalIndex"),
            "description": row.get("entPhysicalDescr", ""),
        }
        for row, position in zip(rows, positions, strict=True)
    ]
    members.sort(key=lambda member: member["position"])
    return members


# The member shapes that :func:`extract_vc_stack` recognizes; the data-shape signature records them.
VC_MEMBER_SHAPE_CHASSIS = "chassis"
VC_MEMBER_SHAPE_JUNOS_FPC = "junos-fpc"


class VcStack(NamedTuple):
    """A stack that :func:`extract_vc_stack` found: its root row, member rows, members and member shape."""

    root: dict | None
    member_rows: list
    members: list
    shape: str | None


def extract_vc_members(rows: list, contexts) -> list[dict]:
    """
    Return the Virtual Chassis members that ENTITY-MIB inventory rows describe.

    This is the one definition of a stack member, for import detection, the sync-tab serials
    modal and the data-shape signature. Two shapes describe members:

    * Two or more ``chassis`` children of the stack (or chassis) root.
    * Junos: a ``chassis`` root whose description names the Virtual Chassis, with two or more
      ``container`` children whose description starts with "FPC". Each FPC needs its own real
      serial, and the root serial must match exactly one FPC, all under one of *contexts*. The
      model is the FPC name, and the position is the FPC's parent-relative position (the member id).

    Which member is the master is not part of this shape: :func:`identify_vc_master` decides it.

    Args:
        rows: Inventory rows. They must hold the root rows and at least their direct children;
            deeper rows are ignored.
        contexts: The serial contexts that may compare the Junos FPC serials.

    Returns:
        list[dict]: The members in position order, or [] when the rows describe no stack.

    """
    return extract_vc_stack(rows, contexts).members


def extract_vc_stack(rows: list, contexts) -> VcStack:
    """Return the stack that :func:`extract_vc_members` reads, with the member shape that matched."""
    rows = [row for row in rows if isinstance(row, dict)]
    roots = [row for row in rows if _as_int(row.get("entPhysicalContainedIn")) == 0]
    parent_index = select_vc_parent_index(roots)
    if parent_index is None:
        return VcStack(None, [], [], None)
    root = next(row for row in roots if _as_int(row.get("entPhysicalIndex")) == parent_index)
    children = [row for row in rows if _as_int(row.get("entPhysicalContainedIn")) == parent_index]

    chassis = [row for row in children if row.get("entPhysicalClass") == "chassis"]
    if len(chassis) >= 2:
        members = _member_entries(chassis, model_field="entPhysicalModelName")
        return VcStack(root, chassis, members, VC_MEMBER_SHAPE_CHASSIS)
    fpcs = _junos_fpc_rows(root, children, contexts)
    if fpcs:
        # The master decision checks the FPC serials again under the context of the imported DeviceType.
        members = [
            {**member, "root_serial": root.get("entPhysicalSerialNum")}
            for member in _member_entries(fpcs, model_field="entPhysicalName")
        ]
        return VcStack(root, fpcs, members, VC_MEMBER_SHAPE_JUNOS_FPC)
    return VcStack(None, [], [], None)


def _junos_fpc_named_slot(row: dict) -> int | None:
    """Return the FPC slot that a Junos row's description names as "FPC <n> ...", or None."""
    # The anonymizer keeps this prefix only in trimmed descriptions, so a name never links.
    named = junos_fpc_slot_match(librenms_text(row.get("entPhysicalDescr")))
    return int(named.group("member")) if named else None


class _StackRows(NamedTuple):
    """The inventory rows that :func:`extract_vc_members` reads as a stack, keyed by raw index."""

    root_index: object
    # {member row index: NetBox position under the stack's numbering mode}.
    positions: dict
    # False when the positions come from row order, which names a member only for a member row without a serial.
    positions_reported: bool
    # Junos only: {reported FPC slot: FPC row index} for slots that exactly one FPC reports.
    fpc_slots: dict

    def fpc_named_by(self, row: dict):
        """Return ``(names_a_slot, FPC index)`` of a Junos "FPC <n> ..." row directly under the root.

        The index is None unless exactly one FPC reports slot n.
        """
        if row.get("entPhysicalContainedIn") != self.root_index or row.get("entPhysicalIndex") in self.positions:
            return False, None
        slot = _junos_fpc_named_slot(row)
        return slot is not None, self.fpc_slots.get(slot)


def _stack_rows(rows, contexts, numbering_mode) -> _StackRows:
    """Return the stack root, the member rows' projected positions and the Junos FPC slots of an inventory."""
    stack = extract_vc_stack(rows, contexts)
    if stack.root is None:
        return _StackRows(None, {}, False, {})
    fpc_slots = {}
    if stack.shape == VC_MEMBER_SHAPE_JUNOS_FPC:
        slots = [_as_int(row.get("entPhysicalParentRelPos")) for row in stack.member_rows]
        fpc_slots = {
            slot: row.get("entPhysicalIndex")
            for row, slot in zip(stack.member_rows, slots, strict=True)
            if is_vc_position(slot) and slots.count(slot) == 1
        }
    reported = [_as_int(row.get("entPhysicalParentRelPos")) for row in stack.member_rows]
    return _StackRows(
        stack.root.get("entPhysicalIndex"),
        {member["index"]: member["position"] for member in project_vc_members(stack.members, numbering_mode)},
        _member_positions(reported) == reported,
        fpc_slots,
    )


# The source of a row that no rule attributes while it or an ancestor reports a serial without an identity.
UNATTRIBUTED_SOURCE = "unattributed"


class _Attribution:
    """Attribute the rows of one inventory to Virtual Chassis members; see :func:`attribute_inventory`."""

    def __init__(self, rows, vc_members, context, numbering_mode, member_context=None):
        self.vc_members = vc_members
        # Without members every row stays on the page, and context and numbering_mode can be None.
        self.by_serial = MemberSerials(vc_members, member_context or (lambda _member: context))
        self.by_index = {index: row for row in rows if (index := row.get("entPhysicalIndex")) is not None}
        # The stack holds when the rules of any member explain it, as by_serial admits each row.
        self.stack = (
            _stack_rows(rows, self.by_serial.contexts(), numbering_mode)
            if vc_members
            else _StackRows(None, {}, False, {})
        )
        # The members that a member row owns by its serial; a position gives none of them to another member row.
        self.serial_owned = {
            member.pk
            for index in self.stack.positions
            if (member := self.serial_owner(self.by_index.get(index, {}))) is not None
        }
        self.results = {}
        if vc_members and rows:
            self._check_numbering()

    def _check_numbering(self):
        """Refuse an inventory whose member rows do not name the stored members by the current numbering."""
        if not self.stack.positions:
            raise VCNumberingError(VC_NUMBERING_UNRECOGNIZED)
        stored = {member.vc_position for member in self.vc_members}
        for index, position in self.stack.positions.items():
            owner = self.serial_owner(self.by_index[index])
            if position not in stored or (owner is not None and owner.vc_position != position):
                raise VCNumberingError(VC_NUMBERING_MISMATCH)

    def member_at(self, position):
        """Return the member at *position*, or None."""
        position = _as_int(position)
        if not is_vc_position(position):
            return None
        return next((member for member in self.vc_members if member.vc_position == position), None)

    def serial_owner(self, row):
        """Return the member whose serial the row carries, or None; a serial that two members share names neither."""
        members = self.by_serial.find(row.get("entPhysicalSerialNum"))
        return members[0] if len(members) == 1 else None

    def resolve(self, row):
        """Return ``(member | None, source)`` for one row."""
        if id(row) in self.results:
            return self.results[id(row)]
        if not self.vc_members:
            result = (None, "default")
        elif (member := self.serial_owner(row)) is not None:
            result = (member, "serial")
        else:
            result = self._in_stack(row)
        self.results[id(row)] = result
        return result

    def owner(self, row):
        """Return ``(member | None, source)`` for one row; a page row below a serial without an identity is unattributed."""
        member, source = self.resolve(row)
        if member is None and self.vc_members and self._reports_serial_without_identity(row):
            return None, UNATTRIBUTED_SOURCE
        return member, source

    def _reports_serial_without_identity(self, row):
        """Return whether the row or an ancestor reports serial text that no member's rules give an identity."""
        current, visited = row, set()
        while isinstance(current, dict) and id(current) not in visited:
            visited.add(id(current))
            if self._has_serial_without_identity(current):
                return True
            current = self.by_index.get(current.get("entPhysicalContainedIn"))
        return False

    def _has_serial_without_identity(self, row):
        """Return whether the row reports serial text that no member's rules give an identity."""
        serial = row.get("entPhysicalSerialNum")
        return bool(display(serial)) and not self.by_serial.admits(serial)

    def _member_row_owner(self, row):
        """Return the member a member row resolves to: its serial first, then its detected position."""
        if (member := self.serial_owner(row)) is not None:
            return member
        if self._row_order_names_no_member(row):
            return None
        member = self.member_at(self.stack.positions.get(row.get("entPhysicalIndex")))
        return None if member is not None and member.pk in self.serial_owned else member

    def _row_order_names_no_member(self, row):
        """Return whether a member row's position comes from row order and it reports a serial no member owns."""
        serial = row.get("entPhysicalSerialNum")
        return not self.stack.positions_reported and bool(display(serial)) and self.serial_owner(row) is None

    def _in_stack(self, row):
        """Attribute a row of a recognized stack to the member row it is or sits under."""
        # Walk up to the stack root, which owns no member: a nearer serial wins over a member row.
        current = row
        visited = set()
        # The stack shape names its member rows, so a short serial on another row names no member.
        while isinstance(current, dict) and current.get("entPhysicalIndex") != self.stack.root_index:
            if id(current) in visited:
                break
            visited.add(id(current))
            if current is not row and (member := self.serial_owner(current)) is not None:
                return member, "ancestor-serial"
            if current.get("entPhysicalIndex") in self.stack.positions:
                member = self._member_row_owner(current)
                # A member row that names no NetBox member belongs to a member that NetBox does not hold.
                if member is None:
                    return None, UNATTRIBUTED_SOURCE
                return member, "position" if current is row else "parent-context"
            # A Junos PSU or fan tray under the root names its FPC as "FPC <n> ..." and follows only that FPC.
            names_a_slot, fpc_index = self.stack.fpc_named_by(current)
            if names_a_slot and _is_junos_vc_root(self.by_index.get(self.stack.root_index, {})):
                fpc = self.by_index.get(fpc_index)
                member = self._member_row_owner(fpc) if fpc is not None else None
                if member is None:
                    return None, UNATTRIBUTED_SOURCE
                return member, "junos-member" if current is row else "parent-context"
            current = self.by_index.get(current.get("entPhysicalContainedIn"))
        return None, "default"


def attribute_inventory(rows: list, vc_members: list, context, numbering_mode, member_context=None) -> list[tuple]:
    """
    Return the Virtual Chassis member that owns each inventory row, in row order.

    This is all of module sync's member attribution. A row's own serial decides first. Only a
    member row of the stack that :func:`extract_vc_members` recognizes gives a member, by its
    position under *numbering_mode* (a position from row order gives none to a member row that
    reports a serial no member owns, and no position gives a member that another member row owns
    by serial; such a member row, and the rows below it, are unattributed); its descendants inherit
    it unless a nearer serial names another member, and the stack root and the other rows directly
    under it own no member. A Junos PSU or fan tray named "FPC <n> ..." follows the FPC that
    reports slot n. A row that no rule attributes has the source "default", or
    :data:`UNATTRIBUTED_SOURCE` when it or an ancestor reports a serial that has no identity,
    because such a serial names no member.

    Args:
        rows: The main inventory rows of one device; OOB controller rows are no part of it.
        vc_members: The NetBox Virtual Chassis member Devices.
        context: The serial context of the stack's master; unused without members.
        numbering_mode: The numbering mode of the master's DeviceType; unused without members.
        member_context: Return the context of one member's Device serial; None uses *context* for every member.

    Returns:
        list[tuple]: ``(member Device | None, source)`` per row. None means the page device, except with the
        source :data:`UNATTRIBUTED_SOURCE`, where the row needs an explicit member selection.

    Raises:
        VCNumberingError: The members have rows, and the inventory describes no stack, or a member
            row's projected position is no stored member or disagrees with the member its serial names.

    """
    attribution = _Attribution(rows, vc_members, context, numbering_mode, member_context)
    return [attribution.owner(row) for row in rows]


def detect_virtual_chassis_from_inventory(api: LibreNMSAPI, device_id: int, *, serial_rules=None) -> dict | None:
    """
    Detect a stack or Virtual Chassis from ENTITY-MIB inventory.

    Reads the root rows and the direct children of the stack (or chassis) root, then lets
    :func:`extract_vc_members` decide which children are members.

    Args:
        api: LibreNMSAPI instance
        device_id: LibreNMS device ID
        serial_rules: The batch's :class:`SerialRules`; None reads them.

    Returns:
        dict | None: ``{is_stack, member_count, members, detection_failed, detection_error}``.
            Each member carries serial, inventory_position (the reported number), position
            (the same number until :func:`prepare_virtual_chassis` projects it), model, name,
            index, description and a provisional suggested_name. None when the inventory confirms that the device is not a stack.
            A failed inventory read returns an empty payload with detection_failed=True so callers
            can fail closed without caching the failure as a negative result.

    """
    try:
        device_found, device_info = api.get_device_info(device_id)
        master_name = None
        if device_found and device_info:
            master_name = device_info.get("sysName") or device_info.get("hostname")

        # A confirmed device with no inventory rows is a non-stack. For a missing device,
        # an inventory 404 is a failed read and must not be cached as a non-stack.
        success, root_items = api.get_inventory_filtered(
            device_id, ent_physical_contained_in=0, missing_is_empty=device_found
        )
        if not success:
            logger.warning(f"Could not read root inventory items for device {device_id}")
            return _failed_virtual_chassis_data("LibreNMS root inventory request failed")

        root_items = root_items or []
        parent_index = select_vc_parent_index(root_items)
        if parent_index is None:
            return None

        success, child_items = api.get_inventory_filtered(
            device_id, ent_physical_contained_in=parent_index, missing_is_empty=True
        )
        if not success:
            logger.warning(f"Could not read child inventory for device {device_id}")
            return _failed_virtual_chassis_data("LibreNMS child inventory request failed")

        # The DeviceType is not known yet, so every candidate context may explain the stack.
        # They are read lazily: only a Junos FPC shape compares serials, so a broken rule refuses only that decision.
        serial_rules = serial_rules or SerialRules.load()
        members = extract_vc_members([*root_items, *(child_items or [])], serial_rules.iter_candidates())
        if not members:
            return None

        result = {
            "is_stack": True,
            "member_count": len(members),
            "members": members,
            "detection_failed": False,
            "detection_error": None,
        }
        if master_name:
            update_vc_member_suggested_names(result, master_name, serial_rules=serial_rules)
        else:
            for member in members:
                member["suggested_name"] = f"Member-{member['position']}"

        logger.info(f"Detected stack with {len(members)} members for device {device_id}")
        return result

    except Exception as e:
        logger.exception("Error detecting virtual chassis for device %s: %s", device_id, e)
        # The detection result has no viewer: it is cached and shown to any import user.
        return _failed_virtual_chassis_data(exception_text_for(e, VirtualChassis, None) or type(e).__name__)


def _load_vc_member_name_pattern() -> str:
    """Load the VC member name pattern from settings, with fallback to default."""
    from ..models import LibreNMSSettings

    default = "-M{position}"
    try:
        settings = LibreNMSSettings.objects.order_by("pk").first()
        if not settings:
            return default
        pattern = settings.vc_member_name_pattern
        return pattern if isinstance(pattern, str) and pattern.strip() else default
    except Exception as e:
        logger.warning("Could not load VC member name pattern from settings: %s. Using default.", e)
        return default


def _generate_vc_member_name(master_name: str, position: int, serial: str = None, pattern: str = None) -> str:
    """
    Generate name for VC member device using configured pattern from settings.

    Args:
        master_name: Name of the master/primary device
        position: VC position number
        serial: Optional serial number of the member device
        pattern: Optional pre-loaded name pattern; if None, loaded from settings.
                 Pass a pre-loaded pattern when calling inside a loop to avoid
                 repeated DB queries.

    Returns:
        Generated member device name

    Examples:
        pattern="-M{position}" -> "switch01-M2"
        pattern=" ({position})" -> "switch01 (2)"
        pattern="-SW{position}" -> "switch01-SW2"
        pattern=" [{serial}]" -> "switch01 [ABC123]"

    """
    if pattern is None:
        pattern = _load_vc_member_name_pattern()

    # Prepare format variables
    format_vars = {
        "master_name": master_name,
        "position": position,
        "serial": serial or "",
    }

    # Apply pattern - pattern should be suffix/prefix, not full name
    try:
        formatted_suffix = pattern.format(**format_vars)
        return f"{master_name}{formatted_suffix}"
    except (KeyError, ValueError, IndexError) as e:
        logger.error(f"Invalid placeholder in VC naming pattern '{pattern}': {e}. Using default.")
        return f"{master_name}-M{position}"


def _member_name(master_name: str, position: int, serial, pattern: str, context) -> str:
    """Return the name of the stack member; the import preview and member creation both name members here."""
    return _generate_vc_member_name(master_name, position, serial=naming_token(serial, context), pattern=pattern)


def unnamed_members(members_info, pattern, context) -> list[tuple]:
    """Return ``(member, position)`` for each stack member that *pattern* gives no name; the import refuses the stack."""
    if not _vc_name_uses_serial(pattern):
        return []
    return [
        (member, member.get("position")) for member in members_info if not naming_token(member.get("serial"), context)
    ]


def unnamed_member_reason(serial, pattern) -> str:
    """Return why *pattern* gives a stack member with *serial* no name."""
    reported = display(serial)
    reason = f"its serial '{reported}' has no identity" if reported else "it reports no serial"
    return f"{reason}, so the member name pattern '{pattern}' gives it no name."


def unnamed_member_refusal(position, serial, pattern) -> str:
    """Return the issue of a stack member at *position* that *pattern* gives no name."""
    return f"Stack member {position}: {unnamed_member_reason(serial, pattern)}"


def _vc_name_fields(pattern):
    for _, field, format_spec, _ in Formatter().parse(pattern):
        if field:
            yield field
        if format_spec:
            yield from _vc_name_fields(format_spec)


def _vc_name_uses_serial(pattern):
    try:
        fields = list(_vc_name_fields(pattern))
    except ValueError:
        return False
    return any(field == "serial" or field.startswith(("serial[", "serial.")) for field in fields)


def _existing_member_name(master_name, device, rules) -> str | None:
    """Return the name to use for an existing member, or None when its suffix needs unavailable rules or gets no serial."""
    if not master_name or not getattr(device, "virtual_chassis_id", None) or not is_vc_position(device.vc_position):
        return master_name

    pattern = _load_vc_member_name_pattern()
    if not _vc_name_uses_serial(pattern):
        return _member_name(master_name, device.vc_position, None, pattern, plain_context())

    context = _existing_member_context(device, rules)
    if context is None or not naming_token(device.serial, context):
        return None
    return _member_name(master_name, device.vc_position, device.serial, pattern, context)


def existing_member_name_refusal(device, rules) -> str:
    """Return why an existing member gets no name: its serial gives the ``{serial}`` pattern no text; else ""."""
    if not getattr(device, "virtual_chassis_id", None) or not is_vc_position(device.vc_position):
        return ""
    pattern = _load_vc_member_name_pattern()
    context = _existing_member_context(device, rules) if _vc_name_uses_serial(pattern) else None
    if context is None or naming_token(device.serial, context):
        return ""
    return unnamed_member_refusal(device.vc_position, device.serial, pattern)


def _existing_member_context(device, rules):
    """Return the serial context of an existing member, or None when its serial rules are unavailable."""
    try:
        context = rules.for_device(device)
    except RuleConfigurationError:
        return None
    return None if context.unavailable else context


def update_vc_member_suggested_names(vc_data: dict, master_name: str, *, serial_rules, device_type=None) -> dict:
    """
    Regenerate suggested VC member names using the actual master device name.

    This ensures preview shows accurate names after use_sysname and strip_domain
    are applied to the master device name.

    Args:
        vc_data: Virtual chassis detection data dict
        master_name: The actual name that will be used for master device in NetBox
        serial_rules: The batch's serial rules.
        device_type: The DeviceType the device imports as. None uses global rules.

    Returns:
        Updated vc_data dict with corrected suggested_name for each member

    """
    if not vc_data or not vc_data.get("is_stack"):
        return vc_data

    # Load naming pattern once to avoid a DB query per member
    vc_pattern = _load_vc_member_name_pattern()
    context = serial_rules.for_device_type(device_type) if _vc_name_uses_serial(vc_pattern) else plain_context()
    members = vc_data.get("members", [])
    # A member that the pattern gives no name gets no suggestion; validation reports why.
    unnamed = {id(member) for member, _position in unnamed_members(members, vc_pattern, context)}
    for member in members:
        member["suggested_name"] = (
            ""
            if id(member) in unnamed
            else _member_name(master_name, member["position"], member.get("serial"), vc_pattern, context)
        )

    return vc_data


def _as_int(value) -> int | None:
    """Return an ENTITY-MIB number (position, index, containment) as an int, or None."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _member_positions(raw_positions: list) -> list[int]:
    """Return the reported member positions, or row order from 1 when any is missing, negative or shared."""
    parsed = [_as_int(raw) for raw in raw_positions]
    if all(is_vc_position(position) for position in parsed) and len(set(parsed)) == len(parsed):
        return parsed
    # Repairing one member at a time could reuse a slot that another member reports.
    return list(range(1, len(parsed) + 1))


def _sync_module_bay_counter(device: Device) -> None:
    """Reconcile device module_bay_count with actual ModuleBay rows in the DB; a failure fails the caller's transaction."""
    actual_count = device.modulebays.count()
    if getattr(device, "module_bay_count", None) != actual_count:
        Device.objects.filter(pk=device.pk).update(module_bay_count=actual_count)
        device.module_bay_count = actual_count


# Import creates no chassis for a stack whose master is unknown: guessing it splits the stack into
# more devices than switches.
VC_MASTER_UNKNOWN_WARNING = (
    "Imported device {device_id} without a virtual chassis: the stack master could not be identified by serial."
)


def create_virtual_chassis_with_members(
    master_device: Device,
    prepared: dict,
    libre_device: dict,
    server_key: str | None = None,
    *,
    master_member: dict,
    serial_rules: SerialRules | None = None,
) -> VirtualChassis:
    """
    Create Virtual Chassis and member devices from a prepared stack.

    This function creates a NetBox VirtualChassis with the master device
    and all detected member devices, wrapped in a transaction for safety.

    Args:
        master_device: The imported device (becomes VC master)
        prepared: The stack that :func:`prepare_virtual_chassis` prepared for the master's DeviceType;
            its members, the master included, carry the NetBox positions that this function writes.
        libre_device: Original LibreNMS device data
        server_key: LibreNMS server key stored with the created members.
        master_member: The member entry of *prepared* that is *master_device*.
        serial_rules: The batch's :class:`SerialRules`; None reads them.

    Returns:
        VirtualChassis: The created virtual chassis instance

    Raises:
        ValueError: If *prepared* was prepared for another DeviceType than the locked master row
            holds, its positions are no unique NetBox positions, or *master_member* is not one of its
            members or not the master under that DeviceType
        IntegrityError: If duplicate serials/names are detected
        Exception: For other creation errors

    """
    # Members store the raw serial; names and the duplicate check use the serial context of the master.
    serial_rules = serial_rules or SerialRules.load()
    members_info = prepared["members"]
    positions = [member.get("position") for member in members_info]
    # Creation writes the prepared numbers and never numbers a stack itself.
    if not all(is_vc_position(position) for position in positions) or len(set(positions)) != len(positions):
        raise ValueError("The stack was not numbered for import; no chassis was created.")
    master_pos = next((member["position"] for member in members_info if member is master_member), None)
    if master_pos is None:
        raise ValueError("master_member must be one of the prepared members")

    # Save originals for in-memory rollback — transaction.atomic() rolls back DB but
    # not in-memory model fields.
    original_master_name = master_device.name
    original_vc = master_device.virtual_chassis
    original_vc_position = master_device.vc_position

    try:
        with atomic_with_events():
            # The serial identity locks of every member come before any row lock.
            assignment_lock(
                [display(member.get("serial")) for member in members_info if member is not master_member],
                model=Device,
                rules=serial_rules,
            )
            # The master row decides the member context: a type change of the master is waited for, then read.
            # The master decision and the member type follow the type that the locked row holds now.
            locked_master = Device.objects.select_for_update(no_key=True).get(pk=master_device.pk)
            if prepared.get("device_type_id") != locked_master.device_type_id:
                raise ValueError("The stack was numbered for another device type; no chassis was created.")
            # The member inserts write the type row's device count anyway; lock it now, so the context is its maker.
            member_type = DeviceType.objects.select_for_update(no_key=True).get(pk=locked_master.device_type_id)
            context = serial_rules.for_device_type(member_type)
            if identify_vc_master(members_info, locked_master.serial, context) is not master_member:
                raise ValueError(
                    "The stack master does not match under the current device type; no chassis was created."
                )
            # Load naming pattern once to avoid a DB query per member
            vc_pattern = _load_vc_member_name_pattern()
            if unnamed := unnamed_members(members_info, vc_pattern, context):
                refusals = [
                    unnamed_member_refusal(position, member.get("serial"), vc_pattern) for member, position in unnamed
                ]
                raise ValueError(" ".join([*refusals, "No chassis was created."]))
            master_device.device_type = member_type
            master_device.snapshot()
            # Rename the master device with its own position, from its member entry like every other member
            master_device_new_name = _member_name(
                original_master_name, master_pos, master_member.get("serial"), vc_pattern, context
            )

            # Check if renamed master conflicts with existing device
            rename_master = not Device.objects.filter(name=master_device_new_name).exclude(pk=master_device.pk).exists()
            if rename_master:
                master_device.name = master_device_new_name
            else:
                logger.warning(
                    f"Cannot rename master to '{master_device_new_name}' - name already exists. "
                    f"Keeping original name '{original_master_name}'"
                )

            # Create VC using original base name
            _device_id = libre_device.get("device_id") or master_device.pk
            _domain_prefix = f"librenms-{server_key}" if server_key else "librenms"
            vc = VirtualChassis.objects.create(
                name=original_master_name,
                domain=f"{_domain_prefix}-{_device_id}",
            )

            # Update master device
            master_device.virtual_chassis = vc
            master_device.vc_position = master_pos
            save_fields = ["virtual_chassis", "vc_position", "last_updated"]
            if rename_master:
                save_fields.append("name")
            master_device.save(update_fields=save_fields)

            members_created = _create_member_devices(
                master_device,
                original_master_name,
                members_info,
                positions,
                master_member,
                vc,
                vc_pattern,
                context,
                serial_rules,
            )
            expected_members = len(members_info) - 1
            if members_created < expected_members:
                logger.warning(
                    f"Created {members_created} members but expected {expected_members}. "
                    "Some members may have been skipped due to duplicates."
                )

            # Assign VC master only after all members are attached to avoid
            # NetBox's create-time auto-master signal changing order/state.
            vc.snapshot()
            vc.master = master_device
            vc.save(update_fields=["master", "last_updated"])
            _sync_module_bay_counter(master_device)

            logger.info(
                f"Created Virtual Chassis '{vc.name}' with {vc.members.count()} total members "
                f"(1 master + {members_created} additional)"
            )

            return vc

    except Exception as e:
        master_device.name = original_master_name
        master_device.virtual_chassis = original_vc
        master_device.vc_position = original_vc_position
        logger.error("Virtual Chassis creation failed for device %s: %s", original_master_name, e, exc_info=True)
        raise


def _create_member_devices(
    master_device, base_name, members_info, positions, master_member, vc, vc_pattern, context, serial_rules
) -> int:
    """Create a device for each non-master member at its stack position; return how many were created."""
    member_rack = master_device.rack
    member_location = master_device.location or (member_rack.location if member_rack and member_rack.location else None)
    # Text keys: the JSON numbers 123 and 123.0 are one dict key, but two serials.
    members = [
        (member, position, display(member.get("serial")))
        for member, position in zip(members_info, positions, strict=True)
        if member is not master_member
    ]
    # One lookup for the whole stack, after the serial identity locks. A holder's own rules can admit a serial
    # that the master's rules blank. The master holds its own entry's serial, apart from every other member.
    taken = resolve_devices_write(
        [serial for _member, _position, serial in members], context, rules=serial_rules, exclude_pks=[master_device.pk]
    )
    created = 0

    def named(_member, position, serial):
        return _member_name(base_name, position, serial, vc_pattern, context)

    for member, position, serial, member_name in members_to_create(members, taken, context, named):
        Device.objects.create(
            name=member_name,
            device_type=master_device.device_type,
            role=master_device.role,
            site=master_device.site,
            location=member_location,
            rack=member_rack,
            platform=master_device.platform,
            serial=stored_form(serial, Device),
            virtual_chassis=vc,
            vc_position=position,
            comments=f"VC member (LibreNMS: {member.get('name', 'Unknown')})\nAuto-created from stack inventory",
        )
        created += 1
    return created


def members_to_create(members, taken, context, name_of):
    """
    Yield ``(member, position, serial, name)`` for each stack member that the import creates, in order.

    Args:
        members: The ``(member, position, serial text)`` triples of the non-master members.
        taken: The ``resolve_devices_write`` result of their serials.
        context: The serial context of the chassis master.
        name_of: Return the name of a member from its ``(member, position, serial)``.

    """
    created_members = SerialGroups(context)
    for member, position, serial in members:
        if _member_serial_taken(serial, taken[serial], created_members):
            continue
        member_name = name_of(member, position, serial)
        # A member without a name collides with no Device, also not with an unnamed one.
        if member_name and Device.objects.filter(name=member_name).exists():
            logger.warning(f"Device with name '{member_name}' already exists, skipping VC member creation")
            continue
        created_members.add(serial, member)
        yield member, position, serial, member_name


def member_serial_unavailable_message(serial) -> str:
    """Return why the member writer refuses a member serial whose lookup is Unavailable; validation reports it too."""
    # The constant, not the lookup's reason: a Device lookup is Unavailable only for a broken master link.
    return f"Cannot check VC member serial '{display(serial)}': {UNAVAILABLE_MASTER_LINK}"


def _member_serial_taken(serial, resolution, created_members) -> bool:
    """
    Return whether a stack member with *serial* exists already, and log why it is skipped.

    Args:
        serial: The LibreNMS member serial.
        resolution: The Devices that held *serial* before this stack's members were created.
        created_members: The members that this stack created so far, grouped by serial.

    """
    if resolution.kind is ResolutionKind.UNAVAILABLE:
        raise ValidationError(member_serial_unavailable_message(serial))
    if created_members.find(serial) or resolution.kind is ResolutionKind.UNIQUE:
        logger.warning(f"Device with serial '{serial}' already exists, skipping VC member creation")
        return True
    if resolution.kind is ResolutionKind.AMBIGUOUS:
        logger.warning(f"Skipped VC member with serial '{serial}': several devices already hold this serial")
        return True
    if resolution.uncertain:
        logger.warning(
            f"Skipped VC member with serial '{serial}': a device stores serial "
            f"'{display(resolution.uncertain[0].serial)}', which can be the same member. Check the two serials manually."
        )
        return True
    return False
