"""Shared drivers for tests that call a view's ``get``/``post`` directly.

Production always reaches a view through ``dispatch()``, which runs ``View.setup()`` and binds
``self.request``. The object-scoped lookups read it, so a test that calls ``view.post(request, ...)``
straight would hit an unset attribute. These drivers bind the request the same way.

The builders below give a view a REAL request, a REAL user and REAL permission grants, so the
permission gate, ``restrict()`` and the messages framework all run as they do in production. A
view test that instead patches ``Device``/``Interface`` and stubs ``objects.get`` re-asserts its
own assumptions: a ``MagicMock`` answers any attribute, so such a test stays green while the real
query path is broken. Reserve mocks for the LibreNMS HTTP boundary and for errors a local DB
cannot produce (a lock ``DatabaseError``, a ``save()`` that raises).
"""

from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN, PERM_VIEW_PLUGIN
from netbox_librenms_plugin.tests.conftest import make_superuser


def bind_and_call(view, request, method, **kwargs):
    """Call ``view.<method>(request, **kwargs)`` with the request bound as ``setup()`` binds it."""
    view.setup(request, **kwargs)
    return getattr(view, method)(request, **kwargs)


def post(view, request, **kwargs):
    """POST into *view* with the request bound (see :func:`bind_and_call`)."""
    return bind_and_call(view, request, "post", **kwargs)


def get(view, request, **kwargs):
    """GET into *view* with the request bound (see :func:`bind_and_call`)."""
    return bind_and_call(view, request, "get", **kwargs)


# =============================================================================
# Real users and real permission grants
# =============================================================================
#
# NetBox enforces permissions only through ObjectPermissionBackend, which ignores Django's
# ``user_permissions`` m2m. A grant therefore has to be a real ``ObjectPermission`` row, and the
# user must be re-read afterwards to drop the per-instance permission cache.
# ``make_superuser`` (re-exported from conftest) covers the unconstrained case.


def grant(user, action, model, *, constraints=None, name=None):
    """Grant a real object permission, optionally constrained, and return the user with a fresh permission cache."""
    from core.models import ObjectType
    from django.contrib.auth import get_user_model
    from users.models import ObjectPermission

    op = ObjectPermission.objects.create(
        name=name or f"{user.username}-{action}-{model._meta.model_name}-{ObjectPermission.objects.count()}",
        actions=[action],
        constraints=constraints,
    )
    op.object_types.set([ObjectType.objects.get_for_model(model)])
    op.users.set([user])
    return get_user_model().objects.get(pk=user.pk)


def make_user_with_perms(username, perm_specs, *, constraints=None, plugin_write=True):
    """Create a real non-superuser with exact, optionally constrained grants and optional plugin write access."""
    from django.apps import apps
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username=username, password="x")
    if plugin_write:
        # Resolve through the app registry, not the module attribute: a suite-wide autouse
        # fixture patches ``netbox_librenms_plugin.models.LibreNMSSettings`` (spread by
        # pytest_plugins), and importing it here would hand a MagicMock to get_for_model.
        settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        for action in ("view", "change"):
            user = grant(user, action, settings_model, name=f"{username}-plugin-{action}")
    for action, model in perm_specs:
        user = grant(user, action, model, constraints=constraints)
    return user


def plugin_perms():
    """The two plugin permission strings every write view checks."""
    return (PERM_VIEW_PLUGIN, PERM_CHANGE_PLUGIN)


# =============================================================================
# Real requests
# =============================================================================


def make_request(method="post", data=None, *, user=None, path="/", **factory_kwargs):
    """Build a real Django request with a user, session, and working message storage."""
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore
    from django.test import RequestFactory

    request = getattr(RequestFactory(), method)(path, data if data is not None else {}, **factory_kwargs)
    request.user = user if user is not None else make_superuser()
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    return request


def _message_level(name):
    """Map a message name to its level constant because NetBox remaps error tags to ``danger``."""
    from django.contrib import messages

    levels = {
        "debug": messages.DEBUG,
        "info": messages.INFO,
        "success": messages.SUCCESS,
        "warning": messages.WARNING,
        "error": messages.ERROR,
    }
    try:
        return levels[name.lower()]
    except (AttributeError, KeyError):
        raise ValueError(f"unknown message level {name!r}") from None


def messages_on(request):
    """Return the messages recorded on *request* as ``[(level_name, text), ...]``."""
    from django.contrib.messages import constants, get_messages

    names = {level: name.lower() for name, level in constants.DEFAULT_LEVELS.items()}
    return [(names.get(m.level, str(m.level)), str(m.message)) for m in get_messages(request)]


def message_texts(request, level=None):
    """Return the recorded message texts, optionally only those at *level* (e.g. ``"error"``)."""
    from django.contrib.messages import get_messages

    wanted = None if level is None else _message_level(level)
    return [str(m.message) for m in get_messages(request) if wanted is None or m.level == wanted]


def missing_pk(model, offset=1000):
    """Return a primary key that is above every current row for *model*."""
    highest_pk = model.objects.order_by("-pk").values_list("pk", flat=True).first()
    return (highest_pk or 0) + offset


def trusted_module_inventory_payload(device, inventory, *, server_key="default", librenms_id=1):
    """Build a module inventory payload bound to the device's verified current LibreNMS mapping."""
    from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id

    if not isinstance(device.custom_field_data, dict):
        device.custom_field_data = {}
        device.cf = device.custom_field_data
    set_librenms_device_id(device, librenms_id, server_key)
    device.save(update_fields=["custom_field_data"])
    # set_librenms_device_id() only logs and returns when it refuses a write (legacy bare
    # integer, non-positive id), which would leave the payload claiming a mapping the device
    # does not have; every caller would then fail on the production staleness guard instead.
    stored = get_librenms_device_id(device, server_key, auto_save=False)
    assert stored == librenms_id, (
        f"set_librenms_device_id declined the write (stored {stored!r}, wanted {librenms_id!r}); "
        "the payload fingerprint would not match the device mapping"
    )
    return {
        "inventory": inventory,
        "librenms_id": librenms_id,
        "oob_librenms_id": None,
    }


def assert_locked_before_update(captured, table):
    """Verify that exactly one ``SELECT ... FOR UPDATE`` locks *table* before its update."""
    statements = [q["sql"] for q in captured.captured_queries]
    quoted_table = f'"{table.lower()}"'
    locks = [i for i, sql in enumerate(statements) if quoted_table in sql.lower() and "for update" in sql.lower()]
    updates = [i for i, sql in enumerate(statements) if sql.lower().lstrip().startswith(f"update {quoted_table}")]
    assert locks, f"{table} was never locked with SELECT ... FOR UPDATE"
    assert updates, f"{table} was never updated, so the lock ordering is untested"
    assert len(locks) == len(updates) == 1, (
        f"{table} must have exactly one lock/update pair: locks={locks} updates={updates}"
    )
    assert locks[0] < updates[0], f"{table} was updated before it was locked: locks={locks} updates={updates}"


# =============================================================================
# Real views
# =============================================================================


def make_view(view_class, request=None, *, librenms_api=None, **attrs):
    """Instantiate and bind a real view while substituting only its external LibreNMS client."""
    from unittest.mock import MagicMock

    view = view_class()
    if librenms_api is not False:
        if librenms_api is None:
            librenms_api = MagicMock()
            librenms_api.server_key = "default"
        view._librenms_api = librenms_api
    view.setup(request if request is not None else make_request())
    for name, value in attrs.items():
        setattr(view, name, value)
    return view
