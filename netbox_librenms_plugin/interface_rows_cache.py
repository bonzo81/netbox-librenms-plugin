"""
Keep the computed rows of an interfaces tab while none of their inputs change.

Computing the rows reads every NetBox interface of the device and compares each one with the
LibreNMS snapshot. That takes seconds on a device with thousands of interfaces, and a filter
request needs the same rows again. The key holds every input the rows read: the snapshot, the
viewer, the tab options, and a fingerprint (row count and last change) of each NetBox table
involved. A save, a create or a delete in one of those tables gives a new key. The timeout limits
how long a change that leaves no trace in the fingerprint (an object permission edit, a queryset
``update()``) can stay unseen.
"""

import hashlib
import pickle
import zlib

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db.models import Count, Max
from dcim.models import MACAddress
from ipam.models import VLAN, VLANGroup

from netbox_librenms_plugin import __version__
from netbox_librenms_plugin.interface_rules import rule_tables

ROWS_TIMEOUT = 300


def _table_fingerprint(queryset):
    """Return the row count and the newest change of *queryset*, which every save and delete moves."""
    # The aliases name these queries in a query log, apart from the counts the tab itself runs.
    state = queryset.aggregate(rows_cache_count=Count("pk"), rows_cache_changed=Max("last_updated"))
    changed = state["rows_cache_changed"]
    return state["rows_cache_count"], changed.isoformat() if changed else None


def netbox_fingerprint(owners, interfaces):
    """
    Return the state of the NetBox rows that the computed interface rows read.

    Args:
        owners: Queryset of the devices or virtual machines whose interfaces the tab shows.
        interfaces: Queryset of those interfaces.

    Returns:
        tuple: One (count, last change) pair per table.

    """
    interface_type = ContentType.objects.get_for_model(interfaces.model)
    macs = MACAddress.objects.filter(
        assigned_object_type=interface_type, assigned_object_id__in=interfaces.values("pk")
    )
    return tuple(
        _table_fingerprint(queryset)
        for queryset in (
            owners,
            interfaces,
            macs,
            VLAN.objects.all(),
            VLANGroup.objects.all(),
            *rule_tables(),
        )
    )


def rows_key(*parts):
    """Return the cache key of the rows computed from *parts*, every input the rows read."""
    # The rows hold pickled model instances, so only the code that wrote them reads them back.
    digest = hashlib.sha256(repr((__version__, settings.RELEASE.version, *parts)).encode()).hexdigest()
    return f"librenms_interface_rows_{digest}"


def cached_rows(key, compute):
    """Return the rows stored under *key*, or compute them with *compute* and store them."""
    stored = cache.get(key)
    if stored is not None:
        return pickle.loads(zlib.decompress(stored))
    rows = compute()
    # Django's cache pickles every value too, so this trusts the cache no further; zlib keeps 3 MB of rows near 120 KB.
    cache.set(key, zlib.compress(pickle.dumps(rows, protocol=pickle.HIGHEST_PROTOCOL)), ROWS_TIMEOUT)
    return rows
