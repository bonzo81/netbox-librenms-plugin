"""Serialize participating plugin module writers through their complete Device scope.

Claim hardware identities once, then lock every Device in pk order, then validate
membership and topology before Module, bay or component row locks. Native NetBox
forms, API jobs and other plugins do not participate in this protocol.
"""

from dataclasses import dataclass

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


@dataclass(frozen=True)
class ModuleWriteObservation:
    """The complete owner set, topology and released identities read before any lock."""

    device_ids: tuple
    modules: tuple
    bays: tuple
    serials: tuple


def observe_module_write(*, device_ids=(), module_ids=(), subtree_roots=()) -> ModuleWriteObservation:
    """Observe destinations and existing or removed modules without permission-filtering the owner set.

    The caller still authorizes every actual write. Full per-Device topology lets
    the post-lock check detect new children, removal and same-Device reparenting.
    A concurrent change refuses the observation, rather than expanding its locks.
    """
    selected = set(module_ids)
    for root in subtree_roots:
        if root is not None:
            selected.add(root)
            selected.update(pk for pk, _serial in module_subtree_serials(root))
    rows = tuple(Module.objects.filter(pk__in=selected).order_by("pk").values_list(*_MODULE_FIELDS))
    if {row[0] for row in rows} != selected:
        raise ValidationError(_CHANGED)
    owners = tuple(sorted({*device_ids, *(row[1] for row in rows)}))
    modules, bays = _topology(owners)
    if not set(rows).issubset(modules):
        raise ValidationError(_CHANGED)
    return ModuleWriteObservation(owners, modules, bays, tuple(row[4] for row in rows))


@dataclass(frozen=True)
class LockedModuleWrite:
    """Devices locked before downstream rows, plus their original topology."""

    observation: ModuleWriteObservation
    devices: dict

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
    """Claim identities once and lock the complete sorted owner set FOR NO KEY UPDATE.

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
