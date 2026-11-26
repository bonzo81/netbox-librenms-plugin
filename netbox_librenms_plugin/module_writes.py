"""
Serialize participating plugin module writers through their complete Device scope.

Claim hardware identities once, then lock every Device in pk order, then validate
membership and topology before Module, bay or component row locks. Module creators
then claim their complete ModuleType set with shared advisory locks. Template writers
claim their type exclusively and recheck membership before materialization. Native
NetBox forms, API jobs and other plugins do not participate in this protocol.
"""

from dataclasses import dataclass, replace

from dcim.models import Device, Module, ModuleBay
from django.core.exceptions import ValidationError
from django.db import connection

from netbox_librenms_plugin.serial_identity import SerialRules, assignment_lock, module_subtree_serials

_CHANGED = "The modules or module bays changed. Refresh Modules and try again."
_MODULE_FIELDS = ("pk", "device_id", "module_bay_id", "module_bay__module_id", "serial")


def _topology(device_ids):
    """Read complete membership, including hidden modules and empty child bays."""
    modules = tuple(Module.objects.filter(device_id__in=device_ids).order_by("pk").values_list(*_MODULE_FIELDS))
    bays = tuple(
        ModuleBay.objects.filter(device_id__in=device_ids).order_by("pk").values_list("pk", "device_id", "module_id")
    )
    return modules, bays


def _subtrees(modules, roots):
    """Return the pks of every root and every Module below it in the topology rows *modules*."""
    found = {root for root in roots if root is not None}
    while added := {row[0] for row in modules if row[3] in found} - found:
        found |= added
    return found


def template_type_lock_key(target_kind, target_pk):
    """Return the one advisory key shared by creation and bay-template materialization."""
    from netbox_librenms_plugin.utils import advisory_lock_key

    if target_kind not in {"device_type", "module_type"}:
        raise ValueError("Unknown module bay template target kind.")
    return advisory_lock_key(f"netbox-librenms-plugin:module-template:{target_kind}:{target_pk}")


def _lock_template_types(type_keys, *, shared):
    """Lock one complete type set after Devices, without relying on deferred native FKs."""
    if not connection.in_atomic_block:
        raise RuntimeError("A module write requires an open transaction.")
    keys = sorted({template_type_lock_key(kind, pk) for kind, pk in type_keys})
    statement = "SELECT pg_advisory_xact_lock_shared(%s)" if shared else "SELECT pg_advisory_xact_lock(%s)"
    with connection.cursor() as cursor:
        for key in keys:
            cursor.execute(statement, [key])


@dataclass(frozen=True)
class ModuleWriteObservation:
    """The complete owner set, topology and released identities read before any lock."""

    device_ids: tuple
    modules: tuple
    bays: tuple
    serials: tuple


def observe_module_write(*, device_ids=(), module_ids=(), subtree_roots=(), observed_ids=()) -> ModuleWriteObservation:
    """
    Observe destinations and existing or removed modules without permission-filtering the owner set.

    The caller still authorizes every actual write. Full per-Device topology lets
    the post-lock check detect new children, removal and same-Device reparenting.
    A concurrent change refuses the observation, rather than expanding its locks.
    The write claims the serials of *module_ids* and *subtree_roots*. It does not claim the
    serials of *observed_ids*: Modules whose topology it protects but whose serial it keeps.
    """
    released = set(module_ids)
    for root in subtree_roots:
        if root is not None:
            released.add(root)
            released.update(pk for pk, _serial in module_subtree_serials(root))
    selected = released | set(observed_ids)
    rows = tuple(Module.objects.filter(pk__in=selected).order_by("pk").values_list(*_MODULE_FIELDS))
    if {row[0] for row in rows} != selected:
        raise ValidationError(_CHANGED)
    owners = tuple(sorted({*device_ids, *(row[1] for row in rows)}))
    modules, bays = _topology(owners)
    if not set(rows).issubset(modules) or not _subtrees(modules, subtree_roots) <= selected:
        raise ValidationError(_CHANGED)
    return ModuleWriteObservation(owners, modules, bays, tuple(row[4] for row in rows if row[0] in released))


@dataclass(frozen=True)
class LockedModuleWrite:
    """Devices locked before downstream rows, plus their original topology."""

    observation: ModuleWriteObservation
    devices: dict
    module_type_ids: frozenset = frozenset()
    types_locked: bool = False

    def lock_module_types(self, module_type_ids):
        """Claim every type a creation batch can use before its first downstream row lock."""
        self._require_type_phase()
        module_type_ids = frozenset(module_type_ids)
        _lock_template_types((("module_type", pk) for pk in module_type_ids), shared=True)
        self.validate()
        return replace(self, module_type_ids=module_type_ids, types_locked=True)

    def lock_template_type(self, target_kind, target_pk):
        """Exclude participating creation while a template rechecks and materializes its instances."""
        self._require_type_phase()
        _lock_template_types([(target_kind, target_pk)], shared=False)
        self.validate()
        return replace(self, types_locked=True)

    def _require_type_phase(self):
        if self.types_locked:
            raise RuntimeError("A module write must claim its complete type set once.")
        self.validate()

    def require_module_types(self, module_type_ids):
        """Refuse a type resolved after planning instead of expanding the type lock set in place."""
        if not self.types_locked or not set(module_type_ids).issubset(self.module_type_ids):
            raise ValidationError(_CHANGED)

    def require_devices(self, device_ids):
        """Refuse an unplanned owner before any newly discovered Module is locked."""
        if not set(device_ids).issubset(self.devices):
            raise ValidationError(_CHANGED)

    def validate(self):
        """Refuse changed topology before downstream row locks or writes."""
        if not connection.in_atomic_block:
            raise RuntimeError("A module write requires an open transaction.")
        if _topology(self.observation.device_ids) != (self.observation.modules, self.observation.bays):
            raise ValidationError(_CHANGED)


def lock_module_write(observation, *, incoming=(), rules: SerialRules | None = None) -> LockedModuleWrite:
    """
    Claim identities once and lock the complete sorted owner set FOR NO KEY UPDATE.

    Read types after the lock statement, so a type change that held a Device row
    cannot leave a pre-lock type cached on the returned Device instances.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("A module write requires an open transaction.")
    assignment_lock([*observation.serials, *incoming], model=Module, rules=rules or SerialRules.load())
    locked = tuple(
        Device.objects.select_for_update(no_key=True)
        .filter(pk__in=observation.device_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if locked != observation.device_ids:
        raise ValidationError(_CHANGED)
    devices = {
        device.pk: device for device in Device.objects.filter(pk__in=locked).select_related("device_type__manufacturer")
    }
    scope = LockedModuleWrite(observation, devices)
    scope.validate()
    return scope
