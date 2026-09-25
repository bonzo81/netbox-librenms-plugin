"""
A page shows the text of a caught ValidationError only through the superuser rule.

NetBox's ``clean()`` messages can name related objects, and admin ``CUSTOM_VALIDATORS`` or
``post_clean`` receivers can add any text under any key. ``validation_error_text_for`` gives that
text only to a superuser. This scan finds each read of a caught ValidationError in a production
module. A read is safe when it is an argument of a ``logger`` call, the error that a ``raise``
statement raises or chains, or an argument of ``validation_error_text_for``. Each other read needs
an allowlist entry with its reason.
"""

import ast
import textwrap
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
RULE = "validation_error_text_for"
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
        "views/settings_views.py",
        "LibreNMSSettingsView.post",
        "cable_sync_form.add_error",
        "The form raises only its own tag-name message; LibreNMSSettings has no custom validators.",
    ),
    *(
        ("views/sync/device_fields.py", function, "_write_failure_message", "It applies validation_error_text_for.")
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
        ("views/sync/modules.py", function, "_module_write_failure", "It applies validation_error_text_for.")
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


def _catches_validation_error(handler_type):
    """Return whether an ``except`` clause names ``ValidationError``, alone or in a tuple."""
    if handler_type is None:
        return False
    names = handler_type.elts if isinstance(handler_type, ast.Tuple) else [handler_type]
    return any(
        (isinstance(name, ast.Name) and name.id == "ValidationError")
        or (isinstance(name, ast.Attribute) and name.attr == "ValidationError")
        for name in names
    )


def _is_log_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in LOG_METHODS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
    )


def _sink(read, parents):
    """Return None for a safe read of the caught error, else the name of what reads it."""
    parent = parents[read]
    if isinstance(parent, ast.Raise):
        return None
    if isinstance(parent, ast.Attribute):
        return f".{parent.attr}"
    if isinstance(parent, ast.Call) and read in parent.args:
        callee = ast.unparse(parent.func)
        return None if callee == RULE else callee
    if isinstance(parent, ast.keyword) and isinstance(parents[parent], ast.Call):
        return ast.unparse(parents[parent].func)
    return type(parent).__name__


class _ValidationErrorReadScan(ast.NodeVisitor):
    """Collect each read of a caught ValidationError that is not safe, with the function that holds it."""

    def __init__(self):
        self.scope = []
        self.reads = []

    def _visit_scope(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _visit_scope

    def visit_ExceptHandler(self, node):
        if node.name and _catches_validation_error(node.type):
            self._scan_handler(node)
        self.generic_visit(node)

    def _scan_handler(self, handler):
        parents = {child: parent for parent in ast.walk(handler) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(handler):
            if not (isinstance(node, ast.Name) and node.id == handler.name and isinstance(node.ctx, ast.Load)):
                continue
            if any(_is_log_call(ancestor) for ancestor in _ancestors(node, parents, handler)):
                continue
            if (sink := _sink(node, parents)) is not None:
                self.reads.append((".".join(self.scope), sink))


def _ancestors(node, parents, stop):
    while node is not stop:
        node = parents[node]
        yield node


def validation_error_reads(source):
    """Return ``(function, sink)`` for each read of a caught ValidationError in *source* that is not safe."""
    scan = _ValidationErrorReadScan()
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
        for function, sink in validation_error_reads(source)
    }
    allowed = {entry[:3] for entry in ALLOWED}

    assert found - allowed == set(), "a caught ValidationError's text can reach a page; use validation_error_text_for"
    assert allowed - found == set(), "an allowlist entry matches no read; remove it"


def test_the_scan_reads_the_production_handlers():
    """The scan finds an allowlisted read, so it reads the files that it must read."""
    reads = {
        (relative, function)
        for relative, source in _production_files()
        for function, _ in validation_error_reads(source)
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
        ("logger.exception('failed: %s', exc.message_dict)", []),
        ("raise Refused('failed') from exc", []),
        ("raise exc", []),
        ("detail = validation_error_text_for(exc, Device, request.user)", []),
        ("detail = validation_error_text_for(exc.messages, Device, request.user)", [".messages"]),
        ("log.warning('failed: %s', exc)", ["log.warning"]),
    ],
)
def test_the_scan_names_each_read_that_can_reach_a_page(body, expected):
    source = f"def view():\n    try:\n        save()\n    except ValidationError as exc:\n        {body}\n"

    assert [sink for _function, sink in validation_error_reads(source)] == expected


def test_the_scan_reads_only_a_handler_that_catches_a_validation_error():
    source = textwrap.dedent(
        """
        class View:
            def post(self):
                try:
                    save()
                except (IntegrityError, forms.ValidationError) as exc:
                    def later():
                        return str(exc)
                except IntegrityError as exc:
                    detail = str(exc)
                except ValidationError:
                    detail = "refused"
        """
    )

    assert validation_error_reads(source) == [("View.post", "str")]
