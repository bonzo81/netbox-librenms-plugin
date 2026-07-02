"""
Regression tests for reviewer-requested fixes.

Covers: _load_vc_member_name_pattern validation, _generate_vc_member_name pattern
handling, _normalize_librenms_mapping guards, all_server_mappings did validation,
render_device_selection XSS escape, SingleCableVerifyView server_key from POST,
import_single_device lazy validation api passthrough, CreateAndAssignPlatformView
full_clean before save.
"""

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _load_vc_member_name_pattern
# ---------------------------------------------------------------------------
class TestLoadVcMemberNamePattern:
    """_load_vc_member_name_pattern must return valid string or default."""

    DEFAULT = "-M{position}"

    def _call(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _load_vc_member_name_pattern

        return _load_vc_member_name_pattern()

    def _patch_settings(self, settings_obj):
        """Patch the deferred import of LibreNMSSettings inside the function."""
        return patch(
            "netbox_librenms_plugin.models.LibreNMSSettings.objects",
            **{"order_by.return_value.first.return_value": settings_obj},
        )

    def test_returns_valid_pattern(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = "-SW{position}"
        with self._patch_settings(settings):
            assert self._call() == "-SW{position}"

    def test_returns_default_for_none_pattern(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = None
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_for_empty_string(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = ""
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_for_whitespace_only(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = "   "
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_for_boolean(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = True
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_when_no_settings(self):
        with self._patch_settings(None):
            assert self._call() == self.DEFAULT

    def test_returns_default_on_exception(self):
        with patch(
            "netbox_librenms_plugin.models.LibreNMSSettings.objects",
        ) as mock_objs:
            mock_objs.order_by.side_effect = RuntimeError("db error")
            assert self._call() == self.DEFAULT


# ---------------------------------------------------------------------------
# _normalize_librenms_mapping
# ---------------------------------------------------------------------------
class TestNormalizeLibreNMSMapping:
    """_normalize_librenms_mapping must reject booleans and non-digit strings."""

    def _call(self, value):
        # Instantiate the view class minimally to access the method
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        view = object.__new__(RemoveServerMappingView)
        return view._normalize_librenms_mapping(value)

    def test_int_becomes_default_dict(self):
        assert self._call(42) == {"default": 42}

    def test_bool_true_returns_empty(self):
        assert self._call(True) == {}

    def test_bool_false_returns_empty(self):
        assert self._call(False) == {}

    def test_digit_string_coerced(self):
        assert self._call("42") == {"default": 42}

    def test_non_digit_string_returns_empty(self):
        assert self._call("not-a-number") == {}

    def test_plus_prefix_rejected(self):
        """'+1' is not strictly digit-only."""
        assert self._call("+1") == {}

    def test_space_padded_rejected(self):
        """' 42 ' is not strictly digit-only."""
        assert self._call(" 42 ") == {}

    def test_dict_passed_through(self):
        d = {"production": 7}
        assert self._call(d) is d

    def test_none_returns_empty(self):
        assert self._call(None) == {}

    def test_list_returns_empty(self):
        assert self._call([1, 2]) == {}


# ---------------------------------------------------------------------------
# all_server_mappings — did validation
# ---------------------------------------------------------------------------
class TestAllServerMappingsDidValidation:
    """all_server_mappings must skip invalid device IDs in the cf_value dict."""

    def _call(self, obj, active_server_key="default"):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        return BaseLibreNMSSyncView._build_all_server_mappings(obj, active_server_key)

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_skips_boolean_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": True, "prod": 42}}
        result = self._call(obj)
        # Only prod=42 should survive
        assert len(result) == 1
        assert result[0]["device_id"] == 42

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_skips_none_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": None}}
        result = self._call(obj)
        assert result is None  # empty list → returns None

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_coerces_digit_string_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"prod": "99"}}
        result = self._call(obj)
        assert len(result) == 1
        assert result[0]["device_id"] == 99

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_skips_non_digit_string_did(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": "bogus"}}
        result = self._call(obj)
        assert result is None

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings")
    def test_valid_int_passes_through(self, mock_settings):
        mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": 5, "secondary": 10}}
        result = self._call(obj)
        assert len(result) == 2
        ids = {e["device_id"] for e in result}
        assert ids == {5, 10}


# ---------------------------------------------------------------------------
# render_device_selection — XSS escape
# ---------------------------------------------------------------------------
class TestRenderDeviceSelectionEscape:
    """render_device_selection must HTML-escape member.name."""

    def test_member_name_is_escaped(self):
        from netbox_librenms_plugin.tables.cables import VCCableTable

        device = MagicMock()
        device.id = 1
        vc = MagicMock()
        member = MagicMock()
        member.id = 1
        member.name = '<script>alert("xss")</script>'
        vc.members.all.return_value = [member]
        device.virtual_chassis = vc

        # The dropdown options render from the member set cached in __init__.
        table = VCCableTable([], device=device)
        record = {"local_port": "eth0", "local_port_id": "42"}
        html = str(table.render_device_selection(None, record))

        # The raw <script> tag must NOT appear — it should be escaped
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# _generate_vc_member_name — pattern handling
# ---------------------------------------------------------------------------
class TestGenerateVcMemberName:
    """_generate_vc_member_name must respect caller-supplied pattern and catch format errors."""

    def _call(self, master_name, position, serial=None, pattern=None):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        return _generate_vc_member_name(master_name, position, serial=serial, pattern=pattern)

    def test_explicit_pattern_used(self):
        """When pattern is passed, it should be used directly (no DB query)."""
        result = self._call("switch01", 2, pattern="-SW{position}")
        assert result == "switch01-SW2"

    def test_serial_in_pattern(self):
        result = self._call("switch01", 2, serial="ABC123", pattern=" [{serial}]")
        assert result == "switch01 [ABC123]"

    def test_none_pattern_loads_from_settings(self):
        """When pattern is None, _load_vc_member_name_pattern is called."""
        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-STACK{position}",
        ):
            result = self._call("core01", 3, pattern=None)
        assert result == "core01-STACK3"

    def test_malformed_pattern_falls_back_to_default(self):
        """Invalid format spec falls back to -M{position}."""
        result = self._call("switch01", 2, pattern="{position!z}")
        assert result == "switch01-M2"

    def test_missing_key_falls_back_to_default(self):
        """Unknown placeholder falls back to -M{position}."""
        result = self._call("switch01", 2, pattern="-{unknown_key}")
        assert result == "switch01-M2"

    def test_default_pattern(self):
        result = self._call("switch01", 2, pattern="-M{position}")
        assert result == "switch01-M2"


# ---------------------------------------------------------------------------
# SingleCableVerifyView — server_key from POST body
# ---------------------------------------------------------------------------
class TestSingleCableVerifyServerKey:
    """SingleCableVerifyView.post() must thread server_key from the POST body into VC resolution + cache key."""

    @staticmethod
    def _vc_device(tag):
        """A real Device that belongs to a VirtualChassis (so post() takes the VC sync-resolution branch)."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site, VirtualChassis

        mfr, _ = Manufacturer.objects.get_or_create(name=f"SkMfr-{tag}", slug=f"skmfr-{tag}")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=f"SkDT-{tag}", slug=f"skdt-{tag}")
        role, _ = DeviceRole.objects.get_or_create(name="SkRole", slug="skrole")
        site, _ = Site.objects.get_or_create(name="SkSite", slug="sksite")
        vc = VirtualChassis.objects.create(name=f"SkVC-{tag}")
        return Device.objects.create(
            name=f"sk-{tag}", device_type=dt, role=role, site=site, status="active", virtual_chassis=vc, vc_position=1
        )

    @staticmethod
    def _view_and_request(device, body, *, api_server_key):
        """Real view + real superuser request; _librenms_api is stubbed only to supply the active-server key."""
        import json
        from unittest.mock import MagicMock

        from django.contrib.auth import get_user_model
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = SingleCableVerifyView()
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = api_server_key  # config boundary: the active-server fallback
        request = RequestFactory().post("/verify-cable/", data=json.dumps(body), content_type="application/json")
        request.user = get_user_model().objects.create_superuser(username=f"sk-{device.pk}", email="", password="x")
        view.request = request
        view.kwargs = {}
        view.args = ()
        return view, request

    @pytest.mark.django_db
    def test_server_key_used_for_cache_lookup(self):
        """The POSTed server_key is threaded into get_librenms_sync_device and the (real) cache key."""
        from unittest.mock import patch

        device = self._vc_device("used")
        view, request = self._view_and_request(
            device,
            {"device_id": device.pk, "local_port_id": "42", "server_key": "production"},
            api_server_key="default-server",
        )

        with (
            # The posted key is honoured only when it names a configured server; post() checks the
            # LibreNMSAPI.get_available_servers() CLASSMETHOD (not the instance).
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"production": "Production"},
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=device
            ) as mock_sync_device,
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
        ):
            mock_cache.get.return_value = None  # no cached data -> early return once the key is built
            view.post(request)

            # get_librenms_sync_device gets the posted server_key (device compares by pk via Model.__eq__)
            mock_sync_device.assert_called_once_with(device, server_key="production")
            # the real cache key also carries the posted server_key (not the active default)
            cache_key_arg = mock_cache.get.call_args[0][0]
            assert "production" in cache_key_arg

    @pytest.mark.django_db
    def test_fallback_to_api_server_key(self):
        """With no server_key in the POST body, post() falls back to the active-server key."""
        from unittest.mock import patch

        device = self._vc_device("fallback")
        view, request = self._view_and_request(
            device, {"device_id": device.pk, "local_port_id": "42"}, api_server_key="fallback-server"
        )

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                side_effect=lambda dev, **kw: dev,
            ) as mock_sync_device,
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            view.post(request)

            mock_sync_device.assert_called_once()
            assert mock_sync_device.call_args[1]["server_key"] == "fallback-server"
            cache_key_arg = mock_cache.get.call_args[0][0]
            assert "fallback-server" in cache_key_arg


# ---------------------------------------------------------------------------
# import_single_device — lazy validation passes api
# ---------------------------------------------------------------------------
class TestImportSingleDeviceLazyValidation:
    """import_single_device must pass api=api to validate_device_for_import when validation is None."""

    def test_api_passed_to_validate(self):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        mock_api = MagicMock()
        mock_api.server_key = "prod"

        mock_validation = {
            "existing_device": MagicMock(name="existing"),
            "can_import": False,
        }

        with (
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI",
                return_value=mock_api,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.validate_device_for_import",
                return_value=mock_validation,
            ) as mock_validate,
        ):
            # Call with validation=None so lazy path triggers
            import_single_device(
                42,
                server_key="prod",
                sync_options={"use_sysname": True, "strip_domain": False},
                validation=None,
                libre_device={"device_id": 42, "hostname": "test"},
            )

            mock_validate.assert_called_once()
            # api must be passed as keyword arg
            assert mock_validate.call_args[1].get("api") is mock_api


# ---------------------------------------------------------------------------
# CreateAndAssignPlatformView — full_clean before save
# ---------------------------------------------------------------------------
class TestCreatePlatformFullClean:
    """CreateAndAssignPlatformView must surface a real Platform.full_clean() ValidationError, not 500."""

    @staticmethod
    def _make_device(name):
        """Create a minimal real Device (this branch predates the conftest real-DB builders)."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        site, _ = Site.objects.get_or_create(name="PFTestSite", slug="pf-test-site")
        mfr, _ = Manufacturer.objects.get_or_create(name="PFTestMfr", slug="pf-test-mfr")
        dtype, _ = DeviceType.objects.get_or_create(model="PFTestDT", slug="pf-test-dt", defaults={"manufacturer": mfr})
        role, _ = DeviceRole.objects.get_or_create(name="PFTestRole", slug="pf-test-role", defaults={"color": "00ff00"})
        return Device.objects.create(name=name, device_type=dtype, role=role, site=site, status="active")

    @pytest.mark.django_db
    def test_real_slug_collision_is_caught_and_reported(self):
        """A real slug collision (different name, same slugify) is rejected by Platform.full_clean() and reported, not 500'd."""
        from dcim.models import Platform

        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        device = self._make_device("platform-collision-dev")
        # An existing platform already owns the slug "test-platform".
        Platform.objects.create(name="Existing Platform", slug="test-platform")

        view = object.__new__(CreateAndAssignPlatformView)
        view.require_all_permissions = MagicMock(return_value=None)  # the permission boundary
        request = MagicMock()
        request.method = "POST"
        # A DIFFERENT name that slugifies to the SAME slug: passes the name-exists short-circuit
        # but trips the real Platform.full_clean() unique-slug validation.
        request.POST = {"platform_name": "Test Platform"}
        view.request = request

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect", return_value="redirected"),
        ):
            result = view.post(request, pk=device.pk)

        # The real full_clean() rejected the duplicate slug — no second platform was created...
        assert Platform.objects.filter(slug="test-platform").count() == 1
        assert not Platform.objects.filter(name="Test Platform").exists()
        # ...and the device's platform was left unset (the create rolled back before assignment).
        device.refresh_from_db()
        assert device.platform_id is None
        # The user is shown the caught error, not a 500.
        assert result == "redirected"
        mock_messages.error.assert_called_once()
        assert "could not be created" in mock_messages.error.call_args[0][1]
