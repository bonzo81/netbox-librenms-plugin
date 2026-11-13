"""Require pg_trgm and index the trimmed Device and Module serials for the serial identity lookup."""

from django.contrib.postgres.operations import TrigramExtension
from django.db import DatabaseError, migrations, models

# serial_identity.TRIM_CHARACTERS; a migration does not import app code, and a test compares the two.
_SERIAL_TRIM_CHARACTERS = " \t\n\r\v\f"
_TRIGRAM_INDEXES = (
    ("nblp_dcim_device_serial_trgm_idx", "dcim_device"),
    ("nblp_dcim_module_serial_trgm_idx", "dcim_module"),
)
_EXTENSION_SQL = "CREATE EXTENSION IF NOT EXISTS pg_trgm;"


class _RequirePgTrgm(TrigramExtension):
    """Create pg_trgm, or stop with the SQL that a database owner runs; a reverse keeps the extension."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        try:
            super().database_forwards(app_label, schema_editor, from_state, to_state)
        except DatabaseError as exc:
            raise RuntimeError(
                f"The plugin needs the PostgreSQL extension pg_trgm. Run {_EXTENSION_SQL!r} as a database owner, "
                "then run the migrations again."
            ) from exc

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        # Other objects can use pg_trgm, so the plugin does not drop it.
        return


def _existing_index(connection, table, name):
    with connection.cursor() as cursor:
        # pg_get_expr follows quote_all_identifiers, so read the expression under a known setting.
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
                index_state.indpred IS NULL,
                index_state.indnatts = index_state.indnkeyatts,
                access_method.amname,
                index_state.indnkeyatts = 1 AND index_state.indkey[0] = 0,
                operator_class.opcname,
                pg_get_expr(index_state.indexprs, index_state.indrelid)
            FROM pg_index AS index_state
            JOIN pg_class AS index_class ON index_class.oid = index_state.indexrelid
            JOIN pg_namespace AS index_namespace ON index_namespace.oid = index_class.relnamespace
            JOIN pg_am AS access_method ON access_method.oid = index_class.relam
            LEFT JOIN pg_opclass AS operator_class ON operator_class.oid = index_state.indclass[0]
            WHERE index_class.relname = %s
              AND index_namespace.nspname = current_schema()
            """,
            [table, name],
        )
        existing = cursor.fetchone()
        cursor.execute(f"SET quote_all_identifiers = {'on' if previous_quoting == 'on' else 'off'}")
    return existing


def ensure_serial_trigram_indexes(apps, schema_editor):
    """Create both trigram indexes, reusing a valid copy or repairing an interrupted concurrent build."""
    del apps
    connection = schema_editor.connection
    # A literal, not a bound parameter: CREATE INDEX takes none under server-side binding.
    trim_literal = "'" + _SERIAL_TRIM_CHARACTERS.replace("'", "''") + "'"
    for name, table in _TRIGRAM_INDEXES:
        existing = _existing_index(connection, table, name)
        if existing is not None:
            valid, ready, on_table, unique, unfiltered, no_includes, method, one_expression, opclass, expression = (
                existing
            )
            expected_shape = (
                on_table
                and not unique
                and unfiltered
                and no_includes
                and method == "gin"
                and one_expression
                and opclass == "gin_trgm_ops"
                and expression == f"btrim((serial)::text, '{_SERIAL_TRIM_CHARACTERS}'::text)"
            )
            if not expected_shape:
                raise RuntimeError(f"Existing index {name!r} has an incompatible definition")
            if valid and ready:
                continue
            schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema_editor.quote_name(name)}")
        schema_editor.execute(
            f"CREATE INDEX CONCURRENTLY {schema_editor.quote_name(name)} ON {schema_editor.quote_name(table)} "
            f"USING gin ((BTRIM({schema_editor.quote_name('serial')}, {trim_literal})) gin_trgm_ops)"
        )


def drop_serial_trigram_indexes(apps, schema_editor):
    """Drop the plugin-owned trigram indexes when reversing the migration."""
    del apps
    for name, _table in _TRIGRAM_INDEXES:
        # Not CONCURRENTLY: a reverse can run inside a transaction, and a drop is short.
        schema_editor.execute(f"DROP INDEX IF EXISTS {schema_editor.quote_name(name)}")


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    atomic = False

    dependencies = [
        ("netbox_librenms_plugin", "0029_normalizationrule_vlan_name_scope"),
    ]

    operations = [
        _RequirePgTrgm(),
        # Device and Module belong to NetBox's dcim app, so the plugin cannot declare these indexes
        # in its model state. serial_identity reads candidates through them.
        migrations.RunPython(ensure_serial_trigram_indexes, drop_serial_trigram_indexes, atomic=False),
        migrations.AlterField(
            model_name="normalizationrule",
            name="scope",
            field=models.CharField(
                choices=[
                    ("module_type", "Module Type"),
                    ("device_type", "Device Type"),
                    ("module_bay", "Module Bay"),
                    ("serial", "Serial"),
                    ("vlan_name", "VLAN Name"),
                ],
                db_index=True,
                help_text="Which lookup this rule applies to. A serial rule extracts the part of a serial that "
                "identifies the hardware; the stored serial keeps the LibreNMS text. A VLAN name rule rewrites "
                "the proposed VLAN name.",
                max_length=50,
            ),
        ),
        migrations.AlterField(
            model_name="normalizationrule",
            name="replacement",
            field=models.CharField(
                help_text="Replacement string (supports regex back-references \\1, \\2, …). A serial rule "
                "takes exactly one group reference, such as \\1 or \\g<name>, and keeps that group of a full match.",
                max_length=500,
            ),
        ),
    ]
