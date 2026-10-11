"""Guard: a Tabler ``.alert`` holds an optional icon plus ONE content block."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).parents[1] / "templates"
VOID_TAGS = {"br", "img", "input", "hr", "meta", "link", "source", "wbr"}
# Alerts that choose their own layout keep it: d-flex rows of badges, or block/column alerts.
OWN_LAYOUT_CLASSES = {"d-flex", "d-block", "flex-column"}
TEMPLATE_TAGS_ONLY = re.compile(r"^(\s|\{%.*?%\}|\{#.*?#\})*$", re.S)


class _AlertScanner(HTMLParser):
    """Collect the flex items of every Tabler alert (``display: flex``, row direction)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.offenders = []

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if self.stack and self.stack[-1]["alert"]:
            self.stack[-1]["items"].append(("element", tag, classes))
        if tag in VOID_TAGS:
            return
        alert = "alert" in classes and not OWN_LAYOUT_CLASSES & set(classes)
        self.stack.append({"tag": tag, "alert": alert, "items": [], "line": self.getpos()[0]})

    def handle_data(self, data):
        if self.stack and self.stack[-1]["alert"]:
            items = self.stack[-1]["items"]
            if items and items[-1][0] == "text":
                items[-1] = ("text", items[-1][1] + data, [])
            else:
                items.append(("text", data, []))

    def handle_endtag(self, tag):
        while self.stack:
            entry = self.stack.pop()
            if entry["alert"]:
                self._check(entry)
            if entry["tag"] == tag:
                break

    def _check(self, entry):
        items = [
            item
            for item in entry["items"]
            if not (item[0] == "text" and TEMPLATE_TAGS_ONLY.match(item[1]))
            and not (item[0] == "element" and "btn-close" in item[2])
        ]
        if items and items[0][:2] == ("element", "i"):
            items = items[1:]
        if len(items) > 1:
            self.offenders.append((entry["line"], [name if kind == "element" else "text" for kind, name, _ in items]))


def _offenders(source):
    scanner = _AlertScanner()
    scanner.feed(source)
    return scanner.offenders


@pytest.mark.parametrize(
    "source",
    [
        '<div class="alert"><i class="mdi mdi-alert"></i> Run <code>x</code> first.</div>',
        '<div class="alert"><h4>Title</h4><p>Body.</p></div>',
        '<div class="alert"><strong>Note:</strong> text</div>',
    ],
)
def test_the_guard_flags_an_alert_with_several_flex_items(source):
    """Each shape renders as side-by-side columns under Tabler's flex .alert."""
    assert _offenders(source)


@pytest.mark.parametrize(
    "source",
    [
        '<div class="alert"><i class="mdi mdi-alert"></i><div>Run <code>x</code> first.</div></div>',
        '<div class="alert">Plain text with {{ value }} only.</div>',
        '<div class="alert d-flex gap-1"><span class="badge">a</span><span class="badge">b</span></div>',
        '<div class="alert alert-dismissible"><div>Text <b>x</b></div><button class="btn-close"></button></div>',
        '<div class="alert">{% if a %}<div>A <b>x</b></div>{% endif %}</div>',
    ],
)
def test_the_guard_accepts_an_icon_plus_one_content_block(source):
    """An icon plus one block, own-layout alerts, and a close button are all valid."""
    assert _offenders(source) == []


def test_every_template_alert_holds_one_content_block():
    """Text, <code>, <strong> or <p> directly inside a flex .alert would render as separate columns."""
    offenders = [
        f"{path.relative_to(TEMPLATES_DIR)}:{line} {items}"
        for path in sorted(TEMPLATES_DIR.rglob("*.html"))
        for line, items in _offenders(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
