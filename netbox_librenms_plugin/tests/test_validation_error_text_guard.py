"""
A user reads the text of a caught ValidationError only through the superuser rule.

NetBox's ``clean()`` messages can name related objects, and admin ``CUSTOM_VALIDATORS`` or
``post_clean`` and ``pre_save`` receivers can add any text under any key. ``exception_text_for``
gives that text only to a superuser. This scan reads each handler that can catch a ValidationError
in a production module: ``except ValidationError``, a tuple that holds it, ``except Exception`` and
``except BaseException``. A handler after one that catches ValidationError is skipped. A read of
the caught error is safe when it is a direct argument of a ``logger`` call (the error itself, or
``validation_error_detail`` of it), the error that a ``raise`` statement raises or chains, or the
first argument of ``exception_text_for``, ``isinstance`` or ``type``. Each other read needs an
allowlist entry with its reason.
"""

import ast
import textwrap
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
# Each handler that can catch a ValidationError: the class, a tuple that holds it, or a base class.
CAUGHT = frozenset({"ValidationError", "Exception", "BaseException"})
RULE = "exception_text_for"
# A read as the first argument of these calls is safe: the rule itself, or a check of the class.
SAFE_CALLEES = frozenset({RULE, "isinstance", "type"})
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
    (
        "librenms_api.py",
        "LibreNMSAPI.test_connection",
        "str",
        "The try body sends one HTTP request with requests and reads its JSON; it validates no model.",
    ),
    (
        "transactions.py",
        "run_transaction",
        "classify_conflict",
        "classify_conflict returns a bool (lock conflict or not); the text stays in the function.",
    ),
    (
        "views/settings_views.py",
        "TestLibreNMSConnectionView.post",
        "str",
        "LibreNMSAPI() reads the config and LibreNMSSettings; test_connection() sends one HTTP request.",
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
            "InstallBranchView.post",
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


def _caught_names(handler_type):
    """Return the class names that an ``except`` clause names, alone or in a tuple."""
    names = handler_type.elts if isinstance(handler_type, ast.Tuple) else [handler_type]
    return {name.id if isinstance(name, ast.Name) else getattr(name, "attr", None) for name in names}


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
    """Collect each read of a caught error that can hold a ValidationError and is not safe."""

    def __init__(self):
        self.scope = []
        self.reads = []

    def _visit_scope(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _visit_scope

    def visit_Try(self, node):
        validation_error_caught = False
        for handler in node.handlers:
            caught = set() if handler.type is None else _caught_names(handler.type)
            # A handler after one that catches ValidationError never gets a ValidationError.
            if handler.name and caught & CAUGHT and not (validation_error_caught and "ValidationError" not in caught):
                self._scan_handler(handler)
            validation_error_caught = validation_error_caught or "ValidationError" in caught
        self.generic_visit(node)

    visit_TryStar = visit_Try

    def _scan_handler(self, handler):
        parents = {child: parent for parent in ast.walk(handler) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(handler):
            if not (isinstance(node, ast.Name) and node.id == handler.name and isinstance(node.ctx, ast.Load)):
                continue
            if (sink := _sink(node, parents)) is not None:
                self.reads.append((".".join(self.scope), sink))


def caught_error_reads(source):
    """Return ``(function, sink)`` for each read of a caught error in *source* that is not safe."""
    scan = _CaughtErrorReadScan()
    scan.visit(ast.parse(source))
    return scan.reads


def _production_files():
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] not in {"tests", "migrations"}:
            yield relative.as_posix(), path.read_text()


def test_a_caught_validation_error_reaches_a_page_only_through_the_superuser_rule():
    found = {
        (relative, function, sink)
        for relative, source in _production_files()
        for function, sink in caught_error_reads(source)
    }
    allowed = {entry[:3] for entry in ALLOWED}

    assert found - allowed == set(), "a caught ValidationError's text can reach a page; use exception_text_for"
    assert allowed - found == set(), "an allowlist entry matches no read; remove it"


def test_the_scan_reads_the_production_handlers():
    """The scan finds an allowlisted read, so it reads the files that it must read."""
    reads = {
        (relative, function) for relative, source in _production_files() for function, _ in caught_error_reads(source)
    }

    assert ("interface_diff.py", "type_change_refusal") in reads


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
        ("detail = exception_text_for(exc.messages, Device, request.user)", [".messages"]),
        ("log.warning('failed: %s', exc)", ["log.warning"]),
    ],
)
def test_the_scan_names_each_read_that_can_reach_a_page(body, expected):
    source = f"def view():\n    try:\n        save()\n    except ValidationError as exc:\n        {body}\n"

    assert [sink for _function, sink in caught_error_reads(source)] == expected


@pytest.mark.parametrize(
    "caught",
    ["ValidationError", "forms.ValidationError", "(IntegrityError, ValidationError)", "Exception", "BaseException"],
)
def test_the_scan_reads_each_handler_that_can_catch_a_validation_error(caught):
    source = f"def view():\n    try:\n        save()\n    except {caught} as exc:\n        detail = str(exc)\n"

    assert caught_error_reads(source) == [("view", "str")]


def test_the_scan_skips_a_handler_that_cannot_catch_a_validation_error():
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
                    detail = str(exc)
                try:
                    save()
                except IntegrityError as exc:
                    detail = str(exc)
                except ValidationError:
                    detail = "refused"
                except:
                    detail = "failed"
        """
    )

    assert caught_error_reads(source) == [("View.post", "str")]
