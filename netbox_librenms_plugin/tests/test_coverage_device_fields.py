"""Request-level coverage for device and VM field-sync actions."""

from contextlib import ExitStack
from copy import deepcopy

import pytest
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.middleware import TRY_AGAIN_MESSAGE
from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_superuser,
    make_virtual_chassis,
    make_vm,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row, lock_timeout, second_connection
from netbox_librenms_plugin.tests.mapping_fixtures import (
    mapping_from_change_record,
    seed_mapping,
    seed_stored_mapping,
    stored_mapping_for_test,
)
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import (
    assert_update_logged,
    make_user_with_perms,
    message_texts,
    messages_on,
    update_change,
)


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
def librenms_server(settings, monkeypatch):
    """Run a local HTTP server for real LibreNMS client requests."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
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


def _reporting_device(name, server, device_id, os):
    """A linked device whose LibreNMS device reports *os*."""
    device = _linked_device(name, device_id)
    server.device_info_response(device_id=device_id, hostname=name, os=os)
    return device


def _messages(response, level=None):
    return message_texts(response.wsgi_request, level=level)


def _post_while_the_user_row_is_locked(client, name, obj, data, *, check_at_save):
    """
    POST while another session holds the user row: the change record's user foreign key check meets a real 55P03.

    The check is deferred to COMMIT, or with *check_at_save* made immediate at the owner's UPDATE, so it runs inside save().
    """
    from django.contrib.auth import get_user_model
    from django.db import connection

    def immediate_checks(execute, sql, params, many, context):
        if sql.startswith('UPDATE "dcim_device"'):
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        return execute(sql, params, many, context)

    user_model = get_user_model()
    with second_connection() as other, ExitStack() as stack:
        lock_row(other, user_model, int(client.session["_auth_user_id"]))
        stack.enter_context(lock_timeout(200))
        if check_at_save:
            stack.enter_context(connection.execute_wrapper(immediate_checks))
        return _post(client, name, obj, data)


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
        assert_update_logged(device, "name", "old-live-name", "fresh-name")

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

    @pytest.mark.parametrize("hostname", ["stack-a", ""], ids=["upstream-name", "no-upstream-name"])
    def test_member_name_reports_which_name_could_not_be_resolved(self, logged_in_client, librenms_server, hostname):
        from netbox_librenms_plugin.models import LibreNMSSettings, NormalizationRule
        from netbox_librenms_plugin.views.sync.device_fields import _MEMBER_NAME_RULES_UNAVAILABLE, _NO_LIBRENMS_NAME

        member = _linked_device("name-before-unavailable-rules", 6507, serial="MEMBER-SN1")
        make_virtual_chassis("name-unavailable-chassis", make_device("name-master"), member)
        plugin_settings, _ = LibreNMSSettings.objects.get_or_create(pk=1)
        plugin_settings.vc_member_name_pattern = "-{serial}"
        plugin_settings.save(update_fields=["vc_member_name_pattern"])
        rule = NormalizationRule.objects.create(scope="serial", match_pattern=r"^(.+)$", replacement=r"\1")
        NormalizationRule.objects.filter(pk=rule.pk).update(replacement=r"X\1")
        librenms_server.device_info_response(device_id=6507, hostname=hostname)

        response = _post(logged_in_client, "update_device_name", member)

        member.refresh_from_db()
        assert response.status_code == 302
        assert member.name == "name-before-unavailable-rules"
        assert _messages(response, "warning") == [_MEMBER_NAME_RULES_UNAVAILABLE if hostname else _NO_LIBRENMS_NAME]

    def test_real_duplicate_name_validation_rolls_the_change_back(self, logged_in_client, librenms_server):
        device = _linked_device("name-before-duplicate", 6503)
        duplicate = make_device("occupied-name")
        librenms_server.device_info_response(device_id=6503, hostname=duplicate.name)

        response = _post(logged_in_client, "update_device_name", device)

        device.refresh_from_db()
        assert device.name == "name-before-duplicate"
        assert any("Failed to update device name" in text for text in _messages(response, "error"))

    def test_a_pre_existing_invalid_field_is_reported_as_unrelated(self, logged_in_client, librenms_server):
        """A device already failing validation elsewhere must not read as a name problem."""
        from ipam.models import IPAddress

        from dcim.models import Device

        device = _linked_device("name-before-invalid-oob", 6505)
        stray = IPAddress.objects.create(address="192.0.2.50/24")
        # Written past validation, which is how such a row reaches the database at all.
        Device.objects.filter(pk=device.pk).update(oob_ip=stray)
        librenms_server.device_info_response(device_id=6505, hostname="renamed-host")

        response = _post(logged_in_client, "update_device_name", device)

        device.refresh_from_db()
        assert device.name == "name-before-invalid-oob"
        errors = _messages(response, "error")
        assert any("another field fails validation. oob_ip:" in text for text in errors), errors
        # The old rendering dumped a raw error dict, which read as though the rename needed oob_ip.
        assert not any("{'oob_ip'" in text for text in errors), errors

    def test_unknown_server_fails_closed_without_an_http_fallback(self, logged_in_client, librenms_server):
        device = _linked_device("name-stale-server", 6504)

        response = logged_in_client.post(
            _url("update_device_name", device.pk),
            {"server_key": "retired"},
        )

        device.refresh_from_db()
        assert device.name == "name-stale-server"
        assert any("no longer configured" in text for text in _messages(response, "error"))

    def test_repeated_server_keys_fail_closed(self, logged_in_client, librenms_server):
        """Two different configured keys in one POST are ambiguous, so no server may be chosen."""
        device = _linked_device("name-repeated-server", 6506)
        librenms_server.device_info_response(device_id=6506, hostname="renamed-by-ambiguous-post")

        response = logged_in_client.post(
            _url("update_device_name", device.pk),
            {"server_key": [SERVER_KEY, SECONDARY_KEY]},
        )

        device.refresh_from_db()
        assert device.name == "name-repeated-server"
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
        assert_update_logged(device, "serial", "OLD-SERIAL", "NEW-SERIAL")

    @pytest.mark.parametrize("serial", [None, "", "-", "  ", "unknown", " N/A "])
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

    @pytest.mark.parametrize(("reported", "stored"), [(100200, "100200"), (0, "OLD-ZERO")])
    def test_a_numeric_serial_is_stored_as_text_when_it_has_an_identity(
        self, logged_in_client, librenms_server, reported, stored
    ):
        """A one-digit serial identifies no hardware, so the update keeps the stored serial."""
        device = _linked_device(f"serial-numeric-{reported}", 6514, serial="OLD-ZERO")
        librenms_server.device_info_response(device_id=6514, hostname=device.name, serial=reported)

        _post(logged_in_client, "update_device_serial", device)

        device.refresh_from_db()
        assert device.serial == stored

    @pytest.mark.parametrize("serial", [["LIST-SERIAL"], {"serial": "LIST-SERIAL"}, True])
    def test_a_malformed_live_serial_preserves_the_stored_value(self, logged_in_client, librenms_server, serial):
        """A list or an object is no serial: the update refuses it before it locks or resolves it."""
        device = _linked_device("serial-malformed", 6515, serial="KEPT-SERIAL")
        librenms_server.device_info_response(device_id=6515, hostname=device.name, serial=serial)

        response = _post(logged_in_client, "update_device_serial", device)

        device.refresh_from_db()
        assert response.status_code == 302
        assert device.serial == "KEPT-SERIAL"
        assert _messages(response, "error") == ["LibreNMS reports a serial that is not text or a number."]

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

    def test_an_error_the_write_itself_caused_is_not_called_pre_existing(self, logged_in_client, librenms_server):
        """A new device type can invalidate the platform, which the sync caused rather than found."""
        from dcim.models import Platform

        device = _linked_device("device-type-causes-platform-error", 6529)
        platform = Platform.objects.create(
            name="Type-bound platform",
            slug="type-bound-platform",
            manufacturer=device.device_type.manufacturer,
        )
        device.platform = platform
        device.full_clean()
        device.save()
        replacement = self._device_type("causes-conflict", "Conflicting Router")
        librenms_server.device_info_response(
            device_id=6529,
            hostname=device.name,
            hardware=replacement.model,
        )

        response = _post(logged_in_client, "update_device_type", device)

        device.refresh_from_db()
        assert device.device_type != replacement, "the incompatible type must not persist"
        errors = _messages(response, "error")
        assert errors, "the refusal must be reported"
        # The device validated cleanly before this write, so blaming a pre-existing condition
        # would send the operator looking for a fault that was never there.
        assert not any("already fails validation" in text for text in errors), errors
        assert any("platform" in text for text in errors), errors

    def test_exact_hardware_match_changes_the_real_device_type(self, logged_in_client, librenms_server):
        device = _linked_device("device-type-update", 6521)
        original = device.device_type
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
        assert_update_logged(device, "device_type", original.pk, replacement.pk)

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
        assert_update_logged(device, "platform", old_platform.pk, new_platform.pk)


@pytest.mark.django_db
class TestCreateAndAssignPlatformView:
    def test_new_platform_and_mapping_are_created_and_assigned(self, logged_in_client, librenms_server):
        from dcim.models import Platform
        from netbox_librenms_plugin.models import PlatformMapping

        device = _reporting_device("platform-create-device", librenms_server, 6581, " Created-OS ")
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
        assert_update_logged(device, "platform", None, platform.pk)

    def test_existing_platform_is_reused_without_changing_manufacturer(self, logged_in_client, librenms_server):
        from dcim.models import Platform

        device = _reporting_device("platform-reuse-device", librenms_server, 6582, "existing-os")
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

    def test_an_os_mapping_that_now_resolves_makes_the_create_stale(self, logged_in_client, librenms_server):
        """The modal opened while the OS had no platform; a mapping added since then wins over the named create."""
        from dcim.models import Platform
        from netbox_librenms_plugin.models import PlatformMapping

        device = _reporting_device("platform-mapping-conflict", librenms_server, 6583, "conflicted-os")
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
        assert device.platform is None
        assert PlatformMapping.objects.get(librenms_os="conflicted-os").netbox_platform == existing_target
        assert any("now matches LibreNMS OS 'conflicted-os'" in text for text in _messages(response, "error"))

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

    def test_a_platform_netbox_refuses_rolls_back_creation(self, logged_in_client, librenms_server):
        from dcim.models import Platform

        device = _reporting_device("platform-invalid-name", librenms_server, 6584, "invalid-name-os")
        name = "x" * (Platform._meta.get_field("name").max_length + 1)

        response = _post(
            logged_in_client,
            "create_and_assign_platform",
            device,
            {"platform_name": name, "librenms_os": "invalid-name-os"},
        )

        assert not Platform.objects.filter(name=name).exists()
        assert any("could not be created" in text for text in _messages(response, "error"))

    def test_incompatible_manufacturer_rolls_back_platform_and_assignment(
        self,
        logged_in_client,
        librenms_server,
    ):
        from dcim.models import Manufacturer, Platform

        device = _reporting_device("platform-incompatible-manufacturer", librenms_server, 6585, "incompatible-os")
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
                "librenms_os": "incompatible-os",
            },
        )

        device.refresh_from_db()
        assert device.platform is None
        assert not Platform.objects.filter(name="Incompatible Platform").exists()
        errors = _messages(response, "error")
        # The platform is the field being written, so this reports as a failed write, not as a
        # pre-existing problem elsewhere on the device.
        assert any("Failed to assign platform 'Incompatible Platform'" in text for text in errors), errors
        assert any("limited to Incompatible platform manufacturer" in text for text in errors), errors

    def test_missing_mapping_permission_keeps_the_primary_assignment(self, client, librenms_server):
        from dcim.models import Device, Platform

        device = _reporting_device("platform-no-mapping-permission", librenms_server, 6586, "permission-skipped-os")
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
                "librenms_serial_1": " FIRST-VC-SERIAL ",
                "member_id_1": str(first.pk),
                "librenms_serial_2": "SECOND-VC-SERIAL",
                "member_id_2": str(second.pk),
            },
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.serial == "FIRST-VC-SERIAL"
        assert second.serial == "SECOND-VC-SERIAL"
        assert any("assigned 2 serial" in text for text in _messages(response, "success"))
        assert_update_logged(first, "serial", "", "FIRST-VC-SERIAL")
        assert_update_logged(second, "serial", "", "SECOND-VC-SERIAL")

    @transactional_db_with_all_apps()
    def test_a_lock_conflict_on_one_member_changes_no_member(self, logged_in_client, librenms_server):
        """Another session holds the second member's row, so the whole POST gives the try-again answer."""
        from dcim.models import Device

        first = make_device("vc-serial-lock-first")
        second = make_device("vc-serial-lock-second")
        make_virtual_chassis("vc-serial-lock", first, second)

        with second_connection() as other:
            lock_row(other, Device, second.pk)
            with lock_timeout(200):
                response = _post(
                    logged_in_client,
                    "assign_vc_serial",
                    first,
                    {
                        "librenms_serial_1": "FIRST-LOCKED-SERIAL",
                        "member_id_1": str(first.pk),
                        "librenms_serial_2": "SECOND-LOCKED-SERIAL",
                        "member_id_2": str(second.pk),
                    },
                )

        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.serial, second.serial) == ("", "")
        assert response.status_code == 302
        assert messages_on(response.wsgi_request) == [("error", TRY_AGAIN_MESSAGE)]

    def test_a_post_without_the_librenms_serial_writes_nothing(self, logged_in_client, librenms_server):
        """A page rendered before the raw serial field posts only the keyed serial, so the view refuses it."""
        first = make_device("vc-serial-stale-first")
        second = make_device("vc-serial-stale-second")
        make_virtual_chassis("vc-serial-stale", first, second)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            first,
            {
                "librenms_serial_1": "STALE-FIRST",
                "member_id_1": str(first.pk),
                "serial_2": "STALE-SECOND",
                "member_id_2": str(second.pk),
            },
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.serial, second.serial) == ("", "")
        assert _messages(response, "success") == []
        assert len(_messages(response, "error")) == 1

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
                "librenms_serial_1": "OUTSIDER-SERIAL",
                "member_id_1": str(outsider.pk),
                "librenms_serial_2": "MISSING-SERIAL",
                "member_id_2": str(outsider.pk + 10000),
                "librenms_serial_3": "GARBLED-SERIAL",
                "member_id_3": "not-a-number",
            },
        )

        outsider.refresh_from_db()
        assert outsider.serial == ""
        errors = _messages(response, "error")
        assert any("not part of the same virtual chassis" in text for text in errors)
        assert f"Device with ID {outsider.pk + 10000} not found" in errors
        assert "Device with ID not-a-number not found" in errors

    def test_the_redirect_keeps_the_active_server_and_tab(self, logged_in_client, librenms_server):
        """A multi-server user must land back on the server and tab the modal was opened from."""
        first = make_device("vc-serial-context-first")
        second = make_device("vc-serial-context-second")
        make_virtual_chassis("vc-serial-context", first, second)

        response = logged_in_client.post(
            _url("assign_vc_serial", first.pk),
            {
                "server_key": SECONDARY_KEY,
                "tab": "cables",
                "librenms_serial_1": "CONTEXT-SERIAL",
                "member_id_1": str(second.pk),
            },
        )

        assert response.status_code == 302
        assert response.url == f"{_url('device_librenms_sync', first.pk)}?tab=cables&server_key={SECONDARY_KEY}"
        second.refresh_from_db()
        assert second.serial == "CONTEXT-SERIAL"

    def test_a_rejected_device_still_returns_to_its_server_and_tab(self, logged_in_client, librenms_server):
        """The early error branch must preserve the context the success branch does."""
        device = make_device("vc-serial-context-standalone")

        response = logged_in_client.post(
            _url("assign_vc_serial", device.pk),
            {"server_key": SECONDARY_KEY, "tab": "cables"},
        )

        assert response.status_code == 302
        assert response.url == f"{_url('device_librenms_sync', device.pk)}?tab=cables&server_key={SECONDARY_KEY}"
        assert any("not part of a virtual chassis" in text for text in _messages(response, "error"))

    def test_device_without_a_chassis_is_rejected(self, logged_in_client, librenms_server):
        device = make_device("vc-serial-standalone")

        response = _post(logged_in_client, "assign_vc_serial", device)

        assert any("not part of a virtual chassis" in text for text in _messages(response, "error"))

    def test_empty_assignment_form_is_informational(self, logged_in_client, librenms_server):
        device = make_device("vc-serial-empty")
        make_virtual_chassis("vc-serial-empty-chassis", device)

        response = _post(logged_in_client, "assign_vc_serial", device)

        assert any("No serial assignments" in text for text in _messages(response, "info"))

    def test_an_absent_identity_is_refused_and_an_unchanged_serial_is_not_checked(
        self, logged_in_client, librenms_server
    ):
        """A serial without identity refuses its member; a stored duplicate is a baseline that Sync All keeps."""
        absent = make_device("vc-serial-absent")
        kept = make_device("vc-serial-kept", serial="DUP-SERIAL")
        placeholder = make_device("vc-serial-placeholder", serial="PLACEHOLDER-KEPT")
        make_device("vc-serial-duplicate-holder", serial="DUP-SERIAL")
        make_virtual_chassis("vc-serial-baseline", absent, kept, placeholder)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            absent,
            {
                "librenms_serial_1": "0",
                "member_id_1": str(absent.pk),
                "librenms_serial_2": "DUP-SERIAL",
                "member_id_2": str(kept.pk),
                "librenms_serial_3": "-",
                "member_id_3": str(placeholder.pk),
            },
        )

        for member in (absent, kept, placeholder):
            member.refresh_from_db()
        assert (absent.serial, kept.serial, placeholder.serial) == ("", "DUP-SERIAL", "PLACEHOLDER-KEPT")
        assert _messages(response, "error") == [f"{absent.name}: the LibreNMS serial identifies no hardware"]

    def test_only_a_saved_serial_counts_as_an_assignment(self, logged_in_client, librenms_server):
        """A member that already holds the serial is not saved, so the success count leaves it out."""
        changed = make_device("vc-serial-count-changed")
        unchanged = make_device("vc-serial-count-unchanged", serial="COUNT-KEPT")
        make_virtual_chassis("vc-serial-count", changed, unchanged)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            changed,
            {
                "librenms_serial_1": "COUNT-NEW",
                "member_id_1": str(changed.pk),
                "librenms_serial_2": "COUNT-KEPT",
                "member_id_2": str(unchanged.pk),
            },
        )

        changed.refresh_from_db()
        assert changed.serial == "COUNT-NEW"
        success = _messages(response, "success")
        assert len(success) == 1
        assert "assigned 1 serial" in success[0]

    def test_a_post_of_only_unchanged_serials_reports_no_assignment(self, logged_in_client, librenms_server):
        first = make_device("vc-serial-all-kept-first", serial="ALL-KEPT-1")
        second = make_device("vc-serial-all-kept-second", serial="ALL-KEPT-2")
        make_virtual_chassis("vc-serial-all-kept", first, second)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            first,
            {
                "librenms_serial_1": "ALL-KEPT-1",
                "member_id_1": str(first.pk),
                "librenms_serial_2": "ALL-KEPT-2",
                "member_id_2": str(second.pk),
            },
        )

        assert _messages(response, "success") == []
        assert _messages(response, "error") == []
        assert any("No serial assignments" in text for text in _messages(response, "info"))

    def test_a_serial_swap_between_members_applies_in_one_post(self, logged_in_client, librenms_server):
        """Each member gives up its stored serial to the other, so neither one is a holder against the swap."""
        first = make_device("vc-serial-swap-first", serial="SWAP-BBB")
        second = make_device("vc-serial-swap-second", serial="SWAP-AAA")
        make_virtual_chassis("vc-serial-swap", first, second)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            first,
            {
                "librenms_serial_1": "SWAP-AAA",
                "member_id_1": str(first.pk),
                "librenms_serial_2": "SWAP-BBB",
                "member_id_2": str(second.pk),
            },
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.serial, second.serial) == ("SWAP-AAA", "SWAP-BBB")
        assert _messages(response, "error") == []
        assert any("assigned 2 serial" in text for text in _messages(response, "success"))

    def test_two_members_given_one_identity_change_neither(self, logged_in_client, librenms_server):
        first = make_device("vc-serial-same-first", serial="SAME-OLD-1")
        second = make_device("vc-serial-same-second", serial="SAME-OLD-2")
        make_virtual_chassis("vc-serial-same", first, second)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            first,
            {
                "librenms_serial_1": "SAME-NEW",
                "member_id_1": str(first.pk),
                "librenms_serial_2": " SAME-NEW ",
                "member_id_2": str(second.pk),
            },
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.serial, second.serial) == ("SAME-OLD-1", "SAME-OLD-2")
        assert _messages(response, "success") == []
        errors = _messages(response, "error")
        # The serial was never stored on either member, so the text names the post, not a stored holder.
        assert len(errors) == 2, errors
        assert all(first.name in text and second.name in text for text in errors), errors
        assert not any("already assigned" in text for text in errors), errors

    def test_a_member_that_keeps_the_serial_is_the_only_holder_named(self, logged_in_client, librenms_server):
        """Two members take the serial that a third keeps; only the third holds it after the rollback."""
        keeper = make_device("vc-serial-trio-keeper", serial="TRIO-SERIAL")
        second = make_device("vc-serial-trio-second", serial="TRIO-OLD-2")
        third = make_device("vc-serial-trio-third", serial="TRIO-OLD-3")
        make_virtual_chassis("vc-serial-trio", keeper, second, third)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            keeper,
            {
                "librenms_serial_1": "TRIO-SERIAL",
                "member_id_1": str(keeper.pk),
                "librenms_serial_2": "TRIO-SERIAL",
                "member_id_2": str(second.pk),
                "librenms_serial_3": "TRIO-SERIAL",
                "member_id_3": str(third.pk),
            },
        )

        for member in (keeper, second, third):
            member.refresh_from_db()
        assert (keeper.serial, second.serial, third.serial) == ("TRIO-SERIAL", "TRIO-OLD-2", "TRIO-OLD-3")
        errors = _messages(response, "error")
        assert len(errors) == 2, errors
        assert all(f"assigned to device '{keeper.name}'" in text for text in errors), errors
        assert not any("more than one device" in text for text in errors), errors

    def test_a_holder_that_appears_between_the_reads_is_not_named_as_a_member(
        self, logged_in_client, librenms_server, monkeypatch
    ):
        """A device that takes the serial after the stored-holder read is a stored holder, not a member of the post."""
        from netbox_librenms_plugin.views.sync import device_fields

        first = make_device("vc-serial-late-first", serial="LATE-OLD-1")
        second = make_device("vc-serial-late-second", serial="LATE-OLD-2")
        make_virtual_chassis("vc-serial-late", first, second)
        real = device_fields.written_device_conflict
        late = []

        def conflict_with_a_late_holder(member, *, rules, exclude_pks=()):
            if not exclude_pks and not late:
                late.append(make_device("vc-serial-late-outsider", serial="LATE-NEW"))
            return real(member, rules=rules, exclude_pks=exclude_pks)

        monkeypatch.setattr(device_fields, "written_device_conflict", conflict_with_a_late_holder)
        response = _post(
            logged_in_client,
            "assign_vc_serial",
            first,
            {
                "librenms_serial_1": "LATE-NEW",
                "member_id_1": str(first.pk),
                "librenms_serial_2": "LATE-NEW",
                "member_id_2": str(second.pk),
            },
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.serial, second.serial) == ("LATE-OLD-1", "LATE-OLD-2")
        errors = _messages(response, "error")
        assert late, "precondition: the late holder was written"
        (outsider,) = late
        # The stored-holder read of the second member finds the late holder.
        stored_refusal = (
            f"Serial conflict: 'LATE-NEW' is already assigned to device '{outsider.name}' (ID: {outsider.pk})"
        )
        # The inclusive read of the first member finds the late holder and the second member: a stored conflict.
        first_refusal = "Serial conflict: 'LATE-NEW' is already assigned to more than one device."
        assert errors == [f"{first.name}: {first_refusal}", f"{second.name}: {stored_refusal}"], errors
        assert not any("member 'vc-serial-late-outsider'" in text for text in errors), errors

    def test_a_late_holder_on_a_chassis_with_an_outside_master_gives_the_unavailable_reason(
        self, logged_in_client, librenms_server, monkeypatch
    ):
        """The posted read finds no match it can compare, so the refusal names the reason and no member."""
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.serial_identity import UNAVAILABLE_MASTER_LINK
        from netbox_librenms_plugin.views.sync import device_fields

        first = make_device("vc-serial-stray-first", serial="STRAY-OLD-1")
        second = make_device("vc-serial-stray-second", serial="STRAY-OLD-2")
        make_virtual_chassis("vc-serial-stray", first, second)
        real = device_fields.written_device_conflict
        late = []

        def conflict_with_a_late_holder(member, *, rules, exclude_pks=()):
            if not exclude_pks and not late:
                late.append(make_device("vc-serial-stray-holder", serial="STRAY-NEW"))
                chassis = make_virtual_chassis("vc-serial-stray-other", late[0])
                VirtualChassis.objects.filter(pk=chassis.pk).update(master=make_device("vc-serial-stray-master"))
            return real(member, rules=rules, exclude_pks=exclude_pks)

        monkeypatch.setattr(device_fields, "written_device_conflict", conflict_with_a_late_holder)
        response = _post(
            logged_in_client,
            "assign_vc_serial",
            first,
            {
                "librenms_serial_1": "STRAY-NEW",
                "member_id_1": str(first.pk),
                "librenms_serial_2": "STRAY-NEW",
                "member_id_2": str(second.pk),
            },
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.serial, second.serial) == ("STRAY-OLD-1", "STRAY-OLD-2")
        assert late, "precondition: the late holder was written"
        errors = _messages(response, "error")
        # The posted read of the first member is the one that the late holder makes Unavailable.
        assert f"{first.name}: Serial 'STRAY-NEW' cannot be matched. {UNAVAILABLE_MASTER_LINK}" in errors, errors
        assert not any("selected for this member" in text for text in errors), errors

    def test_one_member_selected_for_two_serials_changes_nothing(self, logged_in_client, librenms_server):
        first = make_device("vc-serial-twice-first", serial="TWICE-OLD")
        second = make_device("vc-serial-twice-second")
        make_virtual_chassis("vc-serial-twice", first, second)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            first,
            {
                "librenms_serial_1": "TWICE-NEW-1",
                "member_id_1": str(first.pk),
                "librenms_serial_2": "TWICE-NEW-2",
                "member_id_2": str(first.pk),
            },
        )

        first.refresh_from_db()
        assert first.serial == "TWICE-OLD"
        assert _messages(response, "success") == []
        assert len(_messages(response, "error")) == 1

    @transactional_db_with_all_apps()
    @pytest.mark.parametrize("released", [False, True])
    def test_a_lock_conflict_wrapped_in_a_validation_error_gives_the_try_again_answer(
        self, logged_in_client, librenms_server, released
    ):
        """A member save meets NetBox's wrapped lock conflict, so the runner retries instead of reporting a refusal."""
        from dcim.models import Device
        from django.contrib.auth import get_user_model
        from django.core.exceptions import ValidationError
        from django.db import OperationalError
        from django.db.models.signals import pre_save

        from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row_nowait

        first = make_device("vc-serial-wrapped-first", serial="WRAPPED-OLD")
        second = make_device("vc-serial-wrapped-second")
        make_virtual_chassis("vc-serial-wrapped", first, second)
        user_model = get_user_model()
        user_pk = int(logged_in_client.session["_auth_user_id"])
        conflicts = []

        def save_meets_a_wrapped_conflict(**_kwargs):
            try:
                lock_row_nowait(user_model, user_pk)
            except OperationalError:
                conflicts.append(user_pk)
                if released:
                    # The other session ends its transaction, so the retry can take the lock.
                    other.rollback()
                raise ValidationError("The hierarchy was modified concurrently; please retry.") from None

        pre_save.connect(save_meets_a_wrapped_conflict, sender=Device, weak=False)
        try:
            with second_connection() as other:
                lock_row(other, user_model, user_pk)
                response = _post(
                    logged_in_client,
                    "assign_vc_serial",
                    first,
                    {"librenms_serial_1": "WRAPPED-NEW", "member_id_1": str(first.pk)},
                )
        finally:
            pre_save.disconnect(save_meets_a_wrapped_conflict, sender=Device)

        first.refresh_from_db()
        assert response.status_code == 302
        if released:
            assert (first.serial, len(conflicts)) == ("WRAPPED-NEW", 1)
            assert messages_on(response.wsgi_request) == [
                ("success", "Successfully assigned 1 serial number(s) to VC members")
            ]
        else:
            assert (first.serial, len(conflicts)) == ("WRAPPED-OLD", 2)
            assert messages_on(response.wsgi_request) == [("error", TRY_AGAIN_MESSAGE)]

    def test_a_swap_with_a_serial_held_outside_the_chassis_changes_neither(self, logged_in_client, librenms_server):
        """The outside holder refuses the second member, so the first member's serial is not freed either."""
        first = make_device("vc-serial-outside-first", serial="OUTSIDE-AAA")
        second = make_device("vc-serial-outside-second", serial="OUTSIDE-BBB")
        holder = make_device("vc-serial-outside-holder", serial="OUTSIDE-CCC")
        make_virtual_chassis("vc-serial-outside", first, second)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            first,
            {
                "librenms_serial_1": "OUTSIDE-BBB",
                "member_id_1": str(first.pk),
                "librenms_serial_2": "OUTSIDE-CCC",
                "member_id_2": str(second.pk),
            },
        )

        for member in (first, second, holder):
            member.refresh_from_db()
        assert (first.serial, second.serial, holder.serial) == ("OUTSIDE-AAA", "OUTSIDE-BBB", "OUTSIDE-CCC")
        assert _messages(response, "success") == []
        errors = _messages(response, "error")
        assert any(f"assigned to device '{holder.name}'" in text for text in errors), errors

    def test_assignment_without_a_member_id_is_ignored(self, logged_in_client, librenms_server):
        device = make_device("vc-serial-no-member")
        make_virtual_chassis("vc-serial-no-member-chassis", device)

        response = _post(
            logged_in_client,
            "assign_vc_serial",
            device,
            {"librenms_serial_1": "UNUSED-SERIAL", "member_id_1": ""},
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
            {
                "object_type": "device",
                "server_key": "retired",
                "active_server_key": SERVER_KEY,
                "tab": "interfaces",
            },
        )

        device.refresh_from_db()
        mapping = stored_mapping_for_test(device)
        assert mapping == {SERVER_KEY: 6541}
        assert response.url.endswith(f"?tab=interfaces&server_key={SERVER_KEY}")
        assert any("Removed LibreNMS mapping" in text for text in _messages(response, "success"))

    def test_a_removed_mapping_is_recorded_with_its_before_state(self, logged_in_client, librenms_server):
        """The owner is saved with only its custom field data, so the save must also move last_updated."""
        from core.models import ObjectChange
        from dcim.models import Device
        from django.contrib.contenttypes.models import ContentType

        device = make_device("mapping-remove-log", librenms_cf={SERVER_KEY: 6544, "retired": 9003})
        old_last_updated = Device.objects.values_list("last_updated", flat=True).get(pk=device.pk)

        _post(logged_in_client, "remove_server_mapping", device, {"object_type": "device", "server_key": "retired"})

        device.refresh_from_db()
        assert device.last_updated > old_last_updated
        change = ObjectChange.objects.get(
            changed_object_type=ContentType.objects.get_for_model(Device), changed_object_id=device.pk, action="update"
        )
        assert mapping_from_change_record(change, before=True) == {SERVER_KEY: 6544, "retired": 9003}
        assert mapping_from_change_record(change, before=False) == {SERVER_KEY: 6544}

    @transactional_db_with_all_apps()
    @pytest.mark.parametrize("check_at_save", [True, False], ids=["in-save", "at-commit"])
    def test_a_lock_conflict_of_the_write_gives_the_try_again_answer(
        self, logged_in_client, librenms_server, caplog, check_at_save
    ):
        device = make_device("mapping-remove-locked", librenms_cf={SERVER_KEY: 6545, "retired": 9004})

        with caplog.at_level("ERROR"):
            response = _post_while_the_user_row_is_locked(
                logged_in_client,
                "remove_server_mapping",
                device,
                {"object_type": "device", "server_key": "retired"},
                check_at_save=check_at_save,
            )

        device.refresh_from_db()
        assert stored_mapping_for_test(device) == {SERVER_KEY: 6545, "retired": 9004}
        assert response.status_code == 302
        assert messages_on(response.wsgi_request) == [("error", TRY_AGAIN_MESSAGE)]
        assert [record.getMessage() for record in caplog.records if record.levelname == "ERROR"] == []

    def test_configured_mapping_cannot_be_removed(self, logged_in_client, librenms_server):
        device = _linked_device("mapping-configured", 6542)

        response = _post(
            logged_in_client,
            "remove_server_mapping",
            device,
            {"object_type": "device", "server_key": SERVER_KEY},
        )

        device.refresh_from_db()
        assert stored_mapping_for_test(device) == {SERVER_KEY: 6542}
        assert any("Cannot remove mapping for configured server" in text for text in _messages(response, "error"))

    def test_vm_alias_targets_the_virtual_machine_model(self, logged_in_client, librenms_server):
        vm = make_vm("mapping-remove-vm")
        seed_mapping(vm, SERVER_KEY, own=6543, save=False)
        seed_mapping(vm, "retired", own=9002)

        _post(
            logged_in_client,
            "remove_server_mapping",
            vm,
            {"object_type": "virtualmachine", "server_key": "retired"},
        )

        vm.refresh_from_db()
        assert stored_mapping_for_test(vm) == {SERVER_KEY: 6543}

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

    @pytest.mark.parametrize("failure_type", ["validation", "unexpected"])
    def test_save_failures_roll_back_the_mapping(self, logged_in_client, librenms_server, failure_type):
        """A validation error is reported; any other error propagates, and both leave the mapping as it was."""
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
            if failure_type == "validation":
                response = _post(
                    logged_in_client,
                    "remove_server_mapping",
                    device,
                    {"object_type": "device", "server_key": "retired"},
                )
            else:
                with pytest.raises(RuntimeError, match="database write failed"):
                    _post(
                        logged_in_client,
                        "remove_server_mapping",
                        device,
                        {"object_type": "device", "server_key": "retired"},
                    )
        finally:
            pre_save.disconnect(reject_save, sender=Device)

        device.refresh_from_db()
        assert stored_mapping_for_test(device) == {SERVER_KEY: 6546, "retired": 9003}
        if failure_type == "validation":
            rendered_messages = _messages(response)
            assert any("Validation error removing LibreNMS mapping" in text for text in rendered_messages)
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
        seed_mapping(owner, SERVER_KEY, own=6551, save=False)
        seed_mapping(owner, SECONDARY_KEY, own=6552)

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
        assert stored_mapping_for_test(owner)[PREFERRED_SERVER_FIELD] == SECONDARY_KEY
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

    @transactional_db_with_all_apps()
    @pytest.mark.parametrize("check_at_save", [True, False], ids=["in-save", "at-commit"])
    def test_a_lock_conflict_of_the_write_gives_the_try_again_answer(
        self, logged_in_client, librenms_server, caplog, check_at_save
    ):
        device = make_device("preferred-locked", librenms_cf={SERVER_KEY: 6556, SECONDARY_KEY: 6557})

        with caplog.at_level("ERROR"):
            response = _post_while_the_user_row_is_locked(
                logged_in_client,
                "set_preferred_server",
                device,
                {"object_type": "device", "server_key": SECONDARY_KEY},
                check_at_save=check_at_save,
            )

        device.refresh_from_db()
        assert stored_mapping_for_test(device) == {SERVER_KEY: 6556, SECONDARY_KEY: 6557}
        assert response.status_code == 302
        assert messages_on(response.wsgi_request) == [("error", TRY_AGAIN_MESSAGE)]
        assert [record.getMessage() for record in caplog.records if record.levelname == "ERROR"] == []

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


def _retype_shape(target, shape, new_maker):
    """Give *target* two serials that only the rule of *new_maker* keys to one value: on chassis members or on modules."""
    from dcim.models import VirtualChassis

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import install_module, make_module_bay, make_virtual_chassis

    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1", manufacturer=new_maker
    )
    if shape == "chassis-members":
        target.serial = "XABC"
        target.save()
        member = make_device(f"{target.name}-member", serial="ABC")
        chassis = make_virtual_chassis(f"{target.name}-chassis", target, member)
        VirtualChassis.objects.filter(pk=chassis.pk).update(master=target)
    else:
        for number, serial in ((1, "XABC"), (2, "ABC")):
            make_module_bay(target, f"Slot {number}", position=str(number))
            install_module(target, f"Slot {number}", f"{target.name}-card-{number}".upper(), serial=serial)
    target.refresh_from_db()


@pytest.mark.django_db
class TestUpdateDeviceTypeSerialIdentity:
    def test_a_type_change_refuses_when_another_device_then_holds_the_stored_serial(
        self, logged_in_client, librenms_server
    ):
        """Under the old manufacturer's rule "XABC" has no identity; under the new type it is the holder's serial."""
        from dcim.models import Device, DeviceType, Manufacturer

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.serial_identity import RETYPE_HELD

        device = make_device("retype-sync-target", serial="XABC", librenms_cf={SERVER_KEY: 6571})
        NormalizationRule.objects.create(
            scope="serial", match_pattern=r"^X(AB)C$", replacement=r"\1", manufacturer=device.device_type.manufacturer
        )
        new_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="Retype sync maker", slug="retype-sync-maker"),
            model="Retype sync model",
            slug="retype-sync-model",
            part_number="RETYPE-SYNC-HW",
        )
        holder = make_device("retype-sync-holder", serial="XABC")
        Device.objects.filter(pk=holder.pk).update(device_type=new_type)
        old_type_id = device.device_type_id
        librenms_server.device_info_response(
            device_id=6571, hostname=device.name, serial="XABC", hardware=new_type.part_number
        )

        response = _post(logged_in_client, "update_device_type", device, {})

        assert Device.objects.get(pk=device.pk).device_type_id == old_type_id
        assert RETYPE_HELD in _messages(response, "error")
        assert not any(holder.name in text for text in _messages(response, "error"))

    @pytest.mark.parametrize("shape", ["chassis-members", "device-modules"])
    def test_a_type_change_refuses_two_serials_that_the_new_rules_key_to_one(
        self, logged_in_client, librenms_server, shape
    ):
        from dcim.models import Device, DeviceType, Manufacturer

        from netbox_librenms_plugin.serial_identity import RETYPE_HELD

        device = make_device(f"retype-sync-{shape}", librenms_cf={SERVER_KEY: 6572})
        new_maker = Manufacturer.objects.create(name=f"Retype sync {shape}", slug=f"retype-sync-{shape}-maker")
        _retype_shape(device, shape, new_maker)
        new_type = DeviceType.objects.create(
            manufacturer=new_maker,
            model=f"Retype sync {shape}",
            slug=f"retype-sync-{shape}-type",
            part_number=f"RETYPE-SYNC-{shape}-HW".upper(),
        )
        old_type_id = device.device_type_id
        librenms_server.device_info_response(device_id=6572, hostname=device.name, hardware=new_type.part_number)

        response = _post(logged_in_client, "update_device_type", device, {})

        assert Device.objects.get(pk=device.pk).device_type_id == old_type_id
        assert RETYPE_HELD in _messages(response, "error")

    @pytest.mark.parametrize("holder_serial", ["XABC", "ABC"], ids=["existing-holder", "new-holder"])
    def test_a_type_change_refuses_each_holder_of_the_new_identity(
        self, logged_in_client, librenms_server, holder_serial
    ):
        """Under the new rule A's "XABC" keys to "ABC": a holder of "XABC" already shares it, one of "ABC" then does."""
        from dcim.models import Device, DeviceType, Manufacturer

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.serial_identity import RETYPE_HELD

        device = make_device("retype-masked-a", serial="XABC", librenms_cf={SERVER_KEY: 6574})
        # The only other Device, so the refusal needs this holder.
        make_device("retype-masked-holder", serial=holder_serial)
        new_maker = Manufacturer.objects.create(name="Retype masked maker", slug="retype-masked-maker")
        NormalizationRule.objects.create(
            scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1", manufacturer=new_maker
        )
        new_type = DeviceType.objects.create(
            manufacturer=new_maker, model="Retype masked", slug="retype-masked", part_number="RETYPE-MASKED-HW"
        )
        old_type_id = device.device_type_id
        librenms_server.device_info_response(device_id=6574, hostname=device.name, hardware=new_type.part_number)

        response = _post(logged_in_client, "update_device_type", device, {})

        assert Device.objects.get(pk=device.pk).device_type_id == old_type_id
        assert RETYPE_HELD in _messages(response, "error")

    def test_a_type_change_without_a_new_shared_serial_is_saved(self, logged_in_client, librenms_server):
        """Two module serials that the old and the new rules both keep apart do not block the change."""
        from dcim.models import Device, DeviceType, Manufacturer

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.tests.conftest import install_module, make_module_bay

        device = make_device("retype-sync-clean", librenms_cf={SERVER_KEY: 6573})
        for number, serial in ((1, "XDEF"), (2, "ABC")):
            make_module_bay(device, f"Slot {number}", position=str(number))
            install_module(device, f"Slot {number}", f"RETYPE-CLEAN-CARD-{number}", serial=serial)
        new_maker = Manufacturer.objects.create(name="Retype sync clean", slug="retype-sync-clean-maker")
        NormalizationRule.objects.create(
            scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1", manufacturer=new_maker
        )
        new_type = DeviceType.objects.create(
            manufacturer=new_maker, model="Retype sync clean", slug="retype-sync-clean", part_number="RETYPE-CLEAN-HW"
        )
        librenms_server.device_info_response(device_id=6573, hostname=device.name, hardware=new_type.part_number)

        _post(logged_in_client, "update_device_type", device, {})

        assert Device.objects.get(pk=device.pk).device_type_id == new_type.pk


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
        assert stored_mapping_for_test(device) == {SERVER_KEY: 6561}
        assert any("Converted legacy librenms_id" in text for text in _messages(response, "success"))
        change = update_change(device)
        assert mapping_from_change_record(change, before=True) == " 6561 "
        assert mapping_from_change_record(change, before=False) == {SERVER_KEY: 6561}

    def test_serial_mismatch_preserves_the_legacy_id(self, logged_in_client, librenms_server):
        from netbox_librenms_plugin.views.sync.device_fields import _SERIAL_MISMATCH_MESSAGE

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
        assert stored_mapping_for_test(device) == 6562
        assert _messages(response, "error") == [_SERIAL_MISMATCH_MESSAGE]

    def test_a_decorated_librenms_serial_confirms_the_stored_serial(self, logged_in_client, librenms_server):
        """The serial rules key both sides, so a stored "ABC" matches LibreNMS "S/N ABC"."""
        from dcim.models import Device
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.tests.lock_conflict_helpers import first_row_lock

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        device = make_device("legacy-decorated", serial="ABC-LEGACY", librenms_cf=6566)
        librenms_server.device_info_response(device_id=6566, hostname=device.name, serial="S/N ABC-LEGACY")

        with CaptureQueriesContext(connection) as queries:
            _post(logged_in_client, "convert_legacy_librenms_id", device, {"object_type": "device"})

        device.refresh_from_db()
        assert stored_mapping_for_test(device) == {SERVER_KEY: 6566}
        # The serial identity lock comes before the row lock.
        statements = [query["sql"] for query in queries.captured_queries]
        identity_lock = next(i for i, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql)
        assert identity_lock < first_row_lock(statements, Device, device.pk)

    def test_the_first_row_lock_includes_an_unqualified_key(self):
        """The ordering check above must see a single-table lock that names ``id`` without the table."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.lock_conflict_helpers import first_row_lock

        statements = [
            'SELECT 1 FROM "dcim_device" WHERE id = 7 FOR UPDATE',
            'SELECT "dcim_device"."id" FROM "dcim_device" WHERE "dcim_device"."id" = 7 FOR UPDATE',
            'SELECT "dcim_device"."id" FROM "dcim_device" WHERE "dcim_device"."id" IN (3, 7) FOR UPDATE',
        ]
        assert first_row_lock(statements, Device, 7) == 0
        assert first_row_lock(statements[1:], Device, 7) == 0
        assert first_row_lock(statements[2:], Device, 7) == 0
        assert first_row_lock(['SELECT 1 FROM "dcim_device" WHERE id = 70 FOR UPDATE', *statements], Device, 7) == 1

    @pytest.mark.parametrize(
        "statement",
        [
            f"SELECT 1 FROM {table} {alias}WHERE {qualifier}{column} = 7 FOR UPDATE{lock_of}"
            for table in ('"dcim_device"', "dcim_device")
            for alias, qualifiers, lock_of in (
                ("", ("", '"dcim_device".', "dcim_device."), ""),
                ("AS d ", ("", "d.", '"d".'), " OF d"),
                ("d ", ("", "d.", '"d".'), " OF d"),
                ('AS "d" ', ("", "d.", '"d".'), ' OF "d"'),
            )
            for qualifier in qualifiers
            for column in ("id", '"id"')
        ],
    )
    def test_the_first_row_lock_includes_every_form_of_the_key(self, statement):
        """A lock of the table counts with or without an alias, whatever form of the id column its WHERE names."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.lock_conflict_helpers import first_row_lock

        assert first_row_lock([statement, 'SELECT 1 FROM "dcim_device" WHERE id = 7 FOR UPDATE'], Device, 7) == 0
        # An alias of another table does not qualify the key of the Device row.
        joined = 'SELECT 1 FROM "dcim_device" JOIN "dcim_site" s ON s.id = 7 WHERE s."id" = 7 FOR UPDATE'
        assert first_row_lock([joined, statement], Device, 7) == 1

    @pytest.mark.parametrize(
        ("source", "lock", "counts"),
        [
            ("alias", "FOR UPDATE OF d", True),
            ("alias", "FOR UPDATE OF s, d", True),
            ("alias", "FOR UPDATE", True),
            ("alias", "FOR UPDATE OF s", False),
            ("name", 'FOR UPDATE OF "dcim_device"', True),
            ("name", "FOR NO KEY UPDATE OF dcim_device", True),
            ("name", 'FOR UPDATE OF "dcim_site"', False),
            ("joined", "FOR UPDATE OF d", True),
            ("joined", "FOR UPDATE OF s", False),
            ("joined", "FOR UPDATE", True),
            ("joined_name", 'FOR UPDATE OF "dcim_device"', True),
            ("joined_name", 'FOR UPDATE OF "dcim_site"', False),
            ("joined_other_key", "FOR UPDATE", False),
        ],
    )
    def test_the_first_row_lock_counts_a_lock_of_only_the_target_table(self, source, lock, counts):
        """A ``FOR ... OF`` clause locks only the tables it names; a clause without OF locks every table."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.lock_conflict_helpers import first_row_lock

        joins = {
            "alias": "FROM dcim_device d JOIN dcim_site s ON s.id = d.site_id WHERE d.id = 7",
            "name": 'FROM "dcim_device" JOIN "dcim_site" ON "dcim_site"."id" = "dcim_device"."site_id"'
            ' WHERE "dcim_device"."id" = 7',
            "joined": "FROM dcim_site s JOIN dcim_device d ON d.site_id = s.id WHERE d.id = 7",
            "joined_name": 'FROM "dcim_site" INNER JOIN "dcim_device" ON "dcim_device"."site_id" = "dcim_site"."id"'
            ' WHERE "dcim_device"."id" = 7',
            "joined_other_key": "FROM dcim_site s JOIN dcim_device d ON d.site_id = s.id WHERE s.id = 7",
        }
        statement = f"SELECT 1 {joins[source]} {lock}"
        recognized = 'SELECT 1 FROM "dcim_device" WHERE id = 7 FOR UPDATE'
        assert first_row_lock([statement, recognized], Device, 7) == (0 if counts else 1)

    @pytest.mark.parametrize(("selected", "counts"), [("m", True), ("d", False)])
    def test_the_first_row_lock_checks_every_alias_of_a_self_join(self, selected, counts):
        """A self-join that locks only its second Device alias locks the row that this alias selects."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.lock_conflict_helpers import first_row_lock

        statement = (
            "SELECT 1 FROM dcim_device d JOIN dcim_device m ON m.id = d.vc_master_id"
            f" WHERE {selected}.id = 7 FOR UPDATE OF m"
        )
        recognized = 'SELECT 1 FROM "dcim_device" WHERE id = 7 FOR UPDATE'
        assert first_row_lock([statement, recognized], Device, 7) == (0 if counts else 1)

    def test_the_first_row_lock_reads_a_grouped_positive_key(self):
        """Parentheses around a positive key still select Device 7."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.lock_conflict_helpers import first_row_lock

        statements = [
            'SELECT 1 FROM "dcim_device" WHERE "dcim_device"."id" = 3 FOR UPDATE',
            'SELECT 1 FROM "dcim_device" WHERE (("dcim_device"."id" = 7)) FOR UPDATE',
        ]
        assert first_row_lock(statements, Device, 7) == 1

    @pytest.mark.parametrize(
        "where",
        [
            'NOT ("dcim_device"."id" = 7)',
            'NOT ("dcim_device"."id" IN (3, 7))',
            'NOT ( ( ("dcim_device"."id" IN (3, 7)) ) )',
            'NOT ("dcim_device"."id" = 3 OR "dcim_device"."id" = 7)',
            'NOT ("dcim_device"."id" = 3) AND ("dcim_device"."id" = 7)',
            'NOT ("dcim_device"."id" = 3)',
            'NOT ("dcim_device"."name" IN (\'a\') AND ("dcim_device"."id" = 7))',
            '("dcim_device"."id" IN (3, 7) AND NOT ("dcim_device"."id" = 7))',
        ],
        ids=["key", "key-list", "nested", "or", "after-a-negated-group", "other-key", "and", "inside-and-outside"],
    )
    def test_the_row_lock_check_refuses_a_locking_statement_with_not(self, where):
        """``NOT (id = 3)`` locks Device 7 without naming it, so the check reads no NOT at all."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.lock_conflict_helpers import first_row_lock

        with pytest.raises(AssertionError):
            first_row_lock([f'SELECT 1 FROM "dcim_device" WHERE {where} FOR UPDATE'], Device, 7)

    @pytest.mark.parametrize("hidden_twin", [True, False])
    def test_a_hidden_device_with_the_serial_in_another_form_refuses_the_conversion(
        self, client, librenms_server, hidden_twin
    ):
        """The serial confirms the target only when no other device holds it, also one the user cannot see."""
        from netbox_librenms_plugin.views.sync.device_fields import _SERIAL_MISMATCH_MESSAGE
        from dcim.models import Device

        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        device = make_device("legacy-twin-target", serial="TWIN-LEGACY", librenms_cf=6567)
        if hidden_twin:
            make_device("legacy-twin-hidden", serial="S/N TWIN-LEGACY")
        librenms_server.device_info_response(device_id=6567, hostname=device.name, serial="TWIN-LEGACY")
        client.force_login(
            make_user_with_perms(
                "legacy-twin-user", [("view", Device), ("change", Device)], constraints={"name": device.name}
            )
        )

        response = _post(client, "convert_legacy_librenms_id", device, {"object_type": "device"})

        device.refresh_from_db()
        if hidden_twin:
            assert stored_mapping_for_test(device) == 6567
            assert _messages(response, "error") == [_SERIAL_MISMATCH_MESSAGE]
            assert not any("legacy-twin-hidden" in text for text in _messages(response))
        else:
            assert stored_mapping_for_test(device) == {SERVER_KEY: 6567}
            assert _messages(response, "error") == []

    def test_a_hidden_twin_written_under_the_row_lock_refuses_the_conversion(
        self, client, librenms_server, monkeypatch
    ):
        """The first serial check passes; a twin that appears after the Device row lock must refuse the locked check."""
        from netbox_librenms_plugin.views.sync.device_fields import _SERIAL_MISMATCH_MESSAGE
        from dcim.models import Device

        from netbox_librenms_plugin import transactions
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        device = make_device("legacy-late-twin-target", serial="LATE-TWIN", librenms_cf=6568)
        librenms_server.device_info_response(device_id=6568, hostname=device.name, serial="LATE-TWIN")
        client.force_login(
            make_user_with_perms(
                "legacy-late-twin-user", [("view", Device), ("change", Device)], constraints={"name": device.name}
            )
        )
        lock = transactions.lock_and_snapshot

        def lock_then_twin(queryset):
            # The timing seam: another writer stores the twin while the view holds the Device row lock.
            row = lock(queryset)
            make_device("legacy-late-twin-hidden", serial="S/N LATE-TWIN")
            return row

        monkeypatch.setattr(transactions, "lock_and_snapshot", lock_then_twin)

        response = _post(client, "convert_legacy_librenms_id", device, {"object_type": "device"})

        device.refresh_from_db()
        assert stored_mapping_for_test(device) == 6568
        assert _messages(response, "error") == [_SERIAL_MISMATCH_MESSAGE]
        assert not any("legacy-late-twin-hidden" in text for text in _messages(response))

    @pytest.mark.parametrize("serial", [["MALFORMED-LEGACY"], {"serial": "MALFORMED-LEGACY"}, True])
    def test_a_malformed_librenms_serial_refuses_the_conversion(self, logged_in_client, librenms_server, serial):
        """A list or an object is no serial: the conversion refuses it, and the resolver never reads it."""
        from netbox_librenms_plugin.views.sync.device_fields import _SERIAL_MISMATCH_MESSAGE

        # The stored text is the coerced value, so only the type check can refuse the match.
        device = make_device("legacy-malformed-serial", serial=str(serial), librenms_cf=6569)
        librenms_server.device_info_response(device_id=6569, hostname=device.name, serial=serial)

        response = _post(logged_in_client, "convert_legacy_librenms_id", device, {"object_type": "device"})

        device.refresh_from_db()
        assert response.status_code == 302
        assert stored_mapping_for_test(device) == 6569
        assert _messages(response, "error") == [_SERIAL_MISMATCH_MESSAGE]

    @pytest.mark.parametrize("serial", ["['MALFORMED-LEGACY']", "{'serial': 'MALFORMED-LEGACY'}", "True"])
    def test_the_coerced_text_as_a_serial_confirms_the_conversion(self, logged_in_client, librenms_server, serial):
        """The control of the malformed case: the same text, sent as a string, is a serial."""
        device = make_device("legacy-coerced-serial", serial=serial, librenms_cf=6570)
        librenms_server.device_info_response(device_id=6570, hostname=device.name, serial=serial)

        response = _post(logged_in_client, "convert_legacy_librenms_id", device, {"object_type": "device"})

        device.refresh_from_db()
        assert stored_mapping_for_test(device) == {SERVER_KEY: 6570}
        assert _messages(response, "error") == []

    def test_vm_conversion_skips_the_device_serial_gate(self, logged_in_client, librenms_server):
        vm = make_vm("legacy-convert-vm")
        seed_stored_mapping(vm, 6563, save=True)
        librenms_server.device_info_response(device_id=6563, hostname=vm.name, serial="REMOTE-SERIAL")

        _post(
            logged_in_client,
            "convert_legacy_librenms_id",
            vm,
            {"object_type": "virtualmachine"},
        )

        vm.refresh_from_db()
        assert stored_mapping_for_test(vm) == {SERVER_KEY: 6563}

    @pytest.mark.parametrize(
        ("value", "fragment"),
        [
            ({SERVER_KEY: 6564}, "already in the server-scoped"),
            (True, "not a valid integer"),
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
        assert stored_mapping_for_test(device) == value
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
        assert stored_mapping_for_test(device) == 6565
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
        assert stored_mapping_for_test(device) == 6566
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
        assert stored_mapping_for_test(device) == 6567
        assert any("ambiguous" in text for text in _messages(response, "error"))

    @pytest.mark.parametrize(("converting", "owning"), [("device", "vm"), ("vm", "device")])
    def test_an_owner_of_the_other_model_blocks_conversion(self, logged_in_client, librenms_server, converting, owning):
        """A Device and a VM share one identity space, so an owner of either model refuses the conversion."""
        from core.models import ObjectChange
        from django.contrib.contenttypes.models import ContentType

        librenms_id = 6570 if converting == "device" else 6571
        if converting == "device":
            obj = make_device(f"legacy-cross-{converting}", serial="LEGACY-CROSS", librenms_cf=librenms_id)
        else:
            obj = make_vm(f"legacy-cross-{converting}")
            seed_stored_mapping(obj, librenms_id, save=True)
        if owning == "device":
            make_device(f"legacy-cross-owner-{owning}", librenms_cf={SERVER_KEY: librenms_id})
        else:
            owner = make_vm(f"legacy-cross-owner-{owning}")
            seed_mapping(owner, SERVER_KEY, own=librenms_id)
        librenms_server.device_info_response(device_id=librenms_id, hostname=obj.name, serial="LEGACY-CROSS")
        changes = ObjectChange.objects.filter(
            changed_object_type=ContentType.objects.get_for_model(obj), changed_object_id=obj.pk
        )
        logged_before = changes.count()

        response = _post(
            logged_in_client,
            "convert_legacy_librenms_id",
            obj,
            {"object_type": "device" if converting == "device" else "virtualmachine"},
        )

        obj.refresh_from_db()
        assert stored_mapping_for_test(obj) == librenms_id
        assert _messages(response, "error") == [
            f"LibreNMS ID {librenms_id} is already assigned to another NetBox object. "
            f"Cannot convert the legacy ID for server '{SERVER_KEY}'."
        ]
        assert not _messages(response, "success")
        assert changes.count() == logged_before

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

    @transactional_db_with_all_apps()
    @pytest.mark.parametrize(
        "view_name",
        [
            "update_device_name",
            "update_device_serial",
            "update_device_type",
            "update_device_platform",
        ],
    )
    def test_a_device_deleted_during_the_live_lookup_is_reported_as_gone(
        self,
        logged_in_client,
        librenms_server,
        view_name,
    ):
        """The mock server's thread deletes the device in its own session, so the view's locked write finds no row."""
        from core.models import ObjectChange
        from dcim.models import Device, DeviceType, Platform
        from django.contrib.contenttypes.models import ContentType
        from django.db import connections

        device = _linked_device(f"deleted-{view_name}".replace("_", "-"), 6573)
        new_type = DeviceType.objects.create(
            manufacturer=device.device_type.manufacturer, model="Deleted Lookup Router", slug="deleted-lookup-router"
        )
        new_platform = Platform.objects.create(name="deleted-lookup-os", slug="deleted-lookup-os")
        live = {
            "device_id": 6573,
            "hostname": "renamed-during-lookup",
            "sysName": "renamed-during-lookup",
            "serial": "LOOKUP-SERIAL",
            "hardware": new_type.model,
            "os": new_platform.name,
        }

        def delete_then_answer(**_request):
            try:
                Device.objects.filter(pk=device.pk).delete()
            finally:
                connections.close_all()
            return 200, {"status": "ok", "devices": [live]}

        librenms_server.register("/api/v0/devices/6573", delete_then_answer)

        response = _post(logged_in_client, view_name, device)

        assert _messages(response) == ["Device no longer exists."]
        assert _messages(response, "error") == ["Device no longer exists."]
        assert response.status_code == 302
        assert response.url == f"{_url('device_librenms_sync', device.pk)}?server_key={SERVER_KEY}"
        assert not Device.objects.filter(pk=device.pk).exists()
        device_type = ContentType.objects.get_for_model(Device)
        assert not ObjectChange.objects.filter(changed_object_type=device_type, changed_object_id=device.pk).exists()

    @transactional_db_with_all_apps()
    @pytest.mark.parametrize(
        ("view_name", "saved_model", "data"),
        [
            ("update_device_serial", "device", {}),
            ("convert_legacy_librenms_id", "device", {"object_type": "device"}),
            (
                "create_and_assign_platform",
                "device",
                {"platform_name": "Wrapped Existing Platform", "librenms_os": "ios"},
            ),
            ("create_and_assign_platform", "platform", {"platform_name": "Wrapped New Platform", "librenms_os": "ios"}),
        ],
        ids=["field-write", "legacy-convert", "platform-assign", "platform-create"],
    )
    def test_a_lock_conflict_wrapped_in_a_validation_error_gives_the_try_again_answer(
        self, logged_in_client, librenms_server, view_name, saved_model, data
    ):
        """NetBox raises a ValidationError ``from None`` over its own lock conflict (ltree save); the middleware answers it."""
        from dcim.models import Device, Platform
        from django.contrib.auth import get_user_model
        from django.core.exceptions import ValidationError
        from django.db import OperationalError
        from django.db.models.signals import pre_save

        from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row_nowait

        device = make_device(f"wrapped-{view_name}-{saved_model}", serial="WRAPPED-SERIAL", librenms_cf=6574)
        if view_name == "update_device_serial":
            seed_stored_mapping(device, {SERVER_KEY: 6574}, save=True)
        Platform.objects.create(name="Wrapped Existing Platform", slug="wrapped-existing-platform")
        live_serial = "NEW-SERIAL" if view_name == "update_device_serial" else "WRAPPED-SERIAL"
        librenms_server.device_info_response(device_id=6574, hostname=device.name, serial=live_serial)
        user_model = get_user_model()
        user_pk = int(logged_in_client.session["_auth_user_id"])
        sender = {"device": Device, "platform": Platform}[saved_model]

        def save_meets_a_wrapped_conflict(**_kwargs):
            try:
                lock_row_nowait(user_model, user_pk)
            except OperationalError:
                raise ValidationError("The hierarchy was modified concurrently; please retry.") from None

        pre_save.connect(save_meets_a_wrapped_conflict, sender=sender, weak=False)
        try:
            with second_connection() as other:
                lock_row(other, user_model, user_pk)
                response = _post(logged_in_client, view_name, device, data)
        finally:
            pre_save.disconnect(save_meets_a_wrapped_conflict, sender=sender)

        device.refresh_from_db()
        assert response.status_code == 302
        assert messages_on(response.wsgi_request) == [("error", TRY_AGAIN_MESSAGE)]
        assert (device.serial, device.platform_id) == ("WRAPPED-SERIAL", None)
        assert stored_mapping_for_test(device) in (6574, {SERVER_KEY: 6574})
        assert not Platform.objects.filter(name="Wrapped New Platform").exists()


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

    def test_model_and_url_helpers_distinguish_devices_from_vms(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine
        from netbox_librenms_plugin.views.sync.device_fields import _sync_model, _sync_url_name

        assert _sync_model("device") is Device
        assert _sync_model("vm") is VirtualMachine
        assert _sync_url_name("device").endswith("device_librenms_sync")
        assert _sync_url_name("vm").endswith("vm_librenms_sync")
