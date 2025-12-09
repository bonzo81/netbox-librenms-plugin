from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label="LibreNMS Plugin",  # This will be your main menu heading
    icon_class="mdi mdi-network",
    groups=(
        (
            "Settings",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:settings",
                    link_text="Plugin Settings",
                    permissions=["netbox_librenms_plugin.view_librenmssettings"],
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:interfacetypemapping_list",
                    link_text="Interface Mappings",
                    permissions=["netbox_librenms_plugin.view_interfacetypemapping"],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:interfacetypemapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:interfacetypemapping_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                        ),
                    ),
                ),
            ),
        ),
        (
            "Import",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:librenms_import",
                    link_text="LibreNMS Import",
                    permissions=["dcim.view_device"],
                ),
            ),
        ),
        (
            "Status Check",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:site_location_sync",
                    link_text="Site & Location Sync",
                    permissions=["dcim.view_site"],
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:device_status_list",
                    link_text="Device Status",
                    permissions=["dcim.view_device"],
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:vm_status_list",
                    link_text="VM Status",
                    permissions=["virtualization.view_virtualmachine"],
                ),
            ),
        ),
    ),
)
