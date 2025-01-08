from dcim.models import Device, Interface
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from ipam.models import IPAddress
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.views.mixins import CacheMixin


class SyncIPAddressesView(CacheMixin, View):
    def get_selected_ips(self, request):
        """Retrieve selected IP addresses from the request"""
        return [x for x in request.POST.getlist("select") if x]

    def get_cached_ip_data(self, request, obj):
        cached_data = cache.get(self.get_cache_key(obj, "ip_addresses"))
        if not cached_data:
            return None
        return cached_data.get("ip_addresses", [])

    def get_object(self, object_type, pk):
        """Retrieve the object (Device or VirtualMachine)"""
        if object_type == "device":
            return get_object_or_404(Device, pk=pk)
        elif object_type == "virtualmachine":
            return get_object_or_404(VirtualMachine, pk=pk)
        else:
            raise Http404("Invalid object type.")

    def get_ip_tab_url(self, obj):
        """Return the correct URL based on object type"""
        if isinstance(obj, Device):
            url_name = "plugins:netbox_librenms_plugin:device_librenms_sync"
        else:
            url_name = "plugins:netbox_librenms_plugin:vm_librenms_sync"
        return f"{reverse(url_name, args=[obj.pk])}?tab=ipaddresses"

    @transaction.atomic()
    def post(self, request, object_type, pk):
        obj = self.get_object(object_type, pk)
        selected_ips = self.get_selected_ips(request)
        cached_ips = self.get_cached_ip_data(request, obj)

        if not cached_ips:
            messages.error(request, "Cache has expired. Please refresh the IP data.")
            return redirect(self.get_ip_tab_url(obj))

        if not selected_ips:
            messages.error(request, "No IP addresses selected for synchronization.")
            return redirect(self.get_ip_tab_url(obj))

        results = self.sync_ip_addresses(selected_ips, cached_ips, obj, object_type)
        self.display_sync_results(request, results)

        return redirect(self.get_ip_tab_url(obj))

    def sync_ip_addresses(self, selected_ips, cached_ips, obj, object_type):
        results = {"created": [], "updated": [], "unchanged": [], "failed": []}

        for ip_address in selected_ips:
            try:
                ip_data = next(
                    ip for ip in cached_ips if ip["ipv4_address"] == ip_address
                )

                # Get interface from URL
                interface = None
                if ip_data.get("interface_url"):
                    interface_id = ip_data["interface_url"].split("/")[-2]
                    if object_type == "device":
                        interface = Interface.objects.get(id=interface_id)
                    else:
                        interface = VMInterface.objects.get(id=interface_id)

                # Construct CIDR notation using prefix from data
                ip_with_mask = f"{ip_data['ipv4_address']}/{ip_data['ipv4_prefixlen']}"

                # Check if IP exists
                existing_ip = IPAddress.objects.filter(address=ip_with_mask).first()

                if existing_ip:
                    # Only update if interface assignment is different
                    if existing_ip.assigned_object != interface:
                        existing_ip.assigned_object = interface
                        existing_ip.save()
                        results["updated"].append(ip_address)
                    else:
                        results["unchanged"].append(ip_address)
                else:
                    # Create new IP
                    IPAddress.objects.create(
                        address=ip_with_mask, assigned_object=interface, status="active"
                    )
                    results["created"].append(ip_address)

            except Exception as e:
                results["failed"].append(ip_address)

        return results

    def display_sync_results(self, request, results):
        if results["created"]:
            messages.success(
                request, f"Created IP addresses: {', '.join(results['created'])}"
            )
        if results["updated"]:
            messages.success(
                request, f"Updated IP addresses: {', '.join(results['updated'])}"
            )
        if results["unchanged"]:
            messages.warning(
                request,
                f"IP addresses already exist: {', '.join(results['unchanged'])}",
            )
        if results["failed"]:
            messages.error(
                request, f"Failed to sync IP addresses: {', '.join(results['failed'])}"
            )
