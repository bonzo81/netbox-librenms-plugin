"""Virtual machine creation and import operations."""

import logging

from dcim.models import DeviceRole
from django.db import transaction
from django.utils import timezone
from virtualization.models import Cluster

from ..librenms_api import LibreNMSAPI
from .device_operations import _determine_device_name, fetch_device_with_cache, validate_device_for_import
from .permissions import require_permissions

logger = logging.getLogger(__name__)


def create_vm_from_librenms(
    libre_device: dict,
    validation: dict,
    server_key: str = "default",
    use_sysname: bool = True,
    strip_domain: bool = False,
    role=None,
):
    """
    Create a NetBox VirtualMachine from LibreNMS device data.

    Args:
        libre_device: Device data from LibreNMS
        validation: Validation result from validate_device_for_import with import_as_vm=True
        use_sysname: If True, prefer sysName; if False, use hostname
        server_key: LibreNMS server key used to store the librenms_id custom field

    Returns:
        Created VirtualMachine instance

    Raises:
        Exception if VM cannot be created
    """
    from virtualization.models import VirtualMachine

    if not validation["can_import"]:
        raise ValueError(f"VM cannot be imported: {', '.join(validation['issues'])}")

    # Extract matched objects from validation
    cluster = validation["cluster"]["cluster"]
    platform = validation["platform"].get("platform")
    role = role if role is not None else validation.get("device_role", {}).get("role")

    # Determine VM name - use pre-computed name if available (handles strip_domain),
    # falling back to the validated resolved_name before recomputing from raw fields.
    vm_name = libre_device.get("_computed_name") or validation.get("resolved_name")
    if not vm_name:
        vm_name = _determine_device_name(
            libre_device,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
            device_id=libre_device.get("device_id"),
        )

    # Generate import timestamp comment
    import_time = timezone.now().strftime("%Y-%m-%d %H:%M:%S %Z")

    # Validate device_id before creating the VM so a missing/invalid value
    # never leaves a VM without a librenms_id (partial persistence).
    raw_device_id = libre_device["device_id"]
    if isinstance(raw_device_id, bool):
        raise ValueError(f"device_id is a boolean ({raw_device_id!r}); expected an integer")
    librenms_device_id = int(raw_device_id)

    from ..utils import set_librenms_device_id

    # Create the VM and assign its LibreNMS ID atomically so a failure in
    # set_librenms_device_id never leaves a VM without a mapping.
    with transaction.atomic():
        vm = VirtualMachine.objects.create(
            name=vm_name,
            cluster=cluster,
            role=role,
            platform=platform,
            comments=f"Imported from LibreNMS (device_id={librenms_device_id}) by netbox-librenms-plugin on {import_time}",
        )
        set_librenms_device_id(vm, librenms_device_id, server_key)
        vm.save()

    logger.info(f"Created VM {vm.name} (ID: {vm.pk}) from LibreNMS device {libre_device['device_id']}")
    return vm


def bulk_import_vms(
    vm_imports: dict[int, dict[str, int]],
    api: LibreNMSAPI,
    sync_options: dict = None,
    libre_devices_cache: dict = None,
    job=None,
    user=None,
) -> dict:
    """
    Import multiple LibreNMS devices as VMs in NetBox.

    Handles validation, cluster/role assignment, name determination,
    and VM creation. Supports both synchronous and background job execution.

    This function consolidates VM import logic that was previously duplicated
    in BulkImportDevicesView and ImportDevicesJob, ensuring consistent behavior
    across synchronous and background import paths.

    Args:
        vm_imports: Dict mapping device_id to {"cluster_id": int, "device_role_id": int}
        api: LibreNMSAPI instance for device fetching
        sync_options: Optional dict with use_sysname, strip_domain settings
        libre_devices_cache: Optional pre-fetched device data cache
        job: Optional JobRunner instance for background job logging/cancellation
        user: User performing the import (for permission checks). If job is provided,
            user is extracted from job.job.user if not explicitly passed.

    Returns:
        Dict with keys:
            - success: List of {"device_id": int, "device": VM, "message": str}
            - failed: List of {"device_id": int, "error": str}
            - skipped: List of {"device_id": int, "reason": str}

    Raises:
        PermissionDenied: If user lacks required permissions

    Example:
        >>> # Synchronous import from view
        >>> vm_imports = {123: {"cluster_id": 5, "device_role_id": 2}}
        >>> result = bulk_import_vms(vm_imports, api, sync_options, user=request.user)
        >>> print(f"Created {len(result['success'])} VMs")
        >>>
        >>> # Background job import
        >>> result = bulk_import_vms(vm_imports, api, sync_options, cache, job=self)
    """
    from netbox_librenms_plugin.import_validation_helpers import (
        apply_cluster_to_validation,
        apply_role_to_validation,
    )

    # Extract user from job if not explicitly provided
    if user is None and job is not None:
        user = getattr(job.job, "user", None)

    # Check permissions at start of bulk operation
    require_permissions(user, ["virtualization.add_virtualmachine"], "import VMs")

    result = {"success": [], "failed": [], "skipped": []}
    vm_ids = list(vm_imports.keys())

    # Use job logger if available, otherwise standard logger
    log = job.logger if job else logger

    for idx, vm_id in enumerate(vm_ids, start=1):
        # Check for job cancellation before first VM and every 5 thereafter
        if job and (idx == 1 or idx % 5 == 0):
            cancelled = False
            try:
                from django_rq import get_queue
                from rq.job import Job as RQJob

                queue = get_queue("default")
                rq_job = RQJob.fetch(str(job.job.job_id), connection=queue.connection)
                if rq_job.is_failed or rq_job.is_stopped:
                    cancelled = True
            except Exception:
                job.job.refresh_from_db()
                job_status = job.job.status
                status_value = job_status.value if hasattr(job_status, "value") else job_status
                if status_value in ("failed", "errored", "stopped"):
                    cancelled = True
            if cancelled:
                log.warning(f"Job cancelled at VM {idx} of {len(vm_ids)}")
                break
            log.info(f"Processing VM {idx} of {len(vm_ids)}")

        try:
            # Fetch device data (uses cache helper)
            libre_device = fetch_device_with_cache(vm_id, api, api.server_key, libre_devices_cache)

            if not libre_device:
                result["failed"].append(
                    {
                        "device_id": vm_id,
                        "error": f"Device {vm_id} not found in LibreNMS",
                    }
                )
                log.error(f"Device {vm_id} not found in LibreNMS")
                continue

            # Validate as VM
            use_sysname_opt = sync_options.get("use_sysname", True) if sync_options else True
            strip_domain_opt = sync_options.get("strip_domain", False) if sync_options else False
            validation = validate_device_for_import(
                libre_device,
                import_as_vm=True,
                api=api,
                use_sysname=use_sysname_opt,
                strip_domain=strip_domain_opt,
                server_key=api.server_key,
            )

            # Check if VM already exists
            if validation.get("existing_device"):
                result["skipped"].append(
                    {
                        "device_id": vm_id,
                        "reason": f"VM already exists: {validation['existing_device'].name}",
                    }
                )
                log.info(f"VM already exists: {validation['existing_device'].name}")
                continue

            # Apply manual cluster and role selections
            vm_mappings = vm_imports[vm_id]
            cluster_id = vm_mappings.get("cluster_id")
            role_id = vm_mappings.get("device_role_id")

            if cluster_id:
                cluster = Cluster.objects.filter(id=cluster_id).first()
                if cluster:
                    apply_cluster_to_validation(validation, cluster)
                else:
                    result["failed"].append(
                        {"device_id": vm_id, "error": f"Selected cluster (id={cluster_id}) no longer exists"}
                    )
                    continue

            role = None
            if role_id:
                role = DeviceRole.objects.filter(id=role_id).first()
                if role:
                    apply_role_to_validation(validation, role, is_vm=True)
                else:
                    result["failed"].append(
                        {"device_id": vm_id, "error": f"Selected role (id={role_id}) no longer exists"}
                    )
                    continue

            # Determine VM name
            vm_name = _determine_device_name(
                libre_device,
                use_sysname=use_sysname_opt,
                strip_domain=strip_domain_opt,
                device_id=vm_id,
            )

            # Update validation with computed name
            libre_device["_computed_name"] = vm_name

            # Create VM
            vm = create_vm_from_librenms(
                libre_device,
                validation,
                use_sysname=use_sysname_opt,
                strip_domain=strip_domain_opt,
                server_key=api.server_key,
            )

            result["success"].append(
                {
                    "device_id": vm_id,
                    "device": vm,
                    "message": f"VM {vm.name} created successfully",
                }
            )
            log.info(f"Successfully imported VM {vm.name} (ID: {vm_id})")

        except Exception as vm_error:
            log.error(f"Failed to import VM {vm_id}: {vm_error}", exc_info=True)
            result["failed"].append({"device_id": vm_id, "error": str(vm_error)})

    return result
