import django_filters
from dcim.models import Manufacturer

from .models import (
    DeviceTypeMapping,
    InterfaceTypeMapping,
    InventoryIgnoreRule,
    ModuleBayMapping,
    ModuleTypeMapping,
    NormalizationRule,
    PlatformMapping,
)


class InterfaceTypeMappingFilterSet(django_filters.FilterSet):
    """Filter set for InterfaceTypeMapping model."""

    # Explicit declarations ensure filter names match form field names.
    # Dict-style fields = {"field": ["icontains"]} generates librenms_type__icontains,
    # but the filter form submits librenms_type — causing silent filter failures.
    librenms_type = django_filters.CharFilter(lookup_expr="icontains")
    description = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        """Meta options for InterfaceTypeMappingFilterSet."""

        model = InterfaceTypeMapping
        fields = ["librenms_type", "librenms_speed", "netbox_type", "description"]


class DeviceTypeMappingFilterSet(django_filters.FilterSet):
    """Filter set for DeviceTypeMapping model."""

    librenms_hardware = django_filters.CharFilter(lookup_expr="icontains")
    description = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        """Meta options for DeviceTypeMappingFilterSet."""

        model = DeviceTypeMapping
        fields = ["librenms_hardware", "description"]


class ModuleTypeMappingFilterSet(django_filters.FilterSet):
    """Filter set for ModuleTypeMapping model."""

    librenms_model = django_filters.CharFilter(lookup_expr="icontains")
    description = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        """Meta options for ModuleTypeMappingFilterSet."""

        model = ModuleTypeMapping
        fields = ["librenms_model", "description"]


class ModuleBayMappingFilterSet(django_filters.FilterSet):
    """Filter set for ModuleBayMapping model."""

    librenms_name = django_filters.CharFilter(lookup_expr="icontains")
    librenms_class = django_filters.CharFilter(lookup_expr="icontains")
    netbox_bay_name = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        """Meta options for ModuleBayMappingFilterSet."""

        model = ModuleBayMapping
        fields = ["librenms_name", "librenms_class", "netbox_bay_name", "is_regex"]


class NormalizationRuleFilterSet(django_filters.FilterSet):
    """Filter set for NormalizationRule model."""

    # DynamicModelChoiceField submits manufacturer_id; use a ModelChoiceFilter
    # with field_name="manufacturer" so the filterset resolves it to the FK.
    manufacturer_id = django_filters.ModelChoiceFilter(
        field_name="manufacturer",
        queryset=Manufacturer.objects.all(),
        label="Manufacturer",
    )

    class Meta:
        """Meta options for NormalizationRuleFilterSet."""

        model = NormalizationRule
        fields = ["scope", "manufacturer_id"]


class InventoryIgnoreRuleFilterSet(django_filters.FilterSet):
    """Filter set for InventoryIgnoreRule model."""

    class Meta:
        """Meta options for InventoryIgnoreRuleFilterSet."""

        model = InventoryIgnoreRule
        fields = ["match_type", "action", "enabled"]


class PlatformMappingFilterSet(django_filters.FilterSet):
    """Filter set for PlatformMapping model."""

    librenms_os = django_filters.CharFilter(lookup_expr="icontains")
    description = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        """Meta options for PlatformMappingFilterSet."""

        model = PlatformMapping
        fields = ["librenms_os", "description"]
