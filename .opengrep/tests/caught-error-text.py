# Rule-test fixtures for caught-error-text and caught-error-text-shadow.
#
# Ported from the test suite of the AST guard that these rules replaced, so the verdicts are the
# ones it pinned. Each case is one function. A marked line must match; every other line must not. todoruleid marks an
# accepted detection limit (see .opengrep/README.md). Run scripts/opengrep-test.sh.
import functools
import inspect
import logging
import sys
import traceback
import types

import psycopg
from django import db, forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, DataError, IntegrityError, OperationalError
from django.db import IntegrityError as DbIntegrityError
from django.db.models import ProtectedError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from psycopg.errors import UniqueViolation as PgUniqueViolation
from requests.exceptions import RequestException
from utilities.exceptions import AbortRequest

from netbox_librenms_plugin.transactions import classify_conflict, database_error_sqlstate
from netbox_librenms_plugin.utils import exception_text_for, validation_error_detail

logger = logging.getLogger(__name__)


# --- names each read that can reach a page (except ValidationError)
def read_messages(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        messages.error(request, exc.messages)


def read_message_dict():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = exc.message_dict


def read_message():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = exc.message


def read_error_dict():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = exc.error_dict


def read_error_list():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = exc.error_list


def read_str():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def read_f_string():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = f"failed: {exc}"


def read_percent_format():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = "failed: %s" % exc


def read_str_format():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = "failed: {}".format(exc)


def read_validation_error_detail():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = validation_error_detail(exc)


def read_render_in_json(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return JsonResponse({"error": render(request, error=exc)})


def read_saved_alias():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        saved = exc


def log_validation_error_detail():
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        logger.warning("failed: %s", validation_error_detail(exc))


def log_the_error():
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        logger.warning("failed: %s", exc)


def log_exc_info():
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        logger.warning("failed", exc_info=exc)


def log_message_dict():
    try:
        save()
    except ValidationError as exc:
        # The log is the server's, not a page: a read in a log call is not a finding.
        # ok: caught-error-text
        logger.exception("failed: %s", exc.message_dict)


def log_a_page_message(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        logger.error("%s", messages.error(request, str(exc)))


def log_f_string():
    try:
        save()
    except ValidationError as exc:
        # The log is the server's, not a page: a format in a log call is not a finding.
        # ok: caught-error-text
        logger.error(f"failed: {exc}")


def log_str():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        logger.error("failed: %s", str(exc))


def log_through_a_job_logger(job):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        job.logger.error(f"failed: {exc}")


def check_the_class():
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        if isinstance(exc, IntegrityError):
            # ok: caught-error-text
            return type(exc).__name__


def check_an_attribute():
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        keyed = hasattr(exc, "error_dict")


def chain_the_error():
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        raise Refused("failed") from exc


def raise_the_error():
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        raise exc


def apply_the_rule(request):
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        detail = exception_text_for(exc, Device, request.user)


def apply_the_rule_by_keyword(request):
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        detail = exception_text_for(exc=exc, model=Device, user=request.user)


def apply_the_rule_to_an_attribute_by_keyword(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = exception_text_for(model=Device, user=request.user, exc=exc.messages)


def classify_the_error():
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        if classify_conflict(exc):
            raise


def apply_the_rule_to_an_attribute(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = exception_text_for(exc.messages, Device, request.user)


def log_through_another_logger():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        log.warning("failed: %s", exc)


# --- names each read of a database error that can reach a page (except DatabaseError)
def db_repr():
    try:
        save()
    except DatabaseError as exc:
        # ruleid: caught-error-text
        detail = repr(exc)


def db_args():
    try:
        save()
    except DatabaseError as exc:
        # ruleid: caught-error-text
        detail = exc.args[0]


def db_return():
    try:
        save()
    except DatabaseError as exc:
        # ruleid: caught-error-text
        return exc


def db_http_response():
    try:
        save()
    except DatabaseError as exc:
        # ruleid: caught-error-text
        return HttpResponse(exc)


def db_json_response():
    try:
        save()
    except DatabaseError as exc:
        # ruleid: caught-error-text
        return JsonResponse({"error": str(exc)})


def db_render_context(request):
    try:
        save()
    except DatabaseError as exc:
        # ruleid: caught-error-text
        return render(request, "page.html", {"error": exc})


def db_add_message(request):
    try:
        save()
    except DatabaseError as exc:
        # ruleid: caught-error-text
        messages.add_message(request, messages.ERROR, exc)


def db_abort_request():
    try:
        save()
    except DatabaseError as exc:
        # ruleid: caught-error-text
        raise AbortRequest(f"failed: {exc}")


def db_permission_denied():
    try:
        save()
    except DatabaseError as exc:
        # ruleid: caught-error-text
        raise PermissionDenied(exc)


def db_chain():
    try:
        save()
    except DatabaseError as exc:
        # ok: caught-error-text
        raise ValidationError("failed") from exc


def db_sqlstate():
    try:
        save()
    except DatabaseError as exc:
        # ok: caught-error-text
        if database_error_sqlstate(exc) in CONFLICT_SQLSTATES:
            raise


def db_sqlstate_of_the_cause():
    try:
        save()
    except DatabaseError as exc:
        # A SQLSTATE is a code, not text.
        # ok: caught-error-text
        detail = database_error_sqlstate(exc.__cause__)


def db_message_through_the_rule(request):
    try:
        save()
    except DatabaseError as exc:
        # ok: caught-error-text
        messages.error(request, exception_text_for(exc, Device, request.user))


def db_format_exc():
    try:
        save()
    except DatabaseError:
        # ruleid: caught-error-text
        detail = traceback.format_exc()


def db_exc_info():
    try:
        save()
    except DatabaseError:
        # ruleid: caught-error-text
        return str(sys.exc_info()[1])


def db_current_exception(request):
    try:
        save()
    except DatabaseError:
        # ruleid: caught-error-text
        messages.error(request, sys.exception())


def db_log_format_exc():
    try:
        save()
    except DatabaseError:
        # ok: caught-error-text
        logger.error("failed: %s", traceback.format_exc())


def db_rule_of_format_exc(request):
    try:
        save()
    except DatabaseError:
        # ruleid: caught-error-text
        return exception_text_for(traceback.format_exc(), Device, request.user)


def db_print_exc(buffer):
    try:
        save()
    except DatabaseError:
        # ruleid: caught-error-text
        traceback.print_exc(file=buffer)


def db_format_stack():
    try:
        save()
    except DatabaseError:
        # ok: caught-error-text
        stack = traceback.format_stack()


# --- reads each handler that can catch a validation or database error
def catch_validation_error():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_forms_validation_error():
    try:
        save()
    except forms.ValidationError as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_a_tuple():
    try:
        save()
    except (IntegrityError, ValidationError) as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_exception():
    try:
        save()
    except Exception as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_base_exception():
    try:
        save()
    except BaseException as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_database_error():
    try:
        save()
    except DatabaseError as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_integrity_error():
    try:
        save()
    except IntegrityError as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_db_integrity_error():
    try:
        save()
    except db.IntegrityError as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_operational_error():
    try:
        save()
    except OperationalError as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_protected_error():
    try:
        save()
    except ProtectedError as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_psycopg_error():
    try:
        save()
    except psycopg.Error as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_psycopg_unique_violation():
    try:
        save()
    except psycopg.errors.UniqueViolation as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_abort_request():
    try:
        save()
    except AbortRequest as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_a_mixed_tuple():
    try:
        save()
    except (ValueError, DataError) as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_a_nested_tuple():
    try:
        save()
    except (ValueError, (KeyError, DataError)) as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_a_starred_tuple():
    try:
        save()
    except (*(ValueError,), DataError) as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def catch_a_computed_class():
    try:
        save()
    except RuntimeError.__mro__[-2] as exc:
        # ruleid: caught-error-text
        detail = str(exc)


# --- skips a handler that cannot catch a validation or database error
def skip_value_error():
    try:
        save()
    except ValueError as exc:
        # ok: caught-error-text
        detail = str(exc)


def skip_request_exception():
    try:
        save()
    except RequestException as exc:
        # ok: caught-error-text
        detail = str(exc)


def skip_a_tuple():
    try:
        save()
    except (TypeError, KeyError) as exc:
        # ok: caught-error-text
        detail = str(exc)


class _WriteRefused(RuntimeError):
    pass


def skip_a_local_class():
    try:
        save()
    except _WriteRefused as exc:
        # ok: caught-error-text
        detail = str(exc)


# --- reads each handler of a try and names the enclosing function
class View:
    def post(self):
        try:
            save()
        except (IntegrityError, forms.ValidationError) as exc:

            def later():
                # ruleid: caught-error-text
                return str(exc)

            return later
        except Exception as exc:
            # ruleid: caught-error-text
            detail = repr(exc)
        except:
            # ruleid: caught-error-text
            detail = traceback.format_exc()


# --- resolves error aliases and tuple constants
# ruleid: caught-error-text-shadow
ERRORS = (ValueError, DbIntegrityError)


def alias_validation_error():
    try:
        save()
    except DjangoValidationError as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def alias_in_a_tuple():
    try:
        save()
    except (ValueError, DjangoValidationError) as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def alias_integrity_error():
    try:
        save()
    except DbIntegrityError as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def alias_unique_violation():
    try:
        save()
    except PgUniqueViolation as exc:
        # ruleid: caught-error-text
        detail = str(exc)


def alias_through_a_tuple_constant():
    try:
        save()
    except ERRORS as exc:
        # The shadow rule reports the constant above; the taint rule cannot see through it.
        # todoruleid: caught-error-text
        detail = str(exc)


# --- a broad handler after a validation handler is read because it can catch a database error
def broad_after_narrow(request):
    try:
        save()
    except DjangoValidationError as exc:
        # ok: caught-error-text
        return exception_text_for(exc, Device, request.user)
    except Exception as exc:
        # ruleid: caught-error-text
        return str(exc)


# --- a class that the rule cannot name counts as risky
def computed_nested_class():
    try:
        save()
    except Exception as exc:
        try:
            # ok: caught-error-text
            raise exc
        except type(exc) as nested:
            # ruleid: caught-error-text
            return str(nested)


# --- reads a handler for a validation error that the plugin defines
# ruleid: caught-error-text-shadow
class TagNameTaken(ValidationError):
    pass


def local_subclass(form):
    try:
        save()
    except TagNameTaken as exc:
        # ruleid: caught-error-text
        form.add_error(None, exc.__cause__)


# --- resolves a class that the function imports
def function_import():
    from django.db import IntegrityError as LocalIntegrityError

    try:
        save()
    except LocalIntegrityError as exc:
        # ruleid: caught-error-text
        detail = str(exc)


# --- resolves the does not exist class of a model variable
def model_classes(model):
    try:
        model.objects.get(pk=1)
    except (model.DoesNotExist, model.MultipleObjectsReturned) as exc:
        # ok: caught-error-text
        detail = str(exc)
    try:
        model.objects.get(pk=1)
    except model.WriteFailed as exc:
        # The rule cannot name a class that is an attribute of a variable.
        # todoruleid: caught-error-text
        detail = str(exc)


# --- finds an imported reader of the current exception
from traceback import format_exc as trace_text


def imported_readers(request):
    from sys import exception as current

    try:
        save()
    except Exception:
        # ruleid: caught-error-text
        messages.error(request, trace_text())
    except BaseException:
        # ruleid: caught-error-text
        return current()


# --- an import in a nested function does not change the outer handler
# ruleid: caught-error-text-shadow
Caught = Exception


def nested_import_elsewhere():
    def later():
        from builtins import ValueError as Caught

    try:
        save()
    except Caught as exc:
        # The shadow rule reports the alias above; the taint rule cannot see through it.
        # todoruleid: caught-error-text
        return str(exc)


# --- a name that the function imports twice
def twice_imported():
    from builtins import ValueError as Twice
    from django.db import IntegrityError as Twice

    try:
        save()
    except Twice as exc:
        # ruleid: caught-error-text
        return str(exc)


# --- an except clause resolves a name that two branches import from one place
def two_branches(flag):
    if flag:
        from django.db import IntegrityError as Branch
    else:
        from django.db import IntegrityError as Branch
    try:
        save()
    except Branch as exc:
        # ruleid: caught-error-text
        return str(exc)


# --- a local variable that holds a risky class
def local_class_variable():
    # ruleid: caught-error-text-shadow
    Local = ValidationError
    try:
        save()
    except Local as exc:
        # The shadow rule reports the alias above; the taint rule cannot see through it.
        # todoruleid: caught-error-text
        return str(exc)


# --- imports only the names that an except clause uses
def unrelated_import():
    if False:
        from dcim.models.mixins import NotInThisNetBox
    try:
        save()
    except IntegrityError as exc:
        # ruleid: caught-error-text
        return str(exc)


# --- reads the parameter of a package function that takes the caught error
def report(request, error):
    # ruleid: caught-error-text
    messages.error(request, str(error))


def describe(error, user, *, model=None):
    # ok: caught-error-text
    if isinstance(error, IntegrityError) and hasattr(error, "__cause__"):
        return "The name is taken."
    # ok: caught-error-text
    logger.warning("Write failed: %s", error)
    # ok: caught-error-text
    return exception_text_for(error, model, user)


def collect(*errors, **named):
    # ruleid: caught-error-text
    return errors, named


def relay(error):
    # ruleid: caught-error-text
    return describe(error, None)


def keep(error, output):
    # ruleid: caught-error-text
    output.append(error)


# ruleid: caught-error-text-shadow
def shadowed(error, exception_text_for=str):
    return exception_text_for(error)


def wrapped(function):
    @functools.wraps(function)
    def call(*args):
        return str(args)

    return call


@wrapped
def decorated(error):
    return exception_text_for(error, Device, None)


def helper_by_position(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        report(request, exc)


def helper_by_keyword(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        report(request, error=exc)


def safe_helper(request):
    try:
        save()
    except ValidationError as exc:
        # A helper call is a finding, also when the helper is safe.
        # ruleid: caught-error-text
        detail = describe(exc, request.user)


def safe_helper_by_keyword(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = describe(user=request.user, error=exc)


def safe_helper_wrong_parameter(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = describe(request.user, exc)


def helper_after_a_starred_argument(args):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = describe(*args, exc)


def variadic_helper():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        collect(exc)


def variadic_keyword_helper():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        collect(error=exc)


def relayed_helper():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = relay(exc)


def keeping_helper():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        keep(exc, [])


def shadowing_helper():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = shadowed(exc)


def decorated_helper():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = decorated(exc)


def partial_helper():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        detail = functools.partial(describe, exc)


# --- a call through a name that the scope binds or imports twice
def parameter_named_like_a_helper(describe):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return describe(exc, None)


def class_attribute_named_like_a_helper():
    try:
        save()
    except ValidationError as exc:

        class Output:
            describe = str
            # ruleid: caught-error-text
            text = describe(exc, None)

        return Output


def comprehension_named_like_a_helper(fns):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return [describe(exc, None) for describe in fns]


def imported_twice_like_a_helper():
    from builtins import repr as describe
    from builtins import str as describe

    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return describe(exc, None)


# --- a trusted builtin that the function binds
# ruleid: caught-error-text-shadow
def safe_name_parameter(hasattr=str):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return hasattr(exc)


# --- reads the current exception in any function
def trace(error):
    # ruleid: caught-error-text
    return traceback.format_exc()


def dump(error):
    # ruleid: caught-error-text
    return str(locals())


def current_exception_helpers():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return trace(exc), dump(exc)


# --- a helper that reads the current exception is a read in each caller
def failure_text(error):
    # ruleid: caught-error-text
    return traceback.format_exc()


# A reader kept as a function object, not called, is a detection limit.
def failure_alias(error):
    format_error = traceback.format_exc
    # todoruleid: caught-error-text
    return format_error()


def failure_default(error, format_error=traceback.format_exc):
    # todoruleid: caught-error-text
    return format_error()


def current_exception_callers():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return failure_text(exc), failure_alias(exc), failure_default(exc)


# --- a trusted name that a class body imports or that code stores
formatters = types.SimpleNamespace(exception_text_for=exception_text_for)


def class_body_import(error):
    class Output:
        # ruleid: caught-error-text-shadow
        from builtins import str as exception_text_for

        text = exception_text_for(error)

    return Output.text


def stored_formatter():
    # An attribute is not the trusted name: the call through it below is a finding.
    # ok: caught-error-text-shadow
    formatters.exception_text_for = str
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return class_body_import(exc), formatters.exception_text_for(exc)


# --- reads the caught error in a closure that the function defines before the handler
def closure_before_the_handler():
    def detail():
        # ruleid: caught-error-text
        return str(exc)

    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return detail()


# --- any handler that reads the cause or context of its error reads a chained error
def cause_and_context():
    try:
        try:
            save()
        except ValidationError as exc:
            # ok: caught-error-text
            raise ValueError("failed") from exc
    except ValueError as failure:
        # ruleid: caught-error-text
        return str(failure.__cause__), failure.__context__


# --- a helper that reads the cause of an error from any handler is a read
def failure_detail(error):
    # ruleid: caught-error-text
    return str(error.__cause__ or error)


def cause_in_a_helper():
    try:
        save()
    except ValueError as failure:
        # ruleid: caught-error-text
        return failure_detail(failure)


# --- reads a handler for an exception group because it can hold a risky error
def exception_group():
    try:
        save()
    except ExceptionGroup as failures:
        # ruleid: caught-error-text
        return repr(failures)


def members_of_a_group():
    try:
        save()
    except ValueError as failure:
        # ruleid: caught-error-text
        return [str(each) for each in failure.exceptions]


# --- trusts only the module logger named logger
job_logger = logging.getLogger("fixture.job")


def only_the_module_logger():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        job_logger.error("failed: %s", exc)
        # ok: caught-error-text
        logger.error("failed: %s", exc)


# --- vars of an object does not read the frame
def vars_of_an_object(row):
    # ok: caught-error-text
    fields = vars(row)
    # ruleid: caught-error-text
    frame = vars()
    # ruleid: caught-error-text
    return fields, frame


# --- any handler reads a chained error through getattr but may log it
def getattr_chain():
    try:
        save()
    except ValueError as failure:
        # ok: caught-error-text
        logger.warning("failed: %s", failure.__cause__)
        # ruleid: caught-error-text
        return str(getattr(failure, "__context__", None))


# --- reads a closure that declares the caught error nonlocal
def nonlocal_closure():
    def take_detail():
        nonlocal exc
        # ruleid: caught-error-text
        text = str(exc)
        exc = None
        # ruleid: caught-error-text
        return text

    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return take_detail()


# --- a helper that walks the stack is a read in each caller
def walk_the_stack(error):
    # ruleid: caught-error-text
    stack = traceback.StackSummary.extract(traceback.walk_stack(None), capture_locals=True)
    # ruleid: caught-error-text
    return "".join(stack.format())


def stack_walker_caller():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return walk_the_stack(exc)


# --- a logger method or a chain that the function replaces
log = logging.Logger("fixture")
nested_formatters = types.SimpleNamespace(active=types.SimpleNamespace(exception_text_for=exception_text_for))


def replaced_logger_method():
    log.error = str
    nested_formatters.active = types.SimpleNamespace(exception_text_for=str)
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return log.error(exc), nested_formatters.active.exception_text_for(exc)


# --- reads each method that a call on self can reach
class Base:
    def post(self):
        try:
            save()
        except ValidationError as exc:
            # ruleid: caught-error-text
            return self.failure(exc)

    def failure(self, error):
        return exception_text_for(error, Device, None)

    @staticmethod
    def text(error):
        return exception_text_for(error, Device, None)

    @classmethod
    def build(cls):
        try:
            save()
        except ValidationError as exc:
            # A call of a safe static method is a finding.
            # ruleid: caught-error-text
            return cls.text(exc)


class Child(Base):
    def failure(self, error):
        return error.messages


# --- a method that the instance or the receiver can replace
class ReplacedInInit:
    def __init__(self):
        self.failure = str

    def failure(self, error):
        return exception_text_for(error, Device, None)

    def post(self):
        try:
            save()
        except ValidationError as exc:
            # ruleid: caught-error-text
            return self.failure(exc)


class ReplacedByGetattr:
    def __getattr__(self, name):
        return str

    def post(self):
        try:
            save()
        except ValidationError as exc:
            # ruleid: caught-error-text
            return self.failure(exc)


class ReboundByMatch:
    def failure(self, error):
        return exception_text_for(error, Device, None)

    def post(self):
        match save():
            case self:
                pass
        try:
            save()
        except ValidationError as exc:
            # ruleid: caught-error-text
            return self.failure(exc)


class ReboundByHandler:
    def failure(self, error):
        return exception_text_for(error, Device, None)

    def post(self):
        try:
            save()
        except KeyError as self:
            pass
        try:
            save()
        except ValidationError as exc:
            # ruleid: caught-error-text
            return self.failure(exc)


class ReboundByImport:
    def failure(self, error):
        return exception_text_for(error, Device, None)

    def post(self):
        import builtins as self

        try:
            save()
        except ValidationError as exc:
            # ruleid: caught-error-text
            return self.failure(exc)


# --- a class method that the class replaces
class ReplacedStatic:
    @staticmethod
    def text(error):
        return exception_text_for(error, Device, None)

    @classmethod
    def post(cls):
        cls.text = str
        try:
            save()
        except ValidationError as exc:
            # ruleid: caught-error-text
            return cls.text(exc)


# --- resolves a helper name through the imports of its nested function
def nested_import_helper(error):
    def render_text():
        # ruleid: caught-error-text-shadow
        from builtins import str as exception_text_for

        return exception_text_for(error)

    return render_text()


def nested_import_caller():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return nested_import_helper(exc)


# --- trusts only the log methods of logging
# ruleid: caught-error-text-shadow
class Loud(logging.Logger):
    def error(self, *args):
        return str(args)


loud = Loud("fixture")


def logger_subclass():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return loud.error(exc)


# --- reads a helper that another package module defines
def helper_in_another_module():
    from netbox_librenms_plugin.views.sync.modules import _module_write_failure

    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return _module_write_failure(exc, Device, None)


# --- names the place that keeps the caught error
def store_on_self(self):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        self.last_error = exc


def store_text_on_self(self):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        self.last_error = str(exc)


def store_in_an_item(self, key):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        self.errors[key] = exc.messages


def store_by_append(self):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        self.errors.append(f"failed: {exc}")


def store_by_unpacking(row):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        row.detail, count = repr(exc), 1


def store_in_a_module_list():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        MODULE_ERRORS.append(exc)


def store_in_a_parameter(rows):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        rows.append(exc)


def store_in_a_global():
    global LAST_ERROR
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        LAST_ERROR = str(exc)


def store_in_a_local_list():
    try:
        save()
    except ValidationError as exc:
        errors = []
        # ruleid: caught-error-text
        errors.append(exc)


def store_the_rule_text(self, request):
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        self.last_error = exception_text_for(exc, Device, request.user)


# --- shapes that an AST scan of one helper level could not see
def alias_of_a_chained_error():
    try:
        save()
    except ValueError as failure:
        cause_holder = failure
        # ruleid: caught-error-text
        return str(cause_holder.__cause__)


def second_level(request, error):
    # ruleid: caught-error-text
    messages.error(request, str(error))


def first_level(request, error):
    # ruleid: caught-error-text
    second_level(request, error)


def two_helper_levels(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        first_level(request, exc)


# ruleid: caught-error-text-shadow
class RecordsToThePage(logging.Handler):
    def emit(self, record):
        PAGE_MESSAGES.append(record.getMessage())


def install_a_handler():
    # ruleid: caught-error-text-shadow
    logger.addHandler(RecordsToThePage())


def dynamic_setattr(self, name):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        setattr(self, name, exc)


# --- the trusted names: the shadow rule keeps them bound to the real objects
def local_definition():
    # ruleid: caught-error-text-shadow
    def exception_text_for(exc, model, user):
        return str(exc)

    return exception_text_for


# ruleid: caught-error-text-shadow
logger = logging.getLogger(__name__).getChild("page")


def replace_a_log_method():
    # ruleid: caught-error-text-shadow
    logger.error = str


# ruleid: caught-error-text-shadow
def logger_parameter(logger):
    return logger


def rebind_with_setattr(module):
    # ruleid: caught-error-text-shadow
    setattr(module, "exception_text_for", str)


# ok: caught-error-text-shadow
from netbox_librenms_plugin.utils import exception_text_for

# ok: caught-error-text-shadow
module_logger = logging.getLogger(__name__)


# ok: caught-error-text-shadow
class Refusal(ValueError):
    pass


def group_handler():
    try:
        save()
    # ruleid: caught-error-text-shadow
    except* ValidationError:
        pass


# --- a try statement with an else clause, a finally clause or both
def handler_with_else(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        messages.error(request, str(exc))
    else:
        done()


def handler_with_finally(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        messages.error(request, str(exc))
    finally:
        done()


def handler_with_else_and_finally(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        messages.error(request, str(exc))
    else:
        done()
    finally:
        done()


def second_handler_with_else(request):
    try:
        save()
    except KeyError:
        pass
    except ValidationError as exc:
        # ruleid: caught-error-text
        messages.error(request, str(exc))
    else:
        done()


def handler_before_a_bare_handler(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        messages.error(request, str(exc))
    except:
        pass


def closure_before_a_try_with_finally():
    def detail():
        # ruleid: caught-error-text
        return str(exc)

    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return detail()
    finally:
        done()


def members_of_a_group_with_else():
    try:
        save()
    except ValueError as failure:
        # ruleid: caught-error-text
        return [str(each) for each in failure.exceptions]
    else:
        done()


async def handler_in_async_code(request):
    try:
        await save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        messages.error(request, str(exc))


class HandlerInAClassBody:
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        LAST_ERROR = str(exc)


def handler_in_a_handler(request):
    try:
        save()
    except KeyError:
        try:
            save()
        except ValidationError as exc:
            # ruleid: caught-error-text
            messages.error(request, str(exc))


def handler_in_a_loop_with_else(request, rows):
    for row in rows:
        try:
            save(row)
        except ValidationError as exc:
            # ruleid: caught-error-text
            messages.error(request, str(exc))
            continue
        else:
            done()


def loop_name_in_the_try_body(request, items):
    try:
        for exc in items:
            # ok: caught-error-text
            messages.info(request, exc)
    except ValidationError as exc:
        # ok: caught-error-text
        logger.warning("failed: %s", exc)


# --- shapes beyond the ported cases
def merge_into_form_errors(form_errors):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        exc.update_error_dict(form_errors)


def yield_the_error():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        yield exc


def assert_with_the_error():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        assert False, exc


def add_to_a_message(message):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        message += exc.messages[0]
        # ruleid: caught-error-text
        return message


def walrus_the_error():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        if saved := exc:
            # ruleid: caught-error-text
            return saved


def decorated_helper_in_a_message(request):
    try:
        save()
    except ValidationError as exc:
        # The helper's own body is safe, but the decorator replaces it: the call is a finding.
        # ruleid: caught-error-text
        messages.error(request, decorated(exc))


def overridden_method_in_a_message(view, request):
    try:
        save()
    except ValidationError as exc:
        # A subclass can override the method: the call is a finding.
        # ruleid: caught-error-text
        messages.error(request, view.failure(exc))


def trusted_refusal():
    try:
        save()
    except ValidationError as exc:
        # Only the reviewed factory is trusted: a refusal built anywhere else is a finding.
        # ruleid: caught-error-text
        refusal = TypeRefusal(exc.messages[0], None)
        # ruleid: caught-error-text
        return refusal


# The fixture defines the factory, which the shadow rule reports outside its canonical place.
# ruleid: caught-error-text-shadow
def _first_refusal(exc):
    # ok: caught-error-text
    return TypeRefusal(exc.messages[0], None)


def refusal_through_the_factory(request, user):
    try:
        save()
    except ValidationError as exc:
        # Each call of the factory is a finding, which production reviews at the call.
        # ruleid: caught-error-text
        refusal = _first_refusal(exc)
        # ok: caught-error-text
        messages.error(request, refusal.text_for(user))


def refusal_serialized(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        refusal = TypeRefusal(exc.messages[0], None)
        # ruleid: caught-error-text
        return JsonResponse(refusal._asdict())


def refusal_for_every_viewer():
    try:
        save()
    except ValidationError as exc:
        # plugin_rule=True shows the message to every viewer.
        # ruleid: caught-error-text
        return TypeRefusal(exc.messages[0], None, plugin_rule=True)


def the_error_as_the_viewer():
    try:
        save()
    except ValidationError as exc:
        # The error is the viewer argument here, which never reaches the text.
        # ok: caught-error-text
        return exception_text_for(Device, None, exc)


def id_of_the_error(seen):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        seen.add(id(exc))


# --- the shadow rule: bindings that do not hide a trusted name
class DeviceTable:
    # ok: caught-error-text-shadow
    type = Column()


def set_a_field(interface, original):
    # ok: caught-error-text-shadow
    setattr(interface, "type", original)


# ok: caught-error-text-shadow
class Settings(django.db.models.Model):
    pass


# ruleid: caught-error-text-shadow
class DriverFailure(psycopg.Error):
    pass


# --- each class of the source list, once for each rule: the two copies of the list must agree
def list_django_validation_error():
    try:
        save()
    except django.core.exceptions.ValidationError as exc:
        # ruleid: caught-error-text
        return str(exc)


def list_forms_validation_error():
    try:
        save()
    except django.forms.ValidationError as exc:
        # ruleid: caught-error-text
        return str(exc)


def list_abort_request():
    try:
        save()
    except utilities.exceptions.AbortRequest as exc:
        # ruleid: caught-error-text
        return str(exc)


def list_protected_error():
    try:
        save()
    except (django.db.models.ProtectedError, django.db.models.deletion.RestrictedError) as exc:
        # ruleid: caught-error-text
        return str(exc)


def list_restricted_error():
    try:
        save()
    except (django.db.models.RestrictedError, django.db.models.deletion.ProtectedError) as exc:
        # ruleid: caught-error-text
        return str(exc)


def list_django_db_error():
    try:
        save()
    except (django.db.utils.Error, django.db.transaction.TransactionManagementError) as exc:
        # ruleid: caught-error-text
        return str(exc)


def list_psycopg_error():
    try:
        save()
    except psycopg.OperationalError as exc:
        # ruleid: caught-error-text
        return str(exc)


def list_bare_names():
    try:
        save()
    except (InterfaceError, InternalError, NotSupportedError, ProgrammingError, CableSyncTagNameTaken) as exc:
        # ruleid: caught-error-text
        return str(exc)


def list_group_names():
    try:
        save()
    except (BaseExceptionGroup, RestrictedError, Error) as exc:
        # ruleid: caught-error-text
        return str(exc)


# ruleid: caught-error-text-shadow
LIST_DJANGO_VALIDATION = django.core.exceptions.ValidationError
# ruleid: caught-error-text-shadow
LIST_FORMS_VALIDATION = django.forms.ValidationError
# ruleid: caught-error-text-shadow
LIST_ABORT = utilities.exceptions.AbortRequest
# ruleid: caught-error-text-shadow
LIST_PROTECTED = (django.db.models.ProtectedError, django.db.models.deletion.RestrictedError)
# ruleid: caught-error-text-shadow
LIST_RESTRICTED = (django.db.models.RestrictedError, django.db.models.deletion.ProtectedError)
# ruleid: caught-error-text-shadow
LIST_DJANGO_DB = (django.db.utils.Error, django.db.transaction.TransactionManagementError)
# ruleid: caught-error-text-shadow
LIST_PSYCOPG = psycopg.OperationalError
# ruleid: caught-error-text-shadow
LIST_PSYCOPG_ERRORS = psycopg.errors.UniqueViolation
# ruleid: caught-error-text-shadow
LIST_BARE = (InterfaceError, InternalError, NotSupportedError, ProgrammingError, CableSyncTagNameTaken)
# ruleid: caught-error-text-shadow
LIST_GROUPS = (BaseExceptionGroup, RestrictedError, Error)
# ruleid: caught-error-text-shadow
LIST_BASE = BaseException


# --- a lambda default: taint analysis does not read it, so the shadow rule reports it
def lambda_default_text(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text-shadow
        transaction.on_commit(lambda detail=str(exc): messages.error(request, detail))


def lambda_default_error(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text-shadow
        transaction.on_commit(lambda kind="error", error=exc: messages.error(request, error))


def lambda_default_keyword_only(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text-shadow
        transaction.on_commit(lambda *, detail=exc.messages: messages.error(request, detail))


def lambda_default_in_an_unnamed_handler(request, librenms_os):
    try:
        save()
    except ValidationError:
        # ok: caught-error-text-shadow
        transaction.on_commit(lambda os=librenms_os: messages.warning(request, f"The OS {os} is invalid."))


def partial_in_a_handler(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        transaction.on_commit(functools.partial(messages.error, request, str(exc)))


def decorator_in_a_handler(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text-shadow
        @register(str(exc))
        def later():
            pass

        return later


def match_in_a_handler(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text-shadow
        match exc:
            case ValidationError(messages=found):
                messages.error(request, found)


# --- more bindings of a trusted name
def comprehension_binds_logger(job):
    try:
        save()
    except Exception as exc:
        # ruleid: caught-error-text, caught-error-text-shadow
        return [logger.error(exc) for logger in [job.logger]]


def tuple_target_binds_logger(job):
    # ruleid: caught-error-text-shadow
    count, logger = 1, job.logger
    return count, logger


def loop_tuple_binds_logger(pairs):
    # ruleid: caught-error-text-shadow
    for name, logger in pairs:
        return name, logger


def with_binds_logger(job):
    # ruleid: caught-error-text-shadow
    with job.logging() as logger:
        return logger


def handler_binds_logger():
    try:
        save()
    # ruleid: caught-error-text-shadow
    except RuntimeError as logger:
        return logger


def walrus_binds_the_rule(formatters):
    # ruleid: caught-error-text-shadow
    if exception_text_for := formatters.get("text"):
        return exception_text_for


def loop_reads_the_logger():
    # ok: caught-error-text-shadow
    for handler in logger.handlers:
        return handler


# --- a suppression covers its line only: production keeps a reviewed call on its own line
def reviewed_helper_then_raw_error(request):
    try:
        save()
    except ValidationError as exc:
        # Production suppresses this line; the next line is a finding of its own.
        # ruleid: caught-error-text
        detail = describe(exc, request.user)
        # ruleid: caught-error-text
        messages.error(request, f"{detail} {exc}")


# --- review round two: argument expansion, shapes over several lines, case captures, yield from
def refusal_with_keyword_expansion(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return TypeRefusal(exc.args[0], **{"field": None, "plugin_rule": True}).text_for(request.user)


def refusal_with_positional_expansion(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return TypeRefusal(exc.args[0], *[None, True]).text_for(request.user)


def lambda_default_over_two_lines():
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text-shadow
        report = (lambda
                  text=str(exc): text)
        return report()


def loop_target_over_three_lines(pairs):
    # ruleid: caught-error-text-shadow
    for (
        logger,
    ) in pairs:
        return logger


def case_capture_binds_logger(job):
    match job:
        # ruleid: caught-error-text-shadow
        case logger:
            return logger


def loop_binds_a_builtin(classes):
    # ruleid: caught-error-text-shadow
    for type in classes:
        return type


def errors_of_a_generator():
    try:
        save()
    except Exception as exc:
        # ruleid: caught-error-text
        yield from exc.args


# --- review round three: the field of a refusal, case captures over several lines, builtins classes
def refusal_with_a_raw_field(user):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        for field in exc.error_dict:
            # ruleid: caught-error-text
            return HttpResponse(TypeRefusal("Operation refused.", field).text_for(user))


def refusal_outside_the_factory(request, user):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        key = next(iter(exc.error_dict))
        # ruleid: caught-error-text
        refusal = TypeRefusal("Operation refused.", refused_model_field(Interface, key))
        # ruleid: caught-error-text
        messages.error(request, refusal.text_for(user))


def case_capture_over_three_lines(callbacks):
    match callbacks:
        # ruleid: caught-error-text-shadow
        case [
            exception_text_for
        ]:
            return exception_text_for


def builtins_exception():
    import builtins

    try:
        save()
    except builtins.Exception as exc:
        # ruleid: caught-error-text
        return HttpResponse(str(exc))


def builtins_exception_alias():
    from builtins import Exception as AnyError

    try:
        save()
    except AnyError as exc:
        # ruleid: caught-error-text
        return HttpResponse(str(exc))


# --- review round four: a match header with a comment, a lambda default that calls a function
def match_header_with_a_comment(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text-shadow
        match exc:  # Inspect the validation fields.
            case ValidationError(message=detail):
                messages.error(request, detail)


def lambda_default_call_in_an_unnamed_handler(request):
    try:
        save()
    except ValidationError:
        transaction.on_commit(
            # ruleid: caught-error-text-shadow
            lambda detail=traceback.format_exc():
                messages.error(request, detail)
        )


# ruleid: caught-error-text-shadow
LATER = lambda detail=sys.exc_info(): detail


def factory_name_bound_again():
    # ruleid: caught-error-text-shadow
    _first_refusal = str
    return _first_refusal


# --- review round five: a lambda before the handler, a loop that stores the error
def lambda_before_the_handler(request):
    # ruleid: caught-error-text
    report = lambda: messages.error(request, str(exc))
    try:
        save()
    except ValidationError as exc:
        report()


def same_name_in_an_earlier_handler(request):
    try:
        load()
    except ValueError as exc:
        # ok: caught-error-text
        messages.error(request, str(exc))
    try:
        save()
    except ValidationError as exc:
        # ok: caught-error-text
        logger.warning("failed: %s", exc)


def loop_target_stores_the_error(context):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        for context["error"] in exc.messages:
            pass
        return HttpResponse(context["error"])


# --- review round six: a slice, a colon in a lambda default string, an augmented assignment
def slice_in_a_response(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        return JsonResponse({"errors": exc.messages[:1]}, status=400)


def slice_of_an_index(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        messages.error(request, exc.args[0][:100])


def slice_with_a_step(request):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        messages.error(request, exc.messages[::2])


def lambda_default_string_with_a_colon(request):
    try:
        save()
    except Exception:
        transaction.on_commit(
            # ruleid: caught-error-text-shadow
            lambda text="Sync failed: " + traceback.format_exc():
                messages.error(request, text)
        )


def merge_into_a_context(context):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        context |= {"errors": exc.messages}


def format_into_a_message(message):
    try:
        save()
    except ValidationError as exc:
        # ruleid: caught-error-text
        message %= exc.messages
        # ruleid: caught-error-text
        return message


# --- review round seven: a dict in a lambda default, a builtin bound at module level
def lambda_default_dict_with_a_call(request):
    # ruleid: caught-error-text-shadow
    callback = lambda context={"reason": sys.exception()}: context
    return callback


# ruleid: caught-error-text-shadow
hasattr = getattr
