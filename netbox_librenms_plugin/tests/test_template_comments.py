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
