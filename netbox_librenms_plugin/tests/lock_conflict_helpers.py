"""
A real second database connection, for tests that need PostgreSQL to raise a real lock conflict.

The second connection is a separate backend session on the test database. It holds its row locks
in its own open transaction, so a statement on the test's connection that needs one of those rows
waits (and fails with ``55P03`` under ``lock_timeout``) or fails at once with ``NOWAIT``. It sees
only committed rows: a test that locks a row it created itself must be transactional.
"""

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


def lock_row(other, model, pk):
    """Lock one row ``FOR UPDATE`` on *other*; the lock stays until *other* ends its transaction."""
    with other.cursor() as cursor:
        cursor.execute(f'SELECT 1 FROM "{model._meta.db_table}" WHERE id = %s FOR UPDATE', [pk])
        assert cursor.fetchone() is not None, f"{model.__name__} {pk} is not visible to the second connection"


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
def lock_timeout(milliseconds):
    """Make a statement on the test's connection that waits for a row lock fail with ``55P03`` after *milliseconds*."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET lock_timeout = '{int(milliseconds)}ms'")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET lock_timeout")


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
