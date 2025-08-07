from dcim.models import Device, Interface
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.views.mixins import CacheMixin


class DeleteNetBoxInterfacesView(CacheMixin, View):
    """
    View for deleting interfaces that exist in NetBox but not in LibreNMS.
    """

    def post(self, request, object_type, object_id):
        """Delete selected NetBox-only interfaces."""

        # Get the object
        if object_type == "device":
            obj = get_object_or_404(Device, pk=object_id)
        elif object_type == "virtualmachine":
            obj = get_object_or_404(VirtualMachine, pk=object_id)
        else:
            return JsonResponse({"error": "Invalid object type"}, status=400)

        interface_ids = request.POST.getlist("interface_ids")

        if not interface_ids:
            return JsonResponse(
                {"error": "No interfaces selected for deletion"}, status=400
            )

        deleted_count = 0
        errors = []

        try:
            with transaction.atomic():
                for interface_id in interface_ids:
                    try:
                        if object_type == "device":
                            interface = Interface.objects.get(id=interface_id)
                            # Verify the interface belongs to the device or its virtual chassis
                            if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                                valid_device_ids = [
                                    member.id
                                    for member in obj.virtual_chassis.members.all()
                                ]
                                if interface.device_id not in valid_device_ids:
                                    errors.append(
                                        f"Interface {interface.name} does not belong to this device or its virtual chassis"
                                    )
                                    continue
                            elif interface.device_id != obj.id:
                                errors.append(
                                    f"Interface {interface.name} does not belong to this device"
                                )
                                continue
                        else:  # virtualmachine
                            interface = VMInterface.objects.get(id=interface_id)
                            if interface.virtual_machine_id != obj.id:
                                errors.append(
                                    f"Interface {interface.name} does not belong to this virtual machine"
                                )
                                continue

                        interface_name = interface.name
                        interface.delete()
                        deleted_count += 1

                    except (Interface.DoesNotExist, VMInterface.DoesNotExist):
                        errors.append(f"Interface with ID {interface_id} not found")
                        continue
                    except Exception as e:
                        errors.append(
                            f"Error deleting interface {interface_name}: {str(e)}"
                        )
                        continue

        except Exception as e:
            return JsonResponse({"error": f"Transaction failed: {str(e)}"}, status=500)

        response_data = {
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Successfully deleted {deleted_count} interface(s)",
        }

        if errors:
            response_data["errors"] = errors
            response_data["message"] += f" with {len(errors)} error(s)"

        return JsonResponse(response_data)
