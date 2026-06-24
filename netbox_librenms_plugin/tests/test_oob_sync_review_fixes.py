"""Regression tests for the max-effort review findings on the OOB-sync PR.

Real-DB (django_db) coverage that exercises the actual ORM/model behaviour rather than mocks,
so a broken fix can't stay green by fabricating attributes.
"""

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device


def _superuser():
    User = get_user_model()
    return User.objects.first() or User.objects.create(username="review-su", is_superuser=True, is_active=True)


@pytest.mark.django_db
class TestAttachOobIpForeignKeyConflict:
    """_attach_oob_ip must not try to re-home an IP that is another device's primary/oob FK."""

    def test_conflict_when_ip_is_another_devices_oob_fk(self):
        from dcim.models import Interface
        from ipam.models import IPAddress

        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        donor = make_device("oob-donor")
        target = make_device("oob-target")
        target_iface = Interface.objects.create(device=target, name="mgmt0", type="1000base-t")

        # X is not assigned to ANY interface, yet it IS the donor's oob_ip — a state reachable
        # because the import path persists oob_ip via save(update_fields=[...]) (no full_clean()).
        ip = IPAddress.objects.create(address="10.10.0.5/32", status="active")
        donor.oob_ip = ip
        donor.save(update_fields=["oob_ip"])
        ip.refresh_from_db()
        assert ip.assigned_object is None

        request = RequestFactory().post("/")
        request.user = _superuser()

        # select_for_update needs an open transaction (the real caller provides one).
        with transaction.atomic():
            result_ip, reason = AddAsOOBView._attach_oob_ip(request, "10.10.0.5", target_iface)

        # Must surface a clean conflict, NOT re-home the IP into a doomed UNIQUE-constraint save.
        assert result_ip is None
        assert reason == "conflict"
        # The donor still owns it; nothing was silently re-homed.
        donor.refresh_from_db()
        ip.refresh_from_db()
        assert donor.oob_ip_id == ip.pk
        assert ip.assigned_object is None


@pytest.mark.django_db
class TestSaveDeviceValidatesPlatformDeviceTypeConsistency:
    """update_fields saves skip full_clean(), but a device_type/platform write must still honour the platform/manufacturer cross-field rule."""

    def test_update_fields_save_rejects_manufacturer_mismatch(self):
        from dcim.models import DeviceType, Manufacturer, Platform

        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = make_device("dt-consistency")  # device_type=TestDT, manufacturer=TestMfr
        mfr_a = Manufacturer.objects.get(slug="test-mfr")
        mfr_b, _ = Manufacturer.objects.get_or_create(name="OtherMfr", slug="other-mfr")
        # Platform limited to TestMfr — consistent with the device's current device_type.
        platform = Platform.objects.create(name="P-testmfr", slug="p-testmfr", manufacturer=mfr_a)
        device.platform = platform
        device.save(update_fields=["platform"])
        # A device_type from a DIFFERENT manufacturer than the platform allows.
        dt_other = DeviceType.objects.create(model="DT-other", slug="dt-other", manufacturer=mfr_b)

        device.device_type = dt_other
        resp = _save_device(device, update_fields=["device_type"])

        # Rejected with an error response, NOT silently persisted with a success toast.
        assert resp is not None
        device.refresh_from_db()
        assert device.device_type_id != dt_other.pk

    def test_update_fields_save_allows_consistent_device_type(self):
        from dcim.models import DeviceType, Manufacturer, Platform

        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = make_device("dt-consistent-ok")
        mfr_a = Manufacturer.objects.get(slug="test-mfr")
        platform = Platform.objects.create(name="P-ok", slug="p-ok", manufacturer=mfr_a)
        device.platform = platform
        device.save(update_fields=["platform"])
        # Same-manufacturer device_type — the consistent case must still save cleanly.
        dt_same = DeviceType.objects.create(model="DT-same", slug="dt-same", manufacturer=mfr_a)

        device.device_type = dt_same
        resp = _save_device(device, update_fields=["device_type"])

        assert resp is None
        device.refresh_from_db()
        assert device.device_type_id == dt_same.pk


@pytest.mark.django_db
class TestSerialMatchRoleIgnoresMissingDeviceId:
    """A missing/zero incoming device_id is unknown, not a 'linked elsewhere' mismatch."""

    def test_missing_device_id_does_not_offer_chassis_pair_toggle(self):
        from netbox_librenms_plugin.import_utils.device_operations import _detect_serial_match_role

        device = make_device("host1", serial="SN1")
        existing_link = {"host_id": 42, "oob_id": None}
        result = _detect_serial_match_role(
            existing_by_serial=device,
            existing_link=existing_link,
            hostname="host1",  # matches device.name
            serial="SN1",
            libre_device={"os": "ios", "hardware": "C9300"},  # no device_id key → normalizes to None
            server_key="default",
        )

        # Names match and there's no real incoming id to mismatch against, so this is a plain link
        # — NOT a host/OOB chassis-pair situation. The role-choice toggle must not be offered.
        assert result["serial_role_choice_available"] is False
        assert result["serial_action"] == "link"
