from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View
from netbox.views import generic

from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.tables import LibreNMSInterfaceTable
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

    cache_prefix = "librenms_ports"

    def get_cache_key(self, obj):
        """
        Get the cache key for the object.
        """
        model_name = obj._meta.model_name
        return f"{self.cache_prefix}_{model_name}_{obj.pk}"

    def get_last_fetched_key(self, obj):
        """
        Get the cache key for the last fetched time of the object.
        """
        model_name = obj._meta.model_name
        return f"{self.cache_prefix}_last_fetched_{model_name}_{obj.pk}"


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
        context = self.get_context_data(request, obj)
        return render(request, self.template_name, context)

    def get_context_data(self, request, obj):
        context = {
            "object": obj,
            "tab": self.tab,
            "has_primary_ip": bool(obj.primary_ip),
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
                "object_in_librenms": librenms_info["obj_exists"],
                "interface_sync": interface_context,
                "cable_sync": cable_context,
                "ip_sync": ip_context,
                **librenms_info["details"],
            }
        )

        return context

    def get_librenms_device_info(self, obj):
        obj_exists = False
        details = {
            "librenms_device_id": None,
            "librenms_device_url": None,
            "librenms_device_hardware": "-",
            "librenms_device_location": "-",
        }

        if obj.primary_ip:
            obj_exists, librenms_obj_data = self.librenms_api.get_device_info(
                str(obj.primary_ip.address.ip)
            )
            if obj_exists and librenms_obj_data:
                details.update(
                    {
                        "librenms_device_id": librenms_obj_data.get("device_id"),
                        "librenms_device_hardware": librenms_obj_data.get("hardware"),
                        "librenms_device_location": librenms_obj_data.get("location"),
                    }
                )
                details["librenms_device_url"] = (
                    f"{self.librenms_api.librenms_url}/device/device="
                    f"{details['librenms_device_id']}/"
                )
        return {"obj_exists": obj_exists, "details": details}

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

        ip_address = self.get_ip_address(obj)
        if not ip_address:
            messages.error(
                request,
                "This object has no primary IP set. Unable to fetch data from LibreNMS.",
            )
            return redirect(self.get_redirect_url(obj))

        librenms_data = self.librenms_api.get_ports(ip_address)

        if "error" in librenms_data:
            messages.error(request, librenms_data["error"])
            return redirect(self.get_redirect_url(obj))

        # Store data in cache
        cache.set(
            self.get_cache_key(obj),
            librenms_data,
            timeout=self.librenms_api.cache_timeout,
        )
        last_fetched = timezone.now()
        cache.set(
            self.get_last_fetched_key(obj),
            last_fetched,
            timeout=self.librenms_api.cache_timeout,
        )

        messages.success(request, "Data refreshed successfully.")

        context = self.get_context_data(request, obj)
        context = {"interface_sync": context}

        return render(request, self.partial_template_name, context)

    def get_context_data(self, request, obj):
        """
        Get the context data for the interface sync view.
        """
        ports_data = []
        table = None

        cached_data = cache.get(self.get_cache_key(obj))

        last_fetched = cache.get(self.get_last_fetched_key(obj))

        if cached_data:
            ports_data = cached_data.get("ports", [])
            netbox_interfaces = self.get_interfaces(obj)

            for port in ports_data:
                port["enabled"] = (
                    port["ifAdminStatus"].lower() == "up"
                    if isinstance(port["ifAdminStatus"], str)
                    else bool(port["ifAdminStatus"])
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

        cache_ttl = cache.ttl(self.get_cache_key(obj))
        if cache_ttl is not None:
            cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl)
        else:
            cache_expiry = None

        return {
            "object": obj,
            "table": table,
            "last_fetched": last_fetched,
            "cache_expiry": cache_expiry,
        }


class BaseCableTableView(LibreNMSAPIMixin, View):
    """
    Base view for synchronizing cable information from LibreNMS.
    """

    template_name = "netbox_librenms_plugin/_cable_sync.html"

    def get_context_data(self, request, device):
        """
        Get context data for cable sync view.
        """
        context = {
            "device": device,
            "cable_sync_message": "Cable sync coming soon",
        }
        return context


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
