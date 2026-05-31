from ipaddress import ip_address as _ip_addr
from urllib.parse import quote_plus

from dcim.models import Device, Interface
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from ipam.models import VRF, IPAddress
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.utils import resolve_set_primary_ip
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)


class SyncIPAddressesView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """Synchronize IP addresses from LibreNMS cache into NetBox."""

    required_object_permissions = {
        "POST": [
            ("add", IPAddress),
            ("change", IPAddress),
        ],
    }

    def get_selected_ips(self, request):
        """Return selected IP addresses from POST data."""
        return [x for x in request.POST.getlist("select") if x]

    def get_vrf_selection(self, request, ip_address):
        """Return the VRF selected for a given IP address, or None."""
        vrf_id = request.POST.get(f"vrf_{ip_address}")

        if vrf_id:
            try:
                return VRF.objects.get(pk=vrf_id)
            except VRF.DoesNotExist:
                pass

        return None

    def get_cached_ip_data(self, request, obj):
        """Return cached LibreNMS IP address data for the given object."""
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        cached_data = cache.get(self.get_cache_key(obj, "ip_addresses", server_key))
        if not cached_data:
            return None
        return cached_data.get("ip_addresses", [])

    def get_object(self, object_type, pk):
        """Return the Device or VirtualMachine instance for the given type and pk."""
        if object_type == "device":
            return get_object_or_404(Device, pk=pk)
        if object_type == "virtualmachine":
            return get_object_or_404(VirtualMachine, pk=pk)
        raise Http404("Invalid object type.")

    def get_ip_tab_url(self, obj):
        """Return the URL for the IP addresses sync tab."""
        if isinstance(obj, Device):
            url_name = "plugins:netbox_librenms_plugin:device_librenms_sync"
        else:
            url_name = "plugins:netbox_librenms_plugin:vm_librenms_sync"
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        url = f"{reverse(url_name, args=[obj.pk])}?tab=ipaddresses"
        if server_key:
            url += f"&server_key={quote_plus(server_key)}"
        return url

    def post(self, request, object_type, pk):
        """Sync selected IP addresses from LibreNMS into NetBox."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        # Read server_key from POST so we use the exact server the user was viewing
        self._post_server_key = request.POST.get("server_key") or self.librenms_api.server_key

        obj = self.get_object(object_type, pk)

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
            success, info = self.librenms_api.get_device_info(librenms_id)
            if not success or not isinstance(info, dict):
                return None
            return (info.get("ip") or "").strip() or None
        except Exception:  # pragma: no cover - defensive
            return None

    @staticmethod
    def _same_host(a, b):
        """True if two address strings refer to the same host IP."""
        try:
            return _ip_addr(a) == _ip_addr(b)
        except ValueError:
            return False

    @staticmethod
    def _set_primary_ip(obj, ip_obj):
        """Point obj.primary_ip4/6 (by family) at *ip_obj*. Returns True if changed.

        The caller guarantees ``ip_obj`` is assigned to one of the object's
        interfaces, so this satisfies NetBox's ``primary_ip`` constraint.
        """
        field = "primary_ip6" if ip_obj.family == 6 else "primary_ip4"
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
        results = {"created": [], "updated": [], "unchanged": [], "failed": [], "primary_set": []}

        set_primary = resolve_set_primary_ip(request)
        mgmt_ip = self.get_management_ip(obj) if set_primary else None

        with transaction.atomic():
            for ip_address in selected_ips:
                try:
                    ip_data = next(ip for ip in cached_ips if ip["ip_address"] == ip_address)

                    vrf = self.get_vrf_selection(request, ip_address)

                    interface = None
                    if ip_data.get("interface_url"):
                        interface_id = ip_data["interface_url"].split("/")[-2]
                        if object_type == "device":
                            interface = Interface.objects.get(id=interface_id)
                        else:
                            interface = VMInterface.objects.get(id=interface_id)

                    ip_with_mask = ip_data["ip_with_mask"]

                    existing_ip = IPAddress.objects.filter(address=ip_with_mask).first()

                    if existing_ip:
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

                    # Primary-IP auto-match: only when the IP is interface-assigned
                    # (NetBox requires that for primary_ip) and equals the mgmt IP.
                    if (
                        mgmt_ip
                        and interface is not None
                        and self._same_host(ip_data["ip_address"], mgmt_ip)
                        and self._set_primary_ip(obj, ip_obj)
                    ):
                        results["primary_set"].append(ip_address)

                except Exception:  # pragma: no cover - defensive
                    results["failed"].append(ip_address)

            return results

    def display_sync_results(self, request, results):
        """Display flash messages summarizing the IP sync results."""
        if results["created"]:
            messages.success(request, f"Created IP addresses: {', '.join(results['created'])}")
        if results["updated"]:
            messages.success(request, f"Updated IP addresses: {', '.join(results['updated'])}")
        if results.get("primary_set"):
            messages.success(request, f"Set as Primary IP: {', '.join(results['primary_set'])}")
        if results["unchanged"]:
            messages.warning(
                request,
                f"IP addresses already exist: {', '.join(results['unchanged'])}",
            )
        if results["failed"]:
            messages.error(request, f"Failed to sync IP addresses: {', '.join(results['failed'])}")
