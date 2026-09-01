"""The shared real-request/real-user drivers in view_test_helpers must work end to end.

These are the seam every de-mocked view test stands on: if ``make_request`` silently failed to
attach message storage, or ``grant`` produced a permission the NetBox backend ignores, the
converted tests would pass for the wrong reason. Each assertion here pins one of those.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    make_request,
    make_superuser,
    make_user_with_perms,
    make_view,
    message_texts,
    messages_on,
    post,
)


@pytest.mark.django_db
class TestRealRequest:
    def test_messages_are_recorded_for_real(self):
        """A patched ``messages`` module proves nothing about what the user is told; this records."""
        from django.contrib import messages

        request = make_request()
        messages.error(request, "boom")
        messages.success(request, "ok")

        assert messages_on(request) == [("error", "boom"), ("success", "ok")]

    def test_message_texts_filters_by_level(self):
        from django.contrib import messages

        request = make_request()
        messages.warning(request, "careful")
        messages.error(request, "boom")

        assert message_texts(request, "warning") == ["careful"]

    def test_error_filtering_survives_netboxs_danger_tag(self):
        """NetBox remaps ERROR's tag to "danger"; filtering by tag string would match nothing."""
        from django.contrib import messages
        from django.contrib.messages import get_messages

        request = make_request()
        messages.error(request, "boom")

        assert [m.level_tag for m in get_messages(request)] == ["danger"]  # the trap
        assert message_texts(request, "error") == ["boom"]

    def test_an_unknown_level_name_is_rejected_not_silently_empty(self):
        """A typo'd level must fail loudly, or `assert not message_texts(...)` passes vacuously."""
        request = make_request()

        with pytest.raises(ValueError):
            message_texts(request, "eror")

    def test_post_data_reaches_request_post(self):
        request = make_request("post", {"server_key": "default", "platform_name": "ios"})

        assert request.POST.get("platform_name") == "ios"

    def test_default_user_is_a_real_active_superuser(self):
        request = make_request()

        assert request.user.is_superuser and request.user.is_active
        assert request.user.pk is not None


@pytest.mark.django_db
class TestRealGrants:
    def test_grant_is_honored_by_netbox_has_perm(self):
        """NetBox's ObjectPermissionBackend ignores the user_permissions m2m — only these count."""
        from dcim.models import Device

        user = make_user_with_perms("seam-grantee", [("change", Device)])

        assert user.has_perm("dcim.change_device")
        assert not user.has_perm("dcim.delete_device")

    def test_plugin_write_perms_are_granted_by_default(self):
        user = make_user_with_perms("seam-plugin", [])

        assert user.has_perm("netbox_librenms_plugin.view_librenmssettings")
        assert user.has_perm("netbox_librenms_plugin.change_librenmssettings")

    def test_plugin_write_can_be_withheld(self):
        user = make_user_with_perms("seam-noplugin", [], plugin_write=False)

        assert not user.has_perm("netbox_librenms_plugin.change_librenmssettings")

    def test_a_constrained_grant_passes_has_perm_but_narrows_restrict(self):
        """The exact shape the object-scoped lookups defend against: model-level yes, row-level no."""
        from dcim.models import Device

        mine = make_device("seam-mine")
        theirs = make_device("seam-theirs")
        user = make_user_with_perms("seam-constrained", [("change", Device)], constraints={"name": "seam-mine"})

        assert user.has_perm("dcim.change_device")  # asked without an instance — passes
        visible = Device.objects.restrict(user, "change")
        assert list(visible.values_list("pk", flat=True)) == [mine.pk]
        assert theirs.pk not in set(visible.values_list("pk", flat=True))

    def test_restrict_returns_none_without_the_model_grant(self):
        """Scoping a read the gate never covered would silently drop every row — pin that."""
        from dcim.models import Device

        make_device("seam-ungranted")
        user = make_user_with_perms("seam-nodevice", [])

        assert not Device.objects.restrict(user, "change").exists()

    def test_grant_reloads_the_permission_cache(self):
        """A user object cached before the grant would report the stale answer."""
        from dcim.models import Device
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username="seam-cache", password="x")
        assert not user.has_perm("dcim.change_device")  # primes the cache

        user = grant(user, "change", Device)

        assert user.has_perm("dcim.change_device")


@pytest.mark.django_db
class TestRealView:
    def test_make_view_binds_the_request_and_runs_the_real_gate(self, live_librenms):
        """A superuser drives the real permission gate to a real DB write — no ORM mock in sight."""
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = make_device("seam-serial")
        set_librenms_device_id(dev, 42, "default")
        dev.save(update_fields=["custom_field_data"])
        live_librenms.server.device_info_response(device_id=42, hostname=dev.name, serial="SN-SEAM")
        request = make_request("post")
        view = make_view(UpdateDeviceSerialView, request)

        post(view, request, pk=dev.pk)

        assert Device.objects.get(pk=dev.pk).serial == "SN-SEAM"

    def test_the_real_gate_denies_a_user_without_the_change_grant(self, live_librenms):
        """No mocked ``require_all_permissions``: the denial comes from a real missing grant."""
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = make_device("seam-denied")
        set_librenms_device_id(dev, 42, "default")
        dev.save(update_fields=["custom_field_data"])
        live_librenms.server.device_info_response(device_id=42, hostname=dev.name, serial="SN-DENIED")
        user = make_user_with_perms("seam-viewer", [("view", Device)])
        request = make_request("post", user=user)
        view = make_view(UpdateDeviceSerialView, request)

        post(view, request, pk=dev.pk)

        assert Device.objects.get(pk=dev.pk).serial == ""
        assert any("Missing permissions" in t for t in message_texts(request, "error"))
        assert live_librenms.server.requests == []

    def test_a_constrained_grant_404s_on_a_device_outside_it(self, live_librenms):
        """The scoped lookup must fail closed, not fall through to the plain manager."""
        from django.http import Http404

        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from dcim.models import Device

        mine = make_device("seam-scoped-mine")
        theirs = make_device("seam-scoped-theirs")
        set_librenms_device_id(mine, 42, "default")
        mine.save(update_fields=["custom_field_data"])
        live_librenms.server.device_info_response(device_id=42, hostname=mine.name, serial="SN-X")
        user = make_user_with_perms("seam-scoped", [("change", Device)], constraints={"name": "seam-scoped-mine"})
        request = make_request("post", user=user)
        view = make_view(UpdateDeviceSerialView, request)

        with pytest.raises(Http404):
            post(view, request, pk=theirs.pk)

        post(view, request, pk=mine.pk)
        assert Device.objects.get(pk=mine.pk).serial == "SN-X"

    def test_superuser_helper_returns_the_same_row_across_calls(self):
        """Two builders in one test must not race the unique-username constraint."""
        assert make_superuser().pk == make_superuser().pk
