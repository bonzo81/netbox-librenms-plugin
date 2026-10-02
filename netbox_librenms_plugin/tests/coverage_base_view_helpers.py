"""Real NetBox objects shared by the base-view coverage modules."""

import itertools

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser


_device_counter = itertools.count(1)


def make_sync_page_device():
    """Return a real device that scoped view querysets can resolve."""
    return make_device(f"coverage-base-{next(_device_counter)}")


def make_sync_page_request(path="/plugins/librenms/device/1/cables/"):
    """Return a real GET request carrying a real superuser."""
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.test import RequestFactory

    request = RequestFactory().get(path)
    request.user = make_superuser()
    request.headers = {}
    request.session = {}
    request._messages = FallbackStorage(request)
    return request
