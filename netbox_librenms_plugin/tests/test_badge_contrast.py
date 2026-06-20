"""Project-wide guard: no plugin template may use a bare solid-colour badge.

A Bootstrap/Tabler ``.badge`` sets its OWN muted text colour, so a bare ``bg-{color}`` fill
(danger/success/warning/info/primary/secondary/dark/light) with no companion text colour renders
muted-grey-on-fill — unreadable in NetBox's light AND dark themes (measured ~1.0–2.3:1; the
container does not help). The readable forms are ``text-bg-{color}`` (pairs fg+bg) or
``bg-{color}`` + an explicit ``text-*`` class. The Tabler light variants ``bg-{color}-lt`` are
fine (they ship their own readable text colour).

This scans every plugin HTML template so a future bare badge can't reappear. Python-rendered
badges (tables) carry literal text colours and are covered by their own render tests.
"""

import re
from pathlib import Path

import netbox_librenms_plugin

TEMPLATE_ROOT = Path(netbox_librenms_plugin.__file__).parent / "templates"

# Solid Bootstrap colour fills that need an explicit paired text colour.
_SOLID_BG = re.compile(r"\bbg-(?:danger|success|warning|info|primary|secondary|dark|light)\b")
# Any token that establishes a text colour (text-bg-* pairs both; text-dark/white/... is explicit).
# Only real colour utilities count: a bare ``[a-z]+`` tail wrongly accepted layout utilities
# (text-center, text-uppercase, ...), letting an unreadable bare bg-* badge slip through.
_TEXT_COLOUR = re.compile(
    r"\b(?:"
    r"text-bg-(?:danger|success|warning|info|primary|secondary|dark|light)"
    r"|text-(?:dark|white|light|muted|black|body(?:-emphasis|-secondary|-tertiary)?)"
    r")\b"
)
# Match both quote styles and class attributes that span multiple lines — a single-line,
# double-quote-only regex let single-quoted or wrapped `class=...` badges evade the scan.
_CLASS_ATTR = re.compile(r"""class\s*=\s*(['"])(.*?)\1""", re.DOTALL)


def _bare_badge_offenders():
    offenders = []
    for path in TEMPLATE_ROOT.rglob("*.html"):
        text = path.read_text()
        for m in _CLASS_ATTR.finditer(text):
            cls = " ".join(m.group(2).split())  # collapse multiline / runs of whitespace
            lineno = text.count("\n", 0, m.start()) + 1
            if "badge" not in cls.split():
                continue
            if "-lt" in cls:  # Tabler light variant ships a readable text colour
                continue
            if "text-bg-" in cls:  # pairs fg+bg
                continue
            if _SOLID_BG.search(cls) and not _TEXT_COLOUR.search(cls):
                offenders.append(f'{path.name}:{lineno}  class="{cls}"')
    return offenders


def test_no_bare_solid_colour_badges_in_templates():
    offenders = _bare_badge_offenders()
    assert not offenders, (
        "Bare solid-colour badge(s) found (unreadable grey-on-fill in NetBox themes); "
        "use text-bg-* or add an explicit text-* class:\n  " + "\n  ".join(offenders)
    )


def test_text_colour_regex_rejects_layout_utilities():
    """Only real colour utilities may satisfy the text-colour check. Layout utilities like
    text-center / text-uppercase must NOT count, or a bare ``bg-*`` badge whose only text-*
    class is a layout utility would wrongly pass the contrast scan (false negative)."""
    # Real colour utilities still match.
    assert _TEXT_COLOUR.search("badge bg-warning text-dark")
    assert _TEXT_COLOUR.search("badge bg-danger text-white")
    assert _TEXT_COLOUR.search("text-bg-success")
    assert _TEXT_COLOUR.search("text-body-secondary")
    # Layout / non-colour utilities must NOT be treated as a text colour.
    assert not _TEXT_COLOUR.search("badge bg-warning text-center")
    assert not _TEXT_COLOUR.search("badge bg-danger text-uppercase")
    assert not _TEXT_COLOUR.search("text-nowrap")
    assert not _TEXT_COLOUR.search("text-truncate")


def test_class_attr_parses_single_quoted_and_multiline_attributes():
    """The class scanner must see single-quoted and line-wrapped class attributes, or a bare
    solid-colour badge written that way would evade the contrast guard (false negative)."""
    # Single-quoted attribute.
    single = """<span class='badge bg-danger'>x</span>"""
    assert [m.group(2) for m in _CLASS_ATTR.finditer(single)] == ["badge bg-danger"]
    # Multiline / wrapped attribute → collapses to one class string with the badge tokens.
    multiline = '<span\n  class="badge\n         bg-warning">x</span>'
    classes = [" ".join(m.group(2).split()) for m in _CLASS_ATTR.finditer(multiline)]
    assert "badge bg-warning" in classes
    # And such a bare badge would be flagged by the offender logic (no paired text colour).
    cls = "badge bg-danger"
    assert _SOLID_BG.search(cls) and not _TEXT_COLOUR.search(cls)
