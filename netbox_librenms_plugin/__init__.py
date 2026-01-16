from django.core.exceptions import ImproperlyConfigured
from netbox.plugins import PluginConfig

__author__ = "Andy Norwood"
__version__ = "0.4.2"


class LibreNMSSyncConfig(PluginConfig):
    name = "netbox_librenms_plugin"
    verbose_name = "NetBox Librenms Plugin"
    description = "Netbox plugin to sync data between LibreNMS and Netbox."
    author = __author__
    version = __version__
    base_url = "librenms_plugin"
    min_version = "4.2.0"
    required_settings = []  # Custom validation in ready() method
    default_settings = {
        "enable_caching": True,
        "verify_ssl": True,
        "interface_name_field": "ifName",
    }

    def ready(self):
        """
        Perform custom validation for plugin configuration.
        Supports both legacy single-server and new multi-server configurations.
        """
        super().ready()

        from django.conf import settings

        plugin_config = getattr(settings, "PLUGINS_CONFIG", {}).get(self.name, {})

        # Check if using new multi-server configuration
        if "servers" in plugin_config:
            self._validate_multi_server_config(plugin_config["servers"])
        else:
            self._validate_legacy_config(plugin_config)

    def _validate_multi_server_config(self, servers_config):
        """Validate multi-server configuration."""
        if not servers_config or not isinstance(servers_config, dict):
            raise ImproperlyConfigured(
                f"Plugin {self.name} requires at least one server configuration "
                "in the 'servers' section."
            )

        for server_key, server_config in servers_config.items():
            if not isinstance(server_config, dict):
                raise ImproperlyConfigured(
                    f"Plugin {self.name} server '{server_key}' must be a dictionary."
                )

            for setting in ["librenms_url", "api_token"]:
                if setting not in server_config:
                    raise ImproperlyConfigured(
                        f"Plugin {self.name} server '{server_key}' requires '{setting}'."
                    )

    def _validate_legacy_config(self, plugin_config):
        """Validate legacy single-server configuration."""
        for setting in ["librenms_url", "api_token"]:
            if setting not in plugin_config:
                raise ImproperlyConfigured(
                    f"Plugin {self.name} requires either 'servers' configuration "
                    f"or legacy '{setting}' setting."
                )


config = LibreNMSSyncConfig
