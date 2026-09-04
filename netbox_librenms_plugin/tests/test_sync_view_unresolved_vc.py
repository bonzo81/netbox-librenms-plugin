"""Unresolved ?server_key must fail closed in the VC-status context, not leak the default server.

``BaseLibreNMSSyncView.get`` fails closed on an unresolved ``?server_key`` (a decommissioned /
misconfigured server): the rebind declines, the client stays on the default server, and
``self.librenms_id`` is forced to ``None`` so the header renders "not found in LibreNMS". But
``get_context_data`` recomputes the Virtual-Chassis sync-device linkage from
``self.librenms_api.server_key`` — which, on an unresolved key, is the DEFAULT server. Without a
guard it reports the member as linked to a valid sync device on the (gone) server, contradicting
the failed-closed header and inviting a sync against a server that no longer exists.

This drives a real authenticated request against a real DB Virtual Chassis and real single-server
``settings.PLUGINS_CONFIG``. The rendered response context shows whether default-server linkage
leaked into the unresolved-server page.
"""

import copy

import pytest
from dcim.models import Device

from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
from django.conf import settings
from django.test import override_settings
from django.urls import reverse

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
    def _vc_member(self, name):
        """Build a VC member carrying a real, valid host id on the DEFAULT server.

        On an unresolved ``?server_key`` the client stays bound to "default", so an
        un-guarded VC block would resolve *this* linkage and leak it.
        """
        from dcim.models import VirtualChassis

        vc = VirtualChassis.objects.create(name=f"{name}-vc")
        member = make_device(f"{name}-m1")
        member.virtual_chassis = vc
        member.vc_position = 1
        member.custom_field_data["librenms_id"] = {"default": {"id": 55}}
        member.save()
        return member

    def _get(self, client, member, server_key):
        """Render the sync page for *member* under ``?server_key=<server_key>``."""
        user = make_user_with_perms(f"vc-viewer-{server_key}", [("view", Device)])
        client.force_login(user)
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[member.pk])
        with override_settings(PLUGINS_CONFIG=_plugins_config_with_servers(DEFAULT_ONLY)):
            return client.get(url, {"server_key": server_key})

    def test_resolved_server_key_reports_the_vc_linkage(self, client):
        """Positive control: the same page on a RESOLVED key must publish the VC linkage.

        Without it the unresolved assertion below passes even if the VC block stopped
        setting the key for every request.
        """
        member = self._vc_member("resolved-control")

        response = self._get(client, member, "default")

        assert response.status_code == 200
        assert response.context["sync_device_has_librenms_id"] is True

    def test_unresolved_server_key_does_not_leak_default_vc_linkage(self, client):
        member = self._vc_member("unresolved-leak")

        response = self._get(client, member, "ghost")

        assert response.status_code == 200
        ctx = response.context
        # Sanity: the header failed closed (unresolved -> librenms_id None).
        assert ctx.get("has_librenms_id") is False
        # The bug: the VC-status block leaks the default server's linkage on an unresolved key.
        assert "sync_device_has_librenms_id" not in ctx, (
            "Unresolved ?server_key leaked the default server's VC sync-device linkage "
            "(get_context_data VC block ran without the unresolved guard)"
        )
