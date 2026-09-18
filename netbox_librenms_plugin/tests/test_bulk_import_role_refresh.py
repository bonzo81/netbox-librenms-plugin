"""Role handling when import_utils/bulk_import.py refreshes a cached match.

The primary home for that module is test_coverage_bulk_import.py. These cases live in
their own file so they do not collide at that shared file's tail when the stack is
restacked.

_refresh_existing_device() picks the model to re-read from import_as_vm, so a refreshed
object is a Device exactly when the row is not a VM row. dcim.Device.role is not
nullable, which is why the refresh path never has to reset the role of a device.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device


def _validation(existing, *, import_as_vm):
    """Build the minimal real validation row the refresh path reads."""
    return {
        "device_id": 4242,
        "hostname": existing.name,
        "import_as_vm": import_as_vm,
        "existing_device": existing,
        "existing_match_type": "hostname",
        "existing_librenms_link": None,
        "is_ready": False,
        "can_import": False,
        "issues": [],
        "warnings": [],
        "device_role": {"found": False, "role": None, "available_roles": []},
        "cluster": {"found": False, "cluster": None, "available_clusters": []},
    }


@pytest.mark.django_db
class TestRoleOnRefresh:
    """A refreshed Device always carries a role, so the refresh never resets one."""

    def test_a_device_role_is_required_and_a_vm_role_is_not(self):
        """This schema difference is the reason the device branch needs no role fallback."""
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        assert Device._meta.get_field("role").null is False
        assert VirtualMachine._meta.get_field("role").null is True

    def test_refreshing_a_device_match_adopts_the_role_it_already_has(self):
        """The device branch always finds a role, so it applies it rather than clearing it."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        device = make_device("refresh-role-device")
        validation = _validation(device, import_as_vm=False)

        _refresh_existing_device(validation, libre_device={"device_id": 4242}, server_key="default")

        assert validation["device_role"]["found"] is True
        assert validation["device_role"]["role"] == device.role

    def test_refreshing_a_roleless_vm_keeps_the_role_the_user_picked(self):
        """A VM carries no role of its own, so the refresh must not clear the user's selection."""
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_cluster

        cluster = make_cluster("refresh-role-cluster")
        vm = VirtualMachine.objects.create(name="refresh-role-vm", cluster=cluster)
        assert vm.role is None

        chosen_role = make_device("refresh-role-donor").role
        validation = _validation(vm, import_as_vm=True)
        validation["device_role"] = {"found": True, "role": chosen_role, "available_roles": [chosen_role]}

        _refresh_existing_device(validation, libre_device={"device_id": 4242}, server_key="default")

        assert validation["device_role"]["found"] is True
        assert validation["device_role"]["role"] == chosen_role
