from netbox.plugins import PluginMenu, PluginMenuItem

from netbox_librenms_plugin.constants import PERM_VIEW_PLUGIN

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
        # One sidebar entry per group: the individual object lists cross-link through the
        # switcher tabs rendered on each list page (inc/_mapping_tabs.html and
        # inc/_rules_patterns_tabs.html), and every list page carries its own native
        # add/import controls — so per-model menu items (and their buttons) would only
        # duplicate what the pages already offer while crowding the sidebar.
        (
            "Mappings",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:interfacetypemapping_list",
                    link_text="Mappings",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
            ),
        ),
        (
            "Rules & Patterns",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:inventoryignorerule_list",
                    link_text="Rules & Patterns",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:portstacklagpattern_list",
                    link_text="Port Stack LAG Patterns",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:portstacklagpattern_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                            permissions=[PERM_CHANGE_PLUGIN],
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:portstacklagpattern_bulk_import",
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
