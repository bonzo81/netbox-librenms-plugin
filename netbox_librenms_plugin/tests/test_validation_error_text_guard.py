"""
A user reads the text of a caught ValidationError or database error only through ``exception_text_for``.

NetBox's ``clean()`` messages can name related objects, and admin ``CUSTOM_VALIDATORS`` or
``post_clean`` and ``pre_save`` receivers can add any text under any key. PostgreSQL's error text
can hold key values. ``exception_text_for`` gives validation text only to a superuser and gives a
database error generic text. This scan reads each handler in a production module that can catch
one of these errors. It evaluates the ``except`` expression in the imported module, so aliases and
tuple constants resolve to their classes. A handler can catch such an error when a class that it
names is a ValidationError, a Django or psycopg database error or an ``AbortRequest``, or a base
class of one. A read of the caught error is safe when it is a direct argument of a logger call (the
error itself, or ``validation_error_detail`` of it), the error that a ``raise`` statement raises or
chains, or the first argument of a function in ``SAFE_CALLEES``. The scan trusts a logger method or
a function only when the name resolves to that object, no scope around the read binds the name, and
no code stores a new value under the name.

When a handler gives the caught error to a function of the package, the scan reads that function
in place of the call, one level deep: the parameter that takes the error has the same rules, and a
call that gives it to a further package function is a read. The sink is ``<function>: <sink>`` in
the handler's function, so each caller of a helper needs its own entry. A read that goes into an
attribute, an item, or a name outside the function has the sink ``store <place>``. A reference to a
function that returns the current exception, or a frame or namespace that holds it, is a read in
any function, and a read of each caller that gives the error to that function.
Each other read needs an allowlist entry with its reason.
"""

import ast
import functools
import importlib
import inspect
import logging
import sys
import textwrap
import traceback
import types
from pathlib import Path

import psycopg
import pytest
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist, ValidationError
from django.db.utils import Error as DjangoDatabaseError
from utilities.exceptions import AbortRequest

from netbox_librenms_plugin.transactions import classify_conflict, database_error_sqlstate
from netbox_librenms_plugin.utils import exception_text_for, validation_error_detail

PACKAGE = Path(__file__).resolve().parents[1]

# A handler that names one of these classes, a subclass or a base class can catch its text; a group can hold one.
RISKY = (ValidationError, DjangoDatabaseError, psycopg.Error, AbortRequest, BaseExceptionGroup)
# ``<model>.DoesNotExist`` on a local model variable: Django derives each one from these bases.
MODEL_ERRORS = {"DoesNotExist": ObjectDoesNotExist, "MultipleObjectsReturned": MultipleObjectsReturned}
RULE = "exception_text_for"
# A read as the first argument of these calls is safe: the rule itself, or a check that returns no error text.
SAFE_CALLEES = (exception_text_for, classify_conflict, database_error_sqlstate, hasattr, isinstance, type)
# Calls that return the current exception, its text, or a frame or namespace that holds it, with no read of its name.
CURRENT_EXCEPTION = (
    sys.exc_info,
    sys.exception,
    sys._getframe,
    sys._current_frames,
    traceback.format_exc,
    traceback.format_exception,
    traceback.format_exception_only,
    traceback.format_tb,
    traceback.print_exc,
    traceback.print_exception,
    traceback.walk_stack,
    traceback.walk_tb,
    traceback.extract_stack,
    traceback.extract_tb,
    traceback.format_stack,
    traceback.print_stack,
    traceback.StackSummary,
    traceback.TracebackException,
    inspect.currentframe,
    inspect.stack,
    inspect.trace,
    inspect.getouterframes,
    inspect.getinnerframes,
    locals,
    vars,
    eval,
    exec,
)
CURRENT_NAMES = frozenset(function.__name__ for function in CURRENT_EXCEPTION)
# A function that imports a name from two places: the scan cannot know which import binds it.
TWICE = object()
# An error that another error chains: any handler can read it through these attributes.
CHAIN = frozenset({"__cause__", "__context__", "exceptions"})
LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
# A call of one of these methods keeps its arguments in the object that it is called on.
MUTATORS = frozenset({"add", "append", "appendleft", "extend", "extendleft", "insert", "setdefault", "update"})

# (path, function, sink, reason): each entry waives the reads of one sink in one function.
ALLOWED = [
    *(
        (
            "interface_diff.py",
            "type_change_refusal",
            f"_first_refusal: {sink}",
            "It returns a TypeRefusal, and TypeRefusal.text_for applies the superuser rule.",
        )
        for sink in (".messages", ".message_dict")
    ),
    (
        "models.py",
        "InterfaceTypeMapping.clean",
        ".message_dict",
        "It re-raises the plugin's own regex message about the rule's own pattern; it names no object.",
    ),
    *(
        (
            "views/sync/device_fields.py",
            function,
            "_write_failure_message: .error_dict",
            "The keys only choose the wording; the text comes from exception_text_for.",
        )
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
        (
            "views/sync/modules.py",
            function,
            "_module_write_failure: str",
            "It looks for a constraint name in the text; the page gets fixed text or exception_text_for.",
        )
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


def _resolve(node, namespace, local):
    """Return the classes that an ``except`` expression names in *namespace*; raise when one is unknown or *local*."""
    if isinstance(node, ast.Tuple):
        return [cls for element in node.elts for cls in _resolve(element, namespace, local)]
    if isinstance(node, ast.Starred):
        node = node.value
    try:
        if {name.id for name in ast.walk(node) if isinstance(name, ast.Name)} & local:
            raise NameError("a scope around the clause binds the name")
        return _classes(eval(ast.unparse(node), namespace))
    except (NameError, AttributeError):
        if isinstance(node, ast.Attribute) and node.attr in MODEL_ERRORS:
            return [MODEL_ERRORS[node.attr]]
        raise


def _can_catch_risky(classes):
    return any(issubclass(cls, risky) or issubclass(risky, cls) for cls in classes for risky in RISKY)


def _own_imports(scope):
    """Return a one-name import statement for each name that a function or class *scope* imports outside nested scopes."""
    imports = {}
    nodes = list(scope.body)
    while nodes:
        node = nodes.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                statement = (
                    ast.Import([alias])
                    if isinstance(node, ast.Import)
                    else ast.ImportFrom(node.module, [alias], node.level)
                )
                name = (alias.asname or alias.name).split(".")[0]
                same = ast.dump(imports.get(name, statement)) == ast.dump(statement)
                imports[name] = statement if same else TWICE
        nodes.extend(ast.iter_child_nodes(node))
    return imports


def _namespace_with_imports(expression, namespace, imports):
    """Return *namespace* plus the function *imports* that *expression* names; raise for a name imported twice."""
    names = {node.id for node in ast.walk(expression) if isinstance(node, ast.Name)}
    statements = [next((scope[name] for scope in reversed(imports) if name in scope), None) for name in names]
    if TWICE in statements:
        raise NameError("a function imports this name twice")
    if not any(statements):
        return namespace
    namespace = dict(namespace)
    for statement in filter(None, statements):
        exec(compile(ast.fix_missing_locations(ast.Module([statement], [])), "<local import>", "exec"), namespace)
    return namespace


def _is_name_chain(node):
    """Return whether *node* is a name or a chain of attributes on a name, which eval reads without a call."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def _is_one_of(value, objects):
    return any(value is known for known in objects)


def _identifier(node):
    return node.id if isinstance(node, ast.Name) else node.attr


def _callable(expression, local, namespace, imports, stores):
    """Return the object that a name chain names in the module, or None when a scope binds it or code can replace it."""
    _, global_names, owned = stores
    if not _is_name_chain(expression):
        return None
    links = list(ast.walk(expression))
    if {node.id for node in links if isinstance(node, ast.Name)} & (local | global_names) or {
        (ast.unparse(node.value), node.attr) for node in links if isinstance(node, ast.Attribute)
    } & owned:
        return None
    try:
        return eval(ast.unparse(expression), _namespace_with_imports(expression, namespace, imports))
    except (NameError, AttributeError):
        return None


def _current_exception_read(node, parents, candidates, namespace_for):
    """Return the read that *node* makes when it names a function in CURRENT_EXCEPTION: its call, or the name itself."""
    if not (isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load) and _is_name_chain(node)):
        return None
    parent = parents.get(node)
    if _identifier(node) not in candidates:
        return None
    try:
        found = eval(ast.unparse(node), namespace_for(node))
    except (NameError, AttributeError):
        return None
    if not _is_one_of(found, CURRENT_EXCEPTION):
        return None
    if found is vars and isinstance(parent, ast.Call) and parent.func is node and (parent.args or parent.keywords):
        return None
    return parent if isinstance(parent, ast.Call) and parent.func is node else node


def _aliases(tree):
    """Return the names that the imports of *tree* bind with ``as``: an alias can name a function in CURRENT_EXCEPTION."""
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    return {alias.asname for node in imports for alias in node.names if alias.asname}


def _is_log_call(node, resolve):
    """Return whether *node* calls a log method of ``logging`` on a logger, with no override on its class or itself."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LOG_METHODS):
        return False
    # Only the module's own ``logger``: another logger can write where a page reads.
    if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
        return False
    method = resolve(node.func)
    return any(
        getattr(method, "__func__", None) is getattr(cls, node.func.attr) and isinstance(method.__self__, cls)
        for cls in (logging.Logger, logging.LoggerAdapter)
    )


def _log_argument(node, parents, resolve):
    """Return whether *node* is a direct argument of a call on a logger."""
    parent = parents.get(node)
    if isinstance(parent, ast.keyword):
        parent = parents.get(parent)
    return _is_log_call(parent, resolve)


def _sink(read, parents, bound, resolve):
    """Return None for a safe read of the caught error, else the name of what reads it; SAFE_CALLEES take only *bound*."""
    parent = parents[read]
    if isinstance(parent, ast.Raise) or _log_argument(read, parents, resolve):
        return None
    if isinstance(parent, ast.Attribute):
        return f".{parent.attr}"
    if isinstance(parent, ast.Call) and read in parent.args:
        callee = resolve(parent.func)
        if callee is validation_error_detail and len(parent.args) == 1 and _log_argument(parent, parents, resolve):
            return None
        if bound and _is_one_of(callee, SAFE_CALLEES) and parent.args[0] is read:
            return None
        return ast.unparse(parent.func)
    if isinstance(parent, ast.keyword) and isinstance(parents[parent], ast.Call):
        return ast.unparse(parents[parent].func)
    return type(parent).__name__


def _call_of(read, parents):
    """Return the call that takes *read* as an argument, or None."""
    parent = parents[read]
    if isinstance(parent, ast.keyword) and parent.arg is not None:
        parent = parents[parent]
        return parent if isinstance(parent, ast.Call) else None
    return parent if isinstance(parent, ast.Call) and read in parent.args else None


def _is_read_of(node, name):
    return isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)


def _scope_bindings(scope):
    """Return the names that a function or lambda *scope* binds in its body."""
    if isinstance(scope, ast.Lambda):
        return set()
    made, declared = _statement_bindings(scope.body)
    return made | declared


def _parents(tree):
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _ancestors(node, parents):
    while (node := parents.get(node)) is not None:
        yield node


def _import_stack(read, parents):
    """Return the imports of each function around *read*, from the outermost to the innermost."""
    functions = [
        node for node in _ancestors(read, parents) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return [_own_imports(function) for function in reversed(functions)]


def _statement_bindings(statements):
    """Return the names that *statements* bind other than by an import, and the names that they declare global."""
    made, declared = set(), set()
    nodes = list(statements)
    while nodes:
        node = nodes.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            made.add(node.name)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            made.add(node.id)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            made.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            made.add(node.rest)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
        nodes.extend(ast.iter_child_nodes(node))
    return made, declared


def _parameters(arguments):
    return {arg.arg for arg in ast.walk(arguments) if isinstance(arg, ast.arg)}


def _enclosing(read, parents):
    """Return the names that the scopes around *read* bind, and the names that its functions make and declare global."""
    local, made, declared = set(), set(), set()
    in_function = False
    node = parents.get(read)
    while node is not None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_made, body_declared = _statement_bindings(node.body)
            local |= _parameters(node.args) | body_made | body_declared
            made |= body_made
            declared |= body_declared
            in_function = True
        elif isinstance(node, ast.Lambda):
            local |= _parameters(node.args)
            in_function = True
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            local |= {
                name.id for each in node.generators for name in ast.walk(each.target) if isinstance(name, ast.Name)
            }
        elif isinstance(node, ast.ClassDef) and not in_function:
            local |= _statement_bindings(node.body)[0] | set(_own_imports(node))
        node = parents.get(node)
    return local, made, declared


def _lasting(container, made, declared):
    """Return whether *container* outlives the function: an attribute, or a name that the function does not make."""
    while isinstance(container, ast.Subscript):
        container = container.value
    if isinstance(container, ast.Name):
        return container.id in declared or container.id not in made
    return True


def _places(target, made, declared):
    """Return the places in an assignment *target* that outlive the function."""
    if isinstance(target, (ast.Tuple, ast.List)):
        return [place for element in target.elts for place in _places(element, made, declared)]
    if isinstance(target, ast.Starred):
        return _places(target.value, made, declared)
    if isinstance(target, ast.Name):
        return [target] if target.id in declared else []
    while isinstance(target, ast.Subscript):
        target = target.value
    return [target] if _lasting(target, made, declared) else []


def _store(read, parents, made, declared):
    """Return ``store <place>`` when the value of *read* goes into an attribute, an item or a name outside the function."""
    node = read
    while not isinstance(node, ast.stmt):
        parent = parents[node]
        if (
            isinstance(parent, ast.Call)
            and node is not parent.func
            and isinstance(parent.func, ast.Attribute)
            and parent.func.attr in MUTATORS
            and _lasting(parent.func.value, made, declared)
        ):
            return f"store {ast.unparse(parent.func.value)}"
        if isinstance(parent, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and node is parent.value:
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            if places := [place for target in targets for place in _places(target, made, declared)]:
                return f"store {', '.join(ast.unparse(place) for place in places)}"
        node = parent
    return None


@functools.cache
def _production_paths():
    return tuple(
        path
        for path in sorted(PACKAGE.rglob("*.py"))
        if path.relative_to(PACKAGE).parts[0] not in {"tests", "migrations"}
    )


def _package_function(function, own_file):
    """Return whether *function* is a plain function of a production module of the package, or of the scanned source."""
    if not isinstance(function, types.FunctionType) or hasattr(function, "__wrapped__"):
        return False
    path = Path(function.__code__.co_filename)
    return str(path) == own_file or path in _production_paths()


def _definitions(tree):
    """Return each function of *tree* outside a function body, keyed by its qualified name and first line."""
    found = {}
    nodes = [(node, "") for node in tree.body]
    while nodes:
        node, prefix = nodes.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
            found[(f"{prefix}{node.name}", first)] = node
        elif isinstance(node, ast.ClassDef):
            nodes.extend((child, f"{prefix}{node.name}.") for child in node.body)
        elif isinstance(node, ast.stmt):
            nodes.extend((child, prefix) for child in ast.iter_child_nodes(node))
    return found


def _stores(tree):
    """
    Return what *tree* can replace: the attribute names that it assigns, deletes or sets with a literal name, the
    names that it makes global, and ``(owner, attribute)`` for each attribute that it assigns or deletes.
    """
    names, global_names, owned = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            names.add(node.attr)
            owned.add((ast.unparse(node.value), node.attr))
        elif isinstance(node, ast.Global):
            global_names.update(node.names)
        elif (
            isinstance(node, ast.Call)
            and ast.unparse(node.func) == "setattr"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
        ):
            names.add(node.args[1].value)
    return names, global_names, owned


@functools.cache
def _production_stores():
    stores = [_stores(ast.parse(path.read_text())) for path in _production_paths()]
    return tuple(frozenset().union(*kind) for kind in zip(*stores, strict=True))


def _subclasses(cls):
    """Return the production subclasses of *cls* at every depth."""
    found = []
    for subclass in cls.__subclasses__():
        if not subclass.__module__.startswith(f"{PACKAGE.name}.tests"):
            found += [subclass, *_subclasses(subclass)]
    return found


def _parameter(definition, call, read, offset):
    """Return the parameter of *definition* that *read* binds to in *call*, or None for ``*args``, ``**kwargs`` or none."""
    arguments = definition.args
    if read in call.args:
        index = call.args.index(read)
        positional = [*arguments.posonlyargs, *arguments.args][offset:]
        if any(isinstance(argument, ast.Starred) for argument in call.args[:index]) or index >= len(positional):
            return None
        return positional[index].arg
    keyword = next(keyword for keyword in call.keywords if keyword.value is read)
    return keyword.arg if keyword.arg in {arg.arg for arg in (*arguments.args, *arguments.kwonlyargs)} else None


class _CaughtErrorReadScan(ast.NodeVisitor):
    """Collect each read of a caught error that can hold a ValidationError or database error and is not safe."""

    def __init__(self, namespace, tree):
        self.namespace = namespace
        self.file = namespace["__file__"]
        self.trees = {self.file: tree}
        self.parents = _parents(tree)
        # An attribute that code stores, or a name that code makes global, can replace what the module defines.
        self.stores = tuple(known | found for known, found in zip(_production_stores(), _stores(tree), strict=True))
        self.candidates = CURRENT_NAMES | _aliases(tree)
        self.imports = []
        self.nodes = []
        self.reads = []
        self.followed = {}

    def _visit_scope(self, node):
        self.nodes.append(node)
        self.generic_visit(node)
        self.nodes.pop()

    def _visit_function(self, node):
        self.imports.append(_own_imports(node))
        self._visit_scope(node)
        self.imports.pop()

    def _namespace_for(self, expression):
        """Return the module namespace plus the imports of the enclosing functions that *expression* names."""
        return _namespace_with_imports(expression, self.namespace, self.imports)

    def _resolver(self, read, local, namespace=None, parents=None):
        parents = parents or self.parents
        imports = _import_stack(read, parents)
        return functools.partial(
            _callable, local=local, namespace=namespace or self.namespace, imports=imports, stores=self.stores
        )

    visit_ClassDef = _visit_scope
    visit_FunctionDef = visit_AsyncFunctionDef = _visit_function

    def _visit_reference(self, node):
        if (read := _current_exception_read(node, self.parents, self.candidates, self._namespace_for)) is not None:
            local, made, declared = _enclosing(read, self.parents)
            if (sink := _sink(read, self.parents, False, self._resolver(read, local))) is not None:
                self.reads.append((self.file, self._scope(), _store(read, self.parents, made, declared) or sink))
        self.generic_visit(node)

    visit_Name = visit_Attribute = _visit_reference

    def visit_Try(self, node):
        for handler in node.handlers:
            if handler.type is None:
                self._scan_handler(handler)
                continue
            try:
                local = _enclosing(handler, self.parents)[0]
                classes = _resolve(handler.type, self._namespace_for(handler.type), local)
            except (NameError, AttributeError):
                self.reads.append((self.file, self._scope(), f"except {ast.unparse(handler.type)}: cannot resolve"))
                continue
            if _can_catch_risky(classes):
                self._scan_handler(handler)
            else:
                self._scan_chain_reads(handler)
        self.generic_visit(node)

    visit_TryStar = visit_Try

    def _scope(self):
        return ".".join(node.name for node in self.nodes)

    def _scan_chain_reads(self, handler):
        """Record each read of the error that another error chains, in a handler that cannot catch a risky error."""
        reads = [node for node in ast.walk(handler) if _is_read_of(node, handler.name)]
        for node in sorted(reads, key=lambda read: (read.lineno, read.col_offset)):
            if isinstance(parent := self.parents[node], ast.Attribute) and parent.attr in CHAIN:
                self.reads.append((self.file, self._scope(), f".{parent.attr}"))
            elif (call := _call_of(node, self.parents)) is not None:
                local = _enclosing(node, self.parents)[0]
                sinks = self._follow(call, node, local, self._resolver(node, local), self._chain_sinks)
                self.reads += [(self.file, self._scope(), each) for each in sinks or []]

    def _closure_reads(self, handler):
        """Return the reads of the handler's name in functions that the enclosing function defines outside handlers."""
        scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        function = next((node for node in _ancestors(handler, self.parents) if isinstance(node, scopes)), None)
        handlers = [node for node in ast.walk(function or handler) if isinstance(node, ast.ExceptHandler)]
        # A handler with the same name scans the functions that it defines.
        inner = {node for each in handlers if each.name == handler.name for node in ast.walk(each)}
        nested = (
            [node for node in ast.walk(function) if isinstance(node, scopes) and node not in inner] if function else []
        )
        reads = {
            read
            for scope in nested[1:]
            if handler.name not in _parameters(scope.args) | _scope_bindings(scope)
            for read in ast.walk(scope)
            if _is_read_of(read, handler.name)
        }
        return sorted(reads, key=lambda read: (read.lineno, read.col_offset))

    def _scan_handler(self, handler):
        for node in [*ast.walk(handler), *self._closure_reads(handler)]:
            if not _is_read_of(node, handler.name):
                continue
            local, made, declared = _enclosing(node, self.parents)
            resolve = self._resolver(node, local)
            if (sink := _sink(node, self.parents, True, resolve)) is None:
                continue
            call = _call_of(node, self.parents)
            if (
                call is not None
                and (sinks := self._follow(call, node, local, resolve, self._parameter_sinks)) is not None
            ):
                self.reads += [(self.file, self._scope(), each) for each in sinks]
                continue
            self.reads.append((self.file, self._scope(), _store(node, self.parents, made, declared) or sink))

    def _follow(self, call, read, local, resolve, sinks_of):
        """Return ``<function>: <sink>`` for each sink that *sinks_of* finds in each package function that *call* reaches."""
        if (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in local
        ):
            targets = self._methods(read, call.func.value.id, call.func.attr)
        else:
            targets = [(resolve(call.func), 0)]
        sinks = []
        for function, offset in targets or [(None, 0)]:
            definition = self._definition(function)
            parameter = definition and _parameter(definition, call, read, offset)
            if parameter is None:
                return None
            sinks += [f"{function.__qualname__}: {sink}" for sink in sinks_of(function, definition, parameter)]
        return sinks

    def _methods(self, read, receiver, name):
        """Return each method that ``<receiver>.<name>`` can reach in the enclosing class and its subclasses."""
        scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        method = next((node for node in _ancestors(read, self.parents) if isinstance(node, scopes)), None)
        if method is not self.nodes[-1] or not all(isinstance(node, ast.ClassDef) for node in self.nodes[:-1]):
            return None
        positional = [*method.args.posonlyargs, *method.args.args]
        kinds = [ast.unparse(decorator) for decorator in method.decorator_list]
        rebound = receiver in set().union(*_statement_bindings(method.body), _own_imports(method))
        if not positional or positional[0].arg != receiver or kinds not in ([], ["classmethod"]) or rebound:
            return None
        if name in self.stores[0]:
            return None
        qualname = self._scope().rpartition(".")[0]
        try:
            cls = eval(qualname, self.namespace)
        except (NameError, AttributeError):
            return None
        if not (
            isinstance(cls, type) and cls.__qualname__ == qualname and cls.__module__ == self.namespace["__name__"]
        ):
            return None
        targets = []
        for each in (cls, *_subclasses(cls)):
            hooks = inspect.getattr_static(each, "__getattr__", None), inspect.getattr_static(each, "__getattribute__")
            if hooks != (None, object.__getattribute__):
                return None
            attribute = inspect.getattr_static(each, name, None)
            if isinstance(attribute, (staticmethod, classmethod)):
                targets.append((attribute.__func__, int(isinstance(attribute, classmethod))))
            else:
                targets.append((attribute, int(kinds == [])))
        return list(dict.fromkeys(targets))

    def _definition(self, function):
        """Return the ``def`` of *function* when it is a package function, else None."""
        if not _package_function(function, self.file):
            return None
        file = function.__code__.co_filename
        if file not in self.trees:
            self.trees[file] = ast.parse(Path(file).read_text())
        return _definitions(self.trees[file]).get((function.__qualname__, function.__code__.co_firstlineno))

    def _chain_sinks(self, function, definition, parameter):
        """Return ``.<attribute>`` for each read of *parameter* in *definition* that reads a chained error."""
        parents = _parents(definition)
        reads = [node for statement in definition.body for node in ast.walk(statement) if _is_read_of(node, parameter)]
        return [
            f".{parents[read].attr}"
            for read in reads
            if isinstance(parents[read], ast.Attribute) and parents[read].attr in CHAIN
        ]

    def _parameter_sinks(self, function, definition, parameter):
        """Return the sink of each unsafe read of *parameter*, or of the current exception, in *definition*."""
        key = (function.__code__, parameter)
        if key not in self.followed:
            parents = _parents(definition)
            candidates = CURRENT_NAMES | _aliases(self.trees[function.__code__.co_filename])

            def namespace_for(expression):
                return _namespace_with_imports(expression, function.__globals__, _import_stack(expression, parents))

            reads = [
                (node, True)
                for statement in definition.body
                for node in ast.walk(statement)
                if isinstance(node, ast.Name) and node.id == parameter and isinstance(node.ctx, ast.Load)
            ]
            reads += [
                (read, False)
                for node in ast.walk(definition)
                if (read := _current_exception_read(node, parents, candidates, namespace_for)) is not None
            ]
            sinks = []
            for read, bound in reads:
                local, made, declared = _enclosing(read, parents)
                resolve = self._resolver(read, local, function.__globals__, parents)
                if (sink := _sink(read, parents, bound, resolve)) is not None:
                    sinks.append(_store(read, parents, made, declared) or sink)
            self.followed[key] = sinks
        return self.followed[key]


def caught_error_reads(source, namespace):
    """Return ``(file, function, sink)`` for each unsafe read of a caught error in *source*, with its module's *namespace*."""
    tree = ast.parse(source)
    scan = _CaughtErrorReadScan(namespace, tree)
    scan.visit(tree)
    return scan.reads


def _production_files():
    """Return the source and namespace of each production module; each is imported first, so each subclass exists."""
    paths = _production_paths()
    modules = [
        importlib.import_module(
            ".".join((PACKAGE.name, *path.relative_to(PACKAGE).with_suffix("").parts)).removesuffix(".__init__")
        )
        for path in paths
    ]
    return [(path.read_text(), vars(module)) for path, module in zip(paths, modules, strict=True)]


def _production_reads():
    return {
        (Path(file).relative_to(PACKAGE).as_posix(), function, sink)
        for source, namespace in _production_files()
        for file, function, sink in caught_error_reads(source, namespace)
    }


def test_a_caught_error_reaches_a_page_only_through_exception_text_for():
    found = _production_reads()
    allowed = {entry[:3] for entry in ALLOWED}

    assert found - allowed == set(), "a caught error's text can reach a page; use exception_text_for"
    assert allowed - found == set(), "an allowlist entry matches no read; remove it"


def test_the_scan_reads_the_production_handlers_and_the_helpers_that_they_call():
    """The scan finds allowlisted reads in a handler and in a helper, so it reads the files that it must read."""
    reads = _production_reads()

    assert ("models.py", "InterfaceTypeMapping.clean", ".message_dict") in reads
    assert ("interface_diff.py", "type_change_refusal", "_first_refusal: .messages") in reads


CASE = "<guard case>"
# The reads of ``validation_error_detail`` that a scan finds when a handler gives it the error outside a log call.
VALIDATION_DETAIL = [".message_dict", "str", ".messages"]


def _scan(source):
    """Run *source* in a namespace with the classes that the cases name, then scan it."""
    from django import db, forms
    from django.db import DatabaseError, DataError, IntegrityError, OperationalError
    from django.db.models import ProtectedError
    from requests import exceptions as request_errors

    namespace = {
        **{cls.__name__: cls for cls in (ValidationError, AbortRequest, DatabaseError, DataError, IntegrityError)},
        **{cls.__name__: cls for cls in (OperationalError, ProtectedError)},
        "db": db,
        "forms": forms,
        "psycopg": psycopg,
        "sys": sys,
        "traceback": traceback,
        "RequestException": request_errors.RequestException,
        "_WriteRefused": type("_WriteRefused", (RuntimeError,), {}),
        "exception_text_for": exception_text_for,
        "classify_conflict": classify_conflict,
        "database_error_sqlstate": database_error_sqlstate,
        "validation_error_detail": validation_error_detail,
        "logger": logging.getLogger("guard_case"),
        "__name__": "guard_case",
        "__file__": CASE,
    }
    exec(compile(source, CASE, "exec"), namespace)
    return [(function, sink) for *_file, function, sink in caught_error_reads(source, namespace)]


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
        ("detail = validation_error_detail(exc)", [f"validation_error_detail: {sink}" for sink in VALIDATION_DETAIL]),
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
        ("keyed = hasattr(exc, 'error_dict')", []),
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
        ("return exception_text_for(traceback.format_exc(), Device, request.user)", [RULE]),
        ("traceback.print_exc(file=buffer)", ["Expr"]),
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


def test_the_scan_reads_a_handler_for_a_validation_error_that_the_plugin_defines():
    source = textwrap.dedent(
        """
        class TagNameTaken(ValidationError):
            pass

        def view():
            try:
                save()
            except TagNameTaken as exc:
                form.add_error(None, exc.__cause__)
        """
    )

    assert _scan(source) == [("view", ".__cause__")]


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


def test_the_scan_finds_an_imported_reader_of_the_current_exception():
    source = textwrap.dedent(
        """
        from traceback import format_exc as trace_text

        def view():
            from sys import exception as current

            try:
                save()
            except Exception:
                messages.error(request, trace_text())
            except BaseException:
                return current()
        """
    )

    assert _scan(source) == [("view", "messages.error"), ("view", "Return")]


def test_an_import_in_a_nested_function_does_not_change_the_outer_handler():
    source = textwrap.dedent(
        """
        Caught = Exception

        def view():
            def later():
                from builtins import ValueError as Caught

            try:
                save()
            except Caught as exc:
                return str(exc)
        """
    )

    assert _scan(source) == [("view", "str")]


def test_an_except_clause_that_names_a_twice_imported_name_cannot_resolve():
    source = textwrap.dedent(
        """
        def view():
            from builtins import ValueError as Caught
            from django.db import IntegrityError as Caught

            try:
                save()
            except Caught as exc:
                return str(exc)
        """
    )

    assert _scan(source) == [("view", "except Caught: cannot resolve")]


def test_an_except_clause_resolves_a_name_that_two_branches_import_from_one_place():
    source = textwrap.dedent(
        """
        def view(flag):
            if flag:
                from django.db import IntegrityError as Caught
            else:
                from django.db import IntegrityError as Caught
            try:
                save()
            except Caught as exc:
                return str(exc)
        """
    )

    assert _scan(source) == [("view", "str")]


def test_an_except_clause_that_names_a_local_variable_cannot_resolve():
    source = textwrap.dedent(
        """
        Caught = ValueError

        def view():
            Caught = ValidationError
            try:
                save()
            except Caught as exc:
                return str(exc)
        """
    )

    assert _scan(source) == [("view", "except Caught: cannot resolve")]


def test_the_scan_imports_only_the_names_that_an_except_clause_uses():
    source = textwrap.dedent(
        """
        def view():
            if False:
                from dcim.models.mixins import NotInThisNetBox
            try:
                save()
            except IntegrityError as exc:
                return str(exc)
        """
    )

    assert _scan(source) == [("view", "str")]


HELPERS = textwrap.dedent(
    """
    import functools

    def report(request, error):
        messages.error(request, str(error))

    def describe(error, user, *, model=None):
        if isinstance(error, IntegrityError) and hasattr(error, "__cause__"):
            return "The name is taken."
        logger.warning("Write failed: %s", error)
        return exception_text_for(error, model, user)

    def collect(*errors, **named):
        return errors, named

    def relay(error):
        return describe(error, None)

    def keep(error, output):
        output.append(error)

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
    """
)


@pytest.mark.parametrize(
    "body, expected",
    [
        ("report(request, exc)", ["report: str"]),
        ("report(request, error=exc)", ["report: str"]),
        ("detail = describe(exc, request.user)", []),
        ("detail = describe(user=request.user, error=exc)", []),
        ("detail = describe(request.user, exc)", ["describe: exception_text_for"]),
        ("detail = describe(*args, exc)", ["describe"]),
        ("collect(exc)", ["collect"]),
        ("collect(error=exc)", ["collect"]),
        ("detail = relay(exc)", ["relay: describe"]),
        ("keep(exc, [])", ["keep: store output"]),
        ("detail = shadowed(exc)", ["shadowed: exception_text_for"]),
        ("detail = decorated(exc)", ["decorated"]),
        ("detail = functools.partial(describe, exc)", ["functools.partial"]),
    ],
)
def test_the_scan_reads_the_parameter_of_a_package_function_that_takes_the_caught_error(body, expected):
    source = f"{HELPERS}\ndef view():\n    try:\n        save()\n    except ValidationError as exc:\n        {body}\n"

    assert _scan(source) == [("view", sink) for sink in expected]


@pytest.mark.parametrize(
    "view",
    [
        "def view(describe):\n    try:\n        save()\n    except ValidationError as exc:\n        return describe(exc, None)",
        "def view():\n    try:\n        save()\n    except ValidationError as exc:\n        class Output:\n"
        "            describe = str\n            text = describe(exc, None)",
        "def view(fns):\n    try:\n        save()\n    except ValidationError as exc:\n"
        "        return [describe(exc, None) for describe in fns]",
        "def view():\n    from builtins import str as describe\n    from builtins import repr as describe\n"
        "    try:\n        save()\n    except ValidationError as exc:\n        return describe(exc, None)",
    ],
)
def test_the_scan_does_not_follow_a_name_that_the_scope_binds_or_imports_twice(view):
    assert _scan(f"{HELPERS}\n{view}\n") == [("view", "describe")]


def test_the_scan_does_not_trust_a_safe_name_that_the_function_binds():
    source = "def view(hasattr=str):\n    try:\n        save()\n    except ValidationError as exc:\n        return hasattr(exc)\n"

    assert _scan(source) == [("view", "hasattr")]


def test_the_scan_reads_the_current_exception_in_any_function():
    source = textwrap.dedent(
        """
        def trace(error):
            return traceback.format_exc()

        def dump(error):
            return str(locals())

        def view():
            try:
                save()
            except ValidationError as exc:
                return trace(exc), dump(exc)
        """
    )

    assert _scan(source) == [("trace", "Return"), ("dump", "str"), ("view", "trace: Return"), ("view", "dump: str")]


@pytest.mark.parametrize(
    "helper, sink",
    [
        ("def failure(error):\n    return traceback.format_exc()", "Return"),
        ("def failure(error):\n    format_error = traceback.format_exc\n    return format_error()", "Assign"),
        ("def failure(error, format_error=traceback.format_exc):\n    return format_error()", "arguments"),
    ],
)
def test_a_helper_that_reads_the_current_exception_is_a_read_in_each_caller(helper, sink):
    source = f"{helper}\n\ndef view():\n    try:\n        save()\n    except ValidationError as exc:\n        return failure(exc)\n"

    assert _scan(source) == [("failure", sink), ("view", f"failure: {sink}")]


def test_the_scan_does_not_trust_a_name_that_a_class_body_imports_or_that_code_stores():
    source = textwrap.dedent(
        """
        import types

        formatters = types.SimpleNamespace(exception_text_for=exception_text_for)

        def failure(error):
            class Output:
                from builtins import str as exception_text_for
                text = exception_text_for(error)
            return Output.text

        def view():
            formatters.exception_text_for = str
            try:
                save()
            except ValidationError as exc:
                return failure(exc), formatters.exception_text_for(exc)
        """
    )

    assert _scan(source) == [("view", "failure: exception_text_for"), ("view", "formatters.exception_text_for")]


def test_the_scan_reads_the_caught_error_in_a_closure_that_the_function_defines_before_the_handler():
    source = textwrap.dedent(
        """
        def view():
            def detail():
                return str(exc)

            try:
                save()
            except ValidationError as exc:
                return detail()
        """
    )

    assert _scan(source) == [("view", "str")]


def test_any_handler_that_reads_the_cause_or_context_of_its_error_reads_a_chained_error():
    source = textwrap.dedent(
        """
        def view():
            try:
                try:
                    save()
                except ValidationError as exc:
                    raise ValueError("failed") from exc
            except ValueError as failure:
                return str(failure.__cause__), failure.__context__
        """
    )

    assert _scan(source) == [("view", ".__cause__"), ("view", ".__context__")]


def test_a_helper_that_reads_the_cause_of_an_error_from_any_handler_is_a_read():
    source = textwrap.dedent(
        """
        def failure_detail(error):
            return str(error.__cause__ or error)

        def view():
            try:
                save()
            except ValueError as failure:
                return failure_detail(failure)
        """
    )

    assert _scan(source) == [("view", "failure_detail: .__cause__")]


def test_the_scan_reads_a_handler_for_an_exception_group_because_it_can_hold_a_risky_error():
    source = (
        "def view():\n    try:\n        save()\n    except ExceptionGroup as failures:\n        return repr(failures)\n"
    )

    assert _scan(source) == [("view", "repr")]


def test_the_scan_trusts_only_the_module_logger_named_logger():
    source = textwrap.dedent(
        """
        import logging

        job_logger = logging.getLogger("guard_case.job")

        def view():
            try:
                save()
            except ValidationError as exc:
                job_logger.error("failed: %s", exc)
                logger.error("failed: %s", exc)
        """
    )

    assert _scan(source) == [("view", "job_logger.error")]


def test_vars_of_an_object_does_not_read_the_frame():
    assert _scan("def view(row):\n    return vars(row), vars()\n") == [("view", "Tuple")]


def test_a_helper_that_walks_the_stack_is_a_read_in_each_caller():
    source = textwrap.dedent(
        """
        def detail(error):
            stack = traceback.StackSummary.extract(traceback.walk_stack(None), capture_locals=True)
            return "".join(stack.format())

        def view():
            try:
                save()
            except ValidationError as exc:
                return detail(exc)
        """
    )

    assert _scan(source) == [
        ("detail", ".extract"),
        ("detail", "traceback.StackSummary.extract"),
        ("view", "detail: .extract"),
        ("view", "detail: traceback.StackSummary.extract"),
    ]


def test_the_scan_does_not_trust_a_logger_method_or_a_chain_that_the_function_replaces():
    source = textwrap.dedent(
        """
        import logging
        import types

        log = logging.Logger("guard")
        formatters = types.SimpleNamespace(active=types.SimpleNamespace(exception_text_for=exception_text_for))

        def view():
            log.error = str
            formatters.active = types.SimpleNamespace(exception_text_for=str)
            try:
                save()
            except ValidationError as exc:
                return log.error(exc), formatters.active.exception_text_for(exc)
        """
    )

    assert _scan(source) == [("view", "log.error"), ("view", "formatters.active.exception_text_for")]


def test_the_scan_reads_each_method_that_a_call_on_self_can_reach():
    source = textwrap.dedent(
        """
        class Base:
            def post(self):
                try:
                    save()
                except ValidationError as exc:
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
                    return cls.text(exc)

        class Child(Base):
            def failure(self, error):
                return error.messages
        """
    )

    assert _scan(source) == [("Base.post", "Child.failure: .messages")]


@pytest.mark.parametrize(
    "member, rebinding",
    [
        ("def __init__(self):\n        self.failure = str", ""),
        ("def __getattr__(self, name):\n        return str", ""),
        ("pass", "match save():\n            case self:\n                pass"),
        ("pass", "try:\n            save()\n        except KeyError as self:\n            pass"),
        ("pass", "import builtins as self"),
    ],
)
def test_the_scan_does_not_follow_a_method_that_the_instance_or_the_receiver_can_replace(member, rebinding):
    source = textwrap.dedent(
        """
        class View:
            {member}

            def failure(self, error):
                return exception_text_for(error, Device, None)

            def post(self):
                {rebinding}
                try:
                    save()
                except ValidationError as exc:
                    return self.failure(exc)
        """
    ).format(member=member, rebinding=rebinding)

    assert _scan(source) == [("View.post", "self.failure")]


def test_the_scan_does_not_follow_a_class_method_that_the_class_replaces():
    source = textwrap.dedent(
        """
        class View:
            @staticmethod
            def text(error):
                return exception_text_for(error, Device, None)

            @classmethod
            def post(cls):
                cls.text = str
                try:
                    save()
                except ValidationError as exc:
                    return cls.text(exc)
        """
    )

    assert _scan(source) == [("View.post", "cls.text")]


def test_the_scan_resolves_a_helper_name_through_the_imports_of_its_nested_function():
    source = textwrap.dedent(
        """
        def helper(error):
            def render():
                from builtins import str as exception_text_for
                return exception_text_for(error)
            return render()

        def view():
            try:
                save()
            except ValidationError as exc:
                return helper(exc)
        """
    )

    assert _scan(source) == [("view", "helper: exception_text_for")]


def test_the_scan_trusts_only_the_log_methods_of_logging():
    source = textwrap.dedent(
        """
        import logging

        class Loud(logging.Logger):
            def error(self, *args):
                return str(args)

        log = logging.Logger("guard")
        log.error = str
        loud = Loud("guard")

        def view():
            try:
                save()
            except ValidationError as exc:
                return log.error(exc), loud.error(exc)
        """
    )

    assert _scan(source) == [("view", "log.error"), ("view", "loud.error")]


def test_the_scan_reads_a_helper_that_another_package_module_defines():
    source = textwrap.dedent(
        """
        from netbox_librenms_plugin.views.sync.modules import _module_write_failure

        def view():
            try:
                save()
            except ValidationError as exc:
                return _module_write_failure(exc, Device, None)
        """
    )

    assert _scan(source) == [("view", "_module_write_failure: str")]


@pytest.mark.parametrize(
    "body, expected",
    [
        ("self.last_error = exc", ["store self.last_error"]),
        ("self.last_error = str(exc)", ["store self.last_error"]),
        ("self.errors[key] = exc.messages", ["store self.errors"]),
        ("self.errors.append(f'failed: {exc}')", ["store self.errors"]),
        ("row.detail, count = repr(exc), 1", ["store row.detail"]),
        ("ERRORS.append(exc)", ["store ERRORS"]),
        ("rows.append(exc)", ["store rows"]),
        ("global LAST_ERROR; LAST_ERROR = str(exc)", ["store LAST_ERROR"]),
        ("errors = []; errors.append(exc)", ["errors.append"]),
        ("self.last_error = exception_text_for(exc, Device, request.user)", []),
    ],
)
def test_the_scan_names_the_place_that_keeps_the_caught_error(body, expected):
    source = f"def view(self, key, row, rows):\n    try:\n        save()\n    except ValidationError as exc:\n        {body}\n"

    assert [sink for _function, sink in _scan(source)] == expected
