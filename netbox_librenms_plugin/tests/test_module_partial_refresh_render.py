"""A partial module refresh renders nothing rather than a degraded table.

``post()`` only fingerprints ``librenms_id``/``oob_librenms_id`` when it reads a snapshot back,
so a truncated one would be served as complete until its TTL expired. The refresh therefore
drops the snapshot and renders an empty tab whenever any of the three fetches failed, instead
of building a table from data it knows to be incomplete.
"""

from copy import deepcopy

import pytest
from django.conf import settings
from django.test import override_settings

from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server
from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts
from netbox_librenms_plugin.tests.view_test_helpers import post as _post

SERVER_KEY = "default"
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


def _server_settings(server_key, server):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        server_key: {"librenms_url": server.url, "api_token": "test-token"}
    }
    return override_settings(PLUGINS_CONFIG=plugin_config)


def _register_refresh(server, *, transceivers=(True, []), ports=(True, {"ports": []}), inventory=(True, INVENTORY)):
    inventory_ok, inventory_data = inventory
    server.inventory_response(42, inventory_data if inventory_ok else [], status=200 if inventory_ok else 500)
    transceivers_ok, transceiver_data = transceivers
    server.register(
        "/api/v0/devices/42/transceivers",
        {"status": "ok", "transceivers": transceiver_data} if transceivers_ok else {"status": "error"},
        status=200 if transceivers_ok else 500,
    )
    ports_ok, ports_data = ports
    server.register(
        "/api/v0/devices/42/ports",
        {"status": "ok", **ports_data} if ports_ok else {"status": "error"},
        status=200 if ports_ok else 500,
    )


def _refresh(device, server, **failure):
    """Run a real modules refresh POST and return the rendered tab context."""
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    captured = {}
    original = BaseModuleTableView.render_sync_partial

    def capture(self, request, obj, server_key, context, **kwargs):
        captured["context"] = context
        return original(self, request, obj, server_key, context, **kwargs)

    request = make_request("post", {"server_key": SERVER_KEY}, HTTP_HX_REQUEST="true")
    # The concrete subclass, so a complete refresh can actually build its table.
    view = DeviceModuleTableView()
    _register_refresh(server, **failure)

    BaseModuleTableView.render_sync_partial = capture
    try:
        with _server_settings(SERVER_KEY, server):
            response = _post(view, request, pk=device.pk)
    finally:
        BaseModuleTableView.render_sync_partial = original
    # render_sync_partial nests the tab context under its own key.
    context = captured.get("context", {})
    return response, context.get("module_sync", context), request


@pytest.mark.django_db
class TestPartialModuleRefreshRendersEmpty:
    """Each partial-failure flag must reach the response as an empty tab."""

    @pytest.fixture(autouse=True)
    def _configure_server(self, settings):
        """Configure a usable default LibreNMS server for each test."""
        configure_default_librenms_server(settings)

    @staticmethod
    def _device(name):
        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay

        device = make_device(name, librenms_cf={SERVER_KEY: 42})
        make_module_bay(device, "Bay 1")
        return device

    def test_a_complete_refresh_renders_a_table(self, librenms_server):
        """Positive control: the failures below must not pass for the wrong reason."""
        device = self._device("partial-refresh-complete")

        _response, context, request = _refresh(device, librenms_server)

        assert context.get("table") is not None, "a complete refresh must still build the table"
        assert "Inventory data refreshed successfully." in message_texts(request)

    @pytest.mark.parametrize(
        "failure,expected,empty_rows_notice",
        [
            ({"transceivers": (False, "transceiver fetch failed")}, "transceiver fetch failed", True),
            ({"ports": (False, "port fetch failed")}, "port metadata fetch failed", True),
            # An inventory failure returns before the partial-outcome warning, so it carries none.
            (
                {"inventory": (False, "inventory fetch failed")},
                "Failed to fetch inventory from LibreNMS; see server logs for details.",
                False,
            ),
        ],
        ids=["transceivers", "ports", "inventory"],
    )
    def test_a_partial_refresh_renders_no_table(self, failure, expected, empty_rows_notice, librenms_server):
        from django.core.cache import cache

        from netbox_librenms_plugin.sync_cache import sync_snapshot_key

        device = self._device(f"partial-refresh-{'-'.join(failure)}")
        # Built by the production helper, so a change to the key scheme cannot silently make
        # the assertion below vacuous.
        cache_key = sync_snapshot_key(device, "inventory", SERVER_KEY)
        cache.set(cache_key, {"inventory": INVENTORY, "librenms_id": 42, "oob_librenms_id": None}, timeout=300)
        assert cache.get(cache_key) is not None, "the seed never landed, so the drop assertion proves nothing"

        try:
            _response, context, request = _refresh(device, librenms_server, **failure)
            snapshot_dropped = cache.get(cache_key) is None
        finally:
            cache.delete(cache_key)

        assert context.get("table") is None, "an incomplete refresh rendered a degraded table"
        assert context.get("cache_expiry") is None
        assert snapshot_dropped, "the truncated snapshot was left to be served as complete"
        assert any(expected in text for text in message_texts(request)), message_texts(request)
        assert "Inventory data refreshed successfully." not in message_texts(request)
        warning = next(text for text in message_texts(request) if expected in text)
        assert "Inventory refreshed" not in warning, warning
        if empty_rows_notice:
            assert "no module rows were loaded" in warning, warning
