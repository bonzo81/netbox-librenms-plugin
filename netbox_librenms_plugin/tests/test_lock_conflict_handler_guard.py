"""
A broad exception handler in a write path lets a lock conflict through.

A lock conflict must reach ``run_transaction`` (which runs the attempt once more) or the middleware
(which gives the one "try again" answer). A broad handler that catches it instead can commit a
part of the work, report success for rolled-back work, or show PostgreSQL's text. This scan reads
each handler in a production module that catches ``Exception``, ``BaseException``,
``DatabaseError``, ``OperationalError``, ``ValidationError`` or ``AbortRequest`` (NetBox raises the
last two from its own deadlocks), or that has no class. It checks the handler when it is in a write
path: its ``try`` body writes, or the ``try`` statement is inside ``transaction.atomic`` or inside
a function that ``run_transaction`` runs. A ``try`` body writes when it opens
``transaction.atomic``, calls ``run_transaction``, ``update_existing_row``, a ``save()`` or a
``delete()``, or calls a package function whose own body does one of these. A ``run_transaction``
call raises a conflict only as ``TransactionConflict``, so around it only a catch-all handler is
checked, and only when no earlier handler of the same ``try`` catches ``TransactionConflict``. A
handler passes when its first statement is ``if classify_conflict(<the caught error>): raise``,
when it ends with a bare ``raise`` and has no ``return``, ``continue`` or ``break``, or when an
allowlist entry names it with a reason.

Known gaps: the scan follows a call one level only, and only to a function whose name is defined
once in the package, so a write two calls deep, or behind a shared name such as ``post``, is not
seen. It does not know which model a save writes, so a handler that catches only
``ValidationError`` around a model that is not an ltree model needs an entry. It finds a
``run_transaction`` work function only when the call names it (a name, a lambda call or
``functools.partial``). A handler that raises another error ``from`` the caught one is flagged.
"""

import ast
import textwrap
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
BROAD = frozenset(
    {"Exception", "BaseException", "DatabaseError", "OperationalError", "ValidationError", "AbortRequest"}
)
# A bare ``except:`` has no class; the scan names it "bare".
CATCH_ALL = frozenset({"Exception", "BaseException", "bare"})
WRITE_METHODS = frozenset({"save", "delete"})
WRITE_FUNCTIONS = frozenset({"update_existing_row"})
# Receivers whose save() or delete() writes no model row: the Django cache and the session.
NOT_A_MODEL = frozenset({"cache", "session"})

_NOT_LTREE = "The model it saves is not an ltree model, so NetBox raises a lock conflict as an OperationalError."
_OWN_TRANSACTION = (
    "Each row is its own transaction, which rolled back before the handler runs; the other rows are "
    "independent, and exception_text_for gives a conflict the try-again text."
)
_ROLLED_BACK = (
    "The row is locked before the save, and the save changes no key and no foreign key, so it waits for "
    "no lock; the handler rolls the transaction back."
)
_MAPPING = "The atomic block rolled back before the handler runs, and its answer asks the user to try again."

# (path, function, caught, reason): each entry waives the handlers that catch *caught* in one function
# and in the functions defined inside it.
ALLOWED = [
    (
        "__init__.py",
        "_ensure_librenms_id_custom_field",
        "Exception",
        "It runs from post_migrate, outside any request and any enclosing transaction; it logs the failure.",
    ),
    (
        "api/views.py",
        "sync_job_status",
        "Exception",
        "An API view, which the middleware does not answer: the job save is its own transaction, and "
        "the answer is a JSON error.",
    ),
    (
        "transactions.py",
        "run_transaction",
        "Exception",
        "The runner itself: it runs a conflict once more and re-raises every other error.",
    ),
    ("import_utils/bulk_import.py", "bulk_import_devices_shared", "Exception", _OWN_TRANSACTION),
    ("import_utils/device_operations.py", "import_single_device", "Exception", _OWN_TRANSACTION),
    ("import_utils/vm_operations.py", "bulk_import_vms", "Exception", _OWN_TRANSACTION),
    ("views/sync/device_fields.py", "AssignVCSerialView.post", "Exception", _OWN_TRANSACTION),
    ("views/imports/actions.py", "AddDeviceTypeMappingView.post", "Exception", _MAPPING),
    ("views/imports/actions.py", "AddPlatformMappingView.post", "Exception", _MAPPING),
    *(
        ("views/sync/device_fields.py", function, "Exception", _ROLLED_BACK)
        for function in (
            "RemoveServerMappingView.post",
            "SetPreferredServerView.post",
            "ConvertLegacyLibreNMSIdView.post",
        )
    ),
    (
        "views/sync/device_fields.py",
        "CreateAndAssignPlatformView.post",
        "ValidationError",
        "The platform is a root insert, which takes no tree lock, and a Device is not an ltree model.",
    ),
    *(
        (path, function, caught, _NOT_LTREE)
        for path, function, caught in (
            ("views/imports/actions.py", "_save_device", "ValidationError"),
            ("views/imports/actions.py", "CreatePlatformFromImportView._create_platform_attempt", "ValidationError"),
            ("views/imports/actions.py", "AddAsOOBView._resolve_oob_interface", "DataError, ValidationError"),
            ("views/settings_views.py", "LibreNMSSettingsView.post", "ValidationError"),
            ("views/sync/cables.py", "CableRemoteCreateView._create_remote_interface", "ValidationError"),
            ("views/sync/device_fields.py", "UpdateDeviceNameView.post", "IntegrityError, ValidationError"),
            ("views/sync/device_fields.py", "UpdateDeviceSerialView.post", "IntegrityError, ValidationError"),
            ("views/sync/device_fields.py", "UpdateDeviceTypeView.post", "IntegrityError, ValidationError"),
            ("views/sync/device_fields.py", "UpdateDevicePlatformView.post", "IntegrityError, ValidationError"),
            ("views/sync/device_fields.py", "AssignVCSerialView.post", "IntegrityError, ValidationError"),
            ("views/sync/device_fields.py", "RemoveServerMappingView.post", "ValidationError"),
            ("views/sync/device_fields.py", "SetPreferredServerView.post", "ValidationError"),
            ("views/sync/device_fields.py", "ConvertLegacyLibreNMSIdView.post", "ValidationError"),
            ("views/sync/interfaces.py", "SyncInterfacesView._apply_relationship_edge", "ValidationError"),
            ("views/sync/interfaces.py", "_BaseRelationshipSyncView._link_attempt", "ValidationError"),
            ("views/sync/ip_addresses.py", "CreateVRFFromIPRowView._create_vrf", "IntegrityError, ValidationError"),
            ("views/sync/migrate.py", "MoveInterfaceToWinnerView.post", "ValidationError"),
            ("views/sync/migrate.py", "MoveInterfaceToWinnerView.post", "IntegrityError, ValidationError"),
            ("views/sync/modules.py", "AddBayTemplateView._map_existing_bay", "IntegrityError, ValidationError"),
        )
    ),
]


def _call_name(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _is_atomic(node):
    target = node.func if isinstance(node, ast.Call) else node
    return ast.unparse(target) in {"transaction.atomic", "atomic"}


def _own_nodes(nodes):
    """Yield *nodes* and their descendants, but not the body of a nested function, class or lambda."""
    stack = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
        )


def _writes(nodes, writers=frozenset()):
    """Return the kinds of write in *nodes*: the direct writes, and the calls of the functions in *writers*."""
    kinds = set()
    for node in _own_nodes(nodes):
        if isinstance(node, ast.With) and any(_is_atomic(item.context_expr) for item in node.items):
            kinds.add("atomic")
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "run_transaction":
            kinds.add("runner")
        elif name in WRITE_METHODS and isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if getattr(receiver, "attr", getattr(receiver, "id", None)) not in NOT_A_MODEL:
                kinds.add(name)
        elif name in WRITE_FUNCTIONS or name in writers:
            kinds.add(f"{name}()")
    return kinds


def direct_writers(trees):
    """Return the names, each defined once in *trees*, of the functions whose own body writes."""
    definitions = {}
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definitions.setdefault(node.name, []).append(node)
    return frozenset(
        name
        for name, (node, *others) in definitions.items()
        if not others and (any(_is_atomic(decorator) for decorator in node.decorator_list) or _writes(node.body))
    )


def _work_names(tree):
    """Return the names of the functions that a ``run_transaction`` call in *tree* runs."""
    names = set()
    for call in (
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _call_name(node) == "run_transaction"
    ):
        for argument in call.args:
            if isinstance(argument, (ast.Name, ast.Attribute)):
                names.add(argument.id if isinstance(argument, ast.Name) else argument.attr)
            for inner in (node for node in ast.walk(argument) if isinstance(node, ast.Call)):
                target = inner.args[0] if _call_name(inner) == "partial" and inner.args else inner.func
                names.add(getattr(target, "attr", getattr(target, "id", None)))
    return names - {None}


def _caught_names(handler, aliases):
    if handler.type is None:
        return {"bare"}
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {
        aliases.get(name.id, name.id) if isinstance(name, ast.Name) else getattr(name, "attr", None) for name in names
    }


def _passes_conflicts_on(handler):
    """Return whether *handler* re-raises every lock conflict it catches."""
    first, last = handler.body[0], handler.body[-1]
    leaves = any(isinstance(node, (ast.Return, ast.Continue, ast.Break)) for node in _own_nodes(handler.body))
    if isinstance(last, ast.Raise) and last.exc is None and not leaves:
        return True
    return (
        isinstance(first, ast.If)
        and isinstance(first.test, ast.Call)
        and _call_name(first.test) == "classify_conflict"
        and [ast.unparse(argument) for argument in first.test.args] == [handler.name]
        and len(first.body) == 1
        and isinstance(first.body[0], ast.Raise)
        and first.body[0].exc is None
    )


class _HandlerScan(ast.NodeVisitor):
    """Collect each broad handler in a write path that can keep a lock conflict."""

    def __init__(self, aliases, writers, work_names):
        self.aliases = aliases
        self.writers = writers
        self.work_names = work_names
        self.scope = []
        self.in_write = [False]
        self.functions = set()
        self.found = []

    def _visit_scope(self, node, in_write):
        self.scope.append(node.name)
        self.in_write.append(in_write)
        self.generic_visit(node)
        self.in_write.pop()
        self.scope.pop()

    def visit_ClassDef(self, node):
        self._visit_scope(node, False)

    def visit_FunctionDef(self, node):
        self.functions.add(".".join([*self.scope, node.name]))
        in_write = self.in_write[-1] or node.name in self.work_names
        self._visit_scope(node, in_write or any(_is_atomic(decorator) for decorator in node.decorator_list))

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_With(self, node):
        for item in node.items:
            self.visit(item)
        self.in_write.append(self.in_write[-1] or any(_is_atomic(item.context_expr) for item in node.items))
        for statement in node.body:
            self.visit(statement)
        self.in_write.pop()

    visit_AsyncWith = visit_With

    def visit_Try(self, node):
        in_write = self.in_write[-1]
        writes = _writes(node.body, self.writers)
        runner_only = writes == {"runner"} and not in_write
        conflict_caught = False
        for handler in node.handlers:
            caught = _caught_names(handler, self.aliases)
            exposed = not runner_only or (caught & CATCH_ALL and not conflict_caught)
            if caught & (BROAD | {"bare"}) and (in_write or writes) and exposed and not _passes_conflicts_on(handler):
                self.found.append((".".join(self.scope), ", ".join(sorted(caught))))
            conflict_caught = conflict_caught or "TransactionConflict" in caught
        self.generic_visit(node)

    visit_TryStar = visit_Try


def _aliases(tree):
    return {
        name.asname: name.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for name in node.names
        if name.asname
    }


def scan(tree, writers=frozenset()):
    """Return the scan of one module: ``found`` holds ``(function, caught)`` for each handler that keeps a conflict."""
    handler_scan = _HandlerScan(_aliases(tree), writers, _work_names(tree))
    handler_scan.visit(tree)
    return handler_scan


def handlers_that_keep_a_conflict(source):
    """Return ``(function, caught)`` for each handler in *source* that can keep a lock conflict."""
    tree = ast.parse(textwrap.dedent(source))
    return scan(tree, direct_writers([tree])).found


def not_allowed(found, allowed):
    """Return the ``(path, function, caught)`` handlers in *found* that no entry of *allowed* waives."""
    return {
        (path, function, caught)
        for path, function, caught in found
        if not any(
            (path, caught) == (entry_path, entry_caught) and f"{function}.".startswith(f"{entry_function}.")
            for entry_path, entry_function, entry_caught, _reason in allowed
        )
    }


def _production_modules():
    trees = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] not in {"tests", "migrations"}:
            trees[relative.as_posix()] = ast.parse(path.read_text())
    writers = direct_writers(trees.values())
    return {relative: scan(tree, writers) for relative, tree in trees.items()}


def test_no_broad_handler_in_a_write_path_keeps_a_lock_conflict():
    found = {
        (relative, function, caught)
        for relative, module in _production_modules().items()
        for function, caught in module.found
    }

    assert not_allowed(found, ALLOWED) == set(), (
        "a broad handler can keep a lock conflict: start it with `if classify_conflict(exc): raise`"
    )


def test_each_allowlist_entry_names_a_function_that_exists():
    """An entry for a handler that now passes conflicts on stays valid; an entry for a gone function does not."""
    functions = {
        (relative, function) for relative, module in _production_modules().items() for function in module.functions
    }

    assert {entry[:2] for entry in ALLOWED} - functions == set()


def test_the_scan_reads_the_production_handlers():
    """The scan finds an allowlisted handler, so it reads the files that it must read."""
    found = {(relative, function) for relative, module in _production_modules().items() for function, _ in module.found}

    assert ("transactions.py", "run_transaction") in found


@pytest.mark.parametrize(
    "source",
    [
        # A per-row savepoint that reports every error as a failed row.
        """
        def sync(rows):
            for row in rows:
                try:
                    with transaction.atomic():
                        row.save()
                except Exception as exc:
                    failed.append(str(exc))
        """,
        # A catch without a savepoint inside the caller's transaction.
        """
        def normalize(interface):
            with transaction.atomic():
                try:
                    interface.delete()
                except Exception:
                    skipped += 1
        """,
        # A tree save, whose deadlock NetBox raises as a ValidationError.
        """
        def add_bay(bay):
            try:
                bay.save()
            except (ValidationError, IntegrityError) as exc:
                return str(exc)
        """,
        # A bare except and a DatabaseError around a helper that opens its own transaction.
        """
        @transaction.atomic
        def bind(interface):
            interface.save()

        def install(interface):
            try:
                bind(interface)
            except:
                pass
            try:
                bind(interface)
            except DatabaseError:
                pass
        """,
        # A handler in a function that run_transaction runs, around a read that can wait for a lock.
        """
        class View:
            def post(self):
                return run_transaction(lambda: self._attempt())

            def _attempt(self):
                try:
                    Device.objects.select_for_update().get(pk=1)
                except OperationalError:
                    return None
        """,
        # A catch-all around run_transaction keeps TransactionConflict.
        """
        def post(work):
            try:
                return run_transaction(work)
            except Exception:
                return None
        """,
        # classify_conflict that reports the conflict instead of raising it.
        """
        def save(obj):
            try:
                obj.save()
            except AbortRequest as exc:
                if classify_conflict(exc):
                    return "busy"
                raise
        """,
    ],
    ids=["row-savepoint", "no-savepoint", "tree-save", "helper-writer", "runner-work", "runner-catch-all", "report"],
)
def test_the_scan_flags_each_handler_that_keeps_a_conflict(source):
    assert len(handlers_that_keep_a_conflict(source)) >= 1


@pytest.mark.parametrize(
    "source",
    [
        # The shape that every fixed site uses.
        """
        def sync(row):
            try:
                with transaction.atomic():
                    row.save()
            except Exception as exc:
                if classify_conflict(exc):
                    raise
                failed.append(row)
        """,
        # A handler that ends with a bare raise.
        """
        def create(vc):
            try:
                with transaction.atomic():
                    vc.save()
            except Exception:
                vc.name = "old"
                raise
        """,
        # Around run_transaction, only a catch-all handler can meet a conflict, and only TransactionConflict.
        """
        def post(work):
            try:
                return run_transaction(work)
            except (ValidationError, IntegrityError):
                return "refused"
        """,
        """
        def rows(items, work):
            for item in items:
                try:
                    run_transaction(work)
                except TransactionConflict:
                    busy.append(item)
                except Exception:
                    failed.append(item)
        """,
        # No write in the try body and no enclosing transaction.
        """
        def read(api):
            try:
                return api.get_device_info(1)
            except Exception:
                return None
        """,
        # The cache and the session are not model rows.
        """
        def clear(request):
            try:
                cache.delete("key")
                request.session.save()
            except Exception:
                pass
        """,
        # A narrow handler inside a transaction.
        """
        def claim(port):
            with transaction.atomic():
                try:
                    port.save()
                except IntegrityError:
                    return None
        """,
    ],
    ids=["classify-raise", "bare-raise", "runner-narrow", "runner-after-conflict", "no-write", "cache", "narrow"],
)
def test_the_scan_passes_each_handler_that_passes_conflicts_on(source):
    assert handlers_that_keep_a_conflict(source) == []


def test_an_entry_waives_its_function_and_the_functions_inside_it_only():
    entry = [("views/a.py", "_save", "ValidationError", "reason")]
    found = {
        ("views/a.py", "_save", "ValidationError"),
        ("views/a.py", "_save.write", "ValidationError"),
        ("views/a.py", "_save_all", "ValidationError"),
        ("views/a.py", "_save", "Exception"),
    }

    assert not_allowed(found, entry) == {
        ("views/a.py", "_save_all", "ValidationError"),
        ("views/a.py", "_save", "Exception"),
    }


def test_the_scan_names_the_function_and_the_caught_classes():
    source = """
        from django.core.exceptions import ValidationError as DjangoValidationError

        class View:
            def post(self, obj):
                try:
                    obj.save()
                except (IntegrityError, DjangoValidationError):
                    return None
        """

    assert handlers_that_keep_a_conflict(source) == [("View.post", "IntegrityError, ValidationError")]
