import re

# Plugin permissions (from LibreNMSSettings model)
PERM_VIEW_PLUGIN = "netbox_librenms_plugin.view_librenmssettings"
PERM_CHANGE_PLUGIN = "netbox_librenms_plugin.change_librenmssettings"

# LibreNMS VLAN state values
LIBRENMS_VLAN_STATE_ACTIVE = 1

# Nokia uses this name for its global routing table, not for a VRF.
LIBRENMS_GLOBAL_ROUTING_INSTANCE = "Base"

# Columns every /devices/{id}/ports read requests. The live reader and the data-shape capture
# share this one string, so a captured ports payload can never carry fewer fields than the sync
# logic reads. ifVrf is the per-port VRF id the IP tab joins against /routing/vrf. ifIndex links a
# transceiver to its ENTITY-MIB row.
LIBRENMS_PORTS_COLUMNS = (
    "port_id,ifName,ifIndex,ifType,ifSpeed,ifAdminStatus,ifDescr,ifAlias,ifPhysAddress,ifMtu,ifVlan,ifTrunk,ifVrf"
)

# LibreNMS port fields the plugin can display as the interface name. The preference resolver,
# the snapshot writers, and the snapshot readers all validate against this one set, so a value
# one side accepts can never be rejected by the other.
DEFAULT_INTERFACE_NAME_FIELD = "ifName"
INTERFACE_NAME_FIELDS = frozenset({DEFAULT_INTERFACE_NAME_FIELD, "ifDescr"})

# Fixed response phrases distinguish empty resources from failed requests.
LIBRENMS_NO_LINKS_MESSAGES = ("does not have any links", "links do not exist")
LIBRENMS_NO_IPS_MESSAGE = "does not have any ip addresses"
LIBRENMS_NO_VRFS_MESSAGE = "vrfs do not exist"
LIBRENMS_NO_SENSORS_MESSAGE = "sensors do not exist"
LIBRENMS_EMPTY_MESSAGES = (
    *LIBRENMS_NO_LINKS_MESSAGES,
    LIBRENMS_NO_IPS_MESSAGE,
    LIBRENMS_NO_VRFS_MESSAGE,
    LIBRENMS_NO_SENSORS_MESSAGE,
)

# The typed relationships the resolver derives from a LibreNMS port_stack pair, in the order the
# rules are applied. The resolver, the cached-snapshot reader and the diagnostics report all walk
# this tuple, so a kind cannot be added to one and missed by another. A pair no kind claims is
# carried untyped as "stacked_ports" instead of being dropped.
RELATIONSHIP_KINDS = ("sub_interfaces", "bridge_members", "lag_members")


def is_supported_interface_name_field(value):
    """
    Return whether *value* names a LibreNMS port field usable as the interface name.

    The set membership alone raises TypeError on an unhashable value, and a preference can
    arrive from a JSON body or a cache entry. Every site tests through this one predicate so
    the promise above cannot be kept on one side and dropped on the other.
    """
    return isinstance(value, str) and value in INTERFACE_NAME_FIELDS


# The interface fields one sync writes, as (LibreNMS key, NetBox attribute) pairs, in write
# order. ``INTERFACE_NAME_KEY`` stands for whichever port field is currently acting as the
# interface name; it can never equal a real LibreNMS key. update_interface_from_port() builds its
# field map from this tuple and the row diff reads the same one, so the table cannot paint a
# field the sync leaves alone, or miss one it writes.
INTERFACE_NAME_KEY = "<interface_name_field>"
INTERFACE_SYNC_FIELD_PAIRS = (
    (INTERFACE_NAME_KEY, "name"),
    ("ifType", "type"),
    ("ifSpeed", "speed"),
    ("ifAlias", "description"),
    ("ifMtu", "mtu"),
)

# Fields the same sync writes outside that map, each from its own LibreNMS evidence:
# ifAdminStatus, ifPhysAddress, port_id and the parsed VLAN assignment.
INTERFACE_SYNC_EXTRA_FIELDS = ("enabled", "mac_address", "librenms_id", "vlans")


# Values LibreNMS reports in a model, serial or type field when it has no data: no lookup key, no
# identity evidence, and kept as they are by the anonymizer. "0" is a real-but-falsey serial.
LIBRENMS_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "-",
        "builtin",
        "default",
        "n/a",
        "na",
        "none",
        "not available",
        "notavailable",
        "null",
        "unknown",
        "unspecified",
    }
)


def is_librenms_placeholder(value) -> bool:
    """Return whether *value* is a LibreNMS model, serial or type value that holds no data."""
    return not isinstance(value, str) or value.strip().casefold() in LIBRENMS_PLACEHOLDER_VALUES


# Model values that hide a row as a generic container. Narrower than LIBRENMS_PLACEHOLDER_VALUES on
# purpose: an "unspecified" SFP is still hardware, and its description can resolve its type.
GENERIC_CONTAINER_MODELS = frozenset({"", "builtin", "default", "n/a"})


# Junos Virtual Chassis ENTITY descriptions: the root names the Virtual Chassis, each member is an
# "FPC" row, and an "FPC <n> ..." row names member n. VC detection and the anonymizer read these.
JUNOS_VC_ROOT_DESCR_MARKER = "Virtual Chassis"
JUNOS_FPC_DESCR_PREFIX = "FPC"
JUNOS_FPC_MEMBER_DESCR_RE = re.compile(r"^FPC (?P<member>\d+) ")


# OOB management controller detection
# Trailing \d*\b restricts matches to whole tokens (optionally with a numeric suffix like
# iDRAC9 / drac9) so a prefix collision inside an unrelated word — e.g. "dracut", "ipmitool"
# — can't misclassify a normal device as an OOB controller.
OOB_TYPE_PATTERN = re.compile(r"\b(idrac|ilo|ipmi|bmc|drac|cimc|oob)\d*\b", re.IGNORECASE)
OOB_TYPES = ("idrac", "ilo", "ipmi", "bmc", "drac", "cimc", "oob")

# Marker the interface/cable/module views stamp on rows merged in from an OOB controller. The
# rows are display-only, so every reader gates on this one value; a bare literal at each site
# meant a typo silently turned a read-only row into an actionable one.
OOB_INVENTORY_SOURCE = "oob"
MAIN_INVENTORY_SOURCE = "main"
SERIAL_INVENTORY_SOURCE = "serial"
# Changing this value renames every derived OOB interface on its next sync.
OOB_NAME_SUFFIX = "-oob"

# Shared "From OOB controller" badge markup (the bare <span>; callers add any leading space).
# Centralised so a restyle (color/title/text) happens in one place instead of drifting across the
# cable/module/interface tables and the cable-verify render that each hand-copied it.
OOB_BADGE_HTML = '<span class="badge bg-purple text-white ms-1" title="From OOB controller">OOB</span>'

# A derived OOB name can still collide with another host or OOB row. The sync writer and table
# use one reason so they cannot describe the same condition differently.
HOST_NAME_COLLISION_REASON = "derived interface name is already used by another row"
# A host row cannot claim a name that the active server maps to another port.
REPORTED_NAME_PORT_COLLISION_REASON = "reported name belongs to a different LibreNMS port"
# Mixed sources for one normalized port ID make its identity ambiguous.
PORT_ID_SOURCE_COLLISION_REASON = "LibreNMS port ID is claimed by both host and OOB rows"
# Who holds a reported name: a snapshot row, a port the complete snapshot no longer reports, or
# a port that may sit in the missing OOB inventory.
NAME_OWNER_LIVE = "live"
NAME_OWNER_STALE = "stale"
NAME_OWNER_UNKNOWN = "unknown"


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
