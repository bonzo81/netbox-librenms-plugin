import logging
from urllib.parse import quote_plus

from dcim.models import Device, Interface, MACAddress
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.utils import (
    AmbiguousLibreNMSIdError,
    convert_speed_to_kbps,
    find_by_librenms_id,
    get_interface_name_field,
    get_librenms_sync_device,
    normalize_librenms_port_id,
    set_librenms_device_id,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    VlanAssignmentMixin,
)

logger = logging.getLogger(__name__)


class SyncInterfacesView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, VlanAssignmentMixin, CacheMixin, View
):
    """Sync selected interfaces from LibreNMS into NetBox."""

    def get_required_permissions_for_object_type(self, object_type):
        """Return the required permissions based on object type."""
        # The owner is resolved through a restricted queryset (get_object), so its view
        # permission is stated here: a missing grant is an explicit 403, not a 404.
        if object_type == "device":
            return [("view", Device), ("add", Interface), ("change", Interface)]
        elif object_type == "virtualmachine":
            return [("view", VirtualMachine), ("add", VMInterface), ("change", VMInterface)]
        else:
            raise Http404(f"Invalid object type: {object_type}")

    def post(self, request, object_type, object_id):
        """Sync selected interfaces from LibreNMS into NetBox."""
        # Set permissions dynamically based on object type
        self.required_object_permissions = {
            "POST": self.get_required_permissions_for_object_type(object_type),
        }

        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        url_name = (
            "dcim:device_librenms_sync"
            if object_type == "device"
            else "plugins:netbox_librenms_plugin:vm_librenms_sync"
        )
        obj = self.get_object(object_type, object_id)
        self.object = obj  # Store for use in sync methods

        interface_name_field = get_interface_name_field(request)
        self.interface_name_field = interface_name_field

        # Rebind the client to the POSTed server so cache reads, per-server id writes and
        # the redirect all use the exact server the user was viewing. Fail closed on a
        # stale/unknown key — the old `or self.librenms_api.server_key` fallback rebuilt
        # the lazy client, which can resolve to a different server (wrong-server sync) or
        # raise on a misconfigured default (500).
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return redirect(
                reverse(url_name, kwargs={"pk": object_id})
                + f"?tab=interfaces&interface_name_field={interface_name_field}"
            )
        self._post_server_key = server_key
        selected_interfaces = self.get_selected_interfaces(request, interface_name_field)
        exclude_columns = request.POST.getlist("exclude_columns")

        redirect_url = (
            reverse(url_name, kwargs={"pk": object_id})
            # quote_plus the field too: it comes from the request, so unescaped special chars
            # could corrupt the redirect or inject extra query params (issue #107).
            + f"?tab=interfaces&interface_name_field={quote_plus(interface_name_field)}"
            + (f"&server_key={quote_plus(server_key)}" if server_key else "")
        )

        if selected_interfaces is None:
            return redirect(redirect_url)

        ports_data = self.get_cached_ports_data(request, obj, server_key)
        if ports_data is None:
            return redirect(redirect_url)

        # Prepare VLAN lookup maps if VLAN sync is enabled
        vlan_groups = self.get_vlan_groups_for_device(obj)
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups)
        self._lookup_maps = lookup_maps

        # Collects interfaces skipped because their LibreNMS port_id resolves to an
        # interface on a *different* device (see _resolve_device/vm_interface). Surfaced
        # below so the skip isn't silent — otherwise the user only sees it in the logs.
        self._skipped_conflicts = []
        self.sync_selected_interfaces(obj, selected_interfaces, ports_data, exclude_columns, interface_name_field)

        if self._skipped_conflicts:
            skipped = ", ".join(self._skipped_conflicts)
            messages.warning(
                request,
                f"{len(self._skipped_conflicts)} interface(s) skipped: {skipped}.",
            )
        messages.success(request, "Selected interfaces synced successfully.")
        return redirect(redirect_url)

    def get_object(self, object_type, object_id):
        """Return the Device or VirtualMachine for the given type and ID (object-scoped)."""
        if object_type == "device":
            return self.restrict_object_or_404(Device, pk=object_id)
        if object_type == "virtualmachine":
            return self.restrict_object_or_404(VirtualMachine, pk=object_id)
        raise Http404("Invalid object type.")

    def get_selected_interfaces(self, request, interface_name_field):
        """Return the list of selected interface names from POST data."""
        selected_interfaces = request.POST.getlist("select")
        if not selected_interfaces:
            messages.error(request, "No interfaces selected for synchronization.")
            return None
        return selected_interfaces

    def get_cached_ports_data(self, request, obj, server_key=None):
        """Return cached LibreNMS port data for the given object."""
        if server_key is None:
            server_key = self.librenms_api.server_key
        # On VC member pages the GET tab writes ports under the resolved sync device's
        # cache key. Resolve the same device here — UNCONDITIONALLY, mirroring the
        # writers (BaseInterfaceTableView.post/get_context_data) — so the POST path
        # reads the same entry. Gating the resolve on the viewed member having no id
        # of its own diverges from the writers when the member holds a legacy bare-int
        # while a sibling holds the preferred explicit per-server mapping: the refresh
        # then caches under the sibling and this reader misses forever.
        cache_obj = get_librenms_sync_device(obj, server_key=server_key) or obj
        cached_data = cache.get(self.get_cache_key(cache_obj, "ports", server_key))
        # No cached entry at all (or a non-dict one) → ask the user to refresh before syncing.
        if not isinstance(cached_data, dict):
            messages.warning(
                request,
                "No cached data found. Please refresh the data before syncing.",
            )
            return None
        # A dict that simply lacks a 'ports' key is treated as 'no ports to sync' — a harmless
        # empty no-op (matching the historical behavior). But a PRESENT-but-malformed ports value
        # (None, a non-list, or a list with non-dict entries) is failed closed so the sync loops
        # don't 500 mid-sync.
        ports_data = cached_data.get("ports", [])
        if not isinstance(ports_data, list) or any(not isinstance(port, dict) for port in ports_data):
            messages.warning(
                request,
                "No cached data found. Please refresh the data before syncing.",
            )
            return None
        return ports_data

    def sync_selected_interfaces(
        self,
        obj,
        selected_interfaces,
        ports_data,
        exclude_columns,
        interface_name_field,
    ):
        """Create or update NetBox interfaces from LibreNMS port data."""
        with transaction.atomic():
            if isinstance(obj, Device):
                locked_targets = self._lock_selected_device_targets(
                    obj,
                    selected_interfaces,
                    ports_data,
                    interface_name_field,
                )
                obj = locked_targets.get(obj.pk)
                if obj is None:
                    for interface_name in selected_interfaces:
                        self._record_skipped_conflict(interface_name, "selected target unavailable")
                    return
                self._locked_target_devices = locked_targets

            try:
                for port in ports_data:
                    # OOB-controller rows are merged into the host's interface list only for context
                    # (shared-LOM detection) and are never routed to a real target device by
                    # sync_interface(). They must not sync onto the host — and skipping them prevents
                    # a main/OOB interface-name collision (both "eth0") from double-processing one
                    # selection and overwriting the host interface with the OOB row's port_id/attrs.
                    if port.get("_source") == "oob":
                        continue
                    port_name = port.get(interface_name_field)

                    if port_name in selected_interfaces:
                        self.sync_interface(obj, port, exclude_columns, interface_name_field)
            finally:
                self.__dict__.pop("_locked_target_devices", None)

    def _lock_selected_device_targets(self, obj, selected_interfaces, ports_data, interface_name_field):
        """Lock the page device and selected VC targets for the sync transaction."""
        target_ids = {obj.pk}
        for port in ports_data:
            if port.get("_source") == "oob":
                continue
            interface_name = port.get(interface_name_field)
            if interface_name not in selected_interfaces:
                continue
            selected_id = self.request.POST.get(f"device_selection_{interface_name}")
            if selected_id:
                try:
                    target_ids.add(int(selected_id))
                except (TypeError, ValueError):
                    continue

        queryset = self.restricted_queryset(Device).select_for_update(of=("self",))
        return {device.pk: device for device in queryset.filter(pk__in=target_ids).order_by("pk")}

    def sync_interface(self, obj, librenms_interface, exclude_columns, interface_name_field):
        """Create or update a single NetBox interface from LibreNMS data."""
        interface_name = librenms_interface.get(interface_name_field)
        port_id = normalize_librenms_port_id(librenms_interface.get("port_id"))

        if isinstance(obj, Device):
            server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
            device_selection_key = f"device_selection_{interface_name}"
            selected_device_id = self.request.POST.get(device_selection_key)

            if selected_device_id:
                try:
                    locked_targets = getattr(self, "_locked_target_devices", None)
                    if locked_targets is None:
                        target_device = self.restricted_queryset(Device).get(id=selected_device_id)
                    else:
                        target_device = locked_targets[int(selected_device_id)]
                    # Both rows are current and locked in the HTTP sync path. Re-check that the
                    # selected device is the page device or remains in the same virtual chassis.
                    if target_device.id != obj.id and (
                        obj.virtual_chassis_id is None or target_device.virtual_chassis_id != obj.virtual_chassis_id
                    ):
                        self._record_skipped_conflict(interface_name, "selected target unavailable")
                        return
                except (Device.DoesNotExist, KeyError, ValueError, TypeError):
                    # The user explicitly selected a target. If it is stale or outside the
                    # caller's grant, do not silently sync the row onto the page device.
                    self._record_skipped_conflict(interface_name, "selected target unavailable")
                    return
            else:
                target_device = obj

            interface = self._resolve_device_interface(target_device, interface_name, port_id, server_key)
        elif isinstance(obj, VirtualMachine):
            server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
            interface = self._resolve_vm_interface(obj, interface_name, port_id, server_key)
        else:
            raise ValueError("Invalid object type.")

        if interface is None:
            logger.warning(
                "Skipping interface sync for '%s': unable to resolve target interface safely (port_id=%r).",
                interface_name,
                port_id,
            )
            # Record for the user-facing summary in post(). Defensive getattr: sync_interface
            # may be exercised directly (without post() initialising the list).
            self._record_skipped_conflict(interface_name, "port already mapped elsewhere or ambiguous")
            return

        netbox_type = None
        if isinstance(obj, Device):
            netbox_type = self.get_netbox_interface_type(librenms_interface)

        self.update_interface_attributes(
            interface,
            librenms_interface,
            netbox_type,
            exclude_columns,
            interface_name_field,
        )

        # Sync VLANs if not excluded
        if "vlans" not in exclude_columns:
            self._sync_interface_vlans(interface, librenms_interface, interface_name)

    def _record_skipped_conflict(self, interface_name, reason):
        """Record a row that cannot be synced to its requested target."""
        skipped = getattr(self, "_skipped_conflicts", None)
        if skipped is not None:
            skipped.append(f"{interface_name or '(unnamed)'} ({reason})")

    def _resolve_device_interface(self, target_device, interface_name, port_id, server_key):
        """Resolve a device interface using port_id first, then safe name fallback."""
        changeable = self.restricted_queryset(Interface, "change")
        if port_id:
            try:
                by_id = find_by_librenms_id(Interface, port_id, server_key)
            except AmbiguousLibreNMSIdError:
                # port_id matches multiple interfaces — skip this row rather than bind
                # to an arbitrary one (the caller records the skip).
                logger.warning("Skipping interface row — port_id %s is ambiguous (multiple matches).", port_id)
                return None
            if by_id is not None:
                if not changeable.filter(pk=by_id.pk).exists():
                    return None
                if by_id.device_id == target_device.id:
                    return by_id
                # The port_id resolves to an interface on a DIFFERENT device (a stale or
                # duplicate stored port_id, e.g. after a device replacement). The LibreNMS row
                # still describes THIS device's interface, and the rendered table binds it to the
                # current device's same-named interface, so fall back to that and update it —
                # but only if it already exists. Don't get_or_create here: spawning a new
                # interface when the id really belongs elsewhere would create a duplicate.
                # update_interface_attributes won't reassign the port_id off the other
                # interface (its existing_owner guard), so the foreign binding stays intact.
                existing_by_name = Interface.objects.filter(device=target_device, name=interface_name).first()
                if existing_by_name:
                    return existing_by_name if changeable.filter(pk=existing_by_name.pk).exists() else None
                return None
        interface, created = Interface.objects.get_or_create(device=target_device, name=interface_name)
        return interface if created or changeable.filter(pk=interface.pk).exists() else None

    def _resolve_vm_interface(self, vm, interface_name, port_id, server_key):
        """Resolve a VM interface using port_id first, then safe name fallback."""
        changeable = self.restricted_queryset(VMInterface, "change")
        if port_id:
            try:
                by_id = find_by_librenms_id(VMInterface, port_id, server_key)
            except AmbiguousLibreNMSIdError:
                logger.warning("Skipping VM interface row — port_id %s is ambiguous (multiple matches).", port_id)
                return None
            if by_id is not None:
                if not changeable.filter(pk=by_id.pk).exists():
                    return None
                if by_id.virtual_machine_id == vm.id:
                    return by_id
                # The port_id resolves to an interface on a DIFFERENT VM (a stale or duplicate
                # stored port_id). The LibreNMS row still describes THIS VM's interface, and the
                # rendered table binds it to this VM's same-named interface, so fall back to that
                # and update it — but only if it already exists (don't get_or_create a duplicate
                # for an id that really belongs elsewhere). update_interface_attributes won't
                # reassign the port_id off the other interface (its existing_owner guard).
                existing_by_name = VMInterface.objects.filter(virtual_machine=vm, name=interface_name).first()
                if existing_by_name:
                    return existing_by_name if changeable.filter(pk=existing_by_name.pk).exists() else None
                return None
        interface, created = VMInterface.objects.get_or_create(virtual_machine=vm, name=interface_name)
        return interface if created or changeable.filter(pk=interface.pk).exists() else None

    def get_netbox_interface_type(self, librenms_interface):
        """Return the NetBox interface type mapped from LibreNMS type and speed."""
        speed = convert_speed_to_kbps(librenms_interface.get("ifSpeed"))
        mappings = InterfaceTypeMapping.objects.filter(librenms_type=librenms_interface.get("ifType"))

        if speed is not None:
            speed_mapping = mappings.filter(librenms_speed__lte=speed).order_by("-librenms_speed").first()
            mapping = speed_mapping or mappings.filter(librenms_speed__isnull=True).first()
        else:
            mapping = mappings.filter(librenms_speed__isnull=True).first()

        return mapping.netbox_type if mapping else "other"

    def handle_mac_address(self, interface, ifPhysAddress):
        """Assign or create the MAC address for the given interface."""
        if ifPhysAddress:
            existing_mac = interface.mac_addresses.filter(mac_address=ifPhysAddress).first()
            if existing_mac:
                mac_obj = existing_mac
            else:
                mac_obj = MACAddress.objects.create(mac_address=ifPhysAddress)

            interface.mac_addresses.add(mac_obj)
            if hasattr(interface, "primary_mac_address"):
                interface.primary_mac_address = mac_obj

    def update_interface_attributes(
        self,
        interface,
        librenms_interface,
        netbox_type,
        exclude_columns,
        interface_name_field,
    ):
        """Update interface fields from LibreNMS data, respecting excluded columns."""
        is_device_interface = isinstance(interface, Interface)

        LIBRENMS_TO_NETBOX_MAPPING = {
            interface_name_field: "name",
            "ifType": "type",
            "ifSpeed": "speed",
            "ifAlias": "description",
            "ifMtu": "mtu",
        }

        for librenms_key, netbox_key in LIBRENMS_TO_NETBOX_MAPPING.items():
            if netbox_key in exclude_columns:
                continue

            if librenms_key == "ifSpeed":
                speed = convert_speed_to_kbps(librenms_interface.get(librenms_key))
                setattr(interface, netbox_key, speed)
            elif librenms_key == "ifType":
                if is_device_interface and hasattr(interface, netbox_key):
                    setattr(interface, netbox_key, netbox_type)
            elif librenms_key == "ifAlias":
                interface_name = librenms_interface.get(interface_name_field)
                if librenms_interface.get("ifAlias") != interface_name:
                    setattr(interface, netbox_key, librenms_interface.get(librenms_key))
            else:
                setattr(interface, netbox_key, librenms_interface.get(librenms_key))

        port_id = librenms_interface.get("port_id")
        if port_id is not None:
            server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
            normalized_port_id = normalize_librenms_port_id(port_id)
            if normalized_port_id is not None:
                try:
                    existing_owner = find_by_librenms_id(interface.__class__, normalized_port_id, server_key)
                except AmbiguousLibreNMSIdError:
                    logger.warning(
                        "Not setting port_id %s — it is ambiguous (matches multiple interfaces).",
                        normalized_port_id,
                    )
                else:
                    if existing_owner is None or existing_owner.pk == interface.pk:
                        set_librenms_device_id(interface, normalized_port_id, server_key)
                    else:
                        logger.warning(
                            "Not reassigning port_id %s from %s to %s.",
                            normalized_port_id,
                            existing_owner,
                            interface,
                        )

        if "enabled" not in exclude_columns:
            admin_status = librenms_interface.get("ifAdminStatus")
            interface.enabled = (
                True
                if admin_status is None
                else (admin_status.lower() == "up" if isinstance(admin_status, str) else bool(admin_status))
            )

        if "mac_address" not in exclude_columns:
            ifPhysAddress = librenms_interface.get("ifPhysAddress")
            self.handle_mac_address(interface, ifPhysAddress)

        interface.save()

    def _sync_interface_vlans(self, interface, librenms_port, interface_name):
        """
        Sync VLAN assignments from LibreNMS to NetBox interface.
        Sets mode, untagged_vlan, and tagged_vlans based on LibreNMS data.

        Args:
            interface: NetBox Interface or VMInterface object
            librenms_port: Port data dict from LibreNMS with VLAN info
            interface_name: Original interface name for form field lookup
        """
        # Get per-VLAN group selections from form (safely handle special chars in name)
        safe_name = interface_name.replace("/", "_").replace(":", "_")

        # Build VLAN data from port
        vlan_data = {
            "untagged_vlan": librenms_port.get("untagged_vlan"),
            "tagged_vlans": librenms_port.get("tagged_vlans", []),
        }

        # Build per-VLAN group map from POST data
        vlan_group_map = {}
        all_vids = []
        if vlan_data["untagged_vlan"]:
            all_vids.append(str(vlan_data["untagged_vlan"]))
        for vid in vlan_data.get("tagged_vlans", []):
            all_vids.append(str(vid))

        for vid in all_vids:
            group_id = self.request.POST.get(f"vlan_group_{safe_name}_{vid}", "")
            if group_id:
                vlan_group_map[vid] = group_id

        # Use mixin method to update interface VLAN assignments
        self._update_interface_vlan_assignment(interface, vlan_data, vlan_group_map, self._lookup_maps)


class DeleteNetBoxInterfacesView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """Delete interfaces that exist only in NetBox."""

    def get_required_permissions_for_object_type(self, object_type):
        """Return the required permissions based on object type."""
        # The owner is resolved through a restricted queryset, so its view permission is stated
        # here too (mirroring SyncInterfacesView): a missing grant is a 403, not a bare 404.
        if object_type == "device":
            return [("view", Device), ("delete", Interface)]
        elif object_type == "virtualmachine":
            return [("view", VirtualMachine), ("delete", VMInterface)]
        else:
            raise Http404(f"Invalid object type: {object_type}")

    def post(self, request, object_type, object_id):
        """Delete selected NetBox-only interfaces not present in LibreNMS."""
        # Set permissions dynamically based on object type
        self.required_object_permissions = {
            "POST": self.get_required_permissions_for_object_type(object_type),
        }

        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions_json("POST"):
            return error

        if object_type == "device":
            obj = self.restrict_object_or_404(Device, pk=object_id)
        elif object_type == "virtualmachine":
            obj = self.restrict_object_or_404(VirtualMachine, pk=object_id)
        else:
            return JsonResponse({"error": "Invalid object type"}, status=400)

        interface_ids = request.POST.getlist("interface_ids")

        if not interface_ids:
            return JsonResponse({"error": "No interfaces selected for deletion"}, status=400)

        deleted_count = 0
        errors = []
        interface_name = None

        try:
            with transaction.atomic():
                for interface_id in interface_ids:
                    interface_name = None
                    try:
                        if object_type == "device":
                            # Scoped by "delete": the ownership checks below prove where the
                            # interface sits, not that the grant covers it.
                            interface = self.restricted_queryset(Interface, "delete").get(id=interface_id)
                            interface_name = interface.name
                            if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                                valid_device_ids = [member.id for member in obj.virtual_chassis.members.all()]
                                if interface.device_id not in valid_device_ids:
                                    errors.append(
                                        "Interface {} does not belong to this device or its virtual chassis".format(
                                            interface.name
                                        )
                                    )
                                    continue
                            elif interface.device_id != obj.id:
                                errors.append(f"Interface {interface.name} does not belong to this device")
                                continue
                        else:
                            interface = self.restricted_queryset(VMInterface, "delete").get(id=interface_id)
                            interface_name = interface.name
                            if interface.virtual_machine_id != obj.id:
                                errors.append(f"Interface {interface.name} does not belong to this virtual machine")
                                continue

                        interface.delete()
                        deleted_count += 1

                    except (Interface.DoesNotExist, VMInterface.DoesNotExist):
                        errors.append(f"Interface with ID {interface_id} not found")
                        continue
                    except Exception as exc:  # pragma: no cover - defensive
                        errors.append(f"Error deleting interface {interface_name or interface_id}: {str(exc)}")
                        continue

        except Exception:  # pragma: no cover
            logger.exception("DeleteNetBoxInterfacesView transaction failed")
            return JsonResponse({"error": "Transaction failed. Please check server logs."}, status=500)

        response_data = {
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Successfully deleted {deleted_count} interface(s)",
        }

        if errors:
            response_data["errors"] = errors
            response_data["message"] += f" with {len(errors)} error(s)"

        return JsonResponse(response_data)
