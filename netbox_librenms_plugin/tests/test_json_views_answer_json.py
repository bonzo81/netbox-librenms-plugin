"""
Every plugin view whose HTTP handler calls ``JsonResponse`` sets ``answers_json``.

Without the flag the conflict and rule middleware answer with a redirect or a page, which a fetch() caller cannot read.
Limit: the scan reads handler methods only, so a view that returns JSON through a helper function is not found.
"""

import ast
import inspect
import textwrap

from django.urls import URLPattern, URLResolver, get_resolver
from django.views import View

PLUGIN_PREFIX = "netbox_librenms_plugin."
API_PREFIX = "netbox_librenms_plugin.api."

# Each entry returns JSON only on a side branch, so the middleware must keep its page answer.
SIDE_BRANCH_JSON_VIEWS = {
    # A full list page; only its background-job branch returns JSON, and the filter script also accepts HTML.
    "netbox_librenms_plugin.views.imports.list.LibreNMSImportView",
}


def _routed_view_classes(patterns):
    """Yield the class-based view behind every route in *patterns*."""
    for entry in patterns:
        if isinstance(entry, URLResolver):
            yield from _routed_view_classes(entry.url_patterns)
        elif isinstance(entry, URLPattern):
            view_class = getattr(entry.callback, "view_class", None)
            if view_class is not None:
                yield view_class


def _handler_calls_json_response(view_class):
    """Return True when an HTTP handler method of a plugin class in the MRO calls ``JsonResponse``."""
    for klass in view_class.__mro__:
        if not klass.__module__.startswith(PLUGIN_PREFIX):
            continue
        (class_node,) = ast.parse(textwrap.dedent(inspect.getsource(klass))).body
        for node in class_node.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in View.http_method_names:
                continue
            if any(
                isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "JsonResponse"
                for call in ast.walk(node)
            ):
                return True
    return False


def _json_view_classes():
    """Return the dotted names of the plugin's routed non-API views whose handlers answer with JSON."""
    views = {
        view_class
        for view_class in _routed_view_classes(get_resolver().url_patterns)
        if view_class.__module__.startswith(PLUGIN_PREFIX) and not view_class.__module__.startswith(API_PREFIX)
    }
    return {f"{view.__module__}.{view.__qualname__}": view for view in views if _handler_calls_json_response(view)}


def test_every_view_whose_handler_answers_with_json_sets_answers_json():
    json_views = _json_view_classes()

    missing = sorted(
        name
        for name, view in json_views.items()
        if name not in SIDE_BRANCH_JSON_VIEWS and not getattr(view, "answers_json", False)
    )

    assert missing == []


def test_the_side_branch_list_names_only_routed_json_views_without_the_flag():
    """A stale entry would hide nothing today, but would hide the next view that takes its name."""
    json_views = _json_view_classes()

    assert {name for name in SIDE_BRANCH_JSON_VIEWS if name in json_views} == SIDE_BRANCH_JSON_VIEWS
    assert not any(getattr(json_views[name], "answers_json", False) for name in SIDE_BRANCH_JSON_VIEWS)


def test_the_guard_finds_the_known_json_views():
    """The scan must see the views that already set the flag, or it would pass on an empty set."""
    json_views = _json_view_classes()

    assert "netbox_librenms_plugin.views.sync.interfaces.DeleteNetBoxInterfacesView" in json_views
    assert "netbox_librenms_plugin.views.sync.interfaces.SyncInterfaceLagView" in json_views
