"""Shared HTML-slicing helpers for template-content tests."""


def extract_enclosing_tag(html, marker, tag="<button"):
    """Return the opening ``tag`` of the element that contains ``marker``.

    Slices from the last ``tag`` occurrence before ``marker`` up to (not
    including) the next ``>``, so an assertion can be scoped to one element's
    own attributes — another element carrying the same attribute elsewhere in
    the page can't mask the target element dropping it. Raises ValueError when
    the marker or tag is absent (str.index/str.rindex semantics), which fails
    the calling test loudly instead of asserting against the wrong slice.
    """
    marker_idx = html.index(marker)
    tag_start = html.rindex(tag, 0, marker_idx)
    return html[tag_start : html.index(">", tag_start)]


def open_tags(html, tag):
    """Return one attribute dict per opening ``tag`` element in ``html``, in document order.

    ``html.parser`` unescapes attribute values, so a rendered ``&amp;`` reads back as ``&``.
    Use this instead of a substring check when an assertion must hold for EVERY such element:
    a substring check passes as soon as one element carries the attribute.
    """
    from html.parser import HTMLParser

    class _Collector(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []

        def handle_starttag(self, name, attrs):
            if name == tag:
                self.tags.append(dict(attrs))

    collector = _Collector()
    collector.feed(html)
    return collector.tags
