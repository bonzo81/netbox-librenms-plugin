"""
Utilities for importing devices from LibreNMS to NetBox.

This package provides functions for:
- Validating LibreNMS devices for import
- Retrieving filtered LibreNMS devices
- Importing single and multiple devices
- Smart matching of NetBox objects
- Permission checking for import operations
- Virtual chassis detection and creation

All imports below are intentional re-exports so that existing callers
can continue using ``from netbox_librenms_plugin.import_utils import X``.
The F401 suppressions prevent linters from flagging them as unused.
"""

from .bulk_import import (  # noqa: F401
    bulk_import_devices,
    bulk_import_devices_shared,
    process_device_filters,
)
from .cache import (  # noqa: F401
    get_active_cached_searches,
    get_cache_metadata_key,
    get_import_device_cache_key,
    get_validated_device_cache_key,
)
from .device_operations import (  # noqa: F401
    _determine_device_name,
    fetch_device_with_cache,
    get_librenms_device_by_id,
    import_single_device,
    validate_device_for_import,
)
from .filters import (  # noqa: F401
    _apply_client_filters,
    get_device_count_for_filters,
    get_librenms_devices_for_import,
)
from .permissions import check_user_permissions, require_permissions  # noqa: F401
from .virtual_chassis import (  # noqa: F401
    _clone_virtual_chassis_data,
    _generate_vc_member_name,
    _vc_cache_key,
    create_virtual_chassis_with_members,
    detect_virtual_chassis_from_inventory,
    empty_virtual_chassis_data,
    get_virtual_chassis_data,
    prefetch_vc_data_for_devices,
    update_vc_member_suggested_names,
)
from .vm_operations import bulk_import_vms, create_vm_from_librenms  # noqa: F401
