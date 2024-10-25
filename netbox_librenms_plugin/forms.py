# forms.py
from netbox.forms import NetBoxModelForm

from .models import InterfaceTypeMapping


class InterfaceTypeMappingForm(NetBoxModelForm):
    class Meta:
        model = InterfaceTypeMapping
        fields = ["librenms_type", "librenms_speed", "netbox_type"]


class InterfaceTypeMappingFilterForm(NetBoxModelForm):
    class Meta:
        model = InterfaceTypeMapping
        fields = ["librenms_type", "librenms_speed", "netbox_type"]
