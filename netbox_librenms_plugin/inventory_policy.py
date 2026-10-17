"""
The inputs that decide which inventory rows the Modules tab renders, loaded once per request.

The table, the grants it signs and every module writer read these inputs from one
:class:`InventoryPolicy`. The fingerprint is a hash of the same loaded rows, so a grant signed
for one policy cannot pass a writer that loads another.
"""

import hashlib
import json
from dataclasses import dataclass
from functools import cached_property

# Change this when the fingerprint reads other inputs or the collector reads them differently.
INVENTORY_POLICY_VERSION = 1

_SERIAL_SCOPE = "serial"


def _concrete_values(obj):
    """Return every stored column of *obj*, keyed by attribute name."""
    return {field.attname: field.value_from_object(obj) for field in obj._meta.concrete_fields}


def _device_identity(device):
    """Return the device fields that attribution and the pattern-less rules read."""
    device_type = device.device_type
    return {
        "pk": device.pk,
        "virtual_chassis_id": device.virtual_chassis_id,
        "vc_position": device.vc_position,
        "serial": device.serial,
        "device_type_id": device.device_type_id,
        "manufacturer_id": device_type.manufacturer_id,
        "model": device_type.model,
        "part_number": device_type.part_number,
    }


def _validate_rules(inventory_rules, serial_rules):
    """Raise RuleConfigurationError for the first rule whose regex or replacement does not work."""
    for rule in inventory_rules:
        if rule.enabled:
            _ = rule._compiled_pattern, rule.compiled_filters
    for rule in serial_rules:
        _ = rule.compiled_match_pattern


@dataclass(frozen=True)
class InventoryPolicy:
    """
    The loaded policy inputs of one Modules page and their fingerprint.

    Attributes:
        page_device_id (int): The device whose Modules page the policy belongs to.
        master_id (int | None): The master of the page device's Virtual Chassis.
        members (tuple): The Virtual Chassis members ordered by pk, or only the page device.
        inventory_rules (tuple): Every InventoryIgnoreRule, enabled or not, ordered by pk.
        serial_rules (tuple): Every serial-scope NormalizationRule, ordered by pk.
        fingerprint (str): The SHA-256 of the canonical JSON of all the inputs above.

    """

    page_device_id: int
    master_id: int | None
    members: tuple
    inventory_rules: tuple
    serial_rules: tuple
    fingerprint: str

    def member(self, device_id):
        """Return the loaded device with pk *device_id* when it is the page device or one of its members."""
        return next((member for member in self.members if member.pk == device_id), None)

    @property
    def page_device(self):
        """Return the loaded page device."""
        return self.member(self.page_device_id)

    @property
    def vc_members(self) -> list:
        """Return the Virtual Chassis members for attribution, or [] for a device outside a Virtual Chassis."""
        return list(self.members) if self.page_device.virtual_chassis_id else []

    def rules_for(self, manufacturer) -> list:
        """Return the enabled rules that apply to *manufacturer*: its own and the unscoped ones, in pk order."""
        manufacturer_id = getattr(manufacturer, "pk", None)
        return [
            rule for rule in self.inventory_rules if rule.enabled and rule.manufacturer_id in {None, manufacturer_id}
        ]

    @cached_property
    def serial_rule_sets(self) -> dict:
        """Return the serial rules in the preloaded form that ``apply_normalization_rules`` reads."""

        def chain(manufacturer_id):
            rules = [rule for rule in self.serial_rules if rule.manufacturer_id == manufacturer_id]
            return sorted(rules, key=lambda rule: (rule.priority, rule.pk))

        manufacturer_ids = {member.device_type.manufacturer_id for member in self.members}
        manufacturer_ids |= {rule.manufacturer_id for rule in self.serial_rules if rule.manufacturer_id}
        sets = {(_SERIAL_SCOPE, manufacturer_id): chain(manufacturer_id) for manufacturer_id in manufacturer_ids}
        sets[(_SERIAL_SCOPE, None)] = chain(None)
        return sets

    def chassis_serial_key(self):
        """Return the member serial key under the serial rules of the Virtual Chassis master, else the page device."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import vc_serial_key

        chassis = self.member(self.master_id) or self.page_device
        return vc_serial_key(chassis.device_type.manufacturer, preloaded_rules=self.serial_rule_sets)


def load_inventory_policy(page_device_id) -> InventoryPolicy:
    """
    Load the inventory policy inputs of one Modules page and validate every rule that the table can apply.

    Args:
        page_device_id (int): The pk of the device whose Modules page is rendered or written.

    Returns:
        InventoryPolicy: The loaded inputs and their fingerprint.

    Raises:
        Device.DoesNotExist: When the page device no longer exists.
        RuleConfigurationError: When an enabled InventoryIgnoreRule stores a regex that does not compile,
            or a serial NormalizationRule stores a pattern or replacement that does not work.

    """
    from dcim.models import Device

    from netbox_librenms_plugin.models import InventoryIgnoreRule, NormalizationRule

    page = Device.objects.select_related("virtual_chassis").get(pk=page_device_id)
    master_id = page.virtual_chassis.master_id if page.virtual_chassis_id else None
    devices = Device.objects.select_related("device_type__manufacturer").order_by("pk")
    if page.virtual_chassis_id:
        members = tuple(devices.filter(virtual_chassis_id=page.virtual_chassis_id))
    else:
        members = tuple(devices.filter(pk=page.pk))
    if page.pk not in {member.pk for member in members}:
        # The page device left its Virtual Chassis between the two reads.
        raise Device.DoesNotExist(f"Device {page_device_id} changed while its inventory policy was read")
    inventory_rules = tuple(InventoryIgnoreRule.objects.order_by("pk"))
    serial_rules = tuple(NormalizationRule.objects.filter(scope=_SERIAL_SCOPE).order_by("pk"))
    _validate_rules(inventory_rules, serial_rules)
    canonical = {
        "version": INVENTORY_POLICY_VERSION,
        "page": {"id": page.pk, "virtual_chassis_id": page.virtual_chassis_id, "master_id": master_id},
        "devices": [_device_identity(member) for member in members],
        "inventory_rules": [_concrete_values(rule) for rule in inventory_rules],
        "serial_rules": [_concrete_values(rule) for rule in serial_rules],
    }
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return InventoryPolicy(
        page_device_id=page.pk,
        master_id=master_id,
        members=members,
        inventory_rules=inventory_rules,
        serial_rules=serial_rules,
        fingerprint=hashlib.sha256(serialized.encode()).hexdigest(),
    )
