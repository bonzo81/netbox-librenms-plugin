import logging
import re
from typing import Optional

from dcim.models import Device
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.http import HttpRequest
from netbox.config import get_config
from netbox.plugins import get_plugin_config
from utilities.paginator import get_paginate_count as netbox_get_paginate_count

logger = logging.getLogger(__name__)


try:
    from netbox_librenms_plugin.models import PlatformMapping
except ImportError:
    PlatformMapping = None  # type: ignore[assignment]


def convert_speed_to_kbps(speed_bps: int) -> int | None:
    """
    Convert speed from bits per second to kilobits per second.

    Args:
        speed_bps (int): Speed in bits per second.

    Returns:
        int: Speed in kilobits per second.
    """
    if speed_bps is None:
        return None
    return speed_bps // 1000


def format_mac_address(mac_address: str) -> str:
    """
    Validate and format MAC address string for table display.

    Args:
        mac_address (str): The MAC address string to format.

    Returns:
        str: The MAC address formatted as XX:XX:XX:XX:XX:XX.
    """
    if not mac_address:
        return ""

    mac_address = mac_address.strip().replace(":", "").replace("-", "")

    if len(mac_address) != 12:
        return "Invalid MAC Address"  # Return a message if the address is not valid

    formatted_mac = ":".join(mac_address[i : i + 2] for i in range(0, len(mac_address), 2))
    return formatted_mac.upper()


def get_virtual_chassis_member(device: Device, port_name: str) -> Device:
    """
    Determines the likely virtual chassis member based on the device's vc_position and port name.

    Args:
        device (Device): The NetBox device instance.
        port_name (str): The name of the port (e.g., 'Ethernet1').

    Returns:
        Device: The virtual chassis member device corresponding to the port.
                Returns the original device if not part of a virtual chassis or if matching fails.
    """
    if not hasattr(device, "virtual_chassis") or not device.virtual_chassis:
        return device

    try:
        match = re.match(r"^[A-Za-z]+(\d+)", port_name)
        if not match:
            return device

        # Get the port number and use it
        vc_position = int(match.group(1))
        return device.virtual_chassis.members.get(vc_position=vc_position)
    except (re.error, ValueError, ObjectDoesNotExist):
        return device


def get_librenms_sync_device(device: Device, server_key: str = None) -> Optional[Device]:
    """
    Determine which Virtual Chassis member should handle LibreNMS sync operations.

    LibreNMS treats a Virtual Chassis as a single logical device, so only one member
    should have the librenms_id custom field set and be used for sync operations.

    Priority order for selecting the sync device:
    1. Any member with librenms_id custom field set for *server_key* (highest priority).
       When *server_key* is None, matches any member that has any librenms_id set.
    2. Master device with primary IP (if master is designated)
    3. Any member with primary IP (fallback when no master or master lacks IP)
    4. Member with lowest vc_position (for error messages when no IPs configured)

    Args:
        device (Device): Any device in the virtual chassis.
        server_key: LibreNMS server key used to resolve the correct librenms_id mapping.
                    Pass None to match any member that has any librenms_id (e.g. in
                    contexts where the active server is not known, such as table columns).

    Returns:
        Optional[Device]: The device that should handle LibreNMS sync, or None if
                         the device is not in a virtual chassis.
    """
    if not hasattr(device, "virtual_chassis") or not device.virtual_chassis:
        return device

    vc = device.virtual_chassis
    all_members = vc.members.all()

    if server_key is not None:
        # Priority 1: Prefer member with an explicit per-server dict mapping for server_key.
        # This ensures a migrated device is preferred over one with a legacy bare-int ID.
        for member in all_members:
            raw_cf = member.cf.get("librenms_id")
            if isinstance(raw_cf, dict):
                val = raw_cf.get(server_key)
                if val is not None and not isinstance(val, bool):
                    return member

        # Priority 2 (legacy fallback): Any member whose librenms_id resolves for this server
        # (includes bare-int legacy IDs that are a universal fallback).
        for member in all_members:
            result = get_librenms_device_id(member, server_key, auto_save=False)
            if result:
                return member
    else:
        # server_key is None: match any member that has any librenms_id set (any server).
        # Used in contexts without an active server (e.g. device status table columns).
        for member in all_members:
            raw_cf = member.cf.get("librenms_id")
            if isinstance(raw_cf, dict):
                if any(v is not None and not isinstance(v, bool) for v in raw_cf.values()):
                    return member
            elif raw_cf:
                return member

    # Priority 2: Use master device if it has primary IP
    if vc.master and vc.master.primary_ip:
        return vc.master

    # Priority 3: Find any member with primary IP
    for member in all_members:
        if member.primary_ip:
            return member

    # Priority 4: Use member with lowest vc_position as fallback
    try:
        return min(all_members, key=lambda m: m.vc_position, default=None)
    except (ValueError, TypeError):
        return None


def get_table_paginate_count(request: HttpRequest, table_prefix: str) -> int:
    """
    Extends Netbox pagination to support multiple tables by using table-specific prefixes

    Args:
        request: HTTP request object
        table_prefix: Prefix for the table

    Returns:
        int: Number of items to display per page
    """
    config = get_config()
    if f"{table_prefix}per_page" in request.GET:
        try:
            per_page = int(request.GET.get(f"{table_prefix}per_page"))
            return min(per_page, config.MAX_PAGE_SIZE)
        except ValueError:
            pass

    return netbox_get_paginate_count(request)


def get_user_pref(request, path, default=None):
    """Get a user preference value via request.user.config."""
    if hasattr(request, "user") and hasattr(request.user, "config"):
        return request.user.config.get(path, default)
    return default


def save_user_pref(request, path, value):
    """Save a user preference value via request.user.config."""
    if hasattr(request, "user") and hasattr(request.user, "config"):
        try:
            request.user.config.set(path, value, commit=True)
        except (TypeError, ValueError):
            pass


def get_interface_name_field(request: Optional[HttpRequest] = None) -> str:
    """
    Get interface name field with request override support.

    Checks in order: GET/POST params, user preference, plugin config default.
    When a param is explicitly provided, persists it to user preferences.

    Args:
        request: Optional HTTP request object that may contain override

    Returns:
        str: Interface name field to use
    """
    if request:
        # Explicit override from request params
        param_val = request.GET.get("interface_name_field") or request.POST.get("interface_name_field")
        if param_val:
            existing = get_user_pref(request, "plugins.netbox_librenms_plugin.interface_name_field")
            if param_val != existing:
                save_user_pref(request, "plugins.netbox_librenms_plugin.interface_name_field", param_val)
            return param_val

        # Check user preference
        pref_val = get_user_pref(request, "plugins.netbox_librenms_plugin.interface_name_field")
        if pref_val:
            return pref_val

    # Fall back to plugin config
    return get_plugin_config("netbox_librenms_plugin", "interface_name_field")


def match_librenms_hardware_to_device_type(hardware_name: str) -> dict | None:
    """
    Match LibreNMS hardware string to a NetBox DeviceType.

    Checks DeviceTypeMapping table first, then falls back to exact matching
    on part_number and model fields (case-insensitive).

    Args:
        hardware_name (str): Hardware string from LibreNMS API (e.g., 'C9200L-48P-4X')

    Returns:
        dict: Dictionary containing:
            - matched (bool): Whether a match was found
            - device_type (DeviceType|None): The matched DeviceType object
            - match_type (str|None): 'mapping' if via DeviceTypeMapping, 'exact' if via
              part_number/model, None otherwise
    """
    from dcim.models import DeviceType

    try:
        from netbox_librenms_plugin.models import DeviceTypeMapping

        _has_device_type_mapping = True
    except ImportError:
        _has_device_type_mapping = False

    if not hardware_name or hardware_name == "-":
        return {"matched": False, "device_type": None, "match_type": None}

    # Check DeviceTypeMapping table first (when available)
    if _has_device_type_mapping:
        try:
            mapping = DeviceTypeMapping.objects.get(librenms_hardware__iexact=hardware_name)
            return {
                "matched": True,
                "device_type": mapping.netbox_device_type,
                "match_type": "mapping",
            }
        except DeviceTypeMapping.DoesNotExist:
            pass
        except DeviceTypeMapping.MultipleObjectsReturned:
            logger.warning(
                "Multiple DeviceTypeMapping entries match hardware %r — skipping mapping lookup; "
                "resolve the ambiguity by removing duplicate mappings.",
                hardware_name,
            )
            return None

    # Try part number exact match
    try:
        device_type = DeviceType.objects.get(part_number__iexact=hardware_name)
        return {
            "matched": True,
            "device_type": device_type,
            "match_type": "exact",
        }
    except DeviceType.DoesNotExist:
        pass
    except DeviceType.MultipleObjectsReturned:
        logger.warning(
            "Multiple DeviceType entries match part_number %r — cannot auto-select; "
            "resolve the ambiguity by ensuring part numbers are unique across manufacturers.",
            hardware_name,
        )
        return None

    # Try exact model match (case-insensitive)
    try:
        device_type = DeviceType.objects.get(model__iexact=hardware_name)
        return {"matched": True, "device_type": device_type, "match_type": "exact"}
    except DeviceType.DoesNotExist:
        pass
    except DeviceType.MultipleObjectsReturned:
        logger.warning(
            "Multiple DeviceType entries match model %r — cannot auto-select; "
            "resolve the ambiguity by ensuring model names are unique across manufacturers.",
            hardware_name,
        )
        return None

    return {"matched": False, "device_type": None, "match_type": None}


def find_matching_site(librenms_location: str) -> dict:
    """
    Find exact matching NetBox site for a LibreNMS location.

    Only performs exact name matching (case-insensitive).

    Args:
        librenms_location (str): Location string from LibreNMS

    Returns:
        dict: Dictionary containing:
            - found (bool): Whether a match was found
            - site (Site|None): The matched Site object
            - match_type (str|None): Always 'exact' if found, None otherwise
            - confidence (float): Always 1.0 if found, 0.0 otherwise
    """
    from dcim.models import Site

    if not librenms_location or librenms_location == "-":
        return {"found": False, "site": None, "match_type": None, "confidence": 0.0}

    # Try case-insensitive exact match
    try:
        site = Site.objects.get(name__iexact=librenms_location)
        return {"found": True, "site": site, "match_type": "exact", "confidence": 1.0}
    except Site.DoesNotExist:
        pass
    except Site.MultipleObjectsReturned:
        site = Site.objects.filter(name__iexact=librenms_location).first()
        return {"found": True, "site": site, "match_type": "exact", "confidence": 1.0}

    return {"found": False, "site": None, "match_type": None, "confidence": 0.0}


def find_matching_platform(librenms_os: str) -> dict:
    """
    Find matching NetBox platform for a LibreNMS OS.

    Checks PlatformMapping table first (explicit user-defined mapping),
    then falls back to exact case-insensitive name match.

    Args:
        librenms_os (str): OS string from LibreNMS (e.g., 'ios', 'linux', 'junos')

    Returns:
        dict: Dictionary containing:
            - found (bool): Whether a match was found
            - platform (Platform|None): The matched Platform object
            - match_type (str|None): 'mapping', 'exact', or None
    """
    from dcim.models import Platform

    if not librenms_os or librenms_os == "-":
        return {"found": False, "platform": None, "match_type": None}

    # Check PlatformMapping table first
    if PlatformMapping is not None:
        try:
            mapping = PlatformMapping.objects.get(librenms_os__iexact=librenms_os)
            return {"found": True, "platform": mapping.netbox_platform, "match_type": "mapping"}
        except PlatformMapping.DoesNotExist:
            pass
        except PlatformMapping.MultipleObjectsReturned:
            return {"found": False, "platform": None, "match_type": "ambiguous"}

    # Try case-insensitive exact name match
    try:
        platform = Platform.objects.get(name__iexact=librenms_os)
        return {"found": True, "platform": platform, "match_type": "exact"}
    except Platform.DoesNotExist:
        pass
    except Platform.MultipleObjectsReturned:
        platform = Platform.objects.filter(name__iexact=librenms_os).first()
        return {"found": True, "platform": platform, "match_type": "exact"}

    return {"found": False, "platform": None, "match_type": None}


def get_vlan_sync_css_class(exists_in_netbox: bool, name_matches: bool = True) -> str:
    """
    Determine CSS class for a VLAN row on the VLAN sync tab.

    Used by both the server-side table renderer (LibreNMSVLANTable)
    and the client-facing verify endpoint (VerifyVlanSyncGroupView)
    to keep color logic consistent.

    Args:
        exists_in_netbox: Whether the VLAN exists in NetBox (in the selected group or globally).
        name_matches: Whether the VLAN name in NetBox matches the LibreNMS name.

    Returns:
        CSS class string: 'text-success', 'text-warning', or 'text-danger'.
    """
    if not exists_in_netbox:
        return "text-danger"
    if name_matches:
        return "text-success"
    return "text-warning"


# ============================================
# Interface VLAN CSS helpers
# ============================================
# Shared by LibreNMSInterfaceTable (tables/interfaces.py) and
# SingleVlanGroupVerifyView (views/object_sync/devices.py).


def get_untagged_vlan_css_class(librenms_vid, netbox_vid, exists_in_netbox, missing_vlans, group_matches=True):
    """
    Get CSS class for an untagged VLAN comparison.

    Color logic:
    - Red (text-danger) + warning icon: VLAN not in any NetBox group (cannot sync)
    - Red (text-danger): Interface missing from NetBox, or no untagged VLAN in NetBox
    - Orange (text-warning): Different untagged VLAN assigned, or same VID but different group
    - Green (text-success): Same untagged VLAN assigned in same group (match)

    Args:
        librenms_vid: VLAN ID from LibreNMS.
        netbox_vid: VLAN ID currently assigned in NetBox (int or None).
        exists_in_netbox: Whether the interface exists in NetBox.
        missing_vlans: List of VIDs not found in any NetBox VLAN group.
        group_matches: Whether the selected VLAN group matches the NetBox VLAN's group.
                       Only meaningful when VIDs match; defaults to True.

    Returns:
        CSS class string: text-danger, text-warning, or text-success.
    """
    if not exists_in_netbox:
        return "text-danger"
    if librenms_vid in missing_vlans:
        return "text-danger"
    if librenms_vid == netbox_vid:
        if not group_matches:
            return "text-warning"
        return "text-success"
    if netbox_vid is None:
        return "text-danger"
    return "text-warning"


def get_tagged_vlan_css_class(vid, netbox_tagged_vids, exists_in_netbox, missing_vlans, group_matches=True):
    """
    Get CSS class for a tagged VLAN comparison.

    Color logic:
    - Red (text-danger) + warning icon: VLAN not in any NetBox group (cannot sync)
    - Red (text-danger): Interface missing from NetBox, or VLAN not tagged on this interface
    - Orange (text-warning): Same VID tagged but in different VLAN group
    - Green (text-success): VLAN is tagged on this interface in same group

    Args:
        vid: VLAN ID to check.
        netbox_tagged_vids: Set of VIDs currently tagged on the NetBox interface.
        exists_in_netbox: Whether the interface exists in NetBox.
        missing_vlans: List of VIDs not found in any NetBox VLAN group.
        group_matches: Whether the selected VLAN group matches the NetBox VLAN's group.
                       Only meaningful when VIDs match; defaults to True.

    Returns:
        CSS class string: text-danger, text-warning, or text-success.
    """
    if not exists_in_netbox:
        return "text-danger"
    if vid in missing_vlans:
        return "text-danger"
    if vid in netbox_tagged_vids:
        if not group_matches:
            return "text-warning"
        return "text-success"
    return "text-danger"


def get_missing_vlan_warning(vid, missing_vlans):
    """Return warning icon HTML if VLAN is not found in any NetBox VLAN group."""
    if vid in missing_vlans:
        return (
            ' <i class="mdi mdi-alert text-danger" '
            'title="VLAN not in NetBox\u2014use VLAN Sync first to create it"></i>'
        )
    return ""


def check_vlan_group_matches(
    vlan_type,
    vid,
    selected_group_id,
    netbox_untagged_group_id,
    netbox_tagged_group_ids,
    netbox_untagged_vid,
    netbox_tagged_vids,
):
    """
    Check whether the selected VLAN group matches the NetBox VLAN's group.

    Only relevant when VIDs match — if VIDs differ, the CSS is already
    warning/danger regardless of group.

    Args:
        vlan_type: "U" or "T".
        vid: VLAN ID.
        selected_group_id: Group ID (int or None) the user selected.
        netbox_untagged_group_id: group_id of netbox untagged VLAN (int or None).
        netbox_tagged_group_ids: {vid: group_id} of netbox tagged VLANs.
        netbox_untagged_vid: VID of netbox untagged VLAN (int or None).
        netbox_tagged_vids: set of VIDs tagged in netbox.

    Returns:
        bool: True if groups match (or comparison not applicable).
    """
    if vlan_type == "U":
        if netbox_untagged_vid == vid:
            return netbox_untagged_group_id == selected_group_id
    else:
        if vid in netbox_tagged_vids:
            netbox_gid = netbox_tagged_group_ids.get(vid)
            return netbox_gid == selected_group_id
    return True


def get_librenms_device_id(obj, server_key: str = "default", *, auto_save: bool = True):
    """
    Get the LibreNMS device/port ID for a specific server from the JSON custom field.

    Supports both the legacy integer format and the new multi-server JSON format::

        Legacy:  librenms_id = 42          → returns 42 for any server_key (universal fallback)
        New:     librenms_id = {"primary": 42}  → returns 42 only for server_key="primary"

    If the stored value (or the dict entry for server_key) is a string it is
    normalised to ``int``.  When *auto_save* is ``True`` (the default) the
    normalised value is written back so that subsequent DB queries can use a
    plain integer without defensive ``str()`` casting.  Pass ``auto_save=False``
    in read-only contexts (e.g. table renderers) to avoid triggering unintended
    DB writes or signals.

    Args:
        obj: NetBox object with a ``librenms_id`` custom field.
        server_key: LibreNMS server key (from plugin ``servers`` config).
        auto_save: When True (default), persist any normalised value back to the DB.

    Returns:
        int or None
    """
    cf_value = obj.cf.get("librenms_id")
    if cf_value is None:
        return None
    if isinstance(cf_value, int) and not isinstance(cf_value, bool):
        # Legacy bare integer — universal fallback for any server to ensure
        # devices imported before multi-server support remain discoverable.
        return cf_value if cf_value > 0 else None
    if isinstance(cf_value, str):
        # Someone stored a bare string (e.g., via NetBox UI/API) — normalise to int.
        # Treat as a legacy universal fallback.
        try:
            int_id = int(cf_value)
        except (ValueError, TypeError):
            return None
        if int_id <= 0:
            return None
        if auto_save:
            obj.custom_field_data["librenms_id"] = int_id
            obj.save(update_fields=["custom_field_data"])
        return int_id
    if isinstance(cf_value, dict):
        value = cf_value.get(server_key)
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            # Normalise string-stored ID inside JSON dict and write back.
            try:
                value = int(value)
            except (ValueError, TypeError):
                return None
            if value <= 0:
                return None
            if auto_save:
                cf_value[server_key] = value
                obj.custom_field_data["librenms_id"] = cf_value
                obj.save(update_fields=["custom_field_data"])
            return value
        if isinstance(value, int):
            return value if value > 0 else None
        return None
    return None


def set_librenms_device_id(obj, device_id, server_key: str = "default"):
    """
    Set the LibreNMS device/port ID for a specific server on the JSON custom field.

    Does NOT silently migrate legacy bare-integer values to the dict format.
    If the field contains a legacy bare integer (or a string that parses as an integer),
    a warning is logged and the write is skipped; use the migration workflow instead.

    Args:
        obj: NetBox object with a ``librenms_id`` custom field.
        device_id: LibreNMS device ID (integer).
        server_key: LibreNMS server key (from plugin ``servers`` config).
    """
    if isinstance(device_id, bool):
        logger.warning(
            "librenms_id device_id is a boolean (%r) on %r; not storing.",
            device_id,
            obj,
        )
        return
    cf_value = obj.custom_field_data.get("librenms_id") or {}
    if isinstance(cf_value, int) and not isinstance(cf_value, bool):
        logger.warning(
            "librenms_id on %r has legacy bare integer %r; skipping write to prevent "
            "silent migration. Use the migration workflow to convert.",
            obj,
            cf_value,
        )
        return
    elif isinstance(cf_value, str):
        try:
            int(cf_value)
            logger.warning(
                "librenms_id on %r has legacy bare integer string %r; skipping write to "
                "prevent silent migration. Use the migration workflow to convert.",
                obj,
                cf_value,
            )
            return
        except (ValueError, TypeError):
            logger.warning(
                "librenms_id custom field has unexpected string %r on %r; resetting to empty dict.",
                cf_value,
                obj,
            )
            cf_value = {}
    elif not isinstance(cf_value, dict):
        logger.warning(
            "librenms_id custom field has unexpected type %s on %r; resetting to empty dict.",
            type(cf_value).__name__,
            obj,
        )
        cf_value = {}
    try:
        cf_value[server_key] = int(device_id)
    except (TypeError, ValueError):
        logger.warning(
            "librenms_id device_id %r is not a valid integer on %r; not storing.",
            device_id,
            obj,
        )
        return  # Don't persist an invalid entry
    obj.custom_field_data["librenms_id"] = cf_value


def find_by_librenms_id(model, librenms_id, server_key: str = "default"):
    """
    Return the first object of *model* whose ``librenms_id`` JSON field contains
    *librenms_id* under *server_key*.

    Also matches legacy records stored as a bare ``librenms_id`` integer or string
    in ``custom_field_data``—these predate multi-server support and act as a
    universal fallback for any *server_key*.

    Args:
        model: A Django model class (Device, VirtualMachine, Interface, …).
        librenms_id: The LibreNMS device/port ID to look up.
        server_key: LibreNMS server key (from plugin ``servers`` config).

    Returns:
        Model instance or None
    """
    if librenms_id is None:
        return None
    if isinstance(librenms_id, bool):
        return None
    q = Q(**{f"custom_field_data__librenms_id__{server_key}": librenms_id})
    # Also match when the namespaced value was stored as a string (e.g. {"production": "42"}).
    q |= Q(**{f"custom_field_data__librenms_id__{server_key}": str(librenms_id)})
    # Always include legacy bare-integer and bare-string IDs as a universal fallback.
    # Legacy records were created before multi-server support; they should be visible
    # regardless of which server is currently active.
    q |= Q(custom_field_data__librenms_id=librenms_id)
    q |= Q(custom_field_data__librenms_id=str(librenms_id))
    # When a string ID looks like an integer, also match the numeric JSON form so
    # "42" matches records that store the value as the JSON number 42.
    if isinstance(librenms_id, str) and librenms_id.isdigit():
        int_value = int(librenms_id)
        q |= Q(**{f"custom_field_data__librenms_id__{server_key}": int_value})
        q |= Q(custom_field_data__librenms_id=int_value)
    return model.objects.filter(q).first()


def migrate_legacy_librenms_id(obj, server_key: str = "default") -> bool:
    """
    Migrate a legacy bare-integer ``librenms_id`` custom field to the JSON dict format,
    scoped to *server_key*.

    Only performs the migration when the current value is a bare integer, i.e. a record
    created before the multi-server JSON refactor.  The integer is assumed to belong to
    the server identified by *server_key* (the caller must verify this, e.g. by confirming
    that the LibreNMS device ID and serial number both match).

    Does **not** call ``obj.save()`` — the caller is responsible for persisting the change.

    Args:
        obj: NetBox object with a ``librenms_id`` custom field.
        server_key: LibreNMS server key the legacy integer should be scoped to.

    Returns:
        True if the value was migrated, False if it was already in the correct format.
    """
    cf_value = obj.custom_field_data.get("librenms_id")
    if isinstance(cf_value, bool):
        return False
    if isinstance(cf_value, int):
        int_value = cf_value
    elif isinstance(cf_value, str) and cf_value.isdigit():
        int_value = int(cf_value)
    else:
        return False
    obj.custom_field_data["librenms_id"] = {server_key: int_value}
    logger.info(
        "Migrated legacy librenms_id %r → {%r: %d} on %r",
        cf_value,
        server_key,
        int_value,
        obj,
    )
    return True


def has_nested_name_conflict(module_type, module_bay):
    """
    Check if installing this module type in a nested bay would cause a name conflict.

    Returns True when ALL of the following are true:
    - The module type has interface templates using ``{module}``
    - The bay is nested (its parent is owned by an installed module)
    - There is at least one sibling bay under the same parent

    In this situation NetBox's ``resolve_name()`` replaces ``{module}`` with the
    root ancestor's bay position, producing the same interface name for every
    sibling at this nesting level.
    """
    from dcim.constants import MODULE_TOKEN

    if not module_bay or not module_bay.module_id:
        return False  # Top-level bay — no conflict

    templates = list(module_type.interfacetemplates.all())
    if not templates:
        return False  # No interface templates

    if not any(MODULE_TOKEN in t.name for t in templates):
        return False  # Template doesn't use {module}

    # Count how many unique interface names this template would produce across siblings
    # If all siblings resolve to the same name, there's a conflict
    from dcim.models import ModuleBay as ModuleBayModel

    sibling_count = ModuleBayModel.objects.filter(
        device=module_bay.device,
        module_id=module_bay.module_id,
    ).count()

    return sibling_count > 1


def get_module_types_indexed() -> dict:
    """
    Return all NetBox module types indexed by model (and part_number), with ModuleTypeMapping applied.

    ModuleTypeMapping entries take priority over the base model/part_number keys so that
    explicit overrides win when the same string appears in both.
    """
    from dcim.models import ModuleType

    from netbox_librenms_plugin.models import ModuleTypeMapping

    result: dict = {}
    ambiguous: set = set()
    for mt in ModuleType.objects.all().select_related("manufacturer"):
        seen_this_entry: set = set()
        for key in (mt.model, mt.part_number):
            if not key or key in seen_this_entry:
                continue
            seen_this_entry.add(key)
            if key in ambiguous:
                continue
            if key in result:
                ambiguous.add(key)
                del result[key]
            else:
                result[key] = mt
    for mapping in ModuleTypeMapping.objects.select_related("netbox_module_type__manufacturer"):
        result[mapping.librenms_model] = mapping.netbox_module_type
    return result


def apply_normalization_rules(value: str, scope: str, manufacturer=None) -> str:
    """
    Apply NormalizationRule chain to transform a string before matching.

    Rules for the given scope are applied in priority order.  Each rule's
    regex substitution transforms the output of the previous rule, forming
    a pipeline.  If no rules match, the original value is returned unchanged.

    When *manufacturer* is given, manufacturer-scoped rules run first,
    followed by unscoped (manufacturer=NULL) rules.  When *manufacturer*
    is ``None``, all rules for the scope run in priority order.

    Args:
        value:  The raw string to normalize (e.g. '3HE16474AARA01').
        scope:  One of NormalizationRule.SCOPE_* constants.
        manufacturer:  Optional Manufacturer instance to scope rules.

    Returns:
        The normalized string after all matching rules have been applied.
    """
    from netbox_librenms_plugin.models import NormalizationRule

    if not value:
        return value

    def _apply_rules(val, rules_qs):
        for rule in rules_qs:
            try:
                val = re.sub(rule.match_pattern, rule.replacement, val)
            except (re.error, IndexError):
                logger.error(
                    "Invalid regex in NormalizationRule pk=%s pattern=%r — skipping", rule.pk, rule.match_pattern
                )
        return val

    if manufacturer:
        # Manufacturer-specific rules first, then unscoped rules
        for mfg_filter in [{"manufacturer": manufacturer}, {"manufacturer__isnull": True}]:
            rules = NormalizationRule.objects.filter(scope=scope, **mfg_filter).order_by("priority", "pk")
            value = _apply_rules(value, rules)
    else:
        rules = NormalizationRule.objects.filter(scope=scope).order_by("priority", "pk")
        value = _apply_rules(value, rules)
    return value


def resolve_module_type(model_name: str, module_types: dict, manufacturer=None):
    """
    Resolve a LibreNMS model name to a NetBox ModuleType via direct lookup then normalization.

    Returns the matched ModuleType or None.
    """
    if not model_name:
        return None
    matched = module_types.get(model_name)
    if not matched:
        normalized = apply_normalization_rules(model_name, "module_type", manufacturer=manufacturer)
        if normalized != model_name:
            matched = module_types.get(normalized)
    return matched


def get_enabled_ignore_rules() -> list:
    """Return all enabled InventoryIgnoreRule instances as a list."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return list(InventoryIgnoreRule.objects.filter(enabled=True))


def load_bay_mappings() -> tuple:
    """
    Load all ModuleBayMapping rows, split into exact and regex lists.

    Returns:
        (exact_mappings, regex_mappings) tuple of lists.
    """
    from netbox_librenms_plugin.models import ModuleBayMapping

    all_mappings = list(ModuleBayMapping.objects.all())
    exact = [m for m in all_mappings if not m.is_regex]
    regex = [m for m in all_mappings if m.is_regex]
    return exact, regex
