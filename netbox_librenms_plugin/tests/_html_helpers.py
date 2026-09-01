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
