"""The transaction runner and the lock-conflict classifier, driven by real PostgreSQL lock conflicts."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, OperationalError, connection, transaction
from django.db.transaction import TransactionManagementError
from utilities.exceptions import AbortRequest

from netbox_librenms_plugin.tests.conftest import make_superuser, transactional_db_with_all_apps
from netbox_librenms_plugin.tests.lock_conflict_helpers import (
    lock_row,
    lock_row_nowait,
    second_connection,
    wrapped_database_error,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_request
from netbox_librenms_plugin.transactions import (
    CommittedFollowUpError,
    ConcurrentRowChange,
    TransactionConflict,
    classify_conflict,
    database_error_sqlstate,
    run_transaction,
)


def _context_sqlstates(exc):
    """Return every SQLSTATE in the implicit ``__context__`` chain of *exc*."""
    found = set()
    while exc is not None:
        found.add(getattr(exc, "sqlstate", None))
        exc = exc.__context__
    return found


def _site(name):
    from dcim.models import Site

    return Site.objects.create(name=name, slug=name)


def _committed_row_pk():
    """Return a row that migrations committed, so a second connection can lock it in a non-transactional test."""
    return ContentType.objects.get_for_model(ContentType).pk


@contextmanager
def _netbox_request_context():
    """Set NetBox's request and event queue context as its request processor does, without the flush."""
    from netbox.context import current_request, events_queue

    request = make_request("post", user=make_superuser("transactions-events-user"))
    request.id = uuid4()
    request_token = current_request.set(request)
    queue_token = events_queue.set({})
    try:
        yield
    finally:
        current_request.reset(request_token)
        events_queue.reset(queue_token)


def _queued_events():
    from netbox.context import events_queue

    return events_queue.get()


# ---------------------------------------------------------------------------
# classify_conflict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sqlstate", ["40P01", "55P03"])
def test_a_deadlock_or_a_lock_timeout_is_a_conflict(sqlstate):
    error = wrapped_database_error(sqlstate)

    assert isinstance(error, OperationalError)
    assert classify_conflict(error) is True


@pytest.mark.parametrize("sqlstate", ["23505", "23503", "57014"])
def test_another_database_error_is_not_a_conflict(sqlstate):
    assert classify_conflict(wrapped_database_error(sqlstate)) is False


@pytest.mark.django_db
def test_a_unique_violation_raised_while_handling_a_lock_conflict_is_not_a_conflict():
    """A database error decides by its own SQLSTATE, not by a conflict it was raised while handling."""
    locked_pk = _committed_row_pk()
    with second_connection() as other:
        lock_row(other, ContentType, locked_pk)
        with pytest.raises(IntegrityError) as caught:
            try:
                with transaction.atomic():
                    lock_row_nowait(ContentType, locked_pk)
            except OperationalError:
                with transaction.atomic():
                    _site("classify-duplicate")
                    _site("classify-duplicate")

    assert database_error_sqlstate(caught.value) == "23505"
    assert "55P03" in _context_sqlstates(caught.value), "precondition: the lock conflict is in the context"
    assert classify_conflict(caught.value) is False


def test_a_validation_error_raised_from_none_keeps_its_deadlock_in_the_context():
    """NetBox's ltree save raises its deadlock ValidationError ``from None``; only __context__ keeps the cause."""
    deadlock = wrapped_database_error("40P01")
    try:
        try:
            raise deadlock
        except OperationalError:
            raise ValidationError("Another operation changed the tree.") from None
    except ValidationError as exc:
        error = exc

    assert error.__cause__ is None and error.__context__ is deadlock
    assert classify_conflict(error) is True


def test_an_abort_request_raised_from_a_deadlock_is_a_conflict():
    """NetBox's module save turns its own 40P01 into AbortRequest."""
    try:
        raise AbortRequest("The module could not be saved.") from wrapped_database_error("40P01")
    except AbortRequest as exc:
        error = exc

    assert classify_conflict(error) is True


@pytest.mark.parametrize(
    "error_class, cause",
    [
        (AbortRequest, "23505"),
        (ValidationError, "23503"),
        (AbortRequest, None),
        (ValidationError, None),
        (ValueError, "40P01"),
        (ValueError, None),
    ],
)
def test_an_error_whose_nearest_database_error_is_no_conflict_is_not_a_conflict(error_class, cause):
    """Only the nearest database error of an AbortRequest or a ValidationError counts; any other error never does."""
    try:
        if cause is None:
            raise error_class("not a lock conflict")
        raise error_class("not a lock conflict") from wrapped_database_error(cause)
    except (error_class, DatabaseError) as exc:
        error = exc

    assert classify_conflict(error) is False


def test_the_runner_s_own_conflict_types_are_conflicts():
    assert classify_conflict(TransactionConflict("retry exhausted")) is True
    assert classify_conflict(ConcurrentRowChange("row changed")) is True


def test_a_failure_after_a_commit_is_never_a_conflict():
    """A committed attempt must not be retried, whatever failed after it."""
    try:
        raise CommittedFollowUpError("follow-up failed") from wrapped_database_error("40P01")
    except CommittedFollowUpError as exc:
        error = exc

    assert classify_conflict(error) is False


# ---------------------------------------------------------------------------
# run_transaction inside the test transaction (savepoint attempts)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_conflict_that_escapes_the_first_attempt_is_retried_once_and_its_rows_are_gone():
    from dcim.models import Site

    locked_pk = _committed_row_pk()
    calls = []

    def work():
        calls.append(len(calls) + 1)
        _site(f"runner-attempt-{len(calls)}")
        if len(calls) == 1:
            lock_row_nowait(ContentType, locked_pk)
        return len(calls)

    with second_connection() as other:
        lock_row(other, ContentType, locked_pk)
        result = run_transaction(work)

    assert result == 2
    assert calls == [1, 2]
    assert not Site.objects.filter(name="runner-attempt-1").exists()
    assert Site.objects.filter(name="runner-attempt-2").exists()


@pytest.mark.django_db
def test_a_conflict_on_both_attempts_raises_transaction_conflict():
    locked_pk = _committed_row_pk()
    calls = []

    def work():
        calls.append(len(calls) + 1)
        lock_row_nowait(ContentType, locked_pk)

    with second_connection() as other:
        lock_row(other, ContentType, locked_pk)
        with pytest.raises(TransactionConflict) as caught:
            run_transaction(work)

    assert calls == [1, 2]
    assert database_error_sqlstate(caught.value.__cause__) == "55P03"


@pytest.mark.django_db
def test_a_conflict_that_work_swallowed_is_still_retried():
    """A broad handler in a savepoint can turn a lock conflict into a normal return; the attempt still retries."""
    locked_pk = _committed_row_pk()
    calls = []

    def work():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            try:
                with transaction.atomic():
                    lock_row_nowait(ContentType, locked_pk)
            except OperationalError:
                return "swallowed"
        return "clean"

    with second_connection() as other:
        lock_row(other, ContentType, locked_pk)
        result = run_transaction(work)

    assert (result, calls) == ("clean", [1, 2])


@pytest.mark.django_db
def test_an_error_that_escapes_after_a_swallowed_conflict_propagates_unchanged():
    """The escaping error decides alone: a swallowed 55P03 must not turn a later 23505 into a retry."""
    locked_pk = _committed_row_pk()
    calls = []

    def work():
        calls.append(len(calls) + 1)
        try:
            with transaction.atomic():
                lock_row_nowait(ContentType, locked_pk)
        except OperationalError:
            pass
        _site("runner-mixed-duplicate")
        _site("runner-mixed-duplicate")

    with second_connection() as other:
        lock_row(other, ContentType, locked_pk)
        with pytest.raises(IntegrityError) as caught:
            run_transaction(work)

    assert calls == [1]
    assert database_error_sqlstate(caught.value) == "23505"


@pytest.mark.django_db
def test_an_error_that_is_not_a_conflict_is_not_retried():
    calls = []

    def work():
        calls.append(len(calls) + 1)
        raise ValueError("not a lock conflict")

    with pytest.raises(ValueError, match="not a lock conflict"):
        run_transaction(work)

    assert calls == [1]


@pytest.mark.django_db
def test_a_deferred_foreign_key_violation_surfaces_inside_the_attempt():
    """The runner's last step checks deferred constraints, so a dangling key fails before the attempt ends."""
    from dcim.models import Manufacturer, Platform

    missing_pk = (Manufacturer.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 1000
    calls = []

    def work():
        calls.append(len(calls) + 1)
        Platform.objects.create(name="runner-dangling", slug="runner-dangling", manufacturer_id=missing_pk)

    with pytest.raises(IntegrityError) as caught:
        run_transaction(work)

    assert calls == [1]
    assert database_error_sqlstate(caught.value) == "23503"
    assert not Platform.objects.filter(slug="runner-dangling").exists()


@pytest.mark.django_db
def test_the_runner_refuses_an_enclosing_transaction():
    calls = []

    with transaction.atomic():
        with pytest.raises(RuntimeError, match="durable"):
            run_transaction(lambda: calls.append(1))

    assert calls == []


# ---------------------------------------------------------------------------
# run_transaction with real commits
# ---------------------------------------------------------------------------


@transactional_db_with_all_apps()
def test_the_runner_refuses_manual_transaction_management():
    calls = []
    transaction.set_autocommit(False)
    try:
        with pytest.raises(TransactionManagementError):
            run_transaction(lambda: calls.append(1))
    finally:
        connection.rollback()
        transaction.set_autocommit(True)

    assert calls == []


@transactional_db_with_all_apps()
def test_the_runner_retries_and_commits_without_a_request():
    """A job has no request and no messages; the runner needs neither."""
    from dcim.models import Site
    from netbox.context import current_request

    locked = _site("runner-no-request-locked")
    calls = []

    def work():
        calls.append(len(calls) + 1)
        site = _site(f"runner-no-request-{len(calls)}")
        if len(calls) == 1:
            lock_row_nowait(Site, locked.pk)
        return site.pk

    assert current_request.get() is None, "precondition: no request is bound"
    with second_connection() as other:
        lock_row(other, Site, locked.pk)
        committed_pk = run_transaction(work)
        # The second connection sees only committed rows.
        with other.cursor() as cursor:
            cursor.execute('SELECT name FROM "dcim_site" WHERE id = %s', [committed_pk])
            assert cursor.fetchone() == ("runner-no-request-2",)

    assert calls == [1, 2]
    assert not Site.objects.filter(name="runner-no-request-1").exists()


@transactional_db_with_all_apps()
def test_a_failed_attempt_leaves_no_queued_events():
    from core.models import ObjectChange
    from dcim.models import Site

    locked = _site("runner-events-locked")
    created = []

    def work():
        site = _site(f"runner-events-{len(created) + 1}")
        created.append(site.pk)
        if len(created) == 1:
            lock_row_nowait(Site, locked.pk)
        return site.pk

    with _netbox_request_context(), second_connection() as other:
        lock_row(other, Site, locked.pk)
        committed_pk = run_transaction(work)
        queued = set(_queued_events())

    failed_pk = created[0]
    assert queued == {f"dcim.site:{committed_pk}"}, "only the committed attempt may queue events"
    assert not ObjectChange.objects.filter(changed_object_id=failed_pk, changed_object_type__model="site").exists()
    assert not Site.objects.filter(pk=failed_pk).exists()


@transactional_db_with_all_apps()
def test_a_failing_commit_callback_is_not_retried_and_keeps_the_committed_events():
    from dcim.models import Site

    calls = []

    def fail_after_commit():
        raise RuntimeError("follow-up failed")

    def work():
        calls.append(len(calls) + 1)
        site = _site("runner-follow-up")
        transaction.on_commit(fail_after_commit)
        return site.pk

    with _netbox_request_context():
        with pytest.raises(CommittedFollowUpError) as caught:
            run_transaction(work)
        queued = set(_queued_events())

    site = Site.objects.get(name="runner-follow-up")
    assert calls == [1]
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert queued == {f"dcim.site:{site.pk}"}, "a committed attempt keeps its events"


@transactional_db_with_all_apps()
def test_a_committed_event_for_an_object_the_request_already_queued_is_kept_as_well():
    """Two committed transactions of one request can change the same object; both events stay."""
    with _netbox_request_context():
        site = _site("runner-two-transactions")

        def work():
            site.snapshot()
            site.description = "changed in the runner"
            site.save()

        run_transaction(work)
        events = list(_queued_events().values())

    assert len(events) == 2
    assert events[0] is not events[1]
