from dcim.models import Cable, Device, Interface
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View

from netbox_librenms_plugin.views.mixins import CacheMixin


class SyncCablesView(CacheMixin, View):
    """
    View for creating cables in NetBox from LibreNMS data.
    """

    def get_selected_interfaces(self, request, initial_device):
        """
        Retrieve and validate selected interfaces from the request.
        Include device information with each selected interface.
        """
        selected_data = request.POST.getlist("select")
        if not selected_data:
            return None

        # Parse device and interface information from selected data
        selected_interfaces = []

        for interface in selected_data:
            # For VC members, get the device_id from the selection dropdown
            device_id = request.POST.get(f"device_selection_{interface}")
            # For standalone devices, use the original device id
            if not device_id:
                device_id = initial_device.id
            selected_interfaces.append({"device_id": device_id, "interface": interface})
        return selected_interfaces

    def get_cached_links_data(self, request, obj):
        cached_data = cache.get(self.get_cache_key(obj, "links"))
        if not cached_data:
            return None
        return cached_data.get("links", [])

    def create_cable(self, local_interface, remote_device, remote_interface, request):
        try:
            Cable.objects.create(
                a_terminations=[local_interface],
                b_terminations=[remote_interface],
                status="connected",
            )
        except Exception as e:
            messages.error(request, f"Failed to create cable: {str(e)}")

    def check_existing_cable(self, local_interface, remote_interface):
        return Cable.objects.filter(
            Q(terminations__termination_id=local_interface.pk)
            | Q(terminations__termination_id=remote_interface.pk)
        ).exists()

    def validate_prerequisites(self, cached_links, selected_interfaces, device):
        """
        Validates required data before processing cable creation
        """
        if not cached_links:
            messages.error(
                self.request,
                "Cache has expired. Please refresh the cable data before syncing.",
            )
            return False

        if selected_interfaces is None:
            messages.error(self.request, "No interfaces selected for synchronization.")
            return False

        return True

    def display_result_messages(
        self, request, valid_interfaces, invalid_interfaces, duplicate_interfaces
    ):
        """
        Display appropriate messages for cable creation results
        """
        if duplicate_interfaces:
            messages.warning(
                request,
                f"Cable already exist for interfaces: {', '.join(duplicate_interfaces)}",
            )
        if invalid_interfaces:
            messages.error(
                request,
                f"Cannot create cable - device or interface not found in NetBox: {', '.join(invalid_interfaces)}",
            )
        if valid_interfaces:
            messages.success(
                request,
                f"Successfully created cable for interfaces: {', '.join(valid_interfaces)}",
            )

    @transaction.atomic()
    def post(self, request, pk):
        initial_device = get_object_or_404(Device, pk=pk)
        selected_interfaces = self.get_selected_interfaces(request, initial_device)
        cached_links = self.get_cached_links_data(request, initial_device)

        if not self.validate_prerequisites(
            cached_links, selected_interfaces, initial_device
        ):
            return redirect(
                f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}?tab=cables"
            )

        valid_interfaces = []
        invalid_interfaces = []
        duplicate_interfaces = []

        for interface in selected_interfaces:
            try:
                # Find the matching link data from cache
                link_data = next(
                    link
                    for link in cached_links
                    if link["local_port"] == interface["interface"]
                )

                # Get local interface
                local_device = Device.objects.get(pk=interface["device_id"])
                local_interface = local_device.interfaces.get(
                    name=link_data["local_port"]
                )

                # Get remote interface
                remote_device = Device.objects.get(pk=link_data["remote_device_id"])
                remote_interface = remote_device.interfaces.get(
                    pk=link_data["remote_port_id"]
                )

                if self.check_existing_cable(local_interface, remote_interface):
                    duplicate_interfaces.append(interface["interface"])
                    continue

                self.create_cable(
                    local_interface, remote_device, remote_interface, request
                )
                valid_interfaces.append(interface["interface"])

            except (Device.DoesNotExist, Interface.DoesNotExist, StopIteration):
                invalid_interfaces.append(interface["interface"])

        self.display_result_messages(
            request, valid_interfaces, invalid_interfaces, duplicate_interfaces
        )
        return redirect(
            f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}?tab=cables"
        )
