"""
A real second database connection, for tests that need PostgreSQL to raise a real lock conflict.

The second connection is a separate backend session on the test database. It holds its row locks
in its own open transaction, so a statement on the test's connection that needs one of those rows
waits (and fails with ``55P03`` under ``lock_timeout``) or fails at once with ``NOWAIT``. It sees
only committed rows: a test that locks a row it created itself must be transactional.
"""

import re
import time
from contextlib import contextmanager

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connection, connections


@contextmanager
def second_connection():
    """Yield a second connection in its own transaction; roll it back and close it on exit."""
    other = connections.create_connection(DEFAULT_DB_ALIAS)
    other.set_autocommit(False)
    try:
        yield other
    finally:
        other.rollback()
        other.close()


def locks_row(sql, model, pk):
    """Return whether the SQL statement *sql* locks the *model* row *pk*, at any lock strength."""
    table = re.escape(model._meta.db_table)
    keyword = r"(?:WHERE|ON|USING|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|ORDER|LIMIT|FOR)\b"
    # The target table can come after FROM or after any JOIN.
    source = re.compile(rf'\b(?:FROM|JOIN) (?:"{table}"|{table}\b)(?: (?:AS )?(?!{keyword})("?)(\w+)\1)?')
    row_lock = re.compile(r'\bFOR (?:UPDATE|NO KEY UPDATE|SHARE|KEY SHARE)\b(?: OF ("?\w+"?(?:, "?\w+"?)*))?')
    # A clause without OF locks every table; OF locks only the tables it names.
    clauses = row_lock.findall(sql)
    where = sql.partition(" WHERE ")[2]
    # A self-join reads the table more than once, so every alias must be checked.
    for read in source.finditer(sql):
        name = read.group(2)
        locked = name or model._meta.db_table
        if not any(not of or locked in of.replace('"', "").split(", ") for of in clauses):
            continue
        qualifier = rf'"{re.escape(name)}"|(?<![\w"]){re.escape(name)}' if name else rf'"{table}"|(?<![\w"]){table}'
        # The key is qualified by the table or its alias, or it stands alone (not after another qualifier).
        column = rf'(?:(?:{qualifier})\.|(?<![\w."]))"?id"?'
        key = rf"{column} (?:= {pk}\b|IN \([^)]*\b{pk}\b)"
        any_key = rf"{column} (?:= \d+\b|IN \([^)]*\))"
        # Only a positive predicate selects the row: a NOT before the key or before any enclosing group excludes it.
        if any(not _negated(sql, where, found.start(), any_key) for found in re.finditer(key, where)):
            return True
    return False


def _negated(sql, where, position, any_key):
    """Return whether NOT comes directly before *position* or before a group of *where* that encloses it.

    A negated group must hold only *any_key* predicates joined by OR; any other group raises AssertionError.
    """
    # It reads no string literals and no double negation; the captured lock statements have neither.
    opened = []
    for index, char in enumerate(where[:position]):
        if char == "(":
            opened.append(index)
        elif char == ")" and opened:
            opened.pop()
    negated = [start for start in [position, *opened] if re.search(r"\bNOT\s*$", where[:start])]
    for start in negated:
        body = where[start + 1 : _group_end(where, start)] if start != position else ""
        if re.sub(r"[()\s]|\bOR\b", "", re.sub(any_key, "", body)):
            raise AssertionError(f"locks_row reads no NOT group other than id keys joined by OR: {sql}")
    return bool(negated)


def _group_end(where, start):
    """Return the index of the parenthesis that closes the group that opens at *start*."""
    depth = 0
    for index in range(start, len(where)):
        depth += {"(": 1, ")": -1}.get(where[index], 0)
        if depth == 0:
            return index
    raise AssertionError(f"locks_row found no end of the group at {start}: {where}")


def first_row_lock(statements, model, pk):
    """Return the index of the first captured SQL statement that locks the *model* row *pk*, at any lock strength."""
    return next(i for i, sql in enumerate(statements) if locks_row(sql, model, pk))


def lock_row(other, model, pk):
    """Lock one row ``FOR UPDATE`` on *other*; the lock stays until *other* ends its transaction."""
    with other.cursor() as cursor:
        cursor.execute(f'SELECT 1 FROM "{model._meta.db_table}" WHERE id = %s FOR UPDATE', [pk])
        assert cursor.fetchone() is not None, f"{model.__name__} {pk} is not visible to the second connection"


def commit_row_change(other, model, pk, values):
    """Update one row's *values* (column to value) on *other* and commit; a lock wait fails after two seconds."""
    assignments = ", ".join(f'"{column}" = %s' for column in values)
    with other.cursor() as cursor:
        # A test that commits from inside the sync's own thread must fail, not hang, on a lock the sync holds.
        cursor.execute("SET lock_timeout = '2s'")
        cursor.execute(f'UPDATE "{model._meta.db_table}" SET {assignments} WHERE id = %s', [*values.values(), pk])
        assert cursor.rowcount == 1, f"{model.__name__} {pk} is not visible to the second connection"
    other.commit()


def hold_port_claim(other, port_id, server_key):
    """Claim LibreNMS port *port_id* (an int) on *other*, as another open transaction that binds the port does."""
    from netbox_librenms_plugin.utils import advisory_lock_key, port_binding_lock_identity

    with other.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)", [advisory_lock_key(port_binding_lock_identity(port_id, server_key))]
        )


def lock_row_nowait(model, pk):
    """Lock one row on the test's connection with ``NOWAIT``: a row another session holds raises ``55P03``."""
    return model.objects.select_for_update(nowait=True).filter(pk=pk).first()


def backend_pid(db_connection):
    """Return the PostgreSQL backend process ID of *db_connection*."""
    with db_connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def wait_for_lock_wait(observer, pid, timeout=5.0):
    """Return once backend *pid* waits for a lock; *observer* is any other connection."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with observer.cursor() as cursor:
            # pg_locks shows the current lock table, whatever the observer's snapshot is.
            cursor.execute("SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted", [pid])
            if cursor.fetchone() is not None:
                return
        time.sleep(0.02)
    raise AssertionError(f"backend {pid} did not wait for a lock within {timeout}s")


@contextmanager
def raised_sqlstates():
    """Yield the list of SQLSTATEs that statements on the test's connection raise inside the block."""
    sqlstates = []

    def record(execute, sql, params, many, context):
        try:
            return execute(sql, params, many, context)
        except DatabaseError as exc:
            sqlstates.append(getattr(exc.__cause__, "sqlstate", None))
            raise

    with connection.execute_wrapper(record):
        yield sqlstates


@contextmanager
def lock_timeout(milliseconds):
    """Make a statement on the test's connection that waits for a row lock fail with ``55P03`` after *milliseconds*."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET lock_timeout = '{int(milliseconds)}ms'")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET lock_timeout")


@contextmanager
def failing_statement(matches, sqlstate):
    """
    Make the first statement on the test's connection that *matches* ``(sql, params)`` raise *sqlstate*.

    Use it only where a real conflict cannot be arranged, for example on a row that the code under
    test created in its own transaction. The error is the one Django's error wrapper makes of a
    PostgreSQL error, so NetBox and Django handle it as they handle a real one.
    """
    failed = []

    def fail(execute, sql, params, many, context):
        if not failed and matches(sql, params):
            failed.append(sql)
            raise wrapped_database_error(sqlstate)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail):
        yield failed


@contextmanager
def aborting_statement(matches):
    """
    Run ``SELECT 1/0`` in place of the first statement on the test's connection that *matches*.

    PostgreSQL raises a real ``22012`` (not a lock conflict) and aborts the open transaction, so
    only a savepoint lets the outer transaction continue.
    """
    failed = []

    def fail(execute, sql, params, many, context):
        if not failed and matches(sql, params):
            failed.append(sql)
            return execute("SELECT 1/0", None, many, context)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail):
        yield failed


def wrapped_database_error(sqlstate):
    """Return the Django error that Django's own error wrapper makes of a psycopg error with *sqlstate*."""
    import psycopg.errors

    # A throwaway wrapper: the translation marks errors_occurred on the connection it belongs to.
    wrapper = connections.create_connection(DEFAULT_DB_ALIAS)
    try:
        with wrapper.wrap_database_errors:
            raise psycopg.errors.lookup(sqlstate)(f"SQLSTATE {sqlstate}")
    except DatabaseError as exc:
        return exc
    raise AssertionError("the wrapper did not raise")
