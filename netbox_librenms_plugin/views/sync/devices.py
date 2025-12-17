from dcim.models import Device
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.forms import AddToLIbreSNMPV2, AddToLIbreSNMPV3
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin


class AddDeviceToLibreNMSView(LibreNMSAPIMixin, View):
    """Add a NetBox device or VM to LibreNMS via the API."""

    def get_form_class(self):
        snmp_version = self.request.POST.get("snmp_version")
        if not snmp_version:
            snmp_version = self.request.POST.get(
                "v2-snmp_version"
            ) or self.request.POST.get("v3-snmp_version")

        if snmp_version == "v2c":
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

        snmp_version = (
            request.POST.get("snmp_version")
            or request.POST.get("v2-snmp_version")
            or request.POST.get("v3-snmp_version")
        )
        prefix = "v2" if snmp_version == "v2c" else "v3"

        form = form_class(request.POST, prefix=prefix)
        if form.is_valid():
            return self.form_valid(form)

        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")
        return redirect(self.object.get_absolute_url())

    def form_valid(self, form):
        data = form.cleaned_data
        device_data = {
            "hostname": data.get("hostname"),
            "snmp_version": data.get("snmp_version"),
            "force_add": data.get("force_add", False),
        }

        if data.get("port"):
            device_data["port"] = data.get("port")
        if data.get("transport"):
            device_data["transport"] = data.get("transport")
        if data.get("port_association_mode"):
            device_data["port_association_mode"] = data.get("port_association_mode")
        if data.get("poller_group"):
            try:
                device_data["poller_group"] = int(data.get("poller_group"))
            except (ValueError, TypeError):
                pass

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

        success, message = self.librenms_api.add_device(device_data)

        if success:
            messages.success(self.request, message)
        else:
            messages.error(self.request, message)
        return redirect(self.object.get_absolute_url())


class UpdateDeviceLocationView(LibreNMSAPIMixin, View):
    """Update the LibreNMS site/location based on the NetBox site."""

    def post(self, request, pk):
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
