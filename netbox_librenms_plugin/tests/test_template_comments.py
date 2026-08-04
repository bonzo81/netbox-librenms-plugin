"""Guard against multi-line Django ``{# #}`` comments.

Django's ``{# #}`` comment syntax must stay on a single line. A multi-line
``{# ... #}`` is NOT recognised as a comment and renders as literal text in the
page (e.g. the platform-cell pencil-icon partial once leaked its header comment
into the import modal). ``get_template()`` only *parses* templates, so this slips
past template-compile checks — hence this static scan. Use ``{% comment %}`` for
multi-line comments instead.
"""

import pathlib

import netbox_librenms_plugin

TEMPLATES_DIR = pathlib.Path(netbox_librenms_plugin.__file__).parent / "templates"


def _html_templates():
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def test_templates_found():
    """Sanity check that the scan actually has templates to look at."""
    assert _html_templates(), f"No .html templates found under {TEMPLATES_DIR}"


def test_no_multiline_single_hash_comments():
    """No line may contain an unbalanced ``{#`` / ``#}`` (a multi-line ``{# #}``)."""
    offenders = []
    for path in _html_templates():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.count("{#") != line.count("#}"):
                offenders.append(f"{path.relative_to(TEMPLATES_DIR)}:{lineno}: {line.strip()}")
    assert not offenders, "Multi-line `{# #}` comments render as literal text — use `{% comment %}`:\n" + "\n".join(
        offenders
    )


def _guard_precedes_all(src, needle):
    """Assert every occurrence of *needle* sits inside an open `{% if not migrated_to_marker %}` block."""
    # Walk the template tag-by-tag, maintaining a stack of currently-ACTIVE branch conditions,
    # and require "not migrated_to_marker" to be somewhere in that stack at each needle
    # occurrence. A depth-unaware rfind() would latch onto the INNERMOST {% if %} and miss an
    # OUTER migrated guard whenever the needle is nested inside another conditional (false
    # "unguarded" report). {% elif %}/{% else %} REPLACE their level's condition rather than
    # popping/pushing: content after them renders when the original `if` condition is false,
    # so the spent condition must stop counting as protection there.
    import re

    tag_re = re.compile(
        r"{%-?\s*if\s+(?P<if>.*?)\s*-?%}"
        r"|{%-?\s*elif\s+(?P<elif>.*?)\s*-?%}"
        r"|{%-?\s*(?P<else>else)\s*-?%}"
        r"|{%-?\s*(?P<endif>endif)\s*-?%}"
    )
    occurrences = [m.start() for m in re.finditer(re.escape(needle), src)]
    assert occurrences, f"{needle!r} not found"

    def _condition_guards(condition):
        # Quoted literals are data, not structure: they can carry `and`/`or`/the guard text
        # without evaluating it, so blank them before analyzing the operator shape.
        condition = re.sub(r"'[^']*'|\"[^\"]*\"", "", condition)
        # Only a pure and-conjunction carrying the guard as one conjunct protects the needle:
        # any `or` (and `and` binds tighter than `or` in Django's if-tag) can render the
        # branch with the marker set regardless of the guard operand — fail closed on those.
        if re.search(r"\s+or\s+", condition):
            return False
        return "not migrated_to_marker" in re.split(r"\s+and\s+", condition)

    for pos in occurrences:
        stack = []
        for m in tag_re.finditer(src, 0, pos):
            if m.group("if") is not None:
                stack.append(m.group("if"))
            elif m.group("elif") is not None:
                assert stack, f"{{% elif %}} outside any {{% if %}} before {needle!r}"
                stack[-1] = m.group("elif")
            elif m.group("else") is not None:
                assert stack, f"{{% else %}} outside any {{% if %}} before {needle!r}"
                stack[-1] = "<else>"
            else:  # {% endif %}
                assert stack, f"unbalanced {{% endif %}} before {needle!r}"
                stack.pop()
        assert any(_condition_guards(condition) for condition in stack), (
            f"{needle!r} occurrence at {pos} is not inside a `not migrated_to_marker` block"
        )
    return True


def test_netbox_only_modal_hides_bulk_select_in_migrated_mode():
    """In migrated-donor mode the NetBox-only modal is transfer-only (per-row Move), so the bulk-select controls (select-all header + per-row checkboxes) must be hidden and the copy must describe moving — not deleting — interfaces."""
    src = (TEMPLATES_DIR / "netbox_librenms_plugin" / "_interface_sync_content.html").read_text(encoding="utf-8")

    # Both bulk-select inputs must sit inside a `not migrated_to_marker` guard — every occurrence.
    assert _guard_precedes_all(src, 'id="select-all-netbox-interfaces"')
    assert _guard_precedes_all(src, 'name="interface_ids"')
    # Transfer-oriented copy must be present for migrated mode.
    assert "Migrated device:" in src
    assert "Use <em>Move</em>" in src


def test_guard_scan_is_block_depth_aware():
    """A needle nested inside an inner {% if %} is still recognised as guarded by the outer migrated block."""
    import pytest

    # Guarded: the needle sits inside an INNER conditional that is itself inside the migrated guard.
    # A depth-unaware rfind() latches onto `{% if extra %}` and wrongly reports it unguarded.
    nested_guarded = "{% if not migrated_to_marker %}<div>{% if extra %}NEEDLE{% endif %}</div>{% endif %}"
    assert _guard_precedes_all(nested_guarded, "NEEDLE")

    # Unguarded: the migrated block has already closed; the needle lives in a later, unrelated block.
    unguarded = "{% if not migrated_to_marker %}ok{% endif %}{% if other %}NEEDLE{% endif %}"
    with pytest.raises(AssertionError):
        _guard_precedes_all(unguarded, "NEEDLE")


def test_guard_scan_tracks_else_and_elif_branches():
    """A needle in the {% else %}/{% elif %} branch of the migrated guard renders exactly when the marker IS set, so the scanner must stop counting the spent `if` condition as protection once the branch switches."""
    import pytest

    # The else branch renders in migrated mode — a bulk control moved there must FAIL the guard.
    in_else = "{% if not migrated_to_marker %}ok{% else %}NEEDLE{% endif %}"
    with pytest.raises(AssertionError):
        _guard_precedes_all(in_else, "NEEDLE")

    # Same for an elif branch: its own condition replaces the guard, it doesn't inherit it.
    in_elif = "{% if not migrated_to_marker %}ok{% elif other %}NEEDLE{% endif %}"
    with pytest.raises(AssertionError):
        _guard_precedes_all(in_elif, "NEEDLE")

    # The guard may also ARRIVE via elif — then the needle genuinely is protected.
    guarded_by_elif = "{% if other %}x{% elif not migrated_to_marker %}NEEDLE{% endif %}"
    assert _guard_precedes_all(guarded_by_elif, "NEEDLE")

    # And the if-branch of a guard that HAS an else stays recognised as guarded.
    if_branch = "{% if not migrated_to_marker %}NEEDLE{% else %}x{% endif %}"
    assert _guard_precedes_all(if_branch, "NEEDLE")


def test_guard_scan_handles_compound_conditions():
    """An and-conjunct guard protects the needle; any or-compound must not count as protection."""
    import pytest

    # The guard as one conjunct of a pure `and` chain always gates the render — accepted.
    and_compound = "{% if not migrated_to_marker and extra %}NEEDLE{% endif %}"
    assert _guard_precedes_all(and_compound, "NEEDLE")

    # An `or` branch renders with the marker set whenever the other operand is true — rejected.
    or_compound = "{% if other or not migrated_to_marker %}NEEDLE{% endif %}"
    with pytest.raises(AssertionError):
        _guard_precedes_all(or_compound, "NEEDLE")

    # Django's if-tag binds `and` tighter than `or`, so a guard conjunct left of an `or`
    # doesn't gate the right operand's render path either — still rejected.
    mixed = "{% if not migrated_to_marker and extra or other %}NEEDLE{% endif %}"
    with pytest.raises(AssertionError):
        _guard_precedes_all(mixed, "NEEDLE")

    # Operators and the guard token inside QUOTED literals are data, not structure: a literal
    # containing the guard text must not count as protection...
    quoted_token = '{% if value == "x and not migrated_to_marker and y" %}NEEDLE{% endif %}'
    with pytest.raises(AssertionError):
        _guard_precedes_all(quoted_token, "NEEDLE")

    # ...and an `or` inside a literal must not disqualify a genuine and-conjunct guard.
    quoted_or = '{% if not migrated_to_marker and label == "local or remote" %}NEEDLE{% endif %}'
    assert _guard_precedes_all(quoted_or, "NEEDLE")
