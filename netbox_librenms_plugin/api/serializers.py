from netbox.api.serializers import NetBoxModelSerializer

from netbox_librenms_plugin.models import InterfaceTypeMapping


class InterfaceTypeMappingSerializer(NetBoxModelSerializer):
    class Meta:
        model = InterfaceTypeMapping
        fields = ["id", "librenms_type", "librenms_speed", "netbox_type", "description"]
