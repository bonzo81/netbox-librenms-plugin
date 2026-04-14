"""HTMX endpoints and POST handlers for importing LibreNMS devices."""

import json
import logging
from urllib.parse import parse_qs, urlparse

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils.html import escape
from django.views import View

from netbox_librenms_plugin.import_utils import (
    _determine_device_name,
    bulk_import_devices,
    bulk_import_vms,
    fetch_device_with_cache,
    get_import_device_cache_key,
    get_librenms_device_by_id,
    get_virtual_chassis_data,
    update_vc_member_suggested_names,
    validate_device_for_import,
)
from netbox_librenms_plugin.import_validation_helpers import (
    apply_cluster_to_validation,
    apply_rack_to_validation,
    apply_role_to_validation,
    extract_device_selections,
    fetch_model_by_id,
)
from netbox_librenms_plugin.tables.device_status import DeviceImportTable
from netbox_librenms_plugin.utils import resolve_naming_preferences, save_user_pref, set_librenms_device_id
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin

logger = logging.getLogger(__name__)

# Actions that require the force checkbox when a device-type mismatch is detected.
_FORCE_REQUIRED_ACTIONS = frozenset({"link", "update", "update_serial", "update_type"})

# Actions that operate on Device-only fields and cannot be applied to VMs.
_DEVICE_ONLY_ACTIONS = frozenset({"link", "update", "update_serial", "update_type", "sync_serial", "sync_device_type"})


_TRUTHY_VALUES = {"1", "true", "on", "yes"}
_FALSY_VALUES = {"0", "false", "off", "no", ""}


def _parse_boolish(value) -> bool | None:
    """Parse common form/query boolean values. Return None when value is unset/unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()
    if normalized in _TRUTHY_VALUES:
        return True
    if normalized in _FALSY_VALUES:
        return False
    return None


def _resolve_vc_detection_enabled(request) -> bool:
    """
    Resolve VC detection preference from request payloads.

    Resolution order:
    1. Explicit POST enable_vc_detection
    2. Explicit GET enable_vc_detection
    3. return_url query param fallback (POST, then GET)
    4. Default False
    """
    for source in (request.POST, request.GET):
        parsed = _parse_boolish(source.get("enable_vc_detection"))
        if parsed is not None:
            return parsed

    for source in (request.POST, request.GET):
        return_url = source.get("return_url")
        if not return_url:
            continue
        query = parse_qs(urlparse(return_url).query)

        parsed = _parse_boolish((query.get("enable_vc_detection") or [None])[-1])
        if parsed is not None:
            return parsed

        # Backward compatibility for legacy URLs that used skip_vc_detection.
        skip_vc = _parse_boolish((query.get("skip_vc_detection") or [None])[-1])
        if skip_vc is not None:
            return not skip_vc

    return False


def _save_device(device) -> HttpResponse | None:
    """Call full_clean() then save(). Return an HttpResponse on failure, None on success."""
    from django.db import IntegrityError

    try:
        device.full_clean()
    except ValidationError as exc:
        error_msg = exc.message_dict if hasattr(exc, "message_dict") else str(exc)
        return HttpResponse(f"Validation error: {escape(str(error_msg))}", status=400)
    try:
        device.save()
    except IntegrityError as exc:
        return HttpResponse(f"Integrity error: {escape(str(exc))}", status=409)
    return None


def _get_hostname_for_action(request, validation: dict, libre_device: dict) -> str:
    """
    Return the resolved hostname to use when updating a device during a conflict action.

    Prefer the cached ``resolved_name`` from validation (already computed with the
    user's naming prefs at validation time). Fall back to computing it fresh from
    the current request's naming preferences.
    """
    resolved = validation.get("resolved_name")
    if resolved:
        return resolved
    use_sysname, strip_domain = resolve_naming_preferences(request)
    return _determine_device_name(libre_device, use_sysname=use_sysname, strip_domain=strip_domain)


class DeviceImportHelperMixin:
    """Mixin providing common validation and rendering helpers for device import views."""

    def _should_enable_vc_detection(self, device_id: int, request) -> bool:
        """
        Determine if VC detection should be enabled for this request.

        VC detection is always enabled for role/rack changes and detail views,
        regardless of the initial user preference. This implements smart caching:

        1. If user originally requested VC detection: Uses cached data from initial load
        2. If VC data is already cached: Reuses cached data (no API call)
        3. Otherwise: Fetches VC data from LibreNMS API and caches it

        This approach ensures:
        - Role/rack changes always have VC context available (required for import)
        - No redundant API calls when VC data is already cached
        - Consistent VC detection behavior across dropdowns and detail modals
        - Since role assignment is required before import, VC data is always
          available by the time bulk import/confirm operations run

        Args:
            device_id: LibreNMS device ID
            request: Django request object

        Returns:
            bool: Always returns True to enable VC detection with smart caching
        """
        # Check if user originally requested VC detection
        vc_requested = _resolve_vc_detection_enabled(request)

        if vc_requested:
            # User explicitly enabled it - use it (will use cache if available)
            return True

        # Check if VC data is already cached (no API call will be made)
        from netbox_librenms_plugin.import_utils import _vc_cache_key

        cache_key = _vc_cache_key(self.librenms_api, device_id)
        vc_cached = cache.get(cache_key) is not None

        if vc_cached:
            # Data already in cache - enable detection (no API call)
            return True

        # Not requested and not cached - make API call to get VC data
        # This handles the case where user didn't initially request it
        # but is now changing role/rack (so we fetch it now)
        return True

    def get_validated_device_with_selections(self, device_id: int, request) -> tuple[dict | None, dict | None, dict]:
        """
        Get LibreNMS device, validate it, and apply user selections.

        Consolidates the common pattern across all device import update views.

        Args:
            device_id: LibreNMS device ID
            request: Django request object

        Returns:
            Tuple of (libre_device, validation, selections)
            Returns (None, None, selections) if device not found
        """
        selections = extract_device_selections(request, device_id)
        cluster_id = selections["cluster_id"]
        is_vm = bool(cluster_id)

        # Try to use cached device data from table load (eliminates redundant API calls)
        libre_device = fetch_device_with_cache(device_id, self.librenms_api)

        if not libre_device:
            return None, None, selections

        # Determine if we should enable VC detection for this request
        # This checks: user preference, cache status, and VM vs Device
        enable_vc = not is_vm and self._should_enable_vc_detection(device_id, request)

        # Extract naming preferences: POST data (hx-include) → user pref → plugin settings.
        use_sysname, strip_domain = resolve_naming_preferences(request)

        validation = validate_device_for_import(
            libre_device,
            import_as_vm=is_vm,
            api=self.librenms_api if enable_vc else None,
            include_vc_detection=enable_vc,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
            server_key=self.librenms_api.server_key,
        )
        # Recompute is_vm from validate_device_for_import's own detection
        # (it may have found an existing VM via hostname/IP lookup)
        is_vm = bool(validation.get("import_as_vm"))

        # Apply user selections (cluster, role, rack) to validation
        _apply_user_selections_to_validation(validation, selections, is_vm)

        return libre_device, validation, selections

    def render_device_row(self, request, libre_device: dict, validation: dict, selections: dict):
        """
        Render device import table row with updated validation.

        Args:
            request: Django request object
            libre_device: LibreNMS device data
            validation: Updated validation dict
            selections: User selections dict with cluster_id, role_id, rack_id

        Returns:
            HttpResponse with rendered device row
        """
        libre_device["_validation"] = validation
        table = DeviceImportTable([libre_device])

        context = {
            "record": libre_device,
            "table": table,
            "cluster_id": selections["cluster_id"],
            "role_id": selections["role_id"],
            "rack_id": selections["rack_id"],
        }

        return render(
            request,
            "netbox_librenms_plugin/htmx/device_import_row.html",
            context,
        )


def _apply_user_selections_to_validation(
    validation: dict,
    selections: dict,
    is_vm: bool,
) -> None:
    """
    Apply user-selected cluster, role, and rack to validation dict.

    This helper consolidates the logic shared across DeviceValidationDetailsView,
    DeviceRoleUpdateView, DeviceClusterUpdateView, and DeviceRackUpdateView.

    Args:
        validation: Validation dict from validate_device_for_import()
        selections: Dict with keys: cluster_id, role_id, rack_id
        is_vm: True if importing as VM, False for device

    Modifies validation dict in-place by applying cluster/role/rack selections.
    """
    from dcim.models import DeviceRole, Rack
    from virtualization.models import Cluster

    cluster_id = selections.get("cluster_id")
    role_id = selections.get("role_id")
    rack_id = selections.get("rack_id")

    if is_vm:
        # Handle cluster selection (VM only)
        if cluster_id:
            cluster = fetch_model_by_id(Cluster, cluster_id)
            if cluster:
                apply_cluster_to_validation(validation, cluster)

        # Handle role selection for VM
        if role_id:
            role = fetch_model_by_id(DeviceRole, role_id)
            if role:
                apply_role_to_validation(validation, role, is_vm=True)
    else:
        # Handle role selection for device
        if role_id:
            role = fetch_model_by_id(DeviceRole, role_id)
            if role:
                apply_role_to_validation(validation, role, is_vm=False)

        # Handle rack selection (device only, optional)
        if rack_id:
            rack = fetch_model_by_id(Rack, rack_id)
            if rack:
                apply_rack_to_validation(validation, rack)


class BulkImportConfirmView(LibreNMSPermissionMixin, LibreNMSAPIMixin, View):
    """HTMX view to confirm bulk imports before execution."""

    def post(self, request):
        """Render a confirmation modal for selected devices before bulk import."""
        # Check write permission before showing import confirmation
        if error := self.require_write_permission():
            return error

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            from netbox_librenms_plugin.librenms_api import LibreNMSAPI

            self._librenms_api = LibreNMSAPI(server_key=post_server_key)

        device_ids = request.POST.getlist("select")
        if not device_ids:
            return HttpResponse(
                '<div class="alert alert-warning mb-0">Select at least one device.</div>',
                status=400,
            )

        use_sysname, strip_domain = resolve_naming_preferences(request)
        vc_detection_enabled = _resolve_vc_detection_enabled(request)

        devices = []
        errors = []
        seen_ids = set()
        cache_expired_count = 0

        for raw_device_id in device_ids:
            try:
                device_id = int(raw_device_id)
            except (TypeError, ValueError):
                errors.append(f"Invalid device identifier: {raw_device_id}")
                continue

            if device_id in seen_ids:
                continue
            seen_ids.add(device_id)

            # Try to use cached device data from table load or role changes
            libre_device = fetch_device_with_cache(device_id, self.librenms_api)
            from_cache = libre_device is not None

            if not from_cache:
                cache_expired_count += 1

            if not libre_device:
                errors.append(f"Device ID {device_id} not found in LibreNMS")
                continue

            selections = extract_device_selections(request, device_id)
            cluster_id = selections["cluster_id"]
            role_id = selections["role_id"]
            rack_id = selections["rack_id"]
            is_vm = bool(cluster_id)

            validation = validate_device_for_import(
                libre_device,
                import_as_vm=is_vm,
                api=self.librenms_api,
                use_sysname=use_sysname,
                strip_domain=strip_domain,
                server_key=self.librenms_api.server_key,
                include_vc_detection=vc_detection_enabled,
            )
            # Recompute is_vm from validation result — the function may have
            # detected an existing VM via hostname/IP lookup
            is_vm = bool(validation.get("import_as_vm"))

            # Mark validation with VC detection flag for proper URL generation in table
            # Bulk confirm should respect the initial filter's VC detection preference
            validation["_vc_detection_enabled"] = vc_detection_enabled

            device_name = validation.get("resolved_name") or f"device-{device_id}"

            if validation.get("virtual_chassis", {}).get("is_stack") and device_name:
                validation["virtual_chassis"] = update_vc_member_suggested_names(
                    validation["virtual_chassis"], device_name
                )

            from dcim.models import DeviceRole, Rack
            from virtualization.models import Cluster

            role = fetch_model_by_id(DeviceRole, role_id) if role_id else None
            cluster = fetch_model_by_id(Cluster, cluster_id) if cluster_id else None
            rack = fetch_model_by_id(Rack, rack_id) if rack_id else None

            if is_vm:
                if cluster:
                    apply_cluster_to_validation(validation, cluster)

                if role:
                    apply_role_to_validation(validation, role, is_vm=True)
            else:
                if role:
                    apply_role_to_validation(validation, role, is_vm=False)

                if rack:
                    apply_rack_to_validation(validation, rack)

            devices.append(
                {
                    "device_id": device_id,
                    "device_name": device_name,
                    "validation": validation,
                    "role": role,
                    "cluster": cluster,
                    "rack": rack,
                    "is_vm": is_vm,
                }
            )

        if not devices:
            # Check if this is due to cache expiration
            if cache_expired_count > 0 and cache_expired_count == len(seen_ids):
                return HttpResponse(
                    '<div class="alert alert-warning mb-0">'
                    '<i class="mdi mdi-clock-alert"></i> '
                    "<strong>Filter results have expired.</strong><br>"
                    "The device data is no longer available in cache (5-minute timeout). "
                    'Please <a href="javascript:window.location.reload();" class="alert-link">refresh the page</a> '
                    "or re-run your filter to reload device data."
                    "</div>",
                    status=400,
                )
            elif cache_expired_count > 0:
                # Partial expiration - some devices lost their selections
                return HttpResponse(
                    '<div class="alert alert-warning mb-0">'
                    '<i class="mdi mdi-clock-alert"></i> '
                    f"<strong>Some device data has expired.</strong><br>"
                    f"{cache_expired_count} of {len(seen_ids)} selected devices had expired cache data and may be missing role/rack selections. "
                    'Please <a href="javascript:window.location.reload();" class="alert-link">refresh the page</a> '
                    "or re-run your filter to reload device data."
                    "</div>",
                    status=400,
                )
            else:
                # Generic error - validation failed for all devices
                return HttpResponse(
                    '<div class="alert alert-danger mb-0">'
                    "No valid devices selected. "
                    f"{len(errors)} error(s) occurred: {' '.join(errors) if errors else 'Please check device validation status.'}"
                    "</div>",
                    status=400,
                )

        context = {
            "devices": devices,
            "device_count": len(devices),
            "errors": errors,
            "use_sysname": use_sysname,
            "strip_domain": strip_domain,
            "server_key": self.librenms_api.server_key,
            "vc_detection_enabled": vc_detection_enabled,
        }

        return render(
            request,
            "netbox_librenms_plugin/htmx/bulk_import_confirm.html",
            context,
        )


class BulkImportDevicesView(LibreNMSPermissionMixin, LibreNMSAPIMixin, View):
    """Handle bulk import requests coming from the LibreNMS import table."""

    def should_use_background_job_for_import(self, request):
        """
        Determine if import operation should run as background job.

        Import jobs provide active cancellation and keep the browser responsive
        during bulk imports.

        Note: Non-superusers automatically fall back to synchronous mode because
        the /api/core/background-tasks/ endpoint requires superuser access.

        Args:
            request: Django request object containing POST data

        Returns:
            bool: True if background job should be used, False for synchronous
        """
        # Non-superusers cannot poll background-tasks API (requires IsSuperuser)
        if not request.user.is_superuser:
            return False
        return request.POST.get("use_background_job") == "on"

    def post(self, request):  # noqa: PLR0912 - branching keeps responses explicit
        """Import selected devices from LibreNMS into NetBox."""
        # Check write permission before any import operation
        if error := self.require_write_permission():
            return error

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            from netbox_librenms_plugin.librenms_api import LibreNMSAPI

            self._librenms_api = LibreNMSAPI(server_key=post_server_key)

        device_ids = request.POST.getlist("select")
        if not device_ids:
            messages.error(request, "No devices selected for import")
            return HttpResponse("No devices selected", status=400)

        try:
            parsed_ids = [int(device_id) for device_id in device_ids]
        except (TypeError, ValueError):
            messages.error(request, "Invalid device identifier supplied")
            return HttpResponse("Invalid device identifier", status=400)

        use_sysname, strip_domain = resolve_naming_preferences(request)
        vc_detection_enabled = _resolve_vc_detection_enabled(request)
        sync_options = {
            "sync_interfaces": request.POST.get("sync_interfaces") == "on",
            "sync_cables": request.POST.get("sync_cables") == "on",
            "sync_ips": request.POST.get("sync_ips") == "on",
            "vc_detection_enabled": vc_detection_enabled,
            "use_sysname": use_sysname,
            "strip_domain": strip_domain,
        }

        manual_mappings_per_device: dict[int, dict[str, int]] = {}
        vm_imports: dict[int, dict[str, int]] = {}  # Track which devices to import as VMs

        for device_id in parsed_ids:
            mappings = {}
            cluster_value = request.POST.get(f"cluster_{device_id}")

            # If cluster is selected, this is a VM import
            if cluster_value:
                try:
                    vm_imports[device_id] = {"cluster_id": int(cluster_value)}
                    # VMs can also have roles
                    role_value = request.POST.get(f"role_{device_id}")
                    if role_value:
                        vm_imports[device_id]["device_role_id"] = int(role_value)
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring invalid cluster/role id for VM import of device %s",
                        device_id,
                    )
                continue  # Skip device-specific mappings for VMs

            # Device import mappings
            role_value = request.POST.get(f"role_{device_id}")
            if role_value:
                try:
                    mappings["device_role_id"] = int(role_value)
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring invalid role id '%s' for device %s",
                        role_value,
                        device_id,
                    )

            rack_value = request.POST.get(f"rack_{device_id}")
            if rack_value:
                try:
                    mappings["rack_id"] = int(rack_value)
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring invalid rack id '%s' for device %s",
                        rack_value,
                        device_id,
                    )

            if mappings:
                manual_mappings_per_device[device_id] = mappings

        # Separate device IDs into device imports vs VM imports
        device_ids_to_import = [d for d in parsed_ids if d not in vm_imports]
        vm_ids_to_import = list(vm_imports.keys())

        # Build cache of already-fetched device data to avoid redundant API calls
        libre_devices_cache = {}
        for device_id in parsed_ids:
            cached_device = fetch_device_with_cache(device_id, self.librenms_api)
            if cached_device:
                libre_devices_cache[device_id] = cached_device

        # Check if we should use background job for import
        total_import_count = len(parsed_ids)

        # Decide whether to use background job
        if self.should_use_background_job_for_import(request):
            # Check if RQ workers are available
            from utilities.rqworker import get_workers_for_queue

            if get_workers_for_queue("default") > 0:
                from netbox_librenms_plugin.jobs import ImportDevicesJob

                # Enqueue background job
                job = ImportDevicesJob.enqueue(
                    user=request.user,
                    device_ids=device_ids_to_import,
                    vm_imports=vm_imports,
                    server_key=self.librenms_api.server_key,
                    sync_options=sync_options,
                    manual_mappings_per_device=manual_mappings_per_device,
                    libre_devices_cache=libre_devices_cache,
                )

                logger.info(
                    f"Enqueued ImportDevicesJob {job.pk} (UUID: {job.job_id}) for user {request.user} - {total_import_count} devices/VMs"
                )

                # Show notification and redirect - matching NetBox's native pattern
                from django.utils.safestring import mark_safe

                messages.info(
                    request,
                    mark_safe(
                        f"Import job started for {total_import_count} device{'s' if total_import_count != 1 else ''}. "
                        f'You can monitor progress in the <a href="/core/jobs/{job.pk}/">Jobs interface</a>.'
                    ),
                )

                if request.headers.get("HX-Request"):
                    # For HTMX requests, redirect to clean import page (no filters)
                    # This matches the "Clear" button behavior
                    return HttpResponse(
                        "",
                        headers={"HX-Redirect": "/plugins/librenms_plugin/librenms-import/"},
                    )
                else:
                    return redirect("plugins:netbox_librenms_plugin:librenms_import")
            else:
                # No workers available - warn user and proceed synchronously
                logger.warning("No RQ workers available for import job, falling back to synchronous import")
                messages.warning(
                    request,
                    f"Background job requested but no workers available. Importing {total_import_count} devices synchronously...",
                )

        # Synchronous import execution
        # Build cache of already-fetched device data to avoid redundant API calls
        libre_devices_cache_sync = {}
        for device_id in parsed_ids:
            cached_device = fetch_device_with_cache(device_id, self.librenms_api)
            if cached_device:
                libre_devices_cache_sync[device_id] = cached_device

        # Import devices and VMs separately
        device_result = {
            "success": [],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
        }
        vm_result = {"success": [], "failed": [], "skipped": []}

        try:
            # Import devices if any
            if device_ids_to_import:
                device_result = bulk_import_devices(
                    device_ids=device_ids_to_import,
                    server_key=self.librenms_api.server_key,
                    sync_options=sync_options,
                    manual_mappings_per_device=manual_mappings_per_device,  # type: ignore
                    libre_devices_cache=libre_devices_cache_sync,
                    user=request.user,  # Pass user for permission checks
                )

            # Import VMs if any
            if vm_ids_to_import:
                vm_result = bulk_import_vms(
                    vm_imports,
                    self.librenms_api,
                    sync_options,
                    libre_devices_cache_sync,
                    user=request.user,  # Pass user for permission checks
                )

        except PermissionDenied as exc:
            # Handle permission errors with a user-friendly message
            logger.warning(f"Permission denied during import: {exc}")
            messages.error(request, str(exc))
            if request.headers.get("HX-Request"):
                return HttpResponse(
                    "",
                    headers={"HX-Redirect": "/plugins/librenms_plugin/librenms-import/"},
                )
            return redirect("plugins:netbox_librenms_plugin:librenms_import")

        except Exception as exc:  # pragma: no cover - defensive guard
            logger.exception("Error during bulk import")
            if request.headers.get("HX-Request"):
                return HttpResponse(str(exc), status=500)
            messages.error(request, f"Bulk import failed: {exc}")
            return redirect("plugins:netbox_librenms_plugin:librenms_import")

        # Combine results
        success_count = len(device_result.get("success", [])) + len(vm_result.get("success", []))
        failed_count = len(device_result.get("failed", [])) + len(vm_result.get("failed", []))
        skipped_count = len(device_result.get("skipped", [])) + len(vm_result.get("skipped", []))

        if success_count:
            messages.success(
                request,
                f"Successfully imported {success_count} LibreNMS device{'s' if success_count != 1 else ''}",
            )
        if failed_count:
            messages.error(
                request,
                f"Failed to import {failed_count} device{'s' if failed_count != 1 else ''}",
            )
        if skipped_count:
            messages.warning(
                request,
                f"Skipped {skipped_count} existing device{'s' if skipped_count != 1 else ''}",
            )

        if request.headers.get("HX-Request"):
            # Return updated rows for all imported devices using HTMX OOB swaps
            # This updates only the affected rows instead of refreshing the entire table
            updated_rows_html = []

            # Collect all successfully imported device IDs (devices + VMs)
            imported_device_ids = [item["device_id"] for item in device_result.get("success", [])] + [
                item["device_id"] for item in vm_result.get("success", [])
            ]

            # Re-validate and render each imported device with fresh status
            for device_id in imported_device_ids:
                # Fetch device from cache or API
                libre_device = fetch_device_with_cache(
                    device_id,
                    self.librenms_api,
                    libre_devices_cache=libre_devices_cache_sync,
                )

                if libre_device:
                    # Determine if this was imported as VM or device
                    is_vm = device_id in [item["device_id"] for item in vm_result.get("success", [])]

                    # Re-validate with fresh status (will now show as imported)
                    # Pass naming preferences so name comparison uses the same
                    # resolved name the device was imported with.
                    validation = validate_device_for_import(
                        libre_device,
                        import_as_vm=is_vm,
                        api=None,  # No VC detection needed for already-imported devices
                        include_vc_detection=False,
                        server_key=self.librenms_api.server_key,
                        use_sysname=sync_options.get("use_sysname", True),
                        strip_domain=sync_options.get("strip_domain", False),
                    )
                    validation["import_as_vm"] = is_vm

                    # Update cache with fresh validation
                    libre_device["_validation"] = validation
                    cache_key = get_import_device_cache_key(device_id, self.librenms_api.server_key)
                    cache.set(cache_key, libre_device, self.librenms_api.cache_timeout)

                    # Render updated row
                    table = DeviceImportTable([libre_device])
                    context = {
                        "record": libre_device,
                        "table": table,
                        "cluster_id": None,
                        "role_id": None,
                        "rack_id": None,
                    }

                    row_html = render(
                        request,
                        "netbox_librenms_plugin/htmx/device_import_row.html",
                        context,
                    ).content.decode("utf-8")
                    updated_rows_html.append(row_html)

            # Return concatenated row HTML with closeModal trigger
            return HttpResponse(
                "\n".join(updated_rows_html),
                headers={"HX-Trigger": '{"closeModal": null}'},
            )

        return redirect("plugins:netbox_librenms_plugin:librenms_import")


class DeviceVCDetailsView(LibreNMSPermissionMixin, LibreNMSAPIMixin, View):
    """HTMX view to show virtual chassis details."""

    def get(self, request, device_id):
        """Render virtual chassis details for a LibreNMS device."""
        libre_device = get_librenms_device_by_id(self.librenms_api, device_id)
        if not libre_device:
            return HttpResponse(
                '<div class="alert alert-danger">Device not found in LibreNMS</div>',
                status=404,
            )

        vc_data = get_virtual_chassis_data(self.librenms_api, device_id)

        context = {
            "libre_device": libre_device,
            "vc_data": vc_data,
        }

        return render(
            request,
            "netbox_librenms_plugin/htmx/device_vc_details.html",
            context,
        )


class DeviceValidationDetailsView(LibreNMSPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to show detailed validation information."""

    def get(self, request, device_id):
        """Render detailed validation information for a LibreNMS device."""
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)

        if not libre_device:
            return HttpResponse(
                '<div class="alert alert-danger">Device not found in LibreNMS</div>',
                status=404,
            )

        use_sysname, strip_domain = resolve_naming_preferences(request)

        context = {
            "libre_device": libre_device,
            "validation": validation,
            "use_sysname": use_sysname,
            "strip_domain": strip_domain,
            "server_key": self.librenms_api.server_key,
        }

        # Add sync comparison data for existing devices
        existing = validation.get("existing_device")
        if existing:
            context["sync_info"] = self._build_sync_info(libre_device, existing)
            context["existing_id_servers"] = self._build_id_server_info(existing)
            context["existing_device_model_name"] = existing._meta.model_name

        return render(
            request,
            "netbox_librenms_plugin/htmx/device_validation_details.html",
            context,
        )

    @staticmethod
    def _build_sync_info(libre_device, existing_device):
        """Build sync comparison data between LibreNMS device and existing NetBox device."""
        librenms_serial = libre_device.get("serial") or "-"
        librenms_os = libre_device.get("os") or "-"
        librenms_hardware = libre_device.get("hardware") or "-"

        # Serial comparison (VMs may not have serial in all NetBox versions)
        netbox_serial = getattr(existing_device, "serial", None) or ""
        serial_synced = netbox_serial == librenms_serial or librenms_serial == "-"

        # Platform comparison
        platform_info = {
            "netbox_platform": getattr(existing_device, "platform", None),
            "librenms_os": librenms_os,
            "platform_exists": False,
            "matching_platform": None,
        }
        if librenms_os and librenms_os != "-":
            from netbox_librenms_plugin.utils import find_matching_platform

            match_result = find_matching_platform(librenms_os)
            if match_result["found"]:
                platform_info["platform_exists"] = True
                platform_info["matching_platform"] = match_result["platform"]

        netbox_platform = platform_info["netbox_platform"]
        matching_platform = platform_info["matching_platform"]
        platform_synced = librenms_os == "-" or bool(
            netbox_platform and matching_platform and netbox_platform.pk == matching_platform.pk
        )

        # Device type comparison (VMs don't have device_type)
        device_type_synced = True
        librenms_device_type = None
        from virtualization.models import VirtualMachine

        if not isinstance(existing_device, VirtualMachine):
            netbox_device_type = getattr(existing_device, "device_type", None)
            if librenms_hardware and librenms_hardware != "-":
                from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

                hw_match = match_librenms_hardware_to_device_type(librenms_hardware)
                if hw_match is None:
                    device_type_synced = False
                elif hw_match.get("matched"):
                    librenms_device_type = hw_match["device_type"]
                    if netbox_device_type is None or netbox_device_type.pk != librenms_device_type.pk:
                        device_type_synced = False
                else:
                    device_type_synced = False

        all_synced = serial_synced and platform_synced and device_type_synced

        return {
            "librenms_serial": librenms_serial,
            "serial_synced": serial_synced,
            "platform_info": platform_info,
            "platform_synced": platform_synced,
            "librenms_hardware": librenms_hardware,
            "librenms_device_type": librenms_device_type,
            "device_type_synced": device_type_synced,
            "all_synced": all_synced,
        }

    @staticmethod
    def _build_id_server_info(existing_device):
        """
        Return per-server ID mappings for the existing device's librenms_id custom field.

        Returns a list of dicts with server_key, display_name, and device_id — one entry
        per server the device is linked to. Returns None when the format is legacy (bare int)
        or when the field is absent/invalid.
        """
        from django.conf import settings

        cf_value = existing_device.custom_field_data.get("librenms_id")
        if not isinstance(cf_value, dict):
            return None

        plugins_config = settings.PLUGINS_CONFIG.get("netbox_librenms_plugin") or {}
        servers_config = plugins_config.get("servers") or {}
        if not isinstance(servers_config, dict):
            servers_config = {}
        result = []
        for sk, did in cf_value.items():
            if isinstance(did, bool) or not isinstance(did, (int, str)):
                continue
            if isinstance(did, str):
                if not did.isdigit():
                    continue
                did = int(did)
            srv_cfg = servers_config.get(sk)
            # Legacy single-server config: "default" key with no matching servers entry —
            # fall back to root-level display_name in plugins_config.
            if srv_cfg is None and sk == "default" and not servers_config:
                display_name = plugins_config.get("display_name") or sk
            else:
                if not isinstance(srv_cfg, dict):
                    srv_cfg = {}
                display_name = srv_cfg.get("display_name") or sk
            result.append({"server_key": sk, "display_name": display_name, "device_id": did})
        return result or None


class DeviceRoleUpdateView(LibreNMSPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to update a table row when a role is selected."""

    def post(self, request, device_id):
        """Update the table row after a device role selection change."""
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)

        if not libre_device:
            return HttpResponse("Device not found", status=404)

        return self.render_device_row(request, libre_device, validation, selections)


class DeviceClusterUpdateView(LibreNMSPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to update a table row when a cluster is selected/deselected."""

    def post(self, request, device_id):
        """Update the table row after a cluster selection change."""
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)

        if not libre_device:
            return HttpResponse("Device not found", status=404)

        return self.render_device_row(request, libre_device, validation, selections)


class DeviceRackUpdateView(LibreNMSPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to update a table row when a rack is selected."""

    def post(self, request, device_id):
        """Update the table row after a rack selection change."""
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)

        if not libre_device:
            return HttpResponse("Device not found", status=404)

        return self.render_device_row(request, libre_device, validation, selections)


class DeviceConflictActionView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View
):
    """HTMX view to resolve device conflicts (link, update, update serial)."""

    def post(self, request, device_id):
        """Resolve a device conflict by linking, updating, or syncing serial."""
        if error := self.require_write_permission():
            return error

        from dcim.models import Device
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        action = request.POST.get("action")
        existing_device_id = request.POST.get("existing_device_id")
        existing_device_type = request.POST.get("existing_device_type", "device")

        # If the form submitted a specific server_key, honour it so the handler uses
        # the same server context as the import page when the user clicked the button.
        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            self._librenms_api = LibreNMSAPI(server_key=post_server_key)

        if not action or not existing_device_id:
            return HttpResponse("Missing action or existing_device_id", status=400)

        # VirtualMachine supports migrate_librenms_id, sync_name, and sync_platform.
        # Device-only actions (serial, device_type, legacy link/update) are rejected.
        if existing_device_type == "virtualmachine":
            if action in _DEVICE_ONLY_ACTIONS:
                return HttpResponse(
                    f"Action '{escape(action)}' is not supported for virtual machines",
                    status=400,
                )
            from virtualization.models import VirtualMachine as NetBoxVM

            existing_model: type = NetBoxVM
        else:
            existing_model = Device

        try:
            existing_device = existing_model.objects.get(pk=int(existing_device_id))
        except (existing_model.DoesNotExist, ValueError):
            return HttpResponse("Existing device not found", status=404)

        # Object-level change permission for the specific model being mutated.
        self.required_object_permissions = {"POST": [("change", existing_model)]}
        if error := self.require_object_permissions("POST"):
            return error

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return HttpResponse("LibreNMS device not found", status=404)

        # Verify the POSTed existing_device_id matches the validated conflict target.
        # Require a confirmed conflict target: if validation has no existing_device, the
        # LibreNMS device was not validated against this NetBox device, so mutations are unsafe.
        validated_existing = validation.get("existing_device") if validation else None
        if validated_existing is None:
            return HttpResponse("Missing validated conflict target", status=400)
        if validated_existing.pk != existing_device.pk or type(validated_existing) is not type(existing_device):
            return HttpResponse("Device ID mismatch: existing_device_id does not match validated device", status=400)

        # Require force flag when device type mismatches, but only for actions that use it
        force = request.POST.get("force") == "on"
        if validation.get("device_type_mismatch") and action in _FORCE_REQUIRED_ACTIONS and not force:
            return HttpResponse(
                "Device type mismatch detected. Check the force checkbox to proceed.",
                status=400,
            )

        # When force is used with device_type_mismatch, update device type to LibreNMS value
        librenms_device_type = None
        if validation.get("device_type_mismatch") and force:
            librenms_device_type = validation.get("device_type", {}).get("device_type")

        librenms_id = libre_device.get("device_id")
        if isinstance(librenms_id, bool):
            return HttpResponse("Invalid or missing LibreNMS device_id in payload", status=400)
        try:
            librenms_id = int(librenms_id)
        except (TypeError, ValueError):
            return HttpResponse("Invalid or missing LibreNMS device_id in payload", status=400)
        if librenms_id <= 0:
            return HttpResponse("Invalid or missing LibreNMS device_id in payload", status=400)

        # Wrap the LibreNMS-ID collision check and subsequent write in a single
        # transaction so the read-then-write is atomic for link/update/update_serial.
        # NOTE: A fully race-free guarantee would require a DB-unique constraint on
        # (server_key, librenms_id) — e.g., a dedicated DeviceLibreNMSIDMapping model.
        # That is deferred to a future schema migration.  Until then, we acquire a
        # row-level lock on the target device before re-checking for conflicts, which
        # serializes concurrent operations on the SAME device and greatly reduces the
        # window for assigning the same ID to two DIFFERENT devices.
        if action in {"link", "update", "update_serial"}:
            from netbox_librenms_plugin.utils import find_by_librenms_id

            with transaction.atomic():
                server_key = self.librenms_api.server_key
                # Lock the target device row so concurrent requests for the same
                # device are serialized.  The conflict check below is still a
                # best-effort guard for different devices; a DB unique constraint
                # would be needed for full protection.
                try:
                    existing_device = Device.objects.select_for_update().get(pk=existing_device.pk)
                except Device.DoesNotExist:
                    return HttpResponse(
                        "Device no longer exists; it may have been deleted concurrently.",
                        status=409,
                    )
                id_conflict = find_by_librenms_id(Device, int(librenms_id), server_key)
                if id_conflict and id_conflict.pk != existing_device.pk:
                    return HttpResponse(
                        f"LibreNMS ID conflict: ID {escape(str(librenms_id))} is already assigned to device "
                        f"'{escape(id_conflict.name)}' (ID: {id_conflict.pk})",
                        status=409,
                    )

                # Reject legacy bare-int/string librenms_id: set_librenms_device_id
                # silently skips writes for legacy formats, leaving the device partially
                # updated. User must run "Convert mapping" migration first.
                stored_id = existing_device.custom_field_data.get("librenms_id")
                _is_legacy = isinstance(stored_id, int) and not isinstance(stored_id, bool)
                if not _is_legacy and isinstance(stored_id, str):
                    try:
                        int(stored_id)
                        _is_legacy = True
                    except (ValueError, TypeError):
                        pass
                if _is_legacy:
                    return HttpResponse(
                        "Device has a legacy bare-integer librenms_id; use 'Convert mapping' "
                        "to migrate to the multi-server format before linking.",
                        status=409,
                    )

                if action == "link":
                    # Link to LibreNMS and update name from LibreNMS data
                    hostname = _get_hostname_for_action(request, validation, libre_device)
                    set_librenms_device_id(existing_device, librenms_id, self.librenms_api.server_key)
                    existing_device.name = hostname
                    if librenms_device_type:
                        existing_device.device_type = librenms_device_type
                    if err := _save_device(existing_device):
                        return err
                    logger.info(f"Linked device '{existing_device.name}' to LibreNMS ID {librenms_id}")

                elif action == "update":
                    # Update hostname, serial, and link to LibreNMS
                    hostname = _get_hostname_for_action(request, validation, libre_device)
                    incoming_serial = libre_device.get("serial") or ""
                    if incoming_serial and incoming_serial != "-":
                        # Lock any conflicting device under the same transaction to reduce
                        # the serial-assignment race window (best-effort; a DB unique
                        # constraint on serial would give full protection).
                        conflict_device = (
                            Device.objects.select_for_update()
                            .filter(serial=incoming_serial)
                            .exclude(pk=existing_device.pk)
                            .first()
                        )
                        if conflict_device:
                            return HttpResponse(
                                f"Serial conflict: '{escape(incoming_serial)}' is already assigned to device "
                                f"'{escape(conflict_device.name)}' (ID: {conflict_device.pk})",
                                status=409,
                            )
                        existing_device.serial = incoming_serial
                    existing_device.name = hostname
                    if librenms_device_type:
                        existing_device.device_type = librenms_device_type
                    set_librenms_device_id(existing_device, librenms_id, self.librenms_api.server_key)
                    if err := _save_device(existing_device):
                        return err
                    logger.info(
                        f"Updated device '{existing_device.name}': serial={incoming_serial}, "
                        f"linked to LibreNMS ID {librenms_id}"
                    )

                elif action == "update_serial":
                    # Update only the serial and link to LibreNMS
                    incoming_serial = libre_device.get("serial") or ""
                    if incoming_serial and incoming_serial != "-":
                        # Lock any conflicting device under the same transaction to reduce
                        # the serial-assignment race window (best-effort; a DB unique
                        # constraint on serial would give full protection).
                        conflict_device = (
                            Device.objects.select_for_update()
                            .filter(serial=incoming_serial)
                            .exclude(pk=existing_device.pk)
                            .first()
                        )
                        if conflict_device:
                            return HttpResponse(
                                f"Serial conflict: '{escape(incoming_serial)}' is already assigned to device "
                                f"'{escape(conflict_device.name)}' (ID: {conflict_device.pk})",
                                status=409,
                            )
                        existing_device.serial = incoming_serial
                    if librenms_device_type:
                        existing_device.device_type = librenms_device_type
                    set_librenms_device_id(existing_device, librenms_id, self.librenms_api.server_key)
                    if err := _save_device(existing_device):
                        return err
                    logger.info(
                        f"Updated serial on device '{existing_device.name}' to {incoming_serial}, "
                        f"linked to LibreNMS ID {librenms_id}"
                    )

        elif action == "sync_name":
            # Sync device name from LibreNMS (e.g., IP → sysName)
            hostname = _get_hostname_for_action(request, validation, libre_device)
            existing_device.name = hostname
            if err := _save_device(existing_device):
                return err
            logger.info(f"Synced name on device '{existing_device.name}' from LibreNMS")

        elif action == "update_type":
            # Update device type from LibreNMS (requires force for mismatch)
            if librenms_device_type:
                existing_device.device_type = librenms_device_type
                if err := _save_device(existing_device):
                    return err
                logger.info(f"Updated device type on '{existing_device.name}' to {librenms_device_type}")
            else:
                return HttpResponse("No LibreNMS device type available to update", status=400)

        elif action == "sync_serial":
            # Sync serial number from LibreNMS.
            # Wrap conflict-check-and-write in a transaction with a row lock so
            # concurrent requests cannot both pass the serial uniqueness guard.
            incoming_serial = libre_device.get("serial") or ""
            if incoming_serial and incoming_serial != "-":
                with transaction.atomic():
                    try:
                        locked_device = Device.objects.select_for_update().get(pk=existing_device.pk)
                    except Device.DoesNotExist:
                        return HttpResponse(
                            "Device no longer exists; it may have been deleted concurrently.",
                            status=409,
                        )
                    # Re-check for serial ownership conflict under lock.
                    # Note: We intentionally do NOT enforce a DB-level uniqueness constraint on
                    # Device.serial. During device moves/replacements, multiple devices may
                    # temporarily share a serial (old record gets updated later). A unique
                    # constraint would block those valid workflows. Instead, we rely on this
                    # in-transaction row-lock check to guard concurrent sync of the SAME serial,
                    # and flag conflicts via a 409 response for the user to resolve manually.
                    conflict_device = Device.objects.filter(serial=incoming_serial).exclude(pk=locked_device.pk).first()
                    if conflict_device:
                        logger.warning(
                            f"Serial sync blocked: '{incoming_serial}' already assigned to "
                            f"'{conflict_device.name}' (pk={conflict_device.pk})"
                        )
                        return HttpResponse(
                            f"Serial conflict: '{escape(incoming_serial)}' is already assigned to device "
                            f"'{escape(conflict_device.name)}' (ID: {conflict_device.pk})",
                            status=409,
                        )
                    locked_device.serial = incoming_serial
                    if err := _save_device(locked_device):
                        return err
                    logger.info(f"Synced serial on '{locked_device.name}' to {incoming_serial}")
            else:
                return HttpResponse("No valid serial from LibreNMS", status=400)

        elif action == "sync_platform":
            # Sync platform from LibreNMS OS
            from netbox_librenms_plugin.utils import find_matching_platform

            librenms_os = libre_device.get("os") or ""
            if librenms_os and librenms_os != "-":
                match_result = find_matching_platform(librenms_os)
                if match_result["found"]:
                    existing_device.platform = match_result["platform"]
                    if err := _save_device(existing_device):
                        return err
                    logger.info(f"Synced platform on '{existing_device.name}' to {match_result['platform']}")
                else:
                    return HttpResponse(f"Platform '{escape(librenms_os)}' not found in NetBox", status=400)
            else:
                return HttpResponse("No OS info from LibreNMS", status=400)

        elif action == "sync_device_type":
            # Sync device type from LibreNMS hardware (non-mismatch case)
            from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

            hardware = libre_device.get("hardware") or ""
            hw_match = match_librenms_hardware_to_device_type(hardware)
            if hw_match and hw_match.get("matched"):
                existing_device.device_type = hw_match["device_type"]
                if err := _save_device(existing_device):
                    return err
                logger.info(f"Synced device type on '{existing_device.name}' to {hw_match['device_type']}")
            else:
                return HttpResponse(f"No matching device type for '{escape(hardware)}'", status=400)

        elif action == "migrate_librenms_id":
            # Migrate legacy bare-integer librenms_id to the JSON dict format.
            # Only safe when the integer matches the LibreNMS device ID for this server,
            # confirmed by serial match (or explicit force).
            from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

            # Direct access needed to detect legacy integer format for migration prompt:
            # LibreNMSAPI.get_librenms_id() returns an int in both formats; only the raw
            # type check on custom_field_data reveals whether migration is needed.
            cf_value = existing_device.custom_field_data.get("librenms_id")
            if isinstance(cf_value, bool) or not (
                isinstance(cf_value, int) or (isinstance(cf_value, str) and cf_value.isdigit())
            ):
                return HttpResponse(
                    "Device librenms_id is already in JSON format; no migration needed.",
                    status=400,
                )
            # Normalise string-digit to int for consistent comparison
            cf_int = int(cf_value) if isinstance(cf_value, str) else cf_value
            # Verify the stored legacy ID matches the active LibreNMS device_id so we don't
            # migrate a stale/incorrect association to the wrong server mapping.
            if cf_int != librenms_id:
                return HttpResponse(
                    f"Legacy librenms_id ({cf_int}) does not match the active device ID "
                    f"({librenms_id}); cannot migrate safely.",
                    status=400,
                )
            if not validation.get("serial_confirmed") and not force:
                return HttpResponse(
                    "Serial number not confirmed. Check the force checkbox to migrate without serial verification.",
                    status=400,
                )
            with transaction.atomic():
                try:
                    locked_device = existing_model.objects.select_for_update().get(pk=existing_device.pk)
                except existing_model.DoesNotExist:
                    return HttpResponse(
                        "Object no longer exists; it may have been deleted concurrently.",
                        status=409,
                    )
                # Re-check under lock — another request may have already migrated it
                cf_locked = locked_device.custom_field_data.get("librenms_id")
                if isinstance(cf_locked, bool) or not (
                    isinstance(cf_locked, int) or (isinstance(cf_locked, str) and cf_locked.isdigit())
                ):
                    return HttpResponse(
                        "Device librenms_id is already in JSON format; no migration needed.",
                        status=400,
                    )
                cf_locked_int = int(cf_locked) if isinstance(cf_locked, str) else cf_locked
                if cf_locked_int != librenms_id:
                    return HttpResponse(
                        f"Legacy librenms_id changed under lock ({cf_locked_int} != {librenms_id}); cannot migrate safely.",
                        status=400,
                    )
                # Check that no other object already owns this ID (server-scoped or legacy)
                server_key = self.librenms_api.server_key
                from netbox_librenms_plugin.utils import find_by_librenms_id

                match = find_by_librenms_id(existing_model, cf_locked_int, server_key)
                conflict = match is not None and match.pk != locked_device.pk
                if conflict:
                    return HttpResponse(
                        f"Another device already has librenms_id {cf_locked_int} for server '{server_key}'; cannot migrate.",
                        status=409,
                    )
                if not migrate_legacy_librenms_id(locked_device, self.librenms_api.server_key):
                    return HttpResponse(
                        "Migration failed: librenms_id could not be converted.",
                        status=400,
                    )
                if err := _save_device(locked_device):
                    return err
            logger.info(
                f"Migrated legacy librenms_id on '{locked_device.name}' "
                f"to {{{self.librenms_api.server_key!r}: {cf_locked_int}}}"
            )

        else:
            return HttpResponse(f"Unknown action: {escape(action)}", status=400)

        # Clear cached validation so re-validation picks up the changes
        cache_key = get_import_device_cache_key(device_id, self.librenms_api.server_key)
        cache.delete(cache_key)

        # Re-validate and render updated row
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return HttpResponse("Device not found after action", status=404)

        response = self.render_device_row(request, libre_device, validation, selections)
        response["HX-Trigger"] = "closeModal"
        return response


class SaveUserPrefView(LibreNMSPermissionMixin, View):
    """Save a user preference via POST. Used by JS toggle handlers."""

    ALLOWED_PREFS = {
        "use_sysname": "plugins.netbox_librenms_plugin.use_sysname",
        "strip_domain": "plugins.netbox_librenms_plugin.strip_domain",
        "interface_name_field": "plugins.netbox_librenms_plugin.interface_name_field",
    }

    def post(self, request):
        """Persist a user preference toggle value."""
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        key = data.get("key")
        value = data.get("value")

        if key not in self.ALLOWED_PREFS:
            return JsonResponse({"error": "Invalid preference key"}, status=400)

        save_user_pref(request, self.ALLOWED_PREFS[key], value)
        return JsonResponse({"status": "ok"})
