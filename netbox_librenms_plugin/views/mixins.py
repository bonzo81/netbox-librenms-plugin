from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import HttpResponse
from django.shortcuts import redirect
from utilities.permissions import get_permission_for_model

from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN, PERM_VIEW_PLUGIN
from netbox_librenms_plugin.librenms_api import LibreNMSAPI


class LibreNMSPermissionMixin(PermissionRequiredMixin):
    """
    Mixin for views requiring LibreNMS plugin permissions.

    All plugin views require 'view_librenmssettings' to access the page.
    Write actions require 'change_librenmssettings' plus any relevant
    NetBox object permissions.
    """

    permission_required = PERM_VIEW_PLUGIN

    def has_write_permission(self):
        """Check if user can perform write actions."""
        return self.request.user.has_perm(PERM_CHANGE_PLUGIN)

    def require_write_permission(self, error_message=None):
        """
        Check write permission and return error response if denied.

        Handles both HTMX and regular requests appropriately:
        - HTMX: Returns HX-Redirect to referrer with toast message
        - Regular: Returns redirect to referrer with flash message

        Returns:
            None if permitted, or appropriate response if denied
        """
        if not self.has_write_permission():
            msg = error_message or "You do not have permission to perform this action."
            messages.error(self.request, msg)

            # Get the referrer URL, fallback to a safe default
            referrer = self.request.META.get("HTTP_REFERER", "/")

            # Check if this is an HTMX request
            if self.request.headers.get("HX-Request"):
                return HttpResponse("", headers={"HX-Redirect": referrer})

            return redirect(referrer)
        return None

    def require_write_permission_json(self, error_message=None):
        """
        Check write permission and return JSON error response if denied.

        Use this method for AJAX/HTMX endpoints that return JsonResponse.
        Does not set flash messages since JSON clients handle errors differently.

        Returns:
            None if permitted, or JsonResponse with 403 status if denied
        """
        from django.http import JsonResponse

        if not self.has_write_permission():
            msg = error_message or "You do not have permission to perform this action."
            return JsonResponse({"error": msg}, status=403)
        return None


class NetBoxObjectPermissionMixin:
    """
    Mixin for views requiring specific NetBox object permissions.

    Define required_object_permissions as a dict mapping HTTP methods
    to lists of (action, model) tuples.

    Example:
        required_object_permissions = {
            'POST': [
                ('add', Interface),
                ('change', Interface),
            ],
        }
    """

    required_object_permissions = {}

    def check_object_permissions(self, method):
        """
        Check all required object permissions for the given HTTP method.

        Args:
            method: HTTP method (GET, POST, etc.)

        Returns:
            tuple: (has_all: bool, missing: list[str])
        """
        requirements = self.required_object_permissions.get(method, [])
        missing = []

        for action, model in requirements:
            perm = get_permission_for_model(model, action)
            if not self.request.user.has_perm(perm):
                missing.append(perm)

        return (len(missing) == 0, missing)

    def require_object_permissions(self, method):
        """
        Require all object permissions for the method, returning error response if denied.

        Handles both HTMX and regular requests appropriately:
        - HTMX: Returns HX-Redirect to referrer with flash message
        - Regular: Returns redirect to referrer with flash message

        Returns:
            None if permitted, or appropriate response if denied
        """
        has_perms, missing = self.check_object_permissions(method)
        if not has_perms:
            missing_str = ", ".join(missing)
            msg = f"Missing permissions: {missing_str}"
            messages.error(self.request, msg)

            # Get the referrer URL, fallback to a safe default
            referrer = self.request.META.get("HTTP_REFERER", "/")

            # Check if this is an HTMX request
            if self.request.headers.get("HX-Request"):
                return HttpResponse("", headers={"HX-Redirect": referrer})

            return redirect(referrer)
        return None

    def require_object_permissions_json(self, method):
        """
        Require all object permissions for the method, returning JSON error if denied.

        Use this method for AJAX/HTMX endpoints that return JsonResponse.
        Does not set flash messages since JSON clients handle errors differently.

        Returns:
            None if permitted, or JsonResponse with 403 status if denied
        """
        from django.http import JsonResponse

        has_perms, missing = self.check_object_permissions(method)
        if not has_perms:
            missing_str = ", ".join(missing)
            return JsonResponse({"error": f"Missing permissions: {missing_str}"}, status=403)
        return None

    def require_all_permissions(self, method="POST"):
        """
        Check both plugin write and NetBox object permissions.

        Combines require_write_permission() and require_object_permissions()
        into a single call. Handles HTMX and regular requests.

        Returns:
            None if permitted, or appropriate error response if denied
        """
        if error := self.require_write_permission():
            return error
        return self.require_object_permissions(method)

    def require_all_permissions_json(self, method="POST"):
        """
        Check both plugin write and NetBox object permissions, returning JSON errors.

        Combines require_write_permission_json() and require_object_permissions_json()
        into a single call for JSON/AJAX endpoints.

        Returns:
            None if permitted, or JsonResponse with 403 status if denied
        """
        if error := self.require_write_permission_json():
            return error
        return self.require_object_permissions_json(method)


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
        and reused for subsequent calls. The API instance will use the currently
        selected server from settings.

        Returns:
            LibreNMSAPI: An instance of the LibreNMSAPI class.
        """
        if self._librenms_api is None:
            # The LibreNMSAPI will automatically use the selected server
            self._librenms_api = LibreNMSAPI()
        return self._librenms_api

    def get_server_info(self):
        """
        Get information about the currently active LibreNMS server.

        Returns:
            dict: Server information including display name and URL
        """
        try:
            # Get the current server key
            server_key = self.librenms_api.server_key

            # Try to get multi-server configuration
            from netbox.plugins import get_plugin_config

            servers_config = get_plugin_config("netbox_librenms_plugin", "servers")

            if servers_config and isinstance(servers_config, dict) and server_key in servers_config:
                # Multi-server configuration
                config = servers_config[server_key]
                return {
                    "display_name": config.get("display_name", server_key),
                    "url": config["librenms_url"],
                    "is_legacy": False,
                    "server_key": server_key,
                }
            else:
                # Legacy configuration
                legacy_url = get_plugin_config("netbox_librenms_plugin", "librenms_url")
                return {
                    "display_name": "Default Server",
                    "url": legacy_url or "Not configured",
                    "is_legacy": True,
                    "server_key": "default",
                }
        except (KeyError, AttributeError, ImportError):
            return {
                "display_name": "Unknown Server",
                "url": "Configuration error",
                "is_legacy": True,
                "server_key": "unknown",
            }

    def get_context_data(self, **kwargs):
        """Add server info to context for all views using this mixin."""
        try:
            context = super().get_context_data(**kwargs)
        except AttributeError:
            context = kwargs
        context["librenms_server_info"] = self.get_server_info()
        return context


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
