import logging
from urllib.parse import quote_plus

from dcim.models import Device, Interface, VirtualChassis
from django.contrib import messages
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from ipam.models import VRF, IPAddress
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.constants import is_supported_interface_name_field
from netbox_librenms_plugin.interface_sync import resolve_or_create_interface_from_port
from netbox_librenms_plugin.ip_addressing import parse_address_with_prefix
from netbox_librenms_plugin.utils import (
    acquire_advisory_transaction_lock,
    get_librenms_device_id,
    get_migrated_to_marker,
    get_virtual_chassis_members,
    ip_family,
    normalize_librenms_port_id,
    resolve_interface_row_device,
    resolve_create_missing_interfaces,
    resolve_set_primary_ip,
    same_host,
    syncable_interface_name,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)

logger = logging.getLogger(__name__)

IP_CONFLICT_SIGNING_SALT = "netbox_librenms_plugin.ip_address_conflict"


def _ip_host_lock_identity(parsed, vrf) -> str:
    """Return the advisory-lock identity for one canonical host and VRF."""
    vrf_identity = vrf.pk if vrf is not None else 0
    return f"netbox-librenms-plugin:ip-host:{parsed.ip}:vrf:{vrf_identity}"


def _acquire_ip_host_lock(parsed, vrf) -> None:
    """Serialize writes for one canonical host and VRF."""
    acquire_advisory_transaction_lock(_ip_host_lock_identity(parsed, vrf))


class SyncIPAddressesView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """Synchronize IP addresses from LibreNMS cache into NetBox."""

    required_object_permissions = {
        "POST": [
            ("add", IPAddress),
            ("change", IPAddress),
        ],
    }

    @staticmethod
    def _create_missing_interfaces(request):
        """Return true when this request may materialize missing interfaces."""
        return resolve_create_missing_interfaces(request)

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
        if self._create_missing_interfaces(self.request):
            perms.extend((("add", interface_model), ("change", interface_model)))
        # A per-row VRF is resolved by client-supplied id through a restricted queryset. Only
        # demand its view permission when one is actually posted, so the common no-VRF sync
        # is not gated on a permission it never uses.
        if any(key.startswith("vrf_") and value for key, value in self.request.POST.items()):
            perms.append(("view", VRF))
        return {"POST": perms}

    def get_selected_ips(self, request):
        """Return unique selected canonical IP row identifiers from POST data."""
        sync_one = request.POST.get("sync_one")
        if sync_one:
            return [sync_one]
        return list(dict.fromkeys(value for value in request.POST.getlist("select") if value))

    def get_vrf_selection(self, request, ip_address):
        """Return the selected VRF, or None only when the row requests no VRF."""
        vrf_id = request.POST.get(f"vrf_{ip_address}")

        if not vrf_id:
            return None

        try:
            return self.restricted_queryset(VRF).get(pk=vrf_id)
        except (VRF.DoesNotExist, TypeError, ValueError):
            raise ValueError("Selected VRF is no longer available or you do not have permission to view it.") from None

    def get_cached_ip_snapshot(self, obj, *, require_create_metadata=False):
        """Return the cached LibreNMS IP snapshot when it satisfies the requested contract."""
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        cached_data = cache.get(self.get_cache_key(obj, "ip_addresses", server_key))
        if not isinstance(cached_data, dict) or not isinstance(cached_data.get("ip_addresses"), list):
            return None
        if require_create_metadata and (
            not isinstance(cached_data.get("ports_by_id"), dict)
            or not is_supported_interface_name_field(cached_data.get("interface_name_field"))
        ):
            return None
        return cached_data

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
        # POST-resolved key; without it (the failed-rebind path, which redirects before
        # _post_server_key is assigned) read the librenms_api property, which reuses an already
        # bound client and otherwise builds the active/default one. Swallow a construction failure
        # (a misconfigured default) so we still redirect gracefully instead of raising. The
        # redirect must carry the resolved server_key (see test_unknown_server_key_errors_without_500).
        server_key = getattr(self, "_post_server_key", None)
        if not server_key:
            try:
                server_key = self.librenms_api.server_key
            except Exception:  # a redirect helper must degrade, never 500 (misconfigured default)
                server_key = None
        url = f"{reverse(url_name, args=[obj.pk])}?tab=ipaddresses"
        if server_key:
            url += f"&server_key={quote_plus(server_key)}"
        return url

    def redirect_to_ip_tab(self, request, obj):
        """Reload the full sync page for HTMX requests, or redirect a normal request."""
        url = self.get_ip_tab_url(obj)
        if request.headers.get("HX-Request") == "true":
            return HttpResponse("", headers={"HX-Redirect": url})
        return redirect(url)

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
            return self.redirect_to_ip_tab(request, obj)
        self._post_server_key = post_server_key

        cached_snapshot = self.get_cached_ip_snapshot(
            obj,
            require_create_metadata=self._create_missing_interfaces(request),
        )

        if not cached_snapshot or not cached_snapshot["ip_addresses"]:
            messages.error(request, "Cache has expired. Please refresh the IP data.")
            return self.redirect_to_ip_tab(request, obj)
        cached_ips = cached_snapshot["ip_addresses"]

        force_intents, intent_errors = self._load_force_intents(request, obj, object_type, post_server_key)
        if request.POST.get("force_all"):
            selected_ips = list(force_intents)
        elif request.POST.getlist("force_conflict"):
            # The checkbox and its signed intent post together, but an expired intent drops out of
            # force_intents while the checkbox value survives. The confirmation form carries no
            # vrf_<row_id> field, so such a row would be classified against the Global VRF instead
            # of the one the user confirmed. Keep only rows whose intent is still valid.
            confirmed = set()
            for value in request.POST.getlist("force_conflict"):
                try:
                    confirmed.add(str(parse_address_with_prefix(value)))
                except ValueError:
                    continue
            selected_ips = [row_id for row_id in force_intents if row_id in confirmed]
        else:
            selected_ips = self.get_selected_ips(request)

        if not selected_ips:
            if intent_errors:
                for error in dict.fromkeys(intent_errors.values()):
                    messages.error(request, error)
                return self.redirect_to_ip_tab(request, obj)
            messages.error(request, "No IP addresses selected for synchronization.")
            return self.redirect_to_ip_tab(request, obj)

        results = self.process_ip_sync(
            request,
            selected_ips,
            cached_ips,
            obj,
            object_type,
            force_intents=force_intents,
            cached_ports_by_id=cached_snapshot.get("ports_by_id") or {},
            interface_name_field=cached_snapshot.get("interface_name_field"),
        )
        self.display_sync_results(request, results)
        for error in dict.fromkeys(intent_errors.values()):
            messages.error(request, error)

        if results["conflicts"]:
            conflict_context = {
                "conflicts": results["conflicts"],
                "has_forceable_conflicts": any(conflict["forceable"] for conflict in results["conflicts"]),
                "object_type": object_type,
                "object": obj,
                "server_key": post_server_key,
                "set_primary_ip": resolve_set_primary_ip(request),
                "create_missing_interfaces": resolve_create_missing_interfaces(request),
                "cancel_url": self.get_ip_tab_url(obj),
            }
            if request.headers.get("HX-Request") != "true":
                conflict_context["full_page"] = True
                return render(
                    request,
                    "netbox_librenms_plugin/ip_address_conflicts_page.html",
                    conflict_context,
                )
            return render(
                request,
                "netbox_librenms_plugin/htmx/ip_address_conflicts.html",
                conflict_context,
            )

        return self.redirect_to_ip_tab(request, obj)

    @staticmethod
    def _interface_identity(interface):
        """Return a signed-payload-safe interface identity."""
        return {"model": interface._meta.label_lower, "pk": str(interface.pk)}

    @staticmethod
    def _ip_state(ip_obj):
        """Return the mutable IP state protected by a conflict intent."""
        return {
            "address": str(ip_obj.address),
            "vrf_id": ip_obj.vrf_id,
            "assigned_object_type_id": ip_obj.assigned_object_type_id,
            "assigned_object_id": str(ip_obj.assigned_object_id) if ip_obj.assigned_object_id is not None else None,
        }

    def _load_force_intents(self, request, obj, object_type, server_key):
        """Validate submitted conflict intents and return them by canonical row id."""
        intents = {}
        errors = {}
        for token in request.POST.getlist("conflict_intent"):
            try:
                payload = signing.loads(token, salt=IP_CONFLICT_SIGNING_SALT, max_age=3600)
                row_id = str(parse_address_with_prefix(payload["row_id"]))
                if (
                    payload.get("object_type") != object_type
                    or str(payload.get("object_pk")) != str(obj.pk)
                    or payload.get("server_key") != server_key
                ):
                    raise signing.BadSignature("Conflict intent belongs to another sync context.")
                if row_id in intents:
                    raise signing.BadSignature("Duplicate conflict intent.")
                intents[row_id] = payload
            except (KeyError, TypeError, ValueError, signing.BadSignature, signing.SignatureExpired):
                errors["unknown"] = (
                    "IP address confirmation is invalid or has expired. Refresh the IP data and try again."
                )
        return intents, errors

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
            # get_device_info returns (True, ...) only after its own dict check, so a truthy
            # success already guarantees a dict payload here.
            if not success:
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
            # None marks an ambiguous port id (>1 interface shares it). Fall through to the name
            # match, but never use the cached URL when a stable match was explicitly ambiguous.
        name = ip_data.get("interface_name")
        if name and name in by_name:
            if by_name[name] is not None:
                return by_name[name]
            return None
        if port_id is not None and str(port_id) in by_librenms_id:
            return None
        # Rename-safe fallback: the cached interface_url PK still points at the (renamed)
        # interface. Scope to by_pk (the object's own interfaces) so a stale URL can't bind the
        # address to an unrelated device's interface.
        interface_url = ip_data.get("interface_url")
        if interface_url and by_pk:
            pk = interface_url.rstrip("/").rsplit("/", 1)[-1]
            if pk in by_pk:
                return by_pk[pk]
        return None

    def _lock_interface_owner_scope(self, obj):
        """Lock the current interface-owner scope in the shared chassis-first order."""
        if isinstance(obj, VirtualMachine):
            locked = self.restricted_queryset(VirtualMachine).select_for_update(of=("self",)).filter(pk=obj.pk).first()
            return locked, [locked] if locked is not None else []

        virtual_chassis_id = obj.virtual_chassis_id
        owner_ids = {obj.pk}
        if virtual_chassis_id is not None:
            locked_chassis = self.relock_scoped_row(VirtualChassis, pk=virtual_chassis_id)
            if locked_chassis is None:
                return None, []
            owner_ids.update(Device.objects.filter(virtual_chassis_id=virtual_chassis_id).values_list("pk", flat=True))
        locked_by_id = {
            device.pk: device
            for device in self.restricted_queryset(Device)
            .select_for_update(of=("self",))
            .filter(pk__in=owner_ids)
            .order_by("pk")
        }
        locked_obj = locked_by_id.get(obj.pk)
        if locked_obj is None or locked_obj.virtual_chassis_id != virtual_chassis_id:
            return None, []
        members = [device for device in locked_by_id.values() if device.virtual_chassis_id == virtual_chassis_id]
        return locked_obj, members

    def _build_interface_creation_state(self, obj, server_key):
        """Lock one interface-owner scope and materialize its current interface catalog."""
        locked_obj, locked_members = self._lock_interface_owner_scope(obj)
        if locked_obj is None:
            raise ValueError("The interface owner is no longer available in your view scope.")

        state = {
            "locked_obj": locked_obj,
            "locked_members": locked_members,
            "members_by_id": {member.pk: member for member in locked_members},
            "members_by_position": (
                {member.vc_position: member for member in locked_members if member.vc_position is not None}
                if isinstance(locked_obj, Device)
                else {}
            ),
            "interfaces_by_port_id": {},
        }
        if isinstance(locked_obj, Device):
            current_interfaces = list(
                Interface.objects.filter(device_id__in=state["members_by_id"]).select_related("device")
            )
            for interface in current_interfaces:
                bound_id = get_librenms_device_id(interface, server_key, auto_save=False)
                if bound_id is not None:
                    state["interfaces_by_port_id"].setdefault(bound_id, []).append(interface)
        return state

    @staticmethod
    def _cached_port(cached_ports_by_id, port_id):
        """Return one cached port whose canonical ID matches the IP row."""
        normalized_id = normalize_librenms_port_id(port_id)
        if normalized_id is None or not isinstance(cached_ports_by_id, dict):
            return None
        matches = [
            port
            for raw_id, port in cached_ports_by_id.items()
            if normalize_librenms_port_id(raw_id) == normalized_id and isinstance(port, dict)
        ]
        return matches[0] if len(matches) == 1 else None

    def _create_interface_for_ip(
        self,
        obj,
        ip_data,
        cached_ports_by_id,
        interface_name_field,
        server_key,
        interfaces_by_librenms_id,
        interface_creation_state=None,
    ):
        """Create one missing interface from the cached port that owns an IP row."""
        if not is_supported_interface_name_field(interface_name_field):
            raise ValueError("The cached interface naming field is missing or invalid. Refresh the IP data.")
        port = self._cached_port(cached_ports_by_id, ip_data.get("port_id"))
        if port is None or port.get("_source") == "oob":
            raise ValueError("The cached LibreNMS port is missing or ambiguous. Refresh the IP data.")
        if normalize_librenms_port_id(port.get("port_id")) != normalize_librenms_port_id(ip_data.get("port_id")):
            raise ValueError("The cached LibreNMS port identity changed. Refresh the IP data.")
        interface_name = port.get(interface_name_field)
        same_name_port_ids = {
            normalize_librenms_port_id(candidate.get("port_id"))
            for candidate in cached_ports_by_id.values()
            if isinstance(candidate, dict) and candidate.get(interface_name_field) == interface_name
        }
        same_name_port_ids.discard(None)
        # The name rule lives in one helper, so this reader cannot drift from the writer that
        # created the row; ambiguity stays a separate concern.
        if syncable_interface_name(port, interface_name_field) is None or len(same_name_port_ids) != 1:
            raise ValueError("The cached LibreNMS interface name is missing or ambiguous. Refresh the IP data.")

        if interface_creation_state is None:
            interface_creation_state = self._build_interface_creation_state(obj, server_key)
        locked_obj = interface_creation_state["locked_obj"]
        if isinstance(locked_obj, Device):
            owner = resolve_interface_row_device(
                locked_obj,
                port,
                interface_name_field,
                interfaces_by_port_id=interface_creation_state["interfaces_by_port_id"],
                members_by_position=interface_creation_state["members_by_position"],
                members_by_id=interface_creation_state["members_by_id"],
                return_device_on_failure=False,
            )
            if owner is None or owner.pk not in interface_creation_state["members_by_id"]:
                raise ValueError("The LibreNMS port does not identify one viewable chassis member.")
            if get_migrated_to_marker(locked_obj, server_key) or get_migrated_to_marker(owner, server_key):
                raise ValueError("The interface owner is read-only because it was migrated.")
            interface_model = Interface
        else:
            owner = locked_obj
            interface_model = VMInterface

        interface = resolve_or_create_interface_from_port(
            owner,
            port,
            server_key=server_key,
            interface_name_field=interface_name_field,
            changeable_queryset=self.restricted_queryset(interface_model, "change"),
            viewable_queryset=self.restricted_queryset(interface_model, "view"),
        )
        port_id_key = str(normalize_librenms_port_id(port.get("port_id")))
        interfaces_by_librenms_id[port_id_key] = interface
        interface_creation_state["interfaces_by_port_id"].setdefault(
            normalize_librenms_port_id(port.get("port_id")), []
        ).append(interface)
        return interface, interface_creation_state

    def _lock_target_interface(self, obj, interface, interface_creation_state=None):
        """Lock and recheck the current interface owner before an IP assignment."""
        if interface_creation_state is None:
            locked_obj, locked_owners = self._lock_interface_owner_scope(obj)
        else:
            locked_obj = interface_creation_state["locked_obj"]
            locked_owners = interface_creation_state["locked_members"]
        if locked_obj is None:
            return None
        if isinstance(locked_obj, Device):
            if not isinstance(interface, Interface):
                return None
            allowed_owner_ids = {owner.pk for owner in locked_owners}
            locked_interface = (
                self.restricted_queryset(Interface, "view")
                .select_for_update(of=("self",))
                .filter(pk=interface.pk)
                .first()
            )
            if locked_interface is None or locked_interface.device_id not in allowed_owner_ids:
                return None
            owner_by_id = {owner.pk: owner for owner in locked_owners}
            server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
            if get_migrated_to_marker(locked_obj, server_key) or get_migrated_to_marker(
                owner_by_id[locked_interface.device_id], server_key
            ):
                raise ValueError("The interface owner is read-only because it was migrated.")
            return locked_interface
        if not isinstance(interface, VMInterface):
            return None
        locked_interface = (
            self.restricted_queryset(VMInterface, "view")
            .select_for_update(of=("self",))
            .filter(pk=interface.pk)
            .first()
        )
        if locked_interface is None or locked_interface.virtual_machine_id != locked_obj.pk:
            return None
        return locked_interface

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

    @staticmethod
    def _cached_ip_index(cached_ips):
        """Index cached rows by canonical CIDR and record duplicate row identities."""
        index = {}
        duplicates = set()
        for row in cached_ips:
            if not isinstance(row, dict):
                continue
            try:
                row_id = str(parse_address_with_prefix(row.get("ip_with_mask")))
            except (TypeError, ValueError):
                continue
            if row_id in index:
                duplicates.add(row_id)
            else:
                index[row_id] = row
        return index, duplicates

    def _prelock_ip_hosts(self, request, selected_ips, cached_index, duplicate_cached_rows, force_intents):
        """Acquire every valid batch host lock in one deterministic order.

        The transaction remains open across the batch. Acquire host advisory locks before the
        first Device, virtual-chassis, VM, or interface row lock so opposite row orders cannot
        hold one host lock while waiting for a shared owner scope.
        """
        lock_entries = {}
        for selected_ip in selected_ips:
            try:
                row_id = str(parse_address_with_prefix(selected_ip))
                if row_id in duplicate_cached_rows or row_id not in cached_index:
                    continue
                parsed = parse_address_with_prefix(row_id)
                force_payload = force_intents.get(row_id)
                vrf = (
                    self._resolve_vrf_id(force_payload.get("target_vrf_id"))
                    if force_payload is not None
                    else self.get_vrf_selection(request, row_id)
                )
            except (TypeError, ValueError):
                # The row will report its existing per-row validation error in the main loop.
                continue
            lock_identity = _ip_host_lock_identity(parsed, vrf)
            lock_entries[lock_identity] = (parsed, vrf)

        for lock_identity in sorted(lock_entries):
            parsed, vrf = lock_entries[lock_identity]
            _acquire_ip_host_lock(parsed, vrf)
        return set(lock_entries)

    @staticmethod
    def _vrf_label(vrf):
        """Return a user-facing VRF label."""
        return vrf.name if vrf is not None else "Global"

    def _conflict_intent(self, *, row_id, obj, object_type, server_key, interface, vrf, ip_obj, kind):
        """Sign the complete state required to confirm one destructive IP change."""
        payload = {
            "row_id": row_id,
            "object_type": object_type,
            "object_pk": str(obj.pk),
            "server_key": server_key,
            "target_interface": self._interface_identity(interface),
            "target_vrf_id": vrf.pk if vrf is not None else None,
            "ip_pk": str(ip_obj.pk),
            "ip_state": self._ip_state(ip_obj),
            "kind": kind,
        }
        return signing.dumps(payload, salt=IP_CONFLICT_SIGNING_SALT, compress=True)

    def _resolve_vrf_id(self, vrf_id):
        """Resolve a signed or posted VRF id through the current view scope."""
        if vrf_id is None:
            return None
        try:
            return self.restricted_queryset(VRF).get(pk=vrf_id)
        except (VRF.DoesNotExist, TypeError, ValueError):
            raise ValueError("Selected VRF is no longer available or you do not have permission to view it.") from None

    @staticmethod
    def _host_rows(vrf, parsed):
        """Return current rows for one host inside one VRF."""
        return list(IPAddress.objects.filter(address__net_host=str(parsed.ip), vrf=vrf).order_by("pk"))

    def _locked_changeable_ip(self, pk):
        """Lock an IP row only when it remains in the caller's change scope."""
        return self.restricted_queryset(IPAddress, "change").select_for_update(of=("self",)).filter(pk=pk).first()

    def _build_conflict(
        self,
        *,
        row_id,
        obj,
        object_type,
        server_key,
        interface,
        vrf,
        reason,
        ip_obj=None,
        kind=None,
    ):
        """Return one user-facing conflict and its signed force intent when safe."""
        conflict = {
            "row_id": row_id,
            "address": row_id,
            "target_interface": str(interface),
            "target_vrf": self._vrf_label(vrf),
            "reason": reason,
            "forceable": ip_obj is not None and kind is not None,
            "intent": "",
        }
        if conflict["forceable"]:
            conflict["intent"] = self._conflict_intent(
                row_id=row_id,
                obj=obj,
                object_type=object_type,
                server_key=server_key,
                interface=interface,
                vrf=vrf,
                ip_obj=ip_obj,
                kind=kind,
            )
        return conflict

    def _classify_ip_change(self, *, row_id, parsed, ip_data, obj, object_type, server_key, interface, vrf):
        """Classify a current VRF-scoped IP state without mutating it."""
        target_rows = self._host_rows(vrf, parsed)
        exact_rows = [row for row in target_rows if str(row.address) == row_id]
        other_prefix_rows = [row for row in target_rows if str(row.address) != row_id]

        if other_prefix_rows:
            if len(other_prefix_rows) == 1 and not exact_rows:
                existing_ip = other_prefix_rows[0]
                if not self.restricted_queryset(IPAddress, "change").filter(pk=existing_ip.pk).exists():
                    return (
                        None,
                        None,
                        self._build_conflict(
                            row_id=row_id,
                            obj=obj,
                            object_type=object_type,
                            server_key=server_key,
                            interface=interface,
                            vrf=vrf,
                            reason="The existing IP address is outside your change scope.",
                        ),
                    )
                return (
                    None,
                    None,
                    self._build_conflict(
                        row_id=row_id,
                        obj=obj,
                        object_type=object_type,
                        server_key=server_key,
                        interface=interface,
                        vrf=vrf,
                        reason=(
                            f"Change the existing IP address from {existing_ip.address} to {row_id} "
                            "because it has a different prefix length, and assign it to the selected interface."
                        ),
                        ip_obj=existing_ip,
                        kind="change_prefix",
                    ),
                )
            return (
                None,
                None,
                self._build_conflict(
                    row_id=row_id,
                    obj=obj,
                    object_type=object_type,
                    server_key=server_key,
                    interface=interface,
                    vrf=vrf,
                    reason="The destination VRF already contains this host with a different prefix length.",
                ),
            )
        if len(exact_rows) > 1:
            return (
                None,
                None,
                self._build_conflict(
                    row_id=row_id,
                    obj=obj,
                    object_type=object_type,
                    server_key=server_key,
                    interface=interface,
                    vrf=vrf,
                    reason="The destination VRF contains duplicate matching IP addresses.",
                ),
            )
        if exact_rows:
            existing_ip = exact_rows[0]
            if existing_ip.assigned_object == interface:
                return existing_ip, "unchanged", None
            if not self.restricted_queryset(IPAddress, "change").filter(pk=existing_ip.pk).exists():
                return (
                    None,
                    None,
                    self._build_conflict(
                        row_id=row_id,
                        obj=obj,
                        object_type=object_type,
                        server_key=server_key,
                        interface=interface,
                        vrf=vrf,
                        reason="The existing IP address is outside your change scope.",
                    ),
                )
            return (
                None,
                None,
                self._build_conflict(
                    row_id=row_id,
                    obj=obj,
                    object_type=object_type,
                    server_key=server_key,
                    interface=interface,
                    vrf=vrf,
                    reason="Reassign the existing IP address to the selected interface.",
                    ip_obj=existing_ip,
                    kind="reassign",
                ),
            )

        source_pk = ip_data.get("netbox_ip_id")
        if source_pk is not None:
            exact_rows_across_vrfs = list(IPAddress.objects.filter(address=row_id).order_by("pk"))
            source_ip = next((row for row in exact_rows_across_vrfs if str(row.pk) == str(source_pk)), None)
            if source_ip is None:
                return (
                    None,
                    None,
                    self._build_conflict(
                        row_id=row_id,
                        obj=obj,
                        object_type=object_type,
                        server_key=server_key,
                        interface=interface,
                        vrf=vrf,
                        reason="The previously matched IP address changed. Refresh the IP data.",
                    ),
                )
            if not self.restricted_queryset(IPAddress, "change").filter(pk=source_ip.pk).exists():
                return (
                    None,
                    None,
                    self._build_conflict(
                        row_id=row_id,
                        obj=obj,
                        object_type=object_type,
                        server_key=server_key,
                        interface=interface,
                        vrf=vrf,
                        reason="The existing IP address is outside your change scope.",
                    ),
                )
            return (
                None,
                None,
                self._build_conflict(
                    row_id=row_id,
                    obj=obj,
                    object_type=object_type,
                    server_key=server_key,
                    interface=interface,
                    vrf=vrf,
                    reason=f"Move the existing IP address to {self._vrf_label(vrf)} and assign it to the selected interface.",
                    ip_obj=source_ip,
                    kind="move_vrf",
                ),
            )

        ip_obj = IPAddress.objects.create(
            address=row_id,
            assigned_object=interface,
            status="active",
            vrf=vrf,
        )
        return ip_obj, "created", None

    def _apply_confirmed_ip_change(self, *, row_id, parsed, payload, interface, vrf):
        """Recheck and apply one signed IP change while the target row is locked."""
        if payload.get("target_interface") != self._interface_identity(interface):
            raise ValueError("The target interface changed. Refresh the IP data and try again.")
        if payload.get("target_vrf_id") != (vrf.pk if vrf is not None else None):
            raise ValueError("The target VRF changed. Refresh the IP data and try again.")

        ip_obj = self._locked_changeable_ip(payload.get("ip_pk"))
        if ip_obj is None:
            raise ValueError("The existing IP address is no longer available in your change scope.")
        if payload.get("ip_state") != self._ip_state(ip_obj):
            raise ValueError("The existing IP address changed after confirmation. Refresh the IP data and try again.")
        kind = payload.get("kind")
        if kind != "change_prefix" and str(ip_obj.address) != row_id:
            raise ValueError("The existing IP address changed after confirmation. Refresh the IP data and try again.")

        target_rows = self._host_rows(vrf, parsed)
        exact_rows = [row for row in target_rows if str(row.address) == row_id]
        other_prefix_rows = [row for row in target_rows if str(row.address) != row_id]
        if other_prefix_rows and kind != "change_prefix":
            raise ValueError("The destination VRF now contains this host with a different prefix length.")

        target_vrf_id = vrf.pk if vrf is not None else None
        if kind == "reassign":
            if len(exact_rows) != 1 or exact_rows[0].pk != ip_obj.pk or ip_obj.vrf_id != target_vrf_id:
                raise ValueError("The destination VRF changed after confirmation. Refresh the IP data and try again.")
        elif kind == "move_vrf":
            if exact_rows or ip_obj.vrf_id == target_vrf_id:
                raise ValueError("The destination VRF changed after confirmation. Refresh the IP data and try again.")
            ip_obj.vrf = vrf
        elif kind == "change_prefix":
            if (
                exact_rows
                or len(other_prefix_rows) != 1
                or other_prefix_rows[0].pk != ip_obj.pk
                or ip_obj.vrf_id != target_vrf_id
            ):
                raise ValueError("The destination VRF changed after confirmation. Refresh the IP data and try again.")
            ip_obj.address = row_id
        else:
            raise ValueError("IP address confirmation is invalid. Refresh the IP data and try again.")

        ip_obj.assigned_object = interface
        ip_obj.save()
        return ip_obj

    def process_ip_sync(
        self,
        request,
        selected_ips,
        cached_ips,
        obj,
        object_type,
        force_intents=None,
        cached_ports_by_id=None,
        interface_name_field=None,
    ):
        """Sync selected IP rows in one transaction with per-row savepoints."""
        with transaction.atomic():
            return self._process_ip_sync(
                request,
                selected_ips,
                cached_ips,
                obj,
                object_type,
                force_intents=force_intents,
                cached_ports_by_id=cached_ports_by_id,
                interface_name_field=interface_name_field,
            )

    def _process_ip_sync(
        self,
        request,
        selected_ips,
        cached_ips,
        obj,
        object_type,
        force_intents=None,
        cached_ports_by_id=None,
        interface_name_field=None,
    ):
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
            "conflicts": [],
        }

        set_primary = resolve_set_primary_ip(request)
        mgmt_ip = self.get_management_ip(obj) if set_primary else None

        # Re-resolve interfaces from current NetBox state (not the cached
        # interface_url) so an interface synced after these rows were cached is
        # picked up without a manual cache refresh.
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        interfaces_by_librenms_id, interfaces_by_name, interfaces_by_pk = self._build_interface_maps(obj, server_key)
        cached_index, duplicate_cached_rows = self._cached_ip_index(cached_ips)
        force_intents = force_intents or {}
        create_missing_interfaces = self._create_missing_interfaces(request)
        cached_ports_by_id = cached_ports_by_id or {}
        interface_creation_state = None
        prelocked_host_locks = self._prelock_ip_hosts(
            request,
            selected_ips,
            cached_index,
            duplicate_cached_rows,
            force_intents,
        )

        for selected_ip in selected_ips:
            row_id = str(selected_ip)
            interface_creation_state_before_row = interface_creation_state
            interface_maps_before_row = (
                (
                    dict(interfaces_by_librenms_id),
                    dict(interfaces_by_name),
                    dict(interfaces_by_pk),
                    (
                        {key: list(value) for key, value in interface_creation_state["interfaces_by_port_id"].items()}
                        if interface_creation_state is not None
                        else None
                    ),
                )
                if create_missing_interfaces
                else None
            )
            try:
                # Per-IP savepoint so one bad address rolls back only itself and
                # surfaces a real error, instead of poisoning the whole batch.
                with transaction.atomic():
                    row_id = str(parse_address_with_prefix(selected_ip))
                    if row_id in duplicate_cached_rows:
                        raise ValueError("The cached snapshot contains duplicate rows for this IP address.")
                    ip_data = cached_index.get(row_id)
                    if ip_data is None:
                        raise ValueError("The selected IP address is no longer present in the cached snapshot.")
                    parsed = parse_address_with_prefix(row_id)

                    force_payload = force_intents.get(row_id)
                    if force_payload is not None:
                        vrf = self._resolve_vrf_id(force_payload.get("target_vrf_id"))
                    else:
                        vrf = self.get_vrf_selection(request, row_id)
                    if _ip_host_lock_identity(parsed, vrf) not in prelocked_host_locks:
                        _acquire_ip_host_lock(parsed, vrf)

                    interface = self._match_interface(
                        ip_data, interfaces_by_librenms_id, interfaces_by_name, interfaces_by_pk
                    )

                    if interface is None and create_missing_interfaces:
                        interface, interface_creation_state = self._create_interface_for_ip(
                            obj,
                            ip_data,
                            cached_ports_by_id,
                            interface_name_field,
                            server_key,
                            interfaces_by_librenms_id,
                            interface_creation_state=interface_creation_state,
                        )
                        interfaces_by_name[interface.name] = interface
                        interfaces_by_pk[str(interface.pk)] = interface

                    if interface is not None:
                        locked_interface = self._lock_target_interface(obj, interface, interface_creation_state)
                        if locked_interface is None:
                            raise ValueError(
                                "The matched NetBox interface is no longer available in your view scope. "
                                "Refresh the IP data and try again."
                            )
                        interface = locked_interface

                    # This row ends in obj.save() (primary_ip) when it matches the management
                    # address, so it takes BOTH an ipam_ipaddress and a dcim_device row lock.
                    is_primary_candidate = bool(mgmt_ip) and self._same_host(row_id, mgmt_ip)

                    if interface is None:
                        # No matching NetBox interface — the row is stale, the interface isn't
                        # synced yet, or _match_interface refused an ambiguous port_id. Writing
                        # here would either drop an existing IP's binding (assigned_object=None)
                        # or create an unassigned/global address, both of which violate the
                        # interface-assigned model. Skip the row instead of corrupting state.
                        if is_primary_candidate:
                            results["primary_no_interface"].append(row_id)
                        else:
                            results["skipped_no_interface"].append(row_id)
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

                    if force_payload is not None:
                        ip_obj = self._apply_confirmed_ip_change(
                            row_id=row_id,
                            parsed=parsed,
                            payload=force_payload,
                            interface=interface,
                            vrf=vrf,
                        )
                        results["updated"].append(row_id)
                    else:
                        ip_obj, outcome, conflict = self._classify_ip_change(
                            row_id=row_id,
                            parsed=parsed,
                            ip_data=ip_data,
                            obj=obj,
                            object_type=object_type,
                            server_key=server_key,
                            interface=interface,
                            vrf=vrf,
                        )
                        if conflict is not None:
                            logger.info(
                                "IP sync skipped %s in VRF %s pending confirmation: %s",
                                row_id,
                                conflict["target_vrf"],
                                conflict["reason"],
                            )
                            results["conflicts"].append(conflict)
                            continue
                        results[outcome].append(row_id)

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
                            results["primary_interface_not_eligible"].append(row_id)
                        elif self._set_primary_ip(obj, ip_obj):
                            results["primary_set"].append(row_id)

            except Exception as exc:
                if interface_maps_before_row is not None:
                    (
                        interfaces_by_librenms_id_before,
                        interfaces_by_name_before,
                        interfaces_by_pk_before,
                        interfaces_by_port_id_before,
                    ) = interface_maps_before_row
                    interfaces_by_librenms_id.clear()
                    interfaces_by_librenms_id.update(interfaces_by_librenms_id_before)
                    interfaces_by_name.clear()
                    interfaces_by_name.update(interfaces_by_name_before)
                    interfaces_by_pk.clear()
                    interfaces_by_pk.update(interfaces_by_pk_before)
                    interface_creation_state = interface_creation_state_before_row
                    if interface_creation_state is not None:
                        interface_creation_state["interfaces_by_port_id"].clear()
                        interface_creation_state["interfaces_by_port_id"].update(
                            {key: list(value) for key, value in interfaces_by_port_id_before.items()}
                        )
                logger.warning("IP sync failed for %s: %s", row_id, exc, exc_info=True)
                results["failed"].append(row_id)
                results["errors"][row_id] = str(exc) or exc.__class__.__name__

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
