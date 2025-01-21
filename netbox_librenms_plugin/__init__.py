from netbox.plugins import PluginConfig

__author__ = "Andy Norwood"
__version__ = "0.3.6"


class LibreNMSSyncConfig(PluginConfig):
    name = "netbox_librenms_plugin"
    verbose_name = "NetBox Librenms Plugin"
    description = "Netbox plugin to sync data between LibreNMS and Netbox."
    author = __author__
    version = __version__
    base_url = "librenms_plugin"
    min_version = "4.2.0"
    required_settings = ["librenms_url", "api_token"]
    default_settings = {
        "enable_caching": True,
        "verify_ssl": True,
        "interface_name_field": "ifName",
    }


config = LibreNMSSyncConfig
