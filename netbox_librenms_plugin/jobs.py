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

    Jobs are triggered based on user-configured mode:
    - 'always': All filter operations run as background jobs
    - 'never': All operations run synchronously (no cancellation, browser may hang)
    - 'threshold': Jobs run when device count exceeds configured threshold

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

        Background job execution is controlled by plugin settings (always/never/threshold).
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

        # Store only metadata in job data (not the full device list)
        # Devices are retrieved via shared cache keys in _load_job_results
        self.job.data = {
            "device_ids": device_ids,
            "total_processed": len(validated_devices),
            "filters": filters,
            "server_key": server_key,
            "vc_detection_enabled": vc_detection_enabled,
            "cache_timeout": api.cache_timeout,
            "completed": True,
        }

        self.job.save(update_fields=["data"])

        self.logger.info(
            f"Job completed successfully. Processed {len(validated_devices)} devices. "
            f"Results available via shared cache for {api.cache_timeout} seconds."
        )
