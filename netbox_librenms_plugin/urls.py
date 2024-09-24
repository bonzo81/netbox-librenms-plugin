from django.urls import path, include
from .models import InterfaceTypeMapping
from . import views


urlpatterns = [
    path('device/<int:pk>/librenms-sync/', views.DeviceInterfacesSyncView.as_view(), name='device_interfaces_sync'),
    path('device/<int:device_id>/sync/selected/', views.SyncInterfacesView.as_view(), name='sync_selected_interfaces'),
    path('interface-type-mappings/', views.InterfaceTypeMappingListView.as_view(), name='interfacetypemapping_list'),
    path('interface-type-mappings/<int:pk>/', views.InterfaceTypeMappingView.as_view(), name='interfacetypemapping_detail'),
    path('interface-type-mappings/add/', views.InterfaceTypeMappingCreateView.as_view(), name='interfacetypemapping_add'),
    path('interface-type-mappings/<int:pk>/delete/', views.InterfaceTypeMappingDeleteView.as_view(), name='interfacetypemapping_delete'),
    path('interface-type-mappings/<int:pk>/edit/', views.InterfaceTypeMappingEditView.as_view(), name='interfacetypemapping_edit'),
    path('interface-type-mappings/<int:pk>/changelog/', views.InterfaceTypeMappingChangeLogView.as_view(), name='interfacetypemapping_changelog', kwargs={'model': InterfaceTypeMapping}),
    path('add-device-to-librenms/', views.add_device_to_librenms, name='add_device_to_librenms'),
    path('add-device-modal/<int:pk>/', views.add_device_modal, name='add_device_modal'),
    path('site-location-comparison/', views.SiteLocationSyncView.as_view(), name='site_location_sync'),
    path('create-librenms-location/<int:pk>/', views.SiteLocationSyncView.as_view(), name='create_librenms_location'),
    path('update-librenms-location/<int:pk>/', views.SiteLocationSyncView.as_view(), name='update_librenms_location'),
    path('api/', include('netbox_librenms_plugin.api.urls')),
]
