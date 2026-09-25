"""
Every partial save in the plugin keeps the computed columns of NetBox correct.

Django runs the ``pre_save()`` of a field only for the fields in ``update_fields``. So ``last_updated``
(``auto_now``) does not move, and ``_name`` (the natural order of an interface name) is stale when
``name`` is saved without it. NetBox saves a rename with ``["name", "_name", "last_updated"]``.
"""

import ast
import textwrap
from collections import Counter
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
NO_LAST_UPDATED = "no last_updated"
NAME_WITHOUT_NATURAL_ORDER = "name without _name"
NOT_A_LITERAL = "not a literal"

# (path, function, receiver, violation, reason): each entry waives one violation of one call.
ALLOWED = [
    ("api/views.py", "sync_job_status", "job", NO_LAST_UPDATED, "Job has no last_updated field."),
    ("api/views.py", "sync_job_status", "job", NO_LAST_UPDATED, "Job has no last_updated field."),
    ("jobs.py", "FilterDevicesJob.run", "self.job", NO_LAST_UPDATED, "Job has no last_updated field."),
    ("jobs.py", "ImportDevicesJob._import", "self.job", NO_LAST_UPDATED, "Job has no last_updated field."),
    (
        "forms.py",
        "CableSyncSettingsForm.save",
        "locked_settings",
        NOT_A_LITERAL,
        "LibreNMSSettings has no last_updated or _name field; setting_fields is a literal tuple above.",
    ),
    ("forms.py", "CableSyncSettingsForm.save", "tag", NAME_WITHOUT_NATURAL_ORDER, "Tag has no _name field."),
    (
        "views/sync/vlans.py",
        "SyncVLANsView._apply_confirmed_vlan_change",
        "vlan",
        NAME_WITHOUT_NATURAL_ORDER,
        "VLAN has no _name field.",
    ),
    (
        "import_utils/virtual_chassis.py",
        "create_virtual_chassis_with_members",
        "master_device",
        NOT_A_LITERAL,
        "save_fields lists last_updated, and name only for a rename; Device has no _name field.",
    ),
    (
        "utils.py",
        "set_device_ip_fk",
        "device",
        NOT_A_LITERAL,
        "field is one of DEVICE_IP_FK_FIELDS, which the function checks first.",
    ),
    (
        "views/imports/actions.py",
        "_save_device",
        "device",
        NOT_A_LITERAL,
        "The callers pass Device and VirtualMachine columns, which have no _name; last_updated is added here.",
    ),
    (
        "views/sync/modules.py",
        "_bind_interface_librenms_id",
        "candidate",
        NOT_A_LITERAL,
        "update_fields holds only module and custom_field_data; last_updated is added at the save.",
    ),
    (
        "views/sync/interfaces.py",
        "_apply_interface_relationship",
        "source_iface",
        NOT_A_LITERAL,
        "source_fields holds only the relationship columns and type; last_updated is added at the save.",
    ),
    (
        "models.py",
        "FullCleanOnSaveMixin.save",
        "super()",
        NOT_A_LITERAL,
        "It passes on the arguments of the caller's save call, and the scan checks that call.",
    ),
    (
        "models.py",
        "LibreNMSSettings.save",
        "super()",
        NOT_A_LITERAL,
        "It passes on the arguments of the caller's save call, and the scan checks that call.",
    ),
]


class _PartialSaveScan(ast.NodeVisitor):
    """Collect every ``.save()`` call that can set ``update_fields``, with the function that holds it and its violations."""

    def __init__(self):
        self.scope = []
        self.saves = []

    def _visit_scope(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _visit_scope

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "save":
            for keyword in node.keywords:
                if (violations := _keyword_violations(keyword)) is not None:
                    self.saves.append((".".join(self.scope), ast.unparse(node.func.value), violations))
        self.generic_visit(node)


def _keyword_violations(keyword):
    """Return the violations of one keyword of a ``save()`` call, or None when the keyword sets no ``update_fields``."""
    if keyword.arg is not None:
        return _violations(keyword.value) if keyword.arg == "update_fields" else None
    # A ** unpack can set update_fields; the scan reads only a dict literal whose keys are all literals.
    unpacked = keyword.value
    if not isinstance(unpacked, ast.Dict) or not all(isinstance(key, ast.Constant) for key in unpacked.keys):
        return [NOT_A_LITERAL]
    values = {key.value: value for key, value in zip(unpacked.keys, unpacked.values, strict=True)}
    return _violations(values["update_fields"]) if "update_fields" in values else None


def _violations(value):
    """Return the violations of one ``update_fields`` value: a literal list, tuple or set of strings is checked."""
    elements = getattr(value, "elts", None)
    if not isinstance(value, (ast.List, ast.Tuple, ast.Set)) or not all(
        isinstance(element, ast.Constant) and isinstance(element.value, str) for element in elements
    ):
        return [NOT_A_LITERAL]
    fields = {element.value for element in elements}
    violations = []
    if "last_updated" not in fields:
        violations.append(NO_LAST_UPDATED)
    if "name" in fields and "_name" not in fields:
        violations.append(NAME_WITHOUT_NATURAL_ORDER)
    return violations


def partial_saves(source):
    """Return ``(function, receiver, violations)`` for each ``.save()`` call in *source* that can set ``update_fields``."""
    scan = _PartialSaveScan()
    scan.visit(ast.parse(source))
    return scan.saves


def _production_files():
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] not in {"tests", "migrations"}:
            yield relative.as_posix(), path.read_text()


def test_every_partial_save_moves_last_updated_and_keeps_the_natural_order_of_a_name():
    saves = [
        (relative, function, receiver, violation)
        for relative, source in _production_files()
        for function, receiver, violations in partial_saves(source)
        for violation in violations
    ]
    found, allowed = Counter(saves), Counter(entry[:4] for entry in ALLOWED)

    assert found - allowed == Counter(), "a partial save misses last_updated or _name, or is not a literal"
    assert allowed - found == Counter(), "an allowlist entry matches no partial save; remove it"


def test_the_scan_reads_the_production_partial_saves():
    """The scan finds the saves that the relationship pass makes, so it reads the files that it must read."""
    saves = [
        (relative, function)
        for relative, source in _production_files()
        for function, _receiver, violations in partial_saves(source)
        if not violations
    ]

    assert ("views/sync/interfaces.py", "_promote_lag_aggregate._persist") in saves


@pytest.mark.parametrize(
    "source, expected",
    [
        ("obj.save(update_fields=['type'])", [NO_LAST_UPDATED]),
        ("obj.save(update_fields=['name', 'last_updated'])", [NAME_WITHOUT_NATURAL_ORDER]),
        ("obj.save(update_fields=['name'])", [NO_LAST_UPDATED, NAME_WITHOUT_NATURAL_ORDER]),
        ("obj.save(update_fields=fields)", [NOT_A_LITERAL]),
        ("obj.save(update_fields=[*fields, 'last_updated'])", [NOT_A_LITERAL]),
        ("obj.save(update_fields=[field, 'last_updated'])", [NOT_A_LITERAL]),
        ("obj.save(using='default', update_fields=('type',))", [NO_LAST_UPDATED]),
        ("obj.save(update_fields=['name', '_name', 'last_updated'])", []),
        ("obj.save(update_fields={'type', 'last_updated'})", []),
        ("obj.save(**{'update_fields': ['name']})", [NO_LAST_UPDATED, NAME_WITHOUT_NATURAL_ORDER]),
        ("obj.save(**{'update_fields': ['type', 'last_updated']})", []),
        ("obj.save(**kwargs)", [NOT_A_LITERAL]),
        ("obj.save(**{key: ['type']})", [NOT_A_LITERAL]),
        ("obj.save(**{**defaults, 'update_fields': ['type', 'last_updated']})", [NOT_A_LITERAL]),
    ],
)
def test_the_scan_names_each_violation_of_a_partial_save(source, expected):
    assert partial_saves(source) == [("", "obj", expected)]


def test_the_scan_names_the_function_and_the_receiver_and_ignores_other_calls():
    source = textwrap.dedent(
        """
        class View:
            def post(self):
                def persist():
                    self.row.save(update_fields=["type"])
                self.row.save()
                self.row.save(**{"using": "default"})
                self.row.update(update_fields=["type"])
                save(update_fields=["type"])
        """
    )

    assert partial_saves(source) == [("View.post.persist", "self.row", [NO_LAST_UPDATED])]
