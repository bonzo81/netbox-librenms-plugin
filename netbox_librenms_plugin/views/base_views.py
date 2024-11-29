from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse
from django.http import JsonResponse
from django.views import View
from netbox.views import generic
from dcim.models import Device

from netbox_librenms_plugin.forms import AddToLIbreSNMPV2, AddToLIbreSNMPV3
from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.tables import LibreNMSInterfaceTable, LibreNMSCableTable
from netbox_librenms_plugin.utils import get_virtual_chassis_member


class LibreNMSAPIMixin:
    """
    A mixin class that provides access to the LibreNMS API.

    This mixin initializes a LibreNMSAPI instance and provides a property
    to access it. It's designed to be used with other view classes that
    need to interact with the LibreNMS API.

    Attributes:
        _librenms_api (LibreNMSAPI): An instance of the LibreNMSAPI class.

    Properties:
        librenms_api (LibreNMSAPI): A property that returns the LibreNMSAPI instance,
                                    creating it if it doesn't exist.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._librenms_api = None

    @property
    def librenms_api(self):
        """
        Get or create an instance of LibreNMSAPI.

        This property ensures that only one instance of LibreNMSAPI is created
        and reused for subsequent calls.

        Returns:
            LibreNMSAPI: An instance of the LibreNMSAPI class.
        """
        if self._librenms_api is None:
            self._librenms_api = LibreNMSAPI()
        return self._librenms_api


class CacheMixin:
    """
    A mixin class that provides caching functionality.
    """

    def get_cache_key(self, obj, data_type="ports"):
        """
        Get the cache key for the object.

        Args:
            obj: The object to cache data for
            data_type: Type of data being cached ('ports' or 'links')
        """
        model_name = obj._meta.model_name
        return f"librenms_{data_type}_{model_name}_{obj.pk}"

    def get_last_fetched_key(self, obj, data_type="ports"):
        """
        Get the cache key for the last fetched time of the object.
        """
        model_name = obj._meta.model_name
        return f"librenms_{data_type}_last_fetched_{model_name}_{obj.pk}"


class BaseLibreNMSSyncView(LibreNMSAPIMixin, generic.ObjectListView):
    """
    Base view for LibreNMS sync information.
    """

    queryset = None  # Will be set in subclasses
    model = None  # Will be set in subclasses
    tab = None  # Will be set in subclasses
    template_name = "netbox_librenms_plugin/librenms_sync_base.html"

    def get(self, request, pk, context=None):
        """
        Handle GET request for the LibreNMS sync view.
        """
        obj = get_object_or_404(self.model, pk=pk)

        # Get librenms_id once at the start
        self.librenms_id = self.librenms_api.get_librenms_id(obj)

        context = self.get_context_data(request, obj)

        return render(request, self.template_name, context)

    def get_context_data(self, request, obj):
        context = {
            "object": obj,
            "tab": self.tab,
            "has_librenms_id": bool(self.librenms_id),
        }

        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            vc_master = obj.virtual_chassis.master
            if not vc_master or not vc_master.primary_ip:
                # If no master or master has no primary IP, find first member with primary IP
                vc_master = next(
                    (
                        member
                        for member in obj.virtual_chassis.members.all()
                        if member.primary_ip
                    ),
                    None,
                )

            context.update(
                {
                    "is_vc_member": True,
                    "has_vc_primary_ip": bool(
                        vc_master.primary_ip if vc_master else False
                    ),
                    "vc_primary_device": vc_master,
                }
            )

        librenms_info = self.get_librenms_device_info(obj)

        interface_context = self.get_interface_context(request, obj)
        cable_context = self.get_cable_context(request, obj)
        ip_context = self.get_ip_context(request, obj)

        context.update(
            {
                "interface_sync": interface_context,
                "cable_sync": cable_context,
                "ip_sync": ip_context,
                "v2form": AddToLIbreSNMPV2(),
                "v3form": AddToLIbreSNMPV3(),
                "librenms_device_id": self.librenms_id,
                "found_in_librenms": librenms_info.get("found_in_librenms"),
                "librenms_device_details": librenms_info.get("librenms_device_details"),
                "mismatched_device": librenms_info.get("mismatched_device"),
                **librenms_info["librenms_device_details"],
            }
        )

        return context

    def get_librenms_device_info(self, obj):
        found_in_librenms = False
        mismatched_device = False
        librenms_device_details = {
            "librenms_device_url": None,
            "librenms_device_hardware": "-",
            "librenms_device_location": "-",
        }

        if self.librenms_id:
            success, device_info = self.librenms_api.get_device_info(self.librenms_id)
            if success and device_info:
                # Get NetBox device details
                netbox_ip = str(obj.primary_ip.address.ip) if obj.primary_ip else None
                netbox_hostname = obj.name

                # Get LibreNMS device details
                librenms_hostname = device_info.get("sysName")
                librenms_ip = device_info.get("ip")

                # Update device details regardless of match
                librenms_device_details.update(
                    {
                        "librenms_device_url": f"{self.librenms_api.librenms_url}/device/device={self.librenms_id}/",
                        "librenms_device_hardware": device_info.get("hardware", "-"),
                        "librenms_device_location": device_info.get("location", "-"),
                        "librenms_device_ip": librenms_ip,
                        "sysName": librenms_hostname,
                    }
                )

                # Get just the hostname part from LibreNMS FQDN if present
                librenms_host = (
                    librenms_hostname.split(".")[0] if librenms_hostname else None
                )
                netbox_host = netbox_hostname.split(".")[0] if netbox_hostname else None

                # Check for matching IP or hostname
                if (netbox_ip == librenms_ip) or (netbox_host == librenms_host):
                    found_in_librenms = True
                else:
                    mismatched_device = True

        return {
            "found_in_librenms": found_in_librenms,
            "librenms_device_details": librenms_device_details,
            "mismatched_device": mismatched_device,
        }

    def get_interface_context(self, request, obj):
        """
        Get the context data for interface sync.
        Subclasses should override this method.
        """
        return None

    def get_cable_context(self, request, obj):
        """
        Get the context data for cable sync.
        Subclasses should override this method if applicable.
        """
        return None

    def get_ip_context(self, request, obj):
        """
        Get the context data for IP address sync.
        Subclasses should override this method.
        """
        return None


class BaseInterfaceTableView(LibreNMSAPIMixin, CacheMixin, View):
    """
    Base view for fetching interface data from LibreNMS and generating table data.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_interface_sync_content.html"

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

    def get_table(self, data, obj):
        """
        Returns the table class to use for rendering interface data.
        Can be overridden by subclasses to use different tables.
        """
        return LibreNMSInterfaceTable(data)

    def post(self, request, pk):
        """
        Handle POST request to fetch and cache LibreNMS interface data for an object.
        """
        obj = self.get_object(pk)

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

        context = self.get_context_data(request, obj)
        context = {"interface_sync": context}

        return render(request, self.partial_template_name, context)

    def get_context_data(self, request, obj):
        """
        Get the context data for the interface sync view.
        """
        ports_data = []

        table = None

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
                    chassis_member = get_virtual_chassis_member(obj, port["ifDescr"])
                    netbox_interfaces = self.get_interfaces(chassis_member)

                else:
                    chassis_member = obj  # Not part of a virtual chassis

                netbox_interface = netbox_interfaces.filter(
                    name=port["ifDescr"]
                ).first()
                port["exists_in_netbox"] = bool(netbox_interface)
                port["netbox_interface"] = netbox_interface

                # Ignore when description is the same as interface name
                if port["ifAlias"] == port["ifDescr"]:
                    port["ifAlias"] = ""

            table = self.get_table(ports_data, obj)

            table.configure(request)

        virtual_chassis_members = []
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            virtual_chassis_members = obj.virtual_chassis.members.all()

        cache_ttl = cache.ttl(self.get_cache_key(obj, "ports"))
        if cache_ttl is not None:
            cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl)
        else:
            cache_expiry = None

        return {
            "object": obj,
            "table": table,
            "last_fetched": last_fetched,
            "cache_expiry": cache_expiry,
            "virtual_chassis_members": virtual_chassis_members,
        }


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


class BaseIPAddressTableView(LibreNMSAPIMixin, View):
    """
    Base view for synchronizing IP address information from LibreNMS.
    """

    template_name = "netbox_librenms_plugin/_ipaddress_sync.html"

    def get_context_data(self, request, device):
        """
        Get context data for IP address sync view.
        """
        context = {
            "device": device,
            "ip_sync_message": "IP address sync coming soon",
        }
        return context
