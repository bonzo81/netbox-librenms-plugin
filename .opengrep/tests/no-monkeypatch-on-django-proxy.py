from django.conf import settings as django_settings
from django.core.cache import cache, caches
from django.db import connection
from django.test import override_settings
from netbox import settings as netbox_settings

from netbox_librenms_plugin import librenms_api, utils


def test_cache_proxy(monkeypatch):
    # ruleid: no-monkeypatch-on-django-proxy
    monkeypatch.setattr(cache, "set", lambda *args, **kwargs: None)
    # ruleid: no-monkeypatch-on-django-proxy
    monkeypatch.delattr(cache, "has_key")


def test_cache_proxy_through_a_module(monkeypatch):
    # ruleid: no-monkeypatch-on-django-proxy
    monkeypatch.setattr(librenms_api.cache, "get", lambda key: None)


def test_cache_proxy_in_a_context(monkeypatch):
    with monkeypatch.context() as context:
        # ruleid: no-monkeypatch-on-django-proxy
        context.setattr(cache, "delete_pattern", lambda pattern: None)


def test_settings_fixture(monkeypatch, settings):
    with monkeypatch.context() as version:
        # ruleid: no-monkeypatch-on-django-proxy
        version.setattr(settings, "RELEASE", None)


def test_lazy_settings_and_connection(monkeypatch):
    # ruleid: no-monkeypatch-on-django-proxy
    monkeypatch.setattr(django_settings, "DEBUG", True)
    # ruleid: no-monkeypatch-on-django-proxy
    monkeypatch.setattr(connection, "vendor", "sqlite")


def test_dotted_string_targets(monkeypatch):
    # ruleid: no-monkeypatch-on-django-proxy
    monkeypatch.setattr("django.core.cache.cache.set", lambda *args, **kwargs: None)
    # ruleid: no-monkeypatch-on-django-proxy
    monkeypatch.setattr("django.conf.settings.DEBUG", True)
    # ruleid: no-monkeypatch-on-django-proxy
    monkeypatch.delattr("django.db.connection.vendor")
    # ok: no-monkeypatch-on-django-proxy
    monkeypatch.setattr("netbox_librenms_plugin.utils._get_netbox_version_tuple", lambda: (4, 5, 8))
    # ok: no-monkeypatch-on-django-proxy
    monkeypatch.setattr("django.core.cache.caches", {})


def test_resolved_backend(monkeypatch):
    # ok: no-monkeypatch-on-django-proxy
    monkeypatch.setattr(caches["default"], "set", lambda *args, **kwargs: None)


def test_netbox_settings_module(monkeypatch):
    # ok: no-monkeypatch-on-django-proxy
    monkeypatch.setattr(netbox_settings, "RELEASE", None)


def test_settings_changes(monkeypatch, settings):
    # ok: no-monkeypatch-on-django-proxy
    settings.RELEASE = None
    # ok: no-monkeypatch-on-django-proxy
    monkeypatch.setattr(utils, "_get_netbox_version_tuple", lambda: (4, 5, 8))
    with override_settings(DEBUG=True):
        pass


def test_plain_setattr():
    # ok: no-monkeypatch-on-django-proxy
    setattr(cache, "set", None)
