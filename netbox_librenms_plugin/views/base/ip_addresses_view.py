import json

from dcim.models import Device
from django.contrib import messages
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View
from ipam.models import VRF, IPAddress
from virtualization.models import VirtualMachine

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
            # Skip invalid entries that are not dictionaries
            if not isinstance(ip_entry, dict):
                continue

            # Skip entries missing required fields
            if "port_id" not in ip_entry:
                continue

            # Get or fetch port data (with caching)
            port_info = self._get_port_info(
                ip_entry["port_id"], port_data_cache, interface_name_field
            )

            # Create enriched IP structure with base data
            enriched_ip = self._create_base_ip_entry(
                ip_entry, obj, prefetched_data["vrfs"]
            )

            # Get LibreNMS interface name if available
            librenms_interface_name = None
            if port_info:
                librenms_interface_name = port_info.get(interface_name_field)
                enriched_ip["interface_name"] = librenms_interface_name

            # IP with mask is already calculated in _create_base_ip_entry
            ip_with_mask = enriched_ip["ip_with_mask"]
            ip_address = prefetched_data["ip_addresses_map"].get(ip_with_mask)

            if ip_address:
                # Process existing IP
                self._enrich_existing_ip(
                    enriched_ip,
                    ip_address,
                    ip_entry["port_id"],
                    librenms_interface_name,
                    prefetched_data,
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
                prefetched_data,
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

        interfaces_by_name = {interface.name: interface for interface in all_interfaces}

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
            "vrfs": vrfs,
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
        # Determine if this is an IPv4 or IPv6 address and create unified fields
        if "ip_address" in ip_entry and "prefix_length" in ip_entry:
            # Use unified format directly if available
            ip_address = ip_entry["ip_address"]
            prefix_length = ip_entry["prefix_length"]
        else:
            # Legacy format handling
            if "ipv6_compressed" in ip_entry:
                ip_address = ip_entry["ipv6_compressed"]
                prefix_length = ip_entry["ipv6_prefixlen"]
            elif "ipv4_address" in ip_entry:
                ip_address = ip_entry["ipv4_address"]
                prefix_length = ip_entry["ipv4_prefixlen"]
            else:
                raise KeyError("No valid IP address format found in LibreNMS data")

        ip_with_mask = f"{ip_address}/{prefix_length}"

        return {
            "ip_address": ip_address,
            "prefix_length": prefix_length,
            "ip_with_mask": ip_with_mask,
            "port_id": ip_entry["port_id"],
            "device": obj.name,
            "device_url": obj.get_absolute_url(),
            "vrf_id": None,
            "vrfs": vrfs,
        }

    def _enrich_existing_ip(
        self, enriched_ip, ip_address, port_id, librenms_interface_name, prefetched_data
    ):
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
        if (
            librenms_interface_name
            and assigned_interface.name == librenms_interface_name
        ):
            enriched_ip["status"] = "matched"
            # Add interface information
            enriched_ip["interface_name"] = assigned_interface.name
            enriched_ip["interface_url"] = assigned_interface.get_absolute_url()

    def _add_interface_info_to_ip(
        self, enriched_ip, port_id, librenms_interface_name, prefetched_data
    ):
        """Add interface information to the IP entry regardless of IP status"""
        # First try to match by LibreNMS ID (highest priority)
        if port_id in prefetched_data["interfaces_by_librenms_id"]:
            interface = prefetched_data["interfaces_by_librenms_id"][port_id]
            enriched_ip["interface_name"] = interface.name
            enriched_ip["interface_url"] = interface.get_absolute_url()
            return

        # Then try to match by interface name
        if (
            librenms_interface_name
            and librenms_interface_name in prefetched_data["interfaces_by_name"]
        ):
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
            success, ip_data = self.get_ip_addresses(obj)
        else:
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


class SingleIPAddressVerifyView(CacheMixin, View):
    """
    View for verifying single IP address data with different VRF.
    """

    def _get_object(self, object_id, object_type=None):
        """
        Retrieve the object (Device or VirtualMachine) based on ID and optional type.
        If type is not provided, tries to determine it by checking both Device and VM models.
        """
        if object_type == "device":
            return get_object_or_404(Device, pk=object_id)
        elif object_type == "virtualmachine":
            return get_object_or_404(VirtualMachine, pk=object_id)
        else:
            # Try to find object without knowing its type
            obj = Device.objects.filter(pk=object_id).first()
            if obj:
                return obj

            obj = VirtualMachine.objects.filter(pk=object_id).first()
            if obj:
                return obj

            raise Http404(
                f"Object with ID {object_id} not found in Device or VirtualMachine models"
            )

    def _parse_ip_address(self, ip_address):
        """
        Parse IP address string into address and prefix length.
        Works with both IPv4 and IPv6 addresses.
        """
        ip_address_parts = ip_address.split("/")
        address_no_mask = ip_address_parts[0].strip()

        if len(ip_address_parts) > 1:
            try:
                prefix_len = int(ip_address_parts[1])
                return address_no_mask, prefix_len
            except ValueError:
                raise ValueError(f"Invalid prefix length: {ip_address_parts[1]}")
        else:
            raise ValueError("Prefix length is missing from the IP address")

    def _find_in_cache(self, cached_data, address, prefix_len):
        """Find IP address in cache data using unified fields only."""
        if not cached_data:
            return None, None, None

        for ip_entry in cached_data.get("ip_addresses", []):
            if ip_entry["ip_address"] == address and str(
                ip_entry["prefix_length"]
            ) == str(prefix_len):
                return (ip_entry, ip_entry.get("vrf_id"), ip_entry.get("port_id"))

        return None, None, None

    def _find_existing_ip(self, address_no_mask, prefix_len, vrf_id=None):
        """
        Find existing IP address in NetBox, optionally with specific VRF.
        """
        ip_with_mask = f"{address_no_mask}/{prefix_len}"

        # Check if IP exists in any VRF
        existing_ip = IPAddress.objects.filter(address=ip_with_mask).first()
        if not existing_ip:
            return False, False, None

        # IP exists in some VRF, check if it exists in the specified VRF
        if vrf_id is not None:
            existing_in_vrf = IPAddress.objects.filter(
                address=ip_with_mask, vrf__id=vrf_id
            ).exists()
        else:
            # Check for global VRF (None)
            existing_in_vrf = IPAddress.objects.filter(
                address=ip_with_mask, vrf__isnull=True
            ).exists()

        return True, existing_in_vrf, existing_ip.get_absolute_url()

    def _determine_status(
        self, exists_any_vrf, exists_specific_vrf, original_vrf_id, vrf_id
    ):
        """
        Determine the status of an IP address based on existence and VRF.
        """
        if exists_any_vrf:
            # IP exists in NetBox
            if exists_specific_vrf:
                return "matched"
            else:
                return "update"
        else:
            # IP doesn't exist in NetBox, check if restoring to original VRF
            if original_vrf_id is not None and original_vrf_id == vrf_id:
                return "matched"
            else:
                return "sync"

    def _get_cache_key(self, obj, data_type):
        """
        Generate a cache key for the specified object and data type.
        """
        return f"librenms_plugin:{obj.__class__.__name__}:{obj.pk}:{data_type}"

    def post(self, request):
        """
        POST request to return json response with formatted IP address status.
        """
        try:
            data = json.loads(request.body)
            ip_address = data.get("ip_address")
            vrf_id = data.get("vrf_id")
            object_id = data.get("device_id")
            object_type = data.get("object_type")

            if not ip_address:
                return JsonResponse(
                    {"status": "error", "message": "No IP address provided"}, status=400
                )

            if not object_id:
                return JsonResponse(
                    {"status": "error", "message": "No object ID provided"}, status=400
                )

            # Get the object (Device or VirtualMachine)
            try:
                obj = self._get_object(object_id, object_type)
            except Http404 as e:
                return JsonResponse({"status": "error", "message": str(e)}, status=404)

            # Parse IP address
            try:
                address_no_mask, prefix_len = self._parse_ip_address(ip_address)
            except ValueError as e:
                return JsonResponse({"status": "error", "message": str(e)}, status=400)

            cache_key = self._get_cache_key(obj, "ip_addresses")
            cached_data = cache.get(cache_key)

            # Basic record with default values
            updated_record = {
                "ip_address": address_no_mask,
                "prefix_length": prefix_len,
                "ip_with_mask": f"{address_no_mask}/{prefix_len}",
                "device": obj.name,
                "device_url": obj.get_absolute_url(),
                "vrf_id": vrf_id,
                "exists": False,
                "status": "sync",
            }

            # Try to find the IP in cache data
            cache_entry, original_vrf_id, original_port_id = self._find_in_cache(
                cached_data, address_no_mask, prefix_len
            )

            # Update record with cache data if found
            if cache_entry:
                # Update with all fields except vrf_id and status
                for key, value in cache_entry.items():
                    if key not in ["vrf_id", "status"]:
                        updated_record[key] = value

            # If no interface found in cache, use first device interface
            if original_port_id is None:
                interface = obj.interfaces.first()
                if interface:
                    updated_record["interface_name"] = interface.name
                    updated_record["interface_url"] = interface.get_absolute_url()

            # Check if IP exists in NetBox
            exists_any_vrf, exists_specific_vrf, ip_url = self._find_existing_ip(
                address_no_mask, prefix_len, vrf_id
            )

            if exists_any_vrf:
                updated_record["exists"] = True
                updated_record["ip_url"] = ip_url

            # Determine status based on existence and VRF
            updated_record["status"] = self._determine_status(
                exists_any_vrf, exists_specific_vrf, original_vrf_id, vrf_id
            )

            # Render status HTML
            table = IPAddressTable(data=[])
            status_html = table.render_status(updated_record["status"], updated_record)

            return JsonResponse(
                {
                    "status": "success",
                    "ip_address": ip_address,
                    "formatted_row": {"status": status_html},
                }
            )

        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)}, status=500)
