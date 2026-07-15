"""Real-DB guard against lossy librenms_id coercion in _refresh_existing_device.

CodeRabbit (PR #116): ``_id_lookup`` did ``find_by_librenms_id(m, int(librenms_id), ...)``.
``int(42.9)`` silently truncates to ``42`` and would bind an unrelated NetBox device whose
librenms_id is 42. A float carries no exact integer id, so the refresh must fail closed
instead of truncating into a different valid id.

These tests exercise the real ORM: a real Device carrying ``librenms_id={"default": 42}``,
with the libre_device hostname/sysName deliberately NOT matching the device name so a bind can
only come from the id lookup. The float case must leave existing_device unset; the int case
must bind it (positive control proving the lookup is genuinely exercised).
"""

import pytest


def _make_device(name):
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

    mfr, _ = Manufacturer.objects.get_or_create(name="ACME-116", slug="acme-116")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-116", slug="dt-116")
    role, _ = DeviceRole.objects.get_or_create(name="Role-116", slug="role-116")
    site, _ = Site.objects.get_or_create(name="Site-116", slug="site-116")
    return Device.objects.create(
        name=name,
        device_type=dt,
        role=role,
        site=site,
        status="active",
        custom_field_data={"librenms_id": {"default": 42}},
    )


@pytest.mark.django_db
class TestIdLookupLossyCoercionRealDB:
    def test_float_device_id_does_not_bind_by_truncated_id(self):
        """A float device_id (42.9) must not truncate to 42 and bind the device linked to id 42."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        self._make_device_id_42()
        validation = {"existing_device": None, "import_as_vm": False, "device_role": {}, "issues": []}
        # hostname/sysName intentionally unmatched so a bind can only come from the id lookup.
        libre_device = {"device_id": 42.9, "hostname": "no-such-host-116", "sysName": "no-such-sys-116"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is None

    def test_int_device_id_binds(self):
        """Positive control: an exact int device_id (42) binds the device — the lookup really runs."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        device = self._make_device_id_42()
        validation = {"existing_device": None, "import_as_vm": False, "device_role": {}, "issues": []}
        libre_device = {"device_id": 42, "hostname": "no-such-host-116", "sysName": "no-such-sys-116"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is not None
        assert validation["existing_device"].pk == device.pk
        assert validation["existing_match_type"] == "librenms_id"

    def test_non_numeric_device_id_does_not_bind_by_id(self):
        """A non-numeric device_id ("abc") yields no real id match → existing_device stays None."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        self._make_device_id_42()
        validation = {"existing_device": None, "import_as_vm": False, "device_role": {}, "issues": []}
        libre_device = {"device_id": "abc", "hostname": "no-such-host-116", "sysName": "no-such-sys-116"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is None

    def _make_device_id_42(self):
        return _make_device("lossy-coercion-host-116")
