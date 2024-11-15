from netbox.choices import ButtonColorChoices
from netbox.plugins import PluginMenuButton, PluginMenuItem

menu_items = (
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
)
