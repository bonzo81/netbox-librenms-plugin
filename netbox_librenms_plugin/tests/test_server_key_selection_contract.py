"""Contract guard for posted LibreNMS server-key parsing.

The view behaviour itself lives in test_coverage_device_fields.py and test_view_wiring.py.
"""

import ast
from pathlib import Path

HELPER = "rebind_api_for_posted_server"


def _views_root():
    import netbox_librenms_plugin

    return Path(netbox_librenms_plugin.__file__).parent / "views"


def _helper_line_ranges(tree):
    """Return the line span of the helper that is allowed to read one raw value."""
    return [
        (node.lineno, node.end_lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == HELPER
    ]


def _reads_one_raw_server_key(call):
    return any(
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "get"
        and any(isinstance(const, ast.Constant) and const.value == "server_key" for const in arg.args)
        for arg in call.args
    )


def _loose_rebind_lines(tree):
    allowed = _helper_line_ranges(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "rebind_api_for_server"):
            continue
        if any(start <= node.lineno <= end for start, end in allowed):
            continue
        if _reads_one_raw_server_key(node):
            yield node.lineno


def test_no_view_rebinds_from_a_single_raw_server_key_value():
    """A view must hand the whole payload over so repeated server_key values fail closed."""
    offenders = [
        f"{path.name}:{lineno}"
        for path in sorted(_views_root().rglob("*.py"))
        for lineno in _loose_rebind_lines(ast.parse(path.read_text()))
    ]

    assert offenders == []
