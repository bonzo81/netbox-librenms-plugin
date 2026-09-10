"""
Disclosure control for import-preview validation results.

The conflict searches in :func:`validate_device_for_import` are unrestricted on purpose: a duplicate
the viewer cannot see is still a real conflict that must block the import. Identity is therefore
withheld at DISPLAY time, and this module is the only place that decides it - a second copy could
drift and name an object outside the viewer's scope.

Producers must not put a NetBox object's name or pk into ``warnings``. They record the conflicting
object as structured data (``existing_device``, ``merge_candidates``, ``serial_conflict``) and
:func:`scope_validation_disclosures` renders what the viewer may be told.
"""

from __future__ import annotations

from netbox_librenms_plugin.import_validation_helpers import (
    MERGE_CANDIDATE_SLOTS,
    clear_match_derived_action_fields,
    reset_cluster,
    reset_device_role,
)

# Stand-in for a conflict target the viewer may not see. Shared with :mod:`collisions` so the two
# disclosure paths cannot drift into different labels.
RESTRICTED_OBJECT_LABEL = "restricted NetBox object"

# A user who may CHANGE an object already reads every field of it through NetBox's edit form, so
# both grants count as "may see". Requiring "view" alone would withhold the identity of the very
# object an action view had just resolved through its own ``change`` scope: the plugin's
# conflict-action views authorize with ``change`` and never ask for ``view``.
DISCLOSURE_ACTIONS = ("view", "change")

OUT_OF_SCOPE_MATCH_MESSAGE = (
    "This LibreNMS device matches an existing NetBox object outside your view scope. "
    "Ask an administrator to resolve it before importing."
)

OUT_OF_SCOPE_MERGE_MESSAGE = (
    "This LibreNMS device matches two NetBox objects and at least one is outside your view scope. "
    "Ask an administrator to merge them before importing."
)

_SERIAL_CONFLICT_HIDDEN = (
    "Serial conflict: incoming serial '{serial}' is already assigned to a NetBox device outside "
    "your view scope. Ask an administrator to resolve the duplicate serial."
)
_SERIAL_CONFLICT_VISIBLE = (
    "Serial conflict: incoming serial '{serial}' is already assigned to device '{name}' "
    "(ID: {pk}) in NetBox. Investigate which device should own this serial before {phase}."
)


def models_by_name() -> dict:
    """Return the NetBox models an import row can match, keyed by ``_meta.model_name``."""
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    return {"device": Device, "virtualmachine": VirtualMachine}


class ViewerScope:
    """
    Answers "may this viewer see object X, and what is it called" for a batch of rows.

    One query per model per disclosure action for the whole batch. The table hands it every row it
    is about to render, so scoping a 500-row cached search costs a handful of queries rather than
    one or two per matched row.
    """

    def __init__(self, user):
        self._user = user
        self._models = models_by_name()
        self._asked: dict[str, set] = {}
        self._names: dict[str, dict] = {}

    def prefetch(self, model_name: str, pks) -> None:
        """Resolve every pk not looked up yet, in as few queries as the grants require."""
        pks = {pk for pk in pks if pk is not None}
        asked = self._asked.setdefault(model_name, set())
        missing = pks - asked
        if not missing:
            return
        asked |= missing
        model = self._models.get(model_name)
        if model is None:
            # An unknown model cannot be scope-checked, so nothing about it may be named.
            return
        names = self._names.setdefault(model_name, {})
        for action in DISCLOSURE_ACTIONS:
            remaining = missing - names.keys()
            if not remaining:
                break
            names.update(model.objects.restrict(self._user, action).filter(pk__in=remaining).values_list("pk", "name"))

    def visible(self, model_name: str, pks) -> set:
        """Return the subset of *pks* this viewer may see."""
        pks = {pk for pk in pks if pk is not None}
        self.prefetch(model_name, pks)
        return pks & self._names.get(model_name, {}).keys()

    def can_see(self, model_name: str, pk) -> bool:
        """Whether this viewer may see the object at *pk*.

        Separate from :meth:`name_of` because a NetBox Device may legitimately have no name, and a
        visible-but-unnamed object must not read as withheld.
        """
        if pk is None:
            return False
        self.prefetch(model_name, [pk])
        return pk in self._names.get(model_name, {})

    def name_of(self, model_name: str, pk):
        """Return the object's name when this viewer may see it, else ``None``."""
        self.prefetch(model_name, [pk])
        return self._names.get(model_name, {}).get(pk)


def visible_pks(model, pks, user) -> set:
    """Return the subset of *pks* that *user* may see through any disclosure action."""
    return ViewerScope(user).visible(model._meta.model_name, pks)


def visible_object_label(obj, user) -> tuple[str, str] | None:
    """Return ``(object_label, name)`` when *user* may see *obj*, else ``None``."""
    model_name = obj._meta.model_name
    if not ViewerScope(user).can_see(model_name, obj.pk):
        return None
    return ("VM" if model_name == "virtualmachine" else "device", obj.name)


def scope_validation_disclosures(validations, user) -> None:
    """
    Withhold identity across a batch of import rows, and block each row that loses its match.

    Idempotent, so a request that reaches two display surfaces (the row table and the details
    modal) can call this on the same dicts twice.

    Call it AFTER anything that reads the unrestricted matches for correctness - bulk collision
    detection keys on them, and scoping first would let two rows write the same NetBox device.

    Args:
        validations: Validation dicts from :func:`validate_device_for_import`. Anything that is
            not a dict is ignored, so callers can pass a row's ``.get("_validation")`` directly.
        user: The requesting user whose view scope decides what may be named.
    """
    rows = [validation for validation in validations if isinstance(validation, dict)]
    if not rows:
        return
    scope = ViewerScope(user)
    wanted: dict[str, set] = {}
    for validation in rows:
        for model_name, pk in _referenced_objects(validation):
            wanted.setdefault(model_name, set()).add(pk)
    for model_name, pks in wanted.items():
        scope.prefetch(model_name, pks)
    for validation in rows:
        _scope_one(validation, scope)


def scope_validation_disclosure(validation, user):
    """Withhold identity in one validation dict. See :func:`scope_validation_disclosures`."""
    scope_validation_disclosures([validation], user)
    return validation


def _referenced_objects(validation: dict):
    """Yield every ``(model_name, pk)`` this row would name, so one query can cover the batch."""
    existing = validation.get("existing_device")
    if existing is not None:
        yield existing._meta.model_name, existing.pk
    candidates = validation.get("merge_candidates")
    if isinstance(candidates, dict):
        for slot in MERGE_CANDIDATE_SLOTS:
            candidate = candidates.get(slot)
            if isinstance(candidate, dict) and candidate.get("pk") is not None:
                yield candidate.get("model_name") or "device", candidate["pk"]
    conflict = validation.get("serial_conflict")
    if isinstance(conflict, dict) and conflict.get("pk") is not None:
        yield "device", conflict["pk"]


def _scope_one(validation: dict, scope: ViewerScope) -> None:
    """Apply the disclosure decision to one row."""
    withheld = []
    if _scope_existing_match(validation, scope):
        withheld.append(OUT_OF_SCOPE_MATCH_MESSAGE)
    # elif, not if: a withheld match already cleared merge_candidates, so the peer check would
    # find nothing left to say.
    elif _scope_merge_candidates(validation, scope):
        withheld.append(OUT_OF_SCOPE_MERGE_MESSAGE)
    if withheld:
        _block(validation, withheld)
    # After the reset in _block, so a serial-conflict owner this viewer MAY see is still reported
    # even when the row's own match was withheld: they are different objects.
    _render_serial_conflict(validation, scope)


def _scope_existing_match(validation: dict, scope: ViewerScope) -> bool:
    """Drop an ``existing_device`` the viewer may not see, and everything derived from it."""
    existing = validation.get("existing_device")
    if existing is None or scope.can_see(existing._meta.model_name, existing.pk):
        return False
    validation["existing_device"] = None
    # The serial conflict is a DIFFERENT object, whose visibility is decided on its own, so carry
    # it across the teardown below (which clears it along with the rest of the match state).
    conflict = validation.get("serial_conflict")
    # The same teardown every branch that drops a match performs (``bulk_import``'s refresh
    # branches). The role and cluster matter most here: the validator copies them OFF the matched
    # object, and the modal falls back to printing validation["device_role"] exactly when
    # existing_device is absent, so leaving them would disclose the withheld object's role.
    clear_match_derived_action_fields(validation)
    validation["librenms_id_needs_migration"] = False
    validation["device_type_mismatch"] = False
    reset_device_role(validation)
    reset_cluster(validation)
    # This teardown goes further than the refresh one, which keeps the row's mode: a VM-only
    # Cluster row in the modal would tell the viewer that the withheld object is a VM.
    validation["import_as_vm"] = False
    validation["serial_conflict"] = conflict
    return True


def _scope_merge_candidates(validation: dict, scope: ViewerScope) -> bool:
    """Withdraw the merge suggestion unless the viewer may see BOTH candidates."""
    candidates = validation.get("merge_candidates")
    if not isinstance(candidates, dict):
        return False
    wanted: dict[str, set] = {}
    for slot in MERGE_CANDIDATE_SLOTS:
        candidate = candidates.get(slot)
        if not isinstance(candidate, dict) or candidate.get("pk") is None:
            continue
        wanted.setdefault(candidate.get("model_name") or "device", set()).add(candidate["pk"])
    if not wanted:
        return False
    for model_name, pks in wanted.items():
        if scope.visible(model_name, pks) != pks:
            conflict = validation.get("serial_conflict")
            clear_match_derived_action_fields(validation)
            validation["serial_conflict"] = conflict
            return True
    return False


def _render_serial_conflict(validation: dict, scope: ViewerScope) -> None:
    """Turn the structured serial conflict into the one warning this viewer may be told."""
    conflict = validation.get("serial_conflict")
    if not isinstance(conflict, dict):
        return
    serial = conflict.get("serial") or ""
    pk = conflict.get("pk")
    if not scope.can_see("device", pk):
        message = _SERIAL_CONFLICT_HIDDEN.format(serial=serial)
    else:
        message = _SERIAL_CONFLICT_VISIBLE.format(
            serial=serial,
            # A NetBox Device may have no name; the pk still identifies it for the reader.
            name=scope.name_of("device", pk) or "(unnamed)",
            pk=pk,
            phase=conflict.get("phase") or "importing",
        )
    _append_once(validation, "warnings", message)


def _block(validation: dict, messages: list[str]) -> None:
    """Report *messages* as the row's whole story and hold it closed."""
    # REPLACE the warnings, do not append: every one of them describes the object this viewer may
    # not see (its serial, whether its device type differs, that it exists at all).
    # ``apply_merge_candidates`` resets the list for the same reason - the new state supersedes what
    # earlier detection said. Nothing actionable is lost: the row is terminally blocked for this
    # viewer, so a create-time warning on it could not be acted on anyway.
    validation["warnings"] = list(messages)
    for message in messages:
        # Also an issue: recalculate_validation_status() recomputes can_import from the issues list,
        # so a warning alone would be silently re-enabled by a later recalculation.
        _append_once(validation, "issues", message)
    validation["can_import"] = False
    validation["is_ready"] = False


def _append_once(validation: dict, key: str, message: str) -> None:
    """Append *message* under *key* unless it is already there, keeping repeat calls idempotent."""
    entries = validation.setdefault(key, [])
    if isinstance(entries, list) and message not in entries:
        entries.append(message)
