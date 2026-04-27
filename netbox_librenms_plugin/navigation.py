from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN, PERM_VIEW_PLUGIN

menu = PluginMenu(
    label="LibreNMS",
    icon_class="mdi mdi-network",
    groups=(
        (
            "Import",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:librenms_import",
                    link_text="LibreNMS Import",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
            ),
        ),
        (
            "Status Check",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:site_location_sync",
                    link_text="Site & Location Sync",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:device_status_list",
                    link_text="Device Status",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:vm_status_list",
                    link_text="VM Status",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
            ),
        ),
        (
            "Mappings",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:interfacetypemapping_list",
                    link_text="Interface Mappings",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:interfacetypemapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:interfacetypemapping_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:devicetypemapping_list",
                    link_text="Device Type Mappings",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:devicetypemapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:devicetypemapping_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:moduletypemapping_list",
                    link_text="Module Type Mappings",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:moduletypemapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:moduletypemapping_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:modulebaymapping_list",
                    link_text="Module Bay Mappings",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:modulebaymapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:modulebaymapping_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:normalizationrule_list",
                    link_text="Normalization Rules",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:normalizationrule_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:normalizationrule_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:inventoryignorerule_list",
                    link_text="Inventory Ignore Rules",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:inventoryignorerule_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:inventoryignorerule_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:platformmapping_list",
                    link_text="Platform Mappings",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:platformmapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:platformmapping_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                    ),
                ),
            ),
        ),
        (
            "Settings",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:settings",
                    link_text="Plugin Settings",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
            ),
        ),
    ),
)
