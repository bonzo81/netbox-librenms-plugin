"""
Platform version tracking: assess and apply the platform of one synchronization subject.

An assessment compares the LibreNMS OS and version facts with NetBox and changes nothing. An
application checks the actor's permissions and the assessment again under the subject's row lock,
creates the platforms it needs, and writes the subject. Neither needs an HTTP request, so the sync
page, import, and a background job are adapters over this module.
"""

from dataclasses import dataclass
from enum import StrEnum

from dcim.models import Platform
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils.text import slugify

from netbox_librenms_plugin.models import LibreNMSSettings, PlatformMapping
from netbox_librenms_plugin.transactions import classify_conflict, update_existing_row

_ADD_PLATFORM = "dcim.add_platform"


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
    """The OS platform that the actor names when no platform matches the LibreNMS OS."""

    name: str
    manufacturer: object = None


@dataclass(frozen=True)
class PlatformOutcome:
    """
    The result of one application.

    ``platform`` is the platform written (or attempted, for a failure). ``error`` and
    ``error_model`` hold the refused write of a failure, so an adapter can describe it.
    """

    status: OutcomeStatus
    reason: PlatformReason | None = None
    assessment: PlatformAssessment | None = None
    previous: Platform | None = None
    platform: Platform | None = None
    created: tuple = ()
    error: Exception | None = None
    error_model: type | None = None


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
    """Return the slug of a new version platform; dots become dashes so ``21.2.1`` and ``2.12.1`` differ."""
    return slugify(name.replace(".", "-"))


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
    mappings = list(
        PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).select_related("netbox_platform")[:2]
    )
    if len(mappings) == 1:
        return mappings[0].netbox_platform, None, ()
    if mappings:
        return None, PlatformReason.OS_MAPPING_AMBIGUOUS, ()
    if by_name:
        return None, PlatformReason.OS_PLATFORM_AMBIGUOUS, tuple(by_name)
    return None, PlatformReason.OS_PLATFORM_MISSING, ()


def _may_add_platform(actor):
    return actor is None or actor.has_perm(_ADD_PLATFORM)


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


def _assess_under(subject, os_platform, facts, *, tracking, actor):
    """Assess *subject* against the known *os_platform*."""
    current_pk = subject.platform_id if subject is not None else None
    shared = {"facts": facts, "tracking": tracking, "os_platform": os_platform}

    def on_or_off(target):
        if current_pk == target.pk:
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

    if not (tracking and facts.version):
        if current_pk is not None and os_platform.get_descendants(include_self=True).filter(pk=current_pk).exists():
            return PlatformAssessment(
                PlatformState.CURRENT,
                PlatformReason.ON_TARGET,
                target=os_platform,
                target_name=os_platform.name,
                **shared,
            )
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
            if not Platform.objects.restrict(actor, "add").filter(pk=platform.pk).exists():
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


def _named_os_platform(new_os_platform, actor, named):
    """Return the OS platform that the actor names, created when no platform has that name; *named* assesses it."""
    existing = Platform.objects.filter(name__iexact=new_os_platform.name).first()
    if existing is not None:
        return existing, None
    if not _may_add_platform(actor):
        raise _Stop(PlatformOutcome(OutcomeStatus.BLOCKED, PlatformReason.ADD_PLATFORM_DENIED, named))
    platform, created = _create_platform(
        actor,
        named,
        lambda: Platform.objects.filter(name__iexact=new_os_platform.name).first(),
        name=new_os_platform.name,
        slug=slugify(new_os_platform.name),
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
        slug=version_platform_slug(assessment.target_name),
        parent=os_platform,
        manufacturer=os_platform.manufacturer,
    )


def apply_platform(model, pk, facts, *, tracking, actor, new_os_platform=None):
    """
    Assess the subject again under its row lock, create the platforms it needs, and write it.

    Args:
        model: Device or VirtualMachine.
        pk: The subject's primary key.
        facts (PlatformFacts): The LibreNMS OS and version.
        tracking (bool): Whether platform version tracking is on.
        actor: The synchronization actor; the write needs ``change`` on the subject, and every
            create needs ``add`` on Platform.
        new_os_platform (NewOSPlatform | None): The OS platform the actor names when none matches
            the LibreNMS OS. A platform with that name is used as it is.

    Returns:
        PlatformOutcome: The outcome. A lock conflict is raised, not returned.

    """
    seen = {"created": [], "writing": model}

    def apply(row):
        seen["previous"] = row.platform
        seen["writing"] = Platform
        if new_os_platform is None:
            assessment = assess_platform(row, facts, tracking=tracking, actor=actor)
        else:
            seen["assessment"] = PlatformAssessment(
                PlatformState.PROPOSED,
                PlatformReason.OS_PLATFORM_MISSING,
                facts,
                tracking,
                target_name=new_os_platform.name,
                operation=PlatformOperation.CREATE_OS_PLATFORM,
            )
            os_platform, created = _named_os_platform(new_os_platform, actor, seen["assessment"])
            if created is not None:
                seen["created"].append(created)
            assessment = _assess_under(row, os_platform, facts, tracking=tracking, actor=actor)
        seen["assessment"] = assessment
        if assessment.state is PlatformState.CURRENT:
            return False
        if assessment.state is PlatformState.BLOCKED or assessment.operation is PlatformOperation.CREATE_OS_PLATFORM:
            raise _Stop(PlatformOutcome(OutcomeStatus.BLOCKED, assessment.reason, assessment, previous=row.platform))
        platform = assessment.target
        if assessment.operation is PlatformOperation.CREATE_VERSION_PLATFORM:
            platform, created = _create_version_platform(assessment, actor)
            if created:
                seen["created"].append(platform)
        seen["platform"] = platform
        seen["writing"] = model
        row.platform = platform
        return None

    try:
        update_existing_row(model.objects.restrict(actor, "change").filter(pk=pk), apply)
    except model.DoesNotExist:
        return PlatformOutcome(OutcomeStatus.BLOCKED, PlatformReason.SUBJECT_UNAVAILABLE)
    except _Stop as stop:
        return stop.outcome
    except (ValidationError, IntegrityError) as exc:
        if classify_conflict(exc):
            raise
        refused = seen["writing"]
        reason = PlatformReason.PLATFORM_INVALID if refused is Platform else None
        failed = {"previous": seen.get("previous"), "platform": seen.get("platform"), "error_model": refused}
        # The adapter gives the error to exception_text_for with its own viewer.
        # nosemgrep: caught-error-text  # noqa: ERA001
        return PlatformOutcome(OutcomeStatus.FAILED, reason, seen.get("assessment"), error=exc, **failed)
    assessment = seen["assessment"]
    if assessment.state is PlatformState.CURRENT:
        return PlatformOutcome(
            OutcomeStatus.UNCHANGED,
            assessment.reason,
            assessment,
            previous=seen["previous"],
            platform=seen["previous"],
            created=tuple(seen["created"]),
        )
    return PlatformOutcome(
        OutcomeStatus.APPLIED,
        assessment.reason,
        assessment,
        previous=seen["previous"],
        platform=seen["platform"],
        created=tuple(seen["created"]),
    )


def import_platform(facts, *, tracking, actor, os_platform=None):
    """
    Return the platform for an object that an import creates, and create its version platform.

    The version platform is created when the actor may add platforms. Otherwise, or when the
    version platform is blocked, the import uses the OS platform and the result names the reason.
    Call it inside the import's transaction, so a failed import also rolls the create back.

    Args:
        facts (PlatformFacts): The LibreNMS OS and version.
        tracking (bool): Whether platform version tracking is on.
        actor: The import's synchronization actor, or None when the import has none.
        os_platform: The OS platform, when the caller already knows it.

    Returns:
        ImportPlatform: The platform to assign.

    """
    if os_platform is None:
        assessment = assess_platform(None, facts, tracking=tracking)
    else:
        assessment = _assess_under(None, os_platform, facts, tracking=tracking, actor=None)
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


def reason_text(reason, assessment):
    """Return the plain words for *reason*; *assessment* gives its parameters."""
    facts = assessment.facts if assessment is not None else PlatformFacts("", "")
    name = assessment.target_name if assessment is not None else ""
    os_platform = assessment.os_platform if assessment is not None else None
    named = ", ".join(f"'{platform}'" for platform in assessment.named) if assessment is not None else ""
    texts = {
        PlatformReason.NO_OS: "LibreNMS reports no OS.",
        PlatformReason.ON_TARGET: "The platform matches LibreNMS.",
        PlatformReason.OFF_TARGET: f"The platform must be '{name}'.",
        PlatformReason.VERSION_PLATFORM_MISSING: f"Version platform '{name}' does not exist yet.",
        PlatformReason.OS_PLATFORM_MISSING: f"No NetBox platform matches LibreNMS OS '{facts.librenms_os}'.",
        PlatformReason.OS_PLATFORM_AMBIGUOUS: (
            f"Multiple platforms match '{facts.librenms_os}'. Resolve the ambiguity with a Platform Mapping."
        ),
        PlatformReason.OS_MAPPING_AMBIGUOUS: (
            f"Multiple Platform Mappings match '{facts.librenms_os}'. Resolve the conflict in Platform Mappings."
        ),
        PlatformReason.VERSION_PLATFORM_AMBIGUOUS: (
            f"Multiple version platforms under '{os_platform}' match version '{facts.version}': {named}."
        ),
        PlatformReason.NAME_TAKEN: (
            f"Platform {named} outside OS platform '{os_platform}' already uses the name '{name}'."
        ),
        PlatformReason.ADD_PLATFORM_DENIED: f"You do not have permission to add platform '{name}'.",
        PlatformReason.PLATFORM_INVALID: f"Platform '{name}' could not be created.",
        PlatformReason.SUBJECT_UNAVAILABLE: "The object no longer exists, or you may not change it.",
    }
    return texts[reason]
