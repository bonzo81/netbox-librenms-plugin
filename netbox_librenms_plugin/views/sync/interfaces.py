from dcim.models import Device, Interface, MACAddress
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.utils import convert_speed_to_kbps, get_interface_name_field
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    VlanAssignmentMixin,
)


class SyncInterfacesView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, VlanAssignmentMixin, CacheMixin, View):
    """Sync selected interfaces from LibreNMS into NetBox."""

    def get_required_permissions_for_object_type(self, object_type):
        """Return the required permissions based on object type."""
        if object_type == "device":
            return [("add", Interface), ("change", Interface)]
        elif object_type == "virtualmachine":
            return [("add", VMInterface), ("change", VMInterface)]
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
        selected_interfaces = self.get_selected_interfaces(request, interface_name_field)
        exclude_columns = request.POST.getlist("exclude_columns")

        if selected_interfaces is None:
            return redirect(
                reverse(url_name, kwargs={"pk": object_id})
                + f"?tab=interfaces&interface_name_field={interface_name_field}"
            )

        ports_data = self.get_cached_ports_data(request, obj)
        if ports_data is None:
            return redirect(
                reverse(url_name, kwargs={"pk": object_id})
                + f"?tab=interfaces&interface_name_field={interface_name_field}"
            )

        # Prepare VLAN lookup maps if VLAN sync is enabled
        vlan_groups = self.get_vlan_groups_for_device(obj)
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups)
        self._lookup_maps = lookup_maps

        self.sync_selected_interfaces(obj, selected_interfaces, ports_data, exclude_columns, interface_name_field)

        messages.success(request, "Selected interfaces synced successfully.")
        return redirect(
            reverse(url_name, kwargs={"pk": object_id}) + f"?tab=interfaces&interface_name_field={interface_name_field}"
        )

    def get_object(self, object_type, object_id):
        """Return the Device or VirtualMachine for the given type and ID."""
        if object_type == "device":
            return get_object_or_404(Device, pk=object_id)
        if object_type == "virtualmachine":
            return get_object_or_404(VirtualMachine, pk=object_id)
        raise Http404("Invalid object type.")

    def get_selected_interfaces(self, request, interface_name_field):
        """Return the list of selected interface names from POST data."""
        selected_interfaces = request.POST.getlist("select")
        if not selected_interfaces:
            messages.error(request, "No interfaces selected for synchronization.")
            return None
        return selected_interfaces

    def get_cached_ports_data(self, request, obj):
        """Return cached LibreNMS port data for the given object."""
        cached_data = cache.get(self.get_cache_key(obj, "ports"))
        if not cached_data:
            messages.warning(
                request,
                "No cached data found. Please refresh the data before syncing.",
            )
            return None
        return cached_data.get("ports", [])

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
            for port in ports_data:
                port_name = port.get(interface_name_field)

                if port_name in selected_interfaces:
                    self.sync_interface(obj, port, exclude_columns, interface_name_field)

    def sync_interface(self, obj, librenms_interface, exclude_columns, interface_name_field):
        """Create or update a single NetBox interface from LibreNMS data."""
        interface_name = librenms_interface.get(interface_name_field)

        if isinstance(obj, Device):
            device_selection_key = f"device_selection_{interface_name}"
            selected_device_id = self.request.POST.get(device_selection_key)

            if selected_device_id:
                try:
                    target_device = Device.objects.get(id=selected_device_id)
                    # Validate the target is the current device or a VC member
                    if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                        valid_ids = set(obj.virtual_chassis.members.values_list("id", flat=True))
                        if target_device.id not in valid_ids:
                            target_device = obj
                    elif target_device.id != obj.id:
                        target_device = obj
                except (Device.DoesNotExist, ValueError, TypeError):
                    target_device = obj
            else:
                target_device = obj

            interface, _ = Interface.objects.get_or_create(device=target_device, name=interface_name)
        elif isinstance(obj, VirtualMachine):
            interface, _ = VMInterface.objects.get_or_create(virtual_machine=obj, name=interface_name)
        else:
            raise ValueError("Invalid object type.")

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
        vlan_synced = False
        if "vlans" not in exclude_columns:
            self._sync_interface_vlans(interface, librenms_interface, interface_name)
            vlan_synced = True

        # Skip redundant save when _sync_interface_vlans already saved (via _update_interface_vlan_assignment)
        if not vlan_synced:
            interface.save()

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

        if "librenms_id" in interface.cf:
            interface.custom_field_data["librenms_id"] = librenms_interface.get("port_id")

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
        if object_type == "device":
            return [("delete", Interface)]
        elif object_type == "virtualmachine":
            return [("delete", VMInterface)]
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
            obj = get_object_or_404(Device, pk=object_id)
        elif object_type == "virtualmachine":
            obj = get_object_or_404(VirtualMachine, pk=object_id)
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
                            interface = Interface.objects.get(id=interface_id)
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
                            interface = VMInterface.objects.get(id=interface_id)
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

        except Exception as exc:  # pragma: no cover
            return JsonResponse({"error": f"Transaction failed: {str(exc)}"}, status=500)

        response_data = {
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Successfully deleted {deleted_count} interface(s)",
        }

        if errors:
            response_data["errors"] = errors
            response_data["message"] += f" with {len(errors)} error(s)"

        return JsonResponse(response_data)
