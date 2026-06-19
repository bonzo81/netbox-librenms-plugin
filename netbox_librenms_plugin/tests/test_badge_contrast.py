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
_TEXT_COLOUR = re.compile(r"\btext-(?:bg-\w+|dark|white|light|muted|body\S*|black|[a-z]+)\b")
_CLASS_ATTR = re.compile(r'class="([^"]*)"')


def _bare_badge_offenders():
    offenders = []
    for path in TEMPLATE_ROOT.rglob("*.html"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for cls in _CLASS_ATTR.findall(line):
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
