from django.urls import path
from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_librenms_plugin"

router = NetBoxRouter()
router.register("interface-type-mappings", views.InterfaceTypeMappingViewSet)

urlpatterns = [
    path(
        "jobs/<int:job_pk>/sync-status/", views.sync_job_status, name="sync_job_status"
    ),
] + router.urls
