import re

from django.conf import settings as django_settings
from django.contrib import messages
from django.shortcuts import render
from django.urls import reverse
from netbox.views import generic

from netbox_librenms_plugin.forms import AddToLIbreSNMPV1V2, AddToLIbreSNMPV3
from netbox_librenms_plugin.import_utils import _determine_device_name
from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name
from netbox_librenms_plugin.sync_cache import (
    TAB_SPECS,
    SyncCacheConsistency,
    SyncTab,
    mapped_server_keys,
)
from netbox_librenms_plugin.utils import (
    coerce_librenms_id,
    find_matching_platform,
    get_interface_name_field,
    get_librenms_device_id,
    get_librenms_sync_device,
    is_legacy_librenms_id,
    match_librenms_hardware_to_device_type,
    resolve_naming_preferences,
    resolve_server_mapping_display_id,
)
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin

INTERFACE_NAME_SELECTOR_TABS = ("interfaces", "cables", "ipaddresses")


class BaseLibreNMSSyncView(
    LibreNMSPermissionMixin, LibreNMSAPIMixin, NetBoxObjectPermissionMixin, generic.ObjectListView
):
    """
    Base view for LibreNMS sync information.
    """

    queryset = None  # Will be set in subclasses
    model = None  # Will be set in subclasses
    tab = None  # Will be set in subclasses
    template_name = "netbox_librenms_plugin/librenms_sync_base.html"
    # Render server key resolved in get(); read by get_context_data for the migrated-mode banner.
    # None until get() runs (e.g. a direct get_context_data() call), then active_server_key is used.
    _scoped_render_server_key = None

    def get_object(self, pk):
        """Retrieve the object the user may view (same seam as the base table views)."""
        # The plugin gate is model-level only, so scope the lookup or any pk is reachable.
        return self.restrict_object_or_404(self.model, pk=pk)

    def get(self, request, pk, context=None):
        """Handle GET request for the LibreNMS sync view."""
        obj = self.get_object(pk)

        # Scope the page header (device info, VC inventory serials, active-server highlight) to the
        # same ?server_key the embedded tabs rebind to. Without this the orchestrator reads the
        # session/default server while a ?server_key load renders the tab tables for another server
        # — an internally inconsistent page. A blank/absent key keeps the session/default client, so
        # single-server and default renders are unchanged.
        _scoped_key, unresolved = self.resolve_get_render_server_key(request)
        # Stash the resolved render key so get_context_data builds the migrated-mode banner under
        # the SAME namespace as the header/tabs. On the unresolved (stale ?server_key) path the
        # rebind declines and self.librenms_api.server_key falls back to "default" — using that for
        # the marker lookup would hide a real migration (marker under the requested server) or show
        # a spurious one (unrelated "default" marker). _scoped_key keeps the page self-consistent.
        self._scoped_render_server_key = _scoped_key

        # The resolve can decline WITHOUT binding a client: a blank/absent key with a
        # misconfigured default (build_librenms_api(None) → None), or an unresolved
        # ?server_key on a fresh view. Everything below (and get_context_data) reads the
        # lazy self.librenms_api property, which would reconstruct LibreNMSAPI() and
        # re-raise the very KeyError/ValueError the helper just swallowed — an unhandled
        # 500 for every device page. Bind the default via the fail-closed factory. If even
        # that can't build: for a blank/absent key (the page IS the default server's view)
        # degrade to a minimal render with an error banner; for an unresolved ?server_key
        # continue instead — that path already fails closed (librenms_id=None) and must
        # still render its degraded page (e.g. the migrated banner scoped to the requested
        # key), which an early return here would suppress.
        if getattr(self, "_librenms_api", None) is None:
            from netbox_librenms_plugin.librenms_api import build_librenms_api

            default_api = build_librenms_api(None)
            if default_api is not None:
                self._librenms_api = default_api
            elif not unresolved:
                messages.error(
                    request,
                    "LibreNMS server is not configured correctly (missing URL or API token). "
                    "Check the plugin settings.",
                )
                return render(
                    request,
                    self.template_name,
                    {
                        "object": obj,
                        "tab": self.tab,
                        "has_librenms_id": False,
                        "found_in_librenms": False,
                        "librenms_device_details": {},
                        "platform_info": {},
                    },
                )

        # For Virtual Chassis members, always delegate to get_librenms_sync_device() so
        # self._librenms_lookup_device and self.librenms_id are consistent with the
        # helper-based VC status computed in get_context_data().  A legacy bare-int mapping
        # on the viewed member must not shadow an explicit per-server mapping on another
        # member — get_librenms_sync_device() applies the full priority order.
        librenms_lookup_device = obj
        if not unresolved and hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            sync_device = get_librenms_sync_device(obj, server_key=self.librenms_api.server_key)
            if sync_device:
                librenms_lookup_device = sync_device

        # Store for use in get_context_data (badge generation needs the same object)
        self._librenms_lookup_device = librenms_lookup_device
        # Store the unresolved flag so get_context_data's VC-status block can fail closed the
        # same way this method does. Without it that block would recompute the sync-device
        # linkage against the default-bound client (the rebind declined) and leak the default
        # server's mapping onto a page whose header/tabs are failing closed for the gone server.
        self._server_key_unresolved = unresolved

        if unresolved:
            # ?server_key named a server that no longer resolves; the rebind declined and left the
            # default/session client bound. Fail closed — render the header with no mapping rather
            # than attributing the default server's librenms_id to the requested (gone) server. The
            # embedded tabs each rebind and render empty for the same unresolved key.
            self.librenms_id = None
        else:
            # Get librenms_id using the determined lookup device. Normalise to a positive int
            # or None at the source so every downstream `is not None` check (has_librenms_id,
            # get_device_info, etc.) can rely on the invariant without re-validating.
            self.librenms_id = coerce_librenms_id(self.librenms_api.get_librenms_id(librenms_lookup_device))

        context = self.get_context_data(request, obj)

        return render(request, self.template_name, context)

    def get_context_data(self, request, obj):
        """Get the context data for the LibreNMS sync view."""
        # Get context from parent classes (including LibreNMSAPIMixin)
        context = super().get_context_data()
        applicable_tabs = SyncCacheConsistency(obj).applicable_tabs()
        applicable_tab_names = {tab.value for tab in applicable_tabs}
        requested_sync_tab = request.GET.get("tab") or SyncTab.INTERFACES.value
        active_sync_tab = requested_sync_tab if requested_sync_tab in applicable_tab_names else SyncTab.INTERFACES.value
        sync_tab_urls = {}
        for sync_tab in applicable_tabs:
            query = request.GET.copy()
            query["tab"] = sync_tab.value
            query.pop("interface_name_field", None)
            sync_tab_urls[sync_tab.value] = f"{request.path}?{query.urlencode()}"

        # Add our specific context
        context.update(
            {
                "object": obj,
                "tab": self.tab,
                "active_sync_tab": active_sync_tab,
                "sync_tab_urls": sync_tab_urls,
                "interface_name_selector_visible": active_sync_tab in INTERFACE_NAME_SELECTOR_TABS,
                # self.librenms_id is normalised to a positive int or None at assignment
                # (see post()), so `is not None` is correct here — 0/negatives never reach it.
                "has_librenms_id": self.librenms_id is not None,
            }
        )

        # Skip the VC-status block on an unresolved ?server_key: get() failed closed (librenms_id
        # None, client left on the default server), so resolving the linkage here against the
        # default server would leak a mapping for a server the page is otherwise reporting as gone.
        if (
            not getattr(self, "_server_key_unresolved", False)
            and hasattr(obj, "virtual_chassis")
            and obj.virtual_chassis
        ):
            # Use helper function to determine the sync device
            librenms_sync_device = get_librenms_sync_device(obj, server_key=self.librenms_api.server_key)

            # Determine sync device status
            sync_device_has_librenms_id = False
            sync_device_has_primary_ip = False

            if librenms_sync_device:
                sync_device_has_librenms_id = (
                    get_librenms_device_id(librenms_sync_device, self.librenms_api.server_key, auto_save=False)
                    is not None
                )
                sync_device_has_primary_ip = bool(librenms_sync_device.primary_ip)

            context.update(
                {
                    "is_vc_member": True,
                    "sync_device_has_primary_ip": sync_device_has_primary_ip,
                    "librenms_sync_device": librenms_sync_device,
                    "sync_device_has_librenms_id": sync_device_has_librenms_id,
                }
            )

        render_server_key = self._scoped_render_server_key or self.active_server_key
        coordinator = None
        sync_cache_status = None
        if render_server_key and render_server_key in mapped_server_keys(obj, render_server_key):
            coordinator = SyncCacheConsistency(obj)
            sync_cache_status = coordinator.status_for_request(
                request,
                render_server_key,
                active_tab=SyncTab(active_sync_tab),
            )

        active_cache_state = sync_cache_status.get(active_sync_tab) if sync_cache_status else None
        cache_only_device_info = bool(
            active_cache_state and active_cache_state["state"] in {"invalidated", "refresh_failed"}
        )
        librenms_info = self.get_librenms_device_info(
            obj,
            request,
            cache_only=cache_only_device_info,
        )

        interface_context = self.get_interface_context(request, obj)
        cable_context = self.get_cable_context(request, obj)
        ip_context = self.get_ip_context(request, obj)
        vlan_context = self.get_vlan_context(request, obj)
        module_context = self.get_module_context(request, obj)

        interface_name_field = get_interface_name_field(request, obj)

        # Get platform info for display and sync
        platform_info = self._get_platform_info(librenms_info, obj)

        # Get manufacturers for platform creation modal
        from dcim.models import Manufacturer

        manufacturers = Manufacturer.objects.all().order_by("name")

        # Detect legacy bare-int librenms_id format for conversion badge
        _lookup_device = getattr(self, "_librenms_lookup_device", obj)
        _raw_cf = _lookup_device.cf.get("librenms_id") if _lookup_device else None
        librenms_id_is_legacy = is_legacy_librenms_id(_raw_cf)

        # Determine if serial match allows legacy ID conversion.
        # VMs have no serial field in NetBox; skip the gate so the Convert ID button is enabled.
        _librenms_serial = librenms_info["librenms_device_details"].get("librenms_device_serial", "-")
        _netbox_serial = getattr(_lookup_device, "serial", "") or ""
        _lookup_is_vm = _lookup_device._meta.model_name == "virtualmachine" if _lookup_device else False
        librenms_id_serial_confirmed = _lookup_is_vm or bool(
            _librenms_serial and _librenms_serial != "-" and _netbox_serial and _librenms_serial == _netbox_serial
        )
        device_info_unavailable = librenms_info.get("device_info_unavailable", False)
        show_add_device = not librenms_info["found_in_librenms"] and not device_info_unavailable

        context.update(
            {
                "interface_sync": interface_context,
                "cable_sync": cable_context,
                "ip_sync": ip_context,
                "vlan_sync": vlan_context,
                "module_sync": module_context,
                "has_write_permission": self.has_write_permission(),
                # Active server key, so the create-platform modal (included without `only`, hence
                # inheriting this context) forwards it as a hidden field — otherwise
                # CreateAndAssignPlatformView redirects back to the default-server tab, dropping
                # the non-default server context the user was acting on. Use the resolved render
                # key, NOT the lazy librenms_api property: on the stale-?server_key path with a
                # misconfigured default no client is bound, so the property would reconstruct
                # LibreNMSAPI() and 500 the degraded render; the render key also keeps the page's
                # forms scoped to the requested (gone) server so they fail closed server-side.
                "server_key": render_server_key,
                "v1v2form": AddToLIbreSNMPV1V2(prefix="v1v2") if show_add_device else None,
                "v3form": AddToLIbreSNMPV3(prefix="v3") if show_add_device else None,
                "librenms_device_id": self.librenms_id,
                "found_in_librenms": librenms_info.get("found_in_librenms"),
                "device_info_unavailable": device_info_unavailable,
                "librenms_device_details": librenms_info.get("librenms_device_details"),
                "mismatched_device": librenms_info.get("mismatched_device"),
                **librenms_info["librenms_device_details"],
                "interface_name_field": interface_name_field,
                "platform_info": platform_info,
                "vc_inventory_serials": librenms_info["librenms_device_details"].get("vc_inventory_serials", []),
                "manufacturers": manufacturers,
                # Same safe accessor as "server_key" above — only the is_active highlight needs it.
                "all_server_mappings": self._build_all_server_mappings(
                    _lookup_device, self._scoped_render_server_key or self.active_server_key
                ),
                "librenms_id_is_legacy": librenms_id_is_legacy,
                "librenms_id_serial_confirmed": librenms_id_serial_confirmed,
                # Lookup device may differ from object (e.g. VC master vs member).
                # Used by the Remove server mapping form to post to the correct device.
                "lookup_device_pk": _lookup_device.pk if _lookup_device else obj.pk,
                "lookup_device_model_name": (
                    _lookup_device._meta.model_name if _lookup_device else obj._meta.model_name
                ),
                "object_model_name": obj._meta.model_name,
                # Build migrated mode from obj (the viewed device), NOT a re-resolved sync/lookup
                # device, so the full page and the HTMX tab partials — which also pass obj — stay
                # consistent. The merge stamps the _migrated_to marker on whichever device holds the
                # LibreNMS link (get_librenms_sync_device): that IS obj for a non-VC device or the
                # link-holding VC member (the common case); for a non-sync VC member the marker lands
                # on the sync sibling, whose sync page is where its migrated controls surface.
                # Use the resolved render key (not the lazy librenms_api property, which re-derives
                # the global default and mis-namespaces the marker on the stale-?server_key path).
                **self._build_migrated_context(obj, self._scoped_render_server_key or self.active_server_key),
            }
        )

        if coordinator is not None and sync_cache_status is not None:
            object_type = obj._meta.model_name
            for sync_tab in coordinator.applicable_tabs():
                if sync_cache_status[sync_tab.value]["snapshot_available"]:
                    continue
                context_name = TAB_SPECS[sync_tab].context_name
                tab_context = context.get(context_name)
                if isinstance(tab_context, dict):
                    context[context_name] = {
                        **tab_context,
                        "table": None,
                        "cache_expiry": None,
                    }
            context.update(
                {
                    "sync_cache_status": sync_cache_status,
                    "sync_cache_contract": {
                        tab.value: {
                            "content_id": TAB_SPECS[tab].content_id,
                            "label": TAB_SPECS[tab].label,
                        }
                        for tab in coordinator.applicable_tabs()
                    },
                    "sync_cache_status_url": reverse(
                        "plugins:netbox_librenms_plugin:sync_cache_status",
                        kwargs={"object_type": object_type, "pk": obj.pk},
                    ),
                    "sync_cache_fragment_urls": {
                        tab.value: reverse(
                            "plugins:netbox_librenms_plugin:sync_cache_fragment",
                            kwargs={"object_type": object_type, "pk": obj.pk, "tab": tab.value},
                        )
                        for tab in coordinator.applicable_tabs()
                    },
                }
            )

        return context

    @staticmethod
    def _build_migrated_context(obj, server_key):
        """
        Build Stage 2b "donor migrated mode" context.

        When ``migrated_to_marker`` is set, all sync action buttons should be hidden
        and per-row "Move to winner" actions should be shown instead. Delegates to
        :func:`utils.build_migrated_context` so the full page and the HTMX tab partials
        share one implementation.

        Args:
            obj: The donor device to build migrated-mode context for.
            server_key: The LibreNMS server key the marker is namespaced under.

        Returns:
            dict: ``{migrated_to_marker, migrated_to_winner}`` — the marker dict
                ``{device_id, server_key, at}`` (or None), and the winner
                :class:`Device` (or None if deleted since the marker was written).
        """
        from netbox_librenms_plugin.utils import build_migrated_context

        return build_migrated_context(obj, server_key)

    @staticmethod
    def _build_all_server_mappings(obj, active_server_key):
        """
        Build a list of all LibreNMS server mappings for the given device.

        Each entry describes one server<->ID mapping stored in the ``librenms_id``
        custom field:

        * ``server_key``    – the key as stored in the CF dict.
        * ``display_name``  – human-readable name from PLUGINS_CONFIG, or the key.
        * ``librenms_url``  – base URL of that server (``None`` when not configured).
        * ``device_id``     – the integer device ID on that server.
        * ``device_url``    – direct URL to the device page on that server (or ``None``).
        * ``is_configured`` – True when the server key exists in current plugin config.
        * ``is_active``     – True when this is the currently active server.
        * ``is_oob_only``   – True when the mapping was surfaced via an OOB-only linkage
          (no host ``id``, only a nested ``oob.id``).

        Returns ``None`` for legacy bare-int format (no per-server info to show)
        and ``None`` when the CF is absent/invalid.
        """
        cf_value = obj.custom_field_data.get("librenms_id")
        if not isinstance(cf_value, dict) or not cf_value:
            return None

        plugins_cfg = getattr(django_settings, "PLUGINS_CONFIG", {}).get("netbox_librenms_plugin", {})
        servers_config = plugins_cfg.get("servers") or {}
        if not isinstance(servers_config, dict):
            servers_config = {}

        result = []
        for sk, did in cf_value.items():
            # Resolve the display id (host id, else the nested OOB controller id for an OOB-only
            # entry) and whether it came from that OOB fallback. Shared with the import-validation
            # modal (actions.DeviceValidationDetailsView._build_id_server_info) so both agree on
            # which servers a device is linked to; the bool/str/positive coercion and the OOB
            # fallback live in one place. A None result (migrated-only / corrupt entry) is skipped.
            did, is_oob_only = resolve_server_mapping_display_id(did)
            if did is None:
                continue
            srv_cfg = servers_config.get(sk)
            # Legacy single-server config: "default" key with no matching servers entry —
            # fall back to root-level librenms_url/display_name in plugins_cfg.
            if srv_cfg is None and sk == "default":
                legacy_url = plugins_cfg.get("librenms_url")
                if legacy_url:
                    srv_cfg = {
                        "librenms_url": legacy_url,
                        "display_name": plugins_cfg.get("display_name") or f"Default Server ({legacy_url})",
                    }
            is_configured = srv_cfg is not None
            # Treat malformed (non-dict) server config entries as unconfigured
            if srv_cfg is not None and not isinstance(srv_cfg, dict):
                srv_cfg = None
                is_configured = False
            librenms_url = srv_cfg.get("librenms_url") if srv_cfg else None
            display_name = (srv_cfg.get("display_name") or sk) if srv_cfg else sk
            device_url = f"{librenms_url}/device/device={did}/" if librenms_url else None
            result.append(
                {
                    "server_key": sk,
                    "display_name": display_name,
                    "librenms_url": librenms_url,
                    "device_id": did,
                    "device_url": device_url,
                    "is_configured": is_configured,
                    "is_active": sk == active_server_key,
                    "is_oob_only": is_oob_only,
                }
            )

        # Sort: active first, then configured, then orphaned
        result.sort(key=lambda e: 0 if e["is_active"] else (1 if e["is_configured"] else 2))
        return result or None

    def get_librenms_device_info(self, obj, request=None, *, cache_only=False):
        """Get the LibreNMS device information for the given object."""
        found_in_librenms = False
        mismatched_device = False
        device_info_unavailable = False
        librenms_device_details = {
            "librenms_device_url": None,
            "librenms_device_hardware": "-",
            "librenms_device_serial": "-",
            "librenms_device_os": "-",
            "librenms_device_version": "-",
            "librenms_device_features": "-",
            "librenms_device_location": "-",
            "librenms_device_hardware_match": None,
            "vc_inventory_serials": [],
        }

        if self.librenms_id is not None:
            success, device_info = self.librenms_api.get_device_info(self.librenms_id, cache_only=cache_only)
            device_info_unavailable = cache_only and not success
            # isinstance(dict) guard: a truthy non-dict payload (string/list) would 500 on the
            # device_info.get(...) calls below; fall back to the default details block instead
            # of trusting success=True alone (issue #100).
            if success and isinstance(device_info, dict):
                # Get NetBox device details
                netbox_ip = str(obj.primary_ip.address.ip).lower() if obj.primary_ip else None
                netbox_name = obj.name

                # Get LibreNMS device details
                librenms_sysname = device_info.get("sysName")
                librenms_ip = device_info.get("ip")

                # Extract new fields; use `or "-"` so null/empty from LibreNMS renders as "-"
                hardware = device_info.get("hardware") or "-"
                serial = device_info.get("serial") or "-"
                os_name = device_info.get("os") or "-"
                version = device_info.get("version") or "-"
                features = device_info.get("features") or "-"

                # Try to match hardware to NetBox DeviceType
                hardware_match = match_librenms_hardware_to_device_type(hardware)

                # Compute resolved name using naming preferences
                resolved_name = None
                if request:
                    use_sysname, strip_domain = resolve_naming_preferences(request)
                    resolved_name = _determine_device_name(
                        device_info,
                        use_sysname=use_sysname,
                        strip_domain=strip_domain,
                        device_id=self.librenms_id,
                    )

                    # For VC members, generate the expected VC member name
                    if (
                        resolved_name
                        and hasattr(obj, "virtual_chassis")
                        and obj.virtual_chassis is not None
                        and obj.vc_position is not None
                    ):
                        resolved_name = _generate_vc_member_name(
                            resolved_name,
                            obj.vc_position,
                            serial=getattr(obj, "serial", None),
                        )

                # Update device details regardless of match
                librenms_device_details.update(
                    {
                        "librenms_device_url": f"{self.librenms_api.librenms_url}/device/device={self.librenms_id}/",
                        "librenms_device_hardware": hardware,
                        "librenms_device_serial": serial,
                        "librenms_device_os": os_name,
                        "librenms_device_version": version,
                        "librenms_device_features": features,
                        "librenms_device_location": device_info.get("location") or "-",
                        "librenms_device_ip": librenms_ip,
                        "sysName": librenms_sysname,
                        "resolved_name": resolved_name or librenms_sysname,
                        "librenms_device_hostname": device_info.get("hostname") or "-",
                        "librenms_device_hardware_match": hardware_match,
                    }
                )

                # For Virtual Chassis, fetch inventory
                if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                    vc_serials = self._get_vc_inventory_serials(obj)
                    librenms_device_details["vc_inventory_serials"] = vc_serials

                # Device was retrieved successfully via librenms_id — trust the ID
                found_in_librenms = True

                # Normalise the NetBox name once for comparisons
                netbox_name_norm = netbox_name.lower() if netbox_name else None
                if netbox_name_norm:
                    # Strip VC member suffix like " (1)" before comparing
                    netbox_name_norm = re.sub(r"\s*\(\d+\)$", "", netbox_name_norm)

                # Also strip the VC member naming pattern from settings
                # (e.g. "-M2", " (2)", "-SW3") to recover the base device name
                netbox_name_vc_stripped = None
                if netbox_name_norm:
                    netbox_name_vc_stripped = self._strip_vc_pattern(netbox_name_norm)

                # Collect all NetBox identity values to compare against
                netbox_dns_name = (
                    obj.primary_ip.dns_name.lower() if obj.primary_ip and obj.primary_ip.dns_name else None
                )
                netbox_identities = {
                    v
                    for v in [
                        netbox_name_norm,
                        netbox_ip,
                        netbox_dns_name,
                        netbox_name_vc_stripped,
                    ]
                    if v
                }

                # Collect all LibreNMS identity values, including
                # domain-stripped short names (e.g. "sw01.example.net" → "sw01")
                librenms_hostname = device_info.get("hostname")
                librenms_values = []
                for val in [librenms_sysname, librenms_hostname, librenms_ip]:
                    if val:
                        lower_val = val.lower()
                        librenms_values.append(lower_val)
                        # Add short name (strip domain) if it looks like an FQDN
                        short = lower_val.split(".")[0]
                        if short != lower_val:
                            librenms_values.append(short)
                librenms_identities = set(librenms_values)

                # A device is considered matched when ANY NetBox identity
                # appears in the LibreNMS identities.  This covers:
                #   - NetBox name == sysName or hostname
                #   - NetBox primary IP == LibreNMS hostname (added by IP)
                #   - NetBox DNS name == sysName or hostname (FQDN match)
                if netbox_identities & librenms_identities:
                    mismatched_device = False
                else:
                    mismatched_device = True

                librenms_device_details["netbox_dns_name"] = netbox_dns_name or "-"

        return {
            "found_in_librenms": found_in_librenms,
            "device_info_unavailable": device_info_unavailable,
            "librenms_device_details": librenms_device_details,
            "mismatched_device": mismatched_device,
        }

    def get_interface_context(self, request, obj):
        """
        Get the context data for interface sync.
        Subclasses should override this method.
        """
        return None

    def get_cable_context(self, request, obj):
        """
        Get the context data for cable sync.
        Subclasses should override this method if applicable.
        """
        return None

    def get_ip_context(self, request, obj):
        """
        Get the context data for IP address sync.
        Subclasses should override this method.
        """
        return None

    def get_vlan_context(self, request, obj):
        """
        Get the context data for VLAN sync.
        Subclasses should override this method.
        """
        return None

    def get_module_context(self, request, obj):
        """
        Get the context data for module sync.
        Subclasses should override this method if applicable (e.g. VMs return None).
        """
        return None

    @staticmethod
    def _strip_vc_pattern(name):
        """
        Strip the VC member naming suffix from a device name.

        Uses the vc_member_name_pattern from LibreNMSSettings to build a
        regex that removes the suffix.  For example, with the default
        pattern ``-M{position}`` and name ``switch01-m2``, this returns
        ``switch01``.

        Returns the stripped name, or None if it equals the original
        (i.e. no suffix was found).
        """
        try:
            from netbox_librenms_plugin.models import LibreNMSSettings

            settings = LibreNMSSettings.objects.first()
            pattern = (
                settings.vc_member_name_pattern
                if settings and isinstance(settings.vc_member_name_pattern, str)
                else "-M{position}"
            )
            if not isinstance(pattern, str):
                pattern = "-M{position}"

            # Turn the pattern into a regex by replacing placeholders
            # {position} → \d+   {serial} → .+
            regex_suffix = re.escape(pattern)
            regex_suffix = regex_suffix.replace(re.escape("{position}"), r"\d+")
            regex_suffix = regex_suffix.replace(re.escape("{serial}"), r".+")

            stripped = re.sub(regex_suffix + "$", "", name, flags=re.IGNORECASE)
            return stripped if stripped != name else None
        except Exception:
            return None

    def _get_vc_inventory_serials(self, obj):
        """
        Fetch inventory serials for Virtual Chassis members.

        Args:
            obj: NetBox device object (VC member)

        Returns:
            list: [
                {
                    'description': 'Chassis component description',
                    'serial': 'serial number',
                    'model': 'model name',
                    'assigned_member': Device object or None (if serial matches existing assignment)
                }
            ]
        """
        success, inventory = self.librenms_api.get_device_inventory(self.librenms_id)
        if not success:
            return []

        # Filter for chassis components
        chassis_components = [item for item in inventory if item.get("entPhysicalClass") == "chassis"]

        # Get all VC members
        vc_members = obj.virtual_chassis.members.all()

        result = []
        for component in chassis_components:
            serial = component.get("entPhysicalSerialNum", "-")
            if not serial or serial == "-":
                continue

            # Check if this serial is already assigned to a VC member
            assigned_member = None
            for member in vc_members:
                if member.serial and member.serial.strip() == serial.strip():
                    assigned_member = member
                    break

            result.append(
                {
                    "description": component.get("entPhysicalDescr", "-"),
                    "serial": serial,
                    "model": component.get("entPhysicalModelName", "-"),
                    "assigned_member": assigned_member,
                }
            )

        return result

    def _get_platform_info(self, librenms_info, obj):
        """
        Get platform information from LibreNMS.

        Platform matching is based on OS name only (not version).
        Version is displayed separately as informational data.

        Args:
            librenms_info: Dictionary with LibreNMS device info
            obj: NetBox device object

        Returns:
            dict: {
                'netbox_platform': Platform object or None,
                'librenms_os': str (OS name),
                'librenms_version': str (OS version),
                'platform_exists': bool (whether OS platform exists in NetBox),
                'platform_name': str (OS name for platform matching),
                'matching_platform': Platform object or None
            }
        """
        librenms_os = librenms_info["librenms_device_details"].get("librenms_device_os", "-")
        librenms_version = librenms_info["librenms_device_details"].get("librenms_device_version", "-")

        # Platform name is just the OS (not OS + version)
        platform_name = librenms_os if librenms_os != "-" else None

        # Try case-insensitive exact name match first, then fall back to PlatformMapping
        platform_exists = False
        matching_platform = None
        if platform_name:
            result = find_matching_platform(platform_name)
            if result["found"] and result["match_type"] != "ambiguous":
                matching_platform = result["platform"]
                platform_exists = True

        return {
            "netbox_platform": obj.platform,
            "librenms_os": librenms_os,
            "librenms_version": librenms_version,
            "platform_exists": platform_exists,
            "platform_name": platform_name,
            "matching_platform": matching_platform,
        }
