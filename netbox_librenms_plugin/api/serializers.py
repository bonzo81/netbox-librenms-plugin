from drf_spectacular.utils import extend_schema_serializer
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from netbox_librenms_plugin.models import (
    CarrierAutoInstallRule,
    DeviceTypeMapping,
    InterfaceTypeMapping,
    InventoryIgnoreRule,
    LocationMapping,
    ModuleBayMapping,
    ModuleTypeMapping,
    NormalizationRule,
    PlatformMapping,
    PortStackLagPattern,
)


@extend_schema_serializer(component_name="LibreNMSInterfaceTypeMapping")
class InterfaceTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize InterfaceTypeMapping model for REST API."""

    class Meta:
        """Meta options for InterfaceTypeMappingSerializer."""

        model = InterfaceTypeMapping
        fields = ["id", "url", "display", "librenms_type", "librenms_speed", "netbox_type", "description"]
        brief_fields = ("id", "url", "display", "librenms_type", "netbox_type", "description")


@extend_schema_serializer(component_name="LibreNMSDeviceTypeMapping")
class DeviceTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize DeviceTypeMapping model for REST API."""

    class Meta:
        """Meta options for DeviceTypeMappingSerializer."""

        model = DeviceTypeMapping
        fields = ["id", "url", "display", "librenms_hardware", "netbox_device_type", "description"]
        brief_fields = ("id", "url", "display", "librenms_hardware", "netbox_device_type", "description")


@extend_schema_serializer(component_name="LibreNMSModuleTypeMapping")
class ModuleTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize ModuleTypeMapping model for REST API."""

    class Meta:
        """Meta options for ModuleTypeMappingSerializer."""

        model = ModuleTypeMapping
        fields = ["id", "url", "display", "librenms_model", "manufacturer", "netbox_module_type", "description"]
        brief_fields = ("id", "url", "display", "librenms_model", "netbox_module_type", "description")


@extend_schema_serializer(component_name="LibreNMSModuleBayMapping")
class ModuleBayMappingSerializer(NetBoxModelSerializer):
    """Serialize ModuleBayMapping model for REST API."""

    class Meta:
        """Meta options for ModuleBayMappingSerializer."""

        model = ModuleBayMapping
        fields = [
            "id",
            "url",
            "display",
            "librenms_name",
            "librenms_class",
            "netbox_bay_name",
            "is_regex",
            "manufacturer",
            "description",
        ]
        brief_fields = ("id", "url", "display", "librenms_name", "netbox_bay_name", "description")


@extend_schema_serializer(component_name="LibreNMSNormalizationRule")
class NormalizationRuleSerializer(NetBoxModelSerializer):
    """Serialize NormalizationRule model for REST API."""

    class Meta:
        """Meta options for NormalizationRuleSerializer."""

        model = NormalizationRule
        fields = [
            "id",
            "url",
            "display",
            "scope",
            "manufacturer",
            "match_pattern",
            "replacement",
            "priority",
            "description",
        ]
        brief_fields = ("id", "url", "display", "scope", "match_pattern", "description")


@extend_schema_serializer(component_name="LibreNMSInventoryIgnoreRule")
class InventoryIgnoreRuleSerializer(NetBoxModelSerializer):
    """Serialize InventoryIgnoreRule model for REST API."""

    class Meta:
        """Meta options for InventoryIgnoreRuleSerializer."""

        model = InventoryIgnoreRule
        fields = [
            "id",
            "url",
            "display",
            "name",
            "match_type",
            "pattern",
            "action",
            "require_serial_match_parent",
            "enabled",
            "description",
        ]
        brief_fields = ("id", "url", "display", "name", "description")


@extend_schema_serializer(component_name="LibreNMSPlatformMapping")
class PlatformMappingSerializer(NetBoxModelSerializer):
    """Serialize PlatformMapping model for REST API."""

    class Meta:
        """Meta options for PlatformMappingSerializer."""

        model = PlatformMapping
        fields = ["id", "url", "display", "librenms_os", "netbox_platform", "description"]
        brief_fields = ("id", "url", "display", "librenms_os", "netbox_platform", "description")


class LocationMappingSerializer(NetBoxModelSerializer):
    """Serialize LocationMapping model for REST API."""

    class Meta:
        """Meta options for LocationMappingSerializer."""

        model = LocationMapping
        fields = [
            "id",
            "url",
            "display",
            "field_type",
            "librenms_value",
            "content_type",
            "object_id",
            "description",
        ]
        brief_fields = ("id", "url", "display", "field_type", "librenms_value", "description")


@extend_schema_serializer(component_name="LibreNMSCarrierAutoInstallRule")
class CarrierAutoInstallRuleSerializer(NetBoxModelSerializer):
    """Serialize CarrierAutoInstallRule model for REST API."""

    class Meta:
        """Meta options for CarrierAutoInstallRuleSerializer."""

        model = CarrierAutoInstallRule
        fields = [
            "id",
            "url",
            "display",
            "manufacturer",
            "device_type_pattern",
            "librenms_child_class",
            "librenms_child_name_pattern",
            "netbox_bay_name_pattern",
            "carrier_module_type",
            "description",
        ]
        brief_fields = ("id", "url", "display", "device_type_pattern", "description")


@extend_schema_serializer(component_name="LibreNMSPortStackLagPattern")
class PortStackLagPatternSerializer(NetBoxModelSerializer):
    """Serialize PortStackLagPattern model for REST API."""

    class Meta:
        """Meta options for PortStackLagPatternSerializer."""

        model = PortStackLagPattern
        fields = ["id", "url", "display", "librenms_os", "lag_name_pattern", "sap_name_pattern", "description"]
        brief_fields = ("id", "url", "display", "librenms_os", "lag_name_pattern", "description")


@extend_schema_serializer(component_name="LibreNMSSyncJobStatus")
class SyncJobStatusSerializer(serializers.Serializer):
    """Serialize the sync_job_status response so the endpoint documents a concrete shape."""

    status = serializers.ChoiceField(choices=["updated", "no_change"])
    db_status = serializers.CharField()
    rq_status = serializers.CharField()


@extend_schema_serializer(component_name="LibreNMSJobError")
class JobErrorSerializer(serializers.Serializer):
    """Serialize the sync_job_status error response."""

    error = serializers.CharField()
