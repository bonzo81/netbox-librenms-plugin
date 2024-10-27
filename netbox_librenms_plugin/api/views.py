from netbox.api.viewsets import NetBoxModelViewSet

from netbox_librenms_plugin.models import InterfaceTypeMapping

from .serializers import InterfaceTypeMappingSerializer


class InterfaceTypeMappingViewSet(NetBoxModelViewSet):
    queryset = InterfaceTypeMapping.objects.all()
    serializer_class = InterfaceTypeMappingSerializer
