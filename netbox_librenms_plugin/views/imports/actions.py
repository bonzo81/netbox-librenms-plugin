"""HTMX endpoints and POST handlers for importing LibreNMS devices."""

import json
import logging
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
    # Skip the OOB swap when nothing is queued: an empty #django-messages container
    # would replace (and wipe) toasts already visible on the page from an earlier action.
    storage = messages.get_messages(request)
    if not list(storage):
        return response
    storage.used = False
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


def _save_device(device, update_fields: list[str] | None = None, request=None) -> HttpResponse | None:
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

    When ``request`` is provided and the request is an HTMX request, errors
    are returned via ``_htmx_error_response()`` so modal swap/toast flows
    remain intact.  Otherwise plain ``HttpResponse`` status codes are returned.
    """
    from django.db import DatabaseError, DataError, IntegrityError

    def _err(msg: str, status: int) -> HttpResponse:
        if request is not None and request.META.get("HTTP_HX_REQUEST"):
            return _htmx_error_response(msg)
        return HttpResponse(escape(msg), status=status)

    if update_fields is None:
        try:
            device.full_clean()
        except ValidationError as exc:
            error_msg = exc.message_dict if hasattr(exc, "message_dict") else str(exc)
            return _err(f"Validation error: {error_msg}", 400)
        try:
            device.save()
        except IntegrityError as exc:
            return _err(f"Integrity error: {exc}", 409)
        return None

    try:
        device.save(update_fields=update_fields)
    except IntegrityError as exc:
        return _err(f"Integrity error: {exc}", 409)
    except ValidationError as exc:
        error_msg = exc.message_dict if hasattr(exc, "message_dict") else str(exc)
        return _err(f"Validation error: {error_msg}", 400)
    except DataError as exc:
        # save(update_fields=...) skips full_clean(), so an overlong/invalid value
        # from LibreNMS (e.g. a hostname past Device.name max_length) reaches the DB
        # and raises DataError. Convert it to a clean toast instead of a 500.
        return _err(f"Invalid field value: {exc}", 400)
    except DatabaseError as exc:
        # save(update_fields=...) forces an UPDATE; if a concurrent delete removed the
        # row it affects 0 rows and Django raises DatabaseError ("did not affect any
        # rows" / Model.NotUpdated). Several callers don't re-lock the row first, so
        # surface this as a toast rather than a 500. (Must follow the IntegrityError /
        # DataError handlers above — both subclass DatabaseError.)
        return _err(f"Database error: {exc}", 409)
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
            from netbox_librenms_plugin.librenms_api import build_librenms_api

            # Unknown/tampered server_key would otherwise raise → 500.
            self._librenms_api = build_librenms_api(post_server_key)
            if self._librenms_api is None:
                return _htmx_error_response("Selected LibreNMS server is no longer configured.")

        device_ids = request.POST.getlist("select")
        if not device_ids:
            # This is HTMX modal content (hx-target=#htmx-modal-content). htmx does not
            # swap 4xx responses by default, so a 400 here would leave the alert unrendered
            # (the JS fallback would surface the raw HTML as toast text). Return 200 so the
            # styled alert renders in-place, matching the collision interstitial below.
            return HttpResponse('<div class="alert alert-warning mb-0">Select at least one device.</div>')

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
            # All branches return 200 (not 400): this is HTMX modal content swapped into
            # #htmx-modal-content, and htmx does not swap 4xx responses by default.
            # Check if this is due to cache expiration
            if cache_expired_count > 0 and cache_expired_count == len(seen_ids):
                return HttpResponse(
                    '<div class="alert alert-warning mb-0">'
                    '<i class="mdi mdi-clock-alert"></i> '
                    "<strong>Filter results have expired.</strong><br>"
                    "The device data is no longer available in cache (5-minute timeout). "
                    'Please <a href="javascript:window.location.reload();" class="alert-link">refresh the page</a> '
                    "or re-run your filter to reload device data."
                    "</div>"
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
                    "</div>"
                )
            else:
                # Generic error - validation failed for all devices
                return HttpResponse(
                    '<div class="alert alert-danger mb-0">'
                    "No valid devices selected. "
                    f"{len(errors)} error(s) occurred: {' '.join(escape(e) for e in errors) if errors else 'Please check device validation status.'}"
                    "</div>"
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
            # Render at 200 (not 4xx): this is an interstitial modal swapped
            # into #htmx-modal-content, exactly like the confirm step. A non-2xx
            # status makes HTMX skip the swap and route the body through
            # htmx:responseError -> showErrorToast(), which would dump the
            # collision template as raw text in a toast.
            return render(
                request,
                "netbox_librenms_plugin/htmx/bulk_import_collision.html",
                {"collisions": collisions, "device_count": len(devices)},
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

        # HTMX responses below render device rows / OOB-toast fragments that do NOT
        # consume the Django message queue, so any messages.* queued on the HTMX path
        # would leak onto the next full page load. Queue them only for non-HTMX requests.
        # (4xx bodies below still surface on the HTMX path via the client's
        # htmx:responseError -> showErrorToast fallback.)
        is_htmx = bool(request.headers.get("HX-Request"))

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            from netbox_librenms_plugin.librenms_api import build_librenms_api

            # Unknown/tampered server_key would otherwise raise → 500.
            self._librenms_api = build_librenms_api(post_server_key)
            if self._librenms_api is None:
                msg = "Selected LibreNMS server is no longer configured."
                # HTMX gets a 200 OOB toast; a non-HTMX POST must get the normal
                # message + redirect flow, not a toast fragment served as a full page.
                if is_htmx:
                    return _htmx_error_response(msg)
                messages.error(request, msg)
                return redirect("plugins:netbox_librenms_plugin:librenms_import")

        device_ids = request.POST.getlist("select")
        if not device_ids:
            # HTMX gets a raw 400 (surfaced client-side via htmx:responseError →
            # showErrorToast); a non-HTMX POST gets the message + redirect flow so
            # full-page users actually see the error, not a bare 400 body.
            if is_htmx:
                return HttpResponse("No devices selected", status=400)
            messages.error(request, "No devices selected for import")
            return redirect("plugins:netbox_librenms_plugin:librenms_import")

        try:
            parsed_ids = [int(device_id) for device_id in device_ids]
        except (TypeError, ValueError):
            if is_htmx:
                return HttpResponse("Invalid device identifier", status=400)
            messages.error(request, "Invalid device identifier supplied")
            return redirect("plugins:netbox_librenms_plugin:librenms_import")

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
                if not is_htmx:
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

        # Build summary text once. The two response paths surface it differently and must
        # not double up: the HTMX path renders device_import_row.html (which deliberately
        # does NOT include inc/messages.html) and shows htmx_toasts via an OOB swap, so any
        # messages.* queued here would NOT be consumed and would leak onto the next full
        # page load as stale/duplicate toasts. So only queue Django messages for the
        # non-HTMX (redirect) path; the HTMX path relies solely on htmx_toasts. (is_htmx
        # was resolved at the top of post().)
        htmx_toasts = []  # [(bg_class, mdi_class, label, text), ...]

        if success_count:
            _msg = f"Successfully imported {success_count} LibreNMS device{'s' if success_count != 1 else ''}"
            if not is_htmx:
                messages.success(request, _msg)
            htmx_toasts.append(("text-bg-success", "mdi-check-circle", "Success", _msg))

        if failed_count:
            _msg = f"Failed to import {failed_count} device{'s' if failed_count != 1 else ''}"
            if not is_htmx:
                messages.error(request, _msg)
            htmx_toasts.append(("text-bg-danger", "mdi-alert-circle", "Error", _msg))
        if skipped_count:
            _msg = f"Skipped {skipped_count} existing device{'s' if skipped_count != 1 else ''}"
            if not is_htmx:
                messages.warning(request, _msg)
            htmx_toasts.append(("text-bg-warning", "mdi-alert", "Warning", _msg))

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

            # Append all summary toasts as a single OOB swap. In the HTMX path no
            # Django messages were queued (see above), so this OOB fragment is the
            # sole carrier of the import summary.
            if htmx_toasts:
                toast_items = mark_safe(
                    "".join(
                        format_html(
                            '<div class="toast toast-dark border-0 shadow-sm mb-1" role="alert"'
                            ' aria-live="assertive" aria-atomic="true" data-bs-delay="12000">'
                            '<div class="toast-header {}">'
                            '<i class="mdi {} me-1"></i>{}'
                            '<button type="button" class="btn-close me-0 m-auto"'
                            ' data-bs-dismiss="toast" aria-label="Close"></button>'
                            "</div>"
                            '<div class="toast-body">{}</div>'
                            "</div>",
                            bg_cls,
                            icon_cls,
                            label,
                            text,
                        )
                        for bg_cls, icon_cls, label, text in htmx_toasts
                    )
                )
                updated_rows_html.append(
                    format_html(
                        '<div id="django-messages"'
                        ' class="toast-container position-fixed bottom-0 end-0 p-3"'
                        ' hx-swap-oob="true">{}</div>',
                        toast_items,
                    )
                )
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


def _suggest_oob_interface(device, oob_candidate):
    """Return ``(suggested_interface_id, default_new_name)`` for an OOB IP.

    NetBox requires ``oob_ip`` be assigned to one of the device's interfaces,
    so the OOB-attach form lets the user pick (or create) one. This pre-selects
    the existing interface whose name looks like an OOB/management port
    (idrac/ilo/ipmi/bmc/drac/oob/mgmt), or ``None`` if there's no obvious match,
    and derives a sensible default name for a new interface from the OOB type
    (e.g. ``idrac0``). The OOB IP is frequently *not* physically on the matched
    interface — operators attach it to an ``idrac0``-style port deliberately —
    so this is only a suggestion the user can override.
    """
    import re as _re

    oob_type = (oob_candidate.get("type") or "oob").strip().lower() or "oob"
    default_new_name = f"{oob_type}0"
    pattern = _re.compile(r"(idrac|ilo|ipmi|bmc|drac|oob|mgmt|management)", _re.IGNORECASE)
    for iface in device.interfaces.all():
        if pattern.search(iface.name or ""):
            return iface.pk, default_new_name
    return None, default_new_name


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
            # OOB-attach needs an interface to hang the OOB IP on (NetBox requires
            # oob_ip be interface-assigned). Offer the device's interfaces with a
            # sensible default pre-selected.
            if validation.get("oob_candidate") and existing._meta.model_name == "device":
                context["oob_interfaces"] = list(existing.interfaces.all())
                (
                    context["oob_suggested_interface_id"],
                    context["oob_default_new_name"],
                ) = _suggest_oob_interface(existing, validation["oob_candidate"])

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
            # New dict form {server_key: {"id": N, "oob": {...}}} — display the host id.
            # OOB-only entries ({"oob": {...}} with no "id") have no host mapping to show
            # in this import-action modal and are skipped.
            if isinstance(did, dict):
                did = did.get("id")
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
        from netbox_librenms_plugin.librenms_api import build_librenms_api

        action = request.POST.get("action")
        existing_device_id = request.POST.get("existing_device_id")
        existing_device_type = request.POST.get("existing_device_type", "device")

        # If the form submitted a specific server_key, honour it so the handler uses
        # the same server context as the import page when the user clicked the button.
        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            # Unknown/tampered server_key would otherwise raise → 500.
            self._librenms_api = build_librenms_api(post_server_key)
            if self._librenms_api is None:
                return _htmx_error_response("Selected LibreNMS server is no longer configured.")

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
                    if err := _save_device(existing_device, update_fields=fields, request=request):
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
                    if err := _save_device(existing_device, update_fields=fields, request=request):
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
                    if err := _save_device(existing_device, update_fields=fields, request=request):
                        return err
                    logger.info(
                        f"Updated serial on device '{existing_device.name}' to {incoming_serial}, "
                        f"linked to LibreNMS ID {librenms_id}"
                    )

        elif action == "sync_name":
            # Sync device name from LibreNMS (e.g., IP → sysName)
            hostname = _get_hostname_for_action(request, validation, libre_device)
            existing_device.name = hostname
            if err := _save_device(existing_device, update_fields=["name"], request=request):
                return err
            logger.info(f"Synced name on device '{existing_device.name}' from LibreNMS")

        elif action == "update_type":
            # Update device type from LibreNMS (requires force for mismatch)
            if librenms_device_type:
                existing_device.device_type = librenms_device_type
                if err := _save_device(existing_device, update_fields=["device_type"], request=request):
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
                    if err := _save_device(locked_device, update_fields=["serial"], request=request):
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
                    if err := _save_device(existing_device, update_fields=["platform"], request=request):
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
                if err := _save_device(existing_device, update_fields=["device_type"], request=request):
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
            from netbox_librenms_plugin.librenms_api import build_librenms_api

            # Unknown/tampered server_key would otherwise raise → 500.
            self._librenms_api = build_librenms_api(post_server_key)
            if self._librenms_api is None:
                return _htmx_error_response("Selected LibreNMS server is no longer configured.")

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

        # Reject ambiguous state up front: multiple case-variant rows for the same
        # hardware string mean .first() would silently mutate an arbitrary one and leave
        # the duplicate unresolved. Mirrors AddPlatformMappingView.
        if DeviceTypeMapping.objects.filter(librenms_hardware__iexact=hardware).count() > 1:
            return _htmx_error_response(
                "Multiple mappings exist for this hardware string. Remove duplicates before updating."
            )
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
                # INSERT from slipping through undetected). Materialise [:2] in one query
                # (count() would drop the FOR UPDATE clause) and reject a concurrently-
                # created duplicate rather than mutating an arbitrary row.
                locked_rows = list(
                    DeviceTypeMapping.objects.select_for_update().filter(librenms_hardware__iexact=hardware)[:2]
                )
                if len(locked_rows) > 1:
                    return _htmx_error_response(
                        "Multiple mappings exist for this hardware string. Remove duplicates before updating."
                    )
                locked = locked_rows[0] if locked_rows else None
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
            from netbox_librenms_plugin.librenms_api import build_librenms_api

            # Unknown/tampered server_key would otherwise raise → 500.
            self._librenms_api = build_librenms_api(post_server_key)
            if self._librenms_api is None:
                return _htmx_error_response("Selected LibreNMS server is no longer configured.")

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
        current_platform = None
        if validation:
            existing = validation.get("existing_device")
            if existing:
                device_pk = existing.pk
                current_platform = getattr(existing, "platform", None)
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
                # Enable the "map to existing platform" section of the combined modal.
                "libre_device": libre_device,
                "current_platform": current_platform,
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
            from netbox_librenms_plugin.librenms_api import build_librenms_api

            # Unknown/tampered server_key would otherwise raise → 500.
            self._librenms_api = build_librenms_api(post_server_key)
            if self._librenms_api is None:
                return _htmx_error_response("Selected LibreNMS server is no longer configured.")

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

        librenms_os = (request.POST.get("librenms_os") or "").strip().lower()

        # Require ("add", PlatformMapping) only when a mapping row will actually be
        # written below — i.e. the toggle is on, an OS was supplied, and no mapping for
        # that OS exists yet (mirrors the write guard further down). Demanding the perm
        # for a write that won't happen would needlessly block creating the Platform.
        # (The write block re-checks not-exists, so a concurrent insert can't slip through.)
        will_create_mapping = (
            create_mapping
            and bool(librenms_os)
            and not PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).exists()
        )
        perms = [("add", Platform)]
        if will_create_mapping:
            perms.append(("add", PlatformMapping))
        if target_model is not None:
            perms.append(("change", target_model))
        self.required_object_permissions = {"POST": perms}

        if error := self.require_object_permissions("POST"):
            return error

        platform_name = (request.POST.get("platform_name") or "").strip()
        manufacturer_id = (request.POST.get("manufacturer") or "").strip()

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
                        # Persist only the changed FK via update_fields (skips full_clean()).
                        # Running full_clean() on an existing row can abort on unrelated legacy
                        # data we're not touching; the partial-save pattern used elsewhere in
                        # this module avoids blocking the platform assignment on it.
                        target.save(update_fields=["platform"])
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
        except ValidationError as exc:
            logger.exception("CreatePlatformFromImportView: validation failed while creating platform")
            detail = exc.message_dict if hasattr(exc, "message_dict") else str(exc)
            return _htmx_error_response(f"Error creating platform: {detail}")
        except IntegrityError:
            logger.exception("CreatePlatformFromImportView: integrity error while creating platform")
            return _htmx_error_response(
                "Error creating platform due to a database constraint. Please try again or contact an administrator."
            )

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


class AddAsOOBView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View
):
    """HTMX view to link a LibreNMS OOB controller device to an existing NetBox Device."""

    def post(self, request, device_id):
        """Attach a LibreNMS OOB identity to the matched NetBox device."""
        if error := self.require_write_permission():
            return error

        from dcim.models import Device
        from netbox_librenms_plugin.librenms_api import build_librenms_api

        existing_device_id = request.POST.get("existing_device_id")
        if not existing_device_id:
            return _htmx_error_response("Missing existing_device_id")

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            # Unknown/tampered server_key would otherwise raise → 500.
            self._librenms_api = build_librenms_api(post_server_key)
            if self._librenms_api is None:
                return _htmx_error_response("Selected LibreNMS server is no longer configured.")

        try:
            existing_device = Device.objects.get(pk=int(existing_device_id))
        except (Device.DoesNotExist, ValueError):
            return _htmx_error_response("Existing device not found")

        self.required_object_permissions = {"POST": [("change", Device)]}
        if error := self.require_object_permissions("POST"):
            return error

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return _htmx_error_response("LibreNMS device not found")

        oob_candidate = validation.get("oob_candidate") if validation else None
        if not oob_candidate:
            return _htmx_error_response("No OOB candidate found in validation data")
        if oob_candidate["device"].pk != existing_device.pk:
            return _htmx_error_response("Device ID mismatch: existing_device_id does not match OOB candidate")

        librenms_id = libre_device.get("device_id")
        if isinstance(librenms_id, bool):
            return _htmx_error_response("Invalid or missing LibreNMS device_id")
        try:
            librenms_id = int(librenms_id)
        except (TypeError, ValueError):
            return _htmx_error_response("Invalid or missing LibreNMS device_id")
        if librenms_id <= 0:
            return _htmx_error_response("Invalid LibreNMS device_id")

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
            return _htmx_error_response(
                "Device has a legacy bare-integer librenms_id; use 'Convert mapping' to migrate first."
            )

        from netbox_librenms_plugin.utils import set_librenms_oob

        oob_type = oob_candidate.get("type") or ""
        oob_ip_str = oob_candidate.get("ip") or None
        server_key = self.librenms_api.server_key

        with transaction.atomic():
            try:
                existing_device = Device.objects.select_for_update().get(pk=existing_device.pk)
            except Device.DoesNotExist:
                return _htmx_error_response("Device no longer exists; it may have been deleted concurrently.")

            from netbox_librenms_plugin.utils import (
                coerce_librenms_id,
                find_by_librenms_id,
                get_librenms_device_id,
                get_librenms_oob,
            )

            # Reject if the locked OOB link differs from what this (possibly stale) modal
            # is about to write — by id OR by type. oob_type is already a canonical OOB_TYPES
            # token (so is the stored current_oob["type"]), so this is a like-for-like compare
            # that won't false-trip on an idempotent re-attach; it does catch a concurrent
            # re-detection that changed the controller type.
            current_oob = get_librenms_oob(existing_device, server_key=server_key)
            if current_oob and (
                coerce_librenms_id(current_oob.get("id")) != coerce_librenms_id(librenms_id)
                or (current_oob.get("type") or "") != oob_type
            ):
                return _htmx_error_response("OOB link was modified concurrently; refresh and retry.")

            # A concurrent change could have re-linked THIS device's host id to the incoming
            # OOB id; attaching it as OOB would then store it in both the host slot and oob.id
            # — a self host/OOB conflict. Reject that explicitly (find_by_librenms_id below
            # would match self and wave it through).
            current_host_id = get_librenms_device_id(existing_device, server_key=server_key, auto_save=False)
            if coerce_librenms_id(current_host_id) == coerce_librenms_id(librenms_id):
                return _htmx_error_response(
                    f"LibreNMS device #{librenms_id} is this device's host link; it can't also be its "
                    "OOB controller. Refresh and retry."
                )

            # Another device may already own this LibreNMS id (as its host id or OOB id)
            # since validation ran. Re-check inside the transaction and abort on a non-self
            # conflict so we don't point one LibreNMS device at two NetBox devices. Mirrors
            # PromoteToHostView's host_conflict guard; find_by_librenms_id is an unlocked
            # read, so this narrows — not closes — the window (no unique constraint on the cf).
            oob_conflict = find_by_librenms_id(Device, librenms_id, server_key)
            if oob_conflict is not None and oob_conflict.pk != existing_device.pk:
                return _htmx_error_response(
                    f"LibreNMS device #{librenms_id} is already linked to '{escape(oob_conflict.name)}'; "
                    "refresh and retry."
                )

            try:
                set_librenms_oob(
                    existing_device,
                    librenms_id,
                    server_key,
                    oob_type=oob_type,
                )
            except ValueError as exc:
                return _htmx_error_response(f"Invalid OOB data: {escape(str(exc))}")

            update_fields = ["custom_field_data"]

            # Buffer OOB status messages and emit them only after the transaction
            # commits — a message queued before _save_device() would survive a
            # rollback and falsely claim the OOB link/IP was applied.
            deferred_messages = []

            # Set device.oob_ip from an interface-assigned IPAddress. NetBox
            # requires oob_ip be assigned to one of the device's interfaces, so
            # the user picks (or creates) the interface to hang the OOB IP on
            # via the OOB-attach form. Linkage (set_librenms_oob) happened above.
            if oob_ip_str and existing_device.oob_ip_id is None:
                # The top-level gate only authorizes ("change", Device), but the
                # IP-set sub-flow can create an Interface, create an IPAddress, or
                # re-home an existing one. Require the model perms the requested
                # operation actually needs; if missing, skip the IP-set (the link
                # still commits) rather than hard-failing — a return here would
                # roll back the whole transaction, including the linkage.
                perm_warning = self._missing_oob_ip_permissions(request, oob_ip_str, device=existing_device)
                oob_iface, iface_reason = (
                    (None, None) if perm_warning else self._resolve_oob_interface(request, existing_device)
                )
                if perm_warning:
                    deferred_messages.append((messages.WARNING, perm_warning))
                elif iface_reason == "permission_add":
                    # A concurrent delete turned an interface reuse into a create after the
                    # pre-flight; the write-time re-check refused it for an add-lacking user.
                    deferred_messages.append(
                        (
                            messages.WARNING,
                            f"OOB linked, but OOB IP {oob_ip_str} not set — you lack permission to add an interface.",
                        )
                    )
                elif oob_iface is None:
                    deferred_messages.append(
                        (
                            messages.INFO,
                            "OOB linked. Choose an interface in the OOB form to also set the device's OOB IP.",
                        )
                    )
                else:
                    oob_ip, attach_reason = self._attach_oob_ip(request, oob_ip_str, oob_iface)
                    if oob_ip is None:
                        if attach_reason == "permission_change":
                            msg = (
                                f"OOB linked, but OOB IP {oob_ip_str} not set — you lack permission "
                                "to reassign the existing IP address."
                            )
                        elif attach_reason == "permission_add":
                            msg = (
                                f"OOB linked, but OOB IP {oob_ip_str} not set — you lack permission "
                                "to add a new IP address."
                            )
                        else:
                            msg = (
                                f"OOB linked, but couldn't set OOB IP {oob_ip_str} "
                                "(invalid, or already assigned to another device)."
                            )
                        deferred_messages.append((messages.WARNING, msg))
                    else:
                        existing_device.oob_ip = oob_ip
                        update_fields.append("oob_ip")
                        deferred_messages.append(
                            (messages.INFO, f"Set OOB IP {oob_ip_str} on interface {oob_iface.name}.")
                        )

            if err := _save_device(existing_device, update_fields=update_fields, request=request):
                # _save_device returns an error response (it doesn't raise), so returning
                # here would exit the atomic block normally and COMMIT the Interface/IP
                # rows created above by _resolve_oob_interface()/_attach_oob_ip(). Mark the
                # transaction rollback-only so those side effects are discarded too.
                transaction.set_rollback(True)
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
            return _htmx_error_response("Device not found after action")

        # Transaction committed and the row response is about to render — now it is safe
        # to surface the deferred OOB messages. Queuing them earlier would leak them onto
        # the next full-page load if the revalidation above bailed with an HTMX error
        # (which never consumes the message queue).
        for level, text in deferred_messages:
            messages.add_message(request, level, text)

        response = self.render_device_row(request, libre_device, validation, selections)
        # Keep the validation modal open and refresh its contents in place so
        # the user can confirm the new OOB attachment without losing context.
        response["HX-Trigger"] = json.dumps({"validationRefresh": {"deviceId": device_id}})
        return response

    @staticmethod
    def _missing_oob_ip_permissions(request, ip_str, device=None):
        """Return a warning string naming missing perms for the OOB-IP set, or None.

        The OOB-attach view authorizes ``("change", Device)`` at the top, but the
        IP-set sub-flow can additionally create an :class:`Interface` (when the
        user picks ``__new__``), create an :class:`IPAddress` (no record for the
        host yet), or re-home an existing one. Check the model perms the requested
        operation actually needs so a caller with only Device-change rights can't
        mutate Interface/IPAddress through this view.
        """
        from dcim.models import Interface
        from ipam.models import IPAddress
        from utilities.permissions import get_permission_for_model

        needed = []
        iface_id = (request.POST.get("oob_interface_id") or "").strip()
        new_iface_name = (request.POST.get("oob_new_interface_name") or "").strip()
        if iface_id == "__new__" and new_iface_name:
            # _resolve_oob_interface() reuses an existing (device, name) interface, so no
            # Interface write happens then. Only require 'add' when it doesn't already
            # exist, so a change-Device user picking an existing name isn't needlessly
            # blocked. This is an unlocked pre-flight for the warning UX only —
            # _resolve_oob_interface() re-verifies 'add' from the locked row before any
            # real create, so a create-via-race can't slip past this read.
            exists = device is not None and Interface.objects.filter(device=device, name=new_iface_name).exists()
            if not exists:
                needed.append(("add", Interface))

        # Creating a new IPAddress needs 'add'; re-homing an existing one needs 'change'.
        # But _attach_oob_ip() does NOT save when the existing IP is already assigned to
        # the selected interface — in that case no IPAddress mutation occurs, so a user
        # with only Device-change rights should not be blocked.
        # Mirror _attach_oob_ip's ambiguity rule: net_host ignores prefix length, so more
        # than one row can match. Fetch up to two so an ambiguous match (which the write
        # path refuses) conservatively requires 'change' here rather than wrongly taking
        # the already-on-selected-interface shortcut and waving the request through.
        matches = list(IPAddress.objects.filter(address__net_host=ip_str)[:2])
        ambiguous = len(matches) > 1
        existing = matches[0] if matches else None
        if existing is None:
            needed.append(("add", IPAddress))
        else:
            already_on_selected_iface = False
            if not ambiguous and iface_id and iface_id != "__new__":
                try:
                    already_on_selected_iface = getattr(existing.assigned_object, "pk", None) == int(iface_id)
                except ValueError:
                    already_on_selected_iface = False
            if not already_on_selected_iface:
                needed.append(("change", IPAddress))

        missing = [
            perm
            for action, model in needed
            if not request.user.has_perm(perm := get_permission_for_model(model, action))
        ]
        if missing:
            return f"OOB linked, but OOB IP not set — missing permission(s): {', '.join(missing)}."
        return None

    @staticmethod
    def _resolve_oob_interface(request, device):
        """Resolve (or create) the interface the OOB IP should attach to.

        Reads ``oob_interface_id`` from the OOB-attach form: an interface PK, or
        the sentinel ``"__new__"`` to create one named ``oob_new_interface_name``.

        Returns ``(interface, None)`` on success, ``(None, None)`` when the user made
        no selection (linkage proceeds without setting ``oob_ip``), or
        ``(None, "permission_add")`` when creating a new interface is required but the
        user lacks Interface ``add``.

        The caller runs inside ``transaction.atomic()``. The add-vs-reuse permission
        decision can only be made from the locked row: the unlocked pre-flight in
        ``_missing_oob_ip_permissions`` can race a concurrent delete and wave through a
        change-Device-only user, so re-verify ``add`` here before creating. Symmetric to
        the re-check in :meth:`_attach_oob_ip`.
        """
        from django.db import IntegrityError
        from dcim.models import Interface
        from utilities.permissions import get_permission_for_model

        iface_id = (request.POST.get("oob_interface_id") or "").strip()
        if iface_id == "__new__":
            name = (request.POST.get("oob_new_interface_name") or "").strip()
            if not name:
                return None, None
            # Lock the candidate so a concurrent create/delete can't flip add-vs-reuse
            # between this check and the create below.
            existing = Interface.objects.select_for_update().filter(device=device, name=name).first()
            if existing is not None:
                return existing, None
            if not request.user.has_perm(get_permission_for_model(Interface, "add")):
                return None, "permission_add"
            # Nested savepoint: catching IntegrityError without one would poison the outer
            # transaction. A concurrent create of the same (device, name) — guarded by the
            # dcim_interface_unique_device_name constraint — means we just reuse the winner.
            try:
                with transaction.atomic():
                    return Interface.objects.create(device=device, name=name, type="other"), None
            except IntegrityError:
                # Lock the row we hand back: the OOB-IP assignment is generic-relational,
                # not FK-protected, so a concurrent delete before the IP save would orphan
                # oob_ip on a missing interface. select_for_update blocks that delete.
                existing = Interface.objects.select_for_update().filter(device=device, name=name).first()
                return (existing, None) if existing is not None else (None, None)
        if iface_id:
            try:
                # Lock the reused row too (same orphan-on-concurrent-delete reasoning).
                return Interface.objects.select_for_update().get(pk=int(iface_id), device=device), None
            except (Interface.DoesNotExist, ValueError):
                return None, None
        return None, None

    @staticmethod
    def _attach_oob_ip(request, ip_str, interface):
        """Resolve the OOB :class:`IPAddress` for *ip_str* assigned to *interface*.

        Returns ``(ip, None)`` on success, or ``(None, reason)`` where *reason* is
        one of ``"invalid"``, ``"conflict"`` (already on another device / create
        race), or ``"permission"``.

        Reuses an existing record for the host (matched via ``net_host`` so any
        prefix length is accepted) and re-homes it to *interface*, unless it is
        already assigned to a *different* device's object. Otherwise creates a
        ``/32`` (IPv4) or ``/128`` (IPv6).
        """
        from ipaddress import ip_address as _ip

        from django.db import IntegrityError
        from ipam.models import IPAddress
        from utilities.permissions import get_permission_for_model

        try:
            parsed = _ip(ip_str)
        except ValueError:
            return None, "invalid"

        # Lock the candidate row(s) — the caller runs inside transaction.atomic() — so a
        # concurrent attach can't flip the assignment between this ownership check and
        # the save. NetBox places no unique constraint on IPAddress.address, so the
        # create path stays best-effort: this narrows the TOCTOU window, it can't close it.
        # net_host ignores prefix length, so several rows can share the same host IP;
        # fetch up to two and refuse rather than re-home the wrong one by DB ordering.
        candidates = list(IPAddress.objects.select_for_update().filter(address__net_host=ip_str)[:2])
        if len(candidates) > 1:
            return None, "conflict"
        existing = candidates[0] if candidates else None
        if existing is not None:
            assigned = existing.assigned_object
            owned = assigned is None or getattr(assigned, "device_id", None) == interface.device_id
            if not owned:
                return None, "conflict"
            if assigned != interface:
                # Re-homing an existing IP is a 'change'. The add-vs-change permission
                # decision can only be made from the locked row: the unlocked pre-flight
                # in _missing_oob_ip_permissions can race a concurrent create and wave
                # through an 'add'-only user, so verify 'change' here before saving.
                if not request.user.has_perm(get_permission_for_model(IPAddress, "change")):
                    return None, "permission_change"
                existing.assigned_object = interface
                existing.save()
            return existing, None

        # No row exists under the lock → this is a create, which needs 'add'. Re-verify
        # it here: the unlocked pre-flight in _missing_oob_ip_permissions may have seen an
        # existing row (and so only checked 'change'); if a concurrent delete removed that
        # row between pre-flight and this lock, an 'add'-lacking user must not slip through.
        # Symmetric to the 'change' re-check on the re-home path above.
        if not request.user.has_perm(get_permission_for_model(IPAddress, "add")):
            return None, "permission_add"

        mask = "/128" if parsed.version == 6 else "/32"
        # Nested savepoint: the caller runs inside transaction.atomic(), and catching
        # IntegrityError from create() without one would leave the outer transaction
        # rollback-only (poisoned), breaking the later _save_device() call.
        try:
            with transaction.atomic():
                return (
                    IPAddress.objects.create(address=f"{ip_str}{mask}", assigned_object=interface, status="active"),
                    None,
                )
        except IntegrityError:
            return None, "conflict"


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
        from netbox_librenms_plugin.librenms_api import build_librenms_api

        existing_device_id = request.POST.get("existing_device_id")
        if not existing_device_id:
            return _htmx_error_response("Missing existing_device_id")

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            # Unknown/tampered server_key would otherwise raise → 500.
            self._librenms_api = build_librenms_api(post_server_key)
            if self._librenms_api is None:
                return _htmx_error_response("Selected LibreNMS server is no longer configured.")

        try:
            existing_device = Device.objects.get(pk=int(existing_device_id))
        except (Device.DoesNotExist, ValueError):
            return _htmx_error_response("Existing device not found")

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
                return _htmx_error_response("Invalid override_device_type_id")

        override_platform = None
        if override_platform_id:
            from dcim.models import Platform

            try:
                override_platform = Platform.objects.get(pk=int(override_platform_id))
            except (Platform.DoesNotExist, ValueError, TypeError):
                return _htmx_error_response("Invalid override_platform_id")

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return _htmx_error_response("LibreNMS device not found")

        promote = validation.get("promote_to_host") if validation else None
        if not promote:
            return _htmx_error_response("Promotion is not applicable for this device")
        validated_existing = validation.get("existing_device")
        if validated_existing is None:
            return _htmx_error_response("Missing validated conflict target for promotion")
        if validated_existing.pk != existing_device.pk:
            return _htmx_error_response("Device ID mismatch: existing_device_id does not match validation result")

        new_host_id = libre_device.get("device_id")
        if isinstance(new_host_id, bool):
            return _htmx_error_response("Invalid or missing LibreNMS device_id")
        try:
            new_host_id = int(new_host_id)
        except (TypeError, ValueError):
            return _htmx_error_response("Invalid or missing LibreNMS device_id")
        if new_host_id <= 0:
            return _htmx_error_response("Invalid LibreNMS device_id")

        existing_libre_id = promote.get("existing_libre_id")
        try:
            existing_libre_id = int(existing_libre_id)
        except (TypeError, ValueError):
            return _htmx_error_response("Invalid existing LibreNMS id in promotion data")
        if existing_libre_id == new_host_id:
            return _htmx_error_response("Existing link already points at this LibreNMS device")

        oob_type = promote.get("existing_oob_type") or ""
        if not oob_type:
            return _htmx_error_response("Cannot determine OOB type for promotion")

        from netbox_librenms_plugin.utils import set_librenms_device_id, set_librenms_oob

        server_key = self.librenms_api.server_key

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
            return _htmx_error_response(
                "Device has a legacy bare-integer librenms_id; use 'Convert mapping' to migrate first."
            )

        with transaction.atomic():
            try:
                existing_device = Device.objects.select_for_update().get(pk=existing_device.pk)
            except Device.DoesNotExist:
                return _htmx_error_response("Device no longer exists; it may have been deleted concurrently.")

            from netbox_librenms_plugin.utils import (
                coerce_librenms_id,
                find_by_librenms_id,
                get_librenms_device_id,
                get_librenms_oob,
            )

            current_host_id = get_librenms_device_id(existing_device, server_key=server_key, auto_save=False)
            current_oob = get_librenms_oob(existing_device, server_key=server_key)
            if coerce_librenms_id(current_host_id) != coerce_librenms_id(existing_libre_id):
                return _htmx_error_response("LibreNMS host link changed concurrently; refresh and retry.")
            if current_oob:
                return _htmx_error_response(
                    "OOB link already set; this device may have been promoted by a concurrent request."
                )
            # Another device may have claimed new_host_id since validation ran. Re-check
            # inside the transaction and abort on a non-self conflict so we don't create a
            # duplicate host mapping. find_by_librenms_id is an unlocked read, so this
            # narrows — not closes — the window (no unique constraint exists on the cf).
            host_conflict = find_by_librenms_id(Device, new_host_id, server_key)
            if host_conflict is not None and host_conflict.pk != existing_device.pk:
                return _htmx_error_response(
                    f"LibreNMS device #{new_host_id} is already linked to '{escape(host_conflict.name)}'; "
                    "refresh and retry."
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
                )
            except ValueError as exc:
                return _htmx_error_response(f"Invalid promotion data: {escape(str(exc))}")

            # Promotion re-points the LibreNMS host/OOB linkage only. NetBox
            # requires primary_ip4/6 and oob_ip to be assigned to one of the
            # device's interfaces, so those relationships are set from the
            # interface-assigned IP-sync flow — not from auto-created global
            # records here.
            update_fields = ["custom_field_data"]

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

            if err := _save_device(existing_device, update_fields=update_fields, request=request):
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
            return _htmx_error_response("Device not found after action")

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
        from netbox_librenms_plugin.librenms_api import build_librenms_api
        from netbox_librenms_plugin.utils import (
            mark_librenms_migrated,
            merge_librenms_links,
        )

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            # Unknown/tampered server_key would otherwise raise → 500.
            self._librenms_api = build_librenms_api(post_server_key)
            if self._librenms_api is None:
                return _htmx_error_response("Selected LibreNMS server is no longer configured.")

        winner_pk_raw = request.POST.get("winner_pk")
        donor_pk_raw = request.POST.get("donor_pk")
        if not winner_pk_raw or not donor_pk_raw:
            return _htmx_error_response("Missing winner_pk or donor_pk")
        try:
            winner_pk = int(winner_pk_raw)
            donor_pk = int(donor_pk_raw)
        except (TypeError, ValueError):
            return _htmx_error_response("Invalid winner_pk or donor_pk")
        if winner_pk == donor_pk:
            return _htmx_error_response("Winner and donor must be different devices")

        try:
            winner = Device.objects.get(pk=winner_pk)
            donor = Device.objects.get(pk=donor_pk)
        except Device.DoesNotExist:
            return _htmx_error_response("Winner or donor device not found")

        # Permission gate: user must be able to change BOTH devices.
        self.required_object_permissions = {"POST": [("change", Device)]}
        if error := self.require_object_permissions("POST"):
            return error

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return _htmx_error_response("LibreNMS device not found")

        merge_candidates = (validation or {}).get("merge_candidates") or {}
        candidate_pks = {
            (merge_candidates.get("host_named") or {}).get("pk"),
            (merge_candidates.get("oob_named") or {}).get("pk"),
        }
        candidate_pks.discard(None)
        if {winner_pk, donor_pk} != candidate_pks:
            return _htmx_error_response("winner_pk/donor_pk do not match the validation result's merge candidates")

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
                return _htmx_error_response(
                    f"{label.capitalize()} device has a legacy bare-integer librenms_id; "
                    "use 'Convert mapping' to migrate before merging."
                )

        server_key = self.librenms_api.server_key

        with transaction.atomic():
            # Lock both rows in deterministic pk order to avoid deadlocks.
            locked = list(Device.objects.select_for_update().filter(pk__in=[winner_pk, donor_pk]).order_by("pk"))
            if len(locked) != 2:
                return _htmx_error_response(
                    "One of the devices no longer exists; it may have been deleted concurrently."
                )
            locked_by_pk = {d.pk: d for d in locked}
            winner = locked_by_pk[winner_pk]
            donor = locked_by_pk[donor_pk]

            try:
                summary = merge_librenms_links(winner, donor, server_key=server_key)
            except ValueError as exc:
                return _htmx_error_response(f"Cannot merge: {escape(str(exc))}")

            # Transfer OOB IP relationship if winner has none and donor has one — but
            # only when the underlying IP already sits on a winner-owned interface.
            # NetBox requires Device.oob_ip be assigned to one of that device's own
            # interfaces, and this merge intentionally leaves interfaces on the donor.
            # Since the persist below uses save(update_fields=...) which skips
            # full_clean(), blindly moving oob_ip would silently leave the winner
            # pointing at a donor interface. Leave it on the donor until the IP/interface
            # is re-homed (via the migrate "move IP/interface" actions).
            oob_ip_transferred = False
            if donor.oob_ip_id and not winner.oob_ip_id:
                oob_assigned = donor.oob_ip.assigned_object
                if getattr(oob_assigned, "device_id", None) == winner.pk:
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
                transaction.set_rollback(True)
                return _htmx_error_response(f"Save failed: {escape(str(exc))}")

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
            return _htmx_error_response("Device not found after merge")

        response = self.render_device_row(request, libre_device, validation, selections)
        response["HX-Trigger"] = "closeModal"
        return response


class SaveUserPrefView(LibreNMSPermissionMixin, View):
    """Save a user preference via POST. Used by JS toggle handlers."""

    ALLOWED_PREFS = {
        "use_sysname": "plugins.netbox_librenms_plugin.use_sysname",
        "strip_domain": "plugins.netbox_librenms_plugin.strip_domain",
        "set_primary_ip": "plugins.netbox_librenms_plugin.set_primary_ip",
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


class AddPlatformMappingView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View
):
    """HTMX view to create a PlatformMapping from the import validation modal."""

    def post(self, request, device_id):
        """Create a PlatformMapping linking the LibreNMS OS string to a NetBox Platform."""
        if error := self.require_write_permission():
            return error

        from dcim.models import Platform
        from netbox_librenms_plugin.librenms_api import build_librenms_api
        from netbox_librenms_plugin.models import PlatformMapping

        post_server_key = (request.POST.get("server_key") or "").strip()
        if post_server_key:
            # Unknown/tampered server_key would otherwise raise → 500.
            self._librenms_api = build_librenms_api(post_server_key)
            if self._librenms_api is None:
                return _htmx_error_response("Selected LibreNMS server is no longer configured.")

        libre_device = fetch_device_with_cache(device_id, self.librenms_api)
        if not libre_device:
            return _htmx_error_response("Device not found in LibreNMS.")

        librenms_os = (libre_device.get("os") or "").strip()
        if not librenms_os or librenms_os == "-":
            return _htmx_error_response("Device has no OS string — cannot create mapping.")

        platform_id = request.POST.get("platform_id", "").strip()
        if not platform_id:
            return _htmx_error_response("Please select a platform before submitting.")

        try:
            platform_id = int(platform_id)
        except (ValueError, TypeError):
            return _htmx_error_response("Invalid platform selection.")

        try:
            platform = Platform.objects.get(pk=platform_id)
        except Platform.DoesNotExist:
            return _htmx_error_response("Selected platform not found.")

        if PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).count() > 1:
            return _htmx_error_response(
                "Multiple mappings exist for this OS string. Remove duplicates before updating."
            )
        existing_mapping = PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).first()
        self.required_object_permissions = {
            "POST": [("change", PlatformMapping) if existing_mapping else ("add", PlatformMapping)]
        }
        if error := self.require_object_permissions("POST"):
            return error

        try:
            with transaction.atomic():
                # Lock the row to close the TOCTOU window between the upfront
                # permission check and the actual write. select_for_update cannot
                # lock absent rows, so the create branch handles IntegrityError.
                # Materialise the locked rows in one query — count() would drop
                # the FOR UPDATE clause, leaving the rows unlocked.
                locked_rows = list(
                    PlatformMapping.objects.select_for_update().filter(librenms_os__iexact=librenms_os)[:2]
                )
                if len(locked_rows) > 1:
                    return _htmx_error_response(
                        "Multiple mappings exist for this OS string. Remove duplicates before updating."
                    )
                locked = locked_rows[0] if locked_rows else None
                if locked and not existing_mapping:
                    # Concurrent request created the mapping after our upfront read.
                    # Only escalate to change permission if we would actually mutate.
                    if locked.netbox_platform_id != platform_id:
                        self.required_object_permissions = {"POST": [("change", PlatformMapping)]}
                        if error := self.require_object_permissions("POST"):
                            return error
                if existing_mapping and not locked:
                    # Mapping was deleted between our upfront read and the lock.
                    # We are about to CREATE a new row, so require add permission.
                    self.required_object_permissions = {"POST": [("add", PlatformMapping)]}
                    if error := self.require_object_permissions("POST"):
                        return error
                if locked:
                    if locked.netbox_platform_id != platform_id:
                        locked.netbox_platform = platform
                        locked.full_clean()
                        locked.save()
                else:
                    try:
                        PlatformMapping.objects.create(
                            librenms_os=librenms_os.lower(),
                            netbox_platform=platform,
                        )
                    except IntegrityError:
                        return _htmx_error_response("Mapping was created concurrently. Please try again.")
        except Exception as exc:
            logger.exception("AddPlatformMappingView: failed to save mapping: %s", exc)
            return _htmx_error_response("Error saving mapping. Please try again.")

        cache_key = get_import_device_cache_key(device_id, self.librenms_api.server_key)
        cache.delete(cache_key)

        detail_view = DeviceValidationDetailsView()
        detail_view._librenms_api = self._librenms_api
        modal_html = detail_view.get(request, device_id).content.decode("utf-8")
        oob_modal = format_html(
            '<div id="htmx-modal-content" hx-swap-oob="innerHTML">{}</div>',
            mark_safe(modal_html),
        )

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if libre_device is not None and validation is not None:
            row_response = self.render_device_row(request, libre_device, validation, selections)
            row_html = row_response.content.decode("utf-8")
            row_html = format_html("<table><tbody>{}</tbody></table>", mark_safe(row_html))
        else:
            row_html = mark_safe("")

        return HttpResponse(oob_modal + row_html, content_type="text/html")
