from dcim.models import Device
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils.html import escape
from django.views import View
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.forms import AddToLIbreSNMPV1V2, AddToLIbreSNMPV3
from netbox_librenms_plugin.views.mixins import (
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)
from netbox_librenms_plugin.views.sync.device_fields import _device_sync_redirect


class AddDeviceToLibreNMSView(
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    LibreNMSAPIMixin,
    View,
):
    """Add a NetBox device or VM to LibreNMS via the API."""

    def get_form_class(self):
        """Return the appropriate SNMP form class based on the SNMP version."""
        snmp_version = self.request.POST.get("snmp_version")
        if not snmp_version:
            snmp_version = self.request.POST.get("v1v2-snmp_version") or self.request.POST.get("v3-snmp_version")

        if snmp_version in ("v1", "v2c"):
            return AddToLIbreSNMPV1V2
        return AddToLIbreSNMPV3

    def get_object(self, object_id, object_type=None):
        """
        Return the Device or VirtualMachine for the given ID.

        Uses *object_type* to pick the correct model, preventing a false
        match when a Device and VirtualMachine share the same PK.

        Returns ``None`` for invalid *object_type* — callers must short-circuit
        with HTTP 400 before using the result (a missing/unknown object_type is
        a client error, not a missing-resource error).
        """
        if object_type == "virtualmachine":
            return self.restrict_object_or_404(VirtualMachine, "change", pk=object_id)
        if object_type == "device":
            return self.restrict_object_or_404(Device, "change", pk=object_id)
        return None

    def post(self, request, object_id):
        """Add a device to LibreNMS using the submitted SNMP form."""
        object_type = request.POST.get("object_type")
        if object_type not in ("device", "virtualmachine"):
            # Match the convention used in views/sync/device_fields.py — return
            # 400 (Bad Request) with an escaped echo of the offending value
            # rather than raising 404, which would mislead clients into
            # thinking the object itself is missing.
            return HttpResponse(
                f"Invalid object_type: {escape(str(object_type))}",
                status=400,
            )

        # Gate before the change-scoped lookup so a missing grant produces the
        # named permission error instead of a bare 404.
        target_model = VirtualMachine if object_type == "virtualmachine" else Device
        self.required_object_permissions = {"POST": [("change", target_model)]}
        if error := self.require_all_permissions("POST"):
            return error

        self.object = self.get_object(object_id, object_type=object_type)

        form_class = self.get_form_class()

        snmp_version = request.POST.get("v1v2-snmp_version") or request.POST.get("v3-snmp_version")
        prefix = "v1v2" if snmp_version in ("v1", "v2c") else "v3"

        form = form_class(request.POST, prefix=prefix)
        if form.is_valid():
            # Inject snmp_version from toggle into cleaned_data for v1/v2c forms
            if snmp_version in ("v1", "v2c"):
                form.cleaned_data["snmp_version"] = snmp_version
            return self.form_valid(form, snmp_version=snmp_version)

        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")
        return redirect(self.object.get_absolute_url())

    def form_valid(self, form, snmp_version=None):
        """Submit the validated SNMP form data to the LibreNMS API."""
        data = form.cleaned_data
        # Use the snmp_version from toggle/form for v1/v2c, or from form data for v3
        version = snmp_version or data.get("snmp_version")
        device_data = {
            "hostname": data.get("hostname"),
            "snmp_version": version,
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

        if device_data["snmp_version"] in ("v1", "v2c"):
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


class UpdateDeviceLocationView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Update the LibreNMS site/location based on the NetBox site."""

    # The device is only read here (the write lands in LibreNMS), but it is read by raw URL pk —
    # so gate on the object view permission and resolve through the restricted queryset, or a
    # constrained grant could push any device's site to LibreNMS.
    required_object_permissions = {"POST": [("view", Device)]}

    def post(self, request, pk):
        """Sync the device location to LibreNMS from the NetBox site."""
        # Check plugin write permission AND the object view permission before touching the device.
        if error := self.require_all_permissions("POST"):
            return error

        device = self.restrict_object_or_404(Device, pk=pk)

        # Rebind the API client to the POSTed server before resolving the per-server
        # librenms_id and writing the location, so a multi-server user acting on a
        # non-default tab isn't routed through the globally selected server (writing
        # the location to the wrong LibreNMS instance). Mirrors UpdateDeviceNameView.
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return _device_sync_redirect(request, pk, server_key)

        self.librenms_id, lookup_error = self.resolve_librenms_id(device)
        if lookup_error is not None:
            messages.error(request, lookup_error.message)
            return _device_sync_redirect(request, pk, server_key)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        if device.site:
            librenms_api = self.librenms_api
            field_data = {
                "field": ["location", "override_sysLocation"],
                "data": [device.site.name, "1"],
            }
            success, message = librenms_api.update_device_field(self.librenms_id, field_data)

            if success:
                messages.success(
                    request,
                    f"Device location updated in LibreNMS to {device.site.name}",
                )
            else:
                messages.error(request, f"Failed to update device location in LibreNMS: {message}")
        else:
            messages.warning(request, "Device has no associated site in NetBox")

        return _device_sync_redirect(request, pk, server_key)
