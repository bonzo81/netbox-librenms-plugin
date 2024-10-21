from netbox.plugins import PluginConfig
from netbox.plugins import get_plugin_config


class LibreNMSSyncConfig(PluginConfig):
    name = "netbox_librenms_plugin"
    verbose_name = "NetBox Librenms Plugin"
    description = "Sync data from LibreNMS into NetBox"
    version = "0.2.5"
    base_url = "librenms_plugin"
    required_settings = []
    default_settings = {
        'enable_caching': True
    }


config = LibreNMSSyncConfig
