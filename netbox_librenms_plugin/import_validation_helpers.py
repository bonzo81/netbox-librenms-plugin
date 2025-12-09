"""Helper functions for validation state mutation during import workflow.

These functions centralize the logic for updating validation dictionaries
when users select roles, clusters, or racks during the device import process.
"""

import logging

logger = logging.getLogger(__name__)


def fetch_model_by_id(model_class, pk):
    """
    Generic helper to fetch a model instance by primary key.

    Args:
        model_class: Django model class (e.g., DeviceRole, Cluster, Rack)
        pk: Primary key value (int, str, or None)

    Returns:
        Model instance if found and valid, None otherwise

    Example:
        >>> from dcim.models import DeviceRole
        >>> role = fetch_model_by_id(DeviceRole, "5")
        >>> role.name
        'Router'
    """
    if pk is None:
        return None

    try:
        return model_class.objects.get(pk=int(pk))
    except (model_class.DoesNotExist, ValueError, TypeError):
        return None


def extract_device_selections(request, device_id):
    """
    Extract cluster, role, and rack selections from request POST/GET data.

    Args:
        request: Django request object
        device_id: LibreNMS device ID

    Returns:
        dict with keys: cluster_id, role_id, rack_id (all may be None)

    Example:
        >>> selections = extract_device_selections(request, 1234)
        >>> selections
        {'cluster_id': None, 'role_id': '5', 'rack_id': '12'}
    """
    # Check both POST and GET data (different views use different methods)
    data_source = request.POST if request.method == "POST" else request.GET

    return {
        "cluster_id": data_source.get(f"cluster_{device_id}"),
        "role_id": data_source.get(f"role_{device_id}"),
        "rack_id": data_source.get(f"rack_{device_id}"),
    }


def apply_role_to_validation(validation: dict, role, is_vm: bool = False) -> None:
    """
    Update validation state after device/VM role selection.

    Args:
        validation: Validation dict from validate_device_for_import()
        role: DeviceRole instance selected by user
        is_vm: True if importing as VM, False for device

    Modifies validation dict in-place:
        - Sets device_role["found"] = True
        - Sets device_role["role"] = role
        - Removes "role" related issues
        - Recalculates can_import and is_ready flags
    """
    validation["device_role"]["found"] = True
    validation["device_role"]["role"] = role
    remove_validation_issue(validation, "role")
    recalculate_validation_status(validation, is_vm)


def apply_cluster_to_validation(validation: dict, cluster) -> None:
    """
    Update validation state after cluster selection (VM import only).

    Args:
        validation: Validation dict from validate_device_for_import()
        cluster: Cluster instance selected by user

    Modifies validation dict in-place:
        - Sets cluster["found"] = True
        - Sets cluster["cluster"] = cluster
        - Removes "cluster" related issues
        - Recalculates can_import and is_ready flags (as VM)
    """
    validation["cluster"]["found"] = True
    validation["cluster"]["cluster"] = cluster
    remove_validation_issue(validation, "cluster")
    recalculate_validation_status(validation, is_vm=True)


def apply_rack_to_validation(validation: dict, rack) -> None:
    """
    Update validation state after rack selection (device import only).

    Args:
        validation: Validation dict from validate_device_for_import()
        rack: Rack instance selected by user

    Modifies validation dict in-place:
        - Sets rack["found"] = True
        - Sets rack["rack"] = rack

    Note: Rack is optional, so this doesn't affect can_import/is_ready.
    """
    validation.setdefault("rack", {})
    validation["rack"]["found"] = True
    validation["rack"]["rack"] = rack


def remove_validation_issue(validation: dict, keyword: str) -> None:
    """
    Remove validation issues containing the specified keyword.

    Args:
        validation: Validation dict
        keyword: Keyword to search for in issue messages (case-insensitive)

    Example:
        >>> remove_validation_issue(validation, "role")
        # Removes "Device role must be manually selected before import"
    """
    validation["issues"] = [
        issue for issue in validation["issues"] if keyword.lower() not in issue.lower()
    ]


def recalculate_validation_status(validation: dict, is_vm: bool = False) -> None:
    """
    Recalculate can_import and is_ready flags based on current validation state.

    Args:
        validation: Validation dict
        is_vm: True if importing as VM, False for device

    Updates:
        - can_import: True if no blocking issues remain
        - is_ready: True if can_import AND all required fields are found

    Required fields for devices:
        - site, device_type, device_role

    Required fields for VMs:
        - cluster
    """
    validation["can_import"] = len(validation["issues"]) == 0

    if is_vm:
        validation["is_ready"] = (
            validation["can_import"] and validation["cluster"]["found"]
        )
    else:
        validation["is_ready"] = (
            validation["can_import"]
            and validation["site"]["found"]
            and validation["device_type"]["found"]
            and validation["device_role"]["found"]
        )
