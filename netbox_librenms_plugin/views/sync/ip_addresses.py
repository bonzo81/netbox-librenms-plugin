import logging
from urllib.parse import quote_plus

from dcim.models import Device, Interface
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from ipam.models import VRF, IPAddress
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.utils import (
    get_librenms_device_id,
    get_virtual_chassis_members,
    ip_family,
    resolve_set_primary_ip,
    same_host,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)

logger = logging.getLogger(__name__)


class SyncIPAddressesView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """Synchronize IP addresses from LibreNMS cache into NetBox."""

    required_object_permissions = {
        "POST": [
            ("add", IPAddress),
            ("change", IPAddress),
        ],
    }

    def _required_permissions(self, object_type):
        """
        Return the POST permissions, narrowed to the owner model this request targets.

        The owner and target interfaces are resolved through restricted querysets, so their view
        permissions belong in the gate. They cannot be declared statically because one view serves
        both Devices and VirtualMachines.

        Args:
            object_type (str): ``"device"`` or ``"virtualmachine"`` from the URL.

        Returns:
            dict: The ``required_object_permissions`` mapping for this request.
        """
        if object_type == "device":
            owner_model = Device
            interface_model = Interface
        elif object_type == "virtualmachine":
            owner_model = VirtualMachine
            interface_model = VMInterface
        else:
            raise Http404("Invalid object type.")

        perms = [
            ("view", owner_model),
            ("view", interface_model),
            *type(self).required_object_permissions["POST"],
        ]
        if resolve_set_primary_ip(self.request):
            perms.append(("change", owner_model))
        # A per-row VRF is resolved by client-supplied id through a restricted queryset. Only
        # demand its view permission when one is actually posted, so the common no-VRF sync
        # is not gated on a permission it never uses.
        if any(key.startswith("vrf_") and value for key, value in self.request.POST.items()):
            perms.append(("view", VRF))
        return {"POST": perms}

    def get_selected_ips(self, request):
        """Return selected IP addresses from POST data."""
        return [x for x in request.POST.getlist("select") if x]

    def get_vrf_selection(self, request, ip_address):
        """Return the selected VRF, or None only when the row requests no VRF."""
        vrf_id = request.POST.get(f"vrf_{ip_address}")

        if not vrf_id:
            return None

        try:
            return self.restricted_queryset(VRF).get(pk=vrf_id)
        except (VRF.DoesNotExist, TypeError, ValueError):
            raise ValueError("Selected VRF is no longer available or you do not have permission to view it.") from None

    def get_cached_ip_data(self, request, obj):
        """Return cached LibreNMS IP address data for the given object."""
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        cached_data = cache.get(self.get_cache_key(obj, "ip_addresses", server_key))
        if not cached_data:
            return None
        return cached_data.get("ip_addresses", [])

    def get_object(self, object_type, pk, action="view"):
        """Return the Device or VirtualMachine instance for the given type and pk (object-scoped)."""
        if object_type == "device":
            return self.restrict_object_or_404(Device, action, pk=pk)
        if object_type == "virtualmachine":
            return self.restrict_object_or_404(VirtualMachine, action, pk=pk)
        raise Http404("Invalid object type.")

    def get_ip_tab_url(self, obj):
        """Return the URL for the IP addresses sync tab."""
        if isinstance(obj, Device):
            url_name = "plugins:netbox_librenms_plugin:device_librenms_sync"
        else:
            url_name = "plugins:netbox_librenms_plugin:vm_librenms_sync"
        # Resolve the server_key to scope the redirect back to a working tab. Prefer the
        # POST-resolved key, then the already-bound _librenms_api (avoids reconstructing a
        # client). Only when nothing is bound — the failed-rebind path, where rebind returned
        # None — fall back to the librenms_api property to resolve the active/default server, but
        # swallow a construction failure (a misconfigured default) so we still redirect
        # gracefully instead of raising. The redirect must carry the resolved server_key (see
        # test_unknown_server_key_errors_without_500), so a bare _librenms_api read isn't enough.
        server_key = getattr(self, "_post_server_key", None)
        if not server_key:
            bound = getattr(self, "_librenms_api", None)
            if bound is not None:
                server_key = getattr(bound, "server_key", None)
            else:
                try:
                    server_key = self.librenms_api.server_key
                except Exception:  # a redirect helper must degrade, never 500 (misconfigured default)
                    server_key = None
        url = f"{reverse(url_name, args=[obj.pk])}?tab=ipaddresses"
        if server_key:
            url += f"&server_key={quote_plus(server_key)}"
        return url

    def post(self, request, object_type, pk):
        """Sync selected IP addresses from LibreNMS into NetBox."""
        # Check both plugin write and NetBox object permissions (owner read included).
        self.required_object_permissions = self._required_permissions(object_type)
        if error := self.require_all_permissions("POST"):
            return error

        owner_action = "change" if resolve_set_primary_ip(request) else "view"
        obj = self.get_object(object_type, pk, owner_action)

        # Rebind the cached API client to the POSTed server so live lookups (e.g. the
        # management-IP fetch for Set-Primary-IP) hit the same LibreNMS instance the cached
        # rows came from. The key comes from request POST, so a stale/tampered request could
        # carry an unknown key — surface a user-facing error instead of a 500. (Uses the
        # shared mixin helper, which also resolves/normalizes the key.)
        post_server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if post_server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return redirect(self.get_ip_tab_url(obj))
        self._post_server_key = post_server_key

        selected_ips = self.get_selected_ips(request)
        cached_ips = self.get_cached_ip_data(request, obj)

        if not cached_ips:
            messages.error(request, "Cache has expired. Please refresh the IP data.")
            return redirect(self.get_ip_tab_url(obj))

        if not selected_ips:
            messages.error(request, "No IP addresses selected for synchronization.")
            return redirect(self.get_ip_tab_url(obj))

        results = self.process_ip_sync(request, selected_ips, cached_ips, obj, object_type)
        self.display_sync_results(request, results)

        return redirect(self.get_ip_tab_url(obj))

    def get_management_ip(self, obj):
        """Return the LibreNMS management/polling IP for *obj*, or None.

        Used to decide which synced IP (if any) should become the object's
        Primary IP. Best-effort: any lookup failure yields None so the sync
        itself is never blocked.
        """
        try:
            librenms_id = self.librenms_api.get_librenms_id(obj)
            if not librenms_id:
                return None
            # get_live_device_info reads live (uncached): this feeds the Primary-IP write decision,
            # so it must read the current management IP, not a stale sync-tab snapshot.
            success, info = self.get_live_device_info(librenms_id)
            if not success or not isinstance(info, dict):
                return None
            ip = info.get("ip")
            # Guard the type explicitly (like _resolve_management_ip in ip_addresses_view) rather
            # than letting a non-string ip raise AttributeError into the broad except below: a
            # malformed non-string ip is "no management IP", not an unexpected failure to swallow.
            if not isinstance(ip, str):
                return None
            return ip.strip() or None
        except Exception:  # pragma: no cover - defensive
            return None

    @staticmethod
    def _same_host(a, b):
        """True if two address strings refer to the same host IP."""
        return same_host(a, b)

    def _build_interface_maps(self, obj, server_key):
        """
        Index the object's *current* interfaces by LibreNMS port id and by name.

        Used to re-resolve the target interface at sync time instead of trusting the cached
        ``interface_url`` (see ``_match_interface``).

        For a Device in a Virtual Chassis, authorized member interfaces are indexed (not just the
        viewed member's), matching the shared stable-ID interface resolver:
        LibreNMS treats a VC as one logical device, so a VC member IP can legitimately resolve to an
        interface on another member by stable port id. For the name fallback the viewed object's own
        interface wins. A name shared only by sibling members (none on the viewed object) stays
        ambiguous (None) so the address can't silently rebind to an arbitrary member; duplicate port
        ids are always ambiguous.

        Args:
            obj (Device | VirtualMachine): The synced object whose current interfaces to index.
            server_key (str): LibreNMS server key scoping the per-server librenms_id lookup.

        Returns:
            tuple[dict, dict, dict]: The (by_librenms_id, by_name, by_pk) maps; an ambiguous
                id/name key maps to None. ``by_pk`` keys the same interface set by string PK so a
                cached ``interface_url`` (which survives a rename) can still resolve the target.
        """
        if isinstance(obj, Device):
            # Route member expansion through the shared helper (returns [obj] when not in a VC)
            # so this can't drift from the VC member set used by the interface-sync path.
            member_devices = get_virtual_chassis_members(obj)
            interfaces = list(self.restricted_queryset(Interface).filter(device__in=member_devices))
            obj_device_id = obj.pk
        else:
            interfaces = list(self.restricted_queryset(VMInterface).filter(virtual_machine=obj))
            obj_device_id = None
        by_librenms_id = {}
        by_name = {}
        by_pk = {}
        for iface in interfaces:
            # Key by PK for the cached-interface_url fallback (rename-safe identity). Scoped to
            # this object's (and its VC members') interfaces, so a stale URL can never bind the
            # address to an unrelated device's interface — stricter than the old direct .get(id=).
            by_pk[str(iface.pk)] = iface
            lib_id = get_librenms_device_id(iface, server_key, auto_save=False)
            if lib_id is not None:
                key = str(lib_id)
                if key in by_librenms_id:
                    # Two current interfaces carry the same stored port id — we can't tell which
                    # one the IP belongs to. Mark the id ambiguous (None) so _match_interface fails
                    # the row safe instead of binding the address to an arbitrary interface.
                    by_librenms_id[key] = None
                else:
                    by_librenms_id[key] = iface
            # Name fallback: the VIEWED object's OWN interface always wins. NetBox enforces a unique
            # (device, name), so obj has at most one interface of a given name, and binding the IP to
            # it matches exactly what the rendered table shows (the render indexes only obj.interfaces).
            # A sibling VC member only fills a name obj doesn't own; a collision among siblings alone
            # (none on obj) stays ambiguous (None) so the address can't rebind to an arbitrary member.
            iface_is_obj = obj_device_id is not None and getattr(iface, "device_id", None) == obj_device_id
            if iface_is_obj:
                by_name[iface.name] = iface
            elif iface.name not in by_name:
                by_name[iface.name] = iface
            elif by_name[iface.name] is not None and getattr(by_name[iface.name], "device_id", None) != obj_device_id:
                by_name[iface.name] = None
        return by_librenms_id, by_name, by_pk

    @staticmethod
    def _match_interface(ip_data, by_librenms_id, by_name, by_pk=None):
        """
        Resolve the NetBox interface for a cached IP row against current state.

        The cached ``interface_url`` is enrichment captured when the rows were fetched, so an
        interface synced *afterwards* is missed and the sync would wrongly report "no interface"
        until a manual cache refresh. The rendered table re-enriches on every load (so it already
        shows the link); matching here on the stable LibreNMS port id (preferred), then interface
        name, then the cached ``interface_url`` PK keeps the sync consistent with what the user
        sees. The PK fallback is what recovers a *renamed* interface that has no stored port id
        (e.g. the common VMInterface case): its name no longer matches the cached LibreNMS name,
        but the PK in ``interface_url`` survives the rename — without it the row is silently
        skipped (regression vs. the pre-refactor code that resolved purely by ``interface_url``).

        Args:
            ip_data (dict): The cached IP row (carries ``port_id``, ``interface_name`` and
                ``interface_url``).
            by_librenms_id (dict): Current interfaces keyed by LibreNMS port id (str).
            by_name (dict): Current interfaces keyed by name.
            by_pk (dict | None): Current interfaces keyed by string PK, scoped to the object's own
                (and VC members') interfaces; used for the rename-safe ``interface_url`` fallback.

        Returns:
            Interface | VMInterface | None: The matched interface, or None if none resolves.
        """
        port_id = ip_data.get("port_id")
        if port_id is not None and str(port_id) in by_librenms_id:
            iface = by_librenms_id[str(port_id)]
            if iface is not None:
                return iface
            # None marks an ambiguous port id (>1 interface shares it). Fall through to the name /
            # interface_url match rather than skipping the row — the render path does the same
            # (_add_interface_info_to_ip drops the ambiguous id and links by name), so returning
            # None here would skip a row the table shows linked. Safe because by_name is itself
            # fail-closed: the object's own interface wins and a sibling-only name collision maps
            # to None, so the fall-through can't bind the address to an arbitrary interface.
        name = ip_data.get("interface_name")
        if name and by_name.get(name) is not None:
            return by_name[name]
        # Rename-safe fallback: the cached interface_url PK still points at the (renamed)
        # interface. Scope to by_pk (the object's own interfaces) so a stale URL can't bind the
        # address to an unrelated device's interface.
        interface_url = ip_data.get("interface_url")
        if interface_url and by_pk:
            pk = interface_url.rstrip("/").rsplit("/", 1)[-1]
            if pk in by_pk:
                return by_pk[pk]
        return None

    @staticmethod
    def _set_primary_ip(obj, ip_obj):
        """
        Point ``obj.primary_ip4``/``primary_ip6`` (by family) at *ip_obj*.

        The caller guarantees ``ip_obj`` is assigned to one of the object's interfaces, so this
        satisfies NetBox's ``primary_ip`` constraint.

        Args:
            obj (Device | VirtualMachine): The object whose primary IP to set.
            ip_obj (IPAddress): The address to set as primary (already interface-assigned).

        Returns:
            bool: True if the primary IP changed, False if it was already set.
        """
        # ip_family(), not ip_obj.family: NetBox 4.4's property raises AttributeError on the
        # in-memory str address of a freshly created IPAddress, failing the whole IP sync row.
        field = "primary_ip6" if ip_family(ip_obj) == 6 else "primary_ip4"
        if getattr(obj, f"{field}_id") == ip_obj.pk:
            return False
        setattr(obj, field, ip_obj)
        obj.save()
        return True

    def process_ip_sync(self, request, selected_ips, cached_ips, obj, object_type):
        """Create or update IP addresses in NetBox from cached LibreNMS data.

        When the "set Primary IP" toggle is on, the synced IP that matches the
        LibreNMS management IP — and ends up assigned to one of the object's
        interfaces — is also set as the object's ``primary_ip4``/``primary_ip6``.
        """
        results = {
            "created": [],
            "updated": [],
            "unchanged": [],
            "failed": [],
            "primary_set": [],
            "primary_no_interface": [],
            "primary_interface_not_eligible": [],
            "skipped_no_interface": [],
            "errors": {},
        }

        set_primary = resolve_set_primary_ip(request)
        mgmt_ip = self.get_management_ip(obj) if set_primary else None

        # Re-resolve interfaces from current NetBox state (not the cached
        # interface_url) so an interface synced after these rows were cached is
        # picked up without a manual cache refresh.
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        interfaces_by_librenms_id, interfaces_by_name, interfaces_by_pk = self._build_interface_maps(obj, server_key)

        for ip_address in selected_ips:
            try:
                # Per-IP savepoint so one bad address rolls back only itself and
                # surfaces a real error, instead of poisoning the whole batch.
                with transaction.atomic():
                    ip_data = next(ip for ip in cached_ips if ip["ip_address"] == ip_address)

                    vrf = self.get_vrf_selection(request, ip_address)

                    interface = self._match_interface(
                        ip_data, interfaces_by_librenms_id, interfaces_by_name, interfaces_by_pk
                    )

                    # This row ends in obj.save() (primary_ip) when it matches the management
                    # address, so it takes BOTH an ipam_ipaddress and a dcim_device row lock.
                    is_primary_candidate = bool(mgmt_ip) and self._same_host(ip_data["ip_address"], mgmt_ip)

                    if interface is None:
                        # No matching NetBox interface — the row is stale, the interface isn't
                        # synced yet, or _match_interface refused an ambiguous port_id. Writing
                        # here would either drop an existing IP's binding (assigned_object=None)
                        # or create an unassigned/global address, both of which violate the
                        # interface-assigned model. Skip the row instead of corrupting state.
                        if is_primary_candidate:
                            results["primary_no_interface"].append(ip_address)
                        else:
                            results["skipped_no_interface"].append(ip_address)
                        continue

                    if is_primary_candidate:
                        # Lock obj's row BEFORE the address write, so this path takes the same
                        # Device -> IPAddress lock order as the migrate move views. The reverse
                        # order closes a deadlock cycle with a concurrent donor move.
                        if self.relock_scoped_row(type(obj), pk=obj.pk) is None:
                            raise type(obj).DoesNotExist(f"{type(obj).__name__} {obj.pk} no longer exists")
                        # Re-read the primary_ip ids from the locked row. A stale in-memory value
                        # would make _set_primary_ip skip a set it must perform.
                        obj.refresh_from_db()

                    ip_with_mask = ip_data["ip_with_mask"]

                    # Scope the lookup to the target VRF: the same address can
                    # legitimately exist in multiple VRFs, and matching on address
                    # alone would hijack an unrelated IP and rewrite its VRF.
                    existing_ip = IPAddress.objects.filter(address=ip_with_mask, vrf=vrf).first()

                    if existing_ip:
                        if not self.restricted_queryset(IPAddress, "change").filter(pk=existing_ip.pk).exists():
                            raise ValueError(
                                "Existing IP address is no longer available or you do not have permission to change it."
                            )
                        # The scope check does not lock, so a concurrent assignment or VRF change
                        # could commit between it and the save below and be overwritten. Re-read
                        # the authorized row under a lock and decide from that.
                        existing_ip = self.relock_scoped_row(IPAddress, pk=existing_ip.pk)
                        if existing_ip is None:
                            raise ValueError(
                                "Existing IP address is no longer available or you do not have permission to change it."
                            )
                        if existing_ip.assigned_object != interface or existing_ip.vrf != vrf:
                            existing_ip.assigned_object = interface
                            existing_ip.vrf = vrf
                            existing_ip.save()
                            results["updated"].append(ip_address)
                        else:
                            results["unchanged"].append(ip_address)
                        ip_obj = existing_ip
                    else:
                        ip_obj = IPAddress.objects.create(
                            address=ip_with_mask,
                            assigned_object=interface,
                            status="active",
                            vrf=vrf,
                        )
                        results["created"].append(ip_address)

                    # Primary-IP auto-match for the management IP. The no-interface case is
                    # handled above (the row was skipped before any write), so here the IP is
                    # guaranteed interface-assigned and can satisfy NetBox's primary constraint.
                    if is_primary_candidate:
                        # _build_interface_maps indexes ALL VC member interfaces, so `interface`
                        # can belong to a sibling member. NetBox's Device.clean() accepts a
                        # primary IP on any same-VC member's non-mgmt-only interface
                        # (vc_interfaces(if_master=False)) — mirror that exactly: refuse only an
                        # interface NetBox itself would reject (outside obj's VC, or a sibling's
                        # mgmt-only interface), instead of refusing every sibling match on the
                        # very VC case _build_interface_maps exists to support.
                        if (
                            isinstance(obj, Device)
                            and isinstance(interface, Interface)
                            and interface.device_id != obj.pk
                            and not (
                                obj.virtual_chassis_id is not None
                                and interface.device is not None
                                and interface.device.virtual_chassis_id == obj.virtual_chassis_id
                                and not interface.mgmt_only
                            )
                        ):
                            results["primary_interface_not_eligible"].append(ip_address)
                        elif self._set_primary_ip(obj, ip_obj):
                            results["primary_set"].append(ip_address)

            except Exception as exc:
                logger.warning("IP sync failed for %s: %s", ip_address, exc, exc_info=True)
                results["failed"].append(ip_address)
                results["errors"][ip_address] = str(exc) or exc.__class__.__name__

        return results

    def display_sync_results(self, request, results):
        """Display flash messages summarizing the IP sync results."""
        if results["created"]:
            messages.success(request, f"Created IP addresses: {', '.join(results['created'])}")
        if results["updated"]:
            messages.success(request, f"Updated IP addresses: {', '.join(results['updated'])}")
        if results.get("primary_set"):
            messages.success(request, f"Set as Primary IP: {', '.join(results['primary_set'])}")
        if results.get("primary_no_interface"):
            messages.warning(
                request,
                "Primary IP not set for "
                f"{', '.join(results['primary_no_interface'])} — no NetBox interface for this IP. "
                "Sync interfaces first, then re-run.",
            )
        if results.get("primary_interface_not_eligible"):
            messages.warning(
                request,
                "Primary IP not set for "
                f"{', '.join(results['primary_interface_not_eligible'])} — the matched interface is not eligible "
                "(it is outside this virtual chassis or is a management-only interface).",
            )
        if results.get("skipped_no_interface"):
            messages.warning(
                request,
                "Skipped (no matching NetBox interface): "
                f"{', '.join(results['skipped_no_interface'])}. Sync interfaces first, then re-run.",
            )
        if results["unchanged"]:
            messages.warning(
                request,
                f"IP addresses already exist: {', '.join(results['unchanged'])}",
            )
        if results["failed"]:
            errors = results.get("errors", {})
            detail = ", ".join(f"{ip} ({errors[ip]})" if errors.get(ip) else ip for ip in results["failed"])
            messages.error(request, f"Failed to sync IP addresses: {detail}")
