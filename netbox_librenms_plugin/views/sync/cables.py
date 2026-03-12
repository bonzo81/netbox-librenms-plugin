import logging
from urllib.parse import quote_plus

from dcim.models import Cable, Device, Interface
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View

from netbox_librenms_plugin.utils import get_librenms_sync_device
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)

logger = logging.getLogger(__name__)


class SyncCablesView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """Create NetBox cables using cached LibreNMS link data."""

    required_object_permissions = {
        "POST": [
            ("add", Cable),
            ("change", Cable),
        ],
    }

    def get_selected_interfaces(self, request, initial_device):
        """
        Return selected interface entries from POST data.

        Each ``select`` value is a ``local_port_id`` (stable LibreNMS identifier)
        so that matching against cached link data is user-preference agnostic.
        """
        selected_interfaces = []
        selected_data = [x for x in request.POST.getlist("select") if x]

        if not selected_data:
            return None

        for port_id in selected_data:
            override = request.POST.get(f"device_selection_{port_id}")
            device_id = override or initial_device.id
            selected_interfaces.append(
                {"device_id": device_id, "local_port_id": port_id, "vc_override": bool(override)}
            )

        return selected_interfaces

    def get_cached_links_data(self, request, obj):
        """Return cached LibreNMS link data for the given object."""
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        cache_obj = get_librenms_sync_device(obj, server_key=server_key) or obj
        cached_data = cache.get(self.get_cache_key(cache_obj, "links", server_key))
        if not cached_data:
            return None
        return cached_data.get("links", [])

    def create_cable(self, local_interface, remote_interface, request):
        """
        Create a cable between local and remote interfaces.

        Returns:
            True on success, False on failure.
        """
        try:
            Cable.objects.create(
                a_terminations=[local_interface],
                b_terminations=[remote_interface],
                status="connected",
            )
            return True
        except Exception as exc:  # pragma: no cover - protects UX
            messages.error(request, f"Failed to create cable: {str(exc)}")
            return False

    def check_existing_cable(self, local_interface, remote_interface):
        """Return True if a cable already exists for either interface."""
        from dcim.models import Interface as DCIMInterface

        interface_ct = ContentType.objects.get_for_model(DCIMInterface)
        return Cable.objects.filter(
            Q(
                terminations__termination_type=interface_ct,
                terminations__termination_id=local_interface.pk,
            )
            | Q(
                terminations__termination_type=interface_ct,
                terminations__termination_id=remote_interface.pk,
            )
        ).exists()

    def validate_prerequisites(self, cached_links, selected_interfaces):
        """Validate that cached data and selections are present before sync."""
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
        """Process cable creation for a single interface from cached link data."""
        port_id = str(interface.get("local_port_id", ""))
        try:
            link_data = next(link for link in cached_links if str(link.get("local_port_id", "")) == port_id)
            # Apply posted device override (VC member selection) without mutating the cached list.
            link_data = {**link_data, "device_id": interface.get("device_id", link_data.get("device_id"))}
            # Re-resolve local interface only when a VC member override was explicitly applied.
            if interface.get("vc_override") and hasattr(self, "_initial_device"):
                link_data.pop("netbox_local_interface_id", None)
                link_data.pop("local_port_url", None)
                self.enrich_local_port(link_data, self._initial_device, getattr(self, "_post_server_key", None))
            return self.handle_cable_creation(link_data, interface)
        except StopIteration:
            return {"status": "invalid", "interface": port_id}

    def verify_cable_creation_requirements(self, link_data):
        """Return True if all required NetBox IDs are present in link data."""
        required_fields = [
            "netbox_local_interface_id",
            "netbox_remote_interface_id",
        ]

        return all(link_data.get(field) for field in required_fields)

    def handle_cable_creation(self, link_data, interface):
        """Create a cable from link data and return the operation result."""
        display_name = link_data.get("local_port") or interface.get("local_port_id", "")
        if not self.verify_cable_creation_requirements(link_data):
            if not link_data.get("netbox_remote_device_id") or not link_data.get("netbox_remote_interface_id"):
                return {"status": "missing_remote", "interface": display_name}
            return {"status": "invalid", "interface": display_name}

        try:
            local_interface = Interface.objects.get(pk=link_data["netbox_local_interface_id"])
            remote_interface = Interface.objects.get(pk=link_data["netbox_remote_interface_id"])

            if self.check_existing_cable(local_interface, remote_interface):
                return {"status": "duplicate", "interface": display_name}

            if self.create_cable(local_interface, remote_interface, self.request):
                return {"status": "valid", "interface": display_name}
            return {"status": "invalid", "interface": display_name}  # pragma: no cover

        except Interface.DoesNotExist:
            return {"status": "missing_remote", "interface": display_name}

    def process_interface_sync(self, selected_interfaces, cached_links):
        """
        Process cable sync for all selected interfaces and return results.

        Each interface is processed in its own atomic block so individual
        failures roll back only that cable without affecting others.
        """
        results = {"valid": [], "invalid": [], "duplicate": [], "missing_remote": []}

        for interface in selected_interfaces:
            try:
                with transaction.atomic():
                    result = self.process_single_interface(interface, cached_links)
                results[result["status"]].append(result.get("interface", ""))
            except Exception:
                logger.exception("Failed to sync cable for port_id %s", interface.get("local_port_id", ""))
                results["invalid"].append(interface.get("local_port_id", ""))

        return results

    def post(self, request, pk):
        """Sync selected cable connections from LibreNMS into NetBox."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        initial_device = get_object_or_404(Device, pk=pk)
        server_key = request.POST.get("server_key") or self.librenms_api.server_key
        self._post_server_key = server_key
        self._initial_device = initial_device
        redirect_url = (
            f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}?tab=cables"
            + (f"&server_key={quote_plus(server_key)}" if server_key else "")
        )

        selected_interfaces = self.get_selected_interfaces(request, initial_device)
        cached_links = self.get_cached_links_data(request, initial_device)

        if not self.validate_prerequisites(cached_links, selected_interfaces):
            return redirect(redirect_url)

        results = self.process_interface_sync(selected_interfaces, cached_links)
        self.display_sync_results(request, results)

        return redirect(redirect_url)

    def display_sync_results(self, request, results):
        """Display flash messages summarizing the cable sync results."""
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
