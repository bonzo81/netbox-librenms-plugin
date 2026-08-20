"""A partial module refresh renders nothing rather than a degraded table.

``post()`` only fingerprints ``librenms_id``/``oob_librenms_id`` when it reads a snapshot back,
so a truncated one would be served as complete until its TTL expired. The refresh therefore
drops the snapshot and renders an empty tab whenever any of the three fetches failed, instead
of building a table from data it knows to be incomplete.
"""

from contextlib import ExitStack
from unittest.mock import patch

import pytest

from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts
from netbox_librenms_plugin.tests.view_test_helpers import post as _post


def _configured_server_key():
    """Return a server key this deployment actually configures.

    The names differ between the repository test configuration and a local devcontainer, so
    hardcoding one passes in one place and fails in the other.
    """
    from django.conf import settings

    servers = (settings.PLUGINS_CONFIG.get("netbox_librenms_plugin") or {}).get("servers") or {}
    return next(iter(servers), "default")


INVENTORY = [
    {
        "entPhysicalIndex": 1,
        "entPhysicalName": "Bay 1",
        "entPhysicalModelName": "Partial Model",
        "entPhysicalClass": "module",
        "entPhysicalContainedIn": 0,
        "entPhysicalSerialNum": "PARTIAL-1",
    }
]


def _patched_client(*, transceivers=(True, []), ports=(True, {"ports": []}), inventory=(True, INVENTORY)):
    """Patch only the LibreNMS HTTP client methods; the view resolves its own real client."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    return (
        patch.object(LibreNMSAPI, "get_librenms_id", lambda self, obj: 42),
        patch.object(LibreNMSAPI, "get_stored_librenms_id", lambda self, obj, server_key=None: 42),
        patch.object(LibreNMSAPI, "get_device_inventory", lambda self, *a, **k: inventory),
        patch.object(LibreNMSAPI, "get_device_transceivers", lambda self, *a, **k: transceivers),
        patch.object(LibreNMSAPI, "get_ports", lambda self, *a, **k: ports),
    )


def _refresh(device, **failure):
    """Run a real modules refresh POST and return the rendered tab context."""
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    captured = {}
    original = BaseModuleTableView.render_sync_partial

    def capture(self, request, obj, server_key, context, **kwargs):
        captured["context"] = context
        return original(self, request, obj, server_key, context, **kwargs)

    server_key = _configured_server_key()
    request = make_request("post", {"server_key": server_key}, HTTP_HX_REQUEST="true")
    # The concrete subclass, so a complete refresh can actually build its table.
    view = DeviceModuleTableView()

    BaseModuleTableView.render_sync_partial = capture
    try:
        with ExitStack() as stack:
            for patcher in _patched_client(**failure):
                stack.enter_context(patcher)
            response = _post(view, request, pk=device.pk)
    finally:
        BaseModuleTableView.render_sync_partial = original
    # render_sync_partial nests the tab context under its own key.
    context = captured.get("context", {})
    return response, context.get("module_sync", context), request


@pytest.mark.django_db
class TestPartialModuleRefreshRendersEmpty:
    """Each partial-failure flag must reach the response as an empty tab."""

    @staticmethod
    def _device(name):
        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay

        device = make_device(name, librenms_cf={_configured_server_key(): 42})
        make_module_bay(device, "Bay 1")
        return device

    def test_a_complete_refresh_renders_a_table(self):
        """Positive control: the failures below must not pass for the wrong reason."""
        device = self._device("partial-refresh-complete")

        _response, context, request = _refresh(device)

        assert context.get("table") is not None, "a complete refresh must still build the table"
        assert "Inventory data refreshed successfully." in message_texts(request)

    @pytest.mark.parametrize(
        "failure,expected",
        [
            ({"transceivers": (False, "transceiver fetch failed")}, "transceiver fetch failed"),
            ({"ports": (False, "port fetch failed")}, "port metadata fetch failed"),
        ],
        ids=["transceivers", "ports"],
    )
    def test_a_partial_refresh_renders_no_table(self, failure, expected):
        from django.core.cache import cache

        from netbox_librenms_plugin.sync_cache import sync_snapshot_key

        device = self._device(f"partial-refresh-{'-'.join(failure)}")
        # Built by the production helper, so a change to the key scheme cannot silently make
        # the assertion below vacuous.
        cache_key = sync_snapshot_key(device, "inventory", _configured_server_key())
        cache.set(cache_key, {"inventory": INVENTORY, "librenms_id": 42, "oob_librenms_id": None}, timeout=300)
        assert cache.get(cache_key) is not None, "the seed never landed, so the drop assertion proves nothing"

        try:
            _response, context, request = _refresh(device, **failure)
            snapshot_dropped = cache.get(cache_key) is None
        finally:
            cache.delete(cache_key)

        assert context.get("table") is None, "an incomplete refresh rendered a degraded table"
        assert context.get("cache_expiry") is None
        assert snapshot_dropped, "the truncated snapshot was left to be served as complete"
        assert any(expected in text for text in message_texts(request)), message_texts(request)
        assert "Inventory data refreshed successfully." not in message_texts(request)
