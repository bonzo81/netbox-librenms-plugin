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
from netbox_librenms_plugin.tests.mapping_fixtures import mapping_from_change_record, seed_stored_mapping_row
from netbox_librenms_plugin.tests.lock_conflict_helpers import (
    failing_statement,
    hold_port_claim,
    lock_row,
    lock_row_nowait,
    lock_timeout,
    raised_sqlstates,
    second_connection,
    wrapped_database_error,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
from netbox_librenms_plugin.transactions import (
    TRY_AGAIN_MESSAGE,
    CommittedFollowUpError,
    ConcurrentRowChange,
    TransactionConflict,
    classify_conflict,
    database_error_sqlstate,
    run_transaction,
    update_existing_row,
)
from netbox_librenms_plugin.server_mappings import (
    LibreNMSPortBindingBusy,
    LibreNMSPortBindingConflict,
    claim_librenms_port_binding,
)
from netbox_librenms_plugin.utils import DATABASE_ERROR_MESSAGE, exception_text_for

# Long enough for a blocked statement to be a real lock wait, short enough for two attempts.
LOCK_TIMEOUT_MS = 200


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


@pytest.mark.parametrize("sqlstate", ["40P01", "55P03", "40001"])
def test_a_deadlock_a_lock_timeout_or_a_serialization_failure_is_a_conflict(sqlstate):
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


def test_a_busy_port_claim_is_a_conflict_and_a_port_owned_by_another_interface_is_not():
    assert isinstance(LibreNMSPortBindingBusy(), TransactionConflict)
    assert classify_conflict(LibreNMSPortBindingBusy()) is True
    assert classify_conflict(LibreNMSPortBindingConflict("The LibreNMS port ID is already assigned.")) is False


@pytest.mark.django_db
@pytest.mark.parametrize("superuser", [False, True], ids=["user", "superuser"])
def test_the_text_of_a_caught_conflict_is_the_try_again_answer(superuser):
    """No viewer reads PostgreSQL's deadlock text, or NetBox's refusal of a tree save that met a deadlock."""
    from dcim.models import Interface

    user = make_superuser("conflict-text-su") if superuser else make_user_with_perms("conflict-text-user", [])
    deadlock = wrapped_database_error("40P01")
    try:
        try:
            raise deadlock
        except OperationalError:
            raise ValidationError("Another operation changed the tree.") from None
    except ValidationError as exc:
        tree_refusal = exc

    assert exception_text_for(deadlock, Interface, user) == TRY_AGAIN_MESSAGE
    assert exception_text_for(tree_refusal, Interface, user) == TRY_AGAIN_MESSAGE
    assert exception_text_for(LibreNMSPortBindingBusy(), Interface, user) == TRY_AGAIN_MESSAGE


@pytest.mark.django_db
@pytest.mark.parametrize("superuser", [False, True], ids=["user", "superuser"])
def test_the_text_of_a_caught_database_error_is_the_generic_answer(superuser):
    """PostgreSQL's text can name rows and values; NetBox's AbortRequest can carry it as its own text."""
    from dcim.models import Interface

    user = make_superuser("db-error-text-su") if superuser else make_user_with_perms("db-error-text-user", [])
    data_error = wrapped_database_error("22001")
    try:
        raise AbortRequest(str(data_error)) from data_error
    except AbortRequest as exc:
        wrapped = exc

    assert exception_text_for(data_error, Interface, user) == DATABASE_ERROR_MESSAGE
    assert exception_text_for(wrapped, Interface, user) == DATABASE_ERROR_MESSAGE
    assert exception_text_for(AbortRequest("NetBox refuses the move."), Interface, user) == "NetBox refuses the move."


@pytest.mark.django_db
def test_the_text_of_a_driver_error_is_the_generic_answer():
    """A psycopg error that no Django wrapper translated holds the same PostgreSQL text."""
    import psycopg.errors
    from dcim.models import Interface

    raw = psycopg.errors.UniqueViolation('duplicate key value violates unique constraint "name"')

    assert exception_text_for(raw, Interface, make_user_with_perms("driver-error-text-user", [])) == (
        DATABASE_ERROR_MESSAGE
    )


def test_a_failure_after_a_commit_is_never_a_conflict():
    """A committed attempt must not be retried, whatever failed after it."""
    try:
        raise CommittedFollowUpError("follow-up failed") from wrapped_database_error("40P01")
    except CommittedFollowUpError as exc:
        error = exc

    assert classify_conflict(error) is False


# ---------------------------------------------------------------------------
# update_existing_row
# ---------------------------------------------------------------------------


def _site_changes(site):
    from core.models import ObjectChange
    from dcim.models import Site

    return list(
        ObjectChange.objects.filter(
            changed_object_type=ContentType.objects.get_for_model(Site), changed_object_id=site.pk, action="update"
        ).order_by("pk")
    )


def _set_description(value):
    def apply(row):
        row.description = value

    return apply


@pytest.mark.django_db
def test_an_existing_row_write_records_the_row_before_and_after():
    from dcim.models import Site

    site = _site("existing-row-write")

    with _netbox_request_context():
        written = update_existing_row(Site.objects.filter(pk=site.pk), _set_description("after"))

    assert Site.objects.get(pk=site.pk).description == written.description == "after"
    [change] = _site_changes(site)
    assert change.prechange_data["description"] == ""
    assert change.postchange_data["description"] == "after"


@pytest.mark.django_db
def test_a_second_write_of_the_same_row_records_the_first_write_as_its_before_state():
    from dcim.models import Site

    site = _site("existing-row-second-write")

    with _netbox_request_context():
        update_existing_row(Site.objects.filter(pk=site.pk), _set_description("first"))
        update_existing_row(Site.objects.filter(pk=site.pk), _set_description("second"))

    first, second = _site_changes(site)
    assert (first.prechange_data["description"], first.postchange_data["description"]) == ("", "first")
    assert (second.prechange_data["description"], second.postchange_data["description"]) == ("first", "second")


@pytest.mark.django_db
def test_a_write_that_fails_validation_leaves_the_row_and_the_change_log_unchanged():
    from dcim.models import Site

    site = _site("existing-row-invalid")

    def apply(row):
        row.description = "not saved"
        row.slug = "not a slug"

    with _netbox_request_context(), pytest.raises(ValidationError):
        update_existing_row(Site.objects.filter(pk=site.pk), apply)

    assert Site.objects.filter(pk=site.pk, slug=site.slug, description="").exists()
    assert _site_changes(site) == []


@pytest.mark.django_db
def test_a_row_that_already_holds_the_values_is_neither_validated_nor_saved():
    from dcim.models import Site

    site = _site("existing-row-unchanged")
    Site.objects.filter(pk=site.pk).update(slug="not a slug")
    stored = Site.objects.values_list("last_updated", flat=True).get(pk=site.pk)

    with _netbox_request_context():
        update_existing_row(Site.objects.filter(pk=site.pk), lambda row: False)

    assert Site.objects.values_list("last_updated", flat=True).get(pk=site.pk) == stored
    assert _site_changes(site) == []


def _device_updates(device):
    from core.models import ObjectChange

    return list(
        ObjectChange.objects.filter(
            changed_object_type=ContentType.objects.get_for_model(device), changed_object_id=device.pk, action="update"
        )
    )


@pytest.mark.django_db
def test_a_mapping_change_saves_with_the_other_fields_in_one_validated_save():
    from dcim.models import Device

    from netbox_librenms_plugin.server_mappings import assign_own, read_mapping
    from netbox_librenms_plugin.tests.conftest import make_device

    device = make_device("existing-row-mapping")

    def apply(row):
        row.description = "linked"
        return assign_own(row, "default", 7401)

    with _netbox_request_context():
        update_existing_row(Device.objects.filter(pk=device.pk), apply)

    stored = Device.objects.get(pk=device.pk)
    assert (stored.description, read_mapping(stored).own_id("default")) == ("linked", 7401)
    [change] = _device_updates(device)
    assert change.prechange_data["description"] == ""
    assert mapping_from_change_record(change, before=False) == {"default": 7401}


@pytest.mark.django_db
def test_a_mapping_change_on_an_invalid_row_saves_nothing():
    from dcim.models import Device

    from netbox_librenms_plugin.server_mappings import assign_own, read_mapping
    from netbox_librenms_plugin.tests.conftest import make_device

    device = make_device("existing-row-mapping-invalid")

    def apply(row):
        row.status = "not-a-status"
        return assign_own(row, "default", 7402)

    with _netbox_request_context(), pytest.raises(ValidationError):
        update_existing_row(Device.objects.filter(pk=device.pk), apply)

    assert read_mapping(Device.objects.get(pk=device.pk)).own_id("default") is None
    assert _device_updates(device) == []


@pytest.mark.django_db
def test_an_apply_result_that_is_neither_false_nor_a_change_is_refused():
    from dcim.models import Site

    site = _site("existing-row-truthy-result")

    with pytest.raises(TypeError, match="MappingChange"):
        update_existing_row(Site.objects.filter(pk=site.pk), lambda row: True)

    assert _site_changes(site) == []


@pytest.mark.django_db
def test_a_missing_row_raises_does_not_exist_without_applying_the_change():
    from dcim.models import Site

    applied = []

    with pytest.raises(Site.DoesNotExist):
        update_existing_row(Site.objects.filter(name="existing-row-absent"), applied.append)

    assert applied == []


@transactional_db_with_all_apps()
def test_an_existing_row_write_waits_for_the_row_lock_before_it_applies_the_change():
    from dcim.models import Site

    site = _site("existing-row-locked")
    applied = []

    with second_connection() as other:
        lock_row(other, Site, site.pk)
        with lock_timeout(200), raised_sqlstates() as sqlstates, pytest.raises(OperationalError):
            update_existing_row(Site.objects.filter(pk=site.pk), applied.append)

    assert sqlstates == ["55P03"]
    assert applied == []


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
def test_a_serialization_failure_on_the_first_attempt_is_retried():
    """PostgreSQL asks the client to retry a 40001, so the runner runs the work once more."""
    from dcim.models import Site

    calls = []

    def work():
        calls.append(len(calls) + 1)
        return _site(f"runner-serialization-{len(calls)}").pk

    with failing_statement(lambda sql, params: sql.startswith('INSERT INTO "dcim_site"'), "40001") as failed:
        committed_pk = run_transaction(work)

    assert failed, "precondition: the first attempt met the serialization failure"
    assert calls == [1, 2]
    assert Site.objects.get(pk=committed_pk).name == "runner-serialization-2"


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
def test_a_conflict_caught_without_a_savepoint_is_retried_even_when_a_query_follows():
    """The caught conflict broke the attempt; the next query's TransactionManagementError is its consequence."""
    from dcim.models import Site

    locked_pk = _committed_row_pk()
    calls = []

    def work():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            try:
                ContentType.objects.get(pk=locked_pk).save()
            except OperationalError:
                pass
        return _site(f"runner-broken-conflict-{len(calls)}").pk

    with second_connection() as other:
        lock_row(other, ContentType, locked_pk)
        with lock_timeout(LOCK_TIMEOUT_MS):
            committed_pk = run_transaction(work)

    assert calls == [1, 2]
    assert Site.objects.get(pk=committed_pk).name == "runner-broken-conflict-2"
    assert not Site.objects.filter(name="runner-broken-conflict-1").exists()


@pytest.mark.django_db
def test_a_plain_query_conflict_caught_without_a_savepoint_is_retried_when_postgresql_aborts_the_next_query():
    """A plain query does not mark the attempt for rollback, so the next query fails with 25P02 instead."""
    from dcim.models import Site

    locked_pk = _committed_row_pk()
    calls = []

    def work():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            try:
                lock_row_nowait(ContentType, locked_pk)
            except OperationalError:
                pass
        return _site(f"runner-aborted-conflict-{len(calls)}").pk

    with second_connection() as other:
        lock_row(other, ContentType, locked_pk)
        committed_pk = run_transaction(work)

    assert calls == [1, 2]
    assert Site.objects.get(pk=committed_pk).name == "runner-aborted-conflict-2"
    assert not Site.objects.filter(name="runner-aborted-conflict-1").exists()


@pytest.mark.django_db
def test_work_that_caught_a_database_error_without_a_savepoint_never_reports_success():
    """The caught error left the attempt only a rollback, so the runner raises instead of returning."""
    from dcim.models import Site

    calls = []

    def work():
        calls.append(len(calls) + 1)
        _site("runner-broken-first")
        try:
            _site("runner-broken-duplicate")
            _site("runner-broken-duplicate")
        except IntegrityError:
            pass
        return "reported success"

    with pytest.raises(TransactionManagementError, match="without a savepoint") as caught:
        run_transaction(work)

    assert calls == [1]
    assert classify_conflict(caught.value) is False
    assert not Site.objects.filter(name__startswith="runner-broken").exists()


@pytest.mark.django_db
def test_a_busy_port_claim_that_work_swallowed_is_still_retried():
    """A claim that another transaction holds records itself, so a broad handler cannot turn it into success."""
    calls = []

    with second_connection() as other:
        hold_port_claim(other, 9401, "default")

        def work():
            calls.append(len(calls) + 1)
            if len(calls) == 2:
                other.rollback()
            try:
                claim_librenms_port_binding(9401, "default")
            except Exception:
                return "swallowed"
            return "claimed"

        result = run_transaction(work)

    assert (result, calls) == ("claimed", [1, 2])


@pytest.mark.django_db
def test_a_busy_device_identity_claim_that_work_swallowed_is_still_retried():
    """A held device identity claim records itself, so a broad handler cannot commit the attempt."""
    from dcim.models import Device

    from netbox_librenms_plugin.server_mappings import assign_own, persist_mapping
    from netbox_librenms_plugin.tests.claim_race_helpers import device_claim_key
    from netbox_librenms_plugin.tests.conftest import make_device

    device = make_device(f"runner-identity-{uuid4().hex[:8]}")
    calls = []

    def write(row, fields):
        row.save()
        return row

    with second_connection() as other:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [device_claim_key("default", 9402)])

        def work():
            calls.append(len(calls) + 1)
            if len(calls) == 2:
                other.rollback()
            row = Device.objects.select_for_update().get(pk=device.pk)
            try:
                persist_mapping(row, assign_own(row, "default", 9402), write=write)
            except Exception:
                return "swallowed"
            return "claimed"

        result = run_transaction(work)

    assert (result, calls) == ("claimed", [1, 2])


@pytest.mark.django_db
def test_a_stale_mapping_that_work_swallowed_is_still_retried():
    """A mapping that changed after the read records its conflict, so a broad handler cannot commit the attempt."""
    from dcim.models import Device

    from netbox_librenms_plugin.server_mappings import assign_own, persist_mapping, read_mapping
    from netbox_librenms_plugin.tests.conftest import make_device

    device = make_device(f"runner-stale-{uuid4().hex[:8]}")
    calls = []

    def write(row, fields):
        row.save()
        return row

    def work():
        calls.append(len(calls) + 1)
        change = assign_own(Device.objects.get(pk=device.pk), "default", 9403)
        if len(calls) == 1:
            seed_stored_mapping_row(device, {"default": 9404})
        row = Device.objects.select_for_update().get(pk=device.pk)
        try:
            persist_mapping(row, change, write=write)
        except Exception:
            return "swallowed"
        return "assigned"

    result = run_transaction(work)

    assert (result, calls) == ("assigned", [1, 2])
    device.refresh_from_db()
    assert read_mapping(device).own_id("default") == 9403


@pytest.mark.django_db
def test_a_stale_merge_side_that_work_swallowed_is_still_retried():
    """A merge side that changed after the build records its conflict, so a broad handler cannot commit the attempt."""
    from dcim.models import Device

    from netbox_librenms_plugin.server_mappings import merge_links, persist_mapping, persist_merge, read_mapping
    from netbox_librenms_plugin.tests.conftest import make_device

    suffix = uuid4().hex[:8]
    winner = make_device(f"runner-merge-winner-{suffix}", librenms_cf={"default": {"id": 9405}})
    donor = make_device(f"runner-merge-donor-{suffix}", librenms_cf={"default": {"id": 9406}})
    calls = []

    def work():
        calls.append(len(calls) + 1)
        rows = [Device.objects.get(pk=winner.pk), Device.objects.get(pk=donor.pk)]
        merge = merge_links(*rows, "default")
        if len(calls) == 1:
            seed_stored_mapping_row(donor, {"default": {"id": 9407}})

        def save_both():
            for row in rows:
                persist_mapping(row, merge.change_for(row), write=lambda locked, fields: locked.save())

        try:
            persist_merge(merge, write=save_both)
        except Exception:
            return "swallowed"
        return "merged"

    result = run_transaction(work)

    assert (result, calls) == ("merged", [1, 2])
    winner.refresh_from_db()
    assert read_mapping(winner).oob_id("default") == 9406


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
