"""Define explicit import intent and executable row plans."""

from dataclasses import dataclass
from enum import StrEnum

from netbox_librenms_plugin.utils import coerce_model_pk, coerce_positive_int

_IMPORT_ROW_CONTROL_PREFIXES = (
    "object_type",
    "vm_placement",
    "role",
    "rack",
    "cluster",
    "host_device",
)
_IMPORT_NAMING_CONTROL_SELECTORS = ("#use-sysname-toggle", "#strip-domain-toggle")


def import_row_hx_include(source_device_id: int) -> str:
    """Return the complete selector list for one import row's HTMX requests."""
    source_device_id = coerce_positive_int(source_device_id)
    selectors = [f"[name={prefix}_{source_device_id}]" for prefix in _IMPORT_ROW_CONTROL_PREFIXES]
    return ", ".join((*selectors, *_IMPORT_NAMING_CONTROL_SELECTORS))


class ImportObjectType(StrEnum):
    """Identify the NetBox model requested for an import row."""

    DEVICE = "device"
    VIRTUAL_MACHINE = "virtualmachine"


class VMPlacementMethod(StrEnum):
    """Identify the requested placement source for a new virtual machine."""

    SITE = "site"
    CLUSTER = "cluster"
    HOST = "host"


class InvalidImportIntent(ValueError):
    """Report malformed or incomplete import controls."""


@dataclass(frozen=True)
class ImportRowIntent:
    """Hold a possibly incomplete row selection during search and HTMX updates."""

    source_device_id: int
    object_type: ImportObjectType
    role_id: int | None = None
    rack_id: int | None = None
    vm_placement_method: VMPlacementMethod | None = None
    cluster_id: int | None = None
    host_device_id: int | None = None

    @property
    def is_vm(self) -> bool:
        """Return whether this intent requests a virtual machine."""
        return self.object_type is ImportObjectType.VIRTUAL_MACHINE


@dataclass(frozen=True)
class DeviceTarget:
    """Hold the assignments for a physical Device import."""

    role_id: int | None
    rack_id: int | None


@dataclass(frozen=True)
class MatchedSitePlacement:
    """Use the NetBox site matched from the LibreNMS location."""


@dataclass(frozen=True)
class ClusterPlacement:
    """Place a virtual machine in one selected NetBox cluster."""

    cluster_id: int


@dataclass(frozen=True)
class HostPlacement:
    """Place a virtual machine on one selected NetBox host Device."""

    host_device_id: int


VMPlacement = MatchedSitePlacement | ClusterPlacement | HostPlacement


@dataclass(frozen=True)
class VirtualMachineTarget:
    """Hold placement and optional role for a virtual-machine import."""

    placement: VMPlacement
    role_id: int | None


ImportTarget = DeviceTarget | VirtualMachineTarget


@dataclass(frozen=True)
class ImportRowPlan:
    """Hold one structurally valid executable import request."""

    source_device_id: int
    target: ImportTarget

    @property
    def is_vm(self) -> bool:
        """Return whether this plan creates a virtual machine."""
        return isinstance(self.target, VirtualMachineTarget)


def _single_value(data, field_name: str, *, required: bool = False) -> str | None:
    """Read one form value and reject duplicate or missing values."""
    values = data.getlist(field_name) if hasattr(data, "getlist") else [data.get(field_name)]
    values = [str(value).strip() for value in values if value not in (None, "")]
    if len(values) > 1:
        raise InvalidImportIntent(f"Submit {field_name} only once.")
    if not values:
        if required:
            raise InvalidImportIntent(f"Missing required import field: {field_name}.")
        return None
    return values[0]


def _optional_pk(data, field_name: str) -> int | None:
    """Read an optional positive model primary key."""
    raw_value = _single_value(data, field_name)
    if raw_value is None:
        return None
    value = coerce_model_pk(raw_value)
    if value is None:
        raise InvalidImportIntent(f"Invalid selection for {field_name}.")
    return value


def parse_import_row_intent(
    data,
    source_device_id: int,
    *,
    require_object_type: bool = False,
) -> ImportRowIntent:
    """Parse one import row without requiring its placement to be complete."""
    source_device_id = coerce_positive_int(source_device_id)
    if source_device_id is None:
        raise InvalidImportIntent("Source device ID must be a positive integer.")

    object_type_field = f"object_type_{source_device_id}"
    raw_object_type = _single_value(data, object_type_field, required=require_object_type)
    if raw_object_type is None:
        object_type = ImportObjectType.DEVICE
    else:
        try:
            object_type = ImportObjectType(raw_object_type)
        except ValueError:
            raise InvalidImportIntent(f"Invalid selection for {object_type_field}.") from None

    placement_field = f"vm_placement_{source_device_id}"
    raw_placement = _single_value(data, placement_field)
    placement_method = None
    if raw_placement is not None:
        try:
            placement_method = VMPlacementMethod(raw_placement)
        except ValueError:
            raise InvalidImportIntent(f"Invalid selection for {placement_field}.") from None

    return ImportRowIntent(
        source_device_id=source_device_id,
        object_type=object_type,
        role_id=_optional_pk(data, f"role_{source_device_id}"),
        rack_id=_optional_pk(data, f"rack_{source_device_id}"),
        vm_placement_method=placement_method,
        cluster_id=_optional_pk(data, f"cluster_{source_device_id}"),
        host_device_id=_optional_pk(data, f"host_device_{source_device_id}"),
    )


def compile_import_row_plan(intent: ImportRowIntent) -> ImportRowPlan:
    """Validate a complete row intent and return its executable plan."""
    if intent.object_type is ImportObjectType.DEVICE:
        if intent.vm_placement_method is not None or intent.cluster_id is not None or intent.host_device_id is not None:
            raise InvalidImportIntent("Device imports cannot include virtual-machine placement.")
        return ImportRowPlan(
            source_device_id=intent.source_device_id,
            target=DeviceTarget(role_id=intent.role_id, rack_id=intent.rack_id),
        )

    if intent.rack_id is not None:
        raise InvalidImportIntent("Virtual-machine imports cannot include a rack selection.")
    if intent.vm_placement_method is None:
        raise InvalidImportIntent("Select a virtual-machine placement method.")

    if intent.vm_placement_method is VMPlacementMethod.SITE:
        if intent.cluster_id is not None or intent.host_device_id is not None:
            raise InvalidImportIntent("Matched-site placement cannot include a cluster or host.")
        placement: VMPlacement = MatchedSitePlacement()
    elif intent.vm_placement_method is VMPlacementMethod.CLUSTER:
        if intent.cluster_id is None or intent.host_device_id is not None:
            raise InvalidImportIntent("Cluster placement requires one cluster and no host.")
        placement = ClusterPlacement(intent.cluster_id)
    else:
        if intent.host_device_id is None or intent.cluster_id is not None:
            raise InvalidImportIntent("Host placement requires one host and no cluster selection.")
        placement = HostPlacement(intent.host_device_id)

    return ImportRowPlan(
        source_device_id=intent.source_device_id,
        target=VirtualMachineTarget(placement=placement, role_id=intent.role_id),
    )


def parse_import_row_plan(data, source_device_id: int) -> ImportRowPlan:
    """Parse and compile one executable import row from submitted form data."""
    return compile_import_row_plan(parse_import_row_intent(data, source_device_id))


def vm_target_mapping(target: VirtualMachineTarget) -> dict[str, int | str]:
    """Convert a virtual-machine target to the existing bulk-operation mapping."""
    if isinstance(target.placement, MatchedSitePlacement):
        method = VMPlacementMethod.SITE
    elif isinstance(target.placement, ClusterPlacement):
        method = VMPlacementMethod.CLUSTER
    else:
        method = VMPlacementMethod.HOST
    mapping: dict[str, int | str] = {"placement": method.value}
    if isinstance(target.placement, ClusterPlacement):
        mapping["cluster_id"] = target.placement.cluster_id
    elif isinstance(target.placement, HostPlacement):
        mapping["host_device_id"] = target.placement.host_device_id
    if target.role_id is not None:
        mapping["device_role_id"] = target.role_id
    return mapping


def serialize_import_plans(plans: list[ImportRowPlan]) -> list[dict]:
    """Serialize executable row plans to primitive background-job data."""
    payload = []
    for plan in plans:
        if isinstance(plan.target, DeviceTarget):
            payload.append(
                {
                    "source_device_id": plan.source_device_id,
                    "object_type": ImportObjectType.DEVICE.value,
                    "role_id": plan.target.role_id,
                    "rack_id": plan.target.rack_id,
                }
            )
            continue

        mapping = vm_target_mapping(plan.target)
        placement = {"method": mapping.pop("placement")}
        if "cluster_id" in mapping:
            placement["cluster_id"] = mapping.pop("cluster_id")
        if "host_device_id" in mapping:
            placement["host_device_id"] = mapping.pop("host_device_id")
        payload.append(
            {
                "source_device_id": plan.source_device_id,
                "object_type": ImportObjectType.VIRTUAL_MACHINE.value,
                "placement": placement,
                "role_id": plan.target.role_id,
            }
        )
    return payload


def _deserialize_vm_placement(placement_data) -> VMPlacement:
    """Deserialize one strict virtual-machine placement payload."""
    if not isinstance(placement_data, dict):
        raise InvalidImportIntent("Virtual-machine import plan requires placement.")
    try:
        method = VMPlacementMethod(placement_data.get("method"))
    except (TypeError, ValueError):
        raise InvalidImportIntent("Virtual-machine import plan has invalid placement.") from None

    if method is VMPlacementMethod.SITE:
        if set(placement_data) != {"method"}:
            raise InvalidImportIntent("Matched-site placement contains unsupported fields.")
        return MatchedSitePlacement()

    if method is VMPlacementMethod.CLUSTER:
        if set(placement_data) != {"method", "cluster_id"}:
            raise InvalidImportIntent("Cluster placement requires one cluster.")
        cluster_id = _payload_optional_pk(placement_data.get("cluster_id"), "cluster_id")
        if cluster_id is None:
            raise InvalidImportIntent("Cluster placement requires one cluster.")
        return ClusterPlacement(cluster_id)

    if set(placement_data) != {"method", "host_device_id"}:
        raise InvalidImportIntent("Host placement requires one host device.")
    host_device_id = _payload_optional_pk(
        placement_data.get("host_device_id"),
        "host_device_id",
    )
    if host_device_id is None:
        raise InvalidImportIntent("Host placement requires one host device.")
    return HostPlacement(host_device_id)


def _deserialize_import_target(entry: dict, object_type: ImportObjectType) -> ImportTarget:
    """Deserialize the model-specific target from one import-plan entry."""
    role_id = _payload_optional_pk(entry.get("role_id"), "role_id")
    if object_type is ImportObjectType.DEVICE:
        if set(entry) - {"source_device_id", "object_type", "role_id", "rack_id"}:
            raise InvalidImportIntent("Device import plan contains unsupported fields.")
        rack_id = _payload_optional_pk(entry.get("rack_id"), "rack_id")
        return DeviceTarget(role_id=role_id, rack_id=rack_id)

    if set(entry) - {"source_device_id", "object_type", "role_id", "placement"}:
        raise InvalidImportIntent("Virtual-machine import plan contains unsupported fields.")
    return VirtualMachineTarget(
        placement=_deserialize_vm_placement(entry.get("placement")),
        role_id=role_id,
    )


def deserialize_import_plans(payload) -> list[ImportRowPlan]:
    """Deserialize and strictly validate primitive background-job plans."""
    if not isinstance(payload, list):
        raise InvalidImportIntent("Import plans must be a list.")

    plans = []
    seen_ids = set()
    for entry in payload:
        if not isinstance(entry, dict):
            raise InvalidImportIntent("Each import plan must be an object.")
        source_device_id = coerce_positive_int(entry.get("source_device_id"))
        if source_device_id is None or source_device_id in seen_ids:
            raise InvalidImportIntent("Import plans must use unique positive source device IDs.")
        seen_ids.add(source_device_id)
        try:
            object_type = ImportObjectType(entry.get("object_type"))
        except (TypeError, ValueError):
            raise InvalidImportIntent("Import plan has an invalid object type.") from None
        target = _deserialize_import_target(entry, object_type)
        plans.append(ImportRowPlan(source_device_id=source_device_id, target=target))
    return plans


def _payload_optional_pk(raw_value, field_name: str) -> int | None:
    """Validate one optional primary key from a serialized plan."""
    if raw_value is None:
        return None
    value = coerce_model_pk(raw_value)
    if value is None:
        raise InvalidImportIntent(f"Invalid {field_name} in import plan.")
    return value


def partition_import_plans(
    plans: list[ImportRowPlan],
) -> tuple[list[int], dict[int, dict[str, int]], dict[int, dict[str, int | str]]]:
    """Partition plans for the existing model-specific bulk operations."""
    device_ids = []
    device_mappings = {}
    vm_mappings = {}
    for plan in plans:
        if isinstance(plan.target, VirtualMachineTarget):
            vm_mappings[plan.source_device_id] = vm_target_mapping(plan.target)
            continue
        device_ids.append(plan.source_device_id)
        mapping = {}
        if plan.target.role_id is not None:
            mapping["device_role_id"] = plan.target.role_id
        if plan.target.rack_id is not None:
            mapping["rack_id"] = plan.target.rack_id
        if mapping:
            device_mappings[plan.source_device_id] = mapping
    return device_ids, device_mappings, vm_mappings
