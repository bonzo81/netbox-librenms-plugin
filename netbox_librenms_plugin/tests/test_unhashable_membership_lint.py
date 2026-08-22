"""The unhashable-membership check must flag the real class and stay quiet otherwise.

A lint nobody trusts gets disabled, so the negative cases matter as much as the positive
ones. Each sample is written to a temporary file and scanned exactly as CI scans the package.
"""

import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

from lint_unhashable_membership import check_file, collect_container_names  # noqa: E402


def _scan(tmp_path, source, extra_sources=()):
    """Write *source* to a module and return its findings, resolving cross-module constants."""
    paths = []
    for index, extra in enumerate(extra_sources):
        extra_path = tmp_path / f"extra_{index}.py"
        extra_path.write_text(extra)
        paths.append(extra_path)
    target = tmp_path / "sample.py"
    target.write_text(source)
    paths.append(target)
    return check_file(target, collect_container_names(paths)[target])


CONSTANTS = 'NAMES = frozenset({"ifName", "ifDescr"})\nPREFS = {"a": 1}\n'


FLAGGED = {
    "cache read straight into a frozenset": 'def f(cached):\n    return cached.get("x") in NAMES\n',
    "json body value into a dict": 'def f(data):\n    key = data.get("key")\n    return key in PREFS\n',
    "subscript read into a frozenset": 'def f(cached):\n    return cached["x"] not in NAMES\n',
    "value carried through a local name": 'def f(cached):\n    v = cached.get("x")\n    return v in NAMES\n',
    "value carried through an annotated local name": (
        'def f(cached):\n    v: object = cached.get("x")\n    return v in NAMES\n'
    ),
    "membership inside a chained comparison": ('def f(cached):\n    v = cached.get("x")\n    return v is v in NAMES\n'),
    "tuple narrowing does not prove element hashability": (
        'def f(cached):\n    v = cached.get("x")\n    if isinstance(v, tuple):\n        return v in NAMES\n    return False\n'
    ),
}

CLEAN = {
    "isinstance narrowing in the same and-chain": (
        'def f(cached):\n    v = cached.get("x")\n    return isinstance(v, str) and v in NAMES\n'
    ),
    "fail-closed or-chain": (
        'def f(cached):\n    v = cached.get("x")\n    return not isinstance(v, str) or v not in NAMES\n'
    ),
    "isinstance in a dominating if": (
        'def f(cached):\n    v = cached.get("x")\n    if isinstance(v, str):\n        return v in NAMES\n    return False\n'
    ),
    "value passed through a coercer": 'def f(cached):\n    return int(cached.get("x")) in PREFS\n',
    "django querydict always yields str": 'def f(request):\n    return request.POST.get("k") in PREFS\n',
    "literal left operand": 'def f():\n    return "ifName" in NAMES\n',
    "locally built working dict, not a constant": (
        "def f(rows):\n"
        '    index_map = {r.get("i"): r for r in rows}\n'
        "    for r in rows:\n"
        '        if r.get("parent") in index_map:\n'
        "            pass\n"
    ),
    "membership against a tuple never raises": (
        'CHOICES = ("a", "b")\ndef f(cached):\n    return cached.get("x") in CHOICES\n'
    ),
    "explicitly reviewed and suppressed": (
        'def f(cached):\n    # unhashable-ok: validated at the snapshot boundary\n    return cached.get("x") in NAMES\n'
    ),
}

# Guards that do not actually protect the membership test: the check must still report these.
LATE_OR_NEGATED_GUARDS = {
    "isinstance after the membership test in an and-chain": (
        'def f(cached):\n    v = cached.get("x")\n    return v in NAMES and isinstance(v, str)\n'
    ),
    "isinstance after the membership test in an or-chain": (
        'def f(cached):\n    v = cached.get("x")\n    return v in NAMES or isinstance(v, str)\n'
    ),
    "negated isinstance in an if-test with another operand": (
        "def f(cached, allow):\n"
        '    v = cached.get("x")\n'
        "    if not isinstance(v, str) or allow:\n"
        "        return v in NAMES\n"
        "    return False\n"
    ),
    "negated isinstance guarding the wrong branch": (
        "def f(cached):\n"
        '    v = cached.get("x")\n'
        "    if not isinstance(v, str):\n"
        "        return v in NAMES\n"
        "    return False\n"
    ),
    "isinstance narrowing a different name": (
        'def f(cached, other):\n    v = cached.get("x")\n    return isinstance(other, str) and v in NAMES\n'
    ),
}


@pytest.mark.parametrize("case", sorted(FLAGGED), ids=sorted(FLAGGED))
def test_the_unsafe_shapes_are_flagged(tmp_path, case):
    findings = _scan(tmp_path, CONSTANTS + FLAGGED[case])
    assert findings, f"{case!r} should be reported"


@pytest.mark.parametrize("case", sorted(CLEAN), ids=sorted(CLEAN))
def test_the_safe_shapes_are_not_flagged(tmp_path, case):
    findings = _scan(tmp_path, CONSTANTS + CLEAN[case])
    assert not findings, f"{case!r} should not be reported, got {findings}"


@pytest.mark.parametrize("case", sorted(LATE_OR_NEGATED_GUARDS), ids=sorted(LATE_OR_NEGATED_GUARDS))
def test_a_guard_that_cannot_protect_the_test_is_still_flagged(tmp_path, case):
    """An isinstance that runs after, or narrows something else, is not a guard."""
    findings = _scan(tmp_path, CONSTANTS + LATE_OR_NEGATED_GUARDS[case])
    assert findings, f"{case!r} should still be reported"


def test_a_constant_imported_from_another_module_is_still_resolved(tmp_path):
    """The constant lives in constants.py; the unsafe read lives somewhere else."""
    findings = _scan(
        tmp_path,
        'from extra_0 import NAMES\n\n\ndef f(cached):\n    return cached.get("x") in NAMES\n',
        extra_sources=[CONSTANTS],
    )
    assert findings, "a cross-module constant must not hide the finding"


def test_an_aliased_constant_imported_from_another_module_is_still_resolved(tmp_path):
    """An import alias must retain the imported constant's container type."""
    findings = _scan(
        tmp_path,
        'from extra_0 import NAMES as ALLOWED_NAMES\n\n\ndef f(cached):\n    return cached.get("x") in ALLOWED_NAMES\n',
        extra_sources=[CONSTANTS],
    )
    assert findings, "an imported alias must not hide the finding"


def test_an_import_alias_does_not_inherit_an_unrelated_modules_container_type(tmp_path):
    """An alias must resolve against its imported module, not any same-named symbol."""
    findings = _scan(
        tmp_path,
        'from extra_1 import NAMES as ALLOWED_NAMES\n\n\ndef f(cached):\n    return cached.get("x") in ALLOWED_NAMES\n',
        extra_sources=[CONSTANTS, 'NAMES = ("safe",)\n'],
    )
    assert not findings, f"an unrelated container name contaminated the import alias: {findings}"


def test_the_plugin_package_is_clean():
    """The whole point of the sweep: no unguarded site is left behind."""
    from lint_unhashable_membership import iter_python_files, main

    package = REPOSITORY_ROOT / "netbox_librenms_plugin"
    paths = list(iter_python_files([package]))
    containers_by_path = collect_container_names(paths)
    findings = [f for path in paths for f in check_file(path, containers_by_path[path])]
    assert not findings, "\n".join(f"{p}:{ln}: {msg}" for p, ln, _col, msg in findings)
    assert main([str(package)]) == 0
