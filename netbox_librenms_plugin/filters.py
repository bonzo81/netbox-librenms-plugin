import django_filters

from .models import InterfaceTypeMapping


class InterfaceTypeMappingFilterSet(django_filters.FilterSet):
    class Meta:
        model = InterfaceTypeMapping
        fields = ["librenms_type", "netbox_type"]
