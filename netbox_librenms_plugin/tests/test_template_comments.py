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
    # Walk the template tag-by-tag, maintaining a stack of currently-open {% if %} conditions, and
    # require "not migrated_to_marker" to be somewhere in that stack at each needle occurrence.
    # A depth-unaware rfind() would latch onto the INNERMOST {% if %} and miss an OUTER migrated
    # guard whenever the needle is nested inside another conditional (false "unguarded" report).
    import re

    tag_re = re.compile(r"{%-?\s*(if)\s+(.*?)\s*-?%}|{%-?\s*(endif)\s*-?%}")
    occurrences = [m.start() for m in re.finditer(re.escape(needle), src)]
    assert occurrences, f"{needle!r} not found"
    for pos in occurrences:
        stack = []
        for m in tag_re.finditer(src, 0, pos):
            if m.group(1):  # {% if <cond> %}
                stack.append(m.group(2))
            else:  # {% endif %}
                assert stack, f"unbalanced {{% endif %}} before {needle!r}"
                stack.pop()
        assert "not migrated_to_marker" in stack, (
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
