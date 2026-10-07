"""
Test fixture helpers for the stored object server mapping.

Tests seed and inspect the stored mapping only through these helpers. This is the one test module
that reaches the private storage adapter of ``server_mappings``.
"""

import copy

from netbox_librenms_plugin.server_mappings import ChangeOutcome, assign_own, attach_oob, set_preference

# The reviewed test seam to the raw storage adapter.
# nosemgrep: no-stored-mapping-access-private  # noqa: ERA001
from netbox_librenms_plugin.server_mappings import (
    _MAPPING_KEY,
    _STORAGE_FIELDS,
    _put_on,
    _put_stored_value,
    _stored_value,
)

_SEEDABLE_OUTCOMES = frozenset({ChangeOutcome.APPLIED, ChangeOutcome.UNCHANGED})


def _save(obj, fields):
    if obj.pk is None:
        obj.save()
    else:
        obj.save(update_fields=sorted(fields))


def _seed(obj, change):
    if change.outcome not in _SEEDABLE_OUTCOMES:
        raise ValueError(f"The builder skipped the seed: {change.outcome}.")
    _put_on(obj, change)


def apply_mapping_change(obj, change):
    """Put a built mapping change on *obj*, with no claim, no owner check and no save; return *obj*."""
    _put_on(obj, change)
    return obj


def seed_mapping(obj, server="default", *, own=None, oob=None, oob_type="oob", preferred=False, save=True):
    """
    Seed the mapping of *obj* on *server* through the real builders; return *obj*.

    *own* is the own LibreNMS ID, *oob* the OOB controller ID with *oob_type*, and *preferred* stores
    *server* as the preferred server. A builder that skips the write fails the setup. With *save*,
    save *obj*.
    """
    if own is None and oob is None and not preferred:
        raise ValueError("seed_mapping() needs own, oob or preferred.")
    if own is not None:
        _seed(obj, assign_own(obj, server, own))
    if oob is not None:
        _seed(obj, attach_oob(obj, server, oob, oob_type=oob_type))
    if preferred:
        _seed(obj, set_preference(obj, server))
    if save:
        # An unchanged build writes no field, but the object can hold an unsaved seed.
        _save(obj, _STORAGE_FIELDS)
    return obj


def seed_stored_mapping(obj, value, *, save=False):
    """
    Store a copy of the raw mapping *value* on *obj*, for a legacy, malformed or duplicate state.

    With *save*, save *obj*. Returns *obj*.
    """
    fields = _put_stored_value(obj, value)
    if save:
        _save(obj, fields)
    return obj


def seed_stored_mapping_row(obj, value):
    """
    Write the raw mapping *value* to the database row of *obj* only, as a concurrent writer does.

    *obj* keeps the value that it loaded. The write sends no signal and records no change. The other
    custom fields of the row stay. Returns *obj*.
    """
    row = type(obj).objects.get(pk=obj.pk)
    fields = _put_stored_value(row, value)
    type(obj).objects.filter(pk=obj.pk).update(**{field: getattr(row, field) for field in fields})
    return obj


def stored_mapping_for_test(obj):
    """Return a copy of the raw mapping that *obj* stores, or None when it stores none."""
    return copy.deepcopy(_stored_value(obj))


def mapping_from_change_record(change, *, before):
    """
    Return the raw stored mapping in the before-state (*before*) or the after-state of an ObjectChange.

    Raises:
        ValueError: The record has no data for that side, for example the before-state of a create.

    """
    data = change.prechange_data if before else change.postchange_data
    if data is None:
        raise ValueError(f"The change record has no {'before' if before else 'after'}-state.")
    return data["custom_fields"].get(_MAPPING_KEY)
