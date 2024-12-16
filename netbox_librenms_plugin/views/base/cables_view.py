import json

from dcim.models import Device, Interface
from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.utils import (
    get_interface_name_field,
    get_virtual_chassis_member,
)
from netbox_librenms_plugin.views.mixins import CacheMixin, LibreNMSAPIMixin


class BaseCableTableView(LibreNMSAPIMixin, CacheMixin, View):
    """
    Base view for synchronizing cable information from LibreNMS.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_cable_sync_content.html"
    interface_name_field = get_interface_name_field()

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
        Fetch links data from LibreNMS for the device and add local port names.
        """
        self.librenms_id = self.librenms_api.get_librenms_id(obj)
        success, data = self.librenms_api.get_device_links(self.librenms_id)
        if not success or "error" in data:
            return None

        ports_data = self.get_ports_data(obj)
        local_ports_map = {}
        for port in ports_data.get("ports", []):
            port_id = str(port["port_id"])
            port_name = port[self.interface_name_field]
            local_ports_map[port_id] = port_name

        links = data.get("links", [])
        links_data = []
        for link in links:
            local_port_name = local_ports_map.get(str(link.get("local_port_id")))
            links_data.append(
                {
                    "local_port": local_port_name,
                    "local_port_id": link.get("local_port_id"),
                    "remote_port": link.get("remote_port"),
                    "remote_device": link.get("remote_hostname"),
                    "remote_port_id": link.get("remote_port_id"),
                    "remote_device_id": link.get("remote_device_id"),
                }
            )
        return links_data

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
            interface = None
            local_port_id = link.get("local_port_id")

            if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                chassis_member = get_virtual_chassis_member(obj, local_port)

                # First try to find interface by librenms_id
                if local_port_id:
                    interface = chassis_member.interfaces.filter(
                        custom_field_data__librenms_id=local_port_id
                    ).first()

                # Only if librenms_id match fails, try matching by name
                if not interface:
                    interface = chassis_member.interfaces.filter(
                        name=local_port
                    ).first()
            else:
                # First try to find interface by librenms_id
                if local_port_id:
                    interface = obj.interfaces.filter(
                        custom_field_data__librenms_id=local_port_id
                    ).first()

                # Only if librenms_id match fails, try matching by name
                if not interface:
                    interface = obj.interfaces.filter(name=local_port).first()

            if interface:
                link["local_port_url"] = reverse("dcim:interface", args=[interface.pk])
                link["netbox_local_interface_id"] = interface.pk

    def enrich_remote_port(self, link, device):
        """
        Add remote port URL if device and interface exist in NetBox
        """
        if remote_port := link.get("remote_port"):
            # First try to find interface by librenms_id
            librenms_remote_port_id = link.get("remote_port_id")
            netbox_remote_interface = None
            if librenms_remote_port_id:
                netbox_remote_interface = device.interfaces.filter(
                    custom_field_data__librenms_id=librenms_remote_port_id
                ).first()

            # If not found by librenms_id, fall back to name matching
            if not netbox_remote_interface:
                netbox_remote_interface = device.interfaces.filter(
                    name=remote_port
                ).first()

            if netbox_remote_interface:
                link["remote_port_url"] = reverse(
                    "dcim:interface", args=[netbox_remote_interface.pk]
                )
                link["netbox_remote_interface_id"] = netbox_remote_interface.pk
                link["remote_port_name"] = netbox_remote_interface.name

        return link

    def check_cable_status(self, link):
        """
        Check cable status and add cable URL if cable exists in NetBox
        """
        local_interface_id = link.get("netbox_local_interface_id")
        remote_interface_id = link.get("netbox_remote_interface_id")

        # Default state
        link["can_create_cable"] = False

        if local_interface_id and remote_interface_id:
            local_interface = Interface.objects.get(pk=local_interface_id)
            remote_interface = Interface.objects.get(pk=remote_interface_id)
            existing_cable = local_interface.cable or remote_interface.cable

            if existing_cable:
                link.update(
                    {
                        "cable_status": "Cable Found",
                        "cable_url": reverse("dcim:cable", args=[existing_cable.pk]),
                    }
                )
            else:
                link.update({"cable_status": "No Cable", "can_create_cable": True})
        else:
            link["cable_status"] = (
                "Both Interfaces Not Found in Netbox"
                if not (local_interface_id or remote_interface_id)
                else "Local Interface Not Found in Netbox"
                if not local_interface_id
                else "Remote Interface Not Found in Netbox"
            )

        return link

    def process_remote_device(self, link, remote_hostname, remote_device_id):
        """
        Process remote device data and add remote device URL if device exists in NetBox
        """
        device, found = self.get_device_by_id_or_name(remote_device_id, remote_hostname)
        if found:
            link.update(
                {
                    "remote_device_url": reverse("dcim:device", args=[device.pk]),
                    "netbox_remote_device_id": device.pk,
                }
            )
            return self.enrich_remote_port(link, device)

        link.update(
            {
                "remote_port_name": link["remote_port"],
                "cable_status": "Device Not Found in NetBox",
                "can_create_cable": False,
            }
        )
        return link

    def enrich_links_data(self, links_data, obj):
        """
        Enrich links data with local and remote port URLs and cable status.
        """
        for link in links_data:
            self.enrich_local_port(link, obj)
            link["device_id"] = obj.id

            if remote_hostname := link.get("remote_device"):
                link = self.process_remote_device(
                    link, remote_hostname, link.get("remote_device_id")
                )
                if link.get("netbox_remote_device_id"):
                    link = self.check_cable_status(link)

        return links_data

    def get_table(self, data, obj):
        """
        Get the table instance for the view.
        """
        table = super().get_table(data, obj)
        table.htmx_url = f"{self.request.path}?tab=cables"
        return table

    def _prepare_context(self, request, obj, fetch_fresh=False):
        """
        Helper method to prepare the context data for cable sync views.
        """
        table = None
        cache_expiry = None

        if fetch_fresh:
            # Always fetch new data when requested
            links_data = self.get_links_data(obj)
            if not links_data:
                return None
        else:
            # Try to use cached data
            cached_links_data = cache.get(self.get_cache_key(obj, "links"))
            if cached_links_data:
                links_data = cached_links_data.get("links", [])
            else:
                return None

        # Enrich data in both cases to ensure current NetBox state
        links_data = self.enrich_links_data(links_data, obj)

        if fetch_fresh:
            # Cache the fresh data after enrichment
            cache.set(
                self.get_cache_key(obj, "links"),
                {"links": links_data},
                timeout=self.librenms_api.cache_timeout,
            )

        # Calculate cache expiry
        cache_ttl = cache.ttl(self.get_cache_key(obj, "links"))
        if cache_ttl is not None:
            cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl)

        # Generate the table
        table = self.get_table(links_data, obj)

        table.configure(request)

        # Prepare and return the context
        return {
            "table": table,
            "object": obj,
            "cache_expiry": cache_expiry,
        }

    def get_context_data(self, request, obj):
        """
        Get the context data for the cable sync view.
        """
        context = self._prepare_context(request, obj, fetch_fresh=False)
        if context is None:
            # No data found; return context with empty table
            context = {"table": None, "object": obj, "cache_expiry": None}
        return context

    def post(self, request, pk):
        """
        Handle POST request for cable sync view.
        """
        obj = self.get_object(pk)
        context = self._prepare_context(request, obj, fetch_fresh=True)

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


class SingleCableVerifyView(BaseCableTableView):
    """
    View to verify a single cable link between two devices.
    """

    def post(self, request):
        data = json.loads(request.body)
        selected_device_id = data.get("device_id")
        local_port = data.get("local_port")

        formatted_row = {
            "local_port": local_port,
            "remote_port": "",
            "remote_device": "",
            "cable_status": "Missing Ports",
            "actions": "",
        }

        if selected_device_id:
            selected_device = get_object_or_404(Device, pk=selected_device_id)

            # Get the primary device (master or first with IP) if part of virtual chassis
            if selected_device.virtual_chassis:
                primary_device = selected_device.virtual_chassis.master
                if not primary_device or not primary_device.primary_ip:
                    primary_device = next(
                        (
                            member
                            for member in selected_device.virtual_chassis.members.all()
                            if member.primary_ip
                        ),
                        None,
                    )
            else:
                primary_device = selected_device

            cached_links = cache.get(self.get_cache_key(primary_device, "links"))

            if cached_links:
                link_data = next(
                    (
                        link
                        for link in cached_links.get("links", [])
                        if link["local_port"] == local_port
                    ),
                    None,
                )
                if link_data:
                    # First try to find interface by librenms_id
                    interface = None
                    if local_port_id := link_data.get("local_port_id"):
                        interface = selected_device.interfaces.filter(
                            custom_field_data__librenms_id=local_port_id
                        ).first()

                    # If not found by librenms_id, try matching by name
                    if not interface:
                        interface = selected_device.interfaces.filter(
                            name=local_port
                        ).first()

                    if interface:
                        link_data["netbox_local_interface_id"] = interface.pk

                        # Check remote device existence first
                        remote_device_name = link_data.get("remote_device", "")
                        if remote_device_name and not link_data.get(
                            "remote_device_url"
                        ):
                            formatted_row["cable_status"] = "Device Not Found in NetBox"
                        else:
                            link_data = self.check_cable_status(link_data)
                            formatted_row["cable_status"] = link_data["cable_status"]

                        formatted_row["local_port"] = (
                            f'<a href="{reverse("dcim:interface", args=[interface.pk])}">{local_port}</a>'
                        )
                        remote_port_name = link_data.get(
                            "remote_port_name", link_data.get("remote_port", "")
                        )
                        formatted_row["remote_port"] = (
                            f'<a href="{link_data["remote_port_url"]}">{remote_port_name}</a>'
                            if link_data.get("remote_port_url")
                            else remote_port_name
                        )
                        remote_device_name = link_data.get("remote_device", "")
                        formatted_row["remote_device"] = (
                            f'<a href="{link_data["remote_device_url"]}">{remote_device_name}</a>'
                            if link_data.get("remote_device_url")
                            else remote_device_name
                        )
                        if link_data.get("cable_url"):
                            formatted_row["cable_status"] = (
                                f'<a href="{link_data["cable_url"]}">{link_data["cable_status"]}</a>'
                            )
                        else:
                            formatted_row["cable_status"] = link_data["cable_status"]

                        if link_data.get("can_create_cable"):
                            formatted_row["actions"] = f"""
                                <form method="post" action="{reverse('plugins:netbox_librenms_plugin:sync_device_cables', args=[selected_device.id])}">
                                    <input type="hidden" name="select" value="{local_port}">
                                    <button type="submit" class="btn btn-sm btn-primary">Sync Cable</button>
                                </form>
                            """
                    else:
                        formatted_row["local_port"] = local_port
                        # Keep remote port name visible, add URL if available
                        remote_port_name = link_data.get(
                            "remote_port_name", link_data.get("remote_port", "")
                        )
                        formatted_row["remote_port"] = (
                            f'<a href="{link_data["remote_port_url"]}">{remote_port_name}</a>'
                            if link_data.get("remote_port_url")
                            else remote_port_name
                        )
                        # Keep remote device name visible, add URL if available
                        remote_device_name = link_data.get("remote_device", "")
                        formatted_row["remote_device"] = (
                            f'<a href="{link_data["remote_device_url"]}">{remote_device_name}</a>'
                            if link_data.get("remote_device_url")
                            else remote_device_name
                        )

                        # First check if remote device exists in NetBox
                        if remote_device_name and not link_data.get(
                            "remote_device_url"
                        ):
                            formatted_row["cable_status"] = "Device Not Found in NetBox"
                        # Then check interface status
                        elif link_data.get("remote_device_url") and link_data.get(
                            "remote_port_url"
                        ):
                            formatted_row["cable_status"] = (
                                "Local Interface Not Found in NetBox"
                            )
                        else:
                            formatted_row["cable_status"] = "Missing Interface"

                        formatted_row["actions"] = ""

        return JsonResponse({"status": "success", "formatted_row": formatted_row})
