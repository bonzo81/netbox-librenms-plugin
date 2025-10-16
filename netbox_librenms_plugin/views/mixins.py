from netbox_librenms_plugin.librenms_api import LibreNMSAPI


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

            if (
                servers_config
                and isinstance(servers_config, dict)
                and server_key in servers_config
            ):
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
