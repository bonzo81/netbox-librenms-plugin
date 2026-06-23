"""
Helper functions for validation state mutation during import workflow.

These functions centralize the logic for updating validation dictionaries
when users select roles, clusters, or racks during the device import process.
"""

import logging

logger = logging.getLogger(__name__)

# The two slots a Stage-2 merge_candidates payload carries (the hostname-matched device and the
# serial-matched device). Centralised so the merge view, collision detection, and any future
# reader agree on the slot names rather than each hard-coding the strings.
MERGE_CANDIDATE_SLOTS = ("host_named", "oob_named")


def merge_candidate_pks(validation: dict) -> set:
    """
    Return the set of NetBox device pks a row's ``merge_candidates`` would touch.

    Args:
        validation (dict): A per-row validation dict (or None).

    Returns:
        set: The non-None ``pk`` values from the ``host_named`` / ``oob_named`` slots.
    """
    merge = (validation or {}).get("merge_candidates") or {}
    if not isinstance(merge, dict):
        return set()
    pks = set()
    for slot in MERGE_CANDIDATE_SLOTS:
        entry = merge.get(slot) or {}
        pk = entry.get("pk") if isinstance(entry, dict) else None
        if pk is not None:
            pks.add(pk)
    return pks


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
    validation["issues"] = [issue for issue in validation["issues"] if keyword.lower() not in issue.lower()]


def apply_oob_detection_result(
    result: dict,
    *,
    serial_action: "str | None",
    oob_candidate: "dict | None",
    promote_to_host: "dict | None",
    serial_role_choice_available: bool,
    warnings: "list | None" = None,
) -> None:
    """Apply OOB/promote-to-host serial detection results to the validation dict.

    Call this after computing all OOB/promote-to-host flags from the LibreNMS
    and NetBox data.  All mutations to ``result["oob_candidate"]``,
    ``result["promote_to_host"]``, ``result["serial_action"]``,
    ``result["serial_role_choice_available"]``, and their associated warnings
    are routed through here so the mutation pattern stays consistent and
    testable independently of the DB-heavy computation in device_operations.

    Args:
        result: Validation dict produced by validate_device_for_import()
        serial_action: The resolved action string, or None
        oob_candidate: Dict {device, type, version, ip} when OOB role is available
        promote_to_host: Dict {existing_libre_id, existing_oob_type, existing_device}
            when host-promotion is available
        serial_role_choice_available: True when both oob_candidate and
            promote_to_host are feasible and the UI should offer a toggle
        warnings: Optional list of warning strings to append to result["warnings"]
    """
    result["serial_action"] = serial_action
    result["oob_candidate"] = oob_candidate
    # Honor the "absent otherwise" contract: only carry promote_to_host when a real
    # promotion target exists, clearing any stale key rather than storing a None sentinel.
    if promote_to_host is None:
        result.pop("promote_to_host", None)
    else:
        result["promote_to_host"] = promote_to_host
    result["serial_role_choice_available"] = serial_role_choice_available
    # Clear merge-only state: this is the non-merge path, so if the same result
    # dict was previously marked a merge candidate, the stale merge UI data must
    # not linger (apply_merge_candidates is the only writer of merge_candidates).
    result["merge_candidates"] = None
    result.setdefault("warnings", [])
    for warning in warnings or []:
        result["warnings"].append(warning)


def apply_merge_candidates(
    result: dict,
    *,
    host_named: dict,
    oob_named: dict,
    warning: str,
) -> None:
    """Apply merge-candidates detection results to the validation dict.

    Called when the hostname-matched and serial-matched NetBox devices are
    different objects and at least one already has a LibreNMS linkage,
    indicating they likely represent the two sides of a single physical box.

    Sets ``serial_action`` to ``"merge_netbox_devices"``, populates
    ``merge_candidates``, sets ``can_import`` to False, and appends the
    supplied warning so callers do not need to know the dict shape.

    Args:
        result: Validation dict produced by validate_device_for_import()
        host_named: Dict {pk, name, librenms_link} for the hostname-matched device
        oob_named: Dict {pk, name, librenms_link} for the serial-matched device
        warning: Warning string describing the merge situation
    """
    result["serial_action"] = "merge_netbox_devices"
    result["merge_candidates"] = {
        "host_named": host_named,
        "oob_named": oob_named,
    }
    result["can_import"] = False
    # Keep readiness in lockstep with can_import: an earlier path (e.g. hostname-first row
    # processing) may have set is_ready=True, which would otherwise leave contradictory state
    # (is_ready=True while merge mode blocks import).
    result["is_ready"] = False
    result["oob_candidate"] = None
    # Clear earlier serial-conflict state so the merge path is the single source of truth:
    # a hostname-first row may have already set serial_duplicate / serial_confirmed, which
    # would otherwise leave a stale "serial conflict" signal alongside "merge these devices".
    result["serial_duplicate"] = False
    result["serial_confirmed"] = False
    # "absent otherwise" contract — the merge path has no promotion target.
    result.pop("promote_to_host", None)
    result["serial_role_choice_available"] = False
    # Merge supersedes the serial/hostname-detection signals that ran earlier in this
    # validation pass, so their warnings (e.g. "hostname differs", "already has an OOB
    # controller linked", "serial conflict") would now contradict the merge guidance.
    # Reset to just the merge warning; later validation stages (role/platform/cluster)
    # append their own warnings after this point, so nothing actionable is lost.
    result["warnings"] = [warning]


def clear_match_derived_action_fields(validation: dict) -> None:
    """
    Reset the action/name fields derived from a hostname/serial match.

    Shared by ``device_operations``' terminal-ambiguity teardown and
    ``bulk_import._clear_existing_match_derived_fields`` so the two clearing
    paths can't drift on the common field set. Context-specific state
    (match-type demotion, linkage, migration/device-type flags) stays with
    the callers.

    ``promote_to_host`` follows the "absent otherwise" contract (see
    :func:`apply_oob_detection_result`) and is popped; ``merge_candidates``
    is always present, defaulting to ``None``, and is reset in place.
    """
    validation["serial_action"] = None
    validation["oob_candidate"] = None
    validation["serial_confirmed"] = False
    validation["serial_duplicate"] = False
    validation["serial_role_choice_available"] = False
    validation["name_matches"] = False
    validation["name_sync_available"] = False
    validation["suggested_name"] = None
    validation.pop("promote_to_host", None)
    validation["merge_candidates"] = None


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
    # Merge mode is a hard block that does not live in ``issues`` — apply_merge_candidates()
    # sets can_import=False directly. Without this guard a later mutation (e.g. applying a role
    # selection, which calls back here) would recompute can_import purely from ``issues`` and
    # silently re-enable importing a merge-candidate row, bypassing the merge resolution.
    if validation.get("serial_action") == "merge_netbox_devices" or validation.get("merge_candidates"):
        validation["can_import"] = False
        validation["is_ready"] = False
        return

    validation["can_import"] = len(validation["issues"]) == 0

    if is_vm:
        validation["is_ready"] = validation["can_import"] and validation["cluster"]["found"]
    else:
        validation["is_ready"] = (
            validation["can_import"]
            and validation["site"]["found"]
            and validation["device_type"]["found"]
            and validation["device_role"]["found"]
        )
