"""Coverage tests for views/sync/device_fields.py (target >95%).

The field-update views (name/serial/type/platform) persist through full_clean() + save();
the DB-touching tests run against REAL Devices (only the LibreNMS API — get_librenms_id /
get_device_info — and the messages/redirect framework seams are mocked), so the persisted
field is verified by reloading from the DB rather than asserting on a MagicMock whose
.save() is a no-op. A genuine duplicate-name conflict produces a REAL ValidationError.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_vm


from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    make_request,
    make_user_with_perms,
    make_view,
    message_texts,
)
from netbox_librenms_plugin.tests.view_test_helpers import post as _post


@pytest.mark.django_db
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
        patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_device,
        ),
        patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
        patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
    ):
        _r = _make_request()
        view.request = _r
        view.post(_r, pk=1)
    # Unfixed: called as get_device_info(42) → use_cache defaults True → reads the stale render cache.
    assert view._librenms_api.get_device_info.call_args.kwargs.get("use_cache") is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_view(ViewClass, request=None):
    """A real view instance; only the LibreNMS client is stubbed (the one external boundary)."""
    return make_view(ViewClass, request)


def _make_request(post_data=None, *, user=None):
    """A real POST request: real user, real session, real message storage."""
    return make_request("post", post_data or {}, user=user)


def _plugins_config(*, servers, librenms_url):
    """Override the plugin's configured servers for the protected-key checks."""
    from django.test import override_settings

    return override_settings(
        PLUGINS_CONFIG={"netbox_librenms_plugin": {"servers": servers, "librenms_url": librenms_url}}
    )


@contextmanager
def _before_restricted_read(view, action, *, on_call):
    """Run action before the specified real restricted query and assert that the view reaches that read."""
    real = view.restricted_queryset
    calls = []

    def _wrapper(model, action_name="view"):
        calls.append(model)
        if len(calls) == on_call:
            action()
        return real(model, action_name)

    view.restricted_queryset = _wrapper
    try:
        yield
    finally:
        view.restricted_queryset = real
    assert len(calls) >= on_call, f"the view made {len(calls)} restricted reads — the race window was never hit"


def _before_lock(view, action):
    """Run *action* between the view's first read and its ``select_for_update`` re-read."""
    return _before_restricted_read(view, action, on_call=2)


def _skip_once(method):
    """Return *method* with its first invocation turned into a no-op."""
    skipped = []

    def _wrapper(self, *args, **kwargs):
        if not skipped:
            skipped.append(True)
            return None
        return method(self, *args, **kwargs)

    return _wrapper


def _deleted_before_lock(view, obj):
    """Delete *obj* for real in the window before the view locks it."""

    def _delete():
        type(obj).objects.filter(pk=obj.pk).delete()

    return _before_lock(view, _delete)


def _cf_changed_before_lock(view, obj, librenms_id):
    """Update an object's LibreNMS ID directly in the database before the view locks it."""

    def _change():
        type(obj).objects.filter(pk=obj.pk).update(custom_field_data={"librenms_id": librenms_id})

    return _before_lock(view, _change)


@contextmanager
def _deleted_when_platform_saved(device):
    """Issue a same-transaction device DELETE when a platform is saved to test the locked re-read branch only."""
    from dcim.models import Device, Platform
    from django.db.models.signals import post_save

    def _receiver(sender, **kwargs):
        Device.objects.filter(pk=device.pk).delete()

    post_save.connect(_receiver, sender=Platform, weak=False)
    try:
        yield
    finally:
        post_save.disconnect(_receiver, sender=Platform)


# ---------------------------------------------------------------------------
# UpdateDeviceNameView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateDeviceNameView:
    def _view(self, request=None):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        return _make_view(UpdateDeviceNameView, request)

    def test_permission_denied_returns_error(self):
        view = self._view()
        error_response = MagicMock()
        view.require_all_permissions = MagicMock(return_value=error_response)

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404"
        ) as mock_get:
            _r = _make_request()
            view.request = _r
            result = view.post(_r, pk=1)

        assert result is error_response
        mock_get.assert_not_called()

    def test_no_librenms_id_returns_error(self):
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None
        dev = make_device("name-noid")

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            # The redirect now flows through the server_key-preserving helper (_device_sync_redirect
            # → redirect_with_server_key), so patch that rather than the bare redirect.
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect_with_server_key") as mock_redir,
        ):
            _post(view, _make_request(), pk=dev.pk)

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
            _post(view, _make_request(), pk=dev.pk)

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
            _post(view, _make_request(), pk=dev.pk)

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
            _post(view, _make_request(), pk=dev.pk)

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
            _post(view, _make_request(), pk=dev.pk)

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
            _post(view, _make_request(), pk=dev.pk)

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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.resolve_naming_preferences",
                return_value=(True, False),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _r = _make_request()
            view.request = _r
            view.post(_r, pk=dev.pk)

        mock_msg.error.assert_called_once()
        assert dev.name == "orig-int"  # restored after the failed save


# ---------------------------------------------------------------------------
# UpdateDeviceSerialView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateDeviceSerialView:
    def _view(self, request=None):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView

        return _make_view(UpdateDeviceSerialView, request)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404"
        ) as mock_get:
            _r = _make_request()
            view.request = _r
            result = view.post(_r, pk=1)
        assert result is err
        mock_get.assert_not_called()

    def test_no_librenms_id(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_get_device_info_failure(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (False, None)

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_serial_is_none(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": None})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
        mock_msg.warning.assert_called_once()

    def test_serial_is_dash(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "-"})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
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
            _post(view, _make_request(), pk=dev.pk)
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
            _post(view, _make_request(), pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert "set to" in mock_msg.success.call_args[0][1]
        assert Device.objects.get(pk=dev.pk).serial == "SN001"

    def test_padded_serial_is_normalized_before_persisting(self):
        """LibreNMS whitespace is stripped at this write boundary before Device.save()."""
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "\t SN-SYNC-1 \n"})
        dev = make_device("serial-padded-boundary", serial="")

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=dev.pk)

        assert Device.objects.get(pk=dev.pk).serial == "SN-SYNC-1"

    def test_numeric_zero_serial_is_not_dropped_as_missing(self):
        """Only None is absent: an all-digit JSON serial parsed as numeric zero persists as "0"."""
        from dcim.models import Device

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": 0})
        dev = make_device("serial-zero-boundary", serial="")

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=dev.pk)

        assert Device.objects.get(pk=dev.pk).serial == "0"

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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _r = _make_request()
            view.request = _r
            view.post(_r, pk=dev.pk)
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _r = _make_request()
            view.request = _r
            view.post(_r, pk=dev.pk)
        mock_msg.error.assert_called_once()


# ---------------------------------------------------------------------------
# UpdateDeviceTypeView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateDeviceTypeView:
    def _view(self, request=None):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceTypeView

        return _make_view(UpdateDeviceTypeView, request)

    @staticmethod
    def _new_device_type():
        from dcim.models import DeviceType, Manufacturer

        mfr, _ = Manufacturer.objects.get_or_create(name="TestMfr", slug="test-mfr")
        return DeviceType.objects.create(model="Cisco3750", slug="cisco3750", manufacturer=mfr)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404"
        ) as mock_get:
            _r = _make_request()
            view.request = _r
            result = view.post(_r, pk=1)
        assert result is err
        mock_get.assert_not_called()

    def test_no_librenms_id(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_get_device_info_failure(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (False, None)
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_no_hardware(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": None})
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
        mock_msg.warning.assert_called_once()

    def test_no_match_result(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value={"matched": False},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_match_none_returns_ambiguous_error(self):
        """match_librenms_hardware_to_device_type returns None → ambiguous-match error path."""
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        mock_device = MagicMock()
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value=None,
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
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
            _post(view, _make_request(), pk=dev.pk)
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value={"matched": True, "device_type": new_dt},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _r = _make_request()
            view.request = _r
            view.post(_r, pk=dev.pk)
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value={"matched": True, "device_type": new_dt},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _r = _make_request()
            view.request = _r
            view.post(_r, pk=dev.pk)
        mock_msg.error.assert_called_once()


# ---------------------------------------------------------------------------
# UpdateDevicePlatformView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateDevicePlatformView:
    def _view(self, request=None):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDevicePlatformView

        return _make_view(UpdateDevicePlatformView, request)

    @staticmethod
    def _platform(slug):
        from dcim.models import Platform

        return Platform.objects.create(name=slug, slug=slug)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        with patch("netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404"):
            _r = _make_request()
            view.request = _r
            result = view.post(_r, pk=1)
        assert result is err

    def test_no_librenms_id(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_get_device_info_failure(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (False, None)
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_no_os(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": None})
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
        mock_msg.warning.assert_called_once()

    def test_platform_does_not_exist(self):

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "ios"})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": False, "platform": None, "match_type": None},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": True, "platform": new_platform, "match_type": "exact"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _r = _make_request()
            view.request = _r
            view.post(_r, pk=dev.pk)
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": True, "platform": new_platform, "match_type": "exact"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _r = _make_request()
            view.request = _r
            view.post(_r, pk=dev.pk)
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": True, "platform": new_platform, "match_type": "mapping"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _r = _make_request()
            view.request = _r
            view.post(_r, pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert Device.objects.get(pk=dev.pk).platform_id == new_platform.pk

    def test_ambiguous_platform_returns_error(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "ios"})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": False, "platform": None, "match_type": "ambiguous"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _post(view, _make_request(), pk=1)
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.find_matching_platform",
                return_value={"found": True, "platform": new_platform, "match_type": "exact"},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            _r = _make_request()
            view.request = _r
            view.post(_r, pk=dev.pk)
        mock_msg.error.assert_called_once()
        assert Device.objects.get(pk=dev.pk).platform_id is None  # nothing persisted


# ---------------------------------------------------------------------------
# CreateAndAssignPlatformView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateAndAssignPlatformView:
    def _view(self, request=None):
        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        return _make_view(CreateAndAssignPlatformView, request)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        with patch("netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404"):
            _r = _make_request()
            view.request = _r
            result = view.post(_r, pk=1)
        assert result is err

    def test_no_platform_name(self):
        view = self._view()
        req = _make_request({"platform_name": ""})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.request = req
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        assert "required" in mock_msg.error.call_args[0][1].lower()

    def test_non_numeric_manufacturer_is_rejected(self):
        """A stale or forged manufacturer value must not remove the requested scope."""
        from dcim.models import Device, Platform

        device = make_device("plat-invalid-manufacturer")
        request = _make_request({"platform_name": "Invalid Manufacturer Platform", "manufacturer": "not-a-pk"})
        view = self._view(request)

        _post(view, request, pk=device.pk)

        assert not Platform.objects.filter(name="Invalid Manufacturer Platform").exists()
        assert Device.objects.get(pk=device.pk).platform_id is None
        assert message_texts(request, "error") == ["Selected manufacturer is not available."]

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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.request = req
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.request = req
            view.post(req, pk=dev.pk)
        # Reused — not duplicated.
        assert Platform.objects.filter(name__iexact="ios").count() == 1
        assert Device.objects.get(pk=dev.pk).platform_id == existing.pk
        mock_msg.success.assert_called_once()
        assert "already existed" in mock_msg.success.call_args[0][1].lower()

    def test_existing_platform_outside_the_view_grant_is_not_assigned(self):
        """Name-based reuse must not expose and assign a hidden Platform."""
        from dcim.models import Device, Platform

        hidden = Platform.objects.create(name="Hidden Reuse Platform", slug="hidden-reuse-platform")
        allowed = Platform.objects.create(name="Allowed Reuse Platform", slug="allowed-reuse-platform")
        device = make_device("platform-reuse-view-scope")
        user = make_user_with_perms("platform-reuse-view-scope", [("change", Device)])
        user = grant(user, "view", Platform, constraints={"pk": allowed.pk})
        request = _make_request(
            {"platform_name": hidden.name, "manufacturer": "", "create_mapping": ""},
            user=user,
        )
        view = self._view(request)

        _post(view, request, pk=device.pk)

        device.refresh_from_db()
        assert device.platform_id is None
        assert message_texts(request, "error") == ["Selected platform is not available."]

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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.request = req
            view.post(req, pk=dev.pk)
        assert Platform.objects.filter(name__iexact="ios").count() == 1  # reused
        # A real mapping now points the posted OS at the reused platform.
        mapping = PlatformMapping.objects.get(librenms_os__iexact="ios")
        assert mapping.netbox_platform_id == existing.pk
        assert Device.objects.get(pk=dev.pk).platform_id == existing.pk
        mock_msg.success.assert_called_once()

    def test_mapping_existing_points_to_different_platform_warns(self):
        """An existing OS mapping that targets a DIFFERENT platform must not be reported as 'already exists' — surface a warning and don't create a new mapping."""
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.models import PlatformMapping

        wanted = Platform.objects.create(name="ios", slug="ios")
        other = Platform.objects.create(name="junos", slug="junos")
        PlatformMapping.objects.create(librenms_os="ios", netbox_platform=other)
        dev = make_device("plat-map-conflict")
        req = _make_request({"platform_name": "ios", "manufacturer": "", "librenms_os": "ios", "create_mapping": "1"})
        view = self._view(req)

        _post(view, req, pk=dev.pk)

        # The pre-existing mapping is left pointing where it did, and no second one is added.
        assert PlatformMapping.objects.filter(librenms_os__iexact="ios").count() == 1
        assert PlatformMapping.objects.get(librenms_os__iexact="ios").netbox_platform_id == other.pk
        assert any("pointing to" in t for t in message_texts(req, "warning"))
        # The mapping conflict is non-fatal: the primary action (assign the found platform to
        # the device and persist it) must still happen, not warn-and-return.
        assert Device.objects.get(pk=dev.pk).platform_id == wanted.pk

    def test_manufacturer_not_found(self):
        """An unresolvable manufacturer ID stops the platform write."""
        from dcim.models import Device, Manufacturer, Platform

        dev = make_device("plat-nomanuf")
        missing_id = (Manufacturer.objects.order_by("-pk").first().pk if Manufacturer.objects.exists() else 0) + 1000
        req = _make_request({"platform_name": "ios", "manufacturer": str(missing_id)})
        view = self._view(req)

        _post(view, req, pk=dev.pk)

        assert not Platform.objects.filter(name="ios").exists()
        assert Device.objects.get(pk=dev.pk).platform_id is None
        assert message_texts(req, "error") == ["Selected manufacturer is not available."]

    def test_manufacturer_outside_the_grant_is_rejected(self):
        """A constrained grant must not let a hidden manufacturer become an unscoped platform."""
        from dcim.models import Device, Manufacturer, Platform

        Manufacturer.objects.create(name="Visible Vendor", slug="visible-vendor")
        hidden = Manufacturer.objects.create(name="Hidden Vendor", slug="hidden-vendor")
        device = make_device("plat-hidden-manufacturer")
        user = make_user_with_perms(
            "platform-manufacturer-scoped",
            [("change", Device), ("add", Platform)],
        )
        user = grant(user, "view", Manufacturer, constraints={"name": "Visible Vendor"})
        request = _make_request(
            {"platform_name": "Hidden Vendor Platform", "manufacturer": str(hidden.pk)},
            user=user,
        )
        view = self._view(request)

        _post(view, request, pk=device.pk)

        assert not Platform.objects.filter(name="Hidden Vendor Platform").exists()
        assert Device.objects.get(pk=device.pk).platform_id is None
        assert message_texts(request, "error") == ["Selected manufacturer is not available."]

    def test_success_no_manufacturer(self):
        """A new platform (no manufacturer) is created and assigned to the device (real)."""
        from dcim.models import Device, Platform

        view = self._view()
        dev = make_device("plat-create")
        req = _make_request({"platform_name": "ios-new", "manufacturer": ""})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.request = req
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev,
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.request = req
            view.post(req, pk=dev.pk)

        assert Platform.objects.get(name=platform_name).slug == slugify(platform_name)

    def test_platform_validation_error(self):
        """A real unique-slug collision aborts the create: nothing persists and the user is told."""
        from dcim.models import Device, Platform

        Platform.objects.create(name="Cisco IOS", slug="cisco-ios")
        dev = make_device("plat-badslug")
        # A DIFFERENT name that slugifies to the SAME slug: it misses the name__iexact reuse
        # short-circuit, so the create path runs and trips the real unique-slug validation.
        req = _make_request({"platform_name": "Cisco  IOS", "manufacturer": ""})
        view = self._view(req)

        _post(view, req, pk=dev.pk)

        assert any("could not be created" in t for t in message_texts(req, "error"))
        assert not Platform.objects.filter(name="Cisco  IOS").exists()
        assert Device.objects.get(pk=dev.pk).platform_id is None

    def test_device_does_not_exist_inside_transaction(self):
        """The device vanishing between the first lookup and the lock rolls the whole action back."""
        from dcim.models import Device, Platform

        dev = make_device("plat-vanishes")
        req = _make_request({"platform_name": "ios", "manufacturer": ""})
        view = self._view(req)

        with _deleted_when_platform_saved(dev):
            _post(view, req, pk=dev.pk)

        assert any("no longer exists" in t for t in message_texts(req, "error"))
        # set_rollback(True) unwinds the whole savepoint — the platform insert must not survive.
        assert not Platform.objects.filter(name="ios").exists()
        # The DELETE was issued inside that same savepoint, so it is unwound too (see the helper).
        assert Device.objects.filter(pk=dev.pk).exists()

    def test_device_validation_error(self):
        """A platform scoped to another vendor fails the device's real clean(); nothing persists."""
        from dcim.models import Device, Manufacturer, Platform

        other_vendor = Manufacturer.objects.create(name="OtherVendor", slug="other-vendor")
        dev = make_device("plat-vendor-clash")
        assert dev.device_type.manufacturer_id != other_vendor.pk
        # NetBox's Device.clean() rejects a platform limited to a different manufacturer.
        req = _make_request({"platform_name": "vendor-locked", "manufacturer": str(other_vendor.pk)})
        view = self._view(req)

        _post(view, req, pk=dev.pk)

        assert any("validation failed" in t for t in message_texts(req, "error"))
        assert not Platform.objects.filter(name="vendor-locked").exists()
        assert Device.objects.get(pk=dev.pk).platform_id is None

    def test_integrity_error(self):
        """An IntegrityError with no row behind it aborts the action rather than claiming success."""
        from dcim.models import Device, Platform
        from django.db import IntegrityError

        dev = make_device("plat-integrity")
        req = _make_request({"platform_name": "ios", "manufacturer": ""})
        view = self._view(req)

        # A lost insert race is not reproducible against a single local connection; inject it at
        # the one statement that would raise, leaving the manager, the queries and the
        # transaction real. The re-query then finds nothing, which is the branch under test.
        with patch.object(Platform, "save", side_effect=IntegrityError("duplicate key")):
            _post(view, req, pk=dev.pk)

        assert any("could not be created" in t for t in message_texts(req, "error"))
        assert not Platform.objects.filter(name__iexact="ios").exists()
        assert Device.objects.get(pk=dev.pk).platform_id is None

    def test_integrity_error_reuses_concurrently_created_platform(self):
        """IntegrityError on create, but the same-named platform now exists (a concurrent insert won the race): reuse it and assign — no error, no rollback."""
        from dcim.models import Device, Platform

        dev = make_device("plat-race")
        req = _make_request({"platform_name": "ios", "manufacturer": ""})
        view = self._view(req)

        def _rival_commits_first():
            Platform.objects.create(name="ios", slug="ios")

        # Land the rival's row after the view's up-front existence check, so the view takes the
        # create path; the hook runs outside the view's savepoint, so the rollback of the failed
        # insert cannot undo it. The rival is in place before the view validates, which real
        # concurrency would not guarantee, so full_clean is skipped for that one insert to model
        # a rival that committed after validation. The IntegrityError itself is then raised by
        # the DB's unique constraint, and the re-query that follows is entirely real.
        with (
            _before_restricted_read(view, _rival_commits_first, on_call=1),
            # Skip validation only for the simulated rival insert. The view's candidate still
            # reaches the unique constraint and exercises its IntegrityError recovery path.
            patch.object(Platform, "full_clean", _skip_once(Platform.full_clean)),
        ):
            _post(view, req, pk=dev.pk)

        winner = Platform.objects.get(name__iexact="ios")
        assert Platform.objects.filter(name__iexact="ios").count() == 1
        # The reuse path must still assign AND persist — a regression that wires up the FK but
        # skips the save would leave the device unchanged here.
        assert Device.objects.get(pk=dev.pk).platform_id == winner.pk
        assert not message_texts(req, "error")
        assert message_texts(req, "success")

    def test_integrity_error_reuse_succeeds_for_an_add_only_user(self):
        """Verify an add-only user can reuse the race winner without platform view permission."""
        from dcim.models import Device, Platform

        device = make_device("plat-race-add-only")
        user = make_user_with_perms("plat-race-add-only", [("change", Device), ("add", Platform)])
        request = _make_request(
            {"platform_name": "AddOnlyRacePlatform", "manufacturer": "", "create_mapping": ""},
            user=user,
        )
        view = self._view(request)

        def _rival_commits_first():
            Platform.objects.create(name="AddOnlyRacePlatform", slug="addonlyraceplatform")

        with (
            _before_restricted_read(view, _rival_commits_first, on_call=1),
            patch.object(Platform, "full_clean", _skip_once(Platform.full_clean)),
        ):
            _post(view, request, pk=device.pk)

        winner = Platform.objects.get(name__iexact="AddOnlyRacePlatform")
        assert Platform.objects.filter(name__iexact="AddOnlyRacePlatform").count() == 1
        device.refresh_from_db()
        assert device.platform_id == winner.pk
        assert message_texts(request, "error") == []

    def _success_setup(self, platform_name="ios", librenms_os="ios", create_mapping="1", user=None):
        """Return ``(view, request, device)`` for a run that reaches the mapping block."""
        from django.utils.text import slugify

        req = _make_request(
            {
                "platform_name": platform_name,
                "manufacturer": "",
                "librenms_os": librenms_os,
                "create_mapping": create_mapping,
            },
            user=user,
        )
        dev = make_device(f"plat-map-{slugify(platform_name)}-{create_mapping or 'off'}")
        return self._view(req), req, dev

    def test_mapping_created_when_name_differs(self):
        """A PlatformMapping is created when name differs from librenms_os and checkbox is on."""
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.models import PlatformMapping

        view, req, dev = self._success_setup(platform_name="Cisco IOS", librenms_os="ios", create_mapping="1")

        _post(view, req, pk=dev.pk)

        platform = Platform.objects.get(name="Cisco IOS")
        mapping = PlatformMapping.objects.get(librenms_os="ios")
        assert mapping.netbox_platform_id == platform.pk
        assert Device.objects.get(pk=dev.pk).platform_id == platform.pk
        assert any("platform mapping" in t for t in message_texts(req, "success"))

    def test_mapping_skipped_when_checkbox_off(self):
        """No PlatformMapping is created when checkbox is unchecked."""
        from netbox_librenms_plugin.models import PlatformMapping

        view, req, dev = self._success_setup(platform_name="Cisco IOS", librenms_os="ios", create_mapping="")

        _post(view, req, pk=dev.pk)

        assert not PlatformMapping.objects.filter(librenms_os="ios").exists()

    def test_mapping_skipped_when_already_exists(self):
        """No duplicate PlatformMapping is created when one already exists for the OS."""
        from dcim.models import Platform

        from netbox_librenms_plugin.models import PlatformMapping

        platform = Platform.objects.create(name="Cisco IOS", slug="cisco-ios")
        PlatformMapping.objects.create(librenms_os="ios", netbox_platform=platform)
        view, req, dev = self._success_setup(platform_name="Cisco IOS", librenms_os="ios", create_mapping="1")

        _post(view, req, pk=dev.pk)

        assert PlatformMapping.objects.filter(librenms_os__iexact="ios").count() == 1
        assert any("already exists" in t for t in message_texts(req, "info"))

    def test_mapping_skipped_when_lacking_add_perm_at_write(self):
        """A user who may assign platforms but not add mappings gets the platform and a warning."""
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.models import PlatformMapping

        # The upfront gate deliberately omits ('add', PlatformMapping) — the write-site re-check
        # is the only thing standing between this user and a mapping they may not create.
        user = make_user_with_perms("plat-nomapping", [("change", Device), ("add", Platform)])
        assert not user.has_perm("netbox_librenms_plugin.add_platformmapping")
        view, req, dev = self._success_setup(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping="1", user=user
        )

        _post(view, req, pk=dev.pk)

        assert not PlatformMapping.objects.exists()
        assert any("not created" in t for t in message_texts(req, "warning"))
        # The platform itself must still be assigned and persisted — only the secondary mapping
        # is skipped, so this branch can't silently return pre-persist.
        platform = Platform.objects.get(name="Cisco IOS")
        assert Device.objects.get(pk=dev.pk).platform_id == platform.pk

    def _capture_required_perms(self, view):
        """Record the perms the view computes for POST without changing what the gate decides."""
        captured = {}
        real = view.require_all_permissions

        def _spy(method="POST"):
            captured["perms"] = list(view.required_object_permissions.get(method, []))
            return real(method)

        view.require_all_permissions = _spy
        return captured

    def test_required_object_permissions_never_include_platformmapping_upfront(self):
        """Even when create_mapping is on, an OS is supplied, and no mapping exists yet, the upfront POST gate must NOT require ('add', PlatformMapping): assigning the platform is the primary action and must not be blocked for a user who can't create mappings."""
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.models import PlatformMapping

        user = make_user_with_perms("perms-upfront", [("change", Device), ("add", Platform)])
        view, req, dev = self._success_setup(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping="1", user=user
        )
        captured = self._capture_required_perms(view)

        _post(view, req, pk=dev.pk)

        assert ("add", PlatformMapping) not in captured["perms"], (
            "('add', PlatformMapping) must not gate the upfront POST — the mapping is gated "
            "at the write site so the primary platform-assign isn't blocked"
        )
        # And the gate really let this user through: the platform was assigned for real.
        assert Device.objects.get(pk=dev.pk).platform_id == Platform.objects.get(name="Cisco IOS").pk

    def test_required_object_permissions_exclude_platformmapping_when_mapping_exists(self):
        """create_mapping on but a mapping for the OS already exists → no mapping write occurs, so ('add', PlatformMapping) must NOT be required (don't block the assign)."""
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.models import PlatformMapping

        platform = Platform.objects.create(name="Cisco IOS", slug="cisco-ios")
        PlatformMapping.objects.create(librenms_os="ios", netbox_platform=platform)
        user = make_user_with_perms("perms-mapexists", [("change", Device), ("view", Platform)])
        view, req, dev = self._success_setup(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping="1", user=user
        )
        captured = self._capture_required_perms(view)

        _post(view, req, pk=dev.pk)

        assert ("add", PlatformMapping) not in captured["perms"], (
            "Did not expect ('add', PlatformMapping) when a mapping for the OS already exists"
        )
        assert Device.objects.get(pk=dev.pk).platform_id == platform.pk

    def test_required_object_permissions_exclude_platformmapping_when_no_create_mapping(self):
        """When create_mapping is NOT checked, ('add', PlatformMapping) must NOT be added."""
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.models import PlatformMapping

        user = make_user_with_perms("perms-nomapping", [("change", Device), ("add", Platform)])
        view, req, dev = self._success_setup(platform_name="Cisco IOS", librenms_os="ios", create_mapping="", user=user)
        captured = self._capture_required_perms(view)

        _post(view, req, pk=dev.pk)

        assert ("add", PlatformMapping) not in captured["perms"], (
            "Did not expect ('add', PlatformMapping) when create_mapping is unchecked"
        )
        assert Device.objects.get(pk=dev.pk).platform_id == Platform.objects.get(name="Cisco IOS").pk


# ---------------------------------------------------------------------------
# AssignVCSerialView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAssignVCSerialView:
    def _view(self, request=None):
        from netbox_librenms_plugin.views.sync.device_fields import AssignVCSerialView

        return _make_view(AssignVCSerialView, request)

    def _vc(self, tag, member_serials=("OLD",)):
        """A real VirtualChassis: a host at position 1, then one member per entry."""
        from dcim.models import VirtualChassis

        vc = VirtualChassis.objects.create(name=f"vc-{tag}")
        host = make_device(f"vc-{tag}-host")
        host.virtual_chassis = vc
        host.vc_position = 1
        host.save()
        members = []
        for i, serial in enumerate(member_serials, start=2):
            member = make_device(f"vc-{tag}-member-{i}", serial=serial)
            member.virtual_chassis = vc
            member.vc_position = i
            member.save()
            members.append(member)
        return vc, host, members

    def test_member_save_success_persists_serial(self):
        """A serial is assigned to a real VC member and persisted (verified via DB reload)."""
        from dcim.models import Device

        _vc, host, (member,) = self._vc("serial")
        req = _make_request({"serial_1": "SN100", "member_id_1": str(member.pk)})

        _post(self._view(req), req, pk=host.pk)

        assert message_texts(req, "success")
        assert Device.objects.get(pk=member.pk).serial == "SN100"  # real save committed

    def test_member_serial_is_normalized_before_persisting(self):
        """Whitespace from the posted LibreNMS VC inventory is stripped before Device.save()."""
        from dcim.models import Device

        _vc, host, (member,) = self._vc("padded")
        req = _make_request({"serial_1": "\t SN-VC-1 \n", "member_id_1": str(member.pk)})

        _post(self._view(req), req, pk=host.pk)

        assert Device.objects.get(pk=member.pk).serial == "SN-VC-1"

    def test_permission_denied(self):
        """A user without change_device never reaches the assignment loop."""
        from dcim.models import Device

        _vc, host, (member,) = self._vc("denied")
        user = make_user_with_perms("vc-serial-viewer", [("view", Device)])
        req = _make_request({"serial_1": "SN100", "member_id_1": str(member.pk)}, user=user)

        _post(self._view(req), req, pk=host.pk)

        assert Device.objects.get(pk=member.pk).serial == "OLD"
        assert any("Missing permissions" in t for t in message_texts(req, "error"))

    def test_not_virtual_chassis(self):
        dev = make_device("vc-standalone")
        req = _make_request()

        _post(self._view(req), req, pk=dev.pk)

        assert any("not part of a virtual chassis" in t for t in message_texts(req, "error"))

    def test_no_serial_assignments_no_errors(self):
        """Loop doesn't execute — no serial_N keys in POST."""
        _vc, host, _members = self._vc("noserials")
        req = _make_request({})

        _post(self._view(req), req, pk=host.pk)

        assert message_texts(req, "info") == ["No serial assignments were made"]

    def test_member_id_missing(self):
        """member_id_{N} key is empty → counter incremented, no assignment."""
        _vc, host, _members = self._vc("nomemberid")
        req = _make_request({"serial_1": "SN100", "member_id_1": ""})

        _post(self._view(req), req, pk=host.pk)

        assert message_texts(req, "info") == ["No serial assignments were made"]

    def test_member_not_found(self):
        from dcim.models import Device

        _vc, host, _members = self._vc("missing")
        gone = make_device("vc-missing-gone")
        gone_pk = gone.pk
        gone.delete()
        req = _make_request({"serial_1": "SN100", "member_id_1": str(gone_pk)})

        _post(self._view(req), req, pk=host.pk)

        assert any(f"Device with ID {gone_pk} not found" in t for t in message_texts(req, "error"))
        assert not Device.objects.filter(pk=gone_pk).exists()

    def test_member_outside_the_grant_is_reported_as_not_found(self):
        """A constrained grant must not let a raw member pk reach the serial write."""
        from dcim.models import Device

        _vc, host, (member,) = self._vc("scoped")
        # The grant covers the host but not the member, though both share the chassis: the
        # same-VC check alone would happily overwrite the member's serial.
        user = make_user_with_perms("vc-serial-scoped", [("change", Device)], constraints={"name": "vc-scoped-host"})
        req = _make_request({"serial_1": "SN100", "member_id_1": str(member.pk)}, user=user)

        _post(self._view(req), req, pk=host.pk)

        assert Device.objects.get(pk=member.pk).serial == "OLD"
        assert any(f"Device with ID {member.pk} not found" in t for t in message_texts(req, "error"))

    def test_member_different_chassis(self):
        from dcim.models import Device

        _vc, host, _members = self._vc("chassis-a")
        _other_vc, _other_host, (outsider,) = self._vc("chassis-b")
        req = _make_request({"serial_1": "SN100", "member_id_1": str(outsider.pk)})

        _post(self._view(req), req, pk=host.pk)

        assert any("not part of the same virtual chassis" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=outsider.pk).serial == "OLD"

    def test_member_save_validation_error(self):
        """A serial the field itself rejects is reported per member, and the old value stands."""
        from dcim.models import Device

        _vc, host, (member,) = self._vc("badserial")
        too_long = "S" * (Device._meta.get_field("serial").max_length + 1)
        req = _make_request({"serial_1": too_long, "member_id_1": str(member.pk)})

        _post(self._view(req), req, pk=host.pk)

        assert any(f"Failed to set serial on {member.name}" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=member.pk).serial == "OLD"

    def test_assignments_and_errors_both_reported(self):
        """One success + one rejected serial → both messages, and only the good one persists."""
        from dcim.models import Device

        _vc, host, (good, bad) = self._vc("mixed", member_serials=("", ""))
        too_long = "S" * (Device._meta.get_field("serial").max_length + 1)
        req = _make_request(
            {
                "serial_1": "SN001",
                "member_id_1": str(good.pk),
                "serial_2": too_long,
                "member_id_2": str(bad.pk),
            }
        )

        _post(self._view(req), req, pk=host.pk)

        assert any("Successfully assigned 1 serial" in t for t in message_texts(req, "success"))
        assert any(f"Failed to set serial on {bad.name}" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=good.pk).serial == "SN001"
        assert Device.objects.get(pk=bad.pk).serial == ""


# ---------------------------------------------------------------------------
# RemoveServerMappingView — helper methods
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRemoveServerMappingViewHelpers:
    def _view(self, request=None):
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        return _make_view(RemoveServerMappingView, request)

    def test_get_object_device(self):
        view = self._view()
        mock_device = MagicMock()

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_device,
        ):
            obj, model = view._get_object("device", 1)
        assert obj is mock_device

    def test_get_object_vm(self):
        view = self._view()
        mock_vm = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_vm,
        ):
            obj, model = view._get_object("vm", 1)
        assert obj is mock_vm

    def test_sync_url_name_device(self):
        from netbox_librenms_plugin.views.sync.device_fields import _sync_url_name

        assert _sync_url_name("device") == "plugins:netbox_librenms_plugin:device_librenms_sync"

    def test_sync_url_name_vm(self):
        from netbox_librenms_plugin.views.sync.device_fields import _sync_url_name

        assert _sync_url_name("vm") == "plugins:netbox_librenms_plugin:vm_librenms_sync"

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
    def _view(self, request=None):
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        return _make_view(RemoveServerMappingView, request)

    @staticmethod
    def _assert_tab_redirect(response, object_type, pk, tab="modules"):
        """Assert that a removal outcome returns to the validated submitted sync tab."""
        from django.urls import reverse

        url_name = "vm_librenms_sync" if object_type == "vm" else "device_librenms_sync"
        url = reverse(f"plugins:netbox_librenms_plugin:{url_name}", kwargs={"pk": pk})
        assert response.url == f"{url}?tab={tab}"

    def test_invalid_object_type_returns_400(self):
        req = _make_request({"object_type": "badtype"})

        result = _post(self._view(req), req, pk=1)

        assert result.status_code == 400

    def test_virtualmachine_object_type_normalized_to_vm(self):
        """object_type='virtualmachine' is normalised to 'vm' and the VM's mapping is removed."""
        from virtualization.models import VirtualMachine

        vm = make_vm("rm-vm-orphan")
        vm.custom_field_data["librenms_id"] = {"orphan": 5}
        vm.save()
        req = _make_request({"object_type": "virtualmachine", "server_key": "orphan", "tab": "modules"})

        with _plugins_config(servers={}, librenms_url=""):
            response = _post(self._view(req), req, pk=vm.pk)

        self._assert_tab_redirect(response, "vm", vm.pk)
        assert message_texts(req, "success")
        assert VirtualMachine.objects.get(pk=vm.pk).custom_field_data["librenms_id"] is None

    def test_permission_denied(self):
        """Without change_device the orphaned mapping survives."""
        from dcim.models import Device

        dev = make_device("rm-denied", librenms_cf={"orphan": 5})
        user = make_user_with_perms("rm-viewer", [("view", Device)])
        req = _make_request({"object_type": "device", "server_key": "orphan"}, user=user)

        with _plugins_config(servers={}, librenms_url=""):
            _post(self._view(req), req, pk=dev.pk)

        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"orphan": 5}
        assert any("Missing permissions" in t for t in message_texts(req, "error"))

    def test_no_server_key(self):
        dev = make_device("rm-nokey", librenms_cf={"orphan": 5})
        req = _make_request({"object_type": "device", "server_key": "", "tab": "modules"})

        response = _post(self._view(req), req, pk=dev.pk)

        self._assert_tab_redirect(response, "device", dev.pk)
        assert message_texts(req, "error") == ["No server_key provided."]

    def test_invalid_server_key_preserves_tab(self):
        """A rejected server key returns to the validated submitted sync tab."""
        dev = make_device("rm-invalid-key", librenms_cf={"dc__west": 5})
        req = _make_request({"object_type": "device", "server_key": "dc__west", "tab": "modules"})

        response = _post(self._view(req), req, pk=dev.pk)

        self._assert_tab_redirect(response, "device", dev.pk)
        assert message_texts(req, "error") == ["LibreNMS server key must not contain '__'."]

    def test_invalid_tab_is_not_reflected(self):
        """An unknown submitted tab is omitted from the redirect URL."""
        from django.urls import reverse

        dev = make_device("rm-invalid-tab", librenms_cf={"orphan": 5})
        req = _make_request({"object_type": "device", "server_key": "", "tab": "settings"})

        response = _post(self._view(req), req, pk=dev.pk)

        assert response.url == reverse(
            "plugins:netbox_librenms_plugin:device_librenms_sync",
            kwargs={"pk": dev.pk},
        )

    def test_mapping_not_found_wrong_type(self):
        """cf_value is not a dict → warning."""
        dev = make_device("rm-nulcf", librenms_cf=None)
        req = _make_request({"object_type": "device", "server_key": "default"})

        _post(self._view(req), req, pk=dev.pk)

        assert message_texts(req, "warning") == ["No mapping found for server 'default'."]

    def test_mapping_not_found_missing_key(self):
        """server_key not in cf_value dict → warning."""
        dev = make_device("rm-otherkey", librenms_cf={"other": 5})
        req = _make_request({"object_type": "device", "server_key": "default", "tab": "modules"})

        response = _post(self._view(req), req, pk=dev.pk)

        self._assert_tab_redirect(response, "device", dev.pk)
        assert message_texts(req, "warning") == ["No mapping found for server 'default'."]

    def test_configured_servers_non_dict_treated_as_empty(self):
        """A non-dict servers config is normalised to {}, so the orphan key can be removed."""
        from dcim.models import Device

        dev = make_device("rm-badcfg", librenms_cf={"orphan": 5})
        req = _make_request({"object_type": "device", "server_key": "orphan"})

        with _plugins_config(servers=["not", "a", "dict"], librenms_url=""):
            _post(self._view(req), req, pk=dev.pk)

        assert message_texts(req, "success")
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] is None

    def test_configured_server_key_in_servers_dict(self):
        """server_key is in configured servers → refused, mapping untouched."""
        from dcim.models import Device

        dev = make_device("rm-configured", librenms_cf={"production": 10})
        req = _make_request({"object_type": "device", "server_key": "production", "tab": "modules"})

        with _plugins_config(servers={"production": {}}, librenms_url=""):
            response = _post(self._view(req), req, pk=dev.pk)

        self._assert_tab_redirect(response, "device", dev.pk)
        assert any("Cannot remove" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"production": 10}

    def test_legacy_default_server_protected(self):
        """Legacy mode with librenms_url set and server_key='default' → refused."""
        from dcim.models import Device

        dev = make_device("rm-legacy", librenms_cf={"default": 7})
        req = _make_request({"object_type": "device", "server_key": "default"})

        with _plugins_config(servers={}, librenms_url="https://librenms.example.com"):
            _post(self._view(req), req, pk=dev.pk)

        assert any("Cannot remove" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"default": 7}

    def test_object_no_longer_exists_inside_transaction(self):
        """The row is deleted between the first read and the lock: report, don't crash."""
        from dcim.models import Device

        dev = make_device("rm-vanishes", librenms_cf={"orphan": 5})
        req = _make_request({"object_type": "device", "server_key": "orphan", "tab": "modules"})
        view = self._view(req)

        with _plugins_config(servers={}, librenms_url=""), _deleted_before_lock(view, dev):
            response = _post(view, req, pk=dev.pk)

        self._assert_tab_redirect(response, "device", dev.pk)
        assert message_texts(req, "error") == ["Device no longer exists."]
        assert not Device.objects.filter(pk=dev.pk).exists()

    def test_mapping_already_removed_in_lock(self):
        """server_key is gone from the locked row's cf → warning, no write."""
        from dcim.models import Device

        dev = make_device("rm-raced", librenms_cf={"orphan": 5})
        req = _make_request({"object_type": "device", "server_key": "orphan"})
        view = self._view(req)

        def _drop_the_key():
            Device.objects.filter(pk=dev.pk).update(custom_field_data={"librenms_id": {}})

        with _plugins_config(servers={}, librenms_url=""), _before_lock(view, _drop_the_key):
            _post(view, req, pk=dev.pk)

        assert message_texts(req, "warning") == ["Mapping for server 'orphan' was already removed."]

    def test_validation_error_on_save(self):
        """A rejected write is surfaced and rolled back, leaving the mapping in place."""
        from dcim.models import Device
        from django.core.exceptions import ValidationError

        dev = make_device("rm-validationerr", librenms_cf={"orphan": 5})
        req = _make_request({"object_type": "device", "server_key": "orphan", "tab": "modules"})

        # full_clean() accepts any dict for this custom field, so the rejection has to be
        # injected; the manager, the lock and the transaction all stay real.
        with (
            _plugins_config(servers={}, librenms_url=""),
            patch.object(Device, "full_clean", side_effect=ValidationError({"custom_field_data": ["err"]})),
        ):
            response = _post(self._view(req), req, pk=dev.pk)

        self._assert_tab_redirect(response, "device", dev.pk)
        assert any("Validation error removing" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"orphan": 5}

    def test_unexpected_error_on_save(self):
        """A non-ValidationError is caught, reported and rolled back rather than 500ing."""
        from dcim.models import Device
        from django.db import OperationalError

        dev = make_device("rm-unexpected", librenms_cf={"orphan": 5})
        req = _make_request({"object_type": "device", "server_key": "orphan", "tab": "modules"})

        # Injected at the persistence boundary, which is where an unexpected failure of this
        # kind actually originates; full_clean still runs for real ahead of it.
        with (
            _plugins_config(servers={}, librenms_url=""),
            patch.object(Device, "save", side_effect=OperationalError("disk full")),
        ):
            response = _post(self._view(req), req, pk=dev.pk)

        self._assert_tab_redirect(response, "device", dev.pk)
        assert any("Unexpected error removing" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"orphan": 5}

    def test_success_removes_mapping(self):
        """Happy path: an UNCONFIGURED server's mapping is removed; last entry → cf None."""
        from dcim.models import Device

        view = self._view()
        dev = make_device("rm-orphan", librenms_cf={"orphan": 5})
        req = _make_request({"object_type": "device", "server_key": "orphan", "tab": "modules"})

        with _plugins_config(servers={}, librenms_url=""):
            response = _post(view, req, pk=dev.pk)

        self._assert_tab_redirect(response, "device", dev.pk)
        assert message_texts(req, "success") == ["Removed LibreNMS mapping for server 'orphan'."]
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
            _post(view, req, pk=dev.pk)
        mock_msg.success.assert_called_once()
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"other": 6}


# ---------------------------------------------------------------------------
# ConvertLegacyLibreNMSIdView — helper methods
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestConvertLegacyLibreNMSIdViewHelpers:
    def _view(self, request=None):
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        return _make_view(ConvertLegacyLibreNMSIdView, request)

    def test_get_model_and_object_device(self):
        from dcim.models import Device

        dev = make_device("helper-device")
        view = self._view()

        model, obj = view._get_model_and_object("device", dev.pk)

        assert model is Device
        assert obj == dev

    def test_get_model_and_object_vm(self):
        from virtualization.models import VirtualMachine

        vm = make_vm("helper-vm")
        view = self._view()

        model, obj = view._get_model_and_object("vm", vm.pk)

        assert model is VirtualMachine
        assert obj == vm

    def test_get_model_and_object_404s_outside_the_grant(self):
        """The helper resolves a client-supplied pk, so it must fail closed on a constrained grant."""
        from dcim.models import Device
        from django.http import Http404

        make_device("helper-mine")
        theirs = make_device("helper-theirs")
        user = make_user_with_perms("helper-scoped", [("change", Device)], constraints={"name": "helper-mine"})
        view = self._view(_make_request(user=user))

        with pytest.raises(Http404):
            view._get_model_and_object("device", theirs.pk)

    def test_sync_url_device(self):
        """Invoked outside dispatch (no bound request) the helper emits the bare sync URL."""
        from django.urls import reverse

        view = self._view()
        del view.request

        response = view._sync_url("device", 1)

        assert "server_key" not in response["Location"]
        assert response["Location"] == reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": 1})

    def test_sync_url_vm(self):
        from django.urls import reverse

        view = self._view()
        del view.request

        response = view._sync_url("vm", 1)

        assert "server_key" not in response["Location"]
        assert response["Location"] == reverse("plugins:netbox_librenms_plugin:vm_librenms_sync", kwargs={"pk": 1})

    @staticmethod
    def _request(post):
        """A MagicMock request with concrete host/scheme so the real open-redirect barrier evaluates deterministically."""
        request = MagicMock()
        request.POST = post
        request.GET = {}
        request.get_host.return_value = "testserver"
        request.is_secure.return_value = False
        return request

    def test_sync_url_propagates_known_server_key(self):
        """A POST-scoped server_key that matches a configured server is preserved so multi-server users return to the server they were working in."""
        view = self._view()
        view.request = self._request({"server_key": "prod"})
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"prod": "Prod LibreNMS"},
        ):
            # No redirect patch: exercise the real redirect + shared redirect_with_server_key barrier.
            response = view._sync_url("device", 1)
        assert "server_key=prod" in response["Location"]

    def test_sync_url_stale_post_key_falls_back_to_active_server(self):
        """A POST server_key that is no longer configured (stale page / removed server) must not drop the server context: it doesn't match the allowlist, so the redirect falls back to the active/default server the action ran against (here the bound _librenms_api='default'), re-validated through the allowlist — instead of emitting a bare URL."""
        view = self._view()  # _view() binds _librenms_api = MagicMock(server_key="default")
        view.request = self._request({"server_key": "ghost"})  # unconfigured / stale
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"default": "Default LibreNMS"},
        ):
            response = view._sync_url("device", 1)
        assert "server_key=default" in response["Location"]
        assert "ghost" not in response["Location"]

    def test_sync_url_drops_unknown_server_key(self):
        """An unconfigured/spoofed server_key is not reflected into the redirect URL (allowlist guard — open-redirect safe)."""
        view = self._view()
        view.request = self._request({"server_key": "//evil.com/steal"})
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"prod": "Prod LibreNMS"},
        ):
            response = view._sync_url("device", 1)
        assert "evil.com" not in response["Location"]
        assert "server_key" not in response["Location"]

    def test_sync_url_unbound_api_misconfigured_default_degrades_without_500(self):
        """On a redirect after a failed rebind, _librenms_api is unbound (None) and the request carries no server_key."""
        view = self._view()
        view._librenms_api = None
        view.request = self._request({})
        with patch(
            # Property construction would raise; _sync_url must not touch it (uses the _librenms_api attr).
            "netbox_librenms_plugin.views.mixins.LibreNMSAPI",
            side_effect=KeyError("ghost"),
        ):
            response = view._sync_url("device", 1)  # must not raise
        assert "server_key" not in response["Location"]
        # Prove the claim explicitly: _sync_url read the _librenms_api attr (None) and never
        # triggered the lazy librenms_api property (which constructs the patched-to-raise
        # LibreNMSAPI from views.mixins) — otherwise _librenms_api would be set here or it'd raise.
        assert view._librenms_api is None

    def test_sync_url_drops_server_key_when_url_validation_fails(self):
        """Even for an allowlisted server_key, if url_has_allowed_host_and_scheme rejects the candidate (the CodeQL open-redirect barrier — now shared via redirect_with_server_key), fall back to the bare URL."""
        view = self._view()
        view.request = self._request({"server_key": "prod"})
        with (
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"prod": "Prod LibreNMS"},
            ),
            # Barrier now lives in the shared helper, so patch it where redirect_with_server_key uses it.
            patch(
                "netbox_librenms_plugin.views.mixins.url_has_allowed_host_and_scheme",
                return_value=False,
            ),
        ):
            response = view._sync_url("device", 1)
        assert "server_key" not in response["Location"]


# ---------------------------------------------------------------------------
# ConvertLegacyLibreNMSIdView — post()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestConvertLegacyLibreNMSIdViewPost:
    def _view(self, request=None):
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        return _make_view(ConvertLegacyLibreNMSIdView, request)

    def _device_view(self, dev, *, librenms_serial="SN-MATCH", post=None):
        """Return ``(view, request)`` for converting *dev*, with LibreNMS reporting *librenms_serial*."""
        req = _make_request({"object_type": "device", **(post or {})})
        view = self._view(req)
        view._librenms_api.get_device_info.return_value = (True, {"serial": librenms_serial})
        return view, req

    def test_invalid_object_type_returns_400(self):
        req = _make_request({"object_type": "badtype"})

        result = _post(self._view(req), req, pk=1)

        assert result.status_code == 400

    def test_virtualmachine_object_type_normalised(self):
        """object_type='virtualmachine' is accepted as 'vm' and the VM's legacy id converts."""
        from virtualization.models import VirtualMachine

        vm = make_vm("convert-vm")
        vm.custom_field_data["librenms_id"] = "42"
        vm.save()
        req = _make_request({"object_type": "virtualmachine"})
        view = self._view(req)
        # VMs have no serial field, so the serial gate is skipped for them.
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-ANY"})

        _post(view, req, pk=vm.pk)

        assert message_texts(req, "success")
        assert VirtualMachine.objects.get(pk=vm.pk).custom_field_data["librenms_id"] == {"default": 42}

    def test_whitespace_padded_legacy_id_is_convertible_not_dead_end(self):
        """A padded legacy id (' 42 ') the badge shows via is_legacy_librenms_id must convert, not hit the isdigit() dead-end."""
        from dcim.models import Device

        dev = make_device("convert-padded", serial="SN-MATCH", librenms_cf=" 42 ")
        view, req = self._device_view(dev)

        _post(view, req, pk=dev.pk)

        # Unfixed: ' 42 '.isdigit() is False → error "not a valid integer" and no conversion.
        assert not any("not a valid integer" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"default": 42}
        # The serial gate must read live info (use_cache=False), not a stale sync-tab cache snapshot.
        assert view._librenms_api.get_device_info.call_args.kwargs.get("use_cache") is False

    def test_numeric_librenms_serial_passes_the_serial_gate(self):
        """An all-digit LibreNMS serial can arrive as an int; the gate must coerce it before stripping, not raise."""
        from dcim.models import Device

        dev = make_device("convert-numserial", serial="987654", librenms_cf="42")
        req = _make_request({"object_type": "device"})
        view = self._view(req)
        view._librenms_api.get_device_info.return_value = (True, {"serial": 987654})

        _post(view, req, pk=dev.pk)

        assert not any("Serial number mismatch" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"default": 42}

    def test_permission_denied(self):
        """Without change_device the legacy id is left alone."""
        from dcim.models import Device

        dev = make_device("convert-denied", serial="SN-MATCH", librenms_cf=42)
        user = make_user_with_perms("convert-viewer", [("view", Device)])
        req = _make_request({"object_type": "device"}, user=user)
        view = self._view(req)
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})

        _post(view, req, pk=dev.pk)

        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == 42
        assert any("Missing permissions" in t for t in message_texts(req, "error"))

    def test_already_json_format_dict(self):
        from dcim.models import Device

        dev = make_device("convert-alreadyjson", serial="SN-MATCH", librenms_cf={"default": 5})
        view, req = self._device_view(dev)

        _post(view, req, pk=dev.pk)

        assert any("already" in t.lower() for t in message_texts(req, "warning"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"default": 5}

    def test_already_json_format_bool(self):
        dev = make_device("convert-bool", serial="SN-MATCH", librenms_cf=True)
        view, req = self._device_view(dev)

        _post(view, req, pk=dev.pk)

        assert message_texts(req, "error") == ["librenms_id has an invalid boolean value; cannot convert."]

    def test_non_digit_string_cf_value(self):
        dev = make_device("convert-nondigit", serial="SN-MATCH", librenms_cf="not-a-number")
        view, req = self._device_view(dev)

        _post(view, req, pk=dev.pk)

        assert message_texts(req, "error") == ["librenms_id is not a valid integer; cannot convert."]

    def test_convert_refused_when_id_collides_with_another_devices_oob(self):
        """Fail-closed pin: converting a legacy id is refused when another device uses it as its OOB controller id."""
        from dcim.models import Device

        # Device A: legacy bare-int librenms_id 42, serial matches LibreNMS so the convert proceeds
        # to the conflict check.
        dev_a = make_device("convert-a", serial="SN-A", librenms_cf=42)
        # Device B: a DIFFERENT device using 42 as its OOB controller id under "default" — only the
        # OOB sub-key query (oob_q) surfaces this collision.
        make_device("convert-b", librenms_cf={"default": {"id": 99, "oob": {"id": 42}}})
        view, req = self._device_view(dev_a, librenms_serial="SN-A")

        _post(view, req, pk=dev_a.pk)

        # Refused (fail closed): an error, never a success, and the legacy id is left untouched
        # (NOT silently converted to the JSON dict form).
        assert not message_texts(req, "success")
        errors = message_texts(req, "error")
        assert len(errors) == 1
        assert "ambiguous" in errors[0].lower() or "already has" in errors[0].lower()
        assert Device.objects.get(pk=dev_a.pk).custom_field_data["librenms_id"] == 42

    def test_get_device_info_failure(self):
        from dcim.models import Device

        dev = make_device("convert-infofail", serial="SN-MATCH", librenms_cf=42)
        req = _make_request({"object_type": "device"})
        view = self._view(req)
        view._librenms_api.get_device_info.return_value = (False, None)

        _post(view, req, pk=dev.pk)

        assert message_texts(req, "error") == ["Could not retrieve device info from LibreNMS to verify serial."]
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == 42

    def test_serial_mismatch_empty_netbox_serial(self):
        from dcim.models import Device

        dev = make_device("convert-noserial", serial="", librenms_cf=42)
        view, req = self._device_view(dev, librenms_serial="SN-ABC")

        _post(view, req, pk=dev.pk)

        assert any("Serial number mismatch" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == 42

    def test_serial_mismatch_different(self):
        from dcim.models import Device

        dev = make_device("convert-wrongserial", serial="SN-XYZ", librenms_cf=42)
        view, req = self._device_view(dev, librenms_serial="SN-ABC")

        _post(view, req, pk=dev.pk)

        assert any("Serial number mismatch" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == 42

    def test_object_no_longer_exists_in_lock(self):
        """The row is deleted between the serial check and the lock: report, don't crash."""
        from dcim.models import Device

        dev = make_device("convert-vanishes", serial="SN-MATCH", librenms_cf=42)
        view, req = self._device_view(dev)

        with _deleted_before_lock(view, dev):
            _post(view, req, pk=dev.pk)

        assert message_texts(req, "error") == ["Device no longer exists."]
        assert not Device.objects.filter(pk=dev.pk).exists()

    def test_cf_value_changed_to_json_after_lock(self):
        """Another session converted it first: warn, don't convert twice."""
        from dcim.models import Device

        dev = make_device("convert-raced-json", serial="SN-MATCH", librenms_cf=42)
        view, req = self._device_view(dev)

        with _cf_changed_before_lock(view, dev, {"default": 42}):
            _post(view, req, pk=dev.pk)

        assert any("already in the server-scoped JSON format" in t for t in message_texts(req, "warning"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"default": 42}

    def test_cf_value_not_int_after_lock(self):
        """The locked row shows a non-numeric id: abort rather than convert a value we never checked."""
        from dcim.models import Device

        dev = make_device("convert-raced-junk", serial="SN-MATCH", librenms_cf=42)
        view, req = self._device_view(dev)

        with _cf_changed_before_lock(view, dev, "not-a-digit"):
            _post(view, req, pk=dev.pk)

        assert message_texts(req, "error") == ["librenms_id changed before lock was acquired; aborting."]
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == "not-a-digit"

    def test_data_changed_before_lock(self):
        """The locked row carries a different id than the one whose serial was verified."""
        from dcim.models import Device

        dev = make_device("convert-raced-id", serial="SN-MATCH", librenms_cf=42)
        view, req = self._device_view(dev)

        with _cf_changed_before_lock(view, dev, 99):
            _post(view, req, pk=dev.pk)

        assert message_texts(req, "error") == ["Device data changed before lock was acquired; aborting conversion."]
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == 99

    def test_conflict_with_another_object(self):
        """Verify a bare integer ID and a server-specific owner are ambiguous and leave both devices unchanged."""
        from dcim.models import Device

        dev = make_device("convert-loser", serial="SN-MATCH", librenms_cf=42)
        owner = make_device("convert-owner", librenms_cf={"default": 42})
        view, req = self._device_view(dev)

        _post(view, req, pk=dev.pk)

        errors = message_texts(req, "error")
        assert any("ambiguous" in t.lower() or "already has librenms_id 42" in t for t in errors)
        assert not message_texts(req, "success")
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == 42
        assert Device.objects.get(pk=owner.pk).custom_field_data["librenms_id"] == {"default": 42}

    def test_validation_error_on_save(self):
        """A rejected write is surfaced and rolled back, leaving the legacy id in place."""
        from dcim.models import Device
        from django.core.exceptions import ValidationError

        dev = make_device("convert-validationerr", serial="SN-MATCH", librenms_cf=42)
        view, req = self._device_view(dev)

        # full_clean() accepts the converted dict, so the rejection has to be injected; the
        # manager, the lock, the conflict query and the transaction all stay real.
        with patch.object(Device, "full_clean", side_effect=ValidationError({"custom_field_data": ["err"]})):
            _post(view, req, pk=dev.pk)

        assert any("Failed to save converted librenms_id" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == 42

    def test_unexpected_error_on_save(self):
        """A non-ValidationError is caught, reported and rolled back rather than 500ing."""
        from dcim.models import Device
        from django.db import OperationalError

        dev = make_device("convert-unexpected", serial="SN-MATCH", librenms_cf=42)
        view, req = self._device_view(dev)

        # Injected at the persistence boundary, which is where an unexpected failure of this
        # kind actually originates; full_clean still runs for real ahead of it.
        with patch.object(Device, "save", side_effect=OperationalError("disk full")):
            _post(view, req, pk=dev.pk)

        assert any("Failed to save converted librenms_id" in t for t in message_texts(req, "error"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == 42

    def test_success_integer_cf_value(self):
        """Happy path, legacy int cf_value: the real migrate + save converts it to the dict form."""
        from dcim.models import Device

        dev = make_device("convert-int", serial="SN-MATCH", librenms_cf=42)
        view, req = self._device_view(dev)

        _post(view, req, pk=dev.pk)

        assert any("42" in t for t in message_texts(req, "success"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"default": 42}

    def test_success_string_cf_value(self):
        """Happy path, legacy string-digit cf_value '42' → converted to {'default': 42}."""
        from dcim.models import Device

        dev = make_device("convert-str", serial="SN-MATCH", librenms_cf="42")
        view, req = self._device_view(dev)

        _post(view, req, pk=dev.pk)

        assert message_texts(req, "success")
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"default": 42}

    def test_conflict_same_object_is_not_conflict(self):
        """The legacy id resolves to the device being converted — that is not a conflict."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id

        dev = make_device("convert-self", serial="SN-MATCH", librenms_cf=42)
        # The bare-int form is a universal fallback, so the conflict query DOES find this very
        # device; the branch under test is the `match.pk != locked.pk` guard that lets it through.
        assert find_by_librenms_id(Device, 42, "default").pk == dev.pk
        view, req = self._device_view(dev)

        _post(view, req, pk=dev.pk)

        assert not message_texts(req, "error")
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"default": 42}


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
@pytest.mark.django_db
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
        # The user is shown the caught error, not a 500. The stack's _sync_redirect returns a real
        # HttpResponseRedirect (bypassing the patched module-level redirect), so assert the 302.
        assert result.status_code == 302
        mock_messages.error.assert_called_once()
        assert "could not be created" in mock_messages.error.call_args[0][1]


@pytest.mark.django_db
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
        # RequestFactory skips AuthenticationMiddleware; the object-scoped device lookup reads
        # request.user, and dispatch() is what binds the request in production.
        from netbox_librenms_plugin.tests.conftest import make_superuser

        request.user = make_superuser("devfield-rebind-su")
        view.setup(request)
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


# ---------------------------------------------------------------------------
# ConvertLegacyLibreNMSIdView._sync_url — fail-closed rebind redirect (real DB)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSyncUrlUnboundApiDoesNotReconstructDefault:
    """A post-action redirect after a fail-closed rebind must not rebuild the default client."""

    def test_sync_url_unbound_api_does_not_construct_default(self):
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        dev = make_device("sync-url-dev")
        view = object.__new__(ConvertLegacyLibreNMSIdView)
        view._librenms_api = None  # unbound, as left by a fail-closed rebind that returned None
        request = RequestFactory().post("/", {})  # no server_key in the POST
        request.user = make_superuser()
        view.request = request

        # The lazy librenms_api property constructs LibreNMSAPI() (looked up in views.mixins).
        with patch("netbox_librenms_plugin.views.mixins.LibreNMSAPI") as mock_api:
            resp = view._sync_url("device", dev.pk)

        # The fail-closed rebind already declined to build a client; _sync_url must NOT re-run that
        # construction just to guess a redirect server_key (it can mis-scope to a different
        # configured server). It degrades to a bare redirect instead.
        mock_api.assert_not_called()
        assert "server_key=" not in resp.url


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name",
    ["remove_server_mapping", "set_preferred_server", "convert_legacy_librenms_id"],
)
@pytest.mark.parametrize("posted_type", ["vm", "virtualmachine"])
def test_the_mapping_views_gate_a_vm_object_type_on_the_vm_model(client, url_name, posted_type):
    """The shared model selection decides the permission scope, so Device rights must not carry over."""
    from dcim.models import Device
    from django.contrib.messages import get_messages
    from django.urls import reverse

    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    user = make_user_with_perms(f"objtype-{url_name}-{posted_type}", [("change", Device)])
    client.force_login(user)
    subject = make_device(f"objtype-subject-{url_name}-{posted_type}")
    url = reverse(f"plugins:netbox_librenms_plugin:{url_name}", args=[subject.pk])

    response = client.post(url, {"object_type": posted_type, "server_key": "default"})

    texts = [str(message) for message in get_messages(response.wsgi_request)]
    assert any("Missing permissions" in text and "virtualmachine" in text for text in texts), texts


@pytest.mark.django_db
@pytest.mark.parametrize(
    "posted_type,factory,url_name",
    [
        ("device", make_device, "device_librenms_sync"),
        ("vm", make_vm, "vm_librenms_sync"),
    ],
)
def test_the_convert_view_redirects_to_the_sync_page_of_the_posted_object_type(
    client, settings, posted_type, factory, url_name
):
    """The shared URL-name selection decides the redirect target, so a VM must not land on a device page."""
    from django.urls import reverse

    from netbox_librenms_plugin.tests.import_server_helpers import configure_servers
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    configure_servers(settings)
    subject = factory(f"convert-redirect-{posted_type}")
    subject.custom_field_data["librenms_id"] = {"secondary": {"id": 13411}}
    subject.save(update_fields=["custom_field_data"])
    client.force_login(make_user_with_perms(f"convert-redirect-{posted_type}", [("change", type(subject))]))
    url = reverse("plugins:netbox_librenms_plugin:convert_legacy_librenms_id", args=[subject.pk])

    response = client.post(url, {"object_type": posted_type, "server_key": "secondary"})

    assert response.status_code == 302
    assert response.url.startswith(reverse(f"plugins:netbox_librenms_plugin:{url_name}", args=[subject.pk]))


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name",
    ["remove_server_mapping", "set_preferred_server", "convert_legacy_librenms_id"],
)
def test_the_mapping_views_reject_an_unsupported_object_type_the_same_way(client, url_name):
    """An unsupported object_type is refused before the permission gate, echoing the raw value."""
    from django.urls import reverse

    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    user = make_user_with_perms(f"objtype-bad-{url_name}", [])
    client.force_login(user)
    subject = make_device(f"objtype-bad-subject-{url_name}")
    url = reverse(f"plugins:netbox_librenms_plugin:{url_name}", args=[subject.pk])

    response = client.post(url, {"object_type": "cluster<script>", "server_key": "default"})

    assert response.status_code == 400
    body = response.content.decode()
    assert "Invalid object_type: cluster&lt;script&gt;" in body
