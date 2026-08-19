import re

# Plugin permissions (from LibreNMSSettings model)
PERM_VIEW_PLUGIN = "netbox_librenms_plugin.view_librenmssettings"
PERM_CHANGE_PLUGIN = "netbox_librenms_plugin.change_librenmssettings"

# LibreNMS VLAN state values
LIBRENMS_VLAN_STATE_ACTIVE = 1

# LibreNMS port fields the plugin can display as the interface name. The preference resolver,
# the snapshot writers, and the snapshot readers all validate against this one set, so a value
# one side accepts can never be rejected by the other.
DEFAULT_INTERFACE_NAME_FIELD = "ifName"
INTERFACE_NAME_FIELDS = frozenset({DEFAULT_INTERFACE_NAME_FIELD, "ifDescr"})


def is_supported_interface_name_field(value):
    """Return whether *value* names a LibreNMS port field usable as the interface name.

    The set membership alone raises TypeError on an unhashable value, and a preference can
    arrive from a JSON body or a cache entry. Every site tests through this one predicate so
    the promise above cannot be kept on one side and dropped on the other.
    """
    return isinstance(value, str) and value in INTERFACE_NAME_FIELDS


# OOB management controller detection
# Trailing \d*\b restricts matches to whole tokens (optionally with a numeric suffix like
# iDRAC9 / drac9) so a prefix collision inside an unrelated word — e.g. "dracut", "ipmitool"
# — can't misclassify a normal device as an OOB controller.
OOB_TYPE_PATTERN = re.compile(r"\b(idrac|ilo|ipmi|bmc|drac|cimc|oob)\d*\b", re.IGNORECASE)
OOB_TYPES = ("idrac", "ilo", "ipmi", "bmc", "drac", "cimc", "oob")

# Shared "From OOB controller" badge markup (the bare <span>; callers add any leading space).
# Centralised so a restyle (color/title/text) happens in one place instead of drifting across the
# cable/module/interface tables and the cable-verify render that each hand-copied it.
OOB_BADGE_HTML = '<span class="badge bg-purple text-white ms-1" title="From OOB controller">OOB</span>'


def normalize_oob_type(os_str: str, hardware_str: str = "") -> str | None:
    """
    Extract and normalize the OOB controller type from LibreNMS os/hardware strings.

    A vendor-specific match (idrac/ilo/ipmi/bmc/drac/cimc) always wins over the
    generic ``oob`` token, even when ``oob`` appears earlier in the text, so e.g.
    ``normalize_oob_type("oob", "iDRAC9")`` resolves to ``"idrac"`` rather than
    being masked by the generic token.

    Args:
        os_str (str): LibreNMS ``os`` field for the device.
        hardware_str (str): LibreNMS ``hardware`` field for the device.

    Returns:
        str | None: The canonical lowercase token (one of OOB_TYPES), or None if
            no token matches.

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
