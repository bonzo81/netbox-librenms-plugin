from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.utils import (
    get_interface_name_field,
    get_virtual_chassis_member,
)
from netbox_librenms_plugin.views.mixins import CacheMixin, LibreNMSAPIMixin


class BaseInterfaceTableView(LibreNMSAPIMixin, CacheMixin, View):
    """
    Base view for fetching interface data from LibreNMS and generating table data.
    """

    tab = "interfaces"
    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_interface_sync_content.html"
    interface_name_field = None

    def get_object(self, pk):
        return get_object_or_404(self.model, pk=pk)

    def get_ip_address(self, obj):
        if obj.primary_ip:
            return str(obj.primary_ip.address.ip)
        return None

    def get_interfaces(self, obj):
        """
        Get interfaces related to the object.
        Should be implemented in subclasses.
        """
        raise NotImplementedError

    def get_redirect_url(self, obj):
        """
        Get the redirect URL for the object.
        Should be implemented in subclasses.
        """
        raise NotImplementedError

    def get_table(self, data, obj, interface_name_field):
        """
        Returns the table class to use for rendering interface data.
        Can be overridden by subclasses to use different tables.
        """
        raise NotImplementedError("Subclasses must implement get_table()")

    def post(self, request, pk):
        """
        Handle POST request to fetch and cache LibreNMS interface data for an object.
        """
        obj = self.get_object(pk)

        interface_name_field = get_interface_name_field(request)

        # Get librenms_id at the start
        self.librenms_id = self.librenms_api.get_librenms_id(obj)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS.")
            return redirect(self.get_redirect_url(obj))

        librenms_data = self.librenms_api.get_ports(self.librenms_id)

        if "error" in librenms_data:
            messages.error(request, librenms_data["error"])
            return redirect(self.get_redirect_url(obj))

        # Store data in cache
        cache.set(
            self.get_cache_key(obj, "ports"),
            librenms_data,
            timeout=self.librenms_api.cache_timeout,
        )
        last_fetched = timezone.now()
        cache.set(
            self.get_last_fetched_key(obj, "ports"),
            last_fetched,
            timeout=self.librenms_api.cache_timeout,
        )

        messages.success(request, "Interface data refreshed successfully.")

        context = self.get_context_data(request, obj, interface_name_field)
        context = {"interface_sync": context}
        context["interface_name_field"] = interface_name_field

        return render(request, self.partial_template_name, context)

    def get_context_data(self, request, obj, interface_name_field):
        """
        Get the context data for the interface sync view.
        """
        ports_data = []

        table = None

        if interface_name_field is None:
            interface_name_field = get_interface_name_field(request)

        cached_data = cache.get(self.get_cache_key(obj, "ports"))

        last_fetched = cache.get(self.get_last_fetched_key(obj), "ports")

        if cached_data:
            ports_data = cached_data.get("ports", [])
            netbox_interfaces = self.get_interfaces(obj)

            for port in ports_data:
                port["enabled"] = (
                    True
                    if port["ifAdminStatus"] is None
                    else (
                        port["ifAdminStatus"].lower() == "up"
                        if isinstance(port["ifAdminStatus"], str)
                        else bool(port["ifAdminStatus"])
                    )
                )

                # Determine the correct chassis member based on the port description
                if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                    chassis_member = get_virtual_chassis_member(
                        obj, port[interface_name_field]
                    )
                    netbox_interfaces = self.get_interfaces(chassis_member)

                else:
                    chassis_member = obj  # Not part of a virtual chassis

                netbox_interface = netbox_interfaces.filter(
                    name=port[interface_name_field]
                ).first()
                port["exists_in_netbox"] = bool(netbox_interface)
                port["netbox_interface"] = netbox_interface

                # Ignore when description is the same as interface name
                if (
                    port["ifAlias"] == port["ifDescr"]
                    or port["ifAlias"] == port["ifName"]
                ):
                    port["ifAlias"] = ""

            table = self.get_table(ports_data, obj, interface_name_field)

            table.configure(request)

        virtual_chassis_members = []
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            virtual_chassis_members = obj.virtual_chassis.members.all()

        cache_ttl = cache.ttl(self.get_cache_key(obj, "ports"))
        if cache_ttl is not None:
            cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl)
        else:
            cache_expiry = None

        context = {
            "object": obj,
            "table": table,
            "tab": self.tab,
            "last_fetched": last_fetched,
            "cache_expiry": cache_expiry,
            "virtual_chassis_members": virtual_chassis_members,
            "interface_name_field": interface_name_field,
        }

        return context
