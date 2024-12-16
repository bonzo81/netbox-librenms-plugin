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
        Returns a list of dictionaries containing device_id and interface name.
        """
        selected_interfaces = []
        selected_data = [x for x in request.POST.getlist("select") if x]

        if not selected_data:
            return None

        for interface in selected_data:
            device_id = (
                request.POST.get(f"device_selection_{interface}") or initial_device.id
            )
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

    def process_single_interface(self, interface, cached_links):
        """Process a single interface and return its status"""
        try:
            link_data = next(
                link
                for link in cached_links
                if link["local_port"] == interface["interface"]
            )
            return self.handle_cable_creation(link_data, interface)
        except StopIteration:
            return {"status": "invalid"}

    def verify_cable_creation_requirements(self, link_data):
        """
        Verify if cable can be created by checking required data exists
        """
        required_fields = [
            "netbox_local_interface_id",
            "netbox_remote_device_id",
            "netbox_remote_interface_id",
        ]

        return all(link_data.get(field) for field in required_fields)

    def handle_cable_creation(self, link_data, interface):
        """Handle the cable creation process for valid link data"""
        if not self.verify_cable_creation_requirements(link_data):
            return {"status": "invalid", "interface": interface["interface"]}

        try:
            local_interface = Interface.objects.get(
                pk=link_data["netbox_local_interface_id"]
            )
            remote_device = Device.objects.get(pk=link_data["netbox_remote_device_id"])
            remote_interface = Interface.objects.get(
                pk=link_data["netbox_remote_interface_id"]
            )

            if self.check_existing_cable(local_interface, remote_interface):
                return {"status": "duplicate", "interface": interface["interface"]}

            self.create_cable(
                local_interface, remote_device, remote_interface, self.request
            )
            return {"status": "valid", "interface": interface["interface"]}

        except (Device.DoesNotExist, Interface.DoesNotExist):
            return {"status": "missing_remote", "interface": interface["interface"]}

    @transaction.atomic()
    def post(self, request, pk):
        initial_device = get_object_or_404(Device, pk=pk)
        selected_interfaces = self.get_selected_interfaces(request, initial_device)
        cached_links = self.get_cached_links_data(request, initial_device)

        if not self.validate_prerequisites(
            cached_links, selected_interfaces, initial_device
        ):
            return redirect(self.get_cables_tab_url(initial_device))

        results = {"valid": [], "invalid": [], "duplicate": [], "missing_remote": []}

        for interface in selected_interfaces:
            result = self.process_single_interface(interface, cached_links)
            results[result["status"]].append(result.get("interface", ""))

        self.display_sync_results(request, results)

        return redirect(
            f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}?tab=cables"
        )

    def display_sync_results(self, request, results):
        """Display messages for cable sync results"""
        if results["missing_remote"]:
            messages.error(
                request,
                f"Remote device or interface not found in NetBox for: {', '.join(results['missing_remote'])}",
            )
        if results["invalid"]:
            messages.error(
                request,
                f"No LibreNMS link data found for interfaces: {', '.join(results['invalid'])}",
            )
        if results["duplicate"]:
            messages.warning(
                request,
                f"Cable already exists for interfaces: {', '.join(results['duplicate'])}",
            )
        if results["valid"]:
            messages.success(
                request,
                f"Successfully created cable for interfaces: {', '.join(results['valid'])}",
            )
