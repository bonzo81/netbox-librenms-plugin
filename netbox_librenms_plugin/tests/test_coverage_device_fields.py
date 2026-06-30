"""Coverage tests for views/sync/device_fields.py (target >95%).

The field-update views (name/serial/type/platform) persist through full_clean() + save();
the DB-touching tests run against REAL Devices (only the LibreNMS API — get_librenms_id /
get_device_info — and the messages/redirect framework seams are mocked), so the persisted
field is verified by reloading from the DB rather than asserting on a MagicMock whose
.save() is a no-op. A genuine duplicate-name conflict produces a REAL ValidationError.
"""

from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.tests.conftest import make_device

@pytest.mark.parametrize(
    "view_name",
    ["UpdateDeviceNameView", "UpdateDeviceSerialView", "UpdateDeviceTypeView", "UpdateDevicePlatformView"],
)
def test_write_views_fetch_device_info_live_not_cached(view_name):
    """The device-field write views must fetch LIVE LibreNMS info (use_cache=False) so a stale sync-tab cache snapshot is never persisted into NetBox."""
    import netbox_librenms_plugin.views.sync.device_fields as df

    view = _make_view(getattr(df, view_name))
    view._librenms_api.get_librenms_id.return_value = 42
    view._librenms_api.get_device_info.return_value = (False, None)  # short-circuit right after the call
    mock_device = MagicMock()
    mock_device.virtual_chassis = None
    with (
        patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
        patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
        patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
    ):
        view.post(_make_request(), pk=1)
    # Unfixed: called as get_device_info(42) → use_cache defaults True → reads the stale render cache.
    assert view._librenms_api.get_device_info.call_args.kwargs.get("use_cache") is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_view(ViewClass):
    """Create a view instance bypassing __init__, with a mock LibreNMS API."""
    view = object.__new__(ViewClass)
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    view.require_all_permissions = MagicMock(return_value=None)
    return view


def _make_request(post_data=None):
    req = MagicMock()
    req.POST = post_data or {}
    return req


# ---------------------------------------------------------------------------
# UpdateDeviceNameView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateDeviceNameView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        return _make_view(UpdateDeviceNameView)

    def test_permission_denied_returns_error(self):
        view = self._view()
        error_response = MagicMock()
        view.require_all_permissions = MagicMock(return_value=error_response)

        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404") as mock_get:
            result = view.post(_make_request(), pk=1)

        assert result is error_response
        mock_get.assert_not_called()

    def test_no_librenms_id_returns_error(self):
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None
        dev = make_device("name-noid")

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir,
        ):
            view.post(_make_request(), pk=dev.pk)

        mock_msg.error.assert_called_once()
        mock_redir.assert_called_once()
        assert Device.objects.get(pk=dev.pk).name == "name-noid"  # unchanged

    def test_get_device_info_failure(self):
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (False, None)
        dev = make_device("name-infofail")

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)

        mock_msg.error.assert_called_once()
        assert Device.objects.get(pk=dev.pk).name == "name-infofail"

    def test_get_device_info_empty_dict(self):
        """An empty (falsy) device_info dict triggers the 'Failed to retrieve' error path."""
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {})
        dev = make_device("name-emptyinfo")

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)

        # empty dict is falsy → triggers "Failed to retrieve device info" error
        mock_msg.error.assert_called_once()

    def test_no_sysname_returns_warning(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {"sysName": None, "hostname": None})
        dev = make_device("name-nosysname")

        with (
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.resolve_naming_preferences", return_value=(True, False)
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)

        mock_msg.warning.assert_called_once()

    def test_save_success_persists_name(self):
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {"sysName": "router1-renamed"})
        dev = make_device("old-name-ok")

        with (
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.resolve_naming_preferences",
                return_value=(True, False),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)

        # Real full_clean + save committed the rename — reload from the DB.
        assert Device.objects.get(pk=dev.pk).name == "router1-renamed"
        mock_msg.success.assert_called_once()

    def test_save_validation_error_real_duplicate_name_is_rolled_back(self):
        """A real duplicate name (same site) makes full_clean() raise ValidationError; the view restores the old name and persists nothing."""
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {"sysName": "taken-name"})
        make_device("taken-name")  # occupies the target name in the shared site
        dev = make_device("orig-name")

        with (
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.resolve_naming_preferences",
                return_value=(True, False),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)

        mock_msg.error.assert_called_once()
        assert Device.objects.get(pk=dev.pk).name == "orig-name"  # unchanged

    def test_save_integrity_error_restores_name(self):
        """If save() raises IntegrityError (past full_clean), the in-memory name is restored and an error is surfaced."""
        from django.db import IntegrityError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {"sysName": "router1-int"})
        dev = make_device("orig-int")
        dev.save = MagicMock(side_effect=IntegrityError("duplicate key"))  # inject the save failure

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.resolve_naming_preferences",
                return_value=(True, False),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)

        mock_msg.error.assert_called_once()
        assert dev.name == "orig-int"  # restored after the failed save


# ---------------------------------------------------------------------------
# UpdateDeviceSerialView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateDeviceSerialView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView

        return _make_view(UpdateDeviceSerialView)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)

        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404") as mock_get:
            result = view.post(_make_request(), pk=1)
        assert result is err
        mock_get.assert_not_called()

    def test_no_librenms_id(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_get_device_info_failure(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (False, None)

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_serial_is_none(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": None})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.warning.assert_called_once()

    def test_serial_is_dash(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "-"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.warning.assert_called_once()

    def test_save_success_with_old_serial(self):
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN001"})
        dev = make_device("serial-old", serial="OLDSERIAL")

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert "OLDSERIAL" in mock_msg.success.call_args[0][1]
        assert Device.objects.get(pk=dev.pk).serial == "SN001"  # real save committed

    def test_save_success_no_old_serial(self):
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN001"})
        dev = make_device("serial-none", serial="")

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert "set to" in mock_msg.success.call_args[0][1]
        assert Device.objects.get(pk=dev.pk).serial == "SN001"

    def test_save_validation_error_restores_serial(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN001"})
        # Real device; inject a full_clean failure (a serial constraint is hard to trip naturally)
        # while keeping the device real so the restore + DB-untouched behaviour is exercised.
        dev = make_device("serial-valerr", serial="OLD")
        dev.full_clean = MagicMock(side_effect=ValidationError({"serial": ["err"]}))
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)
        mock_msg.error.assert_called_once()
        assert dev.serial == "OLD"  # restored in memory
        from dcim.models import Device

        assert Device.objects.get(pk=dev.pk).serial == "OLD"  # nothing persisted

    def test_save_integrity_error(self):
        from django.db import IntegrityError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN001"})
        dev = make_device("serial-interr", serial="OLD")
        # Raise from save(), not full_clean(): full_clean runs first, so mocking it would
        # exercise the validation branch and let a regression in the actual save-failure
        # handling pass unnoticed. This test must cover the save IntegrityError path.
        dev.save = MagicMock(side_effect=IntegrityError("dup"))
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)
        mock_msg.error.assert_called_once()


# ---------------------------------------------------------------------------
# UpdateDeviceTypeView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateDeviceTypeView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceTypeView

        return _make_view(UpdateDeviceTypeView)

    @staticmethod
    def _new_device_type():
        from dcim.models import DeviceType, Manufacturer

        mfr, _ = Manufacturer.objects.get_or_create(name="TestMfr", slug="test-mfr")
        return DeviceType.objects.create(model="Cisco3750", slug="cisco3750", manufacturer=mfr)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404") as mock_get:
            result = view.post(_make_request(), pk=1)
        assert result is err
        mock_get.assert_not_called()

    def test_no_librenms_id(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_get_device_info_failure(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (False, None)
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_no_hardware(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": None})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.warning.assert_called_once()

    def test_no_match_result(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value={"matched": False},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_match_none_returns_ambiguous_error(self):
        """match_librenms_hardware_to_device_type returns None → ambiguous-match error path."""
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        mock_device = MagicMock()
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value=None,
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()
        assert "Ambiguous" in mock_msg.error.call_args[0][1]
        mock_device.full_clean.assert_not_called()
        mock_device.save.assert_not_called()

    def test_save_success_persists_device_type(self):
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        dev = make_device("type-ok")
        new_dt = self._new_device_type()
        with (
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value={"matched": True, "device_type": new_dt},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert Device.objects.get(pk=dev.pk).device_type_id == new_dt.pk  # real save committed

    def test_save_validation_error_leaves_type_unchanged(self):
        from django.core.exceptions import ValidationError

        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        dev = make_device("type-valerr")
        original_dt = dev.device_type_id
        new_dt = self._new_device_type()
        dev.full_clean = MagicMock(side_effect=ValidationError({"device_type": ["err"]}))
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value={"matched": True, "device_type": new_dt},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)
        mock_msg.error.assert_called_once()
        assert Device.objects.get(pk=dev.pk).device_type_id == original_dt  # nothing persisted

    def test_save_integrity_error(self):
        from django.db import IntegrityError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        dev = make_device("type-interr")
        new_dt = self._new_device_type()
        # Raise from save(), not full_clean(): full_clean runs first, so mocking it would
        # exercise the validation branch and let a regression in the actual save-failure
        # handling pass unnoticed. This test must cover the save IntegrityError path.
        dev.save = MagicMock(side_effect=IntegrityError("dup"))
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value={"matched": True, "device_type": new_dt},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)
        mock_msg.error.assert_called_once()


# ---------------------------------------------------------------------------
# UpdateDevicePlatformView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateDevicePlatformView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDevicePlatformView

        return _make_view(UpdateDevicePlatformView)

    @staticmethod
    def _platform(slug):
        from dcim.models import Platform

        return Platform.objects.create(name=slug, slug=slug)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"):
            result = view.post(_make_request(), pk=1)
        assert result is err

    def test_no_librenms_id(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_get_device_info_failure(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (False, None)
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_no_os(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": None})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.warning.assert_called_once()

    def test_platform_does_not_exist(self):

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "ios"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": False, "platform": None, "match_type": None},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_save_success_with_old_platform(self):
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "ios"})
        new_platform = self._platform("ios")
        dev = make_device("plat-old")
        dev.platform = self._platform("oldos")  # old platform exists
        dev.save()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": True, "platform": new_platform, "match_type": "exact"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert "updated from" in mock_msg.success.call_args[0][1]
        assert Device.objects.get(pk=dev.pk).platform_id == new_platform.pk

    def test_save_success_no_old_platform(self):
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "ios"})
        new_platform = self._platform("ios")
        dev = make_device("plat-noold")  # no platform set

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": True, "platform": new_platform, "match_type": "exact"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert "set to" in mock_msg.success.call_args[0][1]
        assert Device.objects.get(pk=dev.pk).platform_id == new_platform.pk

    def test_save_success_via_platform_mapping(self):
        """Sync works when a PlatformMapping maps LibreNMS OS to a differently-named NetBox platform."""
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "junos"})
        new_platform = self._platform("junos")
        dev = make_device("plat-mapping")

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": True, "platform": new_platform, "match_type": "mapping"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert Device.objects.get(pk=dev.pk).platform_id == new_platform.pk

    def test_ambiguous_platform_returns_error(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "ios"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": False, "platform": None, "match_type": "ambiguous"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()
        assert "ambiguity" in mock_msg.error.call_args[0][1].lower()

    def test_save_validation_error(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "ios"})

        from dcim.models import Device

        new_platform = self._platform("ios")
        dev = make_device("plat-valerr")
        dev.full_clean = MagicMock(side_effect=ValidationError({"platform": ["err"]}))

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": True, "platform": new_platform, "match_type": "exact"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=dev.pk)
        mock_msg.error.assert_called_once()
        assert Device.objects.get(pk=dev.pk).platform_id is None  # nothing persisted


# ---------------------------------------------------------------------------
# CreateAndAssignPlatformView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateAndAssignPlatformView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        return _make_view(CreateAndAssignPlatformView)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"):
            result = view.post(_make_request(), pk=1)
        assert result is err

    def test_no_platform_name(self):
        view = self._view()
        req = _make_request({"platform_name": ""})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        assert "required" in mock_msg.error.call_args[0][1].lower()

    @pytest.mark.django_db
    def test_rebinds_to_posted_server_for_redirect_fallback(self):
        """The view rebinds to the POSTed server so the _sync_redirect server_key fallback is live, not a dead None."""
        from django.test import override_settings

        view = self._view()  # _make_view seeds a default-server mock client
        dev = make_device("plat-rebind")
        # Empty platform_name → returns right after the rebind, which is all this asserts.
        req = _make_request({"platform_name": "", "server_key": "production"})

        servers = {
            "staging": {"librenms_url": "https://stg.example.com", "api_token": "t", "verify_ssl": False},
            "production": {"librenms_url": "https://prod.example.com", "api_token": "t", "verify_ssl": False},
        }
        with (
            override_settings(PLUGINS_CONFIG={"netbox_librenms_plugin": {"servers": servers}}),
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=dev.pk)

        # Rebound to the POSTed server (not left on the initial/global client), so the
        # getattr(self._librenms_api, "server_key") fallback in _sync_redirect is meaningful.
        assert view._librenms_api is not None
        assert view._librenms_api.server_key == "production"

    def test_platform_already_exists_is_reused_and_assigned(self):
        """Existing platform is reused (not re-created) and assigned to the device."""
        from dcim.models import Device, Platform

        view = self._view()
        existing = Platform.objects.create(name="ios", slug="ios")
        dev = make_device("plat-reuse")
        req = _make_request({"platform_name": "ios", "manufacturer": "", "create_mapping": ""})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=dev.pk)
        # Reused — not duplicated.
        assert Platform.objects.filter(name__iexact="ios").count() == 1
        assert Device.objects.get(pk=dev.pk).platform_id == existing.pk
        mock_msg.success.assert_called_once()
        assert "already existed" in mock_msg.success.call_args[0][1].lower()

    def test_platform_already_exists_creates_missing_mapping(self):
        """When the platform exists and create_mapping is on, the missing mapping is added (real)."""
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.models import PlatformMapping

        view = self._view()
        existing = Platform.objects.create(name="ios", slug="ios")
        dev = make_device("plat-reuse-map")
        req = _make_request({"platform_name": "ios", "manufacturer": "", "librenms_os": "ios", "create_mapping": "1"})
        # The view re-checks PlatformMapping 'add' permission at the write site.
        view.request = req
        req.user.has_perm = MagicMock(return_value=True)

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=dev.pk)
        assert Platform.objects.filter(name__iexact="ios").count() == 1  # reused
        # A real mapping now points the posted OS at the reused platform.
        mapping = PlatformMapping.objects.get(librenms_os__iexact="ios")
        assert mapping.netbox_platform_id == existing.pk
        assert Device.objects.get(pk=dev.pk).platform_id == existing.pk
        mock_msg.success.assert_called_once()

    def test_mapping_existing_points_to_different_platform_warns(self):
        """An existing OS mapping that targets a DIFFERENT platform must not be reported as 'already exists' — surface a warning and don't create a new mapping."""
        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": "", "librenms_os": "ios", "create_mapping": "1"})

        found_platform = MagicMock(pk=5)
        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.first.return_value = found_platform

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        # Existing mapping for "ios" points at a different platform (id 999, not 5).
        other_mapping = MagicMock(netbox_platform_id=999)
        mock_mapping_cls = MagicMock()
        mock_mapping_cls.objects.filter.return_value.first.return_value = other_mapping

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.PlatformMapping", mock_mapping_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)

        # No new mapping created (PlatformMapping class never instantiated for a create), and
        # the platform-mismatch is surfaced as a warning rather than a silent "already exists".
        mock_mapping_cls.assert_not_called()
        assert any("pointing to" in str(c.args) for c in mock_msg.warning.call_args_list)
        # The mapping conflict is non-fatal: the primary action (assign the found platform to
        # the locked device and persist it) must still happen, not warn-and-return.
        assert mock_locked.platform is found_platform
        mock_locked.save.assert_called_once()

    def test_manufacturer_not_found(self):
        """manufacturer_id provided but Manufacturer.DoesNotExist: manufacturer stays None."""
        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": "99"})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.first.return_value = None
        mock_platform_instance = MagicMock()
        mock_platform_cls.return_value = mock_platform_instance

        mock_manuf_cls = MagicMock()
        mock_manuf_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_manuf_cls.objects.get.side_effect = mock_manuf_cls.DoesNotExist()

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Manufacturer", mock_manuf_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        # Should succeed (manufacturer silently ignored)
        mock_msg.success.assert_called_once()
        assert mock_locked.platform == mock_platform_instance
        mock_locked.save.assert_called_once()

    def test_success_no_manufacturer(self):
        """A new platform (no manufacturer) is created and assigned to the device (real)."""
        from dcim.models import Device, Platform

        view = self._view()
        dev = make_device("plat-create")
        req = _make_request({"platform_name": "ios-new", "manufacturer": ""})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=dev.pk)
        mock_msg.success.assert_called_once()
        platform = Platform.objects.get(name="ios-new")
        assert platform.manufacturer_id is None
        assert Device.objects.get(pk=dev.pk).platform_id == platform.pk

    def test_platform_constructor_includes_slug(self):
        """The created platform's slug is slugify(name) — regression for #279 (verified on the DB row)."""
        from django.utils.text import slugify

        from dcim.models import Platform

        view = self._view()
        platform_name = "Cisco IOS-XE 17.x"
        dev = make_device("plat-slug")
        req = _make_request({"platform_name": platform_name, "manufacturer": ""})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=dev.pk)

        assert Platform.objects.get(name=platform_name).slug == slugify(platform_name)

    def test_platform_validation_error(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": ""})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.first.return_value = None
        mock_platform_instance = MagicMock()
        mock_platform_instance.full_clean.side_effect = ValidationError({"name": ["err"]})
        mock_platform_cls.return_value = mock_platform_instance

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_device_does_not_exist_inside_transaction(self):
        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": ""})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.first.return_value = None
        mock_platform_instance = MagicMock()
        mock_platform_cls.return_value = mock_platform_instance

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.side_effect = DoesNotExist()

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_device_validation_error(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": ""})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.first.return_value = None
        mock_platform_instance = MagicMock()
        mock_platform_cls.return_value = mock_platform_instance

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.full_clean.side_effect = ValidationError({"platform": ["err"]})

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_integrity_error(self):
        from django.db import IntegrityError

        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": ""})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.first.return_value = None
        mock_platform_instance = MagicMock()
        # Make save raise IntegrityError
        mock_platform_instance.save.side_effect = IntegrityError("duplicate")
        mock_platform_cls.return_value = mock_platform_instance

        # transaction.atomic().__exit__ must return False so IntegrityError propagates
        mock_atomic_cm = MagicMock()
        mock_atomic_cm.__enter__ = MagicMock(return_value=None)
        mock_atomic_cm.__exit__ = MagicMock(return_value=False)
        mock_txn = MagicMock()
        mock_txn.atomic.return_value = mock_atomic_cm
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_integrity_error_reuses_concurrently_created_platform(self):
        """IntegrityError on create, but the same-named platform now exists (a concurrent insert won the race): reuse it and assign — no error, no rollback."""
        from django.db import IntegrityError

        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": ""})

        reused_platform = MagicMock()
        mock_platform_cls = MagicMock()
        # First .first() is the up-front existence check (None → take the create path);
        # the second is the post-IntegrityError re-query (the concurrently-created row).
        mock_platform_cls.objects.filter.return_value.first.side_effect = [None, reused_platform]
        mock_platform_instance = MagicMock()
        mock_platform_instance.save.side_effect = IntegrityError("duplicate")
        mock_platform_cls.return_value = mock_platform_instance

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        # __exit__ must return False so the IntegrityError propagates out of the nested atomic.
        mock_atomic_cm = MagicMock()
        mock_atomic_cm.__enter__ = MagicMock(return_value=None)
        mock_atomic_cm.__exit__ = MagicMock(return_value=False)
        mock_txn = MagicMock()
        mock_txn.atomic.return_value = mock_atomic_cm
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)

        # Reuse path: the concurrently-created platform is assigned and persisted, with
        # no error/rollback. The save() assertion guards against a regression that wires
        # up the FK but skips persistence.
        mock_msg.error.assert_not_called()
        # Nested atomic() boundaries isolate the Platform.save() that may raise IntegrityError so
        # the outer transaction stays usable for the re-query. The mock makes atomic() a no-op, so
        # a single-atomic regression would otherwise still pass — pin the nesting explicitly.
        assert mock_txn.atomic.call_count >= 2
        mock_txn.set_rollback.assert_not_called()
        assert mock_locked.platform is reused_platform
        mock_locked.save.assert_called_once()
        mock_msg.success.assert_called_once()

    def _success_patches(self, platform_name="ios", librenms_os="ios", create_mapping="1"):
        """Return (view, req, mock_platform_cls, mock_platform_instance, mock_device_cls, mock_locked)."""
        view = self._view()
        req = _make_request(
            {
                "platform_name": platform_name,
                "manufacturer": "",
                "librenms_os": librenms_os,
                "create_mapping": create_mapping,
            }
        )
        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.first.return_value = None
        mock_platform_instance = MagicMock()
        mock_platform_cls.return_value = mock_platform_instance
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked
        return view, req, mock_platform_cls, mock_platform_instance, mock_device_cls, mock_locked

    def test_mapping_created_when_name_differs(self):
        """A PlatformMapping is created when name differs from librenms_os and checkbox is on."""
        view, req, mock_platform_cls, mock_platform_instance, mock_device_cls, _ = self._success_patches(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping="1"
        )
        mock_mapping_cls = MagicMock()
        mock_mapping_instance = MagicMock()
        mock_mapping_cls.return_value = mock_mapping_instance
        mock_mapping_cls.objects.filter.return_value.first.return_value = None

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("netbox_librenms_plugin.views.sync.device_fields.PlatformMapping", mock_mapping_cls),
        ):
            view.post(req, pk=1)

        mock_mapping_cls.assert_called_once_with(librenms_os="ios", netbox_platform=mock_platform_instance)
        mock_mapping_instance.full_clean.assert_called_once()
        mock_mapping_instance.save.assert_called_once()
        success_msg = mock_msg.success.call_args[0][1]
        assert "platform mapping" in success_msg

    def test_mapping_skipped_when_checkbox_off(self):
        """No PlatformMapping is created when checkbox is unchecked."""
        view, req, mock_platform_cls, mock_platform_instance, mock_device_cls, _ = self._success_patches(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping=""
        )
        mock_mapping_cls = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("netbox_librenms_plugin.views.sync.device_fields.PlatformMapping", mock_mapping_cls),
        ):
            view.post(req, pk=1)

        mock_mapping_cls.assert_not_called()

    def test_mapping_skipped_when_already_exists(self):
        """No duplicate PlatformMapping is created when one already exists for the OS."""
        view, req, mock_platform_cls, mock_platform_instance, mock_device_cls, _ = self._success_patches(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping="1"
        )
        mock_mapping_cls = MagicMock()
        mock_mapping_cls.objects.filter.return_value.first.return_value = MagicMock()  # existing mapping

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("netbox_librenms_plugin.views.sync.device_fields.PlatformMapping", mock_mapping_cls),
        ):
            view.post(req, pk=1)

        mock_mapping_cls.assert_not_called()

    def test_mapping_skipped_when_lacking_add_perm_at_write(self):
        """TOCTOU guard: a mapping existed at the preflight gate (so add wasn't required) but was deleted before the write."""
        view, req, mock_platform_cls, mock_platform_instance, mock_device_cls, mock_locked = self._success_patches(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping="1"
        )
        # Upfront object-permission gate passes (user has change Device / add Platform); the
        # write-site re-check is what must catch the missing PlatformMapping add permission.
        view.require_object_permissions = MagicMock(return_value=None)
        # Deny ONLY the PlatformMapping add permission at the write site; every other perm
        # check must pass, so the test can't succeed via an unrelated permission-denied path.
        mapping_add_perm = "netbox_librenms_plugin.add_platformmapping"
        req.user.has_perm = MagicMock(side_effect=lambda perm: perm != mapping_add_perm)
        mock_mapping_cls = MagicMock()
        mock_mapping_cls.objects.filter.return_value.first.return_value = None  # deleted since preflight

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("netbox_librenms_plugin.views.sync.device_fields.PlatformMapping", mock_mapping_cls),
            patch("utilities.permissions.get_permission_for_model", return_value=mapping_add_perm),
        ):
            view.post(req, pk=1)

        # Prove the write-site permission check actually ran (so the skip is due to the TOCTOU
        # re-check, not some unrelated path), then that no mapping was created and the user warned.
        req.user.has_perm.assert_any_call(mapping_add_perm)
        # No mapping created without permission, and the user is told why.
        mock_mapping_cls.assert_not_called()
        assert any("not created" in c.args[1] for c in mock_msg.warning.call_args_list)
        # The platform itself must still be assigned to the locked device and persisted — only
        # the secondary mapping is skipped, so this branch can't silently return pre-persist.
        assert mock_locked.platform is mock_platform_instance
        mock_locked.save.assert_called_once()

    def test_required_object_permissions_never_include_platformmapping_upfront(self):
        """Even when create_mapping is on, an OS is supplied, and no mapping exists yet, the upfront POST gate must NOT require ('add', PlatformMapping): assigning the platform is the primary action and must not be blocked for a user who can't create mappings."""
        view, req, mock_platform_cls, _, mock_device_cls, _ = self._success_patches(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping="1"
        )
        # The upfront gate no longer reads PlatformMapping at all (neither .exists() nor
        # .first()), so no lookup stub is needed — the gate must not require the perm
        # regardless of whether a mapping exists. Assert against the REAL model symbol so a
        # regression that left ('add', PlatformMapping) in the perms can't slip past a
        # MagicMock that never equals the real class.
        from netbox_librenms_plugin.models import PlatformMapping as RealPlatformMapping

        captured = {}

        def fake_require(method):
            captured["perms"] = view.required_object_permissions.get(method, [])
            # Short-circuit by returning a sentinel response so post() exits early.
            return MagicMock()

        view.require_all_permissions = fake_require

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.PlatformMapping", RealPlatformMapping),
        ):
            view.post(req, pk=1)

        assert ("add", RealPlatformMapping) not in captured["perms"], (
            "('add', PlatformMapping) must not gate the upfront POST — the mapping is gated "
            "at the write site so the primary platform-assign isn't blocked"
        )

    def test_required_object_permissions_exclude_platformmapping_when_mapping_exists(self):
        """create_mapping on but a mapping for the OS already exists → no mapping write occurs, so ('add', PlatformMapping) must NOT be required (don't block the assign)."""
        view, req, mock_platform_cls, _, mock_device_cls, _ = self._success_patches(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping="1"
        )
        # The upfront gate no longer reads PlatformMapping (see the sibling test); the perm
        # is never required upfront, the mapping write gates itself at its own site. Assert
        # against the REAL model symbol so a regression can't hide behind a MagicMock.
        from netbox_librenms_plugin.models import PlatformMapping as RealPlatformMapping

        captured = {}

        def fake_require(method):
            captured["perms"] = view.required_object_permissions.get(method, [])
            return MagicMock()

        view.require_all_permissions = fake_require

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.PlatformMapping", RealPlatformMapping),
        ):
            view.post(req, pk=1)

        assert ("add", RealPlatformMapping) not in captured["perms"], (
            "Did not expect ('add', PlatformMapping) when a mapping for the OS already exists"
        )

    def test_required_object_permissions_exclude_platformmapping_when_no_create_mapping(self):
        """When create_mapping is NOT checked, ('add', PlatformMapping) must NOT be added."""
        from netbox_librenms_plugin.models import PlatformMapping as RealPlatformMapping

        view, req, mock_platform_cls, _, mock_device_cls, _ = self._success_patches(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping=""
        )

        captured = {}

        def fake_require(method):
            captured["perms"] = view.required_object_permissions.get(method, [])
            return MagicMock()

        view.require_all_permissions = fake_require

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.PlatformMapping", RealPlatformMapping),
        ):
            view.post(req, pk=1)

        assert ("add", RealPlatformMapping) not in captured["perms"], (
            "Did not expect ('add', PlatformMapping) when create_mapping is unchecked"
        )


# ---------------------------------------------------------------------------
# AssignVCSerialView
# ---------------------------------------------------------------------------


class TestAssignVCSerialView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import AssignVCSerialView

        return _make_view(AssignVCSerialView)

    @pytest.mark.django_db
    def test_member_save_success_persists_serial(self):
        """A serial is assigned to a real VC member and persisted (verified via DB reload)."""
        from dcim.models import Device, VirtualChassis

        view = self._view()
        vc = VirtualChassis.objects.create(name="vc-serial")
        host = make_device("vc-host")
        host.virtual_chassis = vc
        host.vc_position = 1
        host.save()
        member = make_device("vc-member", serial="OLD")
        member.virtual_chassis = vc
        member.vc_position = 2
        member.save()

        req = _make_request({"serial_1": "SN100", "member_id_1": str(member.pk)})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=host.pk)
        mock_msg.success.assert_called_once()
        assert Device.objects.get(pk=member.pk).serial == "SN100"  # real save committed

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"):
            result = view.post(_make_request(), pk=1)
        assert result is err

    def test_not_virtual_chassis(self):
        view = self._view()
        mock_device = MagicMock()
        mock_device.virtual_chassis = None
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_no_serial_assignments_no_errors(self):
        """Loop doesn't execute — no serial_N keys in POST."""
        view = self._view()
        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock()
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({}), pk=1)
        mock_msg.info.assert_called_once()

    def test_member_id_missing(self):
        """member_id_{N} key is absent → counter incremented, no assignment."""
        view = self._view()
        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock()
        # serial_1 exists but member_id_1 is empty
        req = _make_request({"serial_1": "SN100", "member_id_1": ""})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.info.assert_called_once()

    def test_member_not_found(self):
        view = self._view()
        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock(pk=10)

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.get.side_effect = DoesNotExist()

        req = _make_request({"serial_1": "SN100", "member_id_1": "99"})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        # Should call error for the missing device
        mock_msg.error.assert_called()

    def test_member_different_chassis(self):
        view = self._view()
        vc = MagicMock(pk=10)
        mock_device = MagicMock()
        mock_device.virtual_chassis = vc

        member = MagicMock()
        member.name = "sw-member"
        member.virtual_chassis = MagicMock(pk=99)  # different VC!

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.get.return_value = member

        req = _make_request({"serial_1": "SN100", "member_id_1": "5"})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called()

    def test_member_save_validation_error(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        vc = MagicMock(pk=10)
        mock_device = MagicMock()
        mock_device.virtual_chassis = vc

        member = MagicMock()
        member.name = "sw-member"
        member.virtual_chassis = vc  # same VC
        member.serial = "OLD"
        member.full_clean.side_effect = ValidationError({"serial": ["err"]})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.get.return_value = member

        req = _make_request({"serial_1": "SN100", "member_id_1": "5"})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called()

    def test_assignments_and_errors_both_reported(self):
        """One success + one error → both messages emitted."""
        from django.core.exceptions import ValidationError

        view = self._view()
        vc = MagicMock(pk=10)
        mock_device = MagicMock()
        mock_device.virtual_chassis = vc

        good_member = MagicMock()
        good_member.name = "sw1"
        good_member.virtual_chassis = vc
        good_member.serial = ""

        bad_member = MagicMock()
        bad_member.name = "sw2"
        bad_member.virtual_chassis = vc
        bad_member.serial = ""
        bad_member.full_clean.side_effect = ValidationError({"serial": ["dup"]})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.get.side_effect = [good_member, bad_member]

        req = _make_request(
            {
                "serial_1": "SN001",
                "member_id_1": "1",
                "serial_2": "SN002",
                "member_id_2": "2",
            }
        )
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.success.assert_called()
        mock_msg.error.assert_called()
        assert good_member.serial == "SN001"
        good_member.save.assert_called_once()


# ---------------------------------------------------------------------------
# RemoveServerMappingView — helper methods
# ---------------------------------------------------------------------------


class TestRemoveServerMappingViewHelpers:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        view = object.__new__(RemoveServerMappingView)
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_get_object_device(self):
        view = self._view()
        mock_device = MagicMock()

        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device):
            obj, model = view._get_object("device", 1)
        assert obj is mock_device

    def test_get_object_vm(self):
        view = self._view()
        mock_vm = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_vm):
            obj, model = view._get_object("vm", 1)
        assert obj is mock_vm

    def test_sync_url_name_device(self):
        view = self._view()
        assert view._sync_url_name("device") == "plugins:netbox_librenms_plugin:device_librenms_sync"

    def test_sync_url_name_vm(self):
        view = self._view()
        assert view._sync_url_name("vm") == "plugins:netbox_librenms_plugin:vm_librenms_sync"

    def test_normalize_bool(self):
        view = self._view()
        assert view._normalize_librenms_mapping(True) == {}
        assert view._normalize_librenms_mapping(False) == {}

    def test_normalize_int(self):
        view = self._view()
        assert view._normalize_librenms_mapping(42) == {"default": 42}

    def test_normalize_string_digit(self):
        view = self._view()
        assert view._normalize_librenms_mapping("99") == {"default": 99}

    def test_normalize_dict(self):
        view = self._view()
        d = {"server1": 10}
        assert view._normalize_librenms_mapping(d) == d

    def test_normalize_non_digit_string_returns_empty(self):
        view = self._view()
        assert view._normalize_librenms_mapping("not-a-number") == {}

    def test_normalize_none_returns_empty(self):
        view = self._view()
        assert view._normalize_librenms_mapping(None) == {}


# ---------------------------------------------------------------------------
# RemoveServerMappingView — post()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRemoveServerMappingViewPost:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        view = object.__new__(RemoveServerMappingView)
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_invalid_object_type_returns_400(self):
        view = self._view()
        req = _make_request({"object_type": "badtype"})
        result = view.post(req, pk=1)
        assert result.status_code == 400

    def test_virtualmachine_object_type_normalized_to_vm(self):
        """object_type='virtualmachine' is normalised to 'vm'."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "virtualmachine", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_vm_cls = MagicMock()
        mock_vm_cls.DoesNotExist = DoesNotExist
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"orphan": 5}}
        mock_vm_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.VirtualMachine", mock_vm_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.success.assert_called_once()

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        req = _make_request({"object_type": "device", "server_key": "x"})
        result = view.post(req, pk=1)
        assert result is err

    def test_no_server_key(self):
        view = self._view()
        req = _make_request({"object_type": "device", "server_key": ""})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()

    def test_mapping_not_found_wrong_type(self):
        """cf_value is not a dict → warning."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": None}

        req = _make_request({"object_type": "device", "server_key": "default"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.warning.assert_called_once()

    def test_mapping_not_found_missing_key(self):
        """server_key not in cf_value dict → warning."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"other": 5}}

        req = _make_request({"object_type": "device", "server_key": "default"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.warning.assert_called_once()

    def test_configured_servers_non_dict_treated_as_empty(self):
        """servers config is a list (non-dict) → treated as empty dict, orphan key can be removed."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"orphan": 5}}

        mock_device_cls = MagicMock()
        mock_device_cls.__name__ = "Device"
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        # servers is a list (non-dict) → line 496 normalises it to {}
        mock_cfg = {"netbox_librenms_plugin": {"servers": ["not", "a", "dict"], "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.success.assert_called_once()

    def test_configured_server_key_in_servers_dict(self):
        """server_key is in configured servers → error."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"production": 10}}

        req = _make_request({"object_type": "device", "server_key": "production"})

        mock_cfg = {"netbox_librenms_plugin": {"servers": {"production": {}}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        assert "Cannot remove" in mock_msg.error.call_args[0][1]

    def test_legacy_default_server_protected(self):
        """Legacy mode with librenms_url set and server_key='default' → error."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"default": 7}}

        req = _make_request({"object_type": "device", "server_key": "default"})

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": "https://librenms.example.com"}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()

    def test_object_no_longer_exists_inside_transaction(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.__name__ = "Device"
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.side_effect = DoesNotExist()

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()

    def test_mapping_already_removed_in_lock(self):
        """server_key is gone from the locked object's cf → warning."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        # Key was removed between the first read and the lock
        mock_locked.custom_field_data = {"librenms_id": {}}

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.warning.assert_called_once()

    def test_validation_error_on_save(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"orphan": 5}}
        mock_locked.full_clean.side_effect = ValidationError({"librenms_id": ["err"]})

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_unexpected_error_on_save(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"orphan": 5}}
        mock_locked.full_clean.side_effect = RuntimeError("disk full")

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_success_removes_mapping(self):
        """Happy path: an UNCONFIGURED server's mapping is removed; last entry → cf None."""
        from dcim.models import Device

        view = self._view()
        dev = make_device("rm-orphan", librenms_cf={"orphan": 5})
        req = _make_request({"object_type": "device", "server_key": "orphan"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=dev.pk)
        mock_msg.success.assert_called_once()
        # Last key removed → cf librenms_id collapses to None.
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] is None

    def test_success_keeps_remaining_mappings(self):
        """Happy path: removing one unconfigured mapping keeps the others (verified via DB reload)."""
        from dcim.models import Device

        view = self._view()
        dev = make_device("rm-keep", librenms_cf={"orphan": 5, "other": 6})
        req = _make_request({"object_type": "device", "server_key": "orphan"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"other": 6}


# ---------------------------------------------------------------------------
# ConvertLegacyLibreNMSIdView — helper methods
# ---------------------------------------------------------------------------


class TestConvertLegacyLibreNMSIdViewHelpers:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        view = object.__new__(ConvertLegacyLibreNMSIdView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_get_model_and_object_device(self):
        view = self._view()
        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device):
            model, obj = view._get_model_and_object("device", 1)
        assert obj is mock_device

    def test_get_model_and_object_vm(self):
        view = self._view()
        mock_vm = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_vm):
            model, obj = view._get_model_and_object("vm", 1)
        assert obj is mock_vm

    def test_sync_url_device(self):
        view = self._view()
        with patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir:
            view._sync_url("device", 1)
        # No request/server_key in scope → bare reversed sync URL (no query string).
        (url,), _ = mock_redir.call_args
        assert isinstance(url, str) and "server_key" not in url
        assert "1" in url

    def test_sync_url_vm(self):
        view = self._view()
        with patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir:
            view._sync_url("vm", 1)
        (url,), _ = mock_redir.call_args
        assert isinstance(url, str) and "server_key" not in url

    def test_sync_url_propagates_known_server_key(self):
        """A POST-scoped server_key that matches a configured server is preserved so multi-server users return to the server they were working in."""
        view = self._view()
        view.request = MagicMock()
        view.request.POST = {"server_key": "prod"}
        view.request.GET = {}
        with (
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"prod": "Prod LibreNMS"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir,
        ):
            view._sync_url("device", 1)
        (url,), _ = mock_redir.call_args
        assert "server_key=prod" in url

    def test_sync_url_stale_post_key_falls_back_to_active_server(self):
        """A POST server_key that is no longer configured (stale page / removed server) must not drop the server context: it doesn't match the allowlist, so the redirect falls back to the active/default server the action ran against (here the bound _librenms_api='default'), re-validated through the allowlist — instead of emitting a bare URL."""
        view = self._view()  # _view() binds _librenms_api = MagicMock(server_key="default")
        view.request = MagicMock()
        view.request.POST = {"server_key": "ghost"}  # unconfigured / stale
        view.request.GET = {}
        with (
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"default": "Default LibreNMS"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir,
        ):
            view._sync_url("device", 1)
        (url,), _ = mock_redir.call_args
        assert "server_key=default" in url
        assert "ghost" not in url

    def test_sync_url_drops_unknown_server_key(self):
        """An unconfigured/spoofed server_key is not reflected into the redirect URL (allowlist guard — open-redirect safe)."""
        view = self._view()
        view.request = MagicMock()
        view.request.POST = {"server_key": "//evil.com/steal"}
        view.request.GET = {}
        with (
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"prod": "Prod LibreNMS"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir,
        ):
            view._sync_url("device", 1)
        (url,), _ = mock_redir.call_args
        assert "evil.com" not in url
        assert "server_key" not in url

    def test_sync_url_unbound_api_misconfigured_default_degrades_without_500(self):
        """On a redirect after a failed rebind, _librenms_api is unbound (None) and the request carries no server_key."""
        view = self._view()
        view._librenms_api = None
        view.request = MagicMock()
        view.request.POST = {}
        view.request.GET = {}
        with (
            # Property constructs the default client → misconfigured default raises.
            patch("netbox_librenms_plugin.views.mixins.LibreNMSAPI", side_effect=KeyError("ghost")),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir,
        ):
            view._sync_url("device", 1)  # must not raise
        (url,), _ = mock_redir.call_args
        assert "server_key" not in url

    def test_sync_url_drops_server_key_when_url_validation_fails(self):
        """Even for an allowlisted server_key, if url_has_allowed_host_and_scheme rejects the candidate (the CodeQL open-redirect barrier), fall back to the bare URL."""
        view = self._view()
        view.request = MagicMock()
        view.request.POST = {"server_key": "prod"}
        view.request.GET = {}
        with (
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"prod": "Prod LibreNMS"},
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.url_has_allowed_host_and_scheme",
                return_value=False,
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir,
        ):
            view._sync_url("device", 1)
        (url,), _ = mock_redir.call_args
        assert "server_key" not in url


# ---------------------------------------------------------------------------
# ConvertLegacyLibreNMSIdView — post()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestConvertLegacyLibreNMSIdViewPost:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        view = object.__new__(ConvertLegacyLibreNMSIdView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_invalid_object_type_returns_400(self):
        view = self._view()
        req = _make_request({"object_type": "badtype"})
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"):
            result = view.post(req, pk=1)
        assert result.status_code == 400

    def test_virtualmachine_object_type_normalised(self):
        """object_type='virtualmachine' is accepted as 'vm'."""
        view = self._view()
        # Provide a legacy string int as cf_value
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": "42"}
        mock_obj.serial = "SN-MATCH"

        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": "42"}
        mock_locked.serial = "SN-MATCH"

        mock_vm_cls = MagicMock()
        mock_vm_cls.DoesNotExist = DoesNotExist
        mock_vm_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.VirtualMachine", mock_vm_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=True
            ) as mock_migrate,
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "virtualmachine"}), pk=1)
        mock_msg.success.assert_called_once()
        mock_migrate.assert_called_once()
        mock_locked.full_clean.assert_called_once()
        mock_locked.save.assert_called_once()

    def test_whitespace_padded_legacy_id_is_convertible_not_dead_end(self):
        """A padded legacy id (' 42 ') the badge shows via is_legacy_librenms_id must convert, not hit the isdigit() dead-end."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": " 42 "}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": " 42 "}
        mock_locked.serial = "SN-MATCH"
        mock_dev_cls = MagicMock()
        mock_dev_cls.DoesNotExist = DoesNotExist
        mock_dev_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_dev_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=True
            ) as mock_migrate,
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        # Unfixed: ' 42 '.isdigit() is False → error "not a valid integer" and migrate NOT called.
        mock_migrate.assert_called_once()
        assert not any("not a valid integer" in str(c) for c in mock_msg.error.call_args_list)
        # The serial gate must read live info (use_cache=False), not a stale sync-tab cache snapshot.
        assert view._librenms_api.get_device_info.call_args.kwargs.get("use_cache") is False

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        req = _make_request({"object_type": "device"})
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"):
            result = view.post(req, pk=1)
        assert result is err

    def test_already_json_format_dict(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"default": 5}}

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.warning.assert_called_once()
        assert "already" in mock_msg.warning.call_args[0][1].lower()

    def test_already_json_format_bool(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": True}

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_non_digit_string_cf_value(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": "not-a-number"}

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    @pytest.mark.django_db
    def test_convert_refused_when_id_collides_with_another_devices_oob(self):
        """Fail-closed pin: converting a legacy id is refused when another device uses it as its OOB controller id."""
        from dcim.models import Device

        view = self._view()
        # Blank-key rebind returns "default" without rebuilding the (mock) client.
        view.rebind_api_for_server = MagicMock(return_value="default")
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-A"})

        # Device A: legacy bare-int librenms_id 42, serial matches LibreNMS so the convert proceeds
        # to the conflict check.
        dev_a = make_device("convert-a", serial="SN-A", librenms_cf=42)
        # Device B: a DIFFERENT device using 42 as its OOB controller id under "default" — only the
        # OOB sub-key query (oob_q) surfaces this collision.
        make_device("convert-b", librenms_cf={"default": {"id": 99, "oob": {"id": 42}}})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=dev_a),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=dev_a.pk)

        # Refused (fail closed): an error, never a success, and the legacy id is left untouched
        # (NOT silently converted to the JSON dict form).
        mock_msg.success.assert_not_called()
        mock_msg.error.assert_called_once()
        body = mock_msg.error.call_args[0][1].lower()
        assert "ambiguous" in body or "already has" in body
        assert Device.objects.get(pk=dev_a.pk).custom_field_data["librenms_id"] == 42

    def test_get_device_info_failure(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        view._librenms_api.get_device_info.return_value = (False, None)

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_serial_mismatch_empty_netbox_serial(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = ""
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-ABC"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()
        assert "Serial" in mock_msg.error.call_args[0][1]

    def test_serial_mismatch_different(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-XYZ"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-ABC"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_object_no_longer_exists_in_lock(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.__name__ = "Device"
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.side_effect = DoesNotExist()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_cf_value_changed_to_json_after_lock(self):
        """Locked row shows cf_value already as dict → warning."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"default": 42}}  # already dict

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.warning.assert_called_once()

    def test_cf_value_not_int_after_lock(self):
        """Locked row shows non-digit string → error: cannot convert."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": "not-a-digit"}

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_data_changed_before_lock(self):
        """locked_id or locked_serial differs → error: aborting."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": 99}  # different id
        mock_locked.serial = "SN-MATCH"

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_conflict_with_another_object(self):
        """Another object already has the same librenms_id for this server."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": 42}
        mock_locked.serial = "SN-MATCH"

        other_obj = MagicMock()
        other_obj.pk = 99  # different pk → conflict

        mock_device_cls = MagicMock()
        mock_device_cls.__name__ = "Device"
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=other_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_migrate_returns_false(self):
        """migrate_legacy_librenms_id returns False → warning."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": 42}
        mock_locked.serial = "SN-MATCH"

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=False),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.warning.assert_called_once()

    def test_validation_error_on_save(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": 42}
        mock_locked.serial = "SN-MATCH"
        mock_locked.full_clean.side_effect = ValidationError({"librenms_id": ["err"]})

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=True),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_unexpected_error_on_save(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": 42}
        mock_locked.serial = "SN-MATCH"
        mock_locked.full_clean.side_effect = RuntimeError("disk full")

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=True),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_success_integer_cf_value(self):
        """Happy path, legacy int cf_value: the real migrate + save converts it to the dict form."""
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        dev = make_device("convert-int", serial="SN-MATCH", librenms_cf=42)

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert "42" in mock_msg.success.call_args[0][1]
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"default": 42}

    def test_success_string_cf_value(self):
        """Happy path, legacy string-digit cf_value '42' → converted to {'default': 42}."""
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        dev = make_device("convert-str", serial="SN-MATCH", librenms_cf="42")

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"default": 42}

    def test_conflict_same_object_is_not_conflict(self):
        """find_by_librenms_id returns the same object → no conflict, proceeds."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": 42}
        mock_locked.serial = "SN-MATCH"

        # find_by_librenms_id returns the SAME object → match.pk == locked.pk → no conflict
        same_obj = MagicMock()
        same_obj.pk = 1

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=same_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=True),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.success.assert_called_once()
        mock_locked.full_clean.assert_called_once()
        mock_locked.save.assert_called_once()


# ---------------------------------------------------------------------------
# Wiring assertions — ensure views keep required mixins and permissions
# ---------------------------------------------------------------------------


class TestDeviceFieldsViewWiring:
    """Structural checks: views must retain required mixins and permissions."""

    def test_convert_legacy_id_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        assert issubclass(ConvertLegacyLibreNMSIdView, LibreNMSAPIMixin)

    def test_convert_legacy_id_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        assert "POST" in ConvertLegacyLibreNMSIdView.required_object_permissions

    def test_remove_server_mapping_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        assert "POST" in RemoveServerMappingView.required_object_permissions


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
class TestSyncRedirectServerKeyValidation:
    """_sync_redirect must only reflect a server_key that matches a configured server, so untrusted request input can't be steered into the redirect URL (open-redirect)."""

    def test_valid_server_key_is_reflected(self):
        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        req = _make_request(post_data={"server_key": "prod"})
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"prod": "Prod LibreNMS"},
        ):
            resp = CreateAndAssignPlatformView._sync_redirect(req, 1)
        assert "server_key=prod" in resp.url

    def test_unknown_server_key_is_dropped(self):
        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        req = _make_request(post_data={"server_key": "//evil.com/steal"})
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"prod": "Prod LibreNMS"},
        ):
            resp = CreateAndAssignPlatformView._sync_redirect(req, 1)
        assert "evil.com" not in resp.url
        assert "server_key" not in resp.url

    def test_falls_back_to_active_server_when_form_omits_key(self):
        """When the form omits server_key, _sync_redirect reflects the active API server (passed as fallback) so a multi-server user isn't dropped onto the default tab."""
        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        req = _make_request(post_data={})  # no server_key in POST
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"prod": "Prod LibreNMS"},
        ):
            resp = CreateAndAssignPlatformView._sync_redirect(req, 1, "prod")
        assert "server_key=prod" in resp.url

    def test_valid_server_key_is_dropped_when_redirect_url_fails_validation(self):
        """Even an allowlisted server_key must be dropped if the resulting redirect URL fails url_has_allowed_host_and_scheme (the shared redirect_with_server_key barrier), so a regression that reflected a known key into a rejected URL is caught here too."""
        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        req = _make_request(post_data={"server_key": "prod"})
        with (
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"prod": "Prod LibreNMS"},
            ),
            # The barrier now lives in the shared mixins.redirect_with_server_key helper.
            patch(
                "netbox_librenms_plugin.views.mixins.url_has_allowed_host_and_scheme",
                return_value=False,
            ),
        ):
            resp = CreateAndAssignPlatformView._sync_redirect(req, 1)
        assert "server_key" not in resp.url


# ---------------------------------------------------------------------------
# Multi-server: Update* views must rebind to the POSTed server_key
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateDeviceFieldsServerRebind:
    """Each Update* field view must rebind the API client to the POSTed server_key."""

    # "staging" first so the global/default fallback (LibreNMSSettings absent) resolves THERE,
    # not "production" — a missing rebind then looks up the per-server id under the wrong server.
    SERVERS = {
        "staging": {"librenms_url": "https://stg.example.com", "api_token": "t", "verify_ssl": False},
        "production": {"librenms_url": "https://prod.example.com", "api_token": "t", "verify_ssl": False},
    }

    def _view(self, ViewClass):
        view = object.__new__(ViewClass)
        view.kwargs = {}
        view._librenms_api = None  # force a real build via rebind / the lazy property
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def _post(self, view, pk, device_info):
        from django.test import RequestFactory, override_settings

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        request = RequestFactory().post(f"/sync/{pk}/", {"server_key": "production"})
        global_settings = MagicMock()
        global_settings.first.return_value = None  # no selected server -> default -> first config key
        with (
            override_settings(PLUGINS_CONFIG={"netbox_librenms_plugin": {"servers": self.SERVERS}}),
            patch("netbox_librenms_plugin.models.LibreNMSSettings.objects", global_settings),
            patch.object(LibreNMSAPI, "get_device_info", return_value=(True, device_info)),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(request, pk=pk)

    def test_update_name_rebinds_and_renames_from_posted_server(self):
        """The id lives only under 'production'; the rename only resolves if the view rebinds to the POSTed key."""
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        device = make_device("rebind-name", librenms_cf={"production": 5})
        view = self._view(UpdateDeviceNameView)

        self._post(view, device.pk, {"sysName": "renamed-from-prod"})

        # Rebound to the POSTed server…
        assert view._librenms_api.server_key == "production"
        # …and the per-server id (5) resolved, so the device actually renamed.
        device.refresh_from_db()
        assert device.name == "renamed-from-prod"

    @pytest.mark.parametrize(
        "view_path",
        [
            "UpdateDeviceNameView",
            "UpdateDeviceSerialView",
            "UpdateDeviceTypeView",
            "UpdateDevicePlatformView",
        ],
    )
    def test_update_views_rebind_to_posted_server(self, view_path):
        """Every Update* view rebinds self.librenms_api to the POSTed server_key before lookup."""
        import netbox_librenms_plugin.views.sync.device_fields as df

        ViewClass = getattr(df, view_path)
        device = make_device(f"rebind-{view_path.lower()}", librenms_cf={"production": 5})
        view = self._view(ViewClass)

        self._post(view, device.pk, {"sysName": "h", "serial": "", "hardware": "", "os": ""})

        # The fix: the client is rebound to 'production' (not left on the global 'staging').
        assert view._librenms_api is not None
        assert view._librenms_api.server_key == "production"
