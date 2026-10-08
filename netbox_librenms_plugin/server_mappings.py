"""
The one module that knows how object server mappings are stored and read.

A mapping links a NetBox object to one LibreNMS identity per server. Device and VirtualMachine
use the device identity space. Interface and VMInterface use the port identity space. The space
comes from the model, so no caller names it. Callers read mappings through :func:`read_mapping`,
:func:`read_mappings` and the lookups below, and never read the stored value themselves.
"""

import copy
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from django.db.models import JSONField, Q

from netbox_librenms_plugin.librenms_ids import coerce_librenms_id, librenms_id_text_pattern

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


def readable_legacy_id(value) -> int | None:
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


def decode_stored_mapping(value) -> MappingState:
    """
    Decode one stored mapping value into a snapshot.

    :func:`read_mapping` uses it, and so do the writers that still edit the stored value, so every
    reader and writer classifies a value with the same rules.
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
    legacy = LegacyState(readable_id=readable_legacy_id(value), queryable_id=coerce_librenms_id(value))
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
    return decode_stored_mapping(obj.custom_field_data.get(_MAPPING_KEY))


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
        mapping = decode_stored_mapping(stored.get(_MAPPING_KEY) if isinstance(stored, dict) else None)
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


def with_preferred_server(value, server_key: str) -> dict:
    """Return a copy of a mapping with only its preference metadata changed."""
    server_key = require_server_key(server_key)
    if not isinstance(value, dict):
        raise ValueError("Object does not have a server-scoped LibreNMS mapping.")
    updated = dict(value)
    updated[PREFERRED_SERVER_FIELD] = server_key
    return updated


def without_server_mapping(value, server_key: str) -> dict:
    """Return a copy without one server identity and its matching preference."""
    server_key = require_server_key(server_key)
    if not isinstance(value, dict):
        return {}
    updated = dict(value)
    updated.pop(server_key, None)
    if updated.get(PREFERRED_SERVER_FIELD) == server_key:
        updated.pop(PREFERRED_SERVER_FIELD, None)
    return updated


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
