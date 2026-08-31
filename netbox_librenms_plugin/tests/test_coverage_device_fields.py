"""Request-level coverage for device and VM field-sync actions."""

from copy import deepcopy

import pytest
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_superuser,
    make_virtual_chassis,
    make_vm,
)
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms, message_texts


SERVER_KEY = "default"
SECONDARY_KEY = "secondary"


def _configure_servers(settings, server):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        SERVER_KEY: {
            "display_name": "Default field-sync test server",
            "librenms_url": server.url,
            "api_token": "default-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        },
        SECONDARY_KEY: {
            "display_name": "Secondary field-sync test server",
            "librenms_url": server.url,
            "api_token": "secondary-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        },
    }
    plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def librenms_server(settings):
    with librenms_mock_server() as server:
        _configure_servers(settings, server)
        yield server


@pytest.fixture
def logged_in_client(client):
    client.force_login(make_superuser("device-fields-superuser"))
    return client


def _url(name, pk):
    return reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs={"pk": pk})


def _post(client, name, obj, data=None):
    submitted = {"server_key": SERVER_KEY}
    submitted.update(data or {})
    return client.post(_url(name, obj.pk), submitted)


def _linked_device(name, device_id, **kwargs):
    return make_device(name, librenms_cf={SERVER_KEY: device_id}, **kwargs)


def _messages(response, level=None):
    return message_texts(response.wsgi_request, level=level)


@pytest.mark.django_db
class TestUpdateDeviceNameView:
    def test_live_name_replaces_a_stale_api_snapshot(self, logged_in_client, librenms_server):
        from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key

        device = _linked_device("old-live-name", 6501)
        cache.set(
            f"librenms_device_info_{SERVER_KEY}_6501",
            {"device_id": 6501, "hostname": "stale-name", "sysName": "stale-name"},
            300,
        )
        cache.set(
            get_import_device_cache_key(6501, SERVER_KEY),
            {"device_id": 6501, "hostname": "also-stale", "sysName": "also-stale"},
            300,
        )
        librenms_server.device_info_response(device_id=6501, hostname="fresh-name.example.test")

        response = _post(
            logged_in_client,
            "update_device_name",
            device,
            {"use_sysname": "on", "strip_domain": "on"},
        )

        device.refresh_from_db()
        assert response.status_code == 302
        assert response.url.endswith(f"?server_key={SERVER_KEY}")
        assert device.name == "fresh-name"
        assert any("Device name updated" in text for text in _messages(response, "success"))

    def test_missing_mapping_leaves_the_name_unchanged(self, logged_in_client, librenms_server):
        device = make_device("name-without-mapping")

        response = _post(logged_in_client, "update_device_name", device)

        device.refresh_from_db()
        assert device.name == "name-without-mapping"
        assert any("not found in LibreNMS" in text for text in _messages(response, "error"))

    def test_live_response_without_a_name_is_informational(self, logged_in_client, librenms_server):
        device = _linked_device("name-without-live-value", 6502)
        librenms_server.register(
            "/api/v0/devices/6502",
            {"status": "ok", "devices": [{"device_id": 6502, "hostname": "", "sysName": ""}]},
        )

        response = _post(logged_in_client, "update_device_name", device)

        device.refresh_from_db()
        assert device.name == "name-without-live-value"
        assert any("No name could be determined" in text for text in _messages(response, "warning"))

    def test_real_duplicate_name_validation_rolls_the_change_back(self, logged_in_client, librenms_server):
        device = _linked_device("name-before-duplicate", 6503)
        duplicate = make_device("occupied-name")
        librenms_server.device_info_response(device_id=6503, hostname=duplicate.name)

        response = _post(logged_in_client, "update_device_name", device)

        device.refresh_from_db()
        assert device.name == "name-before-duplicate"
        assert any("Failed to update device name" in text for text in _messages(response, "error"))

    def test_unknown_server_fails_closed_without_an_http_fallback(self, logged_in_client, librenms_server):
        device = _linked_device("name-stale-server", 6504)

        response = logged_in_client.post(
            _url("update_device_name", device.pk),
            {"server_key": "retired"},
        )

        device.refresh_from_db()
        assert device.name == "name-stale-server"
        assert any("no longer configured" in text for text in _messages(response, "error"))


@pytest.mark.django_db
class TestUpdateDeviceSerialView:
    def test_live_serial_is_normalized_and_persisted(self, logged_in_client, librenms_server):
        device = _linked_device("serial-update", 6511, serial="OLD-SERIAL")
        librenms_server.device_info_response(device_id=6511, hostname=device.name, serial=" NEW-SERIAL ")

        response = _post(logged_in_client, "update_device_serial", device)

        device.refresh_from_db()
        assert device.serial == "NEW-SERIAL"
        assert any("updated from 'OLD-SERIAL'" in text for text in _messages(response, "success"))

    @pytest.mark.parametrize("serial", [None, "", "-"])
    def test_missing_live_serial_preserves_the_stored_value(self, logged_in_client, librenms_server, serial):
        device = _linked_device(f"serial-missing-{serial!s}", 6512, serial="KEPT-SERIAL")
        librenms_server.register(
            "/api/v0/devices/6512",
            {
                "status": "ok",
                "devices": [{"device_id": 6512, "hostname": device.name, "serial": serial}],
            },
        )

        response = _post(logged_in_client, "update_device_serial", device)

        device.refresh_from_db()
        assert device.serial == "KEPT-SERIAL"
        assert any("No serial number available" in text for text in _messages(response, "warning"))

    def test_first_serial_uses_the_set_message(self, logged_in_client, librenms_server):
        device = _linked_device("serial-first", 6513)
        librenms_server.device_info_response(device_id=6513, hostname=device.name, serial="FIRST-SERIAL")

        response = _post(logged_in_client, "update_device_serial", device)

        device.refresh_from_db()
        assert device.serial == "FIRST-SERIAL"
        assert any("serial set to 'FIRST-SERIAL'" in text for text in _messages(response, "success"))


@pytest.mark.django_db
class TestUpdateDeviceTypeView:
    def _device_type(self, tag, model):
        from dcim.models import DeviceType, Manufacturer

        manufacturer = Manufacturer.objects.create(
            name=f"Device type manufacturer {tag}",
            slug=f"device-type-manufacturer-{tag}",
        )
        return DeviceType.objects.create(
            manufacturer=manufacturer,
            model=model,
            slug=f"device-type-{tag}",
        )

    def test_exact_hardware_match_changes_the_real_device_type(self, logged_in_client, librenms_server):
        device = _linked_device("device-type-update", 6521)
        replacement = self._device_type("replacement", "Replacement Router")
        librenms_server.device_info_response(
            device_id=6521,
            hostname=device.name,
            hardware=replacement.model,
        )

        response = _post(logged_in_client, "update_device_type", device)

        device.refresh_from_db()
        assert device.device_type == replacement
        assert any("Device type updated" in text for text in _messages(response, "success"))

    def test_unmatched_hardware_preserves_the_device_type(self, logged_in_client, librenms_server):
        device = _linked_device("device-type-unmatched", 6522)
        original = device.device_type
        librenms_server.device_info_response(
            device_id=6522,
            hostname=device.name,
            hardware="No such hardware model",
        )

        response = _post(logged_in_client, "update_device_type", device)

        device.refresh_from_db()
        assert device.device_type == original
        assert any("No matching DeviceType" in text for text in _messages(response, "error"))

    def test_ambiguous_hardware_preserves_the_device_type(self, logged_in_client, librenms_server):
        device = _linked_device("device-type-ambiguous", 6523)
        original = device.device_type
        self._device_type("ambiguous-a", "Shared Router")
        self._device_type("ambiguous-b", "Shared Router")
        librenms_server.device_info_response(device_id=6523, hostname=device.name, hardware="Shared Router")

        response = _post(logged_in_client, "update_device_type", device)

        device.refresh_from_db()
        assert device.device_type == original
        assert any("Ambiguous hardware match" in text for text in _messages(response, "error"))

    def test_missing_hardware_is_informational(self, logged_in_client, librenms_server):
        device = _linked_device("device-type-missing-hardware", 6524)
        librenms_server.register(
            "/api/v0/devices/6524",
            {"status": "ok", "devices": [{"device_id": 6524, "hostname": device.name, "hardware": ""}]},
        )

        response = _post(logged_in_client, "update_device_type", device)

        assert any("No hardware information" in text for text in _messages(response, "warning"))


@pytest.mark.django_db
class TestUpdateDevicePlatformView:
    def test_exact_platform_match_is_assigned(self, logged_in_client, librenms_server):
        from dcim.models import Platform

        device = _linked_device("platform-update", 6531)
        platform = Platform.objects.create(name="Exact Platform", slug="exact-platform")
        librenms_server.device_info_response(device_id=6531, hostname=device.name, os=platform.name)

        response = _post(logged_in_client, "update_device_platform", device)

        device.refresh_from_db()
        assert device.platform == platform
        assert any("platform set" in text for text in _messages(response, "success"))

    def test_platform_mapping_is_honored(self, logged_in_client, librenms_server):
        from dcim.models import Platform
        from netbox_librenms_plugin.models import PlatformMapping

        device = _linked_device("platform-mapped", 6532)
        platform = Platform.objects.create(name="Mapped NetBox Platform", slug="mapped-netbox-platform")
        PlatformMapping.objects.create(librenms_os="mapped-os", netbox_platform=platform)
        librenms_server.device_info_response(device_id=6532, hostname=device.name, os="mapped-os")

        _post(logged_in_client, "update_device_platform", device)

        device.refresh_from_db()
        assert device.platform == platform

    def test_missing_platform_returns_creation_guidance(self, logged_in_client, librenms_server):
        device = _linked_device("platform-missing", 6533)
        librenms_server.device_info_response(device_id=6533, hostname=device.name, os="missing-os")

        response = _post(logged_in_client, "update_device_platform", device)

        device.refresh_from_db()
        assert device.platform is None
        assert any("Create & Sync" in text for text in _messages(response, "error"))

    def test_duplicate_platform_names_fail_closed(self, logged_in_client, librenms_server):
        from dcim.models import Manufacturer, Platform

        device = _linked_device("platform-ambiguous", 6534)
        for suffix in ("a", "b"):
            manufacturer = Manufacturer.objects.create(
                name=f"Platform manufacturer {suffix}",
                slug=f"platform-manufacturer-{suffix}",
            )
            Platform.objects.create(
                name="Ambiguous Platform",
                slug=f"ambiguous-platform-{suffix}",
                manufacturer=manufacturer,
            )
        librenms_server.device_info_response(device_id=6534, hostname=device.name, os="Ambiguous Platform")

        response = _post(logged_in_client, "update_device_platform", device)

        device.refresh_from_db()
        assert device.platform is None
        assert any("Multiple platforms match" in text for text in _messages(response, "error"))

    def test_missing_os_is_informational(self, logged_in_client, librenms_server):
        device = _linked_device("platform-missing-os", 6535)
        librenms_server.register(
            "/api/v0/devices/6535",
            {"status": "ok", "devices": [{"device_id": 6535, "hostname": device.name, "os": ""}]},
        )

        response = _post(logged_in_client, "update_device_platform", device)

        assert any("No OS information" in text for text in _messages(response, "warning"))

    def test_existing_platform_is_replaced_and_named_in_the_message(self, logged_in_client, librenms_server):
        from dcim.models import Platform

        old_platform = Platform.objects.create(name="Old exact platform", slug="old-exact-platform")
        new_platform = Platform.objects.create(name="New exact platform", slug="new-exact-platform")
        device = _linked_device("platform-replace", 6536)
        device.platform = old_platform
        device.save()
        librenms_server.device_info_response(device_id=6536, hostname=device.name, os=new_platform.name)

        response = _post(logged_in_client, "update_device_platform", device)

        device.refresh_from_db()
        assert device.platform == new_platform
        assert any("updated from 'Old exact platform'" in text for text in _messages(response, "success"))


@pytest.mark.django_db
class TestCreateAndAssignPlatformView:
    def test_new_platform_and_mapping_are_created_and_assigned(self, logged_in_client, librenms_server):
        from dcim.models import Platform
        from netbox_librenms_plugin.models import PlatformMapping

        device = make_device("platform-create-device")
        manufacturer = device.device_type.manufacturer

        response = _post(
            logged_in_client,
            "create_and_assign_platform",
            device,
            {
                "platform_name": "Created Platform",
                "manufacturer": str(manufacturer.pk),
                "librenms_os": " Created-OS ",
                "create_mapping": "on",
            },
        )

        device.refresh_from_db()
        platform = Platform.objects.get(name="Created Platform")
        assert platform.slug == "created-platform"
        assert platform.manufacturer == manufacturer
        assert device.platform == platform
        assert PlatformMapping.objects.get(librenms_os="created-os").netbox_platform == platform
        assert any("Created platform" in text for text in _messages(response, "success"))

    def test_existing_platform_is_reused_without_changing_manufacturer(self, logged_in_client, librenms_server):
        from dcim.models import Platform

        device = make_device("platform-reuse-device")
        manufacturer = device.device_type.manufacturer
        platform = Platform.objects.create(
            name="Existing Platform",
            slug="existing-platform",
            manufacturer=manufacturer,
        )

        response = _post(
            logged_in_client,
            "create_and_assign_platform",
            device,
            {"platform_name": " Existing Platform ", "librenms_os": "existing-os"},
        )

        device.refresh_from_db()
        platform.refresh_from_db()
        assert device.platform == platform
        assert platform.manufacturer == manufacturer
        assert any("already existed" in text for text in _messages(response, "success"))

    def test_missing_platform_name_does_not_change_the_device(self, logged_in_client, librenms_server):
        device = make_device("platform-name-required")

        response = _post(logged_in_client, "create_and_assign_platform", device, {"platform_name": " "})

        device.refresh_from_db()
        assert device.platform is None
        assert any("Platform name is required" in text for text in _messages(response, "error"))

    def test_mapping_conflict_is_reported_without_undoing_assignment(self, logged_in_client, librenms_server):
        from dcim.models import Platform
        from netbox_librenms_plugin.models import PlatformMapping

        device = make_device("platform-mapping-conflict")
        existing_target = Platform.objects.create(name="Existing mapping target", slug="existing-mapping-target")
        requested = Platform.objects.create(name="Requested target", slug="requested-target")
        PlatformMapping.objects.create(librenms_os="conflicted-os", netbox_platform=existing_target)

        response = _post(
            logged_in_client,
            "create_and_assign_platform",
            device,
            {
                "platform_name": requested.name,
                "librenms_os": "conflicted-os",
                "create_mapping": "on",
            },
        )

        device.refresh_from_db()
        assert device.platform == requested
        assert PlatformMapping.objects.get(librenms_os="conflicted-os").netbox_platform == existing_target
        assert any("could not be created" in text for text in _messages(response, "warning"))

    def test_invalid_manufacturer_is_rejected_before_platform_creation(self, logged_in_client, librenms_server):
        from dcim.models import Manufacturer, Platform
        from netbox_librenms_plugin.tests.view_test_helpers import missing_pk

        device = make_device("platform-invalid-manufacturer")

        response = _post(
            logged_in_client,
            "create_and_assign_platform",
            device,
            {
                "platform_name": "Manufacturer Rejected Platform",
                "manufacturer": str(missing_pk(Manufacturer)),
            },
        )

        assert not Platform.objects.filter(name="Manufacturer Rejected Platform").exists()
        assert any("manufacturer is not available" in text for text in _messages(response, "error"))

    def test_invalid_platform_slug_rolls_back_creation(self, logged_in_client, librenms_server):
        from dcim.models import Platform

        device = make_device("platform-invalid-slug")

        response = _post(
            logged_in_client,
            "create_and_assign_platform",
            device,
            {"platform_name": "!!!"},
        )

        assert not Platform.objects.filter(name="!!!").exists()
        assert any("could not be created" in text for text in _messages(response, "error"))

    def test_incompatible_manufacturer_rolls_back_platform_and_assignment(
        self,
        logged_in_client,
        librenms_server,
    ):
        from dcim.models import Manufacturer, Platform

        device = make_device("platform-incompatible-manufacturer")
        incompatible = Manufacturer.objects.create(
            name="Incompatible platform manufacturer",
            slug="incompatible-platform-manufacturer",
        )

        response = _post(
            logged_in_client,
            "create_and_assign_platform",
            device,
            {
                "platform_name": "Incompatible Platform",
                "manufacturer": str(incompatible.pk),
            },
        )

        device.refresh_from_db()
        assert device.platform is None
        assert not Platform.objects.filter(name="Incompatible Platform").exists()
        assert any("validation failed" in text for text in _messages(response, "error"))

    def test_missing_mapping_permission_keeps_the_primary_assignment(self, client, librenms_server):
        from dcim.models import Device, Platform

        device = make_device("platform-no-mapping-permission")
        platform = Platform.objects.create(name="Assignable without mapping", slug="assignable-without-mapping")
        user = make_user_with_perms(
            "platform-assignment-without-mapping",
            [("change", Device), ("view", Platform)],
        )
        client.force_login(user)

        response = _post(
            client,
            "create_and_assign_platform",
            device,
            {
                "platform_name": platform.name,
                "librenms_os": "permission-skipped-os",
                "create_mapping": "on",
            },
        )

        device.refresh_from_db()
        assert device.platform == platform
        assert any("lack permission to add mappings" in text for text in _messages(response, "warning"))


@pytest.mark.django_db
class TestAssignVCSerialView:
    def test_real_members_receive_normalized_serials(self, logged_in_client, librenms_server):
        first = make_device("vc-serial-first")
        second = make_device("vc-serial-second")
        make_virtual_chassis("vc-serial-success", first, second)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            first,
            {
                "serial_1": " FIRST-VC-SERIAL ",
                "member_id_1": str(first.pk),
                "serial_2": "SECOND-VC-SERIAL",
                "member_id_2": str(second.pk),
            },
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.serial == "FIRST-VC-SERIAL"
        assert second.serial == "SECOND-VC-SERIAL"
        assert any("assigned 2 serial" in text for text in _messages(response, "success"))

    def test_wrong_chassis_and_missing_member_are_both_reported(self, logged_in_client, librenms_server):
        root = make_device("vc-serial-root")
        sibling = make_device("vc-serial-sibling")
        outsider = make_device("vc-serial-outsider")
        make_virtual_chassis("vc-serial-main", root, sibling)
        make_virtual_chassis("vc-serial-other", outsider)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            root,
            {
                "serial_1": "OUTSIDER-SERIAL",
                "member_id_1": str(outsider.pk),
                "serial_2": "MISSING-SERIAL",
                "member_id_2": str(outsider.pk + 10000),
            },
        )

        outsider.refresh_from_db()
        assert outsider.serial == ""
        errors = _messages(response, "error")
        assert any("not part of the same virtual chassis" in text for text in errors)
        assert any("not found" in text for text in errors)

    def test_device_without_a_chassis_is_rejected(self, logged_in_client, librenms_server):
        device = make_device("vc-serial-standalone")

        response = _post(logged_in_client, "assign_vc_serial", device)

        assert any("not part of a virtual chassis" in text for text in _messages(response, "error"))

    def test_empty_assignment_form_is_informational(self, logged_in_client, librenms_server):
        device = make_device("vc-serial-empty")
        make_virtual_chassis("vc-serial-empty-chassis", device)

        response = _post(logged_in_client, "assign_vc_serial", device)

        assert any("No serial assignments" in text for text in _messages(response, "info"))

    def test_assignment_without_a_member_id_is_ignored(self, logged_in_client, librenms_server):
        device = make_device("vc-serial-no-member")
        make_virtual_chassis("vc-serial-no-member-chassis", device)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            device,
            {"serial_1": "UNUSED-SERIAL", "member_id_1": ""},
        )

        device.refresh_from_db()
        assert device.serial == ""
        assert any("No serial assignments" in text for text in _messages(response, "info"))


@pytest.mark.django_db
class TestRemoveServerMappingView:
    def test_orphaned_mapping_is_removed_while_configured_mapping_remains(
        self,
        logged_in_client,
        librenms_server,
    ):
        from netbox_librenms_plugin.server_mappings import PREFERRED_SERVER_FIELD

        device = make_device(
            "mapping-remove",
            librenms_cf={SERVER_KEY: 6541, "retired": 9001, PREFERRED_SERVER_FIELD: "retired"},
        )

        response = _post(
            logged_in_client,
            "remove_server_mapping",
            device,
            {"object_type": "device", "server_key": "retired", "tab": "interfaces"},
        )

        device.refresh_from_db()
        mapping = device.custom_field_data["librenms_id"]
        assert mapping == {SERVER_KEY: 6541}
        assert response.url.endswith("?tab=interfaces")
        assert any("Removed LibreNMS mapping" in text for text in _messages(response, "success"))

    def test_configured_mapping_cannot_be_removed(self, logged_in_client, librenms_server):
        device = _linked_device("mapping-configured", 6542)

        response = _post(
            logged_in_client,
            "remove_server_mapping",
            device,
            {"object_type": "device", "server_key": SERVER_KEY},
        )

        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == {SERVER_KEY: 6542}
        assert any("Cannot remove mapping for configured server" in text for text in _messages(response, "error"))

    def test_vm_alias_targets_the_virtual_machine_model(self, logged_in_client, librenms_server):
        vm = make_vm("mapping-remove-vm")
        vm.custom_field_data["librenms_id"] = {SERVER_KEY: 6543, "retired": 9002}
        vm.save()

        _post(
            logged_in_client,
            "remove_server_mapping",
            vm,
            {"object_type": "virtualmachine", "server_key": "retired"},
        )

        vm.refresh_from_db()
        assert vm.custom_field_data["librenms_id"] == {SERVER_KEY: 6543}

    def test_unsupported_object_type_is_a_400(self, logged_in_client, librenms_server):
        device = make_device("mapping-invalid-type")

        response = _post(
            logged_in_client,
            "remove_server_mapping",
            device,
            {"object_type": "rack", "server_key": "retired"},
        )

        assert response.status_code == 400
        assert b"Invalid object_type" in response.content

    def test_missing_server_key_is_rejected(self, logged_in_client, librenms_server):
        device = _linked_device("mapping-missing-key", 6544)

        response = _post(
            logged_in_client,
            "remove_server_mapping",
            device,
            {"object_type": "device", "server_key": ""},
        )

        assert any("No server_key provided" in text for text in _messages(response, "error"))

    def test_absent_orphaned_mapping_is_informational(self, logged_in_client, librenms_server):
        device = _linked_device("mapping-absent-key", 6545)

        response = _post(
            logged_in_client,
            "remove_server_mapping",
            device,
            {"object_type": "device", "server_key": "retired"},
        )

        assert any("No mapping found" in text for text in _messages(response, "warning"))

    @pytest.mark.parametrize(
        ("failure_type", "expected_message"),
        [
            ("validation", "Validation error removing LibreNMS mapping"),
            ("unexpected", "Unexpected error removing LibreNMS mapping"),
        ],
    )
    def test_save_failures_roll_back_the_mapping(
        self,
        logged_in_client,
        librenms_server,
        failure_type,
        expected_message,
    ):
        from dcim.models import Device
        from django.core.exceptions import ValidationError
        from django.db.models.signals import pre_save

        device = make_device(
            f"mapping-{failure_type}-failure",
            librenms_cf={SERVER_KEY: 6546, "retired": 9003},
        )

        def reject_save(sender, instance, **kwargs):
            if instance.pk != device.pk:
                return
            if failure_type == "validation":
                raise ValidationError({"custom_field_data": ["mapping rejected"]})
            raise RuntimeError("database write failed")

        pre_save.connect(reject_save, sender=Device, weak=False)
        try:
            response = _post(
                logged_in_client,
                "remove_server_mapping",
                device,
                {"object_type": "device", "server_key": "retired"},
            )
        finally:
            pre_save.disconnect(reject_save, sender=Device)

        device.refresh_from_db()
        rendered_messages = _messages(response)
        assert device.custom_field_data["librenms_id"] == {SERVER_KEY: 6546, "retired": 9003}
        assert any(expected_message in text for text in rendered_messages)
        assert not any("Removed LibreNMS mapping" in text for text in rendered_messages)


@pytest.mark.django_db
class TestSetPreferredServerView:
    @pytest.mark.parametrize("object_type", ["device", "virtualmachine"])
    def test_preference_is_persisted_for_devices_and_vms(
        self,
        logged_in_client,
        librenms_server,
        object_type,
    ):
        from netbox_librenms_plugin.server_mappings import PREFERRED_SERVER_FIELD

        owner = make_device("preferred-device") if object_type == "device" else make_vm("preferred-vm")
        owner.custom_field_data["librenms_id"] = {SERVER_KEY: 6551, SECONDARY_KEY: 6552}
        owner.save()

        response = _post(
            logged_in_client,
            "set_preferred_server",
            owner,
            {
                "object_type": object_type,
                "server_key": SECONDARY_KEY,
                "active_server_key": SERVER_KEY,
                "tab": "interfaces",
            },
        )

        owner.refresh_from_db()
        assert owner.custom_field_data["librenms_id"][PREFERRED_SERVER_FIELD] == SECONDARY_KEY
        assert f"server_key={SERVER_KEY}" in response.url
        assert "tab=interfaces" in response.url
        assert any("Preferred LibreNMS server changed" in text for text in _messages(response, "success"))

    def test_single_usable_mapping_cannot_have_a_preference(self, logged_in_client, librenms_server):
        device = _linked_device("preferred-single", 6553)

        response = _post(
            logged_in_client,
            "set_preferred_server",
            device,
            {"object_type": "device", "server_key": SERVER_KEY},
        )

        assert any("requires at least two usable" in text for text in _messages(response, "error"))

    def test_unknown_preference_key_is_rejected(self, logged_in_client, librenms_server):
        device = make_device(
            "preferred-unknown",
            librenms_cf={SERVER_KEY: 6554, SECONDARY_KEY: 6555},
        )

        response = _post(
            logged_in_client,
            "set_preferred_server",
            device,
            {"object_type": "device", "server_key": "retired"},
        )

        assert any("not a usable mapping" in text for text in _messages(response, "error"))

    def test_unsupported_object_type_is_a_400(self, logged_in_client, librenms_server):
        device = make_device("preferred-invalid-type")

        response = _post(
            logged_in_client,
            "set_preferred_server",
            device,
            {"object_type": "rack"},
        )

        assert response.status_code == 400

    def test_missing_preference_key_is_rejected(self, logged_in_client, librenms_server):
        device = make_device(
            "preferred-missing-key",
            librenms_cf={SERVER_KEY: 6556, SECONDARY_KEY: 6557},
        )

        response = _post(
            logged_in_client,
            "set_preferred_server",
            device,
            {"object_type": "device", "server_key": ""},
        )

        assert any("server key must be a non-empty string" in text for text in _messages(response, "error"))


@pytest.mark.django_db
class TestConvertLegacyLibreNMSIdView:
    def test_matching_device_serial_converts_the_legacy_id(self, logged_in_client, librenms_server):
        device = make_device("legacy-convert", serial="LEGACY-SERIAL", librenms_cf=" 6561 ")
        librenms_server.device_info_response(
            device_id=6561,
            hostname=device.name,
            serial=" LEGACY-SERIAL ",
        )

        response = _post(
            logged_in_client,
            "convert_legacy_librenms_id",
            device,
            {"object_type": "device"},
        )

        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == {SERVER_KEY: 6561}
        assert any("Converted legacy librenms_id" in text for text in _messages(response, "success"))

    def test_serial_mismatch_preserves_the_legacy_id(self, logged_in_client, librenms_server):
        device = make_device("legacy-mismatch", serial="NETBOX-SERIAL", librenms_cf=6562)
        librenms_server.device_info_response(
            device_id=6562,
            hostname=device.name,
            serial="LIBRENMS-SERIAL",
        )

        response = _post(
            logged_in_client,
            "convert_legacy_librenms_id",
            device,
            {"object_type": "device"},
        )

        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == 6562
        assert any("Serial number mismatch" in text for text in _messages(response, "error"))

    def test_vm_conversion_skips_the_device_serial_gate(self, logged_in_client, librenms_server):
        vm = make_vm("legacy-convert-vm")
        vm.custom_field_data["librenms_id"] = 6563
        vm.save()
        librenms_server.device_info_response(device_id=6563, hostname=vm.name, serial="REMOTE-SERIAL")

        _post(
            logged_in_client,
            "convert_legacy_librenms_id",
            vm,
            {"object_type": "virtualmachine"},
        )

        vm.refresh_from_db()
        assert vm.custom_field_data["librenms_id"] == {SERVER_KEY: 6563}

    @pytest.mark.parametrize(
        ("value", "fragment"),
        [
            ({SERVER_KEY: 6564}, "already in the server-scoped"),
            (True, "invalid boolean"),
            ("not-an-id", "not a valid integer"),
        ],
    )
    def test_nonconvertible_values_fail_without_http(
        self,
        logged_in_client,
        librenms_server,
        value,
        fragment,
    ):
        device = make_device(f"legacy-invalid-{type(value).__name__}", librenms_cf=value)

        response = _post(
            logged_in_client,
            "convert_legacy_librenms_id",
            device,
            {"object_type": "device"},
        )

        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == value
        assert any(fragment in text for text in _messages(response))

    def test_live_lookup_failure_preserves_the_legacy_id(self, logged_in_client, librenms_server):
        device = make_device("legacy-live-failure", serial="LEGACY-LIVE", librenms_cf=6565)
        librenms_server.register("/api/v0/devices/6565", {"status": "error"}, status=404)

        response = _post(
            logged_in_client,
            "convert_legacy_librenms_id",
            device,
            {"object_type": "device"},
        )

        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == 6565
        assert any("Could not retrieve device info" in text for text in _messages(response, "error"))

    def test_stale_server_key_fails_closed(self, logged_in_client, librenms_server):
        device = make_device("legacy-stale-server", serial="LEGACY-STALE", librenms_cf=6566)

        response = _post(
            logged_in_client,
            "convert_legacy_librenms_id",
            device,
            {"object_type": "device", "server_key": "retired"},
        )

        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == 6566
        assert any("no longer configured" in text for text in _messages(response, "error"))

    def test_conflicting_assignment_blocks_conversion(self, logged_in_client, librenms_server):
        device = make_device("legacy-conflict-source", serial="LEGACY-CONFLICT", librenms_cf=6567)
        make_device("legacy-conflict-owner", librenms_cf={SERVER_KEY: 6567})
        librenms_server.device_info_response(
            device_id=6567,
            hostname=device.name,
            serial=device.serial,
        )

        response = _post(
            logged_in_client,
            "convert_legacy_librenms_id",
            device,
            {"object_type": "device"},
        )

        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == 6567
        assert any("ambiguous" in text for text in _messages(response, "error"))

    def test_unsupported_object_type_is_a_400(self, logged_in_client, librenms_server):
        device = make_device("legacy-invalid-type")

        response = _post(
            logged_in_client,
            "convert_legacy_librenms_id",
            device,
            {"object_type": "rack"},
        )

        assert response.status_code == 400


@pytest.mark.django_db
class TestCommonFieldUpdateFailures:
    @pytest.mark.parametrize(
        "view_name",
        [
            "update_device_name",
            "update_device_serial",
            "update_device_type",
            "update_device_platform",
        ],
    )
    def test_real_permission_gate_denies_an_unprivileged_user(
        self,
        client,
        django_user_model,
        librenms_server,
        view_name,
    ):
        device = _linked_device(f"permission-{view_name}", 6571)
        user = django_user_model.objects.create_user(username=f"denied-{view_name}")
        client.force_login(user)

        response = _post(client, view_name, device)

        assert response.status_code == 403

    @pytest.mark.parametrize(
        "view_name",
        [
            "update_device_name",
            "update_device_serial",
            "update_device_type",
            "update_device_platform",
        ],
    )
    def test_live_lookup_failure_is_reported_consistently(
        self,
        logged_in_client,
        librenms_server,
        view_name,
    ):
        device = _linked_device(f"lookup-failure-{view_name}", 6572)
        librenms_server.register("/api/v0/devices/6572", {"status": "error"}, status=404)

        response = _post(logged_in_client, view_name, device)

        assert any("Failed to retrieve device info" in text for text in _messages(response, "error"))


class TestDeviceFieldHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("device", "device"),
            ("vm", "vm"),
            ("virtualmachine", "vm"),
            ("rack", None),
            (None, None),
        ],
    )
    def test_object_type_normalization(self, value, expected):
        from netbox_librenms_plugin.views.sync.device_fields import _normalize_sync_object_type

        assert _normalize_sync_object_type(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (42, {"default": 42}),
            ("42", {"default": 42}),
            ({"secondary": 42}, {"secondary": 42}),
            (True, {}),
            (" 42 ", {}),
            (None, {}),
            ([], {}),
        ],
    )
    def test_mapping_normalization(self, value, expected):
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        assert RemoveServerMappingView()._normalize_librenms_mapping(value) == expected

    def test_model_and_url_helpers_distinguish_devices_from_vms(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine
        from netbox_librenms_plugin.views.sync.device_fields import _sync_model, _sync_url_name

        assert _sync_model("device") is Device
        assert _sync_model("vm") is VirtualMachine
        assert _sync_url_name("device").endswith("device_librenms_sync")
        assert _sync_url_name("vm").endswith("vm_librenms_sync")
