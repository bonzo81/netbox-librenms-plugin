"""
The row lock of the late write: which rows it locks, in which mode, and where in the save.

The late write takes its lock in ``pre_save``, after NetBox's tagged-VLAN clear, in the mode the
``UPDATE`` would take. So it adds no wait that today's ``UPDATE`` does not have.
"""

import pytest
from dcim.models import Interface
from django.db import connection
from virtualization.models import VMInterface

from netbox_librenms_plugin.transactions import key_columns, save_at_version

# ---------------------------------------------------------------------------
# key_columns agrees with PostgreSQL; a versioned save is always checked
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("model", [Interface, VMInterface], ids=["interface", "vminterface"])
def test_key_columns_are_the_columns_of_the_unique_indexes_postgresql_uses_for_foreign_keys(model):
    """PostgreSQL's key columns: the key columns of each unique, immediate index with no predicate and no expression."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT attribute.attname
            FROM pg_index AS index
            CROSS JOIN LATERAL unnest(index.indkey::int2[]) WITH ORDINALITY AS key(attnum, position)
            JOIN pg_attribute AS attribute
              ON attribute.attrelid = index.indrelid AND attribute.attnum = key.attnum
            WHERE index.indrelid = %s::regclass
              AND index.indisunique
              AND index.indimmediate
              AND index.indpred IS NULL
              AND index.indexprs IS NULL
              AND key.position <= index.indnkeyatts
            """,
            [model._meta.db_table],
        )
        database_key_columns = {row[0] for row in cursor.fetchall()}

    assert "primary_mac_address_id" in database_key_columns, "precondition: the one-to-one MAC is a key column"
    assert key_columns(model) == database_key_columns


@pytest.mark.django_db
def test_a_versioned_save_of_a_model_with_no_version_check_fails():
    """Only the interface models have the pre_save check; a save that nothing checked must not pass as checked."""
    from dcim.models import Site

    site = Site.objects.create(name="late-lock-unchecked", slug="late-lock-unchecked")
    site.description = "changed"

    with pytest.raises(RuntimeError, match="No row-version check is connected for Site"):
        save_at_version(site, version="0", changed_columns={"description"}, name=site.name)
