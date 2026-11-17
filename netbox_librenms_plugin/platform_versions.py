"""
Platform version tracking: assess and apply the platform of one synchronization subject.

An assessment compares the LibreNMS OS and version facts with NetBox and changes nothing. An
application checks the actor's permissions and the assessment again under the subject's row lock,
creates the platforms it needs, and writes the subject in one transaction. A chassis application
writes the same platform to every member of the subject's virtual chassis. Neither needs an HTTP request, so the sync
page, import, and a background job are adapters over this module.
"""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import NamedTuple

from dcim.models import Platform
from django.contrib.messages import constants as message_levels
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Q
from django.utils.text import slugify

from netbox_librenms_plugin.models import LibreNMSSettings, PlatformMapping
from netbox_librenms_plugin.transactions import (
    TransactionConflict,
    classify_conflict,
    run_transaction,
    saved_in_scope,
    update_existing_row,
)
from netbox_librenms_plugin.utils import exception_text_for, write_failure_message

_ADD_PLATFORM = "dcim.add_platform"
_ADD_MAPPING = "netbox_librenms_plugin.add_platformmapping"
_SLUG_MAX_LENGTH = 100
_MAPPING_DENIED = "you lack permission to add mappings."
HIDDEN_PLATFORM = "a platform you cannot view"


class PlatformState(StrEnum):
    """Whether the subject's platform is current, can change, or cannot change."""

    CURRENT = "current"
    PROPOSED = "proposed"
    BLOCKED = "blocked"


class PlatformReason(StrEnum):
    """The named reason of an assessment or an outcome; ``reason_text`` gives its words."""

    NO_OS = "no_os"
    ON_TARGET = "on_target"
    OFF_TARGET = "off_target"
    VERSION_PLATFORM_MISSING = "version_platform_missing"
    OS_PLATFORM_MISSING = "os_platform_missing"
    OS_PLATFORM_AMBIGUOUS = "os_platform_ambiguous"
    OS_MAPPING_AMBIGUOUS = "os_mapping_ambiguous"
    VERSION_PLATFORM_AMBIGUOUS = "version_platform_ambiguous"
    NAME_TAKEN = "name_taken"
    ADD_PLATFORM_DENIED = "add_platform_denied"
    PLATFORM_INVALID = "platform_invalid"
    SUBJECT_UNAVAILABLE = "subject_unavailable"
    VERSION_CHANGED = "version_changed"
    OS_PLATFORM_RESOLVED = "os_platform_resolved"
    NAMED_PLATFORM_UNAVAILABLE = "named_platform_unavailable"
    OS_CHANGED = "os_changed"
    CHASSIS_MEMBER_DENIED = "chassis_member_denied"
    CHASSIS_CHANGED = "chassis_changed"


class PlatformOperation(StrEnum):
    """The change that an assessment offers."""

    ASSIGN = "assign"
    CREATE_VERSION_PLATFORM = "create_version_platform"
    CREATE_OS_PLATFORM = "create_os_platform"


class OutcomeStatus(StrEnum):
    """The result of one application."""

    APPLIED = "applied"
    UNCHANGED = "unchanged"
    BLOCKED = "blocked"
    STALE = "stale"
    FAILED = "failed"


def _fact(value):
    text = "" if value is None else str(value).strip()
    return "" if text == "-" else text


@dataclass(frozen=True)
class PlatformFacts:
    """The LibreNMS OS and version of one device. An absent value (empty or ``-``) is ``""``."""

    librenms_os: str
    version: str

    @classmethod
    def from_librenms(cls, librenms_os, version):
        """Return the facts for the raw LibreNMS ``os`` and ``version`` values."""
        return cls(_fact(librenms_os), _fact(version))


@dataclass(frozen=True)
class FactsSource:
    """The object whose LibreNMS facts were read (``source_pk``), and the virtual chassis of the subject at that read."""

    source_pk: int
    chassis_id: int | None


@dataclass(frozen=True)
class PlatformAssessment:
    """
    The platform that the LibreNMS facts call for on one subject.

    ``target`` is the existing platform that the subject must have. ``target_name`` is its name,
    or the derived name of a version platform that does not exist yet. ``named`` holds the
    platforms that a blocked reason names.
    """

    state: PlatformState
    reason: PlatformReason
    facts: PlatformFacts
    tracking: bool
    os_platform: Platform | None = None
    target: Platform | None = None
    target_name: str = ""
    operation: PlatformOperation | None = None
    named: tuple = ()

    @property
    def tracks_version(self):
        """True when the subject must have a version platform, not only its OS platform."""
        return self.tracking and bool(self.facts.version)


@dataclass(frozen=True)
class NewOSPlatform:
    """
    The OS platform that the actor names when no platform matches the LibreNMS OS.

    ``reported_os`` and ``reported_version`` are the OS and the version that the actor saw. A
    different current OS, or with tracking on a different current version, makes the application stale.
    """

    name: str
    reported_os: str
    manufacturer: object = None
    reported_version: str = ""


@dataclass(frozen=True)
class PlatformOutcome:
    """
    The result of one application.

    ``platform`` is the platform written (or attempted, for a failure). ``error`` and
    ``error_model`` hold the refused write of a failure, so an adapter can describe it. A
    virtual chassis application holds the pks of its ``members``, the pks of the members it
    ``changed``, and the ``member`` whose write failed.
    """

    status: OutcomeStatus
    reason: PlatformReason | None = None
    assessment: PlatformAssessment | None = None
    previous: Platform | None = None
    platform: Platform | None = None
    created: tuple = ()
    error: Exception | None = None
    error_model: type | None = None
    members: tuple = ()
    changed: tuple = ()
    member: object = None


@dataclass(frozen=True)
class ImportPlatform:
    """
    The platform that an import assigns to an object it creates.

    ``reason`` is set when the import could not use the version platform and assigns the OS
    platform instead; ``error`` holds a refused platform create.
    """

    platform: Platform | None
    assessment: PlatformAssessment
    reason: PlatformReason | None = None
    created: Platform | None = None
    error: Exception | None = None


class Report(NamedTuple):
    """One message for the actor; an adapter adds it with ``messages.add_message(request, *report)``."""

    level: int
    text: str


class _Stop(Exception):
    """Stop an application with *outcome*; raised inside a savepoint, it rolls the savepoint back."""

    def __init__(self, outcome):
        super().__init__(outcome.reason)
        self.outcome = outcome


def platform_version_tracking_enabled():
    """Return the installation setting for platform version tracking."""
    return bool(LibreNMSSettings.objects.filter(pk=1).values_list("platform_version_tracking", flat=True).first())


def version_platform_name(os_platform, version):
    """Return the name of a new version platform: ``<OS platform name> <version>``."""
    return f"{os_platform.name} {version}"


def version_platform_slug(name):
    """Return the base slug of a new version platform; dots become dashes so ``21.2.1`` and ``2.12.1`` differ."""
    return slugify(name.replace(".", "-"))


def _free_slug(base, manufacturer):
    """Return *base*, or *base* with the first free ``-2``, ``-3``, ... suffix for *manufacturer*."""
    # slugify drops every character that is not ASCII, so a name such as "华为" has no base.
    base = base or "platform"
    taken = Platform.objects.filter(manufacturer=manufacturer)
    candidate, number = base[:_SLUG_MAX_LENGTH], 1
    while taken.filter(slug=candidate).exists():
        number += 1
        suffix = f"-{number}"
        candidate = f"{base[: _SLUG_MAX_LENGTH - len(suffix)]}{suffix}"
    return candidate


def _version_platforms(os_platform, version):
    """Return the children of *os_platform* whose name is *version* or ends with a space and *version*."""
    return Platform.objects.filter(parent=os_platform).filter(
        Q(name__iexact=version) | Q(name__iendswith=f" {version}")
    )


def _find_os_platform(librenms_os):
    """
    Return ``(platform, reason, named)`` for the OS platform of *librenms_os*.

    An exact name (case-insensitive) wins. Duplicate names defer to a Platform Mapping, which
    can break the tie; without one they are ambiguous.
    """
    by_name = list(Platform.objects.filter(name__iexact=librenms_os)[:2])
    if len(by_name) == 1:
        return by_name[0], None, ()
    mappings = _os_mappings(librenms_os)
    if len(mappings) == 1:
        return mappings[0].netbox_platform, None, ()
    if mappings:
        return None, PlatformReason.OS_MAPPING_AMBIGUOUS, ()
    if by_name:
        return None, PlatformReason.OS_PLATFORM_AMBIGUOUS, tuple(by_name)
    return None, PlatformReason.OS_PLATFORM_MISSING, ()


def _os_mappings(librenms_os):
    """Return up to two Platform Mappings whose OS is *librenms_os* (case-insensitive), with their platforms."""
    return list(PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).select_related("netbox_platform")[:2])


def _may_add_platform(actor):
    return actor is None or actor.has_perm(_ADD_PLATFORM)


def add_platform_grant_is_constrained(actor):
    """
    Return True when every add-platform grant of *actor* has constraints, so only the saved platform decides it.

    A create that the constraints refuse rolls back, and the import then assigns the OS platform.
    """
    from netbox.authentication import ObjectPermissionBackend

    if actor is None or not actor.has_perm(_ADD_PLATFORM) or (actor.is_active and actor.is_superuser):
        return False
    return all(ObjectPermissionBackend().get_all_permissions(actor).get(_ADD_PLATFORM, ()))


def assess_platform(subject, facts, *, tracking, actor=None):
    """
    Assess which platform *facts* call for on *subject*, without a change.

    Args:
        subject: The Device or VirtualMachine, or None for an object that an import creates.
        facts (PlatformFacts): The LibreNMS OS and version.
        tracking (bool): Whether platform version tracking is on.
        actor: The synchronization actor. None skips the permission check, for an assessment that
            no single user reads, such as a cached import validation.

    Returns:
        PlatformAssessment: The assessment.

    """
    if not facts.librenms_os:
        return PlatformAssessment(PlatformState.CURRENT, PlatformReason.NO_OS, facts, tracking)
    os_platform, reason, named = _find_os_platform(facts.librenms_os)
    if os_platform is not None:
        return _assess_under(subject, os_platform, facts, tracking=tracking, actor=actor)
    if reason is not PlatformReason.OS_PLATFORM_MISSING:
        return PlatformAssessment(PlatformState.BLOCKED, reason, facts, tracking, named=named)
    # With a version to track, the OS platform the actor names always needs a new version platform.
    if tracking and facts.version and not _may_add_platform(actor):
        return PlatformAssessment(PlatformState.BLOCKED, PlatformReason.ADD_PLATFORM_DENIED, facts, tracking)
    return PlatformAssessment(
        PlatformState.PROPOSED,
        reason,
        facts,
        tracking,
        operation=PlatformOperation.CREATE_OS_PLATFORM,
    )


def _holds(platform_pk, target, os_platform, tracks_version):
    """
    Return whether a subject on the platform *platform_pk* has the *target* platform.

    Without a tracked version, the target is the OS platform, and each platform under it is current too.
    """
    if platform_pk is None:
        return False
    if platform_pk == target.pk:
        return True
    return not tracks_version and os_platform.get_descendants(include_self=True).filter(pk=platform_pk).exists()


def _assess_under(subject, os_platform, facts, *, tracking, actor):
    """Assess *subject* against the known *os_platform*."""
    current_pk = subject.platform_id if subject is not None else None
    shared = {"facts": facts, "tracking": tracking, "os_platform": os_platform}
    tracks_version = tracking and bool(facts.version)

    def on_or_off(target):
        if _holds(current_pk, target, os_platform, tracks_version):
            return PlatformAssessment(
                PlatformState.CURRENT, PlatformReason.ON_TARGET, target=target, target_name=target.name, **shared
            )
        return PlatformAssessment(
            PlatformState.PROPOSED,
            PlatformReason.OFF_TARGET,
            target=target,
            target_name=target.name,
            operation=PlatformOperation.ASSIGN,
            **shared,
        )

    if not tracks_version:
        return on_or_off(os_platform)

    matches = tuple(_version_platforms(os_platform, facts.version))
    if len(matches) > 1:
        return PlatformAssessment(
            PlatformState.BLOCKED, PlatformReason.VERSION_PLATFORM_AMBIGUOUS, named=matches, **shared
        )
    if matches:
        return on_or_off(matches[0])

    name = version_platform_name(os_platform, facts.version)
    holder = Platform.objects.filter(name__iexact=name).first()
    if holder is not None:
        return PlatformAssessment(
            PlatformState.BLOCKED, PlatformReason.NAME_TAKEN, target_name=name, named=(holder,), **shared
        )
    if not _may_add_platform(actor):
        return PlatformAssessment(PlatformState.BLOCKED, PlatformReason.ADD_PLATFORM_DENIED, target_name=name, **shared)
    return PlatformAssessment(
        PlatformState.PROPOSED,
        PlatformReason.VERSION_PLATFORM_MISSING,
        target_name=name,
        operation=PlatformOperation.CREATE_VERSION_PLATFORM,
        **shared,
    )


class _AddDenied(Exception):
    pass


def _create_platform(actor, assessment, winner, **fields):
    """
    Create one platform in a savepoint, or return the winner of a concurrent create.

    Returns:
        tuple[Platform, bool]: The platform, and whether this call created it.

    Raises:
        _Stop: The actor may not add the platform.
        ValidationError | IntegrityError: NetBox refused the platform, and no winner exists.

    """
    try:
        with transaction.atomic():
            platform = Platform(**fields)
            platform.full_clean()
            platform.save()
            # A constrained grant decides on the saved row; a denial rolls this savepoint back.
            if not saved_in_scope(platform, actor, "add"):
                raise _AddDenied
    except _AddDenied:
        raise _Stop(PlatformOutcome(OutcomeStatus.BLOCKED, PlatformReason.ADD_PLATFORM_DENIED, assessment)) from None
    except (ValidationError, IntegrityError) as exc:
        if classify_conflict(exc):
            raise
        found = winner()
        if found is None:
            raise
        return found, False
    return platform, True


def named_platform(name, manufacturer):
    """
    Return the platform that a named OS platform reuses, or None.

    NetBox keeps a platform name unique per manufacturer. A given *manufacturer*, a Manufacturer or
    its posted pk, narrows the case-insensitive name match to that manufacturer; without one the
    name alone matches. A posted pk that is not an integer names no platform.
    """
    platforms = Platform.objects.filter(name__iexact=name)
    if isinstance(manufacturer, str):
        manufacturer = manufacturer.strip() or None
        if manufacturer is not None and not manufacturer.isdecimal():
            return None
    if manufacturer is not None:
        platforms = platforms.filter(manufacturer=manufacturer)
    return platforms.first()


def _named_os_platform(new_os_platform, actor, named):
    """
    Return the OS platform that the actor names, created when no platform has that name; *named* assesses it.

    The platform that ``named_platform`` finds, now or as the winner of a concurrent create, is reused
    only when the actor may view it.
    """

    def reusable():
        found = named_platform(new_os_platform.name, new_os_platform.manufacturer)
        if found is not None and not platform_visible(found, actor):
            raise _Stop(PlatformOutcome(OutcomeStatus.BLOCKED, PlatformReason.NAMED_PLATFORM_UNAVAILABLE, named))
        return found

    existing = reusable()
    if existing is not None:
        return existing, None
    if not _may_add_platform(actor):
        raise _Stop(PlatformOutcome(OutcomeStatus.BLOCKED, PlatformReason.ADD_PLATFORM_DENIED, named))
    platform, created = _create_platform(
        actor,
        named,
        reusable,
        name=new_os_platform.name,
        slug=_free_slug(slugify(new_os_platform.name), new_os_platform.manufacturer),
        manufacturer=new_os_platform.manufacturer,
    )
    return platform, platform if created else None


def _create_version_platform(assessment, actor):
    """Create the version platform of *assessment*, or return the winner of a concurrent create."""
    os_platform = assessment.os_platform
    if not _may_add_platform(actor):
        raise _Stop(PlatformOutcome(OutcomeStatus.BLOCKED, PlatformReason.ADD_PLATFORM_DENIED, assessment))

    def winner():
        found = list(_version_platforms(os_platform, assessment.facts.version)[:2])
        return found[0] if len(found) == 1 else None

    return _create_platform(
        actor,
        assessment,
        winner,
        name=assessment.target_name,
        slug=_free_slug(version_platform_slug(assessment.target_name), os_platform.manufacturer),
        parent=os_platform,
        manufacturer=os_platform.manufacturer,
    )


def _named_request(new_os_platform, facts, tracking):
    """
    Return the assessment of the OS platform that the actor names, and the outcome that refuses it.

    Returns:
        tuple[PlatformAssessment, PlatformOutcome | None]: The assessment, and the outcome when
        LibreNMS reports no OS, now reports another OS than the actor saw, or with tracking on
        another version.

    """
    named = PlatformAssessment(
        PlatformState.PROPOSED,
        PlatformReason.OS_PLATFORM_MISSING,
        facts,
        tracking,
        target_name=new_os_platform.name,
        operation=PlatformOperation.CREATE_OS_PLATFORM,
    )
    # No OS rule could find a platform named for a device without an OS.
    if not facts.librenms_os:
        return named, PlatformOutcome(OutcomeStatus.BLOCKED, PlatformReason.NO_OS, named)
    # The OS platform lookup ignores case, so the OS comparison does too.
    if _fact(new_os_platform.reported_os).casefold() != facts.librenms_os.casefold():
        return named, PlatformOutcome(OutcomeStatus.STALE, PlatformReason.OS_CHANGED, named)
    if tracking and _fact(new_os_platform.reported_version) != facts.version:
        return named, PlatformOutcome(OutcomeStatus.STALE, PlatformReason.VERSION_CHANGED, named)
    return named, None


def _resolve_named_os_platform(new_os_platform, facts, actor, named, created):
    """
    Return the OS platform that the actor names; a new one is added to *created*.

    The platform is used only while the LibreNMS OS still has no platform.

    Raises:
        _Stop: A platform or a Platform Mapping now matches the OS, or the actor may not use the platform.

    """
    if facts.librenms_os and _find_os_platform(facts.librenms_os)[1] is not PlatformReason.OS_PLATFORM_MISSING:
        raise _Stop(PlatformOutcome(OutcomeStatus.STALE, PlatformReason.OS_PLATFORM_RESOLVED, named))
    os_platform, made = _named_os_platform(new_os_platform, actor, named)
    if made is not None:
        created.append(made)
    return os_platform


def members_to_change(assessment, members):
    """
    Return the *members* whose platform an application of *assessment* changes.

    A blocked assessment, or one without an OS, changes no member. A target that does not exist
    yet changes each member.
    """
    if assessment.state is PlatformState.BLOCKED or assessment.reason is PlatformReason.NO_OS:
        return []
    if assessment.target is None:
        return list(members)
    os_platform, tracks_version = assessment.os_platform, assessment.tracks_version
    return [
        member for member in members if not _holds(member.platform_id, assessment.target, os_platform, tracks_version)
    ]


def _lock_chassis(model, pk, actor, source):
    """
    Lock the members of the virtual chassis of *source* in pk order, and return their pks.

    The binding holds while the subject *pk* and the device whose facts were read are both
    members. The locks keep it until the transaction ends.

    Raises:
        _Stop: The binding changed, or the actor may not change every member.

    """
    # Pk order, so two chassis applications take the member locks in the same order.
    members = model.objects.filter(virtual_chassis_id=source.chassis_id).order_by("pk").select_for_update()
    pks = tuple(members.values_list("pk", flat=True))
    if pk not in pks or source.source_pk not in pks:
        raise _Stop(PlatformOutcome(OutcomeStatus.STALE, PlatformReason.CHASSIS_CHANGED))
    if model.objects.restrict(actor, "change").filter(pk__in=pks).count() != len(pks):
        raise _Stop(PlatformOutcome(OutcomeStatus.BLOCKED, PlatformReason.CHASSIS_MEMBER_DENIED))
    return pks


def _decide(row, seen, facts, *, tracking, actor, new_os_platform):
    """
    Assess the locked *row*, create the platforms that the assessment needs, and return the target platform.

    *seen* records the assessment and the new platforms, so a refused create can be reported.

    Raises:
        _Stop: The assessment is blocked, or it needs an OS platform that the actor did not name.

    """
    named = seen.get("named")
    seen["writing"] = Platform
    seen["assessment"] = named
    if new_os_platform is None:
        assessment = assess_platform(row, facts, tracking=tracking, actor=actor)
    else:
        os_platform = _resolve_named_os_platform(new_os_platform, facts, actor, named, seen["created"])
        assessment = _assess_under(row, os_platform, facts, tracking=tracking, actor=actor)
    seen["assessment"] = assessment
    if assessment.state is PlatformState.CURRENT:
        return assessment.target
    if assessment.state is PlatformState.BLOCKED or assessment.operation is PlatformOperation.CREATE_OS_PLATFORM:
        raise _Stop(PlatformOutcome(OutcomeStatus.BLOCKED, assessment.reason, assessment, previous=row.platform))
    if assessment.operation is not PlatformOperation.CREATE_VERSION_PLATFORM:
        return assessment.target
    platform, created = _create_version_platform(assessment, actor)
    if created:
        seen["created"].append(platform)
    return platform


def apply_platform(model, pk, facts, *, tracking, actor, new_os_platform=None, source=None):
    """
    Assess the subject again under its row lock, create the platforms it needs, and write it.

    The application is one outermost transaction (``run_transaction``). An outcome other than
    APPLIED or UNCHANGED rolls the whole transaction back, so NetBox sends no event for a row
    or a platform that does not stay.

    Args:
        model: Device or VirtualMachine.
        pk: The subject's primary key.
        facts (PlatformFacts): The LibreNMS OS and version.
        tracking (bool): Whether platform version tracking is on.
        actor: The synchronization actor; the write needs ``change`` on the subject, and every
            create needs ``add`` on Platform.
        new_os_platform (NewOSPlatform | None): The OS platform the actor names when none matches
            the LibreNMS OS. A platform with that name is used as it is. When a platform or a
            Platform Mapping now matches the OS, or the OS or the version changed, the outcome is stale.
        source (FactsSource | None): Where *facts* came from. With a virtual chassis, the platform
            goes to every member in pk order: the first member decides it, and each other member
            gets it unless it has it. The actor needs ``change`` on every member, or no member
            changes. When the chassis of the subject or of the source device changed after the
            read, the outcome is stale. None applies to the subject alone, without that check.

    Returns:
        PlatformOutcome: The outcome. A lock conflict on both attempts is raised, not returned.

    Raises:
        TransactionConflict: Both attempts met a lock conflict.
        RuntimeError: The call is inside an atomic block.

    """
    named = None
    if new_os_platform is not None:
        named, stale = _named_request(new_os_platform, facts, tracking)
        if stale is not None:
            return stale
    application = {
        "model": model,
        "pk": pk,
        "facts": facts,
        "tracking": tracking,
        "actor": actor,
        "new_os_platform": new_os_platform,
        "named": named,
        "source": source,
    }
    seen = {}
    try:
        return run_transaction(lambda: _apply_once(seen, **application))
    except _Stop as stop:
        return stop.outcome
    except model.DoesNotExist:
        return PlatformOutcome(OutcomeStatus.BLOCKED, PlatformReason.SUBJECT_UNAVAILABLE)
    except (ValidationError, IntegrityError) as exc:
        # A lock conflict leaves the runner as TransactionConflict; the adapter describes this error.
        # nosemgrep: caught-error-text  # noqa: ERA001
        return _failed_outcome(exc, seen, model)


def _apply_once(seen, model, pk, facts, *, tracking, actor, new_os_platform, named, source):
    """
    Run one attempt of ``apply_platform``, and record its state in *seen* for a refused write.

    Each exception leaves the runner, so the attempt rolls back, and NetBox's events of the attempt go with it.

    Raises:
        _Stop: An outcome other than APPLIED or UNCHANGED.
        Model.DoesNotExist | ValidationError | IntegrityError: The subject is gone, or a write was refused.

    """
    seen.clear()
    seen.update({"created": [], "writing": model, "changed": [], "members": (), "named": named})

    def write(row):
        assessment, target = seen["assessment"], seen["target"]
        if target is None or _holds(row.platform_id, target, assessment.os_platform, assessment.tracks_version):
            return False
        seen["platform"] = target
        seen["writing"] = model
        seen["member"] = row
        seen["changed"].append(row.pk)
        row.platform = target
        return None

    def apply(row):
        if row.pk == pk:
            if source is not None and getattr(row, "virtual_chassis_id", None) != source.chassis_id:
                raise _Stop(PlatformOutcome(OutcomeStatus.STALE, PlatformReason.CHASSIS_CHANGED))
            seen["previous"] = row.platform
        if "target" not in seen:
            seen["target"] = _decide(row, seen, facts, tracking=tracking, actor=actor, new_os_platform=new_os_platform)
        return write(row)

    if source is not None and source.chassis_id is not None:
        seen["members"] = _lock_chassis(model, pk, actor, source)
    for member_pk in seen["members"] or (pk,):
        update_existing_row(model.objects.restrict(actor, "change").filter(pk=member_pk), apply)
    return _written_outcome(seen)


def _failed_outcome(exc, seen, model):
    """Return the outcome of the refused write that *seen* records; a chassis failure names its member."""
    refused = seen["writing"]
    reason = PlatformReason.PLATFORM_INVALID if refused is Platform else None
    failed = {"previous": seen.get("previous"), "platform": seen.get("platform"), "error_model": refused}
    if seen["members"] and refused is model:
        failed["member"] = seen["member"]
    # nosemgrep: caught-error-text  # noqa: ERA001
    return PlatformOutcome(OutcomeStatus.FAILED, reason, seen.get("assessment"), error=exc, **failed)


def _written_outcome(seen):
    """Return the outcome of the application that *seen* records; an unchanged chassis reports its target."""
    assessment, changed = seen["assessment"], tuple(seen["changed"])
    kept = seen["target"] if seen["members"] else seen["previous"]
    return PlatformOutcome(
        OutcomeStatus.APPLIED if changed else OutcomeStatus.UNCHANGED,
        assessment.reason,
        assessment,
        previous=seen["previous"],
        platform=seen["platform"] if changed else kept,
        created=tuple(seen["created"]),
        members=seen["members"],
        changed=changed,
    )


def create_os_platform(new_os_platform, facts, *, tracking, actor):
    """
    Create the OS platform that the actor names for an object that an import creates later.

    The rules are those of ``apply_platform`` with *new_os_platform*. The version platform is not
    created here: the import creates it with the object (``import_platform``).

    Args:
        new_os_platform (NewOSPlatform): The OS platform the actor names. A platform with that
            name is used as it is when the actor may view it.
        facts (PlatformFacts): The LibreNMS OS and version.
        tracking (bool): Whether platform version tracking is on.
        actor: The user; a create needs ``add`` on Platform for the new row.

    Returns:
        PlatformOutcome: APPLIED with the new platform in ``created``, or UNCHANGED for a reused
        platform; the assessment holds the OS platform. A lock conflict on both attempts is raised.

    Raises:
        TransactionConflict: Both attempts met a lock conflict.
        RuntimeError: The call is inside an atomic block.

    """
    named, stale = _named_request(new_os_platform, facts, tracking)
    if stale is not None:
        return stale
    # A refused create leaves the runner as an exception, so NetBox drops the event of the rolled-back row.
    try:
        return run_transaction(lambda: _create_os_platform_once(new_os_platform, facts, actor, named))
    except _Stop as stop:
        return stop.outcome
    except (ValidationError, IntegrityError) as exc:
        refused = {"error_model": Platform}
        # A lock conflict leaves the runner as TransactionConflict; the adapter describes this error.
        # nosemgrep: caught-error-text  # noqa: ERA001
        return PlatformOutcome(OutcomeStatus.FAILED, PlatformReason.PLATFORM_INVALID, named, error=exc, **refused)


def _create_os_platform_once(new_os_platform, facts, actor, named):
    """Run one attempt of ``create_os_platform``."""
    created = []
    os_platform = _resolve_named_os_platform(new_os_platform, facts, actor, named, created)
    status = OutcomeStatus.APPLIED if created else OutcomeStatus.UNCHANGED
    return PlatformOutcome(
        status, assessment=replace(named, os_platform=os_platform), platform=os_platform, created=tuple(created)
    )


def import_platform(facts, *, tracking, actor):
    """
    Return the platform for an object that an import creates, and create its version platform.

    The version platform is created when the actor may add platforms. Otherwise, or when the
    version platform is blocked, the import uses the OS platform and the result names the reason.
    Call it inside the import's transaction, so a failed import also rolls the create back.

    Args:
        facts (PlatformFacts): The LibreNMS OS and version.
        tracking (bool): Whether platform version tracking is on.
        actor: The import's synchronization actor, or None when the import has none.

    Returns:
        ImportPlatform: The platform to assign.

    """
    assessment = assess_platform(None, facts, tracking=tracking)
    if assessment.operation is PlatformOperation.ASSIGN:
        return ImportPlatform(assessment.target, assessment)
    if assessment.os_platform is None:
        return ImportPlatform(None, assessment)
    if assessment.operation is not PlatformOperation.CREATE_VERSION_PLATFORM:
        return ImportPlatform(assessment.os_platform, assessment, assessment.reason)
    if actor is None or not actor.has_perm(_ADD_PLATFORM):
        return ImportPlatform(assessment.os_platform, assessment, PlatformReason.ADD_PLATFORM_DENIED)
    try:
        platform, created = _create_version_platform(assessment, actor)
    except _Stop as stop:
        return ImportPlatform(assessment.os_platform, assessment, stop.outcome.reason)
    except (ValidationError, IntegrityError) as exc:
        if classify_conflict(exc):
            raise
        # The import gives the error to exception_text_for with its own text viewer.
        # nosemgrep: caught-error-text  # noqa: ERA001
        return ImportPlatform(assessment.os_platform, assessment, PlatformReason.PLATFORM_INVALID, error=exc)
    return ImportPlatform(platform, assessment, created=platform if created else None)


def platform_visible(platform, viewer):
    """Return whether *viewer* may view *platform*; None as *viewer* views nothing."""
    return viewer is not None and Platform.objects.restrict(viewer, "view").filter(pk=platform.pk).exists()


def platform_label(platform, viewer):
    """Return the quoted name of *platform* when *viewer* may view it, or a neutral phrase."""
    return f"'{platform.name}'" if platform_visible(platform, viewer) else HIDDEN_PLATFORM


def attempted_label(platform, viewer):
    """
    Return the words for *platform* that an application tried to write.

    A failed write rolls back the platforms it created. A rolled-back platform without a parent is
    the OS platform that the actor named; a version platform is named only when *viewer* may view
    its OS platform.
    """
    if Platform.objects.filter(pk=platform.pk).exists():
        return platform_label(platform, viewer)
    if platform.parent_id is None:
        return f"'{platform.name}'"
    if platform_visible(platform.parent, viewer):
        return f"'{platform.name}'"
    return "a new version platform"


def target_label(assessment, viewer):
    """Return the words for the target platform of *assessment* that *viewer* may read."""
    if assessment.target is not None:
        return platform_label(assessment.target, viewer)
    if not assessment.target_name:
        return "the platforms it needs"
    # A new version platform's name holds the OS platform's name.
    if assessment.os_platform is not None and platform_label(assessment.os_platform, viewer) == HIDDEN_PLATFORM:
        return f"a new version platform for version '{assessment.facts.version}'"
    return f"'{assessment.target_name}'"


def _labels(platforms, viewer):
    labels = [platform_label(platform, viewer) for platform in platforms]
    shown = [label for label in labels if label != HIDDEN_PLATFORM]
    hidden = len(labels) - len(shown)
    if hidden:
        shown.append(HIDDEN_PLATFORM if hidden == 1 else f"{hidden} platforms you cannot view")
    return ", ".join(shown)


def reason_text(reason, assessment, viewer):
    """
    Return the plain words for *reason*; *assessment* gives its parameters.

    A platform that *viewer* may not view is not named. None as *viewer* is text that is saved or
    shown to any user, so it names no platform.
    """
    facts = assessment.facts if assessment is not None else PlatformFacts("", "")
    os_name = facts.librenms_os

    def os_platform():
        return platform_label(assessment.os_platform, viewer)

    def target():
        return target_label(assessment, viewer)

    texts = {
        PlatformReason.NO_OS: lambda: "LibreNMS reports no OS.",
        PlatformReason.ON_TARGET: lambda: "The platform matches LibreNMS.",
        PlatformReason.OFF_TARGET: lambda: f"The platform must be {target()}.",
        PlatformReason.VERSION_PLATFORM_MISSING: lambda: f"The version platform {target()} does not exist yet.",
        PlatformReason.OS_PLATFORM_MISSING: lambda: f"No NetBox platform matches LibreNMS OS '{os_name}'.",
        PlatformReason.OS_PLATFORM_AMBIGUOUS: lambda: (
            f"Multiple platforms match '{os_name}'. Resolve the ambiguity with a Platform Mapping."
        ),
        PlatformReason.OS_MAPPING_AMBIGUOUS: lambda: (
            f"Multiple Platform Mappings match '{os_name}'. Resolve the conflict in Platform Mappings."
        ),
        PlatformReason.VERSION_PLATFORM_AMBIGUOUS: lambda: (
            f"Multiple version platforms under {os_platform()} match version '{facts.version}': "
            f"{_labels(assessment.named, viewer)}."
        ),
        PlatformReason.NAME_TAKEN: lambda: (
            f"The name of the new version platform is taken by {_labels(assessment.named, viewer)}, "
            f"which is outside OS platform {os_platform()}."
        ),
        PlatformReason.ADD_PLATFORM_DENIED: lambda: (
            f"You do not have permission to add platforms, so {target()} cannot be created."
        ),
        PlatformReason.PLATFORM_INVALID: lambda: f"NetBox refused to create {target()}.",
        PlatformReason.SUBJECT_UNAVAILABLE: lambda: "The object no longer exists, or you may not change it.",
        PlatformReason.VERSION_CHANGED: lambda: (
            f"LibreNMS now reports version '{facts.version}', not the version the page showed. "
            "Refresh the page and try again."
        ),
        PlatformReason.OS_CHANGED: lambda: (
            (f"LibreNMS now reports OS '{os_name}'" if os_name else "LibreNMS now reports no OS")
            + ", not the OS the page showed. Refresh the page and try again."
        ),
        PlatformReason.OS_PLATFORM_RESOLVED: lambda: (
            f"A platform now matches LibreNMS OS '{os_name}'. Refresh the page and try again."
        ),
        PlatformReason.NAMED_PLATFORM_UNAVAILABLE: lambda: f"Platform '{assessment.target_name}' is not available.",
        PlatformReason.CHASSIS_MEMBER_DENIED: lambda: (
            "You do not have permission to change every member of the virtual chassis, so no platform was changed."
        ),
        PlatformReason.CHASSIS_CHANGED: lambda: (
            "The virtual chassis changed after LibreNMS was read. Refresh the page and try again."
        ),
    }
    return texts[reason]()


def map_os_platform(librenms_os, platform, actor):
    """
    Add the Platform Mapping from *librenms_os* to the OS *platform* when no mapping exists, and report it.

    The call runs in one outermost transaction (``run_transaction``). The caller's platform work
    has committed before it, so a failed query or a refused insert, a lock conflict too, is a
    warning and never an exception. A refused insert rolls the transaction back, so NetBox sends
    no event for it.

    Args:
        librenms_os (str): The LibreNMS OS; the mapping stores it in lowercase.
        platform (Platform): The OS platform.
        actor: The user; the insert needs ``add`` on PlatformMapping for the new row.

    Returns:
        Report: The message for *actor*.

    """
    librenms_os = librenms_os.lower()
    try:
        return run_transaction(lambda: _map_os_platform(librenms_os, platform, actor))
    except _AddDenied:
        return _mapping_not_created(librenms_os, _MAPPING_DENIED)
    except (ValidationError, DatabaseError, TransactionConflict) as exc:
        return _mapping_not_created(librenms_os, exception_text_for(exc, PlatformMapping, actor))


def _map_os_platform(librenms_os, platform, actor):
    existing = _os_mappings(librenms_os)
    if existing:
        return _existing_mapping_report(librenms_os, existing, platform, actor)
    if not actor.has_perm(_ADD_MAPPING):
        return _mapping_not_created(librenms_os, _MAPPING_DENIED)
    # Read the label before the insert, so a failed query cannot follow a committed mapping.
    added = Report(
        message_levels.SUCCESS, f"Platform mapping '{librenms_os}' to {platform_label(platform, actor)} added"
    )
    try:
        with transaction.atomic():
            mapping = PlatformMapping(librenms_os=librenms_os, netbox_platform=platform)
            mapping.full_clean()
            mapping.save()
            # A constrained grant decides on the saved row; a denial leaves the runner and rolls it back.
            if not saved_in_scope(mapping, actor, "add"):
                raise _AddDenied
    except (ValidationError, IntegrityError) as exc:
        if classify_conflict(exc):
            raise
        # A concurrent request may have added the mapping first.
        winners = _os_mappings(librenms_os)
        if not winners:
            raise
        return _existing_mapping_report(librenms_os, winners, platform, actor)
    return added


def _existing_mapping_report(librenms_os, mappings, platform, actor):
    if len(mappings) > 1:
        ambiguous = PlatformAssessment(
            PlatformState.BLOCKED, PlatformReason.OS_MAPPING_AMBIGUOUS, PlatformFacts(librenms_os, ""), False
        )
        return _mapping_not_created(librenms_os, reason_text(ambiguous.reason, ambiguous, actor))
    mapping = mappings[0]
    if mapping.netbox_platform_id == platform.pk:
        return Report(message_levels.INFO, f"Platform mapping for '{librenms_os}' already exists; not modified.")
    target = platform_label(mapping.netbox_platform, actor)
    return _mapping_not_created(librenms_os, f"a mapping already points to {target}.")


def _mapping_not_created(librenms_os, reason):
    return Report(message_levels.WARNING, f"Platform mapping for '{librenms_os}' not created: {reason}")


def platform_reports(outcome, model, viewer, *, named=False):
    """
    Return the messages of *outcome* for *viewer*.

    Args:
        outcome (PlatformOutcome): The outcome of ``apply_platform`` or ``create_os_platform``.
        model: Device or VirtualMachine, or None when the outcome has no subject.
        viewer: The user who reads the messages; they name only platforms that the user may view.
        named (bool): Whether the actor named the OS platform; then a reused platform is reported.

    Returns:
        tuple[Report, ...]: The messages, in order. A refused outcome gives one ERROR message.

    """
    if outcome.status is OutcomeStatus.FAILED:
        if outcome.error_model is Platform:
            detail = exception_text_for(outcome.error, Platform, viewer)
            target = target_label(outcome.assessment, viewer)
            return (Report(message_levels.ERROR, f"Platform {target} could not be created: {detail}"),)
        action = (
            f"assign platform {attempted_label(outcome.platform, viewer)} to {_subject_label(outcome, model, viewer)}"
        )
        return (Report(message_levels.ERROR, write_failure_message(outcome.error, action, "platform", model, viewer)),)
    if outcome.reason is PlatformReason.SUBJECT_UNAVAILABLE:
        return (Report(message_levels.ERROR, f"{model._meta.verbose_name.capitalize()} no longer exists."),)
    if outcome.status in (OutcomeStatus.BLOCKED, OutcomeStatus.STALE):
        text = reason_text(outcome.reason, outcome.assessment, viewer)
        if outcome.reason is PlatformReason.OS_PLATFORM_MISSING:
            text = f"{text} Use the 'Create & Sync' button to create it first."
        return (Report(message_levels.ERROR, text),)
    reports = [
        Report(message_levels.SUCCESS, f"Created platform {platform_label(platform, viewer)}")
        for platform in outcome.created
    ]
    if outcome.members:
        reports.append(_chassis_report(outcome, viewer))
    elif model is not None:
        reports.append(_assignment_report(outcome, model, viewer))
    os_platform = outcome.assessment.os_platform
    if named and os_platform not in outcome.created:
        label = platform_label(os_platform, viewer)
        reports.append(Report(message_levels.SUCCESS, f"Platform {label} already existed and was used as it is"))
    return tuple(reports)


def _subject_label(outcome, model, viewer):
    """Return the words for the subject of a failed write; a chassis member is named only when *viewer* may view it."""
    if outcome.member is None:
        return f"the {model._meta.verbose_name}"
    # A local import: the import_utils package imports this module.
    from netbox_librenms_plugin.import_utils.disclosure import visible_object_label

    visible = visible_object_label(outcome.member, viewer)
    return f"{visible[0]} '{visible[1]}'" if visible else "a member of the virtual chassis"


def _chassis_report(outcome, viewer):
    platform = platform_label(outcome.platform, viewer)
    total = len(outcome.members)
    if outcome.status is OutcomeStatus.UNCHANGED:
        return Report(message_levels.INFO, f"All {total} virtual chassis members already match platform {platform}")
    return Report(
        message_levels.SUCCESS, f"Platform {platform} set on {len(outcome.changed)} of {total} virtual chassis members"
    )


def _assignment_report(outcome, model, viewer):
    subject = model._meta.verbose_name.capitalize()
    platform = platform_label(outcome.platform, viewer)
    if outcome.status is OutcomeStatus.UNCHANGED:
        return Report(message_levels.INFO, f"{subject} platform is already {platform}")
    if outcome.previous is not None:
        previous = platform_label(outcome.previous, viewer)
        return Report(message_levels.SUCCESS, f"{subject} platform updated from {previous} to {platform}")
    return Report(message_levels.SUCCESS, f"{subject} platform set to {platform}")
