"""
A user reads the text of a caught ValidationError or database error only through ``exception_text_for``.

NetBox's ``clean()`` messages can name related objects, and admin ``CUSTOM_VALIDATORS`` or
``post_clean`` and ``pre_save`` receivers can add any text under any key. PostgreSQL's error text
can hold key values. ``exception_text_for`` gives validation text only to a superuser and gives a
database error generic text. This scan reads each handler in a production module that can catch
one of these errors. It evaluates the ``except`` expression in the imported module, so aliases and
tuple constants resolve to their classes. A handler can catch such an error when a class that it
names is a ValidationError, a Django or psycopg database error or an ``AbortRequest``, or a base
class of one. A read of the caught error, or of the current exception through ``sys`` or
``traceback``, is safe when it is a direct argument of a ``logger`` call (the error itself, or
``validation_error_detail`` of it), the error that a ``raise`` statement raises or chains, or the
first argument of ``exception_text_for`` or of a check in ``SAFE_CALLEES``. Each other read needs
an allowlist entry with its reason.
"""

import ast
import importlib
import textwrap
from pathlib import Path

import psycopg
import pytest
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist, ValidationError
from django.db.utils import Error as DjangoDatabaseError
from utilities.exceptions import AbortRequest

PACKAGE = Path(__file__).resolve().parents[1]

# A handler that names one of these classes, a subclass or a base class can catch its text.
RISKY = (ValidationError, DjangoDatabaseError, psycopg.Error, AbortRequest)
# ``<model>.DoesNotExist`` on a local model variable: Django derives each one from these bases.
MODEL_ERRORS = {"DoesNotExist": ObjectDoesNotExist, "MultipleObjectsReturned": MultipleObjectsReturned}
RULE = "exception_text_for"
# A read as the first argument of these calls is safe: the rule itself, or a check that returns no error text.
SAFE_CALLEES = frozenset({RULE, "classify_conflict", "database_error_sqlstate", "isinstance", "type"})
# Calls that return the current exception or its text without a read of the bound name.
CURRENT_EXCEPTION = frozenset(
    {
        "sys.exc_info",
        "sys.exception",
        "traceback.format_exc",
        "traceback.format_exception",
        "traceback.format_exception_only",
    }
)
LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})

# (path, function, sink, reason): each entry waives the reads of one sink in one function.
ALLOWED = [
    (
        "interface_diff.py",
        "type_change_refusal",
        "_first_refusal",
        "It builds a TypeRefusal, and TypeRefusal.text_for applies the superuser rule.",
    ),
    (
        "models.py",
        "InterfaceTypeMapping.clean",
        ".message_dict",
        "It re-raises the plugin's own regex message about the rule's own pattern; it names no object.",
    ),
    *(
        ("views/sync/device_fields.py", function, "_write_failure_message", "It applies exception_text_for.")
        for function in (
            "UpdateDeviceNameView.post",
            "UpdateDeviceSerialView.post",
            "UpdateDeviceTypeView.post",
            "UpdateDevicePlatformView.post",
            "CreateAndAssignPlatformView.post",
            "AssignVCSerialView.post",
            "ConvertLegacyLibreNMSIdView.post",
        )
    ),
    *(
        ("views/sync/modules.py", function, "_module_write_failure", "It applies exception_text_for.")
        for function in (
            "InstallModuleView.post",
            "InstallBranchView._install_branch",
            "InstallBranchView._install_single",
            "InstallSelectedView.post",
            "UpdateModuleSerialView.post",
            "ReplaceModuleView.post",
            "MoveModuleView.post",
            "AddBayTemplateView.post",
            "AddBayTemplateView._map_existing_bay",
        )
    ),
]


def _classes(caught):
    """Return the classes in an evaluated ``except`` expression, with nested tuples flattened."""
    return [cls for item in caught for cls in _classes(item)] if isinstance(caught, tuple) else [caught]


def _resolve(node, namespace):
    """Return the classes that an ``except`` expression names in *namespace*; raise when one is unknown."""
    if isinstance(node, ast.Tuple):
        return [cls for element in node.elts for cls in _resolve(element, namespace)]
    if isinstance(node, ast.Starred):
        node = node.value
    try:
        return _classes(eval(ast.unparse(node), namespace))
    except (NameError, AttributeError):
        if isinstance(node, ast.Attribute) and node.attr in MODEL_ERRORS:
            return [MODEL_ERRORS[node.attr]]
        raise


def _can_catch_risky(classes):
    """Return whether a handler for *classes* can get a risky error; the plugin writes its own classes' text."""
    return any(
        issubclass(cls, risky) or issubclass(risky, cls)
        for cls in classes
        if not cls.__module__.startswith(f"{PACKAGE.name}.")
        for risky in RISKY
    )


def _is_log_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in LOG_METHODS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
    )


def _log_argument(node, parents):
    """Return whether *node* is a direct argument of a ``logger`` call."""
    parent = parents.get(node)
    if isinstance(parent, ast.keyword):
        parent = parents.get(parent)
    return _is_log_call(parent)


def _sink(read, parents):
    """Return None for a safe read of the caught error, else the name of what reads it."""
    parent = parents[read]
    if isinstance(parent, ast.Raise) or _log_argument(read, parents):
        return None
    if isinstance(parent, ast.Attribute):
        return f".{parent.attr}"
    if isinstance(parent, ast.Call) and read in parent.args:
        callee = ast.unparse(parent.func)
        if callee == "validation_error_detail" and len(parent.args) == 1 and _log_argument(parent, parents):
            return None
        if callee in SAFE_CALLEES and parent.args[0] is read:
            return None
        return callee
    if isinstance(parent, ast.keyword) and isinstance(parents[parent], ast.Call):
        return ast.unparse(parents[parent].func)
    return type(parent).__name__


class _CaughtErrorReadScan(ast.NodeVisitor):
    """Collect each read of a caught error that can hold a ValidationError or database error and is not safe."""

    def __init__(self, namespace):
        self.namespaces = [namespace]
        self.scope = []
        self.reads = []

    def _visit_scope(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node):
        # A function can import the class that it catches.
        namespace = dict(self.namespaces[-1])
        for statement in ast.walk(node):
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                exec(compile(ast.Module([statement], []), "<local import>", "exec"), namespace)
        self.namespaces.append(namespace)
        self._visit_scope(node)
        self.namespaces.pop()

    visit_ClassDef = _visit_scope
    visit_FunctionDef = visit_AsyncFunctionDef = _visit_function

    def visit_Try(self, node):
        for handler in node.handlers:
            if handler.type is None:
                self._scan_handler(handler)
                continue
            try:
                classes = _resolve(handler.type, self.namespaces[-1])
            except (NameError, AttributeError):
                self.reads.append((".".join(self.scope), f"except {ast.unparse(handler.type)}: cannot resolve"))
                continue
            if _can_catch_risky(classes):
                self._scan_handler(handler)
        self.generic_visit(node)

    visit_TryStar = visit_Try

    def _scan_handler(self, handler):
        parents = {child: parent for parent in ast.walk(handler) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(handler):
            bound = isinstance(node, ast.Name) and node.id == handler.name and isinstance(node.ctx, ast.Load)
            current = isinstance(node, ast.Call) and ast.unparse(node.func) in CURRENT_EXCEPTION
            if not (bound or current):
                continue
            if (sink := _sink(node, parents)) is not None:
                self.reads.append((".".join(self.scope), sink))


def caught_error_reads(source, namespace):
    """Return ``(function, sink)`` for each unsafe read of a caught error in *source*, with its module's *namespace*."""
    scan = _CaughtErrorReadScan(namespace)
    scan.visit(ast.parse(source))
    return scan.reads


def _production_files():
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] not in {"tests", "migrations"}:
            module = ".".join((PACKAGE.name, *relative.with_suffix("").parts)).removesuffix(".__init__")
            yield relative.as_posix(), path.read_text(), vars(importlib.import_module(module))


def test_a_caught_error_reaches_a_page_only_through_exception_text_for():
    found = {
        (relative, function, sink)
        for relative, source, namespace in _production_files()
        for function, sink in caught_error_reads(source, namespace)
    }
    allowed = {entry[:3] for entry in ALLOWED}

    assert found - allowed == set(), "a caught error's text can reach a page; use exception_text_for"
    assert allowed - found == set(), "an allowlist entry matches no read; remove it"


def test_the_scan_reads_the_production_handlers():
    """The scan finds an allowlisted read, so it reads the files that it must read."""
    reads = {
        (relative, function)
        for relative, source, namespace in _production_files()
        for function, _ in caught_error_reads(source, namespace)
    }

    assert ("interface_diff.py", "type_change_refusal") in reads


def _scan(source):
    """Run *source* in a namespace with the classes that the cases name, then scan it."""
    from django import db, forms
    from django.db import DatabaseError, DataError, IntegrityError, OperationalError
    from django.db.models import ProtectedError
    from requests import exceptions as request_errors

    namespace = {
        "__name__": f"{PACKAGE.name}.views.example",
        **{cls.__name__: cls for cls in (ValidationError, AbortRequest, DatabaseError, DataError, IntegrityError)},
        **{cls.__name__: cls for cls in (OperationalError, ProtectedError)},
        "db": db,
        "forms": forms,
        "psycopg": psycopg,
        "RequestException": request_errors.RequestException,
        "_WriteRefused": type("_WriteRefused", (RuntimeError,), {"__module__": "example"}),
    }
    exec(source, namespace)
    return caught_error_reads(source, namespace)


@pytest.mark.parametrize(
    "body, expected",
    [
        ("messages.error(request, exc.messages)", [".messages"]),
        ("detail = exc.message_dict", [".message_dict"]),
        ("detail = exc.message", [".message"]),
        ("detail = exc.error_dict", [".error_dict"]),
        ("detail = exc.error_list", [".error_list"]),
        ("detail = str(exc)", ["str"]),
        ("detail = f'failed: {exc}'", ["FormattedValue"]),
        ("detail = 'failed: %s' % exc", ["BinOp"]),
        ("detail = 'failed: {}'.format(exc)", ["'failed: {}'.format"]),
        ("detail = validation_error_detail(exc)", ["validation_error_detail"]),
        ("return JsonResponse({'error': render(error=exc)})", ["render"]),
        ("saved = exc", ["Assign"]),
        ("logger.warning('failed: %s', validation_error_detail(exc))", []),
        ("logger.warning('failed: %s', exc)", []),
        ("logger.warning('failed', exc_info=exc)", []),
        ("logger.exception('failed: %s', exc.message_dict)", [".message_dict"]),
        ("logger.error('%s', messages.error(request, str(exc)))", ["str"]),
        ("logger.error(f'failed: {exc}')", ["FormattedValue"]),
        ("logger.error('failed: %s', str(exc))", ["str"]),
        ("job.logger.error(f'failed: {exc}')", ["FormattedValue"]),
        ("if isinstance(exc, IntegrityError): name = type(exc).__name__", []),
        ("raise Refused('failed') from exc", []),
        ("raise exc", []),
        ("detail = exception_text_for(exc, Device, request.user)", []),
        ("if classify_conflict(exc): raise", []),
        ("detail = exception_text_for(exc.messages, Device, request.user)", [".messages"]),
        ("log.warning('failed: %s', exc)", ["log.warning"]),
    ],
)
def test_the_scan_names_each_read_that_can_reach_a_page(body, expected):
    source = f"def view():\n    try:\n        save()\n    except ValidationError as exc:\n        {body}\n"

    assert [sink for _function, sink in _scan(source)] == expected


@pytest.mark.parametrize(
    "body, expected",
    [
        ("detail = repr(exc)", ["repr"]),
        ("detail = exc.args[0]", [".args"]),
        ("return exc", ["Return"]),
        ("return HttpResponse(exc)", ["HttpResponse"]),
        ("return JsonResponse({'error': str(exc)})", ["str"]),
        ("return render(request, 'page.html', {'error': exc})", ["Dict"]),
        ("messages.add_message(request, messages.ERROR, exc)", ["messages.add_message"]),
        ("raise AbortRequest(f'failed: {exc}')", ["FormattedValue"]),
        ("raise PermissionDenied(exc)", ["PermissionDenied"]),
        ("raise ValidationError('failed') from exc", []),
        ("if database_error_sqlstate(exc) in CONFLICT_SQLSTATES: raise", []),
        ("detail = database_error_sqlstate(exc.__cause__)", [".__cause__"]),
        ("messages.error(request, exception_text_for(exc, Device, request.user))", []),
        ("detail = traceback.format_exc()", ["Assign"]),
        ("return str(sys.exc_info()[1])", ["Subscript"]),
        ("messages.error(request, sys.exception())", ["messages.error"]),
        ("logger.error('failed: %s', traceback.format_exc())", []),
    ],
)
def test_the_scan_names_each_read_of_a_database_error_that_can_reach_a_page(body, expected):
    source = f"def view():\n    try:\n        save()\n    except DatabaseError as exc:\n        {body}\n"

    assert [sink for _function, sink in _scan(source)] == expected


@pytest.mark.parametrize(
    "caught",
    [
        "ValidationError",
        "forms.ValidationError",
        "(IntegrityError, ValidationError)",
        "Exception",
        "BaseException",
        "DatabaseError",
        "IntegrityError",
        "db.IntegrityError",
        "OperationalError",
        "ProtectedError",
        "psycopg.Error",
        "psycopg.errors.UniqueViolation",
        "AbortRequest",
        "(ValueError, DataError)",
        "(ValueError, (KeyError, DataError))",
        "(*(ValueError,), DataError)",
        "RuntimeError.__mro__[-2]",
    ],
)
def test_the_scan_reads_each_handler_that_can_catch_a_validation_or_database_error(caught):
    source = f"def view():\n    try:\n        save()\n    except {caught} as exc:\n        detail = str(exc)\n"

    assert _scan(source) == [("view", "str")]


@pytest.mark.parametrize("caught", ["ValueError", "RequestException", "(TypeError, KeyError)", "_WriteRefused"])
def test_the_scan_skips_a_handler_that_cannot_catch_a_validation_or_database_error(caught):
    source = f"def view():\n    try:\n        save()\n    except {caught} as exc:\n        detail = str(exc)\n"

    assert _scan(source) == []


def test_the_scan_reads_each_handler_of_a_try_and_names_the_enclosing_function():
    source = textwrap.dedent(
        """
        class View:
            def post(self):
                try:
                    save()
                except (IntegrityError, forms.ValidationError) as exc:
                    def later():
                        return str(exc)
                except Exception as exc:
                    detail = repr(exc)
                except:
                    detail = traceback.format_exc()
        """
    )

    assert _scan(source) == [("View.post", "str"), ("View.post", "repr"), ("View.post", "Assign")]


@pytest.mark.parametrize(
    "caught",
    ["DjangoValidationError", "(ValueError, DjangoValidationError)", "DbIntegrityError", "PgUniqueViolation", "ERRORS"],
)
def test_the_scan_resolves_error_aliases_and_tuple_constants(caught):
    source = (
        "from django.core.exceptions import ValidationError as DjangoValidationError\n"
        "from django.db import IntegrityError as DbIntegrityError\n"
        "from psycopg.errors import UniqueViolation as PgUniqueViolation\n"
        "ERRORS = (ValueError, DbIntegrityError)\n"
        f"def view():\n    try:\n        save()\n    except {caught} as exc:\n        detail = str(exc)\n"
    )

    assert _scan(source) == [("view", "str")]


def test_a_broad_handler_after_a_validation_handler_is_read_because_it_can_catch_a_database_error():
    source = textwrap.dedent(
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        def view():
            try:
                save()
            except DjangoValidationError as exc:
                return exception_text_for(exc, Device, request.user)
            except Exception as exc:
                return str(exc)
        """
    )

    assert _scan(source) == [("view", "str")]


def test_an_except_expression_that_the_module_cannot_resolve_fails_the_scan():
    source = textwrap.dedent(
        """
        def view():
            try:
                save()
            except Exception as exc:
                try:
                    raise exc
                except type(exc) as nested:
                    return str(nested)
        """
    )

    assert _scan(source) == [("view", "except type(exc): cannot resolve")]


def test_the_scan_skips_a_handler_for_a_validation_error_that_the_plugin_defines():
    source = textwrap.dedent(
        """
        class TagNameTaken(ValidationError):
            pass

        def view():
            try:
                save()
            except TagNameTaken as exc:
                form.add_error(None, exc)
        """
    )

    assert _scan(source) == []


def test_the_scan_resolves_a_class_that_the_function_imports():
    source = textwrap.dedent(
        """
        def view():
            from django.db import IntegrityError as LocalIntegrityError

            try:
                save()
            except LocalIntegrityError as exc:
                detail = str(exc)
        """
    )

    assert _scan(source) == [("view", "str")]


def test_the_scan_resolves_the_does_not_exist_class_of_a_model_variable():
    source = textwrap.dedent(
        """
        def view(model):
            try:
                model.objects.get(pk=1)
            except (model.DoesNotExist, model.MultipleObjectsReturned) as exc:
                detail = str(exc)
            try:
                model.objects.get(pk=1)
            except model.WriteFailed as exc:
                detail = str(exc)
        """
    )

    assert _scan(source) == [("view", "except model.WriteFailed: cannot resolve")]
