from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from netbox_librenms_plugin.models import InterfaceTypeMapping


class InterfaceTypeMappingSerializer(NetBoxModelSerializer):
    class Meta:
        model = InterfaceTypeMapping
        fields = ["id", "librenms_type", "netbox_type"]
