"""Guard against model/migration state drift for the plugin's own models."""

import importlib
import uuid

import pytest


def test_migration_state_carries_the_attributes_netbox_hides_from_makemigrations():
    """Every plugin model field in the final migration state keeps the model's choices, help_text and verbose_name (NetBox's deconstruct drops them, so makemigrations --check cannot see the drift)."""
    from django.apps import apps
    from django.db.migrations.loader import MigrationLoader
    from utilities.migration import EXEMPT_ATTRS, _deconstruct

    app_label = "netbox_librenms_plugin"
    loader = MigrationLoader(None, ignore_no_migrations=True)
    (leaf,) = (node for node in loader.graph.leaf_nodes() if node[0] == app_label)
    state = loader.project_state(leaf, at_end=True)

    def hidden_attrs(field):
        # NetBox keeps Django's stock Field.deconstruct as _deconstruct; it still emits the exempt attributes.
        kwargs = _deconstruct(field)[3]
        return {attr: kwargs.get(attr) for attr in EXEMPT_ATTRS}

    drifted = {}
    for model in apps.get_app_config(app_label).get_models():
        state_fields = state.models[(app_label, model._meta.model_name)].fields
        for field in (*model._meta.local_fields, *model._meta.local_many_to_many):
            model_attrs, state_attrs = hidden_attrs(field), hidden_attrs(state_fields[field.name])
            for attr in EXEMPT_ATTRS:
                if model_attrs[attr] != state_attrs[attr]:
                    drifted[f"{model.__name__}.{field.name}.{attr}"] = (state_attrs[attr], model_attrs[attr])
    assert not drifted, "migration state drifted from the model:\n" + "\n".join(
        f"{key}: {state!r} -> {model!r}" for key, (state, model) in sorted(drifted.items())
    )


@pytest.mark.django_db
@pytest.mark.parametrize("operator_edit", ["custom-data", "tag"])
def test_reverse_bridge_seed_preserves_operator_data(operator_edit):
    """Rollback must keep a seeded row after an operator adds data."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from extras.models import Tag

    from netbox_librenms_plugin.models import PortStackLagPattern

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0019_portstacklagpattern_bridge_name_pattern")
    widened = importlib.import_module("netbox_librenms_plugin.migrations.0021_widen_linux_bridge_pattern")
    row = PortStackLagPattern.objects.get(librenms_os=mod.BRIDGE_OS)
    if operator_edit == "custom-data":
        PortStackLagPattern.objects.filter(pk=row.pk).update(custom_field_data={"operator-note": "keep"})
    else:
        row.tags.add(Tag.objects.create(name="Bridge operator tag", slug="bridge-operator-tag"))

    historical_apps = (
        MigrationExecutor(connection)
        .loader.project_state([("netbox_librenms_plugin", "0019_portstacklagpattern_bridge_name_pattern")])
        .apps
    )
    with connection.schema_editor() as editor:
        # 0021 widened what 0019 seeded, so a rollback reverses it first and 0019's reverse then
        # sees its own value. Skipping that step would make this assert nothing.
        widened.restore_bridge_pattern(historical_apps, editor)
        mod.clear_bridge_pattern(historical_apps, editor)

    row.refresh_from_db()
    if operator_edit == "custom-data":
        assert row.custom_field_data == {"operator-note": "keep"}
    else:
        assert row.tags.filter(slug="bridge-operator-tag").exists()
    assert row.bridge_name_pattern == ""


@pytest.mark.django_db
def test_reverse_bridge_seed_without_content_type():
    """Rollback must work before post-migrate creates the plugin content type."""
    from django.contrib.contenttypes.models import ContentType
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from netbox_librenms_plugin.models import PortStackLagPattern

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0019_portstacklagpattern_bridge_name_pattern")
    widened = importlib.import_module("netbox_librenms_plugin.migrations.0021_widen_linux_bridge_pattern")
    row = PortStackLagPattern.objects.get(librenms_os=mod.BRIDGE_OS)
    ContentType.objects.filter(
        app_label="netbox_librenms_plugin",
        model="portstacklagpattern",
    ).delete()
    historical_apps = (
        MigrationExecutor(connection)
        .loader.project_state([("netbox_librenms_plugin", "0019_portstacklagpattern_bridge_name_pattern")])
        .apps
    )

    with connection.schema_editor() as editor:
        widened.restore_bridge_pattern(historical_apps, editor)
        mod.clear_bridge_pattern(historical_apps, editor)

    assert not PortStackLagPattern.objects.filter(pk=row.pk).exists()


@pytest.mark.django_db
def test_plugin_migrations_do_not_redeclare_squashed_core_ancestors():
    """A plugin migration must not repeat a squashed core dependency from its plugin parent."""
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection, ignore_no_migrations=True)
    plugin_migrations = {
        key: migration for key, migration in loader.disk_migrations.items() if key[0] == "netbox_librenms_plugin"
    }
    assert plugin_migrations, "No plugin migrations were loaded"

    for migration_key, migration in plugin_migrations.items():
        plugin_parents = [dependency for dependency in migration.dependencies if dependency[0] == migration_key[0]]
        inherited_dependencies = {
            ancestor for parent in plugin_parents for ancestor in loader.graph.forwards_plan(parent)
        }
        for dependency in migration.dependencies:
            dependency_migration = loader.disk_migrations.get(dependency)
            if (
                dependency[0] != migration_key[0]
                and dependency in inherited_dependencies
                and dependency_migration is not None
                and dependency_migration.replaces
            ):
                raise AssertionError(
                    f"{migration_key} repeats squashed core dependency {dependency}; "
                    "the plugin parent already reaches it"
                )


def test_plugin_migrations_have_one_leaf():
    """Every plugin migration must belong to one ordered migration graph."""
    from django.db.migrations.graph import MigrationGraph
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, load=False)
    loader.load_disk()
    graph = MigrationGraph()
    migrations = {key: value for key, value in loader.disk_migrations.items() if key[0] == "netbox_librenms_plugin"}
    assert migrations, "No plugin migrations were loaded"
    for key, migration in migrations.items():
        graph.add_node(key, migration)
    for key, migration in migrations.items():
        for dependency in migration.dependencies:
            if dependency[0] == key[0] and not dependency[1].startswith("__"):
                graph.add_dependency(migration, key, dependency)
    graph.validate_consistency()
    graph.ensure_not_cyclic()

    leaves = graph.leaf_nodes()
    assert len(leaves) == 1, f"Plugin migrations have multiple leaves: {leaves}"


@pytest.mark.django_db
@pytest.mark.parametrize("operator_edited", [False, True])
def test_reverse_inventory_seed_preserves_operator_rules(operator_edited):
    """Rollback cannot identify whether matching rules belong to the operator."""
    from django.apps import apps
    from django.db import connection, migrations
    from netbox_librenms_plugin.models import InventoryIgnoreRule, NormalizationRule

    module = importlib.import_module("netbox_librenms_plugin.migrations.0017_inventory_class_include_rule")
    InventoryIgnoreRule.objects.filter(name=module.DEFAULT_RULE["name"]).delete()
    NormalizationRule.objects.filter(scope="serial", match_pattern=module.SERIAL_RULE["match_pattern"]).delete()
    inventory_rule = InventoryIgnoreRule.objects.create(**module.DEFAULT_RULE)
    serial_rule = NormalizationRule.objects.create(**module.SERIAL_RULE)
    if operator_edited:
        inventory_rule.description = "Operator-owned inventory rule"
        inventory_rule.save()
        serial_rule.description = "Operator-owned serial rule"
        serial_rule.save()
    before_inventory = InventoryIgnoreRule.objects.filter(pk=inventory_rule.pk).values().get()
    before_serial = NormalizationRule.objects.filter(pk=serial_rule.pk).values().get()
    operation = next(op for op in module.Migration.operations if isinstance(op, migrations.RunPython))
    with connection.schema_editor() as editor:
        operation.code(apps, editor)
        operation.reverse_code(apps, editor)
    assert InventoryIgnoreRule.objects.filter(pk=inventory_rule.pk).values().get() == before_inventory
    assert NormalizationRule.objects.filter(pk=serial_rule.pk).values().get() == before_serial


@pytest.mark.django_db
def test_inventory_seed_survives_duplicate_operator_rules():
    """Neither seeded model enforces uniqueness, so the seed lookups must not assume one row."""
    from django.apps import apps
    from django.db import connection, migrations

    from netbox_librenms_plugin.models import InventoryIgnoreRule, NormalizationRule

    module = importlib.import_module("netbox_librenms_plugin.migrations.0017_inventory_class_include_rule")
    InventoryIgnoreRule.objects.filter(name=module.DEFAULT_RULE["name"]).delete()
    NormalizationRule.objects.filter(scope="serial", match_pattern=module.SERIAL_RULE["match_pattern"]).delete()
    # An operator may keep two rules that share the seed's lookup fields; both are valid rows.
    for _ in range(2):
        InventoryIgnoreRule.objects.create(**module.DEFAULT_RULE)
        NormalizationRule.objects.create(**module.SERIAL_RULE)

    operation = next(op for op in module.Migration.operations if isinstance(op, migrations.RunPython))
    with connection.schema_editor() as editor:
        operation.code(apps, editor)

    # The seed found existing rows, so it must not have added a third of either.
    assert InventoryIgnoreRule.objects.filter(name=module.DEFAULT_RULE["name"]).count() == 2
    assert (
        NormalizationRule.objects.filter(scope="serial", match_pattern=module.SERIAL_RULE["match_pattern"]).count() == 2
    )


def test_plugin_cross_app_migration_dependencies_resolve():
    """Report every missing dependency without validating unrelated installed apps."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, load=False, replace_migrations=False)
    loader.load_disk()
    migrations = loader.disk_migrations
    plugin_migrations = {key: value for key, value in migrations.items() if key[0] == "netbox_librenms_plugin"}
    assert plugin_migrations, "No plugin migrations were loaded"

    missing = []
    for key, migration in sorted(plugin_migrations.items()):
        for dependency in migration.dependencies:
            if dependency[0] == key[0] or dependency[1].startswith("__") or dependency in migrations:
                continue
            pending = list(migration.dependencies)
            ancestors = set()
            while pending:
                ancestor = pending.pop()
                if ancestor in ancestors or ancestor not in migrations:
                    continue
                ancestors.add(ancestor)
                pending.extend(migrations[ancestor].dependencies)
            available = sorted(name for app, name in ancestors if app == dependency[0])
            newest = available[-1] if available else "none"
            kind = "initial" if migration.initial else "non-initial"
            missing.append(
                f"{key[1]}.py ({kind}) -> {dependency[0]}.{dependency[1]}; "
                f"newest available ancestor in {dependency[0]}: {newest}"
            )

    assert not missing, (
        "Missing cross-app migration dependencies:\n"
        + "\n".join(missing)
        + "\nDrop a redundant edge only if ancestry guarantees ordering. Otherwise use a live dependency, "
        "or __first__ when an initial migration needs app ordering."
    )


@pytest.mark.django_db
def test_conftest_restores_exactly_the_rules_migration_0010_seeds():
    """
    The seed-restore signatures in conftest must match what the migration's own insert produces.

    conftest re-runs the migration's insert to repair a transactional flush, but its intactness
    check compares signatures it declares itself. Pin the two against each other so a change to
    the migration's seeded rules cannot silently leave the restore reporting "intact".
    """
    from types import SimpleNamespace

    from django.apps import apps as global_apps

    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.tests.conftest import _seeded_ignore_rule_signatures

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0010_inventory_and_mapping_models")
    schema_editor = SimpleNamespace(connection=SimpleNamespace(alias="default"))
    signature_fields = ("name", "match_type", "pattern", "action", "require_serial_match_parent")

    InventoryIgnoreRule.objects.all().delete()
    mod._insert_default_inventory_ignore_rules(global_apps, schema_editor)

    produced = set(InventoryIgnoreRule.objects.values_list(*signature_fields))
    declared = {
        tuple(signature[field] for field in signature_fields) for _model, signature in _seeded_ignore_rule_signatures()
    }

    assert produced == declared


def _seeded_ignore_rule_count():
    """Count the ignore rules the restore declares, across every migration that seeds one."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.tests.conftest import (
        _seeded_ignore_rule_signatures,
        _seeded_rule_rows,
        seeded_class_rule_rows,
        seeded_fixed_platform_rule_rows,
    )

    declarative = sum(1 for model, _lookup, _defaults in _seeded_rule_rows() if model is InventoryIgnoreRule)
    migrations = len(list(seeded_class_rule_rows())) + len(list(seeded_fixed_platform_rule_rows()))
    return len(list(_seeded_ignore_rule_signatures())) + declarative + migrations


@pytest.mark.django_db
def test_the_seeded_ignore_rules_survive_a_seed_restore():
    """restore_seeded_state() must put every migration's seeded ignore rules back after a flush."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.tests.conftest import restore_seeded_state

    expected = _seeded_ignore_rule_count()
    InventoryIgnoreRule.objects.all().delete()

    assert restore_seeded_state(force=False) is True
    assert InventoryIgnoreRule.objects.count() == expected
    # A second pass must not duplicate them.
    restore_seeded_state(force=True)
    assert InventoryIgnoreRule.objects.count() == expected


@pytest.mark.django_db
def test_a_seed_restore_puts_back_the_rule_of_each_cisco_manufacturer():
    """Migration 0026 seeds one rule per Cisco manufacturer, so the restore and the intactness probe cover each."""
    from dcim.models import Manufacturer

    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.tests.conftest import _seeds_are_intact, restore_seeded_state

    cisco = [Manufacturer.objects.create(name=f"Cisco {slug}", slug=slug) for slug in ("cisco", "cisco-systems")]
    assert not _seeds_are_intact()
    InventoryIgnoreRule.objects.all().delete()

    assert restore_seeded_state(force=False) is True
    assert _seeds_are_intact()
    seeded = InventoryIgnoreRule.objects.filter(match_type="model_matches_device_type")
    assert sorted(seeded.values_list("manufacturer_id", flat=True)) == sorted(m.pk for m in cisco)
    assert InventoryIgnoreRule.objects.count() == _seeded_ignore_rule_count()
    restore_seeded_state(force=True)
    assert InventoryIgnoreRule.objects.count() == _seeded_ignore_rule_count()


@pytest.mark.django_db
def test_the_seeded_ignore_rules_are_present_before_a_test_body_runs():
    """
    Every test starts with the seeded rules, including one that follows a flush.

    A ``transaction=True`` test truncates the tables, so without the restore this passes or fails
    purely on xdist scheduling: the modules sync reads these rules on every render.
    """
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    assert InventoryIgnoreRule.objects.count() == _seeded_ignore_rule_count()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "librenms_type, netbox_type, error",
    [
        ("legacy-test", "", "empty NetBox type"),
        ("", "1000base-t", "empty LibreNMS type"),
        ("legacy-test", "1000base-t", None),
    ],
)
def test_interface_rules_validate_legacy_output_before_schema_changes(librenms_type, netbox_type, error):
    from django.db import connection, migrations
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection)
    state = loader.project_state(("netbox_librenms_plugin", "0021_widen_linux_bridge_pattern"))
    model = state.apps.get_model("netbox_librenms_plugin", "InterfaceTypeMapping")
    model._meta.db_table = "test_legacy_interface_type_mapping"
    module = importlib.import_module("netbox_librenms_plugin.migrations.0022_interface_rules")
    operation = module.Migration.operations[0]
    assert isinstance(operation, migrations.RunPython)
    with connection.schema_editor() as editor:
        editor.create_model(model)
    try:
        row = model.objects.create(librenms_type=librenms_type, netbox_type=netbox_type)
        with connection.schema_editor() as editor:
            if error:
                with pytest.raises(RuntimeError, match=error):
                    operation.code(state.apps, editor)
            else:
                operation.code(state.apps, editor)
        row.refresh_from_db()
        assert row.netbox_type == netbox_type
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(model)


_TRGM_MIGRATION = "netbox_librenms_plugin.migrations.0030_serial_trigram_indexes"


def _trigram_index_schemas():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT index_class.relname, index_class.relnamespace::regnamespace::text,
                   operator_class.opcnamespace::regnamespace::text, index_state.indisvalid
            FROM pg_index AS index_state
            JOIN pg_class AS index_class ON index_class.oid = index_state.indexrelid
            JOIN pg_opclass AS operator_class ON operator_class.oid = index_state.indclass[0]
            WHERE index_class.relname IN ('nblp_dcim_device_serial_trgm_idx', 'nblp_dcim_module_serial_trgm_idx')
              AND pg_table_is_visible(index_class.oid)
            ORDER BY index_class.relname
            """
        )
        return cursor.fetchall()


def _trigram_index_oids():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, oid FROM pg_class"
            " WHERE relname IN ('nblp_dcim_device_serial_trgm_idx', 'nblp_dcim_module_serial_trgm_idx')"
            " AND pg_table_is_visible(oid)"
            " ORDER BY relname"
        )
        return cursor.fetchall()


def _probe_schema_name():
    """Return a schema name that no other run uses, so the cleanup drops only the schema of this test."""
    return f"nblp_probe_{uuid.uuid4().hex}"


def _run_index_step(step):
    from django.db import connection

    with connection.schema_editor(atomic=False) as editor:
        getattr(importlib.import_module(_TRGM_MIGRATION), step)(None, editor)


@pytest.mark.django_db(transaction=True)
def test_the_trigram_migration_finds_its_indexes_in_the_schema_of_their_table():
    """A search path whose first schema exists makes current_schema() differ from the schema of the tables."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'pg_trgm'")
        (extension_schema,) = cursor.fetchone()
    schema = _probe_schema_name()
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA {schema}")
            cursor.execute(f"SET search_path = {schema}, public")
        _run_index_step("ensure_serial_trigram_indexes")
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET search_path")
            cursor.execute(f"DROP SCHEMA IF EXISTS {schema}")

    assert _trigram_index_schemas() == [
        ("nblp_dcim_device_serial_trgm_idx", "public", extension_schema, True),
        ("nblp_dcim_module_serial_trgm_idx", "public", extension_schema, True),
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("schema_suffix", ["ops", '"ops%'], ids=["plain", "quoted"])
def test_the_trigram_migration_uses_the_operator_class_of_the_installed_extension(schema_suffix):
    """pg_trgm installed in a schema outside the search path still gives the indexes their operator class."""
    from django.db import OperationalError, connection
    from psycopg import sql
    from psycopg.errors import LockNotAvailable

    from netbox_librenms_plugin.tests.lock_conflict_helpers import second_connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_has_role(extowner, 'MEMBER'), pg_get_userbyid(extowner), extnamespace::regnamespace::text"
            " FROM pg_extension WHERE extname = 'pg_trgm'"
        )
        can_move, owner, home = cursor.fetchone()
    if not can_move:
        pytest.fail(f"Only the owner of pg_trgm can move it: run the tests as {owner!r} or as a superuser")
    extension_schema = f"{_probe_schema_name()}_trgm{schema_suffix}"
    table_schema = f"{_probe_schema_name()}_tables{schema_suffix}"
    extension = sql.Identifier(extension_schema)
    tables = sql.Identifier(table_schema)
    indexes = importlib.import_module(_TRGM_MIGRATION)._TRIGRAM_INDEXES
    moved = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(extension))
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(tables))
            cursor.execute(sql.SQL("ALTER EXTENSION pg_trgm SET SCHEMA {}").format(extension))
            moved = True
            cursor.execute(sql.SQL("SET search_path = {}").format(tables))
            cursor.execute("SELECT quote_ident(%s), quote_ident(%s)", [table_schema, extension_schema])
            table_name, extension_name = cursor.fetchone()
            for _, table in indexes:
                cursor.execute(sql.SQL("CREATE TABLE {} (serial varchar(50))").format(sql.Identifier(table)))

        name, table = indexes[0]
        with second_connection() as other, other.cursor() as writer, connection.cursor() as cursor:
            writer.execute(
                sql.SQL("INSERT INTO {} (serial) VALUES ('ABC123')").format(sql.Identifier(table_schema, table))
            )
            cursor.execute("SET lock_timeout = '1s'")
            try:
                with pytest.raises(OperationalError) as caught:
                    cursor.execute(
                        sql.SQL("CREATE INDEX CONCURRENTLY {} ON {} USING gin ((btrim(serial, {})) {})").format(
                            sql.Identifier(name),
                            sql.Identifier(table),
                            sql.Literal(" \t\n\r\v\f"),
                            sql.Identifier(extension_schema, "gin_trgm_ops"),
                        )
                    )
                assert isinstance(caught.value.__cause__, LockNotAvailable)
            finally:
                cursor.execute("RESET lock_timeout")
        assert _trigram_index_schemas() == [(name, table_name, extension_name, False)]
        invalid_oids = _trigram_index_oids()

        _run_index_step("ensure_serial_trigram_indexes")
        built, built_oids = _trigram_index_schemas(), _trigram_index_oids()
        assert built_oids[0] != invalid_oids[0]
        _run_index_step("ensure_serial_trigram_indexes")
        assert _trigram_index_oids() == built_oids
        _run_index_step("drop_serial_trigram_indexes")
        assert _trigram_index_schemas() == []
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET search_path")
            if moved:
                cursor.execute(f"ALTER EXTENSION pg_trgm SET SCHEMA {home}")
            cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(tables))
            cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {}").format(extension))

    assert built == [(name, table_name, extension_name, True) for name, _ in indexes]


@pytest.mark.django_db(transaction=True)
def test_the_trigram_migration_reverse_does_not_block_new_reads_behind_an_open_read():
    """A plain DROP INDEX queues for ACCESS EXCLUSIVE behind an open read, and a new read waits behind it."""
    import threading
    import time

    from dcim.models import Device
    from django.db import connection, transaction

    from netbox_librenms_plugin.tests.conftest import run_in_threads

    reading, release = threading.Event(), threading.Event()

    def open_read():
        try:
            with transaction.atomic():
                Device.objects.exists()
                reading.set()
                assert release.wait(30)
        finally:
            connection.close()

    def reverse():
        assert reading.wait(30)
        try:
            _run_index_step("drop_serial_trigram_indexes")
        finally:
            connection.close()

    def new_read():
        try:
            assert reading.wait(30)
            deadline = time.monotonic() + 30
            with connection.cursor() as cursor:
                while True:
                    cursor.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock' AND query LIKE 'DROP INDEX%'"
                    )
                    if cursor.fetchone()[0]:
                        break
                    assert time.monotonic() < deadline, "the reverse did not start to wait"
                    time.sleep(0.05)
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '2s'")
                cursor.execute("SELECT count(*) FROM dcim_device")
                return cursor.fetchone()[0]
        finally:
            release.set()
            connection.close()

    try:
        _reader, _reverse, count = run_in_threads(open_read, reverse, new_read, barriers=(), timeout=60)
        assert count == Device.objects.count()
        assert _trigram_index_schemas() == []
    finally:
        _run_index_step("ensure_serial_trigram_indexes")
