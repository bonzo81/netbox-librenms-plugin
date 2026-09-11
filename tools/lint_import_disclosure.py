#!/usr/bin/env python3
"""Flag warning and issue messages that name a NetBox object found by an UNRESTRICTED lookup.

The import preview searches NetBox without a permission scope on purpose: a duplicate the viewer
cannot see is still a real conflict that must block the import. Identity is therefore withheld at
display time, by ``import_utils/disclosure.py``, which can only do that if the identity travels as
structured data. A name or pk baked into a ``warnings``/``issues`` string escapes the gate.

Patching the known sites does not keep the invariant; the next warning forgets. This check reports
any message built from an object that no ``restrict()`` call filtered:

- Sources: a ``Model.objects`` chain, one of the unrestricted lookup helpers, or a match already
  bound into a validation dict (``validation["existing_device"]``).
- Propagation: assignment, tuple unpacking, and the wrappers that pass an object straight through
  (``list()``, ``.first()``, indexing). Taint also crosses into a module-private helper whose
  argument is tainted at a call site, which is where the analysis used to lose the serial-match
  branch.
- Sinks: ``warnings``/``issues`` list writes and whole-list assignment, a ``warning=``/``message=``
  keyword, and a local list the function hands back under one of those keys.

Deliberately narrow: taint follows objects and collections of objects, not every value derived from
a query. A count carries no identity, and flagging one would push real code into contortions.
"""

import argparse
import ast
import sys
from pathlib import Path

# Helpers that resolve a NetBox object with no permission scope applied.
UNRESTRICTED_RESOLVERS = frozenset(
    {
        "find_devices_by_serial",
        "resolve_device_by_host_ip",
        "find_by_librenms_id",
        "get_librenms_sync_device",
    }
)
# Validation-dict keys holding a match that an unrestricted search bound.
UNRESTRICTED_KEYS = frozenset({"existing_device"})
# Calls that apply a permission scope, so their result is not an unrestricted object.
SCOPING_CALLS = frozenset({"restrict", "restricted_queryset", "restrict_object_or_404"})
# Wrappers that hand the object itself onward, so taint must pass through them.
TRANSPARENT_CALLS = frozenset(
    {
        "list",
        "tuple",
        "next",
        "first",
        "last",
        "get",
        "filter",
        "exclude",
        "all",
        "order_by",
        "select_related",
        "prefetch_related",
    }
)
# Attributes and builtins that reduce a tainted object to an aggregate, so a message may use them.
NON_IDENTITY_ATTRS = frozenset({"count", "exists"})
NON_IDENTITY_CALLS = frozenset({"len", "sum", "min", "max", "bool", "int", "any", "all"})
MESSAGE_LISTS = frozenset({"warnings", "issues"})
MESSAGE_LIST_WRITERS = frozenset({"append", "extend", "insert"})
MESSAGE_KWARGS = frozenset({"warning", "message"})

DEFAULT_PATHS = ("netbox_librenms_plugin",)


def _unwrap(node, tainted=frozenset()):
    """Strip the wrappers that pass a NetBox object (or a collection of them) straight through."""
    while True:
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and _variadic_taint(node.value.id, tainted):
                return node
            node = node.value
            continue
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in TRANSPARENT_CALLS:
                if not node.args:
                    return node
                node = node.args[0]
                continue
            if isinstance(func, ast.Attribute) and func.attr in TRANSPARENT_CALLS:
                # ``qs.get(pk=...)`` passes an object through; ``d.get("key")`` reads a value out
                # of a mapping and must not make the mapping itself look like an object.
                if func.attr == "get" and node.args:
                    return node
                node = func.value
                continue
        return node


def _variadic_taint(name, tainted):
    """Whether any element of a variadic parameter is tainted."""
    return any(isinstance(parameter, tuple) and parameter[0] == name for parameter in tainted)


def _tainted_element(node, tainted):
    """Whether a variadic element received an unrestricted object."""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and isinstance(node.slice, ast.Constant)
        and (node.value.id, node.slice.value) in tainted
    )


def reads_unrestricted_key(node) -> bool:
    """Whether *node* reads an unrestricted match straight out of a validation dict."""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value in UNRESTRICTED_KEYS
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        return bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value in UNRESTRICTED_KEYS
    return False


def _receiver_chain(node):
    """Yield each call and attribute on the chain that produces *node*'s own value."""
    while True:
        if isinstance(node, ast.Subscript):
            node = node.value
            continue
        if isinstance(node, ast.Attribute):
            yield node
            node = node.value
            continue
        if isinstance(node, ast.Call):
            yield node
            node = node.func
            continue
        return


def _is_scoped(node) -> bool:
    """Whether a permission scope was applied on the chain that produces *node*.

    Only the receiver chain counts. A whole-expression walk would read the ``restrict()`` inside an
    argument, so ``Device.objects.filter(site=Site.objects.restrict(user).first())`` would pass as
    scoped while the Device it returns is not.
    """
    for sub in _receiver_chain(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in SCOPING_CALLS:
                return True
    return False


def names_an_unrestricted_object(node, tainted, object_functions=frozenset()) -> bool:
    """Whether *node* evaluates to a NetBox object that no permission scope filtered."""
    inner_branch = _unwrap(node, tainted)
    # Per branch, before the scope check: ``unrestricted or Model.objects.restrict(user).first()``
    # returns the UNRESTRICTED side whenever it is truthy, so a scoped sibling proves nothing about
    # the value that actually arrives. A whole-expression scope check would call this one scoped.
    if isinstance(inner_branch, ast.IfExp):
        return any(
            names_an_unrestricted_object(branch, tainted, object_functions)
            for branch in (inner_branch.body, inner_branch.orelse)
        )
    if isinstance(inner_branch, ast.BoolOp):
        return any(names_an_unrestricted_object(value, tainted, object_functions) for value in inner_branch.values)
    if _is_scoped(node):
        return False
    for sub in ast.walk(node):
        # A module-private helper that hands the matched object back is as unrestricted as the
        # query inside it: ``_device()`` returning ``Device.objects.filter(...).first()``.
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            if sub.func.id in UNRESTRICTED_RESOLVERS or sub in object_functions:
                return True
        if reads_unrestricted_key(sub):
            return True
    inner = _unwrap(node, tainted)
    if _tainted_element(inner, tainted):
        return True
    if isinstance(inner, ast.Name):
        return inner.id in tainted or _variadic_taint(inner.id, tainted)
    if isinstance(inner, ast.Attribute):
        return inner.attr == "objects"
    return False


def bound_names(targets):
    """The plain names these assignment targets bind, including through tuple unpacking.

    A ``Subscript`` or ``Attribute`` target is skipped: ``result["existing_device"] = device``
    binds a key, not the name ``result``, and treating it as a binding would taint the whole
    validation dict and every value ever read out of it.
    """
    names = set()
    pending = list(targets)
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
    return names


def scope_nodes(scope):
    """Walk one scope without entering nested definitions."""
    pending = list(reversed(list(ast.iter_child_nodes(scope))))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(reversed(list(ast.iter_child_nodes(node))))


def local_bindings(function):
    """Names bound in a function that shadow its enclosing scope."""
    names = set()
    for node in scope_nodes(function):
        if isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return names


def resolve_functions(tree):
    """Index every function and resolve bare calls in their lexical scope."""
    functions = {}
    callees = {}
    parents = {}

    def visit(scope, enclosing, parent=None):
        nodes = list(scope_nodes(scope))
        bindings = dict(enclosing)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for name in local_bindings(scope):
                bindings.pop(name, None)
            parent = id(scope)
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[id(node)] = node
                parents[id(node)] = parent
                bindings[node.name] = id(node)
        for node in nodes:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in bindings:
                    callees[node] = bindings[node.func.id]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(node, enclosing if isinstance(scope, ast.ClassDef) else bindings, parent)

    visit(tree, {})
    return functions, callees, parents


def _local_taint(function, seeded, object_functions=frozenset()):
    """Names inside *function* bound to an unrestricted NetBox object, to a fixed point."""
    tainted = set(seeded)
    while True:
        grew = False
        for node in scope_nodes(function):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = bound_names(targets) - tainted
            if names and node.value is not None and names_an_unrestricted_object(node.value, tainted, object_functions):
                tainted |= names
                grew = True
        if not grew:
            return tainted


def positional_parameters(function):
    """The parameter names *function* takes positionally, in order."""
    args = function.args
    return [arg.arg for arg in (*args.posonlyargs, *args.args)]


def call_arguments(function, call):
    """Yield each argument with the parameter that receives it."""
    positional = positional_parameters(function)
    for index, argument in enumerate(call.args):
        if index < len(positional):
            yield positional[index], argument
        elif function.args.vararg:
            yield (function.args.vararg.arg, index - len(positional)), argument
    keywords = {arg.arg for arg in (*function.args.args, *function.args.kwonlyargs)}
    for keyword in call.keywords:
        if keyword.arg in keywords:
            yield keyword.arg, keyword.value
        elif function.args.kwarg:
            yield (function.args.kwarg.arg, keyword.arg), keyword.value


def module_taint(functions, callees, parents, object_functions=frozenset()):
    """Taint every function, letting it cross into helpers defined in the same module.

    An unrestricted object handed to a module-private helper is still unrestricted inside it, and
    that is exactly how the serial-match branch built three warnings naming its matched device.
    """
    seeded = {key: set() for key in functions}
    bindings = {key: local_bindings(function) for key, function in functions.items()}
    while True:
        taint = {key: _local_taint(node, seeded[key], object_functions) for key, node in functions.items()}
        grew = False
        for key, node in functions.items():
            inherited = {
                parameter
                for parameter in taint.get(parents[key], ())
                if (parameter[0] if isinstance(parameter, tuple) else parameter) not in bindings[key]
            }
            if inherited - seeded[key]:
                seeded[key].update(inherited)
                grew = True
            for call in scope_nodes(node):
                callee_key = callees.get(call)
                if callee_key is None:
                    continue
                for parameter, argument in call_arguments(functions[callee_key], call):
                    if names_an_unrestricted_object(argument, taint[key], object_functions):
                        if parameter not in seeded[callee_key]:
                            seeded[callee_key].add(parameter)
                            grew = True
        if not grew:
            return taint


def message_list_key(node):
    """The warnings/issues key *node* addresses, else ``None``."""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault":
        if node.args and isinstance(node.args[0], ast.Constant):
            return node.args[0].value
    return None


def message_list_aliases(function):
    """Local names whose list the function hands back as a warnings/issues list."""
    aliases = set()
    for node in scope_nodes(function):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in MESSAGE_LISTS and isinstance(value, ast.Name):
                    aliases.add(value.id)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
            if any(message_list_key(target) in MESSAGE_LISTS for target in node.targets):
                aliases.add(node.value.id)
        if isinstance(node, ast.Call):
            for keyword in node.keywords or []:
                if keyword.arg in MESSAGE_LISTS and isinstance(keyword.value, ast.Name):
                    aliases.add(keyword.value.id)
    return aliases


def message_expressions(node, aliases):
    """Every expression that becomes a user-visible warning or issue message."""
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(message_list_key(target) in MESSAGE_LISTS for target in targets):
            if node.value is not None:
                yield node.value
        # ``notes = [f"{device.name}"]`` where the function hands ``notes`` back as its warnings
        # list: the identity sits in the initializer, never in an append.
        elif node.value is not None and any(
            isinstance(target, ast.Name) and target.id in aliases for target in targets
        ):
            yield node.value
        return
    if not isinstance(node, ast.Call):
        return
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in MESSAGE_LIST_WRITERS:
        if message_list_key(func.value) in MESSAGE_LISTS:
            yield from node.args
        elif isinstance(func.value, ast.Name) and func.value.id in aliases:
            yield from node.args
    for keyword in node.keywords or []:
        if keyword.arg in MESSAGE_KWARGS:
            yield keyword.value


def identity_references(
    node,
    tainted,
    local_functions=frozenset(),
    identity_functions=frozenset(),
    object_functions=frozenset(),
    aggregate_functions=frozenset(),
):
    """Yield the identity-bearing subexpressions of *node*.

    Prunes where identity cannot survive: an aggregate of a tainted object, and a call to a helper
    defined in the same module, whose own body this check reads anyway.
    """
    children = ast.iter_child_nodes(node)
    if reads_unrestricted_key(node) or _tainted_element(node, tainted):
        yield node
        return
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and _variadic_taint(node.value.id, tainted):
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        # A local helper that hands identity back carries it through its call, even with no tainted
        # argument to follow: ``_owner_name()`` returning ``Device.objects...first().name``.
        if node in identity_functions:
            yield node
            return
        if node.func.id in NON_IDENTITY_CALLS or node in aggregate_functions:
            return
        if node in local_functions:
            # Object arguments pass through transparent wrappers into the helper's body.
            children = (
                argument
                for argument in (*node.args, *(keyword.value for keyword in node.keywords))
                if not isinstance(_unwrap(argument), ast.Name)
            )
    if isinstance(node, ast.Attribute):
        if node.attr in NON_IDENTITY_ATTRS:
            return
        base = node
        while isinstance(base, (ast.Attribute, ast.Subscript)):
            base = base.value
        if isinstance(base, ast.Name) and base.id in tainted:
            yield node
            return
        # ``Device.objects.filter(...).first().name`` never binds a local, so the taint pass has no
        # name to mark. Read the receiver itself, but only where it already resolved to an object:
        # a bare ``Model.objects`` path and the method names on the chain are not identity reads.
        if (
            isinstance(node.value, (ast.Call, ast.Subscript))
            and node.attr not in SCOPING_CALLS
            and node.attr not in TRANSPARENT_CALLS
            and names_an_unrestricted_object(node.value, tainted, object_functions)
        ):
            yield node
            return
    if isinstance(node, ast.Name):
        if node.id in tainted or _variadic_taint(node.id, tainted):
            yield node
        return
    for child in children:
        yield from identity_references(
            child, tainted, local_functions, identity_functions, object_functions, aggregate_functions
        )


def identity_returning_functions(functions, callees, taint, object_functions, aggregate_functions):
    """Local helpers whose RETURN VALUE can carry identity.

    A call to a local helper is normally pruned at the call site, because this check reads the
    helper's own body. That is wrong when the helper hands identity back instead of appending it:
    ``_label(device)`` returning ``device.name`` has no message sink of its own, so
    ``warnings.append(_label(device))`` would escape. Aggregate-only helpers stay prunable.
    """
    returning = set()
    while True:
        grew = False
        for key, function in functions.items():
            calls = {call for call, callee in callees.items() if callee == key}
            if calls <= returning:
                continue
            prunable = set(callees) - returning - calls
            for node in scope_nodes(function):
                if not isinstance(node, ast.Return) or node.value is None:
                    continue
                if (
                    next(
                        identity_references(
                            node.value, taint[key], prunable, returning, object_functions, aggregate_functions
                        ),
                        None,
                    )
                    is not None
                ):
                    returning.update(calls)
                    grew = True
                    break
        if not grew:
            return returning


def object_returning_functions(functions, callees, taint, object_functions):
    """Local helpers whose RETURN VALUE is the unrestricted object itself.

    Distinct from :func:`identity_returning_functions`, which classifies a helper that returns an
    identity *attribute*. ``_device()`` returning ``Device.objects.filter(...).first()`` hands the
    object back, so ``_device().name`` at the call site reads identity off an unrestricted match.
    """
    returning = set(object_functions)
    while True:
        grew = False
        for key, function in functions.items():
            calls = {call for call, callee in callees.items() if callee == key}
            if calls <= returning:
                continue
            for node in scope_nodes(function):
                if not isinstance(node, ast.Return) or node.value is None:
                    continue
                if names_an_unrestricted_object(node.value, taint[key], returning):
                    returning.update(calls)
                    grew = True
                    break
        if not grew:
            return frozenset(returning)


def aggregate_returning_functions(functions, callees):
    """Calls to helpers whose returns all reduce their inputs to aggregates."""
    returning = set()
    while True:
        previous = set(returning)
        for key, function in functions.items():
            values = [node.value for node in scope_nodes(function) if isinstance(node, ast.Return)]
            if values and all(
                isinstance(value, ast.Call)
                and (
                    value in returning
                    or isinstance(value.func, ast.Name)
                    and value.func.id in NON_IDENTITY_CALLS
                    or isinstance(value.func, ast.Attribute)
                    and value.func.attr in NON_IDENTITY_ATTRS
                )
                for value in values
            ):
                returning.update(call for call, callee in callees.items() if callee == key)
        if returning == previous:
            return returning


def check_file(path):
    """Report ``(path, line, expression)`` for every disclosing message in one file."""
    tree = ast.parse(Path(path).read_text())
    # Taint and the object-returning helper set define each other, so grow both to a joint fixed
    # point: a helper can only be classified once its own body is tainted, and classifying it can
    # taint a caller that then classifies another helper.
    functions, callees, parents = resolve_functions(tree)
    object_functions = frozenset()
    while True:
        taint = module_taint(functions, callees, parents, object_functions)
        grown = object_returning_functions(functions, callees, taint, object_functions)
        if grown == object_functions:
            break
        object_functions = grown
    aggregates = aggregate_returning_functions(functions, callees)
    returning = identity_returning_functions(functions, callees, taint, object_functions, aggregates)
    prunable = set(callees) - returning
    findings = []
    for key, function in functions.items():
        # No early skip on an empty taint set: a message can read the match straight out of the
        # validation dict, with no local in between.
        tainted = taint[key]
        aliases = message_list_aliases(function)
        for node in scope_nodes(function):
            for message in message_expressions(node, aliases):
                for reference in identity_references(
                    message, tainted, prunable, returning, object_functions, aggregates
                ):
                    findings.append((str(path), node.lineno, ast.unparse(reference)))
    return list(dict.fromkeys(findings))


def iter_python_files(paths):
    """Yield every Python file under *paths*."""
    for entry in paths:
        candidate = Path(entry)
        if candidate.is_dir():
            yield from sorted(candidate.rglob("*.py"))
        elif candidate.suffix == ".py":
            yield candidate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=list(DEFAULT_PATHS))
    parser.add_argument("--github", action="store_true", help="emit GitHub annotations")
    args = parser.parse_args(argv)

    findings = []
    for path in iter_python_files(args.paths or list(DEFAULT_PATHS)):
        findings.extend(check_file(path))

    for path, line, expression in findings:
        message = f"warning names an unrestricted NetBox object: {expression}"
        if args.github:
            print(f"::error file={path},line={line}::{message}")
        else:
            print(f"{path}:{line}: {message}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
