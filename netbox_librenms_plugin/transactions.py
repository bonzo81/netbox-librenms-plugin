"""
Run a unit of work as one outermost transaction, and run it once more after a lock conflict.

PostgreSQL aborts a transaction with SQLSTATE ``40P01`` (deadlock detected) or ``55P03`` (lock not
available). The plugin cannot prevent these: NetBox takes its own locks in orders the plugin does
not control. ``run_transaction`` rolls the whole attempt back and runs it once more. A second
conflict raises ``TransactionConflict``, which the HTTP adapter (``middleware.py``) shows as a
"try again" answer. The runner needs no request, so a background job can use it too.
"""

import logging

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.transaction import TransactionManagementError
from netbox.context import events_queue
from utilities.exceptions import AbortRequest

logger = logging.getLogger(__name__)

CONFLICT_SQLSTATES = frozenset({"40P01", "55P03"})
_ATTEMPTS = 2


class TransactionConflict(Exception):
    """Both attempts of a transaction met a lock conflict; nothing of the work was committed."""


class ConcurrentRowChange(Exception):
    """Another operation changed a row between the read of the row and the write of the row."""


class CommittedFollowUpError(Exception):
    """The attempt committed, then a commit callback failed; ``__cause__`` is that failure."""


class _SwallowedConflict(Exception):
    """The work returned normally, but one of its statements met a lock conflict."""


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
    """Record the lock conflict of every statement, also one that the work catches later; then re-raise."""

    def __init__(self):
        self.sqlstates = []

    def __call__(self, execute, sql, params, many, context):
        try:
            return execute(sql, params, many, context)
        except DatabaseError as exc:
            if (sqlstate := database_error_sqlstate(exc)) in CONFLICT_SQLSTATES:
                self.sqlstates.append(sqlstate)
            raise


def run_transaction(work):
    """
    Run ``work()`` in one outermost transaction, and once more when the first attempt meets a lock conflict.

    Each attempt decides in this order:

    1. ``work`` raised: that exception decides alone (``classify_conflict``). A conflict starts the
       next attempt; any other exception propagates unchanged.
    2. ``work`` returned, but a statement met a conflict that the work caught, or the deferred
       constraint check met one: the attempt rolls back and the next attempt starts.
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
    queue_token = events_queue.set({})
    try:
        # durable: Django refuses an enclosing atomic block, except a test case's own block.
        with transaction.atomic(durable=True):
            transaction.on_commit(mark_committed)
            with connection.execute_wrapper(recorder):
                result = work()
                if recorder.sqlstates:
                    raise _SwallowedConflict(", ".join(recorder.sqlstates))
                # Checks deferred foreign keys now, so their lock conflicts pass the recorder too.
                connection.check_constraints()
        return result
    except Exception as exc:
        if committed:
            raise CommittedFollowUpError("The transaction committed, but a commit callback failed.") from exc
        raise
    finally:
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
