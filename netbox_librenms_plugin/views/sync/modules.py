"""Sync action views for module/inventory installation from LibreNMS."""

import copy
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.http import HttpResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from utilities.exceptions import AbortRequest

from netbox_librenms_plugin.constants import OOB_INVENTORY_SOURCE, is_librenms_placeholder
from netbox_librenms_plugin.interface_diff import type_change_refusal
from netbox_librenms_plugin.inventory_policy import load_inventory_policy
from netbox_librenms_plugin.module_grants import (
    ACTION_INSTALL_BRANCH,
    ACTION_INSTALL_MODULE,
    ACTION_INSTALL_SELECTED,
    ACTION_INSTALL_SELECTED_ROW,
    ACTION_MISMATCH_PREVIEW,
    ACTION_REPLACE_MODULE,
    ACTION_UPDATE_INTERFACE,
    ACTION_UPDATE_SERIAL,
    RENDER_PINNED,
    GrantRefusal,
    GrantRefused,
    read_module_grant,
    sign_module_grant,
)
from netbox_librenms_plugin.server_mappings import (
    AmbiguousLibreNMSIdError,
    assign_own,
    claim_librenms_port_binding,
    find_port_owner,
    persist_mapping,
    read_mapping,
)
from netbox_librenms_plugin.server_selection import get_librenms_sync_device
from netbox_librenms_plugin.sync_cache import (
    SyncTab,
    apply_request_cache_transition,
    cached_module_inventory,
    module_inventory_oob_matches,
    schedule_request_cache_mutation,
)
from netbox_librenms_plugin.transactions import classify_conflict, update_existing_row
from netbox_librenms_plugin.utils import (
    REGEX_COMPILE_ERRORS,
    acquire_advisory_transaction_lock,
    get_module_adoptable_interface_names,
    get_module_template_interface_specs,
    get_module_types_indexed,
    get_vc_member_positions,
    load_module_type_bay_types,
    is_vc_position,
    module_type_bay_type_ids,
    netbox_relocates_module_subtree,
    normalize_inventory_serial,
    normalize_serial,
    prefetch_module_bay_types,
    rewrite_interface_name_for_vc_member,
    exception_text_for,
)
from netbox_librenms_plugin.utils import coerce_positive_int as _coerce_positive_int
from netbox_librenms_plugin.views.base.modules_view import (
    BaseModuleTableView,
    _get_item_port_identity,
    _inventory_item_key,
    _is_holder,
    _normalize_librenms_text,
    select_module_interface,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    SyncSubjectClaimMixin,
)
from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

logger = logging.getLogger(__name__)

# Every module action below scopes its cache reads and its LibreNMS id writes by server key.
MODULE_MOVE_REQUIRES_NETBOX_MESSAGE = "Moving a module needs NetBox 4.7 or later. Earlier releases move only the module itself and leave its interfaces, its module bays and any modules installed in them on the original device."
NO_LIBRENMS_SERVER_MESSAGE = (
    "No LibreNMS server is configured. Add a server to the plugin configuration before syncing modules."
)

# OOB-controller rows are merged into the cached snapshot for display only, and every entry point
# that can act on an inventory row rejects them. The marker itself is shared with the readers in
# constants.py; only the wording of the refusal belongs to this module.
OOB_INVENTORY_READ_ONLY_REASON = "OOB controller inventory is read-only"

# A selected install runs every selected row or none of them.
SELECTED_ROWS_REFUSED_MESSAGE = (
    "Nothing was installed: the Modules table does not offer {rows} for install on the chosen member. "
    "Refresh Modules and try again."
)


def _modules_redirect_response(request, sync_url, server_key=None):
    """
    Return a Django redirect back to the modules tab for a classic (non-HTMX) form post.

    These module sync actions are server-scoped, so the follow-up page must stay on
    the server whose cache namespace this request just mutated/read. The active
    ``server_key`` is propagated as a ``?server_key=`` query param; when not passed
    explicitly it is read from the request (POST then GET) so every redirect site
    keeps the server context without each caller having to thread it through.

    Args:
        request: The current HTTP request (server_key source).
        sync_url (str): The base sync URL to redirect to.
        server_key: The active LibreNMS server key; read from the request when None.

    Returns:
        HttpResponse: A Django redirect to the modules tab.

    """
    if server_key is None:
        server_key = request.POST.get("server_key") or request.GET.get("server_key") or ""
    target = f"{sync_url}?tab=modules"
    if server_key:
        target += f"&server_key={quote_plus(str(server_key))}"
    target += "#librenms-module-table"
    return apply_request_cache_transition(request, redirect(target))


def _modules_action_response(request, page_device, server_key=None):
    """
    Answer a module action with the re-rendered module tab, or a redirect for a classic post.

    An HTMX action re-renders the module tab from its cache and returns that fragment, so the
    page is not reloaded: the queued messages travel with it in the out-of-band toast container.
    The table keeps the page and sort of the browser URL (``HX-Current-URL``), because the
    action URL itself carries no query string.
    ``HX-Retarget`` and ``HX-Reswap`` name ``#module-sync-content`` and ``innerHTML`` on the
    server side, so every action lands in the tab whatever the posting form declares, and the
    ``closeModal`` trigger closes the mismatch modal, which nothing else closes because the swap
    happens outside it.

    Args:
        request: The current HTTP request.
        page_device: The device whose sync page the action was posted from.
        server_key: The active LibreNMS server key; read from the request when None.

    Returns:
        HttpResponse: The module tab fragment for an HTMX post, or a Django redirect.

    """
    sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": page_device.pk})
    if not request.htmx:
        return _modules_redirect_response(request, sync_url, server_key)
    # The action URL has no query, so the table's page and sort come from the page the post was sent from.
    render_request = copy.copy(request)
    render_request.GET = QueryDict(urlsplit(request.headers.get("HX-Current-URL", "")).query)
    tab_view = DeviceModuleTableView()
    tab_view.setup(render_request, pk=page_device.pk)
    tab_view.cache_only = True
    context = tab_view.get_context_data(render_request, page_device, server_key=server_key)
    response = tab_view.render_sync_partial(
        render_request, page_device, context["server_key"], {"module_sync": context}
    )
    response["HX-Retarget"] = "#module-sync-content"
    response["HX-Reswap"] = "innerHTML"
    response["HX-Trigger"] = json.dumps({"closeModal": None})
    return apply_request_cache_transition(request, response)


def _schedule_module_cache_mutation(request, page_device, server_key):
    """Publish one module-tab transition after a committed module action."""
    schedule_request_cache_mutation(request, page_device, SyncTab.MODULES, server_key)


def _module_bind_result_changed(bind_result):
    """Return whether an interface bind or template adoption changed NetBox."""
    return bool(
        bind_result
        and bind_result.get("status") == "bound"
        and (bind_result.get("changed") or bind_result.get("adopted_count"))
    )


def _lock_page_device_serials(page_device):
    """
    Serialise serial checks for one sync page, before any row lock is taken.

    Must be the FIRST lock in the transaction. A bulk install holds one transaction across its
    whole loop, so acquiring this after a bay lock lets a bulk run holding the advisory lock wait
    on a bay a single install holds, while that install waits on the advisory lock.

    One key per page device, so a transaction never acquires a second key of this kind and the
    incremental-acquisition ordering problem cannot arise.
    """
    acquire_advisory_transaction_lock(f"netbox-librenms-plugin:module-serial:{page_device.pk}")


def _module_holder_map(device):
    """Read module ancestry from all modules on a device, independent of bay permissions."""
    from dcim.models import Module

    return dict(Module.objects.filter(device=device).values_list("pk", "module_bay__module_id"))


def _module_already_on_device(device, serial, *, exclude_pk=None):
    """
    Return the Module already holding ``serial`` on ``device``, or None.

    Identity is device-scoped on purpose. A serial is evidence about one physical part, and
    vendors reuse a serial across unrelated parts, so a match on another device must not block a
    genuine first install here.

    Existence is read WITHOUT permission filtering. Installing needs ``add_module``, not
    ``change_module``, so a restricted queryset can come back empty for an operator who may still
    create the duplicate. Callers must report the hit without naming objects the operator cannot
    see; ``_lock_page_device_serials`` must already be held.
    """
    from dcim.models import Module

    if not serial:
        return None
    conflicts = Module.objects.select_for_update(of=("self",)).filter(device=device, serial=serial)
    if exclude_pk:
        conflicts = conflicts.exclude(pk=exclude_pk)
    return conflicts.first()


def _get_cached_inventory_for_device(sync_device, server_key, get_cache_key):
    """
    Return cached inventory for ``sync_device``, or ``None`` when it is absent or stale.

    Cache entries are namespaced by server key and include ``librenms_id``.
    When both current and cached IDs are valid positive integers, they must
    match; otherwise cached data is treated as stale. The OOB controller link must
    match too, the same check the module table makes. A malformed payload or one
    from an older schema version is deleted and read as absent.

    Args:
        sync_device (object): The NetBox device whose inventory is requested.
        server_key (str): The LibreNMS server key for the cache namespace.
        get_cache_key (Callable): The function that builds the inventory cache key.

    Returns:
        list[dict] | None: The cached inventory, or ``None`` when it is absent or stale.

    """
    cache_key = get_cache_key(sync_device, "inventory", server_key=server_key)
    cached_payload = cache.get(cache_key)
    inventory = cached_module_inventory(cached_payload)
    if inventory is None:
        cache.delete(cache_key)
        return None

    current_librenms_id = _coerce_positive_int(read_mapping(sync_device).own_id(server_key))
    cached_librenms_id = _coerce_positive_int(cached_payload.get("librenms_id"))
    if current_librenms_id is None or cached_librenms_id is None or current_librenms_id != cached_librenms_id:
        return None
    if not module_inventory_oob_matches(cached_payload, sync_device, server_key):
        return None

    return inventory


def _module_write_failure(exc, model, user):
    """
    Return the text of a failed module write that a page may show *user*.

    Args:
        exc (AbortRequest | ValidationError | IntegrityError): The error of the write.
        model (type[Model]): The model that NetBox validated.
        user (User | None): The viewer.

    Returns:
        str: The duplicate interface name hint, else the text from ``exception_text_for``.

    """
    # It looks for a constraint name in the text; the page gets fixed text or exception_text_for.
    # nosemgrep: caught-error-text  # noqa: ERA001
    if isinstance(exc, IntegrityError) and "dcim_interface_unique_device_name" in str(exc):
        return (
            "duplicate interface name — this module type's interface template "
            "uses the '{module}' token which resolves to the same name for all siblings. "
            "An interface naming plugin with a rewrite rule for this module type can fix this."
        )
    return exception_text_for(exc, model, user)


def _report_install_results(request, installed, skipped, failed):
    """Emit Django messages summarising an install run."""
    if installed:
        messages.success(request, f"Installed {len(installed)} module(s): {', '.join(installed)}")
    if skipped:
        messages.info(request, f"Skipped {len(skipped)}: {'; '.join(skipped)}")
    if failed:
        messages.warning(request, f"Failed {len(failed)}: {'; '.join(failed)}")


def _resolve_destination(page_device, selected_device_id, devices):
    """
    Return the posted write target: the page device, or a member of its Virtual Chassis in *devices*.

    A module writer refuses an invalid target instead of falling back to the page device.

    Args:
        page_device (Device): The device whose Modules page posted the action.
        selected_device_id (str | None): The posted target; blank means the page device.
        devices (QuerySet): The devices the user may view.

    Returns:
        Device | None: The target, or None when the posted value names no valid target.

    """
    if selected_device_id in (None, ""):
        return page_device
    device_id = _coerce_positive_int(selected_device_id)
    if device_id is None:
        return None
    if device_id == page_device.pk:
        return page_device
    if not page_device.virtual_chassis_id:
        return None
    return (
        devices.filter(pk=device_id, virtual_chassis_id=page_device.virtual_chassis_id)
        .select_related("device_type__manufacturer")
        .first()
    )


@dataclass(frozen=True)
class _GrantedInventory:
    """An authenticated grant with the snapshot and the policy it was checked against."""

    grant: object
    inventory: list
    index_map: dict
    policy: object

    def row(self, index):
        """Return the cached main-inventory row of a granted index."""
        item = self.index_map.get(index)
        if item is None:
            raise GrantRefused(GrantRefusal.STALE)
        if refusal := _never_installable_reason(item):
            raise _ModuleActionRefused(f"Nothing was changed: {refusal}.")
        return item


def _authorize_module_grant(view, page_device, server_key, action, token):
    """
    Return the posted grant of *action* with the current snapshot and policy, or raise GrantRefused.

    A writer calls it inside its write transaction, after the page lock where it takes one, and
    before any write. A change committed after this read is not detected.

    Args:
        view (View): The writer, for its cache key.
        page_device (Device): The device whose Modules page posted the action.
        server_key (str): The posted LibreNMS server.
        action (str): The action the grant must name.
        token (str | None): The posted grant.

    Returns:
        _GrantedInventory: The grant, the cached snapshot, its index map and the policy.

    """
    from dcim.models import Device

    from netbox_librenms_plugin.models import InventoryRuleFilterError

    sync_device = _get_sync_device_for_inventory(page_device, server_key)
    inventory = _get_cached_inventory_for_device(sync_device, server_key, view.get_cache_key)
    if inventory is None:
        raise GrantRefused(GrantRefusal.CACHE_MISSING)
    try:
        policy = load_inventory_policy(page_device.pk)
    except Device.DoesNotExist as exc:
        raise GrantRefused(GrantRefusal.DESTINATION) from exc
    except InventoryRuleFilterError as exc:
        logger.error("Module action %s on %s refused: %s", action, page_device, exc)
        raise GrantRefused(GrantRefusal.RULE_ERROR) from exc
    grant = read_module_grant(token, action=action, page_device_id=page_device.pk, server_key=server_key)
    grant.check_state(inventory, policy)
    return _GrantedInventory(grant, inventory, _inventory_index_map(inventory), policy)


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


def _warn_invalid_selected_device(request):
    """Warn the user that selected device input was invalid and fallback was applied."""
    messages.warning(
        request,
        "Invalid selected device context detected; falling back to the page device for this operation.",
    )


class _SerialConflictAmbiguous(Exception):
    """
    Signal that more than one module holds the incoming serial during a replacement transaction.

    ReplaceModuleView uses this exception to abort the atomic block and show a user-friendly error after the rollback.
    """

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
    template_names = get_module_adoptable_interface_names(device, module).names
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
            interface.snapshot()
            interface.module = module
            interface.save(update_fields=["module", "last_updated"])
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

    Args:
        device (Device): The selected virtual chassis member that owns the installed module.
        module (Module): The newly installed module whose interfaces are normalized.
        changeable_interfaces (QuerySet): The permission-scoped change queryset.
        deletable_interfaces (QuerySet): The permission-scoped delete queryset.

    Returns:
        dict[str, int]: The counts of renamed, adopted, removed, and skipped interfaces.

    """
    result = {
        "renamed": 0,
        "adopted": 0,
        "removed": 0,
        "skipped": 0,
    }

    vc_position = getattr(device, "vc_position", None)
    vc_id = getattr(device, "virtual_chassis_id", None)
    if not is_vc_position(vc_position) or not isinstance(vc_id, int):
        return result
    member_positions = get_vc_member_positions(device)

    from dcim.models import Interface

    module_interfaces = list(Interface.objects.filter(device=device, module=module).order_by("pk"))
    changeable_interface_ids = set(changeable_interfaces.filter(device=device).values_list("pk", flat=True))
    deletable_interface_ids = set(deletable_interfaces.filter(device=device).values_list("pk", flat=True))

    for interface in module_interfaces:
        if interface.pk not in changeable_interface_ids:
            result["skipped"] += 1
            continue
        desired_name = rewrite_interface_name_for_vc_member(
            interface.name,
            vc_position,
            member_positions=member_positions,
        )
        if not desired_name or desired_name == interface.name:
            continue

        conflict = Interface.objects.filter(device=device, name=desired_name).exclude(pk=interface.pk).first()
        if conflict is not None:
            if getattr(conflict, "module_id", None) is None:
                if conflict.pk not in changeable_interface_ids:
                    result["skipped"] += 1
                    continue
                if interface.pk not in deletable_interface_ids:
                    result["skipped"] += 1
                    continue
                conflict.snapshot()
                conflict.module = module
                conflict.save(update_fields=["module", "last_updated"])
                result["adopted"] += 1
                try:
                    with transaction.atomic():
                        interface.delete()
                    result["removed"] += 1
                except Exception as exc:
                    if classify_conflict(exc):
                        raise
                    result["skipped"] += 1
            else:
                result["skipped"] += 1
            continue

        interface.snapshot()
        interface.name = desired_name
        try:
            with transaction.atomic():
                interface.full_clean()
                interface.save(update_fields=["name", "_name", "last_updated"])
            result["renamed"] += 1
        except Exception as exc:
            if classify_conflict(exc):
                raise
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


def _select_bind_candidate(device, item, module_pk, server_key, interfaces, port_id, interface_names):
    """
    Choose the interface that a port bind writes, from the current state, and return ``(candidate, refusal)``.

    The port's owner is the evidence first, then the item's names, then the module's interfaces.
    The binder runs it before and after it locks the candidate, and both runs must choose one row.
    """
    from dcim.models import Interface

    try:
        existing_owner = find_port_owner(port_id, server=server_key)
    except AmbiguousLibreNMSIdError:
        return None, {
            "status": "conflict",
            "reason": (
                f"port_id {port_id} is ambiguous — it matches more than one interface; "
                "not reassigning. Resolve the duplicate librenms_id first."
            ),
        }
    if existing_owner is not None and not isinstance(existing_owner, Interface):
        return None, {
            "status": "conflict",
            "reason": f"port_id {port_id} already assigned to another NetBox interface owner; not reassigning",
        }
    if existing_owner is not None and not interfaces.filter(pk=existing_owner.pk).exists():
        return None, {
            "status": "skipped",
            "reason": f"matching interface is not available for port_id {port_id}",
        }
    if existing_owner is not None and existing_owner.device_id != device.pk:
        return None, {
            "status": "conflict",
            "reason": (
                f"port_id {port_id} already assigned to {existing_owner.device.name}/{existing_owner.name}; "
                "not reassigning"
            ),
        }

    candidate = existing_owner
    if candidate is None:
        # Choose from every interface, as the table does, then refuse a choice outside the caller's scope.
        device_interfaces = Interface.objects.filter(device=device)
        choice = select_module_interface(
            device,
            item,
            server_key=server_key,
            interfaces_by_name={
                interface.name: interface for interface in device_interfaces.filter(name__in=interface_names)
            },
            module_interfaces=list(device_interfaces.filter(module_id=module_pk)) if module_pk else [],
        )
        if choice.interface is not None and not interfaces.filter(pk=choice.interface.pk).exists():
            return None, {
                "status": "skipped",
                "reason": f"matching interface is not available for port_id {port_id}",
            }
        if choice.status:
            return None, {"status": choice.status, "reason": choice.reason}
        candidate = choice.interface

    if candidate is None:
        return None, {
            "status": "skipped",
            "reason": f"no matching interface found for port_id {port_id}",
        }
    return candidate, None


def _module_may_own_interface(device, module_pk, name):
    """Return whether the bind may move the interface named *name* into the module."""
    from dcim.models import Module

    module = Module.objects.select_related("module_type").get(pk=module_pk)
    return get_module_adoptable_interface_names(device, module).may_move(name)


# Its own savepoint: callers catch a non-conflict error and keep their transaction.
@transaction.atomic
def _bind_interface_librenms_id(device, item, module_pk, server_key, interfaces):
    """
    Bind LibreNMS ``port_id`` to the best matching NetBox interface.

    Applies only for inventory items carrying stable port identity metadata.
    The binding is non-destructive: if the port ID already belongs to a different
    interface, no reassignment is performed and a conflict is reported.

    Args:
        device (Device): The NetBox device that owns the interface.
        item (dict): The LibreNMS inventory item with port identity metadata.
        module_pk (int | None): The module primary key for the interface.
        server_key (str): The LibreNMS server key for the port identity.
        interfaces (QuerySet): The interfaces available to the caller.

    Returns:
        dict | None: The binding outcome, or ``None`` when the item has no port ID.

    Raises:
        LibreNMSPortBindingBusy: Another open transaction holds the claim on the port.

    """
    port_id, interface_names = _get_item_port_identity(item)
    if not port_id:
        return None

    claim_librenms_port_binding(port_id, server_key)
    candidate, refusal = _select_bind_candidate(
        device, item, module_pk, server_key, interfaces, port_id, interface_names
    )
    if refusal is not None:
        return refusal

    # The checks and the change below read the locked row: a concurrent bind may have written it since the read above.
    locked = interfaces.select_for_update(of=("self",)).filter(pk=candidate.pk, device=device).first()
    if locked is None:
        return {
            "status": "skipped",
            "reason": f"matching interface is not available for port_id {port_id}",
        }
    # The same evidence must still choose the locked row: a rename can move a name to another interface.
    current, refusal = _select_bind_candidate(device, item, module_pk, server_key, interfaces, port_id, interface_names)
    if refusal is not None:
        return refusal
    if current is None or current.pk != locked.pk:
        return {
            "status": "skipped",
            "reason": f"the matching interface for port_id {port_id} changed; refresh and retry",
        }
    candidate = locked

    set_module = False
    if module_pk:
        candidate_module_id = getattr(candidate, "module_id", None)
        if candidate_module_id and candidate_module_id != module_pk:
            return {
                "status": "conflict",
                "reason": (f"{candidate.name} already attached to module {candidate_module_id}; not reassigning"),
            }
        set_module = not candidate_module_id and _module_may_own_interface(device, module_pk, candidate.name)

    current_port_id = _coerce_positive_int(read_mapping(candidate).own_id(server_key))
    if current_port_id and current_port_id != port_id:
        return {
            "status": "conflict",
            "reason": f"{candidate.name} already mapped to port_id {current_port_id}; not overwriting",
        }

    bind_port = current_port_id != port_id
    if not (set_module or bind_port):
        return {"status": "bound", "interface": candidate.name, "port_id": port_id, "changed": False}
    candidate.snapshot()
    update_fields = []
    if set_module:
        candidate.module_id = module_pk
        update_fields.append("module")

    def save(row, mapping_fields=frozenset()):
        row.save(update_fields=[*update_fields, *mapping_fields, "last_updated"])

    if bind_port:
        persist_mapping(candidate, assign_own(candidate, server_key, port_id), write=save)
    else:
        save(candidate)
    return {"status": "bound", "interface": candidate.name, "port_id": port_id, "changed": True}


def _inventory_index_map(inventory):
    """Return the inventory rows keyed by physical index, as every install writer reads them."""
    return {idx: item for item in inventory if (idx := item.get("entPhysicalIndex")) is not None}


def _should_attempt_bind_for_result(result):
    """Return True when a module install result carries a bindable module context."""
    if result.get("status") == "installed":
        return bool(result.get("module_pk"))
    if result.get("status") == "skipped" and result.get("module_pk"):
        return result.get("reason") == "bay already occupied"
    return False


def _record_install_result(result, installed, skipped, failed):
    """Add one install result to the list of its status in the install summary."""
    if result["status"] == "installed":
        installed.append(result["name"])
    elif result["status"] == "skipped":
        skipped.append(f"{result['name']}: {result['reason']}")
    else:
        failed.append(f"{result['name']}: {result['reason']}")


def _record_bind_outcome(bind_result, result, skipped):
    """Report one bind attempt in the install summary, returning whether it changed NetBox."""
    if not bind_result:
        return False
    if bind_result["status"] != "bound":
        skipped.append(f"{result['name']}: {bind_result['reason']}")
        return False
    return _module_bind_result_changed(bind_result)


def _never_installable_reason(item):
    """
    Return why an inventory row can never install onto the host, or None.

    OOB-controller rows are in the snapshot for display only, and the table never renders a
    holder. A crafted POST can still name either row by its entPhysicalIndex.

    Args:
        item (dict): The cached inventory row.

    Returns:
        str | None: The skip reason, or None when the row may install.

    """
    if item.get("_source") == OOB_INVENTORY_SOURCE:
        return OOB_INVENTORY_READ_ONLY_REASON
    if _is_holder(item):
        return "not hardware"
    return None


class _ModuleActionRefused(Exception):
    """Leave a module write transaction before any write, with the text the page shows."""

    def __init__(self, message, level=messages.ERROR):
        self.message = message
        self.level = level
        super().__init__(message)

    def report(self, request):
        """Queue the refusal text on *request*."""
        messages.add_message(request, self.level, self.message)


class InstallModuleView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """Install a NetBox Module into a ModuleBay from LibreNMS inventory data."""

    def post(self, request, pk):  # noqa: C901
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
        changeable_components = _restricted_module_component_querysets(self)
        changeable_interfaces = changeable_components[Interface]
        deletable_interfaces = self.restricted_queryset(Interface, "delete")
        server_key = self.resolve_posted_server_key_or_none(request.POST)
        if server_key is None:
            messages.error(request, NO_LIBRENMS_SERVER_MESSAGE)
            return _modules_action_response(request, page_device)
        target_device = _resolve_destination(
            page_device, request.POST.get("selected_device_id"), self.restricted_queryset(Device)
        )
        if target_device is None:
            messages.error(request, GrantRefusal.DESTINATION.message)
            return _modules_action_response(request, page_device, server_key)

        try:
            module_bay_id = int(request.POST.get("module_bay_id"))
            module_type_id = int(request.POST.get("module_type_id"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module bay/module type ID.")
            return _modules_action_response(request, page_device, server_key)

        self.restrict_object_or_404(
            ModuleBay, pk=module_bay_id, device=target_device
        )  # verify bay belongs to selected device
        module_type = self.restrict_object_or_404(ModuleType, pk=module_type_id)
        # Carrier installs have no inventory row of their own, so they omit ent_index and need no grant.
        indexed = bool(request.POST.get("ent_index"))

        try:
            with transaction.atomic():
                _lock_page_device_serials(page_device)
                bind_item = None
                serial = ""
                if indexed:
                    granted = _authorize_module_grant(
                        self, page_device, server_key, ACTION_INSTALL_MODULE, request.POST.get("inventory_binding")
                    )
                    granted.grant.require(
                        device_id=target_device.pk,
                        ent_index=request.POST.get("ent_index"),
                        action_target={"module_bay_id": module_bay_id, "module_type_id": module_type_id},
                    )
                    bind_item = granted.row(granted.grant.ent_index)
                    serial = normalize_inventory_serial(
                        bind_item.get("entPhysicalSerialNum"),
                        manufacturer=target_device.device_type.manufacturer,
                        preloaded_rules=granted.policy.serial_rule_sets,
                    )
                    if is_librenms_placeholder(serial):
                        serial = ""
                # Re-fetch bay under lock to prevent TOCTOU race with concurrent installs.
                locked_bay = (
                    self.restricted_queryset(ModuleBay)
                    .select_for_update(of=("self",))
                    .filter(pk=module_bay_id, device=target_device)
                    .first()
                )
                if not locked_bay:
                    raise _ModuleActionRefused("Module bay no longer exists.")
                if hasattr(locked_bay, "installed_module") and locked_bay.installed_module:
                    raise _ModuleActionRefused(
                        f"Module bay '{locked_bay.name}' already has a module installed.", messages.WARNING
                    )
                if _module_already_on_device(target_device, serial):
                    # The bay is deliberately not named: the conflicting module may be outside
                    # this operator's scope, and its existence is what blocks the install.
                    raise _ModuleActionRefused(
                        f"Serial '{serial}' is already installed on this device. Nothing was installed."
                    )
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
                # In the install's transaction: a lock conflict in the bind rolls back the install too.
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
                    except Exception as exc:
                        if classify_conflict(exc):
                            raise
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
            _schedule_module_cache_mutation(request, page_device, server_key)
        except GrantRefused as exc:
            messages.error(request, exc.refusal.message)
        except _ModuleActionRefused as exc:
            exc.report(request)
        except _ModuleComponentAdoptionUnavailable as exc:
            messages.error(request, f"A matching {exc.component_label} is not available for module adoption.")
        except (AbortRequest, ValidationError, IntegrityError) as e:
            if classify_conflict(e):
                raise
            # It looks for a constraint name in the text; the page gets fixed text or exception_text_for.
            # nosemgrep: caught-error-text  # noqa: ERA001
            detail = _module_write_failure(e, Module, request.user)
            messages.error(request, f"Failed to install module: {detail}")

        return _modules_action_response(request, page_device, server_key)


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
        parent_index = request.POST.get("parent_index")
        server_key = self.resolve_posted_server_key_or_none(request.POST)
        if server_key is None:
            messages.error(request, NO_LIBRENMS_SERVER_MESSAGE)
            return _modules_action_response(request, page_device)

        if not parent_index:
            messages.error(request, "Missing parent inventory index.")
            return _modules_action_response(request, page_device, server_key)

        try:
            parent_index = int(parent_index)
        except ValueError:
            messages.error(request, "Invalid parent inventory index.")
            return _modules_action_response(request, page_device, server_key)

        self._install_branch(request, page_device, server_key, parent_index)
        page_device.refresh_from_db()
        return _modules_action_response(request, page_device, server_key)

    @transaction.atomic
    def _install_branch(self, request, page_device, server_key, parent_index):
        """Plan and write under the page lock, then commit before response rendering."""
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

        module_bays = self.restricted_queryset(ModuleBay)
        changeable_components = _restricted_module_component_querysets(self)
        changeable_interfaces = changeable_components[Interface]
        deletable_interfaces = self.restricted_queryset(Interface, "delete")
        _lock_page_device_serials(page_device)
        # The advisory lock does not refresh model instances loaded before its wait.
        page_device = self.restrict_object_or_404(Device, pk=page_device.pk)
        try:
            granted = _authorize_module_grant(
                self, page_device, server_key, ACTION_INSTALL_BRANCH, request.POST.get("inventory_binding")
            )
            grant = granted.grant.require(
                device_id=request.POST.get("selected_device_id", ""),
                ent_index=parent_index,
                action_target={"parent_index": parent_index},
            )
            # The table signed the branch it rendered; the writer runs exactly that list, in that order.
            entries = [(granted.row(index), target_id) for index, target_id in grant.rows]
        except GrantRefused as exc:
            messages.error(request, exc.refusal.message)
            return
        except _ModuleActionRefused as exc:
            exc.report(request)
            return
        if not entries:
            messages.warning(request, "No installable items found in this branch.")
            return
        targets = {
            device.pk: device
            for device in self.restricted_queryset(Device)
            .filter(pk__in={target_id for _item, target_id in entries})
            .select_related("device_type__manufacturer")
        }
        if len(targets) != len({target_id for _item, target_id in entries}):
            messages.error(request, "An inventory destination is unavailable. Refresh Modules and try again.")
            return

        # The parent-module walk keeps a row's whole attributed subtree, hidden holders too.
        member_index_maps = self._ancestry_maps(granted, set(targets))

        # Load module types (with mappings)
        module_types = get_module_types_indexed()
        allowed_module_type_ids = set(self.restricted_queryset(ModuleType).values_list("pk", flat=True))

        # Preload all ModuleBayMappings once to avoid N+1 per-item queries.
        # Filter by device manufacturer so vendor-scoped mappings only apply to
        # matching vendors and mismatched ones are skipped.
        from netbox_librenms_plugin.utils import load_bay_mappings

        exact_mappings, regex_mappings = load_bay_mappings()
        manufacturer_contexts = self._build_manufacturer_contexts(targets.values(), exact_mappings, regex_mappings)

        # Preload module_bay normalization rules once so _match_bay_for_item considers the
        # same normalized candidate names as the table/UI matcher.
        from netbox_librenms_plugin.utils import preload_normalization_rules

        norm_rules_bay = preload_normalization_rules("module_bay")
        type_bay_types = load_module_type_bay_types()

        # Install top-down: each install may create new child bays
        installed = []
        skipped = []
        failed = []
        bound_any = False

        try:
            with transaction.atomic():
                holder_of_by_device = {}
                for item, target_id in entries:
                    target_device = targets[target_id]
                    if target_device.pk not in holder_of_by_device:
                        holder_of_by_device[target_device.pk] = _module_holder_map(target_device)
                    holder_of = holder_of_by_device[target_device.pk]
                    mfr_id = target_device.device_type.manufacturer_id
                    policy = manufacturer_contexts[mfr_id]
                    result = self._install_single(
                        target_device,
                        item,
                        module_types,
                        naming_map=granted.index_map,
                        ancestry_map=member_index_maps[target_device.pk],
                        exact_mappings=policy["exact_mappings"],
                        regex_mappings=policy["regex_mappings"],
                        manufacturer_id=mfr_id,
                        norm_rules_bay=norm_rules_bay,
                        norm_rules_serial=granted.policy.serial_rule_sets,
                        type_bay_types=type_bay_types,
                        module_bays=module_bays,
                        allowed_module_type_ids=allowed_module_type_ids,
                        changeable_components=changeable_components,
                        changeable_interfaces=changeable_interfaces,
                        deletable_interfaces=deletable_interfaces,
                        holder_of=holder_of,
                        user=request.user,
                    )
                    should_bind = _should_attempt_bind_for_result(result)
                    _record_install_result(result, installed, skipped, failed)

                    if should_bind:
                        bind_result = _bind_interface_librenms_id(
                            target_device,
                            item,
                            result.get("module_pk"),
                            server_key,
                            changeable_interfaces,
                        )
                        bound_any = _record_bind_outcome(bind_result, result, skipped) or bound_any
        except (AbortRequest, ValidationError, IntegrityError) as e:
            if classify_conflict(e):
                raise
            # It looks for a constraint name in the text; the page gets fixed text or exception_text_for.
            # nosemgrep: caught-error-text  # noqa: ERA001
            detail = _module_write_failure(e, Module, request.user)
            messages.error(request, f"Branch install failed: {detail}")
            return

        _report_install_results(request, installed, skipped, failed)
        if installed or bound_any:
            _schedule_module_cache_mutation(request, page_device, server_key)

    @staticmethod
    def _ancestry_maps(granted, target_ids):
        """
        Return, per target, the inventory rows its parent-module walk may use: the rows the signed render attributes to it.

        A pinned render attributes every row to its one member; the page render attributes rows as the table did.
        """
        policy = granted.policy
        if granted.grant.render_mode == RENDER_PINNED:
            return InstallBranchView._attributed_rows(granted, policy.member(granted.grant.device_id), [], target_ids)
        return InstallBranchView._attributed_rows(granted, policy.page_device, policy.vc_members, target_ids)

    @staticmethod
    def _attributed_rows(granted, owner, vc_members, target_ids):
        """Return, per target, the inventory rows that the render of *owner* over *vc_members* attributes to it."""
        _default, contexts = BaseModuleTableView._build_inventory_ignore_contexts(
            owner, granted.inventory, vc_members, granted.policy
        )
        return {
            device_id: {
                idx: item
                for idx, item in granted.index_map.items()
                if contexts[_inventory_item_key(item)]["selected_device"].pk == device_id
            }
            for device_id in target_ids
        }

    @staticmethod
    def _build_manufacturer_contexts(target_devices, exact_mappings, regex_mappings):
        """Share one bay-mapping policy for each destination manufacturer."""
        manufacturer_contexts = {}
        for device in target_devices:
            manufacturer = device.device_type.manufacturer
            if manufacturer.pk not in manufacturer_contexts:
                manufacturer_contexts[manufacturer.pk] = {
                    "exact_mappings": BaseModuleTableView._filter_mappings_by_manufacturer(
                        exact_mappings, manufacturer.pk
                    ),
                    "regex_mappings": BaseModuleTableView._filter_mappings_by_manufacturer(
                        regex_mappings, manufacturer.pk
                    ),
                    "manufacturer": manufacturer,
                }
        return manufacturer_contexts

    @staticmethod
    def _install_single(
        device,
        item,
        module_types,
        *,
        naming_map,
        ancestry_map,
        module_bays,
        allowed_module_type_ids,
        changeable_components,
        changeable_interfaces,
        deletable_interfaces,
        exact_mappings=None,
        regex_mappings=None,
        manufacturer_id=None,
        norm_rules_bay=None,
        norm_rules_serial=None,
        holder_of=None,
        type_bay_types=None,
        user=None,
    ):
        """
        Try to install a single inventory item.

        Re-fetches module bays each time since parent installs create new ones.
        Scopes bay lookup to the correct parent module to handle duplicate bay names.

        Args:
            device (Device): The NetBox device that receives the module.
            item (dict): The LibreNMS inventory item to install.
            module_types (dict): The available module types indexed for matching.
            naming_map (dict): The rendered inventory items keyed by index, for the bay names.
            ancestry_map (dict): The items keyed by index that the parent-module walk may use.
            module_bays (QuerySet): The module bays available to the caller.
            allowed_module_type_ids (set[int]): The module type IDs available to the caller.
            changeable_components (dict): The component querysets available for adoption.
            changeable_interfaces (QuerySet): The interfaces the caller can change.
            deletable_interfaces (QuerySet): The interfaces the caller can delete.
            exact_mappings (list | None): The optional exact module bay mappings.
            regex_mappings (list | None): The optional regular expression module bay mappings.
            manufacturer_id (int | None): The optional device manufacturer ID.
            norm_rules_bay (dict | None): The optional module bay normalization rules.
            norm_rules_serial (dict | None): The optional serial normalization rules.
            holder_of (dict | None): Mutable module ancestry reused during a bulk install.
            type_bay_types (dict | None): Preloaded module bay types per module type, for a bulk install.
            user (User | None): The viewer of a failure reason; only a superuser gets NetBox's message.

        Returns:
            dict: The install status and its result details.

        """
        from dcim.models import Module

        from netbox_librenms_plugin.utils import module_type_lookup_candidates, resolve_module_type

        model_name = (item.get("entPhysicalModelName") or "").strip()
        # The serial-scope rules strip vendor markers such as Juniper's "S/N ", and the
        # coercion handles the all-digit serials LibreNMS sends as JSON numbers.
        serial = normalize_inventory_serial(
            item.get("entPhysicalSerialNum"),
            manufacturer=device.device_type.manufacturer,
            preloaded_rules=norm_rules_serial,
        )
        if is_librenms_placeholder(serial):
            serial = ""
        name = item.get("entPhysicalName", "") or model_name

        # Reject at this shared chokepoint so both the branch and selected install paths are covered.
        if refusal := _never_installable_reason(item):
            return {"status": "skipped", "name": name, "reason": refusal}

        # Match module type (direct, then normalization fallback)
        manufacturer = getattr(getattr(device, "device_type", None), "manufacturer", None)
        matched_type = resolve_module_type(
            model_name,
            module_types,
            manufacturer=manufacturer,
            fallback_names=module_type_lookup_candidates(item),
        )
        if not matched_type:
            return {"status": "skipped", "name": name, "reason": "no matching type"}
        if matched_type.pk not in allowed_module_type_ids:
            return {"status": "skipped", "name": name, "reason": "no matching type"}

        # Re-fetch module bays (parent install creates new child bays)
        bays = prefetch_module_bay_types(
            module_bays.filter(device=device).select_related("installed_module__module_type")
        )

        # Use preloaded mappings if provided, otherwise load from DB
        if exact_mappings is None or regex_mappings is None:
            from netbox_librenms_plugin.utils import load_bay_mappings

            exact_mappings, regex_mappings = load_bay_mappings()

        matched_bay = InstallBranchView._resolve_bay_for_item(
            device,
            item,
            bays,
            exact_mappings,
            regex_mappings,
            manufacturer_id,
            norm_rules_bay,
            naming_map=naming_map,
            ancestry_map=ancestry_map,
            holder_of=holder_of,
            accepted_bay_types=module_type_bay_type_ids(matched_type, type_bay_types),
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
                if _module_already_on_device(device, serial):
                    return {
                        "status": "skipped",
                        "name": name,
                        "reason": "serial already installed on this device",
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
        except (AbortRequest, ValidationError, IntegrityError) as e:
            if classify_conflict(e):
                raise
            # It looks for a constraint name in the text; the page gets fixed text or exception_text_for.
            # nosemgrep: caught-error-text  # noqa: ERA001
            reason = _module_write_failure(e, Module, user)
            return {"status": "failed", "name": name, "reason": reason}

        if holder_of is not None:
            holder_of[module.pk] = locked_bay.module_id

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
    def _resolve_bay_for_item(
        device,
        item,
        bays,
        exact_mappings,
        regex_mappings,
        manufacturer_id,
        norm_rules_bay,
        *,
        naming_map,
        ancestry_map,
        holder_of=None,
        accepted_bay_types=frozenset(),
    ):
        """
        Return the module bay to install *item* into, or None when nothing matches.

        An installed parent module narrows the candidate names so duplicate bay names resolve to
        the right module. It must not shrink the search to nothing: the parent only resolves once
        it is installed, so an item whose NetBox bay sits outside that module (a device-level bay)
        installed on a first pass and then skipped as "no matching bay" on the next, which is what
        a second branch install, or installing the rows one at a time first, produces.

        The fallback widens to the device-level bays and to the resolved parent's own subtree.
        A bay owned by a module OUTSIDE that subtree is never offered: it may be unambiguous, but
        unambiguous is not the same as correctly owned, so installing into it is a wrong-bay write.

        Args:
            device (Device): The device being installed onto.
            item (dict): The LibreNMS inventory item to place.
            bays: The device's module bays, with ``installed_module`` selected.
            exact_mappings (list): The exact module bay mappings.
            regex_mappings (list): The regular expression module bay mappings.
            manufacturer_id (int | None): The device manufacturer ID.
            norm_rules_bay (dict | None): The module bay normalization rules.
            naming_map (dict): The rendered inventory items keyed by index, for the bay names.
            ancestry_map (dict): The items keyed by index that the parent-module walk may use.
            holder_of (dict | None): Preloaded module ancestry for a bulk install.
            accepted_bay_types (frozenset): The module bay types the module type fits.

        Returns:
            ModuleBay | None: The bay to install into.

        """

        def match_against(candidate_bays):
            return BaseModuleTableView._match_bay_for_item(
                item,
                naming_map,
                candidate_bays,
                exact_mappings=exact_mappings,
                regex_mappings=regex_mappings,
                manufacturer_id=manufacturer_id,
                norm_rules_bay=norm_rules_bay,
                foreign_indices=foreign_indices,
                accepted_bay_types=accepted_bay_types,
            )

        foreign_indices = naming_map.keys() - ancestry_map.keys()

        # The bay names come from the whole render, as in the table; the module walk stops at a member boundary.
        parent_module_id = InstallBranchView._find_parent_module_id(
            item, ancestry_map, bays, exact_mappings, regex_mappings, manufacturer_id
        )
        matched = match_against(InstallBranchView._candidate_bays_for_item(bays, parent_module_id))
        if not matched and parent_module_id:
            matched = match_against(
                InstallBranchView._fallback_bays_for_resolved_parent(
                    device, bays, parent_module_id, holder_of=holder_of
                )
            )
        return matched

    @staticmethod
    def _fallback_bays_for_resolved_parent(device, bays, parent_module_id, *, holder_of=None):
        """
        Return the bays a row may fall back to once its resolved parent narrowed the search away.

        Two different questions decide membership, and _bay_is_owned_within_subtree and
        _bay_holds_resolved_parent name them: where a NEW module may go, and whether this row IS
        the module already there. They feed ONE candidate set on purpose. _match_bay_for_item resolves a
        candidate name through exact mappings, then regex, then direct names, then position, each
        as a pass over the whole set, and the positional pass rejects a position held by two bays.
        Answering the two questions as separate searches would put every method of one ahead of
        every method of the other, and would let a positional tie that this set rejects resolve
        inside the smaller one. So they are merged here and searched together.

        The resolved parent bounds the result. Three kinds of bay qualify, and nothing else.

        Bays outside every module, for the row whose NetBox bay is device-level.

        Bays owned by the parent or by a module below it. LibreNMS can omit an intermediate
        module, so a row can trace to an ancestor while its bay belongs to a descendant: a Nokia
        connector reports as ``2/x1/1/c2`` under the XIOM when the MDA is missing, and its bay
        ``1/c2`` belongs to that MDA. See _nest_synthetic_transceivers.

        The bay the parent itself occupies, which is owned by the parent's own holder and so sits
        outside the subtree. The row can BE that module, because LibreNMS reports a container and
        the module inside it as two rows. The resulting "bay already occupied" carries the module
        pk, and that is the only way a SKIPPED row reaches _should_attempt_bind_for_result and
        binds its LibreNMS port to an interface. An installed row carries its own module pk.

        A bay owned by a module outside the subtree, a sibling above all, stays out.

        A device-level bay wins a name collision, and a name owned by two modules in the subtree
        is dropped rather than guessed. Both match _candidate_bays_for_item.

        Args:
            device (Device): The device being installed onto, used to read the module ancestry.
            bays: The candidate module bays, with ``installed_module`` selected.
            parent_module_id (int): The installed parent module the narrowed pass used.
            holder_of (dict | None): Preloaded ancestry, updated as modules are installed.

        Returns:
            dict: A ``name -> bay`` mapping to match the inventory item against.

        """
        # Ancestry comes from every module on the device, never from *bays*: that queryset is
        # permission-restricted, so a bay the user cannot change would cut the chain and hide a
        # descendant bay they can. Only the candidates below stay restricted.
        if holder_of is None:
            holder_of = _module_holder_map(device)

        fallback = {bay.name: bay for bay in bays if not bay.module_id}
        scoped: dict = {}
        for bay in bays:
            if bay.name in fallback:
                continue
            if InstallBranchView._bay_holds_resolved_parent(
                bay, parent_module_id
            ) or InstallBranchView._bay_is_owned_within_subtree(bay, parent_module_id, holder_of):
                scoped.setdefault(bay.name, []).append(bay)

        for name, candidates in scoped.items():
            if len(candidates) == 1:
                fallback[name] = candidates[0]
            else:
                logger.info(
                    "Bulk install: dropping ambiguous bay name %r — %d modules under the resolved "
                    "parent define it; the row skips as 'no matching bay' instead of installing "
                    "into an arbitrary one.",
                    name,
                    len(candidates),
                )
        return fallback

    @staticmethod
    def _bay_is_owned_within_subtree(bay, parent_module_id, holder_of):
        """
        Report whether *bay* belongs to the resolved parent or to a module below it.

        This is the INSTALL question: may a new module go here. Ownership is strict, because a
        bay owned by a module outside the subtree, a sibling above all, is not this row's to
        take. It may be unambiguous, but unambiguous is not the same as correctly owned.

        The subtree reaches all the way down, not one level. LibreNMS can omit an intermediate
        module, so a row traces to an ancestor while its bay belongs to a descendant. A Nokia
        connector reports as ``2/x1/1/c2`` and nests under the XIOM when the MDA is missing, and
        the bay ``1/c2`` belongs to that MDA. See _nest_synthetic_transceivers.

        Args:
            bay (ModuleBay): The bay to judge.
            parent_module_id (int): The installed parent module the narrowed pass used.
            holder_of (dict): Module pk -> pk of the module whose bay holds it, device-wide.

        Returns:
            bool: True when the bay sits in the parent's subtree.

        """
        module_id = bay.module_id
        seen = set()
        while module_id is not None and module_id not in seen:
            if module_id == parent_module_id:
                return True
            seen.add(module_id)
            module_id = holder_of.get(module_id)
        return False

    @staticmethod
    def _bay_holds_resolved_parent(bay, parent_module_id):
        """
        Report whether *bay* is the one the resolved parent already occupies.

        This is the IDENTITY question, and it is a different one: not where a new module goes,
        but whether this row IS the module that is already there. LibreNMS reports a container
        and the module inside it as two rows, so a row can resolve its own module as its parent.
        Matching this bay makes _install_single report "bay already occupied" carrying the module
        pk, which is the only way a SKIPPED row reaches _should_attempt_bind_for_result and binds
        its LibreNMS port to an interface. An installed row carries its own module pk instead.

        The bay belongs to the parent's own holder, so the subtree test cannot reach it, and it
        can be nested: a converter inside a line card occupies a module-scoped bay.

        Args:
            bay (ModuleBay): The bay to judge.
            parent_module_id (int): The installed parent module the narrowed pass used.

        Returns:
            bool: True when the parent module is installed in this bay.

        """
        installed = getattr(bay, "installed_module", None)
        return installed is not None and installed.pk == parent_module_id

    @staticmethod
    def _find_parent_module_id(item, index_map, device_bays, exact_mappings, regex_mappings, manufacturer_id):
        """
        Find the NetBox module ID for the installed parent of this inventory item.

        Walks up the LibreNMS hierarchy to the first ancestor whose name, directly or through a
        bay mapping, names an occupied module bay on the device. Mappings resolve through the
        same lookups as the bay matcher.

        Args:
            item (dict): The inventory item.
            index_map (dict): The inventory items keyed by index.
            device_bays: The device's module bays, with ``installed_module`` selected.
            exact_mappings (list): The exact ModuleBayMapping entries.
            regex_mappings (list): The regex ModuleBayMapping entries.
            manufacturer_id (int | None): The device manufacturer ID.

        Returns:
            int | None: The installed parent module ID, or None if no ancestor resolves.

        """
        bays_by_name = InstallBranchView._parent_bays_by_name(device_bays)
        current = item
        visited = set()
        while True:
            parent_idx = current.get("entPhysicalContainedIn", 0)
            if not parent_idx or parent_idx not in index_map or parent_idx in visited:
                return None
            visited.add(parent_idx)
            parent = index_map[parent_idx]
            parent_class = _normalize_librenms_text(parent.get("entPhysicalClass"))
            names = [
                name
                for name in (
                    _normalize_librenms_text(parent.get("entPhysicalName")),
                    _normalize_librenms_text(parent.get("entPhysicalDescr")),
                )
                if name
            ]
            for bay in device_bays:
                if bay.name in names and getattr(bay, "installed_module", None):
                    return bay.installed_module.pk
            for name in names:
                # The walk resolves no module type for the parent row, so no bay type constrains the bay.
                bay = BaseModuleTableView._lookup_exact_bay_mapping(
                    name, parent_class, bays_by_name, exact_mappings, manufacturer_id, accepted_bay_types=frozenset()
                ) or BaseModuleTableView._lookup_regex_bay_mapping(
                    name, parent_class, bays_by_name, regex_mappings, manufacturer_id, accepted_bay_types=frozenset()
                )
                if bay is not None:
                    return bay.installed_module.pk
            current = parent

    @staticmethod
    def _parent_bays_by_name(device_bays):
        """
        Return the occupied bays a mapping can resolve to, keyed by a name exactly one occupied bay carries.

        The lookups skip a name that is absent, so a mapping to an empty bay gives way to the next mapping.

        Args:
            device_bays: The device's module bays, with ``installed_module`` selected.

        Returns:
            dict: The occupied bays keyed by name.

        """
        occupied: dict = {}
        for bay in device_bays:
            if getattr(bay, "installed_module", None):
                occupied.setdefault(bay.name, []).append(bay)
        return {name: bays[0] for name, bays in occupied.items() if len(bays) == 1}

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
        server_key = self.resolve_posted_server_key_or_none(request.POST)
        if server_key is None:
            messages.error(request, NO_LIBRENMS_SERVER_MESSAGE)
            return _modules_action_response(request, page_device)

        selected_indices = request.POST.getlist("select")
        if not selected_indices:
            messages.warning(request, "No modules selected.")
            return _modules_action_response(request, page_device, server_key)

        try:
            # Use dict.fromkeys to preserve order while deduplicating
            selected_list = list(dict.fromkeys(int(i) for i in selected_indices))
        except ValueError:
            messages.error(request, "Invalid selection.")
            return _modules_action_response(request, page_device, server_key)

        # Preload all ModuleBayMappings once to avoid N+1 per-item queries.
        # Manufacturer-scoping happens per-iteration since target_device may
        # differ across rows (VC members can have different device types).
        from netbox_librenms_plugin.utils import load_bay_mappings

        module_types = get_module_types_indexed()
        allowed_module_type_ids = set(self.restricted_queryset(ModuleType).values_list("pk", flat=True))
        all_exact, all_regex = load_bay_mappings()

        # Preload module_bay normalization rules once so _match_bay_for_item considers the
        # same normalized candidate names as the table/UI matcher.
        from netbox_librenms_plugin.utils import preload_normalization_rules

        norm_rules_bay = preload_normalization_rules("module_bay")
        type_bay_types = load_module_type_bay_types()

        holder_of_by_device = {}
        installed, skipped, failed = [], [], []
        bound_any = False
        try:
            with transaction.atomic():
                _lock_page_device_serials(page_device)
                granted = _authorize_module_grant(
                    self, page_device, server_key, ACTION_INSTALL_SELECTED, request.POST.get("inventory_binding")
                )
                granted.grant.require(device_id=page_device.pk)
                plan = self._granted_selection(request, granted, selected_list, page_device, server_key)
                # A row walks its parent modules in the render that authorized it: the manifest or a pinned override.
                manifest_maps = InstallBranchView._ancestry_maps(
                    granted, {target.pk for _item, target, pinned in plan if not pinned}
                )
                pinned_maps = {
                    target_id: InstallBranchView._attributed_rows(
                        granted, granted.policy.member(target_id), [], {target_id}
                    )[target_id]
                    for target_id in {target.pk for _item, target, pinned in plan if pinned}
                }
                for item, target_device, pinned in plan:
                    mfr_id = target_device.device_type.manufacturer_id
                    if target_device.pk not in holder_of_by_device:
                        holder_of_by_device[target_device.pk] = _module_holder_map(target_device)
                    holder_of = holder_of_by_device[target_device.pk]
                    exact_mappings = BaseModuleTableView._filter_mappings_by_manufacturer(all_exact, mfr_id)
                    regex_mappings = BaseModuleTableView._filter_mappings_by_manufacturer(all_regex, mfr_id)
                    result = InstallBranchView._install_single(
                        target_device,
                        item,
                        module_types,
                        naming_map=granted.index_map,
                        ancestry_map=(pinned_maps if pinned else manifest_maps)[target_device.pk],
                        exact_mappings=exact_mappings,
                        regex_mappings=regex_mappings,
                        manufacturer_id=mfr_id,
                        norm_rules_bay=norm_rules_bay,
                        norm_rules_serial=granted.policy.serial_rule_sets,
                        type_bay_types=type_bay_types,
                        module_bays=module_bays,
                        allowed_module_type_ids=allowed_module_type_ids,
                        changeable_components=changeable_components,
                        changeable_interfaces=changeable_interfaces,
                        deletable_interfaces=deletable_interfaces,
                        holder_of=holder_of,
                        user=request.user,
                    )
                    should_bind = _should_attempt_bind_for_result(result)
                    _record_install_result(result, installed, skipped, failed)

                    if should_bind:
                        bind_result = _bind_interface_librenms_id(
                            target_device,
                            item,
                            result.get("module_pk"),
                            server_key,
                            changeable_interfaces,
                        )
                        bound_any = _record_bind_outcome(bind_result, result, skipped) or bound_any
        except GrantRefused as exc:
            messages.error(request, exc.refusal.message)
            return _modules_action_response(request, page_device, server_key)
        except _ModuleActionRefused as exc:
            exc.report(request)
            return _modules_action_response(request, page_device, server_key)
        except (AbortRequest, ValidationError, IntegrityError) as e:
            if classify_conflict(e):
                raise
            # It looks for a constraint name in the text; the page gets fixed text or exception_text_for.
            # nosemgrep: caught-error-text  # noqa: ERA001
            detail = _module_write_failure(e, Module, request.user)
            messages.error(request, f"Install failed: {detail}")
            return _modules_action_response(request, page_device, server_key)

        _report_install_results(request, installed, skipped, failed)
        if installed or bound_any:
            _schedule_module_cache_mutation(request, page_device, server_key)
        return _modules_action_response(request, page_device, server_key)

    def _granted_selection(self, request, granted, selected_list, page_device, server_key):
        """
        Return ``[(row, target device, pinned)]`` for the selected indices, or refuse the whole request.

        A posted override, which a verify request signed for the row and its target, authorizes the
        row on any target, the manifest target too: the row then shows the verified render. Without an
        override, the row installs only onto the target the manifest signed and offered for it.
        ``pinned`` is True when the override, not the manifest, authorized the row.
        """
        from dcim.models import Device

        manifest = {index: (target_id, selectable) for index, target_id, selectable in granted.grant.rows}
        plan, refused = [], []
        for index in selected_list:
            entry = manifest.get(index)
            posted = request.POST.get(f"device_selection_{index}")
            target_id = entry[0] if entry and posted in (None, "") else _coerce_positive_int(posted)
            if entry is None or target_id is None:
                refused.append(index)
                continue
            override_token = request.POST.get(f"selection_grant_{index}")
            pinned = bool(override_token) or not (target_id == entry[0] and entry[1])
            if pinned:
                try:
                    override = read_module_grant(
                        override_token,
                        action=ACTION_INSTALL_SELECTED_ROW,
                        page_device_id=page_device.pk,
                        server_key=server_key,
                    )
                    override.require(device_id=target_id, ent_index=index)
                    if override.render_mode != RENDER_PINNED:
                        raise GrantRefused(GrantRefusal.STALE)
                    override.check_state(granted.inventory, granted.policy)
                except GrantRefused:
                    refused.append(index)
                    continue
            plan.append((index, target_id, pinned))
        if refused:
            names = [str(granted.index_map.get(index, {}).get("entPhysicalName") or index) for index in refused]
            raise _ModuleActionRefused(SELECTED_ROWS_REFUSED_MESSAGE.format(rows=", ".join(names)))
        target_ids = {target_id for _index, target_id, _pinned in plan}
        targets = {
            device.pk: device
            for device in self.restricted_queryset(Device)
            .filter(pk__in=target_ids)
            .select_related("device_type__manufacturer")
        }
        if len(targets) != len(target_ids):
            raise GrantRefused(GrantRefusal.DESTINATION)
        return [(granted.row(index), targets[target_id], pinned) for index, target_id, pinned in plan]


class UpdateModuleSerialView(
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    LibreNMSAPIMixin,
    CacheMixin,
    SyncSubjectClaimMixin,
    View,
):
    """Update the serial number of an already-installed module from LibreNMS inventory data."""

    SYNC_SUBJECT_MODEL_LABEL = "dcim.device"
    DROP_SYNC_SUBJECT_CLAIM_WITHOUT_SERVER = True

    def post(self, request, pk):
        from dcim.models import Device, Module

        self.required_object_permissions = {"POST": [("view", Device), ("change", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        page_device = self.restrict_object_or_404(Device, pk=pk)
        server_key = self.resolve_posted_server_key_or_none(request.POST)
        if server_key is None:
            messages.error(request, NO_LIBRENMS_SERVER_MESSAGE)
            return _modules_action_response(request, page_device)
        target_device = _resolve_destination(
            page_device, request.POST.get("selected_device_id"), self.restricted_queryset(Device)
        )
        if target_device is None:
            messages.error(request, GrantRefusal.DESTINATION.message)
            return _modules_action_response(request, page_device, server_key)

        try:
            module_id = int(request.POST.get("module_id"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module ID.")
            return _modules_action_response(request, page_device, server_key)

        try:
            with transaction.atomic():
                granted = _authorize_module_grant(
                    self, page_device, server_key, ACTION_UPDATE_SERIAL, request.POST.get("inventory_binding")
                )
                granted.grant.require(
                    device_id=target_device.pk,
                    ent_index=request.POST.get("ent_index", ""),
                    action_target={"module_id": module_id},
                )
                librenms_item = granted.row(granted.grant.ent_index)
                serial = normalize_inventory_serial(
                    librenms_item.get("entPhysicalSerialNum"),
                    manufacturer=target_device.device_type.manufacturer,
                    preloaded_rules=granted.policy.serial_rule_sets,
                )
                if is_librenms_placeholder(serial):
                    serial = ""
                module = (
                    self.restricted_queryset(Module, "change")
                    .select_for_update(of=("self",))
                    .select_related("module_type", "module_bay")
                    .filter(pk=module_id, device=target_device)
                    .first()
                )
                if not module:
                    raise _ModuleActionRefused("Module no longer exists.")
                changed = module.serial != serial
                if changed:
                    module.snapshot()
                    module.serial = serial
                    module.full_clean()
                    module.save(update_fields=["serial", "last_updated"])
            if changed:
                messages.success(
                    request,
                    f"Updated serial for {module.module_type.model} in {module.module_bay.name} to '{serial}'.",
                )
                if server_key:
                    _schedule_module_cache_mutation(request, page_device, server_key)
            else:
                messages.info(request, "The module serial already matches LibreNMS. No change was needed.")
        except GrantRefused as exc:
            messages.error(request, exc.refusal.message)
        except _ModuleActionRefused as exc:
            exc.report(request)
        except (AbortRequest, ValidationError, IntegrityError) as e:
            if classify_conflict(e):
                raise
            # It looks for a constraint name in the text; the page gets fixed text or exception_text_for.
            # nosemgrep: caught-error-text  # noqa: ERA001
            detail = _module_write_failure(e, Module, request.user)
            messages.error(request, f"Failed to update serial: {detail}")

        return _modules_action_response(request, page_device, server_key)


class UpdateModuleInterfaceView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View
):
    """Associate a matching NetBox interface with an already-installed module."""

    def post(self, request, pk):
        from dcim.models import Device, Interface, Module

        self.required_object_permissions = {"POST": [("view", Device), ("view", Module), ("change", Interface)]}
        if error := self.require_all_permissions("POST"):
            return error

        # Resolve the page device first: every early exit below re-renders that device's module tab.
        page_device = self.restrict_object_or_404(Device, pk=pk)
        server_key = self.resolve_posted_server_key_or_none(request.POST)
        if server_key is None:
            messages.error(request, NO_LIBRENMS_SERVER_MESSAGE)
            return _modules_action_response(request, page_device)
        try:
            module_id = int(request.POST.get("module_id"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module ID.")
            return _modules_action_response(request, page_device, server_key)

        target_device = _resolve_destination(
            page_device, request.POST.get("selected_device_id"), self.restricted_queryset(Device)
        )
        if target_device is None:
            messages.error(request, GrantRefusal.DESTINATION.message)
            return _modules_action_response(request, page_device, server_key)
        changeable_interfaces = self.restricted_queryset(Interface, "change")
        module = self.restrict_object_or_404(Module, "view", pk=module_id, device=target_device)

        bind_result = None
        adoption_failed = False
        try:
            # One transaction: a lock conflict in the bind or the adoption rolls back both.
            with transaction.atomic():
                granted = _authorize_module_grant(
                    self, page_device, server_key, ACTION_UPDATE_INTERFACE, request.POST.get("inventory_binding")
                )
                granted.grant.require(
                    device_id=target_device.pk,
                    ent_index=request.POST.get("ent_index", ""),
                    action_target={"module_id": module_id},
                )
                bind_item = granted.row(granted.grant.ent_index)
                bind_result, adoption_failed = self._bind_and_adopt(
                    target_device, module, bind_item, server_key, changeable_interfaces
                )
        except GrantRefused as exc:
            messages.error(request, exc.refusal.message)
            return _modules_action_response(request, page_device, server_key)
        except _ModuleActionRefused as exc:
            exc.report(request)
            return _modules_action_response(request, page_device, server_key)
        if adoption_failed:
            messages.warning(
                request,
                "Primary interface binding succeeded, but adopting standalone "
                "template interfaces failed; see server logs for details.",
            )

        if bind_result is None:
            messages.error(request, "No LibreNMS interface identity is available for this row.")
        elif bind_result.get("status") == "bound":
            location = f"{module.module_type.model} in {module.module_bay.name}"
            messages.success(request, _module_interface_update_message(bind_result, location))
            if _module_bind_result_changed(bind_result):
                _schedule_module_cache_mutation(request, page_device, server_key)
        else:
            messages.warning(
                request,
                f"Could not update interface association: {bind_result.get('reason', 'unknown reason')}",
            )

        return _modules_action_response(request, page_device, server_key)

    @staticmethod
    def _bind_and_adopt(target_device, module, bind_item, server_key, changeable_interfaces):
        """Bind the row's LibreNMS port and adopt the module's standalone template interfaces; return the outcome."""
        bind_result = None
        adoption_failed = False
        # The missing-server guard above already returned, so a resolved primary is always bound.
        try:
            bind_result = _bind_interface_librenms_id(
                target_device,
                bind_item,
                module.pk,
                server_key,
                changeable_interfaces,
            )
        except Exception as exc:
            if classify_conflict(exc):
                raise
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
            # nothing to do (None) or succeeded (bound). Otherwise an already-bound interface
            # makes the bind a no-op, the adoption is skipped, and the button never clears.
            # A hard conflict/skip is left untouched so we don't mutate past an unresolved issue.
            if bind_result is None or bind_result.get("status") == "bound":
                try:
                    with transaction.atomic():
                        adopt_result = _adopt_existing_template_interfaces(
                            target_device,
                            module,
                            changeable_interfaces,
                        )
                except Exception as exc:
                    if classify_conflict(exc):
                        raise
                    logger.exception(
                        "Unexpected error adopting standalone template interfaces (device %s, module %s)",
                        target_device.pk,
                        module.pk,
                    )
                    # The adoption's savepoint keeps the primary bind: that interface is bound
                    # regardless, and reporting "failed" would make a retry look like a fresh
                    # conflict. Only the bind-less path downgrades to a hard failure.
                    if bind_result is None:
                        bind_result = {
                            "status": "failed",
                            "reason": "unexpected error while associating interface to installed module",
                        }
                    else:
                        adoption_failed = True
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
                            "changed": bind_result.get("changed", False),
                            "adopted_count": (bind_result.get("adopted_count") or 0)
                            + (adopt_result.get("adopted_count") or 0),
                        }
        return bind_result, adoption_failed


def _module_interface_type_targets(device, module):
    """Return typed interface-template targets keyed by predicted interface name."""
    return {
        name: template_type
        for name, template_type in get_module_template_interface_specs(device, module)
        if template_type
    }


def _module_interface_skip_message(names, singular_reason, plural_reason):
    """Compose one warning naming the interfaces an apply left alone."""
    if len(names) == 1:
        return f"Skipped 1 interface because {singular_reason}: {names[0]}."
    return f"Skipped {len(names)} interfaces because {plural_reason}: {', '.join(names)}."


def _module_interface_success_message(count):
    """Compose the success message for applied interface types."""
    noun = "type" if count == 1 else "types"
    interface_noun = "interface" if count == 1 else "interfaces"
    possessive = "its" if count == 1 else "their"
    template_noun = "template" if count == 1 else "templates"
    return f"Updated the {noun} of {count} {interface_noun} from {possessive} module {template_noun}."


def _apply_module_interface_type(interface, template_type, current_type, offered_template_type):
    """Apply one offered template type and return its outcome and ``TypeRefusal`` (None unless it is refused)."""
    if interface.type != current_type:
        return "interface_changed", None
    if template_type != offered_template_type:
        return "template_changed", None
    if not template_type or interface.type == template_type:
        return "not_different", None

    refusal = type_change_refusal(interface, template_type)
    if refusal is not None:
        return "validation_failed", refusal
    interface.snapshot()
    interface.type = template_type
    interface.save(update_fields=["type", "last_updated"])
    return "updated", None


def module_interface_type_preview_permissions():
    """Return the (action, model) grants that the interface type preview needs."""
    from dcim.models import Device, Interface, Module

    return [("view", Device), ("view", Module), ("view", Interface)]


class ModuleInterfaceTypePreviewView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Render the current module interface type differences."""

    def get(self, request, pk):
        from dcim.models import Device, Interface, Module

        self.required_object_permissions = {"GET": module_interface_type_preview_permissions()}
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
        server_key = self.resolve_posted_server_key_or_none(request.GET)
        if server_key is None:
            return HttpResponse(NO_LIBRENMS_SERVER_MESSAGE, status=400)

        try:
            module_id = int(request.GET.get("module_id"))
        except (TypeError, ValueError):
            return HttpResponse("Missing or invalid module_id.", status=400)

        module = self.restrict_object_or_404(
            Module,
            "view",
            select_related=("module_type", "module_bay", "device"),
            pk=module_id,
            device=target_device,
        )
        template_types = _module_interface_type_targets(target_device, module)
        interfaces = (
            self.restricted_queryset(Interface).filter(module=module, name__in=template_types).order_by("name", "pk")
        )
        mismatches = [
            {
                "interface": interface,
                "template_type": template_types[interface.name],
                "template_type_label": Interface(type=template_types[interface.name]).get_type_display(),
            }
            for interface in interfaces
            if interface.type != template_types[interface.name]
        ]

        return render(
            request,
            "netbox_librenms_plugin/htmx/module_interface_type_modal.html",
            {
                "device_pk": page_device.pk,
                "module": module,
                "mismatches": mismatches,
                "server_key": server_key,
                "selected_device_id": target_device.pk,
            },
        )


class _InterfaceChangeScopeViolation(Exception):
    """Abort a type update that leaves the caller's change permission scope."""


class ApplyModuleInterfaceTypesView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View
):
    """Apply selected module interface-template types after checking preview state."""

    def _assert_updated_interface_change_scope(self, interface, outcome):
        from dcim.models import Interface

        if outcome == "updated" and not self.restricted_queryset(Interface, "change").filter(pk=interface.pk).exists():
            raise _InterfaceChangeScopeViolation(interface.name)

    def post(self, request, pk):  # noqa: C901
        from dcim.models import Device, Interface, Module

        self.required_object_permissions = {"POST": [("view", Device), ("view", Module), ("change", Interface)]}
        if error := self.require_all_permissions("POST"):
            return error

        page_device = self.restrict_object_or_404(Device, pk=pk)
        server_key = self.resolve_posted_server_key_or_none(request.POST)
        if server_key is None:
            messages.error(request, NO_LIBRENMS_SERVER_MESSAGE)
            return _modules_action_response(request, page_device)

        target_device, invalid_selected_device = _resolve_target_device_with_validation(
            page_device,
            request.POST.get("selected_device_id"),
            self.restricted_queryset(Device),
        )
        if invalid_selected_device:
            _warn_invalid_selected_device(request)

        try:
            module_id = int(request.POST.get("module_id"))
            interface_ids = {int(interface_id) for interface_id in request.POST.getlist("interface_id")}
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module or interface ID.")
            return _modules_action_response(request, page_device, server_key)

        module = self.restrict_object_or_404(Module, "view", pk=module_id, device=target_device)
        if not interface_ids:
            messages.warning(request, "No interfaces were selected.")
            return _modules_action_response(request, page_device, server_key)

        outcomes = {
            "updated": [],
            "interface_changed": [],
            "template_changed": [],
            "not_different": [],
        }
        validation_failures = []
        try:
            with transaction.atomic():
                template_types = _module_interface_type_targets(target_device, module)
                interfaces = list(
                    self.restricted_queryset(Interface, "change")
                    .select_for_update(of=("self",))
                    .filter(module=module, pk__in=interface_ids)
                    .order_by("name", "pk")
                )
                unavailable_count = len(interface_ids) - len(interfaces)
                for interface in interfaces:
                    template_type = template_types.get(interface.name)
                    outcome, reason = _apply_module_interface_type(
                        interface,
                        template_type,
                        request.POST.get(f"current_type_{interface.pk}"),
                        request.POST.get(f"template_type_{interface.pk}"),
                    )
                    self._assert_updated_interface_change_scope(interface, outcome)
                    if reason is not None:
                        validation_failures.append((interface.name, reason))
                    else:
                        outcomes[outcome].append(interface.name)
        except _InterfaceChangeScopeViolation as exc:
            messages.error(
                request,
                f"No interface types were changed. The updated interface is outside your change permission scope: {exc}.",
            )
            return _modules_action_response(request, page_device, server_key)

        updated_names = outcomes["updated"]
        if updated_names:
            _schedule_module_cache_mutation(request, page_device, server_key)
            messages.success(request, _module_interface_success_message(len(updated_names)))
        if changed_names := outcomes["interface_changed"]:
            messages.warning(
                request,
                _module_interface_skip_message(
                    changed_names,
                    "it changed after the preview",
                    "they changed after the preview",
                ),
            )
        if changed_template_names := outcomes["template_changed"]:
            messages.warning(
                request,
                _module_interface_skip_message(
                    changed_template_names,
                    "its template type changed after the preview",
                    "their template types changed after the preview",
                ),
            )
        if no_longer_different_names := outcomes["not_different"]:
            messages.warning(
                request,
                _module_interface_skip_message(
                    no_longer_different_names,
                    "its template no longer requires a type change",
                    "their templates no longer require a type change",
                ),
            )
        for interface_name, refusal in validation_failures:
            messages.warning(request, f"Skipped {interface_name} because {refusal.text_for(request.user).rstrip('.')}.")
        if unavailable_count:
            if unavailable_count == 1:
                detail = "1 selected interface because it is unavailable for this module"
            else:
                detail = f"{unavailable_count} selected interfaces because they are unavailable for this module"
            messages.warning(request, f"Skipped {detail}.")

        # No inventory binding token is needed: object IDs identify the targets, and current types guard the race.
        return _modules_action_response(request, page_device, server_key)


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
        server_key = self.resolve_posted_server_key_or_none(request.GET)
        if server_key is None:
            return HttpResponse(NO_LIBRENMS_SERVER_MESSAGE, status=400)
        target_device = _resolve_destination(
            page_device, request.GET.get("selected_device_id"), self.restricted_queryset(Device)
        )
        if target_device is None:
            return HttpResponse(GrantRefusal.DESTINATION.message, status=400)

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

        # The preview signs the replace and serial actions, so it needs the table's own grant for this row.
        action_target = {"module_id": installed_module.pk}
        try:
            granted = _authorize_module_grant(
                self, page_device, server_key, ACTION_MISMATCH_PREVIEW, request.GET.get("inventory_binding")
            )
            grant = granted.grant.require(
                device_id=target_device.pk, ent_index=ent_index_int, action_target=action_target
            )
            librenms_item = granted.row(grant.ent_index)
        except GrantRefused as exc:
            return HttpResponse(exc.refusal.message, status=400)
        except _ModuleActionRefused as exc:
            return HttpResponse(exc.message, status=400)

        from netbox_librenms_plugin.utils import module_type_lookup_candidates, resolve_module_type

        manufacturer = target_device.device_type.manufacturer
        librenms_model = normalize_serial(librenms_item.get("entPhysicalModelName")) or "-"
        # The preview sits beside the stored module serial, which the install path writes through
        # this same normalization, so the raw inventory value would read as a mismatch.
        librenms_serial = normalize_inventory_serial(
            librenms_item.get("entPhysicalSerialNum"),
            manufacturer=manufacturer,
            preloaded_rules=granted.policy.serial_rule_sets,
        )
        if is_librenms_placeholder(librenms_serial):
            librenms_serial = ""

        # Detect type mismatch
        module_types = get_module_types_indexed()
        matched_type = resolve_module_type(
            librenms_model if librenms_model != "-" else "",
            module_types,
            manufacturer=manufacturer,
            fallback_names=module_type_lookup_candidates(librenms_item),
        )

        type_mismatch = matched_type is not None and installed_module.module_type_id != matched_type.pk
        # type_matched: the LibreNMS model RESOLVED to the installed module's type (directly or via a
        # ModuleTypeMapping). Distinct from "not type_mismatch", which also covers an UNRECOGNISED model
        # (matched_type is None). The modal uses this to badge the LibreNMS model when its display string
        # differs from the NetBox type but is a confirmed match (the common serial-mismatch case).
        type_matched = matched_type is not None and installed_module.module_type_id == matched_type.pk
        installed_serial = (installed_module.serial or "").strip()
        if is_librenms_placeholder(installed_serial):
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
                "serial_inventory_binding": self._derived_grant(grant, ACTION_UPDATE_SERIAL),
                "replace_inventory_binding": self._derived_grant(grant, ACTION_REPLACE_MODULE),
            },
        )

    @staticmethod
    def _derived_grant(grant, action):
        """Sign a modal action with the page, target, snapshot, policy and render mode of the preview grant."""
        return sign_module_grant(
            page_device_id=grant.page_device_id,
            device_id=grant.device_id,
            server_key=grant.server_key,
            action=action,
            ent_index=grant.ent_index,
            action_target=grant.action_target,
            inventory_digest=grant.inventory_digest,
            policy_fingerprint=grant.policy_fingerprint,
            render_mode=grant.render_mode,
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

    def post(self, request, pk):  # noqa: C901
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
        changeable_components = _restricted_module_component_querysets(self)
        changeable_interfaces = changeable_components[Interface]
        deletable_interfaces = self.restricted_queryset(Interface, "delete")
        server_key = self.resolve_posted_server_key_or_none(request.POST)
        if server_key is None:
            messages.error(request, NO_LIBRENMS_SERVER_MESSAGE)
            return _modules_action_response(request, page_device)
        target_device = _resolve_destination(
            page_device, request.POST.get("selected_device_id"), self.restricted_queryset(Device)
        )
        if target_device is None:
            messages.error(request, GrantRefusal.DESTINATION.message)
            return _modules_action_response(request, page_device, server_key)

        try:
            module_id = int(request.POST.get("module_id"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module ID.")
            return _modules_action_response(request, page_device, server_key)

        installed_module = self.restrict_object_or_404(
            Module,
            "change",
            select_related=("module_type", "module_bay"),
            pk=module_id,
            device=target_device,
            pk__in=self.restricted_queryset(Module, "delete").values("pk"),
        )

        from netbox_librenms_plugin.utils import module_type_lookup_candidates, resolve_module_type

        manufacturer = target_device.device_type.manufacturer
        try:
            conflict_removed_msg = None
            conflict_device = None
            bind_result = None
            adopted_components = 0
            vc_adjustments = {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 0}
            with transaction.atomic():
                granted = _authorize_module_grant(
                    self, page_device, server_key, ACTION_REPLACE_MODULE, request.POST.get("inventory_binding")
                )
                granted.grant.require(
                    device_id=target_device.pk,
                    ent_index=request.POST.get("ent_index", ""),
                    action_target={"module_id": module_id},
                )
                librenms_item = granted.row(granted.grant.ent_index)
                model_name = normalize_serial(librenms_item.get("entPhysicalModelName"))
                # The replacement is stored and matched against serials the install path already
                # normalized, so a raw vendor marker here would never match either.
                serial = normalize_inventory_serial(
                    librenms_item.get("entPhysicalSerialNum"),
                    manufacturer=manufacturer,
                    preloaded_rules=granted.policy.serial_rule_sets,
                )
                if is_librenms_placeholder(serial):
                    serial = ""
                matched_type = resolve_module_type(
                    model_name,
                    get_module_types_indexed(),
                    manufacturer=manufacturer,
                    fallback_names=module_type_lookup_candidates(librenms_item),
                )
                if not matched_type or not self.restricted_queryset(ModuleType).filter(pk=matched_type.pk).exists():
                    raise _ModuleActionRefused(f"No matching module type found for '{model_name}'.")

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
                    raise _ModuleActionRefused("Module no longer exists.")

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
                    conflict_device = conflict_module.device
                    c_device = conflict_device.name
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
                # In the replace's transaction: a lock conflict in the bind rolls back the replace too.
                if server_key:
                    try:
                        bind_result = _bind_interface_librenms_id(
                            target_device,
                            librenms_item,
                            new_module.pk,
                            server_key,
                            changeable_interfaces,
                        )
                    except Exception as exc:
                        if classify_conflict(exc):
                            raise
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
            _schedule_module_cache_mutation(request, page_device, server_key)
        except GrantRefused as exc:
            messages.error(request, exc.refusal.message)
        except _ModuleActionRefused as exc:
            exc.report(request)
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
        except (AbortRequest, ValidationError, IntegrityError) as e:
            if classify_conflict(e):
                raise
            # It looks for a constraint name in the text; the page gets fixed text or exception_text_for.
            # nosemgrep: caught-error-text  # noqa: ERA001
            detail = _module_write_failure(e, Module, request.user)
            messages.error(request, f"Replace failed: {detail}")

        return _modules_action_response(request, page_device, server_key)


def _parse_move_ids(post):
    """Return (module, target bay, optional occupant) IDs from a move form, or None when a required one is invalid."""
    try:
        conflict_module_id = int(post.get("conflict_module_id"))
        target_bay_id = int(post.get("target_bay_id"))
    except (TypeError, ValueError):
        return None
    raw_module_id = post.get("module_id")
    try:
        module_id = int(raw_module_id) if raw_module_id else None
    except (TypeError, ValueError):
        module_id = None
    return conflict_module_id, target_bay_id, module_id


class MoveModuleView(
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    LibreNMSAPIMixin,
    SyncSubjectClaimMixin,
    View,
):
    """
    Move an existing module from its current location to a target bay.

    Handles the case where a module (identified by serial) has been physically
    moved from one slot to another — possibly on a different device.  Updates
    the module_bay (and device when moving cross-device) rather than deleting
    and recreating, preserving the module's history.
    """

    SYNC_SUBJECT_MODEL_LABEL = "dcim.device"
    DROP_SYNC_SUBJECT_CLAIM_WITHOUT_SERVER = True

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
        server_key = self.resolve_posted_server_key_or_none(request.POST)

        # The rendered control is advisory; a scripted POST never reads it.
        if not netbox_relocates_module_subtree():
            messages.error(request, MODULE_MOVE_REQUIRES_NETBOX_MESSAGE)
            return _modules_action_response(request, page_device, server_key)

        if (move_ids := _parse_move_ids(request.POST)) is None:
            messages.error(request, "Missing or invalid conflict_module_id/target_bay_id.")
            return _modules_action_response(request, page_device, server_key)
        conflict_module_id, target_bay_id, module_id = move_ids

        self.restrict_object_or_404(ModuleBay, pk=target_bay_id, device=target_device)

        occupant_removed_msg = from_device = from_bay = None

        def move(module):
            nonlocal occupant_removed_msg, from_device, from_bay
            # Remove whatever is currently in the target bay (if provided and different).
            # Scoped by "delete": the device and bay filters prove where the row sits, not that
            # the grant covers it, and the gate asked has_perm without an instance.
            if module_id and module_id != module.pk:
                occupant = (
                    self.restricted_queryset(Module, "delete")
                    .select_for_update(of=("self",))
                    .filter(pk=module_id, device=target_device, module_bay=target_bay)
                    .first()
                )
                if occupant:
                    occupant_removed_msg = f"Removed {occupant.module_type.model} from {target_bay.name}."
                    occupant.delete()
            from_device, from_bay = module.device.name, module.module_bay.name
            module.module_bay = target_bay
            module.device = target_device

        try:
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
                    return _modules_action_response(request, page_device, server_key)

                # Scoped like the primary lookup: this module's device and bay are reassigned, and
                # its pk comes straight from the POST, so an unscoped read would move a module the
                # user's grant does not cover. The module is locked before the occupant.
                try:
                    conflict_module = update_existing_row(
                        self.restricted_queryset(Module, "change")
                        .filter(pk=conflict_module_id)
                        .select_related("module_type", "module_bay", "device"),
                        move,
                    )
                except Module.DoesNotExist:
                    messages.error(request, "Module no longer exists.")
                    return _modules_action_response(request, page_device, server_key)

            if occupant_removed_msg:
                messages.info(request, occupant_removed_msg)
            moved_msg = f"Moved {conflict_module.module_type.model}"
            if from_device != target_device.name:
                moved_msg += f" from {from_device}"
            moved_msg += f"/{from_bay} to {target_bay.name}."
            messages.success(request, moved_msg)
            if server_key:
                _schedule_module_cache_mutation(request, page_device, server_key)
        except (AbortRequest, ValidationError, IntegrityError) as e:
            if classify_conflict(e):
                raise
            # It looks for a constraint name in the text; the page gets fixed text or exception_text_for.
            # nosemgrep: caught-error-text  # noqa: ERA001
            detail = _module_write_failure(e, Module, request.user)
            messages.error(request, f"Move failed: {detail}")

        return _modules_action_response(request, page_device, server_key)


class AddBayTemplateView(
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    LibreNMSAPIMixin,
    SyncSubjectClaimMixin,
    View,
):
    """
    Create a missing ModuleBayTemplate on a Device Type or Module Type.

    GET renders a pre-filled modal fragment that targets ``#htmx-modal-content``;
    POST creates the bay template and returns the user to the modules tab.
    """

    SYNC_SUBJECT_MODEL_LABEL = "dcim.device"
    DROP_SYNC_SUBJECT_CLAIM_WITHOUT_SERVER = True

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
        Materialize a saved ``ModuleBayTemplate`` on every existing target instance.

        NetBox auto-creates bays from templates only when a Device/Module is
        first created. A template added later is invisible to existing
        instances until manually instantiated. ``target_kind`` selects the
        scope: device-type templates are instantiated on every Device of that
        type; module-type templates are instantiated on every installed Module
        of that type. Pre-existing bays with the resolved name (under the same
        device/module scope) are skipped so re-adding a template after a
        partial manual fix is safe.

        Args:
            bay_template (ModuleBayTemplate): The saved module bay template to instantiate.
            target_kind (str): The target type, either ``device_type`` or ``module_type``.
            target (DeviceType | ModuleType): The device type or module type that owns the template.

        Returns:
            int: The number of module bays created on existing instances.

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
        r"""
        Derive a regex ``ModuleBayMapping`` rule from two example names.

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
        except REGEX_COMPILE_ERRORS:
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
        Return whether an existing regex ModuleBayMapping already matches the requested scope.

        The function checks ``librenms_name`` for the given manufacturer or global scope.
        It iterates regex rows in Python. The row count is small (one per
        bay-family), so this is cheap, and Postgres cannot compare its
        re-flavoured patterns server-side anyway.

        Args:
            librenms_name (str): The LibreNMS bay name to match.
            librenms_class (str): The LibreNMS inventory class to match.
            manufacturer (Manufacturer | None): The optional manufacturer scope.

        Returns:
            bool: Whether an existing regular expression mapping covers the name and scope.

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
            except REGEX_COMPILE_ERRORS:
                continue
        return False

    @staticmethod
    def _existing_bay_mapping(librenms_name, librenms_class, manufacturer):
        """
        Return whether a ModuleBayMapping already covers the requested name, class, and scope.

        The scope is the given manufacturer (vendor-scoped) or global (manufacturer is null).
        This function checks only exact mappings. It intentionally ignores regex mappings
        because the per-row suggestion is for one specific name and we do not
        want to second-guess broader patterns the operator already wrote.

        Args:
            librenms_name (str): The LibreNMS bay name to match.
            librenms_class (str): The LibreNMS inventory class to match.
            manufacturer (Manufacturer | None): The optional manufacturer scope.

        Returns:
            bool: Whether an existing exact mapping covers the name, class, and scope.

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

    def _map_existing_bay(self, request, pk):
        """Reuse the mapping proposal with a visible, empty bay on this device."""
        from dcim.models import Device, ModuleBay

        from netbox_librenms_plugin.models import ModuleBayMapping

        self.required_object_permissions = {
            request.method: [("view", Device), ("view", ModuleBay), ("add", ModuleBayMapping)]
        }
        if error := self.require_all_permissions(request.method):
            return error
        device = self.restrict_object_or_404(Device, pk=pk)
        available_bay_names = sorted(
            set(
                ModuleBay.objects.restrict(request.user, "view")
                .filter(device=device, module__isnull=True, installed_module__isnull=True)
                .values_list("name", flat=True)
            )
        )
        inputs = request.POST if request.method == "POST" else request.GET
        librenms_name = inputs.get("librenms_name", "").strip()
        librenms_class = inputs.get("librenms_class", "").strip()
        if not librenms_name:
            return HttpResponse("LibreNMS inventory name is required.", status=400)
        manufacturer = self._device_manufacturer(device)
        if request.method == "POST":
            name = inputs.get("name", "")
            if name not in available_bay_names:
                return HttpResponse("Select a visible, empty bay on this device.", status=400)
            mapping_kind = inputs.get("mapping_kind", "exact")
            if mapping_kind not in ("exact", "regex"):
                return HttpResponse("Invalid mapping kind.", status=400)
            pattern = self._derive_mapping_pattern(librenms_name, name) if mapping_kind == "regex" else None
            if mapping_kind == "regex" and pattern is None:
                return HttpResponse("These names do not support the proposed regex mapping.", status=400)
            server_key = self.resolve_posted_server_key_or_none(request.POST)
            mapping = ModuleBayMapping(
                librenms_name=pattern["librenms_pattern"] if pattern else librenms_name,
                librenms_class=librenms_class,
                netbox_bay_name=pattern["netbox_replacement"] if pattern else name,
                is_regex=pattern is not None,
                manufacturer=manufacturer,
            )
            try:
                with transaction.atomic():
                    mapping.full_clean()
                    mapping.save()
            except (ValidationError, IntegrityError) as exc:
                # It looks for a constraint name in the text; the page gets fixed text or exception_text_for.
                # nosemgrep: caught-error-text  # noqa: ERA001
                detail = _module_write_failure(exc, ModuleBayMapping, request.user)
                messages.error(request, f"Failed to add bay mapping: {detail}")
            else:
                messages.success(request, f"Added bay mapping for '{librenms_name}' to '{name}'.")
                if server_key:
                    _schedule_module_cache_mutation(request, device, server_key)
            return _modules_action_response(request, device, server_key)

        # Step one picks the bay; step two settles the mapping kind for THAT bay. The kind used
        # to be derived from the alphabetically first offered bay, so an unrelated bay decided
        # it for every other one: on an MX304 "LCMIC1" derives nothing from "Routing Engine 0",
        # which forced the exact default onto the whole Routing Engine family.
        # Not stripped: the POST matches the submitted name against the stored bay names as-is,
        # so a bay stored with surrounding whitespace must survive step two unchanged.
        chosen_name = inputs.get("name", "")
        # A bay filled since the modal opened is no longer offered, so return a fresh chooser
        # rather than reviewing a mapping onto a bay that can no longer be saved.
        on_kind_step = inputs.get("step") == "kind" and chosen_name in available_bay_names
        mapping_pattern = self._derive_mapping_pattern(librenms_name, chosen_name) if on_kind_step else None
        return render(
            request,
            "netbox_librenms_plugin/htmx/add_bay_template_modal.html",
            {
                "device_pk": pk,
                "mapping_only": True,
                "mapping_step": "kind" if on_kind_step else "bay",
                "chosen_name": chosen_name if on_kind_step else "",
                # A single offered bay is not a guess, so preselect it. With several, the
                # operator picks: choosing for them is what proposed an unrelated bay before.
                "preselected_name": available_bay_names[0] if len(available_bay_names) == 1 else "",
                "available_bay_names": available_bay_names,
                "librenms_name": librenms_name,
                "librenms_class": librenms_class,
                "manufacturer_label": str(manufacturer) if manufacturer else "",
                "offer_mapping_checkbox": bool(available_bay_names),
                "mapping_pattern": mapping_pattern,
                "mapping_default_kind": "regex" if mapping_pattern else "exact",
                "server_key": request.GET.get("server_key", ""),
            },
        )

    def get(self, request, pk):
        if request.GET.get("mode") == "map_existing":
            return self._map_existing_bay(request, pk)

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
            "server_key": request.GET.get("server_key", ""),
        }
        return render(request, "netbox_librenms_plugin/htmx/add_bay_template_modal.html", context)

    def post(self, request, pk):  # noqa: C901
        if request.POST.get("mode") == "map_existing":
            return self._map_existing_bay(request, pk)

        from dcim.models import Device, DeviceType, ModuleBay, ModuleBayTemplate, ModuleType

        from netbox_librenms_plugin.models import ModuleBayMapping

        target_kind = request.POST.get("target_kind", "")
        if target_kind not in self.TARGET_KINDS:
            # The modal echoes target_kind from its GET render, so a bad value is a tampered request: refuse it like get().
            return HttpResponse("Invalid target_kind.", status=400)
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
        server_key = self.resolve_posted_server_key_or_none(request.POST)

        try:
            target_pk = int(request.POST.get("target_pk", ""))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid target_pk for bay template.")
            return _modules_action_response(request, device, server_key)

        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Bay template name is required.")
            return _modules_action_response(request, device, server_key)
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
            if server_key:
                _schedule_module_cache_mutation(request, device, server_key)
        except (ValidationError, IntegrityError) as e:
            if classify_conflict(e):
                raise
            # It looks for a constraint name in the text; the page gets fixed text or exception_text_for.
            # nosemgrep: caught-error-text  # noqa: ERA001
            detail = _module_write_failure(e, ModuleBayTemplate, request.user)
            messages.error(request, f"Failed to add bay template: {detail}")

        return _modules_action_response(request, device, server_key)
