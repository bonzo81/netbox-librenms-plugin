from django.urls import path
from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_librenms_plugin"

router = NetBoxRouter()
router.register("interface-type-mappings", views.InterfaceTypeMappingViewSet)
router.register("device-type-mappings", views.DeviceTypeMappingViewSet)
router.register("module-type-mappings", views.ModuleTypeMappingViewSet)
router.register("module-bay-mappings", views.ModuleBayMappingViewSet)
router.register("normalization-rules", views.NormalizationRuleViewSet)
router.register("inventory-ignore-rules", views.InventoryIgnoreRuleViewSet)
router.register("platform-mappings", views.PlatformMappingViewSet)

urlpatterns = [
    path("jobs/<int:job_pk>/sync-status/", views.sync_job_status, name="sync_job_status"),
] + router.urls
