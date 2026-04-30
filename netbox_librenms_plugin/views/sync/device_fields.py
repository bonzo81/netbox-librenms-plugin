import logging

from dcim.models import Device, Manufacturer, Platform
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.import_utils import _determine_device_name
from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name
from netbox_librenms_plugin.models import PlatformMapping
from netbox_librenms_plugin.utils import (
    find_by_librenms_id,
    get_librenms_sync_device,
    match_librenms_hardware_to_device_type,
    migrate_legacy_librenms_id,
    resolve_naming_preferences,
)
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin

logger = logging.getLogger(__name__)


class UpdateDeviceNameView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Update NetBox device name from LibreNMS sysName."""

    required_object_permissions = {
        "POST": [("change", Device)],
    }

    def post(self, request, pk):
        """Sync the device name from LibreNMS sysName."""
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)

        # For VC members without their own librenms_id, use the VC sync device
        librenms_lookup_device = device
        if hasattr(device, "virtual_chassis") and device.virtual_chassis:
            if not device.cf.get("librenms_id"):
                sync_device = get_librenms_sync_device(device)
                if sync_device:
                    librenms_lookup_device = sync_device

        self.librenms_id = self.librenms_api.get_librenms_id(librenms_lookup_device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        success, device_info = self.librenms_api.get_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        # Bail out early when LibreNMS has no usable name – the fallback
        # names that _determine_device_name generates (e.g. "device-42")
        # are only useful during import, not for renaming an existing device.
        if not (device_info.get("sysName") or device_info.get("hostname")):
            messages.warning(request, "No name could be determined from LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        use_sysname, strip_domain = resolve_naming_preferences(request)

        resolved_name = _determine_device_name(
            device_info,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
        )

        # For VC members, generate the expected VC member name
        if (
            resolved_name
            and hasattr(device, "virtual_chassis")
            and device.virtual_chassis is not None
            and device.vc_position is not None
        ):
            resolved_name = _generate_vc_member_name(
                resolved_name,
                device.vc_position,
                serial=getattr(device, "serial", None),
            )

        if not resolved_name:
            messages.warning(request, "No name could be determined from LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        old_name = device.name
        device.name = resolved_name
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.name = old_name
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to update device name to '{resolved_name}': {error_msg}")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        messages.success(request, f"Device name updated from '{old_name}' to '{resolved_name}'")

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class UpdateDeviceSerialView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Update NetBox device serial number from LibreNMS."""

    required_object_permissions = {
        "POST": [("change", Device)],
    }

    def post(self, request, pk):
        """Sync the device serial number from LibreNMS."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        success, device_info = self.librenms_api.get_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        serial = device_info.get("serial")

        if not serial or serial == "-":
            messages.warning(request, "No serial number available in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        old_serial = device.serial
        device.serial = serial
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.serial = old_serial
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to update serial to '{serial}': {error_msg}")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        if old_serial:
            messages.success(
                request,
                f"Device serial updated from '{old_serial}' to '{serial}'",
            )
        else:
            messages.success(request, f"Device serial set to '{serial}'")

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class UpdateDeviceTypeView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Update NetBox DeviceType using LibreNMS hardware metadata."""

    required_object_permissions = {
        "POST": [("change", Device)],
    }

    def post(self, request, pk):
        """Sync the device type from LibreNMS hardware info."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        success, device_info = self.librenms_api.get_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        hardware = device_info.get("hardware")

        if not hardware:
            messages.warning(request, "No hardware information available in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        match_result = match_librenms_hardware_to_device_type(hardware)

        if match_result is None:
            messages.error(
                request,
                f"Ambiguous hardware match for '{hardware}': multiple matching mappings/device types found.",
            )
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        if not match_result["matched"]:
            messages.error(
                request,
                f"No matching DeviceType found for hardware '{hardware}'",
            )
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        device_type = match_result["device_type"]
        old_device_type = device.device_type
        device.device_type = device_type
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.device_type = old_device_type
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to update device type to '{device_type}': {error_msg}")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        messages.success(
            request,
            f"Device type updated from '{old_device_type}' to '{device_type}'",
        )

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class UpdateDevicePlatformView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Update NetBox Platform based on LibreNMS OS info."""

    required_object_permissions = {
        "POST": [("change", Device)],
    }

    def post(self, request, pk):
        """Sync the device platform from LibreNMS OS name."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)
        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        success, device_info = self.librenms_api.get_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        os_name = device_info.get("os")

        if not os_name:
            messages.warning(request, "No OS information available in LibreNMS")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        platform_name = os_name

        try:
            platform = Platform.objects.get(name__iexact=platform_name)
        except Platform.DoesNotExist:
            messages.error(
                request,
                "Platform '{}' does not exist in NetBox. Use 'Create & Sync' button to create it first.".format(
                    platform_name
                ),
            )
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        old_platform = device.platform
        device.platform = platform
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.platform = old_platform
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to update platform to '{platform}': {error_msg}")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        if old_platform:
            messages.success(
                request,
                f"Device platform updated from '{old_platform}' to '{platform}'",
            )
        else:
            messages.success(request, f"Device platform set to '{platform}'")

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class CreateAndAssignPlatformView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Create a new Platform and assign it to the device."""

    required_object_permissions = {
        "POST": [
            ("change", Device),
            ("add", Platform),
        ],
    }

    def post(self, request, pk):
        """Create a new platform and assign it to the device."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)

        platform_name = request.POST.get("platform_name")
        manufacturer_id = request.POST.get("manufacturer")
        librenms_os = (request.POST.get("librenms_os") or "").strip().lower()
        create_mapping = bool(request.POST.get("create_mapping"))

        if not platform_name:
            messages.error(request, "Platform name is required")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        if Platform.objects.filter(name__iexact=platform_name).exists():
            messages.warning(
                request,
                f"Platform '{platform_name}' already exists. Use the regular sync button.",
            )
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        manufacturer = None
        if manufacturer_id:
            try:
                manufacturer = Manufacturer.objects.get(pk=manufacturer_id)
            except Manufacturer.DoesNotExist:
                pass

        with transaction.atomic():
            try:
                platform = Platform(
                    name=platform_name,
                    slug=slugify(platform_name),
                    manufacturer=manufacturer,
                )
                platform.full_clean()
                platform.save()
            except ValidationError as e:
                transaction.set_rollback(True)
                error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
                logger.exception(
                    "ValidationError creating platform '%s' for device pk=%s: %s",
                    platform_name,
                    pk,
                    error_msg,
                )
                messages.error(
                    request,
                    f"Platform '{platform_name}' could not be created: {error_msg}",
                )
                return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)
            except IntegrityError as e:
                transaction.set_rollback(True)
                logger.exception(
                    "IntegrityError creating platform '%s' for device pk=%s",
                    platform_name,
                    pk,
                )
                messages.error(
                    request,
                    f"Platform '{platform_name}' could not be created: {e}",
                )
                return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

            try:
                device = Device.objects.select_for_update().get(pk=pk)
            except Device.DoesNotExist:
                transaction.set_rollback(True)
                messages.error(request, "Device no longer exists.")
                return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

            device.platform = platform
            try:
                device.full_clean()
            except ValidationError as e:
                transaction.set_rollback(True)
                error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
                logger.exception(
                    "ValidationError validating device pk=%s: %s",
                    pk,
                    error_msg,
                )
                messages.error(
                    request,
                    f"Device (pk={pk}) validation failed: {error_msg}",
                )
                return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)
            try:
                device.save()
            except IntegrityError as e:
                transaction.set_rollback(True)
                logger.exception("IntegrityError saving device pk=%s after platform assignment", pk)
                messages.error(
                    request,
                    f"Error saving device (pk={pk}): {e}",
                )
                return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

            mapping_created = False
            mapping_error = None
            mapping_existed = False
            if create_mapping and librenms_os:
                existing = PlatformMapping.objects.filter(librenms_os=librenms_os).first()
                if existing is not None:
                    mapping_existed = True
                else:
                    try:
                        mapping = PlatformMapping(librenms_os=librenms_os, netbox_platform=platform)
                        mapping.full_clean()
                        mapping.save()
                        mapping_created = True
                    except (ValidationError, IntegrityError) as e:
                        mapping_error = e.message_dict if hasattr(e, "message_dict") else str(e)
                        logger.exception("Failed to create PlatformMapping '%s' -> '%s'", librenms_os, platform_name)

        msg = f"Created platform '{platform}' and assigned to device"
        if mapping_created:
            msg += f" — platform mapping '{librenms_os}' → '{platform}' added"
        messages.success(request, msg)
        if mapping_error:
            messages.warning(
                request,
                f"Platform mapping '{librenms_os}' → '{platform}' could not be created: {mapping_error}",
            )
        elif mapping_existed:
            messages.info(
                request,
                f"Platform mapping for '{librenms_os}' already exists; not modified.",
            )

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class AssignVCSerialView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Assign serial numbers to each virtual chassis member."""

    required_object_permissions = {
        "POST": [("change", Device)],
    }

    def post(self, request, pk):
        """Sync serial numbers to virtual chassis member devices."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        device = get_object_or_404(Device, pk=pk)

        if not device.virtual_chassis:
            messages.error(request, "Device is not part of a virtual chassis")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        assignments_made = 0
        errors = []

        counter = 1
        while f"serial_{counter}" in request.POST:
            serial = request.POST.get(f"serial_{counter}")
            member_id = request.POST.get(f"member_id_{counter}")

            if not member_id:
                counter += 1
                continue

            try:
                member = Device.objects.get(pk=member_id)

                if not member.virtual_chassis or member.virtual_chassis.pk != device.virtual_chassis.pk:
                    errors.append(f"{member.name} is not part of the same virtual chassis")
                    counter += 1
                    continue

                old_serial = member.serial
                member.serial = serial
                try:
                    member.full_clean()
                    member.save()
                except (ValidationError, IntegrityError) as e:
                    member.serial = old_serial
                    error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
                    errors.append(f"Failed to set serial on {member.name}: {error_msg}")
                    counter += 1
                    continue

                assignments_made += 1

            except Device.DoesNotExist:
                errors.append(f"Device with ID {member_id} not found")
            except Exception as exc:  # pragma: no cover - defensive guard
                errors.append(f"Error assigning serial to member {member_id}: {str(exc)}")

            counter += 1

        if assignments_made > 0:
            messages.success(
                request,
                f"Successfully assigned {assignments_made} serial number(s) to VC members",
            )

        if errors:
            for error in errors:
                messages.error(request, error)

        if assignments_made == 0 and not errors:
            messages.info(request, "No serial assignments were made")

        return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)


class RemoveServerMappingView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Remove a single server entry from the device's (or VM's) librenms_id custom field dict."""

    required_object_permissions = {
        "POST": [("change", Device), ("change", VirtualMachine)],
    }

    def _get_object(self, object_type, pk):
        """Return the Device or VirtualMachine for the given pk."""
        model = VirtualMachine if object_type == "vm" else Device
        return get_object_or_404(model, pk=pk), model

    def _sync_url_name(self, object_type):
        if object_type == "vm":
            return "plugins:netbox_librenms_plugin:vm_librenms_sync"
        return "plugins:netbox_librenms_plugin:device_librenms_sync"

    def _normalize_librenms_mapping(self, value):
        if isinstance(value, bool):
            return {}
        if isinstance(value, int):
            return {"default": value}
        if isinstance(value, str) and value.isdigit():
            return {"default": int(value)}
        return value if isinstance(value, dict) else {}

    def post(self, request, pk):
        # Scope required permissions to the specific model being modified before checking.
        object_type = request.POST.get("object_type", "device")
        if object_type == "virtualmachine":
            object_type = "vm"
        if object_type not in ("device", "vm"):
            return HttpResponse(f"Invalid object_type: {object_type!r}", status=400)
        target_model = VirtualMachine if object_type == "vm" else Device
        self.required_object_permissions = {"POST": [("change", target_model)]}

        if error := self.require_all_permissions("POST"):
            return error

        obj, model = self._get_object(object_type, pk)
        sync_url = self._sync_url_name(object_type)
        server_key = request.POST.get("server_key", "").strip()

        if not server_key:
            messages.error(request, "No server_key provided.")
            return redirect(sync_url, pk=pk)

        cf_value = self._normalize_librenms_mapping(obj.custom_field_data.get("librenms_id"))
        if not isinstance(cf_value, dict) or server_key not in cf_value:
            messages.warning(request, f"No mapping found for server '{server_key}'.")
            return redirect(sync_url, pk=pk)

        # Refuse to remove mappings for servers that are still configured in the plugin.
        # Only orphaned (unconfigured) mappings may be removed via this endpoint.
        # Guard both multi-server mode (servers dict) and legacy single-server mode
        # (top-level librenms_url in plugin config, which implicitly defines "default")
        # but only when no servers section is configured (pure legacy mode).
        from django.conf import settings as django_settings

        plugins_cfg = django_settings.PLUGINS_CONFIG.get("netbox_librenms_plugin", {})
        configured_servers = plugins_cfg.get("servers") or {}
        if not isinstance(configured_servers, dict):
            configured_servers = {}
        legacy_url_configured = bool(plugins_cfg.get("librenms_url"))
        if server_key in configured_servers or (
            legacy_url_configured and not configured_servers and server_key == "default"
        ):
            messages.error(
                request,
                f"Cannot remove mapping for configured server '{server_key}'. "
                "Remove the server from plugin configuration first, then retry.",
            )
            return redirect(sync_url, pk=pk)

        with transaction.atomic():
            try:
                obj_locked = model.objects.select_for_update().get(pk=pk)
            except model.DoesNotExist:
                messages.error(request, f"{model.__name__} no longer exists.")
                return redirect(sync_url, pk=pk)
            cf = self._normalize_librenms_mapping(obj_locked.custom_field_data.get("librenms_id"))
            # Re-check after acquiring lock; mirror the pre-transaction protection logic
            _is_protected = server_key in configured_servers or (
                legacy_url_configured and not configured_servers and server_key == "default"
            )
            if isinstance(cf, dict) and server_key in cf and not _is_protected:
                del cf[server_key]
                obj_locked.custom_field_data["librenms_id"] = cf if cf else None
                try:
                    obj_locked.full_clean()
                    obj_locked.save()
                except ValidationError as exc:
                    transaction.set_rollback(True)
                    error_msg = exc.message_dict if hasattr(exc, "message_dict") else str(exc)
                    logger.exception(
                        "Validation error removing LibreNMS mapping for server %r: %s", server_key, error_msg
                    )
                    messages.error(request, f"Validation error removing LibreNMS mapping: {error_msg}")
                    return redirect(sync_url, pk=pk)
                except Exception as exc:
                    transaction.set_rollback(True)
                    logger.exception("Unexpected error removing LibreNMS mapping for server %r", server_key)
                    messages.error(request, f"Unexpected error removing LibreNMS mapping: {exc}")
                    return redirect(sync_url, pk=pk)
                messages.success(request, f"Removed LibreNMS mapping for server '{server_key}'.")
            else:
                messages.warning(request, f"Mapping for server '{server_key}' was already removed.")

        return redirect(sync_url, pk=pk)


class ConvertLegacyLibreNMSIdView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """
    Convert a legacy bare-integer librenms_id to the server-scoped JSON dict format.

    Only allowed when the NetBox serial matches the LibreNMS serial, so the
    association can be verified before scoping the ID to the active server.
    """

    required_object_permissions = {
        "POST": [("change", Device), ("change", VirtualMachine)],
    }

    def _get_model_and_object(self, object_type, pk):
        model = VirtualMachine if object_type == "vm" else Device
        return model, get_object_or_404(model, pk=pk)

    def _sync_url(self, object_type, pk):
        name = "vm_librenms_sync" if object_type == "vm" else "device_librenms_sync"
        return redirect(f"plugins:netbox_librenms_plugin:{name}", pk=pk)

    def post(self, request, pk):
        object_type = request.POST.get("object_type", "device")
        if object_type == "virtualmachine":
            object_type = "vm"
        if object_type not in ("device", "vm"):
            return HttpResponse(f"Invalid object_type: {object_type!r}", status=400)

        target_model = VirtualMachine if object_type == "vm" else Device
        self.required_object_permissions = {"POST": [("change", target_model)]}
        if error := self.require_all_permissions("POST"):
            return error

        model, obj = self._get_model_and_object(object_type, pk)
        server_key = self.librenms_api.server_key

        # Verify the device actually has a legacy bare-int librenms_id
        cf_value = obj.custom_field_data.get("librenms_id")
        if isinstance(cf_value, bool):
            messages.error(request, "librenms_id has an invalid boolean value; cannot convert.")
            return self._sync_url(object_type, pk)
        if not isinstance(cf_value, (int, str)):
            messages.warning(request, "librenms_id is already in the server-scoped JSON format.")
            return self._sync_url(object_type, pk)
        if isinstance(cf_value, str):
            if not cf_value.isdigit():
                messages.error(request, "librenms_id is not a valid integer; cannot convert.")
                return self._sync_url(object_type, pk)

        # Verify serial match before converting
        librenms_id = int(cf_value) if isinstance(cf_value, str) else cf_value
        success, device_info = self.librenms_api.get_device_info(librenms_id)
        if not success or not device_info:
            messages.error(request, "Could not retrieve device info from LibreNMS to verify serial.")
            return self._sync_url(object_type, pk)

        librenms_serial = (device_info.get("serial") or "").strip()
        netbox_serial = (getattr(obj, "serial", None) or "").strip()
        # VMs have no serial field in NetBox; skip the serial gate for them.
        is_vm = object_type == "vm"
        if not is_vm and (not netbox_serial or not librenms_serial or netbox_serial != librenms_serial):
            messages.error(
                request,
                "Serial number mismatch — cannot convert legacy ID without serial confirmation.",
            )
            return self._sync_url(object_type, pk)

        with transaction.atomic():
            try:
                locked = model.objects.select_for_update().get(pk=pk)
            except model.DoesNotExist:
                messages.error(request, f"{model.__name__} no longer exists.")
                return self._sync_url(object_type, pk)
            # Re-check preconditions on the locked row (another admin may have
            # changed cf_value or serial between the initial read and the lock).
            locked_cf = locked.custom_field_data.get("librenms_id")
            if not isinstance(locked_cf, (int, str)) or isinstance(locked_cf, bool):
                messages.warning(request, "librenms_id is already in the server-scoped JSON format.")
                return self._sync_url(object_type, pk)
            locked_id = int(locked_cf) if isinstance(locked_cf, str) and locked_cf.isdigit() else locked_cf
            if not isinstance(locked_id, int):
                messages.error(request, "librenms_id changed before lock was acquired; aborting.")
                return self._sync_url(object_type, pk)
            locked_serial = (getattr(locked, "serial", None) or "").strip()
            if locked_id != librenms_id or locked_serial != netbox_serial:
                messages.error(request, "Device data changed before lock was acquired; aborting conversion.")
                return self._sync_url(object_type, pk)
            # Check that no other object already owns this ID (server-scoped or legacy)
            match = find_by_librenms_id(model, librenms_id, server_key)
            conflict = match is not None and match.pk != locked.pk
            if conflict:
                transaction.set_rollback(True)
                messages.error(
                    request,
                    f"Another {model.__name__} already has librenms_id {librenms_id} "
                    f"for server '{server_key}'; cannot convert.",
                )
                return self._sync_url(object_type, pk)
            migrated = migrate_legacy_librenms_id(locked, server_key)
            if not migrated:
                messages.warning(request, "librenms_id is already in the server-scoped JSON format.")
                return self._sync_url(object_type, pk)
            try:
                locked.full_clean()
                locked.save()
            except ValidationError as exc:
                transaction.set_rollback(True)
                error_msg = exc.message_dict if hasattr(exc, "message_dict") else str(exc)
                messages.error(request, f"Failed to save converted librenms_id: {error_msg}")
                return self._sync_url(object_type, pk)
            except Exception as exc:
                transaction.set_rollback(True)
                logger.exception("Failed saving converted librenms_id for %s/%s", object_type, pk)
                messages.error(request, f"Failed to save converted librenms_id: {exc}")
                return self._sync_url(object_type, pk)

        messages.success(
            request,
            f"Converted legacy librenms_id {librenms_id} → {{'{server_key}': {librenms_id}}}.",
        )
        return self._sync_url(object_type, pk)
