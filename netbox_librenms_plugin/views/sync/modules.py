"""Sync action views for module/inventory installation from LibreNMS."""

import re

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from netbox_librenms_plugin.utils import get_librenms_sync_device, get_module_types_indexed
from netbox_librenms_plugin.views.base.modules_view import _PLACEHOLDER_VALUES
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)


def _modules_redirect_response(request, sync_url):
    """Return a redirect response that works for both classic and HTMX form posts.

    For HTMX requests (those carrying ``HX-Request: true``) we return an empty
    200 response with an ``HX-Redirect`` header so the browser performs a full
    navigation that picks up Django messages and refreshes the modules table.
    For non-HTMX requests we return a normal Django redirect.
    """
    target = f"{sync_url}?tab=modules#librenms-module-table"
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


def _report_install_results(request, installed, skipped, failed):
    """Emit Django messages summarising an install run."""
    if installed:
        messages.success(request, f"Installed {len(installed)} module(s): {', '.join(installed)}")
    if skipped:
        messages.info(request, f"Skipped {len(skipped)}: {'; '.join(skipped)}")
    if failed:
        messages.warning(request, f"Failed {len(failed)}: {'; '.join(failed)}")


def _resolve_target_device(page_device, selected_device_id):
    """Resolve and validate a target device from row-level VC selection."""
    if not selected_device_id:
        return page_device

    try:
        selected_device_id = int(selected_device_id)
    except (TypeError, ValueError):
        return page_device

    if not getattr(page_device, "virtual_chassis", None):
        return page_device

    member = page_device.virtual_chassis.members.filter(pk=selected_device_id).first()
    return member or page_device


class _SerialConflictAmbiguous(Exception):
    """Raised inside ReplaceModuleView's transaction when more than one module
    holds the incoming serial — used to abort the atomic block and surface a
    user-friendly error after the rollback."""

    def __init__(self, serial):
        super().__init__(serial)
        self.serial = serial


def _get_sync_device_for_inventory(device, server_key):
    """Return the VC sync device used for module inventory cache keys."""
    return get_librenms_sync_device(device, server_key=server_key) or device


class InstallModuleView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """Install a NetBox Module into a ModuleBay from LibreNMS inventory data."""

    def post(self, request, pk):
        from dcim.models import Device, Module, ModuleBay, ModuleType

        self.required_object_permissions = {"POST": [("add", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        page_device = get_object_or_404(Device, pk=pk)
        target_device = _resolve_target_device(page_device, request.POST.get("selected_device_id"))
        serial = request.POST.get("serial", "").strip()
        if serial.lower() in _PLACEHOLDER_VALUES:
            serial = ""
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        try:
            module_bay_id = int(request.POST.get("module_bay_id"))
            module_type_id = int(request.POST.get("module_type_id"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module bay/module type ID.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        get_object_or_404(ModuleBay, pk=module_bay_id, device=target_device)  # verify bay belongs to selected device
        module_type = get_object_or_404(ModuleType, pk=module_type_id)

        try:
            with transaction.atomic():
                # Re-fetch bay under lock to prevent TOCTOU race with concurrent installs.
                locked_bay = ModuleBay.objects.select_for_update().get(pk=module_bay_id)
                if hasattr(locked_bay, "installed_module") and locked_bay.installed_module:
                    messages.warning(request, f"Module bay '{locked_bay.name}' already has a module installed.")
                    return redirect(f"{sync_url}?tab=modules#librenms-module-table")
                module = Module(
                    device=target_device,
                    module_bay=locked_bay,
                    module_type=module_type,
                    serial=serial,
                    status="active",
                )
                module.full_clean()
                module.save()

            messages.success(
                request, f"Installed {module_type.model} in {locked_bay.name} (serial: {serial or 'N/A'})."
            )
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Failed to install module: {e}")

        return redirect(f"{sync_url}?tab=modules#librenms-module-table")


class InstallBranchView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """Install a module and all its installable descendants from LibreNMS inventory."""

    def post(self, request, pk):
        from dcim.models import Device, Module, ModuleBay, ModuleType

        self.required_object_permissions = {"POST": [("add", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        page_device = get_object_or_404(Device, pk=pk)
        target_device = _resolve_target_device(page_device, request.POST.get("selected_device_id"))
        parent_index = request.POST.get("parent_index")
        server_key = request.POST.get("server_key") or self.librenms_api.server_key
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        if not parent_index:
            messages.error(request, "Missing parent inventory index.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        try:
            parent_index = int(parent_index)
        except ValueError:
            messages.error(request, "Invalid parent inventory index.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Get cached inventory data
        sync_device = _get_sync_device_for_inventory(target_device, server_key)
        cached_payload = cache.get(self.get_cache_key(sync_device, "inventory", server_key=server_key))
        cached_data = _extract_inventory_list(cached_payload)
        if not cached_data:
            messages.error(request, "No cached inventory data. Please refresh modules first.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Load ignore rules so the branch respects the same filters shown in the table
        from netbox_librenms_plugin.utils import get_enabled_ignore_rules

        ignore_rules = get_enabled_ignore_rules()
        device_serial = (getattr(target_device, "serial", None) or "").strip()

        # Build index map and collect the branch to install
        index_map = {idx: item for item in cached_data if (idx := item.get("entPhysicalIndex")) is not None}
        branch_items = self._collect_branch(parent_index, cached_data, ignore_rules, device_serial, index_map)

        if not branch_items:
            messages.warning(request, "No installable items found in this branch.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Load module types (with mappings)
        module_types = get_module_types_indexed()

        # Preload all ModuleBayMappings once to avoid N+1 per-item queries.
        # Filter by device manufacturer so vendor-scoped mappings only apply to
        # matching vendors and mismatched ones are skipped.
        from netbox_librenms_plugin.utils import load_bay_mappings
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        exact_mappings, regex_mappings = load_bay_mappings()
        mfr_id = getattr(getattr(target_device, "device_type", None), "manufacturer_id", None)
        exact_mappings = BaseModuleTableView._filter_mappings_by_manufacturer(exact_mappings, mfr_id)
        regex_mappings = BaseModuleTableView._filter_mappings_by_manufacturer(regex_mappings, mfr_id)

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
                        ModuleBay,
                        ModuleType,
                        Module,
                        exact_mappings=exact_mappings,
                        regex_mappings=regex_mappings,
                    )
                    if result["status"] == "installed":
                        installed.append(result["name"])
                    elif result["status"] == "skipped":
                        skipped.append(f"{result['name']}: {result['reason']}")
                    else:
                        failed.append(f"{result['name']}: {result['reason']}")
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Branch install failed: {e}")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        _report_install_results(request, installed, skipped, failed)
        return redirect(f"{sync_url}?tab=modules#librenms-module-table")

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
        ModuleBay,
        ModuleType,
        Module,
        exact_mappings=None,
        regex_mappings=None,
    ):
        """
        Try to install a single inventory item.

        Re-fetches module bays each time since parent installs create new ones.
        Scopes bay lookup to the correct parent module to handle duplicate bay names.
        """
        from netbox_librenms_plugin.utils import resolve_module_type

        model_name = (item.get("entPhysicalModelName") or "").strip()
        serial = (item.get("entPhysicalSerialNum") or "").strip()
        if serial.lower() in _PLACEHOLDER_VALUES:
            serial = ""
        name = item.get("entPhysicalName", "") or model_name

        # Match module type (direct, then normalization fallback)
        manufacturer = getattr(getattr(device, "device_type", None), "manufacturer", None)
        matched_type = resolve_module_type(model_name, module_types, manufacturer=manufacturer)
        if not matched_type:
            return {"status": "skipped", "name": name, "reason": "no matching type"}

        # Re-fetch module bays (parent install creates new child bays)
        bays = ModuleBay.objects.filter(device=device).select_related("installed_module__module_type")

        # Use preloaded mappings if provided, otherwise load from DB
        if exact_mappings is None or regex_mappings is None:
            from netbox_librenms_plugin.utils import load_bay_mappings

            exact_mappings, regex_mappings = load_bay_mappings()

        # Determine if this item belongs under an installed module
        # by tracing its LibreNMS parent hierarchy to an installed item
        parent_module_id = InstallBranchView._find_parent_module_id(
            item, index_map, bays, exact_mappings, regex_mappings
        )

        if parent_module_id:
            bay_dict = {bay.name: bay for bay in bays if bay.module_id == parent_module_id}
        else:
            bay_dict = {bay.name: bay for bay in bays if not bay.module_id}

        # Match module bay using preloaded mapping data
        matched_bay = InstallBranchView._match_bay(item, index_map, bay_dict, exact_mappings, regex_mappings)
        if not matched_bay:
            return {"status": "skipped", "name": name, "reason": "no matching bay"}

        # Install (lock bay to prevent concurrent installs)
        try:
            with transaction.atomic():  # savepoint: failure here won't abort parent tx
                locked_bay = (
                    ModuleBay.objects.select_for_update(of=("self",))
                    .select_related("installed_module")
                    .get(pk=matched_bay.pk)
                )
                if hasattr(locked_bay, "installed_module") and locked_bay.installed_module:
                    return {"status": "skipped", "name": name, "reason": "bay already occupied"}

                module = Module(
                    device=device,
                    module_bay=locked_bay,
                    module_type=matched_type,
                    serial=serial,
                    status="active",
                )
                module.full_clean()
                module.save()
        except (ValidationError, IntegrityError) as e:
            error_msg = str(e)
            if "dcim_interface_unique_device_name" in error_msg:
                error_msg = (
                    "duplicate interface name — this module type's interface template "
                    "uses the '{module}' token which resolves to the same name for all siblings. "
                    "An interface naming plugin with a rewrite rule for this module type can fix this."
                )
            return {"status": "failed", "name": name, "reason": error_msg}

        return {"status": "installed", "name": f"{matched_type.model} → {matched_bay.name}"}

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
    def _match_bay(item, index_map, module_bays, exact_mappings, regex_mappings):
        """Match an inventory item to a module bay (same logic as BaseModuleTableView)."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        # Resolve parent name by walking up the containment hierarchy
        contained_in = item.get("entPhysicalContainedIn", 0)
        parent_name = None
        if contained_in:
            visited_anc = set()
            current_idx = contained_in
            while current_idx and current_idx not in visited_anc:
                visited_anc.add(current_idx)
                ancestor = index_map.get(current_idx)
                if not ancestor:
                    break
                ancestor_name = ancestor.get("entPhysicalName", "")
                if ancestor_name:
                    parent_name = ancestor_name
                    break
                current_idx = ancestor.get("entPhysicalContainedIn", 0)

        item_name = item.get("entPhysicalName", "")
        item_descr = item.get("entPhysicalDescr", "")
        phys_class = item.get("entPhysicalClass", "")

        # Build candidate names: parent, item name, item description
        candidate_names = [n for n in [parent_name, item_name, item_descr] if n]

        # Check mapping for each candidate (exact match)
        for name in candidate_names:
            bay = BaseModuleTableView._lookup_exact_bay_mapping(name, phys_class, module_bays, exact_mappings)
            if bay:
                return bay

        # Regex pattern matching using preloaded list
        for name in candidate_names:
            bay = BaseModuleTableView._lookup_regex_bay_mapping(name, phys_class, module_bays, regex_mappings)
            if bay:
                return bay

        # Fallback: exact match on candidate names against bay dict
        for name in candidate_names:
            if name in module_bays:
                return module_bays[name]

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
        from dcim.models import Device, Module, ModuleBay, ModuleType

        self.required_object_permissions = {"POST": [("add", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        page_device = get_object_or_404(Device, pk=pk)
        server_key = request.POST.get("server_key") or self.librenms_api.server_key
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        selected_indices = request.POST.getlist("select")
        if not selected_indices:
            messages.warning(request, "No modules selected.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        sync_device = _get_sync_device_for_inventory(page_device, server_key)
        cached_payload = cache.get(self.get_cache_key(sync_device, "inventory", server_key=server_key))
        cached_data = _extract_inventory_list(cached_payload)
        if not cached_data:
            messages.error(request, "No cached inventory data. Please refresh modules first.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        try:
            # Use dict.fromkeys to preserve order while deduplicating
            selected_list = list(dict.fromkeys(int(i) for i in selected_indices))
        except ValueError:
            messages.error(request, "Invalid selection.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        index_map = {idx: item for item in cached_data if (idx := item.get("entPhysicalIndex")) is not None}
        items = [index_map[idx] for idx in selected_list if idx in index_map]

        if not items:
            messages.warning(request, "None of the selected indices matched cached inventory.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

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
        all_exact, all_regex = load_bay_mappings()

        installed, skipped, failed = [], [], []

        try:
            with transaction.atomic():
                for item in items:
                    ent_index = item.get("entPhysicalIndex")
                    selected_device_id = request.POST.get(f"device_selection_{ent_index}")
                    target_device = _resolve_target_device(page_device, selected_device_id)
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
                        ModuleBay,
                        ModuleType,
                        Module,
                        exact_mappings=exact_mappings,
                        regex_mappings=regex_mappings,
                    )
                    if result["status"] == "installed":
                        installed.append(result["name"])
                    elif result["status"] == "skipped":
                        skipped.append(f"{result['name']}: {result['reason']}")
                    else:
                        failed.append(f"{result['name']}: {result['reason']}")
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Install failed: {e}")
            return _modules_redirect_response(request, sync_url)

        _report_install_results(request, installed, skipped, failed)
        return _modules_redirect_response(request, sync_url)


class UpdateModuleSerialView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Update the serial number of an already-installed module from LibreNMS inventory data."""

    def post(self, request, pk):
        from dcim.models import Device, Module

        self.required_object_permissions = {"POST": [("change", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        page_device = get_object_or_404(Device, pk=pk)
        target_device = _resolve_target_device(page_device, request.POST.get("selected_device_id"))
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
                    Module.objects.select_for_update()
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

        page_device = get_object_or_404(Device, pk=pk)
        target_device = _resolve_target_device(page_device, request.GET.get("selected_device_id"))
        server_key = request.GET.get("server_key") or self.librenms_api.server_key

        try:
            module_id = int(request.GET.get("module_id"))
            ent_index_int = int(request.GET.get("ent_index"))
        except (TypeError, ValueError):
            return HttpResponse("Missing or invalid module_id/ent_index.", status=400)

        installed_module = get_object_or_404(
            Module.objects.select_related("module_type", "module_bay", "device"),
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
        installed_serial = (installed_module.serial or "").strip()
        if installed_serial.lower() in _PLACEHOLDER_VALUES:
            installed_serial = ""
        serial_mismatch = bool(
            not type_mismatch and librenms_serial != installed_serial and (librenms_serial or installed_serial)
        )

        # Check whether the LibreNMS serial already exists at a different location
        serial_conflict = None
        serial_conflict_ambiguous = False
        if librenms_serial:
            conflict_qs = (
                Module.objects.filter(serial=librenms_serial)
                .exclude(pk=installed_module.pk)
                .select_related("module_type", "module_bay", "device")
            )
            conflict_count = conflict_qs.count()
            if conflict_count == 1:
                serial_conflict = conflict_qs.first()
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
                "serial_mismatch": serial_mismatch,
                "serial_conflict": serial_conflict,
                "serial_conflict_ambiguous": serial_conflict_ambiguous,
                "ent_index": ent_index_int,
                "server_key": server_key or "",
                "selected_device_id": target_device.pk,
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
        from dcim.models import Device, Module, ModuleBay, ModuleType  # noqa: F401

        self.required_object_permissions = {"POST": [("add", Module), ("change", Module), ("delete", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        page_device = get_object_or_404(Device, pk=pk)
        target_device = _resolve_target_device(page_device, request.POST.get("selected_device_id"))
        server_key = request.POST.get("server_key") or self.librenms_api.server_key
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        try:
            module_id = int(request.POST.get("module_id"))
            ent_index_int = int(request.POST.get("ent_index"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module_id/ent_index.")
            return _modules_redirect_response(request, sync_url)

        installed_module = get_object_or_404(
            Module.objects.select_related("module_type", "module_bay"),
            pk=module_id,
            device=target_device,
        )

        sync_device = _get_sync_device_for_inventory(target_device, server_key)
        cached_payload = cache.get(self.get_cache_key(sync_device, "inventory", server_key=server_key))
        cached_data = _extract_inventory_list(cached_payload)
        if not cached_data:
            messages.error(request, "No cached inventory data. Please refresh modules first.")
            return _modules_redirect_response(request, sync_url)

        librenms_item = next(
            (item for item in cached_data if item.get("entPhysicalIndex") == ent_index_int),
            None,
        )
        if not librenms_item:
            messages.error(request, "Inventory item not found in cache.")
            return _modules_redirect_response(request, sync_url)

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
            return _modules_redirect_response(request, sync_url)

        try:
            conflict_removed_msg = None
            with transaction.atomic():
                # Re-fetch with row lock to prevent concurrent modifications
                installed_module = (
                    Module.objects.select_for_update()
                    .filter(pk=module_id, device=target_device)
                    .select_related("module_type", "module_bay")
                    .first()
                )
                if not installed_module:
                    messages.error(request, "Module no longer exists.")
                    return _modules_redirect_response(request, sync_url)

                # Read bay/type from locked row to avoid stale snapshot
                target_bay = installed_module.module_bay
                old_type_name = installed_module.module_type.model
                old_bay_name = target_bay.name

                # Re-derive any serial conflict from the database INSIDE the locked
                # transaction (and lock those rows too) — checking before the lock
                # opens a TOCTOU window where a concurrent request could change a
                # module's serial and we'd then delete a row that no longer
                # conflicts.  Re-querying under select_for_update() guarantees the
                # set we delete from is the same set we validated.
                conflict_module = None
                if serial:
                    conflict_qs = (
                        Module.objects.select_for_update()
                        .filter(serial=serial)
                        .exclude(pk=installed_module.pk)
                        .select_related("module_type", "module_bay", "device")
                    )
                    locked_conflicts = list(conflict_qs)
                    if len(locked_conflicts) > 1:
                        # Roll back and surface a clear error — we don't want to
                        # guess which of N conflicts to remove.
                        raise _SerialConflictAmbiguous(serial)
                    if len(locked_conflicts) == 1:
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
                new_module = Module(
                    device=target_device,
                    module_bay=target_bay,
                    module_type=matched_type,
                    serial=serial,
                    status="active",
                )
                new_module.full_clean()
                new_module.save()

            if conflict_removed_msg:
                messages.info(request, conflict_removed_msg)
            messages.success(
                request,
                f"Replaced {old_type_name} with {matched_type.model} in {old_bay_name}"
                + (f" (serial: {serial})" if serial else "")
                + ".",
            )
        except _SerialConflictAmbiguous as exc:
            messages.error(
                request,
                f"Serial '{exc.serial}' is assigned to multiple modules; cannot determine which to remove. "
                "Please resolve the conflict manually.",
            )
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Replace failed: {e}")

        return _modules_redirect_response(request, sync_url)


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

        self.required_object_permissions = {"POST": [("change", Module), ("delete", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        page_device = get_object_or_404(Device, pk=pk)
        target_device = _resolve_target_device(page_device, request.POST.get("selected_device_id"))
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

        get_object_or_404(ModuleBay, pk=target_bay_id, device=target_device)

        try:
            occupant_removed_msg = None
            with transaction.atomic():
                # Lock target bay to prevent concurrent modifications
                target_bay = ModuleBay.objects.select_for_update().get(pk=target_bay_id, device=target_device)

                # Re-fetch with row lock to prevent concurrent modifications
                conflict_module = (
                    Module.objects.select_for_update()
                    .filter(pk=conflict_module_id)
                    .select_related("module_type", "module_bay", "device")
                    .first()
                )
                if not conflict_module:
                    messages.error(request, "Module no longer exists.")
                    return _modules_redirect_response(request, sync_url)

                # Remove whatever is currently in the target bay (if provided and different)
                if module_id:
                    occupant = (
                        Module.objects.select_for_update()
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
            return get_object_or_404(DeviceType, pk=target_pk)
        if target_kind == "module_type":
            return get_object_or_404(ModuleType, pk=target_pk)
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
        from dcim.models import Device, ModuleBay, ModuleBayTemplate

        # Read-only modal render — only require plugin view permission and
        # NetBox add-permission on ModuleBayTemplate so users without it never
        # see a form they cannot submit. POST also instantiates live ModuleBay
        # rows via _instantiate_template_on_existing(), so require add_modulebay
        # here too to keep the GET/POST permission contract aligned.
        self.required_object_permissions = {"GET": [("add", ModuleBayTemplate), ("add", ModuleBay)]}
        if error := self.require_all_permissions("GET"):
            return error

        device = get_object_or_404(Device, pk=pk)

        target_kind = request.GET.get("target_kind", "")
        if target_kind not in self.TARGET_KINDS:
            return HttpResponse("Invalid target_kind.", status=400)
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
        from dcim.models import Device, ModuleBay, ModuleBayTemplate

        from netbox_librenms_plugin.models import ModuleBayMapping

        # POST creates the template AND instantiates live ModuleBay rows on
        # existing devices/modules via _instantiate_template_on_existing(), so
        # gate on add_modulebay in addition to add_modulebaytemplate.
        self.required_object_permissions = {"POST": [("add", ModuleBayTemplate), ("add", ModuleBay)]}
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        target_kind = request.POST.get("target_kind", "")
        if target_kind not in self.TARGET_KINDS:
            messages.error(request, "Invalid target_kind for bay template.")
            return _modules_redirect_response(request, sync_url)
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
