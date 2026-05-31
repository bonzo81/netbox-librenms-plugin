import logging
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

from netbox_librenms_plugin.utils import resolve_set_primary_ip, same_host
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

        obj = self.get_object(object_type, pk)

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
            success, info = self.librenms_api.get_device_info(librenms_id)
            if not success or not isinstance(info, dict):
                return None
            return (info.get("ip") or "").strip() or None
        except Exception:  # pragma: no cover - defensive
            return None

    @staticmethod
    def _same_host(a, b):
        """True if two address strings refer to the same host IP."""
        return same_host(a, b)

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
        results = {
            "created": [],
            "updated": [],
            "unchanged": [],
            "failed": [],
            "primary_set": [],
            "primary_no_interface": [],
            "errors": {},
        }

        set_primary = resolve_set_primary_ip(request)
        mgmt_ip = self.get_management_ip(obj) if set_primary else None

        for ip_address in selected_ips:
            try:
                # Per-IP savepoint so one bad address rolls back only itself and
                # surfaces a real error, instead of poisoning the whole batch.
                with transaction.atomic():
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

                    # Scope the lookup to the target VRF: the same address can
                    # legitimately exist in multiple VRFs, and matching on address
                    # alone would hijack an unrelated IP and rewrite its VRF.
                    existing_ip = IPAddress.objects.filter(address=ip_with_mask, vrf=vrf).first()

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

                    # Primary-IP auto-match for the management IP. NetBox requires
                    # the IP be interface-assigned to be a primary, so when the
                    # interface is missing we flag it rather than silently skip.
                    if mgmt_ip and self._same_host(ip_data["ip_address"], mgmt_ip):
                        if interface is None:
                            results["primary_no_interface"].append(ip_address)
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
        if results["unchanged"]:
            messages.warning(
                request,
                f"IP addresses already exist: {', '.join(results['unchanged'])}",
            )
        if results["failed"]:
            errors = results.get("errors", {})
            detail = ", ".join(f"{ip} ({errors[ip]})" if errors.get(ip) else ip for ip in results["failed"])
            messages.error(request, f"Failed to sync IP addresses: {detail}")
