from django.urls import path
from netbox.api.routers import NetBoxRouter

from . import views
from ..utils import slashless_route_aliases

app_name = "netbox_librenms_plugin"

router = NetBoxRouter()
router.register("interface-type-mappings", views.InterfaceTypeMappingViewSet)
router.register("device-type-mappings", views.DeviceTypeMappingViewSet)
router.register("module-type-mappings", views.ModuleTypeMappingViewSet)
router.register("module-bay-mappings", views.ModuleBayMappingViewSet)
router.register("normalization-rules", views.NormalizationRuleViewSet)
router.register("inventory-ignore-rules", views.InventoryIgnoreRuleViewSet)
router.register("platform-mappings", views.PlatformMappingViewSet)
router.register("location-mappings", views.LocationMappingViewSet)
router.register("carrier-auto-install-rules", views.CarrierAutoInstallRuleViewSet)
router.register("port-stack-lag-patterns", views.PortStackLagPatternViewSet)
router.register("serial-sensor-type-patterns", views.SerialSensorTypePatternViewSet)

urlpatterns = [
    path("jobs/<int:job_pk>/sync-status/", views.sync_job_status, name="sync_job_status"),
]
# The import page posts here, so a proxy that drops the trailing slash would redirect the POST
# and lose its body. The router's own routes are left alone; API clients follow redirects.
urlpatterns += slashless_route_aliases(urlpatterns)
urlpatterns += router.urls
