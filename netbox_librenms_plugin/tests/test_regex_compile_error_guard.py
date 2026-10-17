"""
Every handler that catches ``re.error`` also catches ``OverflowError``.

``re.compile`` raises ``OverflowError``, not ``re.error``, for a repeat count such as ``a{4294967295}``.
A handler for ``re.error`` alone lets that pattern crash the page. ``utils.REGEX_COMPILE_ERRORS`` holds both.
"""

import ast
import textwrap
from collections import Counter
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]

RE_ERROR_NAMES = {"error", "PatternError"}

TEMPLATE_ONLY = "The try body compiles no pattern; the error comes from the replacement template."

# (path, function, first statement of the try body, reason): each entry waives one handler.
ALLOWED = [
    (
        "models.py",
        "ModuleBayMapping.clean",
        "_validate_replacement_template(pattern, self.netbox_bay_name)",
        "The helper compiles only a synthetic pattern of plain groups; the error comes from the template.",
    ),
    (
        "models.py",
        "NormalizationRule.clean",
        "_validate_replacement_template(compiled, self.replacement)",
        "The helper compiles only a synthetic pattern of plain groups; the error comes from the template.",
    ),
    (
        "views/sync/modules.py",
        "InstallBranchView._find_parent_module_id",
        "bay_name = match.expand(rm.netbox_bay_name)",
        TEMPLATE_ONLY,
    ),
    (
        "views/sync/modules.py",
        "AddBayTemplateView._derive_mapping_pattern",
        "if compiled.sub(netbox_replacement, librenms_name) != netbox_name:\n    return None",
        TEMPLATE_ONLY,
    ),
    (
        "views/base/modules_view.py",
        "BaseModuleTableView._lookup_regex_bay_mapping",
        "resolved_bay = match.expand(mapping.netbox_bay_name)",
        TEMPLATE_ONLY,
    ),
]


def _re_aliases(tree):
    """Return the names bound to the ``re`` module, and the names bound to its error class."""
    modules, errors = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == "re")
        elif isinstance(node, ast.ImportFrom) and node.module == "re":
            errors.update(alias.asname or alias.name for alias in node.names if alias.name in RE_ERROR_NAMES)
    return modules, errors


def _caught(handler_type):
    """Return the expressions that one ``except`` clause names, with tuples and starred tuples flattened."""
    if isinstance(handler_type, ast.Starred):
        return _caught(handler_type.value)
    if isinstance(handler_type, ast.Tuple):
        return [node for element in handler_type.elts for node in _caught(element)]
    return [handler_type]


def _name(node):
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None


class _HandlerScan(ast.NodeVisitor):
    """Collect every ``try`` whose handler catches ``re.error`` without ``OverflowError``."""

    def __init__(self, re_aliases):
        self.re_modules, self.re_errors = re_aliases
        self.scope = []
        self.violations = []

    def _visit_scope(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _visit_scope

    def visit_Try(self, node):
        for handler in node.handlers:
            if handler.type is not None and self._misses_overflow(_caught(handler.type)):
                self.violations.append((".".join(self.scope), ast.unparse(node.body[0])))
        self.generic_visit(node)

    def _misses_overflow(self, caught):
        catches_re_error = any(
            (isinstance(node, ast.Name) and node.id in self.re_errors)
            or (
                isinstance(node, ast.Attribute)
                and node.attr in RE_ERROR_NAMES
                and isinstance(node.value, ast.Name)
                and node.value.id in self.re_modules
            )
            for node in caught
        )
        covers_overflow = any(_name(node) in {"OverflowError", "REGEX_COMPILE_ERRORS"} for node in caught)
        return catches_re_error and not covers_overflow


def re_error_handlers_without_overflow(source):
    """Return ``(function, first statement of the try body)`` for each handler in *source* that misses OverflowError."""
    tree = ast.parse(source)
    scan = _HandlerScan(_re_aliases(tree))
    scan.visit(tree)
    return scan.violations


def _production_files():
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] not in {"tests", "migrations"}:
            yield relative.as_posix(), path.read_text()


def test_every_re_error_handler_also_catches_overflow_error():
    found = Counter(
        (relative, function, statement)
        for relative, source in _production_files()
        for function, statement in re_error_handlers_without_overflow(source)
    )
    allowed = Counter(entry[:3] for entry in ALLOWED)

    assert found - allowed == Counter(), "catch REGEX_COMPILE_ERRORS where the try body compiles a pattern"
    assert allowed - found == Counter(), "an allowlist entry matches no handler; remove it"


@pytest.mark.parametrize(
    "clause, flagged",
    [
        ("except re.error:", True),
        ("except (re.error, IndexError):", True),
        ("except re.PatternError:", True),
        ("except (_re.error, TypeError):", True),
        ("except regex_error:", True),
        ("except (ValueError, PatternError):", True),
        ("except (*(re.error,),):", True),
        ("except (*(re.error, OverflowError),):", False),
        ("except (re.error, OverflowError):", False),
        ("except REGEX_COMPILE_ERRORS:", False),
        ("except (*REGEX_COMPILE_ERRORS, IndexError):", False),
        ("except (*utils.REGEX_COMPILE_ERRORS, TypeError):", False),
        ("except ValueError:", False),
        ("except other.error:", False),
        ("except:", False),
    ],
)
def test_the_scan_flags_a_re_error_handler_that_misses_overflow_error(clause, flagged):
    source = textwrap.dedent(
        f"""
        import re
        import re as _re
        from re import PatternError
        from re import error as regex_error

        class Rule:
            def compile(self):
                try:
                    re.compile(self.pattern)
                {clause}
                    pass
        """
    )

    expected = [("Rule.compile", "re.compile(self.pattern)")] if flagged else []
    assert re_error_handlers_without_overflow(source) == expected
