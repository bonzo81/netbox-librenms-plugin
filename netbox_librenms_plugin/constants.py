import re

# Plugin permissions (from LibreNMSSettings model)
PERM_VIEW_PLUGIN = "netbox_librenms_plugin.view_librenmssettings"
PERM_CHANGE_PLUGIN = "netbox_librenms_plugin.change_librenmssettings"

# LibreNMS VLAN state values
LIBRENMS_VLAN_STATE_ACTIVE = 1

# OOB management controller detection
# Trailing \d*\b restricts matches to whole tokens (optionally with a numeric suffix like
# iDRAC9 / drac9) so a prefix collision inside an unrelated word — e.g. "dracut", "ipmitool"
# — can't misclassify a normal device as an OOB controller.
OOB_TYPE_PATTERN = re.compile(r"\b(idrac|ilo|ipmi|bmc|drac|cimc|oob)\d*\b", re.IGNORECASE)
OOB_TYPES = ("idrac", "ilo", "ipmi", "bmc", "drac", "cimc", "oob")


def normalize_oob_type(os_str: str, hardware_str: str = "") -> str | None:
    """
    Extract and normalize the OOB controller type from LibreNMS os/hardware strings.

    Returns the canonical lowercase token (one of OOB_TYPES) or None if no match.

    A vendor-specific match (idrac/ilo/ipmi/bmc/drac/cimc) always wins over the
    generic ``oob`` token, even when ``oob`` appears earlier in the text, so e.g.
    ``normalize_oob_type("oob", "iDRAC9")`` resolves to ``"idrac"`` rather than
    being masked by the generic token.

    Examples:
        normalize_oob_type("drac9", "iDRAC9") → "drac"
        normalize_oob_type("oob", "iDRAC9")   → "idrac"
        normalize_oob_type("ilo", "")         → "ilo"
        normalize_oob_type("ubuntu", "")      → None
    """
    generic = None
    for text in (os_str or "", hardware_str or ""):
        for m in OOB_TYPE_PATTERN.finditer(text):
            token = m.group(1).lower()
            if token != "oob":
                return token  # vendor-specific match wins immediately
            generic = generic or "oob"  # remember the generic fallback, keep scanning
    return generic
