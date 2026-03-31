from netbox.api.serializers import NetBoxModelSerializer

from netbox_librenms_plugin.models import (
    DeviceTypeMapping,
    InterfaceTypeMapping,
    InventoryIgnoreRule,
    ModuleBayMapping,
    ModuleTypeMapping,
    NormalizationRule,
    PlatformMapping,
)


class InterfaceTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize InterfaceTypeMapping model for REST API."""

    class Meta:
        """Meta options for InterfaceTypeMappingSerializer."""

        model = InterfaceTypeMapping
        fields = ["id", "librenms_type", "librenms_speed", "netbox_type", "description"]


class DeviceTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize DeviceTypeMapping model for REST API."""

    class Meta:
        """Meta options for DeviceTypeMappingSerializer."""

        model = DeviceTypeMapping
        fields = ["id", "librenms_hardware", "netbox_device_type", "description"]


class ModuleTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize ModuleTypeMapping model for REST API."""

    class Meta:
        """Meta options for ModuleTypeMappingSerializer."""

        model = ModuleTypeMapping
        fields = ["id", "librenms_model", "netbox_module_type", "description"]


class ModuleBayMappingSerializer(NetBoxModelSerializer):
    """Serialize ModuleBayMapping model for REST API."""

    class Meta:
        """Meta options for ModuleBayMappingSerializer."""

        model = ModuleBayMapping
        fields = ["id", "librenms_name", "librenms_class", "netbox_bay_name", "is_regex", "description"]


class NormalizationRuleSerializer(NetBoxModelSerializer):
    """Serialize NormalizationRule model for REST API."""

    class Meta:
        """Meta options for NormalizationRuleSerializer."""

        model = NormalizationRule
        fields = [
            "id",
            "scope",
            "manufacturer",
            "match_pattern",
            "replacement",
            "priority",
            "description",
        ]


class InventoryIgnoreRuleSerializer(NetBoxModelSerializer):
    """Serialize InventoryIgnoreRule model for REST API."""

    class Meta:
        """Meta options for InventoryIgnoreRuleSerializer."""

        model = InventoryIgnoreRule
        fields = [
            "id",
            "name",
            "match_type",
            "pattern",
            "action",
            "require_serial_match_parent",
            "enabled",
            "description",
        ]


class PlatformMappingSerializer(NetBoxModelSerializer):
    """Serialize PlatformMapping model for REST API."""

    class Meta:
        """Meta options for PlatformMappingSerializer."""

        model = PlatformMapping
        fields = [
            "id",
            "librenms_os",
            "netbox_platform",
            "description",
        ]
