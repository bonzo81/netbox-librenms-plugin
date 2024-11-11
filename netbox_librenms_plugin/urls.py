from django.urls import include, path

from .models import InterfaceTypeMapping
from .views import (AddDeviceToLibreNMSView, DeviceCableTableView,
                    DeviceInterfaceTableView, DeviceIPAddressTableView,
                    DeviceLibreNMSSyncView, SingleInterfaceVerifyView,
                    InterfaceTypeMappingBulkDeleteView,
                    InterfaceTypeMappingChangeLogView,
                    InterfaceTypeMappingCreateView,
                    InterfaceTypeMappingDeleteView,
                    InterfaceTypeMappingEditView, InterfaceTypeMappingListView,
                    InterfaceTypeMappingView, SyncInterfacesView,
                    SyncSiteLocationView, UpdateDeviceLocationView,
                    VMInterfaceTableView, VMIPAddressTableView,
                    VMLibreNMSSyncView)

urlpatterns = [
    # Device sync URLs
    path(
        "device/<int:pk>/librenms-sync/",
        DeviceLibreNMSSyncView.as_view(),
        name="device_librenms_sync",
    ),
    path(
        "devices/<int:pk>/interface-sync/",
        DeviceInterfaceTableView.as_view(),
        name="device_interface_sync",
    ),
    path(
        "devices/<int:pk>/cable-sync/",
        DeviceCableTableView.as_view(),
        name="device_cable_sync",
    ),
    path(
        "devices/<int:pk>/ipaddress-sync/",
        DeviceIPAddressTableView.as_view(),
        name="device_ipaddress_sync",
    ),
    # Path for single interface verify javascript call
    path(
        "verify-interface/",
        SingleInterfaceVerifyView.as_view(),
        name="verify_interface",
    ),
    # Virtual machine sync URLs
    path(
        "virtual-machines/<int:pk>/interface-sync/",
        VMInterfaceTableView.as_view(),
        name="vm_interface_sync",
    ),
    path(
        "virtual-machines/<int:pk>/ipaddress-sync/",
        VMIPAddressTableView.as_view(),
        name="vm_ipaddress_sync",
    ),
    path(
        "virtual-machines/<int:pk>/librenms-sync/",
        VMLibreNMSSyncView.as_view(),
        name="vm_librenms_sync",
    ),
    # Sync interfaces URLs
    path(
        "<str:object_type>/<int:object_id>/sync-interfaces/",
        SyncInterfacesView.as_view(),
        name="sync_selected_interfaces",
    ),
    # Add Device to LibreNMS URLs
    path(
        "<str:object_type>/<int:object_id>/add-device-to-librenms/",
        AddDeviceToLibreNMSView.as_view(),
        name="add_device_to_librenms",
    ),
    # Site > location sync URLs
    path(
        "site-location-comparison/",
        SyncSiteLocationView.as_view(),
        name="site_location_sync",
    ),
    path(
        "create-librenms-location/<int:pk>/",
        SyncSiteLocationView.as_view(),
        name="create_librenms_location",
    ),
    path(
        "update-librenms-location/<int:pk>/",
        SyncSiteLocationView.as_view(),
        name="update_librenms_location",
    ),
    # Update device location URLs
    path(
        "devices/<int:pk>/update-location/",
        UpdateDeviceLocationView.as_view(),
        name="update_device_location",
    ),
    # Interface type mapping URLs
    path(
        "interface-type-mappings/",
        InterfaceTypeMappingListView.as_view(),
        name="interfacetypemapping_list",
    ),
    path(
        "interface-type-mappings/<int:pk>/",
        InterfaceTypeMappingView.as_view(),
        name="interfacetypemapping_detail",
    ),
    path(
        "interface-type-mappings/add/",
        InterfaceTypeMappingCreateView.as_view(),
        name="interfacetypemapping_add",
    ),
    path(
        "interface-type-mappings/<int:pk>/delete/",
        InterfaceTypeMappingDeleteView.as_view(),
        name="interfacetypemapping_delete",
    ),
    path(
        "interface-type-mappings/<int:pk>/edit/",
        InterfaceTypeMappingEditView.as_view(),
        name="interfacetypemapping_edit",
    ),
    path(
        "interface-type-mappings/<int:pk>/changelog/",
        InterfaceTypeMappingChangeLogView.as_view(),
        name="interfacetypemapping_changelog",
        kwargs={"model": InterfaceTypeMapping},
    ),
    path(
        "interface-type-mappings/delete/",
        InterfaceTypeMappingBulkDeleteView.as_view(),
        name="interfacetypemapping_bulk_delete",
    ),
    path("api/", include("netbox_librenms_plugin.api.urls")),
]
