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


def _unwrap(node):
    """Strip the wrappers that pass a NetBox object (or a collection of them) straight through."""
    while True:
        if isinstance(node, ast.Subscript):
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


def names_an_unrestricted_object(node, tainted) -> bool:
    """Whether *node* evaluates to a NetBox object that no permission scope filtered."""
    inner_branch = _unwrap(node)
    # Per branch, before the scope check: ``unrestricted or Model.objects.restrict(user).first()``
    # returns the UNRESTRICTED side whenever it is truthy, so a scoped sibling proves nothing about
    # the value that actually arrives. A whole-expression scope check would call this one scoped.
    if isinstance(inner_branch, ast.IfExp):
        return any(names_an_unrestricted_object(branch, tainted) for branch in (inner_branch.body, inner_branch.orelse))
    if isinstance(inner_branch, ast.BoolOp):
        return any(names_an_unrestricted_object(value, tainted) for value in inner_branch.values)
    if _is_scoped(node):
        return False
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in UNRESTRICTED_RESOLVERS:
            return True
        if reads_unrestricted_key(sub):
            return True
    inner = _unwrap(node)
    if isinstance(inner, ast.Name):
        return inner.id in tainted
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


def _local_taint(function, seeded):
    """Names inside *function* bound to an unrestricted NetBox object, to a fixed point."""
    tainted = set(seeded)
    while True:
        grew = False
        for node in ast.walk(function):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = bound_names(targets) - tainted
            if names and node.value is not None and names_an_unrestricted_object(node.value, tainted):
                tainted |= names
                grew = True
        if not grew:
            return tainted


def positional_parameters(function):
    """The parameter names *function* takes positionally, in order."""
    args = function.args
    return [arg.arg for arg in (*args.posonlyargs, *args.args)]


def module_taint(tree):
    """Taint every function in *tree*, letting it cross into helpers defined in the same module.

    An unrestricted object handed to a module-private helper is still unrestricted inside it, and
    that is exactly how the serial-match branch built three warnings naming its matched device.
    """
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    seeded = {name: set() for name in functions}
    while True:
        taint = {name: _local_taint(node, seeded[name]) for name, node in functions.items()}
        grew = False
        for name, node in functions.items():
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    continue
                callee = functions.get(call.func.id)
                if callee is None:
                    continue
                positional = positional_parameters(callee)
                for index, argument in enumerate(call.args):
                    if index >= len(positional):
                        break
                    if names_an_unrestricted_object(argument, taint[name]):
                        if positional[index] not in seeded[call.func.id]:
                            seeded[call.func.id].add(positional[index])
                            grew = True
                for keyword in call.keywords:
                    if keyword.arg and names_an_unrestricted_object(keyword.value, taint[name]):
                        if keyword.arg not in seeded[call.func.id]:
                            seeded[call.func.id].add(keyword.arg)
                            grew = True
        if not grew:
            return functions, taint


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
    for node in ast.walk(function):
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


def identity_references(node, tainted, local_functions=frozenset(), identity_functions=frozenset()):
    """Yield the identity-bearing subexpressions of *node*.

    Prunes where identity cannot survive: an aggregate of a tainted object, and a call to a helper
    defined in the same module, whose own body this check reads anyway.
    """
    if reads_unrestricted_key(node):
        yield node
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        # A local helper that hands identity back carries it through its call, even with no tainted
        # argument to follow: ``_owner_name()`` returning ``Device.objects...first().name``.
        if node.func.id in identity_functions:
            yield node
            return
        if node.func.id in NON_IDENTITY_CALLS or node.func.id in local_functions:
            return
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
            and names_an_unrestricted_object(node.value, tainted)
        ):
            yield node
            return
    if isinstance(node, ast.Name):
        if node.id in tainted:
            yield node
        return
    for child in ast.iter_child_nodes(node):
        yield from identity_references(child, tainted, local_functions, identity_functions)


def identity_returning_functions(functions, taint):
    """Local helpers whose RETURN VALUE can carry identity.

    A call to a local helper is normally pruned at the call site, because this check reads the
    helper's own body. That is wrong when the helper hands identity back instead of appending it:
    ``_label(device)`` returning ``device.name`` has no message sink of its own, so
    ``warnings.append(_label(device))`` would escape. Aggregate-only helpers stay prunable.
    """
    returning = set()
    while True:
        grew = False
        for name, function in functions.items():
            if name in returning:
                continue
            # Prune only helpers not yet known to return identity, so a chain of them converges.
            prunable = set(functions) - returning - {name}
            for node in ast.walk(function):
                if not isinstance(node, ast.Return) or node.value is None:
                    continue
                if next(identity_references(node.value, taint[name], prunable, returning), None) is not None:
                    returning.add(name)
                    grew = True
                    break
        if not grew:
            return returning


def check_file(path):
    """Report ``(path, line, expression)`` for every disclosing message in one file."""
    tree = ast.parse(Path(path).read_text())
    functions, taint = module_taint(tree)
    returning = identity_returning_functions(functions, taint)
    prunable = set(functions) - returning
    findings = []
    for name, function in functions.items():
        # No early skip on an empty taint set: a message can read the match straight out of the
        # validation dict, with no local in between.
        tainted = taint[name]
        aliases = message_list_aliases(function)
        for node in ast.walk(function):
            for message in message_expressions(node, aliases):
                for reference in identity_references(message, tainted, prunable, returning):
                    findings.append((str(path), node.lineno, ast.unparse(reference)))
    return findings


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
