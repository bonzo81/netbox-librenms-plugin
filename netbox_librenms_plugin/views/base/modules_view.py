import re
from collections import ChainMap

from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
)


# entPhysicalClass values relevant for module sync
# Includes vendor-specific classes (Nokia TIMETRA-CHASSIS-MIB uses ioModule, cpmModule, etc.)
INVENTORY_CLASSES = {
    "module",
    "powerSupply",
    "fan",
    "port",
    "container",
    "ioModule",
    "cpmModule",
    "mdaModule",
    "fabricModule",
    "xioModule",
}

# Model name values that indicate a generic/empty container (not real hardware)
_GENERIC_CONTAINER_MODELS = {"", "BUILTIN", "Default", "N/A"}

# Lowercase placeholder values that LibreNMS returns for absent model/serial fields.
# Used during transceiver backfill to decide whether existing ENTITY-MIB data should
# be replaced by richer transceiver API data.
_PLACEHOLDER_VALUES = {"", "n/a", "na", "default", "-", "unknown"}

# Transceiver entry types that are containers, not real modules.
_SKIP_TRANSCEIVER_TYPES = {"Port Container", "Port", ""}

# Physical classes filtered out when counting hardware siblings under a parent bay.
_NON_HARDWARE_CLASSES = {"sensor", "backplane", "stack"}


def _check_ignore_rules(
    item: dict,
    parent_item: dict | None,
    rules: list,
    index_map: dict | None = None,
    device_serial: str = "",
) -> str | None:
    """
    Return the matched rule action or ``None`` if no rule matches.

    Return values:
        ``None``          — no rule matched; process the item normally.
        ``"skip"``        — drop the item from the sync table.
        ``"transparent"`` — hide the item's row but promote its ENTITY-MIB
                            children to device-level bay matching (used for
                            embedded RPs on fixed-chassis routers).

    Match logic per rule type:

    **serial_matches_device**
        Matches when the item's ``entPhysicalSerialNum`` equals *device_serial*
        (the NetBox ``Device.serial`` value).  No name pattern is used.
        ``require_serial_match_parent`` is ignored for this type.

    **Name-based types** (ends_with / starts_with / contains / regex):
        Matches on ``entPhysicalName``.  When ``require_serial_match_parent``
        is True the item is only matched if its serial number is non-empty and
        equals **any ancestor's** serial in the ENTITY-MIB hierarchy (walking
        up from the direct parent).

        Ancestor walking handles cases like Cisco IOS-XR where an IDPROM entry
        is not a direct child of the module it represents — e.g.
        ``0/RP0/CPU0-Base Board IDPROM`` is a child of ``0/RP0/CPU0-Mother Board``
        (empty serial), but its serial matches the grandparent ``0/RP0/CPU0``.
        Traversal stops at the first non-empty serial encountered to avoid false
        positives deeper in the tree.
    """
    item_serial = (item.get("entPhysicalSerialNum") or "").strip()
    if item_serial.lower() in _PLACEHOLDER_VALUES:
        item_serial = ""
    if device_serial.lower() in _PLACEHOLDER_VALUES:
        device_serial = ""
    name = (item.get("entPhysicalName") or "").strip()

    for rule in rules:
        # --- serial_matches_device: no name match, just compare serials ---
        if rule.match_type == "serial_matches_device":
            if item_serial and device_serial and item_serial == device_serial:
                return rule.action
            continue

        # --- name-based rules ---
        if not rule.matches_name(name):
            continue
        if not rule.require_serial_match_parent:
            return rule.action
        if parent_item is None:
            # Can't satisfy serial check without a parent — skip conservatively.
            continue
        if not item_serial:
            continue
        # Walk up ancestors until a non-empty serial is found.
        current = parent_item
        visited: set = set()
        while current is not None:
            current_idx = current.get("entPhysicalIndex")
            if current_idx is not None:
                if current_idx in visited:
                    break
                visited.add(current_idx)
            ancestor_serial = (current.get("entPhysicalSerialNum") or "").strip()
            if ancestor_serial.lower() in _PLACEHOLDER_VALUES:
                ancestor_serial = ""
            if ancestor_serial:
                if ancestor_serial == item_serial:
                    return rule.action
                # Non-empty serial that doesn't match — stop looking further up.
                break
            if index_map is not None:
                next_idx = current.get("entPhysicalContainedIn")
                current = index_map.get(next_idx) if next_idx else None
            else:
                break
    return None


class BaseModuleTableView(LibreNMSPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """
    Base view for synchronizing module/inventory data from LibreNMS.
    Fetches inventory, matches against NetBox module bays and module types,
    and renders a comparison table.
    """

    model = None
    partial_template_name = "netbox_librenms_plugin/_module_sync_content.html"

    def get_object(self, pk):
        """Retrieve the object (Device)."""
        return get_object_or_404(self.model, pk=pk)

    def get_table(self, data, obj):
        """Returns the table class. Subclasses should override."""
        raise NotImplementedError("Subclasses must implement get_table()")

    def post(self, request, pk):
        """Fetch inventory from LibreNMS, cache it, and render the module sync table."""
        obj = self.get_object(pk)

        self.librenms_id = self.librenms_api.get_librenms_id(obj)
        if not self.librenms_id:
            cache.delete(self.get_cache_key(obj, "inventory", server_key=self.librenms_api.server_key))
            messages.error(request, "Device not found in LibreNMS.")
            return render(
                request,
                self.partial_template_name,
                {
                    "module_sync": {
                        "object": obj,
                        "table": None,
                        "cache_expiry": None,
                        "server_key": self.librenms_api.server_key,
                    },
                    "has_write_permission": self.has_write_permission(),
                },
            )

        success, inventory_data = self.librenms_api.get_device_inventory(self.librenms_id)

        if not success:
            cache.delete(self.get_cache_key(obj, "inventory", server_key=self.librenms_api.server_key))
            messages.error(request, f"Failed to fetch inventory from LibreNMS: {inventory_data}")
            return render(
                request,
                self.partial_template_name,
                {
                    "module_sync": {
                        "object": obj,
                        "table": None,
                        "cache_expiry": None,
                        "server_key": self.librenms_api.server_key,
                    },
                    "has_write_permission": self.has_write_permission(),
                },
            )

        # Fetch transceiver data and merge with inventory
        inventory_data, txr_error = self._merge_transceiver_data(inventory_data)

        # Cache the merged inventory data, namespaced by server to avoid cross-server collisions
        cache.set(
            self.get_cache_key(obj, "inventory", server_key=self.librenms_api.server_key),
            inventory_data,
            timeout=self.librenms_api.cache_timeout,
        )

        context = self._build_context(request, obj, inventory_data)
        if txr_error:
            messages.warning(request, f"Inventory refreshed, but transceiver fetch failed: {txr_error}")
        else:
            messages.success(request, "Inventory data refreshed successfully.")
        return render(
            request,
            self.partial_template_name,
            {"module_sync": context, "has_write_permission": self.has_write_permission()},
        )

    def get_context_data(self, request, obj):
        """Get context from cache (used by the main sync view on initial page load)."""
        cached_data = cache.get(self.get_cache_key(obj, "inventory", server_key=self.librenms_api.server_key))
        if cached_data is None:
            return {"table": None, "object": obj, "cache_expiry": None, "server_key": self.librenms_api.server_key}
        return self._build_context(request, obj, cached_data)

    def _build_context(self, request, obj, inventory_data):
        """Build context with matched inventory items and table."""
        # Build a lookup of all inventory items by index for parent resolution
        # Skip items with missing entPhysicalIndex to avoid KeyError on malformed data.
        index_map = {idx: item for item in inventory_data if (idx := item.get("entPhysicalIndex")) is not None}

        # Precompute parent→children map once so _get_sub_components runs in O(n) total.
        children_by_parent: dict = {}
        for item in inventory_data:
            p = item.get("entPhysicalContainedIn")
            if p is not None:
                children_by_parent.setdefault(p, []).append(item)

        # Preload all ModuleBayMapping rows once to avoid N+1 queries in _match_module_bay.
        from netbox_librenms_plugin.utils import (
            get_enabled_ignore_rules,
            load_bay_mappings,
            preload_normalization_rules,
        )

        self._exact_bay_mappings, self._regex_bay_mappings = load_bay_mappings()

        # Load enabled ignore rules once; passed to _check_ignore_rules throughout.
        ignore_rules = get_enabled_ignore_rules()

        # Device serial for serial_matches_device rules (strip whitespace defensively).
        device_serial = (getattr(obj, "serial", None) or "").strip()

        # Manufacturer for module-type normalization rules — passed explicitly to
        # _build_table_rows/_build_row instead of stored as an instance attribute.
        manufacturer = getattr(getattr(obj, "device_type", None), "manufacturer", None)

        # Preload NormalizationRule rows once to avoid N+1 queries inside the
        # _match_module_bay and resolve_module_type loops.
        self._norm_rules_bay = preload_normalization_rules("module_bay")
        self._norm_rules_type = preload_normalization_rules("module_type", manufacturer=manufacturer)

        # Pre-compute ignore rule results once to avoid calling _check_ignore_rules
        # twice per item (once in _find_transparent_indices, once in _collect_top_items).
        ignore_cache = {
            item["entPhysicalIndex"]: _check_ignore_rules(
                item,
                index_map.get(item.get("entPhysicalContainedIn")),
                ignore_rules,
                index_map,
                device_serial,
            )
            for item in inventory_data
            if item.get("entPhysicalIndex") is not None
        }

        # Get NetBox module bays and modules for this device
        device_bays, module_scoped_bays = self._get_module_bays(obj)
        module_types = self._get_module_types()

        transparent_indices = self._find_transparent_indices(inventory_data, ignore_cache)
        top_items = self._collect_top_items(
            inventory_data, index_map, ignore_rules, device_serial, transparent_indices, ignore_cache
        )
        table_data = self._build_table_rows(
            top_items,
            index_map,
            children_by_parent,
            ignore_rules,
            device_serial,
            device_bays,
            module_scoped_bays,
            module_types,
            manufacturer=manufacturer,
        )

        # Sort top-level groups by status, keeping children after their parent
        table_data = self._sort_with_hierarchy(table_data)

        # Bulk-detect serial conflicts for rows that can be replaced/installed
        self._detect_serial_conflicts(table_data)

        table = self.get_table(table_data, obj)
        table.configure(request)

        cache_ttl = getattr(cache, "ttl", lambda k: None)(
            self.get_cache_key(obj, "inventory", server_key=self.librenms_api.server_key)
        )
        cache_expiry = (
            timezone.now() + timezone.timedelta(seconds=cache_ttl) if cache_ttl is not None and cache_ttl > 0 else None
        )

        return {
            "table": table,
            "object": obj,
            "cache_expiry": cache_expiry,
            "server_key": self.librenms_api.server_key,
        }

    @staticmethod
    def _find_transparent_indices(inventory_data, ignore_cache):
        """Identify ENTITY-MIB items that should be treated as transparent parents."""
        transparent_indices: set = set()
        for item in inventory_data:
            idx = item.get("entPhysicalIndex")
            if idx is None:
                continue
            if ignore_cache.get(idx) == "transparent":
                transparent_indices.add(idx)
        return transparent_indices

    @staticmethod
    def _collect_top_items(inventory_data, index_map, ignore_rules, device_serial, transparent_indices, ignore_cache):
        """
        Collect top-level inventory items for the sync table.

        Includes synthetic transceiver items. Excludes items that have any
        ancestor with an INVENTORY_CLASSES class (they appear as sub-components).
        """
        top_items = []
        for item in inventory_data:
            if item.get("_from_transceiver_api"):
                idx = item.get("entPhysicalIndex")
                action = (
                    ignore_cache.get(idx)
                    if idx is not None
                    else _check_ignore_rules(
                        item,
                        index_map.get(item.get("entPhysicalContainedIn")),
                        ignore_rules,
                        index_map,
                        device_serial,
                    )
                )
                if action in ("skip", "transparent"):
                    continue
                top_items.append(item)
                continue
            phys_class = item.get("entPhysicalClass")
            if phys_class not in INVENTORY_CLASSES:
                continue
            idx = item.get("entPhysicalIndex")
            action = (
                ignore_cache.get(idx)
                if idx is not None
                else _check_ignore_rules(
                    item,
                    index_map.get(item.get("entPhysicalContainedIn")),
                    ignore_rules,
                    index_map,
                    device_serial,
                )
            )
            if action == "skip":
                continue
            # Transparent items are hidden from the table but must NOT be added as
            # top-level items — their children will appear instead.
            if action == "transparent":
                continue
            # Skip items with generic model names (not real hardware).
            model = (item.get("entPhysicalModelName") or "").strip()
            if phys_class == "container" and model in _GENERIC_CONTAINER_MODELS:
                continue
            if model and model in _GENERIC_CONTAINER_MODELS:
                continue
            # Walk up ancestor chain; skip if any ancestor is an inventory-class item.
            # Transparent ancestors are treated as generic containers.
            is_descendant = False
            current_idx = item.get("entPhysicalContainedIn", 0)
            visited_ancestors = set()
            while current_idx and current_idx in index_map and current_idx not in visited_ancestors:
                visited_ancestors.add(current_idx)
                ancestor = index_map[current_idx]
                if current_idx in transparent_indices:
                    current_idx = ancestor.get("entPhysicalContainedIn", 0)
                    continue
                anc_class = ancestor.get("entPhysicalClass")
                if anc_class in INVENTORY_CLASSES:
                    anc_model = (ancestor.get("entPhysicalModelName") or "").strip()
                    if anc_class == "container" and anc_model in _GENERIC_CONTAINER_MODELS:
                        current_idx = ancestor.get("entPhysicalContainedIn", 0)
                        continue
                    is_descendant = True
                    break
                current_idx = ancestor.get("entPhysicalContainedIn", 0)
            if is_descendant:
                continue
            top_items.append(item)
        return top_items

    def _build_table_rows(
        self,
        top_items,
        index_map,
        children_by_parent,
        ignore_rules,
        device_serial,
        device_bays,
        module_scoped_bays,
        module_types,
        manufacturer=None,
    ):
        """Build table rows from top-level items and their sub-components."""
        # ChainMap preserves scope ordering — device-level bays take precedence.
        all_bays = ChainMap(device_bays, *module_scoped_bays.values())
        # Precompute per-module sibling bay counts to avoid N+1 in has_nested_name_conflict.
        sibling_counts = {mid: len(bays) for mid, bays in module_scoped_bays.items()}
        table_data = []

        for item in top_items:
            item_bays = all_bays if item.get("_from_transceiver_api") else device_bays
            row = self._build_row(
                item,
                index_map,
                item_bays,
                module_types,
                depth=0,
                manufacturer=manufacturer,
                sibling_counts=sibling_counts,
            )
            parent_row_idx = len(table_data)
            table_data.append(row)

            # Determine child bay scope based on parent match state
            parent_module_id = None
            parent_bay_matched_but_uninstalled = False
            if row.get("module_bay_id"):
                matched_bay = item_bays.get(row["module_bay"])
                if matched_bay and hasattr(matched_bay, "installed_module") and matched_bay.installed_module:
                    parent_module_id = matched_bay.installed_module.pk
                else:
                    parent_bay_matched_but_uninstalled = True

            if parent_bay_matched_but_uninstalled:
                child_bays = {}
            elif parent_module_id:
                child_bays = module_scoped_bays.get(parent_module_id, {})
            else:
                child_bays = device_bays

            # Process sub-components with depth-tracked bay scoping
            bays_by_depth = {0: child_bays}
            parent_ent_idx = item.get("entPhysicalIndex")
            if parent_ent_idx is None:
                continue
            sub_items = self._get_sub_components(
                parent_ent_idx, children_by_parent, index_map, ignore_rules, device_serial
            )
            for depth, sub_item in sub_items:
                scope_bays = bays_by_depth.get(depth, child_bays)
                sub_row = self._build_row(
                    sub_item,
                    index_map,
                    scope_bays,
                    module_types,
                    depth=depth,
                    manufacturer=manufacturer,
                    sibling_counts=sibling_counts,
                )
                table_data.append(sub_row)

                # Update bay scope for children of this sub-item.
                if sub_row.get("module_bay_id"):
                    matched_sub_bay = scope_bays.get(sub_row["module_bay"])
                    if (
                        matched_sub_bay
                        and hasattr(matched_sub_bay, "installed_module")
                        and matched_sub_bay.installed_module
                    ):
                        sub_module_id = matched_sub_bay.installed_module.pk
                        bays_by_depth[depth + 1] = module_scoped_bays.get(sub_module_id, {})
                    else:
                        bays_by_depth[depth + 1] = {}
                else:
                    # Preserve parent scope for unmatched intermediate containers
                    bays_by_depth[depth + 1] = scope_bays

                if sub_row.get("can_install"):
                    table_data[parent_row_idx]["has_installable_children"] = True
                # When parent bay is uninstalled, sub-rows have empty bays so
                # can_install is False, but module_type_id is still resolved.
                # Use it to enable "Install Branch" without a second resolve pass.
                elif parent_bay_matched_but_uninstalled and sub_row.get("module_type_id"):
                    table_data[parent_row_idx]["has_installable_children"] = True

        return table_data

    def _merge_transceiver_data(self, inventory_data):
        """
        Merge transceiver API data with entity inventory.

        For vendors like Nokia that don't expose SFPs in ENTITY-MIB,
        the transceiver API provides SFP model, serial, and type info.

        Strategy:
        - For transceivers matching existing inventory items by entity_physical_index:
          supplement entPhysicalModelName if empty
        - For transceivers NOT in inventory: create synthetic inventory items
          so they appear in the modules table

        Returns:
            (inventory_data, error_message) — error_message is None on success
            or a string when the transceiver API call failed.
        """
        success, transceivers = self.librenms_api.get_device_transceivers(self.librenms_id)
        if not success:
            return inventory_data, str(transceivers) if transceivers else "unknown error"
        if not transceivers:
            return inventory_data, None

        # Build lookup of existing inventory items by index and serial
        inv_by_index = {idx: item for item in inventory_data if (idx := item.get("entPhysicalIndex")) is not None}
        inv_serials = {
            s
            for item in inventory_data
            if (s := (item.get("entPhysicalSerialNum") or "").strip()) and s.lower() not in _PLACEHOLDER_VALUES
        }

        # Build port_id → ifName lookup for better synthetic item naming
        port_name_map = self._build_port_name_map(transceivers)

        # Types that are containers, not real transceiver modules

        for txr in transceivers:
            ent_idx = txr.get("entity_physical_index")
            if not ent_idx:
                continue

            model = (txr.get("model") or "").strip()
            if model.lower() in _PLACEHOLDER_VALUES:
                model = ""
            serial = (txr.get("serial") or "").strip()
            if serial.lower() in _PLACEHOLDER_VALUES:
                serial = ""
            txr_type = (txr.get("type") or "").strip()
            if txr_type.lower() in _PLACEHOLDER_VALUES:
                txr_type = ""

            # Skip containers and entries with no useful data
            if txr_type in _SKIP_TRANSCEIVER_TYPES and not model and not serial:
                continue

            # Use transceiver type as model fallback (e.g., "CFP2/QSFP28")
            display_model = model or (txr_type if txr_type not in _SKIP_TRANSCEIVER_TYPES else "")

            if ent_idx in inv_by_index:
                # Supplement existing inventory item if model/serial is missing or a placeholder
                existing = inv_by_index[ent_idx]
                existing_model = (existing.get("entPhysicalModelName") or "").strip()
                if (
                    existing_model.lower() in _PLACEHOLDER_VALUES or existing_model.lower() == "builtin"
                ) and display_model:
                    existing["entPhysicalModelName"] = display_model
                existing_serial = (existing.get("entPhysicalSerialNum") or "").strip()
                if (existing_serial.lower() in _PLACEHOLDER_VALUES or existing_serial.lower() == "builtin") and serial:
                    existing["entPhysicalSerialNum"] = serial
                    inv_serials.add(serial)
            else:
                # Skip if serial already exists in ENTITY-MIB data (avoid duplicates)
                if serial and serial in inv_serials:
                    continue
                # Create synthetic inventory item for SFPs not in entity inventory
                port_id = txr.get("port_id", 0)
                ifname = port_name_map.get(port_id)
                if ifname:
                    name = ifname
                elif port_id:
                    name = f"Transceiver (port {port_id})"
                else:
                    name = f"Transceiver {ent_idx}"

                synthetic = {
                    "entPhysicalIndex": ent_idx,
                    "entPhysicalName": name,
                    "entPhysicalClass": "port",
                    "entPhysicalModelName": display_model,
                    "entPhysicalSerialNum": serial,
                    "entPhysicalDescr": txr_type,
                    "entPhysicalContainedIn": 0,
                    "_from_transceiver_api": True,
                }
                inventory_data.append(synthetic)
                # Update dedupe maps so subsequent iterations skip this entry
                inv_by_index[ent_idx] = synthetic
                if serial:
                    inv_serials.add(serial)

        return inventory_data, None

    def _build_port_name_map(self, transceivers):
        """
        Build port_id → ifName mapping for transceiver ports.

        Fetches port data from LibreNMS to resolve port IDs to interface names,
        enabling better bay matching for synthetic transceiver items (e.g.,
        Nokia 1/1/c1 instead of opaque port IDs).
        """
        port_ids = {txr.get("port_id") for txr in transceivers if txr.get("port_id")}
        if not port_ids:
            return {}

        success, ports_data = self.librenms_api.get_ports(self.librenms_id)
        if not success or not isinstance(ports_data, dict):
            return {}

        ports = ports_data.get("ports")
        if not isinstance(ports, list):
            return {}

        return {
            p["port_id"]: p["ifName"]
            for p in ports
            if isinstance(p, dict) and p.get("port_id") in port_ids and p.get("ifName")
        }

    def _get_sub_components(self, parent_idx, children_by_parent, index_map, ignore_rules, device_serial=""):
        """
        Find descendant items with a model name (real hardware, not empty containers).

        Returns list of (depth, item) tuples.
        """
        results = []
        self._collect_descendants(
            parent_idx,
            children_by_parent,
            index_map,
            ignore_rules,
            device_serial=device_serial,
            depth=1,
            results=results,
            visited={parent_idx},
        )
        return results

    def _collect_descendants(
        self,
        parent_idx,
        children_by_parent,
        index_map,
        ignore_rules,
        depth,
        results,
        visited=None,
        device_serial="",
    ):
        """Recursively collect descendant items that have a model name."""
        if visited is None:
            visited = set()
        for child in children_by_parent.get(parent_idx, []):
            child_idx = child.get("entPhysicalIndex")
            if child_idx is None:
                continue
            if child_idx in visited:
                continue
            visited.add(child_idx)
            # Apply ignore rules: skip drops the item and its subtree; transparent
            # hides the item but promotes its children to the current depth level.
            parent_item = index_map.get(parent_idx)
            action = _check_ignore_rules(child, parent_item, ignore_rules, index_map, device_serial)
            if action == "skip":
                continue
            if action == "transparent":
                # Don't add this item, but recurse at the same depth so its children
                # are promoted (appear at the same level as the transparent item would).
                self._collect_descendants(
                    child_idx,
                    children_by_parent,
                    index_map,
                    ignore_rules,
                    depth=depth,
                    results=results,
                    visited=visited,
                    device_serial=device_serial,
                )
                continue
            model = (child.get("entPhysicalModelName") or "").strip()
            if model and model not in _GENERIC_CONTAINER_MODELS:
                results.append((depth, child))
                # Continue looking for deeper components (e.g., SFPs inside converters)
                self._collect_descendants(
                    child_idx,
                    children_by_parent,
                    index_map,
                    ignore_rules,
                    depth=depth + 1,
                    results=results,
                    visited=visited,
                    device_serial=device_serial,
                )
            else:
                # Skip generic/empty items, but check their children
                self._collect_descendants(
                    child_idx,
                    children_by_parent,
                    index_map,
                    ignore_rules,
                    depth=depth,
                    results=results,
                    visited=visited,
                    device_serial=device_serial,
                )

    def _sort_with_hierarchy(self, table_data):
        """Sort table keeping children grouped under their parent."""
        status_order = {
            "Installed": 0,
            "Serial Mismatch": 1,
            "Type Mismatch": 2,
            "Matched": 3,
            "No Type": 4,
            "No Bay": 5,
            "Unmatched": 6,
        }

        # Group into top-level items with their children
        groups = []
        current_group = None
        for row in table_data:
            if row.get("depth", 0) == 0:
                current_group = {"parent": row, "children": []}
                groups.append(current_group)
            elif current_group is not None:
                current_group["children"].append(row)

        # Sort groups by parent status
        groups.sort(key=lambda g: status_order.get(g["parent"]["status"], 99))

        # Flatten back
        result = []
        for group in groups:
            result.append(group["parent"])
            result.extend(group["children"])
        return result

    def _get_module_bays(self, obj):
        """
        Get module bays for the device, organized by scope.

        Returns:
            tuple: (device_bays, module_bays) where:
                - device_bays: {name: bay} for device-level bays (module=None)
                - module_bays: {module_id: {name: bay}} for bays created by installed modules
        """
        from dcim.models import ModuleBay

        bays = ModuleBay.objects.filter(device=obj).select_related(
            "installed_module__module_type",
            "module__module_bay",
        )
        device_bays = {}
        module_scoped_bays = {}
        for bay in bays:
            if bay.module_id:
                module_scoped_bays.setdefault(bay.module_id, {})[bay.name] = bay
            else:
                device_bays[bay.name] = bay
        return device_bays, module_scoped_bays

    def _get_module_types(self):
        """Get all module types indexed by model/part_number, with ModuleTypeMapping applied."""
        from netbox_librenms_plugin.utils import get_module_types_indexed

        return get_module_types_indexed()

    def _find_parent_container_name(self, item, index_map):
        """
        Resolve the nearest ancestor container name by walking up the containment chain.

        Skips ancestors with an empty entPhysicalName and continues upward until a
        non-empty name is found or the chain is exhausted.
        """
        contained_in = item.get("entPhysicalContainedIn", 0)
        visited: set = set()
        while contained_in and contained_in in index_map and contained_in not in visited:
            visited.add(contained_in)
            parent = index_map[contained_in]
            name = (parent.get("entPhysicalName") or "").strip()
            if name:
                return name
            contained_in = parent.get("entPhysicalContainedIn", 0)
        return None

    def _match_module_bay(self, item, index_map, module_bays):
        """
        Try to match an inventory item to a NetBox ModuleBay.
        Checks ModuleBayMapping table first (exact then regex), then falls back
        to exact parent name match, then positional matching.
        """
        parent_name = self._find_parent_container_name(item, index_map)
        item_name = (item.get("entPhysicalName") or "").strip()
        item_descr = (item.get("entPhysicalDescr") or "").strip()
        phys_class = (item.get("entPhysicalClass") or "").strip()

        # Build candidate names: parent, item name, item description
        candidate_names = [n for n in [parent_name, item_name, item_descr] if n]

        from netbox_librenms_plugin.utils import apply_normalization_rules

        norm_rules_bay = getattr(self, "_norm_rules_bay", None)
        normalized_extras = []
        for name in candidate_names:
            normalized = apply_normalization_rules(name, "module_bay", preloaded_rules=norm_rules_bay)
            if normalized != name and normalized not in candidate_names and normalized not in normalized_extras:
                normalized_extras.append(normalized)
        all_candidates = candidate_names + normalized_extras

        # Use preloaded exact mappings (set in _build_context to avoid N+1 queries).
        exact_mappings = getattr(self, "_exact_bay_mappings", None)
        if exact_mappings is None:
            from netbox_librenms_plugin.models import ModuleBayMapping

            exact_mappings = list(ModuleBayMapping.objects.filter(is_regex=False))

        # Check ModuleBayMapping table for each candidate (exact match)
        for name in all_candidates:
            bay = self._lookup_exact_bay_mapping(name, phys_class, module_bays, exact_mappings)
            if bay:
                return bay

        # Use preloaded regex mappings.
        regex_mappings = getattr(self, "_regex_bay_mappings", None)
        if regex_mappings is None:
            from netbox_librenms_plugin.models import ModuleBayMapping

            regex_mappings = list(ModuleBayMapping.objects.filter(is_regex=True))

        # Regex pattern matching on all candidate names
        for name in all_candidates:
            bay = self._lookup_regex_bay_mapping(name, phys_class, module_bays, regex_mappings)
            if bay:
                return bay

        # Fallback: exact match on candidate names against bay dict, with FPC-scope check
        for name in all_candidates:
            if name in module_bays:
                maps = module_bays.maps if hasattr(module_bays, "maps") else [module_bays]
                for scope_map in maps:
                    if name in scope_map:
                        bay = scope_map[name]
                        if BaseModuleTableView._fpc_slot_matches(name, bay):
                            return bay

        # Positional fallback: determine slot number from container sibling order
        # Handles SFPs inside converters where containers are unnamed
        bay = self._match_bay_by_position(item, index_map, module_bays)
        if bay:
            return bay

        return None

    @staticmethod
    def _fpc_slot_matches(candidate_name, bay):
        """
        Validate that a regex-matched bay's parent slot position is consistent with
        a positional descriptor like 'Model @ FPC/pic/port'.

        Returns True if the descriptor has no FPC reference, or if the bay's parent
        module slot position matches the FPC number in the descriptor. Prevents
        orphaned top-level items (e.g. QSFP @ 1/1/1 when FPC1 is not installed)
        from incorrectly matching bays belonging to a different FPC's module.
        """
        match = re.search(r"@\s+(\d+)/", candidate_name)
        if not match:
            return True
        try:
            expected_fpc = int(match.group(1))
        except (ValueError, IndexError):
            return True
        module = getattr(bay, "module", None)
        if not module:
            return True
        parent_bay = getattr(module, "module_bay", None)
        if not parent_bay:
            return True
        try:
            return int(parent_bay.position) == expected_fpc
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _lookup_exact_bay_mapping(name, phys_class, module_bays, exact_mappings):
        """
        Try exact ModuleBayMapping entries against a candidate name.

        Checks class-scoped mappings first, then falls back to classless mappings.
        Returns the matched module bay or None.
        """
        if phys_class:
            mapping = next(
                (m for m in exact_mappings if m.librenms_name == name and m.librenms_class == phys_class), None
            )
            if mapping and mapping.netbox_bay_name in module_bays:
                return module_bays[mapping.netbox_bay_name]
        mapping = next((m for m in exact_mappings if m.librenms_name == name and m.librenms_class == ""), None)
        if mapping and mapping.netbox_bay_name in module_bays:
            return module_bays[mapping.netbox_bay_name]
        return None

    @staticmethod
    def _lookup_regex_bay_mapping(name, phys_class, module_bays, regex_mappings):
        """
        Try regex ModuleBayMapping patterns against a name.

        ``regex_mappings`` is a pre-filtered list of is_regex=True ModuleBayMapping
        objects (passed in from the caller to avoid per-item DB queries).

        Returns matched module bay or None.
        """
        # Filter preloaded list by class (exact class match, then empty-class fallback)
        if phys_class:
            exact = [m for m in regex_mappings if m.librenms_class == phys_class]
            fallback = [m for m in regex_mappings if m.librenms_class == ""]
            candidates = exact + fallback
        else:
            candidates = [m for m in regex_mappings if m.librenms_class == ""]

        for mapping in candidates:
            compiled = mapping._compiled_pattern
            if compiled is None:
                continue
            try:
                match = compiled.fullmatch(name)
            except re.error:
                continue
            if not match:
                continue
            try:
                resolved_bay = match.expand(mapping.netbox_bay_name)
            except (re.error, IndexError):
                continue
            if resolved_bay in module_bays:
                maps = module_bays.maps if hasattr(module_bays, "maps") else [module_bays]
                for scope_map in maps:
                    if resolved_bay in scope_map:
                        bay = scope_map[resolved_bay]
                        if BaseModuleTableView._fpc_slot_matches(name, bay):
                            return bay
        return None

    @staticmethod
    def _match_bay_by_position(item, index_map, module_bays):
        """
        Match bay by item's positional order among container siblings.

        When an item is inside a container (no model), walk up to find the
        nearest ancestor with a real hardware model, count which container slot
        the item occupies, and match to the bay by number (e.g., SFP 1, SFP 2).
        """
        # Walk up through containers with placeholder/empty models to find the
        # parent with a real hardware model.  Use a visited set to detect cycles.
        current_idx = item.get("entPhysicalContainedIn", 0)
        container_idx = None
        visited = set()
        while current_idx and current_idx in index_map and current_idx not in visited:
            visited.add(current_idx)
            ancestor = index_map[current_idx]
            model = (ancestor.get("entPhysicalModelName") or "").strip()
            if model and model not in _GENERIC_CONTAINER_MODELS:
                # Found the parent with a real model; container_idx is the intermediate container
                break
            container_idx = current_idx
            current_idx = ancestor.get("entPhysicalContainedIn", 0)
        else:
            return None

        if not container_idx:
            return None

        # Determine position: count siblings under the parent, filtering out
        # non-hardware items (sensors, LEDs) that would shift the bay index.
        parent_with_model_idx = current_idx
        siblings = sorted(
            [
                i
                for i in index_map.values()
                if i.get("entPhysicalContainedIn") == parent_with_model_idx
                and i.get("entPhysicalClass") not in _NON_HARDWARE_CLASSES
            ],
            key=lambda x: (
                int(x.get("entPhysicalParentRelPos") or 0)
                if str(x.get("entPhysicalParentRelPos", "0")).lstrip("-").isdigit()
                else 0
            ),
        )
        slot_num = None
        for i, sib in enumerate(siblings):
            if sib["entPhysicalIndex"] == container_idx:
                slot_num = i + 1
                break

        if slot_num is None:
            return None

        # Try common bay naming patterns
        for pattern in [f"SFP {slot_num}", f"Slot {slot_num}", f"Bay {slot_num}", f"Port {slot_num}"]:
            if pattern in module_bays:
                return module_bays[pattern]

        return None

    def _build_row(self, item, index_map, module_bays, module_types, depth=0, manufacturer=None, sibling_counts=None):
        """Build a single table row from a LibreNMS inventory item."""
        from netbox_librenms_plugin.utils import (
            has_nested_name_conflict,
            resolve_module_type,
        )

        model_name = (item.get("entPhysicalModelName", "") or "").strip()
        serial = (item.get("entPhysicalSerialNum", "") or "").strip()
        phys_class = item.get("entPhysicalClass", "")
        name = item.get("entPhysicalName", "") or "-"
        description = item.get("entPhysicalDescr", "") or ""

        # Match to NetBox module bay
        matched_bay = self._match_module_bay(item, index_map, module_bays)

        # Match to NetBox module type (direct lookup, then normalization fallback)
        norm_rules_type = getattr(self, "_norm_rules_type", None)
        matched_type = resolve_module_type(
            model_name, module_types, manufacturer=manufacturer, norm_rules=norm_rules_type
        )

        # Check for nested module naming conflicts
        name_conflict = (
            matched_type and matched_bay and has_nested_name_conflict(matched_type, matched_bay, sibling_counts)
        )

        # Determine status
        status = self._determine_status(matched_bay, matched_type, serial)

        row = {
            "name": name,
            "model": model_name or "-",
            "serial": serial or "-",
            "description": description,
            "item_class": phys_class,
            "module_bay": matched_bay.name if matched_bay else "-",
            "module_type": matched_type.model if matched_type else "-",
            "status": status,
            "row_class": "",
            "can_install": False,
            "module_bay_id": matched_bay.pk if matched_bay else None,
            "module_type_id": matched_type.pk if matched_type else None,
            "depth": depth,
            "ent_physical_index": item.get("entPhysicalIndex"),
            "has_installable_children": False,
        }

        if name_conflict:
            row["row_class"] = "table-warning"
            row["name_conflict_warning"] = (
                "This module type uses {module} in its interface template. "
                "Installing multiple siblings will create duplicate interface names. "
                "An interface naming plugin with a rewrite rule for this module type can resolve this."
            )

        # Add URLs for matched objects
        if matched_bay:
            row["module_bay_url"] = matched_bay.get_absolute_url()
            # Check if a module is already installed in this bay
            if hasattr(matched_bay, "installed_module") and matched_bay.installed_module:
                installed = matched_bay.installed_module
                row["installed_module"] = installed
                row["module_url"] = installed.get_absolute_url()
                row["installed_module_id"] = installed.pk
                self._apply_installed_status(row, installed, matched_type, serial)
            elif matched_type:
                # Bay exists, type matched, no module installed → can install
                row["can_install"] = True

        if matched_type:
            row["module_type_url"] = matched_type.get_absolute_url()

        return row

    @staticmethod
    def _apply_installed_status(row, installed, matched_type, serial):
        """Set status, row_class, and action flags when a module is already installed."""
        if matched_type is not None and installed.module_type_id != matched_type.pk:
            row["status"] = "Type Mismatch"
            row["row_class"] = "table-warning"
            row["can_replace"] = True
        elif matched_type is not None:
            # Normalize both serials: treat None, empty, whitespace, and placeholder values as absent
            nb_serial = (installed.serial or "").strip()
            if nb_serial.lower() in _PLACEHOLDER_VALUES:
                nb_serial = ""
            lnms_serial = serial if serial.lower() not in _PLACEHOLDER_VALUES else ""
            if lnms_serial and lnms_serial != nb_serial:
                row["status"] = "Serial Mismatch"
                row["row_class"] = "table-danger"
                row["can_update_serial"] = True
                row["can_replace"] = True
            else:
                row["status"] = "Installed"
                row["row_class"] = "table-success"
        else:
            row["status"] = "Installed"
            row["row_class"] = "table-success"

    def _determine_status(self, matched_bay, matched_type, serial):
        """Determine the sync status for an inventory item."""
        if matched_bay and matched_type:
            return "Matched"
        if not matched_bay:
            return "No Bay"
        if not matched_type:
            return "No Type"
        return "Unmatched"

    def _detect_serial_conflicts(self, table_data):
        """
        Bulk-check whether LibreNMS serials for replaceable or installable rows already exist elsewhere in NetBox.

        For each row with can_replace or can_install, checks whether the LibreNMS serial (the value we want to
        write) is already assigned to a *different* module.  When a conflict is found the row
        gets two extra keys:

          serial_conflict_module  – the conflicting Module object (with device/module_bay loaded)
          can_move_from           – True (convenience flag for templates/tests)
        """
        from dcim.models import Module

        # Map serial → list of rows that may be affected
        serial_rows: dict = {}
        for row in table_data:
            if not row.get("can_replace") and not row.get("can_install"):
                continue
            serial = row.get("serial", "")
            if serial and serial.lower() not in _PLACEHOLDER_VALUES:
                serial_rows.setdefault(serial, []).append(row)

        if not serial_rows:
            return

        conflicts = Module.objects.filter(serial__in=serial_rows.keys()).select_related(
            "module_type", "module_bay", "device"
        )
        for conflict in conflicts:
            for row in serial_rows.get(conflict.serial, []):
                installed_id = row.get("installed_module_id")
                # Skip if this IS the module already in the current bay
                if installed_id and conflict.pk == installed_id:
                    continue
                row["serial_conflict_module"] = conflict
                row["can_move_from"] = True
