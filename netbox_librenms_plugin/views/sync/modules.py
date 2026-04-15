"""Sync action views for module/inventory installation from LibreNMS."""

import re

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from netbox_librenms_plugin.utils import get_module_types_indexed
from netbox_librenms_plugin.views.base.modules_view import _PLACEHOLDER_VALUES
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)


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


class InstallModuleView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """Install a NetBox Module into a ModuleBay from LibreNMS inventory data."""

    def post(self, request, pk):
        from dcim.models import Device, Module, ModuleBay, ModuleType

        self.required_object_permissions = {"POST": [("add", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
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

        get_object_or_404(ModuleBay, pk=module_bay_id, device=device)  # verify bay belongs to device
        module_type = get_object_or_404(ModuleType, pk=module_type_id)

        try:
            with transaction.atomic():
                # Re-fetch bay under lock to prevent TOCTOU race with concurrent installs.
                locked_bay = ModuleBay.objects.select_for_update().get(pk=module_bay_id)
                if hasattr(locked_bay, "installed_module") and locked_bay.installed_module:
                    messages.warning(request, f"Module bay '{locked_bay.name}' already has a module installed.")
                    return redirect(f"{sync_url}?tab=modules#librenms-module-table")
                module = Module(
                    device=device,
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

        device = get_object_or_404(Device, pk=pk)
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
        cached_payload = cache.get(self.get_cache_key(device, "inventory", server_key=server_key))
        cached_data = _extract_inventory_list(cached_payload)
        if not cached_data:
            messages.error(request, "No cached inventory data. Please refresh modules first.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Load ignore rules so the branch respects the same filters shown in the table
        from netbox_librenms_plugin.utils import get_enabled_ignore_rules

        ignore_rules = get_enabled_ignore_rules()
        device_serial = (getattr(device, "serial", None) or "").strip()

        # Build index map and collect the branch to install
        index_map = {idx: item for item in cached_data if (idx := item.get("entPhysicalIndex")) is not None}
        branch_items = self._collect_branch(parent_index, cached_data, ignore_rules, device_serial, index_map)

        if not branch_items:
            messages.warning(request, "No installable items found in this branch.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Load module types (with mappings)
        module_types = get_module_types_indexed()

        # Preload all ModuleBayMappings once to avoid N+1 per-item queries
        from netbox_librenms_plugin.utils import load_bay_mappings

        exact_mappings, regex_mappings = load_bay_mappings()

        # Install top-down: each install may create new child bays
        installed = []
        skipped = []
        failed = []

        try:
            with transaction.atomic():
                for item in branch_items:
                    result = self._install_single(
                        device,
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
                    ModuleBay.objects.select_for_update().select_related("installed_module").get(pk=matched_bay.pk)
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
                    try:
                        if re.search(rm.librenms_name, name):
                            candidates = bay_by_name.get(rm.netbox_bay_name, [])
                            if len(candidates) == 1:
                                bay = candidates[0]
                            else:
                                occupied = [
                                    b for b in candidates if hasattr(b, "installed_module") and b.installed_module
                                ]
                                bay = occupied[0] if len(occupied) == 1 else None
                            if bay and hasattr(bay, "installed_module") and bay.installed_module:
                                return bay.installed_module.pk
                    except re.error:
                        continue

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

        device = get_object_or_404(Device, pk=pk)
        server_key = request.POST.get("server_key") or self.librenms_api.server_key
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        selected_indices = request.POST.getlist("select")
        if not selected_indices:
            messages.warning(request, "No modules selected.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        cached_payload = cache.get(self.get_cache_key(device, "inventory", server_key=server_key))
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

        # Filter out items matching 'skip' ignore rules (consistent with what the table shows)
        from netbox_librenms_plugin.utils import get_enabled_ignore_rules
        from netbox_librenms_plugin.views.base.modules_view import _check_ignore_rules

        ignore_rules = get_enabled_ignore_rules()
        device_serial = (getattr(device, "serial", None) or "").strip()
        if ignore_rules:
            items = [
                item
                for item in items
                if _check_ignore_rules(
                    item,
                    index_map.get(item.get("entPhysicalContainedIn")),
                    ignore_rules,
                    index_map,
                    device_serial,
                )
                not in {"skip", "transparent"}
            ]

        # Preload all ModuleBayMappings once to avoid N+1 per-item queries
        from netbox_librenms_plugin.utils import load_bay_mappings

        module_types = get_module_types_indexed()
        exact_mappings, regex_mappings = load_bay_mappings()

        installed, skipped, failed = [], [], []

        try:
            with transaction.atomic():
                for item in items:
                    result = InstallBranchView._install_single(
                        device,
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
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        _report_install_results(request, installed, skipped, failed)
        return redirect(f"{sync_url}?tab=modules#librenms-module-table")


class UpdateModuleSerialView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Update the serial number of an already-installed module from LibreNMS inventory data."""

    def post(self, request, pk):
        from dcim.models import Device, Module

        self.required_object_permissions = {"POST": [("change", Module)]}
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        serial = request.POST.get("serial", "").strip()
        if serial.lower() in _PLACEHOLDER_VALUES:
            serial = ""
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        try:
            module_id = int(request.POST.get("module_id"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module ID.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        module = get_object_or_404(Module, pk=module_id, device=device)

        try:
            with transaction.atomic():
                module.serial = serial
                module.full_clean()
                module.save()
            messages.success(
                request,
                f"Updated serial for {module.module_type.model} in {module.module_bay.name} to '{serial}'.",
            )
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Failed to update serial: {e}")

        return redirect(f"{sync_url}?tab=modules#librenms-module-table")


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

        device = get_object_or_404(Device, pk=pk)
        server_key = request.GET.get("server_key") or self.librenms_api.server_key

        try:
            module_id = int(request.GET.get("module_id"))
            ent_index_int = int(request.GET.get("ent_index"))
        except (TypeError, ValueError):
            return HttpResponse("Missing or invalid module_id/ent_index.", status=400)

        installed_module = get_object_or_404(
            Module.objects.select_related("module_type", "module_bay", "device"),
            pk=module_id,
            device=device,
        )

        cached_payload = cache.get(self.get_cache_key(device, "inventory", server_key=server_key))
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
        manufacturer = getattr(getattr(device, "device_type", None), "manufacturer", None)
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

        device = get_object_or_404(Device, pk=pk)
        server_key = request.POST.get("server_key") or self.librenms_api.server_key
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        try:
            module_id = int(request.POST.get("module_id"))
            ent_index_int = int(request.POST.get("ent_index"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid module_id/ent_index.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        installed_module = get_object_or_404(
            Module.objects.select_related("module_type", "module_bay"),
            pk=module_id,
            device=device,
        )

        cached_payload = cache.get(self.get_cache_key(device, "inventory", server_key=server_key))
        cached_data = _extract_inventory_list(cached_payload)
        if not cached_data:
            messages.error(request, "No cached inventory data. Please refresh modules first.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        librenms_item = next(
            (item for item in cached_data if item.get("entPhysicalIndex") == ent_index_int),
            None,
        )
        if not librenms_item:
            messages.error(request, "Inventory item not found in cache.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        model_name = (librenms_item.get("entPhysicalModelName") or "").strip()
        serial = (librenms_item.get("entPhysicalSerialNum") or "").strip()
        if serial.lower() in _PLACEHOLDER_VALUES:
            serial = ""

        # Re-derive any serial conflict from the database rather than trusting
        # a client-submitted conflict_module_id POST parameter.
        conflict_module = None
        if serial:
            conflict_qs = (
                Module.objects.filter(serial=serial)
                .exclude(pk=installed_module.pk)
                .select_related("module_type", "module_bay", "device")
            )
            conflict_count = conflict_qs.count()
            if conflict_count > 1:
                messages.error(
                    request,
                    f"Serial '{serial}' is assigned to multiple modules; cannot determine which to remove. "
                    "Please resolve the conflict manually.",
                )
                return redirect(f"{sync_url}?tab=modules#librenms-module-table")
            if conflict_count == 1:
                conflict_module = conflict_qs.first()

        module_types = get_module_types_indexed()
        from netbox_librenms_plugin.utils import resolve_module_type

        manufacturer = getattr(getattr(device, "device_type", None), "manufacturer", None)
        matched_type = resolve_module_type(model_name, module_types, manufacturer=manufacturer)

        if not matched_type:
            messages.error(request, f"No matching module type found for '{model_name}'.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        try:
            conflict_removed_msg = None
            with transaction.atomic():
                # Re-fetch with row lock to prevent concurrent modifications
                installed_module = (
                    Module.objects.select_for_update()
                    .filter(pk=module_id, device=device)
                    .select_related("module_type", "module_bay")
                    .first()
                )
                if not installed_module:
                    messages.error(request, "Module no longer exists.")
                    return redirect(f"{sync_url}?tab=modules#librenms-module-table")

                # Read bay/type from locked row to avoid stale snapshot
                target_bay = installed_module.module_bay
                old_type_name = installed_module.module_type.model
                old_bay_name = target_bay.name

                # Remove the serial-conflicting module from its current location (re-derived,
                # not trusted from a client-submitted conflict_module_id field).
                if conflict_module:
                    conflict_module = (
                        Module.objects.select_for_update()
                        .filter(pk=conflict_module.pk)
                        .select_related("module_type", "module_bay", "device")
                        .first()
                    )
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
                    device=device,
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
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Replace failed: {e}")

        return redirect(f"{sync_url}?tab=modules#librenms-module-table")


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

        device = get_object_or_404(Device, pk=pk)
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})

        try:
            conflict_module_id = int(request.POST.get("conflict_module_id"))
            target_bay_id = int(request.POST.get("target_bay_id"))
        except (TypeError, ValueError):
            messages.error(request, "Missing or invalid conflict_module_id/target_bay_id.")
            return redirect(f"{sync_url}?tab=modules#librenms-module-table")

        # Optional: current occupant of target bay
        raw_module_id = request.POST.get("module_id")
        try:
            module_id = int(raw_module_id) if raw_module_id else None
        except (TypeError, ValueError):
            module_id = None

        get_object_or_404(ModuleBay, pk=target_bay_id, device=device)

        try:
            occupant_removed_msg = None
            with transaction.atomic():
                # Lock target bay to prevent concurrent modifications
                target_bay = ModuleBay.objects.select_for_update().get(pk=target_bay_id, device=device)

                # Re-fetch with row lock to prevent concurrent modifications
                conflict_module = (
                    Module.objects.select_for_update()
                    .filter(pk=conflict_module_id)
                    .select_related("module_type", "module_bay", "device")
                    .first()
                )
                if not conflict_module:
                    messages.error(request, "Module no longer exists.")
                    return redirect(f"{sync_url}?tab=modules#librenms-module-table")

                # Remove whatever is currently in the target bay (if provided and different)
                if module_id:
                    occupant = (
                        Module.objects.select_for_update()
                        .filter(pk=module_id, device=device, module_bay=target_bay)
                        .first()
                    )
                    if occupant and occupant.pk != conflict_module.pk:
                        occupant_removed_msg = f"Removed {occupant.module_type.model} from {target_bay.name}."
                        occupant.delete()

                # Move the conflict module to the target bay
                from_bay = conflict_module.module_bay.name
                from_device = conflict_module.device.name
                conflict_module.module_bay = target_bay
                conflict_module.device = device
                conflict_module.full_clean()
                conflict_module.save()

            if occupant_removed_msg:
                messages.info(request, occupant_removed_msg)
            moved_msg = f"Moved {conflict_module.module_type.model}"
            if from_device != device.name:
                moved_msg += f" from {from_device}"
            moved_msg += f"/{from_bay} to {target_bay.name}."
            messages.success(request, moved_msg)
        except (ValidationError, IntegrityError) as e:
            messages.error(request, f"Move failed: {e}")

        return redirect(f"{sync_url}?tab=modules#librenms-module-table")
