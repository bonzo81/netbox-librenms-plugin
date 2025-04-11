import json

from dcim.models import Device
from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse
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
        """Enrich IP data with NetBox information"""
        enriched_data = []

        # Pre-fetch interfaces with their related data
        interfaces_map = {
            interface.custom_field_data.get("librenms_id"): interface
            for interface in obj.interfaces.all()
        }

        # Pre-fetch IP addresses with correct related fields
        ip_addresses_map = {
            str(ip.address): ip
            for ip in IPAddress.objects.select_related("assigned_object_type", "vrf")
        }

        # Get all VRFs for the dropdown
        vrfs = list(VRF.objects.all())

        for ip_entry in ip_data:
            enriched_ip = {
                "ipv4_address": ip_entry["ipv4_address"],
                "ipv4_prefixlen": ip_entry["ipv4_prefixlen"],
                "port_id": ip_entry["port_id"],
                "device": obj.name,
                "device_url": obj.get_absolute_url(),
                "vrf_id": None,
                "vrfs": vrfs,
            }

            # Check if IP exists in NetBox using the pre-fetched map
            ip_with_mask = f"{ip_entry['ipv4_address']}/{ip_entry['ipv4_prefixlen']}"
            ip_address = ip_addresses_map.get(ip_with_mask)

            if ip_address:
                enriched_ip["ip_url"] = ip_address.get_absolute_url()
                enriched_ip["exists"] = True
                if ip_address.vrf:
                    enriched_ip["vrf_id"] = ip_address.vrf.pk
                    enriched_ip["vrf"] = ip_address.vrf.name

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
                    port_info = port_data.get("port")[0]  # Get first port from list
                    enriched_ip["interface_name"] = port_info.get(interface_name_field)
                    # Try to find interface by name in pre-fetched map
                    interface = next(
                        (
                            i
                            for i in interfaces_map.values()
                            if i.name == enriched_ip["interface_name"]
                        ),
                        None,
                    )
                    if interface:
                        enriched_ip["interface_url"] = interface.get_absolute_url()

            enriched_data.append(enriched_ip)

        return enriched_data

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


class SingleIPAddressVerifyView(CacheMixin, View):
    """
    View for verifying single IP address data with different VRF.
    """

    def _parse_ip_address(self, ip_address):
        """
        Parse IP address string into address and prefix length.
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
        """
        Find IP address in cache data.
        """
        if not cached_data:
            return None, None, None

        for ip_entry in cached_data.get("ip_addresses", []):
            cache_address = ip_entry.get("ipv4_address")
            cache_prefix = ip_entry.get("ipv4_prefixlen")

            if cache_address == address and str(cache_prefix) == str(prefix_len):
                original_vrf_id = ip_entry.get("vrf_id")
                original_port_id = ip_entry.get("port_id")
                return ip_entry, original_vrf_id, original_port_id

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
            device_id = data.get("device_id")

            if not ip_address:
                return JsonResponse(
                    {"status": "error", "message": "No IP address provided"}, status=400
                )

            # Get the device
            device = get_object_or_404(Device, pk=device_id)

            # Parse IP address
            try:
                address_no_mask, prefix_len = self._parse_ip_address(ip_address)
            except ValueError as e:
                return JsonResponse({"status": "error", "message": str(e)}, status=400)

            cache_key = self._get_cache_key(device, "ip_addresses")
            cached_data = cache.get(cache_key)

            # Basic record with default values
            updated_record = {
                "ipv4_address": address_no_mask,
                "ipv4_prefixlen": prefix_len,
                "device": device.name,
                "device_url": device.get_absolute_url(),
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
                interface = device.interfaces.first()
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
