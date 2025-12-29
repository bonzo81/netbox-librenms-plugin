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
        name = "LibreNMS Device Filter"

    def run(
        self,
        filters,
        vc_detection_enabled,
        clear_cache,
        show_disabled,
        exclude_existing=False,
        server_key=None,
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
        self.logger.info(
            f"LibreNMS API initialized (cache timeout: {api.cache_timeout}s)"
        )

        # Process filters using shared function
        validated_devices = process_device_filters(
            api=api,
            filters=filters,
            vc_detection_enabled=vc_detection_enabled,
            clear_cache=clear_cache,
            show_disabled=show_disabled,
            exclude_existing=exclude_existing,
            job=self,
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
            "server_key": server_key,
            "vc_detection_enabled": vc_detection_enabled,
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
            sync_options: Dict with sync_interfaces, sync_cables, sync_ips, use_sysname, strip_domain
            manual_mappings_per_device: Dict mapping device_id to manual_mappings dict
            libre_devices_cache: Optional dict mapping device_id to pre-fetched device data
            **kwargs: Additional job parameters
        """
        from netbox_librenms_plugin.import_utils import (
            bulk_import_devices_shared,
            create_vm_from_librenms,
            get_librenms_device_by_id,
            validate_device_for_import,
            _determine_device_name,
        )
        from netbox_librenms_plugin.import_validation_helpers import (
            apply_cluster_to_validation,
            apply_role_to_validation,
        )
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from dcim.models import DeviceRole
        from virtualization.models import Cluster
        from django.core.cache import cache

        total_count = len(device_ids) + len(vm_imports)
        self.logger.info(f"Starting LibreNMS import job for {total_count} devices/VMs")
        self.logger.info(f"Device imports: {len(device_ids)}, VM imports: {len(vm_imports)}")
        if server_key:
            self.logger.info(f"Using LibreNMS server: {server_key}")

        # Initialize API client
        api = LibreNMSAPI(server_key=server_key)

        # Import devices using shared function with job context
        device_result = {"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0}
        if device_ids:
            self.logger.info(f"Importing {len(device_ids)} devices...")
            device_result = bulk_import_devices_shared(
                device_ids=device_ids,
                server_key=server_key,
                sync_options=sync_options,
                manual_mappings_per_device=manual_mappings_per_device,
                libre_devices_cache=libre_devices_cache,
                job=self,  # Pass job context for logging and cancellation
            )

        # Import VMs
        vm_result = {"success": [], "failed": [], "skipped": []}
        if vm_imports:
            self.logger.info(f"Importing {len(vm_imports)} VMs...")
            vm_ids_to_import = list(vm_imports.keys())
            
            for idx, vm_id in enumerate(vm_ids_to_import, start=1):
                # Check for job cancellation every 5 VMs
                if idx % 5 == 0:
                    # Refresh job from DB to get current status
                    self.job.refresh_from_db()
                    job_status = self.job.status
                    status_value = job_status.value if hasattr(job_status, 'value') else job_status
                    if status_value in ('failed', 'errored'):
                        self.logger.warning(f"Import job cancelled at VM {idx} of {len(vm_ids_to_import)}")
                        break
                    self.logger.info(f"Imported VM {idx} of {len(vm_ids_to_import)}")

                try:
                    # Try to use cached device data first
                    libre_device = None
                    if libre_devices_cache and vm_id in libre_devices_cache:
                        libre_device = libre_devices_cache[vm_id]
                    else:
                        cache_key = f"import_device_data_{vm_id}"
                        libre_device = cache.get(cache_key)

                    if not libre_device:
                        libre_device = get_librenms_device_by_id(api, vm_id)

                    if not libre_device:
                        vm_result["failed"].append({
                            "device_id": vm_id,
                            "error": f"Device {vm_id} not found in LibreNMS",
                        })
                        self.logger.error(f"Device {vm_id} not found in LibreNMS")
                        continue

                    # Validate as VM
                    validation = validate_device_for_import(
                        libre_device, import_as_vm=True, api=api
                    )

                    # Check if VM already exists
                    if validation.get("existing_device"):
                        vm_result["skipped"].append({
                            "device_id": vm_id,
                            "reason": f"VM already exists: {validation['existing_device'].name}",
                        })
                        self.logger.info(f"VM already exists: {validation['existing_device'].name}")
                        continue

                    # Apply manual cluster and role selections
                    vm_mappings = vm_imports[vm_id]
                    cluster_id = vm_mappings.get("cluster_id")
                    role_id = vm_mappings.get("device_role_id")

                    if cluster_id:
                        cluster = Cluster.objects.filter(id=cluster_id).first()
                        if cluster:
                            apply_cluster_to_validation(validation, cluster)

                    role = None
                    if role_id:
                        role = DeviceRole.objects.filter(id=role_id).first()
                        if role:
                            apply_role_to_validation(validation, role, is_vm=True)

                    # Create the VM
                    use_sysname = sync_options.get("use_sysname", True) if sync_options else True
                    strip_domain = sync_options.get("strip_domain", False) if sync_options else False

                    vm_name = _determine_device_name(
                        libre_device,
                        use_sysname=use_sysname,
                        strip_domain=strip_domain,
                        device_id=vm_id,
                    )

                    # Update validation with the final name
                    libre_device["_computed_name"] = vm_name

                    vm = create_vm_from_librenms(
                        libre_device, validation, use_sysname=use_sysname, role=role
                    )

                    vm_result["success"].append({
                        "device_id": vm_id,
                        "device": vm,
                        "message": f"VM {vm.name} created successfully",
                    })
                    self.logger.info(f"Successfully imported VM {vm.name} (ID: {vm_id})")

                except Exception as vm_error:
                    # Log error but continue with other VMs
                    self.logger.error(f"Failed to import VM {vm_id}: {vm_error}", exc_info=True)
                    vm_result["failed"].append({"device_id": vm_id, "error": str(vm_error)})

        # Combine results
        imported_device_pks = [item["device"].pk for item in device_result.get("success", []) if item.get("device")]
        imported_vm_pks = [item["device"].pk for item in vm_result.get("success", []) if item.get("device")]
        
        # Also store LibreNMS device IDs for re-rendering table rows
        imported_libre_device_ids = [item["device_id"] for item in device_result.get("success", [])]
        imported_libre_vm_ids = [item["device_id"] for item in vm_result.get("success", [])]
        
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
            "server_key": server_key,
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
