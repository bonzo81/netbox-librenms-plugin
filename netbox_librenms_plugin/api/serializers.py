from netbox.api.serializers import NetBoxModelSerializer

from netbox_librenms_plugin.models import (
    CarrierAutoInstallRule,
    DeviceTypeMapping,
    InterfaceTypeMapping,
    InventoryIgnoreRule,
    ModuleBayMapping,
    ModuleTypeMapping,
    NormalizationRule,
    PlatformMapping,
    PortStackLagPattern,
)


class InterfaceTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize InterfaceTypeMapping model for REST API."""

    class Meta:
        """Meta options for InterfaceTypeMappingSerializer."""

        model = InterfaceTypeMapping
        fields = ["id", "url", "display", "librenms_type", "librenms_speed", "netbox_type", "description"]
        brief_fields = ("id", "url", "display", "librenms_type", "netbox_type", "description")


class DeviceTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize DeviceTypeMapping model for REST API."""

    class Meta:
        """Meta options for DeviceTypeMappingSerializer."""

        model = DeviceTypeMapping
        fields = ["id", "url", "display", "librenms_hardware", "netbox_device_type", "description"]
        brief_fields = ("id", "url", "display", "librenms_hardware", "netbox_device_type", "description")


class ModuleTypeMappingSerializer(NetBoxModelSerializer):
    """Serialize ModuleTypeMapping model for REST API."""

    class Meta:
        """Meta options for ModuleTypeMappingSerializer."""

        model = ModuleTypeMapping
        fields = ["id", "url", "display", "librenms_model", "manufacturer", "netbox_module_type", "description"]
        brief_fields = ("id", "url", "display", "librenms_model", "netbox_module_type", "description")


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


class PlatformMappingSerializer(NetBoxModelSerializer):
    """Serialize PlatformMapping model for REST API."""

    class Meta:
        """Meta options for PlatformMappingSerializer."""

        model = PlatformMapping
        fields = ["id", "url", "display", "librenms_os", "netbox_platform", "description"]
        brief_fields = ("id", "url", "display", "librenms_os", "netbox_platform", "description")


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


class PortStackLagPatternSerializer(NetBoxModelSerializer):
    """Serialize PortStackLagPattern model for REST API."""

    class Meta:
        """Meta options for PortStackLagPatternSerializer."""

        model = PortStackLagPattern
        fields = ["id", "url", "display", "librenms_os", "lag_name_pattern", "sap_name_pattern", "description"]
        brief_fields = ("id", "url", "display", "librenms_os", "lag_name_pattern", "description")
