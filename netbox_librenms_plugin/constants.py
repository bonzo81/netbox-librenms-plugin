import re

# Plugin permissions (from LibreNMSSettings model)
PERM_VIEW_PLUGIN = "netbox_librenms_plugin.view_librenmssettings"
PERM_CHANGE_PLUGIN = "netbox_librenms_plugin.change_librenmssettings"

# LibreNMS VLAN state values
LIBRENMS_VLAN_STATE_ACTIVE = 1

# OOB management controller detection
OOB_TYPE_PATTERN = re.compile(r"\b(idrac|ilo|ipmi|bmc|drac)", re.IGNORECASE)
OOB_TYPES = ("idrac", "ilo", "ipmi", "bmc", "drac")


def normalize_oob_type(os_str: str, hardware_str: str = "") -> str | None:
    """
    Extract and normalize the OOB controller type from LibreNMS os/hardware strings.

    Returns the canonical lowercase token (one of OOB_TYPES) or None if no match.

    Examples:
        normalize_oob_type("drac9", "iDRAC9") → "drac"
        normalize_oob_type("ilo", "")         → "ilo"
        normalize_oob_type("ubuntu", "")      → None
    """
    for text in (os_str or "", hardware_str or ""):
        m = OOB_TYPE_PATTERN.search(text)
        if m:
            return m.group(1).lower()
    return None
