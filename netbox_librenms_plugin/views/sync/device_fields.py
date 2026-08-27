import logging

from dcim.models import Device, Manufacturer, Platform
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.html import escape
from django.views import View
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.import_utils import _determine_device_name
from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name
from netbox_librenms_plugin.models import PlatformMapping
from netbox_librenms_plugin.server_mappings import (
    PREFERRED_SERVER_FIELD,
    iter_server_mapping_entries,
    require_server_key,
    with_preferred_server,
    without_server_mapping,
)
from netbox_librenms_plugin.server_selection import build_server_mappings
from netbox_librenms_plugin.sync_cache import SyncTab
from netbox_librenms_plugin.utils import (
    AmbiguousLibreNMSIdError,
    find_by_librenms_id,
    find_matching_platform,
    get_librenms_sync_device,
    is_legacy_librenms_id,
    match_librenms_hardware_to_device_type,
    migrate_legacy_librenms_id,
    normalize_serial,
    resolve_naming_preferences,
)
from netbox_librenms_plugin.views.mixins import (
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    redirect_with_server_key,
    resolve_configured_server_key,
)

logger = logging.getLogger(__name__)


SYNC_OBJECT_TYPES = ("device", "vm")


def _normalize_sync_object_type(value):
    """Return the canonical sync object type, or None when it is not supported."""
    if value == "virtualmachine":
        value = "vm"
    return value if value in SYNC_OBJECT_TYPES else None


def _sync_model(object_type):
    """Return the model that owns the LibreNMS mapping for one object type."""
    return VirtualMachine if object_type == "vm" else Device


def _sync_url_name(object_type):
    """Return the sync-page URL name for one object type."""
    if object_type == "vm":
        return "plugins:netbox_librenms_plugin:vm_librenms_sync"
    return "plugins:netbox_librenms_plugin:device_librenms_sync"


def _device_sync_redirect(request, pk, server_key):
    """
    Redirect to the device sync tab, carrying the already-resolved ``server_key``.

    The four field-sync views (name/serial/type/platform) rebind the API to the POSTed server up
    front; their redirects must preserve that key so a multi-server user returns to the same
    server's tab instead of the session/default one. ``server_key`` here is the config-matched key
    from ``rebind_api_for_server`` (not a raw POST value), so it is reflected directly; a None key
    (a stale/removed server) redirects to the bare tab. ``redirect_with_server_key`` still gates the
    final URL on the open-redirect barrier (CWE-601).
    """
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
    return redirect_with_server_key(request, url, server_key)


class UpdateDeviceNameView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Update NetBox device name from LibreNMS sysName."""

    required_object_permissions = {
        "POST": [("change", Device)],
    }

    def post(self, request, pk):
        """Sync the device name from LibreNMS sysName."""
        if error := self.require_all_permissions("POST"):
            return error

        device = self.restrict_object_or_404(Device, "change", pk=pk)

        # Rebind the API client to the POSTed server before resolving the per-server librenms_id,
        # so a multi-server user acting on a non-default tab isn't routed through the globally
        # selected server (returning the wrong device's id, or none). Mirrors
        # ConvertLegacyLibreNMSIdView.
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return _device_sync_redirect(request, pk, server_key)

        # For VC members without their own librenms_id, use the VC sync device. Scope the
        # resolution to the POST-rebound server_key so a multi-server VC picks the sibling
        # linked to *this* server, not the server-agnostic "any member with any id" fallback
        # (which would rename the device from a different server's LibreNMS device).
        librenms_lookup_device = device
        if hasattr(device, "virtual_chassis") and device.virtual_chassis:
            if not device.cf.get("librenms_id"):
                sync_device = get_librenms_sync_device(device, server_key=server_key)
                if sync_device:
                    librenms_lookup_device = sync_device

        self.librenms_id = self.librenms_api.get_librenms_id(librenms_lookup_device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        success, device_info = self.get_live_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        # Bail out early when LibreNMS has no usable name – the fallback
        # names that _determine_device_name generates (e.g. "device-42")
        # are only useful during import, not for renaming an existing device.
        if not (device_info.get("sysName") or device_info.get("hostname")):
            messages.warning(request, "No name could be determined from LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

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
            return _device_sync_redirect(request, pk, server_key)

        old_name = device.name
        device.name = resolved_name
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.name = old_name
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to update device name to '{resolved_name}': {error_msg}")
            return _device_sync_redirect(request, pk, server_key)

        messages.success(request, f"Device name updated from '{old_name}' to '{resolved_name}'")

        return _device_sync_redirect(request, pk, server_key)


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

        device = self.restrict_object_or_404(Device, "change", pk=pk)

        # Rebind the API client to the POSTed server before resolving the per-server librenms_id,
        # so a multi-server user acting on a non-default tab isn't routed through the globally
        # selected server (returning the wrong device's id, or none). Mirrors
        # ConvertLegacyLibreNMSIdView.
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return _device_sync_redirect(request, pk, server_key)

        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        success, device_info = self.get_live_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        serial = normalize_serial(device_info.get("serial"))

        if not serial or serial == "-":
            messages.warning(request, "No serial number available in LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        old_serial = device.serial
        device.serial = serial
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.serial = old_serial
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to update serial to '{serial}': {error_msg}")
            return _device_sync_redirect(request, pk, server_key)

        if old_serial:
            messages.success(
                request,
                f"Device serial updated from '{old_serial}' to '{serial}'",
            )
        else:
            messages.success(request, f"Device serial set to '{serial}'")

        return _device_sync_redirect(request, pk, server_key)


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

        device = self.restrict_object_or_404(Device, "change", pk=pk)

        # Rebind the API client to the POSTed server before resolving the per-server librenms_id,
        # so a multi-server user acting on a non-default tab isn't routed through the globally
        # selected server (returning the wrong device's id, or none). Mirrors
        # ConvertLegacyLibreNMSIdView.
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return _device_sync_redirect(request, pk, server_key)

        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        success, device_info = self.get_live_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        hardware = device_info.get("hardware")

        if not hardware:
            messages.warning(request, "No hardware information available in LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        match_result = match_librenms_hardware_to_device_type(hardware)

        if match_result is None:
            messages.error(
                request,
                f"Ambiguous hardware match for '{hardware}': multiple matching mappings/device types found.",
            )
            return _device_sync_redirect(request, pk, server_key)

        if not match_result["matched"]:
            messages.error(
                request,
                f"No matching DeviceType found for hardware '{hardware}'",
            )
            return _device_sync_redirect(request, pk, server_key)

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
            return _device_sync_redirect(request, pk, server_key)

        messages.success(
            request,
            f"Device type updated from '{old_device_type}' to '{device_type}'",
        )

        return _device_sync_redirect(request, pk, server_key)


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

        device = self.restrict_object_or_404(Device, "change", pk=pk)

        # Rebind the API client to the POSTed server before resolving the per-server librenms_id,
        # so a multi-server user acting on a non-default tab isn't routed through the globally
        # selected server (returning the wrong device's id, or none). Mirrors
        # ConvertLegacyLibreNMSIdView.
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return _device_sync_redirect(request, pk, server_key)

        self.librenms_id = self.librenms_api.get_librenms_id(device)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        success, device_info = self.get_live_device_info(self.librenms_id)

        if not success or not device_info:
            messages.error(request, "Failed to retrieve device info from LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        os_name = device_info.get("os")

        if not os_name:
            messages.warning(request, "No OS information available in LibreNMS")
            return _device_sync_redirect(request, pk, server_key)

        result = find_matching_platform(os_name)
        if result["match_type"] == "ambiguous":
            messages.error(
                request,
                "Multiple platforms match '{}'. Please resolve the ambiguity via a Platform Mapping.".format(os_name),
            )
            return _device_sync_redirect(request, pk, server_key)
        if not result["found"] or result["platform"] is None:
            messages.error(
                request,
                "Platform '{}' does not exist in NetBox. Use 'Create & Sync' button to create it first.".format(
                    os_name
                ),
            )
            return _device_sync_redirect(request, pk, server_key)

        platform = result["platform"]

        old_platform = device.platform
        device.platform = platform
        try:
            device.full_clean()
            device.save()
        except (ValidationError, IntegrityError) as e:
            device.platform = old_platform
            error_msg = e.message_dict if hasattr(e, "message_dict") else str(e)
            messages.error(request, f"Failed to update platform to '{platform}': {error_msg}")
            return _device_sync_redirect(request, pk, server_key)

        if old_platform:
            messages.success(
                request,
                f"Device platform updated from '{old_platform}' to '{platform}'",
            )
        else:
            messages.success(request, f"Device platform set to '{platform}'")

        return _device_sync_redirect(request, pk, server_key)


class CreateAndAssignPlatformView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Create a new Platform and assign it to the device."""

    required_object_permissions = {
        "POST": [
            ("change", Device),
            ("add", Platform),
        ],
    }

    def post(self, request, pk):
        """Create a new platform (or reuse an existing one) and assign it to the device."""
        # Read inputs before the permission check so required perms can adapt:
        # reusing an existing platform needs no "add Platform" permission, and the
        # mapping insert is only attempted when the toggle is on.
        create_mapping = bool(request.POST.get("create_mapping"))
        # Normalize early: trailing/leading whitespace must not force the create
        # path (a stray space would miss the case-insensitive existing-name match).
        platform_name = (request.POST.get("platform_name") or "").strip()
        librenms_os = (request.POST.get("librenms_os") or "").strip().lower()

        # If a platform with this name already exists we reuse it as-is (never
        # mutating its manufacturer/vendor scoping) and only fill in the missing
        # mapping. This handles the common case where the platform exists but no
        # LibreNMS-OS mapping points at it, so the regular sync can't match it.
        existing_platform = None
        if platform_name:
            existing_platform = Platform.objects.filter(name__iexact=platform_name).first()

        required = [("change", Device)]
        if existing_platform is None:
            required.append(("add", Platform))
        else:
            required.append(("view", Platform))
        # The manufacturer is optional, but when one IS posted it is resolved by client-supplied
        # id through a restricted queryset — so state that read in the gate.
        if (request.POST.get("manufacturer") or "").strip():
            required.append(("view", Manufacturer))
        # Deliberately do NOT gate the upfront POST on "add PlatformMapping": assigning the
        # platform is the primary action and must succeed for a user who can change the device
        # (and create the platform) even when they can't create OS mappings. The optional
        # mapping write is gated at its own site below, where a missing add-permission skips
        # the mapping with a warning (mapping_skipped_no_perm) instead of failing the whole
        # request. Mirrors CreatePlatformFromImportView.
        self.required_object_permissions = {"POST": required}

        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        if existing_platform is not None:
            existing_platform = self.restricted_queryset(Platform).filter(pk=existing_platform.pk).first()
            if existing_platform is None:
                messages.error(request, "Selected platform is not available.")
                return self._sync_redirect(
                    request,
                    pk,
                    getattr(getattr(self, "_librenms_api", None), "server_key", None),
                )

        device = self.restrict_object_or_404(Device, "change", pk=pk)

        # Rebind the API client to the POSTed server so the server_key fallback in _sync_redirect
        # below resolves to the active server instead of None (self._librenms_api would otherwise
        # stay unset — this view makes no live lookup that would lazily build it). A blank/unknown
        # key leaves self._librenms_api unchanged; _sync_redirect still has request.POST as its
        # primary source, so a bad key can't refuse the platform create (which needs no LibreNMS).
        self.rebind_api_for_server(request.POST.get("server_key"))

        manufacturer_id = (request.POST.get("manufacturer") or "").strip()

        if not platform_name:
            messages.error(request, "Platform name is required")
            return self._sync_redirect(request, pk, getattr(getattr(self, "_librenms_api", None), "server_key", None))

        manufacturer = None
        if manufacturer_id:
            try:
                manufacturer = self.restricted_queryset(Manufacturer).get(pk=manufacturer_id)
            except (Manufacturer.DoesNotExist, ValueError):
                messages.error(request, "Selected manufacturer is not available.")
                return self._sync_redirect(
                    request,
                    pk,
                    getattr(getattr(self, "_librenms_api", None), "server_key", None),
                )

        with transaction.atomic():
            platform_created = False
            if existing_platform is None:
                try:
                    # Nested savepoint so a unique-name collision (concurrent insert)
                    # only rolls back this INSERT, leaving the outer transaction usable
                    # for the re-query below — same pattern as the mapping block.
                    with transaction.atomic():
                        platform = Platform(
                            name=platform_name,
                            slug=slugify(platform_name),
                            manufacturer=manufacturer,
                        )
                        platform.full_clean()
                        platform.save()
                    platform_created = True
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
                    return self._sync_redirect(
                        request, pk, getattr(getattr(self, "_librenms_api", None), "server_key", None)
                    )
                except IntegrityError as e:
                    # A concurrent request created this platform between our existence
                    # check and our save. Reuse the winner (the goal is reuse-or-create)
                    # instead of failing the whole assign; only abort if the re-query
                    # finds nothing (the IntegrityError was for some other reason).
                    winner = Platform.objects.filter(name__iexact=platform_name).first()
                    if winner is None:
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
                        return self._sync_redirect(
                            request, pk, getattr(getattr(self, "_librenms_api", None), "server_key", None)
                        )
                    # Reuse the winner directly. This branch runs only when no platform existed at
                    # preflight, so the gate asked for ("add", Platform) and never ("view",
                    # Platform); a restricted_queryset() read here returns none() for an add-only
                    # user and aborts an assign they are authorized to perform.
                    platform = winner
            else:
                # Reuse the existing platform unchanged — do not touch its
                # manufacturer/vendor scoping; we only assign it and add the mapping.
                platform = existing_platform

            try:
                device = self.restricted_queryset(Device, "change").select_for_update(of=("self",)).get(pk=pk)
            except Device.DoesNotExist:
                transaction.set_rollback(True)
                messages.error(request, "Device no longer exists.")
                return self._sync_redirect(
                    request, pk, getattr(getattr(self, "_librenms_api", None), "server_key", None)
                )

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
                return self._sync_redirect(
                    request, pk, getattr(getattr(self, "_librenms_api", None), "server_key", None)
                )
            try:
                device.save()
            except IntegrityError as e:
                transaction.set_rollback(True)
                logger.exception("IntegrityError saving device pk=%s after platform assignment", pk)
                messages.error(
                    request,
                    f"Error saving device (pk={pk}): {e}",
                )
                return self._sync_redirect(
                    request, pk, getattr(getattr(self, "_librenms_api", None), "server_key", None)
                )

            mapping_created = False
            mapping_error = None
            mapping_existed = False
            mapping_skipped_no_perm = False
            if create_mapping and librenms_os:
                from utilities.permissions import get_permission_for_model

                existing = PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).first()
                if existing is not None and existing.netbox_platform_id == platform.pk:
                    mapping_existed = True
                elif existing is not None:
                    # A mapping for this OS exists but points at a DIFFERENT platform. Treating
                    # it as "already exists" would silently leave future OS-based syncs resolving
                    # to the other platform — surface it instead of claiming success.
                    mapping_error = (
                        f"A LibreNMS-OS mapping for '{librenms_os}' already exists pointing to "
                        f"'{existing.netbox_platform}', not '{platform}'."
                    )
                elif not request.user.has_perm(get_permission_for_model(PlatformMapping, "add")):
                    # Re-check the add permission at the write site. The upfront gate only
                    # requires it when no mapping existed at preflight; if one existed then but
                    # was deleted since, creating here would bypass the permission. Skip rather
                    # than fail — the platform is already assigned and is the primary action.
                    mapping_skipped_no_perm = True
                    logger.warning(
                        "CreateAndAssignPlatformView: skipped PlatformMapping create for OS '%s' — "
                        "user lacks add permission (mapping was removed after the preflight check).",
                        librenms_os,
                    )
                else:
                    try:
                        with transaction.atomic():
                            mapping = PlatformMapping(librenms_os=librenms_os, netbox_platform=platform)
                            mapping.full_clean()
                            mapping.save()
                        mapping_created = True
                    except ValidationError as e:
                        mapping_error = e.message_dict if hasattr(e, "message_dict") else str(e)
                        logger.exception("Failed to create PlatformMapping '%s' -> '%s'", librenms_os, platform_name)
                    except IntegrityError as e:
                        # Only treat this as "already exists" if a row is actually present now AND
                        # points at the requested platform (a concurrent insert of the same
                        # mapping). A row for a DIFFERENT platform, or an unrelated IntegrityError
                        # with no row, must be surfaced — not reported as a successful mapping.
                        existing = PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).first()
                        if existing is not None and existing.netbox_platform_id == platform.pk:
                            mapping_existed = True
                            logger.warning(
                                "IntegrityError creating PlatformMapping '%s' -> '%s'; mapping present after "
                                "re-check, treating as concurrent insert",
                                librenms_os,
                                platform_name,
                            )
                        elif existing is not None:
                            mapping_error = (
                                f"A LibreNMS-OS mapping for '{librenms_os}' already exists pointing to "
                                f"'{existing.netbox_platform}', not '{platform}'."
                            )
                        else:
                            mapping_error = str(e)
                            logger.exception(
                                "IntegrityError creating PlatformMapping '%s' -> '%s' with no existing row",
                                librenms_os,
                                platform_name,
                            )

        if platform_created:
            msg = f"Created platform '{platform}' and assigned to device"
        else:
            msg = f"Platform '{platform}' already existed and was assigned to device"
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
        elif mapping_skipped_no_perm:
            messages.warning(
                request,
                f"Platform mapping for '{librenms_os}' was not created — you lack permission to add mappings.",
            )

        return self._sync_redirect(request, pk, getattr(getattr(self, "_librenms_api", None), "server_key", None))

    @staticmethod
    def _sync_redirect(request, pk, fallback_server_key=None):
        """
        Redirect to the device sync tab, preserving the POST-scoped server_key.

        A multi-server user returns to the same server tab instead of the default
        one. When the form omits server_key, fall back to *fallback_server_key* (the
        active API server) so the redirect doesn't drop a non-default server context
        the action actually ran against.

        The server_key is reflected only when it matches a configured server, and the
        final redirect is gated by Django's ``url_has_allowed_host_and_scheme`` with
        the sink inside the validated branch — the open-redirect barrier CodeQL
        recognises for py/url-redirection (CWE-601). Mirrors
        :func:`mixins._safe_redirect_response`.

        Args:
            request: The current HTTP request (source of the ``server_key`` POST).
            pk: The device primary key the sync tab is reversed for.
            fallback_server_key: The active API server key used when the form omits
                ``server_key``.

        Returns:
            HttpResponseRedirect: A redirect to the sync tab, with the validated
                ``server_key`` query param when one matches a configured server.
        """
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": pk})
        requested = (request.POST.get("server_key") or "").strip() or (fallback_server_key or "").strip()
        # Re-source the matched key from the trusted config rather than echoing the raw request
        # value; the shared helper then gates the redirect on url_has_allowed_host_and_scheme.
        server_key = resolve_configured_server_key(requested)
        return redirect_with_server_key(request, url, server_key)


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

        device = self.restrict_object_or_404(Device, "change", pk=pk)

        if not device.virtual_chassis:
            messages.error(request, "Device is not part of a virtual chassis")
            return redirect("plugins:netbox_librenms_plugin:device_librenms_sync", pk=pk)

        assignments_made = 0
        errors = []

        counter = 1
        while f"serial_{counter}" in request.POST:
            serial = normalize_serial(request.POST.get(f"serial_{counter}"))
            member_id = request.POST.get(f"member_id_{counter}")

            if not member_id:
                counter += 1
                continue

            try:
                # Scoped like the page device: the member's serial is overwritten below and its pk
                # comes from the POST, so the same-VC check alone would let a constrained grant
                # write a serial onto a member it does not cover.
                member = self.restricted_queryset(Device, "change").get(pk=member_id)

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
        model = _sync_model(object_type)
        return self.restrict_object_or_404(model, "change", pk=pk), model

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
        raw_object_type = request.POST.get("object_type", "device")
        object_type = _normalize_sync_object_type(raw_object_type)
        if object_type is None:
            return HttpResponse(f"Invalid object_type: {escape(raw_object_type)}", status=400)
        target_model = _sync_model(object_type)
        self.required_object_permissions = {"POST": [("change", target_model)]}

        if error := self.require_all_permissions("POST"):
            return error

        obj, model = self._get_object(object_type, pk)
        sync_url = _sync_url_name(object_type)
        server_key = request.POST.get("server_key", "").strip()

        if not server_key:
            messages.error(request, "No server_key provided.")
            return redirect(sync_url, pk=pk)
        try:
            server_key = require_server_key(server_key)
        except ValueError as exc:
            messages.error(request, str(exc))
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
                obj_locked = self.restricted_queryset(model, "change").select_for_update(of=("self",)).get(pk=pk)
            except model.DoesNotExist:
                messages.error(request, f"{model.__name__} no longer exists.")
                return redirect(sync_url, pk=pk)
            cf = self._normalize_librenms_mapping(obj_locked.custom_field_data.get("librenms_id"))
            # Re-check after acquiring lock; mirror the pre-transaction protection logic
            _is_protected = server_key in configured_servers or (
                legacy_url_configured and not configured_servers and server_key == "default"
            )
            if isinstance(cf, dict) and server_key in cf and not _is_protected:
                cf = without_server_mapping(cf, server_key)
                obj_locked.custom_field_data["librenms_id"] = cf
                usable_count = sum(mapping.is_selectable for mapping in build_server_mappings(obj_locked))
                if usable_count <= 1:
                    cf.pop(PREFERRED_SERVER_FIELD, None)
                obj_locked.custom_field_data["librenms_id"] = cf if any(iter_server_mapping_entries(cf)) else None
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


class SetPreferredServerView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Store the preferred server on a Device or VirtualMachine mapping owner."""

    required_object_permissions = {
        "POST": [("change", Device), ("change", VirtualMachine)],
    }

    def _redirect(self, object_type, pk, active_server_key=None, active_sync_tab=None):
        url = reverse(_sync_url_name(object_type), kwargs={"pk": pk})
        query = {}
        if active_sync_tab:
            query["tab"] = active_sync_tab
        if active_server_key:
            query["server_key"] = active_server_key
        if query:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(query)}"
        return redirect(url)

    def post(self, request, pk):
        raw_object_type = request.POST.get("object_type", "device")
        object_type = _normalize_sync_object_type(raw_object_type)
        if object_type is None:
            return HttpResponse(f"Invalid object_type: {escape(raw_object_type)}", status=400)

        model = _sync_model(object_type)
        self.required_object_permissions = {"POST": [("change", model)]}
        if error := self.require_all_permissions("POST"):
            return error

        submitted_tab = request.POST.get("tab")
        active_sync_tab = submitted_tab if submitted_tab in {tab.value for tab in SyncTab} else None
        try:
            requested_key = require_server_key(request.POST.get("server_key", ""))
        except ValueError as exc:
            messages.error(request, str(exc))
            return self._redirect(object_type, pk, active_sync_tab=active_sync_tab)
        submitted_active_key = request.POST.get("active_server_key", "").strip() or None

        with transaction.atomic():
            owner = get_object_or_404(
                self.restricted_queryset(model, "change").select_for_update(of=("self",)),
                pk=pk,
            )
            raw_mapping = owner.custom_field_data.get("librenms_id")
            mappings = build_server_mappings(owner)
            selectable_keys = {mapping.server_key for mapping in mappings if mapping.is_selectable}
            active_server_key = submitted_active_key if submitted_active_key in selectable_keys else None
            if len(selectable_keys) <= 1:
                messages.error(request, "A preferred server requires at least two usable object mappings.")
                return self._redirect(object_type, pk, active_server_key, active_sync_tab)
            if requested_key not in selectable_keys:
                messages.error(request, "The preferred server is not a usable mapping for this object.")
                return self._redirect(object_type, pk, active_server_key, active_sync_tab)

            owner.custom_field_data["librenms_id"] = with_preferred_server(raw_mapping, requested_key)
            try:
                owner.full_clean()
                owner.save(update_fields=["custom_field_data"])
            except ValidationError as exc:
                transaction.set_rollback(True)
                error = exc.message_dict if hasattr(exc, "message_dict") else str(exc)
                messages.error(request, f"Validation error changing preferred server: {error}")
                return self._redirect(object_type, pk, active_server_key, active_sync_tab)

        messages.success(request, f"Preferred LibreNMS server changed to '{requested_key}'.")
        return self._redirect(object_type, pk, active_server_key, active_sync_tab)


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
        model = _sync_model(object_type)
        return model, self.restrict_object_or_404(model, "change", pk=pk)

    def _sync_url(self, object_type, pk):
        url = reverse(_sync_url_name(object_type), kwargs={"pk": pk})
        # Propagate the active multi-server server_key so redirects land on the server the user was
        # working in. Source it here (re-match against the trusted config, with a bound-API
        # fallback), then delegate the redirect to the shared redirect_with_server_key — like the
        # sibling _sync_redirect / _failure_redirect — so the open-redirect barrier
        # (url_has_allowed_host_and_scheme, sink inside the validated branch — CWE-601) lives in one
        # place and can't drift between the two server_key-preserving redirects.
        request = getattr(self, "request", None)
        if request is None:
            return redirect(url)
        requested = (request.POST.get("server_key") or request.GET.get("server_key") or "").strip()
        # Re-source the matched key from the trusted config rather than echoing the raw request
        # value. A stale/unconfigured POST key (server removed since the page loaded, or tampered)
        # resolves to None here — so it must NOT short-circuit the fallback below.
        server_key = resolve_configured_server_key(requested)
        if not server_key:
            # POST omitted server_key OR sent an unconfigured one — fall back to the active API
            # server the action ran against so a non-default-server user isn't dropped onto the
            # default tab. Prefer the already-bound _librenms_api; when nothing is bound (e.g. a
            # fail-closed rebind returned None on a missing/misconfigured default) leave server_key
            # unset so the redirect lands on the bare sync tab, rather than re-running the lazy
            # self.librenms_api construction the rebind deliberately avoided (it can raise, or
            # silently resolve to a different first-configured server → wrong tab).
            bound = getattr(self, "_librenms_api", None)
            fallback = (getattr(bound, "server_key", "") or "").strip() if bound is not None else ""
            server_key = resolve_configured_server_key(fallback)
        return redirect_with_server_key(request, url, server_key)

    def post(self, request, pk):
        raw_object_type = request.POST.get("object_type", "device")
        object_type = _normalize_sync_object_type(raw_object_type)
        if object_type is None:
            return HttpResponse(f"Invalid object_type: {escape(raw_object_type)}", status=400)

        target_model = _sync_model(object_type)
        self.required_object_permissions = {"POST": [("change", target_model)]}
        if error := self.require_all_permissions("POST"):
            return error

        model, obj = self._get_model_and_object(object_type, pk)
        # Rebind the API client to the POST-scoped server before any lookup/migration so the
        # legacy-ID conversion is verified (get_device_info), conflict-checked
        # (find_by_librenms_id) and written (migrate_legacy_librenms_id) under the same server
        # namespace the user is acting on — otherwise a multi-server page could check server A
        # while redirecting back to server B and write the mapping under the wrong key.
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return self._sync_url(object_type, pk)

        # Verify the device actually has a legacy bare-int librenms_id
        cf_value = obj.custom_field_data.get("librenms_id")
        if isinstance(cf_value, bool):
            messages.error(request, "librenms_id has an invalid boolean value; cannot convert.")
            return self._sync_url(object_type, pk)
        if not isinstance(cf_value, (int, str)):
            messages.warning(request, "librenms_id is already in the server-scoped JSON format.")
            return self._sync_url(object_type, pk)
        # Gate with is_legacy_librenms_id() (not str.isdigit()) so this handler accepts exactly the
        # values the "Convert ID" badge shows — a whitespace-padded legacy int (" 42 ") is legacy
        # via int() coercion, so it must not be a dead-end button (issue #99).
        if not is_legacy_librenms_id(cf_value):
            messages.error(request, "librenms_id is not a valid integer; cannot convert.")
            return self._sync_url(object_type, pk)
        librenms_id = int(cf_value)

        # Verify serial match before converting — get_live_device_info reads live (uncached): the
        # serial gate decides whether to rewrite the id, so it must not read a stale sync-tab snapshot.
        success, device_info = self.get_live_device_info(librenms_id)
        if not success or not device_info:
            messages.error(request, "Could not retrieve device info from LibreNMS to verify serial.")
            return self._sync_url(object_type, pk)

        librenms_serial = normalize_serial(device_info.get("serial"))
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
                locked = self.restricted_queryset(model, "change").select_for_update(of=("self",)).get(pk=pk)
            except model.DoesNotExist:
                messages.error(request, f"{model.__name__} no longer exists.")
                return self._sync_url(object_type, pk)
            # Re-check preconditions on the locked row (another admin may have
            # changed cf_value or serial between the initial read and the lock).
            locked_cf = locked.custom_field_data.get("librenms_id")
            if not isinstance(locked_cf, (int, str)) or isinstance(locked_cf, bool):
                messages.warning(request, "librenms_id is already in the server-scoped JSON format.")
                return self._sync_url(object_type, pk)
            if not is_legacy_librenms_id(locked_cf):
                messages.error(request, "librenms_id changed before lock was acquired; aborting.")
                return self._sync_url(object_type, pk)
            locked_id = int(locked_cf)
            locked_serial = (getattr(locked, "serial", None) or "").strip()
            if locked_id != librenms_id or locked_serial != netbox_serial:
                messages.error(request, "Device data changed before lock was acquired; aborting conversion.")
                return self._sync_url(object_type, pk)
            # Check that no other object already owns this ID (server-scoped or legacy)
            try:
                match = find_by_librenms_id(model, librenms_id, server_key)
            except AmbiguousLibreNMSIdError:
                transaction.set_rollback(True)
                messages.error(
                    request,
                    f"librenms_id {librenms_id} is ambiguous — it matches more than one "
                    f"{model.__name__}; cannot convert. Resolve the duplicate assignment first.",
                )
                return self._sync_url(object_type, pk)
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
