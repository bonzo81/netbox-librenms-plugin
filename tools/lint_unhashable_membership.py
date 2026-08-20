#!/usr/bin/env python3
"""Flag membership tests and key construction that raise TypeError on unhashable input.

``x in {"a", "b"}`` raises ``TypeError: unhashable type: 'list'`` when ``x`` is a list or a
dict, while ``x in ("a", "b")`` returns False. Values decoded from Redis, from a JSON request
body or from a LibreNMS payload can hold any type, so a membership test against a set, a
frozenset or a dict turns a corrupt value into a 500 instead of a rejected input.

The check reports only values it can trace to an external read (``.get()``, a subscript or
``json.loads``) inside the same function, and treats an ``isinstance`` narrowing of the same
expression as a guard. Run it with no arguments to scan the plugin package.
"""

import argparse
import ast
import sys
from pathlib import Path


HASHABLE_NARROWING_TYPES = frozenset({"str", "int", "bytes", "float", "tuple", "frozenset"})
TAINTING_CALLS = frozenset({"get", "loads", "pop"})
# A Django QueryDict always yields str, so a form field can never be unhashable.
QUERYDICT_SOURCES = frozenset({"POST", "GET", "query_params"})
# Helpers that return a hashable value or None, so their result needs no further narrowing.
COERCING_CALLS = frozenset(
    {
        "coerce_librenms_id",
        "coerce_model_pk",
        "_coerce_positive_int",
        "normalize_librenms_port_id",
        "coerce_interface_mtu",
        "normalize_serial",
        "_normalize_librenms_text",
        "_clean_librenms_value",
        "int",
        "str",
        "len",
    }
)
SUPPRESSION = "unhashable-ok:"


def _fingerprint(node):
    """Return a comparable form of an expression, so two spellings of one value match."""
    try:
        return ast.dump(node, annotate_fields=False)
    except Exception:  # pragma: no cover - ast.dump does not raise for parsed trees
        return None


def _is_hashable_container_literal(node):
    """Return whether *node* builds a set, a frozenset or a dict."""
    if isinstance(node, (ast.Set, ast.Dict, ast.SetComp, ast.DictComp)):
        return True
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("set", "frozenset", "dict")
    )


def _isinstance_narrows(node, target_fingerprint):
    """Return whether *node* is an ``isinstance`` call proving *target* is hashable."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance"):
        return False
    if len(node.args) != 2 or _fingerprint(node.args[0]) != target_fingerprint:
        return False
    checked = node.args[1]
    names = [checked] if not isinstance(checked, ast.Tuple) else list(checked.elts)
    return bool(names) and all(isinstance(n, ast.Name) and n.id in HASHABLE_NARROWING_TYPES for n in names)


def _guards_in(node, target_fingerprint, membership_node=None):
    """
    Return whether *node* narrows the target before the membership test can run.

    Position and polarity both matter. ``x in S and isinstance(x, str)`` never protects the
    membership test, and ``not isinstance(x, str) or x not in S`` protects it only because the
    negated form short-circuits first. Walking for any nested ``isinstance`` would call both
    of those guarded.

    Args:
        node: The enclosing guard expression or ``if`` test.
        target_fingerprint: The left operand being tested for membership.
        membership_node: The membership test, when it lives inside *node*.

    Returns:
        bool: Whether the target is provably hashable where the membership test runs.
    """
    if isinstance(node, ast.BoolOp):
        # A negated guard protects only what short-circuits behind it in the SAME or-chain.
        # In an if-test it protects nothing: the body runs whichever operand was true.
        guards_this_chain = membership_node is not None and _contains(node, membership_node)
        for value in node.values:
            # Only operands evaluated BEFORE the membership test can guard it.
            if membership_node is not None and _contains(value, membership_node):
                return False
            if isinstance(node.op, ast.And) and _guards_in(value, target_fingerprint, membership_node):
                return True
            if isinstance(node.op, ast.Or) and guards_this_chain and _negated_isinstance(value, target_fingerprint):
                return True
        return False
    if _isinstance_narrows(node, target_fingerprint):
        return True
    return False


def _negated_isinstance(node, target_fingerprint):
    """Return whether *node* is ``not isinstance(target, ...)``, the fail-closed ``or`` form."""
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and _isinstance_narrows(node.operand, target_fingerprint)
    )


def _contains(node, needle):
    """Return whether *needle* appears anywhere inside *node*."""
    return any(sub is needle for sub in ast.walk(node))


class _Scope:
    """Track which local names hold a value read from outside the process."""

    def __init__(self):
        self.tainted = set()


class MembershipChecker(ast.NodeVisitor):
    """Collect membership tests whose left operand can be unhashable."""

    def __init__(self, path, hashable_containers=(), source_lines=()):
        self.path = path
        self.findings = []
        self._source_lines = list(source_lines)
        # Names are collected across the whole package first, so a constant defined in
        # constants.py is still recognised where another module imports it.
        self.hashable_containers = set(hashable_containers)
        self._scopes = [_Scope()]
        self._guard_stack = []

    def _suppressed(self, lineno):
        """Return whether the flagged line carries an explicit reviewed-and-safe marker."""
        for offset in (lineno - 1, lineno - 2):
            if 0 <= offset < len(self._source_lines) and SUPPRESSION in self._source_lines[offset]:
                return True
        return False

    # -- container constants -------------------------------------------------
    def collect_containers(self, tree):
        """Record every module-level or class-level name bound to a set/frozenset/dict.

        Only constants are tracked. A dict or set built inside a function from a payload
        raises while it is being built, one step before any membership test, so flagging the
        membership test would point at the wrong line and bury the real signal.
        """
        for scope in [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for node in scope.body:
                if not isinstance(node, ast.Assign) or not _is_hashable_container_literal(node.value):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.hashable_containers.add(target.id)
                    elif isinstance(target, ast.Attribute):
                        self.hashable_containers.add(target.attr)

    # -- taint ---------------------------------------------------------------
    @property
    def _scope(self):
        return self._scopes[-1]

    def _is_external_read(self, node):
        """Return whether *node* reads a value the process did not produce."""
        if isinstance(node, ast.Subscript):
            return not self._reads_querydict(node.value)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in COERCING_CALLS:
                return False
            if isinstance(func, ast.Attribute) and func.attr in TAINTING_CALLS:
                return not self._reads_querydict(func.value)
            if isinstance(func, ast.Name) and func.id in TAINTING_CALLS:
                return True
        return False

    @staticmethod
    def _reads_querydict(node):
        """Return whether *node* is ``request.POST``/``request.GET`` or an alias of one."""
        return isinstance(node, ast.Attribute) and node.attr in QUERYDICT_SOURCES

    def _is_tainted(self, node):
        if self._is_external_read(node):
            return True
        return isinstance(node, ast.Name) and node.id in self._scope.tainted

    def visit_FunctionDef(self, node):
        # A fresh taint scope per function; parameters are not treated as external reads,
        # because the caller is checked on its own.
        self._scopes.append(_Scope())
        self.generic_visit(node)
        self._scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node):
        self.generic_visit(node)
        if self._is_tainted(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._scope.tainted.add(target.id)

    # -- guards --------------------------------------------------------------
    def visit_If(self, node):
        self._guard_stack.append(node.test)
        for stmt in node.body:
            self.visit(stmt)
        self._guard_stack.pop()
        for stmt in node.orelse:
            self.visit(stmt)
        self.visit(node.test)

    def visit_BoolOp(self, node):
        self._guard_stack.append(node)
        self.generic_visit(node)
        self._guard_stack.pop()

    # -- the check ------------------------------------------------------------
    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.In, ast.NotIn)):
            return
        right = node.comparators[0]
        right_is_hashable_container = _is_hashable_container_literal(right) or (
            isinstance(right, ast.Name) and right.id in self.hashable_containers
        )
        if isinstance(right, ast.Attribute) and right.attr in self.hashable_containers:
            right_is_hashable_container = True
        if not right_is_hashable_container:
            return

        left = node.left
        if not self._is_tainted(left):
            return

        fingerprint = _fingerprint(left)
        for guard in self._guard_stack:
            if _guards_in(guard, fingerprint, node):
                return

        if self._suppressed(node.lineno):
            return

        self.findings.append(
            (
                self.path,
                node.lineno,
                node.col_offset,
                "membership test against a set/dict on an externally read value; "
                "guard it with isinstance(..., str) first or compare against a tuple",
            )
        )


def collect_container_names(paths):
    """Return every name bound to a set, frozenset or dict literal anywhere in *paths*."""
    names = set()
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        checker = MembershipChecker(path)
        checker.collect_containers(tree)
        names |= checker.hashable_containers
    return names


def check_file(path, hashable_containers=()):
    """Return the findings for one Python file."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [(path, exc.lineno or 0, 0, f"could not parse: {exc.msg}")]
    checker = MembershipChecker(path, hashable_containers, source.splitlines())
    checker.collect_containers(tree)
    checker.visit(tree)
    return checker.findings


def iter_python_files(roots):
    """Yield every Python file under *roots*, skipping migrations and caches."""
    for root in roots:
        root = Path(root)
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*.py")):
            parts = set(path.parts)
            if parts & {"migrations", "__pycache__", "tests"}:
                continue
            yield path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["netbox_librenms_plugin"])
    parser.add_argument("--github", action="store_true", help="emit GitHub annotations")
    args = parser.parse_args(argv)

    paths = list(iter_python_files(args.paths or ["netbox_librenms_plugin"]))
    containers = collect_container_names(paths)
    findings = []
    for path in paths:
        findings.extend(check_file(path, containers))

    for path, line, col, message in findings:
        if args.github:
            print(f"::error file={path},line={line},col={col}::{message}")
        else:
            print(f"{path}:{line}:{col}: {message}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
