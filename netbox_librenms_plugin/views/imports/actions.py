"""HTMX endpoints and POST handlers for importing LibreNMS devices."""

import logging

from django.contrib import messages
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from netbox_librenms_plugin.import_utils import (
    _determine_device_name,
    bulk_import_devices,
    bulk_import_vms,
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
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

logger = logging.getLogger(__name__)


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
        vc_requested = request.GET.get("enable_vc_detection") == "true"

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

    def get_validated_device_with_selections(
        self, device_id: int, request
    ) -> tuple[dict | None, dict | None, dict]:
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

        validation = validate_device_for_import(
            libre_device,
            import_as_vm=is_vm,
            api=self.librenms_api if enable_vc else None,
            include_vc_detection=enable_vc,
        )
        validation["import_as_vm"] = is_vm

        # Apply user selections (cluster, role, rack) to validation
        _apply_user_selections_to_validation(validation, selections, is_vm)

        return libre_device, validation, selections

    def render_device_row(
        self, request, libre_device: dict, validation: dict, selections: dict
    ):
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

        return render(
            request,
            "netbox_librenms_plugin/htmx/device_import_row.html",
            context,
        )


def _apply_user_selections_to_validation(
    validation: dict,
    selections: dict,
    is_vm: bool,
) -> None:
    """Apply user-selected cluster, role, and rack to validation dict.

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


class BulkImportConfirmView(LibreNMSAPIMixin, View):
    """HTMX view to confirm bulk imports before execution."""

    def post(self, request):
        device_ids = request.POST.getlist("select")
        if not device_ids:
            return HttpResponse(
                '<div class="alert alert-warning mb-0">Select at least one device.</div>',
                status=400,
            )

        use_sysname = request.POST.get("use-sysname-toggle") == "on"
        strip_domain = request.POST.get("strip-domain-toggle") == "on"

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
                libre_device, import_as_vm=is_vm, api=self.librenms_api
            )

            # Mark validation with VC detection flag for proper URL generation in table
            # Bulk confirm should respect the initial filter's VC detection preference
            vc_requested = request.GET.get("enable_vc_detection") == "true"
            validation["_vc_detection_enabled"] = vc_requested

            device_name = _determine_device_name(
                libre_device,
                use_sysname=use_sysname,
                strip_domain=strip_domain,
                device_id=device_id,
            )

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
                    f"{len(errors)} error(s) occurred: {' '.join(errors) if errors else 'Please check device validation status.'}"
                    "</div>",
                    status=400,
                )

        context = {
            "devices": devices,
            "device_count": len(devices),
            "errors": errors,
            "use_sysname": use_sysname,
            "strip_domain": strip_domain,
        }

        return render(
            request,
            "netbox_librenms_plugin/htmx/bulk_import_confirm.html",
            context,
        )


class BulkImportDevicesView(LibreNMSAPIMixin, View):
    """Handle bulk import requests coming from the LibreNMS import table."""

    def should_use_background_job_for_import(self, request):
        """
        Determine if import operation should run as background job.

        Import jobs provide active cancellation and keep the browser responsive
        during bulk imports.

        Args:
            request: Django request object containing POST data

        Returns:
            bool: True if background job should be used, False for synchronous
        """
        return request.POST.get("use_background_job") == "on"

    def post(self, request):  # noqa: PLR0912 - branching keeps responses explicit
        device_ids = request.POST.getlist("select")
        if not device_ids:
            messages.error(request, "No devices selected for import")
            return HttpResponse("No devices selected", status=400)

        try:
            parsed_ids = [int(device_id) for device_id in device_ids]
        except (TypeError, ValueError):
            messages.error(request, "Invalid device identifier supplied")
            return HttpResponse("Invalid device identifier", status=400)

        sync_options = {
            "sync_interfaces": request.POST.get("sync_interfaces") == "on",
            "sync_cables": request.POST.get("sync_cables") == "on",
            "sync_ips": request.POST.get("sync_ips") == "on",
            "use_sysname": request.POST.get("use_sysname", "true") == "true",
            "strip_domain": request.POST.get("strip_domain", "false") == "true",
        }

        manual_mappings_per_device: dict[int, dict[str, int]] = {}
        vm_imports: dict[
            int, dict[str, int]
        ] = {}  # Track which devices to import as VMs

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
                        headers={
                            "HX-Redirect": "/plugins/librenms_plugin/librenms-import/"
                        },
                    )
                else:
                    return redirect("plugins:netbox_librenms_plugin:librenms_import")
            else:
                # No workers available - warn user and proceed synchronously
                logger.warning(
                    "No RQ workers available for import job, falling back to synchronous import"
                )
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
                )

            # Import VMs if any
            if vm_ids_to_import:
                vm_result = bulk_import_vms(
                    vm_imports,
                    self.librenms_api,
                    sync_options,
                    libre_devices_cache_sync,
                )

        except Exception as exc:  # pragma: no cover - defensive guard
            logger.exception("Error during bulk import")
            if request.headers.get("HX-Request"):
                return HttpResponse(str(exc), status=500)
            messages.error(request, f"Bulk import failed: {exc}")
            return redirect("plugins:netbox_librenms_plugin:librenms_import")

        # Combine results
        success_count = len(device_result.get("success", [])) + len(
            vm_result.get("success", [])
        )
        failed_count = len(device_result.get("failed", [])) + len(
            vm_result.get("failed", [])
        )
        skipped_count = len(device_result.get("skipped", [])) + len(
            vm_result.get("skipped", [])
        )

        if success_count:
            messages.success(
                request,
                f"Successfully imported {success_count} LibreNMS device"
                f"{'s' if success_count != 1 else ''}",
            )
        if failed_count:
            messages.error(
                request,
                f"Failed to import {failed_count} device"
                f"{'s' if failed_count != 1 else ''}",
            )
        if skipped_count:
            messages.warning(
                request,
                f"Skipped {skipped_count} existing device"
                f"{'s' if skipped_count != 1 else ''}",
            )

        if request.headers.get("HX-Request"):
            # Return updated rows for all imported devices using HTMX OOB swaps
            # This updates only the affected rows instead of refreshing the entire table
            updated_rows_html = []

            # Collect all successfully imported device IDs (devices + VMs)
            imported_device_ids = [
                item["device_id"] for item in device_result.get("success", [])
            ] + [item["device_id"] for item in vm_result.get("success", [])]

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
                    is_vm = device_id in [
                        item["device_id"] for item in vm_result.get("success", [])
                    ]

                    # Re-validate with fresh status (will now show as imported)
                    validation = validate_device_for_import(
                        libre_device,
                        import_as_vm=is_vm,
                        api=None,  # No VC detection needed for already-imported devices
                        include_vc_detection=False,
                    )
                    validation["import_as_vm"] = is_vm

                    # Update cache with fresh validation
                    libre_device["_validation"] = validation
                    cache_key = get_import_device_cache_key(
                        device_id, self.librenms_api.server_key
                    )
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
            return HttpResponse(
                "\n".join(updated_rows_html),
                headers={"HX-Trigger": '{"closeModal": null}'},
            )

        return redirect("plugins:netbox_librenms_plugin:librenms_import")


class DeviceVCDetailsView(LibreNMSAPIMixin, View):
    """HTMX view to show virtual chassis details."""

    def get(self, request, device_id):
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


class DeviceValidationDetailsView(LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to show detailed validation information."""

    def get(self, request, device_id):
        libre_device, validation, selections = (
            self.get_validated_device_with_selections(device_id, request)
        )

        if not libre_device:
            return HttpResponse(
                '<div class="alert alert-danger">Device not found in LibreNMS</div>',
                status=404,
            )

        context = {
            "libre_device": libre_device,
            "validation": validation,
        }

        return render(
            request,
            "netbox_librenms_plugin/htmx/device_validation_details.html",
            context,
        )


class DeviceRoleUpdateView(LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to update a table row when a role is selected."""

    def post(self, request, device_id):
        libre_device, validation, selections = (
            self.get_validated_device_with_selections(device_id, request)
        )

        if not libre_device:
            return HttpResponse("Device not found", status=404)

        return self.render_device_row(request, libre_device, validation, selections)


class DeviceClusterUpdateView(LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to update a table row when a cluster is selected/deselected."""

    def post(self, request, device_id):
        libre_device, validation, selections = (
            self.get_validated_device_with_selections(device_id, request)
        )

        if not libre_device:
            return HttpResponse("Device not found", status=404)

        return self.render_device_row(request, libre_device, validation, selections)


class DeviceRackUpdateView(LibreNMSAPIMixin, DeviceImportHelperMixin, View):
    """HTMX view to update a table row when a rack is selected."""

    def post(self, request, device_id):
        libre_device, validation, selections = (
            self.get_validated_device_with_selections(device_id, request)
        )

        if not libre_device:
            return HttpResponse("Device not found", status=404)

        return self.render_device_row(request, libre_device, validation, selections)
