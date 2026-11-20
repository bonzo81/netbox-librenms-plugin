"""Bulk import orchestration for devices and filter processing."""

import functools
import logging
from contextlib import nullcontext
from dataclasses import dataclass
from typing import List

from dcim.models import Device, VirtualChassis
from django.core.cache import cache

from ..import_validation_helpers import (
    apply_role_to_validation,
)
from ..librenms_api import LibreNMSAPI
from ..models import RuleConfigurationError
from ..serial_identity import (
    SerialRules,
    assignment_lock,
    display,
    fingerprint,
    is_serial_value,
    resolve_devices,
)
from ..transactions import atomic_with_events
from ..utils import (
    cached_row_matches,
    exception_text_for,
    preload_normalization_rules,
    row_identity_matches,
)
from .cache import (
    get_cache_index_key,
    get_chassis_inventory_cache_key,
    get_cache_metadata_key,
    get_import_device_cache_key,
    search_metadata_is_current,
    serial_rules_version,
)
from .collisions import detect_bulk_collisions, scope_bulk_collisions
from .device_operations import (
    VALIDATION_ERROR_ISSUE_PREFIX,
    DeviceTypeOverrideError,
    effective_import_device_type,
    import_single_device,
    is_serial_unavailable_issue,
    validate_device_for_import,
)
from .filters import _safe_disabled, get_librenms_devices_for_import
from .permissions import check_user_permissions, require_permissions
from .virtual_chassis import (
    VC_MASTER_UNKNOWN_WARNING,
    VCNumberingError,
    create_virtual_chassis_with_members,
    empty_virtual_chassis_data,
    get_virtual_chassis_data,
    load_numbering_rules,
    numbering_refusal_text,
    prefetch_vc_data_for_devices,
    prepare_virtual_chassis,
    prepared_master,
)

logger = logging.getLogger(__name__)

LIBRENMS_ROW_UNREADABLE = "LibreNMS data couldn't be read to verify the row"
NETBOX_SERIAL_UNVERIFIED = "NetBox could not verify the serial"


def _is_job_cancelled(job) -> bool:
    """
    Return True if a background job has been stopped or cancelled.

    Checks RQ/Redis state only (reflects stop API calls immediately).
    On Redis connectivity issues or a missing RQ job, returns False to avoid
    false cancellation. Unexpected exceptions are logged and also return False.

    Args:
        job (object): The background job context that contains the RQ job identifier.

    Returns:
        bool: True if the RQ job has failed or stopped, otherwise False.

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
    device_ids,
    api,
    libre_devices_cache=None,
    sync_options=None,
    job=None,
    vm_device_ids=None,
    user=None,
) -> tuple[list[dict], dict[int, str]]:
    """
    Detect NetBox object collisions for a batch, and the rows that cannot be checked.

    Validates each device just enough to read the collision-relevant match fields
    (``include_vc_detection=False``), then groups rows that target the same NetBox
    object. It also reads virtual chassis data for Device rows, so a row whose stack
    inventory cannot be read is unresolved. A later import can reuse these reads while
    the configured virtual chassis cache remains valid.

    This enforces the bulk-confirm collision block on the import paths that do NOT pass
    through the confirm modal — the direct ``BulkImportDevicesView`` POST and the
    background ``ImportDevicesJob`` — so a colliding batch can't be imported by bypassing
    the preview.

    Args:
        device_ids: LibreNMS device ids about to be imported.
        api: A LibreNMS API client. ``get_device_info`` is called directly only for ids
            missing from the cache. Virtual chassis detection also uses the client.
        libre_devices_cache: Optional ``{device_id: libre_device}`` pre-fetched data.
        sync_options: Optional sync options (``use_sysname`` / ``strip_domain``).
        job: Optional background-job context. When set, cancellation is polled the same
            way the import loops poll it (first id, then every 5th); a cancelled job stops
            the scan instead of finishing a large cache-miss batch's API calls.
        vm_device_ids: Optional collection of the batch's VM-selected LibreNMS ids. Each row
            is validated in its ACTUAL import mode: a VM row validated as a Device would run
            the Device-only serial/IP matching that ``bulk_import_vms`` intentionally skips,
            and could fabricate a collision (blocking a valid batch) against a Device it
            will never touch.
        user: Optional requesting user. Production callers pass this so collision targets outside
            the user's view scope are redacted before the result reaches a template or job log.

    Returns:
        tuple: Collision groups and safe reasons keyed by each unresolved row ID.

    """
    use_sysname = (sync_options or {}).get("use_sysname", True)
    strip_domain = (sync_options or {}).get("strip_domain", False)
    # Reuse the caller's dict object (mutate it in place) rather than `or {}`, which would swap in a
    # throwaway dict when the caller passes an empty one — the write-back below must be visible to
    # the caller so the downstream import reuses it.
    devices_cache = libre_devices_cache if libre_devices_cache is not None else {}
    vm_id_set = set(vm_device_ids or ())
    devices = []
    unresolved = {}
    device_ids = list(device_ids)
    serial_rules = SerialRules.load()
    for idx, device_id in enumerate(device_ids, start=1):
        # Mark the unscanned rows unresolved so the caller can stop the batch.
        if job and (idx == 1 or idx % 5 == 0) and _is_job_cancelled(job):
            if getattr(job, "logger", None):
                job.logger.warning(f"Collision pre-check stopped by cancellation at id {idx} of {len(device_ids)}")
            unresolved.update(dict.fromkeys(device_ids[idx - 1 :], LIBRENMS_ROW_UNREADABLE))
            break
        libre_device = devices_cache.get(device_id)
        just_fetched = False
        if libre_device is None:
            try:
                # The verified row is written into the shared import cache below, so bypass the
                # short-lived API snapshot just like the downstream import's own live read.
                success, libre_device = api.get_device_info(device_id, use_cache=False)
            except Exception as exc:
                # get_device_info can raise rather than return (False, None), for example on an
                # unexpected transport/backend failure. The gate's contract is to fail closed per
                # row, so treat an exception like a fetch miss instead of crashing the whole batch.
                if getattr(job, "logger", None):
                    # A job log is read later by each viewer of the job, so it keeps only hidden text.
                    detail = exception_text_for(exc, Device, None)
                    job.logger.warning(f"Collision pre-check couldn't fetch device {device_id}: {detail}")
                else:
                    logger.warning("Collision pre-check couldn't fetch device %s: %s", device_id, exc)
                unresolved[device_id] = LIBRENMS_ROW_UNREADABLE
                continue
            if not success or not isinstance(libre_device, dict):
                # Couldn't fetch device info → can't collision-check this id. Record it so the
                # caller blocks it instead of importing it unchecked (fail closed).
                unresolved[device_id] = LIBRENMS_ROW_UNREADABLE
                continue
            just_fetched = True
        # Fail closed on a bad cached payload too: a negatively-cached lookup (an empty dict or a
        # row whose device_id doesn't match the requested id — e.g. a stale/mis-keyed cache entry)
        # would otherwise reach validation as a brand-new device and make the collision gate report
        # a clean scan for an id it never actually verified.
        if not row_identity_matches(libre_device, device_id):
            unresolved[device_id] = LIBRENMS_ROW_UNREADABLE
            continue
        if just_fetched:
            # Persist the freshly fetched row into the shared cache — but only now that its
            # device_id is verified to match the requested id — so the downstream import
            # (bulk_import_devices_shared / bulk_import_vms) reuses it instead of re-fetching the
            # same device from LibreNMS. Deferring the write until past the mismatch check keeps a
            # mis-keyed payload from poisoning the shared cache for any other consumer that reads it
            # (e.g. a same-batch retry that hits the cache instead of re-fetching), even though this
            # id is (correctly) recorded unresolved above.
            devices_cache[device_id] = libre_device
        validation = validate_device_for_import(
            # DB-only collision pre-check: don't hand the validator an API client (it would let
            # API-backed validation paths run even with the cache supplied + include_vc_detection
            # False). The collision-relevant match fields are resolved from the DB; api is only
            # used for VC/chassis enrichment, all of which is guarded on `api` being truthy.
            libre_device,
            api=None,
            # Validate in the row's ACTUAL import mode: VM rows skip the Device-only
            # serial/IP matching exactly like bulk_import_vms will, so a coincidental
            # serial can't fabricate a collision against a Device the row never touches.
            import_as_vm=device_id in vm_id_set,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
            server_key=api.server_key,
            include_vc_detection=False,
            collision_only=True,
            serial_rules=serial_rules,
        )
        if any(str(issue).startswith(VALIDATION_ERROR_ISSUE_PREFIX) for issue in validation.get("issues", [])):
            # validate_device_for_import() caught an exception and returned only a partial result
            # (its except branch appends a VALIDATION_ERROR_ISSUE_PREFIX issue). The collision-relevant
            # match fields may be missing, so this id was NOT reliably collision-checked. Fail
            # closed: record it as unresolved rather than collision-check it on incomplete data
            # (mirrors the get_device_info miss above). The prefix is a shared constant so this
            # guard can't silently drift from the producer's wording.
            unresolved[device_id] = LIBRENMS_ROW_UNREADABLE
            continue
        if _stopped_at_unavailable_serial(validation):
            unresolved[device_id] = NETBOX_SERIAL_UNVERIFIED
            continue
        if device_id not in vm_id_set and get_virtual_chassis_data(api, device_id, serial_rules=serial_rules).get(
            "detection_failed"
        ):
            unresolved[device_id] = LIBRENMS_ROW_UNREADABLE
        devices.append(
            {
                "device_id": device_id,
                "device_name": validation.get("resolved_name") or f"device-{device_id}",
                "validation": validation,
            }
        )
    collisions = detect_bulk_collisions(devices)
    if user is not None:
        collisions = scope_bulk_collisions(collisions, user)
    return collisions, unresolved


@dataclass
class BulkPrecheckOutcome:
    """
    Shared decision derived from a :func:`detect_collisions_for_device_ids` result.

    Consumed by BOTH non-modal import callers — the synchronous ``BulkImportDevicesView`` and the
    async ``ImportDevicesJob`` — so their block/skip semantics and user-facing wording can't drift
    (they previously re-implemented the branching inline, and the copy had already diverged:
    "Import blocked" vs "Bulk import blocked").

    Semantics:
        * ``blocked``: two LibreNMS rows resolve to the same NetBox object. The WHOLE batch is
          blocked and nothing imports.
        * ``skipped_ids`` (unresolved rows): a row whose LibreNMS info couldn't be fetched/validated
          to collision-check it. These are SKIPPED, not a whole-batch block — a transient miss on
          one row no longer drops the entire import — but they are NOT imported either (importing an
          un-collision-checked row could bypass the collision guard). The rest import normally.

    Attributes:
        blocked: True when a collision blocks the whole batch; import nothing.
        collisions: The collision groups (for the HTMX modal / job log).
        block_message: Shared collision-block copy (``""`` when not blocked).
        skipped_ids: Unresolved ids to skip (import the rest).
        skip_message: Shared copy naming skipped rows in an unblocked batch (``""`` otherwise).
        importable_device_ids: ``device_ids`` minus ``skipped_ids``.
        importable_vm_imports: ``vm_imports`` minus ``skipped_ids``.

    """

    blocked: bool
    collisions: list
    block_message: str
    skipped_ids: list
    skip_message: str
    importable_device_ids: list
    importable_vm_imports: dict


def classify_bulk_precheck(collisions, unresolved, device_ids, vm_imports) -> BulkPrecheckOutcome:
    """
    Turn a collision scan into the shared import decision.

    See :class:`BulkPrecheckOutcome` for the block-vs-skip semantics. NetBox object collisions
    block the whole batch. Unresolved rows are excluded from the importable sets and surfaced via
    ``skip_message`` while the rest import.

    Args:
        collisions: Collision groups from :func:`detect_bulk_collisions`.
        unresolved: Safe reasons keyed by the IDs that could not be checked.
        device_ids: The batch's device-import ids.
        vm_imports: The batch's VM imports mapping (``{device_id: manual_mappings}``).

    Returns:
        BulkPrecheckOutcome: The shared block/skip decision.

    """
    unresolved_set = set(unresolved)
    importable_device_ids = [d for d in device_ids if d not in unresolved_set]
    importable_vm_imports = {d: v for d, v in vm_imports.items() if d not in unresolved_set}

    skip_message = ""
    if unresolved and not collisions:
        ids_by_reason = {}
        for device_id, reason in unresolved.items():
            ids_by_reason.setdefault(reason, []).append(str(device_id))
        skip_message = " ".join(
            f"Skipped {len(ids)} selected row(s) (id(s): {', '.join(ids)}): {reason}, so they were not imported."
            for reason, ids in ids_by_reason.items()
        )
        skip_message += " The remaining selected rows continue through normal import checks. Review the final result."
        if LIBRENMS_ROW_UNREADABLE in ids_by_reason:
            skip_message += " Retry rows with unreadable LibreNMS data individually."

    block_message = ""
    if collisions:
        scoped = any("target_visible" in group for group in collisions)
        visible_pks = [group["nb_device_pk"] for group in collisions if group.get("target_visible") is True]
        if visible_pks:
            target_detail = f" Visible pk(s): {', '.join(str(pk) for pk in visible_pks)}."
        elif scoped:
            target_detail = " Target details are omitted when they are outside your view scope."
        else:
            target_detail = ""
        block_message = (
            f"Bulk import blocked: {len(collisions)} NetBox object collision(s) in this batch."
            f"{target_detail} Two or more selected LibreNMS devices resolve to the same NetBox "
            f"object; resolve each individually, or deselect the duplicates."
        )

    return BulkPrecheckOutcome(
        blocked=bool(collisions),
        collisions=collisions,
        block_message=block_message,
        skipped_ids=list(unresolved),
        skip_message=skip_message,
        importable_device_ids=importable_device_ids,
        importable_vm_imports=importable_vm_imports,
    )


def stack_key(vc_data, device_id, context) -> str:
    """Return the key of one physical stack: its member serial identities under *context*, or the device without one."""
    members = fingerprint((member.get("serial") for member in vc_data.get("members", [])), context)
    return f"librenms-stack-{members}" if members else f"librenms-stack-device-{device_id}"


def bulk_import_devices_shared(  # noqa: C901
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
                'virtual_chassis_created': int,  # Number of VCs created
                'warnings': List[str],  # Imported devices whose follow-up work failed
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
    # A job's data and log are read later by each viewer of the job, so they keep only hidden text.
    text_viewer = None if job is not None else user

    # change_device is needed for VC master/member updates.
    required_perms = [
        "dcim.add_device",
        "dcim.change_device",
    ]
    require_permissions(user, required_perms, "import devices")

    total = len(device_ids)
    success_list = []
    failed_list = []
    skipped_list = []
    warnings_list = []
    vc_created_count = 0
    processed_vc_domains = set()  # Track VCs already created by domain
    _cancelled = False

    # Initialize API client once for all devices to avoid repeated config parsing
    api = LibreNMSAPI(server_key=server_key)

    # Preload the device_type NormalizationRule set once so the per-device hardware→device-type
    # match doesn't re-query it for every device in the loop (issue #90 / N+1 avoidance, #92).
    device_type_norm_rules = preload_normalization_rules("device_type")
    serial_rules = SerialRules.load()
    numbering_rules = load_numbering_rules()

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
            # Use cached device data if available to avoid redundant API calls — but only when the
            # cached row's OWN device_id doesn't contradict the requested id (cached_row_matches).
            # detect_collisions_for_device_ids verifies this too, but its callers skip the pre-check
            # for single-row imports, so re-check at the point of use; a contradiction falls through
            # to a live fetch below.
            cached_row = libre_devices_cache.get(device_id) if libre_devices_cache else None
            if cached_row_matches(cached_row, device_id):
                libre_device = cached_row
                success = True
            else:
                # Import decisions (DeviceType match, serial-conflict, hostname) must run against
                # live LibreNMS data: bypass the short device-info read cache so a value the user
                # just corrected in LibreNMS isn't read back stale within the cache window.
                # The client refuses a row for another device ID, so a row here describes this one.
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
                serial_rules=serial_rules,
                numbering_rules=numbering_rules,
            )

            if validation.get("import_as_vm"):
                has_vm_perm, _ = check_user_permissions(user, ["virtualization.add_virtualmachine"])
                if not has_vm_perm:
                    error_msg = (
                        f"Cannot import device row {device_id} as a VM: "
                        "missing permission virtualization.add_virtualmachine"
                    )
                    failed_list.append({"device_id": device_id, "error": error_msg})
                    if job and job.logger:
                        job.logger.error(error_msg)
                    else:
                        logger.error(error_msg)
                    continue

            vc_data = validation.get("virtual_chassis", {})
            if vc_data.get("detection_failed"):
                error_msg = f"Cannot import device {device_id}: virtual chassis detection failed"
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

            # Override with any manual mappings provided for this device
            if manual_mappings_per_device and device_id in manual_mappings_per_device:
                device_mappings.update(manual_mappings_per_device[device_id])

            # The master is matched with the serial rules of the DeviceType this row imports as.
            try:
                device_type = effective_import_device_type(
                    validation["device_type"].get("device_type"), device_mappings
                )
            except DeviceTypeOverrideError as exc:
                failed_list.append({"device_id": device_id, "error": str(exc)})
                if job and job.logger:
                    job.logger.error(f"Cannot import device {device_id}: {exc}")
                else:
                    logger.error(f"Cannot import device {device_id}: {exc}")
                continue
            # The one numbering and master decision for this row, for its final DeviceType and before any
            # write; a stack whose master is unknown imports standalone, so it needs no chassis permission.
            try:
                vc_data = prepare_virtual_chassis(
                    vc_data,
                    device_type=device_type,
                    master_name=validation.get("resolved_name"),
                    device_serial=libre_device.get("serial"),
                    serial_rules=serial_rules,
                    numbering_rules=numbering_rules,
                )
            except (VCNumberingError, RuleConfigurationError) as exc:
                error_msg = f"Cannot import device {device_id}: {numbering_refusal_text(exc)}"
                failed_list.append({"device_id": device_id, "error": error_msg})
                if job and job.logger:
                    job.logger.error(error_msg)
                else:
                    logger.error(error_msg)
                continue
            validation["virtual_chassis"] = vc_data
            master_member = prepared_master(vc_data) if vc_data.get("is_stack", False) else None
            creates_vc = master_member is not None
            if creates_vc:
                has_vc_perm, _ = check_user_permissions(user, ["dcim.add_virtualchassis"])
                if not has_vc_perm:
                    error_msg = f"Cannot import stack device {device_id}: missing permission dcim.add_virtualchassis"
                    failed_list.append({"device_id": device_id, "error": error_msg})
                    if job and job.logger:
                        job.logger.error(error_msg)
                    else:
                        logger.error(error_msg)
                    continue

            selected_role_id = device_mappings.pop("device_role_id", None)
            if selected_role_id:
                from dcim.models import DeviceRole

                selected_role = DeviceRole.objects.restrict(user, "view").filter(pk=selected_role_id).first()
                if selected_role is None:
                    error_msg = "Selected role is unavailable"
                    failed_list.append({"device_id": device_id, "error": error_msg})
                    if job and job.logger:
                        job.logger.error(error_msg)
                    else:
                        logger.error(error_msg)
                    continue
                apply_role_to_validation(validation, selected_role, is_vm=False)

            # One key per physical stack, so only its first row creates the chassis.
            vc_domain = stack_key(vc_data, device_id, serial_rules.for_device_type(device_type)) if creates_vc else None
            builds_vc = vc_domain is not None and vc_domain not in processed_vc_domains
            vc = None
            try:
                # A stack device and its chassis are one rollback unit: a failed chassis fails the row, and a retry imports both.
                with atomic_with_events() if builds_vc else nullcontext():
                    if builds_vc:
                        # The serial write set of the whole stack, locked before any row lock.
                        serials = [libre_device.get("serial"), *(member.get("serial") for member in vc_data["members"])]
                        assignment_lock([display(serial) for serial in serials], model=Device, rules=serial_rules)
                    result = import_single_device(
                        device_id,
                        server_key=api.server_key,  # use resolved key, not raw parameter (may be None)
                        validation=validation,
                        sync_options=sync_options,
                        manual_mappings=device_mappings if device_mappings else None,
                        libre_device=libre_device,
                        user=user,
                        text_viewer=text_viewer,
                        serial_rules=serial_rules,
                    )
                    if builds_vc and result["success"]:
                        vc = create_virtual_chassis_with_members(
                            result["device"],
                            vc_data,
                            libre_device,
                            server_key=api.server_key,
                            master_member=master_member,
                            serial_rules=serial_rules,
                        )
            except Exception as vc_error:
                error_msg = f"Cannot import stack device {device_id}: {exception_text_for(vc_error, VirtualChassis, text_viewer)}"
                failed_list.append({"device_id": device_id, "error": error_msg})
                if job and job.logger:
                    job.logger.error(error_msg)
                else:
                    logger.error(error_msg)
                continue

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
                for warn_msg in result["warnings"]:
                    warnings_list.append(warn_msg)
                    if job and job.logger:
                        job.logger.warning(warn_msg)
                    else:
                        logger.warning(warn_msg)

                if vc_data.get("is_stack", False) and not creates_vc:
                    warn_msg = VC_MASTER_UNKNOWN_WARNING.format(device_id=device_id)
                    warnings_list.append(warn_msg)
                    if job and job.logger:
                        job.logger.warning(warn_msg)
                    else:
                        logger.warning(warn_msg)

                if vc is not None:
                    processed_vc_domains.add(vc_domain)
                    vc_created_count += 1
                    log_msg = f"Created VC '{vc.name}' during bulk import for device {device_id}"
                    if job and job.logger:
                        job.logger.info(log_msg)
                    else:
                        logger.info(log_msg)

            elif result.get("device"):  # Device exists
                skipped_list.append({"device_id": device_id, "reason": result["error"]})
            else:  # Failed to import
                failed_list.append({"device_id": device_id, "error": result["error"]})
                if job and job.logger:
                    job.logger.error(f"Failed to import device {device_id}: {result['error']}")

        except Exception as e:
            detail = exception_text_for(e, Device, text_viewer)
            if job and job.logger:
                job.logger.error(f"Unexpected error importing device {device_id}: {detail}", exc_info=True)
            else:
                logger.exception(f"Unexpected error importing device {device_id}")
            failed_list.append({"device_id": device_id, "error": detail})

    return {
        "total": total,
        "success": success_list,
        "failed": failed_list,
        "skipped": skipped_list,
        "virtual_chassis_created": vc_created_count,
        "warnings": warnings_list,
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


def _stopped_at_unavailable_serial(validation: dict) -> bool:
    """Return whether validation stopped at an Unavailable serial lookup, before the site and type checks."""
    return any(is_serial_unavailable_issue(issue) for issue in validation.get("issues", []))


def search_serial_discoveries(devices, serial_rules):
    """Build a lazy serial lookup shared by the rows of a search."""

    @functools.cache
    def discover():
        return resolve_devices(
            (display(row.get("serial")) for row in devices if is_serial_value(row.get("serial"))),
            rules=serial_rules,
        )

    return discover


def validate_search_row(
    api: LibreNMSAPI,
    device: dict,
    *,
    vc_detection_enabled,
    use_sysname,
    strip_domain,
    serial_rules,
    numbering_rules,
    serial_discoveries,
) -> dict:
    """
    Validate one row of an import search against the current NetBox state.

    A search and the completed-job page call it for every row. Only the LibreNMS-side inputs come from the cache.
    """
    validation = validate_device_for_import(
        device,
        api=api,
        include_vc_detection=vc_detection_enabled,
        force_vc_refresh=False,
        server_key=api.server_key,
        use_sysname=use_sysname,
        strip_domain=strip_domain,
        serial_rules=serial_rules,
        numbering_rules=numbering_rules,
        serial_discoveries=serial_discoveries,
    )
    if not vc_detection_enabled:
        validation["virtual_chassis"] = empty_virtual_chassis_data()
    return validation


def _empty_return(return_cache_status: bool):
    """Centralised empty-result return value for process_device_filters."""
    return ([], False) if return_cache_status else []


def process_device_filters(  # noqa: C901
    api: LibreNMSAPI,
    filters: dict,
    vc_detection_enabled: bool,
    clear_cache: bool,
    show_disabled: bool,
    exclude_existing: bool = False,
    job=None,
    return_cache_status: bool = False,
    use_sysname: bool = True,
    strip_domain: bool = False,
    *,
    serial_rules: SerialRules | None = None,
) -> List[dict] | tuple[List[dict], bool]:
    """
    Process LibreNMS device filters and return validated devices.

    Shared function used by both synchronous view and background job processing.
    Fetches devices, optionally pre-warms VC cache, validates each device against the
    current NetBox state, and caches the raw rows for the dialog and the completed-job page.

    Args:
        api: LibreNMS API client instance
        filters: Filter dict with location, type, os, hostname, sysname, hardware keys
        vc_detection_enabled: Whether to detect virtual chassis
        clear_cache: Whether to force cache refresh
        show_disabled: Whether to include disabled devices
        exclude_existing: Whether to exclude devices that already exist in NetBox
        job: Optional JobRunner instance for logging job events
        return_cache_status: When True, returns (devices, from_cache) tuple
        use_sysname: If True, prefer sysName over hostname for device name resolution
        strip_domain: If True, strip domain suffix from device name
        serial_rules: The serial rule snapshot used to validate and cache the search.

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

    cache_metadata_key = get_cache_metadata_key(
        server_key=api.server_key,
        filters=filters,
        vc_enabled=vc_detection_enabled,
        use_sysname=use_sysname,
        strip_domain=strip_domain,
    )
    cache_index_key = get_cache_index_key(api.server_key)
    if clear_cache:
        cache.delete(cache_metadata_key)
        cache_index = cache.get(cache_index_key, [])
        if isinstance(cache_index, list):
            remaining_cache_keys = [key for key in cache_index if key != cache_metadata_key]
            if remaining_cache_keys:
                cache.set(cache_index_key, remaining_cache_keys, timeout=api.cache_timeout)
            else:
                cache.delete(cache_index_key)
        else:
            cache.delete(cache_index_key)

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

    if clear_cache and libre_devices:
        # The DeviceType fallback reads the chassis inventory again, as the VC prefetch reads the stacks again.
        cache.delete_many([get_chassis_inventory_cache_key(d["device_id"], api.server_key) for d in libre_devices])

    # Pre-warm VC cache if needed
    if vc_detection_enabled and libre_devices:
        device_ids = [d["device_id"] for d in libre_devices]
        if job:
            job.logger.info(
                f"Pre-fetching virtual chassis data for {len(device_ids)} devices. This may take some time..."
            )
        else:
            logger.info(f"Pre-fetching VC data for {len(device_ids)} devices")

        prefetch_vc_data_for_devices(api, device_ids, force_refresh=clear_cache)
        if job:
            job.logger.info("Virtual chassis data pre-fetch completed")

    # Validate each device
    validated_devices = []
    total = len(libre_devices)
    serial_rules = serial_rules or SerialRules.load()
    numbering_rules = load_numbering_rules()

    serial_discoveries = search_serial_discoveries(libre_devices, serial_rules)

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

        device_id = device["device_id"]
        validation = validate_search_row(
            api,
            device,
            vc_detection_enabled=vc_detection_enabled,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
            serial_rules=serial_rules,
            serial_discoveries=serial_discoveries,
            numbering_rules=numbering_rules,
        )
        # The dialog and the completed-job page read this raw row, so they describe the row that the table shows.
        cache.set(get_import_device_cache_key(device_id, api.server_key), dict(device), timeout=api.cache_timeout)

        if exclude_existing and validation["existing_device"]:
            continue

        device["_validation"] = validation
        validated_devices.append(device)

    # Store cache metadata (timestamp) for all filter operations
    # This enables countdown display regardless of background job vs synchronous execution
    # Always store metadata when we have validated devices, even if from_cache
    # This ensures metadata is available for countdown display
    if validated_devices:
        from datetime import datetime, timezone

        # Check if metadata already exists to preserve original timestamp
        # BUT: if clear_cache was requested or data came fresh from LibreNMS, update it
        existing_metadata = cache.get(cache_metadata_key)
        # Metadata of other serial rules describes stack detections that these rules no longer read.
        should_update = clear_cache or not from_cache or not search_metadata_is_current(existing_metadata, serial_rules)

        if existing_metadata and not should_update:
            # Metadata exists and cache wasn't cleared, keep using it (preserves original cache time)
            pass
        else:
            # No metadata exists, OR cache was cleared, OR fresh data - create/update it now
            cache_metadata = {
                "server_key": api.server_key,
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "cache_timeout": api.cache_timeout,
                "filters": filters,
                "vc_enabled": vc_detection_enabled,
                "use_sysname": use_sysname,
                "strip_domain": strip_domain,
                "device_count": len(validated_devices),
                "serial_rules": serial_rules_version(serial_rules),
            }
            cache.set(cache_metadata_key, cache_metadata, timeout=api.cache_timeout)

            # Maintain cache index for this server to enable listing active searches
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
