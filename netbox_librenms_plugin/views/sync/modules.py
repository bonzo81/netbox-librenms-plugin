"""Sync action views for module/inventory installation from LibreNMS."""

import logging
import re
from urllib.parse import quote_plus

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from netbox_librenms_plugin.utils import (
    AmbiguousLibreNMSIdError,
    coerce_positive_int as _coerce_positive_int,
    find_by_librenms_id,
    get_librenms_device_id,
    get_librenms_sync_device,
    get_module_template_interface_names,
    get_module_types_indexed,
    get_vc_member_positions,
    normalize_inventory_serial,
    rewrite_interface_name_for_vc_member,
    set_librenms_device_id,
)
from netbox_librenms_plugin.views.base.modules_view import _PLACEHOLDER_VALUES
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)

logger = logging.getLogger(__name__)


def _modules_redirect_response(request, sync_url, server_key=None):
    """
    Return a redirect response that works for both classic and HTMX form posts.

    For HTMX requests (those carrying ``HX-Request: true``) we return an empty
    response with an ``HX-Redirect`` header so the browser performs a full navigation
    that picks up Django messages and refreshes the modules table. For non-HTMX
    requests we return a normal Django redirect.

    These module sync actions are server-scoped, so the follow-up page must stay on
    the server whose cache namespace this request just mutated/read. The active
    ``server_key`` is propagated as a ``?server_key=`` query param; when not passed
    explicitly it is read from the request (POST then GET) so every redirect site
    keeps the server context without each caller having to thread it through.

    Args:
        request: The current HTTP request (HTMX header + server_key source).
        sync_url (str): The base sync URL to redirect to.
        server_key: The active LibreNMS server key; read from the request when None.

    Returns:
        HttpResponse: An HTMX ``HX-Redirect`` response, or a Django redirect for a
            classic post.
    """
    if server_key is None:
        server_key = request.POST.get("server_key") or request.GET.get("server_key") or ""
    target = f"{sync_url}?tab=modules"
    if server_key:
        target += f"&server_key={quote_plus(str(server_key))}"
    target += "#librenms-module-table"
    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target
        return response
    return redirect(target)


def _extract_inventory_list(cached_payload):
    """Extract the inventory row list from a cached payload.

    The cache stores ``{"inventory": [...], "librenms_id": ...}``; anything
    else is treated as a cache miss to match BaseModuleTableView.get_context_data.
    """
    if isinstance(cached_payload, dict):
        return cached_payload.get("inventory") or []
    return []


def _get_cached_inventory_for_device(sync_device, server_key, get_cache_key):
    """Return cached inventory for ``sync_device`` validated against device librenms_id.

    Cache entries are namespaced by server key and include ``librenms_id``.
    When both current and cached IDs are valid positive integers, they must
    match; otherwise cached data is treated as stale.
    """
    cached_payload = cache.get(get_cache_key(sync_device, "inventory", server_key=server_key))
    if not isinstance(cached_payload, dict):
        return []

    current_librenms_id = _coerce_positive_int(get_librenms_device_id(sync_device, server_key, auto_save=False))
    cached_librenms_id = _coerce_positive_int(cached_payload.get("librenms_id"))
    if current_librenms_id and cached_librenms_id and current_librenms_id != cached_librenms_id:
        return []

    return _extract_inventory_list(cached_payload)


def _report_install_results(request, installed, skipped, failed):
    """Emit Django messages summarising an install run."""
    if installed:
        messages.success(request, f"Installed {len(installed)} module(s): {', '.join(installed)}")
    if skipped:
        messages.info(request, f"Skipped {len(skipped)}: {'; '.join(skipped)}")
    if failed:
        messages.warning(request, f"Failed {len(failed)}: {'; '.join(failed)}")


def _resolve_target_device_with_validation(page_device, selected_device_id, devices):
    """Resolve a target device and indicate whether selection input was invalid."""
    if not selected_device_id:
        return page_device, False

    try:
        selected_device_id = int(selected_device_id)
    except (TypeError, ValueError):
        return page_device, True

    if selected_device_id == getattr(page_device, "pk", None):
        return page_device, False

    if not getattr(page_device, "virtual_chassis", None):
        return page_device, True

    member = devices.filter(pk=selected_device_id, virtual_chassis=page_device.virtual_chassis).first()
    if member is None:
        return page_device, True
    return member, False


def _resolve_target_device(page_device, selected_device_id, devices):
    """Resolve and validate a target device from row-level VC selection."""
    target_device, _ = _resolve_target_device_with_validation(page_device, selected_device_id, devices)
    return target_device


def _warn_invalid_selected_device(request):
    """Warn the user that selected device input was invalid and fallback was applied."""
    messages.warning(
        request,
        "Invalid selected device context detected; falling back to the page device for this operation.",
    )


class _SerialConflictAmbiguous(Exception):
    """Raised inside ReplaceModuleView's transaction when more than one module
    holds the incoming serial — used to abort the atomic block and surface a
    user-friendly error after the rollback."""

    def __init__(self, serial):
        super().__init__(serial)
        self.serial = serial


class _SerialConflictUnavailable(Exception):
    """Abort replacement when a serial conflict is outside the caller's delete scope."""

    def __init__(self, serial):
        super().__init__(serial)
        self.serial = serial


class _ModuleComponentAdoptionUnavailable(Exception):
    """
    Abort a module write that adopted a component outside the caller's change scope.

    Carries the component so the operator is told WHICH of the eight adoptable types they lack
    ``change`` on; without it all three handlers emitted the same untraceable sentence.

    Args:
        component_model: The component model that could not be adopted, when known.
    """

    def __init__(self, component_model=None):
        self.component_model = component_model
        self.component_label = str(component_model._meta.verbose_name) if component_model is not None else "component"
        super().__init__(f"{self.component_label} is not available for module adoption")


def _get_sync_device_for_inventory(device, server_key):
    """Return the VC sync device used for module inventory cache keys."""
    return get_librenms_sync_device(device, server_key=server_key) or device


def _get_item_port_identity(item):
    """Extract stable port identity metadata from an inventory item."""
    port_id = _coerce_positive_int(item.get("_librenms_port_id") or item.get("port_id"))
    interface_names = []
    for value in [
        item.get("_librenms_ifname"),
        item.get("_librenms_ifdescr"),
        item.get("entPhysicalName"),
        item.get("entPhysicalDescr"),
    ]:
        name = (value or "").strip()
        if name and name not in interface_names:
            interface_names.append(name)
    return port_id, interface_names


def _extract_interface_coordinates(label):
    """Extract slash-delimited numeric interface coordinates from a label."""
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

    return BaseModuleTableView._extract_interface_numeric_coordinates(label)


def _collect_item_interface_coordinates(item):
    """Collect unique numeric interface coordinate tuples from inventory metadata."""
    _, interface_names = _get_item_port_identity(item)
    coordinates = []
    for name in interface_names:
        parts = _extract_interface_coordinates(name)
        if parts and parts not in coordinates:
            coordinates.append(parts)
    return coordinates


def _select_module_interface_by_coordinates(device, module_interfaces, item):
    """Pick a unique best module interface using coordinate similarity scoring."""
    if not module_interfaces:
        return None

    item_coordinates = _collect_item_interface_coordinates(item)
    if not item_coordinates:
        return None

    vc_position = getattr(device, "vc_position", None)
    scored = []

    for interface in module_interfaces:
        coords = _extract_interface_coordinates(getattr(interface, "name", "") or "")
        if not coords:
            continue

        best_score = 0
        for item_coords in item_coordinates:
            score = 0
            if coords and item_coords and coords[-1] == item_coords[-1]:
                score += 4
            if len(coords) >= 2 and len(item_coords) >= 2 and coords[-2] == item_coords[-2]:
                score += 2
            if isinstance(vc_position, int) and vc_position > 0 and coords and coords[0] == vc_position:
                score += 1
            if score > best_score:
                best_score = score

        if best_score > 0:
            scored.append((best_score, getattr(interface, "pk", None), interface))

    if not scored:
        return None

    scored.sort(key=lambda row: row[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][2]


def _module_component_specs():
    """Return the template, device relation, and model used by NetBox module replication."""
    from dcim import models as dcim_models
    from dcim.models import (
        ConsolePort,
        ConsoleServerPort,
        FrontPort,
        Interface,
        ModuleBay,
        PowerOutlet,
        PowerPort,
        RearPort,
    )

    specs = [
        ("consoleporttemplates", "consoleports", ConsolePort),
        ("consoleserverporttemplates", "consoleserverports", ConsoleServerPort),
        ("interfacetemplates", "interfaces", Interface),
        ("powerporttemplates", "powerports", PowerPort),
        ("poweroutlettemplates", "poweroutlets", PowerOutlet),
    ]
    # NetBox 4.7 replicates cooling components too.
    cooling_intake = getattr(dcim_models, "CoolingIntake", None)
    cooling_outflow = getattr(dcim_models, "CoolingOutflow", None)
    if cooling_intake is not None and cooling_outflow is not None:
        specs.append(("coolingintaketemplates", "coolingintakes", cooling_intake))
        specs.append(("coolingoutflowtemplates", "coolingoutflows", cooling_outflow))
    specs.extend(
        [
            ("rearporttemplates", "rearports", RearPort),
            ("frontporttemplates", "frontports", FrontPort),
            ("modulebaytemplates", "modulebays", ModuleBay),
        ]
    )
    return tuple(specs)


def _restricted_module_component_querysets(view):
    """Return change-scoped querysets for every component NetBox can adopt."""
    return {model: view.restricted_queryset(model, "change") for _, _, model in _module_component_specs()}


def _module_template_adoption_name(template_attribute, template, module):
    """Return the exact name that the running NetBox version uses for adoption."""
    if template_attribute == "modulebaytemplates":
        # NetBox 4.4.0 through 4.4.7 instantiate module-bay names without resolving
        # placeholders. This template has no dependent lookup, so it is safe before save.
        return template.instantiate(device=module.device, module=module).name
    # NetBox 4.4 and 4.5 accept only the module argument. Newer releases also
    # resolve the device through module.device, so the compatible call is exact.
    return template.resolve_name(module)


def _authorize_adoptable_module_components(module, component_querysets):
    """Lock and authorize the exact standalone components NetBox can adopt."""
    expected_ids = {}

    for template_attribute, component_attribute, component_model in _module_component_specs():
        names = [
            _module_template_adoption_name(template_attribute, template, module)
            for template in getattr(module.module_type, template_attribute).all()
        ]
        if not names:
            continue

        candidates = list(
            getattr(module.device, component_attribute)
            .select_for_update(of=("self",))
            .filter(module__isnull=True, name__in=names)
        )
        candidates_by_name = {candidate.name: candidate for candidate in candidates}
        candidate_ids = {candidates_by_name[name].pk for name in names if name in candidates_by_name}
        if not candidate_ids:
            continue

        allowed = component_querysets.get(component_model)
        if allowed is None or allowed.filter(pk__in=candidate_ids).count() != len(candidate_ids):
            raise _ModuleComponentAdoptionUnavailable(component_model)
        expected_ids[component_model] = candidate_ids

    return expected_ids


def _save_module_with_expected_component_adoption(module, expected_ids):
    """Save a module and verify that NetBox adopted only the expected components."""
    from django.db.models.signals import post_save

    component_models = tuple(spec[2] for spec in _module_component_specs())
    created_ids = {model: set() for model in component_models}
    adopted_ids = {model: set() for model in component_models}

    def capture_component_save(sender, instance, created, **kwargs):
        if instance.module_id != module.pk:
            return
        if created:
            created_ids[sender].add(instance.pk)
        else:
            adopted_ids[sender].add(instance.pk)

    with transaction.atomic():
        module._adopt_components = True
        module.full_clean()
        for component_model in component_models:
            post_save.connect(capture_component_save, sender=component_model, weak=False)
        try:
            module.save()
        finally:
            for component_model in component_models:
                post_save.disconnect(capture_component_save, sender=component_model)

        actual_ids = {}
        for component_model in component_models:
            ids = adopted_ids[component_model] - created_ids[component_model]
            if ids:
                actual_ids[component_model] = ids
        if actual_ids != expected_ids:
            diverged = next(
                (m for m in component_models if actual_ids.get(m) != expected_ids.get(m)),
                None,
            )
            raise _ModuleComponentAdoptionUnavailable(diverged)
    return sum(len(ids) for ids in actual_ids.values())


def _save_module_with_scoped_component_adoption(module, component_querysets):
    """Authorize, lock, and save every component NetBox can adopt."""
    with transaction.atomic():
        expected_ids = _authorize_adoptable_module_components(module, component_querysets)
        return _save_module_with_expected_component_adoption(module, expected_ids)


def _adopt_existing_template_interfaces(device, module, interfaces):
    """Adopt existing standalone interfaces into an already-installed module by template name."""
    template_names = get_module_template_interface_names(device, module)
    if not template_names:
        return {
            "status": "skipped",
            "reason": "this module type has no interface templates to match against",
        }

    adoptable = list(interfaces.filter(device=device, module__isnull=True, name__in=template_names))
    if not adoptable:
        return {
            "status": "skipped",
            "reason": "no matching standalone interfaces found for this module's interface templates",
        }

    adopted_names = []
    with transaction.atomic():
        for interface in adoptable:
            interface.module = module
            interface.save(update_fields=["module"])
            adopted_names.append(interface.name)

    return {
        "status": "bound",
        "adopted_count": len(adopted_names),
        "interfaces": adopted_names,
    }


def _module_interface_update_message(bind_result, location):
    """
    Compose the success message for a bound module-interface update.

    A single ``post`` can both bind the primary LibreNMS-identified interface
    (``interface``/``port_id`` present) and adopt standalone template interfaces
    (``adopted_count``). When both happen, report both so the primary bind isn't
    silently hidden behind the adoption tally.

    Args:
        bind_result (dict): The bind outcome, read for ``interface`` and
            ``adopted_count``.
        location (str): A human-readable location for the bound interface(s).

    Returns:
        str: The composed success message.
    """
    interface_name = bind_result.get("interface")
    adopted_count = bind_result.get("adopted_count") or 0
    if bind_result.get("changed") is False and not adopted_count:
        return f"No interface changes were needed for {location}."
    if interface_name and adopted_count:
        return (
            f"Updated interface {interface_name} for {location} and "
            f"adopted {adopted_count} existing standalone interface(s)."
        )
    if adopted_count:
        return f"Updated interfaces for {location}: adopted {adopted_count} existing standalone interface(s)."
    if not interface_name:
        # Pure-adoption path (bind_item is None) where a concurrent/duplicate request already
        # adopted the interfaces, so adopted_count is 0 too: neither branch above fired. Without
        # this guard the fall-through renders "Updated interface None for ..." to the user.
        return f"No interface changes were needed for {location}."
    return f"Updated interface {interface_name} for {location}."


def _get_vc_member_positions(device):
    """Compatibility wrapper for VC member position lookups."""
    return get_vc_member_positions(device)


def _rewrite_interface_name_for_vc_member(interface_name, vc_position, member_positions=None):
    """Compatibility wrapper for VC-aware interface name rewriting."""
    return rewrite_interface_name_for_vc_member(
        interface_name,
        vc_position,
        member_positions=member_positions,
    )


def _normalize_module_interface_names_for_vc_member(
    device,
    module,
    changeable_interfaces,
    deletable_interfaces,
):
    """
    Normalize module interface names to the selected VC member position.

    This handles templates with a fixed member index (e.g., Te1/{module}/1)
    when installing onto non-member-1 devices by rewriting names to the member's
    vc_position (e.g., Te3/1/1). If a standalone interface with the rewritten
    name already exists, it is adopted into the module and the newly-created
    conflicting interface is removed.
    """
    result = {
        "renamed": 0,
        "adopted": 0,
        "removed": 0,
        "skipped": 0,
    }

    vc_position = getattr(device, "vc_position", None)
    vc_id = getattr(device, "virtual_chassis_id", None)
    if not isinstance(vc_position, int) or vc_position < 1 or not isinstance(vc_id, int):
        return result
    member_positions = _get_vc_member_positions(device)

    from dcim.models import Interface

    module_interfaces = list(Interface.objects.filter(device=device, module=module).order_by("pk"))

    for interface in module_interfaces:
        if not changeable_interfaces.filter(pk=interface.pk).exists():
            result["skipped"] += 1
            continue
        desired_name = _rewrite_interface_name_for_vc_member(
            interface.name,
            vc_position,
            member_positions=member_positions,
        )
        if not desired_name or desired_name == interface.name:
            continue

        conflict = Interface.objects.filter(device=device, name=desired_name).exclude(pk=interface.pk).first()
        if conflict is not None:
            if getattr(conflict, "module_id", None) is None:
                if not changeable_interfaces.filter(pk=conflict.pk).exists():
                    result["skipped"] += 1
                    continue
                if not deletable_interfaces.filter(pk=interface.pk).exists():
                    result["skipped"] += 1
                    continue
                conflict.module = module
                conflict.save(update_fields=["module"])
                result["adopted"] += 1
                try:
                    interface.delete()
                    result["removed"] += 1
                except Exception:
                    result["skipped"] += 1
            else:
                result["skipped"] += 1
            continue

        interface.name = desired_name
        try:
            interface.full_clean()
            interface.save(update_fields=["name"])
            result["renamed"] += 1
        except Exception:
            result["skipped"] += 1

    return result


def _format_vc_adjustment_summary(adjustments):
    """Format VC member interface normalization summary for UI/status messages."""
    if not adjustments:
        return ""

    parts = []
    if adjustments.get("renamed"):
        parts.append(f"renamed {adjustments['renamed']}")
    if adjustments.get("adopted"):
        parts.append(f"adopted {adjustments['adopted']}")
    if adjustments.get("removed"):
        parts.append(f"removed {adjustments['removed']}")
    if adjustments.get("skipped"):
        parts.append(f"skipped {adjustments['skipped']}")

    return ", ".join(parts)


def _bind_interface_librenms_id(device, item, module_pk, server_key, interfaces):
    """
    Bind LibreNMS ``port_id`` to the best matching NetBox interface.

    Applies only for inventory items carrying stable port identity metadata.
    The binding is non-destructive: if the port ID already belongs to a different
    interface, no reassignment is performed and a conflict is reported.
    """
    from dcim.models import Interface

    port_id, interface_names = _get_item_port_identity(item)
    if not port_id:
        return None

    try:
        existing_owner = find_by_librenms_id(Interface, port_id, server_key)
    except AmbiguousLibreNMSIdError:
        return {
            "status": "conflict",
            "reason": (
                f"port_id {port_id} is ambiguous — it matches more than one interface; "
                "not reassigning. Resolve the duplicate librenms_id first."
            ),
        }
    if existing_owner is not None and not interfaces.filter(pk=existing_owner.pk).exists():
        return {
            "status": "skipped",
            "reason": f"matching interface is not available for port_id {port_id}",
        }
    if existing_owner is not None and existing_owner.device_id != device.pk:
        return {
            "status": "conflict",
            "reason": (
                f"port_id {port_id} already assigned to {existing_owner.device.name}/{existing_owner.name}; "
                "not reassigning"
            ),
        }

    candidate = existing_owner
    if candidate is None and interface_names:
        candidate = interfaces.filter(device=device, name__in=interface_names).first()

    if candidate is None and module_pk:
        module_interfaces = interfaces.filter(device=device, module_id=module_pk)
        if interface_names:
            candidate = module_interfaces.filter(name__in=interface_names).first()
        if candidate is None:
            module_interface_list = list(module_interfaces)
            if module_interface_list:
                coordinate_candidate = _select_module_interface_by_coordinates(device, module_interface_list, item)
                if coordinate_candidate is not None:
                    candidate = coordinate_candidate
                elif len(module_interface_list) == 1:
                    candidate = module_interface_list[0]
                elif len(module_interface_list) > 1:
                    return {
                        "status": "skipped",
                        "reason": f"multiple module interfaces found for port_id {port_id}; manual mapping required",
                    }

    if candidate is None:
        return {
            "status": "skipped",
            "reason": f"no matching interface found for port_id {port_id}",
        }

    update_fields = []
    if module_pk:
        candidate_module_id = getattr(candidate, "module_id", None)
        if candidate_module_id and candidate_module_id != module_pk:
            return {
                "status": "conflict",
                "reason": (f"{candidate.name} already attached to module {candidate_module_id}; not reassigning"),
            }
        if not candidate_module_id:
            candidate.module_id = module_pk
            update_fields.append("module")

    current_port_id = _coerce_positive_int(get_librenms_device_id(candidate, server_key, auto_save=False))
    if current_port_id and current_port_id != port_id:
        return {
            "status": "conflict",
            "reason": f"{candidate.name} already mapped to port_id {current_port_id}; not overwriting",
        }

    if current_port_id != port_id:
        set_librenms_device_id(candidate, port_id, server_key)
        update_fields.append("custom_field_data")

    if update_fields:
        candidate.save(update_fields=sorted(set(update_fields)))

    return {"status": "bound", "interface": candidate.name, "port_id": port_id, "changed": bool(update_fields)}


def _resolve_single_install_binding_item(request, target_device, server_key, get_cache_key):
    """Resolve inventory metadata for single-row install interface binding."""
    ent_index = _coerce_positive_int(request.POST.get("ent_index"))

    if ent_index and server_key:
        sync_device = _get_sync_device_for_inventory(target_device, server_key)
        for item in _get_cached_inventory_for_device(sync_device, server_key, get_cache_key):
            item_index = _coerce_positive_int(item.get("entPhysicalIndex"))
            if item_index == ent_index:
                resolved = dict(item)
                resolved["_binding_source"] = "cache"
                return resolved

    port_id = _coerce_positive_int(request.POST.get("librenms_port_id"))
    ifname = (request.POST.get("librenms_ifname") or "").strip()
    ifdescr = (request.POST.get("librenms_ifdescr") or "").strip()
    name = (request.POST.get("inventory_name") or "").strip()
    descr = (request.POST.get("inventory_descr") or "").strip()

    fallback_item = {}
    if port_id:
        fallback_item["_librenms_port_id"] = port_id
    if ifname:
        fallback_item["_librenms_ifname"] = ifname
    if ifdescr:
        fallback_item["_librenms_ifdescr"] = ifdescr
    if name:
        fallback_item["entPhysicalName"] = name
    if descr:
        fallback_item["entPhysicalDescr"] = descr
    if fallback_item:
        fallback_item["_binding_source"] = "post_fallback"

    return fallback_item or None


def _should_attempt_bind_for_result(result):
    """Return True when a module install result carries a bindable module context."""
    if result.get("status") == "installed":
        return bool(result.get("module_pk"))
    if result.get("status") == "skipped" and result.get("module_pk"):
        return result.get("reason") == "bay already occupied"
    return False


class InstallModuleView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """Install a NetBox Module into a ModuleBay from LibreNMS inventory data."""

    def post(self, request, pk):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

        self.required_object_permissions = {
            "POST": [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ]
        }
        if error := self.require_all_permissions("POST"):
            return error

        page_device = self.restrict_object_or_404(Device, pk=pk)
        target_device, invalid_selected_device = _resolve_target_device_with_validation(
            page_device,
            request.POST.get("selected_device_id"),
            self.restricted_queryset(Device),
        )
        if invalid_selected_device:
            _warn_invalid_selected_device(request)
        changeable_components = _restricted_module_component_querysets(self)
        changeable_interfaces = changeable_components[Interface]
        deletable_interfaces = self.restricted_queryset(Interface, "delete")
        # Validate the posted server_key against configured servers, else fall back to the active
        # client server (resolve_posted_server_key). A blank key (e.g. a fallback render where
        # module_sync.server_key is empty) OR a forged/unconfigured one must degrade rather than scope
        # the bind under a bogus namespace — without a usable key `if bind_item and server_key` below is
        # skipped and the module installs but its interface is never bound to its LibreNMS port_id.
        server_key = self.resolve_posted_server_key(request.POST)
        bind_item = _resolve_single_install_binding_item(request, target_device, server_key, self.get_cache_key)
        serial = request.POST.get("serial", "").strip()
        if serial.lower() in _PLACEHOLDER_VALUES:
            serial = ""
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        try:
            module_bay_id = int(request.POST.get("module_bay_id"))
            module_type_id = int(request.POST.get("module_type_id"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module bay/module type ID.")
            return _modules_redirect_response(request, sync_url, server_key)

        self.restrict_object_or_404(
            ModuleBay, pk=module_bay_id, device=target_device
        )  # verify bay belongs to selected device
        module_type = self.restrict_object_or_404(ModuleType, pk=module_type_id)

        try:
            with transaction.atomic():
                # Re-fetch bay under lock to prevent TOCTOU race with concurrent installs.
                locked_bay = (
                    self.restricted_queryset(ModuleBay)
                    .select_for_update(of=("self",))
                    .filter(pk=module_bay_id, device=target_device)
                    .first()
                )
                if not locked_bay:
                    messages.error(request, "Module bay no longer exists.")
                    return _modules_redirect_response(request, sync_url, server_key)
                if hasattr(locked_bay, "installed_module") and locked_bay.installed_module:
                    messages.warning(request, f"Module bay '{locked_bay.name}' already has a module installed.")
                    return _modules_redirect_response(request, sync_url, server_key)
                module = Module(
                    device=target_device,
                    module_bay=locked_bay,
                    module_type=module_type,
                    serial=serial,
                    status="active",
                )
                adopted_components = _save_module_with_scoped_component_adoption(module, changeable_components)
                vc_adjustments = _normalize_module_interface_names_for_vc_member(
                    target_device,
                    module,
                    changeable_interfaces,
                    deletable_interfaces,
                )

            bind_result = None
            if bind_item and server_key:
                try:
                    bind_result = _bind_interface_librenms_id(
                        target_device,
                        bind_item,
                        module.pk,
                        server_key,
                        changeable_interfaces,
                    )
                except Exception:
                    bind_result = {
                        "status": "failed",
                        "reason": "unexpected error while binding interface to installed module",
                    }

            messages.success(
                request, f"Installed {module_type.model} in {locked_bay.name} (serial: {serial or 'N/A'})."
            )
            if adopted_components:
                messages.warning(
                    request,
                    "Module sync authority applied: adopted "
                    f"{adopted_components} existing standalone component(s) into the module.",
                )
            vc_summary = _format_vc_adjustment_summary(vc_adjustments)
            if vc_summary:
                messages.warning(
                    request,
                    f"VC member interface normalization applied: {vc_summary}.",
                )
            if bind_item and bind_item.get("_binding_source") == "post_fallback":
                messages.warning(
                    request,
                    "Interface identity fallback used posted row metadata because a matching cached "
                    "inventory row was unavailable. Verify the resulting binding.",
                )
            if bind_result and bind_result.get("status") == "bound":
                messages.info(
                    request,
                    f"Bound {bind_result['interface']} to LibreNMS port_id {bind_result['port_id']}.",
                )
            elif bind_result and bind_result.get("status") != "bound":
                messages.warning(
                    request,
                    "Installed module, but interface binding was skipped: "
                    f"{bind_result.get('reason', 'unknown reason')}",
                )
        except _ModuleComponentAdoptionUnavailable as exc:
            messages.error(request, f"A matching {exc.component_label} is not available for module adoption.")
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Failed to install module: {e}")

        return _modules_redirect_response(request, sync_url, server_key)


class InstallBranchView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """Install a module and all its installable descendants from LibreNMS inventory."""

    def post(self, request, pk):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

        self.required_object_permissions = {
            "POST": [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ]
        }
        if error := self.require_all_permissions("POST"):
            return error

        page_device = self.restrict_object_or_404(Device, pk=pk)
        target_device, invalid_selected_device = _resolve_target_device_with_validation(
            page_device,
            request.POST.get("selected_device_id"),
            self.restricted_queryset(Device),
        )
        if invalid_selected_device:
            _warn_invalid_selected_device(request)
        module_bays = self.restricted_queryset(ModuleBay)
        changeable_components = _restricted_module_component_querysets(self)
        changeable_interfaces = changeable_components[Interface]
        deletable_interfaces = self.restricted_queryset(Interface, "delete")
        parent_index = request.POST.get("parent_index")
        server_key = self.resolve_posted_server_key(request.POST)
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        if not parent_index:
            messages.error(request, "Missing parent inventory index.")
            return _modules_redirect_response(request, sync_url, server_key)

        try:
            parent_index = int(parent_index)
        except ValueError:
            messages.error(request, "Invalid parent inventory index.")
            return _modules_redirect_response(request, sync_url, server_key)

        # Get cached inventory data
        sync_device = _get_sync_device_for_inventory(target_device, server_key)
        cached_data = _get_cached_inventory_for_device(sync_device, server_key, self.get_cache_key)
        if not cached_data:
            messages.error(request, "No cached inventory data. Please refresh modules first.")
            return _modules_redirect_response(request, sync_url, server_key)

        # Load ignore rules so the branch respects the same filters shown in the table
        from netbox_librenms_plugin.utils import get_enabled_ignore_rules

        ignore_rules = get_enabled_ignore_rules()
        device_serial = (getattr(target_device, "serial", None) or "").strip()

        # Build index map and collect the branch to install
        index_map = {idx: item for item in cached_data if (idx := item.get("entPhysicalIndex")) is not None}
        branch_items = self._collect_branch(parent_index, cached_data, ignore_rules, device_serial, index_map)

        if not branch_items:
            messages.warning(request, "No installable items found in this branch.")
            return _modules_redirect_response(request, sync_url, server_key)

        # Load module types (with mappings)
        module_types = get_module_types_indexed()
        allowed_module_type_ids = set(self.restricted_queryset(ModuleType).values_list("pk", flat=True))

        # Preload all ModuleBayMappings once to avoid N+1 per-item queries.
        # Filter by device manufacturer so vendor-scoped mappings only apply to
        # matching vendors and mismatched ones are skipped.
        from netbox_librenms_plugin.utils import load_bay_mappings
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        exact_mappings, regex_mappings = load_bay_mappings()
        mfr_id = getattr(getattr(target_device, "device_type", None), "manufacturer_id", None)
        exact_mappings = BaseModuleTableView._filter_mappings_by_manufacturer(exact_mappings, mfr_id)
        regex_mappings = BaseModuleTableView._filter_mappings_by_manufacturer(regex_mappings, mfr_id)

        # Preload module_bay normalization rules once so _match_bay considers the
        # same normalized candidate names as the table/UI matcher.
        from netbox_librenms_plugin.utils import preload_normalization_rules

        norm_rules_bay = preload_normalization_rules("module_bay")

        # Install top-down: each install may create new child bays
        installed = []
        skipped = []
        failed = []

        try:
            with transaction.atomic():
                for item in branch_items:
                    result = self._install_single(
                        target_device,
                        item,
                        index_map,
                        module_types,
                        exact_mappings=exact_mappings,
                        regex_mappings=regex_mappings,
                        manufacturer_id=mfr_id,
                        norm_rules_bay=norm_rules_bay,
                        module_bays=module_bays,
                        allowed_module_type_ids=allowed_module_type_ids,
                        changeable_components=changeable_components,
                        changeable_interfaces=changeable_interfaces,
                        deletable_interfaces=deletable_interfaces,
                    )
                    should_bind = _should_attempt_bind_for_result(result)
                    if result["status"] == "installed":
                        installed.append(result["name"])
                    elif result["status"] == "skipped":
                        skipped.append(f"{result['name']}: {result['reason']}")
                    else:
                        failed.append(f"{result['name']}: {result['reason']}")

                    if should_bind:
                        bind_result = _bind_interface_librenms_id(
                            target_device,
                            item,
                            result.get("module_pk"),
                            server_key,
                            changeable_interfaces,
                        )
                        if bind_result and bind_result["status"] != "bound":
                            skipped.append(f"{result['name']}: {bind_result['reason']}")
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Branch install failed: {e}")
            return _modules_redirect_response(request, sync_url, server_key)

        _report_install_results(request, installed, skipped, failed)
        return _modules_redirect_response(request, sync_url, server_key)

    def _collect_branch(self, parent_index, inventory_data, ignore_rules=None, device_serial="", index_map=None):
        """
        Collect all items in a branch depth-first, parent first.

        Returns items in install order (parent before children).
        Optionally filters items matching 'skip' ignore rules; 'transparent' items
        are excluded from installation but their children are still collected.
        """
        items = []
        parent = next((i for i in inventory_data if i.get("entPhysicalIndex") == parent_index), None)
        if parent:
            if ignore_rules:
                from netbox_librenms_plugin.views.base.modules_view import _check_ignore_rules

                ancestor = index_map.get(parent.get("entPhysicalContainedIn")) if index_map else None
                action = _check_ignore_rules(parent, ancestor, ignore_rules, index_map, device_serial)
                if action == "skip":
                    return []
                if action == "transparent":
                    self._collect_children(
                        parent_index,
                        inventory_data,
                        items,
                        visited={parent_index},
                        ignore_rules=ignore_rules,
                        device_serial=device_serial,
                        index_map=index_map,
                    )
                    return items
            model = (parent.get("entPhysicalModelName") or "").strip()
            if model:
                items.append(parent)
            self._collect_children(
                parent_index,
                inventory_data,
                items,
                visited={parent_index},
                ignore_rules=ignore_rules,
                device_serial=device_serial,
                index_map=index_map,
            )
        return items

    def _collect_children(
        self, parent_idx, inventory_data, items, visited=None, ignore_rules=None, device_serial="", index_map=None
    ):
        """Recursively collect children with models, depth-first.

        When ignore_rules are provided, items matching a 'skip' rule (and their
        subtree) are excluded.  Items matching 'transparent' are not installed but
        their children are still collected at the same depth.
        """
        if visited is None:
            visited = set()
        children = [i for i in inventory_data if i.get("entPhysicalContainedIn") == parent_idx]
        for child in children:
            child_idx = child.get("entPhysicalIndex")
            if child_idx is None:
                continue
            if child_idx in visited:
                continue
            visited.add(child_idx)
            # Apply ignore rules when provided
            if ignore_rules:
                from netbox_librenms_plugin.views.base.modules_view import _check_ignore_rules

                parent_item = index_map.get(child.get("entPhysicalContainedIn")) if index_map else None
                action = _check_ignore_rules(child, parent_item, ignore_rules, index_map, device_serial)
                if action == "skip":
                    continue
                if action == "transparent":
                    # Don't install this item but still collect its children
                    self._collect_children(
                        child_idx, inventory_data, items, visited, ignore_rules, device_serial, index_map
                    )
                    continue
            model = (child.get("entPhysicalModelName") or "").strip()
            if model:
                items.append(child)
            # Always recurse to find deeper items (containers may lack models)
            self._collect_children(child_idx, inventory_data, items, visited, ignore_rules, device_serial, index_map)

    @staticmethod
    def _install_single(
        device,
        item,
        index_map,
        module_types,
        *,
        module_bays,
        allowed_module_type_ids,
        changeable_components,
        changeable_interfaces,
        deletable_interfaces,
        exact_mappings=None,
        regex_mappings=None,
        manufacturer_id=None,
        norm_rules_bay=None,
    ):
        """
        Try to install a single inventory item.

        Re-fetches module bays each time since parent installs create new ones.
        Scopes bay lookup to the correct parent module to handle duplicate bay names.
        """
        from dcim.models import Module

        from netbox_librenms_plugin.utils import resolve_module_type

        model_name = (item.get("entPhysicalModelName") or "").strip()
        # The serial-scope rules strip vendor markers such as Juniper's "S/N ", and the
        # coercion handles the all-digit serials LibreNMS sends as JSON numbers.
        serial = normalize_inventory_serial(
            item.get("entPhysicalSerialNum"), manufacturer=device.device_type.manufacturer
        )
        if serial.lower() in _PLACEHOLDER_VALUES:
            serial = ""
        name = item.get("entPhysicalName", "") or model_name

        # OOB-controller inventory is merged into the cached snapshot for display only (the UI
        # renders those rows read-only). Never install it onto the host: a crafted POST could
        # otherwise target an OOB row by its (offset) entPhysicalIndex. Reject at this shared
        # chokepoint so both the branch and selected install paths are covered.
        if item.get("_source") == "oob":
            return {"status": "skipped", "name": name, "reason": "OOB controller inventory is read-only"}

        # Match module type (direct, then normalization fallback)
        manufacturer = getattr(getattr(device, "device_type", None), "manufacturer", None)
        matched_type = resolve_module_type(model_name, module_types, manufacturer=manufacturer)
        if not matched_type:
            return {"status": "skipped", "name": name, "reason": "no matching type"}
        if matched_type.pk not in allowed_module_type_ids:
            return {"status": "skipped", "name": name, "reason": "no matching type"}

        # Re-fetch module bays (parent install creates new child bays)
        bays = module_bays.filter(device=device).select_related("installed_module__module_type")

        # Use preloaded mappings if provided, otherwise load from DB
        if exact_mappings is None or regex_mappings is None:
            from netbox_librenms_plugin.utils import load_bay_mappings

            exact_mappings, regex_mappings = load_bay_mappings()

        # Determine if this item belongs under an installed module
        # by tracing its LibreNMS parent hierarchy to an installed item
        parent_module_id = InstallBranchView._find_parent_module_id(
            item, index_map, bays, exact_mappings, regex_mappings
        )

        bay_dict = InstallBranchView._candidate_bays_for_item(bays, parent_module_id)

        # Match module bay using preloaded mapping data
        matched_bay = InstallBranchView._match_bay(
            item,
            index_map,
            bay_dict,
            exact_mappings,
            regex_mappings,
            manufacturer_id=manufacturer_id,
            norm_rules_bay=norm_rules_bay,
        )
        if not matched_bay:
            return {"status": "skipped", "name": name, "reason": "no matching bay"}

        # Install (lock bay to prevent concurrent installs)
        try:
            with transaction.atomic():  # savepoint: failure here won't abort parent tx
                locked_bay = (
                    module_bays.select_for_update(of=("self",))
                    .select_related("installed_module")
                    .get(pk=matched_bay.pk)
                )
                if hasattr(locked_bay, "installed_module") and locked_bay.installed_module:
                    return {
                        "status": "skipped",
                        "name": name,
                        "reason": "bay already occupied",
                        "module_pk": locked_bay.installed_module.pk,
                    }

                module = Module(
                    device=device,
                    module_bay=locked_bay,
                    module_type=matched_type,
                    serial=serial,
                    status="active",
                )
                adopted_components = _save_module_with_scoped_component_adoption(module, changeable_components)
                vc_adjustments = _normalize_module_interface_names_for_vc_member(
                    device,
                    module,
                    changeable_interfaces,
                    deletable_interfaces,
                )
        except _ModuleComponentAdoptionUnavailable as exc:
            return {
                "status": "skipped",
                "name": name,
                "reason": f"a matching {exc.component_label} is not available for module adoption",
            }
        except (ValidationError, IntegrityError) as e:
            error_msg = str(e)
            if "dcim_interface_unique_device_name" in error_msg:
                error_msg = (
                    "duplicate interface name — this module type's interface template "
                    "uses the '{module}' token which resolves to the same name for all siblings. "
                    "An interface naming plugin with a rewrite rule for this module type can fix this."
                )
            return {"status": "failed", "name": name, "reason": error_msg}

        name = f"{matched_type.model} → {matched_bay.name}"
        if adopted_components:
            name += f" (adopted {adopted_components} existing component(s))"
        vc_summary = _format_vc_adjustment_summary(vc_adjustments)
        if vc_summary:
            name += f" (vc normalize: {vc_summary})"

        return {
            "status": "installed",
            "name": name,
            "module_pk": module.pk,
            "adopted_components": adopted_components,
            "vc_adjustments": vc_adjustments,
        }

    @staticmethod
    def _find_parent_module_id(item, index_map, device_bays, exact_mappings, regex_mappings):
        """
        Find the NetBox module ID for the installed parent of this inventory item.

        Walks up the LibreNMS hierarchy to find an ancestor whose name matches
        an installed module bay on the device.

        Args:
            item: The inventory item dict.
            index_map: Dict mapping entPhysicalIndex to inventory item.
            device_bays: Pre-fetched queryset/list of ModuleBay objects for the device.
            exact_mappings: Pre-filtered list of exact ModuleBayMapping objects.
            regex_mappings: Pre-filtered list of regex ModuleBayMapping objects.
        """
        current = item
        # Build bay name → list of bays for duplicate-name disambiguation
        bay_by_name: dict = {}
        for bay in device_bays:
            bay_by_name.setdefault(bay.name, []).append(bay)

        # Build exact_mapping index: prefer class-specific over class-empty
        exact_mapping_by_key: dict = {}
        for m in exact_mappings:
            key = (m.librenms_name, m.librenms_class)
            if key not in exact_mapping_by_key:
                exact_mapping_by_key[key] = m

        visited = set()
        while True:
            parent_idx = current.get("entPhysicalContainedIn", 0)
            if not parent_idx or parent_idx not in index_map:
                return None
            if parent_idx in visited:
                return None
            visited.add(parent_idx)
            parent = index_map[parent_idx]
            parent_name = parent.get("entPhysicalName", "")
            parent_descr = parent.get("entPhysicalDescr", "")
            parent_class = parent.get("entPhysicalClass", "")

            # Check if this parent matches an installed module bay on the device
            for bay in device_bays:
                if hasattr(bay, "installed_module") and bay.installed_module:
                    if bay.name == parent_name or (parent_descr and bay.name == parent_descr):
                        return bay.installed_module.pk

            # Also check ModuleBayMapping for indirect matches (exact then regex)
            for name in [parent_name, parent_descr]:
                if not name:
                    continue
                # Exact-name mapping: prefer class-specific, fall back to class-empty
                mapping = exact_mapping_by_key.get((name, parent_class))
                if not mapping:
                    mapping = exact_mapping_by_key.get((name, ""))
                if mapping:
                    candidates = bay_by_name.get(mapping.netbox_bay_name, [])
                    if len(candidates) == 1:
                        bay = candidates[0]
                    else:
                        occupied = [b for b in candidates if hasattr(b, "installed_module") and b.installed_module]
                        bay = occupied[0] if len(occupied) == 1 else None
                    if bay and hasattr(bay, "installed_module") and bay.installed_module:
                        return bay.installed_module.pk
                # Regex mapping: class-specific first, then empty-class fallback
                # (concatenate, don't use ``or``, so fallback is tried even when
                # class-specific rules exist but none match — mirrors base view)
                class_matches = [rm for rm in regex_mappings if rm.librenms_class == parent_class]
                fallback_matches = [rm for rm in regex_mappings if rm.librenms_class == ""]
                for rm in class_matches + fallback_matches:
                    compiled = rm._compiled_pattern
                    if compiled is None:
                        continue
                    try:
                        match = compiled.fullmatch(name)
                    except re.error:
                        continue
                    if not match:
                        continue
                    try:
                        bay_name = match.expand(rm.netbox_bay_name)
                    except (re.error, IndexError):
                        continue
                    candidates = bay_by_name.get(bay_name, [])
                    if len(candidates) == 1:
                        bay = candidates[0]
                    else:
                        occupied = [b for b in candidates if hasattr(b, "installed_module") and b.installed_module]
                        bay = occupied[0] if len(occupied) == 1 else None
                    if bay and hasattr(bay, "installed_module") and bay.installed_module:
                        return bay.installed_module.pk

            current = parent

    @staticmethod
    def _candidate_bays_for_item(bays, parent_module_id):
        """
        Return the name→bay dict to match an inventory item against.

        When the item traces to an installed parent module, scope to that module's
        child bays (so duplicate bay names resolve to the right parent). Otherwise
        match against the SAME combined set the table uses (``all_bays``): device-level
        bays plus every module-scoped bay, device bays winning on a name collision.
        That combined fallback is essential for items whose matched bay is
        module-scoped but whose hierarchy does not resolve a parent module — the common
        case being a synthetic transceiver rendered as a top-level row whose
        ``Transceiver N/M`` bay lives under an installed line card (those bays are
        module-scoped, never top-level). Without it, bulk install skips such a row as
        "no matching bay" even though the table matched it and a single install (which
        trusts the table's bay) succeeds.

        This is the WRITE path (bulk install), so a bay name that appears under more
        than one installed module — and isn't overridden by a device-level bay — is
        dropped rather than resolved by lowest module PK: installing into an
        arbitrarily-picked sibling module's same-named bay would be a wrong-bay write.
        The row then skips as "no matching bay", exactly as it did when the fallback
        only considered device-level bays.

        Args:
            bays: The candidate module bays for the device.
            parent_module_id: The installed parent module id, or falsy to use the
                combined device + module-scoped set.

        Returns:
            dict: A ``name -> bay`` mapping to match the inventory item against.
        """
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        if parent_module_id:
            return {bay.name: bay for bay in bays if bay.module_id == parent_module_id}

        device_bays = {bay.name: bay for bay in bays if not bay.module_id}
        module_scoped_bays: dict = {}
        for bay in bays:
            if bay.module_id:
                module_scoped_bays.setdefault(bay.module_id, {})[bay.name] = bay
        all_bays = BaseModuleTableView._compute_all_bays(device_bays, module_scoped_bays)

        # Names defined by two or more DIFFERENT modules are ambiguous install targets.
        seen_module: dict = {}
        ambiguous: set = set()
        for mid, named in module_scoped_bays.items():
            for bay_name in named:
                if bay_name in seen_module and seen_module[bay_name] != mid:
                    ambiguous.add(bay_name)
                seen_module[bay_name] = mid
        # A device-level bay with the same name isn't ambiguous — device bays win the
        # merge in _compute_all_bays, matching the pre-fallback behaviour.
        ambiguous -= set(device_bays)
        if ambiguous:
            logger.info(
                "Bulk install: dropping ambiguous module-scoped bay name(s) %s — defined by "
                "multiple installed modules and the item's hierarchy resolves no parent module; "
                "matching rows will skip as 'no matching bay' instead of installing into an "
                "arbitrary module's bay.",
                sorted(ambiguous),
            )
            for bay_name in ambiguous:
                all_bays.pop(bay_name, None)
        return all_bays

    @staticmethod
    def _match_bay(
        item,
        index_map,
        module_bays,
        exact_mappings,
        regex_mappings,
        manufacturer_id=None,
        norm_rules_bay=None,
    ):
        """Match an inventory item to a module bay (same logic as BaseModuleTableView)."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        phys_class = item.get("entPhysicalClass", "")

        # Build candidates using the same parent/label extraction path as the
        # table-side matcher to keep install outcomes aligned with UI state.
        # When preloaded module_bay normalization rules are supplied, include the
        # normalized candidate variants too — this mirrors
        # BaseModuleTableView._match_module_bay so installs don't skip bays that
        # appear matched in the UI when normalization rules are in play. Rules are
        # required as a preloaded dict (callers preload once) to avoid per-item DB
        # queries inside install loops.
        candidate_names = BaseModuleTableView._build_bay_candidate_names(
            item,
            index_map,
            include_normalized=norm_rules_bay is not None,
            norm_rules_bay=norm_rules_bay,
        )

        # Check mapping for each candidate (exact match)
        for name in candidate_names:
            bay = BaseModuleTableView._lookup_exact_bay_mapping(
                name, phys_class, module_bays, exact_mappings, manufacturer_id
            )
            if bay:
                return bay

        # Regex pattern matching using preloaded list
        for name in candidate_names:
            bay = BaseModuleTableView._lookup_regex_bay_mapping(
                name, phys_class, module_bays, regex_mappings, manufacturer_id
            )
            if bay:
                return bay

        # Fallback: exact match on candidate names against bay dict, with FPC-scope check
        for name in candidate_names:
            if name in module_bays:
                maps = module_bays.maps if hasattr(module_bays, "maps") else [module_bays]
                for scope_map in maps:
                    if name in scope_map:
                        bay = scope_map[name]
                        if BaseModuleTableView._fpc_slot_matches(name, bay):
                            return bay

        # Positional fallback for items inside converters
        return BaseModuleTableView._match_bay_by_position(item, index_map, module_bays)


class InstallSelectedView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """
    Install a user-selected set of inventory items by their entPhysicalIndex values.

    Reuses InstallBranchView._install_single for each selected item so every item
    goes through the same type/bay/serial resolution pipeline as a branch install.
    Only items where a matching bay *and* module type are found will be installed;
    items with no bay or no type are silently skipped (same behaviour as branch).
    """

    def post(self, request, pk):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

        self.required_object_permissions = {
            "POST": [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ]
        }
        if error := self.require_all_permissions("POST"):
            return error

        page_device = self.restrict_object_or_404(Device, pk=pk)
        module_bays = self.restricted_queryset(ModuleBay)
        changeable_components = _restricted_module_component_querysets(self)
        changeable_interfaces = changeable_components[Interface]
        deletable_interfaces = self.restricted_queryset(Interface, "delete")
        server_key = self.resolve_posted_server_key(request.POST)
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        selected_indices = request.POST.getlist("select")
        if not selected_indices:
            messages.warning(request, "No modules selected.")
            return _modules_redirect_response(request, sync_url, server_key)

        sync_device = _get_sync_device_for_inventory(page_device, server_key)
        cached_data = _get_cached_inventory_for_device(sync_device, server_key, self.get_cache_key)
        if not cached_data:
            messages.error(request, "No cached inventory data. Please refresh modules first.")
            return _modules_redirect_response(request, sync_url, server_key)

        try:
            # Use dict.fromkeys to preserve order while deduplicating
            selected_list = list(dict.fromkeys(int(i) for i in selected_indices))
        except ValueError:
            messages.error(request, "Invalid selection.")
            return _modules_redirect_response(request, sync_url, server_key)

        index_map = {idx: item for item in cached_data if (idx := item.get("entPhysicalIndex")) is not None}
        items = [index_map[idx] for idx in selected_list if idx in index_map]

        if not items:
            messages.warning(request, "None of the selected indices matched cached inventory.")
            return _modules_redirect_response(request, sync_url, server_key)

        # Load ignore rules once; they're evaluated per-row inside the install
        # loop using the *resolved* target device serial, since VC rows may
        # switch to a different member via device_selection_<ent_index>.
        from netbox_librenms_plugin.utils import get_enabled_ignore_rules
        from netbox_librenms_plugin.views.base.modules_view import _check_ignore_rules

        ignore_rules = get_enabled_ignore_rules()

        # Preload all ModuleBayMappings once to avoid N+1 per-item queries.
        # Manufacturer-scoping happens per-iteration since target_device may
        # differ across rows (VC members can have different device types).
        from netbox_librenms_plugin.utils import load_bay_mappings
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        module_types = get_module_types_indexed()
        allowed_module_type_ids = set(self.restricted_queryset(ModuleType).values_list("pk", flat=True))
        all_exact, all_regex = load_bay_mappings()

        # Preload module_bay normalization rules once so _match_bay considers the
        # same normalized candidate names as the table/UI matcher.
        from netbox_librenms_plugin.utils import preload_normalization_rules

        norm_rules_bay = preload_normalization_rules("module_bay")

        installed, skipped, failed = [], [], []

        invalid_selection_seen = False
        try:
            with transaction.atomic():
                for item in items:
                    ent_index = item.get("entPhysicalIndex")
                    selected_device_id = request.POST.get(f"device_selection_{ent_index}")
                    target_device, invalid_selected_device = _resolve_target_device_with_validation(
                        page_device,
                        selected_device_id,
                        self.restricted_queryset(Device),
                    )
                    if invalid_selected_device:
                        invalid_selection_seen = True
                    if ignore_rules:
                        target_serial = (getattr(target_device, "serial", None) or "").strip()
                        rule_action = _check_ignore_rules(
                            item,
                            index_map.get(item.get("entPhysicalContainedIn")),
                            ignore_rules,
                            index_map,
                            target_serial,
                        )
                        if rule_action in {"skip", "transparent"}:
                            skipped.append(f"{item.get('entPhysicalName', '?')}: matched ignore rule")
                            continue
                    mfr_id = getattr(getattr(target_device, "device_type", None), "manufacturer_id", None)
                    exact_mappings = BaseModuleTableView._filter_mappings_by_manufacturer(all_exact, mfr_id)
                    regex_mappings = BaseModuleTableView._filter_mappings_by_manufacturer(all_regex, mfr_id)
                    result = InstallBranchView._install_single(
                        target_device,
                        item,
                        index_map,
                        module_types,
                        exact_mappings=exact_mappings,
                        regex_mappings=regex_mappings,
                        manufacturer_id=mfr_id,
                        norm_rules_bay=norm_rules_bay,
                        module_bays=module_bays,
                        allowed_module_type_ids=allowed_module_type_ids,
                        changeable_components=changeable_components,
                        changeable_interfaces=changeable_interfaces,
                        deletable_interfaces=deletable_interfaces,
                    )
                    should_bind = _should_attempt_bind_for_result(result)
                    if result["status"] == "installed":
                        installed.append(result["name"])
                    elif result["status"] == "skipped":
                        skipped.append(f"{result['name']}: {result['reason']}")
                    else:
                        failed.append(f"{result['name']}: {result['reason']}")

                    if should_bind:
                        bind_result = _bind_interface_librenms_id(
                            target_device,
                            item,
                            result.get("module_pk"),
                            server_key,
                            changeable_interfaces,
                        )
                        if bind_result and bind_result["status"] != "bound":
                            skipped.append(f"{result['name']}: {bind_result['reason']}")
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Install failed: {e}")
            return _modules_redirect_response(request, sync_url, server_key)

        if invalid_selection_seen:
            _warn_invalid_selected_device(request)

        _report_install_results(request, installed, skipped, failed)
        return _modules_redirect_response(request, sync_url, server_key)


class UpdateModuleSerialView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Update the serial number of an already-installed module from LibreNMS inventory data."""

    def post(self, request, pk):
        from dcim.models import Device, Module

        self.required_object_permissions = {"POST": [("view", Device), ("change", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        page_device = self.restrict_object_or_404(Device, pk=pk)
        target_device, invalid_selected_device = _resolve_target_device_with_validation(
            page_device,
            request.POST.get("selected_device_id"),
            self.restricted_queryset(Device),
        )
        if invalid_selected_device:
            _warn_invalid_selected_device(request)
        serial = request.POST.get("serial", "").strip()
        if serial.lower() in _PLACEHOLDER_VALUES:
            serial = ""
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        try:
            module_id = int(request.POST.get("module_id"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module ID.")
            return _modules_redirect_response(request, sync_url)

        try:
            with transaction.atomic():
                module = (
                    self.restricted_queryset(Module, "change")
                    .select_for_update(of=("self",))
                    .select_related("module_type", "module_bay")
                    .filter(pk=module_id, device=target_device)
                    .first()
                )
                if not module:
                    messages.error(request, "Module no longer exists.")
                    return _modules_redirect_response(request, sync_url)
                module.serial = serial
                module.full_clean()
                module.save()
            messages.success(
                request,
                f"Updated serial for {module.module_type.model} in {module.module_bay.name} to '{serial}'.",
            )
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Failed to update serial: {e}")

        return _modules_redirect_response(request, sync_url)


class UpdateModuleInterfaceView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View
):
    """Associate a matching NetBox interface with an already-installed module."""

    def post(self, request, pk):
        from dcim.models import Device, Interface, Module

        self.required_object_permissions = {"POST": [("view", Device), ("view", Module), ("change", Interface)]}
        if error := self.require_all_permissions("POST"):
            return error

        # Resolve redirect context and validate the row identifier before any target or cache lookup.
        server_key = self.resolve_posted_server_key(request.POST)
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
        try:
            module_id = int(request.POST.get("module_id"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module ID.")
            return _modules_redirect_response(request, sync_url, server_key)

        page_device = self.restrict_object_or_404(Device, pk=pk)
        target_device, invalid_selected_device = _resolve_target_device_with_validation(
            page_device,
            request.POST.get("selected_device_id"),
            self.restricted_queryset(Device),
        )
        if invalid_selected_device:
            _warn_invalid_selected_device(request)
        changeable_interfaces = self.restricted_queryset(Interface, "change")
        # Validate the posted server_key against configured servers, else fall back to the active
        # client server (mirrors InstallModuleView via resolve_posted_server_key). A blank key (stale
        # tab / fallback render) OR a forged/unconfigured one degrades to the active server rather than
        # scoping under a bogus namespace. Without a usable key the port-bind is skipped AND the
        # adopt/merge runs without a server scope, so the raw twin's LibreNMS binding can't transfer.
        bind_item = _resolve_single_install_binding_item(request, target_device, server_key, self.get_cache_key)

        module = self.restrict_object_or_404(Module, "view", pk=module_id, device=target_device)

        bind_result = None
        # A primary interface is bindable only with both a cache-resolved item AND a server
        # context. Distinguish "there is no primary to bind" (bind_item falsy) from "a primary
        # exists but we never attempted the bind" (bind_item set, server_key blank): the latter
        # must not fall through to the adoption-only success path below, or we'd report success
        # while the row's primary port_id was never associated.
        primary_bindable = bool(bind_item)
        primary_bind_attempted = bool(bind_item and server_key)
        try:
            if primary_bind_attempted:
                bind_result = _bind_interface_librenms_id(
                    target_device,
                    bind_item,
                    module.pk,
                    server_key,
                    changeable_interfaces,
                )
        except Exception:
            logger.exception(
                "Unexpected error binding interface to module (device %s, module %s)",
                target_device.pk,
                module.pk,
            )
            bind_result = {
                "status": "failed",
                "reason": "unexpected error while associating interface to installed module",
            }
        else:
            # The port_id bind only associates the single LibreNMS-identified interface, but a
            # module can also own template interfaces (e.g. breakout children like c2/1) that
            # remain standalone and independently keep the row's "Update Interface" action on
            # (see _count_adoptable_template_interfaces). Adopt those too when the bind found
            # nothing to do (None) or succeeded (bound) — otherwise an already-bound interface
            # makes the bind a no-op, the adoption is skipped, and the button never clears.
            # A hard conflict/skip is left untouched so we don't mutate past an unresolved issue.
            if primary_bindable and not primary_bind_attempted:
                # Primary identity exists but there was no server context to bind it; don't
                # adopt-and-succeed as if the row were fully handled.
                bind_result = {
                    "status": "failed",
                    "reason": "no LibreNMS server context to associate the interface",
                }
            elif bind_result is None or bind_result.get("status") == "bound":
                try:
                    adopt_result = _adopt_existing_template_interfaces(
                        target_device,
                        module,
                        changeable_interfaces,
                    )
                except Exception:
                    logger.exception(
                        "Unexpected error adopting standalone template interfaces (device %s, module %s)",
                        target_device.pk,
                        module.pk,
                    )
                    # The adoption step is isolated so its failure can't clobber an
                    # already-committed primary bind: that interface is bound regardless, and
                    # reporting "failed" would make a retry look like a fresh conflict. Only the
                    # bind-less path (nothing committed yet) downgrades to a hard failure.
                    if bind_result is None:
                        bind_result = {
                            "status": "failed",
                            "reason": "unexpected error while associating interface to installed module",
                        }
                    else:
                        messages.warning(
                            request,
                            "Primary interface binding succeeded, but adopting standalone "
                            "template interfaces failed; see server logs for details.",
                        )
                else:
                    if bind_result is None:
                        bind_result = adopt_result
                    elif adopt_result.get("status") == "bound":
                        bind_result = {
                            "status": "bound",
                            "interface": bind_result.get("interface"),
                            # Keep the primary bind's port_id so the merged result still
                            # carries the bound interface's LibreNMS identity, not just the
                            # adoption tally.
                            "port_id": bind_result.get("port_id"),
                            "adopted_count": (bind_result.get("adopted_count") or 0)
                            + (adopt_result.get("adopted_count") or 0),
                        }

        if bind_result is None:
            messages.error(request, "No LibreNMS interface identity is available for this row.")
        elif bind_result.get("status") == "bound":
            location = f"{module.module_type.model} in {module.module_bay.name}"
            messages.success(request, _module_interface_update_message(bind_result, location))
        else:
            messages.warning(
                request,
                f"Could not update interface association: {bind_result.get('reason', 'unknown reason')}",
            )

        return _modules_redirect_response(request, sync_url, server_key)


class ModuleMismatchPreviewView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View
):
    """
    Return the modal body HTML fragment for the module replace/move dialog.

    Loads the installed module and the corresponding LibreNMS inventory item from
    cache, detects type/serial mismatch and serial conflicts, then renders the
    comparison template so the user can choose between Replace, Move, or
    Update Serial Only.
    """

    def get(self, request, pk):
        from dcim.models import Device, Module

        self.required_object_permissions = {"GET": [("view", Device), ("view", Module)]}
        if error := self.require_object_permissions("GET"):
            return error

        page_device = self.restrict_object_or_404(Device, pk=pk)
        target_device, invalid_selected_device = _resolve_target_device_with_validation(
            page_device,
            request.GET.get("selected_device_id"),
            self.restricted_queryset(Device),
        )
        if invalid_selected_device:
            _warn_invalid_selected_device(request)
        server_key = self.resolve_posted_server_key(request.GET)

        try:
            module_id = int(request.GET.get("module_id"))
            ent_index_int = int(request.GET.get("ent_index"))
        except (TypeError, ValueError):
            return HttpResponse("Missing or invalid module_id/ent_index.", status=400)

        installed_module = self.restrict_object_or_404(
            Module,
            select_related=("module_type", "module_bay", "device"),
            pk=module_id,
            device=target_device,
        )

        sync_device = _get_sync_device_for_inventory(target_device, server_key)
        cached_payload = cache.get(self.get_cache_key(sync_device, "inventory", server_key=server_key))
        cached_data = _extract_inventory_list(cached_payload)
        if not cached_data:
            return HttpResponse("No cached inventory data. Please refresh modules first.", status=400)

        librenms_item = next(
            (item for item in cached_data if item.get("entPhysicalIndex") == ent_index_int),
            None,
        )
        if not librenms_item:
            return HttpResponse("Inventory item not found in cache.", status=400)

        librenms_model = (librenms_item.get("entPhysicalModelName") or "").strip() or "-"
        librenms_serial = (librenms_item.get("entPhysicalSerialNum") or "").strip()
        if librenms_serial.lower() in _PLACEHOLDER_VALUES:
            librenms_serial = ""

        # Detect type mismatch
        from netbox_librenms_plugin.utils import resolve_module_type

        module_types = get_module_types_indexed()
        manufacturer = getattr(getattr(target_device, "device_type", None), "manufacturer", None)
        matched_type = resolve_module_type(
            librenms_model if librenms_model != "-" else "", module_types, manufacturer=manufacturer
        )

        type_mismatch = matched_type is not None and installed_module.module_type_id != matched_type.pk
        # type_matched: the LibreNMS model RESOLVED to the installed module's type (directly or via a
        # ModuleTypeMapping). Distinct from "not type_mismatch", which also covers an UNRECOGNISED model
        # (matched_type is None). The modal uses this to badge the LibreNMS model when its display string
        # differs from the NetBox type but is a confirmed match (the common serial-mismatch case).
        type_matched = matched_type is not None and installed_module.module_type_id == matched_type.pk
        installed_serial = (installed_module.serial or "").strip()
        if installed_serial.lower() in _PLACEHOLDER_VALUES:
            installed_serial = ""
        serial_mismatch = bool(
            not type_mismatch and librenms_serial != installed_serial and (librenms_serial or installed_serial)
        )

        # Check whether the LibreNMS serial already exists at a different location
        serial_conflict = None
        serial_conflict_ambiguous = False
        serial_conflict_hidden = False
        if librenms_serial:
            conflict_qs = Module.objects.filter(serial=librenms_serial).exclude(pk=installed_module.pk)
            conflict_count = conflict_qs.count()
            if conflict_count == 1:
                serial_conflict = (
                    self.restricted_queryset(Module)
                    .filter(pk__in=conflict_qs)
                    .select_related("module_type", "module_bay", "device")
                    .first()
                )
                serial_conflict_hidden = serial_conflict is None
            elif conflict_count > 1:
                serial_conflict_ambiguous = True

        return render(
            request,
            "netbox_librenms_plugin/htmx/module_mismatch_modal.html",
            {
                "device_pk": pk,
                "installed_module": installed_module,
                "bay_name": installed_module.module_bay.name,
                "target_bay_id": installed_module.module_bay_id,
                "installed_serial": installed_serial,
                "librenms_model": librenms_model,
                "librenms_serial": librenms_serial,
                "type_mismatch": type_mismatch,
                "type_matched": type_matched,
                "serial_mismatch": serial_mismatch,
                "serial_conflict": serial_conflict,
                "serial_conflict_ambiguous": serial_conflict_ambiguous,
                "serial_conflict_hidden": serial_conflict_hidden,
                "ent_index": ent_index_int,
                "server_key": server_key or "",
                "selected_device_id": target_device.pk,
            },
        )


class VCNormalizationReportView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Render a copyable markdown report describing a VC name-rewrite no-op for issue filing."""

    def get(self, request, pk):
        from dcim.models import Device, Module

        from netbox_librenms_plugin.utils import (
            build_vc_normalization_report,
            detect_vc_normalization_noop,
        )

        self.required_object_permissions = {"GET": [("view", Device), ("view", Module)]}
        if error := self.require_object_permissions("GET"):
            return error

        try:
            module_id = int(request.GET.get("module_id"))
        except (TypeError, ValueError):
            return HttpResponse("Missing or invalid module_id.", status=400)

        page_device = self.restrict_object_or_404(Device, pk=pk)
        target_device, invalid_selected_device = _resolve_target_device_with_validation(
            page_device,
            request.GET.get("selected_device_id"),
            self.restricted_queryset(Device),
        )
        if invalid_selected_device:
            _warn_invalid_selected_device(request)

        module = self.restrict_object_or_404(
            Module,
            select_related=("module_type", "module_type__manufacturer", "module_bay", "device"),
            pk=module_id,
            device=target_device,
        )

        diagnostic = detect_vc_normalization_noop(target_device, module)
        if diagnostic is None:
            return HttpResponse(
                "No VC name-rewrite no-op detected for this module — nothing to report.",
                status=400,
            )

        return render(
            request,
            "netbox_librenms_plugin/htmx/vc_normalization_report.html",
            {
                "report_markdown": build_vc_normalization_report(diagnostic),
            },
        )


class ReplaceModuleView(LibreNMSPermissionMixin, LibreNMSAPIMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """
    Replace the installed module in a bay with fresh data from LibreNMS inventory.

    Deletes the currently installed module (and optionally removes a conflicting
    module with the same serial from another location), then installs a new module
    from cached LibreNMS inventory data.
    """

    def post(self, request, pk):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType  # noqa: F401

        self.required_object_permissions = {
            "POST": [
                ("view", Device),
                ("view", ModuleType),
                ("add", Module),
                ("change", Module),
                ("delete", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ]
        }
        if error := self.require_all_permissions("POST"):
            return error

        page_device = self.restrict_object_or_404(Device, pk=pk)
        target_device, invalid_selected_device = _resolve_target_device_with_validation(
            page_device,
            request.POST.get("selected_device_id"),
            self.restricted_queryset(Device),
        )
        if invalid_selected_device:
            _warn_invalid_selected_device(request)
        changeable_components = _restricted_module_component_querysets(self)
        changeable_interfaces = changeable_components[Interface]
        deletable_interfaces = self.restricted_queryset(Interface, "delete")
        server_key = self.resolve_posted_server_key(request.POST)
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        try:
            module_id = int(request.POST.get("module_id"))
            ent_index_int = int(request.POST.get("ent_index"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module_id/ent_index.")
            return _modules_redirect_response(request, sync_url, server_key)

        installed_module = self.restrict_object_or_404(
            Module,
            "change",
            select_related=("module_type", "module_bay"),
            pk=module_id,
            device=target_device,
            pk__in=self.restricted_queryset(Module, "delete").values("pk"),
        )

        sync_device = _get_sync_device_for_inventory(target_device, server_key)
        cached_payload = cache.get(self.get_cache_key(sync_device, "inventory", server_key=server_key))
        cached_data = _extract_inventory_list(cached_payload)
        if not cached_data:
            messages.error(request, "No cached inventory data. Please refresh modules first.")
            return _modules_redirect_response(request, sync_url, server_key)

        librenms_item = next(
            (item for item in cached_data if item.get("entPhysicalIndex") == ent_index_int),
            None,
        )
        if not librenms_item:
            messages.error(request, "Inventory item not found in cache.")
            return _modules_redirect_response(request, sync_url, server_key)

        model_name = (librenms_item.get("entPhysicalModelName") or "").strip()
        serial = (librenms_item.get("entPhysicalSerialNum") or "").strip()
        if serial.lower() in _PLACEHOLDER_VALUES:
            serial = ""

        module_types = get_module_types_indexed()
        from netbox_librenms_plugin.utils import resolve_module_type

        manufacturer = getattr(getattr(target_device, "device_type", None), "manufacturer", None)
        matched_type = resolve_module_type(model_name, module_types, manufacturer=manufacturer)

        if not matched_type:
            messages.error(request, f"No matching module type found for '{model_name}'.")
            return _modules_redirect_response(request, sync_url, server_key)
        if not self.restricted_queryset(ModuleType).filter(pk=matched_type.pk).exists():
            messages.error(request, f"No matching module type found for '{model_name}'.")
            return _modules_redirect_response(request, sync_url, server_key)

        try:
            conflict_removed_msg = None
            bind_result = None
            adopted_components = 0
            vc_adjustments = {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 0}
            with transaction.atomic():
                # Re-fetch with row lock to prevent concurrent modifications
                installed_module = (
                    self.restricted_queryset(Module, "change")
                    .select_for_update(of=("self",))
                    .filter(
                        pk=module_id,
                        device=target_device,
                        pk__in=self.restricted_queryset(Module, "delete").values("pk"),
                    )
                    .select_related("module_type", "module_bay")
                    .first()
                )
                if not installed_module:
                    messages.error(request, "Module no longer exists.")
                    return _modules_redirect_response(request, sync_url, server_key)

                # Read bay/type from locked row to avoid stale snapshot
                target_bay = installed_module.module_bay
                old_type_name = installed_module.module_type.model
                old_bay_name = target_bay.name
                new_module = Module(
                    device=target_device,
                    module_bay=target_bay,
                    module_type=matched_type,
                    serial=serial,
                    status="active",
                )
                expected_component_ids = _authorize_adoptable_module_components(
                    new_module,
                    changeable_components,
                )
                # Re-derive any serial conflict from the database INSIDE the locked
                # transaction (and lock those rows too) — checking before the lock
                # opens a TOCTOU window where a concurrent request could change a
                # module's serial and we'd then delete a row that no longer
                # conflicts.  Re-querying under select_for_update() guarantees the
                # set we delete from is the same set we validated.
                conflict_module = None
                if serial:
                    conflict_qs = (
                        self.restricted_queryset(Module, "delete")
                        .select_for_update(of=("self",))
                        .filter(serial=serial)
                        .exclude(pk=installed_module.pk)
                        .select_related("module_type", "module_bay", "device")
                    )
                    locked_conflicts = list(conflict_qs)
                    conflict_count = Module.objects.filter(serial=serial).exclude(pk=installed_module.pk).count()
                    if conflict_count > 1:
                        # Roll back and surface a clear error — we don't want to
                        # guess which of N conflicts to remove.
                        raise _SerialConflictAmbiguous(serial)
                    if conflict_count != len(locked_conflicts):
                        raise _SerialConflictUnavailable(serial)
                    if conflict_count == 1:
                        conflict_module = locked_conflicts[0]

                # Remove the serial-conflicting module from its current location.
                if conflict_module:
                    c_model = conflict_module.module_type.model
                    c_bay = conflict_module.module_bay.name
                    c_device = conflict_module.device.name
                    conflict_module.delete()
                    conflict_removed_msg = f"Removed {c_model} from {c_device}/{c_bay}."

                # Delete the currently installed module in the target bay
                installed_module.delete()

                # Install fresh module from LibreNMS data
                adopted_components = _save_module_with_expected_component_adoption(
                    new_module,
                    expected_component_ids,
                )
                vc_adjustments = _normalize_module_interface_names_for_vc_member(
                    target_device,
                    new_module,
                    changeable_interfaces,
                    deletable_interfaces,
                )

            if server_key:
                try:
                    bind_result = _bind_interface_librenms_id(
                        target_device,
                        librenms_item,
                        new_module.pk,
                        server_key,
                        changeable_interfaces,
                    )
                except Exception:
                    bind_result = {
                        "status": "failed",
                        "reason": "unexpected error while binding interface to replaced module",
                    }

            if conflict_removed_msg:
                messages.info(request, conflict_removed_msg)
            messages.success(
                request,
                f"Replaced {old_type_name} with {matched_type.model} in {old_bay_name}"
                + (f" (serial: {serial})" if serial else "")
                + ".",
            )
            if adopted_components:
                messages.warning(
                    request,
                    "Module sync authority applied: adopted "
                    f"{adopted_components} existing standalone component(s) into the module.",
                )
            vc_summary = _format_vc_adjustment_summary(vc_adjustments)
            if vc_summary:
                messages.warning(
                    request,
                    f"VC member interface normalization applied: {vc_summary}.",
                )
            if bind_result and bind_result.get("status") == "bound":
                messages.info(
                    request,
                    f"Bound {bind_result['interface']} to LibreNMS port_id {bind_result['port_id']}.",
                )
            elif bind_result and bind_result.get("status") != "bound":
                messages.warning(
                    request,
                    "Replaced module, but interface binding was skipped: "
                    f"{bind_result.get('reason', 'unknown reason')}",
                )
        except _ModuleComponentAdoptionUnavailable as exc:
            messages.error(request, f"A matching {exc.component_label} is not available for module adoption.")
        except _SerialConflictAmbiguous as exc:
            messages.error(
                request,
                f"Serial '{exc.serial}' is assigned to multiple modules; cannot determine which to remove. "
                "Please resolve the conflict manually.",
            )
        except _SerialConflictUnavailable as exc:
            messages.error(
                request,
                f"Serial '{exc.serial}' is assigned to a module you cannot remove. "
                "Ask an administrator to resolve the conflict.",
            )
        except (ValidationError, IntegrityError) as e:
            error_msg = str(e)
            if "dcim_interface_unique_device_name" in error_msg:
                error_msg = (
                    "duplicate interface name — this module type's interface template "
                    "uses the '{module}' token which resolves to the same name for all siblings. "
                    "An interface naming plugin with a rewrite rule for this module type can fix this."
                )
            messages.error(request, f"Replace failed: {error_msg}")

        return _modules_redirect_response(request, sync_url, server_key)


class MoveModuleView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """
    Move an existing module from its current location to a target bay.

    Handles the case where a module (identified by serial) has been physically
    moved from one slot to another — possibly on a different device.  Updates
    the module_bay (and device when moving cross-device) rather than deleting
    and recreating, preserving the module's history.
    """

    def post(self, request, pk):
        from dcim.models import Device, Module, ModuleBay

        self.required_object_permissions = {
            "POST": [
                ("view", Device),
                ("view", ModuleBay),
                ("change", Module),
                ("delete", Module),
            ]
        }
        if error := self.require_all_permissions("POST"):
            return error

        page_device = self.restrict_object_or_404(Device, pk=pk)
        target_device, invalid_selected_device = _resolve_target_device_with_validation(
            page_device,
            request.POST.get("selected_device_id"),
            self.restricted_queryset(Device),
        )
        if invalid_selected_device:
            _warn_invalid_selected_device(request)
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        try:
            conflict_module_id = int(request.POST.get("conflict_module_id"))
            target_bay_id = int(request.POST.get("target_bay_id"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid conflict_module_id/target_bay_id.")
            return _modules_redirect_response(request, sync_url)

        # Optional: current occupant of target bay
        raw_module_id = request.POST.get("module_id")
        try:
            module_id = int(raw_module_id) if raw_module_id else None
        except (TypeError, ValueError):
            module_id = None

        self.restrict_object_or_404(ModuleBay, pk=target_bay_id, device=target_device)

        try:
            occupant_removed_msg = None
            with transaction.atomic():
                # Lock target bay to prevent concurrent modifications
                target_bay = (
                    self.restricted_queryset(ModuleBay)
                    .select_for_update(of=("self",))
                    .filter(pk=target_bay_id, device=target_device)
                    .first()
                )
                if not target_bay:
                    messages.error(request, "Module bay no longer exists.")
                    return _modules_redirect_response(request, sync_url)

                # Re-fetch with row lock to prevent concurrent modifications. Scoped like the
                # primary lookup: this module's device and bay are reassigned below, and its pk
                # comes straight from the POST, so an unscoped read would move a module the
                # user's grant does not cover.
                conflict_module = (
                    self.restricted_queryset(Module, "change")
                    .select_for_update(of=("self",))
                    .filter(pk=conflict_module_id)
                    .select_related("module_type", "module_bay", "device")
                    .first()
                )
                if not conflict_module:
                    messages.error(request, "Module no longer exists.")
                    return _modules_redirect_response(request, sync_url)

                # Remove whatever is currently in the target bay (if provided and different).
                # Scoped by "delete": the device and bay filters prove where the row sits, not that
                # the grant covers it, and the gate asked has_perm without an instance.
                if module_id:
                    occupant = (
                        self.restricted_queryset(Module, "delete")
                        .select_for_update(of=("self",))
                        .filter(pk=module_id, device=target_device, module_bay=target_bay)
                        .first()
                    )
                    if occupant and occupant.pk != conflict_module.pk:
                        occupant_removed_msg = f"Removed {occupant.module_type.model} from {target_bay.name}."
                        occupant.delete()

                # Move the conflict module to the target bay
                from_bay = conflict_module.module_bay.name
                from_device = conflict_module.device.name
                conflict_module.module_bay = target_bay
                conflict_module.device = target_device
                conflict_module.full_clean()
                conflict_module.save()

            if occupant_removed_msg:
                messages.info(request, occupant_removed_msg)
            moved_msg = f"Moved {conflict_module.module_type.model}"
            if from_device != target_device.name:
                moved_msg += f" from {from_device}"
            moved_msg += f"/{from_bay} to {target_bay.name}."
            messages.success(request, moved_msg)
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Move failed: {e}")

        return _modules_redirect_response(request, sync_url)


class AddBayTemplateView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """
    Create a missing ModuleBayTemplate on a Device Type or Module Type so the
    user can install a sub-component without leaving the modules sync tab.

    GET renders a pre-filled modal fragment that targets ``#htmx-modal-content``;
    POST creates the bay template and redirects back to the modules tab.
    """

    TARGET_KINDS = ("device_type", "module_type")

    def _resolve_target(self, target_kind, target_pk):
        """Load the DeviceType or ModuleType the new bay template will attach to."""
        from dcim.models import DeviceType, ModuleType

        if target_kind == "device_type":
            return self.restrict_object_or_404(DeviceType, pk=target_pk)
        if target_kind == "module_type":
            return self.restrict_object_or_404(ModuleType, pk=target_pk)
        return None

    @staticmethod
    def _device_manufacturer(device):
        """Return the device's Manufacturer (or None) for vendor-scoped mapping defaults."""
        device_type = getattr(device, "device_type", None)
        return getattr(device_type, "manufacturer", None) if device_type else None

    @staticmethod
    def _instantiate_template_on_existing(bay_template, target_kind, target):
        """
        Materialise the just-saved ``ModuleBayTemplate`` onto every existing
        device/module of ``target`` so the resolver can match it immediately.

        NetBox auto-creates bays from templates only when a Device/Module is
        first created — a template added later is invisible to existing
        instances until manually instantiated. ``target_kind`` selects the
        scope: device-type templates are instantiated on every Device of that
        type; module-type templates are instantiated on every installed Module
        of that type. Pre-existing bays with the resolved name (under the same
        device/module scope) are skipped so re-adding a template after a
        partial manual fix is safe.
        """
        from dcim.models import Device, Module, ModuleBay

        instantiated = 0
        if target_kind == "device_type":
            for device in Device.objects.filter(device_type=target):
                bay = bay_template.instantiate(device=device)
                if ModuleBay.objects.filter(device=device, module__isnull=True, name=bay.name).exists():
                    continue
                bay.full_clean()
                bay.save()
                instantiated += 1
        elif target_kind == "module_type":
            for module in Module.objects.filter(module_type=target).select_related("device"):
                bay = bay_template.instantiate(device=module.device, module=module)
                if ModuleBay.objects.filter(device=module.device, module=module, name=bay.name).exists():
                    continue
                bay.full_clean()
                bay.save()
                instantiated += 1
        return instantiated

    @staticmethod
    def _derive_mapping_pattern(librenms_name, netbox_name):
        """
        Derive a regex ``ModuleBayMapping`` rule that maps ``librenms_name``
        to ``netbox_name`` and naturally covers every sibling bay sharing
        the same LibreNMS-side literal skeleton.

        Both names are tokenised into alternating literal and digit-run
        segments. Each *distinct* digit value on the LibreNMS side becomes
        a numbered capture group; subsequent occurrences of the same value
        emit back-references, so ``"0/FT0"`` produces ``r"^(\\d+)/FT\\1$"``
        — matching only when both fan-tray digits agree. The NetBox
        replacement preserves the operator-chosen literals verbatim and
        back-references the LibreNMS group whose value matches each digit
        run on the NetBox side.

        Returns ``None`` when:
          * either name is empty,
          * the LibreNMS name has no digit run at all,
          * the NetBox name contains a digit value that does not appear in
            the LibreNMS name (we'd be inventing a value we can't extract),
          * the resulting pattern fails to compile or does not round-trip
            (``compile.fullmatch`` + ``compile.sub`` must reproduce the
            exact NetBox name).

        Examples::

            ('Sfm 1', 'SFM 1')        -> r'^Sfm (\\d+)$'      → r'SFM \\1'
            ('0/FT0', 'Fan Tray 0')   -> r'^(\\d+)/FT\\1$'    → r'Fan Tray \\1'
            ('TenGigE0/0/0/0', same)  -> r'^TenGigE(\\d+)/\\1/\\1/\\1$'
                                       → r'TenGigE\\1/\\1/\\1/\\1'
            ('0/FT0', 'Fan Tray 1')   -> None  (libre has no '1')
            ('Slot A', 'Slot A')      -> None  (no digit run)
        """
        if not librenms_name or not netbox_name:
            return None
        token_re = re.compile(r"(\d+|\D+)")
        libre_tokens = token_re.findall(librenms_name)
        nb_tokens = token_re.findall(netbox_name)

        digit_groups = {}
        pattern_parts = ["^"]
        for tok in libre_tokens:
            if tok.isdigit():
                if tok in digit_groups:
                    pattern_parts.append(rf"\{digit_groups[tok]}")
                else:
                    idx = len(digit_groups) + 1
                    digit_groups[tok] = idx
                    pattern_parts.append(r"(\d+)")
            else:
                pattern_parts.append(re.escape(tok))
        pattern_parts.append("$")
        if not digit_groups:
            return None

        replacement_parts = []
        for tok in nb_tokens:
            if tok.isdigit():
                if tok not in digit_groups:
                    return None
                replacement_parts.append(rf"\{digit_groups[tok]}")
            else:
                replacement_parts.append(tok.replace("\\", r"\\"))

        librenms_pattern = "".join(pattern_parts)
        netbox_replacement = "".join(replacement_parts)
        try:
            compiled = re.compile(librenms_pattern)
        except re.error:
            return None
        if not compiled.fullmatch(librenms_name):
            return None
        try:
            if compiled.sub(netbox_replacement, librenms_name) != netbox_name:
                return None
        except re.error:
            return None
        return {
            "kind": "regex",
            "librenms_pattern": librenms_pattern,
            "netbox_replacement": netbox_replacement,
            "digit_count": len(digit_groups),
        }

    @staticmethod
    def _existing_regex_mapping_covers(librenms_name, librenms_class, manufacturer):
        """
        True when an existing regex ModuleBayMapping already matches
        ``librenms_name`` for the given manufacturer / global scope.
        Iterates regex rows in Python — the row count is small (one per
        bay-family) so this is cheap, and Postgres can't compare its
        re-flavoured patterns server-side anyway.
        """
        from netbox_librenms_plugin.models import ModuleBayMapping

        if not librenms_name:
            return False
        qs = ModuleBayMapping.objects.filter(
            librenms_class=librenms_class or "",
            is_regex=True,
        )
        if manufacturer is not None:
            qs = qs.filter(models.Q(manufacturer=manufacturer) | models.Q(manufacturer__isnull=True))
        else:
            qs = qs.filter(manufacturer__isnull=True)
        for mapping in qs.only("librenms_name"):
            try:
                if re.compile(mapping.librenms_name).fullmatch(librenms_name):
                    return True
            except re.error:
                continue
        return False

    @staticmethod
    def _existing_bay_mapping(librenms_name, librenms_class, manufacturer):
        """
        True when a ModuleBayMapping already covers (librenms_name, librenms_class)
        for the given manufacturer (vendor-scoped) or globally (manufacturer is null).

        Only checks exact mappings — regex mappings are intentionally ignored
        because the per-row suggestion is for one specific name and we don't
        want to second-guess broader patterns the operator already wrote.
        """
        from netbox_librenms_plugin.models import ModuleBayMapping

        if not librenms_name:
            return False
        qs = ModuleBayMapping.objects.filter(
            librenms_name=librenms_name,
            librenms_class=librenms_class or "",
            is_regex=False,
        )
        if manufacturer is not None:
            qs = qs.filter(models.Q(manufacturer=manufacturer) | models.Q(manufacturer__isnull=True))
        else:
            qs = qs.filter(manufacturer__isnull=True)
        return qs.exists()

    def get(self, request, pk):
        from dcim.models import Device, DeviceType, ModuleBay, ModuleBayTemplate, ModuleType

        target_kind = request.GET.get("target_kind", "")
        if target_kind not in self.TARGET_KINDS:
            return HttpResponse("Invalid target_kind.", status=400)
        target_model = DeviceType if target_kind == "device_type" else ModuleType

        # Read-only modal render — only require plugin view permission and
        # NetBox add-permission on ModuleBayTemplate so users without it never
        # see a form they cannot submit. POST also instantiates live ModuleBay
        # rows via _instantiate_template_on_existing(), so require add_modulebay
        # here too to keep the GET/POST permission contract aligned.
        self.required_object_permissions = {
            "GET": [
                ("view", Device),
                ("view", target_model),
                ("add", ModuleBayTemplate),
                ("add", ModuleBay),
            ]
        }
        if error := self.require_all_permissions("GET"):
            return error

        device = self.restrict_object_or_404(Device, pk=pk)

        try:
            target_pk = int(request.GET.get("target_pk", ""))
        except (TypeError, ValueError):
            return HttpResponse("Missing or invalid target_pk.", status=400)
        target = self._resolve_target(target_kind, target_pk)

        librenms_name = request.GET.get("librenms_name", "")
        librenms_class = request.GET.get("librenms_class", "")
        suggested_name = request.GET.get("suggested_name", "")
        manufacturer = self._device_manufacturer(device)
        mapping_exists = self._existing_bay_mapping(
            librenms_name, librenms_class, manufacturer
        ) or self._existing_regex_mapping_covers(librenms_name, librenms_class, manufacturer)
        # Offer the auto-mapping option only when we have a LibreNMS name to
        # map *from* and there's no existing mapping covering it.  Permission
        # to add the mapping itself is also required — users without it would
        # only see the checkbox return a permission error on POST.

        can_add_mapping = request.user.has_perm("netbox_librenms_plugin.add_modulebaymapping")
        offer_mapping_checkbox = bool(librenms_name) and not mapping_exists and can_add_mapping
        # Pattern preview against the *current* suggested NetBox name. The
        # template re-runs this on every keystroke client-side; the
        # server-side derivation is for the initial render + POST-time check.
        mapping_pattern = (
            self._derive_mapping_pattern(librenms_name, suggested_name) if offer_mapping_checkbox else None
        )

        context = {
            "device_pk": pk,
            "target_kind": target_kind,
            "target_pk": target_pk,
            "target_label": str(target),
            "suggested_name": suggested_name,
            "suggested_position": request.GET.get("suggested_position", ""),
            "suggested_label": request.GET.get("suggested_label", ""),
            "librenms_name": librenms_name,
            "librenms_class": librenms_class,
            "manufacturer_label": str(manufacturer) if manufacturer else "",
            "offer_mapping_checkbox": offer_mapping_checkbox,
            "mapping_exists": mapping_exists,
            "mapping_pattern": mapping_pattern,
            "mapping_default_kind": "regex" if mapping_pattern else "exact",
        }
        return render(request, "netbox_librenms_plugin/htmx/add_bay_template_modal.html", context)

    def post(self, request, pk):
        from dcim.models import Device, DeviceType, ModuleBay, ModuleBayTemplate, ModuleType

        from netbox_librenms_plugin.models import ModuleBayMapping

        target_kind = request.POST.get("target_kind", "")
        if target_kind not in self.TARGET_KINDS:
            messages.error(request, "Invalid target_kind for bay template.")
            sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
            return _modules_redirect_response(request, sync_url)
        target_model = DeviceType if target_kind == "device_type" else ModuleType

        # POST creates the template AND instantiates live ModuleBay rows on
        # existing devices/modules via _instantiate_template_on_existing(), so
        # gate on add_modulebay in addition to add_modulebaytemplate.
        self.required_object_permissions = {
            "POST": [
                ("view", Device),
                ("view", target_model),
                ("add", ModuleBayTemplate),
                ("add", ModuleBay),
            ]
        }
        if error := self.require_all_permissions("POST"):
            return error

        device = self.restrict_object_or_404(Device, pk=pk)
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        try:
            target_pk = int(request.POST.get("target_pk", ""))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid target_pk for bay template.")
            return _modules_redirect_response(request, sync_url)

        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Bay template name is required.")
            return _modules_redirect_response(request, sync_url)
        position = (request.POST.get("position") or "").strip()
        label = (request.POST.get("label") or "").strip()
        description = (request.POST.get("description") or "").strip()

        # Optional auto-mapping inputs (echoed from the GET-rendered modal).
        librenms_name = (request.POST.get("librenms_name") or "").strip()
        librenms_class = (request.POST.get("librenms_class") or "").strip()
        also_create_mapping = request.POST.get("also_create_mapping") == "1"
        mapping_kind = request.POST.get("mapping_kind", "exact")
        if mapping_kind not in ("exact", "regex"):
            mapping_kind = "exact"

        target = self._resolve_target(target_kind, target_pk)
        kwargs = {
            "name": name,
            "position": position,
            "label": label,
            "description": description,
        }
        if target_kind == "device_type":
            kwargs["device_type"] = target
        else:
            kwargs["module_type"] = target

        # Only add a mapping when the user actually picked a different
        # NetBox bay name — if the names match, no mapping is needed.
        will_add_mapping = (
            also_create_mapping
            and librenms_name
            and librenms_name != name
            and request.user.has_perm("netbox_librenms_plugin.add_modulebaymapping")
        )
        manufacturer = self._device_manufacturer(device) if will_add_mapping else None
        # Resolve regex pattern + replacement when the user opted in.
        mapping_libre_value = librenms_name
        mapping_netbox_value = name
        mapping_is_regex = False
        if will_add_mapping and mapping_kind == "regex":
            pattern = self._derive_mapping_pattern(librenms_name, name)
            if pattern is None:
                # Server-side rule didn't fire — fall back to exact rather than
                # writing an unverified regex from client input.
                mapping_kind = "exact"
            else:
                mapping_libre_value = pattern["librenms_pattern"]
                mapping_netbox_value = pattern["netbox_replacement"]
                mapping_is_regex = True
        if will_add_mapping:
            if mapping_is_regex:
                race = self._existing_regex_mapping_covers(librenms_name, librenms_class, manufacturer)
            else:
                race = self._existing_bay_mapping(librenms_name, librenms_class, manufacturer)
            if race:
                # Race: a mapping was added between modal render and submit.
                will_add_mapping = False

        try:
            mapping_created = False
            instantiated_count = 0
            with transaction.atomic():
                bay_template = ModuleBayTemplate(**kwargs)
                bay_template.full_clean()
                bay_template.save()
                instantiated_count = self._instantiate_template_on_existing(bay_template, target_kind, target)
                if will_add_mapping:
                    mapping = ModuleBayMapping(
                        librenms_name=mapping_libre_value,
                        librenms_class=librenms_class,
                        netbox_bay_name=mapping_netbox_value,
                        is_regex=mapping_is_regex,
                        manufacturer=manufacturer,
                    )
                    mapping.full_clean()
                    mapping.save()
                    mapping_created = True
            instantiate_note = ""
            if instantiated_count:
                noun = "device" if target_kind == "device_type" else "module"
                plural = "" if instantiated_count == 1 else "s"
                instantiate_note = f" Bay added to {instantiated_count} existing {noun}{plural}."
            if mapping_created:
                vendor_note = f" (scoped to {manufacturer})" if manufacturer else " (global)"
                kind_note = "regex " if mapping_is_regex else ""
                messages.success(
                    request,
                    f"Added bay template '{name}' to {target} and {kind_note}ModuleBayMapping "
                    f"'{mapping_libre_value}' → '{mapping_netbox_value}'{vendor_note}.{instantiate_note}",
                )
            else:
                messages.success(request, f"Added bay template '{name}' to {target}.{instantiate_note}")
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Failed to add bay template: {e}")

        return _modules_redirect_response(request, sync_url)
