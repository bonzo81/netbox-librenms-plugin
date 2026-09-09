"""
Background jobs for LibreNMS plugin.

This module provides background job implementations for long-running operations
such as device filtering with Virtual Chassis detection.
"""

import logging

from netbox.jobs import JobRunner

logger = logging.getLogger(__name__)


class FilterDevicesJob(JobRunner):
    """
    Background job for processing LibreNMS device filters with VC detection.

    Background jobs provide several benefits over synchronous processing:
    - Active cancellation via NetBox Jobs interface
    - Browser remains responsive (no "page loading" state)
    - Job progress tracked in NetBox Jobs table
    - Results persist in cache for later retrieval

    Users control background job execution via the "Run as background job" checkbox
    in the filter form. When enabled, the job runs asynchronously; when disabled,
    filtering runs synchronously.

    Note: Both synchronous and background processing complete once started,
    even if the user navigates away. The key difference is cancellation ability
    and browser responsiveness.

    Results are cached individually per device to avoid exceeding job data size limits.
    """

    class Meta:
        """Meta options for FilterDevicesJob."""

        name = "LibreNMS Device Filter"

    def run(
        self,
        filters,
        vc_detection_enabled,
        clear_cache,
        show_disabled,
        exclude_existing=False,
        server_key=None,
        use_sysname=True,
        strip_domain=False,
        **kwargs,
    ):
        """
        Execute filter processing in background.

        Logs job start, completion, and any early termination events.

        Args:
            filters: Dict with location, type, os, hostname, sysname keys
            vc_detection_enabled: Whether to detect virtual chassis
            clear_cache: Whether to force cache refresh
            show_disabled: Whether to include disabled devices
            exclude_existing: Whether to exclude devices that already exist in NetBox
            server_key: Optional LibreNMS server key for multi-server setups
            use_sysname: If True, prefer sysName over hostname for device name resolution
            strip_domain: If True, strip domain suffix from device names
            **kwargs: Additional job parameters
        """
        from netbox_librenms_plugin.import_utils import process_device_filters
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        self.logger.info("Starting LibreNMS device filter job")
        self.logger.info(f"Filters: {filters}")
        self.logger.info(f"VC detection: {vc_detection_enabled}")
        self.logger.info(f"Clear cache: {clear_cache}")
        self.logger.info(f"Show disabled: {show_disabled}")
        if exclude_existing:
            self.logger.info("Excluding existing devices")
        if server_key:
            self.logger.info(f"Using LibreNMS server: {server_key}")

        # Initialize API client
        api = LibreNMSAPI(server_key=server_key)
        self.logger.info(f"LibreNMS API initialized (cache timeout: {api.cache_timeout}s)")

        # Process filters using shared function
        validated_devices = process_device_filters(
            api=api,
            filters=filters,
            vc_detection_enabled=vc_detection_enabled,
            clear_cache=clear_cache,
            show_disabled=show_disabled,
            exclude_existing=exclude_existing,
            job=self,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
        )

        # Store device IDs for result retrieval
        # Note: Validated devices are cached with shared keys by process_device_filters
        device_ids = [device["device_id"] for device in validated_devices]

        # Track cache timestamp for frontend expiration warnings
        from datetime import datetime, timezone

        cached_at = datetime.now(timezone.utc).isoformat()

        # Store only metadata in job data (not the full device list)
        # Devices are retrieved via shared cache keys in _load_job_results
        self.job.data = {
            "device_ids": device_ids,
            "total_processed": len(validated_devices),
            "filters": filters,
            "server_key": api.server_key,
            "vc_detection_enabled": vc_detection_enabled,
            "use_sysname": use_sysname,
            "strip_domain": strip_domain,
            "cache_timeout": api.cache_timeout,
            "cached_at": cached_at,
            "completed": True,
        }

        self.job.save(update_fields=["data"])

        self.logger.info(
            f"Job completed successfully. Processed {len(validated_devices)} devices. "
            f"Results available via shared cache for {api.cache_timeout} seconds."
        )


class ImportDevicesJob(JobRunner):
    """
    Background job for importing LibreNMS devices to NetBox.

    Handles bulk device/VM imports in the background to keep browser responsive.
    Benefits:
    - Active cancellation via NetBox Jobs interface
    - Browser remains responsive during large imports
    - Job progress tracked with device count logging
    - Errors collected per device without stopping entire import

    Users control background job execution via the "Run as background job" checkbox
    in the import confirmation modal. When enabled, the job runs asynchronously;
    when disabled, imports run synchronously.

    Results stored in job.data with structure:
    {
        "imported_device_pks": [1, 2, 3],  # NetBox Device PKs
        "imported_vm_pks": [10, 11],       # NetBox VirtualMachine PKs
        "total": 5,
        "success_count": 4,
        "failed_count": 1,
        "skipped_count": 0,
        "errors": [{"device_id": 123, "error": "..."}]
    }
    """

    class Meta:
        """Meta options for ImportDevicesJob."""

        name = "LibreNMS Device Import"

    def run(
        self,
        device_ids,
        vm_imports,
        server_key=None,
        sync_options=None,
        manual_mappings_per_device=None,
        libre_devices_cache=None,
        **kwargs,
    ):
        """
        Execute device/VM imports in background.

        Args:
            device_ids: List of LibreNMS device IDs to import as Devices
            vm_imports: Dict mapping device_id to cluster/role info for VM imports
            server_key: Optional LibreNMS server key for multi-server setups
            sync_options: Dict with sync_interfaces, sync_cables,
                use_sysname, strip_domain, and vc_detection_enabled
            manual_mappings_per_device: Dict mapping device_id to manual_mappings dict
            libre_devices_cache: Optional dict mapping device_id to pre-fetched device data
            **kwargs: Additional job parameters
        """

        from netbox_librenms_plugin.import_utils import (
            bulk_import_devices_shared,
            classify_bulk_precheck,
            detect_collisions_for_device_ids,
            require_permissions,
            required_import_permissions,
        )
        from netbox_librenms_plugin.import_utils.bulk_import import _is_job_cancelled
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        total_count = len(device_ids) + len(vm_imports)
        self.logger.info(f"Starting LibreNMS import job for {total_count} devices/VMs")
        self.logger.info(f"Device imports: {len(device_ids)}, VM imports: {len(vm_imports)}")
        if server_key:
            self.logger.info(f"Using LibreNMS server: {server_key}")

        # Authorize BEFORE the collision pre-check: the scan below queries LibreNMS and
        # surfaces collision details (NetBox pks) in the job output, while the
        # require_permissions calls inside bulk_import_devices_shared / bulk_import_vms
        # only run after it. The job executes outside any view's permission gate, so a
        # submitter whose import rights were revoked after enqueueing must be rejected
        # here, with the same standalone helper and perm sets the import paths enforce.
        required_permissions = required_import_permissions(device_ids, vm_imports)
        if required_permissions:
            require_permissions(self.job.user, required_permissions, "import devices and VMs")

        # Initialize API client
        api = LibreNMSAPI(server_key=server_key)

        # Import devices using shared function with job context
        device_result = {
            "success": [],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
        }
        # Set to the block reason when the device collision/unresolved gate fires, so the VM
        # section below skips too — the synchronous view returns before importing ANY of the
        # submitted batch, and the async path must not partially import the same batch's VMs.
        batch_blocked_msg = None
        # Collision-check the WHOLE submitted batch — device imports AND VM imports — exactly like
        # the synchronous view, which passes its full parsed id set (devices + VMs) to the same
        # gate. device_ids here excludes VM rows (split out upstream), so checking it alone would
        # let a VM-only batch, or a collision involving a VM row, slip through to bulk_import_vms().
        collision_check_ids = list(dict.fromkeys([*device_ids, *vm_imports]))
        # The shared block/skip decision (classify_bulk_precheck); None until the pre-check runs and
        # consulted by the VM section below so devices and VMs apply the SAME skip set.
        precheck_outcome = None
        skipped_id_set = set()
        if collision_check_ids:
            # Defense-in-depth: block a batch where two LibreNMS rows resolve to the same NetBox
            # device, mirroring the confirm-preview/sync-view gate so the async path can't import a
            # colliding batch either. A single row can never collide, so skip the extra pass.
            collisions, unresolved = (
                detect_collisions_for_device_ids(
                    collision_check_ids,
                    api,
                    libre_devices_cache=libre_devices_cache,
                    sync_options=sync_options,
                    # Job context so a cancellation stops the scan itself — without it, a large
                    # cache-miss batch keeps issuing LibreNMS calls until the whole pre-check
                    # finishes and only the import loops below would honor the cancel.
                    job=self,
                    # Each row validates in its actual import mode: a VM row checked in Device
                    # mode would run the serial/IP matching bulk_import_vms skips and could
                    # fabricate a collision that blocks a valid batch.
                    vm_device_ids=vm_imports,
                    user=self.job.user,
                )
                if len(collision_check_ids) >= 2
                else ([], [])
            )
            if unresolved and _is_job_cancelled(self):
                # A cancelled pre-check returns its unscanned remainder as unresolved. Cancellation
                # is a hard stop (the user asked to stop), so fail the whole batch closed rather than
                # skip-and-import the scanned portion — and report it as the cancellation it is.
                ids = ", ".join(str(d) for d in unresolved)
                msg = (
                    f"Import cancelled during the collision pre-check; {len(unresolved)} "
                    f"row(s) (id(s): {ids}) were not checked and nothing was imported."
                )
                self.logger.error(msg)
                device_result["failed"] = [{"device_id": device_id, "error": msg} for device_id in device_ids]
                batch_blocked_msg = msg
            else:
                # Shared decision, identical to the sync view: genuine collisions block the whole
                # batch; rows that couldn't be collision-checked are SKIPPED (not a whole-batch
                # block) so a transient miss on one row doesn't drop the entire import.
                precheck_outcome = classify_bulk_precheck(collisions, unresolved, device_ids, vm_imports)
                skipped_id_set = set(precheck_outcome.skipped_ids)
                if precheck_outcome.blocked:
                    self.logger.error(precheck_outcome.block_message)
                    device_result["failed"] = [
                        {"device_id": device_id, "error": precheck_outcome.block_message} for device_id in device_ids
                    ]
                    batch_blocked_msg = precheck_outcome.block_message
                else:
                    if precheck_outcome.importable_device_ids:
                        # Clean device rows — import them. (A VM-only batch has none; its VMs are
                        # handled in the vm_imports block below.)
                        self.logger.info(f"Importing {len(precheck_outcome.importable_device_ids)} devices...")
                        device_result = bulk_import_devices_shared(
                            device_ids=precheck_outcome.importable_device_ids,
                            server_key=api.server_key,
                            sync_options=sync_options,
                            manual_mappings_per_device=manual_mappings_per_device,
                            libre_devices_cache=libre_devices_cache,
                            job=self,  # Pass job context for logging and cancellation
                            user=self.job.user,  # Pass user for permission checks
                        )
                    skipped_device_ids = [d for d in device_ids if d in skipped_id_set]
                    if skipped_device_ids:
                        self.logger.warning(precheck_outcome.skip_message)
                        device_result.setdefault("failed", []).extend(
                            {"device_id": device_id, "error": precheck_outcome.skip_message}
                            for device_id in skipped_device_ids
                        )

        # Import VMs
        vm_result = {"success": [], "failed": [], "skipped": []}
        if vm_imports:
            if batch_blocked_msg:
                # A genuine collision (or a cancellation) blocked this submission — fail the same
                # batch's VMs closed with the block reason rather than partially importing them.
                self.logger.error(f"Skipping {len(vm_imports)} VM import(s); batch blocked: {batch_blocked_msg}")
                vm_result["failed"] = [{"device_id": device_id, "error": batch_blocked_msg} for device_id in vm_imports]
            else:
                # Apply the same skip set to VMs: import the collision-checked VM rows, skip the
                # unresolved ones (surfaced as failures with the shared message).
                importable_vm_imports = precheck_outcome.importable_vm_imports if precheck_outcome else vm_imports
                skipped_vm_ids = [d for d in vm_imports if d in skipped_id_set] if precheck_outcome else []
                if importable_vm_imports:
                    self.logger.info(f"Importing {len(importable_vm_imports)} VMs...")
                    from netbox_librenms_plugin.import_utils import bulk_import_vms

                    vm_result = bulk_import_vms(
                        importable_vm_imports, api, sync_options, libre_devices_cache, job=self, user=self.job.user
                    )
                if skipped_vm_ids:
                    self.logger.warning(precheck_outcome.skip_message)
                    vm_result.setdefault("failed", []).extend(
                        {"device_id": device_id, "error": precheck_outcome.skip_message} for device_id in skipped_vm_ids
                    )

        # Combine results — partition device_result successes by model type since
        # bulk_import_devices_shared() may return VirtualMachine objects when import_as_vm=True.
        device_successes = []
        vm_successes = list(vm_result.get("success", []))
        for item in device_result.get("success", []):
            obj = item.get("device")
            if not obj:
                continue
            if obj._meta.model_name == "virtualmachine":
                vm_successes.append(item)
            else:
                device_successes.append(item)

        imported_device_pks = [item["device"].pk for item in device_successes]
        imported_vm_pks = [item["device"].pk for item in vm_successes]

        # Also store LibreNMS device IDs for re-rendering table rows
        imported_libre_device_ids = [item["device_id"] for item in device_successes]
        imported_libre_vm_ids = [item["device_id"] for item in vm_successes]

        success_count = len(device_result.get("success", [])) + len(vm_result.get("success", []))
        failed_count = len(device_result.get("failed", [])) + len(vm_result.get("failed", []))
        skipped_count = len(device_result.get("skipped", [])) + len(vm_result.get("skipped", []))

        all_errors = device_result.get("failed", []) + vm_result.get("failed", [])

        # Store results in job.data
        self.job.data = {
            "imported_device_pks": imported_device_pks,
            "imported_vm_pks": imported_vm_pks,
            "imported_libre_device_ids": imported_libre_device_ids,
            "imported_libre_vm_ids": imported_libre_vm_ids,
            "server_key": api.server_key,
            "total": total_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "virtual_chassis_created": device_result.get("virtual_chassis_created", 0),
            "errors": all_errors,
            "completed": True,
        }
        self.job.save(update_fields=["data"])

        self.logger.info(
            f"Import job completed. Success: {success_count}, Failed: {failed_count}, Skipped: {skipped_count}"
        )
