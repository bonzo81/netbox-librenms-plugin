"""Bulk import orchestration for devices and filter processing."""

import hashlib
import logging
from typing import List

from core.choices import JobStatusChoices
from django.core.cache import cache

from ..import_validation_helpers import apply_role_to_validation, recalculate_validation_status, remove_validation_issue
from ..librenms_api import LibreNMSAPI
from ..utils import find_by_librenms_id
from .cache import get_cache_metadata_key, get_import_device_cache_key, get_validated_device_cache_key
from .device_operations import import_single_device, validate_device_for_import
from .filters import get_librenms_devices_for_import
from .permissions import check_user_permissions, require_permissions
from .virtual_chassis import (
    create_virtual_chassis_with_members,
    empty_virtual_chassis_data,
    prefetch_vc_data_for_devices,
)

logger = logging.getLogger(__name__)


def _safe_disabled(device: dict) -> int:
    """
    Return 1 if the device is disabled, 0 otherwise.

    Handles None, booleans, numeric strings, and common truthy/falsy tokens
    (e.g. "true"/"yes"/"on" → 1, "false"/"no"/"off" → 0) without raising.
    """
    val = device.get("disabled", 0)
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, str):
        normalized = val.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return 1
        if normalized in ("0", "false", "no", "off", ""):
            return 0
    try:
        int_val = int(val)
        return 1 if int_val else 0
    except (TypeError, ValueError):
        return 0


def bulk_import_devices_shared(
    device_ids: List[int],
    server_key: str = None,
    sync_options: dict = None,
    manual_mappings_per_device: dict = None,
    libre_devices_cache: dict = None,
    vc_detection_enabled: bool = False,
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
        vc_detection_enabled: Whether to enable virtual chassis detection during import.
            Should match the flag used during the filter/preview step for consistency.
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
    # change_device is needed for VC master/member updates; VMs are only created, not changed.
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

    for idx, device_id in enumerate(device_ids, start=1):
        # Check for job cancellation on first iteration and every 5th thereafter.
        # Check RQ/Redis state first (reflects stop API immediately); fall back to DB.
        if job and (idx == 1 or idx % 5 == 0):
            try:
                from django_rq import get_queue
                from rq.job import Job as RQJob

                queue = get_queue("default")
                rq_job = RQJob.fetch(str(job.job.job_id), connection=queue.connection)
                if rq_job.is_failed or rq_job.is_stopped:
                    if job.logger:
                        job.logger.warning(
                            f"Import job stopped at device {idx} of {total} (RQ status: {rq_job.get_status()})"
                        )
                    else:
                        logger.warning(f"Import cancelled at device {idx} of {total}")
                    _cancelled = True
                    break
            except Exception:
                # Fall back to DB check if RQ is unavailable
                job.job.refresh_from_db()
                job_status = job.job.status
                status_value = job_status.value if hasattr(job_status, "value") else job_status
                if status_value in (JobStatusChoices.STATUS_FAILED, "failed", "errored"):
                    if job.logger:
                        job.logger.warning(f"Import job cancelled at device {idx} of {total}")
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
                success, libre_device = api.get_device_info(device_id)

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
                include_vc_detection=vc_detection_enabled,
            )

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
                vc_data = validation.get("virtual_chassis", {})
                if vc_data.get("is_stack", False):
                    # Derive a stack-level dedup key from member serials so that all
                    # LibreNMS devices belonging to the same physical stack (e.g. each
                    # switch in a stacked chassis that appears as a separate device in
                    # LibreNMS) share the same key and VC creation is triggered only once.
                    # Fall back to device_id when no member serials are available.
                    member_serials = sorted(
                        serial
                        for m in vc_data.get("members", [])
                        if (serial := str(m.get("serial") or "").strip()) and serial != "-"
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
                            fingerprint = hashlib.md5(",".join(member_parts).encode()).hexdigest()[:12]
                            vc_domain = f"librenms-stack-{fingerprint}"
                        else:
                            vc_domain = f"librenms-{device_id}"

                    # Only create VC if we haven't processed this stack yet
                    if vc_domain not in processed_vc_domains:
                        # Guard VC creation with its own permission check — the upfront check
                        # only covers add_device/change_device; VirtualChassis needs a separate perm.
                        has_vc_perm, missing_vc_perms = check_user_permissions(user, ["dcim.add_virtualchassis"])
                        if not has_vc_perm:
                            warn_msg = (
                                f"Skipping VC creation for device {device_id}: "
                                f"missing permissions: {', '.join(missing_vc_perms)}"
                            )
                            if job and job.logger:
                                job.logger.warning(warn_msg)
                            else:
                                logger.warning(warn_msg)
                        else:
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
    vc_detection_enabled: bool = False,
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
        vc_detection_enabled: Whether to enable virtual chassis detection during import.
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
        vc_detection_enabled=vc_detection_enabled,
        job=None,  # No job context for synchronous imports
        user=user,
    )


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
                if hasattr(refreshed, "role") and refreshed.role:
                    apply_role_to_validation(validation, refreshed.role, is_vm=bool(validation.get("import_as_vm")))
                elif not validation.get("import_as_vm"):
                    validation["device_role"] = {"found": False, "role": None}
                    remove_validation_issue(validation, "role")
                recalculate_validation_status(validation, is_vm=bool(validation.get("import_as_vm")))
                return
            else:
                # Device was deleted since caching — recompute readiness to match
                # validate_device_for_import logic.
                validation["existing_device"] = None
                validation["existing_match_type"] = None
                # Clear stale device_role so is_ready is computed from scratch.
                # Guard: VMs don't use device_role for readiness, so preserve any
                # user-selected role rather than silently dropping it.
                if not validation.get("import_as_vm"):
                    validation["device_role"] = {"found": False, "role": None}
                recalculate_validation_status(validation, is_vm=bool(validation.get("import_as_vm")))
        except Exception as e:
            existing_id = getattr(existing, "pk", "unknown") if existing else "none"
            logger.error(f"Failed to refresh existing device (pk={existing_id}): {e}")
            return

    # existing_device was None at cache time — check if device was imported since
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

        librenms_id = libre_device.get("device_id")
        hostname = libre_device.get("hostname", "")
        sys_name = libre_device.get("sysName", "")

        new_device = None
        match_type = None
        found_as_cross_model = False

        def _lookup_in_model(m):
            """Return (device, match_type) for model m, or (None, None)."""
            if librenms_id is not None and not isinstance(librenms_id, bool):
                try:
                    dev = find_by_librenms_id(m, int(librenms_id), server_key)
                    if dev:
                        return dev, "librenms_id"
                except (ValueError, TypeError):
                    pass
            resolved_name = validation.get("resolved_name")
            if resolved_name:
                dev = m.objects.filter(name__iexact=resolved_name).first()
                if dev:
                    return dev, "resolved_name"
            if hostname:
                dev = m.objects.filter(name__iexact=hostname).first()
                if dev:
                    return dev, "hostname"
            if sys_name:
                dev = m.objects.filter(name__iexact=sys_name).first()
                if dev:
                    return dev, "sysname"
            return None, None

        new_device, match_type = _lookup_in_model(Model)

        if not new_device:
            # Try the opposite model: catches cross-model imports that happened
            # after the cache was built (e.g. LibreNMS device imported as VM).
            new_device, match_type = _lookup_in_model(CrossModel)
            if new_device:
                found_as_cross_model = True

        if new_device:
            validation["existing_device"] = new_device
            validation["existing_match_type"] = match_type
            validation["can_import"] = False
            validation["is_ready"] = False
            # Determine actual model from the found object, not from import_as_vm flag
            actual_is_vm = found_as_cross_model != import_as_vm  # XOR: cross flips the flag
            validation["import_as_vm"] = actual_is_vm  # Update so future refreshes query correct model
            if not actual_is_vm and hasattr(new_device, "role") and new_device.role:
                apply_role_to_validation(validation, new_device.role, is_vm=False)
            elif not actual_is_vm:
                validation["device_role"] = {"found": False, "role": None}
            recalculate_validation_status(validation, is_vm=actual_is_vm)
    except Exception as e:
        logger.error(f"Failed to check for newly imported device: {e}")


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
        # Initial check if job was already terminated before we even started
        try:
            from django_rq import get_queue
            from rq.job import Job as RQJob

            queue = get_queue("default")
            rq_job = RQJob.fetch(str(job.job.job_id), connection=queue.connection)

            if rq_job.is_failed or rq_job.is_stopped:
                job.logger.warning("Job was already stopped before validation started")
                return _empty_return(return_cache_status)
        except Exception:
            # Fall back to DB check if RQ check fails
            job.job.refresh_from_db()
            if job.job.status in (JobStatusChoices.STATUS_FAILED, JobStatusChoices.STATUS_ERRORED):
                job.logger.warning("Job was stopped before validation started")
                return _empty_return(return_cache_status)
    else:
        logger.info(f"Validating {total} devices")

    for idx, device in enumerate(libre_devices, 1):
        # Check for job termination or client disconnect periodically
        if idx % 5 == 0 or idx == 1:  # Check more frequently (every 5 devices + first device)
            if job:
                # Check if job was terminated via stop API
                # CRITICAL: Check the RQ job status in Redis, not just the DB model
                # NetBox's stop endpoint marks the RQ job as failed in Redis
                try:
                    from django_rq import get_queue
                    from rq.job import Job as RQJob

                    queue = get_queue("default")
                    rq_job = RQJob.fetch(str(job.job.job_id), connection=queue.connection)

                    # Check if RQ job is in a stopped state
                    if rq_job.is_failed or rq_job.is_stopped:
                        job.logger.info(
                            f"Job stopped at device {idx}/{total} (RQ status: {rq_job.get_status()}). Exiting gracefully."
                        )
                        return _empty_return(return_cache_status)
                except Exception:
                    # If we can't check RQ status, fall back to DB status check
                    job.job.refresh_from_db()
                    if job.job.status in (JobStatusChoices.STATUS_FAILED, JobStatusChoices.STATUS_ERRORED):
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
