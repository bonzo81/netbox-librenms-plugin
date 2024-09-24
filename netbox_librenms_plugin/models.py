from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel
from dcim.choices import InterfaceTypeChoices
from dcim.models import DeviceType


class InterfaceTypeMapping(NetBoxModel):
    librenms_type = models.CharField(max_length=100, unique=True)
    netbox_type = models.CharField(
        max_length=50,
        choices=InterfaceTypeChoices,
        default=InterfaceTypeChoices.TYPE_OTHER
    )

    def get_absolute_url(self):
        return reverse("plugins:netbox_librenms_plugin:interfacetypemapping_detail", args=[self.pk])

    def __str__(self):
        return f"{self.librenms_type} -> {self.get_netbox_type_display()}"
