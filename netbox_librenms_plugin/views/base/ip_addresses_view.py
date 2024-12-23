from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View

from ipam.models import IPAddress

from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable
from netbox_librenms_plugin.views.mixins import CacheMixin, LibreNMSAPIMixin
from netbox_librenms_plugin.utils import get_interface_name_field


class BaseIPAddressTableView(LibreNMSAPIMixin, CacheMixin, View):
    """
    Base view for synchronizing IP address information from LibreNMS.
    """

    partial_template_name = "netbox_librenms_plugin/_ipaddress_sync_content.html"
    interface_name_field = None

    def get_object(self, pk):
        return get_object_or_404(self.model, pk=pk)

    def get_ip_addresses(self, obj):
        """
        Fetch IP address data from LibreNMS for the given object.
        """
        self.librenms_id = self.librenms_api.get_librenms_id(obj)
        return self.librenms_api.get_device_ips(self.librenms_id)

    def enrich_ip_data(self, ip_data, obj, interface_name_field):
        enriched_data = []

        # Pre-fetch interfaces with their related data
        interfaces_map = {
            interface.custom_field_data.get("librenms_id"): interface
            for interface in obj.interfaces.all()
        }

        # Pre-fetch IP addresses with correct related fields
        ip_addresses_map = {
            str(ip.address): ip
            for ip in IPAddress.objects.select_related("assigned_object_type")
        }

        for ip_entry in ip_data:
            enriched_ip = {
                "ipv4_address": ip_entry["ipv4_address"],
                "ipv4_prefixlen": ip_entry["ipv4_prefixlen"],
                "port_id": ip_entry["port_id"],
                "device": obj.name,
                "device_url": obj.get_absolute_url(),
            }

            # Check if IP exists in NetBox using the pre-fetched map
            ip_with_mask = f"{ip_entry['ipv4_address']}/{ip_entry['ipv4_prefixlen']}"
            ip_address = ip_addresses_map.get(ip_with_mask)

            if ip_address:
                enriched_ip["ip_url"] = ip_address.get_absolute_url()
                enriched_ip["exists"] = True

                # Get interface from pre-fetched map
                interface = interfaces_map.get(ip_entry["port_id"])
                if interface and ip_address.assigned_object == interface:
                    enriched_ip["status"] = "matched"
                else:
                    enriched_ip["status"] = "update"
            else:
                enriched_ip["exists"] = False
                enriched_ip["status"] = "sync"

            # Get interface info from pre-fetched map
            interface = interfaces_map.get(ip_entry["port_id"])
            if interface:
                enriched_ip["interface_name"] = interface.name
                enriched_ip["interface_url"] = interface.get_absolute_url()
            else:
                # Fallback to API call only when necessary
                success, port_data = self.librenms_api.get_port_by_id(
                    ip_entry["port_id"]
                )
                if success:
                    port_info = port_data.get('port')[0]  # Get first port from list
                    enriched_ip["interface_name"] = port_info.get(interface_name_field)
                    # Try to find interface by name in pre-fetched map
                    interface = next(
                        (i for i in interfaces_map.values() if i.name == enriched_ip["interface_name"]),
                        None
                    )
                    if interface:
                        enriched_ip["interface_url"] = interface.get_absolute_url()

            enriched_data.append(enriched_ip)

        return enriched_data

    def get_table(self, data, obj, request):
        """
        Get the table instance for the view.
        """
        table = IPAddressTable(data)
        table.htmx_url = f"{request.path}?tab=ipaddresses"
        return table

    def _prepare_context(self, request, obj, interface_name_field, fetch_fresh=False):
        """
        Helper method to prepare the context data for IP address sync views.
        """
        table = None
        cache_expiry = None

        if interface_name_field is None:
            interface_name_field = get_interface_name_field(request)

        if fetch_fresh:
            # Always fetch new data when requested
            success, ip_data = self.get_ip_addresses(obj)
            if not success:
                return None
        else:
            # Try to use cached data
            cached_ip_data = cache.get(self.get_cache_key(obj, "ip_addresses"))
            if cached_ip_data:
                ip_data = cached_ip_data.get("ip_addresses", [])
            else:
                return None

        # Enrich data in both cases to ensure current NetBox state
        ip_data = self.enrich_ip_data(ip_data, obj, interface_name_field)

        if fetch_fresh:
            # Cache the fresh data after enrichment
            cache.set(
                self.get_cache_key(obj, "ip_addresses"),
                {"ip_addresses": ip_data},
                timeout=self.librenms_api.cache_timeout,
            )

        # Calculate cache expiry
        cache_ttl = cache.ttl(self.get_cache_key(obj, "ip_addresses"))
        if cache_ttl is not None:
            cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl)

        # Generate the table
        table = self.get_table(ip_data, obj, request)

        table.configure(request)

        # Prepare and return the context
        return {
            "table": table,
            "object": obj,
            "cache_expiry": cache_expiry,
        }

    def get_context_data(self, request, obj):
        """
        Get the context data for the IP address sync view.
        """
        interface_name_field = get_interface_name_field(request)
        context = self._prepare_context(
            request, obj, interface_name_field, fetch_fresh=False
        )
        if context is None:
            # No data found; return context with empty table
            context = {"table": None, "object": obj, "cache_expiry": None}
        return context

    def post(self, request, pk):
        """
        Handle POST request for IP address sync view.
        """
        obj = self.get_object(pk)
        interface_name_field = get_interface_name_field(request)
        context = self._prepare_context(
            request, obj, interface_name_field, fetch_fresh=True
        )

        if context is None:
            messages.error(request, "No IP addresses found in LibreNMS")
            return render(
                request,
                self.partial_template_name,
                {"ip_sync": {"object": obj, "table": None, "cache_expiry": None}},
            )

        messages.success(request, "IP address data refreshed successfully.")
        return render(
            request,
            self.partial_template_name,
            {"ip_sync": context},
        )
