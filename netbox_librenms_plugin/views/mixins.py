from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from utilities.permissions import get_permission_for_model

from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN, PERM_VIEW_PLUGIN
from netbox_librenms_plugin.librenms_api import LibreNMSAPI


def _get_safe_redirect_url(request):
    """
    Return a validated redirect URL from the HTTP Referer header.

    Validates the Referer against allowed hosts and schemes to prevent
    open-redirect attacks. Falls back to the current request path or "/".
    """
    referrer = request.META.get("HTTP_REFERER")
    if referrer and url_has_allowed_host_and_scheme(
        referrer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return referrer
    return getattr(request, "path", "/")


class LibreNMSPermissionMixin(PermissionRequiredMixin):
    """
    Mixin for views requiring LibreNMS plugin permissions.

    All plugin views require 'view_librenmssettings' to access the page.
    Write actions require 'change_librenmssettings' plus any relevant
    NetBox object permissions.
    """

    permission_required = PERM_VIEW_PLUGIN

    def has_write_permission(self):
        """Check if user can perform write actions."""
        return self.request.user.has_perm(PERM_CHANGE_PLUGIN)

    def require_write_permission(self, error_message=None):
        """
        Check write permission and return error response if denied.

        Handles both HTMX and regular requests appropriately:
        - HTMX: Returns HX-Redirect to referrer with toast message
        - Regular: Returns redirect to referrer with flash message

        Returns:
            None if permitted, or appropriate response if denied
        """
        if not self.has_write_permission():
            msg = error_message or "You do not have permission to perform this action."
            messages.error(self.request, msg)

            referrer = _get_safe_redirect_url(self.request)

            # Check if this is an HTMX request
            if self.request.headers.get("HX-Request"):
                return HttpResponse("", headers={"HX-Redirect": referrer})

            # referrer is safe: validated by _get_safe_redirect_url via url_has_allowed_host_and_scheme
            return redirect(referrer)
        return None

    def require_write_permission_json(self, error_message=None):
        """
        Check write permission and return JSON error response if denied.

        Use this method for AJAX/HTMX endpoints that return JsonResponse.
        Does not set flash messages since JSON clients handle errors differently.

        Returns:
            None if permitted, or JsonResponse with 403 status if denied
        """
        from django.http import JsonResponse

        if not self.has_write_permission():
            msg = error_message or "You do not have permission to perform this action."
            return JsonResponse({"error": msg}, status=403)
        return None


class NetBoxObjectPermissionMixin:
    """
    Mixin for views requiring specific NetBox object permissions.

    Define required_object_permissions as a dict mapping HTTP methods
    to lists of (action, model) tuples.

    Example:
        required_object_permissions = {
            'POST': [
                ('add', Interface),
                ('change', Interface),
            ],
        }
    """

    required_object_permissions = {}

    def check_object_permissions(self, method):
        """
        Check all required object permissions for the given HTTP method.

        Args:
            method: HTTP method (GET, POST, etc.)

        Returns:
            tuple: (has_all: bool, missing: list[str])
        """
        requirements = self.required_object_permissions.get(method, [])
        missing = []

        for action, model in requirements:
            perm = get_permission_for_model(model, action)
            if not self.request.user.has_perm(perm):
                missing.append(perm)

        return (len(missing) == 0, missing)

    def require_object_permissions(self, method):
        """
        Require all object permissions for the method, returning error response if denied.

        Handles both HTMX and regular requests appropriately:
        - HTMX: Returns HX-Redirect to referrer with flash message
        - Regular: Returns redirect to referrer with flash message

        Returns:
            None if permitted, or appropriate response if denied
        """
        has_perms, missing = self.check_object_permissions(method)
        if not has_perms:
            missing_str = ", ".join(missing)
            msg = f"Missing permissions: {missing_str}"
            messages.error(self.request, msg)

            referrer = _get_safe_redirect_url(self.request)

            # Check if this is an HTMX request
            if self.request.headers.get("HX-Request"):
                return HttpResponse("", headers={"HX-Redirect": referrer})

            # referrer is safe: validated by _get_safe_redirect_url via url_has_allowed_host_and_scheme
            return redirect(referrer)
        return None

    def require_object_permissions_json(self, method):
        """
        Require all object permissions for the method, returning JSON error if denied.

        Use this method for AJAX/HTMX endpoints that return JsonResponse.
        Does not set flash messages since JSON clients handle errors differently.

        Returns:
            None if permitted, or JsonResponse with 403 status if denied
        """
        from django.http import JsonResponse

        has_perms, missing = self.check_object_permissions(method)
        if not has_perms:
            missing_str = ", ".join(missing)
            return JsonResponse({"error": f"Missing permissions: {missing_str}"}, status=403)
        return None

    def require_all_permissions(self, method="POST"):
        """
        Check both plugin write and NetBox object permissions.

        Combines require_write_permission() and require_object_permissions()
        into a single call. Handles HTMX and regular requests.

        Returns:
            None if permitted, or appropriate error response if denied
        """
        if error := self.require_write_permission():
            return error
        return self.require_object_permissions(method)

    def require_all_permissions_json(self, method="POST"):
        """
        Check both plugin write and NetBox object permissions, returning JSON errors.

        Combines require_write_permission_json() and require_object_permissions_json()
        into a single call for JSON/AJAX endpoints.

        Returns:
            None if permitted, or JsonResponse with 403 status if denied
        """
        if error := self.require_write_permission_json():
            return error
        return self.require_object_permissions_json(method)


class LibreNMSAPIMixin:
    """
    A mixin class that provides access to the LibreNMS API.

    This mixin initializes a LibreNMSAPI instance and provides a property
    to access it. It's designed to be used with other view classes that
    need to interact with the LibreNMS API.

    Attributes:
        _librenms_api (LibreNMSAPI): An instance of the LibreNMSAPI class.

    Properties:
        librenms_api (LibreNMSAPI): A property that returns the LibreNMSAPI instance,
                                    creating it if it doesn't exist.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._librenms_api = None

    @property
    def librenms_api(self):
        """
        Get or create an instance of LibreNMSAPI.

        This property ensures that only one instance of LibreNMSAPI is created
        and reused for subsequent calls. The API instance will use the currently
        selected server from settings.

        Returns:
            LibreNMSAPI: An instance of the LibreNMSAPI class.
        """
        if self._librenms_api is None:
            # The LibreNMSAPI will automatically use the selected server
            self._librenms_api = LibreNMSAPI()
        return self._librenms_api

    def get_server_info(self):
        """
        Get information about the currently active LibreNMS server.

        Returns:
            dict: Server information including display name and URL
        """
        try:
            # Get the current server key
            server_key = self.librenms_api.server_key

            # Try to get multi-server configuration
            from netbox.plugins import get_plugin_config

            servers_config = get_plugin_config("netbox_librenms_plugin", "servers")

            if servers_config and isinstance(servers_config, dict) and server_key in servers_config:
                # Multi-server configuration
                config = servers_config[server_key]
                return {
                    "display_name": config.get("display_name", server_key),
                    "url": config["librenms_url"],
                    "is_legacy": False,
                    "server_key": server_key,
                }
            else:
                # Legacy configuration
                legacy_url = get_plugin_config("netbox_librenms_plugin", "librenms_url")
                return {
                    "display_name": "Default Server",
                    "url": legacy_url or "Not configured",
                    "is_legacy": True,
                    "server_key": "default",
                }
        except (KeyError, AttributeError, ImportError):
            return {
                "display_name": "Unknown Server",
                "url": "Configuration error",
                "is_legacy": True,
                "server_key": "unknown",
            }

    def get_context_data(self, **kwargs):
        """Add server info to context for all views using this mixin."""
        try:
            context = super().get_context_data(**kwargs)
        except AttributeError:
            context = kwargs
        context["librenms_server_info"] = self.get_server_info()
        return context


class CacheMixin:
    """
    A mixin class that provides caching functionality.
    """

    def get_cache_key(self, obj, data_type="ports", server_key=None):
        """
        Get the cache key for the object.

        Args:
            obj: The object to cache data for
            data_type: Type of data being cached ('ports', 'links', 'inventory', etc.)
            server_key: Optional LibreNMS server key for namespacing per-server data
        """
        model_name = obj._meta.model_name
        base = f"librenms_{data_type}_{model_name}_{obj.pk}"
        if server_key:
            return f"{base}_{server_key}"
        return base

    def get_last_fetched_key(self, obj, data_type="ports", server_key=None):
        """
        Get the cache key for the last fetched time of the object.
        """
        model_name = obj._meta.model_name
        base = f"librenms_{data_type}_last_fetched_{model_name}_{obj.pk}"
        if server_key:
            return f"{base}_{server_key}"
        return base

    def get_vlan_overrides_key(self, obj, server_key=None):
        """
        Get the cache key for user VLAN group override selections.

        Stores a {vid_str: group_id_str} map so that "apply to all" VLAN
        group choices persist across table pages. Including server_key scopes
        overrides per-server to avoid leakage when multiple servers are configured.
        """
        model_name = obj._meta.model_name
        if server_key:
            return f"librenms_vlan_group_overrides_{model_name}_{obj.pk}_{server_key}"
        return f"librenms_vlan_group_overrides_{model_name}_{obj.pk}"


class VlanAssignmentMixin:
    """
    Mixin providing VLAN assignment utilities for views.

    Provides methods for:
    - Getting relevant VLAN groups for a device based on scope hierarchy
    - Building lookup maps for VLAN matching
    - Selecting the most specific VLAN group based on device context
    - Finding VLANs by VID within a specific group
    - Updating interface VLAN assignments
    """

    def get_vlan_groups_for_device(self, device):
        """
        Get all VLAN groups relevant to this device.

        Searches for VLAN groups scoped to:
        - Site: The device's assigned site
        - Location: The device's location and all parent locations
        - Region: The device's site's region and all parent regions
        - Site Group: The device's site's group and all parent site groups
        - Rack: The device's rack
        - Global: VLAN groups with no scope

        Returns:
            List of VLANGroup objects, deduplicated and sorted by name
        """
        from dcim.models import Location, Rack, Region, Site, SiteGroup
        from ipam.models import VLANGroup

        groups = set()

        # Site-scoped VLAN groups
        if hasattr(device, "site") and device.site:
            site_groups = self._get_vlan_groups_for_scope(Site, [device.site])
            groups.update(site_groups)

            # Region-scoped VLAN groups (site's region and ancestors)
            if device.site.region:
                region_ancestors = self._get_ancestors(device.site.region)
                region_groups = self._get_vlan_groups_for_scope(Region, region_ancestors)
                groups.update(region_groups)

            # Site Group-scoped VLAN groups (site's group and ancestors)
            if device.site.group:
                site_group_ancestors = self._get_ancestors(device.site.group)
                site_group_groups = self._get_vlan_groups_for_scope(SiteGroup, site_group_ancestors)
                groups.update(site_group_groups)

        # Location-scoped VLAN groups (device's location and ancestors)
        if hasattr(device, "location") and device.location:
            location_ancestors = self._get_ancestors(device.location)
            location_groups = self._get_vlan_groups_for_scope(Location, location_ancestors)
            groups.update(location_groups)

        # Rack-scoped VLAN groups
        if hasattr(device, "rack") and device.rack:
            rack_groups = self._get_vlan_groups_for_scope(Rack, [device.rack])
            groups.update(rack_groups)

        # Global VLAN groups (no scope)
        global_groups = VLANGroup.objects.filter(scope_type__isnull=True)
        groups.update(global_groups)

        # Return sorted by name for consistent display
        return sorted(groups, key=lambda g: g.name.lower())

    def _build_vlan_lookup_maps(self, vlan_groups):
        """
        Build lookup dictionaries for VLAN matching.

        Returns a dict with:
        - vid_to_groups: {vid: [vlan_group, ...]} - VID to groups containing that VID
        - vid_group_to_vlan: {(vid, group_id): vlan} - unique per group lookup
        - vid_to_vlans: {vid: [vlan, ...]} - all VLANs with that VID
        - vid_name_to_vlan: {(vid, name): vlan} - VID + name lookup
        """
        from ipam.models import VLAN

        vid_to_groups = {}
        vid_group_to_vlan = {}
        vid_to_vlans = {}
        vid_name_to_vlan = {}

        # Get all VLANs from relevant groups and global VLANs
        group_pks = [g.pk for g in vlan_groups]
        vlans = VLAN.objects.filter(group__pk__in=group_pks).select_related("group")
        # Also get global VLANs (no group)
        global_vlans = VLAN.objects.filter(group__isnull=True)

        for vlan in list(vlans) + list(global_vlans):
            vid = vlan.vid
            group = vlan.group
            group_id = group.pk if group else None
            name = vlan.name

            # Build VID to groups lookup for ambiguity detection (group VLANs only)
            if group:
                if vid not in vid_to_groups:
                    vid_to_groups[vid] = []
                if group not in vid_to_groups[vid]:
                    vid_to_groups[vid].append(group)

            # Build (vid, group_id) to vlan lookup
            vid_group_to_vlan[(vid, group_id)] = vlan

            # Build VID to all VLANs list (for dropdown options)
            if vid not in vid_to_vlans:
                vid_to_vlans[vid] = []
            vid_to_vlans[vid].append(vlan)

            # Build (vid, name) to vlan lookup
            vid_name_to_vlan[(vid, name)] = vlan

        return {
            "vid_to_groups": vid_to_groups,
            "vid_group_to_vlan": vid_group_to_vlan,
            "vid_to_vlans": vid_to_vlans,
            "vid_name_to_vlan": vid_name_to_vlan,
        }

    def _select_most_specific_group(self, groups, device):
        """
        Select the most specific VLAN group based on device context.

        Priority order (most specific to least specific):
        1. Rack-scoped (device's rack)
        2. Location-scoped (device's location, closer ancestors win)
        3. Site-scoped (device's site)
        4. Site Group-scoped (device's site's group, closer ancestors win)
        5. Region-scoped (device's site's region, closer ancestors win)
        6. Global (no scope)

        Args:
            groups: List of VLANGroup objects that all contain the same VID
            device: NetBox Device object

        Returns:
            VLANGroup or None if no clear winner (e.g., multiple groups at same priority level)
        """
        from dcim.models import Location, Rack, Region, Site, SiteGroup
        from django.contrib.contenttypes.models import ContentType

        if not device or not groups:
            return None

        # Build scope priority lookup for this device
        # Lower number = higher priority (more specific)
        scope_priority = {}
        priority = 0

        # Priority 1: Rack (most specific)
        if hasattr(device, "rack") and device.rack:
            rack_ct = ContentType.objects.get_for_model(Rack)
            scope_priority[(rack_ct.pk, device.rack.pk)] = priority
            priority += 1

        # Priority 2: Location hierarchy (device's location first, then ancestors)
        if hasattr(device, "location") and device.location:
            location_ct = ContentType.objects.get_for_model(Location)
            for loc in self._get_ancestors(device.location):
                scope_priority[(location_ct.pk, loc.pk)] = priority
                priority += 1

        # Priority 3: Site
        if hasattr(device, "site") and device.site:
            site_ct = ContentType.objects.get_for_model(Site)
            scope_priority[(site_ct.pk, device.site.pk)] = priority
            priority += 1

            # Priority 4: Site Group hierarchy
            if device.site.group:
                site_group_ct = ContentType.objects.get_for_model(SiteGroup)
                for sg in self._get_ancestors(device.site.group):
                    scope_priority[(site_group_ct.pk, sg.pk)] = priority
                    priority += 1

            # Priority 5: Region hierarchy
            if device.site.region:
                region_ct = ContentType.objects.get_for_model(Region)
                for reg in self._get_ancestors(device.site.region):
                    scope_priority[(region_ct.pk, reg.pk)] = priority
                    priority += 1

        # Priority 6: Global (no scope) - lowest priority
        global_priority = priority

        # Find the group with the highest priority (lowest number)
        best_group = None
        best_priority = float("inf")
        same_priority_count = 0

        for group in groups:
            if group.scope_type is None:
                # Global scope
                group_priority = global_priority
            else:
                scope_key = (group.scope_type.pk, group.scope_id)
                group_priority = scope_priority.get(scope_key, float("inf"))

            if group_priority < best_priority:
                best_priority = group_priority
                best_group = group
                same_priority_count = 1
            elif group_priority == best_priority:
                same_priority_count += 1

        # Only return a group if there's a single winner at the best priority level
        if same_priority_count == 1 and best_group is not None:
            return best_group

        return None

    def _get_ancestors(self, obj):
        """
        Get all ancestors of a hierarchical object (location, region, site group).
        Returns list including the object itself and all parents up to root.
        """
        ancestors = []
        current = obj
        while current is not None:
            ancestors.append(current)
            current = getattr(current, "parent", None)
        return ancestors

    def _get_vlan_groups_for_scope(self, model_class, objects):
        """
        Get VLAN groups scoped to any of the given objects.

        Args:
            model_class: The Django model class (Site, Location, Region, etc.)
            objects: List of model instances to check

        Returns:
            QuerySet of VLANGroup objects
        """
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        if not objects:
            return VLANGroup.objects.none()

        content_type = ContentType.objects.get_for_model(model_class)
        object_ids = [obj.pk for obj in objects if obj is not None and obj.pk is not None]

        if not object_ids:
            return VLANGroup.objects.none()

        return VLANGroup.objects.filter(scope_type=content_type, scope_id__in=object_ids)

    def _find_vlan_in_group(self, vid, vlan_group_id, lookup_maps):
        """
        Find a VLAN by VID, preferring the specified group.

        Args:
            vid: VLAN ID (integer)
            vlan_group_id: Optional VLAN group ID to prefer
            lookup_maps: Dict from _build_vlan_lookup_maps()

        Returns:
            VLAN object or None
        """
        vid_group_to_vlan = lookup_maps.get("vid_group_to_vlan", {})
        vid_to_vlans = lookup_maps.get("vid_to_vlans", {})

        # Try specific group first
        if vlan_group_id:
            try:
                vlan = vid_group_to_vlan.get((vid, int(vlan_group_id)))
                if vlan:
                    return vlan
            except (ValueError, TypeError):
                pass

        # Try global (no group)
        vlan = vid_group_to_vlan.get((vid, None))
        if vlan:
            return vlan

        # Fallback: first matching VLAN
        vlans = vid_to_vlans.get(vid, [])
        return vlans[0] if vlans else None

    def _update_interface_vlan_assignment(self, interface, vlan_data, vlan_group_map, lookup_maps):
        """
        Update interface VLAN assignments in NetBox (mode, untagged_vlan, tagged_vlans).

        Args:
            interface: NetBox Interface or VMInterface object
            vlan_data: Dict with 'untagged_vlan' (int or None) and 'tagged_vlans' (list of ints)
            vlan_group_map: Dict mapping VID (str) to VLAN group ID for per-VLAN group lookups.
                           Can also be a single group ID string for backward compat.
            lookup_maps: Dict from _build_vlan_lookup_maps()

        Returns:
            Dict with sync results:
                - mode_set: str or None
                - untagged_set: VLAN object or None
                - tagged_set: list of VLAN objects
                - missing_vlans: list of VIDs not found in NetBox
        """
        # Support both dict (per-VLAN) and string/int/None (single group) for backward compat
        if not isinstance(vlan_group_map, dict):
            single_group_id = vlan_group_map
            vlan_group_map = None
        else:
            single_group_id = None

        untagged_vid = vlan_data.get("untagged_vlan")
        tagged_vids = vlan_data.get("tagged_vlans", [])
        missing_vlans = []

        def _get_group_id_for_vid(vid):
            """Resolve the VLAN group ID for a specific VID."""
            if vlan_group_map is not None:
                return vlan_group_map.get(str(vid), "")
            return single_group_id or ""

        # Determine mode
        if tagged_vids:
            interface.mode = "tagged"
        elif untagged_vid:
            interface.mode = "access"
        else:
            # No VLANs - clear mode
            interface.mode = ""

        # Set untagged VLAN
        untagged_set = None
        if untagged_vid:
            vlan = self._find_vlan_in_group(untagged_vid, _get_group_id_for_vid(untagged_vid), lookup_maps)
            if vlan:
                interface.untagged_vlan = vlan
                untagged_set = vlan
            else:
                missing_vlans.append(untagged_vid)
                interface.untagged_vlan = None
        else:
            interface.untagged_vlan = None

        # Save mode + untagged_vlan before M2M operations.
        # tagged_vlans.set() triggers a DB refresh that wipes unsaved
        # in-memory attributes, so we must persist first.
        interface.save()

        # Set tagged VLANs (M2M - requires the instance to be saved first)
        tagged_set = []
        if tagged_vids:
            for vid in tagged_vids:
                vlan = self._find_vlan_in_group(vid, _get_group_id_for_vid(vid), lookup_maps)
                if vlan:
                    tagged_set.append(vlan)
                else:
                    missing_vlans.append(vid)
            interface.tagged_vlans.set(tagged_set)
        else:
            interface.tagged_vlans.clear()

        return {
            "mode_set": interface.mode,
            "untagged_set": untagged_set,
            "tagged_set": tagged_set,
            "missing_vlans": missing_vlans,
        }
