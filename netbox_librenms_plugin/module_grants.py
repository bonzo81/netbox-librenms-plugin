"""
Signed grants that let a Modules tab action write the inventory rows the table rendered.

The table decides which rows it renders. Each action form carries that decision as a grant: the
page, the write target, the action, the inventory snapshot and the inventory policy fingerprint.
A writer checks the grant against the snapshot and the policy it loads itself, and never decides
again whether the table would render a row.
"""

import enum
import hashlib
import json
from collections import Counter
from dataclasses import dataclass

from django.core import signing

from netbox_librenms_plugin.utils import coerce_positive_int

_SALT = "netbox_librenms_plugin.module_write_grant"
# Change this when the payload changes: a token of another version refuses as stale.
GRANT_VERSION = 1

RENDER_ATTRIBUTED = "attributed"
RENDER_PINNED = "pinned"
RENDER_MODES = frozenset({RENDER_ATTRIBUTED, RENDER_PINNED})

ACTION_INSTALL_MODULE = "install_module"
ACTION_INSTALL_BRANCH = "install_branch"
ACTION_INSTALL_SELECTED = "install_selected"
ACTION_INSTALL_SELECTED_ROW = "install_selected_row"
ACTION_UPDATE_SERIAL = "update_module_serial"
ACTION_UPDATE_INTERFACE = "update_module_interface"
ACTION_REPLACE_MODULE = "replace_module"
ACTION_MISMATCH_PREVIEW = "mismatch_preview"
ACTION_VERIFY_MODULE = "verify_module"

# The batch rows each action signs: a branch entry is [index, target]; a manifest entry adds "selectable".
# A verify entry is [index] of a row the page table shows in the verified row's subtree.
_ROW_SHAPES = {
    ACTION_INSTALL_BRANCH: (int, int),
    ACTION_INSTALL_SELECTED: (int, int, bool),
    ACTION_VERIFY_MODULE: (int,),
}
# The actions that act on no single row and so sign no index.
_INDEXLESS_ACTIONS = frozenset({ACTION_INSTALL_SELECTED})
_ACTIONS = frozenset(
    {
        ACTION_INSTALL_MODULE,
        ACTION_INSTALL_BRANCH,
        ACTION_INSTALL_SELECTED,
        ACTION_INSTALL_SELECTED_ROW,
        ACTION_UPDATE_SERIAL,
        ACTION_UPDATE_INTERFACE,
        ACTION_REPLACE_MODULE,
        ACTION_MISMATCH_PREVIEW,
        ACTION_VERIFY_MODULE,
    }
)
_PAYLOAD_KEYS = frozenset(
    {
        "version",
        "page_device_id",
        "device_id",
        "server_key",
        "action",
        "action_target",
        "ent_index",
        "inventory_digest",
        "policy_fingerprint",
        "render_mode",
        "rows",
    }
)


class GrantRefusal(enum.Enum):
    """Why a writer refuses a module action, with the text the page shows."""

    STALE = "Inventory action is stale or does not match this row. Refresh Modules and try again."
    SNAPSHOT = "The LibreNMS inventory changed since the Modules table was rendered. Refresh Modules and try again."
    POLICY = "Inventory rules or device identity changed. Refresh Modules and try again."
    AMBIGUOUS_INDEX = (
        "LibreNMS reports one entPhysicalIndex for more than one inventory row, so no module action can run. "
        "Refresh Modules when LibreNMS reports each index once."
    )
    DESTINATION = "Inventory destination changed. Refresh Modules and try again."
    CACHE_MISSING = "No cached inventory data. Please refresh modules first."
    RULE_ERROR = (
        "An Inventory Ignore Rule or a serial Normalization Rule stores an invalid regex or replacement, "
        "so nothing was changed. Fix the rule that the Modules tab names."
    )

    @property
    def message(self) -> str:
        """Return the text the page shows for this refusal."""
        return self.value


class GrantRefused(Exception):
    """A module action has no valid grant; nothing may be written."""

    def __init__(self, refusal: GrantRefusal):
        self.refusal = refusal
        super().__init__(refusal.name)


def module_inventory_snapshot_digest(inventory) -> str:
    """Return a canonical digest for a complete cached LibreNMS inventory snapshot."""
    if not isinstance(inventory, list) or any(not isinstance(item, dict) for item in inventory):
        raise TypeError("inventory must be a list of dictionaries")
    serialized = json.dumps({"inventory": inventory}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def duplicate_inventory_indices(inventory) -> frozenset:
    """Return each positive entPhysicalIndex that more than one inventory row reports."""
    counts = Counter(
        index for item in inventory if (index := coerce_positive_int(item.get("entPhysicalIndex"))) is not None
    )
    return frozenset(index for index, count in counts.items() if count > 1)


@dataclass(frozen=True)
class ModuleGrant:
    """The authenticated contents of one module grant."""

    page_device_id: int
    device_id: int
    server_key: str
    action: str
    action_target: dict
    ent_index: int | None
    inventory_digest: str
    policy_fingerprint: str
    render_mode: str
    rows: tuple | None

    def require(self, *, device_id=None, ent_index=None, action_target=None):
        """Raise a stale refusal unless the grant names this target, index and action target."""
        if device_id is not None and coerce_positive_int(device_id) != self.device_id:
            raise GrantRefused(GrantRefusal.STALE)
        if ent_index is not None and coerce_positive_int(ent_index) != self.ent_index:
            raise GrantRefused(GrantRefusal.STALE)
        if action_target is not None and _canonical_target(action_target) != self.action_target:
            raise GrantRefused(GrantRefusal.STALE)
        return self

    def check_state(self, inventory, policy):
        """Raise a refusal unless the grant was signed for this snapshot, this policy and its current devices."""
        if module_inventory_snapshot_digest(inventory) != self.inventory_digest:
            raise GrantRefused(GrantRefusal.SNAPSHOT)
        if policy.fingerprint != self.policy_fingerprint:
            raise GrantRefused(GrantRefusal.POLICY)
        if duplicate_inventory_indices(inventory):
            raise GrantRefused(GrantRefusal.AMBIGUOUS_INDEX)
        targets = {self.device_id, *(row[1] for row in self.rows or ())}
        if any(policy.member(target) is None for target in targets):
            raise GrantRefused(GrantRefusal.DESTINATION)
        return self


def _canonical_target(action_target) -> dict:
    return {key: coerce_positive_int(value) for key, value in sorted((action_target or {}).items())}


def sign_module_grant(
    *,
    page_device_id,
    device_id,
    server_key,
    action,
    inventory_digest,
    policy_fingerprint,
    render_mode,
    ent_index=None,
    action_target=None,
    rows=None,
) -> str:
    """
    Sign one rendered module action.

    Args:
        page_device_id (int): The device whose Modules page rendered the action.
        device_id (int): The write target.
        server_key (str): The LibreNMS server of the snapshot.
        action (str): One of the ``ACTION_*`` names.
        inventory_digest (str): The digest of the whole cached snapshot.
        policy_fingerprint (str): The fingerprint of the policy the table was rendered with.
        render_mode (str): ``attributed`` for the page table, ``pinned`` for a verify render.
        ent_index (int | None): The acted-on row; None only for the selected install manifest.
        action_target (dict | None): The NetBox object ids the action writes to.
        rows (list | None): The batch rows of a branch list or a selected install manifest.

    Returns:
        str: The signed, compressed token.

    """
    if action not in _ACTIONS or render_mode not in RENDER_MODES:
        raise ValueError(f"unknown module action {action!r} or render mode {render_mode!r}")
    payload = {
        "version": GRANT_VERSION,
        "page_device_id": coerce_positive_int(page_device_id),
        "device_id": coerce_positive_int(device_id),
        "server_key": server_key,
        "action": action,
        "action_target": _canonical_target(action_target),
        "ent_index": coerce_positive_int(ent_index),
        "inventory_digest": inventory_digest,
        "policy_fingerprint": policy_fingerprint,
        "render_mode": render_mode,
        "rows": [list(row) for row in rows] if rows is not None else None,
    }
    if _payload_shape_error(payload) is not None:
        raise ValueError(f"invalid module grant payload: {_payload_shape_error(payload)}")
    return signing.dumps(payload, salt=_SALT, compress=True)


def _valid_row(row, shape) -> bool:
    if not isinstance(row, list) or len(row) != len(shape):
        return False
    for value, kind in zip(row, shape, strict=True):
        if kind is bool:
            if not isinstance(value, bool):
                return False
        elif coerce_positive_int(value) != value:
            return False
    return True


def _payload_shape_error(payload):
    """Return what is wrong with a payload's shape, or None."""
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        return "keys"
    if (
        payload["version"] != GRANT_VERSION
        or not isinstance(payload["action"], str)
        or payload["action"] not in _ACTIONS
    ):
        return "version or action"
    if not isinstance(payload["render_mode"], str) or payload["render_mode"] not in RENDER_MODES:
        return "render mode"
    for key in ("page_device_id", "device_id"):
        if coerce_positive_int(payload[key]) != payload[key]:
            return key
    if not all(isinstance(payload[key], str) and payload[key] for key in ("server_key", "inventory_digest")):
        return "server or digest"
    if not isinstance(payload["policy_fingerprint"], str) or not payload["policy_fingerprint"]:
        return "fingerprint"
    target = payload["action_target"]
    if not isinstance(target, dict) or any(coerce_positive_int(value) != value for value in target.values()):
        return "action target"
    indexless = payload["action"] in _INDEXLESS_ACTIONS
    if (payload["ent_index"] is None) != indexless:
        return "index"
    if not indexless and coerce_positive_int(payload["ent_index"]) != payload["ent_index"]:
        return "index"
    shape = _ROW_SHAPES.get(payload["action"])
    rows = payload["rows"]
    if shape is None:
        return None if rows is None else "rows"
    if not isinstance(rows, list) or not all(_valid_row(row, shape) for row in rows):
        return "rows"
    if len({row[0] for row in rows}) != len(rows):
        return "rows"
    return None


def read_module_grant(token, *, action, page_device_id, server_key) -> ModuleGrant:
    """
    Authenticate one posted grant for *action* on the Modules page of *page_device_id*.

    Raises:
        GrantRefused: STALE when the token is missing, malformed, of another version or another action.

    """
    if not isinstance(token, str) or not token:
        raise GrantRefused(GrantRefusal.STALE)
    try:
        payload = signing.loads(token, salt=_SALT)
    except signing.BadSignature as exc:
        raise GrantRefused(GrantRefusal.STALE) from exc
    if _payload_shape_error(payload) is not None:
        raise GrantRefused(GrantRefusal.STALE)
    if (
        payload["action"] != action
        or payload["page_device_id"] != coerce_positive_int(page_device_id)
        or payload["server_key"] != server_key
    ):
        raise GrantRefused(GrantRefusal.STALE)
    rows = payload["rows"]
    return ModuleGrant(
        page_device_id=payload["page_device_id"],
        device_id=payload["device_id"],
        server_key=payload["server_key"],
        action=payload["action"],
        action_target=payload["action_target"],
        ent_index=payload["ent_index"],
        inventory_digest=payload["inventory_digest"],
        policy_fingerprint=payload["policy_fingerprint"],
        render_mode=payload["render_mode"],
        rows=tuple(tuple(row) for row in rows) if rows is not None else None,
    )
