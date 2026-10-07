"""
The one module that knows how object server mappings are stored, read and written.

A mapping links a NetBox object to one LibreNMS identity per server. Device and VirtualMachine
use the device identity space. Interface and VMInterface use the port identity space. The space
comes from the model, so no caller names it. Callers read mappings through :func:`read_mapping`,
:func:`read_mappings` and the lookups below, and never read the stored value themselves.

A writer changes a mapping in two steps. A builder (:func:`assign_own`, :func:`link_import` and
the others below) returns a :class:`MappingChange` for the writer's fresh row, and runs no query.
Then :func:`persist_mapping` claims the identities again, checks their owners and the row's
mapping, puts the change on the row, and calls the writer's own save once.
"""

import copy
import logging
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum

from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import JSONField, Q

from netbox_librenms_plugin.constants import OOB_TYPE_PATTERN, OOB_TYPES, normalize_oob_type
from netbox_librenms_plugin.librenms_ids import (
    coerce_librenms_id,
    librenms_id_text_pattern,
    normalize_librenms_port_id,
)
from netbox_librenms_plugin.transactions import TransactionConflict, recorded_conflict

logger = logging.getLogger(__name__)

PREFERRED_SERVER_FIELD = "_preferred_server"
MIGRATED_TO_FIELD = "_migrated_to"
RESERVED_SERVER_KEYS = frozenset({PREFERRED_SERVER_FIELD})
JSON_FIELD_LOOKUP_NAMES = frozenset(JSONField.get_lookups())

_STORAGE_FIELD = "custom_field_data"
_MAPPING_KEY = "librenms_id"


class IdentitySpace(StrEnum):
    """The namespace a LibreNMS identity belongs to."""

    DEVICE = "device"
    PORT = "port"


class MappingRole(StrEnum):
    """What a mapped identity is to the object: its own identity, or its out-of-band controller."""

    OWN = "own"
    OOB = "oob"


class ContainerStatus(StrEnum):
    """The form of an object's stored mapping."""

    ABSENT = "absent"
    SCOPED = "scoped"
    LEGACY = "legacy"
    INVALID = "invalid"


class PreferenceStatus(StrEnum):
    """Whether an object stores a preferred server, and whether that value is usable."""

    ABSENT = "absent"
    MALFORMED = "malformed"
    NAMED = "named"


class AmbiguousLibreNMSIdError(LookupError):
    """
    Raised when a librenms_id resolves to more than one NetBox object.

    Distinguishes a genuine ambiguity (a data-integrity violation — e.g. two devices
    sharing the same host id, or a host id and a *different* OOB id) from a clean
    miss. Returning ``None`` for both would let callers treat an ambiguous link as
    "not found" and proceed (importing/binding), so :func:`find_mapping` raises
    this instead and callers fail closed.
    """


def require_server_key(server_key: str) -> str:
    """Return a safe server identity key or reject invalid metadata paths."""
    if not isinstance(server_key, str) or not server_key.strip():
        raise ValueError("LibreNMS server key must be a non-empty string.")
    if server_key != server_key.strip():
        raise ValueError("LibreNMS server key must not contain leading or trailing whitespace.")
    if "__" in server_key:
        raise ValueError("LibreNMS server key must not contain '__'.")
    if server_key in JSON_FIELD_LOOKUP_NAMES:
        raise ValueError(f"LibreNMS server key {server_key!r} conflicts with a Django JSON lookup.")
    if server_key in RESERVED_SERVER_KEYS:
        raise ValueError(f"LibreNMS server key {server_key!r} is reserved for object metadata.")
    return server_key


def is_server_key(value) -> bool:
    """Return whether *value* is usable as a server identity key."""
    try:
        require_server_key(value)
    except ValueError:
        return False
    return True


def _space_of(model) -> IdentitySpace:
    """Return the identity space of a mapped model class, or raise TypeError."""
    from dcim.models import Device, Interface
    from virtualization.models import VirtualMachine, VMInterface

    if isinstance(model, type):
        if issubclass(model, (Device, VirtualMachine)):
            return IdentitySpace.DEVICE
        if issubclass(model, (Interface, VMInterface)):
            return IdentitySpace.PORT
    raise TypeError(f"{model!r} does not hold a LibreNMS server mapping.")


def _readable_legacy_id(value) -> int | None:
    """
    Return the ID of a bare legacy value under the wide ``int()`` rule, or None.

    This rule is deliberately wider than :func:`coerce_librenms_id`: ``" 42 "``, ``"+42"`` and
    ``"4_2"`` resolve here, but only the strict rule builds a lookup predicate. Keep the two
    separate. Narrowing this one would make the setter treat a readable legacy value as corrupt.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            readable = int(value)
        except ValueError:
            return None
        return readable if readable > 0 else None
    return None


@dataclass(frozen=True)
class LegacyState:
    """A bare legacy value, read under both legacy rules."""

    readable_id: int | None = None
    queryable_id: int | None = None

    @property
    def is_legacy(self) -> bool:
        """Return whether the stored value is a readable legacy ID."""
        return self.readable_id is not None


@dataclass(frozen=True)
class PreferenceState:
    """The preferred server as stored. Selection policy decides what it means."""

    status: PreferenceStatus = PreferenceStatus.ABSENT
    server: str | None = None


@dataclass(frozen=True)
class MigrationTarget:
    """A valid marker that sends a merged donor to its winner device."""

    device_id: int
    server_key: str
    at: object = None


@dataclass(frozen=True)
class MigrationState:
    """The migration marker of one server entry: stored, and valid while no live link exists."""

    recorded: bool = False
    effective: MigrationTarget | None = None


@dataclass(frozen=True)
class ServerState:
    """The mapping of one object on one server."""

    server: str
    own_id: int | None = None
    oob_id: int | None = None
    oob_type: object = None
    oob_recorded: bool = False
    migration: MigrationState = MigrationState()

    @property
    def display_id(self) -> int | None:
        """Return the own ID, or the OOB controller ID when the entry has no own ID."""
        return self.own_id if self.own_id is not None else self.oob_id

    @property
    def is_oob_only(self) -> bool:
        """Return whether only the OOB controller ID identifies this entry."""
        return self.own_id is None and self.oob_id is not None


@dataclass(frozen=True)
class MappingState:
    """An immutable snapshot of one object's mapping, taken at one read."""

    container: ContainerStatus
    has_recorded_state: bool
    legacy: LegacyState
    preference: PreferenceState
    servers: tuple[ServerState, ...]

    def server(self, server_key) -> ServerState | None:
        """Return the recorded entry for *server_key*, or None."""
        return next((entry for entry in self.servers if entry.server == server_key), None)

    def own_id(self, server_key: str) -> int | None:
        """Return the object's own ID on *server_key*. A legacy value resolves on every server."""
        server_key = require_server_key(server_key)
        if self.container is ContainerStatus.LEGACY:
            return self.legacy.readable_id
        entry = self.server(server_key)
        return entry.own_id if entry is not None else None

    def oob_id(self, server_key: str) -> int | None:
        """Return the OOB controller ID the object records on *server_key*."""
        entry = self.server(require_server_key(server_key))
        return entry.oob_id if entry is not None else None

    def has_oob(self, server_key: str) -> bool:
        """Return whether *server_key* records an OOB entry, a metadata-only one included."""
        entry = self.server(require_server_key(server_key))
        return entry is not None and entry.oob_recorded

    def migrated_to(self, server_key: str) -> MigrationTarget | None:
        """Return the effective migration target on *server_key*, or None."""
        entry = self.server(require_server_key(server_key))
        return entry.migration.effective if entry is not None else None


@dataclass(frozen=True)
class MappedRecord:
    """The caller's fields of one row in a bulk read, with the row's mapping snapshot."""

    values: dict
    mapping: MappingState


def _decode_entry(server_key: str, entry) -> ServerState:
    if not isinstance(entry, dict):
        return ServerState(server=server_key, own_id=coerce_librenms_id(entry))
    own_id = coerce_librenms_id(entry.get("id"))
    oob = entry.get("oob")
    oob_id = coerce_librenms_id(oob.get("id")) if isinstance(oob, dict) else None
    marker = entry.get(MIGRATED_TO_FIELD)
    effective = None
    # A live own or OOB link wins over a stale marker, and a marker names its own server.
    if isinstance(marker, dict) and own_id is None and oob_id is None and marker.get("server_key") == server_key:
        target_pk = marker.get("device_id")
        if isinstance(target_pk, int) and not isinstance(target_pk, bool) and target_pk > 0:
            effective = MigrationTarget(device_id=target_pk, server_key=server_key, at=copy.deepcopy(marker.get("at")))
    return ServerState(
        server=server_key,
        own_id=own_id,
        oob_id=oob_id,
        oob_type=copy.deepcopy(oob.get("type")) if isinstance(oob, dict) else None,
        oob_recorded=isinstance(oob, dict) and bool(oob),
        migration=MigrationState(recorded=isinstance(marker, dict), effective=effective),
    )


def _decode_preference(value: dict) -> PreferenceState:
    if PREFERRED_SERVER_FIELD not in value:
        return PreferenceState()
    stored = value[PREFERRED_SERVER_FIELD]
    if isinstance(stored, str) and stored.strip():
        return PreferenceState(status=PreferenceStatus.NAMED, server=stored)
    return PreferenceState(status=PreferenceStatus.MALFORMED)


def _decode_stored_mapping(value) -> MappingState:
    """
    Decode one stored mapping value into a snapshot.

    The readers and the builders below use it, so every reader and writer classifies a value with
    the same rules.
    """
    if isinstance(value, dict):
        # Hand-edited data can hold a key the validator rejects; every lookup would raise on it.
        servers = tuple(
            _decode_entry(server_key, entry) for server_key, entry in value.items() if is_server_key(server_key)
        )
        return MappingState(
            container=ContainerStatus.SCOPED,
            has_recorded_state=bool(value),
            legacy=LegacyState(),
            preference=_decode_preference(value),
            servers=servers,
        )
    legacy = LegacyState(readable_id=_readable_legacy_id(value), queryable_id=coerce_librenms_id(value))
    if value is None:
        container = ContainerStatus.ABSENT
    elif legacy.is_legacy:
        container = ContainerStatus.LEGACY
    else:
        container = ContainerStatus.INVALID
    return MappingState(
        container=container,
        has_recorded_state=bool(value),
        legacy=legacy,
        preference=PreferenceState(),
        servers=(),
    )


def read_mapping(obj) -> MappingState:
    """
    Return a snapshot of the mapping *obj* holds now.

    The read runs no query on a loaded object, writes nothing and takes no lock. Each call reads
    the object again, so a later call sees an unsaved change and an earlier snapshot does not.

    Raises:
        TypeError: If *obj* is not a Device, VirtualMachine, Interface or VMInterface.

    """
    _space_of(type(obj))
    return _decode_stored_mapping(obj.custom_field_data.get(_MAPPING_KEY))


def read_mappings(queryset, *, fields: Iterable[str]) -> tuple[MappedRecord, ...]:
    """
    Read *fields* and the mapping of every row in *queryset*, in one query.

    The rows stay plain values, so a large interface set builds no model instances. The queryset's
    filters and order stay as the caller built them. The module adds the storage it needs, so
    *fields* names only the caller's own fields.
    """
    _space_of(queryset.model)
    fields = tuple(fields)
    if _STORAGE_FIELD in fields:
        raise ValueError(f"{_STORAGE_FIELD!r} is mapping storage; read_mappings() loads it.")
    records = []
    for row in queryset.values(*fields, _STORAGE_FIELD):
        stored = row.pop(_STORAGE_FIELD)
        mapping = _decode_stored_mapping(stored.get(_MAPPING_KEY) if isinstance(stored, dict) else None)
        records.append(MappedRecord(values=row, mapping=mapping))
    return tuple(records)


def _identity_predicates(server_key, values) -> tuple[Q, Q]:
    """
    Return ``(own_q, oob_q)`` matching every stored form of any of *values* under *server_key*.

    Matches the namespaced scalar, the dict-with-id form, the legacy bare value and the OOB
    sub-key, in every text form that coerce_librenms_id() reads (so ``"042"`` and ``" 42 "`` match
    JSON ``42``). An invalid value is dropped. An invalid server key, or no valid value, matches
    nothing.
    """
    match_none = Q(pk__in=[])
    try:
        server_key = require_server_key(server_key)
    except ValueError:
        return match_none, match_none
    normalized_values = sorted({coerce_librenms_id(value) for value in values} - {None})
    if not normalized_values:
        return match_none, match_none

    storage = f"{_STORAGE_FIELD}__{_MAPPING_KEY}"
    # Text regex only: jsonb equality would also find a float 42.0 that the decoder rejects.
    # One alternation per JSON path: N values must not cost N predicates per path.
    numeric_pattern = librenms_id_text_pattern(*normalized_values)
    own_q = (
        Q(**{f"{storage}__{server_key}__regex": numeric_pattern})
        | Q(**{f"{storage}__{server_key}__id__regex": numeric_pattern})
        | Q(**{f"{storage}__regex": numeric_pattern})
    )
    oob_q = Q(**{f"{storage}__{server_key}__oob__id__regex": numeric_pattern})
    return own_q, oob_q


def _require_roles(roles) -> frozenset:
    roles = frozenset(MappingRole(role) for role in roles)
    if not roles:
        raise ValueError("Name at least one mapping role.")
    return roles


def identity_q(model, *, server: str, identities: Iterable, roles: Iterable[MappingRole]) -> Q:
    """
    Return an unevaluated predicate on *model* for objects mapped to any of *identities*.

    An invalid server key or identity matches nothing. The caller composes the predicate with its
    own scope and runs it.
    """
    _space_of(model)
    roles = _require_roles(roles)
    own_q, oob_q = _identity_predicates(server, identities)
    predicate = Q(pk__in=[])
    if MappingRole.OWN in roles:
        predicate |= own_q
    if MappingRole.OOB in roles:
        predicate |= oob_q
    return predicate


def _raise_ambiguous(model_name, identity, server_key, kind, matches):
    logger.warning(
        "Ambiguous librenms_id %r for %s on server %r: multiple %s matches (pk=%s, pk=%s) "
        "— refusing to bind (fail closed).",
        identity,
        model_name,
        server_key,
        kind,
        matches[0].pk,
        matches[1].pk,
    )
    raise AmbiguousLibreNMSIdError(
        f"librenms_id {identity!r} matches multiple {model_name} {kind} records "
        f"(pk={matches[0].pk}, pk={matches[1].pk}) on server {server_key!r}"
    )


def find_mapping(queryset, *, server: str, identity, roles: Iterable[MappingRole]):
    """
    Return the one object in *queryset* mapped to *identity* on *server*, or None.

    The queryset's scope and row locks apply to every query. A legacy bare value matches on every
    server.

    Raises:
        AmbiguousLibreNMSIdError: If more than one object matches, or an own match and a
            different OOB match exist.

    """
    model = queryset.model
    _space_of(model)
    roles = _require_roles(roles)
    # The predicates apply the same rule; checking it first skips the query.
    if coerce_librenms_id(identity) is None:
        return None
    own_q, oob_q = _identity_predicates(server, [identity])
    own_q = own_q if MappingRole.OWN in roles else Q(pk__in=[])
    oob_q = oob_q if MappingRole.OOB in roles else Q(pk__in=[])

    # One query settles the common case. Two matching rows need the per-role queries to classify.
    combined = list(queryset.filter(own_q | oob_q)[:2])
    if len(combined) < 2:
        return combined[0] if combined else None

    own_matches = list(queryset.filter(own_q)[:2]) if MappingRole.OWN in roles else []
    oob_matches = list(queryset.filter(oob_q)[:2]) if MappingRole.OOB in roles else []
    if len(own_matches) > 1:
        _raise_ambiguous(model.__name__, identity, server, "host", own_matches)
    if len(oob_matches) > 1:
        _raise_ambiguous(model.__name__, identity, server, "OOB", oob_matches)
    own_match = own_matches[0] if own_matches else None
    oob_match = oob_matches[0] if oob_matches else None
    if own_match is not None and oob_match is not None and own_match.pk != oob_match.pk:
        logger.warning(
            "Ambiguous librenms_id %r for %s on server %r: host match pk=%s but OOB match "
            "pk=%s — refusing to bind to either (fail closed).",
            identity,
            model.__name__,
            server,
            own_match.pk,
            oob_match.pk,
        )
        raise AmbiguousLibreNMSIdError(
            f"librenms_id {identity!r} matches {model.__name__} host pk={own_match.pk} but a "
            f"different OOB pk={oob_match.pk} on server {server!r}"
        )
    return own_match or oob_match


def find_port_owner(port_id, *, server: str):
    """
    Return the one Interface or VMInterface bound to a LibreNMS port on *server*, or None.

    A LibreNMS port ID names one port, so a holder on either model is the only owner. Every writer
    that binds a port ID reads this, so no writer can add a second owner on the other model.

    Raises:
        AmbiguousLibreNMSIdError: When more than one interface, on either model, holds the port.

    """
    from dcim.models import Interface
    from virtualization.models import VMInterface

    owners = [
        owner
        for model in (Interface, VMInterface)
        if (
            owner := find_mapping(
                model.objects.all(), server=server, identity=port_id, roles=(MappingRole.OWN, MappingRole.OOB)
            )
        )
        is not None
    ]
    if len(owners) > 1:
        raise AmbiguousLibreNMSIdError(
            f"LibreNMS port {port_id!r} is bound to both an Interface and a VMInterface on server {server!r}"
        )
    return owners[0] if owners else None


def port_holders(port_ids: Iterable, *, server: str) -> dict[int, tuple[str, int] | None]:
    """
    Map each held ID among *port_ids* to its one holder on *server*, as ``(model label, pk)``.

    This is the batch form of ``find_port_owner`` for a reader of many rows. An ID that
    ``find_port_owner`` finds maps to that holder. An ID that it refuses as ambiguous maps to None.
    An ID that no interface holds is absent. One query runs for each model.
    """
    from dcim.models import Interface
    from virtualization.models import VMInterface

    requested = {port_id for value in port_ids if (port_id := coerce_librenms_id(value)) is not None}
    if not requested or not is_server_key(server):
        return {}
    holders = {}
    for model in (Interface, VMInterface):
        rows = model.objects.filter(
            identity_q(model, server=server, identities=sorted(requested), roles=(MappingRole.OWN, MappingRole.OOB))
        )
        for record in read_mappings(rows, fields=("pk",)):
            holder = (model._meta.label_lower, record.values["pk"])
            for port_id in {record.mapping.own_id(server), record.mapping.oob_id(server)} & requested:
                holders[port_id] = holder if holders.get(port_id, holder) == holder else None
    return holders


def resolve_device_port(device, *, server: str, port_id, name_candidates: Iterable[str]):
    """Resolve one Interface of *device* by its own LibreNMS port ID first, then by name."""
    interfaces = device.interfaces.all()
    normalized_port_id = coerce_librenms_id(port_id)
    if normalized_port_id is not None:
        predicate = identity_q(
            interfaces.model, server=server, identities=(normalized_port_id,), roles=(MappingRole.OWN,)
        )
        matches = list(interfaces.filter(predicate).order_by("pk")[:2])
        if matches:
            return matches[0] if len(matches) == 1 else None

    names = {name for name in name_candidates if isinstance(name, str) and name}
    if not names:
        return None
    matches = list(interfaces.filter(name__in=names).order_by("pk")[:2])
    if len(matches) != 1 or not name_match_may_be_port(matches[0], server=server, port_id=port_id):
        return None
    return matches[0]


def name_match_may_be_port(interface, *, server: str, port_id) -> bool:
    """
    Return whether a same-name *interface* may stand for LibreNMS *port_id* on *server*.

    A row without a port ID has nothing to contradict. Otherwise the interface must be unbound on
    the server or bound to that port, so a name never wins over a binding to a different port. A
    malformed binding never counts as unbound.
    """
    requested_id = coerce_librenms_id(port_id)
    if requested_id is None:
        return True
    if not is_server_key(server):
        return False
    mapping = read_mapping(interface)
    if mapping.container is ContainerStatus.ABSENT:
        return True
    if mapping.container is ContainerStatus.SCOPED and mapping.server(server) is None:
        return True
    return mapping.own_id(server) == requested_id


def mapped_device_servers(subject, *, active_server: str | None = None) -> tuple[str, ...]:
    """
    Return the sorted server keys that the subject or its virtual chassis is linked to.

    A server counts when an entry resolves an own or OOB ID, or holds an effective migration
    marker. A legacy value counts only for *active_server*, the server the caller acts on.
    """
    members = [subject]
    virtual_chassis = getattr(subject, "virtual_chassis", None)
    if virtual_chassis is not None:
        members = list(virtual_chassis.members.all())
    mappings = [read_mapping(member) for member in members]

    server_keys = {
        entry.server
        for mapping in mappings
        for entry in mapping.servers
        if entry.display_id is not None or entry.migration.effective is not None
    }
    if (
        active_server
        and is_server_key(active_server)
        and active_server not in server_keys
        and any(mapping.own_id(active_server) is not None for mapping in mappings)
    ):
        server_keys.add(active_server)
    return tuple(sorted(server_keys))


def get_librenms_sync_device(device, server_key: str | None = None):  # noqa: C901
    """
    Determine which Virtual Chassis member should handle LibreNMS sync operations.

    LibreNMS treats a Virtual Chassis as a single logical device, so only one member
    should have the librenms_id custom field set and be used for sync operations.

    Priority order for selecting the sync device:
    1. Any member with librenms_id custom field set for *server_key* (highest priority).
       When *server_key* is None, matches any member that has any librenms_id set.
    2. Master device with primary IP (if master is designated)
    3. Any member with primary IP (fallback when no master or master lacks IP)
    4. Member with lowest vc_position (for error messages when no IPs configured)

    Args:
        device (Device): Any device in the virtual chassis.
        server_key: LibreNMS server key used to resolve the correct librenms_id mapping.
                    Pass None to match any member that has any librenms_id (e.g. in
                    contexts where the active server is not known, such as table columns).

    Returns:
        Optional[Device]: The device that should handle LibreNMS sync, or None if
                         the device is not in a virtual chassis.

    """
    if server_key is not None:
        server_key = require_server_key(server_key)

    if not hasattr(device, "virtual_chassis") or not device.virtual_chassis:
        return device

    vc = device.virtual_chassis
    all_members = vc.members.all()
    mappings = [(member, read_mapping(member)) for member in all_members]

    if server_key is not None:
        # Priority 1: a member with a real host id for server_key (a scoped mapping is
        # preferred over a legacy bare value below).
        for member, mapping in mappings:
            entry = mapping.server(server_key)
            if entry is not None and entry.own_id is not None:
                return member

        # Priority 1b (legacy fallback): any member whose host id resolves for this server
        # (includes bare legacy IDs that are a universal fallback).
        for member, mapping in mappings:
            if mapping.own_id(server_key):
                return member

        # Priority 1c: OOB-only mapping for this server — a member linked only as an OOB
        # controller. Evaluated LAST so a member holding the real host id always wins; without
        # this pass an OOB-only member would fall through to the master/primary-IP fallback.
        for member, mapping in mappings:
            entry = mapping.server(server_key)
            if entry is not None and entry.oob_id is not None:
                return member
    else:
        # server_key is None (e.g. table columns without an active server): prefer a member
        # with any host id on any server, then fall back to any OOB-only linkage. A bare legacy
        # value counts only under the strict rule here.
        for member, mapping in mappings:
            if mapping.legacy.queryable_id is not None or any(entry.own_id is not None for entry in mapping.servers):
                return member
        for member, mapping in mappings:
            if any(entry.oob_id is not None for entry in mapping.servers):
                return member

    # Priority 2: Use master device if it has primary IP
    if vc.master and vc.master.primary_ip:
        return vc.master

    # Priority 3: Find any member with primary IP
    for member in all_members:
        if member.primary_ip:
            return member

    # Priority 4: Use member with lowest vc_position as fallback
    try:
        return min(all_members, key=lambda m: m.vc_position, default=None)
    except (ValueError, TypeError):
        return None


class SameServerIdentityConflict(ValueError):
    """The active server already maps this object to a different LibreNMS host ID."""

    def __init__(self, server_key: str, current_host_id: int, proposed_host_id: int):
        self.server_key = server_key
        self.current_host_id = current_host_id
        self.proposed_host_id = proposed_host_id
        super().__init__(
            f"LibreNMS server '{server_key}' is already mapped to host ID {current_host_id}. "
            f"Replacing it with {proposed_host_id} requires the separate replacement confirmation."
        )


class StaleIdentityReplacement(ValueError):
    """A confirmed replacement no longer matches the object's current mapping."""

    def __init__(self, server_key: str, current_host_id, expected_host_id: int):
        self.server_key = server_key
        self.current_host_id = current_host_id
        self.expected_host_id = expected_host_id
        current = "no host ID" if current_host_id is None else f"host ID {current_host_id}"
        super().__init__(
            f"The replacement confirmation no longer matches the current mapping: server "
            f"'{server_key}' now has {current}, not host ID {expected_host_id}. "
            "Re-run the action to get a fresh confirmation."
        )


# The write side. Every builder returns a change and never mutates, queries or saves.

IDENTITY_BUSY_MESSAGE = "Another operation is assigning this LibreNMS identity. Refresh and try again."
_DEVICE_CLAIM_IDENTITY = "netbox-librenms-plugin:librenms-id:{server}:{identity}"
_STORAGE_FIELDS = frozenset({_STORAGE_FIELD})


class IdentityBusy(TransactionConflict):
    """Another open transaction holds the claim on this LibreNMS identity; the claim does not wait."""

    def __init__(self):
        super().__init__(IDENTITY_BUSY_MESSAGE)


class IdentityOwned(ValueError):
    """
    Another object owns the LibreNMS identity.

    ``owner`` comes from an unrestricted search. It is evidence for a message that checks the
    viewer's scope, so the exception text never names it.
    """

    def __init__(self, owner, identity, server_key):
        self.owner = owner
        self.identity = identity
        self.server_key = server_key
        super().__init__(f"LibreNMS ID {identity} is already assigned to another NetBox object.")


class MappingChanged(TransactionConflict):
    """The row's mapping is not the mapping that the change was built on."""


class LibreNMSPortBindingConflict(ValueError):
    """Another NetBox interface holds the LibreNMS port, or more than one does."""


class LibreNMSPortBindingBusy(TransactionConflict):
    """Another open transaction holds the claim on this LibreNMS port; the claim does not wait."""

    def __init__(self):
        super().__init__("Another operation is binding this LibreNMS port. Refresh and try again.")


class ChangeOutcome(StrEnum):
    """What a builder did. A skip changes nothing, so the writer's other fields can still save."""

    APPLIED = "applied"
    UNCHANGED = "unchanged"
    SKIPPED_INVALID_ID = "skipped_invalid_id"
    SKIPPED_LEGACY = "skipped_legacy"


@dataclass(frozen=True)
class _Claim:
    space: IdentitySpace
    server: str
    identity: int


@dataclass(frozen=True, eq=False)
class MappingChange:
    """
    One intended change of one object's mapping.

    A change is intent, never proof of a held claim: a savepoint rollback releases the claim, and
    the change stays. :func:`persist_mapping` claims again before it writes.
    """

    before: MappingState
    after: MappingState
    outcome: ChangeOutcome
    changed: bool
    _model: type = field(repr=False)
    _pk: object = field(repr=False)
    _unsaved: object = field(repr=False)
    _stored: object = field(repr=False)
    _claims: tuple = field(repr=False)
    _group: object = field(default=None, repr=False)


@dataclass(frozen=True, eq=False)
class MergeChange:
    """The winner and donor changes of one merge. Only :func:`persist_merge` persists them, together."""

    summary: dict
    winner: MappingChange
    donor: MappingChange

    def change_for(self, row) -> MappingChange | None:
        """Return the change of the merge side that *row* is, or None."""
        return next((side for side in (self.winner, self.donor) if _is_target(row, side)), None)


def _stored_value(obj):
    _space_of(type(obj))
    return obj.custom_field_data.get(_MAPPING_KEY)


def _put_stored_value(obj, value) -> frozenset:
    """Set a copy of the raw stored *value* on *obj* with no check; return the storage fields a save writes."""
    _space_of(type(obj))
    obj.custom_field_data[_MAPPING_KEY] = copy.deepcopy(value)
    return _STORAGE_FIELDS


def _change(obj, stored_after, outcome=None, *, claims=(), group=None) -> MappingChange:
    stored_before = _stored_value(obj)
    changed = stored_after != stored_before
    if outcome is None:
        outcome = ChangeOutcome.APPLIED if changed else ChangeOutcome.UNCHANGED
    return MappingChange(
        before=_decode_stored_mapping(stored_before),
        after=_decode_stored_mapping(stored_after),
        outcome=outcome,
        changed=changed,
        _model=type(obj),
        _pk=obj.pk,
        _unsaved=obj if obj.pk is None else None,
        _stored=copy.deepcopy(stored_after),
        _claims=tuple(claims),
        _group=group,
    )


def _claim_for(obj, server_key, identity) -> tuple[_Claim, ...]:
    normalized = coerce_librenms_id(identity)
    if normalized is None:
        return ()
    return (_Claim(_space_of(type(obj)), server_key, normalized),)


def _with_own_id(stored, server_key, identity, obj):
    """Return the stored value with *identity* as the own ID on *server_key*, and the outcome."""
    if isinstance(identity, bool):
        logger.warning("librenms_id device_id is a boolean (%r) on %r; not storing.", identity, obj)
        return stored, ChangeOutcome.SKIPPED_INVALID_ID
    value = copy.deepcopy(stored) or {}
    if _readable_legacy_id(value) is not None:
        # A legacy value resolves on every server, so a scoped write would silently migrate it.
        logger.warning(
            "librenms_id on %r has legacy bare integer %r; skipping write to prevent "
            "silent migration. Use the migration workflow to convert.",
            obj,
            value,
        )
        return stored, ChangeOutcome.SKIPPED_LEGACY
    if isinstance(value, str):
        logger.warning("librenms_id custom field has unexpected string %r on %r; resetting to empty dict.", value, obj)
        value = {}
    elif not isinstance(value, dict):
        logger.warning(
            "librenms_id custom field has unexpected type %s on %r; resetting to empty dict.", type(value).__name__, obj
        )
        value = {}
    int_id = coerce_librenms_id(identity)
    if int_id is None:
        logger.warning("librenms_id device_id %r is not a valid positive integer on %r; not storing.", identity, obj)
        return stored, ChangeOutcome.SKIPPED_INVALID_ID
    existing_entry = value.get(server_key)
    if isinstance(existing_entry, dict) and "oob" in existing_entry:
        value[server_key] = {"id": int_id, "oob": existing_entry["oob"]}
    else:
        value[server_key] = int_id
    return value, (ChangeOutcome.APPLIED if value != stored else ChangeOutcome.UNCHANGED)


def assign_own(obj, server: str, identity) -> MappingChange:
    """
    Build the change that maps *obj* to its own *identity* on *server*.

    The general setter. An OOB entry on the server stays. A legacy value is not migrated
    (``SKIPPED_LEGACY``), and an identity that is not a positive ID is not stored
    (``SKIPPED_INVALID_ID``). A corrupt stored value is replaced.
    """
    server = require_server_key(server)
    stored, outcome = _with_own_id(_stored_value(obj), server, identity, obj)
    # A skip still claims a valid ID: the owner check runs whether or not the mapping changes.
    return _change(obj, stored, outcome, claims=_claim_for(obj, server, identity))


def link_import(
    obj, server: str, identity, *, configured_servers: Iterable[str], confirmed_replacement_of: int | None = None
) -> MappingChange:
    """
    Build the change that adds one import mapping and keeps the object's established server choice.

    When the change adds a second usable mapping and the object stores no preference, the previous
    sole mapping becomes preferred.

    Raises:
        ValueError: The identity is not a positive ID, or the stored value is legacy or invalid.
        SameServerIdentityConflict: An unconfirmed change would replace a different host ID.
        StaleIdentityReplacement: *confirmed_replacement_of* is not the current host ID.

    """
    server = require_server_key(server)
    normalized_id = coerce_librenms_id(identity)
    if normalized_id is None:
        raise ValueError("LibreNMS device ID must be a positive integer.")
    stored = _stored_value(obj)
    current = _decode_stored_mapping(stored)
    if current.container is ContainerStatus.LEGACY:
        raise ValueError("Convert the legacy LibreNMS mapping before adding another server.")
    if current.container is ContainerStatus.INVALID:
        raise ValueError("The existing LibreNMS mapping has an invalid format.")

    existing_entry = current.server(server)
    existing_host_id = existing_entry.own_id if existing_entry is not None else None
    # The confirmation carries the host ID the user was shown, so a replay or a later change fails.
    if confirmed_replacement_of is not None:
        if existing_host_id != confirmed_replacement_of:
            raise StaleIdentityReplacement(server, existing_host_id, confirmed_replacement_of)
    elif existing_host_id is not None and existing_host_id != normalized_id:
        raise SameServerIdentityConflict(server, existing_host_id, normalized_id)

    configured_keys = set(configured_servers)
    previous_usable_keys = [
        entry.server for entry in current.servers if entry.server in configured_keys and entry.display_id is not None
    ]
    value, _outcome = _with_own_id(stored, server, normalized_id, obj)
    if server not in previous_usable_keys and len(previous_usable_keys) == 1 and PREFERRED_SERVER_FIELD not in value:
        value[PREFERRED_SERVER_FIELD] = previous_usable_keys[0]
    return _change(obj, value, claims=_claim_for(obj, server, normalized_id))


def _normalized_oob_type(oob_type) -> str:
    candidate = (oob_type or "").strip().lower()
    if candidate == "oob":
        # The generic sentinel: an OOB relationship whose controller type is unknown.
        return "oob"
    if not (match := OOB_TYPE_PATTERN.search(candidate)):
        raise ValueError(f"oob_type {oob_type!r} does not match any known OOB type {OOB_TYPES}")
    return match.group(1).lower()


def _with_oob(stored, server_key, oob_id, oob_type, obj):
    """Return the stored value with an OOB entry on *server_key*; fail closed on a corrupt host ID."""
    normalized_type = _normalized_oob_type(oob_type)
    if isinstance(oob_id, bool) or not isinstance(oob_id, (int, str)) or coerce_librenms_id(oob_id) is None:
        raise ValueError(f"oob_device_id must be a positive integer, got {oob_id!r}")
    value = copy.deepcopy(stored) or {}
    if not isinstance(value, dict):
        # A legacy value is this server's host ID; the OOB attach promotes it to the scoped form.
        legacy_id = coerce_librenms_id(value)
        if legacy_id is None and str(value).strip():
            raise ValueError(f"Cannot attach OOB: legacy librenms_id on {obj!r} is not a valid id: {value!r}")
        value = {server_key: legacy_id} if legacy_id is not None else {}

    entry = value.get(server_key)
    if isinstance(entry, int) and not isinstance(entry, bool):
        coerced = coerce_librenms_id(entry)
        if coerced is None:
            raise ValueError(f"Cannot attach OOB: stored librenms_id for {server_key!r} is not a valid id: {entry!r}")
        entry = {"id": coerced}
    elif isinstance(entry, str):
        coerced = coerce_librenms_id(entry)
        if coerced:
            entry = {"id": coerced}
        elif entry.strip():
            raise ValueError(f"Cannot attach OOB: stored librenms_id for {server_key!r} is not a valid id: {entry!r}")
        else:
            entry = {}
    elif isinstance(entry, dict):
        host_id = entry.get("id")
        if host_id is not None and coerce_librenms_id(host_id) is None and str(host_id).strip():
            raise ValueError(
                f"Cannot attach OOB: stored librenms_id host id for {server_key!r} is not a valid id: {host_id!r}"
            )
    else:
        entry = {}

    # Only the identity essentials: the controller's IP and version belong to NetBox and LibreNMS.
    entry["oob"] = {"id": coerce_librenms_id(oob_id), "type": normalized_type}
    value[server_key] = entry
    return value


def attach_oob(obj, server: str, identity, *, oob_type: str) -> MappingChange:
    """
    Build the change that attaches the OOB controller *identity* to *obj* on *server*.

    *oob_type* is a known OOB type or the generic ``"oob"``. A bare host ID becomes the
    ``{"id": N, "oob": {...}}`` form.

    Raises:
        ValueError: The type or the ID is invalid, or the stored host ID is corrupt.

    """
    server = require_server_key(server)
    value = _with_oob(_stored_value(obj), server, identity, oob_type, obj)
    return _change(obj, value, claims=_claim_for(obj, server, identity))


def clear_oob(obj, server: str) -> MappingChange:
    """Build the change that removes the OOB entry on *server*; the host entry keeps its object form."""
    server = require_server_key(server)
    stored = _stored_value(obj)
    if not isinstance(stored, dict) or not isinstance(stored.get(server), dict):
        return _change(obj, stored)
    value = copy.deepcopy(stored)
    value[server].pop("oob", None)
    return _change(obj, value)


def convert_legacy(obj, server: str) -> MappingChange:
    """
    Build the change that scopes a legacy value to *server*.

    It reads the value under the wide legacy rule, so every value that the reader shows as legacy
    converts. The change claims the identity, so an owner of either model refuses it at persistence.
    A value that is not legacy gives an unchanged change.
    """
    server = require_server_key(server)
    stored = _stored_value(obj)
    legacy_id = _readable_legacy_id(stored)
    if legacy_id is None:
        return _change(obj, stored)
    return _change(obj, {server: legacy_id}, claims=_claim_for(obj, server, legacy_id))


def release_server(obj, server: str, *, clear_preference: bool) -> MappingChange:
    """
    Build the change that removes the mapping on *server* and a preference that names it.

    *clear_preference* also removes any other preference; selection policy decides it. A mapping
    with no server entry left is removed.
    """
    server = require_server_key(server)
    stored = _stored_value(obj)
    value = copy.deepcopy(stored) if isinstance(stored, dict) else {}
    value.pop(server, None)
    if clear_preference or value.get(PREFERRED_SERVER_FIELD) == server:
        value.pop(PREFERRED_SERVER_FIELD, None)
    return _change(obj, value if _decode_stored_mapping(value).servers else None)


def set_preference(obj, server: str) -> MappingChange:
    """
    Build the change that stores *server* as the preferred server; eligibility is selection policy.

    Raises:
        ValueError: The object has no server-scoped mapping.

    """
    server = require_server_key(server)
    stored = _stored_value(obj)
    if not isinstance(stored, dict):
        raise ValueError("Object does not have a server-scoped LibreNMS mapping.")
    value = copy.deepcopy(stored)
    value[PREFERRED_SERVER_FIELD] = server
    return _change(obj, value)


def promote_to_host(obj, server: str, new_host, *, expected_host, oob_type: str) -> MappingChange:
    """
    Build the change that makes *new_host* the host and moves the old host *expected_host* to the OOB entry.

    Raises:
        StaleIdentityReplacement: The host on *server* is not *expected_host*.
        ValueError: The server already has an OOB entry, or the OOB data is invalid.

    """
    server = require_server_key(server)
    stored = _stored_value(obj)
    current = _decode_stored_mapping(stored)
    expected_id = coerce_librenms_id(expected_host)
    if current.own_id(server) != expected_id:
        raise StaleIdentityReplacement(server, current.own_id(server), expected_id)
    if current.has_oob(server):
        raise ValueError("The server already has an OOB link.")
    value, _outcome = _with_own_id(stored, server, new_host, obj)
    value = _with_oob(value, server, expected_host, oob_type, obj)
    return _change(obj, value, claims=_claim_for(obj, server, new_host))


def _donor_label(donor):
    return getattr(donor, "name", donor)


def _with_migration_marker(stored, winner_pk, server_key, at, donor):
    """Return the donor's stored value with its links on *server_key* replaced by the migration marker."""
    from datetime import datetime, timezone

    # bool is an int subclass; a marker must never target the wrong device.
    if isinstance(winner_pk, bool) or not isinstance(winner_pk, int) or winner_pk <= 0:
        raise ValueError(f"winner_pk must be a positive integer, got {winner_pk!r}")
    value = copy.deepcopy(stored)
    if value is None:
        value = {}
    elif not isinstance(value, dict):
        # A legacy or corrupt value is still resolvable; a marker over it would lose the mapping.
        raise ValueError(
            f"Cannot mark '{_donor_label(donor)}' migrated: librenms_id is a legacy "
            f"bare-integer or corrupt value ({value!r}); migrate it to the dict form first."
        )
    entry = value.get(server_key)
    if isinstance(entry, int) and not isinstance(entry, bool):
        entry = {"id": entry}
    elif isinstance(entry, str):
        coerced = coerce_librenms_id(entry)
        if coerced is None and entry.strip():
            raise ValueError(
                f"Cannot mark '{_donor_label(donor)}' migrated: "
                f"librenms_id[{server_key!r}] is unparseable ({entry!r}); migrate it first."
            )
        entry = {"id": coerced} if coerced else {}
    elif isinstance(entry, dict):
        _require_parseable(entry.get("id"), donor, server_key, "id")
        raw_oob = entry.get("oob")
        if raw_oob is not None:
            if not isinstance(raw_oob, dict):
                raise ValueError(
                    f"Cannot mark '{_donor_label(donor)}' migrated: "
                    f"librenms_id[{server_key!r}] has unsupported oob type {type(raw_oob).__name__}."
                )
            _require_parseable(raw_oob.get("id"), donor, server_key, "oob id")
    elif entry is None:
        entry = {}
    else:
        raise ValueError(
            f"Cannot mark '{_donor_label(donor)}' migrated: "
            f"librenms_id[{server_key!r}] has unsupported type {type(entry).__name__}."
        )
    entry.pop("id", None)
    entry.pop("oob", None)
    entry[MIGRATED_TO_FIELD] = {
        "device_id": int(winner_pk),
        "server_key": server_key,
        "at": at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    value[server_key] = entry
    return value


def _require_parseable(raw_id, donor, server_key, kind):
    """Refuse a non-blank ID that is not a positive integer: it is a recoverable link, not "no link"."""
    if isinstance(raw_id, str) and not raw_id.strip():
        raw_id = None
    if raw_id is not None and coerce_librenms_id(raw_id) is None:
        raise ValueError(
            f"Cannot mark '{_donor_label(donor)}' migrated: "
            f"librenms_id[{server_key!r}] has an unparseable {kind} ({raw_id!r}); migrate it first."
        )


def mark_migrated(donor, winner_pk: int, server: str, *, at: str | None = None) -> MappingChange:
    """
    Build the change that releases *donor*'s links on *server* and marks it migrated to *winner_pk*.

    The marker holds the winner's pk, the server and an aware UTC timestamp (or *at*).

    Raises:
        ValueError: *winner_pk* is not a positive integer, or the donor's stored value is legacy or corrupt.

    """
    server = require_server_key(server or "default")
    value = _with_migration_marker(_stored_value(donor), winner_pk, server, at, donor)
    return _change(donor, value)


def _normalize_merge_entry(entry, *, owner_label, owner_name, server_key, copy_dict):
    """
    Coerce one side's entry on the merge server to a dict, failing closed on a corrupt shape.

    A bare int (or numeric string) becomes ``{"id": N}``; a blank or None entry becomes ``{}``. A
    non-blank unparseable string, or an unsupported type, is corrupt state and raises ValueError.
    *copy_dict* returns a dict entry as a shallow copy (the winner's entry is changed later).
    """
    if isinstance(entry, int) and not isinstance(entry, bool):
        return {"id": entry}
    if isinstance(entry, str):
        coerced = coerce_librenms_id(entry)
        if coerced is None and entry.strip():
            raise ValueError(
                f"{owner_label} '{owner_name}' has an unparseable librenms_id[{server_key!r}] "
                f"{entry!r} — expected a positive integer or numeric string."
            )
        return {"id": coerced} if coerced else {}
    if isinstance(entry, dict):
        return dict(entry) if copy_dict else entry
    if entry is None:
        return {}
    raise ValueError(
        f"{owner_label} '{owner_name}' has an unsupported librenms_id[{server_key!r}] of type "
        f"{type(entry).__name__} — expected a positive integer, numeric string, or mapping."
    )


def _coerce_link_id_or_raise(raw, *, owner, name, server_key, kind):
    """Coerce a host or OOB ID to a positive int; a blank value is absent, a non-blank bad one raises."""
    if isinstance(raw, str) and not raw.strip():
        raw = None
    coerced = coerce_librenms_id(raw) if raw is not None else None
    if raw is not None and coerced is None:
        raise ValueError(
            f"{owner} '{name}' has an unparseable librenms_id[{server_key!r}] {kind} "
            f"{raw!r} — expected a positive integer or numeric string."
        )
    return coerced


def _extract_oob_entry(owner_label, owner_name, entry, server_key):
    # A non-dict, non-null OOB is corrupt state, not "no OOB link".
    raw_oob = entry.get("oob")
    if raw_oob is None or isinstance(raw_oob, dict):
        return raw_oob
    raise ValueError(
        f"{owner_label} '{owner_name}' has an unsupported librenms_id[{server_key!r}] oob shape "
        f"{type(raw_oob).__name__} — expected a mapping or null."
    )


def _merged_winner_value(winner_stored, donor_stored, winner, donor, server_key):
    """
    Return the winner's stored value with the donor's links merged in, and the summary.

    Winner wins on a filled slot. A winner without a host ID takes the donor's. A winner with a host
    ID takes a different donor host ID into its free OOB slot. A winner without an OOB takes the
    donor's. A merge that would lose a donor link refuses.
    """
    summary = {"host_id_from_donor": None, "oob_from_donor": None, "donor_id_demoted_to_oob": None}
    # Only an absent value is "no link"; a falsy corrupt value (False, 0) must fail closed below.
    winner_cf = {} if winner_stored is None else copy.deepcopy(winner_stored)
    donor_cf = {} if donor_stored is None else donor_stored
    if not isinstance(winner_cf, dict) or not isinstance(donor_cf, dict):
        raise ValueError("Cannot merge: one or both devices have a legacy bare-integer or corrupt librenms_id.")

    winner_entry = _normalize_merge_entry(
        winner_cf.get(server_key), owner_label="winner", owner_name=winner.name, server_key=server_key, copy_dict=True
    )
    donor_entry = _normalize_merge_entry(
        donor_cf.get(server_key), owner_label="donor", owner_name=donor.name, server_key=server_key, copy_dict=False
    )
    donor_oob = _extract_oob_entry("donor", donor.name, donor_entry, server_key)
    # Coerce both IDs first, so a malformed but truthy winner ID never takes the demote path.
    winner_id = _coerce_link_id_or_raise(
        winner_entry.get("id"), owner="winner", name=winner.name, server_key=server_key, kind="id"
    )
    donor_id = _coerce_link_id_or_raise(
        donor_entry.get("id"), owner="donor", name=donor.name, server_key=server_key, kind="id"
    )
    winner_oob = _extract_oob_entry("winner", winner.name, winner_entry, server_key)
    # A corrupt winner OOB ID would look like an occupied slot and lose the donor's controller.
    if winner_oob is not None:
        _coerce_link_id_or_raise(
            winner_oob.get("id"), owner="winner", name=winner.name, server_key=server_key, kind="oob id"
        )

    # A metadata-only donor OOB (no usable ID) is not a real controller link.
    donor_oob_has_valid_id = False
    donor_oob_id = None
    if winner_oob is None and donor_oob is not None:
        donor_oob_id = _coerce_link_id_or_raise(
            donor_oob.get("id"), owner="donor", name=donor.name, server_key=server_key, kind="oob id"
        )
        donor_oob_has_valid_id = donor_oob_id is not None

    # The winner has one free slot and the donor two distinct links: either choice orphans one.
    if winner_id is not None and donor_id is not None and donor_id != winner_id and donor_oob_has_valid_id:
        raise ValueError(
            f"Cannot merge: donor '{donor.name}' has two distinct LibreNMS links but winner "
            f"'{winner.name}' has only one free link slot. Unlink one donor link first."
        )
    # Both winner slots are full, so a distinct donor host ID has nowhere to go.
    if winner_id is not None and donor_id is not None and donor_id != winner_id and winner_oob is not None:
        raise ValueError(
            f"Cannot merge: winner '{winner.name}' already holds both a LibreNMS host id and an "
            f"OOB link, so donor '{donor.name}' host id {donor_id} has nowhere to move. "
            "Unlink one side first."
        )

    if winner_id is None and donor_id is not None:
        winner_entry["id"] = donor_id
        summary["host_id_from_donor"] = donor_id
    elif (
        winner_id is not None
        and donor_id is not None
        and donor_id != winner_id
        and winner_oob is None
        and not donor_oob_has_valid_id
    ):
        # Demote the donor's host ID into the free OOB slot, and keep a metadata-only donor OOB's type.
        demoted = dict(donor_oob) if donor_oob else {}
        demoted.pop("id", None)
        if not demoted.get("type"):
            # A vendor token wins over the generic 'oob', as in the import's OOB detection.
            demoted["type"] = normalize_oob_type(donor.name or "", "") or "oob"
        demoted["id"] = donor_id
        winner_entry["oob"] = demoted
        summary["donor_id_demoted_to_oob"] = demoted
        winner_oob = demoted

    if donor_oob and winner_oob is None:
        inherited_oob = dict(donor_oob)
        if donor_oob_id is not None:
            inherited_oob["id"] = donor_oob_id
        else:
            inherited_oob.pop("id", None)
        # An empty OOB would read as an occupied slot and block a later demote.
        if inherited_oob:
            winner_entry["oob"] = dict(inherited_oob)
            summary["oob_from_donor"] = dict(inherited_oob)

    winner_cf[server_key] = winner_entry
    return winner_cf, summary


def merge_links(winner, donor, server: str, *, at: str | None = None) -> MergeChange:
    """
    Build the merge of *donor*'s links on *server* into *winner*, and the donor's migration marker.

    The two sides persist only together, through :func:`persist_merge`. A merge moves identities
    between its two locked rows and binds no new one, so it claims nothing.

    Raises:
        ValueError: A side holds a legacy or corrupt value, or the merge would lose a donor link.

    """
    server = require_server_key(server)
    winner_value, summary = _merged_winner_value(_stored_value(winner), _stored_value(donor), winner, donor, server)
    donor_value = _with_migration_marker(_stored_value(donor), winner.pk, server, at, donor)
    group = object()
    return MergeChange(
        summary=summary,
        winner=_change(winner, winner_value, group=group),
        donor=_change(donor, donor_value, group=group),
    )


# Claims and persistence.


def _claim_device_identity(server_key, librenms_id) -> None:
    """Claim one device identity until commit, or raise IdentityBusy at once and record it for the runner."""
    from netbox_librenms_plugin.utils import advisory_lock_key

    connection = connections[DEFAULT_DB_ALIAS]
    if not connection.in_atomic_block:
        raise RuntimeError("A LibreNMS identity claim requires an open transaction")
    identity = _DEVICE_CLAIM_IDENTITY.format(server=server_key, identity=librenms_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [advisory_lock_key(identity)])
        acquired = cursor.fetchone()[0]
    if not acquired:
        raise recorded_conflict(IdentityBusy())


def claim_librenms_port_binding(port_id, server_key, *, using=None):
    """
    Claim one cross-model port identity until commit, or refuse without waiting.

    Raises:
        LibreNMSPortBindingBusy: Another open transaction holds the claim. It is recorded for the
            runner's attempt, so a handler that catches it cannot commit the attempt.

    """
    from netbox_librenms_plugin.utils import advisory_lock_key, port_binding_lock_identity

    server_key = require_server_key(server_key)
    port_id = normalize_librenms_port_id(port_id)
    if port_id is None:
        raise ValueError("The LibreNMS port ID is missing or invalid.")
    connection = connections[using or DEFAULT_DB_ALIAS]
    if not connection.in_atomic_block:
        raise RuntimeError("claim_librenms_port_binding() requires an open transaction")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", [advisory_lock_key(port_binding_lock_identity(port_id, server_key))]
        )
        acquired = cursor.fetchone()[0]
    if not acquired:
        raise recorded_conflict(LibreNMSPortBindingBusy())


def _device_identity_owner(server_key, librenms_id, *, exclude_model=None, exclude_pk=None):
    """Return a Device or VM, other than the excluded row, that owns the identity; read in new statements."""
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    for model in (Device, VirtualMachine):
        owner = find_mapping(
            model.objects.all(), server=server_key, identity=librenms_id, roles=(MappingRole.OWN, MappingRole.OOB)
        )
        if owner is not None and not (model is exclude_model and exclude_pk is not None and owner.pk == exclude_pk):
            return owner
    return None


def lock_librenms_id_assignment(librenms_id, server_key: str, *, owner_queryset=None, owner_pk=None):
    """
    Claim a Device or VM LibreNMS ID without a wait, and return any existing owner.

    The caller must hold an open transaction. The claim comes before the optional target row lock,
    and the owner search runs in later statements, so it sees every owner committed before the claim.

    Args:
        librenms_id: Positive LibreNMS device ID.
        server_key: Configured LibreNMS server key.
        owner_queryset: Optional queryset that applies the caller's permission scope.
        owner_pk: Primary key of the existing owner to lock.

    Returns:
        A pair containing the locked owner, when supplied, and a conflicting Device or VM.

    Raises:
        IdentityBusy: Another open transaction holds the claim.
        ValueError: If the ID, server key, or owner arguments are invalid.
        AmbiguousLibreNMSIdError: If more than one existing object owns the ID.

    """
    if (owner_queryset is None) != (owner_pk is None):
        raise ValueError("owner_queryset and owner_pk must be provided together")
    normalized_id = coerce_librenms_id(librenms_id)
    if normalized_id is None:
        raise ValueError(f"librenms_id {librenms_id!r} is not a positive integer")
    normalized_server_key = require_server_key(server_key)

    _claim_device_identity(normalized_server_key, normalized_id)
    locked_owner = None
    owner_model = None
    if owner_queryset is not None:
        owner_model = owner_queryset.model
        locked_owner = owner_queryset.select_for_update(of=("self",)).get(pk=owner_pk)
    conflict = _device_identity_owner(
        normalized_server_key, normalized_id, exclude_model=owner_model, exclude_pk=owner_pk
    )
    return locked_owner, conflict


def _is_target(row, change) -> bool:
    if type(row) is not change._model:
        return False
    if change._pk is None:
        return row is change._unsaved
    return row.pk == change._pk


def _take_claim(claim) -> None:
    if claim.space is IdentitySpace.DEVICE:
        _claim_device_identity(claim.server, claim.identity)
    else:
        claim_librenms_port_binding(claim.identity, claim.server)


def _check_owner(claim, row) -> None:
    if claim.space is IdentitySpace.DEVICE:
        owner = _device_identity_owner(claim.server, claim.identity, exclude_model=type(row), exclude_pk=row.pk)
        if owner is not None:
            raise IdentityOwned(owner, claim.identity, claim.server)
        return
    try:
        owner = find_port_owner(claim.identity, server=claim.server)
    except AmbiguousLibreNMSIdError:
        raise LibreNMSPortBindingConflict("The LibreNMS port ID matches multiple NetBox interfaces.") from None
    if owner is not None and not (type(owner) is type(row) and owner.pk == row.pk):
        raise LibreNMSPortBindingConflict("The LibreNMS port ID is already assigned to another NetBox interface.")


class _MergeProgress:
    def __init__(self, change):
        self.change = change
        self.written = set()


# The merge that persist_merge() runs now; a merge side persists only inside it.
_active_merge = ContextVar("librenms_active_merge", default=None)


def _enter_group(change) -> None:
    if change._group is None:
        return
    progress = _active_merge.get()
    if progress is None or progress.change.winner._group is not change._group:
        raise ValueError("A merge side persists only inside persist_merge() of its own merge.")
    if id(change) in progress.written:
        raise ValueError("A merge side persists only once.")
    progress.written.add(id(change))


def persist_mapping(row, change: MappingChange, *, write: Callable):
    """
    Put *change* on *row* and call ``write(row, fields)`` once, after the claims and the checks.

    *row* is the writer's fresh row (locked, or not saved yet). The steps: the change must belong
    to the row; each identity the change binds is claimed again without a wait; the owners are read
    in later statements; the row's whole mapping must still be the change's ``before`` (another
    custom field never counts). Then only the mapping is set on the row. *fields* names the
    storage the change writes, for a partial save; it is empty when the mapping did not change.

    Returns:
        The value that *write* returns.

    Raises:
        ValueError: The change belongs to another object, or it is a merge side outside its merge.
        IdentityBusy: Another open transaction holds the claim on a device identity.
        LibreNMSPortBindingBusy: Another open transaction holds the claim on a port.
        IdentityOwned: Another Device or VM owns a device identity.
        LibreNMSPortBindingConflict: Another interface holds a port, or more than one does.
        AmbiguousLibreNMSIdError: More than one Device or VM owns a device identity.
        MappingChanged: The row's mapping changed after the change was built.

    """
    if not isinstance(change, MappingChange):
        raise TypeError(f"persist_mapping() takes a MappingChange, not {type(change).__name__}.")
    if not _is_target(row, change):
        raise ValueError("The mapping change belongs to another object.")
    _enter_group(change)
    for claim in change._claims:
        _take_claim(claim)
    # A separate statement: under READ COMMITTED it sees an owner committed before the claim.
    for claim in change._claims:
        _check_owner(claim, row)
    if read_mapping(row) != change.before:
        raise recorded_conflict(
            MappingChanged("The LibreNMS mapping changed after it was read. Refresh and try again.")
        )
    return write(row, _put_on(row, change))


def _put_on(row, change) -> frozenset:
    """Set only the mapping of *change* on *row*, and return the storage fields that a save must write."""
    if not change.changed:
        return frozenset()
    return _put_stored_value(row, change._stored)


def persist_merge(change: MergeChange, *, write: Callable[[], object]):
    """
    Run ``write()``, which saves the merge's rows, in one atomic group.

    Both sides' mappings are checked before the first write. ``write`` saves each touched row once,
    through :func:`persist_mapping` with ``change.change_for(row)`` for the two sides. Any exception
    rolls the whole group back, so the donor never commits without the winner.

    Raises:
        MappingChanged: A side's mapping changed after the merge was built.
        RuntimeError: ``write`` did not persist each side exactly once.

    """
    with transaction.atomic():
        for side in (change.winner, change.donor):
            row = side._model.objects.select_for_update().get(pk=side._pk)
            if read_mapping(row) != side.before:
                raise recorded_conflict(
                    MappingChanged("The LibreNMS mapping changed after it was read. Refresh and try again.")
                )
        progress = _MergeProgress(change)
        token = _active_merge.set(progress)
        try:
            result = write()
        finally:
            _active_merge.reset(token)
        if progress.written != {id(change.winner), id(change.donor)}:
            raise RuntimeError("A merge must save its winner and its donor once each.")
    return result


def can_store_device_mapping(obj) -> bool:
    """Return whether *obj* is a saved Device or VirtualMachine that has the mapping custom field."""
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    return (
        isinstance(obj, (Device, VirtualMachine))
        and obj.pk is not None
        and not obj._state.adding
        and _MAPPING_KEY in obj.cf
    )


def copy_persisted_mapping(source, target) -> None:
    """
    Copy only the mapping of *source*, a later read of the same row, onto *target*; save nothing.

    Target keeps every other custom field, also one it changed and did not save.
    """
    if type(source) is not type(target) or source.pk is None or source.pk != target.pk:
        raise ValueError("A mapping copies only between two reads of one row.")
    _put_stored_value(target, _stored_value(source))
