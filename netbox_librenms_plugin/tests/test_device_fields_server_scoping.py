"""Server-key scoping for the device-field update views, driven by real plugin settings.

``UpdateDeviceNameView.post`` rebinds the LibreNMS client to the POSTed ``server_key``
before resolving the Virtual-Chassis sync device. The sync-device resolution must be
scoped to that same server, otherwise a multi-server VC whose viewed member has no own
``librenms_id`` is renamed from the WRONG LibreNMS device (the server-agnostic
"any member with any id" fallback picks a sibling linked to a different server).

These tests drive the real ``get_librenms_sync_device`` against a real DB Virtual Chassis
and the real ``settings.PLUGINS_CONFIG`` path (via ``override_settings``); only the LibreNMS
HTTP boundary (``LibreNMSAPI.get_librenms_id``) is faked, and it records which device it was
asked to resolve.
"""

import copy
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.test import override_settings

from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.tests.conftest import make_device


from netbox_librenms_plugin.tests.view_test_helpers import post as _post


TWO_SERVERS = {
    "default": {"librenms_url": "https://default.example.com", "api_token": "default-token"},
    "siteB": {"librenms_url": "https://siteb.example.com", "api_token": "siteb-token"},
}


def _plugins_config_with_servers(servers):
    """Return a PLUGINS_CONFIG copy with the plugin's ``servers`` set to ``servers``."""
    config = copy.deepcopy(settings.PLUGINS_CONFIG)
    plugin_config = dict(config.get("netbox_librenms_plugin", {}))
    plugin_config["servers"] = servers
    config["netbox_librenms_plugin"] = plugin_config
    return config


@pytest.mark.django_db
class TestUpdateDeviceNameServerScoping:
    def test_vc_sync_device_resolved_for_posted_server(self):
        """A POST scoped to ``siteB`` must resolve the siteB-linked sibling, not the default one.

        VC layout (viewed member has no own librenms_id):
          * sib_default -> {"default": {"id": 10}}  (host id only on the default server)
          * sib_siteB   -> {"siteB": {"id": 20}}    (host id only on siteB)

        With the bug (no server_key passed), ``get_librenms_sync_device`` runs its
        server-agnostic "any member with any id" order and returns ``sib_default``. Scoped to
        siteB it must return ``sib_siteB``. We record the device handed to ``get_librenms_id``.
        """
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        vc = VirtualChassis.objects.create(name="vcscope")

        # Names/positions chosen so the default-linked sibling iterates first — that is the
        # member the buggy (server-agnostic) resolution would wrongly pick.
        sib_default = make_device("vcscope-a-default")
        sib_default.virtual_chassis = vc
        sib_default.vc_position = 1
        sib_default.custom_field_data["librenms_id"] = {"default": {"id": 10}}
        sib_default.save()

        sib_siteB = make_device("vcscope-b-siteb")
        sib_siteB.virtual_chassis = vc
        sib_siteB.vc_position = 2
        sib_siteB.custom_field_data["librenms_id"] = {"siteB": {"id": 20}}
        sib_siteB.save()

        viewed = make_device("vcscope-c-viewed")
        viewed.virtual_chassis = vc
        viewed.vc_position = 3
        viewed.save()  # no librenms_id -> delegates to the VC sync device

        recorded = {}

        def _capture(device_arg, *args, **kwargs):
            recorded["device_pk"] = getattr(device_arg, "pk", None)
            return None  # bail the view right after resolution; we only assert the target

        view = object.__new__(UpdateDeviceNameView)
        view.require_all_permissions = MagicMock(return_value=None)
        request = MagicMock()
        request.POST = {"server_key": "siteB"}

        with (
            override_settings(PLUGINS_CONFIG=_plugins_config_with_servers(TWO_SERVERS)),
            patch.object(LibreNMSAPI, "get_librenms_id", side_effect=_capture),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect_with_server_key"),
        ):
            _post(view, request, pk=viewed.pk)

        assert recorded["device_pk"] == sib_siteB.pk, (
            "VC sync-device resolution was not scoped to the POSTed server_key "
            f"(expected siteB sibling pk={sib_siteB.pk}, got pk={recorded['device_pk']})"
        )
