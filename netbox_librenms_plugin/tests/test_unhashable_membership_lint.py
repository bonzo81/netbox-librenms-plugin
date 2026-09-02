"""The unhashable-membership check must flag the real class and stay quiet otherwise.

A lint nobody trusts gets disabled, so the negative cases matter as much as the positive
ones. Each sample is written to a temporary file and scanned exactly as CI scans the package.
"""

import signal
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

from lint_unhashable_membership import check_file, collect_container_names, main as lint_main  # noqa: E402


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


@contextmanager
def _settles_within(seconds):
    """Fail instead of hanging when the fixed-point loop never terminates."""

    def _timed_out(_signum, _frame):
        raise AssertionError(f"collect_container_names did not settle within {seconds}s")

    previous = signal.signal(signal.SIGALRM, _timed_out)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


CONSTANTS = 'NAMES = frozenset({"ifName", "ifDescr"})\nPREFS = {"a": 1}\n'


FLAGGED = {
    "cache read straight into a frozenset": 'def f(cached):\n    return cached.get("x") in NAMES\n',
    "json body value into a dict": 'def f(data):\n    key = data.get("key")\n    return key in PREFS\n',
    "subscript read into a frozenset": 'def f(cached):\n    return cached["x"] not in NAMES\n',
    "value carried through a local name": 'def f(cached):\n    v = cached.get("x")\n    return v in NAMES\n',
    "value carried through an annotated local name": (
        'def f(cached):\n    v: object = cached.get("x")\n    return v in NAMES\n'
    ),
    "value carried through an or-fallback": 'def f(cached):\n    v = cached.get("x") or ""\n    return v in NAMES\n',
    "value carried through a conditional expression": (
        'def f(cached, c):\n    v = cached.get("x") if c else ""\n    return v in NAMES\n'
    ),
    "or-fallback used inline as the left operand": 'def f(cached):\n    return (cached.get("x") or "") in NAMES\n',
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
    "isinstance-guarded conditional expression": (
        'def f(cached):\n    v = cached.get("x")\n    safe = v if isinstance(v, str) else ""\n    return safe in NAMES\n'
    ),
    "negated isinstance guarding the fallback arm": (
        'def f(cached):\n    v = cached.get("x")\n    safe = "" if not isinstance(v, str) else v\n    return safe in NAMES\n'
    ),
    "isinstance-guarded and-chain value": (
        'def f(cached):\n    v = cached.get("x")\n    safe = isinstance(v, str) and v\n    return safe in NAMES\n'
    ),
    "fail-closed or-chain value": (
        'def f(cached):\n    v = cached.get("x")\n    safe = not isinstance(v, str) or v\n    return safe in NAMES\n'
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
    "and-chain that returns the falsy external value": (
        'def f(cached):\n    v = cached.get("x")\n    w = v and isinstance(v, str)\n    return w in NAMES\n'
    ),
    "negated isinstance guarding the wrong arm of a conditional expression": (
        'def f(cached):\n    v = cached.get("x")\n    w = v if not isinstance(v, str) else ""\n    return w in NAMES\n'
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


@pytest.mark.parametrize("failure", ["missing", "invalid-utf8"])
def test_an_unreadable_source_is_reported_as_a_finding(tmp_path, failure):
    """A bad source file must produce a useful diagnostic instead of crashing the lint run."""
    path = tmp_path / "unreadable.py"
    if failure == "invalid-utf8":
        path.write_bytes(b"value = \xff\n")

    findings = check_file(path)

    assert len(findings) == 1
    assert findings[0][:3] == (path, 0, 0)
    assert findings[0][3].startswith("could not read:")


def test_the_cli_reports_an_invalid_source_instead_of_crashing(tmp_path, capsys):
    """The declaration scan must let the normal file check report a read failure."""
    path = tmp_path / "invalid.py"
    path.write_bytes(b"value = \xff\n")

    assert lint_main([str(path)]) == 1
    assert "could not read:" in capsys.readouterr().out


def test_an_aliased_constant_imported_from_another_module_is_still_resolved(tmp_path):
    """An import alias must retain the imported constant's container type."""
    findings = _scan(
        tmp_path,
        'from extra_0 import NAMES as ALLOWED_NAMES\n\n\ndef f(cached):\n    return cached.get("x") in ALLOWED_NAMES\n',
        extra_sources=[CONSTANTS],
    )
    assert findings, "an imported alias must not hide the finding"


def test_a_class_qualified_container_imported_from_another_module_is_still_resolved(tmp_path):
    """Importing the class must carry its class-qualified container names with it."""
    findings = _scan(
        tmp_path,
        'from extra_0 import Choices\n\n\ndef f(cached):\n    return cached.get("x") in Choices.NAMES\n',
        extra_sources=['class Choices:\n    NAMES = frozenset({"ifName"})\n'],
    )
    assert findings, "an imported class must not hide the finding on its own attribute"


def test_an_aliased_class_qualified_container_is_still_resolved(tmp_path):
    """An import alias on the class must rewrite the qualifier, not drop the container."""
    findings = _scan(
        tmp_path,
        'from extra_0 import Choices as Names\n\n\ndef f(cached):\n    return cached.get("x") in Names.NAMES\n',
        extra_sources=['class Choices:\n    NAMES = frozenset({"ifName"})\n'],
    )
    assert findings, "an aliased class must not hide the finding on its own attribute"


def test_an_imported_class_does_not_lend_its_container_type_to_an_unrelated_attribute(tmp_path):
    """Only the attributes the class actually defines are containers."""
    findings = _scan(
        tmp_path,
        'from extra_0 import Choices\n\n\ndef f(cached):\n    return cached.get("x") in Choices.OTHER\n',
        extra_sources=['class Choices:\n    NAMES = frozenset({"ifName"})\n'],
    )
    assert not findings, "an attribute the class never defines must not be treated as a container"


def test_an_annotated_container_constant_is_still_resolved(tmp_path):
    """A type annotation must not hide a module-level container constant."""
    findings = _scan(
        tmp_path,
        'NAMES: frozenset[str] = frozenset({"ifName"})\n\n\ndef f(cached):\n    return cached.get("x") in NAMES\n',
    )
    assert findings, "an annotated container constant must not hide the finding"


def test_a_module_qualified_container_constant_is_still_resolved(tmp_path):
    """A module alias must retain the imported module's container constants."""
    findings = _scan(
        tmp_path,
        'import extra_0 as constants\n\n\ndef f(cached):\n    return cached.get("x") in constants.NAMES\n',
        extra_sources=[CONSTANTS],
    )
    assert findings, "a module-qualified container constant must not hide the finding"


def test_a_module_qualified_name_does_not_inherit_an_unrelated_container_type(tmp_path):
    """A qualified name must resolve against its module, not an imported namesake."""
    findings = _scan(
        tmp_path,
        (
            "from extra_0 import NAMES\n"
            "import extra_1 as constants\n\n\n"
            'def f(cached):\n    return cached.get("x") in constants.NAMES\n'
        ),
        extra_sources=[CONSTANTS, 'NAMES = ("safe",)\n'],
    )
    assert not findings, f"an unrelated container name contaminated the module alias: {findings}"


def test_a_class_qualified_container_constant_is_resolved(tmp_path):
    """A class name must retain the container type of its own constant."""
    findings = _scan(
        tmp_path,
        (
            'class Choices:\n    NAMES: frozenset[str] = frozenset({"ifName"})\n\n\n'
            'def f(cached):\n    return cached.get("x") in Choices.NAMES\n'
        ),
    )
    assert findings, "a class-qualified constant must not hide the finding"


def test_an_instance_qualified_class_container_is_resolved(tmp_path):
    """A method must resolve a container constant through its instance."""
    findings = _scan(
        tmp_path,
        (
            'class Choices:\n    NAMES = frozenset({"ifName"})\n\n'
            '    def accepts(self, cached):\n        return cached.get("x") in self.NAMES\n'
        ),
    )
    assert findings, "an instance-qualified class constant must not hide the finding"


def test_a_class_qualified_name_does_not_inherit_another_class_container_type(tmp_path):
    """A qualified name must resolve against its class, not a class namesake."""
    findings = _scan(
        tmp_path,
        (
            'class SafeChoices:\n    NAMES = ("safe",)\n\n'
            'class UnsafeChoices:\n    NAMES = frozenset({"ifName"})\n\n\n'
            'def f(cached):\n    return cached.get("x") in SafeChoices.NAMES\n'
        ),
    )
    assert not findings, f"another class contaminated the qualified name: {findings}"


def test_an_import_alias_does_not_inherit_an_unrelated_modules_container_type(tmp_path):
    """An alias must resolve against its imported module, not any same-named symbol."""
    findings = _scan(
        tmp_path,
        'from extra_1 import NAMES as ALLOWED_NAMES\n\n\ndef f(cached):\n    return cached.get("x") in ALLOWED_NAMES\n',
        extra_sources=[CONSTANTS, 'NAMES = ("safe",)\n'],
    )
    assert not findings, f"an unrelated container name contaminated the import alias: {findings}"


def test_a_self_importing_module_still_reaches_a_fixed_point(tmp_path):
    """`import sample` inside sample.py makes the resolver read the set it writes."""
    target = tmp_path / "sample.py"
    target.write_text('import sample\n\nNAMES = frozenset({"a"})\n')

    with _settles_within(10):
        names = collect_container_names((target,))[target]

    assert "NAMES" in names
    assert "sample.NAMES" in names


def test_an_unrelated_import_alias_does_not_hide_a_class_qualified_name(tmp_path):
    """A global alias set let one module's `import Choices` hide `Choices.NAMES` everywhere."""
    source = tmp_path / "source.py"
    source.write_text('class Choices:\n    NAMES = frozenset({"a"})\n')
    consumer = tmp_path / "consumer.py"
    consumer.write_text("import source\n")
    unrelated = tmp_path / "unrelated.py"
    unrelated.write_text("import Choices\n")

    with _settles_within(10):
        names = collect_container_names((source, consumer, unrelated))

    assert "Choices.NAMES" in names[source]
    assert "source.Choices.NAMES" in names[consumer]


def test_two_modules_that_import_each_other_reach_a_fixed_point(tmp_path):
    """A mutual `import` pair re-qualified each other's names, so the loop never settled."""
    package = tmp_path / "pkg"
    package.mkdir()
    first = package / "a.py"
    second = package / "b.py"
    first.write_text('import pkg.b\n\nNAMES = frozenset({"a"})\n')
    second.write_text('import pkg.a\n\nOTHER = frozenset({"b"})\n')

    with _settles_within(10):
        names = collect_container_names((first, second))

    assert "pkg.b.OTHER" in names[first]
    assert "pkg.a.NAMES" in names[second]
    assert not [name for name in names[first] if name.startswith("pkg.b.pkg.")]


def test_the_plugin_package_is_clean():
    """The whole point of the sweep: no unguarded site is left behind."""
    from lint_unhashable_membership import iter_python_files, main

    package = REPOSITORY_ROOT / "netbox_librenms_plugin"
    paths = list(iter_python_files([package]))
    containers_by_path = collect_container_names(paths)
    findings = [f for path in paths for f in check_file(path, containers_by_path[path])]
    assert not findings, "\n".join(f"{p}:{ln}: {msg}" for p, ln, _col, msg in findings)
    assert main([str(package)]) == 0
