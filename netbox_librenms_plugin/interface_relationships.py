"""Shared interface relationship discovery and row resolution."""

from dataclasses import dataclass

from dcim.models import Device, Interface
from django.db.models import Q
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.utils import (
    build_librenms_id_qs,
    get_librenms_device_id,
    interface_name_fallback_matches_port,
    is_list_of_dicts,
    normalize_librenms_port_id,
    normalize_relationship_maps,
)


RELATIONSHIP_CANDIDATE_BATCH_SIZE = 64


@dataclass(frozen=True)
class RelationshipMaps:
    """Normalized cached relationship data for one LibreNMS snapshot."""

    lag_members: dict
    sub_interfaces: dict
    ports_by_id: dict


@dataclass(frozen=True)
class RelationshipResolutionContext:
    """Indexes and permission state shared by every row resolver."""

    obj: object
    server_key: str
    catalog_index: dict
    display_index: dict
    related_index: dict
    source_index: dict
    actionable_owner_ids: set
    changeable_interface_ids: set
    can_write: bool


def interface_owner(interface):
    """Return the ``(device_id, virtual_machine_id)`` owner tuple for an interface."""
    return (getattr(interface, "device_id", None), getattr(interface, "virtual_machine_id", None))


def interface_owner_for_object(obj):
    """Return the owner tuple for a Device or VirtualMachine."""
    if isinstance(obj, Device):
        return (obj.pk, None)
    return (None, obj.pk)


def interface_queryset_for_object(obj):
    """Return the interfaces in a Device chassis or VirtualMachine scope."""
    if isinstance(obj, Device):
        if obj.virtual_chassis_id is not None:
            member_ids = obj.virtual_chassis.members.values_list("id", flat=True)
            return Interface.objects.filter(device__in=member_ids)
        return Interface.objects.filter(device=obj)
    if isinstance(obj, VirtualMachine):
        return VMInterface.objects.filter(virtual_machine=obj)
    return None


def relationship_candidate_q(server_key, port_ids, names):
    """Build one query for stable IDs and safe name hints."""
    candidate_q = Q(pk__in=[])
    unique_port_ids = {
        (type(port_id).__name__, str(port_id)): port_id
        for port_id in port_ids
        if normalize_librenms_port_id(port_id) is not None
    }
    for marker in sorted(unique_port_ids):
        port_id = unique_port_ids[marker]
        host_q, oob_q = build_librenms_id_qs(server_key, port_id)
        candidate_q |= host_q | oob_q
    unique_names = sorted({name for name in names if isinstance(name, str) and name})
    if unique_names:
        candidate_q |= Q(name__in=unique_names)
    return candidate_q


def relationship_candidate_ids(obj, server_key, port_ids, names):
    """Find relationship candidates in bounded batches."""
    interface_queryset = interface_queryset_for_object(obj)
    candidate_ids = set()
    unique_port_ids = sorted(
        {port_id for raw_port_id in port_ids if (port_id := normalize_librenms_port_id(raw_port_id)) is not None}
    )
    unique_names = sorted({name for name in names if isinstance(name, str) and name})

    for start in range(0, len(unique_port_ids), RELATIONSHIP_CANDIDATE_BATCH_SIZE):
        batch = unique_port_ids[start : start + RELATIONSHIP_CANDIDATE_BATCH_SIZE]
        candidate_ids.update(
            interface_queryset.filter(relationship_candidate_q(server_key, batch, ())).values_list("pk", flat=True)
        )

    for start in range(0, len(unique_names), RELATIONSHIP_CANDIDATE_BATCH_SIZE):
        batch = unique_names[start : start + RELATIONSHIP_CANDIDATE_BATCH_SIZE]
        candidate_ids.update(interface_queryset.filter(name__in=batch).values_list("pk", flat=True))

    return candidate_ids


def build_interface_index(obj, server_key, user=None, action="change", *, lock=False, allowed_ids=None):
    """Build an ambiguity-preserving interface index for repeated resolution."""
    interface_queryset = interface_queryset_for_object(obj)
    if interface_queryset is None:
        return None

    if allowed_ids is not None:
        interface_queryset = interface_queryset.filter(pk__in=allowed_ids)
    elif user is not None:
        interface_queryset = interface_queryset.restrict(user, action)
    if isinstance(obj, Device):
        interface_queryset = interface_queryset.select_related(
            "bridge__device__virtual_chassis",
            "device__virtual_chassis",
            "device__location",
            "device__rack",
            "device__site",
            "lag__device__virtual_chassis",
            "parent__device__virtual_chassis",
            "untagged_vlan__site",
        )
    else:
        interface_queryset = interface_queryset.select_related(
            "bridge__virtual_machine",
            "virtual_machine",
            "virtual_machine__site",
            "parent__virtual_machine",
            "untagged_vlan__site",
        )
    if lock:
        interface_queryset = interface_queryset.select_for_update(of=("self",)).order_by("pk")

    by_librenms_id = {}
    by_name = {}
    for interface in interface_queryset:
        stored_id = normalize_librenms_port_id(get_librenms_device_id(interface, server_key, auto_save=False))
        if stored_id is not None:
            by_librenms_id.setdefault(stored_id, []).append(interface)
        by_name.setdefault(interface.name, []).append(interface)
    return {"by_lnms_id": by_librenms_id, "by_name": by_name}


def filter_interface_index(index, allowed_ids):
    """Return an interface index limited to the supplied primary keys."""
    return {
        key: {
            value: [interface for interface in interfaces if interface.pk in allowed_ids]
            for value, interfaces in mapping.items()
            if any(interface.pk in allowed_ids for interface in interfaces)
        }
        for key, mapping in index.items()
    }


def resolve_interface_by_port_id(
    obj, port_id: str, server_key: str, name_hint: str = "", expected_owner=None, index=None
):
    """Resolve one stable LibreNMS port ID, with a safe exact-name fallback."""
    if not port_id:
        return None, "port_id is required"

    if index is None:
        index = build_interface_index(obj, server_key)
        if index is None:
            return None, f"Unsupported object type: {type(obj).__name__}"

    target_id = normalize_librenms_port_id(port_id)
    matches = list(index["by_lnms_id"].get(target_id, [])) if target_id is not None else []
    if len(matches) > 1:
        return None, f"LibreNMS port_id {port_id} is ambiguous on {obj} (matches multiple interfaces)"
    if expected_owner is not None:
        owned = [match for match in matches if interface_owner(match) == expected_owner]
        if len(owned) == 1:
            return owned[0], None
    elif len(matches) == 1:
        return matches[0], None

    if name_hint:
        interface, error = _resolve_interface_by_name_hint(
            obj,
            name_hint,
            index=index,
            expected_owner=expected_owner,
        )
        if error:
            return None, error
        if interface is not None:
            if expected_owner is not None and interface_owner(interface) != expected_owner:
                return None, f"Interface name '{name_hint}' resolves to a different owner than the selected row"
            if not interface_name_fallback_matches_port(interface, target_id, server_key):
                stored_id = normalize_librenms_port_id(get_librenms_device_id(interface, server_key, auto_save=False))
                return None, f"Interface name '{name_hint}' is already bound to LibreNMS port_id {stored_id}"
            return interface, None

    if expected_owner is not None and matches:
        return None, f"LibreNMS port_id {port_id} resolves to a different owner than the selected row"
    return None, f"Interface with LibreNMS port_id {port_id} not found on {obj}"


def _resolve_interface_by_name_hint(obj, name_hint, index=None, expected_owner=None):
    """Resolve one exact interface name while preserving ambiguity."""
    if index is not None:
        matches = index["by_name"].get(name_hint, [])
        if expected_owner is not None:
            owned = [match for match in matches if interface_owner(match) == expected_owner]
            if owned:
                matches = owned
        if not matches:
            return None, None
        if len(matches) > 1:
            return None, f"Interface name '{name_hint}' is ambiguous on {obj}"
        return matches[0], None
    try:
        if isinstance(obj, Device):
            if expected_owner is not None and expected_owner[0] is not None:
                interface = Interface.objects.get(device_id=expected_owner[0], name=name_hint)
            elif obj.virtual_chassis_id is not None:
                member_ids = obj.virtual_chassis.members.values_list("id", flat=True)
                interface = Interface.objects.get(device__in=member_ids, name=name_hint)
            else:
                interface = Interface.objects.get(device=obj, name=name_hint)
        else:
            interface = VMInterface.objects.get(virtual_machine=obj, name=name_hint)
        return interface, None
    except (Interface.DoesNotExist, VMInterface.DoesNotExist):
        return None, None
    except (Interface.MultipleObjectsReturned, VMInterface.MultipleObjectsReturned):
        return None, f"Interface name '{name_hint}' is ambiguous on {obj}"


def build_relationship_maps(cached_data):
    """Normalize relationship maps and index host ports by stable ID."""
    lag_members, sub_interfaces = normalize_relationship_maps(cached_data.get("port_stack_relationships"))
    ports = cached_data.get("ports", [])
    if not is_list_of_dicts(ports):
        ports = []
    ports_by_id = {}
    for port in ports:
        if port.get("_source") == "oob":
            continue
        port_id = normalize_librenms_port_id(port.get("port_id"))
        if port_id is not None:
            ports_by_id[port_id] = port
    return RelationshipMaps(lag_members, sub_interfaces, ports_by_id)


def build_candidate_relationship_context(obj, server_key, user, can_write, port_ids, names):
    """Build one permission-aware relationship context for a bounded row set."""
    candidate_queryset = interface_queryset_for_object(obj).filter(
        relationship_candidate_q(server_key, port_ids, names)
    )
    catalog_ids = set(candidate_queryset.values_list("pk", flat=True))
    catalog_index = build_interface_index(obj, server_key, allowed_ids=catalog_ids)

    owner_model = Device if isinstance(obj, Device) else VirtualMachine
    owner_field = "device_id" if isinstance(obj, Device) else "virtual_machine_id"
    owner_ids = candidate_queryset.values_list(owner_field, flat=True)
    actionable_owner_ids = set(
        owner_model.objects.restrict(user, "view").filter(pk__in=owner_ids).values_list("pk", flat=True)
    )
    permitted_queryset = candidate_queryset.filter(**{f"{owner_field}__in": actionable_owner_ids})
    viewable_ids = set(permitted_queryset.restrict(user, "view").values_list("pk", flat=True))
    changeable_ids = set(permitted_queryset.restrict(user, "change").values_list("pk", flat=True))
    display_index = build_interface_index(obj, server_key, allowed_ids=viewable_ids | changeable_ids)
    source_index = filter_interface_index(display_index, changeable_ids)
    return RelationshipResolutionContext(
        obj=obj,
        server_key=server_key,
        catalog_index=catalog_index,
        display_index=display_index,
        related_index=display_index,
        source_index=source_index,
        actionable_owner_ids=actionable_owner_ids,
        changeable_interface_ids=changeable_ids,
        can_write=can_write,
    )


def enrich_port_relationships(
    port,
    relationship_maps,
    interface_name_field="ifName",
    server_key="",
):
    """Add LAG and parent comparison fields to a cached port row."""
    port_id = normalize_librenms_port_id(port.get("port_id"))
    netbox_interface = port.get("netbox_interface")

    def related_interface_matches(netbox_related, librenms_related):
        if netbox_related is None or librenms_related is None:
            return False
        stored_id = normalize_librenms_port_id(
            get_librenms_device_id(netbox_related, server_key or "default", auto_save=False)
        )
        target_id = normalize_librenms_port_id(librenms_related.get("port_id"))
        if stored_id is not None and target_id is not None:
            return stored_id == target_id
        return netbox_related.name in (
            librenms_related.get("ifName"),
            librenms_related.get("ifDescr"),
            librenms_related.get(interface_name_field),
        )

    def relationship_context(port_id_to_related, related_attribute):
        related_port_id = normalize_librenms_port_id(port_id_to_related.get(port_id)) if port_id else None
        related_port = relationship_maps.ports_by_id.get(related_port_id) if related_port_id else None
        related_name = related_port.get(interface_name_field) if related_port else None
        netbox_related = getattr(netbox_interface, related_attribute, None) if netbox_interface else None
        if related_port_id and netbox_interface:
            if netbox_related and related_interface_matches(netbox_related, related_port):
                status = "match"
            elif netbox_related:
                status = "mismatch"
            else:
                status = "missing_nb"
        elif related_port_id:
            status = "missing_nb"
        elif netbox_related:
            status = "missing_lnms"
        else:
            status = None
        return related_name, related_port_id, status

    lag_name, lag_port_id, lag_status = relationship_context(relationship_maps.lag_members, "lag")
    port["librenms_lag_name"] = lag_name
    port["librenms_lag_port_id"] = lag_port_id
    port["lag_sync_status"] = lag_status

    parent_name, parent_port_id, parent_status = relationship_context(
        relationship_maps.sub_interfaces,
        "parent",
    )
    port["librenms_parent_name"] = parent_name
    port["librenms_parent_port_id"] = parent_port_id
    port["parent_sync_status"] = parent_status


def _row_relationship_source_is_actionable(
    context,
    owner,
    port_id,
    resolved_interface,
    *,
    unique_host_port_ids,
    name_fallback_allowed,
    name_hint,
    expected_owner,
):
    """Return whether this row can write a LAG or parent relationship."""
    if not (
        context.can_write
        and owner.pk in context.actionable_owner_ids
        and port_id in unique_host_port_ids
        and resolved_interface is not None
    ):
        return False

    catalog_matches = context.catalog_index["by_lnms_id"].get(port_id, [])
    if catalog_matches:
        return (
            len(catalog_matches) == 1
            and catalog_matches[0].pk == resolved_interface.pk
            and resolved_interface.pk in context.changeable_interface_ids
        )

    if not name_fallback_allowed:
        return False

    source_interface, error = resolve_interface_by_port_id(
        context.obj,
        str(port_id),
        context.server_key,
        name_hint=name_hint,
        expected_owner=expected_owner,
        index=context.source_index,
    )
    return error is None and getattr(source_interface, "pk", None) == resolved_interface.pk


def resolve_relationship_row(
    context,
    port,
    owner,
    interface_name_field,
    unique_host_port_ids,
    unambiguous_name_port_ids,
    relationship_maps,
):
    """Resolve and enrich one relationship row using the shared table and verify rules."""
    port_id = normalize_librenms_port_id(port.get("port_id"))
    if port_id is not None:
        port["port_id"] = port_id
    if port.get("_source") == "oob":
        port["netbox_interface"] = None
        port["exists_in_netbox"] = False
        port["name_fallback_allowed"] = False
        port["relationship_source_resolvable"] = False
        port["lag_target_resolvable"] = False
        port["parent_target_resolvable"] = False
        return None

    name_fallback_allowed = port_id in unambiguous_name_port_ids
    name_hint = (port.get(interface_name_field) or "") if name_fallback_allowed else ""
    expected_owner = interface_owner_for_object(owner)
    resolved_interface = None
    if port_id is not None:
        resolved_interface, _ = resolve_interface_by_port_id(
            context.obj,
            str(port_id),
            context.server_key,
            name_hint=name_hint,
            expected_owner=expected_owner,
            index=context.display_index,
        )
    port["netbox_interface"] = resolved_interface
    port["exists_in_netbox"] = resolved_interface is not None
    port["name_fallback_allowed"] = name_fallback_allowed and resolved_interface is not None

    port["relationship_source_resolvable"] = _row_relationship_source_is_actionable(
        context,
        owner,
        port_id,
        resolved_interface,
        unique_host_port_ids=unique_host_port_ids,
        name_fallback_allowed=name_fallback_allowed,
        name_hint=name_hint,
        expected_owner=expected_owner,
    )

    enrich_port_relationships(port, relationship_maps, interface_name_field, context.server_key)
    for relation in ("lag", "parent"):
        related_port_id = port.get(f"librenms_{relation}_port_id")
        related_port = relationship_maps.ports_by_id.get(related_port_id)
        related_interface = None
        if related_port is not None and related_port_id in unique_host_port_ids:
            related_name_hint = (
                (related_port.get(interface_name_field) or "") if related_port_id in unambiguous_name_port_ids else ""
            )
            catalog_interface, catalog_error = resolve_interface_by_port_id(
                context.obj,
                str(related_port_id),
                context.server_key,
                name_hint=related_name_hint,
                index=context.catalog_index,
            )
            if catalog_error is None:
                permitted_interface, permitted_error = resolve_interface_by_port_id(
                    context.obj,
                    str(related_port_id),
                    context.server_key,
                    name_hint=related_name_hint,
                    index=context.related_index,
                )
                if (
                    permitted_error is None
                    and catalog_interface is not None
                    and getattr(permitted_interface, "pk", None) == catalog_interface.pk
                ):
                    related_interface = permitted_interface
            if (
                relation == "lag"
                and related_interface is not None
                and getattr(related_interface, "type", None) != "lag"
                and related_interface.pk not in context.changeable_interface_ids
            ):
                related_interface = None
        port[f"{relation}_target_resolvable"] = related_interface is not None
    return resolved_interface
