"""Index the trimmed Device serial lookup."""

from django.db import migrations

_SERIAL_INDEX = "nblp_dcim_device_serial_trim_idx"
_SERIAL_TABLE = "dcim_device"
_SERIAL_TRIM_CHARACTERS = " \t\n\r\v\f"


def ensure_device_serial_trim_index(apps, schema_editor):
    """Create the serial index, reusing a valid copy or repairing an interrupted build."""
    del apps
    connection = schema_editor.connection

    # IF NOT EXISTS also accepts an invalid index left by a failed concurrent build.
    # Inspect the catalog so retries can repair that state without trusting a wrong definition.
    with connection.cursor() as cursor:
        # pg_get_expr renders through the session's quoting rules, and quote_all_identifiers=on
        # yields "btrim"(("serial")::"text", ...) instead of btrim((serial)::text, ...). The shape
        # check below compares that text, so read it under a known setting rather than trying to
        # match every spelling, and restore whatever the deployment had set.
        cursor.execute("SHOW quote_all_identifiers")
        previous_quoting = cursor.fetchone()[0]
        cursor.execute("SET quote_all_identifiers = off")
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
                index_state.indnkeyatts = 1 AND index_state.indkey[0] = 0,
                pg_get_expr(index_state.indexprs, index_state.indrelid)
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
        cursor.execute(f"SET quote_all_identifiers = {'on' if previous_quoting == 'on' else 'off'}")

    if existing is not None:
        valid, ready, on_device, unique, primary, unfiltered, no_includes, method, single_expression, expression = (
            existing
        )
        expected_shape = (
            on_device
            and not unique
            and not primary
            and unfiltered
            and no_includes
            and method == "btree"
            and single_expression
            and expression == f"btrim((serial)::text, '{_SERIAL_TRIM_CHARACTERS}'::text)"
        )
        if not expected_shape:
            raise RuntimeError(f"Existing index {_SERIAL_INDEX!r} has an incompatible definition")
        if valid and ready:
            return

        schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema_editor.quote_name(_SERIAL_INDEX)}")

    # CREATE INDEX takes no bound parameters. Django's psycopg3 backend hides that by binding
    # client-side by default, but a deployment setting OPTIONS["server_side_binding"] = True sends
    # the parameter to PostgreSQL, which answers "could not determine data type of parameter $1"
    # and leaves the migration unapplied. Inline the character set as a literal instead.
    trim_literal = "'" + _SERIAL_TRIM_CHARACTERS.replace("'", "''") + "'"
    schema_editor.execute(
        f"CREATE INDEX CONCURRENTLY {schema_editor.quote_name(_SERIAL_INDEX)} "
        f"ON {schema_editor.quote_name(_SERIAL_TABLE)} "
        f"(BTRIM({schema_editor.quote_name('serial')}, {trim_literal}))"
    )


def drop_device_serial_trim_index(apps, schema_editor):
    """Drop the plugin-owned serial index when reversing the migration."""
    del apps
    schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema_editor.quote_name(_SERIAL_INDEX)}")


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    atomic = False

    dependencies = [
        ("netbox_librenms_plugin", "0017_inventory_class_include_rule"),
    ]

    operations = [
        # Device is owned by NetBox's dcim app, so the plugin cannot declare this index
        # through its model state. It serves the trimmed-serial fallback in find_devices_by_serial.
        migrations.RunPython(
            ensure_device_serial_trim_index,
            drop_device_serial_trim_index,
            atomic=False,
        ),
    ]
