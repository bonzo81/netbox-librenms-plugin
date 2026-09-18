"""Canonicalize existing Device serials and index exact serial lookups."""

from django.db import migrations

_BATCH_SIZE = 1000
_SERIAL_INDEX = "nblp_dcim_device_serial_idx"
_SERIAL_TABLE = "dcim_device"


def normalize_device_serials(apps, schema_editor):
    """Strip surrounding whitespace from every existing NetBox Device serial."""
    Device = apps.get_model("dcim", "Device")
    db_alias = schema_editor.connection.alias
    pending = []

    for device in Device.objects.using(db_alias).only("pk", "serial").iterator(chunk_size=_BATCH_SIZE):
        raw_serial = device.serial
        normalized = raw_serial.strip() if raw_serial is not None else ""
        if normalized == raw_serial:
            continue
        device.serial = normalized
        pending.append(device)
        if len(pending) == _BATCH_SIZE:
            Device.objects.using(db_alias).bulk_update(pending, ["serial"], batch_size=_BATCH_SIZE)
            pending.clear()

    if pending:
        Device.objects.using(db_alias).bulk_update(pending, ["serial"], batch_size=_BATCH_SIZE)


def ensure_device_serial_index(apps, schema_editor):
    """Create the serial index, reusing a valid copy or repairing an interrupted build."""
    del apps
    connection = schema_editor.connection

    # IF NOT EXISTS also accepts an invalid index left by a failed concurrent build.
    # Inspect the catalog so retries can repair that state without trusting a wrong definition.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                index_state.indisvalid,
                index_state.indisready,
                index_state.indrelid = to_regclass(%s),
                index_state.indisunique,
                index_state.indisprimary,
                index_state.indpred IS NULL,
                index_state.indnatts = index_state.indnkeyatts,
                access_method.amname,
                ARRAY(
                    SELECT attribute.attname
                    FROM unnest(index_state.indkey) WITH ORDINALITY AS key(attnum, position)
                    JOIN pg_attribute AS attribute
                      ON attribute.attrelid = index_state.indrelid
                     AND attribute.attnum = key.attnum
                    WHERE key.position <= index_state.indnkeyatts
                    ORDER BY key.position
                )
            FROM pg_index AS index_state
            JOIN pg_class AS index_class
              ON index_class.oid = index_state.indexrelid
            JOIN pg_namespace AS index_namespace
              ON index_namespace.oid = index_class.relnamespace
            JOIN pg_am AS access_method
              ON access_method.oid = index_class.relam
            WHERE index_class.relname = %s
              AND index_namespace.nspname = current_schema()
            """,
            [_SERIAL_TABLE, _SERIAL_INDEX],
        )
        existing = cursor.fetchone()

    if existing is not None:
        valid, ready, on_device, unique, primary, unfiltered, no_includes, method, columns = existing
        expected_shape = (
            on_device
            and not unique
            and not primary
            and unfiltered
            and no_includes
            and method == "btree"
            and columns == ["serial"]
        )
        if not expected_shape:
            raise RuntimeError(f"Existing index {_SERIAL_INDEX!r} has an incompatible definition")
        if valid and ready:
            return

        schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema_editor.quote_name(_SERIAL_INDEX)}")

    schema_editor.execute(
        f"CREATE INDEX CONCURRENTLY {schema_editor.quote_name(_SERIAL_INDEX)} "
        f"ON {schema_editor.quote_name(_SERIAL_TABLE)} ({schema_editor.quote_name('serial')})"
    )


def drop_device_serial_index(apps, schema_editor):
    """Drop the plugin-owned serial index when reversing the migration."""
    del apps
    schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema_editor.quote_name(_SERIAL_INDEX)}")


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction. Leave both operations
    # non-atomic so every rewrite batch commits independently before the concurrent DDL.
    atomic = False

    dependencies = [
        ("netbox_librenms_plugin", "0011_renormalize_device_type_mappings"),
    ]

    operations = [
        migrations.RunPython(
            normalize_device_serials,
            migrations.RunPython.noop,
            atomic=False,
        ),
        # Device is owned by NetBox's dcim app, so the plugin cannot declare this index
        # through its model state. NetBox does not otherwise index Device.serial.
        migrations.RunPython(
            ensure_device_serial_index,
            drop_device_serial_index,
            atomic=False,
        ),
    ]
