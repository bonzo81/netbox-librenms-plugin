import json

from dcim.models import Device, Interface
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import MultipleObjectsReturned
from django.db.models import Q
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.views import View

from netbox_librenms_plugin.utils import (
    get_interface_name_field,
    get_librenms_sync_device,
    get_virtual_chassis_member,
)
from netbox_librenms_plugin.views.mixins import CacheMixin, LibreNMSAPIMixin, LibreNMSPermissionMixin


def _librenms_id_q(server_key: str, value) -> Q:
    """
    Return a combined Q matching JSON-field and legacy bare-int librenms_id.

    Matches both integer and string representations to handle any stored format.
    """
    if isinstance(value, bool):
        return Q(pk__isnull=True) & Q(pk__isnull=False)  # match nothing

    q = Q(**{f"custom_field_data__librenms_id__{server_key}": value}) | Q(custom_field_data__librenms_id=value)
    try:
        int_val = int(value)
        str_val = str(int_val)
        if int_val != value:  # value was a string; also add the integer variant
            q |= Q(**{f"custom_field_data__librenms_id__{server_key}": int_val})
            q |= Q(custom_field_data__librenms_id=int_val)
        if str_val != value:  # value was an integer; also add the string variant
            q |= Q(**{f"custom_field_data__librenms_id__{server_key}": str_val})
            q |= Q(custom_field_data__librenms_id=str_val)
    except (TypeError, ValueError):
        pass
    return q


class BaseCableTableView(LibreNMSPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """
    Base view for synchronizing cable information from LibreNMS.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_cable_sync_content.html"

    def get_object(self, pk):
        """Retrieve the object (Device or VirtualMachine)."""
        return get_object_or_404(self.model, pk=pk)

    def get_ip_address(self, obj):
        """Get the primary IP address for the object."""
        if obj.primary_ip:
            return str(obj.primary_ip.address.ip)
        return None

    def get_ports_data(self, obj):
        """Get ports data without affecting cache"""
        server_key = self.librenms_api.server_key
        cached_data = cache.get(self.get_cache_key(obj, "ports", server_key))
        if cached_data:
            return cached_data
        success, data = self.librenms_api.get_ports(self.librenms_id)
        if not success:
            return {"ports": []}
        return data

    def get_links_data(self, obj):
        """Fetch links data from LibreNMS for the device and add local port names."""
        self.librenms_id = self.librenms_api.get_librenms_id(obj)
        success, data = self.librenms_api.get_device_links(self.librenms_id)
        if not success or "error" in data:
            return None

        interface_name_field = get_interface_name_field(getattr(self, "request", None))
        ports_data = self.get_ports_data(obj)
        local_ports_map = {}
        for port in ports_data.get("ports", []):
            raw_port_id = port.get("port_id")
            if raw_port_id is None:
                continue
            port_id = str(raw_port_id)
            port_name = port.get(interface_name_field)
            if port_name is None:
                continue
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

    def get_device_by_id_or_name(self, remote_device_id, hostname, server_key=None):
        """Try to find device in NetBox first by librenms_id custom field, then by name"""
        if server_key is None:
            server_key = self.librenms_api.server_key
        # First try matching by LibreNMS ID
        if remote_device_id is not None:
            try:
                device = Device.objects.get(_librenms_id_q(server_key, remote_device_id))
                return device, True, None
            except Device.DoesNotExist:
                pass
            except MultipleObjectsReturned:
                return (
                    None,
                    False,
                    f"Multiple devices found with the same LibreNMS ID: {remote_device_id}.",
                )

        # Fall back to name matching if no device found by ID
        try:
            device = Device.objects.get(name=hostname)
            return device, True, None
        except Device.DoesNotExist:
            # Try without domain name
            simple_hostname = hostname.split(".")[0]
            try:
                device = Device.objects.get(name=simple_hostname)
                return device, True, None
            except Device.DoesNotExist:
                return None, False, None
            except MultipleObjectsReturned:
                return (
                    None,
                    False,
                    f"Multiple devices found with the same name: {hostname}.",
                )
        except MultipleObjectsReturned:
            return (
                None,
                False,
                f"Multiple devices found with the same name: {hostname}.",
            )

    def enrich_local_port(self, link, obj, server_key=None):
        """Add local port URL if interface exists in NetBox"""
        if local_port := link.get("local_port"):
            interface = None
            local_port_id = link.get("local_port_id")
            if server_key is None:
                server_key = self.librenms_api.server_key

            if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                chassis_member = get_virtual_chassis_member(obj, local_port)

                if chassis_member:
                    # First try to find interface by librenms_id
                    if local_port_id:
                        interface = chassis_member.interfaces.filter(_librenms_id_q(server_key, local_port_id)).first()

                    # Only if librenms_id match fails, try matching by name
                    if not interface:
                        interface = chassis_member.interfaces.filter(name=local_port).first()
            else:
                # First try to find interface by librenms_id
                if local_port_id:
                    interface = obj.interfaces.filter(_librenms_id_q(server_key, local_port_id)).first()

                # Only if librenms_id match fails, try matching by name
                if not interface:
                    interface = obj.interfaces.filter(name=local_port).first()

            if interface:
                link["local_port_url"] = reverse("dcim:interface", args=[interface.pk])
                link["netbox_local_interface_id"] = interface.pk

    def enrich_remote_port(self, link, device, server_key=None):
        """Add remote port URL if device and interface exist in NetBox"""
        if remote_port := link.get("remote_port"):
            netbox_remote_interface = None
            librenms_remote_port_id = link.get("remote_port_id")
            if server_key is None:
                server_key = self.librenms_api.server_key

            # Handle virtual chassis case
            if hasattr(device, "virtual_chassis") and device.virtual_chassis:
                # Get the appropriate chassis member based on the port name
                chassis_member = get_virtual_chassis_member(device, remote_port)

                if chassis_member:
                    # First try to find interface by librenms_id
                    if librenms_remote_port_id:
                        netbox_remote_interface = chassis_member.interfaces.filter(
                            _librenms_id_q(server_key, librenms_remote_port_id)
                        ).first()

                    # If not found by librenms_id, fall back to name matching on the correct chassis member
                    if not netbox_remote_interface:
                        netbox_remote_interface = chassis_member.interfaces.filter(name=remote_port).first()
            else:
                # Non-virtual chassis case
                # First try to find interface by librenms_id
                if librenms_remote_port_id:
                    netbox_remote_interface = device.interfaces.filter(
                        _librenms_id_q(server_key, librenms_remote_port_id)
                    ).first()

                # If not found by librenms_id, fall back to name matching
                if not netbox_remote_interface:
                    netbox_remote_interface = device.interfaces.filter(name=remote_port).first()

            if netbox_remote_interface:
                link["remote_port_url"] = reverse("dcim:interface", args=[netbox_remote_interface.pk])
                link["netbox_remote_interface_id"] = netbox_remote_interface.pk
                link["remote_port_name"] = netbox_remote_interface.name

            return link

    def check_cable_status(self, link):
        """Check cable status and add cable URL if cable exists in NetBox"""
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

    def process_remote_device(self, link, remote_hostname, remote_device_id, server_key=None):
        """Process remote device data and add remote device URL if device exists in NetBox"""
        device, found, error_message = self.get_device_by_id_or_name(
            remote_device_id, remote_hostname, server_key=server_key
        )
        if found:
            link.update(
                {
                    "remote_device_url": reverse("dcim:device", args=[device.pk]),
                    "netbox_remote_device_id": device.pk,
                }
            )
            return self.enrich_remote_port(link, device, server_key=server_key)

        link.update(
            {
                "remote_port_name": link["remote_port"],
                "cable_status": error_message if error_message else "Device Not Found in NetBox",
                "can_create_cable": False,
            }
        )
        return link

    def enrich_links_data(self, links_data, obj, server_key=None):
        """Enrich links data with local and remote port URLs and cable status."""
        for link in links_data:
            self.enrich_local_port(link, obj, server_key=server_key)
            link["device_id"] = obj.id

            if remote_hostname := link.get("remote_device"):
                link = self.process_remote_device(
                    link, remote_hostname, link.get("remote_device_id"), server_key=server_key
                )
                if link.get("netbox_remote_device_id"):
                    link = self.check_cable_status(link)

        return links_data

    def get_table(self, data, obj):
        """Get the table instance for the view."""
        table = super().get_table(data, obj)
        server_key = self.librenms_api.server_key
        table.htmx_url = f"{self.request.path}?tab=cables" + (f"&server_key={server_key}" if server_key else "")
        return table

    def _prepare_context(self, request, obj, fetch_fresh=False):
        """Helper method to prepare the context data for cable sync views."""
        table = None
        cache_expiry = None
        server_key = self.librenms_api.server_key
        # For VC devices, cache under the sync device's key so SingleCableVerifyView reads the same entry.
        cache_device = get_librenms_sync_device(obj, server_key=server_key) or obj

        if fetch_fresh:
            # Always fetch new data when requested
            links_data = self.get_links_data(obj)
            if not links_data:
                return None
        else:
            # Try to use cached data
            cached_links_data = cache.get(self.get_cache_key(cache_device, "links", server_key))
            if cached_links_data:
                links_data = cached_links_data.get("links", [])
            else:
                return None

        if not fetch_fresh:
            # Strip derived fields so re-enrichment starts from raw link data;
            # without this, stale IDs/URLs persist when NetBox objects are
            # deleted and cause DoesNotExist in check_cable_status().
            _raw_keys = {
                "local_port",
                "local_port_id",
                "remote_port",
                "remote_device",
                "remote_port_id",
                "remote_device_id",
            }
            links_data = [{k: v for k, v in link.items() if k in _raw_keys} for link in links_data]

        # Enrich data in both cases to ensure current NetBox state
        links_data = self.enrich_links_data(links_data, obj, server_key=server_key)

        # Cache after enrichment so verify/sync views read current NetBox state
        cache_key = self.get_cache_key(cache_device, "links", server_key)
        if fetch_fresh:
            cache.set(
                cache_key,
                {"links": links_data},
                timeout=self.librenms_api.cache_timeout,
            )
        else:
            # Write enriched data back, preserving original TTL
            remaining_ttl = cache.ttl(cache_key)
            if remaining_ttl and remaining_ttl > 0:
                cache.set(cache_key, {"links": links_data}, timeout=remaining_ttl)

        # Calculate cache expiry
        cache_ttl = cache.ttl(cache_key)
        if cache_ttl is not None and cache_ttl > 0:
            cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl)
        # Generate the table
        table = self.get_table(links_data, obj)

        table.configure(request)

        # Prepare and return the context
        return {
            "table": table,
            "object": obj,
            "cache_expiry": cache_expiry,
            "server_key": server_key,
        }

    def get_context_data(self, request, obj):
        """Get the context data for the cable sync view."""
        context = self._prepare_context(request, obj, fetch_fresh=False)
        if context is None:
            # No data found; return context with empty table
            context = {"table": None, "object": obj, "cache_expiry": None, "server_key": self.librenms_api.server_key}
        return context

    def post(self, request, pk):
        """Handle POST request for cable sync view."""
        obj = self.get_object(pk)
        context = self._prepare_context(request, obj, fetch_fresh=True)

        if context is None:
            messages.error(request, "No links found in LibreNMS")
            return render(
                request,
                self.partial_template_name,
                {
                    "cable_sync": {
                        "object": obj,
                        "table": None,
                        "cache_expiry": None,
                        "server_key": self.librenms_api.server_key,
                    }
                },
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
        local_port_id = data.get("local_port_id")
        # Read server_key from POST so we use the exact server the user was viewing
        server_key = data.get("server_key")
        if not server_key:
            server_key = self.librenms_api.server_key

        formatted_row = {
            "local_port": "",
            "remote_port": "",
            "remote_device": "",
            "cable_status": "Missing Ports",
            "actions": "",
        }

        if selected_device_id:
            selected_device = get_object_or_404(Device, pk=selected_device_id)

            # Use the same sync-device resolution as the GET path so the cache
            # key matches what _prepare_context wrote. When the VC has no
            # resolvable sync device, return an empty row rather than crashing.
            if selected_device.virtual_chassis:
                primary_device = get_librenms_sync_device(selected_device, server_key=server_key)
                if primary_device is None:
                    return JsonResponse({"status": "success", "formatted_row": formatted_row})
            else:
                primary_device = selected_device

            cached_links = cache.get(self.get_cache_key(primary_device, "links", server_key))

            if cached_links:
                link_data = next(
                    (
                        link
                        for link in cached_links.get("links", [])
                        if str(link.get("local_port_id", "")) == str(local_port_id)
                    ),
                    None,
                )
                if link_data:
                    # Strip derived fields from cached data to avoid stale
                    # IDs/URLs when NetBox objects are deleted after caching.
                    _raw_keys = {
                        "local_port",
                        "local_port_id",
                        "remote_port",
                        "remote_device",
                        "remote_port_id",
                        "remote_device_id",
                    }
                    link_data = {k: v for k, v in link_data.items() if k in _raw_keys}

                    # Re-enrich remote side from current NetBox state
                    remote_hostname = link_data.get("remote_device", "")
                    if remote_hostname:
                        link_data = self.process_remote_device(
                            link_data, remote_hostname, link_data.get("remote_device_id"), server_key=server_key
                        )

                    local_port = link_data.get("local_port", "")
                    formatted_row["local_port"] = local_port

                    # First try to find interface by librenms_id (handle VC members)
                    _sk = server_key
                    interface = None
                    lookup_device = selected_device
                    if local_port and hasattr(selected_device, "virtual_chassis") and selected_device.virtual_chassis:
                        chassis_member = get_virtual_chassis_member(selected_device, local_port)
                        if chassis_member:
                            lookup_device = chassis_member
                    if local_port_id:
                        interface = lookup_device.interfaces.filter(_librenms_id_q(_sk, local_port_id)).first()

                    # If not found by librenms_id, try matching by name
                    if not interface and local_port:
                        interface = lookup_device.interfaces.filter(name=local_port).first()

                    if interface:
                        link_data["netbox_local_interface_id"] = interface.pk

                        # Check cable status if remote side was resolved
                        if link_data.get("netbox_remote_device_id"):
                            link_data = self.check_cable_status(link_data)

                        # Escape LibreNMS-sourced labels to prevent XSS
                        safe_local_port = escape(local_port)
                        remote_port_name = link_data.get("remote_port_name", link_data.get("remote_port", ""))
                        safe_remote_port = escape(remote_port_name)
                        remote_device_name = link_data.get("remote_device", "")
                        safe_remote_device = escape(remote_device_name)
                        safe_cable_status = escape(link_data.get("cable_status", "Missing Ports"))

                        formatted_row["cable_status"] = safe_cable_status
                        formatted_row["local_port"] = (
                            f'<a href="{reverse("dcim:interface", args=[interface.pk])}">{safe_local_port}</a>'
                        )
                        formatted_row["remote_port"] = (
                            f'<a href="{link_data["remote_port_url"]}">{safe_remote_port}</a>'
                            if link_data.get("remote_port_url")
                            else safe_remote_port
                        )
                        formatted_row["remote_device"] = (
                            f'<a href="{link_data["remote_device_url"]}">{safe_remote_device}</a>'
                            if link_data.get("remote_device_url")
                            else safe_remote_device
                        )
                        if link_data.get("cable_url"):
                            formatted_row["cable_status"] = (
                                f'<a href="{link_data["cable_url"]}">{safe_cable_status}</a>'
                            )

                        if link_data.get("can_create_cable"):
                            csrf_token = get_token(request)
                            server_key_input = (
                                f'<input type="hidden" name="server_key" value="{escape(str(server_key))}">'
                                if server_key
                                else ""
                            )
                            formatted_row["actions"] = f"""
                                <form method="post" action="{reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[selected_device.id])}">
                                    <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
                                    <input type="hidden" name="select" value="{escape(str(local_port_id))}">
                                    {server_key_input}
                                    <button type="submit" class="btn btn-sm btn-primary">Sync Cable</button>
                                </form>
                            """
                    else:
                        formatted_row["local_port"] = escape(local_port)
                        # Keep remote port name visible, add URL if available
                        remote_port_name = link_data.get("remote_port_name", link_data.get("remote_port", ""))
                        safe_remote_port = escape(remote_port_name)
                        formatted_row["remote_port"] = (
                            f'<a href="{link_data["remote_port_url"]}">{safe_remote_port}</a>'
                            if link_data.get("remote_port_url")
                            else safe_remote_port
                        )
                        # Keep remote device name visible, add URL if available
                        remote_device_name = link_data.get("remote_device", "")
                        safe_remote_device = escape(remote_device_name)
                        formatted_row["remote_device"] = (
                            f'<a href="{link_data["remote_device_url"]}">{safe_remote_device}</a>'
                            if link_data.get("remote_device_url")
                            else safe_remote_device
                        )

                        # First check if remote device exists in NetBox
                        if remote_device_name and not link_data.get("remote_device_url"):
                            formatted_row["cable_status"] = "Device Not Found in NetBox"
                        # Then check interface status
                        elif link_data.get("remote_device_url") and link_data.get("remote_port_url"):
                            formatted_row["cable_status"] = "Local Interface Not Found in NetBox"
                        else:
                            formatted_row["cable_status"] = "Missing Interface"

                        formatted_row["actions"] = ""

        return JsonResponse({"status": "success", "formatted_row": formatted_row})
