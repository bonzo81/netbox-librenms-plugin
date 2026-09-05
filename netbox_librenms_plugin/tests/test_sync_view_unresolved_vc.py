"""Unresolved ?server_key must fail closed in the VC-status context, not leak the default server.

``BaseLibreNMSSyncView.get`` fails closed on an unresolved ``?server_key`` (a decommissioned /
misconfigured server): the rebind declines, the client stays on the default server, and
``self.librenms_id`` is forced to ``None`` so the header renders "not found in LibreNMS". But
``get_context_data`` recomputes the Virtual-Chassis sync-device linkage from
``self.librenms_api.server_key`` — which, on an unresolved key, is the DEFAULT server. Without a
guard it reports the member as linked to a valid sync device on the (gone) server, contradicting
the failed-closed header and inviting a sync against a server that no longer exists.

This drives the real ``get()`` -> ``get_context_data`` flow against a real DB Virtual Chassis and
real single-server ``settings.PLUGINS_CONFIG`` (via ``override_settings``). Only the orthogonal
tab-context / device-info / parent-context seams are stubbed; the VC-linkage computation runs for
real, and the rendered context is captured to assert what the page would show.
"""

import copy
from unittest.mock import MagicMock, patch

import pytest
from dcim.models import Device

from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
from django.conf import settings
from django.test import RequestFactory, override_settings

from netbox_librenms_plugin.tests.conftest import make_device


DEFAULT_ONLY = {"default": {"librenms_url": "https://default.example.com", "api_token": "default-token"}}


def _plugins_config_with_servers(servers):
    """Return a PLUGINS_CONFIG copy with the plugin's ``servers`` set to ``servers``."""
    config = copy.deepcopy(settings.PLUGINS_CONFIG)
    plugin_config = dict(config.get("netbox_librenms_plugin", {}))
    plugin_config["servers"] = servers
    config["netbox_librenms_plugin"] = plugin_config
    return config


@pytest.mark.django_db
class TestUnresolvedServerKeyVCLeak:
    def _make_view(self, request):
        """Build a DeviceLibreNMSSyncView with the orthogonal (non-VC) context seams stubbed."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

        view = object.__new__(DeviceLibreNMSSyncView)
        view.request = request
        view.kwargs = {}
        view.get_librenms_device_info = lambda obj, req: {
            "found_in_librenms": False,
            "librenms_device_details": {"librenms_device_serial": "-", "vc_inventory_serials": []},
            "mismatched_device": False,
        }
        view.get_interface_context = lambda req, obj: None
        view.get_cable_context = lambda req, obj: None
        view.get_ip_context = lambda req, obj: None
        view.get_vlan_context = lambda req, obj: None
        view.get_module_context = lambda req, obj: None
        view._get_platform_info = lambda info, obj: {}
        view.has_write_permission = lambda: False
        return view

    def test_unresolved_server_key_does_not_leak_default_vc_linkage(self):
        from dcim.models import VirtualChassis

        vc = VirtualChassis.objects.create(name="unresolved-leak-vc")
        member = make_device("unresolved-leak-m1")
        member.virtual_chassis = vc
        member.vc_position = 1
        # A real, valid host id on the DEFAULT server. On an unresolved ?server_key the client
        # stays bound to "default", so the un-guarded VC block would resolve *this* linkage.
        member.custom_field_data["librenms_id"] = {"default": {"id": 55}}
        member.save()

        request = RequestFactory().get("/x/?server_key=ghost")  # non-blank, not configured -> unresolved
        # A real permitted user: the scoped lookup would 404 for AnonymousUser, and this
        # test is about server-key resolution, not authorization.
        request.user = make_user_with_perms("unresolved-vc-viewer", [("view", Device)])
        view = self._make_view(request)

        captured = {}

        def _capture_render(req, template, context, *args, **kwargs):
            captured["context"] = context
            return MagicMock()

        with (
            override_settings(PLUGINS_CONFIG=_plugins_config_with_servers(DEFAULT_ONLY)),
            patch(
                "netbox_librenms_plugin.views.base.librenms_sync_view.render",
                side_effect=_capture_render,
            ),
            patch(
                "netbox_librenms_plugin.views.base.librenms_sync_view.get_interface_name_field",
                return_value="ifName",
            ),
            patch(
                "netbox_librenms_plugin.views.base.librenms_sync_view.LibreNMSAPIMixin.get_context_data",
                return_value={},
            ),
        ):
            view.get(request, pk=member.pk)

        ctx = captured["context"]
        # Sanity: the header failed closed (unresolved -> librenms_id None).
        assert ctx.get("has_librenms_id") is False
        assert view.librenms_api.server_key == "default"  # rebind declined, still on default
        # The bug: the VC-status block leaks the default server's linkage on an unresolved key.
        assert ctx.get("sync_device_has_librenms_id") is not True, (
            "Unresolved ?server_key leaked the default server's VC sync-device linkage "
            "(get_context_data VC block ran without the unresolved guard)"
        )
