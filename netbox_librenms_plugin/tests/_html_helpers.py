"""Shared HTML-slicing helpers for template-content tests."""

from contextlib import contextmanager


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


@contextmanager
def patch_move_url_reverse(viewname_suffix, *, resolve):
    """Patch ``django.urls.reverse`` so a move-to-winner viewname looks registered or not.

    ``resolve=True`` makes any viewname ending in ``viewname_suffix`` resolve to a fake path
    carrying the requested pk;
    ``resolve=False`` raises ``NoReverseMatch`` for it. Every other viewname resolves for real.
    The move URLs are only registered up-stack, so forcing the state here keeps the guard tests
    branch-independent. ``django.urls.reverse`` is the correct target because ``{% url %}``
    re-imports ``reverse`` from ``django.urls`` at render time.
    """
    import django.urls
    from django.urls import NoReverseMatch
    from django.urls import reverse as real_reverse

    def _reverse(viewname, *args, **kwargs):
        if str(viewname).endswith(viewname_suffix):
            if resolve:
                route_kwargs = kwargs.get("kwargs") or {}
                route_args = kwargs.get("args") or ()
                pk = route_kwargs.get("pk", route_kwargs.get("device_id"))
                if pk is None and route_args:
                    pk = route_args[-1]
                if pk is None:
                    raise NoReverseMatch(viewname)
                return f"/fake/{viewname_suffix}/{pk}/"
            raise NoReverseMatch(viewname)
        return real_reverse(viewname, *args, **kwargs)

    django.urls.reverse = _reverse
    try:
        yield
    finally:
        django.urls.reverse = real_reverse
