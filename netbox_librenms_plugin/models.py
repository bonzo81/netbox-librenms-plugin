from dcim.choices import InterfaceTypeChoices
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel


class LibreNMSSettings(models.Model):
    """
    Model to store LibreNMS plugin settings, specifically which server to use
    when multiple servers are configured.
    """

    selected_server = models.CharField(
        max_length=100,
        default="default",
        help_text="The key of the selected LibreNMS server from configuration",
    )

    class Meta:
        verbose_name = "LibreNMS Settings"
        verbose_name_plural = "LibreNMS Settings"

    def get_absolute_url(self):
        return reverse("plugins:netbox_librenms_plugin:settings")

    def __str__(self):
        return f"LibreNMS Settings - Server: {self.selected_server}"


class InterfaceTypeMapping(NetBoxModel):
    librenms_type = models.CharField(max_length=100)
    netbox_type = models.CharField(
        max_length=50,
        choices=InterfaceTypeChoices,
        default=InterfaceTypeChoices.TYPE_OTHER,
    )
    librenms_speed = models.BigIntegerField(null=True, blank=True)

    def get_absolute_url(self):
        return reverse(
            "plugins:netbox_librenms_plugin:interfacetypemapping_detail", args=[self.pk]
        )

    class Meta:
        unique_together = ["librenms_type", "librenms_speed"]

    def __str__(self):
        return f"{self.librenms_type} + {self.librenms_speed} -> {self.netbox_type}"
