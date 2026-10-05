"""
Contract guard for posted LibreNMS server-key parsing.

The view behaviour itself lives in test_coverage_device_fields.py and test_view_wiring.py.
"""

import ast
from pathlib import Path

import pytest

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
    arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
    return any(
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "get"
        and any(isinstance(const, ast.Constant) and const.value == "server_key" for const in arg.args)
        for arg in arguments
    )


def _is_posted_server_key_getlist(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getlist"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "POST"
        and any(isinstance(arg, ast.Constant) and arg.value == "server_key" for arg in node.args)
    )


def _reads_raw_posted_server_key(node, getlist_names=frozenset()):
    if isinstance(node, ast.Subscript) and (
        _is_posted_server_key_getlist(node.value)
        or (isinstance(node.value, ast.Name) and node.value.id in getlist_names)
    ):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        payload = node.func.value
        key = node.args[0] if node.args else None
    elif isinstance(node, ast.Subscript):
        payload, key = node.value, node.slice
    else:
        return False
    return (
        isinstance(payload, ast.Attribute)
        and payload.attr == "POST"
        and isinstance(key, ast.Constant)
        and key.value == "server_key"
    )


def _loose_rebind_lines(tree):
    allowed = _helper_line_ranges(tree)
    offenders = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        rebinds = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rebind_api_for_server"
        ]
        if not rebinds:
            continue
        getlist_names = {
            target.id
            for node in ast.walk(function)
            if isinstance(node, ast.Assign) and _is_posted_server_key_getlist(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        reads = [node for node in ast.walk(function) if _reads_raw_posted_server_key(node, getlist_names)]
        reads.extend(call for call in rebinds if _reads_one_raw_server_key(call))
        offenders.update(
            node.lineno for node in reads if not any(start <= node.lineno <= end for start, end in allowed)
        )
    yield from sorted(offenders)


def test_no_view_rebinds_from_a_single_raw_server_key_value():
    """A view must hand the whole payload over so repeated server_key values fail closed."""
    offenders = [
        f"{path.name}:{lineno}"
        for path in sorted(_views_root().rglob("*.py"))
        for lineno in _loose_rebind_lines(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert offenders == []


def test_keyword_argument_raw_server_key_read_is_detected():
    """A keyword call must not bypass the repeated-value server-key contract."""
    tree = ast.parse(
        """
def view(request):
    self.rebind_api_for_server(server_key=request.GET.get("server_key"))
"""
    )

    assert list(_loose_rebind_lines(tree)) == [3]


@pytest.mark.parametrize("read", ['request.POST.get("server_key")', 'request.POST["server_key"]'])
def test_indirect_posted_server_key_reads_are_detected(read):
    """Neither assignment nor subscripting may discard repeated posted values."""
    tree = ast.parse(f"def view(request):\n    key = {read}\n    self.rebind_api_for_server(key)\n")
    assert list(_loose_rebind_lines(tree)) == [2]


def test_subscript_argument_posted_server_key_read_is_detected():
    tree = ast.parse('def view(request):\n    self.rebind_api_for_server(request.POST["server_key"])\n')
    assert list(_loose_rebind_lines(tree)) == [2]


@pytest.mark.parametrize(
    "body",
    [
        'self.rebind_api_for_server(request.POST.getlist("server_key")[0])',
        'keys = request.POST.getlist("server_key"); self.rebind_api_for_server(keys[0])',
    ],
)
def test_indexed_getlist_server_key_reads_are_detected(body):
    """An indexed getlist() value discards repeated posted values too."""
    tree = ast.parse(f"def view(request):\n    {body}\n")
    assert list(_loose_rebind_lines(tree)) == [2]


@pytest.mark.parametrize(
    "body",
    [
        "self.rebind_api_for_posted_server(request.POST)",
        'key = job_data.get("server_key"); self.rebind_api_for_server(parsed.server_key)',
        'key = request.GET.get("server_key"); self.rebind_api_for_server(key)',
    ],
)
def test_unrelated_reads_do_not_trigger_the_posted_server_key_guard(body):
    tree = ast.parse(f"def view(request):\n    {body}\n")
    assert list(_loose_rebind_lines(tree)) == []
