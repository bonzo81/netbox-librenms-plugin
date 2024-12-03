from dcim.models import Device
from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.tables import LibreNMSCableTable
from netbox_librenms_plugin.utils import get_virtual_chassis_member
from netbox_librenms_plugin.views.mixins import CacheMixin, LibreNMSAPIMixin


class BaseCableTableView(LibreNMSAPIMixin, CacheMixin, View):
    """
    Base view for synchronizing cable information from LibreNMS.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_cable_sync_content.html"

    def get_object(self, pk):
        return get_object_or_404(self.model, pk=pk)

    def get_ip_address(self, obj):
        if obj.primary_ip:
            return str(obj.primary_ip.address.ip)
        return None

    def get_ports_data(self, obj):
        """
        Get ports data without affecting cache
        """
        cached_data = cache.get(self.get_cache_key(obj, "ports"))
        if cached_data:
            return cached_data
        return self.librenms_api.get_ports(self.librenms_id)

    def get_links_data(self, obj):
        """
        Fetch links data from LibreNMS for the device
        """
        self.librenms_id = self.librenms_api.get_librenms_id(obj)
        success, data = self.librenms_api.get_device_links(self.librenms_id)
        if not success or "error" in data:
            return None

        ports_data = self.get_ports_data(obj)
        ports_map = {
            str(port["port_id"]): port["ifDescr"]
            for port in ports_data.get("ports", [])
        }

        links = data.get("links", [])
        table_data = []
        for link in links:
            local_port_name = ports_map.get(str(link.get("local_port_id")))
            table_data.append(
                {
                    "local_port": local_port_name,
                    "remote_port": link.get("remote_port"),
                    "remote_device": link.get("remote_hostname"),
                    "remote_port_id": link.get("remote_port_id"),
                    "remote_device_id": link.get("remote_device_id"),
                }
            )

        return table_data

    def get_device_by_id_or_name(self, remote_device_id, hostname):
        """
        Try to find device in NetBox first by librenms_id custom field, then by name
        """
        # First try matching by LibreNMS ID
        if remote_device_id:
            try:
                device = Device.objects.get(
                    custom_field_data__librenms_id=remote_device_id
                )
                return device, True
            except Device.DoesNotExist:
                pass

        # Fall back to name matching if no device found by ID
        try:
            device = Device.objects.get(name=hostname)
            return device, True
        except Device.DoesNotExist:
            # Try without domain name
            simple_hostname = hostname.split(".")[0]
            try:
                device = Device.objects.get(name=simple_hostname)
                return device, True
            except Device.DoesNotExist:
                return None, False

    def enrich_local_port(self, link, obj):
        """
        Add local port URL if interface exists in NetBox
        """
        if local_port := link.get("local_port"):
            if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                chassis_member = get_virtual_chassis_member(obj, local_port)
                if interface := chassis_member.interfaces.filter(
                    name=local_port
                ).first():
                    link["local_port_url"] = reverse(
                        "dcim:interface", args=[interface.pk]
                    )
            else:
                if interface := obj.interfaces.filter(name=local_port).first():
                    link["local_port_url"] = reverse(
                        "dcim:interface", args=[interface.pk]
                    )

    def enrich_remote_port(self, link, device):
        """
        Add remote port URL if device and interface exist in NetBox
        """
        if remote_port := link.get("remote_port"):
            if remote_interface := device.interfaces.filter(name=remote_port).first():
                link["remote_port_url"] = reverse(
                    "dcim:interface", args=[remote_interface.pk]
                )
                link["remote_port_id"] = remote_interface.pk

    def enrich_links_data(self, links_data, obj):
        """
        Enrich links data with NetBox device information and port links
        """
        for link in links_data:
            self.enrich_local_port(link, obj)
            link["device_id"] = obj.id

            if remote_hostname := link.get("remote_device"):
                remote_device_id = link.get("remote_device_id")
                device, found = self.get_device_by_id_or_name(
                    remote_device_id, remote_hostname
                )
                if found:
                    link["remote_device_url"] = reverse("dcim:device", args=[device.pk])
                    link["remote_device_id"] = device.pk
                    self.enrich_remote_port(link, device)

        return links_data

    def get_table(self, data, obj):
        return LibreNMSCableTable(data, device=obj)

    def _prepare_context(self, request, obj, fetch_cached=False):
        """
        Helper method to prepare the context data for cable sync views.
        """
        # Attempt to retrieve cached data
        cached_links_data = cache.get(self.get_cache_key(obj, "links"))
        table = None
        cache_expiry = None

        if cached_links_data:
            links_data = cached_links_data.get("links", [])
        elif fetch_cached:
            # Fetch links data if not cached
            links_data = self.get_links_data(obj)
            if not links_data:
                return None  # Indicate that no data was found

            # Enrich links data
            links_data = self.enrich_links_data(links_data, obj)

            # Cache the enriched data
            cache.set(
                self.get_cache_key(obj, "links"),
                {"links": links_data},
                timeout=self.librenms_api.cache_timeout,
            )
        else:
            # If cache is empty and not fetching new data, return None
            return None

        # Calculate cache expiry
        cache_ttl = cache.ttl(self.get_cache_key(obj, "links"))
        if cache_ttl is not None:
            cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl)

        # Generate the table
        table = self.get_table(links_data, obj)

        # Prepare and return the context
        return {"table": table, "object": obj, "cache_expiry": cache_expiry}

    def get_context_data(self, request, obj):
        """
        Get the context data for the cable sync view.
        """
        context = self._prepare_context(request, obj, fetch_cached=False)
        if context is None:
            # No data found; return context with empty table
            context = {"table": None, "object": obj, "cache_expiry": None}
        return context

    def post(self, request, pk):
        obj = self.get_object(pk)
        context = self._prepare_context(request, obj, fetch_cached=True)

        if context is None:
            messages.error(request, "No links found in LibreNMS")
            return render(
                request,
                self.partial_template_name,
                {"cable_sync": {"object": obj, "table": None, "cache_expiry": None}},
            )

        messages.success(request, "Cable data refreshed successfully.")
        return render(
            request,
            self.partial_template_name,
            {"cable_sync": context},
        )
