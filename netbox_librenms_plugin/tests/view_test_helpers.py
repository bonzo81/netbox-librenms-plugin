"""Shared drivers for tests that call a view's ``get``/``post`` directly.

Production always reaches a view through ``dispatch()``, which runs ``View.setup()`` and binds
``self.request``. The object-scoped lookups read it, so a test that calls ``view.post(request, ...)``
straight would hit an unset attribute. These drivers bind the request the same way.
"""


def bind_and_call(view, request, method, **kwargs):
    """Call ``view.<method>(request, **kwargs)`` with the request bound as ``setup()`` binds it."""
    view.setup(request)
    return getattr(view, method)(request, **kwargs)


def post(view, request, **kwargs):
    """POST into *view* with the request bound (see :func:`bind_and_call`)."""
    return bind_and_call(view, request, "post", **kwargs)


def get(view, request, **kwargs):
    """GET into *view* with the request bound (see :func:`bind_and_call`)."""
    return bind_and_call(view, request, "get", **kwargs)
