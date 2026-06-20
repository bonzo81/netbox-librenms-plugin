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


def test_netbox_only_modal_hides_bulk_select_in_migrated_mode():
    """In migrated-donor mode the NetBox-only modal is transfer-only (per-row Move), so the bulk-select controls (select-all header + per-row checkboxes) must be hidden and the copy must describe moving — not deleting — interfaces."""
    src = (TEMPLATES_DIR / "netbox_librenms_plugin" / "_interface_sync_content.html").read_text(encoding="utf-8")

    def _guard_precedes_all(needle):
        """EVERY occurrence of *needle* must be immediately guarded by the migrated guard."""
        idx = src.find(needle)
        assert idx != -1, f"{needle!r} not found"
        while idx != -1:
            before = src[:idx]
            last_if = before.rfind("{% if ")
            last_endif = before.rfind("{% endif %}")
            assert last_if > last_endif, f"no open {{% if %}} before {needle!r}"
            assert "{% if not migrated_to_marker %}" in before[last_if:idx], (
                f"{needle!r} occurrence at {idx} not guarded by `not migrated_to_marker`"
            )
            idx = src.find(needle, idx + len(needle))
        return True

    # Both bulk-select inputs must sit inside a `not migrated_to_marker` guard — every occurrence.
    assert _guard_precedes_all('id="select-all-netbox-interfaces"')
    assert _guard_precedes_all('name="interface_ids"')
    # Transfer-oriented copy must be present for migrated mode.
    assert "Migrated device:" in src
    assert "Use <em>Move</em>" in src
