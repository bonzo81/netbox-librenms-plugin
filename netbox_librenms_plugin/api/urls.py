from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_librenms_plugin"

router = NetBoxRouter()
router.register("interface-type-mappings", views.InterfaceTypeMappingViewSet)

urlpatterns = router.urls
