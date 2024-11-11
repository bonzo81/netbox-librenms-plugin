from netbox.plugins import PluginConfig


class LibreNMSSyncConfig(PluginConfig):
    name = "netbox_librenms_plugin"
    verbose_name = "NetBox Librenms Plugin"
    description = "Netbox plugin to sync data between LibreNMS and Netbox."
    author = "Andy Norwood"
    version = "0.2.7"
    base_url = "librenms_plugin"
    min_version = '4.1.0'
    required_settings = ["librenms_url", "api_token"]
    default_settings = {"enable_caching": True, "verify_ssl": True}


config = LibreNMSSyncConfig
