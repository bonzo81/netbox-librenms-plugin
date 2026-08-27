"""HTMX endpoints and POST handlers for importing LibreNMS devices."""

import json
import logging
import re
from urllib.parse import parse_qs, urlparse

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, DataError, IntegrityError, transaction

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.text import slugify
from django.views import View

from netbox_librenms_plugin.constants import OOB_TYPES
from netbox_librenms_plugin.import_utils import (
    _determine_device_name,
    bulk_import_devices,
    bulk_import_vms,
    classify_bulk_precheck,
    detect_bulk_collisions,
    detect_collisions_for_device_ids,
    fetch_device_with_cache,
    get_import_device_cache_key,
    get_librenms_device_by_id,
    get_virtual_chassis_data,
    required_import_permissions,
    scope_bulk_collisions,
    update_vc_member_suggested_names,
    validate_device_for_import,
)
from netbox_librenms_plugin.import_validation_helpers import (
    apply_cluster_to_validation,
    apply_rack_to_validation,
    apply_role_to_validation,
    extract_device_selections,
    fetch_model_by_id,
    merge_candidate_pks,
)
from netbox_librenms_plugin.ip_addressing import parse_host_address
from netbox_librenms_plugin.tables.device_status import DeviceImportTable
from netbox_librenms_plugin.utils import (
    acquire_advisory_transaction_lock,
    coerce_librenms_id,
    coerce_model_pk,
    get_librenms_sync_device,
    is_legacy_librenms_id,
    normalize_serial,
    resolve_naming_preferences,
    resolve_server_mapping_display_id,
    same_host,
    save_interface_name_preference,
    save_user_pref,
    set_device_ip_fk,
    set_librenms_device_id,
)
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin

logger = logging.getLogger(__name__)


def _attach_messages_oob(response, request):
    """
    Append a single OOB-swap toast container to an HTMX response.

    NetBox's standard ``inc/messages.html`` renders a
    ``<div id="django-messages" hx-swap-oob="true">`` with one Bootstrap toast per
    pending Django message. Including this snippet inside per-row partials causes
    problems on multi-row OOB responses because each render emits a matching
    ``id="django-messages"`` div and the LAST swap (typically empty once messages
    have been consumed by an earlier render) wipes the toasts.

    Centralising the include here guarantees a single render per HTMX response so
    toasts always make it to NetBox's afterSettle ``initMessages()`` hook.

    Args:
        response: The HTMX response whose content the toast container is appended to.
        request: The current HTTP request (source of pending Django messages).

    Returns:
        The response, unchanged when it has no byte content or nothing is queued.
    """
    if response is None or not hasattr(response, "content"):
        return response
    if not isinstance(response.content, (bytes, bytearray)):
        return response
    # Skip the OOB swap when nothing is queued: an empty #django-messages container
    # would replace (and wipe) toasts already visible on the page from an earlier action.
    # Iterating the storage marks it consumed; restore used=False after peeking so the
    # toast render below (or the page's own renderer) still emits the messages.
    storage = messages.get_messages(request)
    pending = list(storage)
    # ``get_messages`` returns a bare ``list`` (no ``.used``) when no message-storage
    # middleware ran (e.g. RequestFactory requests); a real request always has a storage
    # backend. Guard so re-marking the storage unconsumed can't AttributeError.
    if hasattr(storage, "used"):
        storage.used = False
    if not pending:
        return response
    try:
        rendered = render_to_string("inc/messages.html", request=request)
    except Exception:  # pragma: no cover - defensive: don't break HTMX response on render error
        logger.debug("Failed to render inc/messages.html for OOB toast attach", exc_info=True)
        return response
    # Compose the two trusted HTML fragments (existing response bytes + the rendered
    # inc/messages.html) through format_html()/mark_safe() — the repo's CodeQL-safe HTML
    # envelope — rather than concatenating raw bytes.
    response.content = format_html(
        "{}{}",
        mark_safe(response.content.decode(response.charset)),
        mark_safe(rendered),
    ).encode(response.charset)
    return response


# Actions that require the force checkbox when a device-type mismatch is detected.
_FORCE_REQUIRED_ACTIONS = frozenset({"link", "update", "update_serial", "update_type"})

# Existing-interface names that look like an OOB/management port, used to pre-select the
# OOB-attach interface suggestion. Compiled once at import; see _suggest_oob_interface().
# Derived from OOB_TYPES (the single source of truth for OOB controller types — so a new type can't
# silently drift out of the UI suggestion, as "cimc" had) plus the interface-name-only mgmt tokens.
_OOB_INTERFACE_NAME_TOKENS = (*OOB_TYPES, "mgmt", "management")
# Anchor on word boundaries with an optional trailing index, mirroring constants.OOB_TYPE_PATTERN
# (\b(...)\d*\b). Without \b the tokens matched as bare substrings, so a name merely CONTAINING a
# token (e.g. "bmcswitch-uplink", "submgmt") was wrongly pre-selected as the OOB-IP interface.
_OOB_INTERFACE_NAME_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(token) for token in _OOB_INTERFACE_NAME_TOKENS) + r")\d*\b", re.IGNORECASE
)

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

    Args:
        request (HttpRequest): The HTTP request that contains the preference values.

    Returns:
        bool: Whether VC detection is enabled.
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
    standard ``messages`` framework. It does not depend on ``window.bootstrap``.

    Returns ``200`` (with ``HX-Reswap: none``) so the primary swap target is
    left untouched *and* so ``django-htmx``'s DEBUG-mode handler does not
    replace the page body with the response payload (it only does so for
    4xx/5xx responses).

    Args:
        message (str): The error message to show in the toast.

    Returns:
        HttpResponse: The HTMX error response.
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


def _rebind_or_htmx_error(view, request) -> HttpResponse | None:
    """
    Rebind ``view.librenms_api`` to the POSTed ``server_key``, failing closed.

    Base/import HTMX endpoints must re-scope the client to the tab the user acted on before any
    live lookup. Returns an :func:`_htmx_error_response` toast when the posted key is
    blank/unknown/misconfigured (so a missing or broken default can't 500 via the lazy
    ``librenms_api`` property), else ``None`` so the caller proceeds.

    Args:
        view: The view instance providing ``rebind_api_for_server``.
        request: The current request (source of the ``server_key`` POST param).

    Returns:
        HttpResponse | None: An HTMX error toast when the rebind fails closed, else ``None``.
    """
    post_server_key = (request.POST.get("server_key") or "").strip()
    if view.rebind_api_for_server(post_server_key) is None:
        return _htmx_error_response("Selected LibreNMS server is no longer configured.")
    return None


def _mapping_change_is_allowed(view, model, pk) -> bool:
    """Return whether the current user may change the resolved mapping row."""
    return view.restricted_queryset(model, "change").filter(pk=pk).exists()


def _lock_mapping_in_scope(view, model, lookup, duplicate_message):
    """
    Read the candidate mapping pks unlocked, then lock the first one inside the change scope.

    Shared by the device-type and platform mapping views so the permission guarantee cannot drift
    between two copies. Scope BEFORE locking: locking first lets a caller pin a row it cannot see
    and stall concurrent work on it. The duplicate check still has to see every row, so it reads
    unlocked and by pk only, materialised in one query (count() would drop the FOR UPDATE clause).

    Args:
        view: The calling view, used for ``restricted_queryset``.
        model: The mapping model to lock.
        lookup: Filter kwargs identifying the mapping's natural key.
        duplicate_message: Error text shown when more than one row matches.

    Returns:
        tuple: ``(locked, None)`` on success, where *locked* is None when no row exists, or
        ``(None, error_response)`` when the caller must stop.
    """
    present_pks = list(model.objects.filter(**lookup).values_list("pk", flat=True)[:2])
    if len(present_pks) > 1:
        return None, _htmx_error_response(duplicate_message)
    if not present_pks:
        return None, None
    locked = view.restricted_queryset(model, "change").select_for_update(of=("self",)).filter(pk=present_pks[0]).first()
    # A row appeared (or left this caller's scope) after the upfront check.
    if locked is None:
        return None, _htmx_error_response("Existing mapping is no longer available.")
    return locked, None


def _oob_ip_is_reassignable(candidate, interface) -> bool:
    """
    Return whether *candidate* may be re-homed to *interface* without taking another device's IP.

    Shared by the pre-lock refusal and the post-lock re-verify so the two readings cannot drift.

    Args:
        candidate: The existing IPAddress row matching the requested host address.
        interface: The interface the caller wants the address assigned to.

    Returns:
        bool: True when the row is free or already on this device, and no other device
            references it through primary_ip4/primary_ip6/oob_ip.
    """
    from dcim.models import Device
    from django.db.models import Q

    assigned = candidate.assigned_object
    if not (assigned is None or getattr(assigned, "device_id", None) == interface.device_id):
        return False
    # An unassigned row can still be ANOTHER device's primary_ip4/primary_ip6/oob_ip — a direct
    # Device FK, separate from assigned_object and UNIQUE per address. Claiming it would trip that
    # constraint and roll the whole attach back with an opaque IntegrityError.
    return not (
        Device.objects.filter(Q(primary_ip4=candidate) | Q(primary_ip6=candidate) | Q(oob_ip=candidate))
        .exclude(pk=interface.device_id)
        .exists()
    )


def _acquire_serial_assignment_lock(serial: str) -> None:
    """
    Serialize serial-assignment guards on the serial value (transaction-scoped advisory lock).

    Concurrent writers of the SAME serial contend on one lock, and the second writer's
    conflict check then sees the first one's committed row — closing the both-pass race a
    conflict-row lock never covered (it locks nothing while the serial is unassigned).
    It also replaces that second row lock: with own-row locks already held, two
    swap-direction requests locking each other's conflict row deadlock (A→B / B→A).
    Must be called inside ``transaction.atomic()``; the lock releases on commit/rollback.

    Args:
        serial: The trimmed serial being assigned.

    Raises:
        RuntimeError: When called in autocommit — the lock would release immediately.
    """
    acquire_advisory_transaction_lock(f"netbox-librenms-plugin:device-serial:{serial}")


def _apply_conflict_checked_serial(device, incoming_serial: str) -> HttpResponse | None:
    """
    Assign *incoming_serial* to *device* under the serial advisory lock, or report the conflict.

    Shared by the conflict view's update / update_serial / sync_serial actions so the three can't
    drift on the lock, the ownership guard, or the message. Must be called inside
    ``transaction.atomic()`` — the advisory lock is transaction-scoped, so the read-then-write is
    only serialized while the caller's transaction is open.

    NetBox deliberately has no DB-level uniqueness on ``Device.serial``: during moves and
    replacements two devices may temporarily share one. Conflicts therefore surface as an error
    for the user to resolve rather than a constraint violation.

    Args:
        device: The Device to mutate. ``serial`` is set in memory only; the caller persists it.
        incoming_serial: The already-trimmed serial from LibreNMS.

    Returns:
        HttpResponse | None: An HTMX error toast when another device owns the serial, else None.
    """
    from dcim.models import Device

    _acquire_serial_assignment_lock(incoming_serial)
    conflict_device = Device.objects.filter(serial=incoming_serial).exclude(pk=device.pk).first()
    if conflict_device:
        logger.warning(
            f"Serial assignment blocked: '{incoming_serial}' already assigned to "
            f"'{conflict_device.name}' (pk={conflict_device.pk})"
        )
        return _htmx_error_response(
            f"Serial conflict: '{incoming_serial}' is already assigned to device "
            f"'{conflict_device.name}' (ID: {conflict_device.pk})"
        )
    device.serial = incoming_serial
    return None


def _platform_device_type_mismatch(device) -> HttpResponse | None:
    """
    Mirror NetBox Device.clean()'s platform/device-type manufacturer rule for save paths.

    The save(update_fields=...) mode deliberately skips full_clean() (which would abort on
    unrelated legacy field values), but a device_type/platform write still carries the
    cross-field manufacturer constraint Device.clean() enforces, with no DB backstop. Validate
    only that one rule so an inconsistent platform/device_type pairing can't be persisted
    silently.

    Args:
        device (Device): The device to validate.

    Returns:
        HttpResponse | None: An HTMX error response on mismatch, otherwise None.
    """
    platform = getattr(device, "platform", None)
    device_type = getattr(device, "device_type", None)
    if (
        platform is not None
        and getattr(platform, "manufacturer_id", None)
        and device_type is not None
        and platform.manufacturer_id != device_type.manufacturer_id
    ):
        return _htmx_error_response(
            f"Can't save: the device's platform '{platform}' is limited to {platform.manufacturer} "
            f"device types, but '{device_type}' is a {device_type.manufacturer} device type — "
            "update the platform first."
        )
    return None


def _device_type_rack_fit_error(device) -> HttpResponse | None:
    """
    Mirror NetBox Device.clean()'s rack-placement rules for the ``save(update_fields=...)`` path.

    ``save(update_fields=["device_type", ...])`` skips ``full_clean()`` (it would abort on
    unrelated legacy fields), which also bypasses ``Device.clean()``'s rack checks: a 0U device
    type cannot hold a rack position, a child device type cannot be assigned to a rack
    face/position, and the new device_type's ``u_height`` must fit in the free units at the
    device's rack position/face. A LibreNMS-matched device_type violating any of these would
    otherwise persist an invalid rack elevation with a success toast and no DB backstop.
    Re-validate only those rules (like :func:`_platform_device_type_mismatch`). A device that isn't
    rack-mounted (no rack/position/face) is unaffected. Note the 0U/child rules can't be left to
    the space check: ``get_available_units(u_height=0)`` contains every unit, so it passes
    trivially for exactly the types these rules reject.

    Args:
        device (Device): The device to validate.

    Returns:
        HttpResponse | None: An HTMX error response on a violation, otherwise None.
    """
    rack = getattr(device, "rack", None)
    position = getattr(device, "position", None)
    face = getattr(device, "face", None)
    device_type = getattr(device, "device_type", None)
    if device_type is None:
        return None
    # Device.clean(): "A 0U device type cannot be assigned to a rack position."
    if position and device_type.u_height == 0:
        return _htmx_error_response(
            f"Can't set device type to '{device_type}' (0U): a 0U device type cannot be assigned "
            f"to a rack position — clear the device's position (U{position}) first."
        )
    # Device.clean(): child device types cannot be assigned to a rack face/position
    # (both are attributes of the parent device).
    if rack and getattr(device_type, "is_child_device", False) and (face or position):
        return _htmx_error_response(
            f"Can't set device type to '{device_type}': child device types cannot be assigned to "
            "a rack face or position — these are attributes of the parent device."
        )
    if not (rack and position):
        return None
    try:
        # Full-depth types occupy both faces, so fit is checked rack-wide (rack_face=None); exclude
        # the device's own current occupancy so a same-position resize is measured against the space
        # it would free. Mirrors Device.clean()'s rack-space validation.
        rack_face = device.face if not device_type.is_full_depth else None
        available_units = rack.get_available_units(
            u_height=device_type.u_height,
            rack_face=rack_face,
            exclude=[device.pk] if device.pk else [],
        )
    except Exception:
        # Best-effort backstop around a deliberately-bypassed full_clean(); if NetBox's helper
        # signature/behaviour differs across versions, don't block a legitimate device_type write —
        # fall back to the pre-fix behaviour (no rack-fit check) rather than 500.
        logger.exception("Rack-fit precheck failed for device pk=%s", getattr(device, "pk", None))
        return None
    if position not in available_units:
        return _htmx_error_response(
            f"Can't set device type to '{device_type}' ({device_type.u_height}U): U{position} in "
            f"rack '{rack}' is already occupied or lacks sufficient space to accommodate it."
        )
    return None


def _save_device(device, update_fields: list[str] | None = None, request=None) -> HttpResponse | None:
    """
    Persist a Device row, returning an HttpResponse on failure or None on success.

    When ``update_fields`` is provided, the call uses ``save(update_fields=...)``
    which issues a narrower UPDATE that only writes those columns and bypasses
    ``full_clean()``. This is the correct mode when the caller mutates only a known
    small set of fields and the device row may carry pre-existing inconsistencies on
    *other* fields (e.g. a legacy ``face`` value left behind after a rack was
    cleared); validating those untouched fields would block legitimate updates.

    When ``update_fields`` is ``None`` (the default), the legacy behaviour is
    preserved: ``full_clean()`` runs against the entire row before ``save()`` writes
    every column.

    Args:
        device: The NetBox Device to persist.
        update_fields (list[str] | None): The columns to write; None runs
            ``full_clean()`` and saves the whole row.
        request: The current HTTP request; when it is an HTMX request, errors are
            returned via ``_htmx_error_response()`` so modal swap/toast flows remain
            intact, otherwise plain ``HttpResponse`` status codes are used.

    Returns:
        HttpResponse | None: An error response on failure, or None on success.
    """

    def _err(msg: str, status: int) -> HttpResponse:
        if request is not None and request.META.get("HTTP_HX_REQUEST"):
            return _htmx_error_response(msg)
        return HttpResponse(escape(msg), status=status)

    # ValidationError messages are field-level and safe/useful to surface; raw DB exception
    # strings (IntegrityError/DataError/DatabaseError) can leak constraint names, column
    # details, or backend text, so log them server-side and return a generic toast.
    if update_fields is None:
        try:
            device.full_clean()
        except ValidationError as exc:
            error_msg = exc.message_dict if hasattr(exc, "message_dict") else str(exc)
            return _err(f"Validation error: {error_msg}", 400)
        try:
            device.save()
        except IntegrityError:
            logger.exception("Integrity error saving device pk=%s", getattr(device, "pk", None))
            return _err("Could not save: a database integrity constraint was violated.", 409)
        return None

    # full_clean() is intentionally skipped here (it would abort on unrelated legacy field
    # values), but a device_type/platform write still carries the platform/manufacturer
    # cross-field constraint with no DB backstop — validate just that one rule so an
    # inconsistent pairing can't be persisted silently with a success toast.
    if update_fields and ({"device_type", "platform"} & set(update_fields)):
        if mismatch := _platform_device_type_mismatch(device):
            return mismatch
    # A device_type write also bypasses Device.clean()'s rack-fit check; re-validate just that rule
    # so a taller device_type can't overflow the rack elevation with a success toast.
    if update_fields and "device_type" in update_fields:
        if rack_fit := _device_type_rack_fit_error(device):
            return rack_fit

    try:
        device.save(update_fields=update_fields)
    except IntegrityError:
        logger.exception("Integrity error saving device pk=%s", getattr(device, "pk", None))
        return _err("Could not save: a database integrity constraint was violated.", 409)
    except ValidationError as exc:
        error_msg = exc.message_dict if hasattr(exc, "message_dict") else str(exc)
        return _err(f"Validation error: {error_msg}", 400)
    except DataError:
        # save(update_fields=...) skips full_clean(), so an overlong/invalid value
        # from LibreNMS (e.g. a hostname past Device.name max_length) reaches the DB
        # and raises DataError. Convert it to a clean toast instead of a 500.
        logger.exception("Data error saving device pk=%s", getattr(device, "pk", None))
        return _err("Could not save: a field value is invalid (for example, too long).", 400)
    except DatabaseError:
        # Catch-all for any other backend-level failure during the UPDATE (lock timeout,
        # connection drop, a backend that signals a 0-row forced UPDATE, etc.). Note: a
        # plain save(update_fields=...) against a concurrently-deleted row does NOT reliably
        # raise on Django 6.0 — it issues an UPDATE that affects 0 rows silently — so this is
        # a defensive backstop, not a guaranteed concurrent-delete signal. Several callers
        # don't re-lock the row first, so surface whatever does surface as a toast rather than
        # a 500. (Must follow the IntegrityError / DataError handlers above — both subclass
        # DatabaseError.)
        logger.exception("Database error saving device pk=%s", getattr(device, "pk", None))
        return _err("Could not save: the record may have been changed or deleted; refresh and retry.", 409)
    return None


def _get_hostname_for_action(request, validation: dict, libre_device: dict) -> str:
    """
    Return the resolved hostname to use when updating a device during a conflict action.

    Prefer the cached ``resolved_name`` from validation (already computed with the
    user's naming prefs at validation time). Fall back to computing it fresh from
    the current request's naming preferences.

    Args:
        request (HttpRequest): The current HTTP request.
        validation (dict): The validation data that can contain the cached resolved name.
        libre_device (dict): The LibreNMS device data used to compute a fresh name.

    Returns:
        str: The resolved hostname.
    """
    resolved = validation.get("resolved_name")
    if resolved:
        return resolved
    use_sysname, strip_domain = resolve_naming_preferences(request)
    return _determine_device_name(libre_device, use_sysname=use_sysname, strip_domain=strip_domain)


class DeviceImportHelperMixin:
    """Mixin providing common validation and rendering helpers for device import views."""

    def get_validated_device_with_selections(
        self, device_id: int, request, *, libre_device: dict | None = None
    ) -> tuple[dict | None, dict | None, dict]:
        """
        Get LibreNMS device, validate it, and apply user selections.

        Consolidates the common pattern across all device import update views.

        Args:
            device_id: LibreNMS device ID
            request: Django request object
            libre_device: Optional pre-fetched LibreNMS device data. The LibreNMS side is
                invariant within a single request, so the post-commit re-validation after a
                promote/merge can pass the device fetched earlier to skip a redundant
                ``fetch_device_with_cache`` call. The NetBox-side ``validate_device_for_import``
                is still re-run so the refreshed row reflects the just-committed state.

        Returns:
            Tuple of (libre_device, validation, selections)
            Returns (None, None, selections) if device not found
        """
        # Reuse a caller-supplied device, else use cached device data from the table load
        # (both eliminate redundant API calls).
        if libre_device is None:
            libre_device = fetch_device_with_cache(device_id, self.librenms_api)

        if not libre_device:
            return None, None, extract_device_selections(request, device_id)

        validation, selections = self.validate_and_apply_selections(device_id, request, libre_device)
        return libre_device, validation, selections

    def validate_and_apply_selections(self, device_id: int, request, libre_device: dict) -> tuple[dict, dict]:
        """
        Validate an already-fetched LibreNMS device and apply user selections.

        Split out of :meth:`get_validated_device_with_selections` so callers that already hold
        the LibreNMS device dict (e.g. ``AddDeviceTypeMappingView.post`` right after a mapping
        write) can re-validate without a second LibreNMS round-trip (issue #66). Re-validation
        still reflects NetBox-side changes — a freshly created ``DeviceTypeMapping``, role, etc.
        — because :func:`validate_device_for_import` reads those from the database, not from the
        cached ``libre_device``.

        Args:
            device_id (int): LibreNMS device ID.
            request: Django request object.
            libre_device (dict): Already-fetched LibreNMS device dict.

        Returns:
            tuple[dict, dict]: The (validation, selections) pair.
        """
        selections = extract_device_selections(request, device_id)
        cluster_id = selections["cluster_id"]
        requested_vm = bool(cluster_id)

        # VC detection runs for every non-VM import: role/rack changes and detail views need
        # VC context (served from cache when already fetched), so it's gated purely on not-a-VM.
        enable_vc = not requested_vm

        # Extract naming preferences: POST data (hx-include) → user pref → plugin settings.
        use_sysname, strip_domain = resolve_naming_preferences(request)

        validation = validate_device_for_import(
            libre_device,
            import_as_vm=requested_vm,
            api=self.librenms_api if enable_vc else None,
            include_vc_detection=enable_vc,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
            server_key=self.librenms_api.server_key,
        )
        # Recompute is_vm from validate_device_for_import's own detection
        # (it may have bound an existing Device by librenms_id/hostname/IP and flipped
        # import_as_vm back to False).
        is_vm = bool(validation.get("import_as_vm"))
        if requested_vm and not is_vm:
            # The row flipped VM→Device, but the first pass skipped VC detection / chassis-fallback
            # device-type matching because VM mode was requested (api=None, include_vc_detection=
            # False). Re-validate in device mode so VC metadata and chassis device-type matching are
            # applied for the device it actually resolved to.
            validation = validate_device_for_import(
                libre_device,
                import_as_vm=False,
                api=self.librenms_api,
                include_vc_detection=True,
                use_sysname=use_sysname,
                strip_domain=strip_domain,
                server_key=self.librenms_api.server_key,
            )

        # Apply user selections (cluster, role, rack) to validation
        _apply_user_selections_to_validation(validation, selections, is_vm)

        return validation, selections

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
        table = DeviceImportTable([libre_device], server_key=self.librenms_api.server_key)

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

    def post_commit_refresh_fallback(self, request, hx_trigger, deferred_messages=()):
        """
        Safe HTMX response when a *committed* import mutation can't reload its row.

        The OOB-attach / promote / merge handlers commit their DB mutation, clear the
        cached import row, then re-read LibreNMS to re-render the row. If that
        follow-up read fails (LibreNMS briefly unreachable, the cache was just
        cleared) the mutation has still succeeded — returning an HTMX *error* would
        tell the user the action failed and invite a retry against already-mutated
        state. Instead surface the outcome (any deferred messages plus a refresh hint)
        and return 200 with the same client trigger the success path uses, so the UI
        converges (modal refresh / close + visible toast) rather than reporting a
        false failure.

        Args:
            request: The current HTTP request.
            hx_trigger: The ``HX-Trigger`` value to send (matching the success path).
            deferred_messages: Iterable of ``(level, text)`` messages to surface.

        Returns:
            HttpResponse: A 200 response with the OOB toast container attached and
                ``HX-Reswap: none`` so the empty body doesn't blank the modal/row.
        """
        for level, text in deferred_messages:
            messages.add_message(request, level, text)
        messages.warning(
            request,
            "The action was applied, but the updated row could not be reloaded — LibreNMS may "
            "be temporarily unavailable. Refresh the page to see the latest state.",
        )
        response = HttpResponse(status=200)
        if hx_trigger:
            response["HX-Trigger"] = hx_trigger
        # The body is empty (the outcome is carried by the OOB toast + HX-Trigger). Without
        # HX-Reswap:none HTMX would still swap that empty payload into the original target,
        # blanking the modal/row even though the mutation already committed. OOB swaps apply
        # regardless, so the toast and trigger still fire. Mirrors _htmx_error_response().
        response["HX-Reswap"] = "none"
        return _attach_messages_oob(response, request)


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

        # Rebind to the POSTed server, failing closed (blank/unknown/misconfigured) so a missing
        # or broken default can't 500 via the lazy librenms_api property.
        if err := _rebind_or_htmx_error(self, request):
            return err

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
            # Surface partial cache-expiry: when SOME rows survive (devices non-empty) the
            # all-expired / partial-expired warnings above are skipped, so without this the modal
            # hides that N selected rows were dropped to stale cache and shows only generic errors.
            "cache_expired_count": cache_expired_count,
            "selected_count": len(seen_ids),
            "use_sysname": use_sysname,
            "strip_domain": strip_domain,
            "server_key": self.librenms_api.server_key,
            "vc_detection_enabled": vc_detection_enabled,
        }

        collisions = scope_bulk_collisions(detect_bulk_collisions(devices), request.user)
        if collisions:
            # Render at 200 (not 4xx): this is an interstitial modal swapped
            # into #htmx-modal-content, exactly like the confirm step. A non-2xx
            # status makes HTMX skip the swap and route the body through
            # htmx:responseError -> showErrorToast(), which would dump the
            # collision template as raw text in a toast.
            return render(
                request,
                "netbox_librenms_plugin/htmx/bulk_import_collision.html",
                {"collisions": collisions},
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
        # Rebind to the POSTed server, failing closed on a blank/unknown/misconfigured key so a
        # missing or broken default can't raise a 500 via the lazy self.librenms_api property.
        if self.rebind_api_for_server(post_server_key) is None:
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
            # De-duplicate on the *parsed* int, not the raw string: a POST carrying both "1" and
            # "01" coerces to the same device id, which would otherwise import it twice. dict
            # keys preserve first-seen order. Mirrors BulkImportConfirmView's seen_ids guard.
            parsed_ids = list(dict.fromkeys(int(device_id) for device_id in device_ids))
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

        # Authorize the model add/change perms BEFORE the background dispatch and the collision
        # pre-check below (mirrors ImportDevicesJob, which authorizes before its scan).
        # require_write_permission() above only checks the plugin-settings perm, while the job would
        # raise PermissionDenied only once it ran — leaving the caller with a doomed job and an
        # "Import job started" message. The pre-check likewise surfaces NetBox collision details
        # (object names + pks) in its modal. Enforce the same perm sets the import paths do, for
        # every import, before either path starts.
        required_import_perms = required_import_permissions(device_ids_to_import, vm_imports)
        missing_import_perms = [p for p in required_import_perms if not request.user.has_perm(p)]
        if missing_import_perms:
            deny_msg = f"You do not have permission to import these rows (missing: {', '.join(missing_import_perms)})."
            messages.error(request, deny_msg)
            if is_htmx:
                # 200 + HX-Redirect, like the other denial paths: HTMX skips the swap on non-2xx.
                return HttpResponse(
                    "", headers={"HX-Redirect": reverse("plugins:netbox_librenms_plugin:librenms_import")}
                )
            return redirect("plugins:netbox_librenms_plugin:librenms_import")

        # Seed the shared device cache from ALREADY-cached entries only, before the
        # background-vs-sync decision. Reading the Django cache directly (not
        # fetch_device_with_cache, which falls through to the LibreNMS HTTP API on a miss)
        # keeps this request handler from making one synchronous LibreNMS round-trip per
        # selected device on a cold/expired cache — which would defeat the background path's
        # whole point of returning fast. Misses are left out and fetched by whichever path
        # runs: the background job (async, off the request) or the synchronous import (inline).
        # One batched round-trip instead of one cache.get() per selected device: on Redis a bulk
        # import of N devices otherwise issues N sequential GETs before the fast background path
        # even starts. Map each cache key back to its device_id, then keep only the hits.
        server_key = self.librenms_api.server_key
        key_to_device_id = {get_import_device_cache_key(device_id, server_key): device_id for device_id in parsed_ids}
        libre_devices_cache = {
            key_to_device_id[key]: cached_device
            for key, cached_device in cache.get_many(list(key_to_device_id)).items()
            if cached_device
        }

        # Check if we should use background job for import
        total_import_count = len(parsed_ids)

        # Set when a requested background import falls back to a synchronous run
        # (no RQ workers); surfaced in the HTMX summary toasts below.
        sync_fallback_msg = None

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

                # Show notification and redirect - matching NetBox's native pattern.
                # Build URLs via reverse() so deployments under a script prefix / custom
                # base path get working links.
                job_url = reverse("core:job", kwargs={"pk": job.pk})
                messages.info(
                    request,
                    format_html(
                        "Import job started for {} device{}. "
                        'You can monitor progress in the <a href="{}">Jobs interface</a>.',
                        total_import_count,
                        "s" if total_import_count != 1 else "",
                        job_url,
                    ),
                )

                if request.headers.get("HX-Request"):
                    # For HTMX requests, redirect to clean import page (no filters)
                    # This matches the "Clear" button behavior
                    return HttpResponse(
                        "",
                        headers={"HX-Redirect": reverse("plugins:netbox_librenms_plugin:librenms_import")},
                    )
                else:
                    return redirect("plugins:netbox_librenms_plugin:librenms_import")
            else:
                # No workers available - warn user and proceed synchronously
                logger.warning("No RQ workers available for import job, falling back to synchronous import")
                # Remember the fallback for the HTMX summary toasts below: the normal import
                # page is HTMX, and without an htmx_toasts entry the user's background-import
                # request silently blocks for the whole synchronous run with no explanation
                # (only Django messages were queued, which the HTMX path never renders).
                # Outcome-neutral wording: the per-row summary toasts below report the actual
                # successes / failures / skips, so this banner must not claim every selected row
                # was imported when the synchronous run may have failed or skipped some.
                sync_fallback_msg = (
                    "Background job requested but no workers are available. "
                    f"The request ran synchronously for {total_import_count} selected row(s)."
                )
                if not is_htmx:
                    messages.warning(
                        request,
                        f"Background job requested but no workers available. Importing {total_import_count} devices synchronously...",
                    )

        # Re-run the same-NetBox-device collision check the confirm modal performs. The confirm
        # preview is advisory only — a re-submitted stale confirm form or a scripted POST reaches
        # this view directly — so block a colliding batch here too. This runs on the SYNCHRONOUS
        # path only: it sits after the background-job dispatch above, so a batch that enqueued a job
        # doesn't pay this validation cost synchronously (ImportDevicesJob re-runs the same check).
        # A single selected device can never collide (collisions need two distinct LibreNMS ids on
        # one NetBox object), so skip the extra validation pass for the common single-row case.
        precheck_skip_msg = None
        if len(parsed_ids) >= 2:
            collisions, unresolved = detect_collisions_for_device_ids(
                parsed_ids,
                self.librenms_api,
                libre_devices_cache=libre_devices_cache,
                sync_options=sync_options,
                # Each row validates in its actual import mode: a VM row checked in Device mode
                # would run the serial/IP matching bulk_import_vms skips and could fabricate a
                # collision that blocks a valid batch.
                vm_device_ids=vm_imports,
                user=request.user,
            )
            outcome = classify_bulk_precheck(collisions, unresolved, device_ids_to_import, vm_imports)
            if outcome.blocked:
                # Genuine collision (two rows → one NetBox object): block the whole batch, exactly
                # as the confirm modal does. Same shared wording ImportDevicesJob logs.
                if is_htmx:
                    # 200, like the confirm step: HTMX skips the swap on non-2xx.
                    return render(
                        request,
                        "netbox_librenms_plugin/htmx/bulk_import_collision.html",
                        {"collisions": outcome.collisions, "oob": True},
                    )
                messages.error(request, outcome.block_message)
                return redirect("plugins:netbox_librenms_plugin:librenms_import")
            if outcome.skipped_ids:
                # Skip ONLY the rows that couldn't be fetched/validated to collision-check them
                # (import the rest) rather than blocking the whole batch — a transient fetch miss on
                # one row no longer drops the entire import. Those rows are not imported (importing
                # an un-collision-checked row could bypass this guard); the skip is surfaced below.
                device_ids_to_import = outcome.importable_device_ids
                vm_imports = outcome.importable_vm_imports
                vm_ids_to_import = list(vm_imports.keys())
                precheck_skip_msg = outcome.skip_message

        # Synchronous import execution (reuses libre_devices_cache built above).
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
                    libre_devices_cache=libre_devices_cache,
                    user=request.user,  # Pass user for permission checks
                )

            # Import VMs if any
            if vm_ids_to_import:
                vm_result = bulk_import_vms(
                    vm_imports,
                    self.librenms_api,
                    sync_options,
                    libre_devices_cache,
                    user=request.user,  # Pass user for permission checks
                )

        except PermissionDenied as exc:
            # Handle permission errors with a user-friendly message
            logger.warning(f"Permission denied during import: {exc}")
            messages.error(request, str(exc))
            if request.headers.get("HX-Request"):
                return HttpResponse(
                    "",
                    headers={"HX-Redirect": reverse("plugins:netbox_librenms_plugin:librenms_import")},
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

        # Surface the background→synchronous fallback to HTMX users too: the non-HTMX
        # path queued a Django message before the import ran, but the HTMX path renders
        # only htmx_toasts — without this entry the blocking synchronous run happens
        # with no explanation and the user may re-submit or assume the job system worked.
        if sync_fallback_msg is not None:
            htmx_toasts.append(("text-bg-warning", "mdi-alert", "Warning", sync_fallback_msg))

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
        # Rows the collision pre-check couldn't fetch/validate were skipped (not imported) rather
        # than blocking the whole batch; surface that so the user knows which rows to retry.
        if precheck_skip_msg:
            if not is_htmx:
                messages.warning(request, precheck_skip_msg)
            htmx_toasts.append(("text-bg-warning", "mdi-alert", "Warning", precheck_skip_msg))

        if request.headers.get("HX-Request"):
            # Return updated rows for all imported devices using HTMX OOB swaps
            # This updates only the affected rows instead of refreshing the entire table
            updated_rows_html = []

            # Collect all successfully imported device IDs (devices + VMs)
            imported_device_ids = [item["device_id"] for item in device_result.get("success", [])] + [
                item["device_id"] for item in vm_result.get("success", [])
            ]
            # VM-success ids as a set so the per-row is_vm check below is O(1). Rebuilding the list
            # inside the loop made re-render O(n*m) for an import of n devices and m VM successes.
            imported_vm_ids = {item["device_id"] for item in vm_result.get("success", [])}

            # Re-validate and render each imported device with fresh status
            for device_id in imported_device_ids:
                # Fetch device from cache or API
                libre_device = fetch_device_with_cache(
                    device_id,
                    self.librenms_api,
                    libre_devices_cache=libre_devices_cache,
                )

                if libre_device:
                    # Determine if this was imported as VM or device
                    is_vm = device_id in imported_vm_ids

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
                    table = DeviceImportTable([libre_device], server_key=self.librenms_api.server_key)
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
            # Compose via format_html()/mark_safe() to match the repo's CodeQL-safe envelope
            # pattern (the rows are trusted Django-template output joined into the body).
            return HttpResponse(
                format_html("{}", mark_safe("\n".join(updated_rows_html))),
                headers={"HX-Trigger": '{"closeModal": null}'},
            )

        return redirect("plugins:netbox_librenms_plugin:librenms_import")


class DeviceVCDetailsView(LibreNMSPermissionMixin, LibreNMSAPIMixin, View):
    """HTMX view to show virtual chassis details."""

    def get(self, request, device_id):
        """Render virtual chassis details for a LibreNMS device."""
        libre_device = get_librenms_device_by_id(self.librenms_api, device_id)
        if not libre_device:
            # 200, not 404: this is an HTMX fragment swapped into the modal, and HTMX skips the
            # swap on a 4xx (routing the body through error handling instead), so the inline
            # alert would never render in place.
            return HttpResponse(
                '<div class="alert alert-danger">Device not found in LibreNMS</div>',
                status=200,
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


def _suggest_oob_interface(device, oob_candidate, interfaces=None):
    """
    Suggest an interface (and default new-interface name) for an OOB IP.

    NetBox requires ``oob_ip`` be assigned to one of the device's interfaces, so the
    OOB-attach form lets the user pick (or create) one. This pre-selects the existing
    interface whose name looks like an OOB/management port
    (idrac/ilo/ipmi/bmc/drac/oob/mgmt), and derives a sensible default name for a new
    interface from the OOB type (e.g. ``idrac0``). The OOB IP is frequently *not*
    physically on the matched interface — operators attach it to an ``idrac0``-style
    port deliberately — so this is only a suggestion the user can override.

    Args:
        device: The NetBox device the OOB IP will be attached to.
        oob_candidate (dict): The OOB-controller candidate, read for its ``type``.
        interfaces: Optional pre-materialized iterable of the device's interfaces. When the
            caller has already evaluated ``device.interfaces.all()`` (e.g. for the
            ``oob_interfaces`` form field), pass it here to avoid a duplicate query.

    Returns:
        tuple: ``(suggested_interface_id, default_new_name)``; the id is None when no
            interface name obviously matches.
    """
    oob_type = (oob_candidate.get("type") or "oob").strip().lower() or "oob"
    default_new_name = f"{oob_type}0"
    if interfaces is None:
        interfaces = device.interfaces.all()
    for iface in interfaces:
        if _OOB_INTERFACE_NAME_PATTERN.search(iface.name or ""):
            return iface.pk, default_new_name
    return None, default_new_name


class DeviceValidationDetailsView(LibreNMSPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to show detailed validation information."""

    def get(self, request, device_id):
        """Render detailed validation information for a LibreNMS device."""
        # Rebind to the import page's server (?server_key) before fetching. Reached via its own
        # URL (the modal-open HTMX GET), this view has no parent handler to inject the
        # import-scoped client, so without this it would fetch/cache against the global
        # LibreNMSSettings.selected_server — which may differ from the server the import ran on,
        # rendering "Device not found" for a device that only exists on the import's server. A
        # blank/absent ?server_key keeps the already-bound (parent-injected) or session client.
        _scoped_server, unresolved = self.resolve_get_render_server_key(request)
        if unresolved:
            # ?server_key named a server that no longer resolves (deleted/misconfigured); the rebind
            # declined and left the default/session client bound. Fail closed rather than fetch and
            # render validation data from the wrong server. 200, not 4xx — HTMX swaps the fragment in
            # place (a 4xx makes it skip the swap), matching the "Device not found" branch below.
            return HttpResponse(
                '<div class="alert alert-danger">Selected LibreNMS server is no longer configured.</div>',
                status=200,
            )
        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)

        if not libre_device:
            # 200, not 404 — HTMX fragment swapped into the validation-details modal; a 4xx makes
            # HTMX skip the swap, so the inline alert would never appear in place.
            return HttpResponse(
                '<div class="alert alert-danger">Device not found in LibreNMS</div>',
                status=200,
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
                oob_interfaces = list(existing.interfaces.all())
                context["oob_interfaces"] = oob_interfaces
                (
                    context["oob_suggested_interface_id"],
                    context["oob_default_new_name"],
                ) = _suggest_oob_interface(existing, validation["oob_candidate"], interfaces=oob_interfaces)

        return render(
            request,
            "netbox_librenms_plugin/htmx/device_validation_details.html",
            context,
        )

    @staticmethod
    def _build_sync_info(libre_device, existing_device):
        """Build sync comparison data between LibreNMS device and existing NetBox device."""
        # Trimmed so a padded LibreNMS serial doesn't report drift against the stored trimmed value.
        librenms_serial = normalize_serial(libre_device.get("serial")) or "-"
        librenms_os = libre_device.get("os") or "-"
        librenms_hardware = libre_device.get("hardware") or "-"

        # Serial comparison (VMs may not have serial in all NetBox versions). The stored side
        # is normalized too, so a legacy padded serial doesn't render as drift.
        netbox_serial = normalize_serial(getattr(existing_device, "serial", None))
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

        Returns a list of dicts with server_key, display_name, and device_id, with one entry
        per server the device is linked to. Returns None when the format is legacy (bare int)
        or when the field is absent/invalid.

        Args:
            existing_device (Device | VirtualMachine): The existing NetBox object.

        Returns:
            list[dict] | None: The per-server ID mappings, or None for legacy, absent, or invalid data.
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
            # Resolve the host id, falling back to the OOB controller's id for an OOB-only entry
            # ({"oob": {...}} with no usable host "id"). An OOB-only link is still a real link to
            # this server, so surface it like the device-sync modal (_build_all_server_mappings)
            # does — via the SAME shared helper — instead of dropping it and showing "no link"
            # (which can prompt a duplicate re-import). The helper centralizes the bool/int/str/
            # positive coercion, so 0 / negative / malformed ids can't slip through.
            did, _is_oob_only = resolve_server_mapping_display_id(did)
            if did is None:
                continue
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
        # Pin the client to the import page's server (the selects post it via hx-vals)
        # before get_validated_device_with_selections routes through the lazy client —
        # otherwise a global server switch mid-session re-validates and caches the WRONG
        # server's device under this row. Mirrors the sibling import endpoints.
        if err := _rebind_or_htmx_error(self, request):
            return err

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)

        if not libre_device:
            return _htmx_error_response("Device not found")

        return self.render_device_row(request, libre_device, validation, selections)


class DeviceClusterUpdateView(LibreNMSPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to update a table row when a cluster is selected/deselected."""

    def post(self, request, device_id):
        """Update the table row after a cluster selection change."""
        # Pin to the import page's server before any lookup (see DeviceRoleUpdateView).
        if err := _rebind_or_htmx_error(self, request):
            return err

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)

        if not libre_device:
            return _htmx_error_response("Device not found")

        return self.render_device_row(request, libre_device, validation, selections)


class DeviceRackUpdateView(LibreNMSPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to update a table row when a rack is selected."""

    def post(self, request, device_id):
        """Update the table row after a rack selection change."""
        # Pin to the import page's server before any lookup (see DeviceRoleUpdateView).
        if err := _rebind_or_htmx_error(self, request):
            return err

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

        action = request.POST.get("action")
        existing_device_id = request.POST.get("existing_device_id")
        existing_device_type = request.POST.get("existing_device_type", "device")

        # If the form submitted a specific server_key, honour it so the handler uses
        # the same server context as the import page when the user clicked the button.
        # Rebind to the POSTed server, failing closed (blank/unknown/misconfigured) so a missing
        # or broken default can't 500 via the lazy librenms_api property.
        if err := _rebind_or_htmx_error(self, request):
            return err

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

        # Object-level change permission for the specific model being mutated. Runs BEFORE the
        # lookup so an unauthorized caller can't probe pks, and so a caller holding no change
        # perm at all still gets the named-permission error rather than a bare "not found".
        self.required_object_permissions = {"POST": [("change", existing_model)]}
        if error := self.require_object_permissions("POST"):
            return error

        try:
            # Scope by "change": the gate above only asks the model-level perm, so a constrained
            # grant would otherwise mutate any object by raw pk.
            existing_device = self.restricted_queryset(existing_model, "change").get(pk=int(existing_device_id))
        except (existing_model.DoesNotExist, ValueError):
            return _htmx_error_response("Existing device not found")

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

        # coerce_librenms_id centralizes the bool/int/str/positive checks (rejects bools,
        # non-numeric strings, zero/negatives) in one place.
        librenms_id = coerce_librenms_id(libre_device.get("device_id"))
        if librenms_id is None:
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
            from netbox_librenms_plugin.utils import AmbiguousLibreNMSIdError, find_by_librenms_id

            with transaction.atomic():
                server_key = self.librenms_api.server_key
                # Lock the target device row so concurrent requests for the same
                # device are serialized.  The conflict check below is still a
                # best-effort guard for different devices; a DB unique constraint
                # would be needed for full protection.
                try:
                    existing_device = (
                        self.restricted_queryset(Device, "change")
                        .select_for_update(of=("self",))
                        .get(pk=existing_device.pk)
                    )
                except Device.DoesNotExist:
                    return _htmx_error_response("Device no longer exists; it may have been deleted concurrently.")
                try:
                    id_conflict = find_by_librenms_id(Device, int(librenms_id), server_key)
                except AmbiguousLibreNMSIdError:
                    return _htmx_error_response(
                        f"LibreNMS ID {librenms_id} is ambiguous — it matches more than one device. "
                        "Resolve the duplicate assignment before linking."
                    )
                if id_conflict and id_conflict.pk != existing_device.pk:
                    return _htmx_error_response(
                        f"LibreNMS ID conflict: ID {librenms_id} is already assigned to device "
                        f"'{id_conflict.name}' (ID: {id_conflict.pk})"
                    )

                # Reject legacy bare-int/string librenms_id: set_librenms_device_id
                # silently skips writes for legacy formats, leaving the device partially
                # updated. User must run "Convert mapping" migration first. Shared predicate
                # with AddAsOOBView and set_librenms_device_id so the three can't drift.
                if is_legacy_librenms_id(existing_device.custom_field_data.get("librenms_id")):
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
                    # Trimmed like validate/import_single_device, so the stored value and the
                    # conflict lookup can't disagree with the match paths on whitespace.
                    incoming_serial = normalize_serial(libre_device.get("serial"))
                    fields = ["custom_field_data", "name"]
                    if incoming_serial and incoming_serial != "-":
                        if err := _apply_conflict_checked_serial(existing_device, incoming_serial):
                            return err
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
                    # Trimmed like validate/import_single_device (see the update branch above).
                    incoming_serial = normalize_serial(libre_device.get("serial"))
                    fields = ["custom_field_data"]
                    if incoming_serial and incoming_serial != "-":
                        if err := _apply_conflict_checked_serial(existing_device, incoming_serial):
                            return err
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
            # Trimmed like validate/import_single_device (see the update branch above).
            incoming_serial = normalize_serial(libre_device.get("serial"))
            if incoming_serial and incoming_serial != "-":
                with transaction.atomic():
                    try:
                        locked_device = (
                            self.restricted_queryset(Device, "change")
                            .select_for_update(of=("self",))
                            .get(pk=existing_device.pk)
                        )
                    except Device.DoesNotExist:
                        return _htmx_error_response("Device no longer exists; it may have been deleted concurrently.")
                    # Re-check for serial ownership conflict under the locks, on the LOCKED row.
                    if err := _apply_conflict_checked_serial(locked_device, incoming_serial):
                        return err
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
            if not is_legacy_librenms_id(cf_value):
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
                    locked_device = (
                        self.restricted_queryset(existing_model, "change")
                        .select_for_update(of=("self",))
                        .get(pk=existing_device.pk)
                    )
                except existing_model.DoesNotExist:
                    return _htmx_error_response("Object no longer exists; it may have been deleted concurrently.")
                # Re-check under lock — another request may have already migrated it
                cf_locked = locked_device.custom_field_data.get("librenms_id")
                if not is_legacy_librenms_id(cf_locked):
                    return _htmx_error_response("Device librenms_id is already in JSON format; no migration needed.")
                cf_locked_int = int(cf_locked) if isinstance(cf_locked, str) else cf_locked
                if cf_locked_int != librenms_id:
                    return _htmx_error_response(
                        f"Legacy librenms_id changed under lock ({cf_locked_int} != {librenms_id}); cannot migrate safely."
                    )
                # Check that no other object already owns this ID (server-scoped or legacy)
                server_key = self.librenms_api.server_key
                from netbox_librenms_plugin.utils import AmbiguousLibreNMSIdError, find_by_librenms_id

                try:
                    match = find_by_librenms_id(existing_model, cf_locked_int, server_key)
                except AmbiguousLibreNMSIdError:
                    return _htmx_error_response(
                        f"librenms_id {cf_locked_int} is ambiguous — it matches more than one device. "
                        "Resolve the duplicate assignment before migrating."
                    )
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

        # Rebind to the POSTed server, failing closed (blank/unknown/misconfigured) so a missing
        # or broken default can't 500 via the lazy librenms_api property.
        if err := _rebind_or_htmx_error(self, request):
            return err

        from dcim.models import DeviceType

        libre_device = fetch_device_with_cache(device_id, self.librenms_api)
        if not libre_device:
            return _htmx_error_response("Device not found in LibreNMS.")

        hardware = (libre_device.get("hardware") or "").strip()
        if not hardware or hardware == "-":
            return _htmx_error_response("Device has no hardware string — cannot create mapping.")

        # Key the mapping on the NORMALISED hardware string. match_librenms_hardware_to_device_type
        # normalises via the device_type rules before the DeviceTypeMapping lookup, so a mapping
        # saved under the raw value (e.g. "WS-C9300" when a rule strips it to "C9300") would never
        # be found by the revalidation below. Strip the rule output too: DeviceTypeMapping.clean()
        # strips before save, so an untrimmed key (e.g. " C9300 ") would miss the stored "c9300"
        # mapping and take the add path, then fail uniqueness validation generically on save.
        from netbox_librenms_plugin.utils import apply_normalization_rules

        mapping_hardware = (apply_normalization_rules(value=hardware, scope="device_type") or "").strip()
        if not mapping_hardware or mapping_hardware == "-":
            return _htmx_error_response("Device hardware normalised to an empty value — cannot create mapping.")

        device_type_id = request.POST.get("device_type_id", "").strip()
        if not device_type_id:
            return _htmx_error_response("Please select a device type before submitting.")

        try:
            device_type_id = int(device_type_id)
        except (ValueError, TypeError):
            return _htmx_error_response("Invalid device type selection.")

        # Reject ambiguous state up front: multiple case-variant rows for the same hardware
        # string mean .first() would silently mutate an arbitrary one and leave the duplicate
        # unresolved. Fetch [:2] once and reuse it for both the ambiguity check and the
        # existing-mapping resolution rather than a separate count() + first() (two queries for
        # the same filter). Key on the NORMALISED hardware string (what's actually stored), not
        # the raw value. Mirrors AddPlatformMappingView and the locked read below.
        upfront_rows = list(DeviceTypeMapping.objects.filter(librenms_hardware__iexact=mapping_hardware)[:2])
        if len(upfront_rows) > 1:
            return _htmx_error_response(
                "Multiple mappings exist for this hardware string. Remove duplicates before updating."
            )
        # Resolve the existing mapping first so we only require the permission
        # actually needed: "add" for a new mapping, "change" for an update.
        existing_mapping = upfront_rows[0] if upfront_rows else None
        if existing_mapping:
            self.required_object_permissions = {"POST": [("view", DeviceType), ("change", DeviceTypeMapping)]}
        else:
            self.required_object_permissions = {"POST": [("view", DeviceType), ("add", DeviceTypeMapping)]}
        if error := self.require_object_permissions("POST"):
            return error
        if existing_mapping and not _mapping_change_is_allowed(self, DeviceTypeMapping, existing_mapping.pk):
            return _htmx_error_response("Existing mapping is no longer available.")

        try:
            device_type = self.restricted_queryset(DeviceType).get(pk=device_type_id)
        except DeviceType.DoesNotExist:
            return _htmx_error_response("Selected device type not found.")

        try:
            with transaction.atomic():
                # Lock the row to close the window between the upfront permission
                # check and the actual write (select_for_update prevents a concurrent
                # INSERT from slipping through undetected). Materialise [:2] in one query
                # (count() would drop the FOR UPDATE clause) and reject a concurrently-
                # created duplicate rather than mutating an arbitrary row. Key on the
                # NORMALISED hardware string (mapping_hardware) so the lock matches
                # the existing_mapping lookup and create() below.
                locked, lock_error = _lock_mapping_in_scope(
                    self,
                    DeviceTypeMapping,
                    {"librenms_hardware__iexact": mapping_hardware},
                    "Multiple mappings exist for this hardware string. Remove duplicates before updating.",
                )
                if lock_error is not None:
                    return lock_error
                if locked and not existing_mapping:
                    # A concurrent request created the mapping after our upfront read.
                    # Only escalate to change permission if we would actually mutate the row;
                    # if the locked row already maps to the same device type this is a no-op
                    # and the caller needs only the add permission they already passed above.
                    if locked.netbox_device_type_id != device_type_id:
                        self.required_object_permissions = {
                            "POST": [("view", DeviceType), ("change", DeviceTypeMapping)]
                        }
                        if error := self.require_object_permissions("POST"):
                            return error
                if existing_mapping and not locked:
                    # The mapping was deleted between our upfront read and the lock.
                    # We are about to CREATE a new row, so require add permission.
                    self.required_object_permissions = {"POST": [("view", DeviceType), ("add", DeviceTypeMapping)]}
                    if error := self.require_object_permissions("POST"):
                        return error
                if locked:
                    if locked.netbox_device_type_id != device_type_id:
                        if not _mapping_change_is_allowed(self, DeviceTypeMapping, locked.pk):
                            return _htmx_error_response("Existing mapping is no longer available.")
                        locked.netbox_device_type = device_type
                        locked.full_clean()
                        locked.save()
                else:
                    try:
                        DeviceTypeMapping.objects.create(
                            librenms_hardware=mapping_hardware.lower(),
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

        # Repopulate (rather than clear) the cache with the LibreNMS device we already fetched
        # at the top of this request. Re-validation reads the new mapping from the NetBox DB, so
        # the cached LibreNMS payload stays correct; keeping it means the modal/row refresh below
        # never issues a second LibreNMS round-trip — and a transient LibreNMS outage after the
        # commit can no longer downgrade the refresh to a stale/"not found" state (issue #66).
        # Preserve the entry's REMAINING TTL rather than granting a fresh full window: the
        # snapshot may be minutes old, and re-arming it full-length would keep serving it to
        # the bulk-import seed long after the original expiry. When the entry has already
        # expired mid-request, leave it expired (next read fetches live); when the backend
        # can't report TTLs (non-Redis, e.g. tests' LocMemCache), keep the full-timeout
        # repopulate — NetBox deployments are Redis-backed, so that branch is test-only.
        from netbox_librenms_plugin.utils import cache_remaining_ttl

        cache_key = get_import_device_cache_key(device_id, self.librenms_api.server_key)
        remaining_ttl = cache_remaining_ttl(cache, cache_key)
        if remaining_ttl is None:
            cache.set(cache_key, libre_device, timeout=self.librenms_api.cache_timeout)
        elif remaining_ttl > 0:
            cache.set(cache_key, libre_device, timeout=remaining_ttl)

        # Re-render the modal content as an OOB swap so it updates in place.
        # The inner views render via Django templates (auto-escaped), so the
        # decoded content is already safe HTML; wrap with format_html + mark_safe
        # to compose the OOB envelope without introducing new escape boundaries
        # (CodeQL trust-assertion pattern, see plugin docs).
        # The cache repopulation above keeps DeviceValidationDetailsView.get's
        # fetch_device_with_cache a cache hit, so no extra LibreNMS call is made.
        detail_view = DeviceValidationDetailsView()
        detail_view._librenms_api = self._librenms_api
        modal_html = detail_view.get(request, device_id).content.decode("utf-8")
        oob_modal = format_html(
            '<div id="htmx-modal-content" hx-swap-oob="innerHTML">{}</div>',
            mark_safe(modal_html),
        )

        # Re-validate and include the background table row as a second OOB swap so the
        # row reflects the new mapping immediately without a secondary JS-triggered request.
        # Reuse the in-memory libre_device directly (no re-fetch) via validate_and_apply_selections.
        validation, selections = self.validate_and_apply_selections(device_id, request, libre_device)
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

        return HttpResponse(format_html("{}{}", oob_modal, row_html), content_type="text/html")


class CreatePlatformFromImportView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View
):
    """HTMX view to create a Platform (and optionally a mapping and device assignment) from the import page."""

    def get(self, request, device_id):
        """Render the shared create-platform form fragment for the import HTMX modal."""
        from dcim.models import Manufacturer

        post_server_key = (request.GET.get("server_key") or "").strip()
        # Rebind to the POSTed server, failing closed on a blank/unknown/misconfigured key so a
        # missing or broken default can't raise a 500 via the lazy self.librenms_api property.
        if self.rebind_api_for_server(post_server_key) is None:
            return _htmx_error_response("Selected LibreNMS server is no longer configured.")

        libre_device = fetch_device_with_cache(device_id, self.librenms_api)
        if not libre_device:
            # 200, not 404 — HTMX fragment; a 4xx makes HTMX skip the swap so this inline alert
            # would never render in place.
            return HttpResponse(
                '<div class="alert alert-danger">Device not found in LibreNMS.</div>',
                status=200,
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

        # Rebind to the POSTed server, failing closed (blank/unknown/misconfigured) so a missing
        # or broken default can't 500 via the lazy librenms_api property.
        if err := _rebind_or_htmx_error(self, request):
            return err

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

        # Do NOT gate the upfront POST on ("add", PlatformMapping): creating/assigning the
        # Platform is the primary action and must succeed for a user who can add a Platform
        # even when they can't create OS mappings. The optional mapping write is gated at its
        # own site below, where a missing add-permission skips the mapping with a warning
        # instead of failing the whole request.
        perms = [("add", Platform)]
        if target_model is not None:
            perms.append(("change", target_model))
        # When a manufacturer is posted it is resolved by client-supplied id through a restricted
        # queryset, so state that read in the gate.
        if (request.POST.get("manufacturer") or "").strip():
            perms.append(("view", Manufacturer))
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
                manufacturer = self.restricted_queryset(Manufacturer).get(pk=int(manufacturer_id))
            except (Manufacturer.DoesNotExist, ValueError, TypeError):
                # The user explicitly submitted a manufacturer; a stale/tampered id must be
                # rejected, not silently dropped to None (which would persist a Platform with
                # the wrong/no manufacturer).
                return _htmx_error_response("Selected manufacturer not found.")

        try:
            with transaction.atomic():
                platform = Platform(
                    name=platform_name,
                    slug=slugify(platform_name),
                    manufacturer=manufacturer,
                )
                platform.full_clean()
                platform.save()

                if create_mapping and librenms_os:
                    if not PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).exists():
                        # Re-check the add permission at the write site. The upfront gate only
                        # requires ("add", PlatformMapping) when no mapping existed at preflight;
                        # if one existed then but was deleted since, this branch would otherwise
                        # create a mapping the caller was never authorized for. Skip rather than
                        # error — the Platform is already created and is the primary action.
                        from utilities.permissions import get_permission_for_model

                        if request.user.has_perm(get_permission_for_model(PlatformMapping, "add")):
                            try:
                                with transaction.atomic():
                                    # full_clean() before save so a tampered/overlong POST-derived
                                    # librenms_os fails as a caught ValidationError rather than a raw
                                    # DataError that would 500 the modal. The mapping is a secondary
                                    # side-effect, so skip+warn instead of failing the Platform create.
                                    mapping = PlatformMapping(
                                        librenms_os=librenms_os.lower(),
                                        netbox_platform=platform,
                                    )
                                    mapping.full_clean()
                                    mapping.save()
                            except IntegrityError:
                                # A concurrent request inserted the mapping between our existence
                                # check and save, so ours was not applied. If the winning row
                                # targets a *different* platform, future imports for this OS keep
                                # resolving through it rather than the platform just created —
                                # surface the same warning as the "already exists" branch instead
                                # of reporting a clean success. Same-platform winner is a true no-op.
                                winner = PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).first()
                                if winner is not None and getattr(winner, "netbox_platform_id", None) != platform.pk:
                                    winner_target = getattr(winner.netbox_platform, "name", None)
                                    transaction.on_commit(
                                        lambda os=librenms_os, target=winner_target: messages.warning(
                                            request,
                                            f"Platform created, but a LibreNMS-OS mapping for '{os}' already exists"
                                            + (f" (→ {target})" if target else "")
                                            + ". It was left unchanged, so future imports for this OS will keep "
                                            "using the existing mapping. Update the mapping if you want them to use "
                                            "the new platform.",
                                        )
                                    )
                            except ValidationError:
                                logger.warning(
                                    "CreatePlatformFromImportView: skipped invalid PlatformMapping for OS %r",
                                    librenms_os,
                                    exc_info=True,
                                )
                                transaction.on_commit(
                                    lambda os=librenms_os: messages.warning(
                                        request,
                                        f"Platform created, but the LibreNMS-OS mapping for '{os}' was not "
                                        "added — the OS value was invalid.",
                                    )
                                )
                        else:
                            logger.warning(
                                "CreatePlatformFromImportView: skipped PlatformMapping create for OS %r — "
                                "user lacks add permission (mapping was removed after the preflight check).",
                                librenms_os,
                            )
                            # Surface it in the modal too — but only after the Platform commits,
                            # so we don't warn about a skipped side-effect of a rolled-back write.
                            transaction.on_commit(
                                lambda os=librenms_os: messages.warning(
                                    request,
                                    f"Platform created, but the LibreNMS-OS mapping for '{os}' was not added — "
                                    "you lack permission to add mappings.",
                                )
                            )
                    else:
                        # A mapping for this OS already exists, so create_mapping is a silent
                        # no-op: the new Platform is assigned to the current object, but future
                        # imports for this OS keep resolving through the pre-existing mapping.
                        # Surface that mismatch instead of reporting a clean success.
                        existing = PlatformMapping.objects.filter(librenms_os__iexact=librenms_os).first()
                        existing_target = getattr(existing.netbox_platform, "name", None) if existing else None
                        transaction.on_commit(
                            lambda os=librenms_os, target=existing_target: messages.warning(
                                request,
                                f"Platform created, but a LibreNMS-OS mapping for '{os}' already exists"
                                + (f" (→ {target})" if target else "")
                                + ". It was left unchanged, so future imports for this OS will keep using the "
                                "existing mapping. Update the mapping if you want them to use the new platform.",
                            )
                        )
        except ValidationError as exc:
            logger.exception("CreatePlatformFromImportView: validation failed while creating platform")
            detail = exc.message_dict if hasattr(exc, "message_dict") else str(exc)
            return _htmx_error_response(f"Error creating platform: {detail}")
        except IntegrityError:
            logger.exception("CreatePlatformFromImportView: integrity error while creating platform")
            return _htmx_error_response(
                "Error creating platform due to a database constraint. Please try again or contact an administrator."
            )

        # Assign the new platform to the existing object as a best-effort side effect, in its
        # OWN transaction. The platform create above is the primary action and is already
        # committed; a failure here — the target vanishing before the lock, or full_clean()
        # tripping on unrelated legacy data on that record — must NOT roll back the platform.
        # It must, however, be reported to the user (see assignment_error below): silently
        # rendering the success swap would imply the device received the platform when it did not.
        assignment_error = None
        if target_model is not None and target_pk is not None:
            try:
                with transaction.atomic():
                    target = (
                        self.restricted_queryset(target_model, "change")
                        .select_for_update(of=("self",))
                        .get(pk=target_pk)
                    )
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
                assignment_error = (
                    f'Platform "{platform.name}" was created, but the target '
                    f"{target_model._meta.verbose_name} no longer exists, so it could not be "
                    "assigned. Assign the platform manually if it is still needed."
                )
            except (ValidationError, IntegrityError) as exc:
                logger.warning(
                    "CreatePlatformFromImportView: platform '%s' created but assignment to "
                    "%s pk=%s failed and was skipped: %s",
                    platform.name,
                    target_model.__name__,
                    target_pk,
                    exc,
                )
                assignment_error = (
                    f'Platform "{platform.name}" was created, but could not be assigned to the '
                    f"{target_model._meta.verbose_name}. Assign the platform manually if needed."
                )
        else:
            logger.info(
                "CreatePlatformFromImportView: no existing NetBox object matched "
                "for LibreNMS device_id=%s; platform '%s' created without assignment",
                device_id,
                platform.name,
            )

        cache_key = get_import_device_cache_key(device_id, self.librenms_api.server_key)
        cache.delete(cache_key)

        if assignment_error:
            # The platform (and any mapping) were intentionally kept, but the device assignment
            # failed. Surface that to the user instead of a success swap — the failure detail is
            # already logged above; the toast carries a fixed, non-sensitive message.
            return _htmx_error_response(assignment_error)

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

        response = HttpResponse(format_html("{}{}", oob_modal, row_html), content_type="text/html")
        # render_device_row() already attached any queued messages (it peeks, leaving them
        # pending), so row_html already carries the #django-messages toast. Only attach here
        # when no row was rendered — otherwise a queued warning (e.g. a skipped mapping) would
        # render the same toast twice.
        if libre_device is None or validation is None:
            return _attach_messages_oob(response, request)
        return response


class AddAsOOBView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, DeviceImportHelperMixin, View
):
    """HTMX view to link a LibreNMS OOB controller device to an existing NetBox Device."""

    def post(self, request, device_id):
        """Attach a LibreNMS OOB identity to the matched NetBox device."""
        if error := self.require_write_permission():
            return error

        from dcim.models import Device

        existing_device_id = request.POST.get("existing_device_id")
        if not existing_device_id:
            return _htmx_error_response("Missing existing_device_id")

        # Rebind to the POSTed server, failing closed (blank/unknown/misconfigured) so a missing
        # or broken default can't 500 via the lazy librenms_api property.
        if err := _rebind_or_htmx_error(self, request):
            return err

        # Gate before the lookup (see DeviceConflictActionView.post).
        self.required_object_permissions = {"POST": [("change", Device)]}
        if error := self.require_object_permissions("POST"):
            return error

        try:
            # Scope by "change" so a constrained grant can't attach an OOB link by raw pk.
            existing_device = self.restricted_queryset(Device, "change").get(pk=int(existing_device_id))
        except (Device.DoesNotExist, ValueError):
            return _htmx_error_response("Existing device not found")

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return _htmx_error_response("LibreNMS device not found")

        oob_candidate = validation.get("oob_candidate") if validation else None
        if not oob_candidate:
            return _htmx_error_response("No OOB candidate found in validation data")
        if oob_candidate["device"].pk != existing_device.pk:
            return _htmx_error_response("Device ID mismatch: existing_device_id does not match OOB candidate")

        server_key = self.librenms_api.server_key

        # Resolve the VC sync device up front: LibreNMS treats a Virtual Chassis as one logical
        # device, so the host librenms_id and OOB link live on the single sync member
        # (get_librenms_sync_device) — which may differ from the user-selected member the OOB
        # candidate matched (matched by the controller's shared chassis serial / primary IP).
        # Every reader (interfaces/cables/modules) resolves the sync device before
        # get_librenms_oob, so the link — and the lock, guards, IP set, and save around it — must
        # target the sync device too: writing to a non-sync member stores the OOB where no reader
        # looks and, since that member holds no host id, orphans it under no host link. For a
        # non-VC device (or when the selected member IS the sync device) this resolves to the same
        # row, so the common path is unchanged. The candidate-match check above stays on the
        # user-selected existing_device (it validates the POST, not the linkage target).
        sync_device = get_librenms_sync_device(existing_device, server_key=server_key) or existing_device
        # The sync device is DERIVED (a VC sibling of the scoped existing_device), and the block
        # below locks and saves it. Authorize it too, or a grant covering only the selected member
        # writes the OOB link onto a sibling it cannot see. Skipped when they are the same row.
        if (
            sync_device.pk != existing_device.pk
            and not self.restricted_queryset(Device, "change").filter(pk=sync_device.pk).exists()
        ):
            return _htmx_error_response("Existing device not found")

        # coerce_librenms_id centralizes the bool/int/str/positive checks (rejects bools,
        # non-numeric strings, zero/negatives) in one place.
        librenms_id = coerce_librenms_id(libre_device.get("device_id"))
        if librenms_id is None:
            return _htmx_error_response("Invalid or missing LibreNMS device_id")

        # Reject legacy bare-int librenms_id (shared predicate with DeviceConflictActionView and
        # set_librenms_device_id, so the three can't drift on what counts as legacy).
        if is_legacy_librenms_id(sync_device.custom_field_data.get("librenms_id")):
            return _htmx_error_response(
                "Device has a legacy bare-integer librenms_id; use 'Convert mapping' to migrate first."
            )

        from netbox_librenms_plugin.utils import set_librenms_oob

        oob_type = oob_candidate.get("type") or ""
        oob_ip_str = oob_candidate.get("ip") or None

        with transaction.atomic():
            try:
                sync_device = (
                    self.restricted_queryset(Device, "change").select_for_update(of=("self",)).get(pk=sync_device.pk)
                )
            except Device.DoesNotExist:
                return _htmx_error_response("Device no longer exists; it may have been deleted concurrently.")

            # Re-verify the legacy gate on the LOCKED row (mirrors DeviceConflictActionView's
            # post-lock gate): a legacy bare-int written between the unlocked check above and
            # this lock is valid on EVERY server as the documented universal fallback, and
            # letting it reach set_librenms_oob would trigger its legacy-promotion branch —
            # silently namespacing the id under this server only and dropping the device's
            # LibreNMS linkage on all others.
            if is_legacy_librenms_id(sync_device.custom_field_data.get("librenms_id")):
                return _htmx_error_response(
                    "Device has a legacy bare-integer librenms_id; use 'Convert mapping' to migrate first."
                )

            from netbox_librenms_plugin.utils import (
                AmbiguousLibreNMSIdError,
                find_by_librenms_id,
                get_librenms_device_id,
                get_librenms_oob,
            )

            # Reject if the locked OOB link differs from what this (possibly stale) modal
            # is about to write — by id OR by type. oob_type is already a canonical OOB_TYPES
            # token (so is the stored current_oob["type"]), so this is a like-for-like compare
            # that won't false-trip on an idempotent re-attach; it does catch a concurrent
            # re-detection that changed the controller type.
            current_oob = get_librenms_oob(sync_device, server_key=server_key)
            if current_oob and (
                coerce_librenms_id(current_oob.get("id")) != coerce_librenms_id(librenms_id)
                or (current_oob.get("type") or "") != oob_type
            ):
                return _htmx_error_response("OOB link was modified concurrently; refresh and retry.")

            # A concurrent change could have re-linked THIS device's host id to the incoming
            # OOB id; attaching it as OOB would then store it in both the host slot and oob.id
            # — a self host/OOB conflict. Reject that explicitly (find_by_librenms_id below
            # would match self and wave it through).
            current_host_id = get_librenms_device_id(sync_device, server_key=server_key, auto_save=False)
            if coerce_librenms_id(current_host_id) == coerce_librenms_id(librenms_id):
                return _htmx_error_response(
                    f"LibreNMS device #{librenms_id} is this device's host link; it can't also be its "
                    "OOB controller. Refresh and retry."
                )

            # Another device may already own this LibreNMS id (as its host id or OOB id)
            # since validation ran. Re-check inside the transaction and abort on a non-self
            # conflict so we don't point one LibreNMS device at two NetBox devices. Mirrors
            # PromoteToHostView's host_conflict guard. select_for_update locks the competing
            # owner row so a concurrent attach of the same id serializes against it; best-effort
            # like the serial guard (no unique constraint on the JSON cf to fully close it).
            try:
                oob_conflict = find_by_librenms_id(Device, librenms_id, server_key, select_for_update=True)
            except AmbiguousLibreNMSIdError:
                return _htmx_error_response(
                    f"LibreNMS device #{librenms_id} is ambiguous — it matches more than one NetBox "
                    "device. Resolve the duplicate assignment before attaching as OOB."
                )
            if oob_conflict is not None and oob_conflict.pk != sync_device.pk:
                return _htmx_error_response(
                    f"LibreNMS device #{librenms_id} is already linked to '{oob_conflict.name}'; refresh and retry."
                )

            try:
                set_librenms_oob(
                    sync_device,
                    librenms_id,
                    server_key,
                    oob_type=oob_type,
                )
            except ValueError as exc:
                return _htmx_error_response(f"Invalid OOB data: {exc}")

            update_fields = ["custom_field_data"]

            # Buffer OOB status messages and emit them only after the transaction
            # commits — a message queued before _save_device() would survive a
            # rollback and falsely claim the OOB link/IP was applied.
            deferred_messages = []

            # Set device.oob_ip from an interface-assigned IPAddress. NetBox
            # requires oob_ip be assigned to one of the device's interfaces, so
            # the user picks (or creates) the interface to hang the OOB IP on
            # via the OOB-attach form. Linkage (set_librenms_oob) happened above.
            if oob_ip_str and sync_device.oob_ip_id is None:
                # The top-level gate only authorizes ("change", Device), but the
                # IP-set sub-flow can create an Interface, create an IPAddress, or
                # re-home an existing one. Require the model perms the requested
                # operation actually needs; if missing, skip the IP-set (the link
                # still commits) rather than hard-failing — a return here would
                # roll back the whole transaction, including the linkage.
                perm_warning = self._missing_oob_ip_permissions(request, oob_ip_str, device=sync_device)
                oob_iface, iface_reason = (
                    (None, None) if perm_warning else self._resolve_oob_interface(request, sync_device)
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
                elif iface_reason == "invalid_name":
                    deferred_messages.append(
                        (
                            messages.WARNING,
                            f"OOB linked, but OOB IP {oob_ip_str} not set — the chosen interface name is "
                            "invalid (too long or contains unsupported characters).",
                        )
                    )
                elif iface_reason == "name_out_of_scope":
                    deferred_messages.append(
                        (
                            messages.WARNING,
                            f"OOB linked, but OOB IP {oob_ip_str} not set — an interface with that name "
                            "already exists on the device and is outside your view scope.",
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
                        # Guarded write: set_device_ip_fk() enforces that oob_ip is assigned to
                        # an interface on sync_device (it is — _attach_oob_ip() just hung it
                        # on oob_iface) before the batched update_fields save below, which skips
                        # full_clean() and would otherwise accept an off-device address.
                        update_fields.append(set_device_ip_fk(sync_device, "oob_ip", oob_ip, save=False))
                        deferred_messages.append(
                            (messages.INFO, f"Set OOB IP {oob_ip_str} on interface {oob_iface.name}.")
                        )
            elif oob_ip_str:
                # The device already has an OOB IP set. Don't silently overwrite it — that could
                # clobber an operator-set address — but don't let the user believe the controller's
                # IP was applied either. Surface that the existing OOB IP was kept when it differs
                # from the LibreNMS controller's IP (an equal one needs no message; it's correct).
                existing_oob_host = str(parse_host_address(str(sync_device.oob_ip)))
                # Compare version-aware (same_host parses both sides) so an equal address in a
                # different textual form — expanded vs compressed IPv6, or hex case — isn't reported
                # as "a different OOB IP". A raw != would warn on 2001:db8::1 vs 2001:0db8:...:0001.
                if not same_host(existing_oob_host, oob_ip_str):
                    deferred_messages.append(
                        (
                            messages.WARNING,
                            f"OOB linked, but OOB IP {oob_ip_str} not set — the device already has a "
                            f"different OOB IP ({existing_oob_host}). Clear the existing OOB IP first "
                            "to set the controller's IP.",
                        )
                    )

            if err := _save_device(sync_device, update_fields=update_fields, request=request):
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
            sync_device.name,
            server_key,
        )

        cache_key = get_import_device_cache_key(device_id, server_key)
        cache.delete(cache_key)

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            # The OOB attach already committed; don't report failure on a post-commit reload
            # miss. Surface the deferred outcome messages and ask the client to refresh.
            return self.post_commit_refresh_fallback(
                request,
                json.dumps({"validationRefresh": {"deviceId": device_id}}),
                deferred_messages,
            )

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
        """
        Return a warning string naming missing perms for the OOB-IP set, or None.

        The OOB-attach view authorizes ``("change", Device)`` at the top, but the
        IP-set sub-flow can additionally create an :class:`Interface` (when the user
        picks ``__new__``), create an :class:`IPAddress` (no record for the host yet),
        or re-home an existing one. Check the model perms the requested operation
        actually needs so a caller with only Device-change rights can't mutate
        Interface/IPAddress through this view.

        A malformed *ip_str* short-circuits to an invalid-IP warning: the
        ``address__net_host`` preflight below would raise on it, and
        _attach_oob_ip() would reject it anyway, so surface the same non-attachable
        outcome here.

        Args:
            request: The current HTTP request (source of the interface selection).
            ip_str (str): The OOB IP address to attach.
            device: The target device, used to check whether a named interface
                already exists.

        Returns:
            str | None: A warning naming the missing permission(s) or invalid IP, or
                None when no extra permission is needed.
        """
        from dcim.models import Interface
        from ipam.models import IPAddress
        from utilities.permissions import get_permission_for_model

        try:
            canonical_host = str(parse_host_address(ip_str))
        except ValueError:
            return f"OOB linked, but OOB IP {ip_str} not set — the IP address is invalid."

        needed = []
        iface_id = (request.POST.get("oob_interface_id") or "").strip()
        new_iface_name = (request.POST.get("oob_new_interface_name") or "").strip()
        # No interface target selected — empty, or "__new__" without a name — means
        # _resolve_oob_interface() returns no interface and oob_ip is never set, so neither an
        # Interface nor an IPAddress mutation runs. Don't demand add/change perms (or emit a
        # permission warning) for a write that won't happen; that just blocks the intended
        # "choose an interface" flow with a misleading error.
        has_interface_target = (iface_id and iface_id != "__new__") or (iface_id == "__new__" and bool(new_iface_name))
        if not has_interface_target:
            return None
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
        # Global table only (vrf__isnull) — the write path never touches VRF rows: it
        # creates a global /32, so a same-host address in a tenant VRF must demand 'add',
        # not 'change'.
        matches = list(IPAddress.objects.filter(address__net_host=canonical_host, vrf__isnull=True)[:2])
        ambiguous = len(matches) > 1
        existing = matches[0] if matches else None
        if existing is None:
            needed.append(("add", IPAddress))
        else:
            already_on_selected_iface = False
            if not ambiguous:
                # Resolve the interface the IP would actually land on, mirroring
                # _resolve_oob_interface(): an explicit PK, or — for the "__new__" branch — the
                # existing (device, name) interface it reuses when one already exists. If the IP
                # is already assigned there, _attach_oob_ip() is a no-op, so a change-Device-only
                # user must not be blocked on change-IPAddress.
                selected_iface_pk = None
                if iface_id and iface_id != "__new__":
                    try:
                        selected_iface_pk = int(iface_id)
                    except ValueError:
                        selected_iface_pk = None
                elif iface_id == "__new__" and new_iface_name and device is not None:
                    selected_iface_pk = (
                        Interface.objects.filter(device=device, name=new_iface_name)
                        .values_list("pk", flat=True)
                        .first()
                    )
                if selected_iface_pk is not None:
                    # Compare the assigned-object TYPE too: assigned_object is a GenericForeignKey,
                    # so a different model (e.g. a VMInterface) sharing the selected Interface's pk
                    # must not be treated as "already on the selected interface" and wave the
                    # change-IPAddress permission through.
                    assigned = existing.assigned_object
                    already_on_selected_iface = isinstance(assigned, Interface) and assigned.pk == selected_iface_pk
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
        """
        Resolve (or create) the interface the OOB IP should attach to.

        Reads ``oob_interface_id`` from the OOB-attach form: an interface PK, or the
        sentinel ``"__new__"`` to create one named ``oob_new_interface_name``.

        The caller runs inside ``transaction.atomic()``. The add-vs-reuse permission
        decision can only be made from the locked row: the unlocked pre-flight in
        ``_missing_oob_ip_permissions`` can race a concurrent delete and wave through
        a change-Device-only user, so re-verify ``add`` here before creating.
        Symmetric to the re-check in :meth:`_attach_oob_ip`.

        Args:
            request: The current HTTP request (source of the interface selection).
            device: The device the interface belongs to.

        Returns:
            tuple: ``(interface, None)`` on success, ``(None, None)`` when no
                selection was made, ``(None, "permission_add")`` when creating is
                required but the user lacks Interface ``add``, ``(None, "invalid_name")``
                for a malformed new name, or ``(None, "name_out_of_scope")`` when the
                requested interface is outside the caller's view scope.
        """
        from django.core.exceptions import ValidationError
        from dcim.models import Interface
        from utilities.permissions import get_permission_for_model

        iface_id = (request.POST.get("oob_interface_id") or "").strip()
        if iface_id == "__new__":
            name = (request.POST.get("oob_new_interface_name") or "").strip()
            if not name:
                return None, None
            # Lock the candidate so a concurrent create/delete can't flip add-vs-reuse
            # between this check and the create below.
            # Scope BEFORE locking: locking first lets a caller hold a row it cannot even see and
            # stall concurrent work on it. Lock only within the caller's view scope.
            existing = (
                Interface.objects.restrict(request.user, "view")
                # of=("self",): restrict() joins the permission tables, and a bare
                # select_for_update() would try to lock those joined rows too.
                .select_for_update(of=("self",))
                .filter(device=device, name=name)
                .first()
            )
            if existing is not None:
                return existing, None
            # The name may still be taken by a row outside that scope; refuse rather than race the
            # create below into an IntegrityError. `.exists()` reads no row data and takes no lock.
            if Interface.objects.filter(device=device, name=name).exists():
                return None, "name_out_of_scope"
            if not request.user.has_perm(get_permission_for_model(Interface, "add")):
                return None, "permission_add"
            # Nested savepoint: catching IntegrityError without one would poison the outer
            # transaction. A concurrent create of the same (device, name) — guarded by the
            # dcim_interface_unique_device_name constraint — means we just reuse the winner.
            try:
                with transaction.atomic():
                    iface = Interface(device=device, name=name, type="other")
                    # Validate field formats/length (an invalid/oversized name would otherwise
                    # raise ValidationError/DataError and surface as a 500). Skip the uniqueness
                    # check — the DB constraint + IntegrityError branch below handle the race.
                    iface.full_clean(validate_unique=False)
                    iface.save()
                    return iface, None
            except (ValidationError, DataError):
                return None, "invalid_name"
            except IntegrityError:
                # Lock the row we hand back: the OOB-IP assignment is generic-relational,
                # not FK-protected, so a concurrent delete before the IP save would orphan
                # oob_ip on a missing interface. select_for_update blocks that delete.
                existing = (
                    Interface.objects.restrict(request.user, "view")
                    .select_for_update(of=("self",))
                    .filter(device=device, name=name)
                    .first()
                )
                # The winner of the race can sit outside the caller's view scope, which is the
                # same refusal as the pre-create check above.
                return (existing, None) if existing is not None else (None, "name_out_of_scope")
        if iface_id:
            try:
                iface_pk = int(iface_id)
            except ValueError:
                return None, None
            try:
                # Lock the reused row too (same orphan-on-concurrent-delete reasoning).
                # Scoped like every other client-supplied id: the device filter proves where the
                # interface sits, not that the caller's grant covers it.
                interface = (
                    Interface.objects.restrict(request.user, "view")
                    .select_for_update(of=("self",))
                    .get(pk=iface_pk, device=device)
                )
                return interface, None
            except Interface.DoesNotExist:
                if Interface.objects.filter(pk=iface_pk, device=device).exists():
                    return None, "name_out_of_scope"
                return None, None
        return None, None

    @staticmethod
    def _attach_oob_ip(request, ip_str, interface):
        """
        Resolve the OOB :class:`IPAddress` for *ip_str* assigned to *interface*.

        Reuses an existing global-table record for the host (matched via ``net_host``
        so any prefix length is accepted; VRF rows are never touched) and re-homes it
        to *interface*, unless it is already assigned to a *different* device's object.
        Otherwise creates a global ``/32`` (IPv4) or ``/128`` (IPv6).

        Args:
            request: The current HTTP request (used for permission checks).
            ip_str (str): The OOB IP address to resolve.
            interface: The interface the address should be assigned to.

        Returns:
            tuple: ``(ip, None)`` on success, or ``(None, reason)`` where *reason* is
                ``"invalid"``, ``"conflict"`` (already on another device / create
                race), or ``"permission"``.
        """
        from ipam.models import IPAddress
        from utilities.permissions import get_permission_for_model

        try:
            parsed = parse_host_address(ip_str)
        except ValueError:
            return None, "invalid"

        # Lock the candidate row(s) — the caller runs inside transaction.atomic() — so a
        # concurrent attach can't flip the assignment between this ownership check and
        # the save. NetBox places no unique constraint on IPAddress.address, so the
        # create path stays best-effort: this narrows the TOCTOU window, it can't close it.
        # net_host ignores prefix length, so several rows can share the same host IP;
        # fetch up to two and refuse rather than re-home the wrong one by DB ordering.
        # Scope to the global table (vrf__isnull): the create path below makes a global
        # /32, and a same-host address inside a tenant VRF is a DIFFERENT address
        # (overlapping RFC1918 space) — re-homing it would hijack that VRF's IPAM record.
        # Scope BEFORE locking: locking first lets a caller hold a row it cannot even see and
        # stall concurrent work on it. The ambiguity check still has to see every row, so it
        # reads unlocked and by pk only, then the lock is taken inside the caller's change scope.
        host_rows = list(IPAddress.objects.filter(address__net_host=str(parsed), vrf__isnull=True)[:2])
        if len(host_rows) > 1:
            return None, "conflict"
        existing = None
        if host_rows:
            # Ownership belongs to the data, not the caller, so judge it before taking any lock:
            # a row owned elsewhere is refused without ever being pinned.
            if not _oob_ip_is_reassignable(host_rows[0], interface):
                return None, "conflict"
            existing = (
                IPAddress.objects.restrict(request.user, "change")
                # of=("self",): restrict() joins the permission tables, and a bare
                # select_for_update() would try to lock those joined rows too.
                .select_for_update(of=("self",))
                .filter(pk=host_rows[0].pk)
                .first()
            )
            # The row was there a moment ago, so a miss means the caller's change grant does not
            # cover it (or it was deleted in the race). Refuse either way rather than lock it.
            if existing is None:
                return None, "permission_change"
        if existing is not None:
            # Re-verify from the locked row: the pre-check above read it unlocked, so a concurrent
            # attach could have claimed it in between.
            if not _oob_ip_is_reassignable(existing, interface):
                return None, "conflict"
            if existing.assigned_object != interface:
                # Re-homing an existing IP is a 'change'. The lock above already ran through the
                # caller's change scope, so reaching here means the grant covers this row: the
                # unlocked pre-flight in _missing_oob_ip_permissions can race a concurrent create
                # and wave through an 'add'-only user, and the scoped lock is what catches that.
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
                    IPAddress.objects.create(address=f"{parsed}{mask}", assigned_object=interface, status="active"),
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

        existing_device_id = request.POST.get("existing_device_id")
        if not existing_device_id:
            return _htmx_error_response("Missing existing_device_id")

        # Fail closed on a blank/unknown/misconfigured key (the mixin validates the default too)
        # so a broken default can't 500 via the lazy self.librenms_api property later.
        if self.rebind_api_for_server(request.POST.get("server_key")) is None:
            return _htmx_error_response("Selected LibreNMS server is no longer configured.")

        # Gate before the lookup (see DeviceConflictActionView.post).
        self.required_object_permissions = {"POST": [("change", Device)]}
        if error := self.require_object_permissions("POST"):
            return error

        try:
            # Scope by "change" so a constrained grant can't re-point another device's linkage.
            existing_device = self.restricted_queryset(Device, "change").get(pk=int(existing_device_id))
        except (Device.DoesNotExist, ValueError):
            return _htmx_error_response("Existing device not found")

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
                override_device_type = self.restricted_queryset(DeviceType, "view").get(pk=int(override_dt_id))
            except (DeviceType.DoesNotExist, ValueError, TypeError):
                return _htmx_error_response("Invalid override_device_type_id")

        override_platform = None
        if override_platform_id:
            from dcim.models import Platform

            try:
                override_platform = self.restricted_queryset(Platform, "view").get(pk=int(override_platform_id))
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

        from netbox_librenms_plugin.utils import coerce_librenms_id

        # coerce_librenms_id centralizes the bool/int/str/positive checks (rejects bools,
        # non-numeric strings, zero/negatives) in one place — same guard used elsewhere.
        new_host_id = coerce_librenms_id(libre_device.get("device_id"))
        if new_host_id is None:
            return _htmx_error_response("Invalid or missing LibreNMS device_id")

        # Use coerce_librenms_id (not int()) so a boolean True/False in the JSON CF can't be
        # coerced to 1/0 and treated as a real device id — mirrors the new_host_id guard above.
        existing_libre_id = coerce_librenms_id(promote.get("existing_libre_id"))
        if existing_libre_id is None:
            return _htmx_error_response("Invalid existing LibreNMS id in promotion data")
        if existing_libre_id == new_host_id:
            return _htmx_error_response("Existing link already points at this LibreNMS device")

        oob_type = promote.get("existing_oob_type") or ""
        if not oob_type:
            return _htmx_error_response("Cannot determine OOB type for promotion")

        from netbox_librenms_plugin.utils import set_librenms_device_id, set_librenms_oob

        server_key = self.librenms_api.server_key

        # Reject legacy bare-int librenms_id form (caller should migrate first).
        if is_legacy_librenms_id(existing_device.custom_field_data.get("librenms_id")):
            return _htmx_error_response(
                "Device has a legacy bare-integer librenms_id; use 'Convert mapping' to migrate first."
            )

        from netbox_librenms_plugin.utils import (
            AmbiguousLibreNMSIdError,
            find_by_librenms_id,
            get_librenms_device_id,
            get_librenms_oob,
        )

        # Pre-resolve any device already linked to new_host_id WITHOUT locking, so the
        # transaction can lock every row it touches in one deterministic pk order (mirrors the
        # merge flow). Locking existing_device first and the conflict second would let two
        # concurrent opposite-direction promotions each hold one row and block on the other
        # (lock-order deadlock).
        try:
            pre_conflict = find_by_librenms_id(Device, new_host_id, server_key)
        except AmbiguousLibreNMSIdError:
            return _htmx_error_response(
                f"LibreNMS device #{new_host_id} is ambiguous — it matches more than one NetBox "
                "device. Resolve the duplicate assignment before promoting."
            )
        lock_pks = {existing_device.pk}
        if pre_conflict is not None:
            lock_pks.add(pre_conflict.pk)

        with transaction.atomic():
            locked = {d.pk: d for d in Device.objects.select_for_update().filter(pk__in=lock_pks).order_by("pk")}
            existing_device = locked.get(existing_device.pk)
            if existing_device is None:
                return _htmx_error_response("Device no longer exists; it may have been deleted concurrently.")
            if is_legacy_librenms_id(existing_device.custom_field_data.get("librenms_id")):
                return _htmx_error_response(
                    "Device has a legacy bare-integer librenms_id; use 'Convert mapping' to migrate first."
                )

            current_host_id = get_librenms_device_id(existing_device, server_key=server_key, auto_save=False)
            current_oob = get_librenms_oob(existing_device, server_key=server_key)
            if coerce_librenms_id(current_host_id) != coerce_librenms_id(existing_libre_id):
                return _htmx_error_response("LibreNMS host link changed concurrently; refresh and retry.")
            if current_oob:
                return _htmx_error_response(
                    "OOB link already set; this device may have been promoted by a concurrent request."
                )
            # Re-verify the conflict under lock (TOCTOU): a device could have gained or lost
            # new_host_id between the unlocked pre-lookup and acquiring the row locks. The rows
            # in lock_pks are already locked in pk order, so this re-check doesn't reorder locks.
            try:
                host_conflict = find_by_librenms_id(Device, new_host_id, server_key, select_for_update=True)
            except AmbiguousLibreNMSIdError:
                return _htmx_error_response(
                    f"LibreNMS device #{new_host_id} is ambiguous — it matches more than one NetBox "
                    "device. Resolve the duplicate assignment before promoting."
                )
            if host_conflict is not None and host_conflict.pk != existing_device.pk:
                return _htmx_error_response(
                    f"LibreNMS device #{new_host_id} is already linked to '{host_conflict.name}'; refresh and retry."
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
                return _htmx_error_response(f"Invalid promotion data: {exc}")

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

            # _save_device() persists via update_fields (skipping full_clean()) but re-runs the
            # platform/device_type manufacturer invariant via _platform_device_type_mismatch()
            # whenever those columns are written, so an incompatible override is rejected there —
            # no inline duplicate (which would only drift in wording from the shared check).
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

        # Reuse the LibreNMS device already fetched above (invariant within this request); only the
        # NetBox-side validation needs to re-run to reflect the just-committed promotion/merge.
        libre_device, validation, selections = self.get_validated_device_with_selections(
            device_id, request, libre_device=libre_device
        )
        if not libre_device:
            # Promotion already committed; a post-commit reload miss must not report failure.
            return self.post_commit_refresh_fallback(
                request, json.dumps({"validationRefresh": {"deviceId": device_id}})
            )

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
        from netbox_librenms_plugin.utils import (
            mark_librenms_migrated,
            merge_librenms_links,
        )

        # Fail closed on a blank/unknown/misconfigured key (the mixin validates the default too)
        # so a broken default can't 500 via the lazy self.librenms_api property later.
        if self.rebind_api_for_server(request.POST.get("server_key")) is None:
            return _htmx_error_response("Selected LibreNMS server is no longer configured.")

        winner_pk_raw = request.POST.get("winner_pk")
        if not winner_pk_raw:
            return _htmx_error_response("Missing winner_pk")
        try:
            winner_pk = int(winner_pk_raw)
        except (TypeError, ValueError):
            return _htmx_error_response("Invalid winner_pk")

        libre_device, validation, selections = self.get_validated_device_with_selections(device_id, request)
        if not libre_device:
            return _htmx_error_response("LibreNMS device not found")

        candidate_pks = merge_candidate_pks(validation)
        # Derive the donor from the selected winner. The merge is always between this fixed pair
        # of candidates, so the donor is unambiguously the other candidate and no client-supplied
        # donor state is needed. This also enforces that the winner is one of the two candidates.
        if winner_pk not in candidate_pks or len(candidate_pks) != 2:
            return _htmx_error_response("winner_pk does not match the validation result's merge candidates")
        donor_pk = next(pk for pk in candidate_pks if pk != winner_pk)

        # Permission gate: user must be able to change BOTH devices. Model-level first, then
        # object-scoped: the gate below passes for a constrained grant, so both sides must be
        # resolved through the restricted queryset or the merge could absorb an unseen device.
        self.required_object_permissions = {"POST": [("change", Device)]}
        if error := self.require_object_permissions("POST"):
            return error

        changeable = self.restricted_queryset(Device, "change")
        try:
            winner = changeable.get(pk=winner_pk)
            donor = changeable.get(pk=donor_pk)
        except Device.DoesNotExist:
            return _htmx_error_response("Winner or donor device not found")

        server_key = self.librenms_api.server_key

        def _database_failure_response():
            logger.exception(
                "MergeNetBoxDevicesView: database failure merging winner=%s donor=%s",
                winner.pk,
                donor.pk,
            )
            transaction.set_rollback(True)
            return _htmx_error_response("Cannot merge because the database operation failed. Please retry.")

        # LibreNMS treats a Virtual Chassis as one logical device, so the host/OOB link lives on the
        # single sync member (get_librenms_sync_device) — which may differ from the winner/donor the
        # user selected when a candidate matched a NON-sync VC member by serial/hostname. Merge the
        # link state on the sync devices: writing it to a non-sync member would either split-brain a
        # VC that already has a linked member, or leave the donor's real link (on its sync sibling)
        # uncleared where every reader still resolves and finds it. For non-VC devices these resolve
        # back to the same rows, so the common path is unchanged. The oob_ip transfer and the
        # candidate-match check deliberately stay on the user-selected winner/donor: oob_ip is a
        # per-device FK on those rows, not a VC link.
        with transaction.atomic():
            # Lock the selected winner/donor AND every current member of their virtual chassis, in
            # deterministic pk order to avoid deadlocks, BEFORE resolving the sync device:
            # get_librenms_sync_device scans every VC member's librenms_id custom field, so resolving
            # it from unlocked rows would let a concurrent VC edit (a member joining/leaving, or the
            # id moving between members) shift the sync member to a row we never locked — splitting
            # the link across the VC. Non-VC merges add no members, so they lock the same two rows.
            try:
                lock_pks = {winner.pk, donor.pk}
                for candidate in (winner, donor):
                    vc = getattr(candidate, "virtual_chassis", None)
                    if vc:
                        lock_pks.update(vc.members.values_list("pk", flat=True))
                lock_pks = sorted(lock_pks)
                locked = list(Device.objects.select_for_update().filter(pk__in=lock_pks).order_by("pk"))
            except DatabaseError:
                return _database_failure_response()
            if len(locked) != len(lock_pks):
                return _htmx_error_response(
                    "One of the devices no longer exists; it may have been deleted concurrently."
                )
            locked_by_pk = {d.pk: d for d in locked}
            winner = locked_by_pk[winner.pk]
            donor = locked_by_pk[donor.pk]

            # Resolve the sync devices from the now-locked, current state.
            try:
                winner_sync = get_librenms_sync_device(winner, server_key=server_key) or winner
                donor_sync = get_librenms_sync_device(donor, server_key=server_key) or donor
            except DatabaseError:
                return _database_failure_response()
            # Fail closed if a concurrent VC change added a member after we snapshotted lock_pks and
            # the resolved sync device wasn't among the locked rows — never write the link to an
            # unlocked row; the operator can retry.
            if winner_sync.pk not in locked_by_pk or donor_sync.pk not in locked_by_pk:
                return _htmx_error_response("Virtual chassis membership changed during the merge; please retry.")
            winner_sync = locked_by_pk[winner_sync.pk]
            donor_sync = locked_by_pk[donor_sync.pk]
            # The sync devices are DERIVED (a VC sibling of the selected winner/donor), not the pks
            # scoped above, and the save block below writes their custom_field_data. Authorize them
            # too, or a grant covering only the selected pair mutates the link-holding sibling.
            sync_pks = {winner_sync.pk, donor_sync.pk}
            try:
                changeable_sync_pks = set(changeable.filter(pk__in=sync_pks).values_list("pk", flat=True))
            except DatabaseError:
                return _database_failure_response()
            if changeable_sync_pks != sync_pks:
                return _htmx_error_response("Winner or donor device not found")
            # Gate the link-holding sync devices, not merely the selected VC members. The merge
            # helpers reject legacy data either way; checking here preserves the actionable
            # convert-first message when the legacy link lives on a sibling.
            for label, obj in (("winner", winner_sync), ("donor", donor_sync)):
                if is_legacy_librenms_id(obj.custom_field_data.get("librenms_id")):
                    return _htmx_error_response(
                        f"{label.capitalize()} device has a legacy bare-integer librenms_id; "
                        "use 'Convert mapping' to migrate before merging."
                    )
            if winner_sync.pk == donor_sync.pk:
                # Both candidates belong to the same virtual chassis (one sync device); merging its
                # LibreNMS link into itself would read and write the same CF entry. Fail closed.
                return _htmx_error_response(
                    "Winner and donor resolve to the same LibreNMS sync device "
                    "(they are members of the same virtual chassis); there is nothing to merge."
                )

            # merge_librenms_links(), set_device_ip_fk() and mark_librenms_migrated() all raise
            # ValueError on corrupt link shapes or an ownership violation. The locked OOB-IP reads
            # can also raise DatabaseError (for example, a lock timeout). Guard the whole group:
            # none of it persists anything (the save block is below), so every failure must return
            # a safe toast and roll back rather than bubbling up as a 500.
            try:
                summary = merge_librenms_links(winner_sync, donor_sync, server_key=server_key)

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
                    # Lock the IP row AND its owning interface before reading the assignment and
                    # transferring. The Device-row lock above does NOT stabilize the interface's
                    # device_id, so without these locks a concurrent interface move between the
                    # check and commit could leave winner.oob_ip pointing at an interface no longer
                    # on the winner — the invalid state set_device_ip_fk()'s contract requires the
                    # caller to lock against, exactly as migrate._reconcile_donor_device_ip_fks does.
                    from dcim.models import Interface
                    from ipam.models import IPAddress

                    locked_oob_ip = self.relock_scoped_row(IPAddress, pk=donor.oob_ip_id)
                    oob_assigned = locked_oob_ip.assigned_object if locked_oob_ip is not None else None
                    if isinstance(oob_assigned, Interface):
                        locked_iface = self.relock_scoped_row(Interface, pk=oob_assigned.pk)
                        if locked_iface is not None and locked_iface.device_id == winner.pk:
                            # Refresh the cached GenericForeignKey on locked_oob_ip to the freshly
                            # locked interface: set_device_ip_fk() re-reads locked_oob_ip.assigned_object
                            # for its ownership check, but the GFK cache still holds the pre-lock
                            # snapshot (the FK id fields didn't change, so Django won't re-query). A
                            # concurrent move ONTO the winner landing between the assigned_object read
                            # above and this lock would otherwise pass the locked-row gate here and then
                            # be spuriously rejected against the stale device_id — mirrors the
                            # freshen-after-lock pattern in migrate._reconcile_donor_device_ip_fks.
                            locked_oob_ip.assigned_object = locked_iface
                            # set_device_ip_fk() re-checks the address is on a winner interface
                            # (verified above under lock) and assigns without saving — the batched
                            # donor-then-winner save below preserves the release-before-claim
                            # ordering the UNIQUE oob_ip FK requires.
                            set_device_ip_fk(winner, "oob_ip", locked_oob_ip, save=False)
                            set_device_ip_fk(donor, "oob_ip", None, save=False)
                            oob_ip_transferred = True

                # Clear the donor sync device's active link and stamp the migration marker there —
                # the marker is a sibling key of id/oob in the same librenms_id entry, so it must
                # live wherever the link it supersedes lives (the sync device), not the raw member.
                mark_librenms_migrated(donor_sync, winner_sync.pk, server_key=server_key)
            except ValueError as exc:
                # Nothing was persisted yet, but locks were taken under this atomic block — roll
                # back defensively before returning the fail-closed toast.
                transaction.set_rollback(True)
                return _htmx_error_response(f"Cannot merge: {exc}")
            except DatabaseError:
                return _database_failure_response()

            # Persist only the fields we actually touched. Calling ``full_clean()`` here (or calling
            # ``_save_device`` without update_fields) would re-validate every field on the device —
            # undesirable when the rows hold pre-existing inconsistencies (e.g. ``face`` set without
            # ``rack``) that are unrelated to this merge. See issue surfaced during eve-ng-02 merge.
            # Persist only the fields we actually touched, per row. The LibreNMS link merge +
            # migration marker land on the sync devices (custom_field_data); the oob_ip transfer
            # lands on the selected winner/donor. When a selected device IS its own sync device
            # (the non-VC common case) these collapse onto one row, saved once with both fields —
            # never twice with different update_fields, which would drop one change.
            fields_by_pk = {}

            def _touch(dev, field):
                fields_by_pk.setdefault(dev.pk, set()).add(field)

            _touch(winner_sync, "custom_field_data")
            _touch(donor_sync, "custom_field_data")
            if oob_ip_transferred:
                _touch(winner, "oob_ip")
                _touch(donor, "oob_ip")

            # The donor side must release the OneToOne ``oob_ip`` (set to None) before the winner
            # side claims it, or two devices momentarily point at the same IP and violate the unique
            # constraint on ``Device.oob_ip``. The sync devices only carry custom_field_data (no
            # unique field), so their order is free; save the donor group first and the selected
            # winner last. A sync device can't cross-coincide with the other side's selected row
            # (guaranteed by the winner_sync != donor_sync guard above), so grouping is unambiguous.
            donor_extra = [donor_sync] if donor_sync.pk != donor.pk else []
            winner_extra = [winner_sync] if winner_sync.pk != winner.pk else []
            save_order = [donor, *donor_extra, *winner_extra, winner]
            saved = set()
            for dev in save_order:
                if dev.pk in saved:
                    continue
                saved.add(dev.pk)
                fields = fields_by_pk.get(dev.pk)
                if fields and (error := _save_device(dev, update_fields=sorted(fields), request=request)):
                    logger.error(
                        "MergeNetBoxDevicesView: failed to persist merge winner=%s donor=%s",
                        winner.pk,
                        donor.pk,
                    )
                    transaction.set_rollback(True)
                    return error

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

        # Reuse the LibreNMS device already fetched above (invariant within this request); only the
        # NetBox-side validation needs to re-run to reflect the just-committed promotion/merge.
        libre_device, validation, selections = self.get_validated_device_with_selections(
            device_id, request, libre_device=libre_device
        )
        if not libre_device:
            # Merge already committed; a post-commit reload miss must not report failure
            # (the donor is already absorbed — a retry would target stale/invalid state).
            return self.post_commit_refresh_fallback(request, "closeModal")

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
        # json.loads can return any JSON type (list, str, number, null); a non-object payload would
        # AttributeError on data.get() below. Reject it as a 400 rather than 500.
        if not isinstance(data, dict):
            return JsonResponse({"error": "Invalid JSON payload"}, status=400)

        key = data.get("key")
        value = data.get("value")

        # ALLOWED_PREFS is a dict, so an unhashable key from the JSON body raises rather than
        # missing; the envelope check above stops one level short of this.
        if not isinstance(key, str) or key not in self.ALLOWED_PREFS:
            return JsonResponse({"error": "Invalid preference key"}, status=400)

        if key == "interface_name_field":
            raw_platform_id = data.get("platform_id")
            platform_id = coerce_model_pk(raw_platform_id)
            if raw_platform_id is not None and platform_id is None:
                return JsonResponse({"error": "Invalid platform ID"}, status=400)
            if not save_interface_name_preference(request, value, platform_id):
                return JsonResponse({"error": "Invalid interface name field"}, status=400)
            return JsonResponse({"status": "ok"})

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
        from netbox_librenms_plugin.models import PlatformMapping

        # Rebind to the POSTed server, failing closed (blank/unknown/misconfigured) so a missing
        # or broken default can't 500 via the lazy librenms_api property.
        if err := _rebind_or_htmx_error(self, request):
            return err

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

        # Fetch [:2] once and reuse it for both the ambiguity check and the existing-mapping
        # resolution rather than a separate count() + first() (two queries for the same filter).
        # Mirrors AddDeviceTypeMappingView and the locked read below.
        upfront_rows = list(PlatformMapping.objects.filter(librenms_os__iexact=librenms_os)[:2])
        if len(upfront_rows) > 1:
            return _htmx_error_response(
                "Multiple mappings exist for this OS string. Remove duplicates before updating."
            )
        existing_mapping = upfront_rows[0] if upfront_rows else None
        self.required_object_permissions = {
            "POST": [
                ("view", Platform),
                ("change", PlatformMapping) if existing_mapping else ("add", PlatformMapping),
            ]
        }
        if error := self.require_object_permissions("POST"):
            return error
        if existing_mapping and not _mapping_change_is_allowed(self, PlatformMapping, existing_mapping.pk):
            return _htmx_error_response("Existing mapping is no longer available.")

        try:
            platform = self.restricted_queryset(Platform).get(pk=platform_id)
        except Platform.DoesNotExist:
            return _htmx_error_response("Selected platform not found.")

        try:
            with transaction.atomic():
                # Lock the row to close the TOCTOU window between the upfront
                # permission check and the actual write. select_for_update cannot
                # lock absent rows, so the create branch handles IntegrityError.
                # Materialise the locked rows in one query — count() would drop
                # the FOR UPDATE clause, leaving the rows unlocked.
                locked, lock_error = _lock_mapping_in_scope(
                    self,
                    PlatformMapping,
                    {"librenms_os__iexact": librenms_os},
                    "Multiple mappings exist for this OS string. Remove duplicates before updating.",
                )
                if lock_error is not None:
                    return lock_error
                if locked and not existing_mapping:
                    # Concurrent request created the mapping after our upfront read.
                    # Only escalate to change permission if we would actually mutate.
                    if locked.netbox_platform_id != platform_id:
                        self.required_object_permissions = {"POST": [("view", Platform), ("change", PlatformMapping)]}
                        if error := self.require_object_permissions("POST"):
                            return error
                if existing_mapping and not locked:
                    # Mapping was deleted between our upfront read and the lock.
                    # We are about to CREATE a new row, so require add permission.
                    self.required_object_permissions = {"POST": [("view", Platform), ("add", PlatformMapping)]}
                    if error := self.require_object_permissions("POST"):
                        return error
                if locked:
                    if locked.netbox_platform_id != platform_id:
                        if not _mapping_change_is_allowed(self, PlatformMapping, locked.pk):
                            return _htmx_error_response("Existing mapping is no longer available.")
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

        return HttpResponse(format_html("{}{}", oob_modal, row_html), content_type="text/html")
