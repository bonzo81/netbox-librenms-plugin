"""
A test-time guard: each update of a change-logged object that plugin code asks for has a fresh before-state.

NetBox copies ``_prechange_snapshot`` into ``ObjectChange.prechange_data``. Without a snapshot the change log has
no diff, and netbox-branching cannot find a conflict or revert the change. ``save()`` does not clear the snapshot,
so a second save of the same instance reuses a stale before-state. ``QuerySet.update()``, ``bulk_update()`` and the
``add()`` of a generic relation write no change log record at all.

The guard records each violation and ``conftest.py`` fails the test at teardown, so a broad ``except`` in plugin
code cannot hide it.
"""

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import django
import taggit
from django.db.models import signals
from django.db.models.query import QuerySet
from netbox.context import current_request
from netbox.models.features import ChangeLoggingMixin

PACKAGE = Path(__file__).resolve().parents[1]
TESTS = PACKAGE / "tests"

# (path, function, reason): each entry lets one plugin function write change-logged rows without a change log.
ALLOWED_BULK_WRITES = [
    (
        "import_utils/virtual_chassis.py",
        "_sync_module_bay_counter",
        "module_bay_count is a counter cache; NetBox updates its counters with QuerySet.update() too.",
    ),
]

_CONSUMED = "_before_state_guard_consumed"
_MERGES = "_before_state_guard_merges"
_NOTHING = object()
_SKIP, _PLUGIN, _TESTS, _OTHER = "skip", "plugin", "tests", "other"
_SKIPPED = (Path(__file__).resolve(), Path(django.__file__).resolve().parent, Path(taggit.__file__).resolve().parent)
_GENERIC_RELATIONS = Path(django.__file__).resolve().parent / "contrib" / "contenttypes" / "fields.py"
_ALLOWED = {entry[:2] for entry in ALLOWED_BULK_WRITES}
_original_update = QuerySet.update
_files = {}
# id(instance) -> (instance, the outermost save() frame of its last save)
_last_saves = {}

violations = []


@dataclass(frozen=True)
class Violation:
    """One write of a change-logged row that plugin code asked for without a fresh before-state."""

    path: str
    function: str
    line: int
    model: str
    kind: str
    pk: object

    def __str__(self):
        return f"{self.path}:{self.function} (line {self.line}): {self.kind} on {self.model} pk={self.pk}"


def _file(frame):
    """Return ``(category, path)`` for the file of *frame*; *path* is relative to the plugin package."""
    filename = frame.f_code.co_filename
    if (known := _files.get(filename)) is None:
        path = Path(filename).resolve()
        if any(path == skipped or path.is_relative_to(skipped) for skipped in _SKIPPED):
            known = (_SKIP, None)
        elif path.is_relative_to(TESTS):
            known = (_TESTS, None)
        elif path.is_relative_to(PACKAGE / "migrations"):
            # A data migration has no request and uses historical models, which have no snapshot().
            known = (_OTHER, None)
        elif path.is_relative_to(PACKAGE):
            known = (_PLUGIN, path.relative_to(PACKAGE).as_posix())
        else:
            known = (_OTHER, None)
        _files[filename] = known
    return known


def _caller(instance, *, manager=False):
    """Return ``(caller, outer_save)`` for a write of *instance*.

    *caller* is the first frame that is not Django, taggit, the guard, or a method of *instance* itself. With
    *manager*, the methods of a related manager of *instance* are skipped too. *outer_save* is the outermost
    ``save()`` frame of *instance* below *caller*.
    """
    outer_save = None
    frame = sys._getframe(1)
    while frame is not None:
        owner = frame.f_locals.get("self")
        if owner is instance:
            if frame.f_code.co_name == "save":
                outer_save = frame
        elif _file(frame)[0] != _SKIP and not (manager and getattr(owner, "instance", None) is instance):
            return frame, outer_save
        frame = frame.f_back
    return None, outer_save


def _record(frame, model, kind, pk):
    function = frame.f_code.co_qualname.replace(".<locals>", "")
    violations.append(Violation(_file(frame)[1], function, frame.f_lineno, model._meta.label, kind, pk))


def _has_fresh_before_state(instance):
    """A before-state is fresh when it was taken after the last save of *instance*."""
    snapshot = instance.__dict__.get("_prechange_snapshot", _NOTHING)
    return snapshot is not _NOTHING and snapshot is not instance.__dict__.get(_CONSUMED, _NOTHING)


def _check_instance(instance, kind, *, manager=False):
    frame, outer_save = _caller(instance, manager=manager)
    if frame is None or _file(frame)[0] != _PLUGIN:
        return
    # A model save() that saves its instance twice (NetBox Cable inserts, then updates) is one save for the caller.
    last_save = _last_saves.get(id(instance))
    if not manager and outer_save is not None and last_save is not None and last_save[1] is outer_save:
        return
    _record(frame, type(instance), kind, instance.pk)


def _on_pre_save(sender, instance, raw=False, **kwargs):
    if raw or instance._state.adding or not isinstance(instance, ChangeLoggingMixin):
        return
    if not _has_fresh_before_state(instance):
        _check_instance(instance, "save stale" if "_prechange_snapshot" in instance.__dict__ else "save missing")


def _on_post_save(sender, instance, raw=False, **kwargs):
    if raw or not isinstance(instance, ChangeLoggingMixin):
        return
    instance.__dict__[_CONSUMED] = instance.__dict__.get("_prechange_snapshot")
    caller, outer_save = _caller(instance)
    _last_saves[id(instance)] = (instance, outer_save)
    # NetBox merges a later m2m change into the change log record of this save only in the same request.
    plugin_save = caller is not None and _file(caller)[0] != _TESTS
    instance.__dict__[_MERGES] = current_request.get() if plugin_save else None


def _on_m2m_changed(sender, instance, action, pk_set, **kwargs):
    if action not in ("pre_add", "pre_remove", "pre_clear") or (action != "pre_clear" and not pk_set):
        return
    if instance._state.adding or not isinstance(instance, ChangeLoggingMixin):
        return
    request = current_request.get()
    merges = request is not None and instance.__dict__.get(_MERGES) is request
    if not _has_fresh_before_state(instance) and not merges:
        _check_instance(instance, f"m2m {action}", manager=True)


def _guarded_update(self, **kwargs):
    if issubclass(self.model, ChangeLoggingMixin):
        kind = "update"
        frame = sys._getframe(1)
        while frame is not None and _file(frame)[0] == _SKIP:
            if frame.f_code.co_name == "bulk_update":
                kind = "bulk_update"
            elif frame.f_code.co_name == "add" and Path(frame.f_code.co_filename).resolve() == _GENERIC_RELATIONS:
                kind = "generic add"
            frame = frame.f_back
        if frame is not None and _file(frame)[0] == _PLUGIN:
            function = frame.f_code.co_qualname.replace(".<locals>", "")
            if (_file(frame)[1], function) not in _ALLOWED:
                _record(frame, self.model, kind, None)
    return _original_update(self, **kwargs)


_RECEIVERS = (
    (signals.pre_save, _on_pre_save),
    (signals.post_save, _on_post_save),
    (signals.m2m_changed, _on_m2m_changed),
)


@contextmanager
def installed():
    """Connect the signal receivers and wrap ``QuerySet.update()``; undo both on exit."""
    for signal, receiver in _RECEIVERS:
        signal.connect(receiver, weak=False, dispatch_uid=receiver.__name__)
    QuerySet.update = _guarded_update
    try:
        yield
    finally:
        QuerySet.update = _original_update
        for signal, receiver in _RECEIVERS:
            signal.disconnect(receiver, dispatch_uid=receiver.__name__)


def reset():
    """Forget the violations and the saves of the previous test."""
    violations.clear()
    _last_saves.clear()


@contextmanager
def expected_violations():
    """Yield a list that receives the violations of the block, so that the guard does not fail the test for them."""
    start = len(violations)
    found = []
    try:
        yield found
    finally:
        found.extend(violations[start:])
        del violations[start:]


def report():
    """Return the failure message for the violations of the current test."""
    lines = sorted({str(violation) for violation in violations})
    return "Plugin code wrote a change-logged row without a fresh before-state (snapshot()):\n  " + "\n  ".join(lines)
