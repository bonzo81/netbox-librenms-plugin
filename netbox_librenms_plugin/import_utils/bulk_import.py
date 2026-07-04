"""Bulk import orchestration for devices and filter processing."""

import hashlib
import logging
from typing import List

from django.core.cache import cache

from ..import_validation_helpers import (
    apply_cluster_to_validation,
    apply_role_to_validation,
    clear_match_derived_action_fields,
    recalculate_validation_status,
    remove_validation_issue,
)
from ..librenms_api import LibreNMSAPI
from ..utils import (
    AmbiguousLibreNMSIdError,
    coerce_librenms_id,
    find_by_librenms_id,
    normalize_serial,
    preload_normalization_rules,
)
from .cache import get_cache_metadata_key, get_import_device_cache_key, get_validated_device_cache_key
from .collisions import detect_bulk_collisions
from .device_operations import (
    VALIDATION_ERROR_ISSUE_PREFIX,
    _describe_existing_librenms_link,
    import_single_device,
    resolve_device_by_host_ip,
    validate_device_for_import,
)
from .filters import _safe_disabled, get_librenms_devices_for_import
from .permissions import check_user_permissions, require_permissions
from .virtual_chassis import (
    create_virtual_chassis_with_members,
    empty_virtual_chassis_data,
    prefetch_vc_data_for_devices,
)

logger = logging.getLogger(__name__)

# Stable fragment of the ambiguous-librenms_id blocker message. Shared by the writer
# (the AmbiguousLibreNMSIdError handler) and the cleaner (the pre-lookup reset in
# _refresh_existing_device) so a resolved duplicate's stale message is reliably removed
# regardless of which librenms_id value was interpolated into it.
_AMBIGUOUS_LIBRENMS_ID_MARKER = "matches more than one existing NetBox record"
# Substrings of the ambiguity blockers that carry the "ambiguous_hostname_or_serial" match type,
# used to strip a stale instance once the duplicate is resolved (mirrors the librenms_id marker
# above). The blocker can be appended by either the refresh serial/IP fallback below ("serial or
# management IP") or validate_device_for_import's duplicate name/serial guard ("hostname/serial"),
# so the cleanup must recognise either wording — otherwise a hostname/serial blocker survives the
# match_type reset and keeps the row blocked until cache expiry.
_AMBIGUOUS_SERIAL_IP_MARKERS = (
    "serial or management IP",
    "hostname/serial",
)
# Stable fragment of the cross-model (VM + Device share the name) warning. Shared by the
# writer (the both-models name branch in _refresh_existing_device, wording-matched to
# validate_device_for_import's hostname branch) and the pre-lookup cleaner, so a refresh
# neither stacks a duplicate copy on a persisting collision nor leaves a stale warning
# behind once the collision is resolved.
_CROSS_MODEL_HOSTNAME_MARKER = "Both a VM and Device exist with hostname"


def _is_job_cancelled(job) -> bool:
    """
    Return True if a background job has been stopped or cancelled.

    Checks RQ/Redis state only (reflects stop API calls immediately).
    On Redis connectivity issues or a missing RQ job, returns False to avoid
    false cancellation. Unexpected exceptions are logged and also return False.
    """
    from django_rq import get_queue
    from redis.exceptions import RedisError
    from rq.exceptions import NoSuchJobError
    from rq.job import Job as RQJob

    try:
        queue = get_queue("default")
        rq_job = RQJob.fetch(str(job.job.job_id), connection=queue.connection)
        return rq_job.is_failed or rq_job.is_stopped
    except (RedisError, NoSuchJobError):
        return False
    except Exception:
        logger.warning("Unexpected error checking RQ job cancellation state", exc_info=True)
        return False


def detect_collisions_for_device_ids(
    device_ids, api, libre_devices_cache=None, sync_options=None
) -> tuple[list[dict], list]:
    """
    Detect same-NetBox-device collisions for a batch of LibreNMS device ids.

    Validates each device just enough to read the collision-relevant match fields
    (``include_vc_detection=False`` — DB-only, no VC API call), then groups rows that
    target the same NetBox device. Reuses ``libre_devices_cache`` so it adds no LibreNMS
    API calls when the caller already pre-fetched device data.

    This enforces the bulk-confirm collision block on the import paths that do NOT pass
    through the confirm modal — the direct ``BulkImportDevicesView`` POST and the
    background ``ImportDevicesJob`` — so a colliding batch can't be imported by bypassing
    the preview.

    Args:
        device_ids: LibreNMS device ids about to be imported.
        api: A LibreNMS API client (only ``server_key`` and ``get_device_info`` are used;
            ``get_device_info`` is called only for ids missing from the cache).
        libre_devices_cache: Optional ``{device_id: libre_device}`` pre-fetched data.
        sync_options: Optional sync options (``use_sysname`` / ``strip_domain``).

    Returns:
        tuple[list[dict], list]: ``(collisions, unresolved_ids)`` — the collision groups from
            :func:`detect_bulk_collisions` (empty when the batch is clean), and the device ids
            that could NOT be validated (``get_device_info`` failed and they weren't in the
            cache). Those ids were not collision-checked, so the caller must fail closed on them
            rather than import them unchecked — a transient miss could otherwise slip a colliding
            row through on a retry.
    """
    use_sysname = (sync_options or {}).get("use_sysname", True)
    strip_domain = (sync_options or {}).get("strip_domain", False)
    cache = libre_devices_cache or {}
    devices = []
    unresolved_ids = []
    for device_id in device_ids:
        libre_device = cache.get(device_id)
        if libre_device is None:
            success, libre_device = api.get_device_info(device_id)
            if not success or not libre_device:
                # Couldn't fetch device info → can't collision-check this id. Record it so the
                # caller blocks it instead of importing it unchecked (fail closed).
                unresolved_ids.append(device_id)
                continue
        validation = validate_device_for_import(
            # DB-only collision pre-check: don't hand the validator an API client (it would let
            # API-backed validation paths run even with the cache supplied + include_vc_detection
            # False). The collision-relevant match fields are resolved from the DB; api is only
            # used for VC/chassis enrichment, all of which is guarded on `api` being truthy.
            libre_device,
            api=None,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
            server_key=api.server_key,
            include_vc_detection=False,
        )
        if any(str(issue).startswith(VALIDATION_ERROR_ISSUE_PREFIX) for issue in validation.get("issues", [])):
            # validate_device_for_import() caught an exception and returned only a partial result
            # (its except branch appends a VALIDATION_ERROR_ISSUE_PREFIX issue). The collision-relevant
            # match fields may be missing, so this id was NOT reliably collision-checked. Fail
            # closed: record it as unresolved rather than collision-check it on incomplete data
            # (mirrors the get_device_info miss above). The prefix is a shared constant so this
            # guard can't silently drift from the producer's wording.
            unresolved_ids.append(device_id)
            continue
        devices.append(
            {
                "device_id": device_id,
                "device_name": validation.get("resolved_name") or f"device-{device_id}",
                "validation": validation,
            }
        )
    return detect_bulk_collisions(devices), unresolved_ids


def bulk_import_devices_shared(
    device_ids: List[int],
    server_key: str = None,
    sync_options: dict = None,
    manual_mappings_per_device: dict = None,
    libre_devices_cache: dict = None,
    job=None,
    user=None,
) -> dict:
    """
    Shared function for importing multiple LibreNMS devices to NetBox.

    Used by both synchronous imports and background jobs. Handles per-device error
    collection and optional progress logging when job context is provided.

    Args:
        device_ids: List of LibreNMS device IDs to import
        server_key: LibreNMS server configuration key
        sync_options: Sync options to apply to all devices
        manual_mappings_per_device: Dict mapping device_id to manual_mappings dict
            Example: {1179: {'device_role_id': 5}, 1180: {'device_role_id': 3}}
        libre_devices_cache: Optional dict mapping device_id to pre-fetched device data
            to avoid redundant API calls. Example: {123: {...device_data...}}
        job: Optional JobRunner instance for progress logging and cancellation checks
        user: User performing the import (for permission checks). If job is provided,
            user is extracted from job.job.user if not explicitly passed.

    Returns:
        dict: Bulk import result with structure:
            {
                'total': int,
                'success': List[dict],  # Successfully imported devices
                'failed': List[dict],   # Failed imports with errors
                'skipped': List[dict],  # Skipped devices (already exist, etc.)
                'virtual_chassis_created': int  # Number of VCs created
            }

    Raises:
        PermissionDenied: If user lacks required permissions

    Example:
        >>> # Synchronous usage
        >>> result = bulk_import_devices_shared([1, 2, 3, 4, 5], user=request.user)
        >>> # Background job usage
        >>> result = bulk_import_devices_shared([1, 2, 3], job=self)
    """
    # Extract user from job if not explicitly provided
    if user is None and job is not None:
        user = getattr(job.job, "user", None)

    # Check permissions at start of bulk operation — device and VM add perms are
    # required because any device may be flagged as import_as_vm during validation.
    # change_device is needed for VC master/member updates.
    required_perms = [
        "dcim.add_device",
        "dcim.change_device",
        "virtualization.add_virtualmachine",
    ]
    require_permissions(user, required_perms, "import devices")

    total = len(device_ids)
    success_list = []
    failed_list = []
    skipped_list = []
    vc_created_count = 0
    processed_vc_domains = set()  # Track VCs already created by domain
    _cancelled = False

    # Initialize API client once for all devices to avoid repeated config parsing
    api = LibreNMSAPI(server_key=server_key)

    # Preload the device_type NormalizationRule set once so the per-device hardware→device-type
    # match doesn't re-query it for every device in the loop (issue #90 / N+1 avoidance, #92).
    device_type_norm_rules = preload_normalization_rules("device_type")

    for idx, device_id in enumerate(device_ids, start=1):
        # Check for job cancellation on first iteration and every 5th thereafter.
        if job and (idx == 1 or idx % 5 == 0) and _is_job_cancelled(job):
            if job.logger:
                job.logger.warning(f"Import job stopped at device {idx} of {total}")
            else:
                logger.warning(f"Import cancelled at device {idx} of {total}")
            _cancelled = True
            break

        try:
            # Use cached device data if available to avoid redundant API calls
            if libre_devices_cache and device_id in libre_devices_cache:
                libre_device = libre_devices_cache[device_id]
                success = True
            else:
                # Import decisions (DeviceType match, serial-conflict, hostname) must run against
                # live LibreNMS data: bypass the short device-info read cache so a value the user
                # just corrected in LibreNMS isn't read back stale within the cache window.
                success, libre_device = api.get_device_info(device_id, use_cache=False)
                # Backfill the shared cache so the synchronous import's post-import row re-render
                # (which reads the same dict via fetch_device_with_cache) doesn't issue a second
                # LibreNMS round-trip per device on a cold cache. The background path passes a
                # serialized copy and skips that re-render, so this is a harmless no-op there.
                if success and libre_device is not None and libre_devices_cache is not None:
                    libre_devices_cache[device_id] = libre_device

            if not success or not libre_device:
                error_msg = f"Failed to retrieve device {device_id} from LibreNMS"
                failed_list.append({"device_id": device_id, "error": error_msg})
                if job and job.logger:
                    job.logger.error(error_msg)
                else:
                    logger.error(error_msg)
                continue

            use_sysname_opt = sync_options.get("use_sysname", True) if sync_options else True
            strip_domain_opt = sync_options.get("strip_domain", False) if sync_options else False
            validation = validate_device_for_import(
                libre_device,
                api=api,
                use_sysname=use_sysname_opt,
                strip_domain=strip_domain_opt,
                server_key=api.server_key,
                # Import-time behavior: always evaluate VC state from live/cached
                # LibreNMS inventory so stack members are created even when preview
                # flags are stale or omitted.
                include_vc_detection=True,
                preloaded_device_type_rules=device_type_norm_rules,
            )

            vc_data = validation.get("virtual_chassis", {})
            if vc_data.get("is_stack", False):
                has_vc_perm, _ = check_user_permissions(user, ["dcim.add_virtualchassis"])
                if not has_vc_perm:
                    error_msg = f"Cannot import stack device {device_id}: missing permission dcim.add_virtualchassis"
                    failed_list.append({"device_id": device_id, "error": error_msg})
                    if job and job.logger:
                        job.logger.error(error_msg)
                    else:
                        logger.error(error_msg)
                    continue

            # Build manual mappings from validation + any provided overrides
            device_mappings = {}

            # Get site and device_type from validation
            if validation["site"].get("found") and validation["site"].get("site"):
                device_mappings["site_id"] = validation["site"]["site"].id
            if validation["device_type"].get("found") and validation["device_type"].get("device_type"):
                device_mappings["device_type_id"] = validation["device_type"]["device_type"].id
            if validation["platform"].get("found") and validation["platform"].get("platform"):
                device_mappings["platform_id"] = validation["platform"]["platform"].id

            # Override with any manual mappings provided for this device
            if manual_mappings_per_device and device_id in manual_mappings_per_device:
                device_mappings.update(manual_mappings_per_device[device_id])

            result = import_single_device(
                device_id,
                server_key=api.server_key,  # use resolved key, not raw parameter (may be None)
                validation=validation,
                sync_options=sync_options,
                manual_mappings=device_mappings if device_mappings else None,
                libre_device=libre_device,
            )

            if result["success"]:
                success_list.append(
                    {
                        "device_id": device_id,
                        "device": result["device"],
                        "message": result["message"],
                    }
                )
                # Log progress after each successful import
                if job and job.logger:
                    job.logger.info(f"Imported device {idx} of {total}")

                # Handle virtual chassis creation for stacks
                if vc_data.get("is_stack", False):
                    # Derive a stack-level dedup key from member serials so that all
                    # LibreNMS devices belonging to the same physical stack (e.g. each
                    # switch in a stacked chassis that appears as a separate device in
                    # LibreNMS) share the same key and VC creation is triggered only once.
                    # Fall back to device_id when no member serials are available.
                    member_serials = sorted(
                        serial
                        for m in vc_data.get("members", [])
                        if (serial := normalize_serial(m.get("serial"))) and serial != "-"
                    )
                    if member_serials:
                        vc_domain = f"librenms-stack-{','.join(member_serials)}"
                    else:
                        # No serials available — build a stable fingerprint from member name/model/position
                        # so all LibreNMS devices in the same physical stack share the same dedup key.
                        member_parts = sorted(
                            f"{m.get('name', '')}/{m.get('model', '')}:{m.get('position', 0)}"
                            for m in vc_data.get("members", [])
                        )
                        if member_parts:
                            fingerprint = hashlib.sha256(",".join(member_parts).encode()).hexdigest()[:12]
                            vc_domain = f"librenms-stack-{fingerprint}"
                        else:
                            vc_domain = f"librenms-{device_id}"

                    # Only create VC if we haven't processed this stack yet.
                    # Permission was already validated before device import.
                    if vc_domain not in processed_vc_domains:
                        # Add to set BEFORE attempting creation to prevent race condition
                        processed_vc_domains.add(vc_domain)
                        try:
                            vc = create_virtual_chassis_with_members(
                                result["device"],
                                vc_data["members"],
                                libre_device,
                                server_key=api.server_key,
                            )
                            vc_created_count += 1
                            log_msg = f"Created VC '{vc.name}' during bulk import for device {device_id}"
                            if job and job.logger:
                                job.logger.info(log_msg)
                            else:
                                logger.info(log_msg)
                        except Exception as vc_error:
                            # Remove from set on failure so retry is possible
                            processed_vc_domains.discard(vc_domain)
                            warn_msg = f"Failed to create VC for device {device_id}: {vc_error}"
                            if job and job.logger:
                                job.logger.warning(warn_msg)
                            else:
                                logger.warning(warn_msg)
                            # Don't fail the import, just log the warning

            elif result.get("device"):  # Device exists
                skipped_list.append({"device_id": device_id, "reason": result["error"]})
            else:  # Failed to import
                failed_list.append({"device_id": device_id, "error": result["error"]})
                if job and job.logger:
                    job.logger.error(f"Failed to import device {device_id}: {result['error']}")

        except Exception as e:
            error_msg = f"Unexpected error importing device {device_id}: {str(e)}"
            if job and job.logger:
                job.logger.error(error_msg, exc_info=True)
            else:
                logger.exception(f"Unexpected error importing device {device_id}")
            failed_list.append({"device_id": device_id, "error": str(e)})

    return {
        "total": total,
        "success": success_list,
        "failed": failed_list,
        "skipped": skipped_list,
        "virtual_chassis_created": vc_created_count,
        "cancelled": _cancelled,
    }


def bulk_import_devices(
    device_ids: List[int],
    server_key: str = None,
    sync_options: dict = None,
    manual_mappings_per_device: dict = None,
    libre_devices_cache: dict = None,
    user=None,
) -> dict:
    """
    Import multiple LibreNMS devices to NetBox (synchronous).

    This is the public API for synchronous imports. For background job usage,
    use bulk_import_devices_shared() with a job context.

    Args:
        device_ids: List of LibreNMS device IDs to import
        server_key: LibreNMS server configuration key
        sync_options: Sync options to apply to all devices
        manual_mappings_per_device: Dict mapping device_id to manual_mappings dict
            Example: {1179: {'device_role_id': 5}, 1180: {'device_role_id': 3}}
        libre_devices_cache: Optional dict mapping device_id to pre-fetched device data
            to avoid redundant API calls. Example: {123: {...device_data...}}
        user: User performing the import (for permission checks)

    Returns:
        dict: Bulk import result with structure:
            {
                'total': int,
                'success': List[dict],  # Successfully imported devices
                'failed': List[dict],   # Failed imports with errors
                'skipped': List[dict],  # Skipped devices (already exist, etc.)
                'virtual_chassis_created': int  # Number of VCs created
            }

    Raises:
        PermissionDenied: If user lacks required permissions
    """
    return bulk_import_devices_shared(
        device_ids=device_ids,
        server_key=server_key,
        sync_options=sync_options,
        manual_mappings_per_device=manual_mappings_per_device,
        libre_devices_cache=libre_devices_cache,
        job=None,  # No job context for synchronous imports
        user=user,
    )


def _refresh_librenms_linkage(validation: dict, device, libre_device: dict, server_key: str) -> None:
    """
    Re-derive the LibreNMS-id linkage fields for a refreshed device.

    Cheap and DB-only (reads the device's ``librenms_id`` custom field) — no
    LibreNMS API call — so a cached import row picks up OOB-link / host-link
    changes made in NetBox since the row was cached. Without this, the cache-hit
    path keeps the stale ``existing_match_type``/badge (e.g. an OOB controller
    linked after caching still rendered as a conflict until the cache expired).

    Mirrors ``validate_device_for_import``'s linkage logic: always refreshes
    ``existing_librenms_link``, and when the device is matched to the scanned
    LibreNMS id it classifies the match as ``librenms_oob`` (matched via the OOB
    sub-key) or ``librenms_id`` (matched as the host).

    Args:
        validation (dict): The import-row validation dict, mutated in place.
        device: The refreshed NetBox device (or VM) to re-derive linkage from.
        libre_device (dict): The scanned LibreNMS device record (may be empty).
        server_key (str): The LibreNMS server key the row was scanned against.

    Returns:
        None
    """
    link = _describe_existing_librenms_link(device, server_key)
    validation["existing_librenms_link"] = link

    scanned_id = coerce_librenms_id((libre_device or {}).get("device_id"))
    # _describe_existing_librenms_link already read the OOB sub-object via get_librenms_oob and
    # exposes the coerced positive-int id as link["oob_id"]; reuse it instead of re-reading.
    oob_id = link["oob_id"]

    # Promote to a *current* librenms-id / OOB link first, regardless of the cached match type.
    # A row cached as a serial/hostname conflict may since have gained the matching host/OOB
    # librenms_id in NetBox; it should render as the link, not the stale conflict, instead of
    # waiting for the cache to expire.
    if scanned_id is not None and oob_id is not None and oob_id == scanned_id:
        validation["existing_match_type"] = "librenms_oob"
        return
    if scanned_id is not None and link["host_id"] is not None and link["host_id"] == scanned_id:
        validation["existing_match_type"] = "librenms_id"
        return

    # No current id/OOB link matches the scanned device. Only clear a *prior* librenms-id/OOB
    # match here — leave serial/hostname/primary_ip match types untouched (evaluated elsewhere).
    if validation.get("existing_match_type") in ("librenms_id", "librenms_oob"):
        if scanned_id is None:
            # A missing scanned id (libre_device omitted/malformed) is NOT proof the link
            # disappeared — only drop the cached match when the DB linkage itself is gone.
            if link["host_id"] is None and link["oob_id"] is None:
                validation["existing_match_type"] = None
            return
        # scanned id present but neither the host id nor the OOB id matches it anymore.
        validation["existing_match_type"] = None


def _clear_existing_match_derived_fields(validation: dict) -> None:
    """
    Reset the fields produced from an existing match.

    Clears stale serial/OOB/merge/promote actions so they don't linger after that
    match is dropped (device deleted, or librenms/OOB link removed since caching).
    The subsequent fresh lookup re-populates them if it re-matches.

    Args:
        validation (dict): The import-row validation dict, mutated in place.

    Returns:
        None
    """
    clear_match_derived_action_fields(validation)
    # Migration / device-type state is also derived from the (now dropped) match;
    # leaving it set would render a migrate/name-sync action for the old object. The fresh
    # lookup below re-derives these only on a re-match, so reset them here (they are
    # refresh-specific, so they stay out of the shared helper).
    validation["librenms_id_needs_migration"] = False
    validation["device_type_mismatch"] = False


def _reset_device_role(validation: dict) -> None:
    """
    Reset the row's role selection to "not found", preserving available_roles.

    One shape shared by every refresh branch that drops a device match (deleted device,
    vanished link, late cross-model rebind), so the copies can't drift on a future edit.

    Args:
        validation (dict): The import-row validation dict, mutated in place.

    Returns:
        None
    """
    validation["device_role"] = {
        "found": False,
        "role": None,
        "available_roles": validation.get("device_role", {}).get("available_roles", []),
    }


def _reset_cluster(validation: dict) -> None:
    """
    Reset the row's cluster selection to "not found", preserving available_clusters.

    The VM twin of :func:`_reset_device_role` — VM rows are gated on cluster, not role.

    Args:
        validation (dict): The import-row validation dict, mutated in place.

    Returns:
        None
    """
    validation["cluster"] = {
        "found": False,
        "cluster": None,
        "available_clusters": validation.get("cluster", {}).get("available_clusters", []),
    }


def _reassert_new_import_blockers(validation: dict) -> None:
    """
    Re-add the create-time role/cluster blocker for unmatched rows.

    ``validate_device_for_import()`` attaches this blocker to unmatched rows. When a
    refresh drops a cached match (or never had one) and the fresh lookup finds
    nothing, the row is back in the "new import" path.
    ``recalculate_validation_status()`` recomputes can_import purely from the issues
    list, so without re-adding this blocker a row that still has no role/cluster
    selected could flip back to importable and then fail at import time.

    Guarded by the selection state (found/role/cluster), so a row where the user
    *has* picked a role/cluster — which sets found=True and removed the issue — is
    left importable.

    Args:
        validation (dict): The import-row validation dict, mutated in place.

    Returns:
        None
    """
    if validation.get("import_as_vm"):
        cluster = validation.get("cluster") or {}
        if not cluster.get("found") and not cluster.get("cluster"):
            msg = "Cluster must be manually selected before importing as VM"
            if msg not in validation.setdefault("issues", []):
                validation["issues"].append(msg)
    else:
        role = validation.get("device_role") or {}
        if not role.get("found") and not role.get("role"):
            msg = "Device role must be manually selected before import"
            if msg not in validation.setdefault("issues", []):
                validation["issues"].append(msg)


def _refresh_existing_device(validation: dict, libre_device: dict = None, server_key: str = "default") -> None:
    """
    Refresh existing_device from DB to pick up changes made in NetBox since caching.

    When existing_device is None (wasn't found at cache time), re-check if the device
    was imported since caching by looking up librenms_id or hostname.
    """
    existing = validation.get("existing_device")
    if existing and hasattr(existing, "pk"):
        try:
            from dcim.models import Device
            from virtualization.models import VirtualMachine

            if validation.get("import_as_vm"):
                refreshed = VirtualMachine.objects.filter(pk=existing.pk).first()
            else:
                refreshed = Device.objects.filter(pk=existing.pk).first()

            if refreshed:
                validation["existing_device"] = refreshed
                # Re-derive linkage so an OOB-link/host-link change since caching
                # is reflected in the badge (DB-only; no LibreNMS API call).
                prior_match = validation.get("existing_match_type")
                _refresh_librenms_linkage(validation, refreshed, libre_device, server_key)
                if prior_match not in ("librenms_id", "librenms_oob") and validation.get("existing_match_type") in (
                    "librenms_id",
                    "librenms_oob",
                ):
                    # _refresh_librenms_linkage just PROMOTED a row cached under a non-link match
                    # (serial/hostname/primary_ip) to a current librenms_id/OOB link. The
                    # serial/merge/oob/promote actions derived for that old match are now stale and
                    # would render a destructive "Merge two NetBox devices" form (the template
                    # checks serial_action == "merge_netbox_devices" before the match_type chain)
                    # or a misleading "Add as OOB" badge (device_status keys that on serial_action,
                    # independent of match_type) on a row that is actually host/OOB-linked. Drop
                    # them; a row already cached as a link type keeps its link-derived fields.
                    _clear_existing_match_derived_fields(validation)
                if prior_match in ("librenms_id", "librenms_oob") and validation.get("existing_match_type") is None:
                    # The librenms-id/OOB link that made this the cached match is gone
                    # (removed/repointed in NetBox since caching). Treat it like a vanished
                    # match — clear it and recompute readiness, then fall through to the fresh
                    # lookup below so the row is re-evaluated under current rules (it may now
                    # match by hostname/serial/IP, or become importable as new) instead of
                    # staying blocked until cache expiry. Mirrors the deleted-device branch.
                    validation["existing_device"] = None
                    validation["existing_librenms_link"] = None
                    _clear_existing_match_derived_fields(validation)
                    if not validation.get("import_as_vm"):
                        _reset_device_role(validation)
                    else:
                        # VM rows are gated on cluster, not role: a dropped match must also clear
                        # the stale cluster selection (preserving available_clusters), or
                        # _reassert_new_import_blockers() sees found/cluster still set and lets
                        # the row re-enter the new-import path without a fresh cluster choice.
                        _reset_cluster(validation)
                    # Fail-closed: this branch drops the vanished-link match and recomputes
                    # readiness, then falls through to the fresh lookup that would normally re-add
                    # the create-time role/cluster blocker. But the fresh lookup early-returns when
                    # libre_device is None (and its broad except can swallow), so re-assert here too
                    # — otherwise the row can stay importable with no role/cluster selected.
                    _reassert_new_import_blockers(validation)
                    recalculate_validation_status(validation, is_vm=bool(validation.get("import_as_vm")))
                else:
                    if hasattr(refreshed, "role") and refreshed.role:
                        apply_role_to_validation(validation, refreshed.role, is_vm=bool(validation.get("import_as_vm")))
                    elif not validation.get("import_as_vm"):
                        _reset_device_role(validation)
                        remove_validation_issue(validation, "role")
                    recalculate_validation_status(validation, is_vm=bool(validation.get("import_as_vm")))
                    # Re-assert non-importable state: recalculate bases can_import on
                    # issues alone, but an existing matched device must never be import-ready.
                    validation["can_import"] = False
                    validation["is_ready"] = False
                    return
            else:
                # Device was deleted since caching — recompute readiness to match
                # validate_device_for_import logic.
                validation["existing_device"] = None
                validation["existing_match_type"] = None
                # Nothing is linked anymore — clear the linkage so the row can't
                # keep rendering a stale host/OOB badge.
                validation["existing_librenms_link"] = None
                # Drop serial/OOB/merge/promote actions that pointed at the deleted device.
                _clear_existing_match_derived_fields(validation)
                # Clear stale device_role so is_ready is computed from scratch.
                # Guard: VMs don't use device_role for readiness, so preserve any
                # user-selected role rather than silently dropping it.
                if not validation.get("import_as_vm"):
                    _reset_device_role(validation)
                else:
                    # Mirror the stale-match branch: a deleted cached VM match must drop the
                    # stale cluster selection (keeping available_clusters) so the row returns to
                    # the same create-time state as a brand-new VM import row.
                    _reset_cluster(validation)
                # Same fail-closed reasoning as the vanished-link branch above: re-assert the
                # create-time blocker before recompute so a deleted-match row can't stay importable
                # if the fresh lookup early-returns (libre_device None) or its except swallows.
                _reassert_new_import_blockers(validation)
                recalculate_validation_status(validation, is_vm=bool(validation.get("import_as_vm")))
        except Exception as e:
            existing_id = getattr(existing, "pk", "unknown") if existing else "none"
            logger.error(f"Failed to refresh existing device (pk={existing_id}): {e}")
            return

    # Re-evaluate the match under current DB state. Reached when existing_device was None at
    # cache time, or when a cached librenms_id/OOB link disappeared (cleared above) or its
    # device was deleted — in every case re-check whether a matching NetBox object exists now,
    # using the full id/name/serial/IP breadth so the row can't flip to importable and create
    # a duplicate of a device that still exists under a different identity.
    if not libre_device:
        return
    try:
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        import_as_vm = validation.get("import_as_vm", False)
        Model = VirtualMachine if import_as_vm else Device
        # Also check the opposite model — the LibreNMS object may have been
        # imported as a VM even though import_as_vm=False (or vice versa).
        CrossModel = Device if import_as_vm else VirtualMachine

        # Coerce up front so malformed values (e.g. "42.0", floats, booleans) are rejected
        # rather than truncated by int() and matched to the wrong record.
        librenms_id = coerce_librenms_id(libre_device.get("device_id"))
        hostname = libre_device.get("hostname", "")
        sys_name = libre_device.get("sysName", "")

        # Clear any stale ambiguous-librenms_id blocker set by a prior refresh before
        # re-running the lookup. If the duplicate still exists, _lookup_in_model() below
        # re-raises AmbiguousLibreNMSIdError and the except handler re-adds the blocker;
        # if it was resolved since, the row must not stay blocked until cache expiry.
        if validation.get("ambiguous_librenms_id"):
            validation["ambiguous_librenms_id"] = False
            if validation.get("existing_match_type") == "ambiguous_librenms_id":
                validation["existing_match_type"] = None
            for _key in ("issues", "warnings"):
                msgs = validation.get(_key)
                if isinstance(msgs, list):
                    validation[_key] = [
                        m for m in msgs if not (isinstance(m, str) and _AMBIGUOUS_LIBRENMS_ID_MARKER in m)
                    ]

        # Same for a stale serial/IP ambiguity blocker: if the duplicate was resolved since caching,
        # the fresh fallback below would not re-flag it, but the cached issue/match_type would keep
        # the row blocked until cache expiry. Clear it so the row is re-evaluated under current rules.
        if validation.get("existing_match_type") == "ambiguous_hostname_or_serial":
            validation["existing_match_type"] = None
            for _key in ("issues", "warnings"):
                msgs = validation.get(_key)
                if isinstance(msgs, list):
                    validation[_key] = [
                        m
                        for m in msgs
                        if not (isinstance(m, str) and any(marker in m for marker in _AMBIGUOUS_SERIAL_IP_MARKERS))
                    ]

        # Same for the cross-model (VM + Device) name warning: the both-models branch below
        # re-adds it while the collision persists, so stripping it here both prevents a
        # duplicate copy per refresh and drops the stale warning once the collision is
        # resolved. Unconditional (no match_type gate) — this warning never binds a match.
        msgs = validation.get("warnings")
        if isinstance(msgs, list):
            validation["warnings"] = [m for m in msgs if not (isinstance(m, str) and _CROSS_MODEL_HOSTNAME_MARKER in m)]

        new_device = None
        match_type = None
        found_as_cross_model = False

        def _lookup_value(m, value):
            """
            Return (device, ambiguous) for the single NAME *value* in model m.

            NetBox device names are unique only per-site, so a name can resolve to MORE THAN ONE
            device. Fail closed exactly like the serial/IP fallback below (and the full
            validate_device_for_import() path): when a value matches >1 object, return
            ``(None, True)`` so the caller blocks the row instead of binding ``.first()`` to an
            arbitrary one.
            """
            matches = list(m.objects.filter(name__iexact=value)[:2])
            if len(matches) > 1:
                return None, True
            return (matches[0] if matches else None), False

        # Fail closed on a CROSS-MODEL librenms_id collision before selecting a match — i.e.
        # the same (server_key, librenms_id) bound to BOTH a Device and a VirtualMachine. A
        # LibreNMS device_id is unique within a server, so this never happens in a clean state;
        # it's a NetBox-side data-integrity hazard (custom fields have no cross-model uniqueness)
        # from a stale/duplicate binding — e.g. a thing imported as a VM then re-imported as a
        # Device without clearing the old VM link, or a manual CF edit. validate_device_for_import()
        # already detects and blocks exactly this (see device_operations.py "Cross-model collision"),
        # but _lookup_in_model(Model) here returns on the first preferred-model id hit and never
        # consults CrossModel — so without this guard the refresh re-check would silently bind to
        # one model and disagree with the validation path that originally blocked the row. Check
        # both models and raise the existing ambiguous-id blocker when both resolve (single-model
        # duplicates are already raised inside find_by_librenms_id).
        if librenms_id is not None:
            model_id_match = find_by_librenms_id(Model, librenms_id, server_key)
            cross_id_match = find_by_librenms_id(CrossModel, librenms_id, server_key)
            if model_id_match and cross_id_match:
                raise AmbiguousLibreNMSIdError(
                    f"LibreNMS ID {librenms_id} matches both {Model.__name__} and {CrossModel.__name__}"
                )
            # An exact librenms_id owner must win over any name/hostname fallback: if the id now
            # belongs to the opposite model only, binding by name to a same-named preferred-model
            # object would silently re-home the row to the wrong device. Prefer the id match here,
            # before _lookup_in_model(Model) can return a name hit.
            if model_id_match:
                new_device, match_type = model_id_match, "librenms_id"
            elif cross_id_match:
                new_device, match_type = cross_id_match, "librenms_id"
                found_as_cross_model = True

        name_ambiguous = False
        if not new_device:
            # Compare BOTH models against the SAME name candidate before advancing to the next
            # fallback. Two INDEPENDENT per-model searches (the old _lookup_in_model, which returned
            # on the first hit within each model) could match a Device by one candidate (e.g.
            # resolved_name) and an UNRELATED VM by a *different* one (e.g. raw hostname), then
            # treat that as a cross-model collision — leaving the real preferred-name match unbound
            # and letting a duplicate import slip through. Iterate the candidates in priority order
            # and, per value, query both models: a same-value hit in BOTH is the genuine cross-model
            # ambiguity (warn + leave unmatched, exactly like validate_device_for_import()'s
            # hostname path); a single-model hit binds and wins over any lower-priority candidate.
            # The librenms_id match above already short-circuited, so this only does the
            # name/hostname/sysName fallbacks.
            for value, mt in (
                (validation.get("resolved_name"), "resolved_name"),
                (hostname, "hostname"),
                (sys_name, "sysname"),
            ):
                if not value:
                    continue
                model_match, model_amb = _lookup_value(Model, value)
                cross_match, cross_amb = _lookup_value(CrossModel, value)
                if model_amb or cross_amb:
                    # >1 match within a single model for this value — terminal ambiguity, fail
                    # closed below.
                    name_ambiguous = True
                    break
                if model_match and cross_match:
                    # Same value resolves in BOTH models: warn and leave unmatched (do NOT block),
                    # exactly like the validator's cross-model hostname branch. A serial/IP match
                    # can still bind below (a stronger identity), mirroring the validator's
                    # fall-through. setdefault (not a plain get + isinstance guard) so the warning
                    # is surfaced even when the caller built a minimal validation dict without
                    # "warnings", matching the AmbiguousLibreNMSIdError handler below.
                    validation.setdefault("warnings", []).append(
                        f"Both a VM and Device exist with hostname '{value}' in NetBox. Cannot "
                        "determine which to match. Please set the librenms_id custom field on the "
                        "correct object."
                    )
                    break
                if model_match:
                    new_device, match_type = model_match, mt
                    break
                if cross_match:
                    # Cross-model import that happened after the cache was built (e.g. a LibreNMS
                    # device imported as a VM): the preferred model has no match on this value, the
                    # opposite one does.
                    new_device, match_type = cross_match, mt
                    found_as_cross_model = True
                    break

        if not new_device and name_ambiguous:
            # A hostname/sysName resolved to MORE THAN ONE NetBox device (names are unique only
            # per-site). Binding to whichever sorts first would render the wrong device as the
            # existing match, so fail closed exactly like the serial/IP fallback below and the
            # full validate_device_for_import() path — block instead of picking arbitrarily. The
            # "hostname/serial" marker keeps this in lock-step with the stale-blocker cleanup above.
            msgs = validation.get("issues")
            if isinstance(msgs, list):
                msgs.append(
                    "Multiple NetBox devices share this device's hostname/serial; resolve the "
                    "duplicate before importing."
                )
            validation["existing_match_type"] = "ambiguous_hostname_or_serial"
            validation["can_import"] = False
            validation["is_ready"] = False
            # Terminal: this row is blocked pending duplicate resolution. Return before the
            # no-match `else` below re-adds create-time role/cluster blockers via
            # _reassert_new_import_blockers — the cached row must show ONLY the
            # duplicate-resolution blocker, not stale new-import ones.
            return

        if not new_device and not name_ambiguous and not import_as_vm:
            # Serial- and IP-based matches: validate_device_for_import() catches these, so the
            # refresh re-check must have the same breadth. Without them a row whose
            # librenms_id/name link disappeared (or that never matched) can flip to importable
            # and re-import a device that already exists in NetBox under a different name —
            # matched only by hardware serial or management IP. Device-only (VMs have no serial
            # or primary-IP identity here). The richer serial_action/OOB-candidate heuristics
            # stay in the full validation path; here the contract is simply: block the import.
            from dcim.models import Device as _Device

            # This fallback fails closed on ambiguity exactly like validate_device_for_import():
            # if the serial OR the management IP resolves to more than one distinct NetBox device,
            # binding to whichever row sorts first would render the wrong device as the existing
            # match, so flag the row ambiguous and block instead of picking arbitrarily.
            ambiguous_fallback = False
            serial = normalize_serial(libre_device.get("serial"))
            if serial and serial != "-":
                serial_matches = list(_Device.objects.filter(serial=serial)[:2])
                if len(serial_matches) > 1:
                    ambiguous_fallback = True
                elif serial_matches:
                    new_device, match_type = serial_matches[0], "serial"

            if not new_device and not ambiguous_fallback:
                primary_ip = libre_device.get("ip")
                if primary_ip:
                    # Shared resolver (scans interface-assignment + oob_ip-FK across all duplicate
                    # net_host rows, fails closed on >1 distinct device) — same helper
                    # validate_device_for_import() uses, so the two paths can't drift.
                    device, ip_ambiguous, _matching_ips = resolve_device_by_host_ip(primary_ip)
                    if ip_ambiguous:
                        ambiguous_fallback = True
                    elif device:
                        new_device, match_type = device, "primary_ip"

            if ambiguous_fallback:
                # Block without binding to an arbitrary device: append a blocking issue (the
                # new_device=None `else` branch below recomputes can_import from the issues list)
                # and mark the row ambiguous so the UI doesn't render a wrong existing match.
                msgs = validation.get("issues")
                if isinstance(msgs, list):
                    msgs.append(
                        "Multiple NetBox devices match this device's serial or management IP; "
                        "resolve the duplicate before importing."
                    )
                validation["existing_match_type"] = "ambiguous_hostname_or_serial"
                validation["can_import"] = False
                validation["is_ready"] = False
                # Terminal, same as the name-ambiguous branch above: block on the serial/IP
                # duplicate and return before the no-match `else` re-adds create-time
                # role/cluster blockers.
                return

        if new_device:
            # A stronger identity (serial / management IP) uniquely bound this row after the
            # name fallback flagged a cross-model (VM + Device) hostname collision. That warning
            # said "cannot determine which to match" — now moot, since new_device IS the match.
            # Drop it so the resolved row doesn't keep showing a stale "ambiguous" warning.
            msgs = validation.get("warnings")
            if isinstance(msgs, list):
                validation["warnings"] = [
                    m for m in msgs if not (isinstance(m, str) and _CROSS_MODEL_HOSTNAME_MARKER in m)
                ]
            validation["existing_device"] = new_device
            validation["existing_match_type"] = match_type
            # Re-derive linkage so a librenms_id match is correctly shown as the
            # host vs. OOB half, and existing_librenms_link is populated for the
            # paired badge (DB-only; no LibreNMS API call).
            _refresh_librenms_linkage(validation, new_device, libre_device, server_key)
            validation["can_import"] = False
            validation["is_ready"] = False
            # Determine actual model from the found object, not from import_as_vm flag
            actual_is_vm = found_as_cross_model != import_as_vm  # XOR: cross flips the flag
            validation["import_as_vm"] = actual_is_vm  # Update so future refreshes query correct model
            # A row that was previously unmatched can carry create-time blockers — "Device role
            # must be manually selected" and/or "Cluster must be manually selected" — that
            # validate_device_for_import() only adds when there's no existing_device. Now that
            # the row resolves to an existing object, none of those apply (and a cross-model
            # match can carry the *other* model's blocker). Drop both before recalculating so a
            # stale message doesn't linger in the UI; the row stays force-blocked as an existing
            # match regardless. The VM path previously cleared neither.
            remove_validation_issue(validation, "role")
            remove_validation_issue(validation, "cluster")
            # A cached new-import row can also carry "No matching site found…" / "No matching
            # device type found…" create-time blockers (device_operations.py). They don't apply
            # to a now-resolved existing match either, so clear them too or the validation detail
            # stays inconsistent with the resolved match.
            remove_validation_issue(validation, "site")
            remove_validation_issue(validation, "device type")
            if not actual_is_vm and hasattr(new_device, "role") and new_device.role:
                apply_role_to_validation(validation, new_device.role, is_vm=False)
            elif not actual_is_vm:
                _reset_device_role(validation)
            elif hasattr(new_device, "cluster") and new_device.cluster:
                # VM match: mirror the device-role display above — show the matched
                # VM's actual cluster rather than leaving the cached selection stale.
                apply_cluster_to_validation(validation, new_device.cluster)
            else:
                _reset_cluster(validation)
            recalculate_validation_status(validation, is_vm=actual_is_vm)
            # Re-assert non-importable: recalculate sets can_import from issues list,
            # but a late-found existing match must never be import-ready.
            validation["can_import"] = False
            validation["is_ready"] = False
        else:
            # No existing match at all — the row is a genuine new import. If a cached match was
            # just cleared above, its create-time role/cluster blocker was lost; re-add it so the
            # row can't flip to importable while still missing a required selection.
            _reassert_new_import_blockers(validation)
            recalculate_validation_status(validation, is_vm=import_as_vm)
    except AmbiguousLibreNMSIdError as exc:
        # An ambiguous librenms_id (matching multiple records) must block import rather
        # than fall through as "not found" and stay importable.
        logger.warning("Bulk re-check blocked — ambiguous librenms_id %r: %s", librenms_id, exc)
        validation["can_import"] = False
        validation["is_ready"] = False
        validation["ambiguous_librenms_id"] = True
        validation["existing_match_type"] = "ambiguous_librenms_id"
        message = (
            f"LibreNMS ID {librenms_id} {_AMBIGUOUS_LIBRENMS_ID_MARKER}; import "
            "blocked to avoid binding to the wrong object. Resolve the duplicate librenms_id "
            "assignment, then retry."
        )
        # Append to issues (not just warnings) — a later recalculate_validation_status()
        # recomputes can_import from issues, so a warning alone would be silently re-enabled.
        # Dedup so repeated refreshes don't stack the same message.
        if message not in validation.setdefault("warnings", []):
            validation["warnings"].append(message)
        if message not in validation.setdefault("issues", []):
            validation["issues"].append(message)
    except Exception as e:
        logger.error(f"Failed to check for newly imported device: {e}")
        # Fail closed: this recheck exists to catch duplicates that appeared after the cache
        # was built, so a transient failure (e.g. a DB error mid-lookup) must not leave a
        # previously-cached "importable" row importable — that would let a duplicate import
        # slip through exactly when the duplicate check couldn't run. Matches the fail-closed
        # behaviour of every other failure branch in this function.
        validation["can_import"] = False
        validation["is_ready"] = False
        message = "Duplicate re-check failed (transient lookup error); import blocked. Refresh to retry."
        # Append to issues (not just a warning) — a later recalculate_validation_status()
        # recomputes can_import from the issues list, so anything weaker would be silently
        # re-enabled. Dedup so repeated refreshes don't stack the same message.
        if message not in validation.setdefault("issues", []):
            validation["issues"].append(message)


def _empty_return(return_cache_status: bool):
    """Centralised empty-result return value for process_device_filters."""
    return ([], False) if return_cache_status else []


def process_device_filters(
    api: LibreNMSAPI,
    filters: dict,
    vc_detection_enabled: bool,
    clear_cache: bool,
    show_disabled: bool,
    exclude_existing: bool = False,
    job=None,
    request=None,
    return_cache_status: bool = False,
    use_sysname: bool = True,
    strip_domain: bool = False,
) -> List[dict] | tuple[List[dict], bool]:
    """
    Process LibreNMS device filters and return validated devices.

    Shared function used by both synchronous view and background job processing.
    Fetches devices, optionally pre-warms VC cache, validates each device, and
    caches results for HTMX row updates.

    Args:
        api: LibreNMS API client instance
        filters: Filter dict with location, type, os, hostname, sysname, hardware keys
        vc_detection_enabled: Whether to detect virtual chassis
        clear_cache: Whether to force cache refresh
        show_disabled: Whether to include disabled devices
        exclude_existing: Whether to exclude devices that already exist in NetBox
        job: Optional JobRunner instance for logging job events
        request: Optional Django request for client disconnect detection (synchronous only)
        return_cache_status: When True, returns (devices, from_cache) tuple
        use_sysname: If True, prefer sysName over hostname for device name resolution
        strip_domain: If True, strip domain suffix from device name

    Returns:
        List[dict]: Validated devices with _validation key, or tuple of (devices, from_cache)
        if return_cache_status is True. from_cache=True means data was loaded from existing
        cache; from_cache=False means data was just fetched from LibreNMS.
    """
    # Fetch devices from LibreNMS
    if job:
        job.logger.info(f"Fetching devices with filters: {filters}")
        if _is_job_cancelled(job):
            job.logger.warning("Job was stopped before fetching devices")
            return _empty_return(return_cache_status)
    else:
        logger.info(f"Fetching devices with filters: {filters}")

    # Always get cache status internally, even if not returning it
    # We need it to determine if metadata should be updated
    libre_devices, from_cache = get_librenms_devices_for_import(
        api,
        filters=filters,
        force_refresh=clear_cache,
        return_cache_status=True,
    )

    # Filter out disabled devices if requested. LibreNMS's "disabled" field (1=disabled,
    # 0=enabled) reflects manual device disablement; "status" reflects SNMP reachability.
    # show_disabled controls the former: hidden when disabled==1, shown regardless of status.
    if not show_disabled:
        libre_devices = [d for d in libre_devices if _safe_disabled(d) != 1]

    if job:
        job.logger.info(f"Found {len(libre_devices)} devices to process")
    else:
        logger.info(f"Found {len(libre_devices)} devices")

    # Check for early cancellation before the expensive VC prefetch
    if job and _is_job_cancelled(job):
        job.logger.warning("Job was stopped before VC pre-fetch")
        return _empty_return(return_cache_status)

    # Pre-warm VC cache if needed
    if vc_detection_enabled and libre_devices:
        device_ids = [d["device_id"] for d in libre_devices]
        if job:
            job.logger.info(
                f"Pre-fetching virtual chassis data for {len(device_ids)} devices. This may take some time..."
            )
        else:
            logger.info(f"Pre-fetching VC data for {len(device_ids)} devices")

        try:
            prefetch_vc_data_for_devices(api, device_ids, force_refresh=clear_cache)
            if job:
                job.logger.info("Virtual chassis data pre-fetch completed")
        except (BrokenPipeError, ConnectionError, IOError) as e:
            if request:
                logger.info(f"Client disconnected during VC prefetch: {e}")
                return _empty_return(return_cache_status)
            raise

    # Validate each device
    validated_devices = []
    total = len(libre_devices)
    # Always pass api so validate_device_for_import can run hardware/chassis lookups.
    # vc_detection_enabled only gates VC-specific paths inside that function.

    if job:
        job.logger.info(f"Starting validation of {total} devices")
        if _is_job_cancelled(job):
            job.logger.warning("Job was already stopped before validation started")
            return _empty_return(return_cache_status)
    else:
        logger.info(f"Validating {total} devices")

    for idx, device in enumerate(libre_devices, 1):
        # Check for job termination periodically
        if (idx % 5 == 0 or idx == 1) and job and _is_job_cancelled(job):
            job.logger.info(f"Job stopped at device {idx}/{total}. Exiting gracefully.")
            return _empty_return(return_cache_status)

        # Drop any cached validation/meta keys before recomputing
        device.pop("_validation", None)

        # Generate shared cache key for this validated device
        device_id = device["device_id"]
        cache_key = get_validated_device_cache_key(
            server_key=api.server_key,
            filters=filters,
            device_id=device_id,
            vc_enabled=vc_detection_enabled,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
        )

        # Check if we already have cached validation for this device
        # (only if not forcing refresh)
        if not clear_cache:
            cached_device = cache.get(cache_key)
            if cached_device:
                # Use cached validation
                device["_validation"] = cached_device["_validation"]

                # Refresh existing_device from DB to avoid stale data
                # (user may have changed role, name, etc. in NetBox)
                _refresh_existing_device(device["_validation"], libre_device=device, server_key=api.server_key)

                # Apply exclude_existing filter if enabled
                if exclude_existing:
                    validation = device["_validation"]
                    if validation["existing_device"]:
                        continue

                validated_devices.append(device)
                continue

        # Not in cache or forcing refresh - validate now
        try:
            validation = validate_device_for_import(
                device,
                api=api,
                include_vc_detection=vc_detection_enabled,
                force_vc_refresh=False,
                server_key=api.server_key,
                use_sysname=use_sysname,
                strip_domain=strip_domain,
            )
        except (BrokenPipeError, ConnectionError, IOError) as e:
            if request:
                logger.info(f"Client disconnected during device validation: {e}")
                return _empty_return(return_cache_status)
            raise

        # Set VC detection metadata
        if not vc_detection_enabled:
            validation["virtual_chassis"] = empty_virtual_chassis_data()

        # Apply exclude_existing filter if enabled
        if exclude_existing and validation["existing_device"]:
            continue

        device["_validation"] = validation
        validated_devices.append(device)

        # Cache with TWO keys for different purposes:
        # 1. Complex key (with filter context) - for full validated device with all metadata
        cache.set(cache_key, device, timeout=api.cache_timeout)

        # 2. Simple key (device ID only) - for quick device data lookup by role/rack updates
        #    This avoids redundant API calls when user interacts with dropdowns
        simple_cache_key = get_import_device_cache_key(device_id, api.server_key)
        # Cache just the raw device data (not the full validation result)
        # This is what get_validated_device_with_selections() expects
        device_data_only = {k: v for k, v in device.items() if k != "_validation"}
        cache.set(simple_cache_key, device_data_only, timeout=api.cache_timeout)

    # Store cache metadata (timestamp) for all filter operations
    # This enables countdown display regardless of background job vs synchronous execution
    # Always store metadata when we have validated devices, even if from_cache
    # This ensures metadata is available for countdown display
    if validated_devices:
        from datetime import datetime, timezone

        cache_metadata_key = get_cache_metadata_key(
            server_key=api.server_key,
            filters=filters,
            vc_enabled=vc_detection_enabled,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
        )

        # Check if metadata already exists to preserve original timestamp
        # BUT: if clear_cache was requested or data came fresh from LibreNMS, update it
        existing_metadata = cache.get(cache_metadata_key)
        should_update = clear_cache or not from_cache

        if existing_metadata and not should_update:
            # Metadata exists and cache wasn't cleared, keep using it (preserves original cache time)
            pass
        else:
            # No metadata exists, OR cache was cleared, OR fresh data - create/update it now
            cache_metadata = {
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "cache_timeout": api.cache_timeout,
                "filters": filters,
                "vc_enabled": vc_detection_enabled,
                "device_count": len(validated_devices),
            }
            cache.set(cache_metadata_key, cache_metadata, timeout=api.cache_timeout)

            # Maintain cache index for this server to enable listing active searches
            cache_index_key = f"librenms_cache_index_{api.server_key}"
            cache_index = cache.get(cache_index_key, [])
            # Add this cache key if not already in index
            if cache_metadata_key not in cache_index:
                cache_index.append(cache_metadata_key)
            # Always re-write the index so its TTL matches the freshly-written metadata.
            # Without this the index can expire before the metadata and the active
            # search entry disappears from the UI.
            cache.set(cache_index_key, cache_index, timeout=api.cache_timeout)

    if job:
        if exclude_existing:
            filtered_count = total - len(validated_devices)
            job.logger.info(
                f"Validation complete: {len(validated_devices)} devices passed filter, "
                f"{filtered_count} filtered out (existing devices excluded)"
            )
        else:
            job.logger.info(f"Validation complete: {len(validated_devices)} devices ready for import")
    else:
        logger.info(f"Processed {len(validated_devices)} validated devices")

    if return_cache_status:
        return validated_devices, from_cache
    return validated_devices
