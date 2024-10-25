from netbox.plugins import PluginConfig


class LibreNMSSyncConfig(PluginConfig):
    name = "netbox_librenms_plugin"
    verbose_name = "NetBox Librenms Plugin"
    description = "Sync data from LibreNMS into NetBox"
    version = "0.2.6"
    base_url = "librenms_plugin"
    required_settings = []
    default_settings = {"enable_caching": True}


config = LibreNMSSyncConfig
