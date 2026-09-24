"""
Run a unit of work as one outermost transaction, and run it once more after a lock conflict.

PostgreSQL aborts a transaction with SQLSTATE ``40P01`` (deadlock detected) or ``55P03`` (lock not
available). The plugin cannot prevent these: NetBox takes its own locks in orders the plugin does
not control. ``run_transaction`` rolls the whole attempt back and runs it once more. A second
conflict raises ``TransactionConflict``, which the HTTP adapter (``middleware.py``) shows as a
"try again" answer. The runner needs no request, so a background job can use it too.

A write of a row that another operation changed after the read is a conflict of the same kind.
``save_at_version`` saves a row only when its PostgreSQL row version (``xmin``) is still the one
that the read returned. It locks the row in ``pre_save``, at the point and in the mode of the
``UPDATE`` that follows, so the check adds no lock wait that the ``UPDATE`` does not have.
"""

import logging
from contextvars import ContextVar
from functools import cache

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connections, transaction
from django.db.models import UniqueConstraint
from django.db.models.expressions import RawSQL
from django.db.transaction import TransactionManagementError
from netbox.context import events_queue
from utilities.exceptions import AbortRequest

logger = logging.getLogger(__name__)

CONFLICT_SQLSTATES = frozenset({"40P01", "55P03"})
_ATTEMPTS = 2
# The recorder of the attempt that runs now, so a row-version check can record a conflict that the work swallows.
_active_recorder = ContextVar("librenms_conflict_recorder", default=None)
# The instance attribute that tells the pre_save check which row version and lock mode a save expects.
_VERSIONED_SAVE = "_librenms_versioned_save"
# The annotation that carries the row version of a read.
_ROW_VERSION = "_librenms_row_version"


class TransactionConflict(Exception):
    """Both attempts of a transaction met a lock conflict; nothing of the work was committed."""


class ConcurrentRowChange(Exception):
    """Another operation changed a row between the read of the row and the write of the row."""


class CommittedFollowUpError(Exception):
    """The attempt committed, then a commit callback failed; ``__cause__`` is that failure."""


class _SwallowedConflict(Exception):
    """The work returned normally, but it caught a lock conflict or a stale row version."""


def database_error_sqlstate(exc):
    """Return the SQLSTATE that PostgreSQL gave for a Django database error, or None."""
    # Django raises its own error from the driver's error, which carries the SQLSTATE.
    return getattr(exc.__cause__, "sqlstate", None)


def _nearest_database_error(exc):
    """Return the nearest database error in the chain of *exc*: ``__cause__``, else ``__context__``."""
    seen = set()
    link = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        link = link.__cause__ if link.__cause__ is not None else link.__context__
        if isinstance(link, DatabaseError):
            return link
    return None


def classify_conflict(exc):
    """
    Return True when *exc* is a lock conflict that a new attempt of the transaction can resolve.

    A database error decides by its own SQLSTATE, never by an error it was raised while handling.
    NetBox raises ``AbortRequest`` from its own ``40P01``, and a ``ValidationError`` ``from None``
    that keeps the ``40P01`` only in ``__context__``, so those two decide by their nearest database
    error. A failure after a commit is never a conflict: a retry would repeat committed work.

    Args:
        exc (BaseException): The exception that escaped a transaction.

    Returns:
        bool: True for a lock conflict, otherwise False.

    """
    if isinstance(exc, CommittedFollowUpError):
        return False
    if isinstance(exc, (TransactionConflict, ConcurrentRowChange)):
        return True
    if isinstance(exc, DatabaseError):
        return database_error_sqlstate(exc) in CONFLICT_SQLSTATES
    if isinstance(exc, (AbortRequest, ValidationError)):
        nearest = _nearest_database_error(exc)
        return nearest is not None and database_error_sqlstate(nearest) in CONFLICT_SQLSTATES
    return False


class _ConflictRecorder:
    """Record every lock conflict and stale row of an attempt, also one that the work catches later."""

    def __init__(self):
        self.conflicts = []

    def __call__(self, execute, sql, params, many, context):
        try:
            return execute(sql, params, many, context)
        except DatabaseError as exc:
            if (sqlstate := database_error_sqlstate(exc)) in CONFLICT_SQLSTATES:
                self.conflicts.append(sqlstate)
            raise


def run_transaction(work):
    """
    Run ``work()`` in one outermost transaction, and once more when the first attempt meets a lock conflict.

    Each attempt decides in this order:

    1. ``work`` raised: that exception decides alone (``classify_conflict``). A conflict starts the
       next attempt; any other exception propagates unchanged.
    2. ``work`` returned, but it caught a lock conflict of a statement or a stale row version
       (``row_changed``), or the deferred constraint check met a lock conflict: the attempt rolls
       back and the next attempt starts.
    3. ``work`` returned with no conflict: the attempt commits, and its return value is returned.

    ``work`` must build its own state on each call and must not publish anything (messages,
    counters) before the runner returns. NetBox events that an attempt queues are kept only when
    that attempt commits. A failure in a commit callback is never retried.

    Args:
        work (Callable[[], T]): The unit of work. It takes no arguments.

    Returns:
        T: The value that ``work`` returned in the committed attempt.

    Raises:
        TransactionConflict: Both attempts met a lock conflict.
        CommittedFollowUpError: The attempt committed, and then a commit callback failed.
        RuntimeError: The runner was called inside an atomic block (Django's durable check).
        TransactionManagementError: The connection is in manual transaction management.

    """
    connection = transaction.get_connection()
    if not connection.in_atomic_block and not connection.get_autocommit():
        raise TransactionManagementError("run_transaction() needs autocommit: it owns the outermost transaction.")
    attempt = 1
    while True:
        try:
            return _run_attempt(work, connection)
        except Exception as exc:
            if not (isinstance(exc, _SwallowedConflict) or classify_conflict(exc)):
                raise
            if attempt == _ATTEMPTS:
                raise TransactionConflict("The transaction met a lock conflict on every attempt.") from exc
            logger.warning("Transaction attempt %d met a lock conflict; running it once more: %s", attempt, exc)
            attempt += 1


def _run_attempt(work, connection):
    """Run one attempt in a durable atomic block, with its own NetBox event queue."""
    committed = False

    def mark_committed():
        nonlocal committed
        committed = True

    recorder = _ConflictRecorder()
    recorder_token = _active_recorder.set(recorder)
    queue_token = events_queue.set({})
    try:
        # durable: Django refuses an enclosing atomic block, except a test case's own block.
        with transaction.atomic(durable=True):
            transaction.on_commit(mark_committed)
            with connection.execute_wrapper(recorder):
                result = work()
                if recorder.conflicts:
                    raise _SwallowedConflict("; ".join(recorder.conflicts))
                # Checks deferred foreign keys now, so their lock conflicts pass the recorder too.
                connection.check_constraints()
        return result
    except Exception as exc:
        if committed:
            raise CommittedFollowUpError("The transaction committed, but a commit callback failed.") from exc
        raise
    finally:
        _active_recorder.reset(recorder_token)
        attempt_events = events_queue.get()
        events_queue.reset(queue_token)
        if committed:
            _keep_events(attempt_events)


def _keep_events(attempt_events):
    """Add a committed attempt's events to the enclosing queue; an event for the same object gets its own key."""
    if not attempt_events:
        return
    queue = events_queue.get()
    for key, event in attempt_events.items():
        kept_key, number = key, 1
        while kept_key in queue:
            number += 1
            kept_key = f"{key}#{number}"
        queue[kept_key] = event


def _row_version_sql(table):
    """Return the SQL expression of the row version of *table* (a quoted name): PostgreSQL's ``xmin``."""
    return f"{table}.xmin::text"


def first_at_version(queryset):
    """
    Return the first row of *queryset* and the row version that the read returned.

    Returns:
        tuple: ``(instance, version)``, or ``(None, None)`` when *queryset* has no row.

    """
    table = connections[queryset.db].ops.quote_name(queryset.model._meta.db_table)
    row = queryset.annotate(**{_ROW_VERSION: RawSQL(_row_version_sql(table), ())}).first()
    if row is None:
        return None, None
    return row, row.__dict__.pop(_ROW_VERSION)


def row_changed(name):
    """
    Return the conflict for interface *name*, changed by another operation after the read.

    The conflict is also recorded for the attempt that runs now, so the runner retries the attempt
    even when a broad handler in the work catches the exception. Outside the runner it is not recorded.

    Args:
        name (str): The interface name that the caller read. Never a name read after the caller's
            permission check: another operation can change it.

    Returns:
        ConcurrentRowChange: The exception for the caller to raise.

    """
    conflict = ConcurrentRowChange(f"NetBox interface {name} was changed by another operation. Refresh and try again.")
    if (recorder := _active_recorder.get()) is not None:
        recorder.conflicts.append(str(conflict))
    return conflict


@cache
def key_columns(model):
    """
    Return the columns of *model* that PostgreSQL treats as key columns in its row locks.

    A key column is in a unique index that a foreign key can reference: a unique field (a
    ``OneToOneField`` too), a ``unique_together`` set, or a ``UniqueConstraint`` of plain fields
    with no condition. An ``UPDATE`` that changes a key column locks the row ``FOR UPDATE``; any
    other ``UPDATE`` locks it ``FOR NO KEY UPDATE``.

    Args:
        model (type[Model]): The model.

    Returns:
        frozenset[str]: The database column names.

    """
    meta = model._meta
    field_sets = [(field.name,) for field in meta.concrete_fields if field.unique]
    field_sets += [tuple(fields) for fields in meta.unique_together]
    field_sets += [
        constraint.fields
        for constraint in meta.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.fields
        and constraint.condition is None
        and not constraint.expressions
    ]
    return frozenset(meta.get_field(name).column for fields in field_sets for name in fields)


def save_at_version(instance, *, version, changed_columns, name):
    """
    Save *instance* with a full ``save()``, but only when its row still has row *version*.

    The ``pre_save`` receiver ``lock_row_at_version`` locks the row and compares the version. It
    locks ``FOR UPDATE`` when a key column changes, else ``FOR NO KEY UPDATE``: the mode of the
    ``UPDATE`` that follows. A row with another version raises ``ConcurrentRowChange``. Then the
    caller must discard *instance*.

    Args:
        instance (Interface | VMInterface): The instance that ``first_at_version`` read, with its new values.
        version (str): The row version that the read returned.
        changed_columns (set[str]): The columns whose values the save changes.
        name (str): The interface name that the caller read, for the conflict message.

    Raises:
        ConcurrentRowChange: Another operation changed or deleted the row after the read.
        RuntimeError: No ``pre_save`` receiver checked the version, so the save was not checked.

    """
    lock_mode = "UPDATE" if changed_columns & key_columns(type(instance)) else "NO KEY UPDATE"
    instance.__dict__[_VERSIONED_SAVE] = (version, lock_mode, name)
    try:
        instance.save()
    finally:
        unchecked = instance.__dict__.pop(_VERSIONED_SAVE, None)
    if unchecked is not None:
        raise RuntimeError(f"No row-version check is connected for {type(instance).__name__}.")


def lock_row_at_version(sender, instance, using, **kwargs):
    """``pre_save`` receiver: lock the row of a ``save_at_version`` save and refuse a changed row."""
    # pop: the check is for one save only, so a later save of the instance is not checked again.
    expected = instance.__dict__.pop(_VERSIONED_SAVE, None)
    if expected is None:
        return
    version, lock_mode, name = expected
    database = connections[using]
    table = database.ops.quote_name(sender._meta.db_table)
    with database.cursor() as cursor:
        cursor.execute(
            f"SELECT {_row_version_sql(table)} FROM {table} "
            f"WHERE {database.ops.quote_name(sender._meta.pk.column)} = %s FOR {lock_mode}",
            [instance.pk],
        )
        current = cursor.fetchone()
    if current is None or current[0] != version:
        raise row_changed(name)
