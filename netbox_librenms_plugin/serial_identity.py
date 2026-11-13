"""
Hardware serial identity: the one owner of how the plugin reads, compares, finds, groups and stores a serial.

A serial identifies hardware only through its key. The key of a value under a context is the
trimmed value after the serial-scope NormalizationRule chain of that context: the rules of the
context's manufacturer, then the global rules, each ordered by priority and pk. A serial rule
extracts: when its pattern fullmatches the current value, the value becomes the one capture group
that the rule names. So a key is always a contiguous substring of the trimmed value.

A key is Absent when the value is a LibreNMS placeholder (before or after the rules), when a rule
extracts a group that did not take part in the match or is empty, or when the key has no run of
three ASCII letters or digits. An Absent key never identifies hardware.

Contexts: a Device in a Virtual Chassis uses the manufacturer of the chassis master's DeviceType
(its own one without a master); a Module uses the manufacturer of the Device that holds it. Before
the DeviceType of an import is known, the candidates are the global context and each manufacturer
that has serial rules.

A lookup keys each incoming value under every candidate context and reads the candidate rows in one
indexed query: the trimmed stored value equals an incoming value or a key, or contains a key. Each
candidate row is then confirmed under its own context. The count of confirmed rows is read before
any permission filter, so a row that the user cannot see still makes a match Ambiguous.

A writer takes ``assignment_lock`` for the incoming serials of its whole write set, before any row
lock, then locks its rows, resolves again and writes ``stored_form``.
"""

import enum
import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db.models import BooleanField, CharField, F, Func

from netbox_librenms_plugin.constants import is_librenms_placeholder

# The characters that migrations 0018 and 0030 trim in their index expressions. Python's
# str.strip() removes more, so a key (a substring of the Python-trimmed value) is always a
# substring of the database-trimmed value too.
TRIM_CHARACTERS = " \t\n\r\v\f"
_TRIM_LITERAL = "'" + TRIM_CHARACTERS.replace("'", "''") + "'"
# A key without three consecutive ASCII letters or digits gives a LIKE pattern without a trigram.
_IDENTITY_RUN = re.compile(r"[A-Za-z0-9]{3}")
_LOCK_PREFIX = "netbox-librenms-plugin:serial-identity"
# Stop reading candidate rows when every incoming value has this many confirmed matches.
_AMBIGUOUS_AT = 2


class Comparison(enum.Enum):
    """The outcome of comparing two serial values under one context."""

    SAME = "same"
    DIFFERENT = "different"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


class ResolutionKind(enum.Enum):
    """The outcome of a serial lookup."""

    ABSENT = "absent"
    NO_MATCH = "no_match"
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"


UNAVAILABLE_MASTER_LINK = (
    "A virtual chassis names a master that is not one of its members, so its member serials cannot be compared."
)

# The outcomes that refuse a write: another row holds the identity, or the plugin cannot tell.
REFUSING = frozenset({ResolutionKind.UNIQUE, ResolutionKind.AMBIGUOUS, ResolutionKind.UNAVAILABLE})


@dataclass(frozen=True)
class Resolution:
    """
    The rows that hold one incoming serial.

    ``matches`` holds the confirmed rows of a Unique or an Ambiguous outcome. They come from an
    unrestricted read, so a caller names one only after its own permission check. ``object`` is
    the row of a Unique outcome and None for every other outcome. ``complete`` is False when the
    read stopped before it saw every match.
    """

    kind: ResolutionKind
    matches: tuple = ()
    reason: str = ""
    complete: bool = True

    @property
    def object(self):
        """Return the one matching row of a Unique outcome, else None."""
        return self.matches[0] if self.kind is ResolutionKind.UNIQUE else None

    def excluding(self, pks) -> "Resolution":
        """
        Return this outcome without the rows whose pk is in *pks*.

        Raises:
            RuntimeError: The read stopped early and too few rows remain to count; the lookup needs more ``spare``.

        """
        if self.kind not in (ResolutionKind.UNIQUE, ResolutionKind.AMBIGUOUS):
            return self
        excluded = set(pks)
        remaining = tuple(row for row in self.matches if row.pk not in excluded)
        if not self.complete and len(remaining) < _AMBIGUOUS_AT:
            raise RuntimeError("The serial lookup stopped before the excluded rows; give it more spare reads.")
        return _counted(remaining, complete=self.complete)


def _counted(matches: tuple, *, complete: bool = True) -> Resolution:
    if not matches:
        return Resolution(ResolutionKind.NO_MATCH)
    if len(matches) == 1:
        return Resolution(ResolutionKind.UNIQUE, matches)
    return Resolution(ResolutionKind.AMBIGUOUS, matches, complete=complete)


_ABSENT = Resolution(ResolutionKind.ABSENT)


def _text(value) -> str:
    """Return a LibreNMS or NetBox value as trimmed text; LibreNMS sends an all-digit serial as a JSON number."""
    return "" if value is None else str(value).strip()


def _rules_digest(rules) -> str:
    rows = [[rule.pk, rule.manufacturer_id, rule.match_pattern, rule.replacement, rule.priority] for rule in rules]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


class SerialContext:
    """One serial rule chain: the rules of one manufacturer, then the global rules. Callers never read its keys."""

    def __init__(self, manufacturer_id, rules: tuple, *, unavailable: str = ""):
        self.manufacturer_id = manufacturer_id
        self._rules = rules
        self.unavailable = unavailable
        self._keys: dict = {}

    def __repr__(self):
        state = f" unavailable={self.unavailable!r}" if self.unavailable else ""
        return f"<SerialContext manufacturer={self.manufacturer_id}{state}>"

    @property
    def rules_digest(self) -> str:
        """Return a hash of the rule rows of this chain; a rule edit changes it."""
        return _rules_digest(self._rules)

    @property
    def fingerprint(self) -> str:
        """Return a hash of what this context decides with: its chain, its rules and an Unavailable reason."""
        parts = [self.manufacturer_id, self.unavailable, self.rules_digest]
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()

    def _key(self, value) -> str:
        text = _text(value)
        if text in self._keys:
            return self._keys[text]
        key = self._extract(text)
        self._keys[text] = key
        return key

    def _extract(self, text: str) -> str:
        if is_librenms_placeholder(text):
            return ""
        for rule in self._rules:
            # The rule's pattern and group reference raise RuleConfigurationError when they do not work.
            match = rule.compiled_match_pattern.fullmatch(text)
            if match is None:
                continue
            text = match.group(rule.serial_group)
            if not text:
                return ""
        text = text.strip()
        if is_librenms_placeholder(text) or not _IDENTITY_RUN.search(text):
            return ""
        return text


class SerialRules:
    """
    The serial-scope NormalizationRules of one request or batch, read once.

    A context checks its rules when it is first used. A rule whose pattern does not compile, or
    whose replacement is not one group reference that the pattern defines, raises
    RuleConfigurationError, so every decision that uses its chain fails closed.
    """

    def __init__(self, rules: Iterable | None = ()):
        self._rows = None if rules is None else list(rules)
        self._chains = None
        self._contexts: dict = {}

    @classmethod
    def load(cls) -> "SerialRules":
        """Return the serial-scope NormalizationRules, read in one query when a decision first needs them."""
        return cls(None)

    def _chains_by_manufacturer(self) -> dict:
        if self._chains is None:
            if self._rows is None:
                from netbox_librenms_plugin.models import NormalizationRule

                self._rows = list(NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL))
            chains: dict = {}
            for rule in sorted(self._rows, key=lambda rule: (rule.priority, rule.pk)):
                chains.setdefault(rule.manufacturer_id, []).append(rule)
            self._chains = chains
        return self._chains

    @property
    def digest(self) -> str:
        """Return a hash of every serial rule row; a rule edit changes it, so a cached derived result keys on it."""
        return _rules_digest(rule for chain in self._chains_by_manufacturer().values() for rule in chain)

    def context(self, manufacturer_id) -> SerialContext:
        """Return the chain of *manufacturer_id*; a manufacturer without serial rules uses the global chain."""
        chains = self._chains_by_manufacturer()
        context_id = manufacturer_id if manufacturer_id is not None and manufacturer_id in chains else None
        if context_id not in self._contexts:
            rules = (*(chains.get(context_id, ()) if context_id is not None else ()), *chains.get(None, ()))
            for rule in rules:
                _ = rule.compiled_match_pattern, rule.serial_group
            self._contexts[context_id] = SerialContext(context_id, rules)
        return self._contexts[context_id]

    def candidates(self) -> tuple:
        """Return the global context and the context of each manufacturer that has serial rules."""
        scoped = sorted(manufacturer_id for manufacturer_id in self._chains_by_manufacturer() if manufacturer_id)
        return (self.context(None), *(self.context(manufacturer_id) for manufacturer_id in scoped))

    def for_device_type(self, device_type) -> SerialContext:
        """Return the context that a Device of *device_type* uses as a chassis master; None gives the global context."""
        return self.context(getattr(device_type, "manufacturer_id", None))

    def for_module_target(self, device) -> SerialContext:
        """Return the context of a Module that *device* holds: the manufacturer of the device's own type."""
        return self.context(device.device_type.manufacturer_id)

    def for_device(self, device) -> SerialContext:
        """
        Return the context of *device*: its Virtual Chassis master's manufacturer, else its own.

        A master link that names a device outside the chassis gives an Unavailable context.
        """
        chassis_id = device.virtual_chassis_id
        if chassis_id is None:
            return self.for_module_target(device)
        chassis = device.virtual_chassis
        if chassis.master_id is None:
            return self.for_module_target(device)
        master = device if chassis.master_id == device.pk else chassis.master
        if master.virtual_chassis_id != chassis_id:
            # No identity in the text: it can reach a viewer who may not see the chassis.
            return SerialContext(None, (), unavailable=UNAVAILABLE_MASTER_LINK)
        return self.context(master.device_type.manufacturer_id)


def plain_context() -> SerialContext:
    """Return a context without rules, for stored recordings: only trimming, placeholders and admission apply."""
    return SerialContext(None, ())


def compare(stored, incoming, context: SerialContext) -> Comparison:
    """Compare two serial values under *context*; Absent when either value has no identity."""
    if context.unavailable:
        return Comparison.UNAVAILABLE
    stored_key = context._key(stored)
    incoming_key = context._key(incoming)
    if not (stored_key and incoming_key):
        return Comparison.ABSENT
    return Comparison.SAME if stored_key == incoming_key else Comparison.DIFFERENT


def is_identity(value, context: SerialContext) -> bool:
    """Return whether *value* identifies hardware under *context*."""
    return not context.unavailable and bool(context._key(value))


def stored_form_differs(stored, incoming, context: SerialContext) -> bool:
    """Return whether *stored* names the hardware of *incoming* but is not the form a writer stores for it."""
    return compare(stored, incoming, context) is Comparison.SAME and _text(stored) != display(incoming)


def display(value) -> str:
    """Return the source text of a serial for a page: trimmed, and "" for a placeholder."""
    text = _text(value)
    return "" if is_librenms_placeholder(text) else text


def stored_form(value, model) -> str:
    """
    Return the text that a plugin writer stores in ``model.serial`` for the LibreNMS *value*.

    Raises:
        ValidationError: The text is longer than the field; the writer refuses rather than cut it.

    """
    text = display(value)
    max_length = model._meta.get_field("serial").max_length
    if max_length is not None and len(text) > max_length:
        raise ValidationError(
            {"serial": f"The LibreNMS serial has {len(text)} characters; the field holds at most {max_length}."}
        )
    return text


def naming_token(value, context: SerialContext) -> str:
    """Return the text that a ``{serial}`` name placeholder shows for *value*, or "" without an identity."""
    return "" if context.unavailable else context._key(value)


def anonymize_input(value, context: SerialContext) -> str:
    """Return the text that the anonymizer hashes for *value*, or "" when *value* has no identity."""
    return "" if context.unavailable else context._key(value)


def fingerprint(values: Iterable, context: SerialContext) -> str:
    """Return an opaque fingerprint of the identities in *values*, or "" when none has one."""
    keys = sorted({key for value in values if (key := naming_token(value, context))})
    return hashlib.sha256(json.dumps(keys).encode()).hexdigest() if keys else ""


def freshness_token(stored, context: SerialContext) -> int:
    """Return a positive int that a module grant signs: the stored text, its context and that context's rules."""
    payload = json.dumps([_text(stored), context.manufacturer_id, context.rules_digest], separators=(",", ":"))
    return int.from_bytes(hashlib.sha256(payload.encode()).digest()[:7], "big") + 1


@dataclass
class SerialGroups:
    """Observations grouped by identity under one context; a group of two or more is kept, never collapsed."""

    context: SerialContext
    _groups: dict = field(default_factory=dict)
    absent: list = field(default_factory=list)

    def add(self, value, item) -> None:
        """Add *item*, observed with serial *value*."""
        key = "" if self.context.unavailable else self.context._key(value)
        if key:
            self._groups.setdefault(key, []).append(item)
        else:
            self.absent.append(item)

    def find(self, value) -> tuple:
        """Return the items whose serial names the hardware of *value*; () without an identity."""
        key = "" if self.context.unavailable else self.context._key(value)
        return tuple(self._groups.get(key, ())) if key else ()

    def __iter__(self):
        return (tuple(items) for items in self._groups.values())

    def __len__(self):
        return len(self._groups)


def group(observations: Iterable, context: SerialContext, *, serial: Callable = lambda item: item) -> SerialGroups:
    """Group *observations* by the identity of ``serial(observation)`` under *context*."""
    groups = SerialGroups(context)
    for observation in observations:
        groups.add(serial(observation), observation)
    return groups


def trimmed_serial(field_name: str = "serial") -> Func:
    """Return the trimmed serial expression that the trigram and exact indexes of migrations 0018 and 0030 store."""
    # The trim set is a literal, never a bound parameter, so the expression is the indexed one in every binding mode.
    return Func(F(field_name), template=f"BTRIM(%(expressions)s, {_TRIM_LITERAL})", output_field=CharField())


class _SerialCandidates(Func):
    """The one candidate predicate: the trimmed serial equals a value or a key, or contains a key."""

    output_field = BooleanField()

    def __init__(self, exact, patterns):
        super().__init__(trimmed_serial())
        self.exact = list(exact)
        self.patterns = list(patterns)

    def as_sql(self, compiler, connection, **extra_context):
        sql, params = compiler.compile(self.source_expressions[0])
        return f"({sql} = ANY(%s) OR {sql} LIKE ANY(%s))", (*params, self.exact, *params, self.patterns)


def contains_pattern(key: str) -> str:
    """Return the LIKE pattern that finds a trimmed serial containing *key*, with the LIKE wildcards escaped."""
    escaped = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _probe_keys(values, rules: SerialRules) -> dict:
    """Return ``{value: keys}``: the keys of each value under every candidate context."""
    contexts = rules.candidates()
    return {value: {key for context in contexts if (key := context._key(value))} for value in values}


def _resolve(queryset, values, *, rules, row_context, lock, spare=0, own_keys=None):
    """
    Read the candidate rows of *values* once and confirm each row under its own context.

    *own_keys* maps a value to the key that a write gives it under the target's context; a row
    whose own key equals it also matches, because the write would give the two rows one identity.
    """
    own_keys = own_keys or {}
    values = list(dict.fromkeys(values))
    probes = _probe_keys(values, rules)
    for value, key in own_keys.items():
        if key:
            probes[value].add(key)
    results = {value: _ABSENT for value, keys in probes.items() if not keys}
    open_values = [value for value in values if probes[value]]
    if not open_values:
        return results
    exact = sorted({_text(value) for value in open_values} | {key for value in open_values for key in probes[value]})
    patterns = sorted({contains_pattern(key) for value in open_values for key in probes[value]})
    queryset = queryset.filter(_SerialCandidates(exact, patterns)).order_by("pk")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    matches = {value: [] for value in open_values}
    unavailable = {}
    complete = True
    # One client-side read: a server-side cursor plans for its first rows and can leave the trigram index.
    for row in queryset:
        context = row_context(row)
        stored = _text(row.serial)
        for value in open_values:
            if context.unavailable:
                if any(key in stored for key in probes[value]) or stored == _text(value):
                    unavailable.setdefault(value, context.unavailable)
                continue
            own_key = own_keys.get(value)
            if compare(row.serial, value, context) is Comparison.SAME or (own_key and context._key(stored) == own_key):
                matches[value].append(row)
        if not unavailable and all(len(found) >= _AMBIGUOUS_AT + spare for found in matches.values()):
            complete = False
            break
    for value in open_values:
        if value in unavailable:
            results[value] = Resolution(ResolutionKind.UNAVAILABLE, reason=unavailable[value])
        else:
            results[value] = _counted(tuple(matches[value]), complete=complete)
    return results


def resolve_devices(serials: Iterable, *, rules: SerialRules | None = None, exclude_pks=(), lock=False) -> dict:
    """
    Return ``{serial: Resolution}``: the Devices that hold each incoming serial, in one query.

    Args:
        serials: The incoming LibreNMS serials.
        rules: The batch's serial rules; None reads them.
        exclude_pks: Devices to leave out before the count, for example the device that a write changes.
        lock: Lock every candidate row ``FOR UPDATE``; the caller holds a transaction.

    """
    from dcim.models import Device

    rules = rules or SerialRules.load()
    queryset = Device.objects.select_related("device_type", "virtual_chassis__master__device_type")
    if exclude_pks:
        queryset = queryset.exclude(pk__in=list(exclude_pks))
    return _resolve(queryset, serials, rules=rules, row_context=rules.for_device, lock=lock)


def resolve_device(serial, *, rules: SerialRules | None = None, exclude_pks=(), lock=False) -> Resolution:
    """Return the Resolution of one incoming serial among Devices; see :func:`resolve_devices`."""
    return resolve_devices([serial], rules=rules, exclude_pks=exclude_pks, lock=lock)[serial]


def resolve_devices_write(serials: Iterable, context: SerialContext, *, rules: SerialRules, exclude_pks=()) -> dict:
    """
    Return ``{serial: Resolution}``: the Devices that a write of each serial under *context* would duplicate.

    A row matches when it holds the LibreNMS text under its own context, as in ``resolve_devices``,
    and also when its own key equals the key that *context* gives the text: after the write, the
    two rows would have one identity. The lookup is one query.
    """
    from dcim.models import Device

    serials = list(dict.fromkeys(serials))
    queryset = Device.objects.select_related("device_type", "virtual_chassis__master__device_type")
    if exclude_pks:
        queryset = queryset.exclude(pk__in=list(exclude_pks))
    own_keys = {serial: context._key(serial) for serial in serials}
    return _resolve(queryset, serials, rules=rules, row_context=rules.for_device, lock=False, own_keys=own_keys)


def resolve_device_write(incoming, context: SerialContext, *, rules: SerialRules, exclude_pks=()) -> Resolution:
    """Return the Devices that a write of *incoming* under *context* would duplicate; see ``resolve_devices_write``."""
    return resolve_devices_write([incoming], context, rules=rules, exclude_pks=exclude_pks)[incoming]


def is_sole_holder(device, incoming, *, rules: SerialRules, exclude_pks=()) -> bool:
    """Return whether *device* is the one Device, apart from *exclude_pks*, that holds the hardware *incoming* names."""
    holder = resolve_device(incoming, rules=rules, exclude_pks=exclude_pks).object
    return holder is not None and holder.pk == device.pk


def resolve_modules(
    serials: Iterable, *, rules: SerialRules | None = None, device_ids=None, exclude_pks=(), lock=False, spare=0
) -> dict:
    """
    Return ``{serial: Resolution}``: the Modules that hold each incoming serial, in one query.

    Args:
        serials: The incoming LibreNMS serials.
        rules: The batch's serial rules; None reads them.
        device_ids: Only Modules on these Devices count; None counts every Module.
        exclude_pks: Modules to leave out before the count, for example the module that a write replaces.
        lock: Lock every candidate row ``FOR UPDATE``; the caller holds a transaction.
        spare: Read this many matches past the Ambiguous count, so that ``Resolution.excluding`` can
            leave out up to this many rows per serial later.

    """
    from dcim.models import Module

    rules = rules or SerialRules.load()
    queryset = Module.objects.select_related("device__device_type", "module_type", "module_bay")
    if device_ids is not None:
        queryset = queryset.filter(device_id__in=list(device_ids))
    if exclude_pks:
        queryset = queryset.exclude(pk__in=list(exclude_pks))
    return _resolve(
        queryset,
        serials,
        rules=rules,
        row_context=lambda module: rules.for_module_target(module.device),
        lock=lock,
        spare=spare,
    )


def resolve_module(serial, *, rules: SerialRules | None = None, device_ids=None, exclude_pks=(), lock=False):
    """Return the Resolution of one incoming serial among Modules; see :func:`resolve_modules`."""
    return resolve_modules([serial], rules=rules, device_ids=device_ids, exclude_pks=exclude_pks, lock=lock)[serial]


def assignment_lock(serials: Iterable, *, model, rules: SerialRules) -> None:
    """
    Take the transaction advisory locks of every identity that the write set *serials* can have.

    Call it once per transaction and model, with every serial the transaction writes, before any row lock.
    The keys come from every candidate context, so two writers of rule-equivalent serials share a
    lock. The locks are taken in one sorted order.

    Raises:
        RuntimeError: The connection has no open transaction.

    """
    from netbox_librenms_plugin.utils import acquire_advisory_transaction_lock, advisory_lock_key

    label = model._meta.label_lower
    identities = {
        f"{_LOCK_PREFIX}:{label}:{key}" for keys in _probe_keys(list(serials), rules).values() for key in keys
    }
    for identity in sorted(identities, key=advisory_lock_key):
        acquire_advisory_transaction_lock(identity)


@dataclass(frozen=True)
class Assignment:
    """
    What a Device or Module serial writer may store on its locked target.

    ``value`` is the text to store, or None when nothing is written: the incoming serial has no
    identity (an update never clears a stored serial), the target already stores that text, or
    ``conflict`` refuses the write.
    """

    value: str | None
    conflict: Resolution | None = None
    absent: bool = False


def locked_context(device, *, rules: SerialRules) -> SerialContext:
    """
    Return the serial context of the locked *device*, under a lock on the row whose DeviceType decides it.

    A standalone device or a chassis master decides its own context: the caller holds its row, and
    the object may carry a type that the same write sets. A member reads the context of its master
    after it locks the master row, so a type change of the master that has not committed is
    waited for and then read.
    """
    from dcim.models import VirtualChassis

    chassis_id = device.virtual_chassis_id
    master_id = VirtualChassis.objects.filter(pk=chassis_id).values_list("master_id", flat=True).first()
    if chassis_id is None or master_id is None or master_id == device.pk:
        return rules.for_device(device)
    master_chassis_id, manufacturer_id = _lock_type_row(master_id)
    if master_chassis_id != chassis_id:
        return SerialContext(None, (), unavailable=UNAVAILABLE_MASTER_LINK)
    return rules.context(manufacturer_id)


def _lock_type_row(device_pk) -> tuple:
    """
    Lock the Device row *device_pk* ``FOR NO KEY UPDATE``; return its chassis id and its type's manufacturer id.

    The lock statement reads no other table: a join is checked again against the row that a type
    change commits, and drops the row. The type is read after the lock, in its own statement.
    """
    from dcim.models import Device, DeviceType

    chassis_id, type_id = (
        Device.objects.select_for_update(no_key=True)
        .filter(pk=device_pk)
        .values_list("virtual_chassis_id", "device_type_id")
        .get()
    )
    return chassis_id, DeviceType.objects.filter(pk=type_id).values_list("manufacturer_id", flat=True).get()


def locked_type_context(device, *, rules: SerialRules | None) -> SerialContext:
    """
    Return the serial context of the DeviceType of *device*, under a lock on the device row.

    It is the context of the device's Modules, and of the members of a chassis that the device
    masters. A type change holds the row until it commits, so a write waits and then reads the new
    type; call it before the lookup of the write. None reads the rules.
    """
    _chassis_id, manufacturer_id = _lock_type_row(device.pk)
    return (rules or SerialRules.load()).context(manufacturer_id)


def device_assignment(device, incoming, *, rules: SerialRules) -> Assignment:
    """
    Decide the serial write of a locked Device, after ``assignment_lock``.

    A stored serial that names the same hardware in another form is written in the raw form (healing).
    Another Device that holds the identity, or an identity the plugin cannot read, refuses the write.
    """
    from dcim.models import Device

    context = locked_context(device, rules=rules)
    if not is_identity(incoming, context):
        if context.unavailable:
            return Assignment(None, Resolution(ResolutionKind.UNAVAILABLE, reason=context.unavailable))
        return Assignment(None, absent=True)
    value = stored_form(incoming, Device)
    if _text(device.serial) == value:
        return Assignment(None)
    conflict = resolve_device_write(incoming, context, rules=rules, exclude_pks=[device.pk])
    if conflict.kind in REFUSING:
        return Assignment(None, conflict)
    return Assignment(value)


def module_assignment(module, incoming, *, rules: SerialRules) -> Assignment:
    """
    Decide the serial write of a locked Module, after ``assignment_lock``.

    The Module's device decides the context. Another Module on that device that holds the identity
    refuses the write, as an install refuses it.
    """
    from dcim.models import Module

    device = module.device
    if not is_identity(incoming, locked_type_context(device, rules=rules)):
        return Assignment(None, absent=True)
    value = stored_form(incoming, Module)
    if _text(module.serial) == value:
        return Assignment(None)
    conflict = resolve_module(incoming, rules=rules, device_ids=[device.pk], exclude_pks=[module.pk], lock=True)
    if conflict.kind in REFUSING:
        return Assignment(None, conflict)
    return Assignment(value)


# The refusal of a DeviceType change; it names no device, because the lookup is unrestricted.
RETYPE_HELD = (
    "This device type would give one serial identity to two rows, or the serials changed while the type was "
    "checked. Resolve the duplicate serial, or try again."
)


@dataclass(frozen=True)
class RetypeScope:
    """
    The serials whose identity a new DeviceType of one device decides, as ``(pk, serial)`` pairs.

    ``devices``: the device's own serial; for a chassis master, every member serial; for another
    member, none, because it keeps the master's context. ``modules``: the serials of the device's
    Modules, which use the manufacturer of the device's own type.
    """

    devices: tuple
    modules: tuple


def retype_scope(device) -> RetypeScope:
    """Read the serials whose identity a new DeviceType of *device* decides, from the database."""
    from dcim.models import Device, Module

    row = Device.objects.filter(pk=device.pk).values("virtual_chassis_id", "virtual_chassis__master_id").first()
    chassis_id = row["virtual_chassis_id"] if row else None
    master_id = row["virtual_chassis__master_id"] if row else None
    if chassis_id is None or master_id is None:
        devices = Device.objects.filter(pk=device.pk)
    elif master_id == device.pk:
        devices = Device.objects.filter(virtual_chassis_id=chassis_id)
    else:
        devices = Device.objects.none()
    return RetypeScope(
        tuple(devices.order_by("pk").values_list("pk", "serial")),
        tuple(Module.objects.filter(device_id=device.pk).order_by("pk").values_list("pk", "serial")),
    )


def retype_lock(scope: RetypeScope, *, rules: SerialRules, serials=()) -> None:
    """
    Take the identity locks of a DeviceType change before any row lock: the Device locks, then the Module locks.

    *serials* are the other Device serials that the same transaction writes; the Device locks are
    taken in one call with them.
    """
    from dcim.models import Device, Module

    assignment_lock([*serials, *(serial for _pk, serial in scope.devices)], model=Device, rules=rules)
    assignment_lock([serial for _pk, serial in scope.modules], model=Module, rules=rules)


def _new_shared_key(rows, old: SerialContext, new: SerialContext) -> bool:
    """Return whether *new* keys two of *rows* to one identity that *old* did not give them both."""
    groups: dict = {}
    for _pk, serial in rows:
        if key := new._key(serial):
            groups.setdefault(key, []).append(old._key(serial))
    return any(len(before) > 1 and (not before[0] or len(set(before)) > 1) for before in groups.values())


def retype_conflict(device, device_type, *, rules: SerialRules, scope: RetypeScope) -> bool:
    """
    Return whether the DeviceType *device_type* must not be saved on *device*.

    Call it after ``retype_lock`` and the device row lock, before the save. It refuses when the
    serials of *scope* changed since they were read, when an Unavailable chassis link hides the
    current identities, and when the new type gives one identity to two rows that did not share
    it: two serials in *scope*, or a Device serial and another Device. The serial of *device*
    comes from the object, so a serial that the same write sets counts.
    """
    from dcim.models import Device

    if retype_scope(device) != scope:
        return True
    stored = Device.objects.select_related("device_type", "virtual_chassis__master__device_type").get(pk=device.pk)
    old, new = rules.for_device(stored), rules.for_device_type(device_type)
    if old.unavailable:
        return True
    rows = [(pk, device.serial if pk == device.pk else serial) for pk, serial in scope.devices]
    module_old = rules.for_module_target(stored)
    if _new_shared_key(rows, old, new) or _new_shared_key(scope.modules, module_old, new):
        return True
    admitted = [serial for _pk, serial in rows if new._key(serial)]
    if not admitted:
        return False
    outside = [pk for pk, _serial in rows]
    after = resolve_devices_write(admitted, new, rules=rules, exclude_pks=outside)
    before = resolve_devices_write(
        [serial for serial in admitted if old._key(serial)], old, rules=rules, exclude_pks=outside
    )
    return any(
        after[serial].kind in REFUSING and (serial not in before or before[serial].kind not in REFUSING)
        for serial in admitted
    )


def report_rows(rules: SerialRules | None = None):
    """
    Yield ``(row, stored text, key or "", reason)`` for each Device and Module whose stored text changes under the rules.

    A legacy row can hold rule output (Modules before raw storage, and Virtual Chassis members for
    a while). A rule that matches its own output keys such a row again, so the operator reviews it.
    """
    from dcim.models import Device, Module

    rules = rules or SerialRules.load()
    devices = Device.objects.exclude(serial="").select_related("device_type", "virtual_chassis__master__device_type")
    modules = Module.objects.exclude(serial="").select_related("device__device_type", "module_bay")
    sources = (
        (devices, rules.for_device),
        (modules, lambda module: rules.for_module_target(module.device)),
    )
    for queryset, row_context in sources:
        for row in queryset.order_by("pk").iterator(chunk_size=500):
            stored = display(row.serial)
            if not stored:
                continue
            context = row_context(row)
            if context.unavailable:
                yield row, stored, "", context.unavailable
                continue
            key = context._key(row.serial)
            if key != stored:
                yield row, stored, key, "" if key else "no hardware identity under the current rules"
