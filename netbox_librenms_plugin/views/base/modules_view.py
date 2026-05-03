import logging
import re

from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.utils import get_librenms_oob, get_librenms_sync_device
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
)

logger = logging.getLogger(__name__)


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
_GENERIC_CONTAINER_MODELS = {"", "builtin", "default", "n/a"}

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
        (the NetBox ``Device.serial`` value) **and** the item sits at chassis
        level — i.e. has no parent (top-level entity) or its direct parent has
        ``entPhysicalClass="chassis"``.  No name pattern is used.
        ``require_serial_match_parent`` is ignored for this type.

        The chassis-level requirement prevents the rule from misfiring on
        chassis-based devices whose line cards happen to share a serial with
        the device record (e.g. Cisco ASR-9904 with ``Device.serial`` set to
        the linecard's serial — without the guard, the linecard becomes
        transparent and its sub-ports collapse to chassis-level bay matching).

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
            if not (item_serial and device_serial and item_serial == device_serial):
                continue
            # Restrict to chassis-level entries.  The rule targets fixed-form
            # routers' system board (top-level or direct child of the chassis
            # entity); on chassis devices a linecard sharing the device serial
            # is *not* a system board, and marking it transparent silently
            # promotes its sub-ports to chassis bay-matching.
            if parent_item is not None and parent_item.get("entPhysicalClass") != "chassis":
                continue
            return rule.action

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

    def _get_sync_device(self, obj):
        """Resolve the LibreNMS sync device for cache reads/writes in VC contexts."""
        sync_device = get_librenms_sync_device(obj, server_key=self.librenms_api.server_key)
        return sync_device or obj

    @staticmethod
    def _normalize_serial(value):
        """Normalize serial values for reliable cross-source comparison."""
        serial = (value or "").strip()
        if serial.lower() in _PLACEHOLDER_VALUES:
            return ""
        return serial

    def _infer_vc_member_for_item(self, obj, item, index_map, vc_members):
        """
        Infer VC member ownership for an inventory item using LibreNMS ENTITY data.

        Returns:
            tuple: (Device, source) where source is a short reason string.
        """
        if not vc_members:
            return obj, "default"

        member_by_serial = {
            self._normalize_serial(getattr(member, "serial", "")): member
            for member in vc_members
            if self._normalize_serial(getattr(member, "serial", ""))
        }

        item_serial = self._normalize_serial(item.get("entPhysicalSerialNum"))
        if item_serial and item_serial in member_by_serial:
            return member_by_serial[item_serial], "serial"

        # Walk ancestors to find a serial tied to a VC member.
        parent_idx = item.get("entPhysicalContainedIn", 0)
        visited = set()
        while parent_idx and parent_idx in index_map and parent_idx not in visited:
            visited.add(parent_idx)
            parent = index_map[parent_idx]
            parent_serial = self._normalize_serial(parent.get("entPhysicalSerialNum"))
            if parent_serial and parent_serial in member_by_serial:
                return member_by_serial[parent_serial], "ancestor-serial"
            parent_idx = parent.get("entPhysicalContainedIn", 0)

        # Position-based fallback from ENTITY parentRelPos.
        rel_pos = item.get("entPhysicalParentRelPos")
        try:
            rel_pos = int(rel_pos)
        except (TypeError, ValueError):
            rel_pos = None

        if rel_pos:
            for member in vc_members:
                if getattr(member, "vc_position", None) == rel_pos:
                    return member, "position"

        # Name/model hint fallback: common "<position>/..." prefixes.
        hints = [
            (item.get("entPhysicalName") or "").strip(),
            (item.get("entPhysicalDescr") or "").strip(),
            (item.get("entPhysicalModelName") or "").strip(),
        ]
        for hint in hints:
            if not hint:
                continue
            match = re.match(r"^\D*([1-9]\d*)[/:\-].*", hint)
            if not match:
                continue
            try:
                hinted_pos = int(match.group(1))
            except (TypeError, ValueError):
                continue
            for member in vc_members:
                if getattr(member, "vc_position", None) == hinted_pos:
                    return member, "name-hint"

        return obj, "default"

    def post(self, request, pk):
        """Fetch inventory from LibreNMS, cache it, and render the module sync table."""
        obj = self.get_object(pk)
        sync_device = self._get_sync_device(obj)

        self.librenms_id = self.librenms_api.get_librenms_id(sync_device)
        if not self.librenms_id:
            cache.delete(self.get_cache_key(sync_device, "inventory", server_key=self.librenms_api.server_key))
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
            cache.delete(self.get_cache_key(sync_device, "inventory", server_key=self.librenms_api.server_key))
            logger.error("Failed to fetch inventory from LibreNMS for device %s: %s", self.librenms_id, inventory_data)
            messages.error(request, "Failed to fetch inventory from LibreNMS; see server logs for details.")
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

        for item in inventory_data:
            item["_source"] = "main"

        # If an OOB controller is linked, fetch its inventory and merge.
        # Offset OOB entPhysicalIndex values by 1_000_000 to prevent collisions.
        _server_key = self.librenms_api.server_key
        oob = get_librenms_oob(obj, server_key=_server_key)
        if oob and oob.get("id"):
            oob_success, oob_inventory = self.librenms_api.get_device_inventory(oob["id"])
            if oob_success:
                _OOB_OFFSET = 1_000_000
                for item in oob_inventory:
                    item["_source"] = "oob"
                    if (idx := item.get("entPhysicalIndex")) is not None:
                        item["entPhysicalIndex"] = idx + _OOB_OFFSET
                    if (parent := item.get("entPhysicalContainedIn")) is not None and parent != 0:
                        item["entPhysicalContainedIn"] = parent + _OOB_OFFSET
                inventory_data = inventory_data + oob_inventory

        # Fetch transceiver data and merge with inventory
        inventory_data, txr_error = self._merge_transceiver_data(inventory_data)

        # Cache the merged inventory data, namespaced by server and librenms_id to detect remapping
        cache.set(
            self.get_cache_key(sync_device, "inventory", server_key=self.librenms_api.server_key),
            {"inventory": inventory_data, "librenms_id": self.librenms_id},
            timeout=self.librenms_api.cache_timeout,
        )

        context = self._build_context(request, obj, inventory_data)
        if txr_error:
            logger.warning("Transceiver fetch failed for device %s: %s", self.librenms_id, txr_error)
            messages.warning(request, "Inventory refreshed, but transceiver fetch failed; see server logs for details.")
        else:
            messages.success(request, "Inventory data refreshed successfully.")
        return render(
            request,
            self.partial_template_name,
            {"module_sync": context, "has_write_permission": self.has_write_permission()},
        )

    def get_context_data(self, request, obj):
        """Get context from cache (used by the main sync view on initial page load)."""
        sync_device = self._get_sync_device(obj)
        cache_key = self.get_cache_key(sync_device, "inventory", server_key=self.librenms_api.server_key)
        cached_payload = cache.get(cache_key)
        if not isinstance(cached_payload, dict) or "inventory" not in cached_payload:
            cache.delete(cache_key)
            return {"table": None, "object": obj, "cache_expiry": None, "server_key": self.librenms_api.server_key}
        # Validate that the cached inventory was built for the same LibreNMS device.
        # If the object has been remapped to a different device, discard stale inventory.
        current_librenms_id = self.librenms_api.get_librenms_id(sync_device)
        if cached_payload.get("librenms_id") != current_librenms_id:
            cache.delete(cache_key)
            return {"table": None, "object": obj, "cache_expiry": None, "server_key": self.librenms_api.server_key}
        return self._build_context(request, obj, cached_payload["inventory"])

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

        module_types = self._get_module_types()
        self._generic_module_types = self._get_generic_module_types()
        self._module_type_ambiguities = self._get_module_type_ambiguities()
        self._carrier_install_rules = self._get_carrier_install_rules(manufacturer)

        transparent_indices = self._find_transparent_indices(inventory_data, ignore_cache)
        top_items = self._collect_top_items(
            inventory_data, index_map, ignore_rules, device_serial, transparent_indices, ignore_cache
        )
        table_data = self._build_table_rows(
            obj,
            top_items,
            index_map,
            children_by_parent,
            ignore_rules,
            device_serial,
            module_types,
            manufacturer=manufacturer,
        )

        # Sort top-level groups by status, keeping children after their parent
        table_data = self._sort_with_hierarchy(table_data)

        # Bulk-detect serial conflicts for rows that can be replaced/installed
        self._detect_serial_conflicts(table_data)

        table = self.get_table(table_data, obj)
        table.configure(request)

        sync_device = self._get_sync_device(obj)
        cache_ttl = getattr(cache, "ttl", lambda k: None)(
            self.get_cache_key(sync_device, "inventory", server_key=self.librenms_api.server_key)
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
                # If a parent in the inventory was identified (e.g. via
                # ``_nest_synthetic_transceivers``), let normal hierarchy
                # walking render this item under that parent instead of at
                # the top level.
                parent_idx = item.get("entPhysicalContainedIn")
                if parent_idx and parent_idx in index_map:
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
            # Skip items with generic model names (not real hardware), regardless of class.
            model = (item.get("entPhysicalModelName") or "").strip().lower()
            if model in _GENERIC_CONTAINER_MODELS:
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
                    anc_model = (ancestor.get("entPhysicalModelName") or "").strip().lower()
                    if anc_model in _GENERIC_CONTAINER_MODELS:
                        current_idx = ancestor.get("entPhysicalContainedIn", 0)
                        continue
                    is_descendant = True
                    break
                current_idx = ancestor.get("entPhysicalContainedIn", 0)
            if is_descendant:
                continue
            top_items.append(item)
        return top_items

    @staticmethod
    def _compute_all_bays(device_bays: dict, module_scoped_bays: dict) -> dict:
        """Build a deterministic flat bay lookup from module-scoped and device bays.

        Module IDs are sorted so the first-match-wins behaviour is stable across
        runs (lower PK wins on collision).  Device-level bays are merged last so
        they always take precedence over module-scoped bays.

        Logs a DEBUG message when the same bay name appears in more than one
        module scope.
        """
        module_bay_flat: dict = {}
        collision_names: set = set()
        for mid in sorted(module_scoped_bays):
            for name, bay in module_scoped_bays[mid].items():
                if name in module_bay_flat:
                    collision_names.add(name)
                else:
                    module_bay_flat[name] = bay
        all_bays = {**module_bay_flat, **device_bays}
        if collision_names:
            logger.debug(
                "Module-scoped bay name collisions (first-match by module PK kept): %s",
                sorted(collision_names),
            )
        return all_bays

    def _build_table_rows(
        self,
        obj,
        top_items,
        index_map,
        children_by_parent,
        ignore_rules,
        device_serial,
        module_types,
        manufacturer=None,
    ):
        """Build table rows from top-level items and their sub-components."""
        vc_members = list(obj.virtual_chassis.members.all()) if getattr(obj, "virtual_chassis", None) else []

        member_contexts = self._build_member_contexts(obj, vc_members)

        table_data = []

        for item in top_items:
            target_device, resolution_source = self._infer_vc_member_for_item(obj, item, index_map, vc_members)
            target_context = member_contexts.get(target_device.id) or member_contexts.get(obj.id)
            if target_context is None:
                continue
            self._append_rows_for_item_context(
                table_data,
                item,
                target_context,
                index_map,
                children_by_parent,
                ignore_rules,
                device_serial,
                module_types,
                manufacturer=manufacturer,
                selected_device=target_device,
                resolution_source=resolution_source,
            )

        return table_data

    def _build_table_rows_for_member(
        self,
        member,
        top_items,
        index_map,
        children_by_parent,
        ignore_rules,
        device_serial,
        module_types,
        manufacturer=None,
    ):
        """Build rows using a fixed target member for every inventory item."""
        member_contexts = self._build_member_contexts(member, vc_members=[])
        target_context = member_contexts.get(member.id)
        if target_context is None:
            return []

        table_data = []
        for item in top_items:
            self._append_rows_for_item_context(
                table_data,
                item,
                target_context,
                index_map,
                children_by_parent,
                ignore_rules,
                device_serial,
                module_types,
                manufacturer=manufacturer,
                selected_device=member,
                resolution_source="manual",
            )

        return table_data

    def _build_member_contexts(self, obj, vc_members):
        """Build per-member bay context data used for row resolution."""
        member_contexts = {}
        context_members = vc_members if vc_members else [obj]
        for member in context_members:
            device_bays, module_scoped_bays = self._get_module_bays(member)
            member_contexts[member.id] = {
                "device": member,
                "device_bays": device_bays,
                "module_scoped_bays": module_scoped_bays,
                "all_bays": self._compute_all_bays(device_bays, module_scoped_bays),
                "sibling_counts": {mid: len(bays) for mid, bays in module_scoped_bays.items()},
            }
        return member_contexts

    def _append_rows_for_item_context(
        self,
        table_data,
        item,
        target_context,
        index_map,
        children_by_parent,
        ignore_rules,
        device_serial,
        module_types,
        manufacturer,
        selected_device,
        resolution_source,
    ):
        """Append one top-level item and descendants using one target device context."""
        # Stash full device-level bay set for the holder-install hint in
        # _build_no_bay_warning. Set per-item-context so virtual-chassis members
        # see the right device's bays.
        self._current_device_bays = target_context.get("device_bays") or {}
        # Manufacturer for ModuleBayMapping vendor-scoping: prefer mappings
        # whose manufacturer matches this device's manufacturer; fall back to
        # vendor-agnostic (NULL) mappings; skip mappings scoped to a different
        # manufacturer. Set per-item-context so VC members resolve correctly.
        sel_dt = getattr(selected_device, "device_type", None)
        sel_mfr = getattr(sel_dt, "manufacturer", None)
        self._current_manufacturer_id = getattr(sel_mfr, "id", None)
        self._current_manufacturer_name = getattr(sel_mfr, "name", None)
        # Top-level items match against the full bay set: device-level bays plus
        # bays exposed by already-installed carriers/modules. This lets a
        # cpmModule reported by SNMP at the chassis level (e.g. Nokia 'Slot A')
        # resolve into a carrier-installed child bay (e.g. CMA's 'CPM A') via a
        # ModuleBayMapping. device_bays takes precedence on key collisions
        # because all_bays is built as {**module_bay_flat, **device_bays}.
        item_bays = target_context["all_bays"]
        row = self._build_row(
            item,
            index_map,
            item_bays,
            module_types,
            depth=0,
            manufacturer=manufacturer,
            sibling_counts=target_context["sibling_counts"],
        )
        row["selected_device_id"] = selected_device.id
        row["selected_device_name"] = selected_device.name
        row["member_resolution_source"] = resolution_source
        self._apply_carrier_install_rules(row, item, selected_device)
        parent_row_idx = len(table_data)
        table_data.append(row)

        # Flag device type as incomplete when a top-level item has no bay and
        # no mapping suggestion — the device type is likely missing bay templates
        # for that class of component (fan tray, PSU, etc.).
        if row.get("status") == "No Bay" and "model_suggestion" not in row:
            # Before flagging device-type as incomplete, check whether any
            # bay among the carriers already installed at device level (i.e.
            # module-scoped child bays) would yield a mapping suggestion.
            # This handles the common "user installed a carrier card whose
            # children are letter-named (Slot A → CPM A) and now needs a
            # ModuleBayMapping" follow-up flow.
            fallback_suggestion = self._suggest_bay_mapping(item, target_context["all_bays"], scope_preserved=False)
            if fallback_suggestion:
                # Pre-fill the suggestion with this device's manufacturer so
                # the new ModuleBayMapping is auto-scoped to the vendor — the
                # user can clear it in the form to make it global.
                if self._current_manufacturer_id:
                    fallback_suggestion.setdefault("manufacturer", self._current_manufacturer_id)
                    if self._current_manufacturer_name:
                        fallback_suggestion.setdefault("manufacturer_name", self._current_manufacturer_name)
                row["model_suggestion"] = fallback_suggestion
                # Refresh the warning so the tooltip text reflects the new
                # suggestion instead of the previous "no candidate" message.
                row["model_warning"] = self._build_no_bay_warning(item, target_context["all_bays"], fallback_suggestion)
                # Drop the carrier-install hint badge: a concrete mapping
                # suggestion is more actionable than "Possible Carrier?".
                row.pop("holder_hint_present", None)
            else:
                device_type = getattr(selected_device, "device_type", None)
                if device_type:
                    row["device_type_incomplete"] = True
                    row["device_type_incomplete_url"] = device_type.get_absolute_url()
                    row["device_type_incomplete_name"] = str(device_type)
                    row["device_type_incomplete_target_pk"] = device_type.pk
                    row["device_type_incomplete_suggestion"] = self._derive_bay_template_suggestion(item)

        # Determine child bay scope based on parent match state
        parent_module_id = None
        parent_bay_matched_but_uninstalled = False
        parent_installed_module = None
        if row.get("module_bay_id"):
            matched_bay = item_bays.get(row["module_bay"])
            if matched_bay and hasattr(matched_bay, "installed_module") and matched_bay.installed_module:
                parent_module_id = matched_bay.installed_module.pk
                parent_installed_module = matched_bay.installed_module
            else:
                parent_bay_matched_but_uninstalled = True

        if parent_bay_matched_but_uninstalled:
            child_bays = {}
        elif parent_module_id:
            child_bays = target_context["module_scoped_bays"].get(parent_module_id, {})
        else:
            child_bays = target_context["device_bays"]

        # Process sub-components with depth-tracked bay scoping
        bays_by_depth = {0: child_bays}
        # Parallel state: at each depth, is the (empty) scope a result of an
        # uninstalled ancestor bay match?  Used to drive the "install parent
        # first" hint on No Bay rows.
        scope_uninstalled_init = parent_bay_matched_but_uninstalled
        scope_uninstalled_by_depth = {0: scope_uninstalled_init}
        # Parallel state: was the scope inherited from an unmatched ancestor?
        # Used to suppress mapping suggestions whose target bays would be at
        # the wrong physical level.
        scope_preserved_init = not (parent_module_id or parent_bay_matched_but_uninstalled)
        scope_preserved_by_depth = {0: scope_preserved_init}
        # Parallel state: is the empty scope specifically because an installed
        # module's type has no bay templates defined?  Distinct from
        # scope_preserved (unmatched ancestor) and scope_uninstalled (bay
        # matched but no module installed).  Propagates through intermediate
        # unmatched containers so deeply-nested items still show the right hint.
        scope_empty_installed_bays_init = bool(parent_module_id) and not child_bays
        scope_empty_installed_bays_by_depth = {0: scope_empty_installed_bays_init}
        parent_ent_idx = item.get("entPhysicalIndex")
        if parent_ent_idx is None:
            return

        sub_items = self._get_sub_components(
            parent_ent_idx,
            children_by_parent,
            index_map,
            ignore_rules,
            device_serial,
        )
        for depth, sub_item in sub_items:
            # Fallbacks return the top-level state so the first iteration
            # (typically depth=1) inherits the right scope semantics rather
            # than silently defaulting to False.
            scope_bays = bays_by_depth.get(depth, child_bays)
            scope_uninstalled = scope_uninstalled_by_depth.get(depth, scope_uninstalled_init)
            scope_preserved = scope_preserved_by_depth.get(depth, scope_preserved_init)
            scope_empty_installed_bays = scope_empty_installed_bays_by_depth.get(depth, scope_empty_installed_bays_init)
            sub_row = self._build_row(
                sub_item,
                index_map,
                scope_bays,
                module_types,
                depth=depth,
                manufacturer=manufacturer,
                sibling_counts=target_context["sibling_counts"],
                scope_uninstalled=scope_uninstalled,
                scope_preserved=scope_preserved,
                scope_empty_installed_bays=scope_empty_installed_bays,
            )
            sub_row["selected_device_id"] = selected_device.id
            sub_row["selected_device_name"] = selected_device.name
            sub_row["member_resolution_source"] = resolution_source
            self._apply_carrier_install_rules(sub_row, sub_item, selected_device)
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
                    sub_bays = target_context["module_scoped_bays"].get(sub_module_id, {})
                    bays_by_depth[depth + 1] = sub_bays
                    scope_uninstalled_by_depth[depth + 1] = False
                    scope_preserved_by_depth[depth + 1] = False
                    scope_empty_installed_bays_by_depth[depth + 1] = not sub_bays
                else:
                    bays_by_depth[depth + 1] = {}
                    scope_uninstalled_by_depth[depth + 1] = True
                    scope_preserved_by_depth[depth + 1] = False
                    scope_empty_installed_bays_by_depth[depth + 1] = False
            else:
                # Preserve parent scope for unmatched intermediate containers
                bays_by_depth[depth + 1] = scope_bays
                scope_uninstalled_by_depth[depth + 1] = scope_uninstalled
                # Integrated children (e.g. Nokia MDA fused into a XIOM) share
                # the integrating ancestor's bay scope at the correct hierarchical
                # level, so propagate the parent's scope_preserved flag rather
                # than forcing True (which would suppress bay-mapping suggestions
                # for their grandchildren).
                if sub_row.get("integrated_in_index"):
                    scope_preserved_by_depth[depth + 1] = scope_preserved
                else:
                    scope_preserved_by_depth[depth + 1] = True
                scope_empty_installed_bays_by_depth[depth + 1] = scope_empty_installed_bays

            if sub_row.get("can_install"):
                table_data[parent_row_idx]["has_installable_children"] = True
            # When parent bay is uninstalled, sub-rows have empty bays so
            # can_install is False, but module_type_id is still resolved.
            # Use it to enable "Install Branch" without a second resolve pass.
            elif parent_bay_matched_but_uninstalled and sub_row.get("module_type_id"):
                table_data[parent_row_idx]["has_installable_children"] = True

        # If the installed module's type has no bay templates but has LibreNMS
        # sub-items, flag the parent row so the table can render a "Fix Model"
        # badge linking directly to the module type for quick editing.
        if parent_installed_module and not child_bays:
            first_no_bay_child_idx = None
            for i in range(parent_row_idx + 1, len(table_data)):
                if table_data[i].get("no_bay_reason") == "empty_parent_bays":
                    first_no_bay_child_idx = i
                    break
            if first_no_bay_child_idx is not None:
                mt = parent_installed_module.module_type
                table_data[parent_row_idx]["model_incomplete"] = True
                table_data[parent_row_idx]["model_incomplete_url"] = mt.get_absolute_url()
                table_data[parent_row_idx]["model_incomplete_name"] = str(mt)
                table_data[parent_row_idx]["model_incomplete_target_pk"] = mt.pk
                child_row = table_data[first_no_bay_child_idx]
                # Reconstruct a minimal item dict from the row so the helper
                # can extract a sensible bay name suggestion.
                child_item = {
                    "entPhysicalName": child_row.get("name") or "",
                    "entPhysicalClass": child_row.get("item_class") or "",
                    "entPhysicalDescr": child_row.get("description") or "",
                }
                table_data[parent_row_idx]["model_incomplete_suggestion"] = self._derive_bay_template_suggestion(
                    child_item
                )

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

        # Nest synthetic transceivers under their parent inventory item by
        # matching the leading slash-separated path components of the synthetic
        # name (e.g. ``1/1/c1`` or ``2/x1/1/c2``) against existing item
        # entPhysicalName values. Vendors that name slot/MDA items with the
        # same path (Nokia: ``MDA 1/1`` / ``MDA 2/x1/1``; Juniper FPC ports;
        # etc.) get correctly hierarchical rendering instead of a flat list of
        # orphan transceivers at the top of the modules table.
        self._nest_synthetic_transceivers(inventory_data)

        return inventory_data, None

    @staticmethod
    def _nest_synthetic_transceivers(inventory_data):
        """Set ``entPhysicalContainedIn`` on synthetic transceiver items by
        matching path-prefix in their name against an existing item's name.

        Generic, vendor-agnostic: only relies on the convention that ports
        are named like ``a/b/c`` and their parent module is named in a way
        that ends with the path prefix (e.g. ``MDA 1/1`` for ``1/1/c1``,
        ``XIOM 2/x1`` for ``2/x1/1/c2`` if the MDA is missing). Items
        already nested by ENTITY-MIB (``entPhysicalContainedIn != 0``) and
        non-synthetic items are left untouched.
        """
        # Build name → index lookup once
        name_to_index = {}
        for it in inventory_data:
            nm = (it.get("entPhysicalName") or "").strip()
            if nm:
                name_to_index.setdefault(nm, it.get("entPhysicalIndex"))

        for it in inventory_data:
            if not it.get("_from_transceiver_api"):
                continue
            if it.get("entPhysicalContainedIn"):
                continue
            name = (it.get("entPhysicalName") or "").strip()
            if not name or "/" not in name:
                continue
            parts = name.split("/")
            # Try the longest prefix first, shortening one component at a time
            for i in range(len(parts) - 1, 0, -1):
                prefix = "/".join(parts[:i])
                parent_idx = None
                for cand_name, cand_idx in name_to_index.items():
                    if cand_name == prefix or cand_name.endswith(" " + prefix) or cand_name.endswith("/" + prefix):
                        parent_idx = cand_idx
                        break
                if parent_idx and parent_idx != it.get("entPhysicalIndex"):
                    it["entPhysicalContainedIn"] = parent_idx
                    break

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
            model = (child.get("entPhysicalModelName") or "").strip().lower()
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

    def _get_generic_module_types(self):
        """Get module types from the 'Generic' manufacturer for secondary fallback matching."""
        from netbox_librenms_plugin.utils import get_generic_module_types_indexed

        return get_generic_module_types_indexed()

    def _get_module_type_ambiguities(self):
        """Return part_number/model strings that map to multiple NetBox ModuleTypes."""
        from netbox_librenms_plugin.utils import get_module_type_ambiguities

        return get_module_type_ambiguities()

    def _get_carrier_install_rules(self, manufacturer):
        """Return CarrierAutoInstallRule rows applicable to this device."""
        from django.db.models import Q
        from dcim.models import Manufacturer as _Manufacturer

        from netbox_librenms_plugin.models import CarrierAutoInstallRule

        qs = CarrierAutoInstallRule.objects.select_related("manufacturer", "carrier_module_type")
        if isinstance(manufacturer, _Manufacturer):
            qs = qs.filter(Q(manufacturer__isnull=True) | Q(manufacturer=manufacturer))
        else:
            qs = qs.filter(manufacturer__isnull=True)
        return list(qs)

    def _apply_carrier_install_rules(self, row, item, selected_device):
        """Attach carrier_install_options to a No Bay row when configured rules match.

        Rules match when:
          * device_type_pattern (if set) fullmatches the selected device's
            device_type model;
          * librenms_child_class equals (case-insensitive) the item's
            entPhysicalClass;
          * librenms_child_name_pattern fullmatches the item's entPhysicalName;
          * at least one EMPTY device-level bay's name fullmatches
            netbox_bay_name_pattern.

        Each matching (rule, empty bay) pair becomes one suggestion. Also
        attaches ``device_empty_bay_names`` on No Bay rows so the table can
        pre-fill an "Add Carrier Rule" link.
        """
        if row.get("status") != "No Bay":
            return
        device_bays = getattr(self, "_current_device_bays", None) or {}
        if not device_bays:
            return
        empty_bays = [(name, bay) for name, bay in device_bays.items() if not getattr(bay, "installed_module", None)]
        if not empty_bays:
            return
        # Always expose empty bay names on No Bay rows so the table can
        # pre-fill an "Add Carrier Rule" form even when no rule matches.
        row["device_empty_bay_names"] = [name for name, _ in empty_bays]

        rules = getattr(self, "_carrier_install_rules", None) or []
        if not rules:
            return
        device_type = getattr(selected_device, "device_type", None)
        device_type_model = getattr(device_type, "model", "") or ""
        item_class = (item.get("entPhysicalClass") or "").strip().lower() if item else ""
        item_name = (item.get("entPhysicalName") or "").strip() if item else ""

        options = []
        for rule in rules:
            if rule.librenms_child_class.strip().lower() != item_class:
                continue
            dt_re = rule._compiled_device_type_pattern
            if dt_re is not None and not dt_re.fullmatch(device_type_model):
                continue
            child_re = rule._compiled_child_name_pattern
            if child_re is None or not child_re.fullmatch(item_name):
                continue
            bay_re = rule._compiled_bay_name_pattern
            if bay_re is None:
                continue
            mt = rule.carrier_module_type
            for bay_name, bay in empty_bays:
                if not bay_re.fullmatch(bay_name):
                    continue
                options.append(
                    {
                        "rule_id": rule.pk,
                        "module_type_id": mt.pk,
                        "module_type_name": str(mt),
                        "bay_id": bay.pk,
                        "bay_name": bay_name,
                    }
                )
        if options:
            row["carrier_install_options"] = options

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
        manufacturer_id = getattr(self, "_current_manufacturer_id", None)

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
            bay = self._lookup_exact_bay_mapping(name, phys_class, module_bays, exact_mappings, manufacturer_id)
            if bay:
                return bay

        # Use preloaded regex mappings.
        regex_mappings = getattr(self, "_regex_bay_mappings", None)
        if regex_mappings is None:
            from netbox_librenms_plugin.models import ModuleBayMapping

            regex_mappings = list(ModuleBayMapping.objects.filter(is_regex=True))

        # Regex pattern matching on all candidate names
        for name in all_candidates:
            bay = self._lookup_regex_bay_mapping(name, phys_class, module_bays, regex_mappings, manufacturer_id)
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
    def _filter_mappings_by_manufacturer(mappings, manufacturer_id):
        """
        Order mappings by manufacturer scoping priority:
        device-manufacturer match first, then vendor-agnostic (NULL), skip
        mappings scoped to a different manufacturer.
        """
        scoped = []
        global_ = []
        for m in mappings:
            mfr = getattr(m, "manufacturer_id", None)
            if mfr is None:
                global_.append(m)
            elif manufacturer_id and mfr == manufacturer_id:
                scoped.append(m)
        return scoped + global_

    @staticmethod
    def _lookup_exact_bay_mapping(name, phys_class, module_bays, exact_mappings, manufacturer_id=None):
        """
        Try exact ModuleBayMapping entries against a candidate name.

        Checks class-scoped mappings first, then falls back to classless mappings.
        Within each scope, prefers manufacturer-matched mappings over global ones
        and skips mappings scoped to a different manufacturer.
        Iterates the underlying dict scopes (supports both plain dicts and legacy
        ChainMap instances) so the returned bay is validated via _fpc_slot_matches.
        Returns the matched module bay or None.
        """
        maps = module_bays.maps if hasattr(module_bays, "maps") else [module_bays]
        scoped_mappings = BaseModuleTableView._filter_mappings_by_manufacturer(exact_mappings, manufacturer_id)
        if phys_class:
            mapping = next(
                (m for m in scoped_mappings if m.librenms_name == name and m.librenms_class == phys_class), None
            )
            if mapping and mapping.netbox_bay_name in module_bays:
                for scope_map in maps:
                    if mapping.netbox_bay_name in scope_map:
                        bay = scope_map[mapping.netbox_bay_name]
                        if BaseModuleTableView._fpc_slot_matches(name, bay):
                            return bay
        mapping = next((m for m in scoped_mappings if m.librenms_name == name and m.librenms_class == ""), None)
        if mapping and mapping.netbox_bay_name in module_bays:
            for scope_map in maps:
                if mapping.netbox_bay_name in scope_map:
                    bay = scope_map[mapping.netbox_bay_name]
                    if BaseModuleTableView._fpc_slot_matches(name, bay):
                        return bay
        return None

    @staticmethod
    def _lookup_regex_bay_mapping(name, phys_class, module_bays, regex_mappings, manufacturer_id=None):
        """
        Try regex ModuleBayMapping patterns against a name.

        ``regex_mappings`` is a pre-filtered list of is_regex=True ModuleBayMapping
        objects (passed in from the caller to avoid per-item DB queries).
        Manufacturer-scoped mappings (matching the device's manufacturer) take
        precedence over vendor-agnostic ones; mappings scoped to a different
        manufacturer are skipped.

        Returns matched module bay or None.
        """
        scoped_mappings = BaseModuleTableView._filter_mappings_by_manufacturer(regex_mappings, manufacturer_id)
        # Filter preloaded list by class (exact class match, then empty-class fallback)
        if phys_class:
            exact = [m for m in scoped_mappings if m.librenms_class == phys_class]
            fallback = [m for m in scoped_mappings if m.librenms_class == ""]
            candidates = exact + fallback
        else:
            candidates = [m for m in scoped_mappings if m.librenms_class == ""]

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
        #
        # Only walk through ENTITY-MIB containers (entPhysicalClass="container").
        # A modelless ancestor of any other class (e.g. class="module" with
        # model="N/A" — Cisco IOS-XR uses these for motherboard/slice/EZChip
        # scaffolding under a linecard) is hierarchical scaffolding, not a bay
        # position; walking past it would silently align all deeply nested
        # siblings to the same bay slot on the parent module.  Bail in that case.
        current_idx = item.get("entPhysicalContainedIn", 0)
        container_idx = None
        visited = set()
        while current_idx and current_idx in index_map and current_idx not in visited:
            visited.add(current_idx)
            ancestor = index_map[current_idx]
            model = (ancestor.get("entPhysicalModelName") or "").strip().lower()
            if model and model not in _GENERIC_CONTAINER_MODELS:
                # Found the parent with a real model; container_idx is the intermediate container
                break
            cls = ancestor.get("entPhysicalClass") or ""
            if cls != "container":
                return None
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

        # Pick patterns appropriate for the item's hardware class.  Without this,
        # a fan or power-supply lands in a module/line-card "Slot N" bay because
        # `Slot N` is in every chassis device — silently mis-installing the wrong
        # hardware class.  When NetBox lacks bays named for the item's class
        # (e.g. no "Fan Tray N" / "PSU N" defined on the device type), we
        # surface "No Bay" and let the user fix the model rather than guess.
        phys_class = (item.get("entPhysicalClass") or "").strip().lower()
        if phys_class == "fan":
            patterns = [f"Fan Tray {slot_num}", f"Fan {slot_num}", f"FT {slot_num}", f"FT{slot_num}"]
        elif phys_class == "powersupply":
            patterns = [
                f"Power Supply {slot_num}",
                f"PSU {slot_num}",
                f"PSU{slot_num}",
                f"PS{slot_num}",
                f"PEM {slot_num}",
                f"PM {slot_num}",
                f"PM{slot_num}",
            ]
        elif phys_class in {"module", "port", "iomodule", "cpmmodule", "mdamodule", "fabricmodule", "xiomodule"}:
            patterns = [f"SFP {slot_num}", f"Slot {slot_num}", f"Bay {slot_num}", f"Port {slot_num}"]
        else:
            return None

        for pattern in patterns:
            if pattern in module_bays:
                return module_bays[pattern]

        return None

    def _build_row(
        self,
        item,
        index_map,
        module_bays,
        module_types,
        depth=0,
        manufacturer=None,
        sibling_counts=None,
        scope_uninstalled=False,
        scope_preserved=False,
        scope_empty_installed_bays=False,
    ):
        """Build a single table row from a LibreNMS inventory item.

        ``scope_uninstalled`` (caller-provided) indicates the empty bay scope
        is empty because some ancestor's bay matched but has no installed
        module — the user can fix the row by installing the ancestor first
        (which materialises the bay templates) rather than by editing the
        device/module-type templates.

        ``scope_preserved`` indicates the scope was inherited from an
        ancestor that didn't match a bay (so the bays in scope don't
        accurately reflect the item's nesting level).  Used to suppress
        misleading mapping suggestions that would land deeply-nested
        items in chassis-level bays.

        ``scope_empty_installed_bays`` indicates the empty scope is because
        the nearest installed module ancestor's type has no bay templates
        defined.  Propagates through intermediate unmatched containers so
        deeply-nested items (e.g. SFPs nested under a transceiver carrier)
        still show "No Bay on Parent" rather than plain "No Bay"."""
        from netbox_librenms_plugin.utils import (
            has_nested_name_conflict,
            resolve_module_type,
        )

        model_name = (item.get("entPhysicalModelName", "") or "").strip()
        serial = (item.get("entPhysicalSerialNum", "") or "").strip()
        phys_class = item.get("entPhysicalClass", "")
        name = item.get("entPhysicalName", "") or "-"
        description = item.get("entPhysicalDescr", "") or ""

        # Detect "integrated child" SNMP duplicates (e.g. Nokia XIOM with a
        # fixed integrated MDA exposed as two ENTITY-MIB rows sharing the
        # same serial+model).  The child row is informational only — it has
        # no separate bay/type to match — so render it muted with no
        # actions and no warnings.
        integrating_ancestor = self._find_integrating_ancestor(item, index_map)
        if integrating_ancestor is not None:
            ancestor_name = (integrating_ancestor.get("entPhysicalName") or "").strip() or "parent module"
            return {
                "name": name,
                "model": model_name or "-",
                "serial": serial or "-",
                "description": description,
                "item_class": phys_class,
                "module_bay": "-",
                "module_type": "-",
                "status": "Integrated",
                "can_install": False,
                "module_bay_id": None,
                "module_type_id": None,
                "depth": depth,
                "ent_physical_index": item.get("entPhysicalIndex"),
                "has_installable_children": False,
                "integrated_in_name": ancestor_name,
                "integrated_in_index": integrating_ancestor.get("entPhysicalIndex"),
            }

        # Match to NetBox module bay
        matched_bay = self._match_module_bay(item, index_map, module_bays)

        # Match to NetBox module type (direct lookup, normalization fallback, then Generic fallback)
        norm_rules_type = getattr(self, "_norm_rules_type", None)
        generic_module_types = getattr(self, "_generic_module_types", None)
        matched_type = resolve_module_type(
            model_name,
            module_types,
            manufacturer=manufacturer,
            norm_rules=norm_rules_type,
            generic_fallback=generic_module_types,
        )

        # Determine status; override to "Name Conflict" when the matched module
        # type uses {module} in interface templates and has sibling bays (which
        # would produce duplicate interface names on install).
        status = self._determine_status(matched_bay, matched_type, serial)
        name_conflict_reason = ""
        if matched_type and matched_bay:
            name_conflict_reason = has_nested_name_conflict(matched_type, matched_bay, sibling_counts)
            if name_conflict_reason:
                status = "Name Conflict"

        row = {
            "name": name,
            "model": model_name or "-",
            "serial": serial or "-",
            "description": description,
            "item_class": phys_class,
            "module_bay": matched_bay.name if matched_bay else "-",
            "module_type": matched_type.model if matched_type else "-",
            "status": status,
            "can_install": False,
            "module_bay_id": matched_bay.pk if matched_bay else None,
            "module_type_id": matched_type.pk if matched_type else None,
            "depth": depth,
            "ent_physical_index": item.get("entPhysicalIndex"),
            "has_installable_children": False,
            "_source": item.get("_source", "main"),
        }
        if name_conflict_reason:
            row["name_conflict_reason"] = name_conflict_reason

        # Surface NetBox-model gaps that produced No Bay / No Type so the user
        # can fix the model rather than wonder why nothing matched.
        if status == "No Bay":
            suggestion = self._suggest_bay_mapping(item, module_bays, scope_preserved=scope_preserved)
            holder_hint = None
            if suggestion is None:
                holder_hint = self._build_holder_install_hint(
                    item,
                    phys_class,
                    getattr(self, "_current_device_bays", None),
                    scope_uninstalled=scope_uninstalled,
                    scope_empty_installed_bays=scope_empty_installed_bays,
                )
            row["model_warning"] = self._build_no_bay_warning(
                item, module_bays, suggestion, scope_uninstalled=scope_uninstalled, holder_hint=holder_hint
            )
            if suggestion:
                # Pre-fill manufacturer from current device so the new mapping
                # is auto-scoped to the vendor (the user can clear it in the
                # form to make it global).
                mfr_id = getattr(self, "_current_manufacturer_id", None)
                mfr_name = getattr(self, "_current_manufacturer_name", None)
                if mfr_id:
                    suggestion.setdefault("manufacturer", mfr_id)
                    if mfr_name:
                        suggestion.setdefault("manufacturer_name", mfr_name)
                row["model_suggestion"] = suggestion
            if holder_hint:
                row["holder_hint_present"] = True
            # Tag the root cause so the table can render a more specific status
            # badge: installed parent module has no bay templates at all.
            if scope_empty_installed_bays and not scope_uninstalled:
                row["no_bay_reason"] = "empty_parent_bays"
        elif status == "No Type":
            ambiguities = getattr(self, "_module_type_ambiguities", None)
            ambiguity_candidates = self._find_ambiguity_candidates(
                model_name, ambiguities, manufacturer=manufacturer, norm_rules=norm_rules_type
            )
            row["model_warning"] = self._build_no_type_warning(item, ambiguity_candidates=ambiguity_candidates)
            if ambiguity_candidates:
                row["module_type_ambiguity"] = [
                    {
                        "pk": mt.pk,
                        "model": mt.model,
                        "manufacturer": mt.manufacturer.name if getattr(mt, "manufacturer", None) else "",
                        "url": mt.get_absolute_url() if hasattr(mt, "get_absolute_url") else "",
                    }
                    for mt in ambiguity_candidates
                ]
                # Don't offer "Add Module Type" or auto-mapping when the user
                # already has too many — they need to disambiguate first.
            else:
                type_suggestion = self._suggest_type_mapping(item, matched_bay)
                if type_suggestion:
                    # Pre-fill manufacturer from current device so the new
                    # mapping is auto-scoped to the vendor (the user can clear
                    # it in the form to make it global).
                    mfr_id = getattr(self, "_current_manufacturer_id", None)
                    mfr_name = getattr(self, "_current_manufacturer_name", None)
                    if mfr_id:
                        type_suggestion.setdefault("manufacturer", mfr_id)
                        if mfr_name:
                            type_suggestion.setdefault("manufacturer_name", mfr_name)
                    row["type_suggestion"] = type_suggestion
                module_type_create = self._suggest_module_type_create(item, manufacturer)
                if module_type_create:
                    row["module_type_create"] = module_type_create

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

        # Re-apply "Name Conflict" last — _apply_installed_status may have
        # overwritten it with "Installed" / "Serial Mismatch" / "Type Mismatch".
        if name_conflict_reason:
            row["status"] = "Name Conflict"

        return row

    @staticmethod
    def _apply_installed_status(row, installed, matched_type, serial):
        """Set status and action flags when a module is already installed."""
        if matched_type is not None and installed.module_type_id != matched_type.pk:
            row["status"] = "Type Mismatch"
            row["can_replace"] = True
        elif matched_type is not None:
            # Normalize both serials: treat None, empty, whitespace, and placeholder values as absent
            nb_serial = (installed.serial or "").strip()
            if nb_serial.lower() in _PLACEHOLDER_VALUES:
                nb_serial = ""
            lnms_serial = serial if serial.lower() not in _PLACEHOLDER_VALUES else ""
            if lnms_serial and lnms_serial != nb_serial:
                row["status"] = "Serial Mismatch"
                row["can_update_serial"] = True
                row["can_replace"] = True
            else:
                row["status"] = "Installed"
        else:
            row["status"] = "No Type"

    def _determine_status(self, matched_bay, matched_type, serial):
        """Determine the sync status for an inventory item."""
        if matched_bay and matched_type:
            return "Matched"
        if not matched_bay:
            return "No Bay"
        if not matched_type:
            return "No Type"
        return "Unmatched"

    @staticmethod
    def _derive_bay_template_suggestion(item):
        """
        Derive a sensible pre-fill for the Add Bay Template modal from a
        LibreNMS inventory item dict.

        Returns a dict with keys ``name``, ``position`` and ``label``.

        - ``name``: the LibreNMS item name as-is (the user can edit before
          submit).  Falls back to a class-derived placeholder when the name
          is empty.
        - ``position``: the trailing digit/letter token of the name when
          present (e.g. "Slot 1" -> "1", "CMA-A" -> "A").
        - ``label``: the LibreNMS description (entPhysicalDescr) trimmed,
          which is usually a richer human-readable label than the bay name.
        """
        raw_name = (item.get("entPhysicalName") or "").strip()
        descr = (item.get("entPhysicalDescr") or "").strip()
        phys_class = (item.get("entPhysicalClass") or "").strip().lower()

        position = ""
        if raw_name:
            m = re.search(r"(\d+|[A-Za-z]+)$", raw_name)
            if m:
                position = m.group(0)

        if not raw_name:
            class_placeholders = {
                "fan": "Fan Tray 1",
                "powersupply": "Power Supply 1",
                "port": "Port 1",
            }
            raw_name = class_placeholders.get(phys_class, "Slot 1")
            position = re.search(r"\d+$", raw_name).group(0)

        return {
            "name": raw_name,
            "position": position,
            "label": descr,
            # Original LibreNMS values — passed through to the Add Bay Template
            # modal so it can offer to also create a matching ModuleBayMapping
            # when the user picks a NetBox bay name that differs from the
            # LibreNMS one (case differences, vendor naming, etc.).  Bay
            # matching is exact + case-sensitive dict lookup, so without a
            # mapping the row would still show "No Bay" after the bay is added.
            "librenms_name": (item.get("entPhysicalName") or "").strip(),
            "librenms_class": (item.get("entPhysicalClass") or "").strip(),
        }

    @staticmethod
    def _build_no_bay_warning(item, module_bays, suggestion=None, scope_uninstalled=False, holder_hint=None):
        """
        Hint the user toward the missing piece of the NetBox model when bay
        matching produces "No Bay".

        Distinguishes:
          - empty scope due to an uninstalled ancestor -> install the ancestor
            module first (its bay templates will then be available)
          - empty scope due to no bay templates -> add bay templates to the
            parent module/device type
          - non-empty scope, hardware-class mismatch (fan / powerSupply) ->
            add the appropriate class bay templates
          - non-empty scope, generic module/port -> add Slot/SFP/Bay/Port
            templates or a ModuleBayMapping

        When a `suggestion` dict is provided (from `_suggest_bay_mapping`),
        appends the proposed regex/target so the user sees a concrete fix
        alongside the diagnosis.
        """
        phys_class = (item.get("entPhysicalClass") or "").strip().lower()
        class_hints = {
            "fan": "Fan Tray N or Fan N",
            "powersupply": "Power Supply N, PSU N, or PEM N",
        }
        module_classes = {"module", "port", "iomodule", "cpmmodule", "mdamodule", "fabricmodule", "xiomodule"}

        if phys_class in class_hints:
            class_part = f"No bay defined for class={phys_class}; add {class_hints[phys_class]} bay templates"
        elif phys_class in module_classes:
            class_part = "No matching bay; add Slot N / SFP N / Bay N / Port N bay templates"
        else:
            class_part = "No matching bay; verify NetBox bay templates"

        if not module_bays:
            if scope_uninstalled:
                base = (
                    "An ancestor module bay matched but has no module installed in NetBox; "
                    "install the parent module first so its bay templates become available, "
                    "then refresh this tab."
                )
            else:
                base = (
                    f"Parent module type has no bay templates defined in NetBox. {class_part} "
                    "to the parent module/device type, or add a ModuleBayMapping."
                )
        else:
            base = f"{class_part} on the NetBox device or parent module type, or add a ModuleBayMapping."

        if suggestion:
            base += (
                f" Suggested mapping: librenms_name='{suggestion['librenms_name']}' "
                f"(regex), librenms_class='{suggestion.get('librenms_class') or ''}', "
                f"netbox_bay_name='{suggestion['netbox_bay_name']}' "
                f"— would map '{suggestion['example_item']}' to '{suggestion['example_bay']}' "
                "and any sibling with the same trailing-number pattern."
            )
        if holder_hint:
            base += f" {holder_hint}"
        return base

    @staticmethod
    def _build_holder_install_hint(
        item, phys_class, device_bays, scope_uninstalled=False, scope_empty_installed_bays=False
    ):
        """
        Vendor-agnostic hint that surfaces the "missing holder/carrier module" pattern.

        Some chassis (e.g. Nokia 7750 SR-s with CMA controller carriers, mezzanine
        carriers, line-card cassettes) expose a holder module bay at the chassis
        level. Until the holder ModuleType is installed in that bay, NetBox does
        not expose the holder's nested child bays — so LibreNMS-reported children
        (CPMs, MDAs, mezzanines) appear with no matching bay.

        Triggers only when:
          * the unmatched item is a module-class component (not fan/PSU);
          * we are not already showing a more specific hint
            (scope_uninstalled / scope_empty_installed_bays);
          * the device has at least one EMPTY device-level bay.

        The hint is informational only — it lists the empty bay names so the
        user can recognise the pattern and install the appropriate holder
        ModuleType themselves. Doing so will expose nested child bays which
        will then match LibreNMS-reported children automatically.
        """
        if scope_uninstalled or scope_empty_installed_bays:
            return None
        # Only items that could plausibly be a "card inside a carrier" — i.e.
        # named module-class items at the top of the SNMP tree. Excludes plain
        # 'port' (transceivers, where the right fix is line-card bay templates,
        # not a chassis-level carrier) and items whose name encodes a path
        # like '1/1/c1' (clearly nested, not a top-level holder child).
        holder_child_classes = {
            "module",
            "iomodule",
            "cpmmodule",
            "mdamodule",
            "fabricmodule",
            "xiomodule",
            "container",
        }
        if (phys_class or "").strip().lower() not in holder_child_classes:
            return None
        item_name = (item.get("entPhysicalName") or "").strip() if item else ""
        if "/" in item_name:
            return None
        if not device_bays:
            return None
        empty_names = sorted(name for name, bay in device_bays.items() if not getattr(bay, "installed_module", None))
        if not empty_names:
            return None
        # Cap the listed names to keep the message readable on wide chassis.
        shown = empty_names[:5]
        more = "" if len(empty_names) <= 5 else f" (+{len(empty_names) - 5} more)"
        names_text = ", ".join(f"'{n}'" for n in shown) + more
        return (
            f"Tip: device has empty bay(s) [{names_text}]. "
            "Some chassis require a holder/carrier module (e.g. controller-card "
            "carrier, mezzanine carrier, line-card cassette) to be installed in "
            "such a bay before nested child bays become available — install the "
            "appropriate ModuleType if applicable, then refresh."
        )

    @staticmethod
    def _suggest_bay_mapping(item, module_bays, scope_preserved=False):
        """
        Suggest a ModuleBayMapping that would resolve a No Bay row.

        Heuristic: when the item's name ends with a number and a bay in scope
        ends with the same number, propose a regex that captures the trailing
        number and maps it to the corresponding bay.  Example: item "0/0" with
        bay "Slot 0" in scope yields ``^0/(\\d+)$`` -> ``Slot \\1``, which
        generalises to all sibling slots without needing one mapping per slot.

        Suppressed in two cases to avoid wrong suggestions:

        * **scope_preserved=True**: the bay scope was inherited from a
          higher ancestor that didn't match a bay, so the item is at a
          deeper hierarchical level than ``module_bays`` represents.
          Suggesting "0/0/0 -> Slot 0" when "Slot 0" is actually a chassis
          line-card bay would invite installing a transceiver into a
          line-card slot.

        * **Class-bay mismatch**: the item's hardware class disagrees with
          every bay in scope.  A fan only proposes mappings to Fan/Fan
          Tray/FT named bays; a power supply only to Power Supply / PSU /
          PEM / PM bays.  Module/port classes accept Slot/SFP/Bay/Port.

        Returns a dict with the suggested mapping fields, or None when no
        plausible mapping can be derived.
        """
        if scope_preserved:
            return None
        item_name = (item.get("entPhysicalName") or "").strip()
        if not item_name or not module_bays:
            return None

        # Match either trailing digits (e.g. "Slot 1", "0/0") or a trailing
        # alphabetic token (e.g. "Slot A", "CMA-A") so chassis that label
        # carrier-card slots with letters get a mapping suggestion too.
        m = re.search(r"(\d+|[A-Za-z]+)$", item_name)
        if not m:
            return None
        item_trail = m.group(0)
        item_prefix = item_name[: m.start()]
        trail_is_digits = item_trail.isdigit()
        item_class = (item.get("entPhysicalClass") or "").strip()
        # Description-based fallback: when the item description encodes a
        # class+slot hint like "MIC: ... @ 0/0/*" (Juniper), try mapping to a
        # bay named "<CLASS> <slot>" even when the LibreNMS name is just a
        # model number with no positional info. Runs before the class filter
        # so 'container' MICs/PICs/etc. still get a suggestion.
        descr_suggestion = BaseModuleTableView._suggest_bay_mapping_from_descr(item, module_bays, item_name, item_class)

        # Filter bays by hardware class so transceivers don't propose chassis
        # line-card bays as targets, fans don't propose Slot N, etc.
        class_keywords = {
            "fan": ("fan", "ft"),
            "powersupply": ("psu", "power supply", "pem", "pm"),
        }
        module_classes = {"module", "port", "iomodule", "cpmmodule", "mdamodule", "fabricmodule", "xiomodule"}
        phys_class = item_class.lower()
        if phys_class in class_keywords:
            keywords = class_keywords[phys_class]
            candidate_names = [n for n in module_bays if any(k in n.lower() for k in keywords)]
        elif phys_class in module_classes:
            candidate_names = list(module_bays)
        else:
            return descr_suggestion

        # Tighten matching for module-class items with an alphabetic prefix
        # (e.g. "Sfm 1", "Card 1"): require the candidate bay's alphabetic
        # prefix tokens to share at least one token with the item's prefix,
        # otherwise we end up suggesting "Sfm 1" -> "Card 1" just because both
        # end in "1". Items with no alphabetic prefix (e.g. "0/0", "1/1/1")
        # keep the previous numeric-only behaviour. Skipped for fan/powerSupply
        # because class_keywords already curates the candidate set for those.
        # Also skipped for trailing-letter items where the trail itself ("A",
        # "B") is the discriminator and prefix tokens (e.g. "Slot" vs "CPM")
        # frequently disagree across vendor naming conventions.
        require_token_overlap = phys_class in module_classes and trail_is_digits
        item_alpha_tokens = {t.lower() for t in re.findall(r"[A-Za-z]+", item_prefix)}

        candidate_bay = None
        bay_prefix = None

        def _bay_compatible(bay_name: str) -> bool:
            if not require_token_overlap or not item_alpha_tokens:
                return True
            bay_alpha_tokens = {t.lower() for t in re.findall(r"[A-Za-z]+", bay_name)}
            return bool(item_alpha_tokens & bay_alpha_tokens)

        trail_pattern = r"\d+$" if trail_is_digits else r"[A-Za-z]+$"
        for bay_name in candidate_names:
            bm = re.search(trail_pattern, bay_name)
            if bm and bm.group(0) == item_trail and _bay_compatible(bay_name):
                candidate_bay = bay_name
                bay_prefix = bay_name[: bm.start()]
                break

        if candidate_bay is None:
            if descr_suggestion is not None:
                return descr_suggestion
            # Final fallback: same trail-heuristic but anchored on the
            # description. Handles entries whose entPhysicalName is the model
            # string (no positional info) while entPhysicalDescr carries the
            # human-readable position — e.g. Juniper fan trays where
            # name='JNP10008-FTC2' and descr='Fan Tray Controller 0' should
            # map to bay 'Fan Tray 0'. Mapping evaluation already considers
            # entPhysicalDescr (see _match_module_bay candidate_names), so a
            # descr-anchored regex resolves at lookup time.
            return BaseModuleTableView._suggest_bay_mapping_from_descr_trail(
                item, candidate_names, item_name, item_class
            )

        capture_group = r"(\d+)" if trail_is_digits else r"([A-Za-z]+)"
        librenms_pattern = "^" + re.escape(item_prefix) + capture_group + "$"
        netbox_target = bay_prefix + r"\1"

        return {
            "librenms_name": librenms_pattern,
            "netbox_bay_name": netbox_target,
            "is_regex": True,
            "librenms_class": item_class,
            "description": (
                f"Auto-suggested from device modules tab: maps LibreNMS '{item_name}' "
                f"and similar names sharing the same prefix to NetBox bay '{candidate_bay}'."
            ),
            "example_item": item_name,
            "example_bay": candidate_bay,
        }

    @staticmethod
    def _suggest_bay_mapping_from_descr(item, module_bays, item_name, item_class):
        """
        Derive a ModuleBayMapping suggestion from the item's description when
        the description encodes a class+slot hint like ``"MIC: ... @ 0/0/*"``
        (Juniper) — useful when the LibreNMS name is just a model number with
        no positional info that the name-based heuristic could latch onto.

        Returns the suggestion dict (with a regex matching the description and
        a target bay name like ``"MIC \\1"``) or None when the description
        doesn't fit the pattern or the implied bay isn't present in scope.
        """
        descr = (item.get("entPhysicalDescr") or "").strip()
        if not descr or not module_bays:
            return None
        dm = re.match(r"^([A-Z][A-Za-z0-9_]{0,15}):\s+.*@\s*(\d+)(?:/|\s|$)", descr)
        if not dm:
            return None
        descr_class = dm.group(1)
        descr_slot = dm.group(2)
        expected_bay = f"{descr_class} {descr_slot}"
        if expected_bay not in module_bays:
            return None
        librenms_pattern = "^" + re.escape(descr_class) + r":\s+.*@\s*(\d+)(?:/.*)?$"
        return {
            "librenms_name": librenms_pattern,
            "netbox_bay_name": f"{descr_class} \\1",
            "is_regex": True,
            "librenms_class": item_class,
            "description": (
                f"Auto-suggested from device modules tab: maps LibreNMS items "
                f"whose description starts with '{descr_class}:' and includes "
                f"'@ <slot>/...' to NetBox bay '{descr_class} <slot>' "
                f"(e.g. '{descr}' → '{expected_bay}')."
            ),
            "example_item": item_name,
            "example_bay": expected_bay,
        }

    @staticmethod
    def _suggest_bay_mapping_from_descr_trail(item, candidate_names, item_name, item_class):
        """
        Last-chance heuristic: derive a ModuleBayMapping suggestion by applying
        the trailing-number/letter pattern to ``entPhysicalDescr`` instead of
        ``entPhysicalName``.

        Useful for vendors that report the model string in entPhysicalName
        and the human-readable position in entPhysicalDescr (e.g. Juniper
        fan trays: name='JNP10008-FTC2', descr='Fan Tray Controller 0').
        Mapping lookup already considers entPhysicalDescr (see
        ``_match_module_bay`` candidate_names), so a descr-anchored regex
        such as ``^Fan Tray Controller (\\d+)$`` will resolve correctly at
        match time.

        ``candidate_names`` should be the bay-name list already filtered by
        hardware class (the same list ``_suggest_bay_mapping`` built for the
        name-based pass) so transceivers don't propose chassis line-card
        bays as targets, fans don't propose Slot N, etc.

        Returns the suggestion dict, or None when no plausible mapping
        can be derived (no descr, no trailing token, descr same as name,
        or no bay shares the trailing token).
        """
        descr = (item.get("entPhysicalDescr") or "").strip()
        if not descr or not candidate_names:
            return None
        # Skip when descr is identical to the name we already tried — the
        # name-based heuristic was authoritative for that string.
        if descr == item_name:
            return None
        m = re.search(r"(\d+|[A-Za-z]+)$", descr)
        if not m:
            return None
        descr_trail = m.group(0)
        descr_prefix = descr[: m.start()]
        trail_is_digits = descr_trail.isdigit()
        trail_pattern = r"\d+$" if trail_is_digits else r"[A-Za-z]+$"
        candidate_bay = None
        bay_prefix = None
        for bay_name in candidate_names:
            bm = re.search(trail_pattern, bay_name)
            if bm and bm.group(0) == descr_trail:
                candidate_bay = bay_name
                bay_prefix = bay_name[: bm.start()]
                break
        if candidate_bay is None:
            return None
        capture_group = r"(\d+)" if trail_is_digits else r"([A-Za-z]+)"
        librenms_pattern = "^" + re.escape(descr_prefix) + capture_group + "$"
        netbox_target = bay_prefix + r"\1"
        return {
            "librenms_name": librenms_pattern,
            "netbox_bay_name": netbox_target,
            "is_regex": True,
            "librenms_class": item_class,
            "description": (
                f"Auto-suggested from device modules tab: maps LibreNMS items "
                f"whose description matches '{descr_prefix}<N>' to NetBox bay "
                f"'{bay_prefix}<N>' (e.g. '{descr}' → '{candidate_bay}'). "
                f"Triggered because entPhysicalName ('{item_name}') carries "
                f"no positional token but entPhysicalDescr does."
            ),
            "example_item": descr,
            "example_bay": candidate_bay,
        }

    @staticmethod
    def _suggest_type_mapping(item, matched_bay):
        """
        Suggest a ModuleTypeMapping that would resolve a No Type row.

        Uses the LibreNMS model name as ``librenms_model`` and builds a helpful
        ``description`` from the physical description and bay context so the
        create form arrives pre-filled for the user.  The user still needs to
        select or create the matching NetBox ModuleType.

        Returns None when the model name is blank — no meaningful mapping can
        be created without at least a model name to key on.
        """
        model = (item.get("entPhysicalModelName") or "").strip()
        if not model:
            return None

        parts = [f"Auto-suggested: maps LibreNMS model '{model}'"]
        phys_descr = (item.get("entPhysicalDescr") or "").strip()
        if phys_descr:
            parts.append(f"described as '{phys_descr}'")
        if matched_bay:
            parts.append(f"fitted in bay '{matched_bay.name}'")
        parts.append("to a NetBox ModuleType.")
        description = " ".join(parts)

        return {
            "librenms_model": model,
            "description": description,
        }

    @staticmethod
    def _suggest_module_type_create(item, manufacturer):
        """
        Suggest pre-fill values for NetBox's native ModuleType create form.

        Returns a dict suitable for building a querystring against
        ``/dcim/module-types/add/`` so the user can create the missing
        ModuleType in one click instead of opening the form blank.

        Pre-filled fields (all derived from the LibreNMS ENTITY-MIB row):
          * ``manufacturer``  — PK of the device's manufacturer (when known)
          * ``model``         — entPhysicalModelName (truncated to 100 chars)
          * ``part_number``   — entPhysicalModelName (truncated to 50 chars)
          * ``description``   — entPhysicalDescr (truncated to 200 chars)
          * ``comments``      — full entPhysicalDescr when it had to be
                                truncated for ``description``

        Returns None when no model name was reported — without a model name
        there's nothing meaningful to pre-fill and the row can't be made
        installable by adding a type either.
        """
        model = (item.get("entPhysicalModelName") or "").strip()
        if not model:
            return None

        suggestion = {
            "model": model[:100],
            "part_number": model[:50],
        }
        if manufacturer is not None:
            suggestion["manufacturer"] = manufacturer.pk

        phys_descr = (item.get("entPhysicalDescr") or "").strip()
        if phys_descr:
            suggestion["description"] = phys_descr[:200]
            if len(phys_descr) > 200:
                suggestion["comments"] = phys_descr

        return suggestion

    @staticmethod
    def _build_no_type_warning(item, ambiguity_candidates=None):
        """Hint when LibreNMS reports a model that NetBox doesn't define.

        When ``ambiguity_candidates`` is a non-empty list of ModuleType
        instances, the warning explains that NetBox has *multiple* types
        sharing the same model/part_number string, so the plugin refuses to
        guess.  The message names each conflicting ``manufacturer / model``
        pair so the user can resolve the data issue in NetBox itself.
        """
        model = (item.get("entPhysicalModelName") or "").strip()
        if not model:
            return "LibreNMS did not report a model name for this item; cannot match to a NetBox ModuleType."
        if ambiguity_candidates:
            names = ", ".join(
                f"{(mt.manufacturer.name if getattr(mt, 'manufacturer', None) else '?')} / {mt.model}"
                for mt in ambiguity_candidates
            )
            return (
                f"Cannot match LibreNMS model '{model}': NetBox has "
                f"{len(ambiguity_candidates)} ModuleTypes sharing this model or part_number "
                f"({names}). Resolve the duplicate in NetBox (delete one, "
                "rename, or set distinct part_numbers) so the lookup becomes unambiguous."
            )
        return (
            f"No NetBox ModuleType matches '{model}'. Create a ModuleType for this "
            "model (or add a ModuleTypeMapping) so the row becomes installable."
        )

    @staticmethod
    def _find_ambiguity_candidates(model_name, ambiguities, manufacturer=None, norm_rules=None):
        """
        Return the list of ModuleType candidates that collide for *model_name*.

        Checks both the raw LibreNMS string and the normalized form (so a
        Nokia ``3HE18883AARB01`` whose normalized key ``3HE18883AA`` is
        ambiguous is still detected).  Returns an empty list when there is
        no collision (or when *ambiguities* is falsy).
        """
        from netbox_librenms_plugin.utils import apply_normalization_rules

        if not (model_name and ambiguities):
            return []
        if model_name in ambiguities:
            return list(ambiguities[model_name])
        normalized = apply_normalization_rules(
            model_name, "module_type", manufacturer=manufacturer, preloaded_rules=norm_rules
        )
        if normalized != model_name and normalized in ambiguities:
            return list(ambiguities[normalized])
        return []

    @staticmethod
    def _find_integrating_ancestor(item, index_map):
        """
        Detect the "integrated child" SNMP pattern (e.g. Nokia XIOM hosting a
        single fixed MDA).

        Some vendors expose the same physical card as two ENTITY-MIB rows —
        a parent module and a child module — that share both
        ``entPhysicalSerialNum`` and ``entPhysicalModelName``.  Returns the
        ancestor item that matches *item*'s serial+model (so the caller can
        present the row as ``Integrated in <parent>`` instead of trying to
        find a bay/type for a card that does not physically exist as a
        separate entity).

        Returns None when no such ancestor exists, when serial/model are
        empty/placeholder values, or when *item* is not itself a
        module-class entry (we don't dedupe chassis / PSU / fan rows
        because shared serials there usually indicate a real vendor data
        bug we want to surface).
        """
        item_class = (item.get("entPhysicalClass") or "").strip()
        if item_class not in INVENTORY_CLASSES or item_class in {"container", "powerSupply", "fan"}:
            return None
        item_serial = (item.get("entPhysicalSerialNum") or "").strip()
        if not item_serial or item_serial.lower() in _PLACEHOLDER_VALUES:
            return None
        item_model = (item.get("entPhysicalModelName") or "").strip().lower()
        if not item_model or item_model in _PLACEHOLDER_VALUES:
            return None

        visited: set = set()
        current_idx = item.get("entPhysicalContainedIn", 0)
        while current_idx and current_idx not in visited:
            visited.add(current_idx)
            ancestor = index_map.get(current_idx)
            if ancestor is None:
                return None
            anc_class = (ancestor.get("entPhysicalClass") or "").strip()
            # Stop at chassis — never dedupe against the chassis itself.
            if anc_class == "chassis":
                return None
            if anc_class in INVENTORY_CLASSES and anc_class not in {"container", "powerSupply", "fan"}:
                anc_serial = (ancestor.get("entPhysicalSerialNum") or "").strip()
                anc_model = (ancestor.get("entPhysicalModelName") or "").strip().lower()
                if (
                    anc_serial
                    and anc_serial.lower() not in _PLACEHOLDER_VALUES
                    and anc_serial == item_serial
                    and anc_model
                    and anc_model == item_model
                ):
                    return ancestor
            current_idx = ancestor.get("entPhysicalContainedIn", 0)
        return None

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

        # Group conflict modules by serial
        conflicts_by_serial: dict = {}
        for conflict in conflicts:
            conflicts_by_serial.setdefault(conflict.serial, []).append(conflict)

        for serial, rows in serial_rows.items():
            modules = conflicts_by_serial.get(serial, [])
            for row in rows:
                installed_id = row.get("installed_module_id")
                # Exclude the module already in the current bay
                candidates = [m for m in modules if not (installed_id and m.pk == installed_id)]
                if len(candidates) == 1:
                    row["serial_conflict_module"] = candidates[0]
                    row["can_move_from"] = True
                elif len(candidates) > 1:
                    row["serial_conflict_ambiguous"] = True
