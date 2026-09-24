"""
The one reader of interface rules (``InterfaceTypeMapping``).

A rule selects LibreNMS ports by platform, ``ifType``, name pattern and minimum speed, and either
sets a NetBox interface type or ignores the port. Load the rules once per request (or once per job
batch) and decide every port against that snapshot.
"""

import enum
import re
from dataclasses import dataclass

from django.http import HttpRequest

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.utils import REGEX_COMPILE_ERRORS, convert_speed_to_kbps

_REQUEST_CACHE_ATTRIBUTE = "_librenms_interface_rules"

# The port keys a decision reads. An interface write refuses a record that lacks one (None is a value).
PORT_RECORD_KEYS = ("ifName", "ifDescr", "ifType", "ifSpeed")


class RuleConfigurationError(Exception):
    """A stored rule cannot be applied, so no decision from this rule set is safe."""


class RuleDecisionKind(enum.Enum):
    """What the rules say about one port."""

    UNMAPPED = "unmapped"
    SET_TYPE = "set_type"
    IGNORE = "ignore"
    AMBIGUOUS = "ambiguous"
    # Only an interface write check gives this: the port record lacks a key a decision reads.
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class InterfaceRule:
    """An immutable snapshot of one stored rule."""

    pk: int
    label: str
    action: str
    platform_id: int | None
    librenms_type: str
    pattern: re.Pattern | None
    librenms_speed: int | None
    netbox_type: str | None

    @property
    def rank(self) -> tuple:
        """Specificity of a Set type rule: platform, then pattern, then type, then speed."""
        return (
            self.platform_id is not None,
            self.pattern is not None,
            bool(self.librenms_type),
            self.librenms_speed is not None,
            self.librenms_speed or 0,
        )

    def matches(self, port: dict, platform_id: int | None, speed: int | None) -> bool:
        """Return whether every selector of this rule matches the port (speed in Kbps)."""
        if self.platform_id is not None and self.platform_id != platform_id:
            return False
        if self.librenms_type and self.librenms_type != port.get("ifType"):
            return False
        if self.librenms_speed is not None and (speed is None or speed < self.librenms_speed):
            return False
        if self.pattern is None:
            return True
        return any(
            isinstance(name, str) and self.pattern.search(name) for name in (port.get("ifName"), port.get("ifDescr"))
        )


@dataclass(frozen=True)
class RuleDecision:
    """The outcome for one port: the kind, the type to write (SET_TYPE only), and the rules behind it."""

    kind: RuleDecisionKind
    netbox_type: str | None
    rules: tuple[InterfaceRule, ...]
    missing_keys: tuple[str, ...] = ()


_UNMAPPED = RuleDecision(RuleDecisionKind.UNMAPPED, None, ())


def rule_names(rules) -> str:
    """Name rules for a message: ``rule 3 (label)`` or ``rules 3 (label), 5 (label)``."""
    noun = "rule" if len(rules) == 1 else "rules"
    return f"{noun} " + ", ".join(f"{rule.pk} ({rule.label})" for rule in rules)


def decision_reason(decision: RuleDecision) -> str | None:
    """Return why a decision blocks interface writes, or None when it does not."""
    if decision.kind is RuleDecisionKind.IGNORE:
        return f"ignored by interface {rule_names(decision.rules)}"
    if decision.kind is RuleDecisionKind.AMBIGUOUS:
        return f"interface {rule_names(decision.rules)} match with equal rank"
    if decision.kind is RuleDecisionKind.INCOMPLETE:
        return f"the cached LibreNMS port record has no {', '.join(decision.missing_keys)}; refresh the data"
    return None


class PortSyncBlocked(Exception):
    """
    An interface write for one port is refused before it writes anything.

    ``decision`` is the blocking decision: IGNORE, AMBIGUOUS or INCOMPLETE.
    """

    def __init__(self, decision: RuleDecision):
        """Keep the decision for callers and use its reason as the message."""
        super().__init__(decision_reason(decision))
        self.decision = decision


class InterfaceRuleMatcher:
    """Decide ports against one snapshot of the interface rules, with no database access."""

    def __init__(self, rules):
        """Split the snapshot into Ignore and Set type rules, each in primary-key order."""
        self._ignore_rules = tuple(rule for rule in rules if rule.action == InterfaceTypeMapping.ACTION_IGNORE)
        self._set_rules = tuple(rule for rule in rules if rule.action == InterfaceTypeMapping.ACTION_SET_TYPE)

    @classmethod
    def load(cls) -> "InterfaceRuleMatcher":
        """
        Read every rule in one query and compile its name pattern.

        Raises:
            RuleConfigurationError: A stored pattern does not compile (it bypassed ``clean()``).

        """
        rules = []
        for mapping in InterfaceTypeMapping.objects.select_related("platform").order_by("pk"):
            try:
                pattern = re.compile(mapping.name_pattern) if mapping.name_pattern else None
            except REGEX_COMPILE_ERRORS as exc:
                raise RuleConfigurationError(
                    f"Interface rule {mapping.pk} has a name pattern that does not compile: {exc}"
                ) from exc
            rules.append(
                InterfaceRule(
                    pk=mapping.pk,
                    label=str(mapping),
                    action=mapping.action,
                    platform_id=mapping.platform_id,
                    librenms_type=mapping.librenms_type,
                    pattern=pattern,
                    librenms_speed=mapping.librenms_speed,
                    netbox_type=mapping.netbox_type,
                )
            )
        return cls(rules)

    def decide(self, port: dict, *, platform_id: int | None) -> RuleDecision:
        """
        Decide one LibreNMS port for an interface owned by an object on *platform_id*.

        Any matching Ignore rule wins. Otherwise the Set type rule with the highest rank wins, and
        two rules on that rank make the port ambiguous, even when their types agree.

        Args:
            port (dict): The LibreNMS port record (``ifName``, ``ifDescr``, ``ifType``, ``ifSpeed``).
            platform_id (int | None): The platform of the interface's owner; None matches only
                global rules.

        Returns:
            RuleDecision: The decision and the rules that produced it.

        """
        speed = convert_speed_to_kbps(port.get("ifSpeed"))
        ignored_by = tuple(rule for rule in self._ignore_rules if rule.matches(port, platform_id, speed))
        if ignored_by:
            return RuleDecision(RuleDecisionKind.IGNORE, None, ignored_by)
        top_rank = None
        top = []
        for rule in self._set_rules:
            if not rule.matches(port, platform_id, speed):
                continue
            if top_rank is None or rule.rank > top_rank:
                top_rank, top = rule.rank, [rule]
            elif rule.rank == top_rank:
                top.append(rule)
        if not top:
            return _UNMAPPED
        if len(top) > 1:
            return RuleDecision(RuleDecisionKind.AMBIGUOUS, None, tuple(top))
        return RuleDecision(RuleDecisionKind.SET_TYPE, top[0].netbox_type, (top[0],))

    def check_interface_write(self, port: dict, *, platform_id: int | None) -> RuleDecision:
        """
        Decide one port for an interface write, which also needs the complete port record.

        The writers, the interfaces table and the verify repaint all call this, so a row cannot
        offer a sync that the writer refuses. ``decision_reason`` says why a decision blocks.

        Args:
            port (dict): The LibreNMS port record.
            platform_id (int | None): The platform of the interface's owner.

        Returns:
            RuleDecision: The decision; INCOMPLETE when the record lacks a key in ``PORT_RECORD_KEYS``.

        """
        missing = tuple(key for key in PORT_RECORD_KEYS if key not in port)
        if missing:
            return RuleDecision(RuleDecisionKind.INCOMPLETE, None, (), missing)
        return self.decide(port, platform_id=platform_id)

    def decide_interface_write(self, port: dict, *, platform_id: int | None) -> RuleDecision:
        """
        Return the UNMAPPED or SET_TYPE decision an interface write may use.

        Raises:
            PortSyncBlocked: The record is incomplete, or the port is ignored or ambiguous.

        """
        decision = self.check_interface_write(port, platform_id=platform_id)
        if decision_reason(decision) is not None:
            raise PortSyncBlocked(decision)
        return decision

    def may_ignore(self, platform_id: int | None) -> bool:
        """Return whether any Ignore rule could apply to a port on *platform_id*."""
        return any(rule.platform_id is None or rule.platform_id == platform_id for rule in self._ignore_rules)


def interface_rules_for_request(request: HttpRequest) -> InterfaceRuleMatcher:
    """Return the request's one rule snapshot, loading it on first use."""
    request_state = request.__dict__
    if _REQUEST_CACHE_ATTRIBUTE not in request_state:
        setattr(request, _REQUEST_CACHE_ATTRIBUTE, InterfaceRuleMatcher.load())
    return request_state[_REQUEST_CACHE_ATTRIBUTE]
