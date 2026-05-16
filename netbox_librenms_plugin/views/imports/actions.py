"""HTMX endpoints and POST handlers for importing LibreNMS devices."""

import json
import logging
from ipaddress import ip_address as _ipaddr_parse
from urllib.parse import parse_qs, urlparse

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.text import slugify
from django.views import View

from netbox_librenms_plugin.import_utils import (
    _determine_device_name,
    bulk_import_devices,
    bulk_import_vms,
    detect_bulk_collisions,
    fetch_device_with_cache,
    get_import_device_cache_key,
    get_librenms_device_by_id,
    get_or_create_global_ip,
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
from netbox_librenms_plugin.utils import (
    resolve_auto_create_ipam,
    resolve_naming_preferences,
    save_user_pref,
    set_librenms_device_id,
)
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin

logger = logging.getLogger(__name__)


def _attach_messages_oob(response, request):
    """
    Append a single OOB-swap toast container to an HTMX response.

    NetBox's standard ``inc/messages.html`` renders a
    ``<div id="django-messages" hx-swap-oob="true">`` with one Bootstrap toast
    per pending Django message. Including this snippet inside per-row partials
    causes problems on multi-row OOB responses because each render emits a
    matching ``id="django-messages"`` div and the LAST swap (typically empty
    once messages have been consumed by an earlier render) wipes the toasts.

    Centralising the include here guarantees a single render per HTMX response
    so toasts always make it to NetBox's afterSettle ``initMessages()`` hook.
    """
    if response is None or not hasattr(response, "content"):
        return response
    if not isinstance(response.content, (bytes, bytearray)):
        return response
    try:
        rendered = render_to_string("inc/messages.html", request=request)
    except Exception:  # pragma: no cover - defensive: don't break HTMX response on render error
        logger.debug("Failed to render inc/messages.html for OOB toast attach", exc_info=True)
        return response
    response.content = response.content + rendered.encode("utf-8")
    return response


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


def _htmx_error_response(message: str) -> HttpResponse:
    """
    Return an HTMX-friendly error response that surfaces ``message`` as a toast.

    Uses an out-of-band swap of NetBox's ``#django-messages`` container so the
    toast renders through the same Bootstrap pipeline NetBox uses for the
    standard ``messages`` framework — no dependency on ``window.bootstrap``.

    Returns ``200`` (with ``HX-Reswap: none``) so the primary swap target is
    left untouched *and* so ``django-htmx``'s DEBUG-mode handler does not
    replace the page body with the response payload (it only does so for
    4xx/5xx responses).
    """
    toast_html = format_html(
        '<div id="django-messages" class="toast-container position-fixed bottom-0 end-0 p-3" hx-swap-oob="true">'
        '<div class="toast toast-dark border-0 shadow-sm" role="alert" aria-live="assertive" '
        'aria-atomic="true" data-bs-delay="12000">'
        '<div class="toast-header text-bg-danger">'
        '<i class="mdi mdi-alert-circle me-1"></i>Error'
        '<button type="button" class="btn-close me-0 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>'
        "</div>"
        '<div class="toast-body">{}</div>'
        "</div>"
        "</div>",
        message,
    )
    resp = HttpResponse(toast_html, content_type="text/html")
    # Prevent the triggering element's hx-swap from clobbering its target with
    # our OOB-only payload; OOB still applies regardless of HX-Reswap.
    resp["HX-Reswap"] = "none"
    return resp


def _save_device(device, update_fields: list[str] | None = None) -> HttpResponse | None:
    """Persist a Device row, returning an HttpResponse on failure or None on success.

    When ``update_fields`` is provided, the call uses ``save(update_fields=...)``
    which (a) issues a narrower UPDATE that only writes those columns and
    (b) bypasses ``full_clean()``.  This is the correct mode when the
    caller mutates only a known small set of fields and the device row
    may carry pre-existing inconsistencies on *other* fields (e.g. a
    legacy ``face`` value left behind after a rack was cleared).
    Validating those untouched fields would block legitimate updates.

    When ``update_fields`` is ``None`` (the default), the legacy behaviour
    is preserved: ``full_clean()`` runs against the entire row before
    ``save()`` writes every column.
    """
    from django.db import IntegrityError

    if update_fields is None:
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

    try:
        device.save(update_fields=update_fields)
    except IntegrityError as exc:
        return HttpResponse(f"Integrity error: {escape(str(exc))}", status=409)
    except ValidationError as exc:
        error_msg = exc.message_dict if hasattr(exc, "message_dict") else str(exc)
        return HttpResponse(f"Validation error: {escape(str(error_msg))}", status=400)
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

        return _attach_messages_oob(
            render(
                request,
                "netbox_librenms_plugin/htmx/device_import_row.html",
                context,
            ),
            request,
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
                # Keep confirm modal aligned with import-time behavior: always
                # detect VC membership so stack members are visible before import.
                include_vc_detection=True,
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
                    f"{len(errors)} error(s) occurred: {' '.join(escape(e) for e in errors) if errors else 'Please check device validation status.'}"
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

        collisions = detect_bulk_collisions(devices)
        if collisions:
            return render(
                request,
                "netbox_librenms_plugin/htmx/bulk_import_collision.html",
                {"collisions": collisions, "device_count": len(devices)},
                status=409,
            )

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
        auto_create_ipam = resolve_auto_create_ipam(request)
        vc_detection_enabled = _resolve_vc_detection_enabled(request)
        sync_options = {
            "sync_interfaces": request.POST.get("sync_interfaces") == "on",
            "sync_cables": request.POST.get("sync_cables") == "on",
            "sync_ips": request.POST.get("sync_ips") == "on",
            "vc_detection_enabled": vc_detection_enabled,
            "use_sysname": use_sysname,
            "strip_domain": strip_domain,
            "auto_create_ipam": auto_create_ipam,
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

        except Exception:  # pragma: no cover - defensive guard
            logger.exception("Error during bulk import")
            if request.headers.get("HX-Request"):
                return HttpResponse("Import failed. Please check server logs.", status=500)
            messages.error(request, "Bulk import failed. Please check server logs.")
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
        # Aggregate auto-created IPAM entries across the batch and surface a
        # single info toast so the user knows a side-effect happened on import.
        created_ips_all = []
        for item in device_result.get("success", []):
            created_ips_all.extend(item.get("created_ips") or [])
        for item in vm_result.get("success", []):
            vm_obj = item.get("device") or item.get("vm")
            ips = getattr(vm_obj, "_librenms_created_ips", None) if vm_obj is not None else None
            if ips:
                created_ips_all.extend(ips)
        if created_ips_all:
            unique_ips = sorted(set(created_ips_all))
            preview = ", ".join(unique_ips[:5]) + (f" (+{len(unique_ips) - 5} more)" if len(unique_ips) > 5 else "")
            messages.info(
                request,
                f"Auto-created {len(unique_ips)} IPAM entr{'y' if len(unique_ips) == 1 else 'ies'} "
                f"in the global scope (unassigned): {preview}.",
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
            response = HttpResponse(
                "\n".join(updated_rows_html),
                headers={"HX-Trigger": '{"closeModal": null}'},
            )
            return _attach_messages_oob(response, request)

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
            return _htmx_error_response("Device not found")

        return self.render_device_row(request, libre_device, validation, selections)


class DeviceClusterUpdateView(LibreNMSPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to update a table row when a cluster is selected/deselected."""

    def post(self, request, device_id):
        """Update the table row after a cluster selection change."""
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)

        if not libre_device:
            return _htmx_error_response("Device not found")

        return self.render_device_row(request, libre_device, validation, selections)


class DeviceRackUpdateView(LibreNMSPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to update a table row when a rack is selected."""

    def post(self, request, device_id):
        """Update the table row after a rack selection change."""
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)

        if not libre_device:
            return _htmx_error_response("Device not found")

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
            return _htmx_error_response("Missing action or existing_device_id")

        # VirtualMachine supports migrate_librenms_id, sync_name, and sync_platform.
        # Device-only actions (serial, device_type, legacy link/update) are rejected.
        if existing_device_type == "virtualmachine":
            if action in _DEVICE_ONLY_ACTIONS:
                return _htmx_error_response(f"Action '{action}' is not supported for virtual machines")
            from virtualization.models import VirtualMachine as NetBoxVM

            existing_model: type = NetBoxVM
        else:
            existing_model = Device

        try:
            existing_device = existing_model.objects.get(pk=int(existing_device_id))
        except (existing_model.DoesNotExist, ValueError):
            return _htmx_error_response("Existing device not found")

        # Object-level change permission for the specific model being mutated.
        self.required_object_permissions = {"POST": [("change", existing_model)]}
        if error := self.require_object_permissions("POST"):
            return error

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return _htmx_error_response("LibreNMS device not found")

        # Verify the POSTed existing_device_id matches the validated conflict target.
        # Require a confirmed conflict target: if validation has no existing_device, the
        # LibreNMS device was not validated against this NetBox device, so mutations are unsafe.
        validated_existing = validation.get("existing_device") if validation else None
        if validated_existing is None:
            return _htmx_error_response("Missing validated conflict target")
        if validated_existing.pk != existing_device.pk or type(validated_existing) is not type(existing_device):
            return _htmx_error_response("Device ID mismatch: existing_device_id does not match validated device")

        # Require force flag when device type mismatches, but only for actions that use it
        force = request.POST.get("force") == "on"
        if validation.get("device_type_mismatch") and action in _FORCE_REQUIRED_ACTIONS and not force:
            return _htmx_error_response("Device type mismatch detected. Check the force checkbox to proceed.")

        # When force is used with device_type_mismatch, update device type to LibreNMS value
        librenms_device_type = None
        if validation.get("device_type_mismatch") and force:
            librenms_device_type = validation.get("device_type", {}).get("device_type")

        librenms_id = libre_device.get("device_id")
        if isinstance(librenms_id, bool):
            return _htmx_error_response("Invalid or missing LibreNMS device_id in payload")
        try:
            librenms_id = int(librenms_id)
        except (TypeError, ValueError):
            return _htmx_error_response("Invalid or missing LibreNMS device_id in payload")
        if librenms_id <= 0:
            return _htmx_error_response("Invalid or missing LibreNMS device_id in payload")

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
                    return _htmx_error_response("Device no longer exists; it may have been deleted concurrently.")
                id_conflict = find_by_librenms_id(Device, int(librenms_id), server_key)
                if id_conflict and id_conflict.pk != existing_device.pk:
                    return _htmx_error_response(
                        f"LibreNMS ID conflict: ID {librenms_id} is already assigned to device "
                        f"'{id_conflict.name}' (ID: {id_conflict.pk})"
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
                    return _htmx_error_response(
                        "Device has a legacy bare-integer librenms_id; use 'Convert mapping' "
                        "to migrate to the multi-server format before linking."
                    )

                if action == "link":
                    # Link to LibreNMS and update name from LibreNMS data
                    hostname = _get_hostname_for_action(request, validation, libre_device)
                    set_librenms_device_id(existing_device, librenms_id, self.librenms_api.server_key)
                    existing_device.name = hostname
                    fields = ["custom_field_data", "name"]
                    if librenms_device_type:
                        existing_device.device_type = librenms_device_type
                        fields.append("device_type")
                    if err := _save_device(existing_device, update_fields=fields):
                        return err
                    logger.info(f"Linked device '{existing_device.name}' to LibreNMS ID {librenms_id}")

                elif action == "update":
                    # Update hostname, serial, and link to LibreNMS
                    hostname = _get_hostname_for_action(request, validation, libre_device)
                    incoming_serial = libre_device.get("serial") or ""
                    fields = ["custom_field_data", "name"]
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
                            return _htmx_error_response(
                                f"Serial conflict: '{incoming_serial}' is already assigned to device "
                                f"'{conflict_device.name}' (ID: {conflict_device.pk})"
                            )
                        existing_device.serial = incoming_serial
                        fields.append("serial")
                    existing_device.name = hostname
                    if librenms_device_type:
                        existing_device.device_type = librenms_device_type
                        fields.append("device_type")
                    set_librenms_device_id(existing_device, librenms_id, self.librenms_api.server_key)
                    if err := _save_device(existing_device, update_fields=fields):
                        return err
                    logger.info(
                        f"Updated device '{existing_device.name}': serial={incoming_serial}, "
                        f"linked to LibreNMS ID {librenms_id}"
                    )

                elif action == "update_serial":
                    # Update only the serial and link to LibreNMS
                    incoming_serial = libre_device.get("serial") or ""
                    fields = ["custom_field_data"]
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
                            return _htmx_error_response(
                                f"Serial conflict: '{incoming_serial}' is already assigned to device "
                                f"'{conflict_device.name}' (ID: {conflict_device.pk})"
                            )
                        existing_device.serial = incoming_serial
                        fields.append("serial")
                    if librenms_device_type:
                        existing_device.device_type = librenms_device_type
                        fields.append("device_type")
                    set_librenms_device_id(existing_device, librenms_id, self.librenms_api.server_key)
                    if err := _save_device(existing_device, update_fields=fields):
                        return err
                    logger.info(
                        f"Updated serial on device '{existing_device.name}' to {incoming_serial}, "
                        f"linked to LibreNMS ID {librenms_id}"
                    )

        elif action == "sync_name":
            # Sync device name from LibreNMS (e.g., IP → sysName)
            hostname = _get_hostname_for_action(request, validation, libre_device)
            existing_device.name = hostname
            if err := _save_device(existing_device, update_fields=["name"]):
                return err
            logger.info(f"Synced name on device '{existing_device.name}' from LibreNMS")

        elif action == "update_type":
            # Update device type from LibreNMS (requires force for mismatch)
            if librenms_device_type:
                existing_device.device_type = librenms_device_type
                if err := _save_device(existing_device, update_fields=["device_type"]):
                    return err
                logger.info(f"Updated device type on '{existing_device.name}' to {librenms_device_type}")
            else:
                return _htmx_error_response("No LibreNMS device type available to update")

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
                        return _htmx_error_response("Device no longer exists; it may have been deleted concurrently.")
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
                        return _htmx_error_response(
                            f"Serial conflict: '{incoming_serial}' is already assigned to device "
                            f"'{conflict_device.name}' (ID: {conflict_device.pk})"
                        )
                    locked_device.serial = incoming_serial
                    if err := _save_device(locked_device, update_fields=["serial"]):
                        return err
                    logger.info(f"Synced serial on '{locked_device.name}' to {incoming_serial}")
            else:
                return _htmx_error_response("No valid serial from LibreNMS")

        elif action == "sync_platform":
            # Sync platform from LibreNMS OS
            from netbox_librenms_plugin.utils import find_matching_platform

            librenms_os = libre_device.get("os") or ""
            if librenms_os and librenms_os != "-":
                match_result = find_matching_platform(librenms_os)
                if match_result["found"]:
                    existing_device.platform = match_result["platform"]
                    if err := _save_device(existing_device, update_fields=["platform"]):
                        return err
                    logger.info(f"Synced platform on '{existing_device.name}' to {match_result['platform']}")
                elif match_result.get("match_type") == "ambiguous":
                    ambiguity_source = match_result.get("ambiguity_source", "mapping")
                    if ambiguity_source == "platform":
                        target = "Platforms"
                    else:
                        target = "Platform Mappings"
                    return _htmx_error_response(
                        f"Multiple {target} match OS '{librenms_os}' — resolve the conflict in {target}"
                    )
                else:
                    return _htmx_error_response(f"Platform '{librenms_os}' not found in NetBox")
            else:
                return _htmx_error_response("No OS info from LibreNMS")

        elif action == "sync_device_type":
            # Sync device type from LibreNMS hardware (non-mismatch case)
            from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

            hardware = libre_device.get("hardware") or ""
            hw_match = match_librenms_hardware_to_device_type(hardware)
            if hw_match and hw_match.get("matched"):
                existing_device.device_type = hw_match["device_type"]
                if err := _save_device(existing_device, update_fields=["device_type"]):
                    return err
                logger.info(f"Synced device type on '{existing_device.name}' to {hw_match['device_type']}")
            else:
                return _htmx_error_response(f"No matching device type for '{hardware}'")

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
                return _htmx_error_response("Device librenms_id is already in JSON format; no migration needed.")
            # Normalise string-digit to int for consistent comparison
            cf_int = int(cf_value) if isinstance(cf_value, str) else cf_value
            # Verify the stored legacy ID matches the active LibreNMS device_id so we don't
            # migrate a stale/incorrect association to the wrong server mapping.
            if cf_int != librenms_id:
                return _htmx_error_response(
                    f"Legacy librenms_id ({cf_int}) does not match the active device ID "
                    f"({librenms_id}); cannot migrate safely."
                )
            if not validation.get("serial_confirmed") and not force:
                return _htmx_error_response(
                    "Serial number not confirmed. Check the force checkbox to migrate without serial verification."
                )
            with transaction.atomic():
                try:
                    locked_device = existing_model.objects.select_for_update().get(pk=existing_device.pk)
                except existing_model.DoesNotExist:
                    return _htmx_error_response("Object no longer exists; it may have been deleted concurrently.")
                # Re-check under lock — another request may have already migrated it
                cf_locked = locked_device.custom_field_data.get("librenms_id")
                if isinstance(cf_locked, bool) or not (
                    isinstance(cf_locked, int) or (isinstance(cf_locked, str) and cf_locked.isdigit())
                ):
                    return _htmx_error_response("Device librenms_id is already in JSON format; no migration needed.")
                cf_locked_int = int(cf_locked) if isinstance(cf_locked, str) else cf_locked
                if cf_locked_int != librenms_id:
                    return _htmx_error_response(
                        f"Legacy librenms_id changed under lock ({cf_locked_int} != {librenms_id}); cannot migrate safely."
                    )
                # Check that no other object already owns this ID (server-scoped or legacy)
                server_key = self.librenms_api.server_key
                from netbox_librenms_plugin.utils import find_by_librenms_id

                match = find_by_librenms_id(existing_model, cf_locked_int, server_key)
                conflict = match is not None and match.pk != locked_device.pk
                if conflict:
                    return _htmx_error_response(
                        f"Another device already has librenms_id {cf_locked_int} for server '{server_key}'; cannot migrate."
                    )
                if not migrate_legacy_librenms_id(locked_device, self.librenms_api.server_key):
                    return _htmx_error_response("Migration failed: librenms_id could not be converted.")
                # Save only the field we actually mutated. Running full_clean() on the
                # whole object would reject the migration over unrelated pre-existing
                # validation issues (e.g. legacy rack face/position without a rack),
                # which is too strict for an ID-only migration: the point of "Convert
                # mapping" is to clean up the librenms_id custom field, not to gate
                # on every other field being valid.
                try:
                    locked_device.save(update_fields=["custom_field_data"])
                except IntegrityError:
                    logger.exception(
                        "Failed to persist migrated LibreNMS mapping for %s pk=%s",
                        type(locked_device).__name__,
                        locked_device.pk,
                    )
                    return _htmx_error_response("Unable to migrate the LibreNMS mapping. Please try again.")
            logger.info(
                f"Migrated legacy librenms_id on '{locked_device.name}' "
                f"to {{{self.librenms_api.server_key!r}: {cf_locked_int}}}"
            )

        else:
            return _htmx_error_response(f"Unknown action: {action}")

        # Clear cached validation so re-validation picks up the changes
        cache_key = get_import_device_cache_key(device_id, self.librenms_api.server_key)
        cache.delete(cache_key)

        # Re-validate and render updated row
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return _htmx_error_response("Device not found after action")

        response = self.render_device_row(request, libre_device, validation, selections)
        response["HX-Trigger"] = "closeModal"
        return response


class AddDeviceTypeMappingView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View
):
    """HTMX view to create a DeviceTypeMapping from the import validation modal."""

    def post(self, request, device_id):
        """Create a DeviceTypeMapping linking the LibreNMS hardware string to a NetBox DeviceType."""
        from netbox_librenms_plugin.models import DeviceTypeMapping

        # Check plugin write permission early (cheap, no API call needed).
        if error := self.require_write_permission():
            return error

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            from netbox_librenms_plugin.librenms_api import LibreNMSAPI

            self._librenms_api = LibreNMSAPI(server_key=post_server_key)

        from dcim.models import DeviceType

        libre_device = fetch_device_with_cache(device_id, self.librenms_api)
        if not libre_device:
            return _htmx_error_response("Device not found in LibreNMS.")

        hardware = (libre_device.get("hardware") or "").strip()
        if not hardware or hardware == "-":
            return _htmx_error_response("Device has no hardware string — cannot create mapping.")

        device_type_id = request.POST.get("device_type_id", "").strip()
        if not device_type_id:
            return _htmx_error_response("Please select a device type before submitting.")

        try:
            device_type_id = int(device_type_id)
        except (ValueError, TypeError):
            return _htmx_error_response("Invalid device type selection.")

        try:
            device_type = DeviceType.objects.get(pk=device_type_id)
        except DeviceType.DoesNotExist:
            return _htmx_error_response("Selected device type not found.")

        # Resolve the existing mapping first so we only require the permission
        # actually needed: "add" for a new mapping, "change" for an update.
        existing_mapping = DeviceTypeMapping.objects.filter(librenms_hardware__iexact=hardware).first()
        if existing_mapping:
            self.required_object_permissions = {"POST": [("change", DeviceTypeMapping)]}
        else:
            self.required_object_permissions = {"POST": [("add", DeviceTypeMapping)]}
        if error := self.require_object_permissions("POST"):
            return error

        try:
            with transaction.atomic():
                # Lock the row to close the window between the upfront permission
                # check and the actual write (select_for_update prevents a concurrent
                # INSERT from slipping through undetected).
                locked = (
                    DeviceTypeMapping.objects.select_for_update().filter(librenms_hardware__iexact=hardware).first()
                )
                if locked and not existing_mapping:
                    # A concurrent request created the mapping after our upfront read.
                    # Only escalate to change permission if we would actually mutate the row;
                    # if the locked row already maps to the same device type this is a no-op
                    # and the caller needs only the add permission they already passed above.
                    if locked.netbox_device_type_id != device_type_id:
                        self.required_object_permissions = {"POST": [("change", DeviceTypeMapping)]}
                        if error := self.require_object_permissions("POST"):
                            return error
                if existing_mapping and not locked:
                    # The mapping was deleted between our upfront read and the lock.
                    # We are about to CREATE a new row, so require add permission.
                    self.required_object_permissions = {"POST": [("add", DeviceTypeMapping)]}
                    if error := self.require_object_permissions("POST"):
                        return error
                if locked:
                    if locked.netbox_device_type_id != device_type_id:
                        locked.netbox_device_type = device_type
                        locked.full_clean()
                        locked.save()
                else:
                    try:
                        DeviceTypeMapping.objects.create(
                            librenms_hardware=hardware.lower(),
                            netbox_device_type=device_type,
                        )
                    except IntegrityError:
                        # Two concurrent requests both saw no existing mapping and
                        # both attempted create(); select_for_update() cannot lock
                        # absent rows. Surface a toast asking the user to retry
                        # (the second attempt will find the row and take the update path).
                        return _htmx_error_response("Mapping was created concurrently. Please try again.")
        except Exception as exc:
            logger.exception("AddDeviceTypeMappingView: failed to save mapping: %s", exc)
            return _htmx_error_response("Error saving mapping. Please try again.")

        # Clear cached LibreNMS device data so re-validation picks up the new mapping
        cache_key = get_import_device_cache_key(device_id, self.librenms_api.server_key)
        cache.delete(cache_key)

        # Re-render the modal content as an OOB swap so it updates in place.
        # The inner views render via Django templates (auto-escaped), so the
        # decoded content is already safe HTML; wrap with format_html + mark_safe
        # to compose the OOB envelope without introducing new escape boundaries
        # (CodeQL trust-assertion pattern, see plugin docs).
        detail_view = DeviceValidationDetailsView()
        detail_view._librenms_api = self._librenms_api
        modal_html = detail_view.get(request, device_id).content.decode("utf-8")
        oob_modal = format_html(
            '<div id="htmx-modal-content" hx-swap-oob="innerHTML">{}</div>',
            mark_safe(modal_html),
        )

        # Re-validate and include the background table row as a second OOB swap so the
        # row reflects the new mapping immediately without a secondary JS-triggered request.
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if libre_device is not None and validation is not None:
            row_response = self.render_device_row(request, libre_device, validation, selections)
            row_html = row_response.content.decode("utf-8")
            # The row template already includes hx-swap-oob="true" on the <tr>, so HTMX
            # will perform an outerHTML swap targeted by the row's id. No further
            # attribute injection is needed (and adding one would create a duplicate
            # hx-swap-oob attribute that breaks HTMX OOB parsing).
            # A <tr> following a <div> is invalid HTML and gets silently dropped by the browser
            # parser when HTMX wraps the combined response in a <template> for parsing. Wrapping
            # in <table><tbody> keeps the <tr> in a valid table context so HTMX finds and applies
            # the OOB swap. The <div id="django-messages"> inside is foster-parented outside the
            # table by the parser, so both OOB elements are preserved.
            row_html = format_html("<table><tbody>{}</tbody></table>", mark_safe(row_html))
        else:
            row_html = mark_safe("")

        return HttpResponse(oob_modal + row_html, content_type="text/html")


class CreatePlatformFromImportView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View
):
    """HTMX view to create a Platform (and optionally a mapping and device assignment) from the import page."""

    def get(self, request, device_id):
        """Render the shared create-platform form fragment for the import HTMX modal."""
        from dcim.models import Manufacturer

        post_server_key = (request.GET.get("server_key") or "").strip()
        if post_server_key:
            from netbox_librenms_plugin.librenms_api import LibreNMSAPI

            self._librenms_api = LibreNMSAPI(server_key=post_server_key)

        libre_device = fetch_device_with_cache(device_id, self.librenms_api)
        if not libre_device:
            return HttpResponse(
                '<div class="alert alert-danger">Device not found in LibreNMS.</div>',
                status=404,
            )

        librenms_os = (libre_device.get("os") or "").strip().lower()
        manufacturers = list(Manufacturer.objects.all().order_by("name"))

        _, validation, _ = self.get_validated_device_with_selections(device_id, request)
        device_pk = None
        selected_manufacturer_pk = None
        if validation:
            existing = validation.get("existing_device")
            if existing:
                device_pk = existing.pk
                device_type = getattr(existing, "device_type", None)
                if device_type:
                    selected_manufacturer_pk = device_type.manufacturer_id

        htmx_include = (
            f"[name=role_{device_id}], [name=rack_{device_id}], "
            f"[name=cluster_{device_id}], #use-sysname-toggle, #strip-domain-toggle"
        )

        return render(
            request,
            "netbox_librenms_plugin/htmx/create_platform_modal.html",
            {
                "librenms_os": librenms_os,
                "platform_name": librenms_os,
                "manufacturers": manufacturers,
                "form_action": request.path,
                "device_pk": device_pk,
                "selected_manufacturer_pk": selected_manufacturer_pk,
                "server_key": self.librenms_api.server_key,
                "use_htmx": True,
                "htmx_include": htmx_include,
            },
        )

    def post(self, request, device_id):
        """Create platform + optional mapping + optional device assignment, then return OOB swaps."""
        from dcim.models import Manufacturer, Platform

        from netbox_librenms_plugin.models import PlatformMapping

        if error := self.require_write_permission():
            return error

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            from netbox_librenms_plugin.librenms_api import LibreNMSAPI

            self._librenms_api = LibreNMSAPI(server_key=post_server_key)

        create_mapping = _parse_boolish(request.POST.get("create_mapping")) is True
        device_pk_str = (request.POST.get("device_pk") or "").strip()
        device_pk = None
        if device_pk_str:
            try:
                device_pk = int(device_pk_str)
            except (ValueError, TypeError):
                device_pk = None

        # Re-resolve the matched NetBox object via current validation. This
        # tells us which model (Device vs VirtualMachine) to assign to and
        # protects against a stale/spoofed hidden device_pk: we only mutate an
        # existing object when current validation unambiguously resolves it
        # (and the supplied device_pk, if any, agrees with that resolution).
        try:
            _, _validation, _ = self.get_validated_device_with_selections(device_id, request)
        except Exception:
            logger.exception(
                "CreatePlatformFromImportView: failed to resolve assignment target for device_id=%s",
                device_id,
            )
            return _htmx_error_response("Unable to confirm the target object for platform assignment.")
        existing_obj = _validation.get("existing_device") if _validation else None

        if existing_obj is not None and (device_pk is None or device_pk == existing_obj.pk):
            target_model = type(existing_obj)
            target_pk = existing_obj.pk
        else:
            # Either validation could not confirm a target, or the hidden
            # device_pk disagreed with the validated object. Don't guess a
            # model — create the platform but skip assignment so we never
            # mutate the wrong record.
            target_model = None
            target_pk = None

        perms = [("add", Platform)]
        if create_mapping:
            perms.append(("add", PlatformMapping))
        if target_model is not None:
            perms.append(("change", target_model))
        self.required_object_permissions = {"POST": perms}

        if error := self.require_object_permissions("POST"):
            return error

        platform_name = (request.POST.get("platform_name") or "").strip()
        manufacturer_id = (request.POST.get("manufacturer") or "").strip()
        librenms_os = (request.POST.get("librenms_os") or "").strip().lower()

        if not platform_name:
            return _htmx_error_response("Platform name is required.")

        if Platform.objects.filter(name__iexact=platform_name).exists():
            return _htmx_error_response(f'Platform "{platform_name}" already exists.')

        manufacturer = None
        if manufacturer_id:
            try:
                manufacturer = Manufacturer.objects.get(pk=int(manufacturer_id))
            except (Manufacturer.DoesNotExist, ValueError, TypeError):
                pass

        try:
            with transaction.atomic():
                platform = Platform(
                    name=platform_name,
                    slug=slugify(platform_name),
                    manufacturer=manufacturer,
                )
                platform.full_clean()
                platform.save()

                if target_model is not None and target_pk is not None:
                    try:
                        target = target_model.objects.select_for_update().get(pk=target_pk)
                        target.platform = platform
                        target.full_clean()
                        target.save()
                        logger.info(
                            "CreatePlatformFromImportView: assigned platform '%s' to %s pk=%s",
                            platform.name,
                            target_model.__name__,
                            target_pk,
                        )
                    except target_model.DoesNotExist:
                        logger.warning(
                            "CreatePlatformFromImportView: %s pk=%s not found; platform "
                            "'%s' created but not assigned to any object",
                            target_model.__name__,
                            target_pk,
                            platform.name,
                        )
                else:
                    logger.info(
                        "CreatePlatformFromImportView: no existing NetBox object matched "
                        "for LibreNMS device_id=%s; platform '%s' created without assignment",
                        device_id,
                        platform.name,
                    )

                if create_mapping and librenms_os:
                    if not PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).exists():
                        try:
                            with transaction.atomic():
                                PlatformMapping.objects.create(
                                    librenms_os=librenms_os.lower(),
                                    netbox_platform=platform,
                                )
                        except IntegrityError:
                            # Concurrent request created the mapping; safe to ignore.
                            pass
        except (ValidationError, IntegrityError) as exc:
            logger.exception("CreatePlatformFromImportView: failed to create platform: %s", exc)
            return _htmx_error_response("Error creating platform. Please try again.")

        cache_key = get_import_device_cache_key(device_id, self.librenms_api.server_key)
        cache.delete(cache_key)

        # Re-render the modal content as an OOB swap so it updates in place.
        # The inner views render via Django templates (auto-escaped), so the
        # decoded content is already safe HTML; wrap with format_html + mark_safe
        # to compose the OOB envelope without introducing new escape boundaries.
        detail_view = DeviceValidationDetailsView()
        detail_view._librenms_api = self._librenms_api
        modal_html = detail_view.get(request, device_id).content.decode("utf-8")
        oob_modal = format_html(
            '<div id="htmx-modal-content" hx-swap-oob="innerHTML">{}</div>',
            mark_safe(modal_html),
        )

        # Re-validate and include the background table row as a second OOB swap so the
        # row reflects the new platform/mapping immediately without a secondary request.
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if libre_device is not None and validation is not None:
            row_response = self.render_device_row(request, libre_device, validation, selections)
            row_html = row_response.content.decode("utf-8")
            row_html = format_html("<table><tbody>{}</tbody></table>", mark_safe(row_html))
        else:
            row_html = mark_safe("")

        return HttpResponse(oob_modal + row_html, content_type="text/html")


class AddPlatformMappingView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View
):
    """HTMX view to create a PlatformMapping from the import validation modal."""

    def post(self, request, device_id):
        """Create a PlatformMapping linking the LibreNMS OS string to a NetBox Platform."""
        if error := self.require_write_permission():
            return error

        from dcim.models import Platform
        from netbox_librenms_plugin.models import PlatformMapping

        libre_device = fetch_device_with_cache(device_id, self.librenms_api)
        if not libre_device:
            return HttpResponse(
                '<span class="text-danger small">Device not found in LibreNMS.</span>',
                status=404,
            )

        librenms_os = (libre_device.get("os") or "").strip()
        if not librenms_os:
            return HttpResponse(
                '<span class="text-danger small">Device has no OS string — cannot create mapping.</span>',
                status=400,
            )

        platform_id = request.POST.get("platform_id", "").strip()
        if not platform_id:
            return HttpResponse(
                '<span class="text-danger small">Please select a platform before submitting.</span>',
                status=400,
            )

        try:
            platform_id = int(platform_id)
        except (ValueError, TypeError):
            return HttpResponse(
                '<span class="text-danger small">Invalid platform selection.</span>',
                status=400,
            )

        try:
            platform = Platform.objects.get(pk=platform_id)
        except Platform.DoesNotExist:
            return HttpResponse(
                '<span class="text-danger small">Selected platform not found.</span>',
                status=404,
            )

        existing_mapping = PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).first()
        self.required_object_permissions = {
            "POST": [("change", PlatformMapping) if existing_mapping else ("add", PlatformMapping)]
        }
        if error := self.require_object_permissions("POST"):
            return error

        try:
            with transaction.atomic():
                mapping, created = PlatformMapping.objects.get_or_create(
                    librenms_os=librenms_os.lower(),
                    defaults={"netbox_platform": platform},
                )
                if not created and mapping.netbox_platform_id != platform_id:
                    mapping.netbox_platform = platform
                    mapping.save()
        except Exception as exc:
            logger.warning("AddPlatformMappingView: failed to save mapping: %s", exc)
            return HttpResponse(
                f'<span class="text-danger small">Error saving mapping: {escape(str(exc))}</span>',
                status=500,
            )

        cache_key = get_import_device_cache_key(device_id, self.librenms_api.server_key)
        cache.delete(cache_key)

        # Re-render the modal as an OOB swap and the background row so both update in place.
        # Use format_html + mark_safe per CodeQL trust-assertion pattern (see plugin docs).
        detail_view = DeviceValidationDetailsView()
        detail_view._librenms_api = self._librenms_api
        modal_html = detail_view.get(request, device_id).content.decode("utf-8")
        oob_modal = format_html(
            '<div id="htmx-modal-content" hx-swap-oob="innerHTML">{}</div>',
            mark_safe(modal_html),
        )

        # Re-validate and include the background table row as a second OOB swap so the
        # row reflects the new platform/mapping immediately without a secondary request.
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if libre_device is not None and validation is not None:
            row_response = self.render_device_row(request, libre_device, validation, selections)
            row_html = row_response.content.decode("utf-8")
            row_html = format_html("<table><tbody>{}</tbody></table>", mark_safe(row_html))
        else:
            row_html = mark_safe("")

        return HttpResponse(oob_modal + row_html, content_type="text/html")


class AddAsOOBView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View
):
    """HTMX view to link a LibreNMS OOB controller device to an existing NetBox Device."""

    def post(self, request, device_id):
        """Attach a LibreNMS OOB identity to the matched NetBox device."""
        if error := self.require_write_permission():
            return error

        from dcim.models import Device
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        existing_device_id = request.POST.get("existing_device_id")
        if not existing_device_id:
            return HttpResponse("Missing existing_device_id", status=400)

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            self._librenms_api = LibreNMSAPI(server_key=post_server_key)

        try:
            existing_device = Device.objects.get(pk=int(existing_device_id))
        except (Device.DoesNotExist, ValueError):
            return HttpResponse("Existing device not found", status=404)

        self.required_object_permissions = {"POST": [("change", Device)]}
        if error := self.require_object_permissions("POST"):
            return error

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return HttpResponse("LibreNMS device not found", status=404)

        oob_candidate = validation.get("oob_candidate") if validation else None
        if not oob_candidate:
            return HttpResponse("No OOB candidate found in validation data", status=400)
        if oob_candidate["device"].pk != existing_device.pk:
            return HttpResponse(
                "Device ID mismatch: existing_device_id does not match OOB candidate",
                status=400,
            )

        librenms_id = libre_device.get("device_id")
        if isinstance(librenms_id, bool):
            return HttpResponse("Invalid or missing LibreNMS device_id", status=400)
        try:
            librenms_id = int(librenms_id)
        except (TypeError, ValueError):
            return HttpResponse("Invalid or missing LibreNMS device_id", status=400)
        if librenms_id <= 0:
            return HttpResponse("Invalid LibreNMS device_id", status=400)

        # Reject legacy bare-int librenms_id (same guard as DeviceConflictActionView).
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
                "Device has a legacy bare-integer librenms_id; use 'Convert mapping' to migrate first.",
                status=409,
            )

        from netbox_librenms_plugin.utils import set_librenms_oob

        oob_type = oob_candidate.get("type") or ""
        oob_version = oob_candidate.get("version") or None
        oob_ip_str = oob_candidate.get("ip") or None
        server_key = self.librenms_api.server_key

        with transaction.atomic():
            try:
                existing_device = Device.objects.select_for_update().get(pk=existing_device.pk)
            except Device.DoesNotExist:
                return HttpResponse(
                    "Device no longer exists; it may have been deleted concurrently.",
                    status=409,
                )

            try:
                set_librenms_oob(
                    existing_device,
                    librenms_id,
                    server_key,
                    oob_type=oob_type,
                    version=oob_version,
                    ip=oob_ip_str,
                )
            except ValueError as exc:
                return HttpResponse(f"Invalid OOB data: {escape(str(exc))}", status=400)

            update_fields = ["custom_field_data"]
            # Assign device.oob_ip if not already set; auto-create the IPAM
            # record if it doesn't exist yet so the user has something to
            # later attach to an interface and re-home if needed.
            if oob_ip_str and existing_device.oob_ip_id is None:
                oob_ip, oob_ip_created = get_or_create_global_ip(
                    oob_ip_str, auto_create=resolve_auto_create_ipam(request)
                )
                if oob_ip is not None:
                    existing_device.oob_ip = oob_ip
                    update_fields.append("oob_ip")
                    if oob_ip_created:
                        messages.info(
                            request,
                            f"Auto-created OOB IP {oob_ip_str} in IPAM (unassigned, global scope).",
                        )

            if err := _save_device(existing_device, update_fields=update_fields):
                return err

        logger.info(
            "Linked OOB device (LibreNMS ID %d, type %s) to '%s' (server: %s)",
            librenms_id,
            oob_type,
            existing_device.name,
            server_key,
        )

        cache_key = get_import_device_cache_key(device_id, server_key)
        cache.delete(cache_key)

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return HttpResponse("Device not found after action", status=404)

        response = self.render_device_row(request, libre_device, validation, selections)
        # Keep the validation modal open and refresh its contents in place so
        # the user can confirm the new OOB attachment without losing context.
        response["HX-Trigger"] = json.dumps({"validationRefresh": {"deviceId": device_id}})
        return response


class PromoteToHostView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View
):
    """
    Promote an incoming LibreNMS host device to be the *primary* link of an existing
    NetBox device whose current LibreNMS link is the OOB controller.

    The existing NetBox device's current ``librenms_id.{server_key}.id`` is moved into
    the ``oob`` slot (preserving its bare-int → dict-form transition), and the incoming
    LibreNMS device id becomes the new host id.  No new NetBox device is created — this
    is a reassignment, not an import.
    """

    def post(self, request, device_id):
        if error := self.require_write_permission():
            return error

        from dcim.models import Device
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        existing_device_id = request.POST.get("existing_device_id")
        if not existing_device_id:
            return HttpResponse("Missing existing_device_id", status=400)

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            self._librenms_api = LibreNMSAPI(server_key=post_server_key)

        try:
            existing_device = Device.objects.get(pk=int(existing_device_id))
        except (Device.DoesNotExist, ValueError):
            return HttpResponse("Existing device not found", status=404)

        self.required_object_permissions = {"POST": [("change", Device)]}
        if error := self.require_object_permissions("POST"):
            return error

        # Optional per-field overrides from the pre-promote pick modal.
        # All three default to "keep current"; only applied when the POST
        # carries an explicit non-empty value.
        override_name = (request.POST.get("override_name") or "").strip() or None
        override_dt_id = (request.POST.get("override_device_type_id") or "").strip() or None
        override_platform_id = (request.POST.get("override_platform_id") or "").strip() or None

        override_device_type = None
        if override_dt_id:
            from dcim.models import DeviceType

            try:
                override_device_type = DeviceType.objects.get(pk=int(override_dt_id))
            except (DeviceType.DoesNotExist, ValueError, TypeError):
                return HttpResponse("Invalid override_device_type_id", status=400)

        override_platform = None
        if override_platform_id:
            from dcim.models import Platform

            try:
                override_platform = Platform.objects.get(pk=int(override_platform_id))
            except (Platform.DoesNotExist, ValueError, TypeError):
                return HttpResponse("Invalid override_platform_id", status=400)

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return HttpResponse("LibreNMS device not found", status=404)

        promote = validation.get("promote_to_host") if validation else None
        if not promote:
            return HttpResponse("Promotion is not applicable for this device", status=400)
        if (validation.get("existing_device") or existing_device).pk != existing_device.pk:
            return HttpResponse(
                "Device ID mismatch: existing_device_id does not match validation result",
                status=400,
            )

        new_host_id = libre_device.get("device_id")
        if isinstance(new_host_id, bool):
            return HttpResponse("Invalid or missing LibreNMS device_id", status=400)
        try:
            new_host_id = int(new_host_id)
        except (TypeError, ValueError):
            return HttpResponse("Invalid or missing LibreNMS device_id", status=400)
        if new_host_id <= 0:
            return HttpResponse("Invalid LibreNMS device_id", status=400)

        existing_libre_id = promote.get("existing_libre_id")
        try:
            existing_libre_id = int(existing_libre_id)
        except (TypeError, ValueError):
            return HttpResponse("Invalid existing LibreNMS id in promotion data", status=400)
        if existing_libre_id == new_host_id:
            return HttpResponse("Existing link already points at this LibreNMS device", status=409)

        oob_type = promote.get("existing_oob_type") or ""
        if not oob_type:
            return HttpResponse("Cannot determine OOB type for promotion", status=400)

        from netbox_librenms_plugin.utils import set_librenms_device_id, set_librenms_oob

        server_key = self.librenms_api.server_key

        # Pull stored OOB metadata (ip/version) from existing librenms link if possible,
        # then ensure the incoming device's IP populates oob_ip on the existing device
        # only if oob_ip isn't already set.
        existing_oob_ip = None
        oob_ip_str = None
        cf_value = existing_device.custom_field_data.get("librenms_id")
        if isinstance(cf_value, dict):
            entry = cf_value.get(server_key)
            if isinstance(entry, dict):
                existing_oob_dict = entry.get("oob") if isinstance(entry.get("oob"), dict) else {}
                existing_oob_ip = existing_oob_dict.get("ip") if isinstance(existing_oob_dict, dict) else None
        # Prefer existing OOB ip if it was already known; otherwise use existing device's
        # current oob_ip relationship; otherwise leave unset (do NOT inherit incoming
        # device's IP — that IP belongs to the host, not the OOB controller).
        if not existing_oob_ip and existing_device.oob_ip_id:
            try:
                from ipam.models import IPAddress  # noqa

                existing_oob_ip = str(existing_device.oob_ip).split("/")[0] if existing_device.oob_ip else None
            except Exception:  # pragma: no cover - defensive
                existing_oob_ip = None
        oob_ip_str = existing_oob_ip or None

        # Reject legacy bare-int librenms_id form (caller should migrate first).
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
                "Device has a legacy bare-integer librenms_id; use 'Convert mapping' to migrate first.",
                status=409,
            )

        with transaction.atomic():
            try:
                existing_device = Device.objects.select_for_update().get(pk=existing_device.pk)
            except Device.DoesNotExist:
                return HttpResponse(
                    "Device no longer exists; it may have been deleted concurrently.",
                    status=409,
                )

            try:
                # First, swap the host id to the incoming LibreNMS device id.
                # set_librenms_device_id preserves any existing OOB sub-object.
                set_librenms_device_id(
                    existing_device,
                    new_host_id,
                    server_key=server_key,
                )
                # Then attach the previously-linked LibreNMS id as the OOB controller.
                set_librenms_oob(
                    existing_device,
                    existing_libre_id,
                    server_key,
                    oob_type=oob_type,
                    ip=oob_ip_str,
                )
            except ValueError as exc:
                return HttpResponse(f"Invalid promotion data: {escape(str(exc))}", status=400)

            # After promotion, populate IP relationships on the existing device:
            #   - primary_ip4 / primary_ip6 from the incoming LibreNMS host's IP
            #     (the device that's now linked as the host)
            #   - oob_ip from the previously-linked LibreNMS device's IP
            #     (the device that's now demoted into the OOB slot)
            # If the IP does not yet exist in NetBox we create a global /32 (or
            # /128) entry so the device row looks complete after promotion;
            # the user can re-home / mask it later via the IP-sync flow.
            # Both writes are best-effort and never overwrite an already-set
            # primary_ip4 / primary_ip6 / oob_ip relationship.
            update_fields = ["custom_field_data"]

            host_ip_str = (libre_device.get("ip") or "").strip() or None
            if host_ip_str:
                _auto_create = resolve_auto_create_ipam(request)
                host_ip, host_ip_created = get_or_create_global_ip(host_ip_str, auto_create=_auto_create)
                if host_ip is not None:
                    try:
                        is_v6 = _ipaddr_parse(host_ip_str).version == 6
                    except ValueError:
                        is_v6 = False
                    assigned = False
                    if is_v6 and existing_device.primary_ip6_id is None:
                        existing_device.primary_ip6 = host_ip
                        update_fields.append("primary_ip6")
                        assigned = True
                    elif not is_v6 and existing_device.primary_ip4_id is None:
                        existing_device.primary_ip4 = host_ip
                        update_fields.append("primary_ip4")
                        assigned = True
                    if host_ip_created and assigned:
                        messages.info(
                            request,
                            f"Auto-created primary IP {host_ip_str} in IPAM (unassigned, global scope).",
                        )

            # Fetch OOB device's IP from LibreNMS if we don't already have one
            # cached in CFD / the relationship.
            if not oob_ip_str and existing_device.oob_ip_id is None:
                try:
                    ok, oob_info = self.librenms_api.get_device_info(existing_libre_id)
                except Exception:  # pragma: no cover - defensive
                    ok, oob_info = False, None
                if ok and isinstance(oob_info, dict):
                    fetched_oob_ip = (oob_info.get("ip") or "").strip() or None
                    if fetched_oob_ip:
                        oob_ip_str = fetched_oob_ip
                        # Re-write the OOB sub-object so the IP is also cached in CFD.
                        try:
                            set_librenms_oob(
                                existing_device,
                                existing_libre_id,
                                server_key,
                                oob_type=oob_type,
                                ip=oob_ip_str,
                            )
                        except ValueError:  # pragma: no cover - defensive
                            pass

            if oob_ip_str and existing_device.oob_ip_id is None:
                oob_ip, oob_ip_created = get_or_create_global_ip(
                    oob_ip_str, auto_create=resolve_auto_create_ipam(request)
                )
                if oob_ip is not None:
                    existing_device.oob_ip = oob_ip
                    update_fields.append("oob_ip")
                    if oob_ip_created:
                        messages.info(
                            request,
                            f"Auto-created OOB IP {oob_ip_str} in IPAM (unassigned, global scope).",
                        )

            # Apply any explicit per-field overrides chosen in the pre-promote modal.
            # Default behaviour (no overrides) keeps the existing device's name, type
            # and platform — matching the original promote semantics.
            if override_name and override_name != existing_device.name:
                existing_device.name = override_name
                update_fields.append("name")
            if override_device_type and existing_device.device_type_id != override_device_type.pk:
                existing_device.device_type = override_device_type
                update_fields.append("device_type")
            if override_platform and existing_device.platform_id != override_platform.pk:
                existing_device.platform = override_platform
                update_fields.append("platform")

            if err := _save_device(existing_device, update_fields=update_fields):
                return err

        logger.info(
            "Promoted LibreNMS host (id %d) to '%s' on server %s; demoted previous link (id %d, type %s) to OOB slot",
            new_host_id,
            existing_device.name,
            server_key,
            existing_libre_id,
            oob_type,
        )

        cache_key = get_import_device_cache_key(device_id, server_key)
        cache.delete(cache_key)

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return HttpResponse("Device not found after action", status=404)

        response = self.render_device_row(request, libre_device, validation, selections)
        # Keep the underlying validation modal open and re-fetch its content so
        # the user can see the device's new link state (host id + OOB slot)
        # without losing context. The JS handler in librenms_import.js fires a
        # fresh GET to the validation URL using the row's existing details
        # button.
        response["HX-Trigger"] = json.dumps({"validationRefresh": {"deviceId": device_id}})
        return response


class MergeNetBoxDevicesView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View
):
    """
    Merge two existing NetBox devices that represent the same physical box.

    The user (via radio buttons in the validation modal) picks which device is
    the **winner** (kept) and which is the **donor** (absorbed). The donor's
    LibreNMS link state under the active ``server_key`` is merged into the
    winner; the donor's active link is then cleared and a ``_migrated_to``
    marker is written.  Interfaces, cables and primary IPs are NOT moved —
    those stay on the donor for the user to re-home incrementally via the
    Stage-2b "Migrated to X" tab.
    """

    def post(self, request, device_id):
        if error := self.require_write_permission():
            return error

        from dcim.models import Device
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import (
            mark_librenms_migrated,
            merge_librenms_links,
        )

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            self._librenms_api = LibreNMSAPI(server_key=post_server_key)

        winner_pk_raw = request.POST.get("winner_pk")
        donor_pk_raw = request.POST.get("donor_pk")
        if not winner_pk_raw or not donor_pk_raw:
            return HttpResponse("Missing winner_pk or donor_pk", status=400)
        try:
            winner_pk = int(winner_pk_raw)
            donor_pk = int(donor_pk_raw)
        except (TypeError, ValueError):
            return HttpResponse("Invalid winner_pk or donor_pk", status=400)
        if winner_pk == donor_pk:
            return HttpResponse("Winner and donor must be different devices", status=400)

        try:
            winner = Device.objects.get(pk=winner_pk)
            donor = Device.objects.get(pk=donor_pk)
        except Device.DoesNotExist:
            return HttpResponse("Winner or donor device not found", status=404)

        # Permission gate: user must be able to change BOTH devices.
        self.required_object_permissions = {"POST": [("change", Device)]}
        if error := self.require_object_permissions("POST"):
            return error

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return HttpResponse("LibreNMS device not found", status=404)

        merge_candidates = (validation or {}).get("merge_candidates") or {}
        candidate_pks = {
            (merge_candidates.get("host_named") or {}).get("pk"),
            (merge_candidates.get("oob_named") or {}).get("pk"),
        }
        candidate_pks.discard(None)
        if {winner_pk, donor_pk} != candidate_pks:
            return HttpResponse(
                "winner_pk/donor_pk do not match the validation result's merge candidates",
                status=400,
            )

        # Reject legacy bare-int librenms_id form on either side. The merge
        # helpers refuse to operate on legacy data to prevent silent migration.
        for label, obj in (("winner", winner), ("donor", donor)):
            stored = obj.custom_field_data.get("librenms_id")
            is_legacy = isinstance(stored, int) and not isinstance(stored, bool)
            if not is_legacy and isinstance(stored, str):
                try:
                    int(stored)
                    is_legacy = True
                except (ValueError, TypeError):
                    pass
            if is_legacy:
                return HttpResponse(
                    f"{label.capitalize()} device has a legacy bare-integer librenms_id; "
                    "use 'Convert mapping' to migrate before merging.",
                    status=409,
                )

        server_key = self.librenms_api.server_key

        with transaction.atomic():
            # Lock both rows in deterministic pk order to avoid deadlocks.
            locked = list(Device.objects.select_for_update().filter(pk__in=[winner_pk, donor_pk]).order_by("pk"))
            if len(locked) != 2:
                return HttpResponse(
                    "One of the devices no longer exists; it may have been deleted concurrently.",
                    status=409,
                )
            locked_by_pk = {d.pk: d for d in locked}
            winner = locked_by_pk[winner_pk]
            donor = locked_by_pk[donor_pk]

            try:
                summary = merge_librenms_links(winner, donor, server_key=server_key)
            except ValueError as exc:
                return HttpResponse(f"Cannot merge: {escape(str(exc))}", status=400)

            # Transfer OOB IP relationship if winner has none and donor has one.
            oob_ip_transferred = False
            if donor.oob_ip_id and not winner.oob_ip_id:
                winner.oob_ip = donor.oob_ip
                donor.oob_ip = None
                oob_ip_transferred = True

            # Clear donor's active link and stamp migration marker.
            mark_librenms_migrated(donor, winner.pk, server_key=server_key)

            # Persist only the fields we actually touched.  Calling
            # ``full_clean()`` here (or relying on it via ``_save_device``)
            # would re-validate every field on the device — which is
            # undesirable when the rows hold pre-existing inconsistencies
            # (e.g. ``face`` set without ``rack``) that are unrelated to
            # this merge.  See issue surfaced during eve-ng-02 merge.
            update_fields = ["custom_field_data"]
            if oob_ip_transferred:
                update_fields.append("oob_ip")
            try:
                winner.save(update_fields=update_fields)
                donor.save(update_fields=update_fields)
            except Exception as exc:  # pragma: no cover - defensive
                return HttpResponse(f"Save failed: {escape(str(exc))}", status=500)

        logger.info(
            "Merged NetBox device '%s' (pk=%d) into '%s' (pk=%d) on server %s. Summary: %s; oob_ip_transferred=%s",
            donor.name,
            donor.pk,
            winner.name,
            winner.pk,
            server_key,
            summary,
            oob_ip_transferred,
        )

        cache_key = get_import_device_cache_key(device_id, server_key)
        cache.delete(cache_key)

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return HttpResponse("Device not found after merge", status=404)

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
