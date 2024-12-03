from dcim.models import Device
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import FormView
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.forms import AddToLIbreSNMPV2, AddToLIbreSNMPV3
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin


class AddDeviceToLibreNMSView(LibreNMSAPIMixin, FormView):
    template_name = "add_device_modal.html"
    success_url = reverse_lazy("device_list")

    def get_form_class(self):
        if self.request.POST.get("snmp_version") == "v2c":
            return AddToLIbreSNMPV2
        return AddToLIbreSNMPV3

    def get_object(self, object_id):
        try:
            return Device.objects.get(pk=object_id)
        except Device.DoesNotExist:
            return VirtualMachine.objects.get(pk=object_id)

    def post(self, request, object_id):
        self.object = self.get_object(object_id)
        form_class = self.get_form_class()
        form = form_class(request.POST)
        if form.is_valid():
            return self.form_valid(form)
        return self.form_invalid(form)

    def form_valid(self, form):
        data = form.cleaned_data
        device_data = {
            "hostname": data.get("hostname"),
            "snmp_version": data.get("snmp_version"),
        }

        if device_data["snmp_version"] == "v2c":
            device_data["community"] = data.get("community")
        elif device_data["snmp_version"] == "v3":
            device_data.update(
                {
                    "authlevel": data.get("authlevel"),
                    "authname": data.get("authname"),
                    "authpass": data.get("authpass"),
                    "authalgo": data.get("authalgo"),
                    "cryptopass": data.get("cryptopass"),
                    "cryptoalgo": data.get("cryptoalgo"),
                }
            )
        else:
            messages.error(self.request, "Unknown SNMP version.")
            return redirect(self.object.get_absolute_url())

        result = self.librenms_api.add_device(device_data)

        if result["success"]:
            messages.success(self.request, result["message"])
        else:
            messages.error(self.request, result["message"])
        return redirect(self.object.get_absolute_url())


class UpdateDeviceLocationView(LibreNMSAPIMixin, View):
    """
    Update the device location in LibreNMS based on the device's site in NetBox.
    """

    def post(self, request, pk):
        """
        Handle the POST request to update the device location in LibreNMS.
        """
        device = get_object_or_404(Device, pk=pk)

        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if device.site:
            librenms_api = self.librenms_api
            field_data = {
                "field": ["location", "override_sysLocation"],
                "data": [device.site.name, "1"],
            }
            success, message = librenms_api.update_device_field(
                self.librenms_id, field_data
            )

            if success:
                messages.success(
                    request,
                    f"Device location updated in LibreNMS to {device.site.name}",
                )
            else:
                messages.error(
                    request, f"Failed to update device location in LibreNMS: {message}"
                )
        else:
            messages.warning(request, "Device has no associated site in NetBox")

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)
