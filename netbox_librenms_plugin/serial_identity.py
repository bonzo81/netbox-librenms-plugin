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

A writer takes ``assignment_lock`` for the serials of its whole write set before row locks,
then resolves again and writes ``stored_form``. Participating Module writers use
``module_writes`` to lock their complete Device scope before Module, bay and component rows.
"""

import enum
import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db.models import BooleanField, CharField, F, Func
from django.utils.functional import cached_property

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
    read stopped before it saw every match. ``uncertain`` holds the rows of a write lookup that
    store the writer's key of the value but do not match: a legacy row that stores rule output
    keys again under a rule that changes its own output, so it can be the same hardware.
    """

    kind: ResolutionKind
    matches: tuple = ()
    reason: str = ""
    complete: bool = True
    uncertain: tuple = ()

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


def _counted(matches: tuple, *, complete: bool = True, uncertain: tuple = ()) -> Resolution:
    if not matches:
        return Resolution(ResolutionKind.NO_MATCH, uncertain=uncertain)
    if len(matches) == 1:
        return Resolution(ResolutionKind.UNIQUE, matches)
    return Resolution(ResolutionKind.AMBIGUOUS, matches, complete=complete)


_ABSENT = Resolution(ResolutionKind.ABSENT)


def _text(value) -> str:
    """Return a LibreNMS or NetBox value as trimmed text; LibreNMS sends an all-digit serial as a JSON number."""
    return "" if value is None else str(value).strip()


def is_serial_value(value) -> bool:
    """Return whether a LibreNMS serial field has a serial's shape: absent, text, or a JSON number."""
    return value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool))


def _stored_text(value) -> str:
    """Return the persisted text of a NetBox serial field, untrimmed."""
    return "" if value is None else str(value)


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

    @cached_property
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
        return tuple(self.iter_candidates())

    def iter_candidates(self) -> Iterable[SerialContext]:
        """Read each candidate context when the caller needs it."""
        yield self.context(None)
        scoped = sorted(manufacturer_id for manufacturer_id in self._chains_by_manufacturer() if manufacturer_id)
        for manufacturer_id in scoped:
            yield self.context(manufacturer_id)

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
    # The exact stored text: padding around a matching serial is another form too.
    return compare(stored, incoming, context) is Comparison.SAME and _stored_text(stored) != display(incoming)


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


class MemberSerials:
    """The Device serials of chassis members, each grouped under the context of its own Device serial."""

    def __init__(self, members: Iterable, member_context: Callable):
        by_context = {}
        for member in members:
            context = member_context(member)
            by_context.setdefault(context.fingerprint, (context, []))[1].append(member)
        self._groups = tuple(
            group(found, context, serial=lambda member: member.serial) for context, found in by_context.values()
        )

    def find(self, value) -> list:
        """Return the members whose serial names the hardware of *value* under their own context."""
        return [member for groups in self._groups for member in groups.find(value)]

    def admits(self, value) -> bool:
        """Return whether *value* has an identity under the context of at least one member."""
        return any(is_identity(value, groups.context) for groups in self._groups)

    def contexts(self) -> tuple:
        """Return the distinct serial contexts of the members, one per context."""
        return tuple(groups.context for groups in self._groups)


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
    # A missing or placeholder serial has no key under any chain, so it reads no rule and a broken rule cannot refuse it.
    contexts = rules.candidates() if any(display(value) for value in values) else ()
    return {value: {key for context in contexts if (key := context._key(value))} for value in values}


def _matches_serial(stored, incoming, context, writer):
    if compare(stored, incoming, context) is Comparison.SAME:
        return True
    own_key = writer._key(incoming) if writer is not None else ""
    # Both directions: the row's own key, or the key that the writer's context gives the row's text.
    return bool(own_key) and own_key in (context._key(stored), writer._key(stored))


def _values_by_key(values, probes) -> dict:
    """Return ``{key: values}`` over the probe keys; a value's key under a row's own context is one of them."""
    by_key: dict = {}
    for value in values:
        for key in probes[value]:
            by_key.setdefault(key, []).append(value)
    return by_key


def _writer_keys(values, probes, writer) -> dict:
    """Return ``{key: values}`` over the keys that *writer* gives the values; add each key to its value's probes."""
    by_own_key: dict = {}
    if writer is None:
        return by_own_key
    for value in values:
        if key := writer._key(value):
            probes[value].add(key)
            by_own_key.setdefault(key, []).append(value)
    return by_own_key


def _row_candidates(stored, context, by_key, by_own_key, writer) -> tuple:
    """Return the values that a row with *stored* text under *context* can match, each one once."""
    candidates = by_key.get(context._key(stored), [])
    if writer is not None:
        candidates = [*candidates, *by_own_key.get(writer._key(stored), ())]
    return tuple(dict.fromkeys(candidates))


def _mark_unavailable(unavailable, values, probes, stored, reason):
    for value in values:
        if stored == value or any(key in stored for key in probes[value]):
            unavailable.setdefault(value, reason)


def _resolve(queryset, values, *, rules, row_context, lock, spare=0, writer=None, stop_at_ambiguity=False):
    """
    Read the candidate rows of *values* once and confirm each row under its own context.

    *writer* is the target's context of a write. A row also matches when its own key, or the key
    that *writer* gives its text, equals the key that *writer* gives a value: after the write, the
    two rows would have one identity, or a lookup of the row's text would find the target.
    """
    values = list(dict.fromkeys(_text(value) for value in values))
    probes = _probe_keys(values, rules)
    by_own_key = _writer_keys(values, probes, writer)
    results = {value: _ABSENT for value, keys in probes.items() if not keys}
    open_values = [value for value in values if probes[value]]
    if not open_values:
        return results
    exact = sorted(set(open_values) | {key for value in open_values for key in probes[value]})
    patterns = sorted({contains_pattern(key) for value in open_values for key in probes[value]})
    queryset = queryset.filter(_SerialCandidates(exact, patterns)).order_by("pk")
    matches = {value: [] for value in open_values}
    # The pks of matches[value]: a list scan for each row would make many duplicate holders quadratic.
    matched_pks = {value: set() for value in open_values}
    uncertain = {value: [] for value in open_values}
    by_key = _values_by_key(open_values, probes)
    full = _AMBIGUOUS_AT + spare
    saturated = 0
    unavailable = {}
    complete = True
    # One client-side read: a server-side cursor plans for its first rows and can leave the trigram index.
    for row in queryset:
        context = row_context(row)
        stored = _text(row.serial)
        candidates = _row_candidates(stored, context, by_key, by_own_key, writer) if complete else ()
        if lock and any(_matches_serial(stored, value, context, writer) for value in candidates):
            # A substring candidate can name unrelated hardware. Lock only confirmed matches,
            # then read their identity again after the lock to exclude a concurrent change.
            row = queryset.select_for_update(of=("self",)).filter(pk=row.pk).first()
            if row is None:
                continue
            context = row_context(row)
            stored = _text(row.serial)
            candidates = _row_candidates(stored, context, by_key, by_own_key, writer)
        if context.unavailable:
            _mark_unavailable(unavailable, open_values, probes, stored, context.unavailable)
        elif complete:
            for value in candidates:
                if _matches_serial(stored, value, context, writer):
                    matches[value].append(row)
                    matched_pks[value].add(row.pk)
                    saturated += len(matches[value]) == full
            for value in by_own_key.get(stored, ()):
                if row.pk not in matched_pks[value]:
                    uncertain[value].append(row)
            complete = saturated < len(open_values)
            # Device contexts can be unavailable even after the match count is settled.
            if stop_at_ambiguity and not complete:
                break
    for value in open_values:
        if value in unavailable:
            results[value] = Resolution(ResolutionKind.UNAVAILABLE, reason=unavailable[value])
        else:
            results[value] = _counted(tuple(matches[value]), complete=complete, uncertain=tuple(uncertain[value]))
    return results


def resolve_devices(serials: Iterable, *, rules: SerialRules | None = None, exclude_pks=(), lock=False) -> dict:
    """
    Return ``{serial_text: Resolution}``: the Devices that hold each incoming serial, in one query.

    Args:
        serials: The incoming LibreNMS serials. Result keys are their trimmed text.
        rules: The batch's serial rules; None reads them.
        exclude_pks: Devices to leave out before the count, for example the device that a write changes.
        lock: Lock confirmed matches ``FOR UPDATE`` and check their identity again; the caller holds a transaction.

    """
    from dcim.models import Device

    rules = rules or SerialRules.load()
    queryset = Device.objects.select_related("device_type", "virtual_chassis__master__device_type")
    if exclude_pks:
        queryset = queryset.exclude(pk__in=list(exclude_pks))
    return _resolve(queryset, serials, rules=rules, row_context=rules.for_device, lock=lock)


def resolve_device(serial, *, rules: SerialRules | None = None, exclude_pks=(), lock=False) -> Resolution:
    """Return the Resolution of one incoming serial among Devices; see :func:`resolve_devices`."""
    return resolve_devices([serial], rules=rules, exclude_pks=exclude_pks, lock=lock)[_text(serial)]


def resolve_devices_write(
    serials: Iterable, context: SerialContext, *, rules: SerialRules, exclude_pks=(), master=None
) -> dict:
    """
    Return the Devices that each write under *context* would duplicate, keyed by trimmed serial text.

    A row matches when it holds the LibreNMS text under its own context, as in ``resolve_devices``,
    and also when its own key, or the key that *context* gives the row's text, equals the key that
    *context* gives the incoming text. So the two write orders of two rows give one result. The
    lookup is one query. When *master* is the chassis master that the write decides, its members
    are read under *context*, the context that the write gives them.
    """
    from dcim.models import Device

    queryset = Device.objects.select_related("device_type", "virtual_chassis__master__device_type")
    if exclude_pks:
        queryset = queryset.exclude(pk__in=list(exclude_pks))
    row_context = rules.for_device
    if master is not None and master.virtual_chassis_id is not None:

        def row_context(row):
            if row.virtual_chassis_id == master.virtual_chassis_id and row.virtual_chassis.master_id == master.pk:
                return context
            return rules.for_device(row)

    return _resolve(queryset, serials, rules=rules, row_context=row_context, lock=False, writer=context)


def resolve_device_write(
    incoming, context: SerialContext, *, rules: SerialRules, exclude_pks=(), master=None
) -> Resolution:
    """Return the Devices that a write of *incoming* under *context* would duplicate; see ``resolve_devices_write``."""
    return resolve_devices_write([incoming], context, rules=rules, exclude_pks=exclude_pks, master=master)[
        _text(incoming)
    ]


def is_sole_holder(device, incoming, *, rules: SerialRules, exclude_pks=()) -> bool:
    """Return whether *device* is the one Device, apart from *exclude_pks*, that holds the hardware *incoming* names."""
    holder = resolve_device(incoming, rules=rules, exclude_pks=exclude_pks).object
    return holder is not None and holder.pk == device.pk


def resolve_modules(
    serials: Iterable, *, rules: SerialRules | None = None, device_ids=None, exclude_pks=(), lock=False, spare=0
) -> dict:
    """
    Return ``{serial_text: Resolution}``: the Modules that hold each incoming serial, in one query.

    Args:
        serials: The incoming LibreNMS serials. Result keys are their trimmed text.
        rules: The batch's serial rules; None reads them.
        device_ids: Only Modules on these Devices count; None counts every Module.
        exclude_pks: Modules to leave out before the count, for example the module that a write replaces.
        lock: Lock confirmed matches ``FOR UPDATE`` and check their identity again; the caller holds a transaction.
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
        stop_at_ambiguity=True,
    )


def resolve_module(serial, *, rules: SerialRules | None = None, device_ids=None, exclude_pks=(), lock=False):
    """Return the Resolution of one incoming serial among Modules; see :func:`resolve_modules`."""
    return resolve_modules([serial], rules=rules, device_ids=device_ids, exclude_pks=exclude_pks, lock=lock)[
        _text(serial)
    ]


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
    # Text first: the probe map keys on the value, and the JSON numbers 123 and 123.0 are one Python key.
    texts = [_text(serial) for serial in serials]
    identities = {f"{_LOCK_PREFIX}:{label}:{key}" for keys in _probe_keys(texts, rules).values() for key in keys}
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


def _share_locked_master_id(chassis_id, *, wait=True):
    """
    Return the master id of the chassis *chassis_id* under ``FOR SHARE`` on its row; None without a chassis.

    The lock waits for a master change that has not committed, but not for another holder of the lock.
    Without *wait*, a conflicting holder raises the lock conflict ``55P03`` at once.
    """
    from dcim.models import VirtualChassis
    from django.db import connection

    if chassis_id is None:
        return None
    mode = "FOR SHARE" if wait else "FOR SHARE NOWAIT"
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT master_id FROM "{VirtualChassis._meta.db_table}" WHERE id = %s {mode}', [chassis_id])
        row = cursor.fetchone()
    return row[0] if row else None


def lock_chassis_of(device_pk) -> None:
    """
    Take ``FOR SHARE`` on the chassis row of the Device *device_pk*; a writer calls it before the device row lock.

    NetBox's chassis edit locks the chassis row before the member rows, so a writer takes the same order.
    """
    from dcim.models import Device

    _share_locked_master_id(Device.objects.filter(pk=device_pk).values_list("virtual_chassis_id", flat=True).first())


def locked_context(device, *, rules: SerialRules) -> SerialContext:
    """
    Return the serial context of the locked *device*, under a lock on the row whose DeviceType decides it.

    A standalone device or a chassis master decides its own context: the caller holds its row, and
    the object may carry a type that the same write sets. A member reads the context of its master
    after it locks the master row, so a type change of the master that has not committed is
    waited for and then read. The master link is read under a share lock on the chassis row.
    """
    chassis_id = device.virtual_chassis_id
    # Held already after lock_chassis_of(); a chassis that the device joined later is never waited for under its row.
    master_id = _share_locked_master_id(chassis_id, wait=False)
    if chassis_id is None or master_id is None or master_id == device.pk:
        return rules.for_module_target(device)
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
    if _stored_text(device.serial) == value:
        return Assignment(None)
    # A master's members take the context of its type as the object holds it, also a type that the same write sets.
    conflict = resolve_device_write(incoming, context, rules=rules, exclude_pks=[device.pk], master=device)
    if conflict.kind in REFUSING:
        return Assignment(None, conflict)
    return Assignment(value)


def module_assignment(module, incoming, *, rules: SerialRules) -> Assignment:
    """
    Decide the serial write of a locked Module, after ``assignment_lock``.

    The caller holds the Device owner lock before the Module row lock. The Module's
    device decides the context. Another Module on that device that holds the identity
    refuses the write, as an install refuses it.
    """
    from dcim.models import Module

    device = module.device
    if not is_identity(incoming, locked_type_context(device, rules=rules)):
        return Assignment(None, absent=True)
    value = stored_form(incoming, Module)
    if _stored_text(module.serial) == value:
        return Assignment(None)
    # The Device lock serializes its module writers. A conflict only refuses this write,
    # so it must not wait for a Module held by a writer that is waiting for this Device.
    conflict = resolve_module(incoming, rules=rules, device_ids=[device.pk], exclude_pks=[module.pk])
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
    """
    Read the serials whose identity a new DeviceType of *device* decides, from the database.

    It takes no lock: the first read comes before the locks, and ``retype_conflict`` reads it again under them.
    """
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


def _new_shared_key(rows, old: SerialContext, new: SerialContext, *, stored=None) -> bool:
    """
    Return whether *new* keys two of *rows* to one identity that *old* did not give them both.

    *stored* maps a pk to the serial that the row holds before the write; *old* keys that serial.
    """
    stored = stored or {}
    groups: dict = {}
    for pk, serial in rows:
        if key := new._key(serial):
            groups.setdefault(key, []).append(old._key(stored.get(pk, serial)))
    return any(len(before) > 1 and (not before[0] or len(set(before)) > 1) for before in groups.values())


def locked_device_type(device_type):
    """
    Return *device_type* as its row holds it now, locked ``FOR NO KEY UPDATE`` until the transaction ends.

    A writer that sets a new type calls it after the device row lock and before any serial
    decision, so a manufacturer edit cannot change the context between the decision and the save.
    The save writes the type row's device count anyway, so the lock adds no new contention.
    """
    from dcim.models import DeviceType

    return DeviceType.objects.select_for_update(no_key=True).get(pk=device_type.pk)


def retype_conflict(device, device_type, *, rules: SerialRules, scope: RetypeScope) -> bool:
    """
    Return whether the DeviceType *device_type* must not be saved on *device*.

    Call it after ``retype_lock`` and the device row lock, before the save. It refuses when the
    serials of *scope* changed since they were read, when an Unavailable chassis link hides the
    current identities, when the new type gives one identity to two serials in *scope* that did
    not share it, and when another Device holds a Device serial of *scope* under the new type.
    The serial of *device* comes from the object, so a serial that the same write sets counts.
    The manufacturer of *device_type* is read again under a lock on its row.
    """
    from dcim.models import Device

    # The caller holds the device row, so its chassis stays; this lock holds the master link until the save.
    _share_locked_master_id(device.virtual_chassis_id, wait=False)
    if retype_scope(device) != scope:
        return True
    stored = Device.objects.select_related("device_type", "virtual_chassis__master__device_type").get(pk=device.pk)
    old, new = rules.for_device(stored), rules.for_device_type(locked_device_type(device_type))
    if old.unavailable:
        return True
    rows = [(pk, device.serial if pk == device.pk else serial) for pk, serial in scope.devices]
    module_old = rules.for_module_target(stored)
    # The old identities are those of the stored serials: a serial that the same write sets is not one of them.
    if _new_shared_key(rows, old, new, stored=dict(scope.devices)) or _new_shared_key(scope.modules, module_old, new):
        return True
    # Another holder refuses the change, also one that held a serial before: the read stops at two
    # rows, so it cannot tell a new holder from an old one.
    admitted = [serial for _pk, serial in rows if new._key(serial)]
    if not admitted:
        return False
    after = resolve_devices_write(admitted, new, rules=rules, exclude_pks=[pk for pk, _serial in rows])
    return any(resolution.kind in REFUSING for resolution in after.values())


# The refusal of a module move; it names no module, because the lookup is unrestricted.
RELOCATION_HELD = (
    "Moving this module would give one serial identity to two modules of the destination device, or a moved "
    "serial changed while the move was checked. Nothing was moved."
)


def module_subtree_serials(module_pk) -> tuple:
    """Return the sorted ``(pk, serial)`` pairs of the Module *module_pk* and of every Module below it."""
    from dcim.models import Module

    found = list(Module.objects.filter(pk=module_pk).values_list("pk", "serial"))
    frontier = [pk for pk, _serial in found]
    while frontier:
        children = list(Module.objects.filter(module_bay__module_id__in=frontier).values_list("pk", "serial"))
        found.extend(children)
        frontier = [pk for pk, _serial in children]
    return tuple(sorted(found))


def relocation_conflict(module, target_device, moving, *, rules: SerialRules, scope) -> bool:
    """Check a move after the shared writer locked both Devices before any Module.

    The caller holds the complete owner set. This helper never expands that set.
    Compare the moved identities again before native relocation and counter saves.
    """
    from dcim.models import Module

    scope.require_devices([module.device_id, target_device.pk])
    list(
        Module.objects.select_for_update()
        .filter(pk__in=[pk for pk, _serial in moving])
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    source = locked_type_context(module.device, rules=rules)
    target = locked_type_context(target_device, rules=rules)
    if module_subtree_serials(module.pk) != moving:
        return True
    if _new_shared_key(moving, source, target):
        return True
    serials = [serial for _pk, serial in moving if target._key(serial)]
    if not serials:
        return False
    found = resolve_modules(
        serials, rules=rules, device_ids=[target_device.pk], exclude_pks=[pk for pk, _serial in moving]
    )
    return any(resolution.kind in REFUSING for resolution in found.values())


def report_rows(rules: SerialRules | None = None):
    """
    Yield ``(row, stored text, key or "", reason)`` for each Device and Module whose stored text changes under the rules.

    A legacy row can hold rule output (Modules before raw storage, and Virtual Chassis members for
    a while). A rule that matches its own output keys such a row again, so the operator reviews it.
    """
    from dcim.models import Device, Module

    from netbox_librenms_plugin.models import RuleConfigurationError

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
            try:
                context = row_context(row)
            except RuleConfigurationError as exc:
                # A broken rule fails only the rows of its chain; the scan reports them and goes on.
                yield row, stored, "", exc.public_message
                continue
            if context.unavailable:
                yield row, stored, "", context.unavailable
                continue
            key = context._extract(stored)
            if key != stored:
                yield row, stored, key, "" if key else "no hardware identity under the current rules"
