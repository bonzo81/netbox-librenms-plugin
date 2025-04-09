from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View
from ipam.models import VRF, IPAddress

from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable
from netbox_librenms_plugin.utils import get_interface_name_field
from netbox_librenms_plugin.views.mixins import CacheMixin, LibreNMSAPIMixin


class BaseIPAddressTableView(LibreNMSAPIMixin, CacheMixin, View):
    """
    Base view for synchronizing IP address information from LibreNMS.
    """

    partial_template_name = "netbox_librenms_plugin/_ipaddress_sync_content.html"
    interface_name_field = None

    def get_object(self, pk):
        return get_object_or_404(self.model, pk=pk)

    def get_ip_addresses(self, obj):
        """Fetch IP address data from LibreNMS for the given object."""
        self.librenms_id = self.librenms_api.get_librenms_id(obj)
        return self.librenms_api.get_device_ips(self.librenms_id)

    def enrich_ip_data(self, ip_data, obj, interface_name_field):
        """
        Enrich IP data with NetBox information in a more efficient manner.
        
        This optimized implementation:
        1. Caches port data to reduce API calls
        2. Pre-loads all relevant device data
        3. Uses dictionary lookups instead of repeated iterations
        """
        # Prefetch all necessary data
        prefetched_data = self._prefetch_netbox_data(obj)
        port_data_cache = {}  # Cache for LibreNMS port data to minimize API calls
        
        enriched_data = []
        
        # Process each IP address from LibreNMS
        for ip_entry in ip_data:
            # Get or fetch port data (with caching)
            port_info = self._get_port_info(ip_entry["port_id"], port_data_cache, interface_name_field)
            
            # Create enriched IP structure with base data
            enriched_ip = self._create_base_ip_entry(ip_entry, obj, prefetched_data["vrfs"])
            
            # Get LibreNMS interface name if available
            librenms_interface_name = None
            if port_info:
                librenms_interface_name = port_info.get(interface_name_field)
                enriched_ip["interface_name"] = librenms_interface_name
            
            # Check if IP exists in NetBox
            ip_with_mask = f"{ip_entry['ipv4_address']}/{ip_entry['ipv4_prefixlen']}"
            ip_address = prefetched_data["ip_addresses_map"].get(ip_with_mask)
            
            if ip_address:
                # Process existing IP
                self._enrich_existing_ip(
                    enriched_ip, 
                    ip_address, 
                    ip_entry["port_id"],
                    librenms_interface_name,
                    prefetched_data
                )
            else:
                # New IP that doesn't exist in NetBox
                enriched_ip["exists"] = False
                enriched_ip["status"] = "sync"
                
            # Add interface information (regardless of IP status)
            self._add_interface_info_to_ip(
                enriched_ip,
                ip_entry["port_id"],
                librenms_interface_name,
                prefetched_data
            )
                
            enriched_data.append(enriched_ip)
            
        return enriched_data

    def _prefetch_netbox_data(self, obj):
        """Prefetch all necessary NetBox data to minimize database queries"""
        # Get all interfaces for the device
        all_interfaces = list(obj.interfaces.all())
        
        # Create maps for efficient lookups
        interfaces_by_librenms_id = {
            interface.custom_field_data.get("librenms_id"): interface
            for interface in all_interfaces
            if interface.custom_field_data.get("librenms_id")
        }
        
        interfaces_by_name = {
            interface.name: interface
            for interface in all_interfaces
        }
        
        # Get all IP addresses
        ip_addresses_map = {
            str(ip.address): ip
            for ip in IPAddress.objects.select_related("assigned_object_type", "vrf")
        }
        
        # Get all VRFs
        vrfs = list(VRF.objects.all())
        
        return {
            "interfaces_by_librenms_id": interfaces_by_librenms_id,
            "interfaces_by_name": interfaces_by_name,
            "all_interfaces": all_interfaces,
            "device": obj,
            "ip_addresses_map": ip_addresses_map,
            "vrfs": vrfs
        }

    def _get_port_info(self, port_id, port_data_cache, interface_name_field):
        """Get port info from LibreNMS with caching to minimize API calls"""
        if port_id not in port_data_cache:
            success, port_data = self.librenms_api.get_port_by_id(port_id)
            if success and "port" in port_data and port_data["port"]:
                port_data_cache[port_id] = port_data["port"][0]
            else:
                port_data_cache[port_id] = None
        
        return port_data_cache[port_id]

    def _create_base_ip_entry(self, ip_entry, obj, vrfs):
        """Create the base data structure for an IP entry"""
        return {
            "ipv4_address": ip_entry["ipv4_address"],
            "ipv4_prefixlen": ip_entry["ipv4_prefixlen"],
            "port_id": ip_entry["port_id"],
            "device": obj.name,
            "device_url": obj.get_absolute_url(),
            "vrf_id": None,
            "vrfs": vrfs,
        }

    def _enrich_existing_ip(self, enriched_ip, ip_address, port_id, librenms_interface_name, prefetched_data):
        """Add information for IP addresses that exist in NetBox"""
        enriched_ip["ip_url"] = ip_address.get_absolute_url()
        enriched_ip["exists"] = True
        
        # Add VRF info if available
        if ip_address.vrf:
            enriched_ip["vrf_id"] = ip_address.vrf.pk
            enriched_ip["vrf"] = ip_address.vrf.name
        
        # Set initial status to update (will change to matched if criteria met)
        enriched_ip["status"] = "update"
        
        # Only proceed if IP is assigned to an object
        if not ip_address.assigned_object:
            return
        
        assigned_interface = ip_address.assigned_object
        
        # Check if interface matches by LibreNMS ID
        if port_id in prefetched_data["interfaces_by_librenms_id"]:
            interface = prefetched_data["interfaces_by_librenms_id"][port_id]
            if assigned_interface == interface:
                enriched_ip["status"] = "matched"
                return
                
        # Check if interface matches by name
        if (librenms_interface_name and 
                assigned_interface.name == librenms_interface_name):
            enriched_ip["status"] = "matched"
            # Add interface information
            enriched_ip["interface_name"] = assigned_interface.name
            enriched_ip["interface_url"] = assigned_interface.get_absolute_url()

    def _add_interface_info_to_ip(self, enriched_ip, port_id, librenms_interface_name, prefetched_data):
        """Add interface information to the IP entry regardless of IP status"""
        # First try to match by LibreNMS ID (highest priority)
        if port_id in prefetched_data["interfaces_by_librenms_id"]:
            interface = prefetched_data["interfaces_by_librenms_id"][port_id]
            enriched_ip["interface_name"] = interface.name
            enriched_ip["interface_url"] = interface.get_absolute_url()
            return
            
        # Then try to match by interface name
        if librenms_interface_name and librenms_interface_name in prefetched_data["interfaces_by_name"]:
            interface = prefetched_data["interfaces_by_name"][librenms_interface_name]
            # Don't overwrite the interface name from LibreNMS but do add the URL
            enriched_ip["interface_url"] = interface.get_absolute_url()

    def get_table(self, data, obj, request):
        """Get the table instance for the view."""
        table = IPAddressTable(data)
        table.htmx_url = f"{request.path}?tab=ipaddresses"
        return table

    def _prepare_context(self, request, obj, interface_name_field, fetch_fresh=False):
        """Helper method to prepare the context data for IP address sync views."""
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
        """Get the context data for the IP address sync view."""
        interface_name_field = get_interface_name_field(request)
        context = self._prepare_context(
            request, obj, interface_name_field, fetch_fresh=False
        )
        if context is None:
            # No data found; return context with empty table
            context = {"table": None, "object": obj, "cache_expiry": None}
        return context

    def post(self, request, pk):
        """Handle POST request for IP address sync view."""
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
