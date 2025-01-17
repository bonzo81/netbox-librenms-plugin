from netbox.choices import ButtonColorChoices
from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label="LibreNMS Plugin",  # This will be your main menu heading
    groups=(
        (
            "Management",
            (  # This is a group label
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:interfacetypemapping_list",
                    link_text="Interface Mappings",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:interfacetypemapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                            color=ButtonColorChoices.GREEN,
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:site_location_sync",
                    link_text="Site & Location Sync",
                    permissions=["dcim.view_site"],
                ),
                PluginMenuItem(
                link='plugins:netbox_librenms_plugin:device_status_list',
                link_text='Device Status',
                permissions=['dcim.view_device']
                ),
            ),
        ),
    ),
    icon_class="mdi mdi-network",  # You can choose an appropriate icon from Material Design Icons
)
