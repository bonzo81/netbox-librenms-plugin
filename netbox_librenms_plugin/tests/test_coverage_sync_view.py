"""Coverage tests for views/base/librenms_sync_view.py missing lines."""

from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
)


TEST_SERVERS = {
    "default": {
        "librenms_url": "https://librenms-default.example.com",
        "api_token": "test-token-default",
        "display_name": "Default",
    },
    "secondary": {
        "librenms_url": "https://librenms-secondary.example.com",
        "api_token": "test-token-secondary",
        "display_name": "Secondary",
    },
    "production": {
        "librenms_url": "https://librenms-production.example.com",
        "api_token": "test-token-production",
        "display_name": "Production",
    },
    "prod": {
        "librenms_url": "https://librenms-prod.example.com",
        "api_token": "test-token-prod",
        "display_name": "Prod",
    },
}


def _configure_servers(settings, *server_keys):
    """Configure the requested test LibreNMS servers through Django settings."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        server_key: deepcopy(TEST_SERVERS[server_key]) for server_key in server_keys
    }
    settings.PLUGINS_CONFIG = plugin_config


def _librenms_get(device_name, *, device_id=42, inventory_payload=None, **device_overrides):
    """Return a requests.get side effect for one device and its VC inventory."""
    device_info = {
        "device_id": device_id,
        "hardware": "Test hardware",
        "serial": "TEST-SERIAL",
        "os": "test-os",
        "version": "1.0",
        "features": "-",
        "sysName": device_name,
        "hostname": device_name,
        "ip": "198.18.0.1",
        "location": "Test location",
    }
    device_info.update(device_overrides)
    if inventory_payload is None:
        inventory_payload = {"inventory": []}

    def get(url, **_kwargs):
        response = MagicMock()
        response.status_code = 200
        if "/inventory/" in url:
            response.json.return_value = inventory_payload
        else:
            response.json.return_value = {"status": "ok", "devices": [device_info]}
        return response

    return get


def _make_view():
    """Create a BaseLibreNMSSyncView instance bypassing __init__."""
    from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

    view = object.__new__(BaseLibreNMSSyncView)
    view.request = MagicMock()
    view.tab = "librenms_sync"
    view.model = MagicMock()
    view.queryset = MagicMock()
    view.kwargs = {}
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    view._librenms_api.librenms_url = "https://x.example.com"
    view._librenms_api.cache_timeout = 300
    return view


@pytest.mark.django_db
class TestBaseLibreNMSSyncViewGet:
    """Tests for get() method (lines 29-53)."""

    def test_get_non_vc_device(self, client, settings):
        """Non-VC device: librenms_lookup_device stays as obj."""
        device = make_device("get-non-vc", librenms_cf={"default": 42})
        _configure_servers(settings, "default")
        client.force_login(make_superuser("get-non-vc-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(device.name),
        ):
            response = client.get(url)

        assert response.status_code == 200
        assert response.context["lookup_device_pk"] == device.pk
        assert response.context["librenms_device_id"] == 42

    def test_get_rebinds_header_to_request_server_key(self, client, settings):
        """The page header must rebind to ?server_key so it matches the server the tabs render for."""
        device = make_device(
            "get-secondary-header",
            librenms_cf={"default": 41, "secondary": 42},
        )
        _configure_servers(settings, "default", "secondary")
        client.force_login(make_superuser("get-secondary-header-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(device.name),
        ):
            response = client.get(url, {"server_key": "secondary"})

        assert response.status_code == 200
        assert response.context["server_key"] == "secondary"
        assert response.context["librenms_server_info"]["server_key"] == "secondary"
        assert response.context["librenms_device_id"] == 42

    def test_get_unresolved_server_key_fails_closed(self, client, settings):
        """Stale ?server_key fails closed: no default-server librenms_id, VC delegation skipped."""
        _virtual_chassis, members = make_virtual_chassis_members("get-stale", count=2)
        viewed_member, mapped_member = members
        mapped_member.custom_field_data["librenms_id"] = {"default": 99}
        mapped_member.save(update_fields=["custom_field_data"])
        _configure_servers(settings, "default")
        client.force_login(make_superuser("get-stale-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[viewed_member.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(mapped_member.name, device_id=99),
        ):
            response = client.get(url, {"server_key": "ghost"})

        assert response.status_code == 200
        assert response.context["server_key"] == "ghost"
        assert response.context["lookup_device_pk"] == viewed_member.pk
        assert response.context["librenms_device_id"] is None
        assert response.context["has_librenms_id"] is False
        assert response.context.get("is_vc_member") is None
        assert response.context["all_server_mappings"] is None

    def test_get_vc_member_always_delegates_to_sync_device(self, client, settings):
        """VC member: no own librenms_id - get_librenms_sync_device returns VC primary."""
        _virtual_chassis, members = make_virtual_chassis_members("get-vc-delegate", count=2)
        sync_device, viewed_member = members
        sync_device.custom_field_data["librenms_id"] = {"default": 42}
        sync_device.save(update_fields=["custom_field_data"])
        _configure_servers(settings, "default")
        client.force_login(make_superuser("get-vc-delegate-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[viewed_member.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(sync_device.name),
        ):
            response = client.get(url)

        assert response.status_code == 200
        assert response.context["lookup_device_pk"] == sync_device.pk
        assert response.context["librenms_sync_device"].pk == sync_device.pk
        assert response.context["librenms_device_id"] == 42
        assert response.context["sync_device_has_librenms_id"] is True

    def test_get_vc_member_with_own_librenms_id_uses_itself(self, client, settings):
        """VC member: has own librenms_id - get_librenms_sync_device still called, returns member itself."""
        _virtual_chassis, members = make_virtual_chassis_members("get-vc-own", count=2)
        viewed_member = members[1]
        viewed_member.custom_field_data["librenms_id"] = {"default": 55}
        viewed_member.save(update_fields=["custom_field_data"])
        _configure_servers(settings, "default")
        client.force_login(make_superuser("get-vc-own-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[viewed_member.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(viewed_member.name, device_id=55),
        ):
            response = client.get(url)

        assert response.status_code == 200
        assert response.context["lookup_device_pk"] == viewed_member.pk
        assert response.context["librenms_sync_device"].pk == viewed_member.pk
        assert response.context["librenms_device_id"] == 55

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.render")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView.get_object")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_sync_device")
    def test_get_vc_member_no_sync_device_falls_back_to_obj(self, mock_get_sync, mock_get_obj, mock_render):
        """VC member: when get_librenms_sync_device returns None, keeps obj."""
        view = _make_view()

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()
        mock_get_obj.return_value = obj

        mock_get_sync.return_value = None

        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.get_librenms_id.return_value = 55

        view.get_context_data = MagicMock(return_value={})
        mock_render.return_value = MagicMock()

        request = RequestFactory().get("/")
        view.get(request, pk=1)

        mock_get_sync.assert_called_once_with(obj, server_key="default")
        assert view._librenms_lookup_device is obj


@pytest.mark.django_db
class TestGetContextDataVC:
    """Tests for get_context_data() VC context (lines 69-91)."""

    def test_vc_context_sync_device_has_id_and_ip(self, client, settings):
        """VC device: sync_device_has_librenms_id and sync_device_has_primary_ip set."""
        _virtual_chassis, members = make_virtual_chassis_members("context-vc", count=2)
        sync_device, viewed_member = members
        sync_device.custom_field_data["librenms_id"] = {"default": 42}
        sync_device.save(update_fields=["custom_field_data"])
        interface = make_interface(sync_device, "mgmt0")
        primary_ip = make_ip("198.18.1.1/32", assigned_object=interface)
        sync_device.primary_ip4 = primary_ip
        sync_device.save(update_fields=["primary_ip4"])
        _configure_servers(settings, "default")
        client.force_login(make_superuser("context-vc-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[viewed_member.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(sync_device.name),
        ):
            response = client.get(url)

        assert response.status_code == 200
        assert response.context["is_vc_member"] is True
        assert response.context["librenms_sync_device"].pk == sync_device.pk
        assert response.context["sync_device_has_librenms_id"] is True
        assert response.context["sync_device_has_primary_ip"] is True

    def test_vc_context_sync_device_has_no_id(self):
        """VC device where get_librenms_device_id returns None → sync_device_has_librenms_id is False."""
        view = _make_view()
        view.librenms_id = 42
        view._librenms_lookup_device = MagicMock()

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()
        obj._meta = MagicMock()
        obj._meta.model_name = "device"

        sync_device = MagicMock()
        sync_device.primary_ip = None  # also no IP
        sync_device._meta.model_name = "device"
        sync_device.pk = 10

        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.librenms_url = "https://x.example.com"
        # Explicitly set to None so sync_device_has_librenms_id computes as False
        # (determined by the patched get_librenms_device_id returning None below).
        view._librenms_api.get_librenms_id.return_value = None

        view.get_librenms_device_info = MagicMock(
            return_value={
                "found_in_librenms": False,
                "librenms_device_details": {
                    "librenms_device_serial": "",
                    "librenms_device_hardware": "-",
                    "librenms_device_os": "-",
                    "librenms_device_version": "-",
                    "librenms_device_features": "-",
                    "librenms_device_location": "-",
                    "librenms_device_hardware_match": None,
                    "vc_inventory_serials": [],
                },
                "mismatched_device": False,
            }
        )
        view.get_interface_context = MagicMock(return_value=None)
        view.get_cable_context = MagicMock(return_value=None)
        view.get_ip_context = MagicMock(return_value=None)
        view.get_vlan_context = MagicMock(return_value=None)

        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_sync_device") as mock_sync:
            mock_sync.return_value = sync_device
            with patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_device_id") as mock_id:
                mock_id.return_value = None  # No ID → flag should be False
                with patch(
                    "netbox_librenms_plugin.views.base.librenms_sync_view.get_interface_name_field",
                    return_value="ifName",
                ):
                    with patch(
                        "netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView._build_all_server_mappings",
                        return_value=None,
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.base.librenms_sync_view.BaseLibreNMSSyncView._get_platform_info",
                            return_value={},
                        ):
                            with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV1V2"):
                                with patch("netbox_librenms_plugin.views.base.librenms_sync_view.AddToLIbreSNMPV3"):
                                    with patch("dcim.models.Manufacturer") as MockMfr:
                                        MockMfr.objects.all.return_value.order_by.return_value = []
                                        with patch.object(view, "get_context_data", wraps=view.get_context_data):
                                            with patch(
                                                "netbox_librenms_plugin.views.base.librenms_sync_view.LibreNMSAPIMixin.get_context_data",
                                                return_value={},
                                            ):
                                                ctx = view.get_context_data(MagicMock(), obj)

        assert ctx.get("is_vc_member") is True
        assert ctx.get("sync_device_has_librenms_id") is False
        assert ctx.get("sync_device_has_primary_ip") is False


@pytest.mark.django_db
class TestContextAllTabsPresent:
    """Regression coverage for sync-tab context keys."""

    def test_get_context_data_contains_all_sync_tabs(self, client, settings):
        """Context always exposes all tab keys, including module_sync."""
        device = make_device("all-tab-context", librenms_cf={"default": 42})
        _configure_servers(settings, "default")
        client.force_login(make_superuser("all-tab-context-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(device.name),
        ):
            response = client.get(url)

        assert response.status_code == 200
        for context_name in ("interface_sync", "cable_sync", "ip_sync", "vlan_sync", "module_sync"):
            assert context_name in response.context
            assert response.context[context_name] is not None
            assert response.context[context_name]["object"].pk == device.pk

    def test_get_context_data_exposes_active_server_key(self, client, settings):
        """The active server_key is in context so the create-platform modal forwards it (preserving the server tab on redirect)."""
        device = make_device("ctx-serverkey-dev", librenms_cf={"production": 42})
        _configure_servers(settings, "production")
        client.force_login(make_superuser("ctx-serverkey-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(device.name),
        ):
            response = client.get(url, {"server_key": "production"})

        assert response.status_code == 200
        assert response.context["server_key"] == "production"


@pytest.mark.django_db
class TestModuleContextDefaults:
    """Tests for module-context defaults and concrete overrides."""

    def test_module_sync_is_none_when_not_overridden(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView
        from netbox_librenms_plugin.views.object_sync.vms import VMLibreNMSSyncView

        request = RequestFactory().get("/")
        vm = make_vm("module-context-vm")

        assert BaseLibreNMSSyncView().get_module_context(request, vm) is None
        assert VMLibreNMSSyncView().get_module_context(request, vm) is None

    def test_device_view_module_context_is_non_none(self, client, settings):
        device = make_device("module-context-device", librenms_cf={"default": 42})
        _configure_servers(settings, "default")
        client.force_login(make_superuser("module-context-device-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(device.name),
        ):
            response = client.get(url)

        assert response.status_code == 200
        assert response.context["module_sync"] is not None
        assert response.context["module_sync"]["object"].pk == device.pk


@pytest.mark.django_db
class TestBuildAllServerMappings:
    """Tests for _build_all_server_mappings (lines 181, 193, 200, 207-208)."""

    def test_returns_none_for_non_dict_cf(self, settings):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device("mapping-non-dict", librenms_cf=42)
        _configure_servers(settings, "default")

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result is None

    def test_returns_none_for_empty_dict_cf(self, settings):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device("mapping-empty-dict", librenms_cf={})
        _configure_servers(settings, "default")

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result is None

    def test_valid_dict_cf_returns_list(self, settings):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device(
            "mapping-valid-dict",
            librenms_cf={"default": 42, "secondary": 99},
        )
        _configure_servers(settings, "default", "secondary")

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result is not None
        assert len(result) == 2
        # Active server should be first
        assert result[0]["is_active"] is True
        assert result[0]["server_key"] == "default"

    def test_bool_value_skipped(self, settings):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device(
            "mapping-bool",
            librenms_cf={"default": True, "other": 42},
        )
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {
            "other": {
                "librenms_url": "https://librenms-other.example.com",
                "api_token": "test-token-other",
                "display_name": "Other",
            }
        }
        settings.PLUGINS_CONFIG = plugin_config

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result is not None
        assert len(result) == 1
        assert result[0]["server_key"] == "other"

    def test_oob_only_entry_with_invalid_host_id_is_surfaced(self, settings):
        """An entry whose host "id" is a corrupt non-None value (0) but whose "oob.id" is valid must still surface as an OOB-only mapping so the user can see/remove it, not be dropped by the <=0 guard."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device(
            "mapping-invalid-host-oob",
            librenms_cf={"default": {"id": 0, "oob": {"id": 42}}},
        )
        _configure_servers(settings, "default")

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result is not None
        entry = next((m for m in result if m["server_key"] == "default"), None)
        assert entry is not None  # not dropped despite the invalid host id
        assert entry["is_oob_only"] is True

    def test_string_device_id_converted_to_int(self, settings):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device("mapping-string-id", librenms_cf={"default": "77"})
        _configure_servers(settings, "default")

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result[0]["device_id"] == 77

    def test_non_digit_string_skipped(self, settings):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device(
            "mapping-non-digit-string",
            librenms_cf={"default": "not-a-number"},
        )
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {}
        settings.PLUGINS_CONFIG = plugin_config

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result is None

    def test_legacy_default_key_falls_back_to_root_librenms_url(self, settings):
        """'default' key with no matching servers entry uses root librenms_url."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device("mapping-legacy-default", librenms_cf={"default": 42})
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_settings = plugin_config["netbox_librenms_plugin"]
        plugin_settings["librenms_url"] = "https://librenms-legacy.example.com"
        plugin_settings["display_name"] = "Legacy Server"
        plugin_settings["servers"] = {}
        settings.PLUGINS_CONFIG = plugin_config

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result is not None
        assert result[0]["librenms_url"] == "https://librenms-legacy.example.com"

    def test_malformed_server_config_treated_as_unconfigured(self, settings):
        """Non-dict server config entry → is_configured=False."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device("mapping-malformed-server", librenms_cf={"default": 42})
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {"default": "this-is-not-a-dict"}
        settings.PLUGINS_CONFIG = plugin_config

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result is not None
        assert result[0]["is_configured"] is False

    def test_dict_entry_uses_host_id(self, settings):
        """New dict form {server_key: {"id": N, "oob": {...}}} renders the host id."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device(
            "mapping-dict-host-id",
            librenms_cf={"default": {"id": 42, "oob": {"id": 17, "type": "idrac"}}},
        )
        _configure_servers(settings, "default")

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result is not None
        assert result[0]["device_id"] == 42
        assert result[0]["is_oob_only"] is False

    def test_oob_only_entry_surfaced_with_oob_id(self, settings):
        """An OOB-only entry ({"oob": {...}} with no host id) must still surface so the user can see/remove it; it falls back to the OOB controller's id and is flagged."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device(
            "mapping-oob-only",
            librenms_cf={"default": {"oob": {"id": 17, "type": "idrac"}}},
        )
        _configure_servers(settings, "default")

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result is not None
        assert len(result) == 1
        assert result[0]["device_id"] == 17
        assert result[0]["is_oob_only"] is True
        assert result[0]["device_url"] == "https://librenms-default.example.com/device/device=17/"

    def test_migrated_only_dict_entry_skipped(self, settings):
        """A migrated-only entry ({"_migrated_to": ...}) has neither id nor oob → skipped."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = make_device(
            "mapping-migrated-only",
            librenms_cf={"default": {"_migrated_to": {"device_id": 5}}},
        )
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {}
        settings.PLUGINS_CONFIG = plugin_config

        result = BaseLibreNMSSyncView._build_all_server_mappings(device, "default")

        assert result is None


@pytest.mark.django_db
class TestAllServerMappingsDidValidation:
    """all_server_mappings must skip invalid device IDs in the cf_value dict."""

    def _call(self, obj, active_server_key="default"):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        return BaseLibreNMSSyncView._build_all_server_mappings(obj, active_server_key)

    def test_skips_boolean_did(self, settings):
        device = make_device(
            "did-validation-bool",
            librenms_cf={"default": True, "prod": 42},
        )
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {}
        settings.PLUGINS_CONFIG = plugin_config

        result = self._call(device)

        # Only prod=42 should survive
        assert len(result) == 1
        assert result[0]["device_id"] == 42

    def test_skips_none_did(self, settings):
        device = make_device("did-validation-none", librenms_cf={"default": None})
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {}
        settings.PLUGINS_CONFIG = plugin_config

        result = self._call(device)

        assert result is None  # empty list → returns None

    def test_coerces_digit_string_did(self, settings):
        device = make_device("did-validation-digit-string", librenms_cf={"prod": "99"})
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {}
        settings.PLUGINS_CONFIG = plugin_config

        result = self._call(device)

        assert len(result) == 1
        assert result[0]["device_id"] == 99

    def test_skips_non_digit_string_did(self, settings):
        device = make_device(
            "did-validation-non-digit-string",
            librenms_cf={"default": "bogus"},
        )
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {}
        settings.PLUGINS_CONFIG = plugin_config

        result = self._call(device)

        assert result is None

    def test_valid_int_passes_through(self, settings):
        device = make_device(
            "did-validation-ints",
            librenms_cf={"default": 5, "secondary": 10},
        )
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {}
        settings.PLUGINS_CONFIG = plugin_config

        result = self._call(device)

        assert len(result) == 2
        ids = {e["device_id"] for e in result}
        assert ids == {5, 10}


@pytest.mark.django_db
class TestGetLibreNMSDeviceInfo:
    """Tests for get_librenms_device_info (lines 228+)."""

    def test_no_librenms_id_returns_defaults(self):
        view = _make_view()
        view.librenms_id = None
        view._librenms_api = MagicMock()

        obj = MagicMock()
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is False
        assert result["mismatched_device"] is False

    def test_librenms_id_success_sets_found(self, client, settings):
        device = make_device(
            "device-info-found",
            serial="SN001",
            librenms_cf={"default": 42},
        )
        _configure_servers(settings, "default")
        client.force_login(make_superuser("device-info-found-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(
                device.name,
                hardware="Cisco C9300",
                serial="SN001",
                os="ios",
                version="16.9",
            ),
        ):
            response = client.get(url)

        assert response.status_code == 200
        assert response.context["found_in_librenms"] is True

    def test_mismatched_device_when_names_differ(self, client, settings):
        device = make_device("device-netbox", librenms_cf={"default": 42})
        _configure_servers(settings, "default")
        client.force_login(make_superuser("device-info-mismatch-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(
                "completely-different",
                hardware="-",
                serial="-",
                os="-",
                version="-",
                hostname="also-different.example.com",
                ip="198.18.2.1",
                location="-",
            ),
        ):
            response = client.get(url)

        assert response.status_code == 200
        assert response.context["mismatched_device"] is True


@pytest.mark.django_db
class TestStripVcPattern:
    """Tests for _strip_vc_pattern (lines 378+)."""

    def test_strips_default_pattern(self):
        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        LibreNMSSettings.objects.update_or_create(
            pk=1,
            defaults={"vc_member_name_pattern": "-M{position}"},
        )

        result = BaseLibreNMSSyncView._strip_vc_pattern("switch01-m2")

        assert result == "switch01"

    def test_returns_none_on_exception(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        mock_settings_cls = MagicMock()
        mock_settings_cls.objects.first.side_effect = Exception("DB error")

        with patch("netbox_librenms_plugin.models.LibreNMSSettings", mock_settings_cls, create=True):
            result = BaseLibreNMSSyncView._strip_vc_pattern("some-device")
            assert result is None


@pytest.mark.django_db
class TestLibreNMSIdLegacyDetection:
    """Tests for librenms_id_is_legacy detection (lines 113-115)."""

    def test_bare_int_cf_detected_as_legacy(self, client, settings):
        """bare int CF → librenms_id_is_legacy = True."""
        device = make_device("legacy-id-device", serial="SN", librenms_cf=42)
        _configure_servers(settings, "default")
        client.force_login(make_superuser("legacy-id-device-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(device.name, serial="SN"),
        ):
            response = client.get(url)

        assert response.status_code == 200
        assert response.context["librenms_id_is_legacy"] is True


@pytest.mark.django_db
class TestAbstractMethods:
    """Tests for abstract get_*_context methods (lines 349-376)."""

    def test_get_interface_context_returns_none(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        request = RequestFactory().get("/")
        device = make_device("abstract-interface-context")

        result = BaseLibreNMSSyncView().get_interface_context(request, device)

        assert result is None

    def test_get_cable_context_returns_none(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        request = RequestFactory().get("/")
        device = make_device("abstract-cable-context")

        result = BaseLibreNMSSyncView().get_cable_context(request, device)

        assert result is None

    def test_get_ip_context_returns_none(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        request = RequestFactory().get("/")
        device = make_device("abstract-ip-context")

        result = BaseLibreNMSSyncView().get_ip_context(request, device)

        assert result is None

    def test_get_vlan_context_returns_none(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        request = RequestFactory().get("/")
        device = make_device("abstract-vlan-context")

        result = BaseLibreNMSSyncView().get_vlan_context(request, device)

        assert result is None


@pytest.mark.django_db
class TestGetVCInventorySerials:
    """Tests for _get_vc_inventory_serials (lines 412-452)."""

    def test_no_inventory_returns_empty(self, client, settings):
        _virtual_chassis, members = make_virtual_chassis_members("inventory-empty", count=2)
        sync_device, viewed_member = members
        sync_device.custom_field_data["librenms_id"] = {"default": 42}
        sync_device.save(update_fields=["custom_field_data"])
        _configure_servers(settings, "default")
        client.force_login(make_superuser("inventory-empty-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[viewed_member.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(sync_device.name, inventory_payload={}),
        ):
            response = client.get(url)

        assert response.status_code == 200
        assert response.context["vc_inventory_serials"] == []

    def test_chassis_components_matched(self, client, settings):
        inventory = [
            {
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "SN001",
                "entPhysicalDescr": "Chassis",
                "entPhysicalModelName": "C9300",
            },
            {
                "entPhysicalClass": "module",
                "entPhysicalSerialNum": "SN002",
                "entPhysicalDescr": "Module",
                "entPhysicalModelName": "",
            },
        ]
        _virtual_chassis, members = make_virtual_chassis_members("inventory-match", count=2)
        sync_device, viewed_member = members
        sync_device.serial = "SN001"
        sync_device.custom_field_data["librenms_id"] = {"default": 42}
        sync_device.save(update_fields=["serial", "custom_field_data"])
        _configure_servers(settings, "default")
        client.force_login(make_superuser("inventory-match-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[viewed_member.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(sync_device.name, inventory_payload={"inventory": inventory}),
        ):
            response = client.get(url)

        assert response.status_code == 200
        result = response.context["vc_inventory_serials"]
        assert len(result) == 1
        assert result[0]["serial"] == "SN001"
        assert result[0]["assigned_member"].pk == sync_device.pk

    def test_unassigned_serial_returns_none_member(self, client, settings):
        inventory = [
            {
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "UNKNOWN_SN",
                "entPhysicalDescr": "Chassis",
                "entPhysicalModelName": "MX480",
            },
        ]
        _virtual_chassis, members = make_virtual_chassis_members("inventory-unassigned", count=2)
        sync_device, viewed_member = members
        sync_device.serial = "SN001"
        sync_device.custom_field_data["librenms_id"] = {"default": 42}
        sync_device.save(update_fields=["serial", "custom_field_data"])
        _configure_servers(settings, "default")
        client.force_login(make_superuser("inventory-unassigned-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[viewed_member.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(sync_device.name, inventory_payload={"inventory": inventory}),
        ):
            response = client.get(url)

        assert response.status_code == 200
        result = response.context["vc_inventory_serials"]
        assert len(result) == 1
        assert result[0]["assigned_member"] is None

    def test_empty_serial_skipped(self, client, settings):
        inventory = [
            {
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "-",
                "entPhysicalDescr": "Chassis",
                "entPhysicalModelName": "",
            },
        ]
        _virtual_chassis, members = make_virtual_chassis_members("inventory-no-serial", count=2)
        sync_device, viewed_member = members
        sync_device.custom_field_data["librenms_id"] = {"default": 42}
        sync_device.save(update_fields=["custom_field_data"])
        _configure_servers(settings, "default")
        client.force_login(make_superuser("inventory-no-serial-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[viewed_member.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(sync_device.name, inventory_payload={"inventory": inventory}),
        ):
            response = client.get(url)

        assert response.status_code == 200
        assert response.context["vc_inventory_serials"] == []


@pytest.mark.django_db
class TestGetPlatformInfo:
    """Tests for _get_platform_info (lines 463-502)."""

    def test_no_os_returns_no_platform(self, client, settings):
        device = make_device("platform-no-os", librenms_cf={"default": 42})
        _configure_servers(settings, "default")
        client.force_login(make_superuser("platform-no-os-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(device.name, os="-", version="-"),
        ):
            response = client.get(url)

        assert response.status_code == 200
        platform_info = response.context["platform_info"]
        assert platform_info["platform_exists"] is False
        assert platform_info["platform_name"] is None

    def test_matching_platform_found(self, client, settings):
        from dcim.models import Platform

        platform, _created = Platform.objects.get_or_create(name="ios", defaults={"slug": "ios"})
        device = make_device("platform-match", librenms_cf={"default": 42})
        _configure_servers(settings, "default")
        client.force_login(make_superuser("platform-match-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(device.name, os="ios", version="16.9"),
        ):
            response = client.get(url)

        assert response.status_code == 200
        platform_info = response.context["platform_info"]
        assert platform_info["platform_exists"] is True
        assert platform_info["matching_platform"].pk == platform.pk

    def test_platform_does_not_exist(self, client, settings):
        device = make_device("platform-missing", librenms_cf={"default": 42})
        _configure_servers(settings, "default")
        client.force_login(make_superuser("platform-missing-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(
                device.name,
                os="missing-test-os",
                version="4.28",
            ),
        ):
            response = client.get(url)

        assert response.status_code == 200
        platform_info = response.context["platform_info"]
        assert platform_info["platform_exists"] is False
        assert platform_info["matching_platform"] is None


@pytest.mark.django_db
class TestInterfaceSyncRefreshButtonServerKey:
    """The 'Refresh Interfaces' button must carry the active server_key — via the hidden input the enclosing form emits (htmx includes form values on non-GET) plus the button's own hx-vals context fallback — so a non-default server tab refreshes from the right LibreNMS server/cache, not the fallback."""

    def test_refresh_button_includes_server_key(self, client, settings):
        device = make_device("ifsync-refresh-dev", librenms_cf={"prod": 42})
        _configure_servers(settings, "prod")
        client.force_login(make_superuser("ifsync-refresh-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=_librenms_get(device.name),
        ):
            response = client.get(url, {"server_key": "prod"})

        html = response.content.decode()
        assert response.status_code == 200
        # The refresh POST carries the active server key two ways: the hidden input the enclosing
        # form emits (htmx includes form values on non-GET requests) and the button's own hx-vals
        # context fallback. parent-child moved server_key OUT of the button's hx-include into hx-vals,
        # but the hidden input the form relies on stays.
        assert '<input type="hidden" name="server_key" value="prod">' in html
        assert "get('server_key') || 'prod'" in html


@pytest.mark.django_db
class TestFullPageMigratedContextServerScope:
    """The full-page migrated banner must use the resolved render key, not the global default."""

    def test_stale_server_key_builds_migrated_banner_under_requested_key(self, client, settings):
        """A marker under a removed server still renders through the real request path."""
        winner = make_device("f4-winner")
        donor = make_device("f4-donor")
        # The migration marker lives under a NON-default server that is now stale/deleted.
        donor.custom_field_data["librenms_id"] = {
            "edgelondon": {"_migrated_to": {"device_id": winner.pk, "server_key": "edgelondon", "at": "x"}}
        }
        donor.save()

        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {
            "default": {
                "librenms_url": "https://default.example.com",
                "api_token": "test-token",
            }
        }
        settings.PLUGINS_CONFIG = plugin_config
        client.force_login(make_superuser("stale-key-su"))
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[donor.pk])

        response = client.get(url, {"server_key": "edgelondon"})

        assert response.status_code == 200
        assert response.context["migrated_to_marker"]
        assert response.context["migrated_to_winner"].pk == winner.pk
