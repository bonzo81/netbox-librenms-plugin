"""Behavior tests for VLAN parsing, presentation, and the real sync template."""

from copy import deepcopy

import pytest


def _api(settings):
    """Build a real client from a local-only configuration."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "default": {
            "librenms_url": "http://127.0.0.1:9",
            "api_token": "test-token",
            "verify_ssl": False,
        }
    }
    settings.PLUGINS_CONFIG = plugin_config
    return LibreNMSAPI(server_key="default")


class TestVLANModeDetection:
    """Exercise the production parser with representative LibreNMS rows."""

    @pytest.mark.parametrize(
        ("port", "expected"),
        [
            (
                {"port_id": 1, "ifName": "Gi1/0/1", "ifVlan": "50", "ifTrunk": None},
                {"mode": "access", "untagged_vlan": 50, "tagged_vlans": []},
            ),
            (
                {
                    "port_id": 2,
                    "ifName": "Te1/1/1",
                    "ifVlan": "90",
                    "ifTrunk": "dot1Q",
                    "vlans": [
                        {"vlan": 90, "untagged": 1},
                        {"vlan": 50, "untagged": 0},
                        {"vlan": 60, "untagged": 0},
                    ],
                },
                {"mode": "tagged", "untagged_vlan": 90, "tagged_vlans": [50, 60]},
            ),
            (
                {"port_id": 3, "ifName": "Gi1/0/48", "ifVlan": "", "ifTrunk": None},
                {"mode": None, "untagged_vlan": None, "tagged_vlans": []},
            ),
        ],
    )
    def test_parse_port_vlan_data(self, settings, port, expected):
        result = _api(settings).parse_port_vlan_data(port)

        assert result["mode"] == expected["mode"]
        assert result["untagged_vlan"] == expected["untagged_vlan"]
        assert result["tagged_vlans"] == expected["tagged_vlans"]

    def test_non_dict_vlan_entries_are_ignored(self, settings):
        port = {
            "port_id": 4,
            "ifName": "Gi0/0",
            "ifTrunk": "dot1Q",
            "ifVlan": None,
            "vlans": [{"vlan": 10, "untagged": 1}, "invalid", {"vlan": 20}],
        }

        result = _api(settings).parse_port_vlan_data(port)

        assert result["untagged_vlan"] == 10
        assert result["tagged_vlans"] == [20]


class TestGetVlanSyncCssClass:
    @pytest.mark.parametrize(
        ("exists", "name_matches", "expected"),
        [
            (False, True, "text-danger"),
            (False, False, "text-danger"),
            (True, True, "text-success"),
            (True, False, "text-warning"),
        ],
    )
    def test_status_class(self, exists, name_matches, expected):
        from netbox_librenms_plugin.utils import get_vlan_sync_css_class

        assert get_vlan_sync_css_class(exists_in_netbox=exists, name_matches=name_matches) == expected


@pytest.mark.django_db
class TestVLANErrorContextServerKey:
    """The error fragment keeps an explicit server scope."""

    @staticmethod
    def _view(settings):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

        view = DeviceVLANTableView()
        view._librenms_api = _api(settings)
        return view

    def test_explicit_none_is_preserved(self, settings):
        from netbox_librenms_plugin.tests.conftest import make_device

        context = self._view(settings)._get_error_context(make_device("vlan-error-none"), "error", server_key=None)

        assert context["server_key"] is None

    def test_omitted_key_uses_active_server(self, settings):
        from netbox_librenms_plugin.tests.conftest import make_device

        context = self._view(settings)._get_error_context(make_device("vlan-error-default"), "error")

        assert context["server_key"] == "default"


@pytest.mark.django_db
class TestVlanSyncContentTemplateMigratedMode:
    """Render the real VLAN sync fragment in live and migrated modes."""

    @staticmethod
    def _render(*, migrated, server_key="default"):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device(f"vlan-template-{server_key}-{bool(migrated)}")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        table = LibreNMSVLANTable([{"vlan_id": 10, "name": "v10", "type": "tagged", "state": "active"}])
        RequestConfig(request).configure(table)
        return render_to_string(
            "netbox_librenms_plugin/_vlan_sync_content.html",
            {
                "vlan_sync": {
                    "object": device,
                    "vlan_table": table,
                    "server_key": server_key,
                    "error_message": None,
                    "cache_expiry": None,
                },
                "migrated_to_marker": migrated,
            },
            request=request,
        )

    def test_migrated_mode_removes_sync_form(self):
        html = self._render(migrated={"server_key": "prod", "device_id": 1, "at": "now"}, server_key="prod")

        assert "<form" not in html
        assert "csrfmiddlewaretoken" in html
        assert 'name="server_key"' in html
        assert 'value="prod"' in html
        assert 'name="action"' not in html

    def test_live_mode_renders_sync_form(self):
        html = self._render(migrated=False)

        assert "<form" in html
        assert "csrfmiddlewaretoken" in html
        assert 'name="action"' in html
        assert 'name="server_key"' in html


@pytest.mark.django_db
@pytest.mark.parametrize("endpoint", ["verify_vlan_group", "verify_vlan_sync_group"])
@pytest.mark.parametrize("group_id", [True, False, 1.5, 0, -1, 10**40, [], {}])
def test_vlan_verification_rejects_malformed_group_ids(client, endpoint, group_id):
    """JSON values must not become a different group or an implicit global selection."""
    from django.urls import reverse
    from netbox_librenms_plugin.tests.conftest import make_device, make_superuser

    device = make_device("invalid-vlan-group")
    client.force_login(make_superuser())
    response = client.post(
        reverse(f"plugins:netbox_librenms_plugin:{endpoint}"),
        {
            "device_id": device.pk,
            "interface_name": "eth0",
            "vid": 100,
            "vlan_group_id": group_id,
        },
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.json()["message"] == "Invalid VLAN group ID"
